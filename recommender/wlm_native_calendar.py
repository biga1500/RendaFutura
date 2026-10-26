"""Offline, whole-event WLM calendar proof; never admits an asset or fetches data.

The native form has no cash-kind field. Its four gross components must bind to
the complete, explicitly typed cached exchange event. A later approval-document
revision blocks use until its actual contents have been independently reviewed.
"""

import argparse
import base64
from collections import Counter
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

import pdfplumber
from pypdf import PdfReader

from . import alternative_cash_research as shared
from . import cvm_proventos_documents as cvm
from . import payment_sources as inspector


CACHE = 'raw/primary-research/wlm-native-calendar'
CUTOFF = date(2026, 9, 30)
APPROVAL = '2024-04-29'
CNPJ = '33228024000151'
STOCK = {'ticker': 'WLMM3', 'cvm': 11070, 'share_class': 'ON', 'isin': 'BRWLMMACNOR6'}
CLASSES = {'ON': ('WLMM3', 'BRWLMMACNOR6'), 'PN': ('WLMM4', 'BRWLMMACNPR3')}
FORM_SHA = '72e2cb8dd7ddddbad43c41f3541348dacaa6f2e29adc8d95187d7345b9e80a9d'
FORM = 'raw/primary-research/cash-calendars/cvm753217v1-' + FORM_SHA + '.pdf'
RECEIPT_SHA = '5a7614d3bdd934ed4de0cf09b79bc6bc27779cff0b47544055570e87e7997b27'
IPE_MANIFEST = 'raw/primary-research/cvm-documents/index-sources.json'
IPE_MANIFEST_SHA = '0d5f66f63001963adf3355f103c4fae241435ebf5f22bbfeb75693f072dedc14'
EXCHANGE_MANIFEST = 'expanded-source-audit.json'
EXCHANGE_MANIFEST_SHA = '0b80c25e01dbe15445d13f593622d21fe15cc345cb99e281723db61da0473ac5'
IPE_HASHES = {
    2021: 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7',
    2022: '44e4a5bf88efa6696b9150f4b7fde3693cbf7ec79d511b65a62c0135ac674c5d',
    2023: 'ef1093983df55399b8ec02255eaa822d941ea797f881d00b8f61f23fb63fb1b0',
    2024: '65878657e487ba86b717e93f112a11cf08faf0356ae245ef6e0e72b0fbac80e2',
    2025: '22f38dff778a2b036f4b38a8f3aac2c860c2f58adf6f457784111f3684b28409',
    2026: 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
}
PAYMENTS = 'raw/expanded/WLMM3-payments.html'
PAYMENTS_SHA = '17d22df8973a558debce3d180a190a34ef19083180e40aeea88067a823d3dd3e'
RULE = 'whole_native_entitlement_explicit_type_exact_identity_not_settlement_v1'
APPROVAL_V2_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&'
                   'CodigoInstituicao=1&numProtocolo=1243874&numSequencia=768600&numVersao=2')
APPROVAL_V2_SHA = '445dfabb9386de14ebb186ba6652736be7af9113b64aec54270356a23de11b45'
APPROVAL_V2_FILE = CACHE + '/approval-768600-v2-' + APPROVAL_V2_SHA + '.pdf'
APPROVAL_V2_RECEIPT_SHA = 'd476d94bef87d714f12959abd031337fa24510036a66c008276cbacc348575d6'
APPROVAL_V2_RECEIPT = CACHE + '/response-approval-768600-v2-' + APPROVAL_V2_RECEIPT_SHA + '.json'
APPROVAL_V2_REQUEST_SHA = '8f8b3efa8cad0e26be44014e9da8357e746ffdb439b2142f9448e9bb40791423'
REVIEWED_V2_STATUS = 'verified_latest_native_calendar_with_reviewed_same_family_approval_v2'
PN_PAYMENTS_SHA = '045222b0c911385f5d28b0dd79293616b2e3e7d4b267d537ce6bc1dd52dd9a27'
FOLLOWON_PINS = {
    '779969': ('1', 'fb2ad51d5107390f1481512287edf54ef0429893a491e395e8f6ad64031f1c45',
        '6090662d9ac8a4a4131ae142c2d75493b3a826f4fca642792a1cc26c2c3fb19a',
        'dfeb5ae72c28bc150d3e278351ddf7cd5350016528ef61c9dce0926704a02c10', 'pdf'),
    '779545': ('1', '9dcdaa869855df3008c0b791a8b702fe725bf9379db7757693a93c7618109beb',
        '4293a84c9e18702e4bfeaf566e64f9df2fc33a2866f44da700389db6cca2194b',
        '278108991db4614aa2efd9224c5a23f116688c5530b7ee9c134dc9d69b59e964', 'bin'),
    '781239': ('3', '3a7f44e688521504ec5ba17dd505ca4fdafa7815bcac57b408dea83608f4381a',
        '3dbc3562e63bb1eb3b0111f868aa248d2ca8e39b93ab8d997c3d8a1ab7af68f1',
        'b7e8c01cbf9dd6ac61a693848cf777b85cd92cc6c0801bfb8a3da72989914b97', 'pdf'),
    '506835': ('2', 'bfa65c940a60b35b9f327a39c44bd94b77949ffe8fa86d84bbca025df1e518d2',
        'f4efc455342405eb93dee6831465a8c02173df0ce8cef5b06864ee3a1d5f68b2',
        'd4d49be7d7c8700ffc8496e64e750e5dd0422329b99b73aa25b58027c4d45085', 'pdf'),
}
FCA2026_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
NATIVE_FRE_INDEX_SHA = '8f6fefc0f66cf93eb4ff428bf2b032ff555cff8c43bc39d0d1134fc86348f1b6'
NATIVE_FRE_SHA = '233525cdfddd921517d84d472ec5f31fc2c2acf98a38df2f45e0bfd5339051a9'
NATIVE_FRE_PDF_SHA = '148e171c264299516cbe65ea1d331cfc02604c5d02bfe90acf614ade99afbe2b'
NATIVE_FRE_PDF = CACHE + '/native-FRE125831-annex-103-' + NATIVE_FRE_PDF_SHA + '.pdf'


class ProofError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ProofError(code)


def decimal(value):
    require(type(value) is str, 'explicit_Decimal_string_required')
    try:
        number = cvm.gross(value)
    except (ValueError, InvalidOperation) as error:
        raise ProofError('invalid_explicit_gross') from error
    require(number.is_finite() and number > 0, 'positive_finite_gross_required')
    return number


def day(value):
    require(type(value) is str and cvm.day(value) is not None, 'explicit_valid_date_required')
    return cvm.day(value)


class EvidenceReader:
    def __init__(self, folder):
        self.reader = shared.Reader(folder)
        self.folder = self.reader.folder

    @property
    def sources(self):
        return self.reader.sources

    def read(self, name, role, expected, url=None, retrieved_at=None, published_at=None):
        body, source = self.reader.read(name, role, expected, url)
        require(source.get('url') in (None, url), 'conflicting_source_URL')
        source.update(url=url, retrieved_at=retrieved_at)
        if published_at is not None:
            source['published_at'] = published_at
        return body, source

    def json(self, name, role, expected, **metadata):
        body, source = self.read(name, role, expected, **metadata)
        return json.loads(body), source


