"""SLCE3-only primary action proof and offline parent integration research.

Original public receipts are immutable. This module cannot publish a model,
change an inventory, manufacture cash dates, or fit a price adjustment factor.
"""
import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import io
import json
import math
from pathlib import Path
import sys
import urllib.request

from pypdf import PdfReader

from .bulk_pending_sources import PublicSourceCollector
from .pending_all_prices import (
    CACHE, START, compact, digest, encoded, primary_index_candidates, read_source,
    source, verified_native_history,
)


MARKET = Path(__file__).parent / 'data/market'
IDENTITY = {'ticker': 'SLCE3', 'cvm': 20745, 'share_class': 'ON', 'isin': 'BRSLCEACNOR2'}
CUTOFF = date(2026, 9, 30)
ISSUER_ID = 'a975c39b-3eca-4ad8-9330-2c0a0b8d1060'
PUBLICATIONS = 'https://ri.slcagricola.com.br/informacoes-aos-investidores/publicacoes-cvm/'
DISCOVERY_PAGES = (
    'https://radardeadministradores.com.br/empresa_slce3',
    'https://homolog.investnews.com.br/financas/slc-aprova-r-601-milhoes-em-dividendos-e-aumento-de-capital/',
)
DOCUMENTS = {
    454573: 'e59acb9da1594861bff8585a9329293673c25cfccc36ccb66184a118c80a41ba',
    608350: '8a5308d733ce5e5fddfd56cf71533170e9f009dfdd2613d50fcefc28988bd6b0',
    616714: '06e390665b7fabbf045fc9de1c26594cd898a231a91f66ab20e13997937bb82a',
    697726: '328e11cedd222b606ca85fdbd0e6be28843022b74a59997ac24b1aa3be8037d7',
    987354: 'c80b4955e34e544a4010c8b6ddac279c8b8996b1d2caf7ff63e60cd6a43497cf',
}
RI_SPLIT_SHA = '22ce60e11698c2bd26279a35d303f20457878f0b4914c2d546cf1dd26a2c5e7f'
HISTORY_SHA = 'f690a707fab4a7d40cf5730bd6e9766b10bba10d39b5653b5d61ebc3e4f19efe'
SUPPLEMENT_SHA = '6ce12ada60c3be54e54c28bb223f7973abf758b7a2360e443851dbad4effdcf6'
QUOTE_LABEL = 'issuer_published_Enfoque_raw_history_plus_retained_cached_exchange'


@dataclass(frozen=True)
class PrimaryAction:
    kind: str
    approval_date: str
    record_date: str
    ex_date: str | None
    credit_date: str | None
    new_shares: int
    per_existing_shares: int
    citations: tuple
    credit_terms: str | None = None

    def __post_init__(self):
        _require(self.kind in ('stock_bonus', 'stock_split')
                 and type(self.new_shares) is int and self.new_shares > 0
                 and type(self.per_existing_shares) is int and self.per_existing_shares > 0,
                 'SLCE_invalid_native_share_action')
        _require(date.fromisoformat(self.approval_date) <= date.fromisoformat(self.record_date),
                 'SLCE_action_date_order_invalid')
        if self.ex_date is not None:
            _require(date.fromisoformat(self.record_date) < date.fromisoformat(self.ex_date),
                     'SLCE_record_must_not_be_relabelled_ex_date')
        if self.credit_date is not None:
            _require(date.fromisoformat(self.credit_date) >= date.fromisoformat(self.record_date),
                     'SLCE_credit_date_order_invalid')

    @property
    def total_factor(self):
        numerator = self.new_shares + self.per_existing_shares if self.kind == 'stock_bonus' else self.new_shares
        return Fraction(numerator, self.per_existing_shares)

    def core_action(self):
        _require(self.ex_date is not None, 'SLCE_primary_ex_date_unverified_2023_split')
        return {'date': self.ex_date, 'ratio': float(self.total_factor)}

    def describe(self):
        return {**asdict(self), 'total_factor_numerator': self.total_factor.numerator,
                'total_factor_denominator': self.total_factor.denominator,
                'total_factor_decimal': str(Decimal(self.total_factor.numerator) /
                                            Decimal(self.total_factor.denominator)),
                'identity': dict(IDENTITY), 'payment_date': None,
                'not_a_cash_distribution': True, 'factor_fitted': False}


