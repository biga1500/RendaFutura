"""Offline BPAC5 date-only repair from an exact indexed, explicitly dated notice.

The unusable latest native form remains in the evidence set; no v1 fallback,
cash-kind inference, gross reconstruction, UNIT multiplication or admission.
"""

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
import zipfile

from lxml import html
import pdfplumber
from pypdf import PdfReader

from . import wlm_native_calendar as evidence
from . import cvm_proventos_documents as cvm
from . import payment_sources as inspector


CACHE = 'raw/primary-research/btg-notice-calendar'
CUTOFF = evidence.CUTOFF
STOCK = {'ticker': 'BPAC5', 'cvm': 22616, 'share_class': 'PNA', 'isin': 'BRBPACACNPA0'}
CNPJ = '30306294000145'
REFERENCE = '2022-12-13'
NOTICE_SHA = '4ee26be00333f3ffef1b673ab3fb77b0daf1849c4967d5c76c148ddd1ea75ac5'
NOTICE = 'raw/primary-research/btg-cash-followon/565973-v1-' + NOTICE_SHA + '.pdf'
NOTICE_RECEIPT_SHA = 'aedd82715a37c3b0c824f633502d58165334ce3298ce41759a31b0f6717e83a5'
EMPTY_SHA = '0bc42f3dce90e0f8bf7c17e3836b7e54bf388edad6a1a422998ab58a98cc6e0e'
EMPTY = 'raw/primary-research/cash-calendars/cvm566342v2-' + EMPTY_SHA + '.pdf'
EMPTY_RECEIPT_SHA = 'dab77eb3eaed5a93089a008023d50c86ad000d8b1abe95ecb31216377499a55a'
PAYMENTS = 'raw/expanded/BPAC5-payments.html'
PAYMENTS_SHA = 'd8ba67d737d2358464c8c9cc1095ae6479c55e4fb6384506e0a749823044bc8e'
IDENTITIES = {'ON': ('BPAC3', 'BRBPACACNOR7'), 'PNA': ('BPAC5', 'BRBPACACNPA0'),
              'UNT': ('BPAC11', 'BRBPACUNT006')}
RULE = 'exact_issuer_notice_explicit_schedule_with_unusable_latest_form_retained_v1'
require = evidence.require


