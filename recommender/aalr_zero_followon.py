"""Offline, source-only follow-on for the specific AALR3 zero-cash gap.

Reads pinned existing bytes for two issuers, not the broad collector. No network
client, admission route, payment-event export, or production pointer is present.
"""

import argparse
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from lxml import html

from .alternative_cash_research import Reader, _safe
from .payment_sources import inspect_payment_page
from .zero_distribution_research import (
    _norm, cash_component, evaluate_period, summarize_periods,
)


CACHE = 'raw/primary-research/aalr-zero-followon'
START, CUTOFF = '2021-10-01', '2026-09-30'
BASE_SHA = '45643c8004c09a35391d062bc58844a8452c4ee3f8412551d3c723407efaccce'
BASE = f'raw/primary-research/zero-distributions/research-{BASE_SHA}.json'
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
PAYMENTS = 'raw/inventory/payments/AALR3.json'
SECONDARY = 'raw/expanded/AALR3-payments.html'
RI_ROOT = 'raw/primary-research/zero-distributions/aalr-history-'
RI_RECEIPT = RI_ROOT + '5476e2de95326e20290edd3889753b23497d46a1ffbc4922f6ab6eafd83e98af-receipt.json'
RI_HTML = RI_ROOT + 'ce18a9c914134cf73776762104bb02d7440b7444b66be7f2611f8d2494dfdef4.html'
IPE = 'raw/primary-research/cvm-documents/index-2026-d2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip'
ARCHIVES = (
    ('raw/cvm-dfp-2022.zip', 'DFP', 2022),
    ('raw/cvm-dfp-2023.zip', 'DFP', 2023),
    ('raw/cvm-dfp-2024.zip', 'DFP', 2024),
    ('raw/cvm-dfp-2025.zip', 'DFP', 2025),
    ('raw/official-financials/itr_cia_aberta_2026.zip', 'ITR', 2026),
)
INPUTS = {
    BASE: BASE_SHA,
    FCA: 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b',
    PAYMENTS: '017e06dac3e9bc84b9d3454f1d8bd2c8828aac08050d18e1aee53eab0f25f7e8',
    SECONDARY: 'b51c136d68ff77d8f341b0b36e9fe4450e2d60e4fce7c18c4f110ca58f109ec5',
    RI_RECEIPT: '5476e2de95326e20290edd3889753b23497d46a1ffbc4922f6ab6eafd83e98af',
    RI_HTML: 'ce18a9c914134cf73776762104bb02d7440b7444b66be7f2611f8d2494dfdef4',
    IPE: 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
    ARCHIVES[0][0]: '1272858bbaf30c0434e70dae060ef12ec6474131f8ec5fd05938686184e907c9',
    ARCHIVES[1][0]: '46f281e5849ae7e7f0862aa9b65241a1ead95bb22644d1b5310d6b6542d7c398',
    ARCHIVES[2][0]: 'b51c920793c18edb66e8780c0780836d441af1199e1077064b73c7de6c865df0',
    ARCHIVES[3][0]: '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f',
    ARCHIVES[4][0]: '56a82941d1c381b421a8075a584b498e4f183986ad526546e14682422773e6a1',
}
ISSUERS = {
    24058: ('AALR3', '42.771.949/0001-35'),
    24910: ('LWSA3', '02.351.877/0001-52'),
}


def _dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _load(reader, name, role, url=None):
    return reader.read(name, role, INPUTS[name], url)


def _rows(archive, member, source, predicate):
    if archive.getinfo(member).file_size > 100_000_000:
        raise ValueError('oversized_CVM_member')
    result = []
    with archive.open(member) as stream:
        reader = csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
        for number, row in enumerate(reader, 2):
            if predicate(row):
                result.append({**row, 'evidence': {
                    'source_file': source['file'], 'archive_sha256': source['sha256'],
                    'member': member, 'csv_record_number_including_header': number,
                    'csv_physical_line_end': reader.line_num, 'cells_sha256': _sha(_dump(row)),
                }})
    return result


def _ours(row):
    return int(row['CD_CVM']) in ISSUERS


def _latest(metadata):
    grouped = defaultdict(list)
    for row in metadata:
        cvm = int(row['CD_CVM'])
        if row['CNPJ_CIA'] != ISSUERS[cvm][1]:
            raise ValueError('CVM_CNPJ_conflict')
        date.fromisoformat(row['DT_RECEB'])
        date.fromisoformat(row['DT_REFER'])
        if row['DT_RECEB'] <= CUTOFF and row['DT_REFER'] <= CUTOFF:
            grouped[cvm, row['DT_REFER']].append(row)
    return {key: max(rows, key=lambda r: (int(r['VERSAO']), r['DT_RECEB']))
            for key, rows in grouped.items()}