@dataclass(frozen=True)
class ActionProof:
    actions: tuple[PrimaryAction, ...]
    sources: tuple[dict, ...]
    blockers: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def complete(self):
        return not self.blockers

    def core_actions(self):
        _require(self.complete, self.blockers[0] if self.blockers else 'SLCE_incomplete_action_proof')
        return [a.core_action() for a in self.actions]

    def describe(self):
        return {'identity': dict(IDENTITY), 'cutoff': CUTOFF.isoformat(),
                'actions': [a.describe() for a in self.actions], 'sources': list(self.sources),
                'complete': self.complete, 'blockers': list(self.blockers), 'notes': list(self.notes),
                'publication_equals_retrieval': False, 'network_requests': 0,
                'model_admission_granted': False}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def exact_scope(identity, cutoff):
    _require(isinstance(identity, dict) and type(identity.get('cvm')) is int
             and all(identity.get(k) == v for k, v in IDENTITY.items()),
             'SLCE_exact_CVM_class_ISIN_identity_required')
    _require(type(cutoff) is date and cutoff == CUTOFF, 'SLCE_exact_reviewed_cutoff_required')


def source_view(receipt, role, *, published_at=None):
    return {**{k: receipt.get(k) for k in (
                'file', 'sha256', 'bytes', 'url', 'retrieved_at', 'published_at')},
            'published_at': published_at if published_at is not None else receipt.get('published_at'),
            'role': role, 'access': 'existing_cache_read_only',
            'point_in_time_archive_verified': False}


def receipts(root):
    return [json.loads(p.read_bytes()) for p in (Path(root) / CACHE).glob('response-*.json')]


def cached(root, *, url=None, sha256=None):
    rows = [r for r in receipts(root) if (url is None or r['url'] == url)
            and (sha256 is None or r['sha256'] == sha256)]
    _require(len(rows) == 1, 'SLCE_original_receipt_missing_or_ambiguous')
    receipt, = rows
    body = read_source(root, receipt)
    _require(receipt['status'] == 200 and not receipt['error'], 'SLCE_source_not_accessible')
    return body, receipt


def pdf_pages(body):
    _require(body.startswith(b'%PDF'), 'SLCE_expected_original_PDF_not_ENET_HTML')
    return [p.extract_text() or '' for p in PdfReader(io.BytesIO(body)).pages]


def _citation(pages, receipt, page, field, checks):
    value = compact(pages[page - 1])
    _require(all(compact(t) in value for t in checks), 'SLCE_primary_semantic_field_mismatch_' + field)
    return {'file': receipt['file'], 'sha256': receipt['sha256'], 'url': receipt['url'],
            'page_one_based': page, 'field': field, 'native_text_checks': list(checks)}