def indexed_documents(reader, cutoff):
    manifest, _ = reader.json(evidence.IPE_MANIFEST, 'pinned_six_year_IPE_manifest', evidence.IPE_MANIFEST_SHA)
    require(type(manifest) is list and len(manifest) == 6, 'six_full_pinned_IPE_indices_required')
    documents, coverage = [], []
    for year, digest in evidence.IPE_HASHES.items():
        sources = [s for s in manifest if type(s.get('year')) is int and s['year'] == year]
        require(len(sources) == 1, 'one_pinned_IPE_source_per_year_required')
        source = sources[0]
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        require(source['sha256'] == digest and source['url'] == url, 'pinned_IPE_origin_or_hash_mismatch')
        body, receipt = reader.read(source['file'], 'whole_native_IPE_all_categories', digest,
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
                    published = evidence.day(row['Data_Entrega'])
                    require(published.startswith(str(year)), 'IPE_delivery_year_mismatch')
                    if published > cutoff.isoformat():
                        continue
                    require(re.sub(r'\D', '', row['CNPJ_Companhia']) == CNPJ, 'IPE_CNPJ_mismatch')
                    seq, version = cvm.document_id(row['Link_Download'])
                    require(row['Versao'] == str(version), 'IPE_version_URL_mismatch')
                    count += 1
                    documents.append({'cvm': STOCK['cvm'], 'sequence': seq, 'version': version,
                        'reference_date': evidence.day(row['Data_Referencia']), 'published_at': published,
                        'url': row['Link_Download'], 'category': cvm.norm(row['Categoria']),
                        'type': cvm.norm(row['Tipo']), 'species': cvm.norm(row['Especie']),
                        'protocol': row['Protocolo_Entrega'],
                        'index_evidence': {'file': receipt['file'], 'sha256': digest, 'url': url,
                            'member': member, 'csv_line': line, 'raw_row': row}})
        coverage.append({'year': year, 'file': receipt['file'], 'sha256': digest,
                         'issuer_rows_before_cutoff': count, 'all_categories': True})
    return documents, coverage


def select_documents(documents):
    def family(protocol):
        match = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', protocol)
        return (match[1], match[2]) if match else None
    result = {}
    for kind, category, suffix, seq, version in (
        ('notice', 'aviso aos acionistas', '04490814-22', '565973', 1),
        ('board', 'reuniao da administracao', '04490814-43', '565974', 1),
        ('native', 'relatorio proventos', None, '566342', 2)):
        expected_family = ('022616IPE13122022', suffix) if suffix else None
        rows = [d for d in documents if d['category'] == category
                and (d['reference_date'] == REFERENCE or (suffix and family(d['protocol']) == expected_family))]
        require(rows and all(type(d['version']) is int and d['version'] > 0 for d in rows),
                'whole_same_event_document_family_missing:' + kind)
        latest = [d for d in rows if d['version'] == max(r['version'] for r in rows)
                  and d['published_at'] == max(r['published_at'] for r in rows)]
        require(len(latest) == 1, 'ambiguous_latest_same_event_document:' + kind)
        doc = latest[0]
        require(doc['sequence'] == seq and doc['version'] == version
                and doc['reference_date'] == REFERENCE, 'unreviewed_latest_document_no_older_fallback:' + kind)
        raw = doc['index_evidence']['raw_row']
        require(cvm.document_id(doc['url']) == (seq, version)
                and raw['Link_Download'] == doc['url'] and raw['Codigo_CVM'] == str(STOCK['cvm'])
                and raw['Versao'] == str(version) and evidence.day(raw['Data_Entrega']) == doc['published_at']
                and evidence.day(raw['Data_Referencia']) == doc['reference_date']
                and re.sub(r'\D', '', raw['CNPJ_Companhia']) == CNPJ
                and raw['Protocolo_Entrega'] == doc['protocol']
                and doc['cvm'] == STOCK['cvm'], 'exact_document_index_metadata_mismatch')
        if kind == 'board':
            require(doc['type'] == 'conselho de administracao' and doc['species'] == 'ata',
                    'exact_approval_board_document_required')
        if suffix:
            require(doc['protocol'] == '022616IPE1312202201' + suffix
                    and doc['published_at'] == REFERENCE, 'exact_full_IPE_protocol_required')
        else:
            require(doc['published_at'] == '2022-12-15', 'latest_native_publication_changed')
        result[kind] = doc
    return result


def one_page_text(body):
    require(body.lstrip().startswith(b'%PDF-') and len(body) <= 8_000_000, 'bounded_original_PDF_required')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    require(not pdf.is_encrypted and len(pdf.pages) == 1, 'whole_one_page_original_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        return pdf.pages[0].extract_text() or ''


def parse_notice_text(raw, document):
    require(document['sequence'] == '565973' and document['version'] == 1
            and document['published_at'] == document['reference_date'] == REFERENCE,
            'exact_notice_version_and_publication_required')
    text = ' '.join(unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode().lower().split())
    require('cnpj/mf 30.306.294/0001-45' in text and 'banco btg pactual s.a.' in text
            and 'aviso aos acionistas' in text and 'deliberacao de juros sobre capital proprio' in text
            and 'seu conselho de administracao aprovou, nesta data' in text
            and 'juros sobre capital proprio complementar, em 2022' in text
            and 'retencao de 15% referente ao imposto de renda na fonte' in text,
            'whole_explicit_issuer_identity_kind_approval_or_tax_spans_missing')
    amounts = {}
    for key, pattern in {
        'share_gross': r'a\) valor bruto por acao ordinaria ou acao preferencial: r\$ (\d+,\d{9});',
        'share_net': r'b\) valor liquido por acao ordinaria ou acao preferencial: r\$ (\d+,\d{9});',
        'unit_gross': r'c\) valor bruto por unit bpac11: r\$ (\d+,\d{9});',
        'unit_net': r'd\) valor liquido por unit bpac11: r\$ (\d+,\d{9})\.',
    }.items():
        values = re.findall(pattern, text)
        require(len(values) == 1, 'entire_unique_explicit_gross_net_table_required:' + key)
        amounts[key] = evidence.decimal(values[0])
    # Both net figures are explicitly reported, never used to reconstruct gross.
    for prefix in ('share', 'unit'):
        require((amounts[prefix + '_gross'] * Decimal('.85')).quantize(Decimal('.000000001'), rounding=ROUND_HALF_UP)
                == amounts[prefix + '_net'], 'reported_gross_net_15_percent_relation_conflict')
    months = {'janeiro': 1, 'fevereiro': 2, 'marco': 3, 'abril': 4, 'maio': 5, 'junho': 6,
              'julho': 7, 'agosto': 8, 'setembro': 9, 'outubro': 10, 'novembro': 11, 'dezembro': 12}
    def explicit_date(prefix):
        matches = re.findall(prefix + r'(\d{1,2}) de ([a-z]+) de (\d{4})', text)
        require(len(matches) == 1 and matches[0][1] in months, 'one_explicit_notice_date_required')
        d, m, y = matches[0]
        return evidence.day(f'{int(y):04d}-{months[m]:02d}-{int(d):02d}')
    record = explicit_date(r'no final do dia ')
    ex = explicit_date(r'a partir de ')
    payment = explicit_date(r'ocorrera no dia ')
    signed = explicit_date(r'rio de janeiro, ')
    require(signed == REFERENCE and signed <= record < ex < payment, 'explicit_notice_event_date_order_invalid')
    return {'issuer_cnpj': CNPJ, 'approval_date': signed, 'record_date': record, 'ex_date': ex,
        'payment_date': payment, 'published_at': document['published_at'], 'cash_kind': 'JCP',
        'kind_basis': 'explicit_issuer_notice_JCP_complementar_not_inferred',
        'amounts': {k: str(v) for k, v in amounts.items()},
        'gross_basis': 'explicit_gross_BRL_per_reported_share_or_UNIT_not_net_reconstruction',
        'UNIT_amount_basis': 'explicitly_reported_notice_UNIT_amount_not_multiplied_from_share',
        'tax_withholding_reported_percent': 15, 'gross_reconstructed_from_net': False,
        'is_settlement_confirmation': False, 'point_in_time_archive_verified': False}


def read_originals(reader, documents):
    notice_receipt, _ = reader.json(NOTICE + '-source.json', 'actual_notice_transport_receipt', NOTICE_RECEIPT_SHA)
    notice = documents['notice']
    body, source = reader.read(NOTICE, 'exact_CVM_issuer_notice_explicit_dates_gross_and_type', NOTICE_SHA,
                               notice['url'], notice_receipt['retrieved_at'], notice['published_at'])
    require(notice_receipt['http_status'] == 200
            and all(notice_receipt[k] == source[k] for k in ('file', 'sha256', 'bytes', 'url'))
            and all(notice_receipt['document'][k] == notice[k] for k in
                    ('cvm', 'sequence', 'version', 'published_at', 'reference_date', 'protocol'))
            and notice_receipt['document']['index_sha256'] == evidence.IPE_HASHES[2022]
            and re.sub(r'\D', '', notice_receipt['document']['cnpj']) == CNPJ
            and parse_qs(urlsplit(notice['url']).query)['numProtocolo'] == ['1041243'],
            'notice_receipt_or_exact_index_identity_mismatch')
    source['authority'] = 'primary_CVM_issuer_reported_schedule_not_settlement'
    form = parse_notice_text(one_page_text(body), notice)
    receipt, _ = reader.json(EMPTY.removesuffix('.pdf') + '-source.json', 'unusable_latest_form_acquisition_receipt', EMPTY_RECEIPT_SHA)
    native = documents['native']
    body, empty_source = reader.read(EMPTY, 'unusable_latest_native_form_preserved_no_v1_fallback', EMPTY_SHA,
                                     native['url'], receipt['retrieved_at'], native['published_at'])
    require(all(receipt[k] == empty_source[k] for k in ('file', 'sha256', 'bytes', 'url'))
            and all(receipt['document'][k] == native[k] for k in
                    ('cvm', 'sequence', 'version', 'published_at', 'reference_date'))
            and receipt['document']['index_sha256'] == evidence.IPE_HASHES[2022]
            and cvm.norm(one_page_text(body)) == 'indice provento dividendo',
            'reviewed_unusable_latest_form_changed_requires_new_review')
    unusable = {'document': deepcopy(native), 'source_receipt': deepcopy(empty_source),
        'body_is_valid_PDF_but_has_no_issuer_or_event_rows': True, 'model_eligible': False,
        'older_native_version_used': False, 'claim_v2_amended_payment_dates': False,
        'alternative_source_is_separate_exact_issuer_notice': True}
    return form, unusable


def cached_sources(reader):
    manifest, _ = reader.json(evidence.EXCHANGE_MANIFEST, 'pinned_cached_acquisition_manifest', evidence.EXCHANGE_MANIFEST_SHA)
    payloads = {}
    for name in ('expanded/b3-detail-22616.json', 'expanded/BPAC3-b3-cash-1.json', 'expanded/BPAC5-payments.html'):
        choices = {json.dumps(s, sort_keys=True): s for s in manifest['sources'] if s.get('file') == name}
        require(len(choices) == 1, 'unique_existing_source_receipt_required')
        receipt = next(iter(choices.values()))
        body, source = reader.read('raw/' + name, 'cached_whole_explicit_type_identity_or_vendor_original',
                                    receipt['sha256'], receipt['url'], receipt.get('retrieved_at'))
        require(source['bytes'] == receipt['bytes'], 'cached_source_receipt_size_mismatch')
        if name.endswith('.html'):
            require(source['sha256'] == PAYMENTS_SHA, 'unreviewed_vendor_original')
        payloads[name] = body if name.endswith('.html') else json.loads(body)
    return payloads


def prove_whole_cash_group(form, detail, payload):
    require(detail.get('cnpj') == CNPJ and str(detail.get('codeCVM')) == str(STOCK['cvm']), 'exact_CVM_CNPJ_identity_required')
    for ticker, isin in IDENTITIES.values():
        require([r for r in detail.get('otherCodes', []) if r.get('code') == ticker] == [{'code': ticker, 'isin': isin}],
                'exact_cached_class_ticker_ISIN_required')
    require(type(payload.get('results')) is list and payload.get('page') == {
        'pageNumber': 1, 'pageSize': 120, 'totalRecords': len(payload['results']), 'totalPages': 1},
        'whole_cached_cash_page_required')
    rows = [{'row': i, 'raw': r} for i, r in enumerate(payload['results'])
            if cvm.day(r.get('dateApproval')) == form['approval_date']
            and cvm.day(r.get('lastDatePriorEx')) == form['record_date']]
    require(len(rows) == 3 and Counter(r['raw'].get('typeStock') for r in rows) == Counter(IDENTITIES.keys()),
            'complete_ON_PNA_UNT_event_required')
    for located in rows:
        r = located['raw']
        expected = form['amounts']['unit_gross' if r['typeStock'] == 'UNT' else 'share_gross']
        require(r.get('corporateAction') == 'JRS CAP PROPRIO'
                and r.get('quotedPerShares') == '1' and r.get('ratio') == '1'
                and evidence.decimal(r['valueCash']) == Decimal(expected), 'exact_explicit_cash_type_gross_and_unit_required')
    return {'label': 'JCP', 'components': rows, 'whole_event_complete': True,
        'cash_type_inferred_from_amount_or_status': False, 'UNIT_amount_multiplied_from_share': False,
        'CVM_notice_reports_ISIN': False, 'ISIN_binding_basis': 'exact_cached_CVM_CNPJ_ticker_class_event_identity',
        'gross_reconstructed_from_net': False}


def verified_calendar(folder, stock, cutoff=CUTOFF):
    require(cutoff == CUTOFF and all(type(stock.get(k)) is type(v) and stock[k] == v for k, v in STOCK.items()),
            'reviewed_exact_BPAC5_identity_and_cutoff_required')
    reader = evidence.EvidenceReader(folder)
    documents, coverage = indexed_documents(reader, cutoff)
    selected = select_documents(documents)
    form, unusable = read_originals(reader, selected)
    payloads = cached_sources(reader)
    proof = prove_whole_cash_group(form, payloads['expanded/b3-detail-22616.json'], payloads['expanded/BPAC3-b3-cash-1.json'])
    return {**STOCK, 'as_of': cutoff.isoformat(), 'notice': form, 'selected_documents': selected,
        'unusable_latest_native_form': unusable, 'cash_type_proof': proof, 'IPE_coverage': coverage,
        'evidence_sources': deepcopy(list(reader.sources.values())), 'rule': RULE,
        'cash_model_eligible': False, 'parent_admission_performed': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}


def vendor_amount_text(body, event):
    match = re.fullmatch(r'table:(\d+):row:(\d+)', event['source_row'])
    require(match is not None, 'exact_original_vendor_row_locator_required')
    tables = html.fromstring(body, parser=html.HTMLParser(no_network=True)).xpath('//table')
    ti, ri = int(match[1]) - 1, int(match[2]) - 1
    require(0 <= ti < len(tables), 'vendor_table_locator_missing')
    rows = list(inspector._table_rows(tables[ti]))
    columns, header, _ = inspector._find_header(rows)
    require(columns and header < ri < len(rows) and 'gross_per_share' in columns, 'vendor_amount_column_missing')
    raw = rows[ri][columns['gross_per_share']]
    require(re.fullmatch(r'0,\d{8}', raw) is not None, 'reviewed_vendor_display_format_required')
    return raw


def _repair_parsed(parsed, calendar, raw_amount):
    form = calendar['notice']
    require(parsed['report']['sha256'] == PAYMENTS_SHA and parsed['report']['as_of'] == calendar['as_of']
            and parsed['source_format'] == 'html_tables', 'exact_inspected_vendor_source_required')
    group = [e for e in parsed['events'] if e.get('record_date') == form['record_date']]
    require(len(group) == 1 and calendar['cash_type_proof']['whole_event_complete'] is True
            and calendar['cash_type_proof']['gross_reconstructed_from_net'] is False, 'whole_target_class_group_required')
    event = group[0]
    require(event['ticker'] == STOCK['ticker'] and event['label'] == 'JCP' and event['isin'] in (None, STOCK['isin'])
            and type(event['quoted_quantity']) is int and event['quoted_quantity'] == 1
            and event['approval_date'] in (None, form['approval_date']) and event['reported_status'] is None
            and event['date_order_valid'] is False and evidence.day(event['payment_date']) < event['record_date'],
            'only_exact_malformed_JCP_date_can_be_repaired')
    vendor = evidence.decimal(raw_amount)
    primary = Decimal(form['amounts']['share_gross'])
    require(Decimal(str(event['gross_per_share'])) == Decimal(str(event['reported_amount'])) == vendor
            and primary.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP) == vendor,
            'reviewed_vendor_six_decimal_numeric_value_must_match_explicit_primary_gross')
    issues = parsed['report']['issues']
    removed = [i for i in issues if i.get('row') == event['source_row']
               and i.get('code') == 'payment_before_record_date' and i.get('blocking') is True]
    require(len(removed) == 1 and not any(i.get('blocking') and i.get('row') == event['source_row']
            and i not in removed for i in issues), 'only_specific_date_blocker_may_be_removed')
    out = deepcopy(parsed)
    target = next(e for e in out['events'] if e['source_row'] == event['source_row'])
    source = next(s for s in calendar['evidence_sources'] if s['file'] == NOTICE)
    target.update(payment_date=form['payment_date'], date_order_valid=True,
        status='past_scheduled' if form['payment_date'] <= calendar['as_of'] else 'announced',
        primary_evidence=[deepcopy(source)], primary_declaration_id='BPAC5:' + form['record_date'],
        primary_exact_gross_per_share_decimal=str(primary),
        payment_date_basis='explicit_exact_issuer_notice_not_inferred_year_or_ex_date',
        payment_claim_basis='reported_schedule_not_bank_settlement', full_settlement_on_date_verified=False)
    out['report']['issues'] = [deepcopy(i) for i in issues if i not in removed]
    inspector._finish(out)
    out['btg_notice_calendar_repair'] = {'rule': RULE, 'original_group': deepcopy(group),
        'original_issues': deepcopy(issues), 'removed_issues': deepcopy(removed), 'changed_date_count': 1,
        'raw_vendor_amount_text': raw_amount, 'vendor_display_decimal_places': 8,
        'reviewed_numeric_comparison_decimal_places': 6, 'display_padding_independently_verified': False,
        'comparison_policy': 'explicit_reviewed_six_decimal_numeric_match_not_gross_or_share_scale_imputation',
        'primary_exact_gross_per_share_decimal': str(primary), 'vendor_gross_amount_changed': False,
        'whole_cached_event': deepcopy(calendar['cash_type_proof']),
        'notice': deepcopy(form), 'selected_documents': deepcopy(calendar['selected_documents']),
        'unusable_latest_native_form': deepcopy(calendar['unusable_latest_native_form']),
        'evidence_sources': deepcopy(calendar['evidence_sources']),
        'settlement_verified': False, 'point_in_time_archive_verified': False,
        'parent_admission_performed': False, 'financial_checks_performed': False,
        'required_parent_gates': ['normal_independent_cash_and_raw_price_reconciliation',
                                  'normal_share_action_and_annual_financial_and_training_gates']}
    return out


def repair_inspected_calendar(folder, stock, cutoff, parsed):
    calendar = verified_calendar(folder, stock, cutoff)
    observed = datetime.fromisoformat(parsed['report']['observed_at'])
    require(observed.utcoffset() is not None and observed.date() >= cutoff, 'valid_inspector_observation_required')
    body = evidence.shared._safe(Path(folder).resolve(), PAYMENTS).read_bytes()
    require(hashlib.sha256(body).hexdigest() == PAYMENTS_SHA, 'vendor_bytes_changed_after_verification')
    expected = inspector.inspect_payment_page(body, stock['ticker'], cutoff, observed)
    require(parsed == expected, 'parsed_rows_or_issues_not_exact_original_inspector_output')
    group = [e for e in parsed['events'] if e.get('record_date') == calendar['notice']['record_date']]
    require(len(group) == 1, 'whole_target_class_group_required')
    return _repair_parsed(parsed, calendar, vendor_amount_text(body, group[0]))


def build_report(folder):
    calendar = verified_calendar(folder, STOCK)
    report = {'schema_version': 1, 'status': 'verified_date_only_repair_pending_all_normal_parent_gates',
        'calendar': calendar, 'network_requests': 0, 'parent_files_changed': False,
        'pointers_written': False, 'thresholds_changed': False, 'parent_admission_performed': False,
        'integration_contract': {'API': 'repair_inspected_calendar(folder, stock, cutoff, parsed)',
            'hook': 'immediately_after_inspector_before_primary_enrichment_and_any_payment_issue_gate',
            'stock': STOCK, 'cutoff': CUTOFF.isoformat(),
            'source_registration': 'all calendar.evidence_sources actual SHA file URL retrieval timestamp',
            'preserve_audit': 'parsed.btg_notice_calendar_repair including unusable_latest_native_form and original amounts',
            'normal_gates_unchanged': True}}
    body = evidence.shared._dump(report)
    name = CACHE + '/report-' + hashlib.sha256(body).hexdigest() + '.json'
    path = evidence.shared._safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_owned_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return report, name


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    args = parser.parse_args()
    report, name = build_report(args.folder)
    print(json.dumps({'report': name, 'status': report['status'], 'network_requests': 0,
                      'parent_admission_performed': False}, indent=2))
