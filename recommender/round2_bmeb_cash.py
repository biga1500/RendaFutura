"""Exact BMEB4 native gross/net group proof and isolated ordinary replay.

Original cash/price caches, core modules and published pointers are read-only.
Acquisition is confined to PublicSourceCollector's existing round2-actions scope.
"""

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import socket

import pdfplumber
from pypdf import PdfReader
from lxml import html

from . import round2_share_actions as actions
from . import cash_repair_proofs as repair
from .bulk_pending_sources import PublicSourceCollector


ROOT = actions.ROOT
CUTOFF = actions.CUTOFF
CACHE = actions.CACHE
IDENTITY = {'ticker': 'BMEB4', 'cvm': 1325, 'share_class': 'PN', 'isin': 'BRBMEBACNPR5'}
CNPJ = '17.184.037/0001-10'
ISSUER_NOTICE = ('https://api.mziq.com/mzfilemanager/v2/d/'
                 '33817f28-e2e7-40d0-892a-09ddba2e44d6/66aec612-dd3f-0c29-f8ea-2d670e370f5b')
ISSUER_HISTORY = 'https://ri.bancomercantil.com.br/informacoes-aos-investidores/historico-de-dividendos-e-jscp/'
NOTICE_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
              '&CodigoInstituicao=1&numProtocolo=1069710&numSequencia=594440&numVersao=1')
BOARD_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
             '&CodigoInstituicao=1&numProtocolo=1069707&numSequencia=594437&numVersao=1')
SOURCE_PINS = {
    NOTICE_URL: ('eac26916a456509f70b38941c69f3a36cb891df1ac1a9362428642e844cc0fbd',
                 '2ffd8f021af36283e9c0cc85cca4d4f32f7735fa3781997c97d43ef4254c1686'),
    BOARD_URL: ('98522eaa18719ebb597c468dac2c3574ce65de9843aa21eae4bc247a4a0a301b',
                'c5e33f0878e558228e69e6915203b83574be472227842ee5082e43d3f79ac10c'),
    ISSUER_NOTICE: ('eac26916a456509f70b38941c69f3a36cb891df1ac1a9362428642e844cc0fbd',
                    '068c08a83f28decb118f725db52ce6577f7689a6645a87c5c701e02095164138'),
    ISSUER_HISTORY: ('2417e6b2b38d394338a48516c38e341f6481b19a2de11015892d52b3e76c99b5',
                     '17f38add674be24ee750d87c025d599a72a6c839f6f56b5d1082d9013b04a550'),
}
PAYMENT_FILE = 'raw/expanded/BMEB4-payments.html'
PAYMENT_SHA = '24e182a95ef69e9af98a48c7d4f00e17f65b38f468f62e24d6567771edf91be3'
REPAIR_ID = 'BMEB4_594440_v1_PN_literal_gross_not_net'
GROUP = '2023-03-10'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def exact_scope(identity, cutoff):
    require(type(identity.get('cvm')) is int and {k: identity.get(k) for k in IDENTITY} == IDENTITY
            and type(cutoff) is date and cutoff == CUTOFF, 'bmeb_cash_exact_identity_and_cutoff_required')


def source(root, url):
    """Require the code-pinned transport receipt, not a fresh observed hash."""
    digest, receipt_sha = SOURCE_PINS[url]
    relative = CACHE + '/response-' + actions.sha(url.encode()) + '.json'
    receipt = json.loads(actions.read(root, relative, receipt_sha))
    require(receipt['url'] == url and receipt['sha256'] == digest
            and receipt['file'] == CACHE + '/body-' + digest + '.bin',
            'bmeb_cash_native_receipt_binding_changed')
    ref = {**{k: receipt[k] for k in ('url', 'file', 'sha256', 'bytes', 'retrieved_at')},
           'receipt_file': relative, 'receipt_sha256': receipt_sha, 'published_at': None,
           'authority': 'primary_CVM_or_literal_issuer_publication', 'settlement_verified': False}
    body = actions.checked_source(root, ref)
    return body, ref