def proof_loader(exactidentity, cutoff, *, root=MARKET):
    """Return typed primary readings; an unknown ex-date blocks core_actions().

    The split notice supplies D+3 credit terms, not an ex-date. No provider
    observation, next business day, price jump, or ratio fit fills that field.
    """
    exact_scope(exactidentity, cutoff)
    rows = primary_index_candidates(root, {20745}, sequences=set(DOCUMENTS))
    _require(len(rows) == len(DOCUMENTS), 'SLCE_exact_IPE_rows_missing')
    index_receipts = json.loads((Path(root) / 'raw/primary-research/cvm-documents/index-sources.json').read_bytes())
    documents, sources = {}, {}
    for row in rows:
        sequence = int(row['url'].split('numSequencia=')[1].split('&')[0])
        body, receipt = cached(root, url=row['url'], sha256=DOCUMENTS[sequence])
        native = row['index_row']
        _require(native['Codigo_CVM'] == '20745' and native['CNPJ_Companhia'] == '89.096.457/0001-55'
                 and date.fromisoformat(row['published_at']) <= cutoff
                 and native['Link_Download'] == receipt['url'], 'SLCE_IPE_identity_or_publication_mismatch')
        indexes = [r for r in index_receipts if r['sha256'] == row['index_source']['sha256']
                   and r['file'] == row['index_source']['file']]
        _require(len(indexes) == 1, 'SLCE_IPE_index_receipt_missing')
        index, = indexes
        read_source(root, index)
        sources[index['file']] = source_view(index, 'primary_CVM_IPE_index')
        sources[receipt['file']] = {
            **source_view(receipt, 'primary_CVM_non_cash_action', published_at=row['published_at']),
            'publication_basis': 'CVM_IPE_Data_Entrega_not_HTTP_retrieval',
            'index_file': index['file'], 'index_sha256': index['sha256'],
            'index_member': row['member'], 'index_csv_line': row['csv_line'],
            'index_row': native,
        }
        documents[sequence] = pdf_pages(body), receipt

    p21, s21 = documents[454573]
    first = PrimaryAction('stock_bonus', '2021-12-30', '2021-12-30', '2022-01-03', '2022-01-05', 1, 10, (
        _citation(p21, s21, 1, 'bonus_ratio_ON', ('1 (uma) nova acao para cada 10 (dez) acoes ordinarias',)),
        _citation(p21, s21, 1, 'record_ex_credit', ('acoes em 30/12/2021', 'a partir do dia 03/01/2022',
                                               'posicoes dos acionistas em 05/01/2022')),
    ))
    p23, s23 = documents[616714]
    dates23, sd23 = documents[608350]
    bonus = PrimaryAction('stock_bonus', '2023-04-27', '2023-05-08', '2023-05-09', None, 1, 10, (
        _citation(p23, s23, 1, 'approval_date_and_issuer', ('89.096.457/0001-55', '27 de abril de 2023')),
        _citation(p23, s23, 6, 'approved_not_proposed', ('II. Aprovado', 'aumento do capital social')),
        _citation(p23, s23, 7, 'approved_ON_bonus_ratio', ('21.242.259', 'novas acoes ordinarias',
                                                        '1 nova acao para cada 10 acoes')),
        _citation(dates23, sd23, 1, 'record_and_ex_dates', ('08/05/2023 (data -base)',
                                                         '09/05/2023 ex -direito',
                                                         'caso os acionistas aprovem')),
    ))
    split_pages, split_receipt = documents[697726]
    ri_body, ri_receipt = cached(root, sha256=RI_SPLIT_SHA)
    configuration, configuration_receipt = cached(root, url=PUBLICATIONS)
    _require(ISSUER_ID in configuration.decode('utf-8')
             and ri_receipt['url'].startswith('https://api.mziq.com/mzfilemanager/v2/d/' + ISSUER_ID + '/'),
             'SLCE_original_RI_issuer_configuration_mismatch')
    ri_pages = pdf_pages(ri_body)
    sources[configuration_receipt['file']] = source_view(configuration_receipt, 'issuer_published_filemanager_configuration')
    sources[ri_receipt['file']] = {**source_view(ri_receipt, 'primary_RI_split_notice'),
                                 'document_date': '2023-12-13', 'publication_date_unknown': True}
    supplement_body, supplement_source = source(root, 'raw/expanded/b3-supplement-SLCE.json')
    _require(supplement_source['sha256'] == SUPPLEMENT_SHA, 'SLCE_cached_exchange_original_changed')
    supplement, = json.loads(supplement_body)
    _require(supplement['codeCVM'] == '20745' and supplement['code'] == 'SLCE',
             'SLCE_cached_exchange_identity_mismatch')
    event, = [e for e in supplement['stockDividends'] if e['isinCode'] == IDENTITY['isin']
              and e['label'] == 'DESDOBRAMENTO']
    _require(event['lastDatePrior'] == event['approvedOn'] == '13/12/2023'
             and Decimal(event['factor'].replace(',', '.')) == 100,
             'SLCE_cached_exchange_split_terms_mismatch')
    sources[supplement_source['file']] = {**supplement_source, 'role': 'existing_cached_exchange_record_date',
        'url': None, 'URL_not_inferred_from_filename': True}
    split = PrimaryAction('stock_split', '2023-12-13', '2023-12-13', None, None, 2, 1, (
        _citation(split_pages, split_receipt, 2, 'approved_split_ON_ratio', (
            'I. Aprovada', 'cada 1 (uma) acao ordinaria existente a corresponder a 2 (duas) acoes ordinarias')),
        _citation(ri_pages, ri_receipt, 1, 'non_calendar_credit_terms', ('desdobramento de acoes em D+3',)),
        {'file': supplement_source['file'], 'sha256': supplement_source['sha256'],
         'json_pointer': '/0/stockDividends/1', 'field': 'record_date', 'native': event},
    ), credit_terms='D+3; settlement transaction-relative, not a calendar payment or ex date')
    p25, s25 = documents[987354]
    final = PrimaryAction('stock_bonus', '2025-12-30', '2025-12-30', '2026-01-02', None, 1, 8, (
        _citation(p25, s25, 1, 'ON_bonus_ratio_and_ex_date', (
            '1 (uma) nova acao ordinaria para cada 8 (oito) acoes', 'a partir do dia 02/01/2026')),
        _citation(p25, s25, 1, 'record_on_approved_AGE_date', (
            'na data de realizacao da AGE', 'Porto Alegre, 30 de dezembro de 2025')),
    ))
    return ActionProof((first, bonus, split, final), tuple(sorted(sources.values(), key=lambda r: r['file'])),
        ('SLCE_primary_ex_date_unverified_2023_split',), (
            'The April 6 record/ex notice was conditional; the April 27 final minutes confirm approval of that increase.',
            '2023 bonus credit date remains unknown in these primary documents; no secondary date is substituted.',
            'December final minutes have an IPE reference-date typo 2013-12-13; signed body and delivery say 2023.',
            'The December issuer notice confirms 1:2 and D+3 but does not explicitly state an ex date.',
            'Cached exchange record 2023-12-13 is retained; Yahoo 2023-12-14 remains a provider observation.',
        ))