def indexed_documents(reader, cutoff):
    manifest, _ = reader.json(IPE_MANIFEST, 'pinned_six_year_IPE_manifest', IPE_MANIFEST_SHA)
    require(type(manifest) is list and len(manifest) == 6, 'six_complete_pinned_IPE_indices_required')
    documents, coverage = [], []
    for year, digest in IPE_HASHES.items():
        choices = [s for s in manifest if s.get('year') == year and type(s.get('year')) is int]
        require(len(choices) == 1, 'missing_or_duplicate_IPE_year')
        source = choices[0]
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        require(source.get('sha256') == digest and source.get('url') == url
                and source.get('status') == 'downloaded_valid_zip', 'pinned_IPE_identity_changed')
        body, receipt = reader.read(source['file'], 'full_native_IPE_all_categories', digest,
                                    url, source.get('retrieved_at'))
        require(len(body) == source['bytes'], 'IPE_receipt_size_mismatch')
        member = f'ipe_cia_aberta_{year}.csv'
        count = 0
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            require(archive.namelist() == [member] and archive.getinfo(member).file_size <= 100_000_000,
                    'invalid_native_IPE_archive')
            with archive.open(member) as stream:
                for line, row in enumerate(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'), 2):
                    if not row['Codigo_CVM'].isdigit() or int(row['Codigo_CVM']) != STOCK['cvm']:
                        continue
                    published = day(row['Data_Entrega'])
                    require(published.startswith(str(year)), 'IPE_delivery_year_mismatch')
                    if published > cutoff.isoformat():
                        continue
                    require(re.sub(r'\D', '', row['CNPJ_Companhia']) == CNPJ, 'IPE_CNPJ_mismatch')
                    sequence, version = cvm.document_id(row['Link_Download'])
                    require(row['Versao'].isdigit() and int(row['Versao']) == version,
                            'IPE_version_URL_mismatch')
                    count += 1
                    documents.append({'cvm': STOCK['cvm'], 'issuer_name': row['Nome_Companhia'],
                        'sequence': sequence, 'version': version, 'published_at': published,
                        'reference_date': day(row['Data_Referencia']), 'url': row['Link_Download'],
                        'category': cvm.norm(row['Categoria']), 'type': cvm.norm(row['Tipo']),
                        'species': cvm.norm(row['Especie']),
                        'index_evidence': {'file': receipt['file'], 'sha256': digest,
                            'url': url, 'member': member, 'csv_line': line, 'raw_row': row}})
        coverage.append({'year': year, 'file': receipt['file'], 'sha256': digest,
                         'issuer_rows_before_cutoff': count, 'all_categories': True})
    return documents, coverage


def select_form(documents, cutoff):
    rows = [d for d in documents if d['reference_date'] == APPROVAL
            and d['published_at'] <= cutoff.isoformat() and d['category'] == 'relatorio proventos']
    require(rows and all(type(d['version']) is int and d['version'] > 0 for d in rows),
            'same_entitlement_native_form_missing')
    latest = [d for d in rows if d['version'] == max(x['version'] for x in rows)
              and d['published_at'] == max(x['published_at'] for x in rows)]
    require(len(latest) == 1, 'ambiguous_latest_native_form')
    doc = latest[0]
    require(doc['sequence'] == '753217' and doc['version'] == 1 and doc['published_at'] == APPROVAL,
            'latest_native_form_unreviewed_no_older_fallback')
    require(cvm.document_id(doc['url']) == (doc['sequence'], doc['version'])
            and doc['index_evidence']['raw_row']['Link_Download'] == doc['url']
            and doc['cvm'] == STOCK['cvm'], 'native_form_index_identity_mismatch')
    return doc


def metadata(tables, label):
    matches = []
    for table in tables:
        for index, row in enumerate(table[:-1]):
            for column, cell in enumerate(row):
                if cvm.norm(cell) == label:
                    require(column < len(table[index + 1]), 'metadata_value_missing')
                    matches.append(table[index + 1][column])
    require(len(matches) == 1 and cvm.compact(matches[0]), 'missing_or_ambiguous_metadata:' + label)
    return matches[0]


def parse_native_tables(tables, document, cutoff):
    require(len(tables) == 3, 'whole_three_table_native_form_required')
    require(cvm.norm(metadata(tables, 'denominacao social')) == cvm.norm(document['issuer_name']),
            'native_index_issuer_name_mismatch')
    require(re.sub(r'\D', '', document['index_evidence']['raw_row']['CNPJ_Companhia']) == CNPJ
            and document['cvm'] == STOCK['cvm'], 'native_index_CNPJ_CVM_mismatch')
    ipe = re.findall(r'\b\d{6}IPE\d{8}\d{2}\d+-\d+\b', metadata(tables, 'protocolo ipe'))
    require(len(ipe) == 1 and ipe[0] == '011070IPE290420240104541110-27', 'native_exact_IPE_required')
    approval = day(metadata(tables, 'data aprovacao'))
    record = day(metadata(tables, 'ultimo dia de negociacao com direitos'))
    sent = day(metadata(tables, 'data envio'))
    require(approval == record == sent == document['reference_date'] == document['published_at'] == APPROVAL
            and sent <= cutoff.isoformat(), 'native_event_dates_or_publication_mismatch')
    require(cvm.compact(metadata(tables, 'versao')) == str(document['version']) == '1'
            and cvm.compact(metadata(tables, 'protocolo provento')) == '1228245',
            'native_version_or_approval_protocol_mismatch')
    require(cvm.norm(metadata(tables, 'ato societario')) == 'ago', 'native_approval_kind_mismatch')
    table = tables[1]
    require(len(table) == 6 and len(table[1]) == 7
            and cvm.norm(table[1][0]) == 'codigo isin'
            and cvm.norm(table[1][1]) == 'valor bruto r unidade'
            and cvm.norm(table[1][6]) == 'data pagamento', 'entire_four_component_gross_table_required')
    require(len(tables[2]) == 1 and len(tables[2][0]) == 2
            and cvm.norm(tables[2][0][0]) == 'dia de pagamento das parcelas'
            and cvm.norm(tables[2][0][1]) == 'valor bruto r unidade', 'unexpected_installments_or_net_annex')
    rows = []
    for index, row in enumerate(table[2:], 2):
        require(len(row) == 7, 'malformed_native_row_must_not_be_skipped')
        isin = cvm.compact(row[0])
        shares = [share for share, (_, code) in CLASSES.items() if code == isin]
        require(len(shares) == 1, 'unknown_or_cross_class_native_ISIN')
        # Native wrapping splits a decimal's last digit, not its monetary unit.
        gross = decimal(cvm.compact(row[1]))
        period, year = cvm.norm(cvm.compact(row[2])), cvm.compact(row[3])
        require((period, year) in {('anual', '2023'), ('complementar', '2022')},
                'native_component_period_missing_or_changed')
        payment = day(row[6])
        require(payment >= record and cvm.norm(row[4]) == 'nao' and cvm.norm(row[5]) == 'a vista',
                'native_payment_order_update_or_form_unresolved')
        rows.append({'share_class': shares[0], 'isin': isin, 'gross_per_share_decimal': str(gross),
                     'period': period, 'fiscal_year': year, 'payment_date': payment,
                     'source_locator': {'page': 1, 'table': 1, 'row': index}})
    require(Counter((r['share_class'], r['period'], r['fiscal_year']) for r in rows) == Counter(
        (share, period, year) for share in CLASSES for period, year in [('anual', '2023'), ('complementar', '2022')]),
        'both_classes_and_all_components_required_no_duplicate_or_partial_group')
    return {'cvm': STOCK['cvm'], 'issuer_cnpj': CNPJ, 'native_protocol': '1228245', 'native_IPE': ipe[0],
        'version': document['version'], 'approval_date': approval, 'record_date': record,
        'sent_at': sent, 'published_at': document['published_at'], 'cash_kind': None, 'components': rows,
        'gross_totals_by_class': {share: str(sum((Decimal(r['gross_per_share_decimal'])
            for r in rows if r['share_class'] == share), Decimal(0))) for share in CLASSES},
        'tax_basis': 'explicit_gross_BRL_per_exact_ISIN_unit', 'gross_reconstructed_from_net': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}


def approval_revision_review(documents, form, cutoff):
    def family(value):
        match = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', value)
        return (match[1], match[2]) if match else None
    bound_family = family(form['native_IPE'])
    related = [d for d in documents if d['published_at'] <= cutoff.isoformat()
               and family(d['index_evidence']['raw_row']['Protocolo_Entrega']) == bound_family]
    require(related, 'approval_protocol_family_missing_from_full_IPE_indices')
    require(all(d['category'] == 'assembleia' and d['type'] == 'ago' and d['species'] == 'ata'
                and d['cvm'] == STOCK['cvm'] and type(d['version']) is int for d in related),
            'approval_document_identity_or_kind_mismatch')
    latest = [d for d in related if d['version'] == max(r['version'] for r in related)
              and d['published_at'] == max(r['published_at'] for r in related)]
    require(len(latest) == 1, 'ambiguous_latest_approval_revision')
    current = latest[0]
    raw = current['index_evidence']['raw_row']
    require(cvm.document_id(current['url']) == (current['sequence'], current['version'])
            and raw['Link_Download'] == current['url'] and raw['Codigo_CVM'] == str(STOCK['cvm'])
            and re.sub(r'\D', '', raw['CNPJ_Companhia']) == CNPJ
            and raw['Versao'] == str(current['version'])
            and day(raw['Data_Entrega']) == current['published_at']
            and day(raw['Data_Referencia']) == current['reference_date']
            and int(re.fullmatch(r'\d{6}IPE\d{8}(\d{2})\d+-\d+', raw['Protocolo_Entrega'])[1]) == current['version'],
            'latest_approval_exact_index_metadata_mismatch')
    protocol = parse_qs(urlsplit(current['url']).query)['numProtocolo'][0]
    exact = (current['version'] == form['version'] and protocol == form['native_protocol']
             and raw['Protocolo_Entrega'] == form['native_IPE']
             and current['reference_date'] == form['approval_date']
             and current['published_at'] <= form['sent_at'])
    return {'status': 'verified_exact_current_approval_protocol' if exact else 'blocked_later_approval_revision_unreviewed',
            'repair_allowed': exact, 'native_referenced_IPE': form['native_IPE'],
            'native_referenced_protocol': form['native_protocol'], 'latest_approval_document': current,
            'same_protocol_family_records': related,
            'unreviewed_latest_document_URL': None if exact else current['url'],
            'claim_later_revision_does_not_change_dividends': False}


def review_approval_pages(pages, document, form):
    """Review the complete exact v2, without inventing restated unit/date fields.

    The latest minutes approve the same event's mandatory/additional dividends;
    they do not restate the native form's per-share amounts or payment dates.
    We do not claim byte/content equality with an unavailable original v1.
    """
    require(len(pages) == 8 and all(type(p) is str and len(p) > 300 for p in pages),
            'whole_eight_page_latest_approval_body_required')
    require(document['sequence'] == '768600' and document['version'] == 2
            and document['url'] == APPROVAL_V2_URL and document['reference_date'] == form['approval_date']
            and document['published_at'] == '2024-05-29', 'reviewed_exact_approval_v2_identity_required')
    texts = [cvm.norm(p) for p in pages]
    require(all('cnpj no 33 228 024 0001 51' in t for t in texts)
            and 'ata da assembleia geral ordinaria de 29 de abril de 2024' in texts[0]
            and 'rio de janeiro 29 de abril de 2024' in texts[7], 'approval_v2_CNPJ_meeting_or_signature_mismatch')
    page = texts[2]
    required = [
        'item 3 da ordem do dia que aprovou',
        'ratificacao dos juros sobre capital proprio distribuidos durante o exercicio de 2023',
        'restando ainda o saldo de r 1 229 496 77',
        'item 4 da ordem do dia tendo sido aprovado',
        'distribuicao de dividendo obrigatorio sobre o lucro liquido ajustado do exercicio de 2023',
        'resulta no saldo obrigatorio de dividendos a pagar de r 1 229 496 77',
        'item 5 da ordem do dia que aprovou',
        'distribuicao de dividendo adicional com base em lucro de exercicios anteriores',
        'no valor de r 24 237 822 41',
        'imputando o pagamento a conta de reserva para garantia de pagamento de dividendos',
    ]
    require(all(span in page for span in required), 'latest_approval_complete_dividend_resolutions_changed')
    whole = ' '.join(texts)
    require(not any(token in whole for token in ('retificacao', 'retificar', 'reagendamento',
        'alteracao da data de pagamento', 'novo calendario', 'cancelamento dos dividendos')),
        'latest_approval_possible_cash_amendment_requires_new_review')
    require('28 de junho de 2024' not in whole and '28 06 2024' not in whole
            and all(cvm.norm(r['gross_per_share_decimal'].replace('.', ',')) not in whole
                    for r in form['components']), 'latest_approval_unexpected_unit_or_payment_fields_require_review')
    return {'document': deepcopy(document), 'page_count': len(pages), 'whole_body_reviewed': True,
        'approval_date': form['approval_date'], 'dividend_resolution_page': 3,
        'mandatory_outstanding_total_BRL': '1229496.77', 'additional_total_BRL': '24237822.41',
        'explicit_current_cash_kind': 'DIVIDENDO',
        'individual_per_share_amounts_restated': False, 'record_date_restated': False,
        'payment_date_restated': False, 'native_fields_inferred_from_aggregate_totals': False,
        'explicit_conflicting_cash_amendment_detected': False,
        'unavailable_v1_body_equality_verified': False, 'legal_priority_claim': False,
        'schedule_source_remains': 'latest_complete_native_form_753217v1',
        'settlement_verified': False, 'point_in_time_archive_verified': False}


def read_reviewed_approval_v2(reader, document, form):
    request, _ = reader.json(CACHE + '/request-approval-768600-v2.json', 'single_CVM_transport_request',
                             APPROVAL_V2_REQUEST_SHA)
    receipt, _ = reader.json(APPROVAL_V2_RECEIPT, 'actual_CVM_200_transport_receipt', APPROVAL_V2_RECEIPT_SHA)
    require(request['url'] == receipt['url'] == receipt['response_url'] == APPROVAL_V2_URL
            and receipt['document'] == request['document'] == document
            and receipt['http_status'] == 200 and receipt['network_requests'] == 1
            and receipt['retries_performed'] == 0 and receipt['redirects'] is False
            and receipt['access_stop'] is False and receipt['sha256'] == APPROVAL_V2_SHA
            and receipt['file'] == APPROVAL_V2_FILE, 'approval_v2_transport_or_exact_index_receipt_mismatch')
    body, source = reader.read(APPROVAL_V2_FILE, 'whole_reviewed_latest_same_family_approval', APPROVAL_V2_SHA,
                               APPROVAL_V2_URL, receipt['retrieved_at'], document['published_at'])
    source['authority'] = 'primary_CVM_exact_latest_approval_not_settlement'
    require(len(body) == receipt['bytes'] and body.lstrip().startswith(b'%PDF-'), 'approval_v2_actual_PDF_required')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    require(not pdf.is_encrypted and len(pdf.pages) == 8, 'whole_eight_page_approval_PDF_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        pages = [p.extract_text() or '' for p in pdf.pages]
    proof = review_approval_pages(pages, document, form)
    proof['source_receipt'] = deepcopy(source)
    return proof


def cached_sources(reader):
    manifest, _ = reader.json(EXCHANGE_MANIFEST, 'pinned_existing_acquisition_manifest', EXCHANGE_MANIFEST_SHA)
    payloads, receipts = {}, {}
    for name, role in [('expanded/b3-detail-11070.json', 'cached_exact_CVM_CNPJ_ticker_ISIN_identity'),
                       ('expanded/WLMM4-b3-cash-1.json', 'cached_explicit_cash_type_whole_event_no_network'),
                       ('expanded/WLMM3-payments.html', 'original_vendor_calendar_read_only')]:
        # The original acquisition ledger duplicates the identical issuer detail.
        found = {json.dumps(s, sort_keys=True): s for s in manifest['sources'] if s.get('file') == name}
        require(len(found) == 1, 'one_unambiguous_existing_source_receipt_required')
        receipt = next(iter(found.values()))
        body, source = reader.read('raw/' + name, role, receipt['sha256'], receipt['url'], receipt.get('retrieved_at'))
        require(len(body) == receipt['bytes'], 'source_receipt_size_mismatch')
        if name.endswith('.html'):
            require(source['sha256'] == PAYMENTS_SHA, 'unreviewed_vendor_calendar_bytes')
        payloads[name] = body if name.endswith('.html') else json.loads(body)
        receipts[name] = source
    return payloads, receipts


def prove_cash_kind(form, detail, payload):
    require(detail.get('cnpj') == CNPJ and str(detail.get('codeCVM')) == str(STOCK['cvm']),
            'cached_CNPJ_CVM_identity_mismatch')
    for share, (ticker, isin) in CLASSES.items():
        require([r for r in detail.get('otherCodes', []) if r.get('code') == ticker] == [{'code': ticker, 'isin': isin}],
                'cached_exact_class_ticker_ISIN_identity_mismatch')
    page = payload.get('page', {})
    require(type(payload.get('results')) is list and page == {
        'pageNumber': 1, 'pageSize': 120, 'totalRecords': len(payload['results']), 'totalPages': 1},
        'complete_cached_cash_pagination_required')
    located = [{'row': n, 'raw': r} for n, r in enumerate(payload['results'])
               if cvm.day(r.get('dateApproval')) == form['approval_date']
               and cvm.day(r.get('lastDatePriorEx')) == form['record_date']]
    require(len(located) == 4, 'whole_four_row_explicit_cash_event_required')
    actual = []
    for item in located:
        row = item['raw']
        require(row.get('typeStock') in CLASSES and row.get('corporateAction') == 'DIVIDENDO',
                'explicit_cash_kind_or_class_conflict_not_inferred')
        require(row.get('quotedPerShares') == '1' and row.get('ratio') == '1',
                'exact_unscaled_one_share_unit_required')
        actual.append((row['typeStock'], decimal(row['valueCash'])))
    require(Counter(actual) == Counter((r['share_class'], Decimal(r['gross_per_share_decimal']))
                                      for r in form['components']), 'whole_class_gross_components_mismatch')
    return {'label': 'DIVIDENDO', 'basis': 'explicit_complete_cached_exchange_event_bound_to_native_both_ISIN_classes',
            'located_rows': located, 'kind_inferred_from_amount_or_status': False,
            'native_cash_kind_reported': False, 'tax_basis': form['tax_basis'],
            'gross_reconstructed_from_net': False, 'settlement_verified': False}


def inspect_evidence(folder, stock, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'reviewed_exact_cutoff_required')
    require(all(type(stock.get(k)) is type(v) and stock[k] == v for k, v in STOCK.items()),
            'reviewed_exact_WLMM3_identity_required')
    reader = EvidenceReader(folder)
    documents, coverage = indexed_documents(reader, cutoff)
    document = select_form(documents, cutoff)
    receipt, _ = reader.json(FORM.removesuffix('.pdf') + '-source.json', 'native_acquisition_receipt', RECEIPT_SHA)
    body, source = reader.read(FORM, 'primary_exact_native_calendar', FORM_SHA, document['url'],
                               receipt.get('retrieved_at'), document['published_at'])
    source['authority'] = 'primary_CVM_reported_calendar_not_settlement'
    require(all(receipt.get(k) == source[k] for k in ('file', 'sha256', 'url', 'bytes'))
            and all(receipt['document'].get(k) == document[k] for k in
                    ('cvm', 'sequence', 'version', 'published_at', 'reference_date', 'url'))
            and receipt['document']['index_sha256'] == IPE_HASHES[2024]
            and receipt['document']['index_url'] == document['index_evidence']['url']
            and parse_qs(urlsplit(source['url']).query)['numProtocolo'] == ['1228491'],
            'native_acquisition_receipt_or_download_protocol_mismatch')
    require(body.lstrip().startswith(b'%PDF-') and len(body) < 8_000_000, 'bounded_native_PDF_required')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    require(not pdf.is_encrypted and len(pdf.pages) == 1, 'exact_one_page_native_form_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = pdf.pages[0].extract_tables()
    form = parse_native_tables(tables, document, cutoff)
    payloads, receipts = cached_sources(reader)
    proof = prove_cash_kind(form, payloads['expanded/b3-detail-11070.json'], payloads['expanded/WLMM4-b3-cash-1.json'])
    proof['source_receipts'] = deepcopy([receipts[n] for n in
        ('expanded/b3-detail-11070.json', 'expanded/WLMM4-b3-cash-1.json')])
    revision = approval_revision_review(documents, form, cutoff)
    if not revision['repair_allowed']:
        reviewed = read_reviewed_approval_v2(reader, revision['latest_approval_document'], form)
        revision.update(status=REVIEWED_V2_STATUS, repair_allowed=True,
            reviewed_latest_approval=reviewed, unreviewed_latest_document_URL=None,
            modelling_rule='latest_structured_calendar_plus_reviewed_same_family_approval_no_explicit_schedule_conflict',
            per_share_and_payment_date_evidence_remains_native_form=True)
    return {**STOCK, 'as_of': cutoff.isoformat(), 'native_form': form, 'cash_type_proof': proof,
        'document': document, 'IPE_coverage': coverage, 'approval_revision_review': revision,
        'evidence_sources': deepcopy(list(reader.sources.values())), 'rule': RULE,
        'repair_allowed': revision['repair_allowed'], 'settlement_verified': False,
        'point_in_time_archive_verified': False, 'cash_model_eligible': False,
        'parent_admission_performed': False, 'active_model_changed': False}


def verified_calendar(folder, stock, cutoff=CUTOFF):
    calendar = inspect_evidence(folder, stock, cutoff)
    require(calendar['repair_allowed'], calendar['approval_revision_review']['status'])
    return calendar


def _repair_parsed(parsed, calendar):
    """Atomic transform, used only after all real-byte and latest-version proofs."""
    require(calendar.get('repair_allowed') is True and calendar['approval_revision_review']['repair_allowed'] is True
            and calendar['approval_revision_review']['status'] in
                ('verified_exact_current_approval_protocol', REVIEWED_V2_STATUS),
            'unreviewed_revision_cannot_repair_calendar')
    require(parsed.get('report', {}).get('sha256') == PAYMENTS_SHA
            and parsed['report'].get('as_of') == calendar['as_of']
            and parsed.get('source_format') == 'html_tables', 'inspected_vendor_source_or_cutoff_mismatch')
    group = [e for e in parsed['events'] if e.get('record_date') == APPROVAL]
    components = [r for r in calendar['native_form']['components'] if r['share_class'] == STOCK['share_class']]
    require(len(group) == len(components) == 2, 'atomic_complete_two_ON_components_required')
    require(len(calendar['native_form']['components']) == 4
            and calendar['cash_type_proof']['label'] == 'DIVIDENDO'
            and calendar['cash_type_proof']['gross_reconstructed_from_net'] is False
            and calendar['native_form']['tax_basis'] == 'explicit_gross_BRL_per_exact_ISIN_unit',
            'full_both_class_explicit_gross_type_proof_required')
    matched = []
    for native in components:
        rows = [e for e in group if e.get('gross_per_share') is not None
                and Decimal(str(e['gross_per_share'])) == Decimal(native['gross_per_share_decimal'])]
        require(len(rows) == 1, 'unique_entire_native_vendor_gross_group_required')
        event = rows[0]
        require(event.get('ticker') == STOCK['ticker'] and event.get('label') == 'DIVIDENDO'
                and event.get('quoted_quantity') == 1 and type(event.get('quoted_quantity')) is int
                and event.get('isin') in (None, STOCK['isin'])
                and event.get('approval_date') in (None, APPROVAL)
                and event.get('published_at') in (None, calendar['native_form']['published_at'])
                and Decimal(str(event.get('reported_amount'))) == Decimal(native['gross_per_share_decimal'])
                and event.get('reported_status') is None, 'vendor_class_type_gross_unit_or_source_conflict')
        require(event['payment_date'] == native['payment_date'] or (
            event.get('date_order_valid') is False and day(event['payment_date']) < event['record_date']),
            'only_malformed_payment_before_record_may_be_repaired')
        matched.append((event, native))
    require(len({e['source_row'] for e, _ in matched}) == 2, 'duplicate_vendor_source_row')
    malformed = [e['source_row'] for e, n in matched if e['payment_date'] != n['payment_date']]
    issues = parsed['report']['issues']
    removable = [i for i in issues if i.get('row') in malformed and i.get('code') == 'payment_before_record_date'
                 and i.get('blocking') is True]
    require(len(malformed) == len(removable) == 1, 'one_specific_matched_date_blocker_required')
    require(not any(i.get('blocking') and i.get('row') in {e['source_row'] for e in group}
                    and i not in removable for i in issues), 'other_matched_group_source_issue_must_not_be_removed')
    out = deepcopy(parsed)
    primary = next(s for s in calendar['evidence_sources'] if s['file'] == FORM)
    group_id = 'WLMM3:2024-04-29'
    for original, native in matched:
        event = next(e for e in out['events'] if e['source_row'] == original['source_row'])
        event.update(payment_date=native['payment_date'], date_order_valid=True,
            status='past_scheduled' if native['payment_date'] <= calendar['as_of'] else 'announced',
            primary_evidence=[deepcopy(primary)], primary_declaration_id=group_id,
            payment_date_basis='explicit_native_CVM_reported_schedule_not_settlement',
            payment_claim_basis='declared_calendar_not_bank_settlement',
            full_settlement_on_date_verified=False, cash_model_eligible=False, financially_confirmed=False,
            native_calendar_component=deepcopy(native))
    out['report']['issues'] = [deepcopy(i) for i in issues if i not in removable]
    inspector._finish(out)
    out['native_calendar_repair'] = {'rule': RULE, 'group_id': group_id,
        'original_group': deepcopy(group), 'original_issues': deepcopy(issues), 'removed_issues': deepcopy(removable),
        'recomputed_added_issues': [deepcopy(i) for i in out['report']['issues'] if i not in issues],
        'complete_group_matched': True, 'matched_components': len(matched), 'changed_date_count': 1,
        'gross_amounts_changed': False, 'unmatched_rows_changed': False,
        'evidence_sources': deepcopy(calendar['evidence_sources']),
        'approval_revision_review': deepcopy(calendar['approval_revision_review']),
        'published_at': calendar['native_form']['published_at'],
        'declared_payment_date': components[0]['payment_date'], 'settlement_verified': False,
        'point_in_time_archive_verified': False,
        'required_parent_gates': ['independent_cash_reconciliation', 'independent_raw_price_reconciliation',
                                  'normal_share_action_reconciliation', 'normal_annual_financial_and_training_gates'],
        'parent_admission_performed': False}
    return out


def repair_inspected_calendar(folder, stock, cutoff, parsed):
    """Return a new parsed dict or fail without mutation; never changes raw HTML."""
    calendar = verified_calendar(folder, stock, cutoff)
    source = next(s for s in calendar['evidence_sources'] if s['file'] == PAYMENTS)
    observed = datetime.fromisoformat(parsed['report']['observed_at'])
    require(observed.utcoffset() is not None and observed.date() >= cutoff, 'valid_inspector_observation_required')
    body = shared._safe(Path(folder).resolve(), PAYMENTS).read_bytes()
    require(hashlib.sha256(body).hexdigest() == source['sha256'], 'vendor_changed_after_proof')
    expected = inspector.inspect_payment_page(body, stock['ticker'], cutoff, observed)
    require(parsed == expected, 'parsed_events_or_issues_not_exact_original_inspector_output')
    return _repair_parsed(parsed, calendar)


def save_report(folder, report):
    body = shared._dump(report)
    name = CACHE + '/report-' + hashlib.sha256(body).hexdigest() + '.json'
    path = shared._safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_owned_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def save_owned(folder, filename, body):
    require(re.fullmatch(r'[a-zA-Z0-9_.-]+', filename) is not None, 'unsafe_owned_filename')
    path = shared._safe(Path(folder).resolve(), CACHE + '/' + filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_owned_file_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(Path(folder).resolve()).as_posix()


def acquire_approval_v2(folder):
    """One explicitly requested public CVM attempt, with durable no-retry guard."""
    reader = EvidenceReader(folder)
    documents, _ = indexed_documents(reader, CUTOFF)
    found = [d for d in documents if d['url'] == APPROVAL_V2_URL]
    require(len(found) == 1 and found[0]['sequence'] == '768600' and found[0]['version'] == 2
            and found[0]['reference_date'] == APPROVAL, 'exact_observed_approval_v2_index_required')
    name = CACHE + '/request-approval-768600-v2.json'
    require(not shared._safe(Path(folder).resolve(), name).exists(), 'one_request_already_attempted_no_retry')
    request = {'url': APPROVAL_V2_URL, 'ordinal': 1, 'max_requests': 1,
        'started_at': datetime.now(timezone.utc).isoformat(), 'redirects': False,
        'redirect_policy': 'no_redirects_stricter_than_same_host_only', 'proxy': False, 'retries': 0,
        'document': found[0]}
    save_owned(folder, 'request-approval-768600-v2.json', shared._dump(request))
    result = {**request, 'http_status': None, 'status': 'failed', 'transport_headers': {}}
    body = b''
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())
    try:
        req = urllib.request.Request(APPROVAL_V2_URL, headers={'User-Agent': 'RendaFutura-Academic/1.0',
            'Accept': 'application/pdf,application/octet-stream;q=0.9', 'Accept-Encoding': 'identity'})
        with opener.open(req, timeout=45) as response:
            result.update(http_status=response.status, response_url=response.url,
                transport_headers={key: response.headers.get(key) for key in
                    ('Content-Type', 'Content-Length', 'Content-Disposition', 'Date', 'Last-Modified')})
            require(response.url == APPROVAL_V2_URL, 'unrequested_redirect_no_further_requests')
            body = response.read(12_000_001)
        require(len(body) <= 12_000_000 and body.lstrip().startswith(b'%PDF-'), 'bounded_actual_PDF_response_required')
        result['status'] = 'received_valid_PDF_signature_pending_whole_body_review'
    except urllib.error.HTTPError as error:
        result.update(http_status=error.code, error=type(error).__name__)
        body = error.read(12_000_001)[:12_000_000]
    except (ValueError, OSError) as error:
        result['error'] = str(error)
    result.update(retrieved_at=datetime.now(timezone.utc).isoformat(), sha256=hashlib.sha256(body).hexdigest(),
                  bytes=len(body), access_stop=result['http_status'] in (401, 403, 429),
                  network_requests=1, retries_performed=0, settlement_verified=False)
    extension = 'pdf' if result['status'].startswith('received_valid_PDF') else 'bin'
    result['file'] = save_owned(folder, 'approval-768600-v2-' + result['sha256'] + '.' + extension, body)
    receipt = save_owned(folder, 'response-approval-768600-v2-' + hashlib.sha256(shared._dump(result)).hexdigest()
                         + '.json', shared._dump(result))
    return result, receipt


def followon_documents(documents):
    """Select all latest event revisions before the separately authorized phase."""
    selections = {}
    for role, reference, category, sequence, version in (
        ('jcp_form', '2024-06-28', 'relatorio proventos', '779969', 1),
        ('jcp_notice', '2024-06-28', 'aviso aos acionistas', '779545', 1),
        ('jcp_board', '2024-06-28', 'reuniao da administracao', '781239', 3),
        ('ago2022', '2022-04-29', 'assembleia', '506835', 2)):
        seeds = [d for d in documents if d['reference_date'] == reference and d['category'] == category]
        def family(d):
            m = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', d['index_evidence']['raw_row']['Protocolo_Entrega'])
            return (m[1], m[2]) if m else None
        families = {family(d) for d in seeds if family(d)}
        rows = [d for d in documents if (d['reference_date'] == reference or family(d) in families) and d['category'] == category
                and (role != 'ago2022' or (d['type'] == 'ago' and d['species'] == 'ata'))]
        require(rows, 'followon_exact_document_missing:' + role)
        latest = [d for d in rows if d['version'] == max(r['version'] for r in rows)
                  and d['published_at'] == max(r['published_at'] for r in rows)]
        require(len(latest) == 1 and latest[0]['sequence'] == sequence and latest[0]['version'] == version,
                'followon_latest_revision_not_reviewed:' + role)
        selections[role] = latest[0]
    return selections


def acquire_followon(folder):
    """Four additional exact indexed requests, one per document, never retried."""
    reader = EvidenceReader(folder)
    documents, _ = indexed_documents(reader, CUTOFF)
    selected = followon_documents(documents)
    previous_responses = list((Path(folder) / CACHE).glob('response-followon-*.json'))
    require(not any(json.loads(p.read_bytes()).get('access_stop') for p in previous_responses),
            'persisted_followon_access_stop_no_more_requests')
    outcomes = []
    for role, document in selected.items():
        sequence, version = document['sequence'], document['version']
        stem = 'followon-' + sequence + '-v' + str(version)
        request_name = 'request-' + stem + '.json'
        request_path = shared._safe(Path(folder).resolve(), CACHE + '/' + request_name)
        if request_path.exists():
            outcomes.append({'role': role, 'status': 'already_attempted_no_retry', 'sequence': sequence})
            continue
        prior = list((Path(folder) / CACHE).glob('request-followon-*.json'))
        require(len(prior) < 4, 'four_followon_request_phase_budget_exhausted')
        request = {'url': document['url'], 'document': document, 'role': role,
            'phase': 'explicit_four_additional_CVM_originals', 'ordinal': len(prior) + 1,
            'phase_max_requests': 4, 'prior_approval_phase_requests': 1,
            'started_at': datetime.now(timezone.utc).isoformat(), 'redirects': False, 'proxy': False, 'retries': 0}
        save_owned(folder, request_name, shared._dump(request))
        result = {**request, 'http_status': None, 'status': 'failed', 'transport_headers': {}}
        body = b''
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())
            req = urllib.request.Request(document['url'], headers={'User-Agent': 'RendaFutura-Academic/1.0',
                'Accept': 'application/pdf,application/octet-stream;q=0.9', 'Accept-Encoding': 'identity'})
            with opener.open(req, timeout=45) as response:
                result.update(http_status=response.status, response_url=response.url,
                    transport_headers={k: response.headers.get(k) for k in
                        ('Content-Type', 'Content-Disposition', 'Content-Length', 'Date', 'Last-Modified')})
                require(response.url == document['url'], 'unrequested_redirect_no_followup')
                body = response.read(12_000_001)
            require(len(body) <= 12_000_000 and body.lstrip().startswith(b'%PDF-'), 'bounded_actual_PDF_required')
            result['status'] = 'received_valid_PDF_signature_pending_review'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, error=type(error).__name__)
            body = error.read(12_000_001)[:12_000_000]
        except (OSError, ValueError) as error:
            result['error'] = str(error)
        result.update(retrieved_at=datetime.now(timezone.utc).isoformat(),
                      sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
                      access_stop=result['http_status'] in (401, 403, 429), network_requests=1, retries_performed=0)
        extension = 'pdf' if result['status'].startswith('received_valid_PDF') else 'bin'
        result['file'] = save_owned(folder, stem + '-' + result['sha256'] + '.' + extension, body)
        receipt = save_owned(folder, 'response-' + stem + '-' + hashlib.sha256(shared._dump(result)).hexdigest()
                             + '.json', shared._dump(result))
        outcomes.append({'role': role, 'receipt': receipt, **{k: result[k] for k in
            ('status', 'http_status', 'file', 'sha256', 'bytes', 'network_requests')}})
        if result['access_stop']:
            break
    return outcomes


def acquire_fre2022_v2(folder):
    """One separately authorized issuer original; no date inferred from its URL."""
    url = 'https://www.wlm.com.br/wp-content/uploads/2022/08/WLM-FR-2022-V2.pdf'
    filename = 'request-issuer-FRE2022-v2.json'
    require(not shared._safe(Path(folder).resolve(), CACHE + '/' + filename).exists(),
            'issuer_FRE_request_already_attempted_no_retry')
    request = {'url': url, 'max_requests': 1, 'cumulative_authorized_request_max': 6,
        'started_at': datetime.now(timezone.utc).isoformat(), 'redirects': False, 'proxy': False, 'retries': 0,
        'publication_date_inferred_from_URL': False}
    save_owned(folder, filename, shared._dump(request))
    result = {**request, 'status': 'failed', 'http_status': None, 'transport_headers': {}}
    body = b''
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())
        req = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0',
            'Accept': 'application/pdf', 'Accept-Encoding': 'identity'})
        with opener.open(req, timeout=60) as response:
            result.update(http_status=response.status, response_url=response.url,
                transport_headers={k: response.headers.get(k) for k in
                    ('Content-Type', 'Content-Length', 'Date', 'Last-Modified')})
            require(response.url == url, 'issuer_redirect_not_followed')
            body = response.read(25_000_001)
        require(len(body) <= 25_000_000 and body.lstrip().startswith(b'%PDF-'), 'bounded_issuer_PDF_required')
        result['status'] = 'received_valid_PDF_signature_pending_full_review'
    except urllib.error.HTTPError as error:
        result.update(http_status=error.code, error=type(error).__name__)
        body = error.read(25_000_001)[:25_000_000]
    except (ValueError, OSError) as error:
        result['error'] = str(error)
    result.update(sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
        retrieved_at=datetime.now(timezone.utc).isoformat(), access_stop=result['http_status'] in (401, 403, 429),
        network_requests=1, retries_performed=0, published_at=None, exact_CVM_blob_identity_verified=False)
    ext = 'pdf' if result['status'].startswith('received_valid_PDF') else 'bin'
    result['file'] = save_owned(folder, 'issuer-FRE2022-v2-' + result['sha256'] + '.' + ext, body)
    name = save_owned(folder, 'response-issuer-FRE2022-v2-' + hashlib.sha256(shared._dump(result)).hexdigest()
                       + '.json', shared._dump(result))
    return result, name


