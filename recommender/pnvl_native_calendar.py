"""Offline reviewed native calendar; no collection, admission or pointer writes.

Dates and gross amounts come exclusively from a complete latest protocol-bound
CVM form. Explicit cash type is proved separately using an issuer notice and
the unique full cached exchange event, not amounts or payment status alone.
Unversioned issuer copies never become purported exact CVM revision copies.
"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import pdfplumber

from . import primary_cash_exact_research as exact


CACHE = 'raw/pnvl-native-calendar'
CUTOFF = date(2026, 9, 30)
STOCK = {'ticker': 'PNVL3', 'cvm': 9342, 'share_class': 'ON', 'isin': 'BRPNVLACNOR9'}
APPROVAL = '2024-03-28'
FORM_SHA = '054d45dcb7270d7ee621616d28eb342dba1c8fa074ce5204309c7237b0b965f2'
BOARD_SHA = 'ba54a72384c647067602e968198f492d198d3201ba75c43cd4cdad66339f2b61'
FOLLOWON = 'raw/primary-research/primary-cash-followon/report-v3-e241dfd532c456bbd1587e8999181b016de56ccf355e49be670a6108d96f4429.json'
FOLLOWON_SHA = 'e241dfd532c456bbd1587e8999181b016de56ccf355e49be670a6108d96f4429'
EXCHANGE_AUDIT_SHA = '0b80c25e01dbe15445d13f593622d21fe15cc345cb99e281723db61da0473ac5'
RULE_ID = 'latest_complete_protocol_bound_native_CVM_reported_schedule_v1'


class ProofError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ProofError(code)


def modelling_priority_rule():
    return {'id': RULE_ID,
        'scope': 'generic_any_issuer_meeting_all_proof_guards_not_reconciliation_optimisation',
        'schedule_authority': 'latest_complete_exact_protocol_bound_native_CVM_form',
        'selection_uses_cash_or_price_match': False,
        'cash_type_requires_separate_explicit_full_event_proof': True,
        'unresolved_later_same_event_revision': 'fail_closed',
        'conflicting_unversioned_RI_prose': 'preserve_with_original_SHA_and_require_metadata_warning',
        'company_legal_precedence_claim': False, 'date_amendment_claim': False,
        'RI_typo_claim': False, 'settlement_claim': False}


class EvidenceReader:
    """Keep distinct actual retrieval URLs even when their bytes are identical."""
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.reader = exact.shared.Reader(self.folder)
        self.sources = {}

    def read(self, name, role, expected=None, url=None):
        body, actual = self.reader.read(name, role, expected, url)
        key = (name, url)
        source = self.sources.setdefault(key, {**actual, 'roles': [], 'url': url, 'retrieved_at': None})
        require(source['sha256'] == actual['sha256'], 'evidence_changed_during_verification')
        if role not in source['roles']:
            source['roles'].append(role)
        return body, source

    def json(self, name, role):
        body, source = self.read(name, role)
        return json.loads(body), source


def save_report(folder, value):
    body = exact.shared._dump(value)
    name = CACHE + '/report-' + exact.shared._sha(body) + '.json'
    path = exact.shared._safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_owned_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def strict_day(value):
    require(type(value) is str, 'explicit_date_string_required')
    result = exact.cvm.day(value)
    require(result is not None, 'invalid_or_inferred_date_not_allowed')
    return result


def amount(value, *, annex=False):
    require(type(value) is str, 'Decimal_amount_requires_explicit_string_not_float')
    text = exact.cvm.compact(value)
    if annex and re.fullmatch(r'\d+\.\d{4,}', text):
        result = Decimal(text)
    else:
        require(not annex or '.' not in text or ',' in text, 'ambiguous_native_annex_decimal_dot')
        try:
            result = exact.cvm.gross(text)
        except ValueError as error:
            raise ProofError('invalid_explicit_gross_amount') from error
    require(result.is_finite() and result > 0, 'nonpositive_or_nonfinite_gross_amount')
    return result


def metadata(tables, label, *, optional=False):
    matches = []
    for table in tables:
        for index, row in enumerate(table):
            for column, cell in enumerate(row):
                if exact.cvm.norm(cell) == label:
                    require(index + 1 < len(table) and column < len(table[index + 1]), 'metadata_value_missing')
                    matches.append(table[index + 1][column])
    if optional and not matches:
        return None
    require(len(matches) == 1 and exact.cvm.compact(matches[0]), 'metadata_missing_or_ambiguous:' + label)
    return matches[0]


def parse_native_tables(tables, document, stock, cutoff):
    """Issuer-agnostic complete-form parser; never repairs or drops annex rows."""
    issuer = metadata(tables, 'denominacao social')
    key = lambda s: re.sub(r'[^a-z0-9]', '', exact.norm(s))
    require(key(issuer) == key(document['issuer_name']), 'native_issuer_name_mismatch')
    raw = document['index_evidence']['raw_row']
    cnpj = re.sub(r'\D', '', raw['CNPJ_Companhia'])
    require(len(cnpj) == 14 and key(raw['Nome_Companhia']) == key(issuer), 'native_index_CNPJ_or_name_mismatch')
    ipe_cell = metadata(tables, 'protocolo ipe')
    identifiers = re.findall(r'\b\d{6}IPE\d{8}\d{2}\d+-\d+\b', ipe_cell)
    require(len(identifiers) == 1 and int(identifiers[0][:6]) == stock['cvm'] == document['cvm'],
            'native_CVM_or_full_IPE_identity_mismatch')
    approval = strict_day(exact.cvm.compact(metadata(tables, 'data aprovacao')))
    record = strict_day(exact.cvm.compact(metadata(tables, 'ultimo dia de negociacao com direitos')))
    sent = strict_day(exact.cvm.compact(metadata(tables, 'data envio')))
    published = strict_day(document['published_at'])
    require(approval == document['reference_date'] and approval <= record
            and approval <= sent <= published <= cutoff.isoformat(), 'native_date_order_or_publication_invalid')
    version_text = exact.cvm.compact(metadata(tables, 'versao'))
    protocol = exact.cvm.compact(metadata(tables, 'protocolo provento'))
    require(version_text.isdigit() and int(version_text) == document['version']
            and type(document['version']) is int and protocol.isdigit(), 'native_version_or_protocol_invalid')
    headers = [(ti, ri) for ti, t in enumerate(tables) for ri, r in enumerate(t)
               if r and 'isin' in exact.cvm.norm(r[0])]
    require(len(headers) == 1, 'one_entire_explicit_ISIN_table_required')
    ti, hi = headers[0]
    header = tables[ti][hi]
    require(len(header) == 7 and exact.cvm.norm(header[1]) == 'valor bruto r unidade'
            and exact.cvm.norm(header[6]) == 'data pagamento', 'explicit_gross_ISIN_header_required')
    rows = tables[ti][hi + 1:]
    require(len(rows) == 1 and len(rows[0]) == 7, 'strict_one_ISIN_whole_form_required')
    row = rows[0]
    isin = exact.cvm.compact(row[0])
    require(isin == stock['isin'] and re.fullmatch(r'BR[A-Z0-9]{9}\d', isin), 'native_ISIN_mismatch')
    require(exact.cvm.norm(row[4]) == 'nao' and exact.cvm.norm(row[5]) == 'parcelado'
            and not exact.cvm.compact(row[6]), 'native_update_or_payment_form_unresolved')
    require(exact.cvm.compact(row[2]) and re.fullmatch(r'\d{4}', exact.cvm.compact(row[3])),
            'native_fiscal_period_required')
    total = amount(row[1])
    annexes = [(i, t) for i, t in enumerate(tables) if t and t[0]
               and 'pagamento das parcelas' in exact.cvm.norm(t[0][0])]
    require(len(annexes) == 1, 'one_whole_native_annex_required')
    ai, annex = annexes[0]
    require(len(annex[0]) == 2 and exact.cvm.norm(annex[0][1]) == 'valor bruto r unidade',
            'explicit_gross_annex_header_required')
    require(len(annex) > 1, 'empty_or_partial_native_annex')
    installments = []
    for index, item in enumerate(annex[1:], 1):
        require(len(item) == 2, 'malformed_annex_row_must_not_be_skipped')
        payment = strict_day(item[0])
        gross = amount(item[1], annex=True)
        require(payment >= record, 'payment_before_record_date')
        installments.append({'payment_date': payment, 'gross': str(gross),
                             'locator': {'table': ai, 'row': index}})
    dates = [i['payment_date'] for i in installments]
    require(len(set(dates)) == len(dates), 'duplicate_or_ambiguous_installment_date')
    require(dates == sorted(dates), 'native_annex_order_invalid')
    require(sum((Decimal(i['gross']) for i in installments), Decimal(0)) == total,
            'entire_annex_Decimal_sum_disagrees_with_gross_total')
    kind_value = metadata(tables, 'tipo de provento', optional=True)
    kind = explicit_kind(kind_value) if kind_value is not None else None
    return {'approval_date': approval, 'record_date': record, 'published_at': published,
        'sent_at': sent, 'native_protocol': protocol, 'native_IPE': identifiers[0],
        'version': int(version_text), 'isin': isin, 'issuer_cnpj': cnpj, 'gross_total_decimal': str(total),
        'installments': installments, 'cash_kind': kind, 'fiscal_year': exact.cvm.compact(row[3]),
        'period': exact.cvm.norm(row[2]), 'amount_tax_basis': 'explicit_gross_exact_ISIN_unit',
        'gross_reconstructed_from_net': False, 'settlement_verified': False}


def explicit_kind(value):
    normalized = exact.cvm.norm(value)
    if normalized in ('jcp', 'juros sobre capital proprio', 'jrs cap proprio'):
        return 'JCP'
    if normalized in ('dividendo', 'dividendos'):
        return 'DIVIDENDO'
    raise ProofError('cash_kind_missing_or_ambiguous_not_inferred')


def select_revision_set(documents, stock, approval, cutoff):
    available = [d for d in documents if d['cvm'] == stock['cvm'] and d['published_at'] <= cutoff.isoformat()]
    def family(document):
        ipe = document['index_evidence']['raw_row']['Protocolo_Entrega']
        match = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', ipe)
        return (match[1], match[2]) if match else None
    families = {family(d) for d in available if d['reference_date'] == approval and family(d)}
    related = [d for d in available if d['reference_date'] == approval or family(d) in families]
    categories = {'form': 'relatorio proventos', 'board': 'reuniao da administracao', 'notice': 'aviso aos acionistas'}
    selected = {}
    for role, category in categories.items():
        rows = [d for d in related if exact.cvm.norm(d['category']) == category]
        require(rows, 'same_event_index_category_missing:' + role)
        require(all(type(d['version']) is int and d['version'] > 0 for d in rows), 'invalid_index_version')
        latest = [d for d in rows if d['version'] == max(r['version'] for r in rows)
                  and d['published_at'] == max(r['published_at'] for r in rows)]
        require(len(latest) == 1, 'latest_same_event_version_ambiguous:' + role)
        selected[role] = latest[0]
    form = selected['form']
    require(all(d['version'] <= form['version'] and d['published_at'] <= form['published_at']
                for d in related), 'unresolved_later_same_event_revision_no_fallback')
    for role, document in selected.items():
        sequence, version = exact.cvm.document_id(document['url'])
        require((sequence, version) == (document['sequence'], document['version']), 'indexed_URL_version_mismatch')
        raw = document['index_evidence']['raw_row']
        require(raw['Link_Download'] == document['url'] and int(raw['Codigo_CVM']) == stock['cvm']
                and int(raw['Versao']) == version and strict_day(raw['Data_Entrega']) == document['published_at']
                and strict_day(raw['Data_Referencia']) == approval, 'exact_index_row_metadata_mismatch')
        if role != 'form':
            ipe = raw['Protocolo_Entrega']
            match = re.fullmatch(r'(\d{6})IPE(\d{8})(\d{2})(\d+-\d+)', ipe)
            require(match is not None and int(match[1]) == stock['cvm'] and int(match[3]) == version
                    and match[2] == date.fromisoformat(approval).strftime('%d%m%Y'), 'full_IPE_revision_metadata_mismatch')
    return selected, related


def bind_native_protocol(form, revisions):
    board = revisions['board']
    protocol = parse_qs(urlsplit(board['url']).query)['numProtocolo'][0]
    require(form['native_protocol'] == protocol
            and form['native_IPE'] == board['index_evidence']['raw_row']['Protocolo_Entrega']
            and form['version'] == board['version'] == revisions['form']['version']
            and board['published_at'] <= form['sent_at'] <= form['published_at'],
            'native_form_not_bound_to_latest_exact_board_protocol_and_IPE')


def verify_cached_identity(detail, stock, cnpj):
    require(str(detail.get('codeCVM', '')).isdigit() and int(detail['codeCVM']) == stock['cvm']
            and detail.get('cnpj') == cnpj, 'cached_identity_CVM_or_CNPJ_mismatch')
    matches = [r for r in detail.get('otherCodes', []) if r.get('code') == stock['ticker']]
    require(len(matches) == 1 and matches[0].get('isin') == stock['isin'], 'cached_identity_exact_ticker_ISIN_mismatch')
    require(stock['share_class'] in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'), 'unsupported_share_class')


def review_notice(body):
    pages = exact.pdf_pages(body)
    require(len(pages) == 2, 'reviewed_notice_full_PT_EN_body_required')
    text = exact.norm(pages[0])
    required = ['cnpj no 92.665.611/0001-77', '(pnvl3)', 'acoes ordinarias',
        'realizada no dia 28 de marco de 2024', 'posicao acionaria de 05/04/2024',
        'valor por acao (bruto)', 'pagos em duas parcelas',
        'juros a titulo de remuneracao sobre o capital proprio']
    require(all(span in text for span in required), 'full_explicit_issuer_notice_binding_spans_missing')
    require('installments payment start date pnvl3 (on)' in exact.norm(pages[1]),
            'notice_start_date_qualification_missing')
    rows = re.findall(r'([12])[ao] parcela (\d{2}/\d{2}/\d{4}) r\$ (\d+,\d+)', text)
    require([r[0] for r in rows] == ['1', '2'], 'whole_reviewed_notice_table_required')
    total = re.findall(r'total r\$ (\d+,\d+)', text)
    require(len(total) == 1, 'notice_gross_total_ambiguous')
    return {'cnpj': '92665611000177', 'ticker': 'PNVL3', 'share_class': 'ON',
        'approval_date': '2024-03-28', 'record_date': '2024-04-05', 'label': 'JCP',
        'label_basis': 'explicit_issuer_notice_principal_description_not_annual_dividend_imputation',
        'gross_total_decimal': str(amount(total[0])),
        'installments': [{'payment_date': strict_day(d), 'gross': str(amount(a))} for _, d, a in rows],
        'explicit_cash_kind_span': 'juros a titulo de remuneracao sobre o capital proprio',
        'version': None, 'exact_CVM_copy_identity_verified': False,
        'date_basis': 'explicit_payment_start_date_not_settlement'}


def verify_cash_pages(pages):
    require(bool(pages), 'cached_exchange_pages_missing')
    first = pages[0].get('page', {})
    count, total = first.get('totalPages'), first.get('totalRecords')
    require(type(count) is int and type(total) is int and count == len(pages) and total > 0,
            'cached_exchange_pagination_incomplete')
    rows = []
    for number, page in enumerate(pages, 1):
        meta = page.get('page', {})
        require(meta.get('pageNumber') == number and meta.get('totalPages') == count
                and meta.get('totalRecords') == total and meta.get('pageSize') == first.get('pageSize')
                and type(page.get('results')) is list, 'cached_exchange_pagination_inconsistent')
        rows.extend({'page': number, 'row': index, 'raw': row} for index, row in enumerate(page['results']))
    require(len(rows) == total, 'cached_exchange_row_count_incomplete')
    return rows


def prove_cash_kind(form, notice, stock, detail, pages):
    """Bind explicit labels through identity, approval, rights and the whole event.

    Exchange prices/status do not enter this proof. Exact gross components are
    additional identity terms, not a mechanism for choosing a preferred label.
    """
    verify_cached_identity(detail, stock, notice['cnpj'])
    require(notice['cnpj'] == form['issuer_cnpj'], 'native_index_and_notice_CNPJ_mismatch')
    require((notice['ticker'], notice['share_class'], notice['approval_date'], notice['record_date']) ==
            (stock['ticker'], stock['share_class'], form['approval_date'], form['record_date']),
            'issuer_notice_full_event_identity_mismatch')
    require(Decimal(notice['gross_total_decimal']) == Decimal(form['gross_total_decimal'])
            and [(i['payment_date'], Decimal(i['gross'])) for i in notice['installments']] ==
                [(i['payment_date'], Decimal(i['gross'])) for i in form['installments']],
            'issuer_notice_entire_gross_group_mismatch')
    rows = verify_cash_pages(pages)
    matches = [r for r in rows if r['raw'].get('typeStock', '').strip() == stock['share_class']
               and exact.cvm.day(r['raw'].get('dateApproval')) == form['approval_date']
               and exact.cvm.day(r['raw'].get('lastDatePriorEx')) == form['record_date']]
    require(len(matches) == len(form['installments']), 'unique_whole_cached_cash_event_required')
    labels = {explicit_kind(r['raw'].get('corporateAction')) for r in matches}
    require(len(labels) == 1 and labels == {notice['label']}, 'explicit_cash_type_conflict_no_majority_choice')
    require(form['cash_kind'] is None or labels == {form['cash_kind']}, 'native_and_supplemental_cash_type_conflict')
    values = []
    for located in matches:
        raw = located['raw']
        require(raw.get('quotedPerShares') == '1' and raw.get('ratio') == '1', 'type_binding_requires_exact_unscaled_share_unit')
        values.append(amount(raw['valueCash']))
    native = [Decimal(i['gross']) for i in form['installments']]
    require(len(set(native)) == len(native) and Counter(values) == Counter(native),
            'nonunique_or_partial_gross_component_type_binding')
    return {'label': notice['label'],
        'basis': 'explicit_issuer_notice_plus_unique_complete_cached_exchange_event',
        'binding_terms': ['CVM', 'CNPJ', 'ticker', 'ISIN', 'share_class', 'approval_date',
                          'record_date', 'each_gross_installment', 'whole_gross_sum', 'explicit_type'],
        'located_cached_rows': matches, 'issuer_notice_version': None,
        'issuer_notice_exact_CVM_v2_identity_verified': False,
        'prior_version_type_inherited': False, 'kind_inferred_from_amount_or_status': False}


def read_followon(reader):
    body, _ = reader.read(FOLLOWON, 'frozen_RI_acquisition_and_catalogue_source_chain', FOLLOWON_SHA)
    report = json.loads(body)
    for source in report['source_receipts']:
        reader.read(source['file'], 'actual_frozen_RI_chain_' + '_'.join(source['roles']),
                    source['sha256'], source['url'])
    for receipt in report['actual_HTTP_reader_receipts']:
        _, source = reader.read(receipt['file'], 'actual_HTTP_original_receipt', receipt['sha256'], receipt['url'])
        source['retrieved_at'] = receipt['retrieved_at']
    return report['results'][0]['originals']


def cached_exchange(reader):
    body, _ = reader.read('expanded-source-audit.json', 'cached_exchange_original_acquisition_manifest', EXCHANGE_AUDIT_SHA)
    audit = json.loads(body)
    wanted = ['expanded/b3-detail-9342.json', 'expanded/PNVL3-b3-cash-1.json', 'expanded/PNVL3-b3-cash-2.json']
    payloads, sources = [], []
    for name in wanted:
        candidates = [s for s in audit['sources'] if s['file'] == name]
        require(len(candidates) == 1, 'one_cached_exchange_original_receipt_required')
        receipt = candidates[0]
        body, source = reader.read('raw/' + name, 'cached_exchange_type_identity_only_no_network',
                                   receipt['sha256'], receipt['url'])
        source['retrieved_at'] = receipt.get('retrieved_at')
        payloads.append(json.loads(body))
        sources.append(source)
    return payloads[0], payloads[1:], sources


def source_conflict(board_body, source, form_source):
    text = exact.norm(exact.pdf_pages(board_body)[0])
    require('30 de agosto de 2024, 28 de marco de marco de 2025' in text
            and '2a parcela do jcp 31/03/2025 r$ 0,01880932' in text, 'reviewed_RI_conflict_spans_missing')
    return {'id': 'unresolved_RI_board_prose:' + source['sha256'], 'unresolved': True,
        'source_file': source['file'], 'source_sha256': source['sha256'], 'source_url': source['url'],
        'source_version': None, 'exact_CVM_v2_copy_identity_verified': False, 'page': 1,
        'reported_prose_date': '2025-03-28', 'reported_table_date': '2025-03-31',
        'model_declared_date': '2025-03-31', 'model_date_source_sha256': form_source['sha256'],
        'resolution': None, 'typo_claim': False, 'date_amendment_claim': False,
        'modelling_priority_rule_id': RULE_ID, 'settlement_verified': False}


def validate_calendar_contract(calendar):
    require(calendar.get('modelling_priority_rule') == modelling_priority_rule(), 'model_priority_rule_missing_or_changed')
    require(len(calendar.get('complete_groups', [])) == 1 and calendar.get('source_conflicts'),
            'complete_group_and_source_conflicts_required')
    group = calendar['complete_groups'][0]
    events, form = calendar.get('events'), calendar['native_form']
    require(events and events == group['events'] and len(events) == group['installment_count']
            and group['target_group_complete'] is True, 'atomic_whole_calendar_group_required')
    require([(e['payment_date'], e['gross_per_share_decimal']) for e in events] ==
            [(i['payment_date'], i['gross']) for i in form['installments']], 'calendar_must_preserve_entire_native_annex')
    require(all(type(e['gross_per_share_decimal']) is str for e in events), 'calendar_Decimal_strings_required')
    require(type(group['gross_total_decimal']) is str
            and all(Decimal(e['gross_per_share_decimal']).is_finite()
                    and Decimal(e['gross_per_share_decimal']) > 0 for e in events), 'calendar_positive_finite_gross_required')
    require(sum((Decimal(e['gross_per_share_decimal']) for e in events), Decimal(0)) ==
            Decimal(group['gross_total_decimal']) == Decimal(form['gross_total_decimal']), 'calendar_full_gross_sum_required')
    proof = group['cash_type_proof']
    require(proof['label'] in ('JCP', 'DIVIDENDO') and proof['kind_inferred_from_amount_or_status'] is False
            and proof['issuer_notice_version'] is None and proof['prior_version_type_inherited'] is False,
            'explicit_cash_type_and_unknown_RI_version_must_be_preserved')
    for event in events:
        require(all(event[k] == calendar[k] for k in STOCK)
                and event['complete_group_id'] == group['id'] and event['label'] == proof['label']
                and event['gross_reconstructed_from_net'] is False
                and event['reported_per_share_decimal'] == event['gross_per_share_decimal']
                and event['payment_date_basis'] == 'explicit_calendar_date'
                and event['full_settlement_on_date_verified'] is False and event['issues'] == []
                and event['source_conflict_ids'] == [c['id'] for c in calendar['source_conflicts']],
                'calendar_identity_type_gross_or_conflict_contract_changed')
    receipts = {(s['file'], s['sha256']) for s in calendar['evidence_sources']}
    require(any(digest == FORM_SHA for _, digest in receipts)
            and all((s['file'], s['sha256']) in receipts for s in proof['source_receipts']),
            'native_and_explicit_type_source_closure_required')
    for conflict in calendar['source_conflicts']:
        require((conflict['source_file'], conflict['source_sha256']) in receipts
                and conflict['source_sha256'] == BOARD_SHA
                and conflict['reported_prose_date'] == '2025-03-28'
                and conflict['reported_table_date'] == conflict['model_declared_date'] == '2025-03-31'
                and conflict['model_date_source_sha256'] == FORM_SHA and conflict['unresolved'] is True
                and conflict['source_version'] is None and conflict['resolution'] is None
                and conflict['exact_CVM_v2_copy_identity_verified'] is False
                and conflict['modelling_priority_rule_id'] == RULE_ID
                and conflict['typo_claim'] is False and conflict['date_amendment_claim'] is False
                and conflict['settlement_verified'] is False, 'known_source_SHA_unresolved_conflict_must_be_preserved')
    return True


def metadata_warnings(calendar):
    validate_calendar_contract(calendar)
    require(calendar.get('modelling_priority_rule') == modelling_priority_rule(), 'model_priority_rule_missing_or_changed')
    return [{'ticker': calendar['ticker'], 'complete_group_id': calendar['complete_groups'][0]['id'],
        'warning': 'unresolved_source_date_conflict_native_reported_schedule_selected', **deepcopy(conflict)}
        for conflict in calendar['source_conflicts']]


def validate_warning_propagation(calendar, warnings):
    expected = metadata_warnings(calendar)
    require(all(sum(row == item for row in warnings) == 1 for item in expected),
            'required_source_conflict_metadata_warning_missing_changed_or_duplicated')
    return True


def _verified_calendar(reader, stock, cutoff):
    require(cutoff == CUTOFF, 'reviewed_cutoff_required')
    require(all(stock.get(k) == v and type(stock.get(k)) is type(v) for k, v in STOCK.items()),
            'reviewed_exact_security_identity_required')
    documents, coverage = exact.indexed_documents(reader)
    revisions, related = select_revision_set(documents, stock, APPROVAL, cutoff)
    document = revisions['form']
    require(document['sequence'] == '742120' and document['version'] == 2,
            'latest_native_form_not_reviewed_742120v2_no_fallback')
    body, source = exact.document_body(reader, document)
    require(body is not None and source['sha256'] == FORM_SHA, 'exact_reviewed_native_original_required')
    exact.pdf_pages(body)
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    form = parse_native_tables(tables, document, stock, cutoff)
    require(form['record_date'] == '2024-04-05', 'reviewed_entitlement_record_date_changed')
    bind_native_protocol(form, revisions)
    receipt_file = source['file'].removesuffix('.pdf') + '-source.json'
    receipt, _ = reader.json(receipt_file, 'native_original_acquisition_receipt')
    require(all(receipt.get(k) == source[k] for k in ('file', 'sha256', 'url', 'bytes'))
            and all(receipt['document'].get(k) == document[k] for k in
                    ('sequence', 'version', 'cvm', 'published_at', 'reference_date')),
            'native_original_acquisition_receipt_mismatch')
    source['retrieved_at'] = receipt.get('retrieved_at')
    originals = read_followon(reader)
    notice_original, board_original = originals['741697'], originals['741696']
    notice_body, notice_source = reader.read(notice_original['source']['file'], 'unversioned_issuer_explicit_type_corroboration',
        notice_original['source']['sha256'], notice_original['source']['url'])
    board_body, board_source = reader.read(board_original['source']['file'], 'unversioned_RI_date_conflict_audit_only',
        board_original['source']['sha256'], board_original['source']['url'])
    notice = review_notice(notice_body)
    detail, pages, exchange_sources = cached_exchange(reader)
    proof = prove_cash_kind(form, notice, stock, detail, pages)
    proof['source_receipts'] = deepcopy([notice_source, *exchange_sources])
    conflict = source_conflict(board_body, board_source, source)
    primary = {**source, 'id': 'pnvl-native:' + source['sha256'],
               'authority': 'primary_CVM_reported_calendar_not_settlement', 'document': document,
               'observed_at': None, 'observed_at_basis': 'unknown_if_actual_retrieval_receipt_is_missing'}
    group_id = stock['ticker'] + ':' + form['record_date']
    events = []
    for index, installment in enumerate(form['installments'], 1):
        events.append({**STOCK, 'label': proof['label'], 'label_basis': proof['basis'],
            'record_date': form['record_date'], 'approval_date': form['approval_date'],
            'complete_group_id': group_id, 'target_group_complete': True,
            'payment_date': installment['payment_date'], 'published_at': document['published_at'],
            'available_as_of': True, 'reported_per_share_decimal': installment['gross'],
            'gross_per_share_decimal': installment['gross'], 'amount_tax_basis': form['amount_tax_basis'],
            'gross_reconstructed_from_net': False, 'share_basis': 'reported_unverified',
            'payment_date_basis': 'explicit_calendar_date',
            'payment_claim_basis': 'latest_protocol_bound_native_CVM_reported_schedule_not_settlement',
            'corroborating_notice_date_basis': notice['date_basis'],
            'full_settlement_on_date_verified': False, 'is_settlement_confirmation': False,
            'source_ref': primary['id'], 'primary_declaration_id': group_id + ':native-installment:' + str(index),
            'source_locator': {'format': 'exact_reviewed_native_entitlement_group', 'sequence': '742120',
                'version': 2, **installment['locator']},
            'status': 'past_scheduled' if installment['payment_date'] <= cutoff.isoformat() else 'announced',
            'source_conflict_ids': [conflict['id']], 'modelling_priority_rule_id': RULE_ID,
            'issues': [], 'cash_model_eligible': False, 'point_in_time_archive_verified': False})
    calendar = {**STOCK, 'as_of': cutoff.isoformat(), 'events': events, 'sources': [primary],
        'evidence_sources': deepcopy(list(reader.sources.values())), 'native_form': form,
        'complete_groups': [{'id': group_id, **STOCK, 'record_date': form['record_date'],
            'target_group_complete': True, 'installment_count': len(events), 'component_count': len(events),
            'gross_total_decimal': form['gross_total_decimal'], 'events': deepcopy(events),
            'cash_type_proof': proof, 'source_chain': {'native_form': document, 'latest_revisions': revisions,
                'full_protocol_bound_board_IPE': form['native_IPE'], 'same_event_index_records': related,
                'coverage': coverage}}],
        'source_conflicts': [conflict], 'modelling_priority_rule': modelling_priority_rule(),
        'manifest_pins': {
            'native_original': {'file': source['file'], 'sha256': FORM_SHA},
            'IPE_manifest': {'file': 'raw/primary-research/cvm-documents/index-sources.json',
                             'sha256': exact.IPE_MANIFEST_SHA},
            'frozen_RI_chain': {'file': FOLLOWON, 'sha256': FOLLOWON_SHA},
            'cached_exchange_acquisition_manifest': {'file': 'expanded-source-audit.json', 'sha256': EXCHANGE_AUDIT_SHA}},
        'warning_propagation_contract': {'required': True, 'API': 'metadata_warnings(calendar); validate_warning_propagation(calendar, actual_flat_warnings)',
            'preserve_fields': ['source_sha256', 'source_version', 'reported_prose_date', 'reported_table_date',
                                'model_declared_date', 'model_date_source_sha256', 'unresolved', 'modelling_priority_rule_id'],
            'parent_implementation_performed': False},
        'required_parent_gates': ['normal_atomic_replace_verified_primary_groups_expected_actions',
            'normal_cash_reconciliation', 'normal_price_reconciliation', 'normal_financial_features_and_training',
            'source_conflict_warning_in_flat_metadata_and_dataset'],
        'parent_admission_performed': False, 'financially_validated': False,
        'history_complete': False, 'active_model_changed': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False}
    validate_warning_propagation(calendar, metadata_warnings(calendar))
    return calendar


def verified_calendar(folder, stock, cutoff=CUTOFF):
    return _verified_calendar(EvidenceReader(folder), stock, cutoff)


def build_report(folder):
    reader = EvidenceReader(folder)
    try:
        calendar = _verified_calendar(reader, STOCK, CUTOFF)
        outcome = {'status': 'verified_reported_calendar_pending_parent_gates', 'calendar': calendar,
                   'required_metadata_warnings': metadata_warnings(calendar), 'blocked_reason': None}
    except (ProofError, ValueError, OSError, KeyError) as error:
        outcome = {'status': 'blocked_no_calendar', 'calendar': None, 'blocked_reason': str(error),
                   'required_metadata_warnings': []}
    report = {'schema_version': 1, **outcome, 'evidence_sources': deepcopy(list(reader.sources.values())),
        'modelling_priority_rule': modelling_priority_rule(), 'network_requests': 0,
        'production_files_changed': False, 'old_reports_changed': False, 'pointers_written': False,
        'parent_admission_performed': False, 'thresholds_changed': False}
    return report, save_report(folder, report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    args = parser.parse_args()
    report, path = build_report(args.folder)
    print(json.dumps({'report': path, 'status': report['status'], 'blocked_reason': report['blocked_reason'],
                      'network_requests': 0, 'parent_admission_performed': False}, indent=2))