def raw_quote_evidence(root, identity, cutoff, existing_quotes):
    """Quote-only evidence. Does not authorize using unresolved share actions."""
    exact_scope(identity, cutoff)
    _, receipt = cached(root, sha256=HISTORY_SHA)
    page, bundle = receipt['discovery']['page'], receipt['discovery']['bundle']
    history = verified_native_history(root, dict(IDENTITY), dict(IDENTITY), receipt, page, bundle)
    quotes = {date.fromisoformat(r['date']): float(Decimal(r['native_close_decimal']))
              for r in history['rows'] if START <= date.fromisoformat(r['date']) <= cutoff}
    conflicts = [{'date': d.isoformat(), 'cached_exchange_native_close': value,
                  'Enfoque_native_close': quotes[d]}
                 for d, value in existing_quotes.items() if d in quotes
                 and not math.isclose(value, quotes[d], rel_tol=.001, abs_tol=.015)]
    # Neither favorable replacement nor discarded conflicting daily evidence.
    _require(not conflicts, 'SLCE_existing_exchange_native_quote_conflict')
    combined = {**quotes, **existing_quotes}
    sources = tuple(source_view(s, role) for s, role in (
        (receipt, 'independent_issuer_Enfoque_unadjusted_history'),
        (page, 'issuer_exact_stockinfo_ID_and_ticker_configuration'),
        (bundle, 'issuer_published_unadjusted_widget_request_template'),
    ))
    return {'quotes': combined, 'native_rows': history['rows'], 'inactive_rows': history['inactive_rows'],
            'sources': sources, 'proof': {'source_label': QUOTE_LABEL, 'adjusted': False,
                'independent_window_dates': len(quotes), 'all_native_dates_retained': True,
                'existing_exchange_quotes_retained': len(existing_quotes), 'conflicts': conflicts,
                'source_history_SHA256': HISTORY_SHA, 'ultimate_vendor_upstream_independence_verified': False,
                'native_prices_modified': False, 'model_admission_granted': False}}


def get_slce_independent_raw_quotes(root, identity, cutoff, observed_actions, existing_quotes, *, price_content=None):
    """Fail-closed parent hook; no candidate escapes missing primary dates."""
    proof = proof_loader(identity, cutoff, root=root)
    primary = proof.core_actions()
    window = [a for a in observed_actions if START <= date.fromisoformat(a['date']) <= cutoff]
    _require(window == primary, 'SLCE_provider_actions_disagree_with_primary')
    evidence = raw_quote_evidence(root, identity, cutoff, existing_quotes)
    return {**evidence, 'primary_actions': proof.describe(),
            'sources': tuple([*proof.sources, *evidence['sources']])}


def monthly_input_audit(content, native_rows, observed_actions):
    from .collect_market_data import month_end, shift_month
    from .share_basis import factor_after

    result, = json.loads(content)['chart']['result']
    _require(result['meta']['symbol'] == 'SLCE3.SA' and result['meta']['currency'] == 'BRL',
             'SLCE_monthly_input_identity_mismatch')
    _require(len(result['timestamp']) == len(result['indicators']['quote'][0]['close'])
             and len(set(result['timestamp'])) == len(result['timestamp']),
             'SLCE_daily_quote_length_or_duplicate_mismatch')
    native = {r['date']: r['native_close_decimal'] for r in native_rows}
    last = {}
    for stamp, value in zip(result['timestamp'], result['indicators']['quote'][0]['close']):
        observed = datetime.fromtimestamp(stamp, timezone.utc).date()
        if value is not None and START <= observed <= CUTOFF:
            _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                     'SLCE_invalid_native_Yahoo_close')
            key = month_end(observed.year, observed.month)
            if key not in last or observed > last[key][0]:
                last[key] = observed, value
    anchors = []
    for n in range(60, -1, -1):
        month = shift_month(CUTOFF, -n)
        observed, yahoo = last.get(month, (None, None))
        raw = native.get(observed.isoformat()) if observed else None
        factor = factor_after(observed, observed_actions, CUTOFF) if observed else None
        compatible = raw is not None and yahoo is not None and math.isclose(
            yahoo, float(Decimal(raw)) / factor, rel_tol=.006, abs_tol=.015)
        anchors.append({'month': month.isoformat(), 'actual_observed_on': observed.isoformat() if observed else None,
            'Yahoo_original_close': yahoo, 'Enfoque_original_raw_close_decimal': raw,
            'reported_provider_action_factor_diagnostic_only': factor,
            'passes_unchanged_epsilon': compatible})
    return {'required_anchors': 61, 'anchors': anchors, 'all_61_pass': all(a['passes_unchanged_epsilon'] for a in anchors),
            'relative_tolerance': .006, 'absolute_tolerance': .015, 'favorable_subset_selected': False,
            'action_date_proof_not_supplied_by_this_price_comparison': True}


