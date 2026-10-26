"""Offline issuer UNIT cash research and exact RI-published raw quote discovery.

Acquisition is explicit, scoped to brbi and immutable. Printed UNIT amounts are
retained directly; announced payment dates are not bank-settlement evidence.
Partial notices and a failed history request never authorize model admission.
"""

import argparse
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from . import brbi_2021_identity as identity
from .bulk_pending_sources import PublicSourceCollector, _save
from .issuer_raw_quote_research import parse_payload as _raw_quotes, widget_request


BASE = identity.BASE
CUTOFF = date(2026, 9, 30)
PAGE_URL = 'https://ri.brpartners.com.br/informacoes-publicas/cotacoes-e-graficos/'
PAGE_SHA = 'a1620979ab962534e5ae842175696cad43bea718632308cf7f66f653602f9f37'
BUNDLE_URL = 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js'
BUNDLE_SHA = '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
WIDGET_ID = 'b3daf44e-11d7-11ee-a86c-12dd520511fd'
HISTORY_URL = (f'https://api.mziq.com/mzstockinfo/{WIDGET_ID}/history'
               '?from=2021-09-01&to=2026-09-30&tickers=BRBI11&adjusted=false')
IPE_PINS = {
    2022: '44e4a5bf88efa6696b9150f4b7fde3693cbf7ec79d511b65a62c0135ac674c5d',
    2024: '65878657e487ba86b717e93f112a11cf08faf0356ae245ef6e0e72b0fbac80e2',
    2026: 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c'}
NOTICES = (
    {'year': 2022, 'day': '2022-03-18', 'protocol': '955827', 'sequence': '480561',
     'sha256': 'b884f03facc241ccebd46ffd5f14a6865f8fbb7cb1a0031f073f6063afbdb341'},
    {'year': 2024, 'day': '2024-11-27', 'protocol': '1308716', 'sequence': '833442',
     'sha256': 'a9d19373af5d60e2b95454168aba07081f082e8920efd4bb6f2db749692c6385'},
    {'year': 2026, 'day': '2026-08-06', 'protocol': '1553430', 'sequence': '1078136',
     'sha256': '13401ae16e0d63763303865b864272bf8d5602f8b6c2d112f0dab5c444ffcc8d'})
MONTHS = {'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4, 'maio': 5, 'junho': 6,
          'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12}
PT_DATE = r'(\d{1,2}) de ([a-z]+) de (\d{4})'


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def notice_url(notice):
    return ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
            f'&CodigoInstituicao=1&numProtocolo={notice["protocol"]}'
            f'&numSequencia={notice["sequence"]}&numVersao=1')


def _response(root, url, digest=None, *, allow_failure=False):
    relative = f'{BASE}/response-{identity._sha(url.encode())}.json'
    receipt = json.loads(identity._local(root, relative))
    _require(receipt['url'] == url and receipt['scope'] == 'brbi' and receipt['retries'] == 0,
             'brbi_issuer_receipt_identity_changed')
    _require(receipt['file'] == f'{BASE}/body-{receipt["sha256"]}.bin', 'brbi_issuer_body_reference_changed')
    body = identity._local(root, receipt['file'], receipt['sha256'])
    _require(len(body) == receipt['bytes'] and (digest is None or receipt['sha256'] == digest),
             'brbi_issuer_original_changed')
    if not allow_failure:
        _require(receipt['status'] == 200 and receipt['error'] is None, 'brbi_issuer_transport_unavailable')
    source = {k: receipt[k] for k in ('url', 'file', 'sha256', 'bytes', 'retrieved_at', 'status', 'error')}
    source['receipt_file'] = relative
    return body, source


def _notice_index(root, notice):
    year, digest = notice['year'], IPE_PINS[notice['year']]
    relative = f'raw/primary-research/cvm-documents/index-{year}-{digest}.zip'
    content = identity._local(root, relative, digest)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        rows = identity._rows(archive, f'ipe_cia_aberta_{year}.csv')
    candidates = [r for r in rows if r['Codigo_CVM'] == '25860' and r['Categoria'] == 'Aviso aos Acionistas'
                  and re.search(r'dividend|juros', identity._text(r['Assunto']))]
    matches = [r for r in candidates if r['Link_Download'] == notice_url(notice)]
    _require(len(matches) == 1, 'brbi_issuer_notice_index_ambiguous')
    record, = matches
    _require(record['CNPJ_Companhia'] == identity.IDENTITY['cnpj']
             and record['Data_Referencia'] == record['Data_Entrega'] == notice['day']
             and record['Versao'] == '1', 'brbi_issuer_notice_metadata_changed')
    source = {'file': relative, 'sha256': digest, 'bytes': len(content),
              'url': f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip',
              'authority': 'primary', 'use': 'exact_native_issuer_notice_publication', 'retrieved_at': None}
    return record, source, candidates


def _one_date(pattern, text):
    matches = re.findall(pattern + PT_DATE, text)
    _require(len(matches) == 1, 'brbi_issuer_cash_date_missing_or_ambiguous')
    day, month, year = matches[0]
    _require(month in MONTHS, 'brbi_issuer_cash_month_unknown')
    return date(int(year), MONTHS[month], int(day))


def parse_cash_notice(pages, published_at):
    """Extract printed per-UNIT amounts and all dates, without a three-times rule."""
    _require(type(published_at) is date and published_at <= CUTOFF, 'brbi_issuer_cash_publication_invalid')
    _require(bool(pages), 'brbi_issuer_empty_cash_notice')
    first, text = identity._text(pages[0]), identity._text(' '.join(pages))
    _require(first.startswith(('br advisory partners participacoes s.a.', 'brbi br partners s.a.'))
             and identity.IDENTITY['cnpj'] in first and 'aviso aos acionistas' in first
             and 'pagamento' in first, 'brbi_issuer_cash_identity_missing')
    _require(re.search(r'units\s*\(\s*brbi11\s*\), formadas por 1 \(uma\) acao ordinaria '
                       r'e 2 \(duas\) acoes preferenciais', first), 'brbi_issuer_effective_UNIT_composition_missing')
    amounts = re.findall(r'correspondente a r\$\s*(\d+,\s*\d+)\s*\([^)]*\)\s*por unit\b', first)
    _require(len(amounts) == 1, 'brbi_issuer_literal_UNIT_cash_missing_or_ambiguous')
    printed = amounts[0].replace(' ', '')
    amount = Decimal(printed.replace(',', '.'))
    _require(amount.is_finite() and amount > 0, 'brbi_issuer_UNIT_cash_invalid')
    record = _one_date(r'inscritos nos registros da companhia na data de ', text)
    ex_date = _one_date(r'a partir de ', text)
    payment = _one_date(r'sera realizado em uma unica parcela no dia ', text)
    signed = _one_date(r'sao paulo, ', text)
    _require(signed == published_at and published_at <= record < ex_date <= payment <= CUTOFF,
             'brbi_issuer_cash_dates_conflict')
    _require(not re.search(r'cancelad[oa]|pagamento suspenso', text), 'brbi_issuer_cash_cancelled')
    return {'ticker': 'BRBI11', 'isin': 'BRBRBICDAM10', 'cvm': 25860, 'currency': 'BRL',
            'cash_kind': 'extraordinary_dividend' if 'dividendos extraordinarios' in first else 'dividend',
            'record_date': record, 'ex_date': ex_date, 'payment_date': payment, 'published_at': published_at,
            'gross_per_UNIT': str(amount), 'printed_per_UNIT': printed, 'net_per_UNIT': None,
            'amount_basis': 'explicit_native_per_UNIT', 'effective_composition': {'ON': 1, 'PN': 2},
            'arithmetic_conversion_applied': False, 'status': 'issuer_announced_payment',
            'bank_settlement_verified': False, 'point_in_time_archive_verified': False,
            'numeric_precision': 'retain_all_printed_decimal_places_without_quantization'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'brbi_issuer_duplicate_JSON_key')
        result[key] = value
    return result


def parse_quotes(body, digest):
    # Preserve existing OHLC/date/number validation, adding duplicate-key rejection.
    json.loads(body, object_pairs_hook=_unique, parse_float=Decimal)
    rows = _raw_quotes(body, 'BRBI11', digest)
    _require(bool(rows), 'brbi_issuer_empty_raw_quotes')
    return [{**row, 'date': date.fromisoformat(row['date'])} for row in rows]


def _widget(root):
    _, page = _response(root, PAGE_URL, PAGE_SHA)
    _, bundle = _response(root, BUNDLE_URL, BUNDLE_SHA)
    url, discovery = widget_request(root, 'BRBI11', {**page, 'ticker': 'BRBI11'}, bundle, kind='history')
    _require(url == HISTORY_URL, 'brbi_issuer_widget_identity_changed')
    page.update(authority='issuer_published_configuration', use='exact_BRBI11_widget_and_BRL')
    bundle.update(authority='issuer_linked_MZ_public_widget', use='published_unadjusted_history_request_template')
    return page, bundle, discovery


def get_brbi_issuer_data(folder, cutoff=CUTOFF):
    """Return typed dates, printed UNIT cash, raw quotes and explicit qualifications."""
    _require(cutoff == CUTOFF, 'brbi_issuer_unreviewed_cutoff')
    root = Path(folder).resolve()
    proof = identity.audit(root, cutoff)
    page, bundle, discovery = _widget(root)
    sources = [*proof['sources'], page, bundle]
    cash, diagnostics, unsampled = [], [], []
    from pypdf import PdfReader

    for notice in NOTICES:
        record, index_source, candidates = _notice_index(root, notice)
        body, source = _response(root, notice_url(notice), notice['sha256'])
        _require(body.startswith(b'%PDF-'), 'brbi_issuer_cash_not_PDF')
        reader = PdfReader(io.BytesIO(body))
        event = parse_cash_notice([p.extract_text() for p in reader.pages], date.fromisoformat(record['Data_Entrega']))
        source.update(authority='primary_issuer_signed_CVM_notice', published_at=record['Data_Entrega'],
                      use='native_literal_UNIT_cash_record_ex_and_announced_payment_dates')
        event.update(source_ref='brbi-native-cash:' + source['sha256'], source_file=source['file'],
                     source_sha256=source['sha256'], pdf_pages=list(range(1, len(reader.pages) + 1)))
        cash.append(event)
        sources.extend((index_source, source))
        unsampled.extend({'published_at': r['Data_Entrega'], 'url': r['Link_Download'], 'subject': r['Assunto']}
                         for r in candidates if r['Link_Download'] != notice_url(notice))
    history_body, history_source = _response(root, HISTORY_URL, allow_failure=True)
    history_source.update(authority='Enfoque_vendor_via_issuer_published_MZ_widget', use='raw_history_acquisition_attempt')
    sources.append(history_source)
    quotes = []
    if history_source['status'] == 200 and history_source['error'] is None:
        quotes = parse_quotes(history_body, history_source['sha256'])
    else:
        diagnostics.append({'kind': 'raw_quotes', 'status': 'unavailable', 'http_status': history_source['status'],
                            'error': history_source['error'], 'automatic_retry': False,
                            'source_file': history_source['file']})
    diagnostics.extend((
        {'kind': 'cash_coverage', 'status': 'partial', 'reason': 'three_reviewed_notices_are_not_a_complete_five_year_calendar',
         'unsampled_dividend_notices_in_reviewed_IPE_years': unsampled,
         'additional_unreviewed_years': [2021, 2023, 2025]},
        {'kind': 'rejected_discovery', 'status': 'not_cash_evidence',
         'source_file': f'{BASE}/body-73e75a5c9d8675e42eb8e2f45ebd19572e055029ef2d4f01bc2474e73a59d8a0.bin',
         'reason': 'AGO_date_notice_does_not_declare_UNIT_amount_record_and_payment'},
        {'kind': 'independent_reconciliation', 'status': 'pending',
         'reason': 'announced_payment_is_not_settlement_and_vendor_upstream_independence_is_unverified'}))
    return {'policy': 'BRBI_ISSUER_UNIT_DATA_RESEARCH_1', 'cutoff': cutoff, 'identity': proof['identity'],
            'cash_per_UNIT': cash, 'quotes_raw': quotes, 'sources': sources, 'diagnostics': diagnostics,
            'widget_request_proof': discovery, 'qualifications': {
                'cash_calendar_complete': False, 'all_61_monthly_quote_anchors_verified': False,
                'published_widget_adjusted': False, 'quote_currency': 'BRL',
                'quote_vendor': 'Enfoque', 'upstream_vendor_independence_verified': False,
                'cash_is_announced_payment_not_settlement_confirmation': True,
                'UNIT_amounts_derived_from_component_multiplication': False,
                'corporate_action_adjustments_applied': False, 'share_action_ledger_verified': False,
                'financial_features_validated': False, 'retirement_main_income_recommended': False,
                'point_in_time_archive_verified': False},
            'B3_response_fabricated': False, 'unit_candidates_modified': False,
            'model_admitted': False, 'admissions': []}


def collect(folder):
    """Explicit acquisition only; collector returns existing failed attempts without retry."""
    root = Path(folder).resolve()
    collector = PublicSourceCollector(root, 'brbi')
    for url in (PAGE_URL, BUNDLE_URL):
        collector.fetch(url, discovery={'kind': 'exact_BRBI11_RI_page_or_literal_linked_widget'})
    _, _, discovery = _widget(root)
    collector.fetch(HISTORY_URL, discovery=discovery)
    for notice in NOTICES:
        record, source, _ = _notice_index(root, notice)
        collector.fetch(notice_url(notice), discovery={'kind': 'exact_native_UNIT_payment_notice',
                        'IPE_record': record, 'index_file': source['file']})


def write_report(folder, result):
    def serialize(value):
        if isinstance(value, date):
            return value.isoformat()
        raise TypeError(type(value).__name__)

    body = (json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False, default=serialize) + '\n').encode()
    path = Path(folder).resolve() / BASE / f'issuer-data-{hashlib.sha256(body).hexdigest()}.json'
    _require(identity._safe(Path(folder).resolve(), path), 'brbi_issuer_unsafe_report_path')
    path.parent.mkdir(parents=True, exist_ok=True)
    _save(path, body)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent / 'data/market')
    parser.add_argument('--collect', action='store_true')
    args = parser.parse_args()
    if args.collect:
        collect(args.root)
    print(write_report(args.root, get_brbi_issuer_data(args.root)))


if __name__ == '__main__':
    main()