def parse_notice(body, ref, document):
    """Read printed native gross and net columns, retaining the full ON/PN grid."""
    pages = [p.extract_text() for p in PdfReader(io.BytesIO(body)).pages]
    require(len(pages) == 1 and CNPJ in pages[0]
            and 'BANCO MERCANTIL DO BRASIL S.A.' in pages[0]
            and 'BMEB3; BMEB4' in pages[0], 'bmeb_cash_complete_exact_issuer_notice_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        found = pdf.pages[0].find_tables()
        require(len(found) == 1, 'bmeb_cash_unique_native_gross_net_table_required')
        raw_grid, bbox = found[0].extract(), list(found[0].bbox)
    grid = [[' '.join(cell.split()) for cell in row if cell is not None and cell.strip()]
            for row in raw_grid]
    require(len(grid) == 3 and all(len(row) == 3 for row in grid), 'bmeb_cash_whole_native_class_grid_required')
    header, on, pn = grid
    require([actions.normalized(v) for v in header] == [
        'acoes bmeb', 'valor bruto por acao', 'valor liquido por acao']
        and actions.compact(on[0]) == 'onbmeb3' and actions.compact(pn[0]) == 'pnbmeb4',
        'bmeb_cash_literal_class_and_gross_net_headers_required')

    def amount(cell):
        require(cell.startswith('R$ '), 'bmeb_cash_literal_BRL_column_required')
        value = Decimal(cell.removeprefix('R$ ').replace(',', '.'))
        require(value.is_finite() and value > 0, 'bmeb_cash_positive_printed_amount_required')
        return format(value, 'f')

    gross, net = amount(pn[1]), amount(pn[2])
    require((gross, net, amount(on[1]), amount(on[2])) == (
        '0.308633', '0.262338', '0.280576', '0.238490'), 'bmeb_cash_literal_native_amounts_changed')
    locators = [actions.clause(pages[0], fragment, ref, 1) for fragment in (
        'Juros sobre Capital Proprio relativos ao 2',
        'O pagamento sera efetuado em 17 de marco de 2023',
        'base acionaria do Banco em 10 de marco de 2023',
        'A partir de 13 de marco de 2023 (inclusive)',
        'Belo Horizonte, 07 de marco de 2023',
        'Imposto de Renda na Fonte a aliquota de 15%')]
    fact = {**IDENTITY, 'label': 'JCP', 'record_date': GROUP, 'ex_date': '2023-03-13',
        'payment_date': '2023-03-17', 'approval_date': '2023-03-07',
        'published_at': document['published_at'], 'gross_per_share_decimal': gross,
        'net_per_share_decimal': net, 'gross_reconstructed_from_net': False,
        'date_roles': {'record_date': 'explicit_data_com', 'ex_date': 'explicit_ex_proventos',
                       'payment_date': 'explicit_financial_payment_date_not_settlement_confirmation'},
        'source_locator': {'file': ref['file'], 'sha256': ref['sha256'], 'page': 1,
            'table': 0, 'native_class_row': 2, 'semantic_gross_column': 1, 'semantic_net_column': 2,
            'bbox': bbox, 'raw_grid': raw_grid, 'normalized_grid': grid, 'date_and_type_clauses': locators},
        'source_locator_id': 'primary:CVM594440:v1:BMEB4:PN:JCP:20230310'}
    return fact


def load_proof(folder, identity, cutoff=CUTOFF, *, cache=None):
    """Typed exact-record cash proof, with primary dates and both tax columns."""
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    exact_scope(identity, cutoff)
    root = Path(folder).resolve()
    cache = cache or _Cache(root, load_inventory(root))
    action_proof = actions.primary_actions(root, identity, cutoff)
    cached_cash = actions.packet(root)['targets'][IDENTITY['ticker']]['cash']
    require(cached_cash, 'bmeb_cash_original_independent_cash_pages_missing')
    for ref in cached_cash:
        actions.checked_source(root, ref)
    document, index_source = repair.index_document(cache, 2023, '594440', 1, 1325, CNPJ,
                                                   '2023-03-07', 'fato relevante')
    board_document, board_index = repair.index_document(cache, 2023, '594437', 1, 1325, CNPJ,
                                                        '2023-03-07', 'reuniao da administracao')
    body, notice_source = source(root, NOTICE_URL)
    issuer_body, issuer_source = source(root, ISSUER_NOTICE)
    require(body == issuer_body and notice_source['url'] == document['url'],
            'bmeb_cash_CVM_and_issuer_native_publications_disagree')
    fact = parse_notice(body, notice_source, document)
    board_body, board_source = source(root, BOARD_URL)
    board_pages = [p.extract_text() for p in PdfReader(io.BytesIO(board_body)).pages]
    require(len(board_pages) == 2 and CNPJ in board_pages[0], 'bmeb_cash_board_identity_changed')
    board_locators = [actions.clause(board_pages[1], value, board_source, 2) for value in (
        'valor liquido de R$ 0,238490 por acao ordinaria e R$ 0,262338 por acao preferencial',
        'posicao acionaria considerada para o pagamento sera 10 de marco de 2023',
        'disponibilizacao financeira ocorrera em 17 de marco de 2023')]
    history_body, history_source = source(root, ISSUER_HISTORY)
    tree = html.fromstring(history_body.decode('utf-8'))
    rows = [r for r in tree.xpath('//table//tr') if [
        ' '.join(c.text_content().split()) for c in r.xpath('./th|./td')] ==
        ['JCP', 'Preferencial', '0,308633', '17/03/2023']]
    require(len(rows) == 1, 'bmeb_cash_issuer_gross_history_corroboration_changed')
    table = rows[0].xpath('ancestor::table')[0]
    require('valor bruto' in actions.normalized(table.xpath('.//tr')[0].text_content()),
            'bmeb_cash_issuer_gross_history_header_required')
    history_locator = {'file': history_source['file'], 'sha256': history_source['sha256'],
        'xpath': tree.getroottree().getpath(rows[0]), 'literal': ' '.join(rows[0].text_content().split()),
        'header': ' '.join(table.xpath('.//tr')[0].text_content().split()), 'entitlement_date_inferred': False}
    sources = [notice_source, issuer_source, board_source, history_source, index_source, board_index,
               *action_proof['sources'], *cached_cash]
    for ref in (notice_source, issuer_source, board_source, history_source):
        native_receipt = actions.read(root, ref['receipt_file'], ref['receipt_sha256'])
        sources.append({'file': ref['receipt_file'], 'sha256': ref['receipt_sha256'],
                        'bytes': len(native_receipt), 'url': None, 'retrieved_at': None})
    repair.register(cache, sources)
    return {'identity': dict(IDENTITY), 'facts': [fact], 'sources': sources,
        'primary_actions': action_proof, 'contract': {
            'document': document, 'board_document': board_document, 'board_locators': board_locators,
            'issuer_history': history_locator, 'native_final_revision_verified': True,
            'gross_per_share_decimal': fact['gross_per_share_decimal'],
            'net_per_share_decimal': fact['net_per_share_decimal'],
            'source_error': 'secondary_payment_page_net_column_amount_presented_as_gross',
            'cached_exchange_gross_amount_is_correct': True, 'ON_rows_excluded_from_PN_group': True,
            'gross_reconstructed_from_net': False, 'amounts_derived_from_withholding_rate': False,
            'unknown_dates_inferred': False, 'original_sources_modified': False,
            'bank_settlement_verified': False, 'point_in_time_archive_verified': False}}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    """Copy one complete inspected group using a literal primary gross value."""
    exact_scope(identity, cutoff)
    proof = load_proof(cache.folder, identity, cutoff, cache=cache)
    current = [r for r in parsed['events'] if r.get('record_date') == GROUP]
    require(current and all(r.get('ticker') == IDENTITY['ticker'] for r in current),
            'bmeb_cash_parsed_group_security_mismatch')
    prior = parsed.get('exact_cash_repairs', {}).get(REPAIR_ID)
    if prior is not None:
        fact = proof['facts'][0]
        require(len(current) == 1 and all(current[0].get(k) == v for k, v in fact.items())
                and current[0].get('gross_per_share') == float(Decimal(fact['gross_per_share_decimal']))
                and prior['source_contract'] == proof['contract'],
                'bmeb_cash_reapplied_native_fact_or_contract_changed')
    return repair.apply_groups(parsed, identity, cutoff, proof, repair_id=REPAIR_ID,
        payment_sha=PAYMENT_SHA, groups={GROUP: {
            'expected_original': [{'label': 'JCP', 'payment_date': '2023-03-17',
                'gross_per_share': 0.262338, 'source_row': 'table:2:row:10'}],
            'facts': proof['facts']}})


def audit_groups(original, expected, action_list, cutoff=CUTOFF):
    """Audit every required group, including pre-window records paid in-window."""
    from .share_basis import factor_after
    start = actions.START
    groups = {}
    for row in original.installments:
        groups.setdefault(date.fromisoformat(row['record_date']), []).append(row)
    required = {day for day in expected if start <= day <= cutoff}
    required.update(day for day, rows in groups.items() if any(
        start <= date.fromisoformat(r['payment_date']) <= cutoff for r in rows))
    bases = [date.fromisoformat(a['date']) for a in action_list]
    audits = []
    for day in sorted(required):
        rows, declared = groups.get(day, []), expected.get(day)
        reported = sum(r['gross_per_unit'] for r in rows) if rows else None
        factors = {1.0}
        factors.update(factor_after(day, action_list, end) for end in [*bases, cutoff] if end >= day)
        matching = [f for f in sorted(factors) if declared is not None and reported is not None
                    and reported > 0 and math.isclose(reported, declared / f, rel_tol=.002, abs_tol=.00002)]
        audits.append({'record_date': day.isoformat(), 'independent_gross_amount': declared,
            'reported_installment_sum': reported, 'installments': deepcopy(rows),
            'documented_share_factors': sorted(factors), 'matching_factors': matching,
            'passes_unchanged_cash_gate_arithmetic': bool(matching)})
    conflicts = [r for r in audits if not r['passes_unchanged_cash_gate_arithmetic']]
    return {'required_groups': len(audits), 'all_groups': audits, 'conflicts': conflicts,
            'all_required_groups_checked': True, 'cash_tolerances_unchanged': {'relative': .002, 'absolute_BRL': .00002},
            'favorable_groups_selected': False, 'net_to_gross_reconstruction': False}


def _native_calendar(row, cache):
    from . import inventory_candidates as candidates
    from .cash_calendar_research import apply_research_calendar, deduplicate_independent_rows
    from .share_basis import b3_evidence
    _, codes = candidates._identity(row, cache, CUTOFF)
    rows = candidates._cash_pages(row['ticker'], codes, row['cvm'], cache)
    rows = candidates._reviewed_cash_view(row, cache, CUTOFF, rows, {})
    rows, _ = deduplicate_independent_rows(row, cache, CUTOFF, rows)
    expected, quotes = b3_evidence(rows, row['share_class'])
    primary = cache.primary.primary_events(row, CUTOFF) if cache.primary else {}
    original, _ = candidates._payments(row['ticker'], cache, CUTOFF, primary, identity=row)
    action_list = actions.primary_actions(cache.folder, row)['observed_actions']
    original = apply_research_calendar(row, cache, CUTOFF, original, rows, action_list)
    return original, expected, quotes, action_list


def replay(folder=ROOT):
    """Run ordinary _one after the typed parser-group repair; never patch gates."""
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from .collect_market_data import financial_features, shift_month, stock_observations
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    before = actions.protected_hashes(root)
    inventory = load_inventory(root)
    row, = [r for r in inventory['stocks'] if r['ticker'] == IDENTITY['ticker']]
    exact_scope(row, CUTOFF)
    cache = candidates._Cache(root, inventory)
    actions.read(root, PAYMENT_FILE, PAYMENT_SHA)
    original, expected, independent, observed_actions = _native_calendar(row, cache)
    original_audit = audit_groups(original, expected, observed_actions)
    require([r['record_date'] for r in original_audit['conflicts']] == [GROUP],
            'bmeb_cash_original_conflict_set_changed_requires_new_primary_proofs')
    checks, repair_calls, price_calls, cash_calls = {}, [], [], []
    old_inspector, old_prices, old_payments = (candidates.inspect_payment_page,
                                             candidates.reconcile_prices, candidates.reconcile_payments)
    old_one = candidates._one

    def inspected(body, ticker, cutoff, observed):
        parsed = old_inspector(body, ticker, cutoff, observed)
        if ticker == IDENTITY['ticker']:
            require(actions.sha(body) == PAYMENT_SHA, 'bmeb_cash_original_payment_bytes_changed')
            result = apply_exact_repairs(parsed, row, cutoff, cache)
            repair_calls.append(deepcopy(result['exact_cash_repairs'][REPAIR_ID]))
            return result
        return parsed

    def prices(content, ticker, action_list, quotes, cutoff, **kwargs):
        if ticker != IDENTITY['ticker']:
            return old_prices(content, ticker, action_list, quotes, cutoff, **kwargs)
        require(kwargs.get('coverage_start') in (None, actions.START), 'bmeb_cash_earlier_price_coverage_not_proven')
        evidence = actions.load_evidence(root, row, cutoff, action_list, quotes, content)
        actions.validate_primary_actions(root, row, cutoff, action_list)
        repair.register(cache, evidence['sources'])
        result = old_prices(content, ticker, action_list, evidence['raw_quotes'], cutoff,
                            **{**kwargs, 'evidence_provider': actions.PROVIDER})
        price_calls.append({'model_monthly_inputs': evidence['checks']['monthly_inputs'],
            'mode': result[2], 'independent_price_matches': len(result[1]),
            'all_original_independent_quotes': evidence['checks']['all_original_independent_quotes'],
            'whole_61_months_passed': True})
        checks['BMEB4_primary_action_and_native_price_proof'] = evidence['primary_actions']
        return result

    def observe_cash(ticker, history, cash, action_list, cutoff, **kwargs):
        audit = audit_groups(history, cash, action_list, cutoff)
        cash_calls.append(audit)
        # This observer delegates unchanged arguments to the actual cash gate.
        return old_payments(ticker, history, cash, action_list, cutoff, **kwargs)

    def forbidden(*args, **kwargs):
        raise RuntimeError('bmeb_cash_offline_replay_network_forbidden')

    result = {'identity': dict(IDENTITY), 'cutoff': CUTOFF.isoformat(), 'required_months': 61,
              'source_candidate': False, 'financial_current_pass': False, 'pending_reason': None}
    candidate = None
    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden):
        with patch.object(candidates, 'inspect_payment_page', inspected), \
                patch.object(candidates, 'reconcile_prices', prices), \
                patch.object(candidates, 'reconcile_payments', observe_cash):
            try:
                candidate = candidates._one(row, cache, CUTOFF, checks)
                result.update(source_candidate=True, ledger_rows=len(candidate['ledger']),
                    independent_cash_matches=len(candidate['independent_cash_matches']),
                    independent_price_matches=len(candidate['independent_price_matches']),
                    repaired_ledger_group=[r for r in candidate['ledger'] if r['record_date'] == GROUP])
            except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                result['pending_reason'] = str(error)
        # Still audit the full corrected calendar if a later, unrelated gate blocks.
        with patch.object(candidates, 'inspect_payment_page', inspected):
            corrected, corrected_expected, _, corrected_actions = _native_calendar(row, cache)
        corrected_audit = audit_groups(corrected, corrected_expected, corrected_actions)
        require(not corrected_audit['conflicts'], 'bmeb_cash_additional_conflict_requires_primary_notice')
        print(json.dumps({'stage': 'ordinary_financial_replay', 'source_candidate': candidate is not None}), flush=True)
        statements, diagnostics, financial_sources = prepare_financials(root, [row], CUTOFF)
        exact = [r for r in statements if r['cvm'] == row['cvm']
                 and r.get('share_class', row['share_class']) == row['share_class']
                 and r['published_at'] <= CUTOFF.isoformat()]
        latest = max(exact, key=lambda r: (r['fiscal_end'], int(r['version'])), default=None)
        current, observations, coverage = None, [], None
        if candidate is not None:
            spec = row['cvm'], row['share_class'], row['sector_code']
            current = financial_features(statements, candidate['payments'], CUTOFF, spec)
            observations, coverage = stock_observations(row['ticker'], candidate['prices'],
                                                       candidate['payments'], statements, CUTOFF, spec=spec)
            training = [r for r in observations if date.fromisoformat(r['as_of']) >= candidate['training_since']
                        and r['fiscal_end'] >= candidate['financial_basis_start']]
            result['financial_current_pass'] = (current is not None
                and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
            if not result['financial_current_pass']:
                result['pending_reason'] = 'ordinary_financial_current_or_action_basis_gate_unavailable'
            result['training_rows'] = len(training)
            result['training_filing_ids'] = sorted({r['filing_id'] for r in training})
        else:
            result['financial_pending_without_candidate_cash'] = True
        result['financial_replay'] = {'latest_annual_statement': latest, 'current': current,
            'financial_basis_start': candidate['financial_basis_start'] if candidate else None,
            'coverage': coverage, 'diagnostics': diagnostics, 'sources': financial_sources,
            'normal_prepare_financials_called': True, 'normal_financial_features_called': candidate is not None,
            'normal_stock_observations_called': candidate is not None,
            'EPS_imputed': False, 'financial_scopes_mixed': False,
            'current_features_taken_from_older_filing': False, 'unresolved_cash_treated_as_zero': False}
    require(candidates._one is old_one and candidates.inspect_payment_page is old_inspector
            and candidates.reconcile_prices is old_prices and candidates.reconcile_payments is old_payments,
            'bmeb_cash_replay_source_hooks_not_restored')
    require(actions.protected_hashes(root) == before, 'bmeb_cash_replay_pointer_changed')
    require(actions.sha((root / PAYMENT_FILE).read_bytes()) == PAYMENT_SHA, 'bmeb_cash_source_mutated')
    result.update(original_full_group_audit=original_audit, corrected_full_group_audit=corrected_audit,
        actual_cash_gate_calls=cash_calls, typed_cash_group_repairs=repair_calls, price_gate_calls=price_calls,
        checks=checks, source_receipts=cache.all_receipts(), protected_pointer_hashes=before,
        source_and_current_financial_pass=result['source_candidate'] and result['financial_current_pass'],
        gates_relaxed=False, cash_gate_instrumentation='read_only_observer_then_original_function_same_arguments',
        core_files_edited=False, original_caches_mutated=False, model_admission=False,
        network_calls=0, published_pointer_writes=0, hooks_restored=True)
    return actions.json_safe(result)


def acquire(root=ROOT):
    from .pending_all_prices import primary_index_candidates
    root = Path(root).resolve()
    collector = PublicSourceCollector(root, 'round2-actions')
    receipts = actions.receipt_index(root)
    candidates = primary_index_candidates(root, {1325}, {594440, 594437})
    requests = [(r['url'], {'kind': 'literal_CVM_published_cash_notice', **r}) for r in candidates]
    requests += [(ISSUER_NOTICE, {'kind': 'indexed_literal_issuer_cash_notice', 'cvm': 1325,
                                  'discovery_query': 'site.ri.bancomercantil.com.br 2023 0,308633'}),
                 (ISSUER_HISTORY, {'kind': 'issuer_published_gross_cash_history', 'cvm': 1325})]
    results = []
    for url, discovery in requests:
        prior = actions.existing(root, url, receipts)
        try:
            receipt = prior or collector.fetch(url, discovery=discovery)
            results.append(receipt)
        except ValueError as error:
            receipt = {'url': url, 'error': str(error)}
            results.append(receipt)
        print(json.dumps({k: receipt.get(k) for k in ('url', 'status', 'error', 'sha256')}), flush=True)
    return actions.save(root, 'bmeb-cash-acquisition', {'results': results, 'model_admission': False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market', type=Path, default=ROOT)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--replay', action='store_true')
    args = parser.parse_args(argv)
    if args.acquire:
        print(acquire(args.market))
    if args.report:
        require(args.report.name.startswith('actions-bmeb-cash-') and args.report.suffix == '.json',
                'bmeb_cash_report_outside_assigned_prefix')
        report = {'proof': load_proof(args.market, IDENTITY), 'replay': replay(args.market) if args.replay else None,
                  'producer_sha256': actions.sha(Path(__file__).read_bytes()),
                  'model_admission': False, 'original_source_caches_modified': False}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(actions.encoded(actions.json_safe(report)))
        print(json.dumps({'report': str(args.report), 'result': report['replay'] and {k: report['replay'][k]
              for k in ('source_candidate', 'financial_current_pass', 'pending_reason')}}))


if __name__ == '__main__':
    main()