def financial_trial(root, stock, candidate):
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials

    statements, audit, sources = prepare_financials(root, [stock], CUTOFF)
    result = {'annual_candidate_audit': [r for r in audit if r.get('kind') == 'candidate'],
              'statement_records': len(statements), 'DFP_audit': audit,
              'latest_annual_features_available': False, 'post_action_fiscal_basis_valid': False,
              'old_filings_fallback_used': False, 'EPS_rescaled': False,
              'financial_basis_start_modified': False, 'fully_eligible': False}
    if candidate is None:
        result['status'] = 'source_candidate_unavailable'
        result['would_require_fiscal_end_on_or_after'] = '2026-01-02'
        result['current_features_not_computed_from_unreconciled_cash'] = True
        return result, sources
    spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
    current = financial_features(statements, candidate['payments'], CUTOFF, spec)
    basis = date.fromisoformat(candidate['financial_basis_start'])
    training, coverage = stock_observations('SLCE3', candidate['prices'], candidate['payments'],
                                           statements, CUTOFF, spec=spec)
    filtered = [r for r in training if date.fromisoformat(r['as_of']) >= candidate['training_since']
                and date.fromisoformat(r['fiscal_end']) >= basis]
    result.update(latest_annual_features_available=current is not None,
        post_action_fiscal_basis_valid=bool(current and date.fromisoformat(current[1]['fiscal_end']) >= basis),
        financial_basis_start=basis.isoformat(), training_observations_before_normal_filters=len(training),
        training_observations_after_normal_filters=len(filtered), training_coverage=coverage,
        first_training_as_of=filtered[0]['as_of'] if filtered else None,
        last_training_as_of=filtered[-1]['as_of'] if filtered else None)
    if current:
        result['features'], filing = current
        result['latest_filing'] = {k: filing.get(k) for k in (
            'cvm', 'share_class', 'fiscal_end', 'published_at', 'filing_id',
            'financial_method', 'source_ref', 'source_file', 'profit_per_share', 'free_cash_per_share')}
    result['fully_eligible'] = result['latest_annual_features_available'] and result['post_action_fiscal_basis_valid']
    result['status'] = 'passed' if result['fully_eligible'] else 'blocked_by_unchanged_prepare_inventory_financial_gate'
    return result, sources


