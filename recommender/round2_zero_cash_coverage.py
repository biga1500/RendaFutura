"""Isolated primary period coverage research for DESK3 and BMOB3.

Period observations are never exported as payment events or settlement proof.
No production importer or core gate is changed by this research module.
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from .bulk_pending_sources import PublicSourceCollector
from .inventory_financials import _rows


ROOT = Path(__file__).resolve().parent / 'data/market'
BASE = 'raw/primary-research/all-pending/round2-zero'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
START = date(2021, 9, 30)
CUTOFF = date(2026, 9, 30)
IDENTITIES = {
    'DESK3': {'ticker': 'DESK3', 'cvm': 26026, 'cnpj': '08.170.849/0001-15',
              'share_class': 'ON', 'isin': 'BRDESKACNOR2'},
    'BMOB3': {'ticker': 'BMOB3', 'cvm': 25500, 'cnpj': '09.042.817/0001-05',
              'share_class': 'ON', 'isin': 'BRBMOBACNOR7'},
}
SOURCES = {
    'desk-2021': ('DESK3', 'https://api.mziq.com/mzfilemanager/v2/d/18c2715d-fc34-4975-8be2-51e411bcd259/e56f3963-2ec1-d159-8f73-8d70cd48857d?origin=2'),
    'desk-first-payment-notice': ('DESK3', 'https://api.mziq.com/mzfilemanager/v2/d/18c2715d-fc34-4975-8be2-51e411bcd259/a17cfb57-53af-6a95-0589-91e2e56cbfdf'),
    'bmob-2021': ('BMOB3', 'https://api.mziq.com/mzfilemanager/v2/d/deed72f2-beae-4c6f-b423-db57178bae31/4cf24812-2689-77f8-f569-fb6b5db74630?origin=1'),
    'bmob-2022': ('BMOB3', 'https://api.mziq.com/mzfilemanager/v2/d/deed72f2-beae-4c6f-b423-db57178bae31/d337e273-1008-12a2-10d9-f16b50543de1?origin=1'),
    'bmob-2021-proposal': ('BMOB3', 'https://api.mziq.com/mzfilemanager/v2/d/deed72f2-beae-4c6f-b423-db57178bae31/31e0d4c3-38f9-308d-9517-c01189a51ff8?origin=1'),
    'desk-2q2021': ('DESK3', 'https://api.mziq.com/mzfilemanager/v2/d/18c2715d-fc34-4975-8be2-51e411bcd259/9ff054fe-378b-219e-6f66-f4a9f83147ac?origin=2'),
    'desk-1q2022': ('DESK3', 'https://api.mziq.com/mzfilemanager/v2/d/18c2715d-fc34-4975-8be2-51e411bcd259/c4938333-a3a4-8b1b-f995-d087ea496725?origin=1'),
    'desk-3q2022': ('DESK3', 'https://api.mziq.com/mzfilemanager/v2/d/18c2715d-fc34-4975-8be2-51e411bcd259/d43ed7d6-9354-1329-46eb-2e8170f9e6dd?origin=1'),
    'desk-native-2021': ('DESK3', 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=113293'),
    'bmob-native-2021': ('BMOB3', 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=112613'),
}
PINS = {
    'desk-2021': 'f7f9f436e287a2fc7fa51012ec67074cba9bca77b190e9c48ad92b73d4dff1f9',
    'desk-first-payment-notice': '207c37028217037ca5a2b9051488af0e063e6664a6a130ccdd6c9cf7d26bacb8',
    'bmob-2021': 'f95a489b0e3d9a575324660bd6b881055797deef6b3df040c65c05fbcaa1f2d3',
    'bmob-2022': '73c5b485f0bfcfbcab46719d1190c1ed39f725783779c47ca265d985343c26a9',
    'bmob-2021-proposal': 'fc8325eae5f456da4371c7bce0808ffcd7e169a1a67abcff503933975e45ab9a',
    'desk-2q2021': 'b27c8336acd278076b033a1eaf4a0dc494482360698f594b127369133159b720',
    'desk-1q2022': 'af19fdc97d399f6116d28b0d05370232bf1d4b6112823a04ad1a97dbcfceca6c',
    'desk-3q2022': 'a7522f10937c96627e6814cb154010682f67bfe6cf99a860fb7bbd3b8392fe90',
    'bmob-native-2021': '1724405552b7789c7b7d0d8e3d9198e07cdb436f3dd30239560aae8f202cdc35',
}
ANNUAL = 'raw/cvm-dfp-2021.zip'
ANNUAL_SHA = '64b8693b709538a0357c5179ae02231cb380d0000956ee57a873a21d4efda2e4'


class CoverageBasis(str, Enum):
    PAID_CASH = 'paid_cash'
    ENTITLEMENT = 'entitlement_and_opening_pending_rights'


@dataclass(frozen=True)
class PrimaryPeriodObservation:
    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    source_name: str
    pdf_page: int
    period_start: str
    period_end: str
    measure: str
    components: tuple[str, ...]
    scope: str
    literal: str
    reported_value: str | None = None
    reporting_unit_brl: int | None = None
    boundary_rights_resolved: bool = False
    usable_as_leading_window_coverage: bool = False


@dataclass(frozen=True)
class PrimaryPeriodCoverageProof:
    """Reviewed coverage, deliberately not a per-share cash event.

    Paid-cash and entitlement coverage have distinct meanings. Construction
    does not approve a source: the primary loader must verify literal evidence
    and the reviewer must explicitly approve the complete claim. This audit
    exports no approved instances because its observed claims are incomplete.
    """

    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    isin: str
    period_start: str
    period_end: str
    basis: CoverageBasis
    components: tuple[str, ...]
    scope: str
    source_refs: tuple[str, ...]
    primary_literal_review_verified: bool
    boundary_rights_resolved: bool
    gross_nonnetted: bool
    zero_basis: str
    point_in_time_archive_verified: bool = False
    bank_settlement_verified: bool = False


def leading_gap_decision(identity, first_actual_date, basis, verified_proofs, *, start=START):
    """Pure proposed gate semantics; it never edits dates or inserts events.

    Call only with proofs returned by a reviewed primary loader. Existing cash
    amount/date/class reconciliation and the 61 price anchors remain required.
    """
    _require(identity == IDENTITIES.get(identity.get('ticker')), 'round2_zero_exact_identity_required')
    _require(start == START and type(first_actual_date) is date
             and first_actual_date <= CUTOFF, 'round2_zero_fixed_window_required')
    _require(isinstance(basis, CoverageBasis), 'round2_zero_typed_coverage_basis_required')
    gap_end = first_actual_date - timedelta(days=1)
    _require(first_actual_date > start, 'round2_zero_no_leading_gap')
    intervals, reasons = [], []
    for proof in verified_proofs:
        _require(type(proof) is PrimaryPeriodCoverageProof, 'round2_zero_event_is_not_period_proof')
        if any(getattr(proof, key) != identity[key] for key in ('ticker', 'cvm', 'cnpj', 'share_class', 'isin')):
            reasons.append('wrong_issuer_or_security')
            continue
        if proof.basis != basis:
            continue
        if proof.scope != 'issuer_individual':
            reasons.append('issuer_individual_scope_not_proved')
            continue
        if not proof.primary_literal_review_verified or not proof.source_refs:
            reasons.append('literal_primary_review_unavailable')
            continue
        if set(proof.components) != {'dividends', 'jcp'}:
            reasons.append('both_components_not_explicit')
            continue
        if not proof.boundary_rights_resolved:
            reasons.append('opening_payables_or_pending_rights_unresolved')
            continue
        if not proof.gross_nonnetted or proof.zero_basis not in (
                'explicit_primary_no_payments_or_entitlements', 'explicit_reported_zero_at_precision'):
            reasons.append('gross_zero_not_explicit_or_aggregate_netted')
            continue
        left, right = date.fromisoformat(proof.period_start), date.fromisoformat(proof.period_end)
        _require(left <= right, 'round2_zero_invalid_proof_period')
        intervals.append((max(start, left), min(gap_end, right)))
    cursor, gaps = start, []
    for left, right in sorted(intervals):
        if right < cursor or left > gap_end:
            continue
        if left > cursor:
            gaps.append((cursor.isoformat(), (left - timedelta(days=1)).isoformat()))
        cursor = max(cursor, right + timedelta(days=1))
    if cursor <= gap_end:
        gaps.append((cursor.isoformat(), gap_end.isoformat()))
    return {'basis': basis.value, 'window_start': start.isoformat(),
            'first_actual_date': first_actual_date.isoformat(),
            'leading_gap_end': gap_end.isoformat(), 'leading_gap_covered': not gaps,
            'uncovered_intervals': gaps, 'rejected_proof_reasons': sorted(set(reasons)),
            'synthetic_cash_events': [], 'first_actual_event_moved': False,
            'required_monthly_price_anchors': 61, 'bank_settlement_verified': False,
            'model_admission': False}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _save(path, value):
    body = value if isinstance(value, bytes) else (
        json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == body, 'round2_zero_immutable_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def acquire(names, folder=ROOT):
    collector = PublicSourceCollector(folder, 'round2-zero')
    for name in names:
        ticker, url = SOURCES[name]
        receipt = collector.fetch(url, discovery={
            'kind': 'actual_official_issuer_document_search_result',
            'ticker': ticker, 'purpose': 'literal_2021_and_first_distribution_period_evidence',
            'source_name': name})
        _save(Path(folder) / BASE / f'source-{name}.json', receipt)
        print(name, receipt['status'], receipt['error'], receipt['bytes'], flush=True)
        if receipt['status'] == 200 and receipt['error'] is None:
            body = collector.body(receipt)
            if body.startswith(b'%PDF'):
                _save(Path(folder) / BASE / f'{name}-{_sha(body)}.pdf', body)


def document(name, folder=ROOT):
    folder = Path(folder)
    receipt = json.loads((folder / BASE / f'source-{name}.json').read_bytes())
    path = folder / receipt['file']
    _require(path.parent.resolve() == (folder / BASE).resolve() and not path.is_symlink()
             and path.name == 'body-' + receipt['sha256'] + '.bin', 'round2_zero_unsafe_source_path')
    body = path.read_bytes()
    _require(receipt['url'] == SOURCES[name][1]
             and receipt['status'] == 200 and receipt['error'] is None
             and receipt['sha256'] == PINS.get(name)
             and _sha(body) == receipt['sha256'] and len(body) == receipt['bytes'],
             'round2_zero_source_changed')
    if body.startswith(b'PK'):
        identity = IDENTITIES[SOURCES[name][0]]
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            _require(archive.testzip() is None, 'round2_zero_native_CRC_failed')
            xml_name = f'{identity["cvm"]:06d}DFP31-12-2021v1.xml'
            xml = ET.fromstring(archive.read(xml_name))
            _require(tuple(xml.findtext(field) for field in (
                'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
                'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) == (
                    f'{identity["cvm"]:06d}', re.sub(r'\D', '', identity['cnpj']), '1', '31/12/2021'),
                    'round2_zero_native_identity_mismatch')
            filing = '112613' if identity['ticker'] == 'BMOB3' else '113293'
            names = [n for n in archive.namelist()
                     if re.fullmatch(filing + f'_{identity["cvm"]:06d}_' + r'\d+\.pdf', n)]
            _require(len(names) == 1, 'round2_zero_native_PDF_ambiguous')
            body = archive.read(names[0])
            receipt = {**receipt, 'original_container': receipt['file'],
                'original_container_sha256': receipt['sha256'], 'original_zip_member': names[0],
                'file': f'{BASE}/{name}-{_sha(body)}.pdf', 'sha256': _sha(body), 'bytes': len(body)}
    _require(body.startswith(b'%PDF'), 'round2_zero_document_not_PDF')
    return receipt, body


def primary_observations(folder=ROOT):
    """Verify literal, component-limited observations. Approve no full proof."""
    from pypdf import PdfReader

    def observation(ticker, source, page, begin, end, measure, literal,
                    value=None, unit=None, scope='issuer_individual', components=('dividends',)):
        identity = IDENTITIES[ticker]
        return PrimaryPeriodObservation(ticker, identity['cvm'], identity['cnpj'],
            identity['share_class'], source, page, begin, end, measure, components,
            scope, literal, value, unit)

    observations = (
        observation('DESK3', 'desk-2q2021', 64, '2021-06-30', '2021-06-30',
            'explicit_non_distribution_statement_at_date_only',
            'Em 30 de junho de 2021 a Companhia n\u00e3o distribuiu dividendos.'),
        observation('DESK3', 'desk-2021', 15, '2021-12-31', '2021-12-31',
            'nonzero_dividend_payable_snapshot', 'Dividendos a pagar 21 1.482 255 1.554 255', '1482', 1000),
        observation('DESK3', 'desk-1q2022', 8, '2022-03-31', '2022-03-31',
            'nonzero_dividend_payable_snapshot', 'Dividendos a pagar 1.482 1.482 1.518 1.518', '1482', 1000),
        observation('DESK3', 'desk-3q2022', 8, '2022-09-30', '2022-09-30',
            'nonzero_dividend_payable_snapshot', 'Dividendos a pagar 1.482 1.482 1.518 1.518', '1482', 1000),
        observation('DESK3', 'desk-first-payment-notice', 1, '2022-12-22', '2022-12-22',
            'scheduled_payment_not_explicit_first_ever_payment',
            'O pagamento ser\u00e1 realizado em 22 de dezembro de 2022'),
        observation('DESK3', 'desk-first-payment-notice', 1, '2022-04-29', '2022-04-29',
            'explicit_record_date_for_known_distribution', 'data-base de 29 de abril de 2022 (inclusive)'),
        observation('BMOB3', 'bmob-native-2021', 9, '2021-01-01', '2021-12-31',
            'positive_individual_annual_dividends_paid', '6.03.03 Pagamento de dividendos -186.718 0 0', '186718', 1000),
        observation('BMOB3', 'bmob-native-2021', 63, '2021-04-26', '2021-04-26',
            'named_prewindow_payee_not_public_holder_cash',
            'Esse valor foi liquidado em 26 de abril de 2021.', '178232', 1000),
        observation('BMOB3', 'bmob-native-2021', 64, '2021-04-26', '2021-04-26',
            'named_prewindow_payee_not_public_holder_cash',
            'Esse valor foi liquidado em 26 de abril de 2021.', '8486', 1000),
        observation('BMOB3', 'bmob-native-2021', 5, '2021-12-31', '2021-12-31',
            'nonzero_combined_payable_snapshot', 'Dividendos e JCP a Pagar 17.895 8.486 0',
            '17895', 1000, components=('dividends', 'jcp')),
        observation('BMOB3', 'bmob-2022', 40, '2022-03-16', '2022-03-16',
            'reported_proposal_date_for_FY2021_dividend_not_record_date',
            'propostos pelo Conselho de Administra\u00e7\u00e3o em 16 de mar\u00e7o de 2022'),
    )
    readers, sources = {}, {}
    for item in observations:
        if item.source_name not in readers:
            receipt, body = document(item.source_name, folder)
            readers[item.source_name] = PdfReader(io.BytesIO(body))
            sources[item.source_name] = {key: receipt.get(key) for key in (
                'url', 'file', 'sha256', 'bytes', 'retrieved_at', 'published_at',
                'original_container', 'original_container_sha256', 'original_zip_member')}
            sources[item.source_name].update(authority='primary_issuer_or_CVM',
                use='literal_period_observation_not_payment_event',
                bank_settlement_verified=False, point_in_time_archive_verified=False)
        text = ' '.join(readers[item.source_name].pages[item.pdf_page - 1].extract_text().split())
        _require(' '.join(item.literal.split()) in text, 'round2_zero_primary_literal_changed')
    # The two explicit pre-window payments exhaust the reported annual dividend
    # line at its precision. This locates dividends, not absent JCP or all rights.
    _require(Decimal(observations[7].reported_value) + Decimal(observations[8].reported_value)
             == Decimal(observations[6].reported_value), 'round2_zero_BMOB_annual_cash_not_reconciled')
    return observations, sources


def annual_source_observations(folder=ROOT):
    body = (Path(folder) / ANNUAL).read_bytes()
    _require(_sha(body) == ANNUAL_SHA, 'round2_zero_annual_changed')
    result = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        masters = list(_rows(archive, 'dfp_cia_aberta_2021.csv'))
        for ticker, identity in IDENTITIES.items():
            master, = [r for r in masters if int(r['CD_CVM']) == identity['cvm']
                       and r['DT_REFER'] == '2021-12-31']
            _require(master['CNPJ_CIA'] == identity['cnpj'] and master['VERSAO'] == '1'
                     and master['ID_DOC'] == ('113293' if ticker == 'DESK3' else '112613'),
                     'round2_zero_annual_identity_changed')
            selected = {}
            for suffix in ('_DFC_MI_ind', '_BPP_ind'):
                member = 'dfp_cia_aberta' + suffix + '_2021.csv'
                selected[member] = [{k: r.get(k) for k in (
                    'CNPJ_CIA', 'CD_CVM', 'DT_REFER', 'VERSAO', 'GRUPO_DFP', 'ORDEM_EXERC',
                    'DT_INI_EXERC', 'DT_FIM_EXERC', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA',
                    'MOEDA', 'ESCALA_MOEDA')}
                    for r in _rows(archive, member) if int(r['CD_CVM']) == identity['cvm']
                    and r['ORDEM_EXERC'] == '\u00daLTIMO' and r['VERSAO'] == '1'
                    and re.search(r'(?i)dividend|capital pr.prio|jcp|jscp', r['DS_CONTA'])]
            result[ticker] = {'master': master, 'individual_distribution_rows': selected,
                'absence_of_any_paid_component_is_not_zero': True}
    return {'file': ANNUAL, 'sha256': ANNUAL_SHA, 'issuers': result,
            'retrieved_at': None, 'mtime_not_used_as_retrieval': True}


def legacy_gate_replay(folder=ROOT):
    """Read-only unmodified gates, with actual cached calendars and identities."""
    from .inventory_candidates import _Cache, _one, _payments
    from .market_inventory import load_inventory

    folder = Path(folder)
    inventory = load_inventory(folder)
    results = {}
    for ticker in IDENTITIES:
        row, = [r for r in inventory['stocks'] if r['ticker'] == ticker]
        _require(all(row[k] == IDENTITIES[ticker][k] for k in ('ticker', 'cvm', 'isin', 'share_class')),
                 'round2_zero_inventory_identity_changed')
        outputs = {}
        for name in ('_one', '_payments'):
            checks, cache = {}, _Cache(folder, inventory)
            try:
                value = (_one(row, cache, CUTOFF, checks) if name == '_one' else
                         _payments(ticker, cache, CUTOFF, identity=IDENTITIES[ticker]))
                outputs[name] = {'passed': value is not None, 'checks': checks}
            except ValueError as error:
                outputs[name] = {'passed': False, 'reason': str(error), 'checks': checks}
        results[ticker] = outputs
    return results


def get_zero_cash_coverage(folder=ROOT):
    """Typed handoff: verified partial observations; no approved gap proof."""
    observations, sources = primary_observations(folder)
    return {'observations': observations, 'approved_period_coverage_proofs': (),
            'sources': sources, 'synthetic_cash_events': (), 'model_admission': False}


def write_report(folder=ROOT, reports=REPORTS):
    from lxml import html

    folder, reports = Path(folder), Path(reports)
    before = legacy_gate_replay(folder)
    evidence = get_zero_cash_coverage(folder)
    annual = annual_source_observations(folder)
    after = legacy_gate_replay(folder)
    _require(before == after, 'round2_zero_unexpected_core_behavior_change')
    _save(reports / 'financial-zero-before.json', before)
    _save(reports / 'financial-zero-after.json', after)
    prior_path = REPORTS.parent / 'pending_recheck_20261025/pending-a-independent-report.json'
    prior_body = prior_path.read_bytes()
    _require(_sha(prior_body) == 'ccbe96ba077fc3448e6271a1616b0a43ffc1c25cf68812bfde99c74c0ac07da7',
             'round2_zero_prior_BMOB_report_changed')
    prior = next(row for row in json.loads(prior_body)['trials'] if row['identity']['ticker'] == 'BMOB3')
    bmob_events = prior['evidence_audit']['calendar']['events']
    _require(len(bmob_events) == 12 and sum(e['record_date'] is None for e in bmob_events) == 2,
             'round2_zero_prior_BMOB_calendar_changed')
    secondary = 'raw/primary-research/all-pending/round2-alt-cash/body-50ef01d6b3d7fb39478720fdceb8d104c986a9158e8cbe8ed96c91ea0448f822.bin'
    secondary_body = (folder / secondary).read_bytes()
    _require(_sha(secondary_body) == '50ef01d6b3d7fb39478720fdceb8d104c986a9158e8cbe8ed96c91ea0448f822',
             'round2_zero_DDM_original_changed')
    tree = html.fromstring(secondary_body, parser=html.HTMLParser(no_network=True))
    secondary_tables = [[[' '.join(c.text_content().split()) for c in row.xpath('./th|./td')]
                         for row in table.xpath('.//tr')] for table in tree.xpath('//table')]
    ddm = {'file': secondary, 'sha256': _sha(secondary_body),
           'url': 'https://origin.dadosdemercado.com.br/acoes/desk3/dividendos',
           'authority': 'secondary', 'approved_for_model': False,
           'adjusted_notice_literal_present': 'ajustado para eventos corporativos' in tree.text_content(),
           'tables': secondary_tables, 'cannot_prove_missing_prior_cash_is_zero': True,
           'cannot_silently_treat_adjusted_values_as_native_gross': True}
    decisions = {
        'DESK3': {
            'paid_cash': leading_gap_decision(IDENTITIES['DESK3'], date(2022, 12, 22),
                                             CoverageBasis.PAID_CASH, ()),
            'entitlement': leading_gap_decision(IDENTITIES['DESK3'], date(2022, 4, 29),
                                                CoverageBasis.ENTITLEMENT, ()),
            'status': 'pending_no_complete_primary_leading_period_coverage',
            'literal_zero_limit': 'The 30-Jun-2021 non-distribution sentence is a date-specific '
                'dividends statement before the required 30-Sep-2021 boundary; it does not '
                'declare JCP zero or cover later months. No absent DFC line became zero.',
            'pending_rights': 'Individual dividend payable is 255 thousand at 31-Dec-2020 and '
                '1482 thousand at 31-Dec-2021, 31-Mar-2022 and 30-Sep-2022. Its opening '
                'beneficiary/record dates and complete roll-forward remain unproved. '
                'The Dec-2022 notice covers 1227654.77 BRL with record date 29-Apr-2022; '
                'it does not expressly call itself the first-ever distribution or explain the full payable.',
            'required_next_primary_evidence': [
                'Explicit individual paid-dividends AND paid-JCP coverage from 30-Sep-2021 '
                'through 21-Dec-2022, with reporting precision and gross non-netted basis.',
                'Explicit entitlement/declaration coverage through 28-Apr-2022, including '
                'opening payable beneficiaries and record dates; exclude no old rights by assumption.',
                'Keep DDM adjusted-value basis unapproved until an independent native basis reconciliation.'],
        },
        'BMOB3': {
            'paid_cash': leading_gap_decision(IDENTITIES['BMOB3'], date(2022, 4, 12),
                                             CoverageBasis.PAID_CASH, ()),
            'entitlement': {'leading_gap_covered': False, 'reason': 'first_primary_record_date_unresolved',
                            'record_date_derived_from_ex_date': False, 'first_actual_record_date': None},
            'status': 'pending_partial_prewindow_dividend_allocation_not_full_zero_proof',
            'supported_dividend_allocation': {'annual_individual_paid_dividends_thousand_BRL': '186718',
                'native_components_thousand_BRL': ['178232', '8486'],
                'both_reported_paid_on': '2021-04-26', 'beneficiary': 'Otello Technology Investment AS',
                'beneficiary_held_100_percent_at_declaration': True,
                'matches_annual_at_reported_precision': True, 'no_new_per_share_payment_event': True,
                'JCP_coverage_established': False, 'Jan_Mar_2022_coverage_established': False,
                'bank_settlement_verified': False},
            'pending_rights': 'Exact native 112613 v1 reports FY2021 dividend payable 17895 '
                'thousand at 31-Dec-2021. Later v2 annual statement says proposal 16-Mar-2022, '
                'ex date 22-Mar-2022, payment 12-Apr-2022; this is not an explicit record date.',
            'required_next_primary_evidence': [
                'Explicit same-issuer individual JCP nonpayment/nondeclaration and opening '
                'rights proof at/after 30-Sep-2021; absence of a JCP row is not zero.',
                'Complete primary Jan-2022 through 11-Apr-2022 paid-cash coverage and '
                'payable/declaration roll-forward for the first public-holder distribution.',
                'Resolve the two missing native record dates without deriving them from ex dates.'],
            'prior_calendar': {'file': str(prior_path), 'sha256': _sha(prior_body),
                'native_component_count': len(bmob_events),
                'missing_primary_record_dates': sum(e['record_date'] is None for e in bmob_events),
                'existing_blockers': prior['evidence_audit']['checks']['blockers'],
                'event_dates_or_amounts_modified': False,
                'primary_monthly_price_matches': len(prior['primary_action_and_monthly_price_gate']['price_matches'])},
        },
    }
    source = evidence['sources']['bmob-native-2021']
    source.update(published_at=annual['issuers']['BMOB3']['master']['DT_RECEB'],
                  publication_basis='exact_CVM_DFP_2021_master_ID_112613_v1',
                  member_pdf_sha256='12deff57ef47b74a11908c4f3322d62e0160c1b36f22bc778bb032526f830119')
    responses = [json.loads(p.read_bytes()) for p in sorted((folder / BASE).glob('response-*.json'))]
    report = {
        'schema_version': 1, 'scope': ['DESK3', 'BMOB3'], 'window_start': START.isoformat(),
        'cutoff': CUTOFF.isoformat(), 'generated_at': datetime.now(timezone.utc).isoformat(),
        'policy_reviewed': ['recommender/zero_distribution_research.py:RF-ZERO-AGGREGATE-RESEARCH-1',
                            'recommender/aalr_zero_followon.py:evaluate_explicit_paid'],
        'typed_contract': {'period_observation_type': 'PrimaryPeriodObservation',
            'approved_coverage_type': 'PrimaryPeriodCoverageProof',
            'separate_dimensions': [item.value for item in CoverageBasis],
            'coverage_proofs_are_not_payment_events': True,
            'verified_primary_literal_and_complete_components_required': True,
            'opening_pending_rights_must_be_explicitly_resolved': True,
            'reported_precision_is_not_exact_unrounded_cash_zero': True},
        'observations': [asdict(row) for row in evidence['observations']],
        'approved_period_coverage_proofs': [], 'synthetic_cash_events': [],
        'new_model_admissions': [], 'production_cash_gate_changed': False,
        'required_monthly_price_anchors': 61, 'five_year_window_shortened': False,
        'findings': decisions, 'annual_source': annual, 'primary_sources': evidence['sources'],
        'unapproved_secondary_DESK': ddm,
        'legacy_before': before, 'legacy_after': after, 'legacy_before_after_equal': True,
        'network': {'collector_scope': 'round2-zero', 'retries': 0,
            'responses': [{k: r.get(k) for k in ('url', 'status', 'error', 'sha256', 'bytes',
                           'file', 'started_at', 'retrieved_at', 'published_at')} for r in responses],
            'robots_stop': {'origin': 'components.mz-sites.com',
                'blocked_target': 'https://components.mz-sites.com/wp-content/themes/components/short-filemanager/dist/js/main.js',
                'blocked_target_request_sent': False, 'origin_retried_after_stop': False},
            'CVM_DESK_2021_download': 'one timeout preserved; no retry or old-filing substitution',
            'denied_origins_requested': [], 'authentication_or_TLS_bypass': False},
        'visual_review_pages': {'desk-2q2021': [64], 'desk-2021': [15, 21, 108],
            'desk-1q2022': [8], 'desk-3q2022': [8], 'desk-first-payment-notice': [1],
            'bmob-2021': [7, 44, 45], 'bmob-native-2021': [9, 63, 64], 'bmob-2022': [40]},
        'parent_integration': 'Do not relax _one/_payments for either issuer on this evidence. '
            'A future reviewed proof may cover the leading empty interval separately from '
            'actual events, keeping the original first event, the full five years, and 61 price '
            'anchors. Never add Otello pre-IPO rights to public-holder cash just to meet an old-event gate.',
        'changed_files': ['recommender/round2_zero_cash_coverage.py',
            'recommender/test_round2_zero_cash_coverage.py', 'recommender/data/market/' + BASE + '/',
            '../work/recovery_round2_20261025/financial-zero-*'],
        'bank_settlement_verified': False, 'point_in_time_archive_verified': False,
    }
    _save(reports / 'financial-zero-handoff.json', report)
    return report


def inspect(name, pages=None, folder=ROOT):
    from pypdf import PdfReader

    folder = Path(folder)
    receipt, body = document(name, folder)
    if receipt.get('original_container'):
        _save(folder / receipt['file'], body)
        _save(folder / BASE / f'native-PDF-{name}.json', receipt)
    reader = PdfReader(io.BytesIO(body))
    if pages is None:
        all_pages = [{'pdf_page': index + 1, 'text': page.extract_text()}
                     for index, page in enumerate(reader.pages)]
        _save(folder / BASE / f'text-{name}.json', all_pages)
        for page in all_pages:
            text = page['text']
            if re.search(r'(?i)dividend|capital pr.prio|jcp|jscp', text):
                matches = [m.start() for m in re.finditer(r'(?i)dividend|capital pr.prio|jcp|jscp', text)]
                print(page['pdf_page'], ' | '.join(' '.join(text[max(0, at-70):at+300].split())
                      for at in matches[:5]))
        return
    poppler = shutil.which('pdftoppm')
    _require(poppler is not None, 'round2_zero_Poppler_required')
    for number in pages:
        prefix = folder / BASE / f'review-{name}-p{number}'
        process = subprocess.run([poppler, '-f', str(number), '-l', str(number),
            '-scale-to', '1900', '-png', '-singlefile', '-', str(prefix)],
            input=body, capture_output=True, timeout=45, check=False)
        _require(process.returncode == 0, 'round2_zero_render_failed')
        print(prefix.with_suffix('.png'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('acquire', 'inspect', 'report'))
    parser.add_argument('--names', nargs='+', choices=tuple(SOURCES), default=list(SOURCES))
    parser.add_argument('--pages', nargs='+', type=int)
    args = parser.parse_args()
    if args.command == 'acquire':
        acquire(args.names)
    elif args.command == 'report':
        report = write_report()
        print('DESK3/BMOB3: no approved leading zero-cash proof; no gate changes.')
        print(REPORTS / 'financial-zero-handoff.json')
    else:
        for name in args.names:
            inspect(name, args.pages)


if __name__ == '__main__':
    main()