def read_followon(reader, selected):
    result = {}
    for role, document in selected.items():
        seq = document['sequence']
        version, digest, receipt_sha, request_sha, extension = FOLLOWON_PINS[seq]
        stem = 'followon-' + seq + '-v' + version
        request, _ = reader.json(CACHE + '/request-' + stem + '.json', 'followon_actual_transport_request', request_sha)
        receipt, _ = reader.json(CACHE + '/response-' + stem + '-' + receipt_sha + '.json',
                                  'followon_actual_transport_response', receipt_sha)
        require(request['document'] == receipt['document'] == document
                and request['url'] == receipt['url'] == receipt['response_url'] == document['url']
                and receipt['http_status'] == 200 and receipt['network_requests'] == 1
                and receipt['redirects'] is False and receipt['retries_performed'] == 0
                and receipt['access_stop'] is False and receipt['sha256'] == digest,
                'followon_exact_transport_and_index_binding_required:' + role)
        name = CACHE + '/' + stem + '-' + digest + '.' + extension
        require(receipt['file'] == name, 'followon_receipt_filename_mismatch')
        body, source = reader.read(name, 'reviewed_followon_' + role, digest, document['url'],
                                    receipt['retrieved_at'], document['published_at'])
        require(len(body) == receipt['bytes'], 'followon_receipt_size_mismatch')
        if extension == 'pdf':
            require(body.lstrip().startswith(b'%PDF-'), 'followon_original_PDF_required')
            source['authority'] = 'primary_CVM_original_reported_not_settlement'
        else:
            require(not body.lstrip().startswith(b'%PDF-') and b'ENET Download de Documento' in body,
                    'reviewed_unusable_notice_shell_changed_requires_review')
            source['authority'] = 'CVM_download_HTML_shell_not_notice_contents'
        result[role] = {'body': body, 'source': source, 'document': document,
                        'status': 'native_original_read' if extension == 'pdf' else 'unusable_HTML_shell_no_retry'}
    return result