def isolated_parent_trial(root=MARKET):
    """Execute real _one and financial functions, without writing production.

    A second, explicitly non-admissible quote-only diagnostic reaches the cash
    and financial gates despite the still-incomplete primary split date proof.
    Its candidate is never returned as an admission-ready instrument.
    """
    from unittest.mock import patch

    from . import inventory_candidates as parent
    from .broad_stocks import split_price_history
    from .collect_market_data import shift_month
    from .market_inventory import load_inventory
    from .share_basis import factor_after

    root = Path(root)
    inventory = load_inventory(root)
    stock, = [r for r in inventory['stocks'] if r['ticker'] == 'SLCE3']
    exact_scope(stock, CUTOFF)
    scoped = {**inventory, 'stocks': [stock]}
    protected = {name: root / name for name in ('current.json', 'inventory/current.json')}
    protected.update({name: Path(__file__).with_name(name) for name in (
        'inventory_candidates.py', 'share_basis.py', 'inventory_financials.py',
        'prepare_inventory.py', 'collect_market_data.py')})
    before = {name: digest(p.read_bytes()) for name, p in protected.items()}
    proof = proof_loader(dict(IDENTITY), CUTOFF, root=root)
    quotes = raw_quote_evidence(root, dict(IDENTITY), CUTOFF, {})
    original_one, original_reconcile = parent._one, parent.reconcile_prices
    original_payments = parent.reconcile_payments
    calls, cash_checks = [], []
    content = (root / 'raw/expanded/SLCE3-prices.json').read_bytes()
    _, provider_actions = split_price_history(content, 'SLCE3', CUTOFF, allow_recent_actions=True)
    grid = monthly_input_audit(content, quotes['native_rows'], provider_actions)

    def no_network(*args, **kwargs):
        raise AssertionError('SLCE_offline_trial_attempted_network')

    def registered_one(row, cache, cutoff, checks):
        exact_scope(row, cutoff)
        for receipt in [*proof.sources, *quotes['sources']]:
            cache.register_research_evidence(receipt)
        checks['SLCE_primary_actions'] = proof.describe()
        checks['SLCE_quote_diagnostic_not_admission'] = quotes['proof']
        return original_one(row, cache, cutoff, checks)

    def strict_hook(content, ticker, actions, existing, cutoff, **kwargs):
        calls.append({'kind': 'strict_primary_action_hook', 'ticker': ticker})
        evidence = get_slce_independent_raw_quotes(root, dict(IDENTITY), cutoff, actions, existing,
                                                   price_content=content)
        return original_reconcile(content, ticker, actions, evidence['quotes'], cutoff, **kwargs)

    def diagnostic_hook(content, ticker, actions, existing, cutoff, **kwargs):
        _require(ticker == 'SLCE3' and actions == provider_actions, 'SLCE_diagnostic_provider_actions_changed')
        evidence = raw_quote_evidence(root, dict(IDENTITY), cutoff, existing)
        call = {'kind': 'quote_only_non_admissible_diagnostic', 'ticker': ticker,
                'coverage_start': kwargs.get('coverage_start', START).isoformat(),
                'primary_actions_complete': False, 'proof': evidence['proof']}
        calls.append(call)
        try:
            prices, matches, mode = original_reconcile(content, ticker, actions, evidence['quotes'], cutoff, **kwargs)
        except ValueError as exc:
            call.update(status='blocked_by_unchanged_price_gate', reason=str(exc))
            raise
        call.update(status='passed_unchanged_price_gate', independent_dates=len(matches), mode=mode,
                    required_monthly_prices=sum(START <= day <= cutoff for day in prices))
        return prices, matches, mode

    def observed_payment_gate(ticker, original, expected, actions, cutoff, **kwargs):
        start = kwargs.get('coverage_start', START)
        groups = {}
        for row in original.installments:
            groups.setdefault(date.fromisoformat(row['record_date']), []).append(row)
        required = {d for d in expected if start <= d <= cutoff}
        required.update(d for d, rows in groups.items() if any(
            start <= date.fromisoformat(r['payment_date']) <= cutoff for r in rows))
        bases = sorted({date.fromisoformat(a['date']) for a in actions})
        for record in sorted(required):
            rows = groups.get(record, [])
            reported = sum(r['gross_per_unit'] for r in rows)
            declared = expected.get(record)
            factors = {1.0}
            factors.update(factor_after(record, actions, b) for b in [*bases, cutoff] if b >= record)
            matches = [f for f in sorted(factors) if declared is not None
                       and math.isclose(reported, declared / f, rel_tol=.002, abs_tol=.00002)]
            cash_checks.append({'record_date': record.isoformat(), 'declared_cached_exchange_per_record_share': declared,
                'reported_installment_sum': reported, 'original_installments': rows,
                'permitted_reported_action_chain_factors': sorted(factors), 'matching_factors': matches,
                'passes_unchanged_cash_amount_epsilon': bool(matches),
                'relative_tolerance': .002, 'absolute_tolerance': .00002,
                'payment_date_modified': False, 'source_amount_replaced': False})
        return original_payments(ticker, original, expected, actions, cutoff, **kwargs)

    with patch.object(urllib.request.OpenerDirector, 'open', no_network), patch('socket.create_connection', no_network):
        baseline, baseline_pending, _ = parent.build_candidates(root, scoped, CUTOFF)
        with patch.object(parent, '_one', registered_one), patch.object(parent, 'reconcile_prices', strict_hook):
            strict, strict_pending, strict_sources = parent.build_candidates(root, scoped, CUTOFF)
        with patch.object(parent, '_one', registered_one), patch.object(parent, 'reconcile_prices', diagnostic_hook), \
                patch.object(parent, 'reconcile_payments', observed_payment_gate):
            diagnostic, diagnostic_pending, diagnostic_sources = parent.build_candidates(root, scoped, CUTOFF)
        _require(parent._one is original_one and parent.reconcile_prices is original_reconcile,
                 'SLCE_temporary_hooks_not_restored')
        _require(parent.reconcile_payments is original_payments, 'SLCE_temporary_payment_observer_not_restored')
        candidate = diagnostic[0] if diagnostic else None
        financial, financial_sources = financial_trial(root, stock, candidate)
    _require(not strict, 'SLCE_incomplete_primary_proof_unexpectedly_admitted')
    after = {name: digest(p.read_bytes()) for name, p in protected.items()}
    native_summary = None
    if candidate:
        native_summary = {k: candidate[k] for k in (
            'ticker', 'cvm', 'share_class', 'isin', 'share_actions', 'checks', 'financial_basis_start',
            'share_normalization', 'independent_cash_matches', 'independent_price_matches',
            'price_source_file', 'price_url', 'readiness_scope', 'financially_validated')}
        native_summary.update(
            monthly_model_inputs=sum(shift_month(CUTOFF, -60) <= d <= CUTOFF for d in candidate['prices']),
            payment_events=len(candidate['payments']), ledger=candidate['ledger'],
            training_since=candidate['training_since'].isoformat(),
            candidate_status='diagnostic_only_primary_date_and_financial_admission_not_granted')
    all_sources = {s['file']: s for s in [*strict_sources, *diagnostic_sources, *financial_sources,
                                         *proof.sources, *quotes['sources']]}
    outcomes = []
    for r in receipts(root):
        if ('numSequencia=' in r['url'] and any('numSequencia=' + str(n) + '&' in r['url']
            for n in (616722, 697792, 616714, 617162, 608350, 697726, 617442))) or r['url'] in (
                PUBLICATIONS, *DISCOVERY_PAGES,
                'https://homolog.investnews.com.br/robots.txt',
                'https://radardeadministradores.com.br/robots.txt',
                'https://ri.slcagricola.com.br/servicos-aos-investidores/central-de-downloads/',
                'https://ri.slcagricola.com.br/servicos-aos-investidores/perguntas-frequentes/') \
                or r['sha256'] == RI_SPLIT_SHA:
            original = read_source(root, r)
            outcomes.append({**source_view(r, 'inspected_acquisition_outcome'), 'status': r['status'],
                'error': r['error'], 'location': r['location'], 'PDF_original': original.startswith(b'%PDF'),
                'outcome': ('original_PDF_obtained_and_inspected' if original.startswith(b'%PDF') else
                            'ENET_HTML_not_document' if b'ENET Download' in original else
                            'HTML_or_robots_response_preserved_not_action_proof')})
    return {'schema_version': 1, 'scope': 'SLCE3_only_offline_parent_one_and_complete_financial_trial',
        'cutoff': CUTOFF.isoformat(), 'primary_proof': proof.describe(),
        'all_61_monthly_input_validation': grid, 'provider_actions_observed_only': provider_actions,
        'baseline_candidate_count': len(baseline), 'baseline_pending': baseline_pending,
        'strict_candidate_count': len(strict), 'strict_pending': strict_pending,
        'diagnostic_quote_only_candidate_count': len(diagnostic), 'diagnostic_pending': diagnostic_pending,
        'diagnostic_candidate': native_summary, 'financial_gates': financial, 'reconcile_calls': calls,
        'unchanged_cash_gate_groups': cash_checks,
        'cash_amount_or_scale_conflicts': [r for r in cash_checks if not r['passes_unchanged_cash_amount_epsilon']],
        'acquisition_outcomes': outcomes, 'sources': sorted(all_sources.values(), key=lambda r: r['file']),
        'hook_export': 'proof_loader(exactidentity, cutoff, root=MARKET) -> ActionProof',
        'parent_adapter_export': 'get_slce_independent_raw_quotes (fail-closed until primary split ex-date is obtained)',
        'network_requests_during_trial': 0, 'B3_network_requests': 0,
        'EPS_or_price_factors_fitted': False, 'source_primary_record_payment_dates_modified': False,
        'model_admissions': 0, 'publication_performed': False, 'production_files_written': False,
        'temporary_hooks_removed': True, 'protected_before': before, 'protected_after': after,
        'concurrent_external_changes': [name for name in before if before[name] != after[name]],
        'producer_sha256': digest(Path(__file__).read_bytes())}