def evaluate_explicit_paid(rows):
    """Bare equity/distribution labels cannot become explicit paid-cash zeros."""
    result = evaluate_period(rows)
    ambiguous = [row for row in rows if cash_component(row['CD_CONTA'], row['DS_CONTA'])
                 and not re.search(r'\b(?:pagos|pagamento|pagamentos)\b', _norm(row['DS_CONTA']))]
    if ambiguous:
        result.update(status='pending', reason='distribution_label_does_not_explicitly_say_paid',
                      ambiguous_paid_labels=ambiguous)
    return result


def _identity(reader):
    body, source = _load(reader, FCA, 'exact_current_security_FCA',
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        metadata = _rows(archive, 'fca_cia_aberta_2026.csv', source, _ours)
        securities = _rows(archive, 'fca_cia_aberta_valor_mobiliario_2026.csv', source,
                           lambda r: r['CNPJ_Companhia'] in {v[1] for v in ISSUERS.values()})
    latest = _latest(metadata)
    result = {}
    for cvm, (ticker, cnpj) in ISSUERS.items():
        filing = max((r for (code, _), r in latest.items() if code == cvm),
                     key=lambda r: (r['DT_REFER'], int(r['VERSAO']), r['DT_RECEB']))
        issuer_rows = [r for r in securities if r['ID_Documento'] == filing['ID_DOC']]
        exact = [r for r in issuer_rows if r['Codigo_Negociacao'] == ticker]
        if (len(exact) != 1 or exact[0]['CNPJ_Companhia'] != cnpj
                or _norm(exact[0]['Valor_Mobiliario']) != 'acoes ordinarias' or exact[0]['Composicao_BDR_Unit']):
            raise ValueError('exact_current_FCA_ON_identity_not_verified')
        result[ticker] = {'cvm': cvm, 'cnpj': cnpj, 'share_class': 'ON',
            'filing': filing, 'security': exact[0], 'issuer_security_rows': issuer_rows,
            'all_historical_class_cash_allocations_verified': False,
            'FCA_is_not_distribution_settlement_or_trading_confirmation': True}
    return result


def _financials(reader):
    periods, index_observations, other = [], [], {}
    for name, kind, year in ARCHIVES:
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{kind}/DADOS/{kind.lower()}_cia_aberta_{year}.zip'
        body, source = _load(reader, name, 'two_issuer_individual_paid_cash_rows', url)
        prefix = kind.lower() + '_cia_aberta_'
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            metadata = _rows(archive, f'{prefix}{year}.csv', source, _ours)
            latest = _latest(metadata)
            index_observations.append({'source': source, 'metadata': metadata,
                'latest_filings': list(latest.values()),
                'AALR_filings_published_by_cutoff': sum(key[0] == 24058 for key in latest),
                'absence_is_not_zero': True})
            grouped = defaultdict(list)
            for member in archive.namelist():
                if not re.fullmatch(prefix + r'DFC_(?:MI|MD)_ind_' + str(year) + r'\.csv', member):
                    continue
                for row in _rows(archive, member, source, _ours):
                    cvm = int(row['CD_CVM'])
                    filing = latest.get((cvm, row['DT_REFER']))
                    if filing is None or int(row['VERSAO']) != int(filing['VERSAO']):
                        continue
                    if row['CNPJ_CIA'] != ISSUERS[cvm][1]:
                        raise ValueError('cash_row_CNPJ_conflict')
                    start, end = row['DT_INI_EXERC'], row['DT_FIM_EXERC']
                    if not start <= end <= row['DT_REFER']:
                        raise ValueError('invalid_cash_period')
                    if start > CUTOFF or end < START:
                        continue
                    grouped[cvm, row['DT_REFER'], start, end, member].append(row)
            for (cvm, ref, start, end, member), rows in grouped.items():
                filing = latest[cvm, ref]
                periods.append({'ticker': ISSUERS[cvm][0], 'cvm': cvm, 'scope': 'individual',
                    'period_start': start, 'period_end': end, 'document_reference': ref,
                    'document_type': kind, 'table': member, 'published_at': filing['DT_RECEB'],
                    'filing_id': filing['ID_DOC'], 'version': int(filing['VERSAO']),
                    'filing_evidence': filing, 'source': source,
                    'temporal_qualification': 'retrospective_period_reading_published_by_global_cutoff_not_month_PIT',
                    **evaluate_explicit_paid(rows)})
            # A missing latest individual DFC is not permission to reuse an older zero.
            for (cvm, ref), filing in latest.items():
                if not any(key[0:2] == (cvm, ref) for key in grouped):
                    periods.append({'ticker': ISSUERS[cvm][0], 'cvm': cvm, 'scope': 'individual',
                        'period_start': ref[:4] + '-01-01', 'period_end': ref,
                        'document_reference': ref, 'document_type': kind,
                        'table': f'{prefix}DFC_MI_ind_{year}.csv',
                        'published_at': filing['DT_RECEB'], 'filing_id': filing['ID_DOC'],
                        'version': int(filing['VERSAO']), 'source': source, 'rows': [],
                        'status': 'pending', 'reason': 'latest_individual_DFC_missing_no_older_fallback'})
            if kind == 'DFP' and year == 2025:
                for statement in ('BPP', 'DMPL'):
                    member = f'{prefix}{statement}_ind_2025.csv'
                    other[statement] = _rows(archive, member, source, lambda r:
                        int(r['CD_CVM']) == 24058 and r['CNPJ_CIA'] == ISSUERS[24058][1]
                        and int(r['VERSAO']) == int(latest[24058, r['DT_REFER']]['VERSAO'])
                        and any(word in _norm(r['DS_CONTA']) for word in ('dividend', 'capital proprio', 'jcp')))
    return periods, index_observations, other


def payable_review(rows):
    """Keep every specific liability; neither net nor hide it under a zero sibling."""
    payable_rows, nonzero = [], []
    for row in rows:
        if 'a pagar' not in _norm(row['DS_CONTA']):
            continue
        if row['MOEDA'] != 'REAL' or row['ESCALA_MOEDA'] not in ('MIL', 'UNIDADE'):
            raise ValueError('unsupported_payable_units')
        value = Decimal(row['VL_CONTA'])
        if not value.is_finite():
            raise ValueError('invalid_payable_value')
        item = {**row, 'reported_value_brl': str(value * (1000 if row['ESCALA_MOEDA'] == 'MIL' else 1))}
        payable_rows.append(item)
        if value != 0:
            nonzero.append(item)
    return {'payable_rows': payable_rows, 'nonzero_payable_rows': nonzero,
        'prior_payable_settlement_excluded': False, 'holder_class_or_NCI_attribution_verified': False,
        'nonzero_liability_does_not_prove_payment': True,
        'required': '2026 roll-forward and notes establishing beneficiary, gross cash settlement or explicit non-settlement; no class allocation or netting assumption'}


def _claims(reader):
    body, typed_source = _load(reader, PAYMENTS, 'cached_typed_cash_claim_not_issuer_RI')
    typed = json.loads(body)['selected_cash_source']['report']
    secondary, secondary_source = _load(reader, SECONDARY, 'original_secondary_HTML',
                                       'https://investidor10.com.br/acoes/aalr3/')
    if typed['sha256'] != secondary_source['sha256']:
        raise ValueError('typed_secondary_claim_source_conflict')
    parsed = inspect_payment_page(secondary, 'AALR3', typed['as_of'], typed['observed_at'])
    if parsed['report']['zero_claims'] != typed['zero_claims']:
        raise ValueError('cached_typed_claim_does_not_reproduce')
    receipt_body, _ = _load(reader, RI_RECEIPT, 'original_issuer_RI_receipt')
    receipt = json.loads(receipt_body)
    ri_body, ri_source = _load(reader, RI_HTML, 'issuer_RI_history_empty_not_zero', receipt['url'])
    if (receipt['sha256'] != ri_source['sha256'] or receipt['bytes'] != len(ri_body)
            or receipt['file'] != RI_HTML or receipt['http_status'] != 200):
        raise ValueError('RI_receipt_integrity_conflict')
    ri = inspect_payment_page(ri_body, 'AALR3', CUTOFF, receipt['retrieved_at'])
    tree = html.fromstring(ri_body, parser=html.HTMLParser(no_network=True))
    return {'secondary': {'source': typed_source, 'original': secondary_source,
                'claims': typed['zero_claims'], 'original_as_of': typed['as_of'],
                'original_observed_at': typed['observed_at'], 'reanchored_to_cutoff': False,
                'authority': 'secondary_Investidor10_not_issuer_RI',
                'JCP_covered': False, 'publication_date_verified': False,
                'usable_primary_bridge': False},
            'issuer_RI': {'receipt': receipt, 'report': ri['report'],
                'table_count': len(tree.xpath('//table')),
                'nonempty_table_rows': sum(bool(' '.join(t.itertext()).strip()) for t in tree.xpath('//table//tr')),
                'empty_year_message_present': 'nenhum arquivo para o ano selecionado' in _norm(tree.text_content()),
                'usable_primary_bridge': False},
            'author_independence': 'CVM issuer filings and issuer RI share issuer authorship; not two independent authors'}


def _ipe_candidates(reader):
    body, source = _load(reader, IPE, 'exact_AALR_2026_primary_document_index',
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2026.zip')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        rows = _rows(archive, 'ipe_cia_aberta_2026.csv', source,
                     lambda r: int(r['Codigo_CVM']) == 24058 and r['Data_Entrega'] <= CUTOFF)
    chosen = [r for r in rows if any('numSequencia=' + seq + '&' in r['Link_Download']
                                    for seq in ('1074213', '1074212', '1069567', '1090826'))]
    return {'issuer_index_rows_published_by_cutoff': len(rows), 'source': source,
        'exact_candidate_rows': chosen, 'documents_acquired_or_read_in_this_followon': 0,
        'absence_or_board_destination_is_not_paid_zero': True,
        'decision': 'No identified complete January-September 2026 paid-cash source; 2025 annual statements and declarations cannot close that interval. No speculative requests.'}


def audit(folder):
    reader = Reader(folder)
    baseline_body, _ = _load(reader, BASE, 'pinned_prior_research_not_recollected')
    baseline = json.loads(baseline_body)
    if (baseline['window_start'], baseline['window_end']) != (START, CUTOFF):
        raise ValueError('baseline_window_conflict')
    identities = _identity(reader)
    periods, index, other = _financials(reader)
    summaries = {ticker: summarize_periods([r for r in periods if r['ticker'] == ticker],
                 date.fromisoformat(START), date.fromisoformat(CUTOFF)) for ticker, _ in ISSUERS.values()}
    for ticker, summary in summaries.items():
        old = next(c for c in baseline['companies'] if c['identity']['ticker'] == ticker)
        if summary['unresolved_months'] != old['unresolved_months']:
            raise ValueError('narrow_followon_coverage_differs_from_pinned_baseline')
    claims = _claims(reader)
    payables = payable_review(other['BPP'])
    aalr = summaries['AALR3']
    document_triage = _ipe_candidates(reader)
    return {'schema_version': 1, 'policy': 'RF-AALR-ZERO-FOLLOWON-1',
        'window_start': START, 'window_end': CUTOFF, 'purpose': 'source_only_not_model_admission',
        'module_sha256': _sha(Path(__file__).read_bytes()),
        'sources': list(reader.sources.values()), 'identity': identities,
        'AALR3': {'cash_coverage': aalr, 'claims': claims, 'prior_payables': payables,
            'DMPL_declared_rows_not_paid_cash': other['DMPL'],
            'missing_months': aalr['unresolved_months'], 'complete_current_class_paid_zero_proof': False,
            'exact_unrounded_cash_zero_verified': False, 'class_payment_allocation_verified': False,
            'retrospective_reading_only_not_month_PIT': True,
            'blockers': ['January-September 2026 paid dividends and JCP unknown',
                'Rounding in MIL cannot prove exact unrounded cash zero',
                'Specific prior dividend payable requires 2026 beneficiary and settlement reconciliation',
                'No primary RI paid-zero bridge; secondary claim is undated-relative and dividends-only']},
        'LWSA3': {'cash_coverage': summaries['LWSA3'], 'full_window_zero_route': 'contradicted_by_reported_cash_outflows',
            'aggregate_outflows_do_not_locate_payment_month_class_or_component': True},
        'financial_index_observations': index, 'next_document_triage': document_triage,
        'request_decision': {'maximum_new_public_primary_requests': 8, 'new_requests': 0,
            'network_implementation_present': False, 'no_complete_feasible_source_chain_identified': True},
        'conservative_next_step': 'Only reassess after explicit primary paid-dividends and paid-JCP evidence covers all nine months, with prior-payable roll-forward, current-class scope, gross/non-netted amounts and precision qualification. Then propose a separate typed zero-cash route and independently verified 61 price anchors; never fabricate four matching cash events.',
        'production_cash_gate_changed': False, 'typed_zero_cash_route_proposed': False,
        'price_independence_verified': False, 'model_approval_performed': False,
        'individual_events_generated': 0, 'parent_files_or_pointers_written': False}


def save_report(folder, report):
    body = _dump(report)
    name = f'{CACHE}/report-{_sha(body)}.json'
    path = _safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_followon_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--market-dir', type=Path, default=Path('recommender/data/market'))
    args = parser.parse_args()
    report = audit(args.market_dir)
    name = save_report(args.market_dir, report)
    count = report['AALR3']['cash_coverage']['zero_supported_full_month_count']
    missing = len(report['AALR3']['missing_months'])
    print(f'{name}\nAALR3: {count} reported-precision months, {missing} unknown; no complete zero proof. New requests: 0.')


if __name__ == '__main__':
    main()