def pdf_pages(body, expected_pages):
    pdf = PdfReader(io.BytesIO(body), strict=True)
    require(not pdf.is_encrypted and len(pdf.pages) == expected_pages, 'whole_expected_PDF_body_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        return [p.extract_text() or '' for p in pdf.pages], [t for p in pdf.pages for t in p.extract_tables()]


def parse_jcp_form(tables, document):
    require(len(tables) == 3 and document['sequence'] == '779969' and document['version'] == 1,
            'exact_latest_JCP_form_required')
    require(cvm.norm(metadata(tables, 'denominacao social')) == cvm.norm(document['issuer_name'])
            and cvm.compact(metadata(tables, 'protocolo provento')) == '1254819'
            and '011070IPE280620240104547111-09' in metadata(tables, 'protocolo ipe')
            and cvm.compact(metadata(tables, 'versao')) == '1'
            and cvm.norm(metadata(tables, 'ato societario')) == 'rca', 'JCP_exact_notice_protocol_or_identity_mismatch')
    approval = day(metadata(tables, 'data aprovacao'))
    record = day(metadata(tables, 'ultimo dia de negociacao com direitos'))
    sent = day(metadata(tables, 'data envio'))
    require(approval == sent == document['reference_date'] == '2024-06-28'
            and document['published_at'] == '2024-07-01' and approval <= sent <= document['published_at'] <= record,
            'JCP_explicit_metadata_dates_invalid')
    table = tables[1]
    require(len(table) == 4 and len(table[1]) == 7
            and cvm.norm(table[1][0]) == 'codigo isin' and cvm.norm(table[1][1]) == 'valor bruto r unidade'
            and cvm.norm(table[1][6]) == 'data pagamento' and len(tables[2]) == 1,
            'whole_two_ISIN_gross_vista_table_required')
    rows = []
    for i, row in enumerate(table[2:], 2):
        require(len(row) == 7, 'malformed_JCP_component_must_not_be_skipped')
        isin = cvm.compact(row[0]); classes = [s for s, (_, v) in CLASSES.items() if v == isin]
        require(len(classes) == 1, 'exact_JCP_ISIN_required')
        require(cvm.norm(row[2]) == 'anual' and cvm.compact(row[3]) == '2024'
                and cvm.norm(row[4]) == 'nao' and cvm.norm(row[5]) == 'a vista', 'explicit_JCP_period_and_payment_form_required')
        payment = day(row[6])
        require(payment >= record, 'JCP_payment_before_record')
        rows.append({'share_class': classes[0], 'isin': isin, 'record_date': record,
            'approval_date': approval, 'payment_date': payment,
            'gross_per_share_decimal': str(decimal(cvm.compact(row[1]))),
            'source_locator': {'format': 'complete_native_ISIN_rows', 'page': 1, 'table': 1, 'row': i}})
    require(Counter(r['share_class'] for r in rows) == Counter(CLASSES.keys()), 'both_JCP_classes_required')
    return {'approval_date': approval, 'record_date': record, 'sent_at': sent,
        'published_at': document['published_at'], 'native_protocol': '1254819',
        'native_IPE': '011070IPE280620240104547111-09', 'cash_kind': None,
        'components': rows, 'tax_basis': 'explicit_gross_BRL_per_exact_ISIN_unit',
        'gross_reconstructed_from_net': False, 'settlement_verified': False}


def review_jcp_board(pages, document, form):
    require(len(pages) == 2 and document['sequence'] == '781239' and document['version'] == 3
            and document['reference_date'] == form['approval_date'] and document['published_at'] == '2024-07-03',
            'latest_whole_JCP_board_v3_required')
    p1, p2 = (cvm.norm(p) for p in pages)
    require('cnpj no 33 228 024 0001 51' in p1
            and 'ata de reuniao do conselho de administracao de 28 06 2024' in p1
            and 'distribuicao de juros sobre o capital proprio' in p1
            and '15 quinze por cento' in p1 and 'acionistas imunes e ou isentos' in p1
            and 'comprovar esta condicao ate o dia 15 de julho de 2024 enviando documentacao comprobatoria' in p1,
            'explicit_JCP_identity_type_tax_and_document_deadline_roles_required')
    require('posicao final de acoes da companhia em 12 de agosto de 2024' in p2
            and 'o pagamento sera efetuado na data de 27 de agosto de 2024' in p2
            and 'todos os membros do conselho de administracao presentes aprovaram a proposta da diretoria tal como acima transcrita' in p2,
            'explicit_JCP_entitlement_payment_and_approval_required')
    require(form['record_date'] == '2024-08-12'
            and all(r['payment_date'] == '2024-08-27' for r in form['components']), 'native_and_latest_board_dates_conflict')
    compact = ' '.join(pages[1].split())
    amounts = re.findall(r'Valor bruto por a\S+o (ON|PN) \([^)]*\):\s*R\$\s*(\d+,\d{8})', compact)
    require(len(amounts) == 2 and {s for s, _ in amounts} == set(CLASSES), 'both_explicit_latest_board_class_amounts_required')
    require(Counter((s, decimal(a)) for s, a in amounts) == Counter(
        (r['share_class'], Decimal(r['gross_per_share_decimal'])) for r in form['components']),
        'native_and_latest_board_class_gross_amounts_conflict')
    return {'label': 'JCP', 'basis': 'explicit_latest_full_CVM_board_v3_and_complete_native_two_ISIN_form',
        'tax_document_deadline': '2024-07-15', 'record_date': form['record_date'], 'payment_date': '2024-08-27',
        'deadline_is_not_record_or_payment_date': True, 'class_gross_amounts': {s: str(decimal(a)) for s, a in amounts},
        'gross_reconstructed_from_net': False, 'settlement_verified': False,
        'earlier_board_versions_used': False, 'unusable_notice_body_used': False}


def review_ago2022_gap(pages, document):
    require(len(pages) == 8 and document['sequence'] == '506835' and document['version'] == 2,
            'whole_latest_AGO2022_v2_required')
    texts = [cvm.norm(p) for p in pages]
    require(all('cnpj no 33 228 024 0001 51' in p for p in texts)
            and 'ata da assembleia geral ordinaria de 29 de abril de 2022' in texts[0]
            and 'saldo obrigatorio de dividendos a pagar de r 9 709 820 02' in texts[2],
            'AGO2022_identity_and_explicit_aggregate_approval_required')
    whole = ' '.join(texts)
    require(all(value not in whole for value in ('0 25287', '0 27815', '29 de junho de 2022')),
            'new_AGO2022_individual_calendar_fields_require_review')
    return {'whole_body_reviewed': True, 'page_count': 8, 'approved_mandatory_total_BRL': '9709820.02',
        'individual_class_allocations_reported': False, 'cash_payment_calendar_reported': False,
        'vendor_PN_row_removal_authorized': False, 'class_amounts_inferred_from_1_1_ratio': False,
        'status': 'approval_only_class_and_calendar_not_reported_in_this_document'}


def verified_followon_calendar(folder, stock, cutoff=CUTOFF):
    share = 'PN' if stock.get('ticker') == 'WLMM4' and stock.get('share_class') == 'PNA' else stock.get('share_class')
    require(cutoff == CUTOFF and share in CLASSES and type(stock.get('cvm')) is int
            and stock['cvm'] == STOCK['cvm'] and (stock.get('ticker'), stock.get('isin')) == CLASSES[share]
            and (share != 'PN' or stock.get('share_class') == 'PNA'),
            'exact_WLMM3_or_WLMM4_identity_required')
    base = inspect_evidence(folder, STOCK, cutoff)
    reader = EvidenceReader(folder)
    documents, coverage = indexed_documents(reader, cutoff)
    selected = followon_documents(documents)
    originals = read_followon(reader, selected)
    _, tables = pdf_pages(originals['jcp_form']['body'], 1)
    form = parse_jcp_form(tables, selected['jcp_form'])
    board_pages, _ = pdf_pages(originals['jcp_board']['body'], 2)
    typed = review_jcp_board(board_pages, selected['jcp_board'], form)
    ago_pages, _ = pdf_pages(originals['ago2022']['body'], 8)
    gap = review_ago2022_gap(ago_pages, selected['ago2022'])
    payloads, _ = cached_sources(reader)
    detail, cash = payloads['expanded/b3-detail-11070.json'], payloads['expanded/WLMM4-b3-cash-1.json']
    require(detail['cnpj'] == CNPJ and detail['codeCVM'] == str(STOCK['cvm']), 'JCP_exact_cached_issuer_identity_required')
    located = [r for r in cash['results'] if cvm.day(r['dateApproval']) == form['approval_date']
               and cvm.day(r['lastDatePriorEx']) == form['record_date']]
    require(len(located) == 2 and all(r['corporateAction'] == 'JRS CAP PROPRIO'
            and r['quotedPerShares'] == r['ratio'] == '1' for r in located)
            and Counter((r['typeStock'], decimal(r['valueCash'])) for r in located) == Counter(
                (r['share_class'], Decimal(r['gross_per_share_decimal'])) for r in form['components']),
            'complete_independent_JCP_class_event_conflict')
    if share == 'PN':
        manifest, _ = reader.json(EXCHANGE_MANIFEST, 'vendor_PN_acquisition_manifest', EXCHANGE_MANIFEST_SHA)
        receipt, = [r for r in manifest['sources'] if r['file'] == 'expanded/WLMM4-payments.html']
        reader.read('raw/' + receipt['file'], 'original_PN_vendor_calendar', PN_PAYMENTS_SHA,
                    receipt['url'], receipt['retrieved_at'])
    crosswalk = read_wlmm4_class_crosswalk(reader)
    failed_fre = read_unavailable_fre(reader)
    native_fre = read_latest_native_fre2022(reader)
    mandatory_rows = [r for r in cash['results'] if cvm.day(r['dateApproval']) == '2022-04-29'
                      and cvm.day(r['lastDatePriorEx']) == '2022-04-29']
    require(len(mandatory_rows) == 2 and all(r['corporateAction'] == 'DIVIDENDO'
            and r['quotedPerShares'] == r['ratio'] == '1' for r in mandatory_rows)
            and Counter((r['typeStock'], decimal(r['valueCash'])) for r in mandatory_rows) == Counter(
                (share, Decimal(gross)) for share, gross in native_fre['schedule']['gross_per_share_by_source_class'].items())
            and native_fre['schedule']['remaining_mandatory_total_BRL'] == gap['approved_mandatory_total_BRL'],
            'whole_native_FRE_latest_AGO_and_independent_mandatory_entitlement_must_agree')
    sources = {}
    for s in [*base['evidence_sources'], *reader.sources.values()]:
        key = (s['file'], s['sha256'], s.get('url'))
        if key not in sources:
            sources[key] = deepcopy(s)
        else:
            sources[key]['roles'] = sorted(set(sources[key]['roles']) | set(s['roles']))
    return {**stock, 'as_of': cutoff.isoformat(), 'base_april_calendar': base, 'JCP_native_form': form,
        'JCP_type_and_role_proof': typed, 'selected_documents': selected, 'AGO2022_scope_review': gap,
        'native_share_class': share, 'legal_to_source_class_crosswalk': crosswalk,
        'additional_issuer_FRE_acquisition': failed_fre,
        'verified_native_FRE2022_schedule': native_fre,
        'mandatory2022_independent_group': deepcopy(mandatory_rows),
        'unusable_JCP_notice': {k: originals['jcp_notice'][k] for k in ('source', 'document', 'status')},
        'evidence_sources': list(sources.values()), 'IPE_coverage': coverage,
        'network_requests_in_verification': 0, 'owned_source_acquisition_total_requests': 8,
        'parent_admission_performed': False, 'normal_reconciliation_still_required': True}


def repair_full_inspected_calendar(folder, stock, cutoff, parsed):
    """Replace only the completely proved 2022/2024 class-specific groups."""
    proof = verified_followon_calendar(folder, stock, cutoff)
    name = f"raw/expanded/{stock['ticker']}-payments.html"
    native_share = proof['native_share_class']
    expected_sha = PAYMENTS_SHA if native_share == 'ON' else PN_PAYMENTS_SHA
    body = shared._safe(Path(folder).resolve(), name).read_bytes()
    require(hashlib.sha256(body).hexdigest() == expected_sha, 'full_vendor_original_hash_mismatch')
    observed = datetime.fromisoformat(parsed['report']['observed_at'])
    expected = inspector.inspect_payment_page(body, stock['ticker'], cutoff, observed)
    require(parsed == expected, 'full_repair_requires_exact_original_inspector_output')
    out = (_repair_parsed(parsed, proof['base_april_calendar'])
           if native_share == 'ON' else deepcopy(parsed))
    form, typed = proof['JCP_native_form'], proof['JCP_type_and_role_proof']
    old = [r for r in out['events'] if r['record_date'] == typed['tax_document_deadline']]
    require(len(old) == 1 and not any(r['record_date'] == form['record_date'] for r in out['events']),
            'one_whole_wrong_role_vendor_group_and_no_existing_native_group_required')
    original = old[0]
    expected_original = Decimal('.2' if native_share == 'ON' else '.22')
    require(original['ticker'] == stock['ticker'] and original['label'] == 'JCP'
            and original['payment_date'] == typed['payment_date'] and original['quoted_quantity'] == 1
            and original['reported_status'] is None and original['isin'] in (None, stock['isin'])
            and Decimal(str(original['gross_per_share'])) == Decimal(str(original['reported_amount'])) == expected_original,
            'reviewed_wrong_role_and_wrong_amount_vendor_group_changed')
    native, = [r for r in form['components'] if r['share_class'] == native_share]
    new = deepcopy(original)
    amount = float(Decimal(native['gross_per_share_decimal']))
    sources = [s for s in proof['evidence_sources'] if s['sha256'] in
               (FOLLOWON_PINS['779969'][1], FOLLOWON_PINS['781239'][1])]
    require(len(sources) == 2 and all(s['published_at'] <= cutoff.isoformat() for s in sources),
            'both_actual_primary_JCP_sources_required')
    new.update(record_date=native['record_date'], payment_date=native['payment_date'],
        approval_date=form['approval_date'], published_at=max(s['published_at'] for s in sources),
        publication_basis='latest_required_primary_source_delivery_not_PIT_archive', available_as_of=True,
        gross_per_share=amount, reported_amount=amount, primary_exact_gross_per_share_decimal=native['gross_per_share_decimal'],
        isin=stock['isin'], label=typed['label'], source_row='primary:cvm779969v1:' + stock['isin'],
        source_locator=deepcopy(native['source_locator']), primary_evidence=deepcopy(sources),
        primary_declaration_id=stock['ticker'] + ':' + form['record_date'] + ':cvm779969v1',
        status='past_scheduled', payment_date_basis='explicit_native_calendar_corrob_latest_board_v3',
        cash_model_eligible=False, financially_confirmed=False, full_settlement_on_date_verified=False)
    out['events'][out['events'].index(original)] = new
    inspector._finish(out)
    out['wlm_full_calendar_repair'] = {'stock': deepcopy(stock), 'original_vendor_JCP_group': deepcopy(old),
        'replacement_primary_JCP_group': [deepcopy(new)], 'entire_target_group_replaced': True,
        'original_issues': deepcopy(parsed['report']['issues']), 'source_format_error': 'tax_document_deadline_used_as_record_date',
        'vendor_amount_rescaled_or_net_reconstructed': False, 'amount_replaced_from_explicit_primary_gross': True,
        'JCP_source_proof': deepcopy(typed), 'AGO2022_scope_review': deepcopy(proof['AGO2022_scope_review']),
        'legal_to_source_class_crosswalk': deepcopy(proof['legal_to_source_class_crosswalk']),
        'additional_issuer_FRE_acquisition': deepcopy(proof['additional_issuer_FRE_acquisition']),
        '2022_vendor_rows_removed': False, 'evidence_sources': deepcopy(proof['evidence_sources']),
        'unusable_JCP_notice': deepcopy(proof['unusable_JCP_notice']),
        'normal_cash_price_share_and_annual_gates_unchanged': True, 'parent_admission_performed': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}
    out = replace_2022_primary_group(out, proof)
    return out


def build_full_report(folder):
    results = {}
    for share, (ticker, isin) in CLASSES.items():
        stock = {'ticker': ticker, 'cvm': STOCK['cvm'], 'share_class': 'PNA' if share == 'PN' else share, 'isin': isin}
        results[ticker] = verified_followon_calendar(folder, stock)
    report = {'schema_version': 3, 'status': 'verified_2022_2024_primary_groups_pending_all_normal_parent_gates',
        'calendars': results, 'integration_API': 'repair_full_inspected_calendar(folder,stock,cutoff,parsed)',
        'hook': 'directly_after_inspector_before_primary_enrichment_and_payment_issue_gates',
        'register_sources': 'all returned wlm_full_calendar_repair.evidence_sources actual bytes and receipts',
        'network_requests_in_verification': 0, 'owned_actual_acquisition_requests': 8,
        'original_reports_overwritten': False, 'parent_admission_performed': False, 'pointers_written': False}
    return report, save_report(folder, report)


def read_wlmm4_class_crosswalk(reader):
    from .security_registration import parse_fca_registry

    url = 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip'
    body, source = reader.read('raw/inventory/fca_cia_aberta_2026.zip', 'exact_primary_WLMM4_legal_class', FCA2026_SHA,
                               url, '2026-10-24T23:57:50.581746+00:00')
    masters, securities = parse_fca_registry(body, 2026)
    filings = [r for r in masters if int(r['CD_CVM']) == STOCK['cvm'] and r['DT_RECEB'] <= CUTOFF.isoformat()]
    require(filings, 'WLM_exact_primary_FCA_filing_required')
    latest = max(filings, key=lambda r: (r['DT_REFER'], int(r['VERSAO']), r['DT_RECEB']))
    require(latest['ID_DOC'] == '158140' and latest['DT_RECEB'] == '2026-05-28'
            and re.sub(r'\D', '', latest['CNPJ_CIA']) == CNPJ, 'newer_or_changed_WLM_FCA_requires_review')
    rows = [r for r in securities if r['ID_Documento'] == latest['ID_DOC']]
    preferred = [r for r in rows if cvm.norm(r['Valor_Mobiliario']) == 'acoes preferenciais']
    require(len(preferred) == 1 and preferred[0]['Codigo_Negociacao'] == 'WLMM4'
            and preferred[0]['Sigla_Classe_Acao_Preferencial'] == 'PNA'
            and preferred[0]['Classe_Acao_Preferencial'] == 'Preferencial Classe A',
            'unique_exact_preferred_FCA_class_required_no_cross_class_guess')
    return {'ticker': 'WLMM4', 'cvm': STOCK['cvm'], 'isin': CLASSES['PN'][1],
        'legal_class_kept': 'PNA', 'source_generic_preferred_label': 'PN',
        'basis': 'exact_FCA_security_code_plus_cached_ticker_ISIN_plus_two_explicit_native_ISIN_forms',
        'sole_preferred_security_in_latest_FCA': True, 'source_FCA_row': deepcopy(preferred[0]),
        'FCA_filing': deepcopy(latest), 'source_receipt': deepcopy(source),
        'ISIN_is_not_reported_in_FCA': True, 'raw_cash_class_labels_kept_in_audit': True,
        'share_or_cash_scale_conversion': False, 'catalogue_legal_class_changed': False}


def read_unavailable_fre(reader):
    request, _ = reader.json(CACHE + '/request-issuer-FRE2022-v2.json', 'single_requested_issuer_FRE_attempt',
        '29fa4b876caf31371b5606adc7e44cfc09e37a2fb4257686de5db05d1ff46eaf')
    receipt, _ = reader.json(CACHE + '/response-issuer-FRE2022-v2-286b1bec29dc834a381385d8a9edc60f4c3ba6230ef5d9bb9a1493ba550a0f12.json',
        'actual_issuer_FRE_404_receipt', '286b1bec29dc834a381385d8a9edc60f4c3ba6230ef5d9bb9a1493ba550a0f12')
    require(request['url'] == receipt['url'] == 'https://www.wlm.com.br/wp-content/uploads/2022/08/WLM-FR-2022-V2.pdf'
            and receipt['http_status'] == 404 and receipt['status'] == 'failed'
            and receipt['network_requests'] == 1 and receipt['retries_performed'] == 0,
            'actual_failed_FRE_acquisition_must_be_preserved')
    body, source = reader.read(receipt['file'], 'failed_issuer_FRE_download_not_primary_calendar_evidence',
        'bda6b23b93e0baa35f270daa0ce663f3a0153311862a5af817c38629fab66952', receipt['url'], receipt['retrieved_at'])
    require(len(body) == receipt['bytes'] and not body.lstrip().startswith(b'%PDF-'), 'actual_FRE_404_body_required')
    return {'http_status': 404, 'status': 'not_a_PDF_no_calendar_extracted_no_retry',
        'source_receipt': deepcopy(source), 'calendar_proof': False,
        'published_at': None, 'publication_inferred_from_URL': False, 'exact_CVM_blob_identity_verified': False}


def canonical_independent_cash_rows(folder, stock, cutoff, rows):
    """Typed source-label view only; retain FCA PNA and every raw amount/date."""
    require(stock.get('ticker') == 'WLMM4' and stock.get('share_class') == 'PNA',
            'exact_WLMM4_legal_PNA_target_required')
    proof = verified_followon_calendar(folder, stock, cutoff)
    reader = EvidenceReader(folder)
    payloads, _ = cached_sources(reader)
    original = payloads['expanded/WLMM4-b3-cash-1.json']['results']
    require(rows == original, 'class_crosswalk_requires_entire_exact_original_cash_page')
    out = deepcopy(rows)
    for row in out:
        if row['typeStock'] == 'PN':
            row['reported_typeStock'] = 'PN'
            row['typeStock'] = 'PNA'
            row['class_normalization'] = 'WLMM4_exact_FCA_PNA_source_generic_PN_no_scale_conversion'
    return {'cash_rows': out, 'original_cash_rows': deepcopy(original),
        'class_crosswalk': deepcopy(proof['legal_to_source_class_crosswalk']),
        'evidence_sources': deepcopy(proof['evidence_sources']), 'raw_files_changed': False,
        'normal_independent_cash_price_and_annual_checks_required': True}


def parse_fre2022_schedule(text):
    """Read the complete native attachment, never derive per-share from totals."""
    import unicodedata

    value = ' '.join(unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().split())
    compact = re.sub(r'\s+', '', value).lower()
    require(compact.count('valorbruto') == 2 and 'acaoon' in compact and 'acaopn' in compact
            and 'pagamento' in compact and 'valorliquido' not in compact,
            'FRE_explicit_gross_ON_PN_payment_headers_required')
    require('DIVIDENDOS PROPOSTOS COM BASE NO LUCRO DO EXERCICIO FINDO EM' in value
            and '31 DE DEZEMBRO DE 2021' in value
            and '22.909.820,03' in value and '13.200.000,01' in value and '9.709.820,02' in value,
            'FRE_full_2021_mandatory_balance_and_issuer_scope_required')
    jcp = re.findall(r'Juros sobre Capital Proprio - (\d{2}/\d{2}/\d{2}) (\d[\d.]*) (0,\d{5}) (0,\d{5}) (\d{2}/\d{2}/\d{4})', value)
    mandatory = re.findall(r'Saldo de dividendo obrigatorio sobre o lucro liquido do exercicio ajustado (\d[\d.]*) (0,\d{5}) (0,\d{5}) (\d{2}/\d{2}/\d{4})', value)
    require(len(jcp) == 4 and len(mandatory) == 1 and value.endswith('Total 22.910'),
            'whole_FRE_schedule_table_required_no_partial_or_duplicate_selection')
    total, on, pn, payment = mandatory[0]
    require(total == '9.710' and Decimal(total.replace('.', ''))
            + sum((Decimal(r[1].replace('.', '')) for r in jcp), Decimal(0)) == Decimal('22910'),
            'whole_FRE_displayed_monetary_table_sum_required')
    return {'fiscal_year': 2021, 'cash_kind': 'DIVIDENDO', 'label': 'saldo_dividendo_obrigatorio',
        'gross_per_share_by_source_class': {'ON': str(decimal(on)), 'PN': str(decimal(pn))},
        'payment_date': day(payment), 'explicit_header': 'VALOR BRUTO POR ACAO ON / PN; DATA DE PAGAMENTO',
        'all_four_other_JCP_rows_retained_for_table_audit_not_used_as_record_dates': jcp,
        'remaining_mandatory_total_BRL': '9709820.02',
        'per_share_amounts_derived_from_aggregate_or_ratio': False,
        'is_payment_deadline_only': False, 'reported_schedule_not_settlement': True}


def bounded_zip(body, *, max_total=150_000_000):
    archive = zipfile.ZipFile(io.BytesIO(body))
    entries = archive.infolist()
    require(len(entries) <= 1000 and len({r.filename for r in entries}) == len(entries)
            and sum(r.file_size for r in entries) <= max_total
            and all(not r.flag_bits & 1 and r.file_size <= 100_000_000
                    and '/' not in r.filename and '\\' not in r.filename and '..' not in r.filename for r in entries),
            'bounded_unique_unencrypted_flat_archive_required')
    return archive


def read_latest_native_fre2022(reader):
    payloads = {}
    for label, request_sha, response_sha, body_sha in (
        ('index', 'ab512d987fcc7fb9688058e81efd94a41b1bc46a5c5100b7f4dc119231fe4bc2',
         '77c0bcd47ad1e73ff80d95bc76b83053694f425064c810449b42221256336aa6', NATIVE_FRE_INDEX_SHA),
        ('latest-125831', 'ad5b4f1d88610e47f923c3566d821c170c6f02467768a0c2cb7d5972438da421',
         '9f7cf4f2e80285ed422f404ddaaba9a6a65b9a05bc93036c37fb0fa42c724c1f', NATIVE_FRE_SHA)):
        request, _ = reader.json(CACHE + '/request-native-FRE2022-' + label + '.json', 'native_FRE_actual_request', request_sha)
        receipt, _ = reader.json(CACHE + '/response-native-FRE2022-' + label + '-' + response_sha + '.json',
                                  'native_FRE_actual_transport_receipt', response_sha)
        require(receipt['http_status'] == 200 and receipt['url'] == receipt['response_url'] == request['url']
                and receipt['sha256'] == body_sha and receipt['access_stop'] is False
                and receipt['redirects'] is False and receipt['retries'] == 0
                and receipt['network_requests'] == 1, 'native_FRE_exact_transport_receipts_required')
        body, source = reader.read(receipt['file'], 'original_native_FRE_' + label, body_sha,
                                    receipt['url'], receipt['retrieved_at'])
        require(source['bytes'] == receipt['bytes'], 'native_FRE_receipt_size_mismatch')
        payloads[label] = (body, source)
    index_body, index_source = payloads['index']
    require(index_source['url'] == 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS/fre_cia_aberta_2022.zip',
            'native_FRE_official_index_origin_required')
    with bounded_zip(index_body, max_total=500_000_000) as archive:
        member = 'fre_cia_aberta_2022.csv'
        rows = list(csv.DictReader(io.TextIOWrapper(archive.open(member), encoding='latin1'), delimiter=';'))
        issuer = [{'csv_line': i, 'raw': r} for i, r in enumerate(rows, 2)
                  if r['CD_CVM'].isdigit() and int(r['CD_CVM']) == STOCK['cvm'] and r['DT_RECEB'] <= CUTOFF.isoformat()]
        require(issuer and all(re.sub(r'\D', '', r['raw']['CNPJ_CIA']) == CNPJ for r in issuer), 'native_FRE_exact_issuer_master_required')
        latest = max(issuer, key=lambda r: (r['raw']['DT_REFER'], int(r['raw']['VERSAO']), r['raw']['DT_RECEB']))
        require(latest['raw']['ID_DOC'] == '125831' and latest['raw']['VERSAO'] == '3'
                and latest['raw']['DT_RECEB'] == '2023-05-05' and latest['raw']['DT_REFER'] == '2022-01-01',
                'newer_native_FRE_revision_no_fallback')
        class_member = 'fre_cia_aberta_distribuicao_dividendos_classe_acao_2022.csv'
        class_rows = list(csv.DictReader(io.TextIOWrapper(archive.open(class_member), encoding='latin1'), delimiter=';'))
        class_rows = [r for r in class_rows if r['ID_Documento'] == '125831' and r['Data_Fim_Exercicio_Social'] == '2021-12-31']
        require(len(class_rows) == 2 and {r['Especie_Acao'] for r in class_rows} == {'Ordin\u00e1ria', 'Preferencial'}
                and all(r['CNPJ_Companhia'] == '33.228.024/0001-51' and r['Versao'] == '3'
                        and r['Data_Pagamento_Dividendo'] == '2022-06-29' for r in class_rows)
                and next(r for r in class_rows if r['Especie_Acao'] == 'Preferencial')['Classe_Acao'] == 'Preferencial Classe A',
                'native_FRE_whole_ON_preferred_A_annual_date_proof_required')
    native_body, native_source = payloads['latest-125831']
    require(native_source['url'] == latest['raw']['LINK_DOC'].replace('http://', 'https://', 1),
            'native_FRE_download_not_exact_master_LINK_DOC')
    with bounded_zip(native_body) as outer:
        require(set(outer.namelist()) == {'011070202201010803.fre', 'FormularioCadastral.xml', 'FormularioReferencia.xml'},
                'whole_exact_native_FRE_outer_package_required')
        header = ET.fromstring(outer.read('FormularioReferencia.xml'))
        require(header.tag == 'Documento', 'native_FRE_document_root_required')
        def one(tag):
            path = 'CompanhiaAberta/' + tag if tag in ('CodigoCvm', 'NumeroCnpjCompanhiaAberta') else tag
            nodes = header.findall(path)
            require(len(nodes) == 1 and nodes[0].text, 'unique_native_FRE_header_required:' + path)
            return nodes[0].text
        require(one('NumeroSequencialDocumento') == '125831' and one('NumeroVersaoDocumento') == '3'
                and re.sub(r'\D', '', one('NumeroCnpjCompanhiaAberta')) == CNPJ
                and int(re.sub(r'\D', '', one('CodigoCvm'))) == STOCK['cvm']
                and one('DataEntrega')[:10] == latest['raw']['DT_RECEB']
                and one('ProtocoloEntrega') == '011070FRE202220220300125831-61', 'native_FRE_master_XML_identity_binding_failed')
        inner_body = outer.read('011070202201010803.fre')
    require(hashlib.sha256(inner_body).hexdigest() == '164f302f9f7c8b54754de52521c19aaf54f63f461683f0030da6b7ada883ba14',
            'native_inner_FRE_package_changed')
    with bounded_zip(inner_body) as inner:
        annex_body = inner.read('AnexoDocumento.xml')
    require(hashlib.sha256(annex_body).hexdigest() == 'a587e6ee5f6a400a4c31f96978207ff20fa3874131b2771a8877548054c65b28',
            'native_FRE_attachment_XML_changed')
    annex = ET.fromstring(annex_body)
    matches = [(i, r) for i, r in enumerate(annex) if r.findtext('NumeroQuadroRelacionado') == '1488'
               and r.findtext('NumeroGrupoRelacionado') == '793' and r.findtext('Idioma') == '1']
    require(len(annex) == 110 and len(matches) == 1 and matches[0][0] == 103, 'unique_full_native_current_attachment_required')
    entry = matches[0][1]
    require(all(entry.findtext(k) == '0' for k in ('NumeroCampoRelacionado', 'NumeroSubCampo', 'NumeroItemTextoLivre')),
            'ambiguous_native_FRE_attachment_scope')
    pdf_body = base64.b64decode(entry.findtext('ImagemObjetoArquivoPdf'), validate=True)
    require(hashlib.sha256(pdf_body).hexdigest() == NATIVE_FRE_PDF_SHA, 'exact_native_FRE_attachment_hash_required')
    derived, derived_source = reader.read(NATIVE_FRE_PDF, 'exact_decoded_native_FRE_PDF_attachment', NATIVE_FRE_PDF_SHA,
                                          native_source['url'], published_at=latest['raw']['DT_RECEB'])
    require(derived == pdf_body, 'decoded_native_FRE_PDF_and_read_artifact_disagree')
    derived_source.update(authority='exact_native_primary_attachment_not_independent_download',
        derived_from={'file': native_source['file'], 'sha256': NATIVE_FRE_SHA,
            'outer_member': '011070202201010803.fre', 'inner_member': 'AnexoDocumento.xml',
            'entry_index': 103, 'frame': '1488', 'group': '793', 'encoding': 'base64_exact_bytes'},
        retrieved_at=None, observed_at_basis='extracted_exact_member_not_network_retrieval')
    pages, _ = pdf_pages(pdf_body, 1)
    schedule = parse_fre2022_schedule(pages[0])
    require(all(r['Data_Pagamento_Dividendo'] == schedule['payment_date'] for r in class_rows),
            'native_FRE_master_class_schedule_payment_conflict')
    return {'schedule': schedule, 'master_member': member, 'all_issuer_master_rows': issuer, 'latest': latest,
        'published_at': latest['raw']['DT_RECEB'], 'protocol': '011070FRE202220220300125831-61',
        'native_original_source': deepcopy(native_source), 'index_source': deepcopy(index_source),
        'schedule_source': deepcopy(derived_source), 'class_A_annual_records': class_rows,
        'annual_class_aggregate_amounts_not_used_to_derive_per_share_cash': True,
        'approval_bound_by_separate_latest_AGO2022': '506835v2', 'settlement_verified': False,
        'point_in_time_archive_verified': False, 'older_FRE_v2_fallback_used': False,
        'later_revision_scope': 'all available exact issuer rows in full frozen official FRE2022 master before cutoff'}


def replace_2022_primary_group(parsed, proof):
    form = proof['verified_native_FRE2022_schedule']; schedule = form['schedule']
    stock = {k: proof[k] for k in STOCK}; native_class = proof['native_share_class']
    group = [r for r in parsed['events'] if r['record_date'] == '2022-04-29']
    primary_amount = Decimal(schedule['gross_per_share_by_source_class'][native_class])
    if native_class == 'ON':
        require(len(group) == 2 and Counter(Decimal(str(r['gross_per_share'])) for r in group) == Counter(
            Decimal(v) for v in schedule['gross_per_share_by_source_class'].values())
            and all(r['ticker'] == stock['ticker'] and r['label'] == 'DIVIDENDO'
                    and r['payment_date'] == schedule['payment_date'] and r['quoted_quantity'] == 1 for r in group),
            'whole_exact_ON_vendor_group_with_proven_errant_PN_amount_required')
    else:
        require(group == [], 'missing_PN_primary_group_must_not_replace_existing_unreviewed_rows')
    # Rights/approval identity is explicit in the already frozen full exchange
    # event; payment and separate class amounts come from the native FRE table.
    native = deepcopy(parsed['events'][0])
    source = form['schedule_source']
    native.update(ticker=stock['ticker'], label='DIVIDENDO', isin=stock['isin'],
        record_date='2022-04-29', approval_date='2022-04-29', payment_date=schedule['payment_date'],
        published_at=form['published_at'], publication_basis='exact_latest_native_FRE_delivery_not_PIT_archive',
        available_as_of=True, gross_per_share=float(primary_amount), reported_amount=float(primary_amount),
        quoted_quantity=1, date_order_valid=True, status='past_scheduled', reported_status=None,
        source_row='primary:native-FRE125831:frame1488:' + stock['isin'],
        source_locator=deepcopy(source['derived_from']), primary_evidence=[deepcopy(source)],
        primary_declaration_id=stock['ticker'] + ':2022-04-29:mandatory2021',
        primary_exact_gross_per_share_decimal=str(primary_amount),
        payment_date_basis='explicit_native_FRE2022v3_full_gross_class_table_not_deadline',
        cash_model_eligible=False, financially_confirmed=False, full_settlement_on_date_verified=False,
        paid_status_as_of_verified=False, reported_expired=False)
    out = deepcopy(parsed)
    retired_issues = [r for r in out['report']['issues'] if r.get('row') in {e['source_row'] for e in group}]
    require(not any(r['blocking'] and r['code'] != 'conflicting_event_amount' for r in retired_issues),
            'unreviewed_mandatory2022_source_issue_must_not_be_hidden')
    out['report']['issues'] = [r for r in out['report']['issues'] if r not in retired_issues]
    out['events'] = [r for r in out['events'] if r['record_date'] != '2022-04-29'] + [native]
    inspector._finish(out)
    out['wlm_full_calendar_repair'].update(
        original_vendor_mandatory2022_group=deepcopy(group), replacement_primary_mandatory2022_group=[deepcopy(native)],
        retired_vendor_mandatory2022_issues=deepcopy(retired_issues),
        verified_native_FRE2022_schedule=deepcopy(form),
        source_2022_group_corrected_from_primary=True, **{'2022_vendor_rows_removed': native_class == 'ON'},
        source_2022_group_added_from_primary_not_zero_imputation=native_class == 'PN')
    return out


def build_report(folder):
    calendar = inspect_evidence(folder, STOCK)
    report = {'schema_version': 1, 'status': 'verified_pending_normal_parent_gates' if calendar['repair_allowed']
              else calendar['approval_revision_review']['status'], 'calendar': calendar,
        'network_requests': 0, 'offline_verification_network_requests': 0,
        'owned_existing_approval_acquisition_requests': 1, 'parent_files_changed': False, 'pointers_written': False,
        'thresholds_changed': False, 'parent_admission_performed': False,
        'integration_contract': {'entrypoint': 'repair_inspected_calendar(folder, stock, cutoff, parsed)',
            'placement': 'immediately_after_inspector_before_enrich_calendar_and_any_payment_issue_gate',
            'stock_identity': STOCK, 'cutoff': CUTOFF.isoformat(),
            'required_source_registration': 'all calendar.evidence_sources; hash original bytes and preserve actual URL/retrieved_at',
            'required_audit_propagation': 'parsed.native_calendar_repair including original_group/original_issues',
            'normal_gates_unchanged': True, 'caller_must_not_catch_blocker_and_use_repaired_rows': True}}
    return report, save_report(folder, report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire-approval-v2', action='store_true')
    parser.add_argument('--acquire-followon', action='store_true')
    parser.add_argument('--full-report', action='store_true')
    parser.add_argument('--acquire-fre2022-v2', action='store_true')
    args = parser.parse_args()
    if args.acquire_fre2022_v2:
        result, name = acquire_fre2022_v2(args.folder)
        print(json.dumps({k: result[k] for k in ('http_status', 'status', 'file', 'sha256', 'bytes')}
                         | {'receipt': name}, indent=2))
    elif args.full_report:
        report, name = build_full_report(args.folder)
        print(json.dumps({'report': name, 'status': report['status'], 'parent_admission_performed': False}, indent=2))
    elif args.acquire_followon:
        print(json.dumps(acquire_followon(args.folder), indent=2))
    elif args.acquire_approval_v2:
        result, receipt = acquire_approval_v2(args.folder)
        print(json.dumps({k: result[k] for k in ('status', 'http_status', 'file', 'sha256', 'bytes', 'network_requests')}
                         | {'receipt': receipt}, indent=2))
    else:
        report, name = build_report(args.folder)
        print(json.dumps({'report': name, 'status': report['status'], 'network_requests': 0,
                          'parent_admission_performed': False}, indent=2))