def save_trial(root, report, workdir=None):
    body = encoded(report)
    target = Path(root) / CACHE / ('SLCE-isolated-' + digest(body) + '.json')
    if target.exists():
        _require(target.read_bytes() == body, 'SLCE_immutable_report_conflict')
    else:
        with target.open('xb') as stream:
            stream.write(body)
    if workdir is not None:
        workdir = Path(workdir)
        _require(workdir.is_dir(), 'SLCE_parent_workdir_missing')
        summary = {k: report[k] for k in (
            'scope', 'strict_candidate_count', 'strict_pending', 'financial_gates',
            'diagnostic_quote_only_candidate_count', 'diagnostic_pending', 'model_admissions',
            'publication_performed', 'network_requests_during_trial', 'concurrent_external_changes')}
        summary.update(report_file=target.resolve().as_posix(), report_sha256=digest(body),
                       primary_actions_complete=report['primary_proof']['complete'],
                       all_61_monthly_inputs_pass=report['all_61_monthly_input_validation']['all_61_pass'])
        summary_body = encoded(summary)
        summary_path = workdir / ('SLCE-isolated-summary-' + digest(summary_body) + '.json')
        if not summary_path.exists():
            with summary_path.open('xb') as stream:
                stream.write(summary_body)
    return target


def acquire(root):
    """One serial collector; requests require actual index or issuer links."""
    collector = PublicSourceCollector(root, 'prices')
    print('SLCE acquisition ready', flush=True)
    for line in sys.stdin:
        if line.strip() == 'quit':
            break
        try:
            request = json.loads(line)
            if isinstance(request, list):
                selected = primary_index_candidates(root, {20745}, sequences=set(request))
                jobs = [(r['url'], {'kind': 'cached_CVM_IPE_index', **{
                    k: r[k] for k in ('index_source', 'member', 'csv_line', 'index_row')}})
                        for r in selected]
            else:
                url, discovery = request['url'], request['discovery']
                if discovery.get('kind') == 'web_search_link_discovery':
                    _require(url in DISCOVERY_PAGES, 'SLCE_unreviewed_search_discovery_URL')
                    jobs = [(url, discovery)]
                    parent = None
                    parent_body = b''
                else:
                    parent_body, parent = cached(root, url=discovery['source_url'])
                if url.startswith('https://api.mziq.com/mzfilemanager/v2/d/' + ISSUER_ID + '/'):
                    _require(ISSUER_ID in parent_body.decode('utf-8')
                             and 'https://api.mziq.com/mzfilemanager' in parent_body.decode('utf-8'),
                             'SLCE_exact_issuer_published_filemanager_required')
                    _require(discovery.get('kind') == 'web_indexed_issuer_original',
                             'SLCE_actual_indexed_original_required')
                elif parent is not None:
                    _require(url in parent_body.decode('utf-8'), 'SLCE_literal_issuer_link_required')
                jobs = [(url, {**discovery, 'issuer_source': parent})]
            for url, discovery in jobs:
                receipt = collector.fetch(url, discovery=discovery)
                body = collector.body(receipt)
                print(json.dumps({'receipt': receipt}), flush=True)
                pages = pdf_pages(body) if body.startswith(b'%PDF') else []
                if pages:
                    print(json.dumps({'pages': [{'page': i + 1, 'text': t} for i, t in enumerate(pages)]}), flush=True)
                else:
                    print(body[:1000].decode('utf-8', errors='replace'), flush=True)
        except (ValueError, KeyError, OSError) as exc:
            print(json.dumps({'error': type(exc).__name__, 'reason': str(exc)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=MARKET)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--trial', action='store_true')
    parser.add_argument('--workdir', type=Path)
    args = parser.parse_args()
    if args.acquire:
        acquire(args.root)
    elif args.trial:
        report = isolated_parent_trial(args.root)
        output = save_trial(args.root, report, args.workdir)
        print(json.dumps({'report': output.resolve().as_posix(),
            'primary_actions_complete': report['primary_proof']['complete'],
            'monthly_inputs_all_61_pass': report['all_61_monthly_input_validation']['all_61_pass'],
            'strict_candidates': report['strict_candidate_count'],
            'diagnostic_only_candidates': report['diagnostic_quote_only_candidate_count'],
            'cash_conflicting_record_dates': [r['record_date'] for r in report['cash_amount_or_scale_conflicts']],
            'financial_audit_latest': report['financial_gates']['annual_candidate_audit']}))
    else:
        print(json.dumps(proof_loader(dict(IDENTITY), CUTOFF, root=args.root).describe(), ensure_ascii=True))


if __name__ == '__main__':
    main()
