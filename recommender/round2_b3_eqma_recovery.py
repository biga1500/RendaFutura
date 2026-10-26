"""Primary cash recovery and real offline gate replays for B3SA3/EQMA3B.

Current exact identity and historical event identity are separate questions.
The ordinary-stock core does not impose an unchanged five-year ticker/ISIN
rule. Historical events below require actual issuer/class/date/amount evidence;
neither a ticker alias nor a payment date is inferred from an ex date.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import io
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit
from unittest.mock import patch

from lxml import html

from . import round2_security_identity as identity_research
from .bulk_pending_sources import _save


ROOT = identity_research.ROOT
BASE = identity_research.BASE
CUTOFF = identity_research.CUTOFF
START = identity_research.WINDOW_START
TARGETS = identity_research.TARGETS
B3_HTML_SHA = 'a12b306f61958aea1983379316e09c2d2315cf1c0d224aeb943b8c21d6d85af6'
B3_HTML = f'raw/primary-research/operating/B3SA3-landing-{B3_HTML_SHA}.html'
B3_RECEIPT = f'raw/primary-research/operating/B3SA3-landing-{B3_HTML_SHA}-source.json'
B3_FINAL_FORMS = frozenset(('589310', '607655', '641824', '670240', '701510', '724580',
    '737422', '776072', '809547', '845300', '876002', '917794', '952631', '987382',
    '1023798', '1062710', '1095274'))
MONTHS = 'janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro'.split()
PT_DATE = r'(?:\d{1,2}\s+de\s+(?:' + '|'.join(MONTHS) + r')\s+de\s+\d{4}|\d{2}/\d{2}/\d{4})'


def require(value, reason):
    if not value:
        raise ValueError('b3_eqma_' + reason)


def norm(value):
    return identity_research.norm(value)


def exact_date(value):
    text = norm(value)
    if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', text):
        return datetime.strptime(text, '%d/%m/%Y').date().isoformat()
    match = re.fullmatch(r'(\d{1,2}) de (\w+) de (\d{4})', text)
    require(match is not None and match[2] in MONTHS, 'literal_date_not_supported')
    return date(int(match[3]), MONTHS.index(match[2]) + 1, int(match[1])).isoformat()


def decimal_cell(value):
    text = ''.join(str(value).split())
    if re.fullmatch(r'\d+,\d{1,14}', text):
        return str(Decimal(text.replace(',', '.')))
    require(bool(re.fullmatch(r'\d+\.\d{4,14}', text)), 'unambiguous_native_decimal_required')
    return str(Decimal(text))


@dataclass(frozen=True)
class CurrentSecurityBinding:
    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    isin: str
    sources: tuple[dict, ...]
    historical_aliases: tuple[str, ...] = ()
    blanket_unchanged_five_year_identity_gate: bool = False
    point_in_time_archive_verified: bool = False


class Corpus:
    """Read only the real cached original bytes and their CVM index bindings."""
    def __init__(self, folder=ROOT):
        self.adapter = identity_research.PrimarySecurityIdentityAdapter(folder)
        self.reader = self.adapter.reader
        self.root = self.reader.root
        self._documents = {}
        body, _ = self.reader.read(identity_research.OLD_REPORT, identity_research.OLD_REPORT_SHA,
                                   role='pinned_existing_document_manifest')
        self.older = json.loads(body)['results']

    def current_binding(self, ticker):
        observed = self.adapter.evidence(ticker)
        # require_window describes a different, stronger claim and is not the
        # ordinary-stock core's identity gate. Retain its caveats as audit data.
        return CurrentSecurityBinding(ticker, observed.cvm, observed.cnpj, observed.share_class,
            observed.isin, tuple(asdict(s) for s in observed.sources))

    def document(self, sequence):
        if sequence in self._documents:
            return deepcopy(self._documents[sequence])
        matches = [r for r in self.adapter.index if r['sequence'] == sequence]
        require(len(matches) == 1, 'unique_primary_index_record_required')
        indexed = matches[0]
        record = indexed['record']
        require(record['Data_Entrega'] <= CUTOFF.isoformat(), 'document_received_after_cutoff')
        url = record['Link_Download']
        receipt_name = f'{BASE}/response-{hashlib.sha256(url.encode()).hexdigest()}.json'
        if (self.root / receipt_name).is_file():
            content, _ = self.reader.read(receipt_name, role='retained_public_HTTP_receipt')
            receipt = json.loads(content)
            require(receipt['url'] == url and receipt['scope'] == 'round2-identity'
                    and receipt['status'] == 200 and receipt['error'] is None
                    and receipt['retries'] == 0 and receipt['file'] == f'{BASE}/body-{receipt["sha256"]}.bin',
                    'native_HTTP_receipt_disagrees')
            body, source = self.reader.source(receipt, 'native_CVM_document')
        else:
            matches = [r for r in self.older if r.get('url') == url and r.get('file')]
            require(len(matches) == 1, 'native_document_not_cached')
            receipt = matches[0]
            require(receipt['version'] == indexed['version']
                    and receipt['published_at'] == record['Data_Entrega'], 'existing_receipt_disagrees')
            body, source = self.reader.read(receipt['file'], receipt['sha256'], url=url,
                retrieved_at=receipt.get('retrieved_at'), size=receipt['bytes'], role='native_CVM_document')
        result = {'sequence': sequence, 'index': indexed, 'source': asdict(source),
                  'format': 'PDF' if body.startswith(b'%PDF-') else 'HTML_not_native_document',
                  'pages': [], 'tables': []}
        if result['format'] == 'PDF':
            import pdfplumber
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(body))
            require(not pdf.is_encrypted and 1 <= len(pdf.pages) <= 20, 'native_PDF_bounds')
            result['pages'] = [p.extract_text() or '' for p in pdf.pages]
            with pdfplumber.open(io.BytesIO(body)) as table_pdf:
                for number, page in enumerate(table_pdf.pages, 1):
                    for index, table in enumerate(page.extract_tables()):
                        result['tables'].append({'page': number, 'table_index': index, 'cells': table})
        self._documents[sequence] = result
        return deepcopy(result)


def identity_policy_review(folder=ROOT):
    """Record the real function and exercise it with unmodified existing caches."""
    from . import inventory_candidates as core
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    text, first = inspect.getsourcelines(core._identity)
    source = ''.join(text)
    inventory = load_inventory(root)
    examples = []
    for ticker in ('WEGE3', 'ITUB4'):
        row = next(r for r in inventory['stocks'] if r['ticker'] == ticker)
        class ExistingReader:
            folder = root
            exact_share_class_bindings = {}
            def __init__(self):
                self.files = []
            def json(self, name, role):
                content = (root / name).read_bytes()
                self.files.append({'file': name, 'sha256': hashlib.sha256(content).hexdigest(),
                                   'role': role, 'bytes': len(content)})
                return json.loads(content)
        reader = ExistingReader()
        detail, codes = core._identity(row, reader, CUTOFF)
        examples.append({'ticker': ticker, 'catalogue_model_ready': row.get('model_ready'),
            'identity_pass': detail is not None, 'isin': codes.get(ticker),
            'files_read': reader.files, 'historical_FCA_tables_read': False})
    return {'ordinary_share_unchanged_five_year_identity_requirement': False,
        'previous_require_window_is_stronger_optional_documentary_claim_not_core_gate': True,
        'core_function': {'file': str(Path(inspect.getfile(core._identity)).resolve()),
                          'line': first, 'literal_source': source,
                          'sha256': hashlib.sha256(Path(inspect.getfile(core._identity)).read_bytes()).hexdigest()},
        'actual_existing_cache_examples': examples,
        'still_required': ['exact_current_CVM_class_ISIN_binding', 'historical_source_issuer_class_binding',
            '61_month_price_history', 'independent_price_anchors_and_share_regime',
            'dated_payment_calendar_and_cash_reconciliation', 'annual_financial_features'],
        'no_core_or_pointer_mutation': True}


def issuer_b3_rows(corpus):
    body, source = corpus.reader.read(B3_HTML, B3_HTML_SHA, role='cached_issuer_cash_calendar')
    content, _ = corpus.reader.read(B3_RECEIPT, role='cached_issuer_HTTP_receipt')
    receipt = json.loads(content)
    require(receipt['sha256'] == B3_HTML_SHA and receipt['bytes'] == len(body)
            and receipt['file'] == B3_HTML and receipt['url'] == 'https://ri.b3.com.br/pt-br/proventos/',
            'cached_issuer_source_receipt_disagrees')
    source = {**asdict(source), 'url': receipt['url'], 'retrieved_at': receipt['retrieved_at'],
              'access': 'existing_local_bytes_no_B3_network'}
    tree = html.fromstring(body.decode('utf-8'))
    output = []
    for number, table in enumerate(tree.xpath('//table')):
        previous = table.xpath('preceding::div[contains(@class,"accordion__item__header")][1]')
        if not previous or norm(previous[0].text_content()) != 'historico de proventos b3':
            continue
        rows = [[' '.join(c.text_content().split()) for c in r.xpath('./th|./td')]
                for r in table.xpath('.//tr')]
        if not rows or len(rows[0]) != 7 or norm(rows[0][1]) != 'provento':
            continue
        for line, cells in enumerate(rows[1:], 1):
            if len(cells) != 7 or norm(cells[1]) not in ('jcp', 'dividendos'):
                continue
            approval = re.search(r'\d{2}/\d{2}/\d{4}', cells[2])
            if not approval or exact_date(approval[0]) < '2021-01-01':
                continue
            output.append({'label': 'JCP' if norm(cells[1]) == 'jcp' else 'DIVIDENDO',
                'approval_date': exact_date(approval[0]), 'payment_date': exact_date(cells[6]),
                'ex_date': exact_date(cells[5]), 'displayed_gross_decimal': decimal_cell(cells[4]),
                'record_date': None, 'source': source, 'literal_cells': cells,
                'locator': f'html_table_index={number}:row_index={line}'})
    require(len(output) >= 30, 'cached_calendar_window_rows_missing')
    return output, source


def form_reading(document):
    from .cvm_proventos_documents import metadata_value
    require(document['format'] == 'PDF', 'native_form_unavailable')
    tables = [t['cells'] for t in document['tables']]
    indexed = document['index']['record']
    require(norm(metadata_value(tables, 'denominacao social')) == norm(indexed['Nome_Companhia']),
            'native_form_issuer_not_confirmed')
    version = str(metadata_value(tables, 'versao')).strip()
    require(version == indexed['Versao'], 'native_form_version_disagrees')
    def field_date(needle):
        value = str(metadata_value(tables, needle)).strip()
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}.*[AP]M', value):
            return datetime.strptime(value, '%m/%d/%Y %I:%M:%S %p').date().isoformat()
        return exact_date(value)
    sent = field_date('data envio')
    require(sent <= indexed['Data_Entrega'], 'native_form_sent_after_received')
    record, approval = field_date('ultimo dia de negociacao com direitos'), field_date('data aprovacao')
    protocol = str(metadata_value(tables, 'protocolo provento')).strip()
    output, installments = [], []
    for table in document['tables']:
        cells = table['cells']
        for header, row in enumerate(cells):
            if len(row) == 7 and 'codigo isin' in norm(row[0]) and 'valor bruto' in norm(row[1]):
                for line, native in enumerate(cells[header + 1:], header + 1):
                    isin = ''.join(str(native[0]).split())
                    require(re.fullmatch(r'BR[A-Z0-9]{9}\d', isin) is not None, 'native_ISIN_missing')
                    output.append({'isin': isin, 'gross_decimal': decimal_cell(native[1]),
                        'reference_period': ''.join(norm(native[2]).split()), 'fiscal_year': native[3],
                        'payment_form': norm(native[5]), 'payment_date': exact_date(native[6]) if native[6] else None,
                        'literal_cells': native, 'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': line}})
        if cells and len(cells[0]) == 2 and 'dia de pagamento das parcelas' in norm(cells[0][0]):
            for line, native in enumerate(cells[1:], 1):
                installments.append({'payment_date': exact_date(native[0]), 'gross_decimal': decimal_cell(native[1]),
                    'literal_cells': native, 'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': line}})
    require(bool(output), 'native_form_rows_missing')
    return {'sequence': document['sequence'], 'approval_date': approval, 'record_date': record,
        'received_on': indexed['Data_Entrega'], 'sent_on': sent, 'version': int(version),
        'protocol_provento': protocol, 'rows': output, 'installments': installments,
        'source': document['source'], 'index': document['index']}


def b3_notice_reading(document):
    require(document['format'] == 'PDF', 'notice_not_native_PDF')
    text = norm(document['pages'][0])
    require('09.346.601/0001-25' in ''.join(text.split()), 'notice_issuer_CNPJ_missing')
    text = (text.replace('ajus tados', 'ajustados').replace('ajusta dos', 'ajustados').replace('ajust ados', 'ajustados')
            .replace('re alizado', 'realizado').replace('h oje', 'hoje'))
    signed = re.search(r'sao paulo,\s*(' + PT_DATE + ')', text)
    approval = re.search(r'conselho de administracao em\s*(' + PT_DATE + ')', text)
    payment = re.search(r'o pagamento.{0,110}?sera realizado em\s*(' + PT_DATE + ')', text)
    require(signed and approval and payment, 'notice_literal_date_roles_unresolved')
    record = re.search(r'posicao acionaria de\s*(hoje|' + PT_DATE + ')', text)
    require(record is not None, 'notice_record_date_missing')
    # "Hoje" refers to the document's signed day, including resubmissions whose
    # CVM received/reference day differs. It never means ex-date minus one day.
    record_day = exact_date(signed[1] if record[1] == 'hoje' else record[1])
    values = []
    for m in re.finditer(r'ajustados de\s*r\$\s*(\d+,\d+)\s*para\s*r\$\s*(\d+,\d+)', text):
        preceding = text[:m.start()]
        label = 'JCP' if preceding.rfind('juros sobre') > preceding.rfind('dividendos') else 'DIVIDENDO'
        values.append({'label': label, 'gross_decimal': decimal_cell(m[2]),
            'original_gross_decimal': decimal_cell(m[1]),
            'literal': m[0], 'locator': f'pdf_page=1:normalized_offset={m.start()}'})
    require(bool(values), 'notice_revised_gross_values_missing')
    return {'approval_date': exact_date(approval[1]), 'record_date': record_day,
        'payment_date': exact_date(payment[1]), 'signed_on': exact_date(signed[1]),
        'received_on': document['index']['record']['Data_Entrega'], 'values': values,
        'source': document['source'], 'index': document['index'], 'sequence': document['sequence']}


def event(binding, record, payment, amount, label, approval, received, evidence):
    require(date.fromisoformat(record) <= date.fromisoformat(payment), 'payment_precedes_record_date')
    require(label in ('DIVIDENDO', 'JCP') and Decimal(amount) > 0, 'invalid_cash_kind_or_amount')
    return {'ticker': binding.ticker, 'cvm': binding.cvm, 'cnpj': binding.cnpj,
        'share_class': binding.share_class, 'isin': binding.isin,
        'record_date': record, 'payment_date': payment, 'gross_per_share_decimal': amount,
        'label': label, 'approval_date': approval, 'published_at': received,
        'status': 'past_scheduled' if payment <= CUTOFF.isoformat() else 'announced',
        'record_date_role': 'literal_entitlement_record', 'payment_date_role': 'literal_payment_schedule',
        'historical_ISIN_asserted_by_each_notice': False,
        'historical_instrument_binding': 'literal_same_CNPJ_and_ordinary_class_not_historical_ticker_alias',
        'settlement_verified': False, 'evidence': evidence}


def recover_b3_calendar(corpus):
    binding = corpus.current_binding('B3SA3')
    issuer_rows, source = issuer_b3_rows(corpus)
    forms, unavailable = [], []
    for indexed in corpus.adapter.index:
        row = indexed['record']
        if row['Codigo_CVM'] != '21610' or norm(row['Categoria']) != 'relatorio proventos':
            continue
        try:
            document = corpus.document(indexed['sequence'])
            forms.append(form_reading(document))
        except (ValueError, OSError) as error:
            unavailable.append({'sequence': indexed['sequence'], 'reason': str(error)})
    grouped = defaultdict(list)
    for form in forms:
        grouped[form['approval_date']].append(form)
    selected = {key: max(values, key=lambda f: (f['received_on'], f['version'], int(f['sequence'])))
                for key, values in grouped.items()}
    require({f['sequence'] for f in selected.values()} == B3_FINAL_FORMS,
            'latest_reviewed_native_form_missing_no_older_version_fallback')
    events, unresolved, conflicts = [], [], []
    used_issuer_rows = set()
    for approval, form in sorted(selected.items()):
        require(all(r['isin'] == binding.isin for r in form['rows']), 'B3_native_ISIN_conflict')
        for row in form['rows']:
            native = Decimal(row['gross_decimal'])
            installments = form['installments'] if 'parcelado' in row['payment_form'] else []
            if installments:
                require(sum('parcelado' in r['payment_form'] for r in form['rows']) == 1
                        and sum(Decimal(p['gross_decimal']) for p in installments) == native,
                        'unambiguous_exact_native_installments_required')
                allocations = installments
            else:
                require(row['payment_date'] is not None, 'native_payment_date_missing')
                allocations = [{'payment_date': row['payment_date'], 'gross_decimal': row['gross_decimal']}]
            for part in allocations:
                versions = [Decimal(r['gross_decimal']) for older in grouped[approval] for r in older['rows']
                            if r['isin'] == row['isin'] and r['reference_period'] == row['reference_period']]
                amounts = [Decimal(part['gross_decimal'])] if installments else versions
                matches = [(i, r) for i, r in enumerate(issuer_rows) if r['approval_date'] == approval
                    and any(abs(Decimal(r['displayed_gross_decimal']) - value)
                            <= Decimal(1).scaleb(Decimal(r['displayed_gross_decimal']).as_tuple().exponent)
                            for value in amounts)]
                dated = [(i, r) for i, r in matches if r['payment_date'] == part['payment_date']]
                if dated:
                    matches = dated
                require(len(matches) == 1, 'unique_native_form_issuer_cash_match_required')
                i, issuer = matches[0]
                used_issuer_rows.add(i)
                if issuer['payment_date'] != part['payment_date']:
                    conflicts.append({'kind': 'issuer_table_payment_date_disagrees_with_native_form',
                        'original_issuer_row': issuer, 'native_installment': part,
                        'selected_source': form['source'], 'priority_reason': 'exact_latest_CVM_form_installment_schedule'})
                display_unit = Decimal(1).scaleb(Decimal(issuer['displayed_gross_decimal']).as_tuple().exponent)
                if abs(Decimal(issuer['displayed_gross_decimal']) - Decimal(part['gross_decimal'])) > display_unit:
                    conflicts.append({'kind': 'issuer_amount_superseded_by_later_native_form',
                        'original_issuer_row': issuer, 'latest_native_row': row,
                        'all_same_component_native_versions': [older for older in grouped[approval]],
                        'priority_reason': 'latest_received_exact_ISIN_component_revision_not_fitted_amount'})
                events.append(event(binding, form['record_date'], part['payment_date'], part['gross_decimal'],
                    issuer['label'], approval, form['received_on'],
                    [{'native_form': form, 'native_row': row, 'installment': part}, {'issuer_row': issuer}]))
    # Notices supply the early window before native proventos forms existed.
    for sequence in ('428702', '452234', '454559', '482509', '518309', '541884', '568644', '570315'):
        notice = b3_notice_reading(corpus.document(sequence))
        for value in notice['values']:
            matches = [(i, r) for i, r in enumerate(issuer_rows) if r['approval_date'] == notice['approval_date']
                and r['label'] == value['label']
                and any(abs(Decimal(r['displayed_gross_decimal']) - Decimal(amount))
                        <= Decimal(1).scaleb(Decimal(r['displayed_gross_decimal']).as_tuple().exponent)
                        for amount in (value['gross_decimal'], value['original_gross_decimal']))]
            require(len(matches) == 1, 'early_notice_issuer_row_match_required')
            i, issuer = matches[0]
            used_issuer_rows.add(i)
            if abs(Decimal(issuer['displayed_gross_decimal']) - Decimal(value['gross_decimal'])) > \
                    Decimal(1).scaleb(Decimal(issuer['displayed_gross_decimal']).as_tuple().exponent):
                conflicts.append({'kind': 'early_issuer_amount_superseded_by_native_notice',
                    'original_issuer_row': issuer, 'native_gross_revision': value,
                    'source': notice['source'], 'priority_reason': 'literal_revised_gross_not_net_or_fitted'})
            events.append(event(binding, notice['record_date'], notice['payment_date'], value['gross_decimal'],
                value['label'], notice['approval_date'], notice['received_on'],
                [{'native_notice': notice, 'native_gross_value': value}, {'issuer_row': issuer}]))
    for i, issuer in enumerate(issuer_rows):
        if i not in used_issuer_rows and issuer['payment_date'] >= START.isoformat():
            unresolved.append(issuer)
    keys = [(e['record_date'], e['payment_date'], e['label'], e['gross_per_share_decimal']) for e in events]
    require(len(keys) == len(set(keys)), 'duplicate_calendar_event')
    return {'identity': asdict(binding), 'events': sorted(events, key=lambda e: (e['payment_date'], e['label'])),
        'original_issuer_rows': issuer_rows, 'unresolved_issuer_rows': unresolved, 'source_conflicts': conflicts,
        'native_forms': forms, 'selected_native_form_sequences': [r['sequence'] for r in selected.values()],
        'unavailable_native_forms': unavailable, 'cash_schedule_recovered': not unresolved,
        'source_payment_observation_start': min(r['payment_date'] for r in issuer_rows),
        'pre_window_unbound_source_rows_not_used_as_model_cash': [r for i, r in enumerate(issuer_rows)
            if i not in used_issuer_rows and r['payment_date'] < START.isoformat()],
        'settlement_verified': False, 'sources': [source],
        'ordinary_share_unchanged_five_year_identity_gate_imposed': False}


def recover_eqma_calendar(corpus):
    binding = corpus.current_binding('EQMA3B')
    events = []
    notices = ('427732', '448339', '515484', '697807', '780822', '962899')
    for sequence in notices:
        document = corpus.document(sequence)
        require(document['format'] == 'PDF', 'EQMA_payment_notice_not_native')
        text = norm(document['pages'][0])
        require(binding.cnpj in ''.join(text.split()), 'EQMA_historical_notice_CNPJ_disagrees')
        payment = re.search(r'(?:sera efetuado em|serao pagos em)\s*(' + PT_DATE + ')', text)
        require(payment is not None, 'EQMA_literal_payment_date_missing')
        ordinary = re.search(r'\bordinarias\s+(\d{2}/\d{2}/\d{4})\s+(?:r\$\s*)?(\d+,\d+)', text)
        visual = None
        if sequence == '697807':
            # The native PDF contains this table as an image. These are reviewed
            # printed cells, not a total/quantity division or an OCR guess.
            require(document['source']['sha256'] == '489d59cfcd47fb8c50f89e19ab152594dbf2f5eafeffc931d833fb216b190b44'
                    and 'em 28 de abril de 2023' in text, 'EQMA_reviewed_image_table_changed')
            record, amount = '2023-04-28', '2.792571337'
            visual = {'method': 'visually_reviewed_native_embedded_table', 'pdf_page': 1,
                'literal_row': ['Ordinarias', '28/04/2023', '2,792571337'],
                'rendered_page': BASE + '/eqma-2023-payment-notice-page1.png',
                'amount_computed_from_total': False}
        else:
            require(ordinary is not None, 'EQMA_literal_ordinary_class_amount_missing')
            record, amount = exact_date(ordinary[1]), decimal_cell(ordinary[2])
        proof = {'native_notice': document, 'ordinary_table_literal': ordinary[0] if ordinary else None,
                 'visual_reading': visual, 'payment_literal': payment[0]}
        if sequence == '697807':
            initial = form_reading(corpus.document('618796'))
            require(initial['record_date'] == record and binding.isin in {r['isin'] for r in initial['rows']},
                    'EQMA_2023_native_historical_ISIN_or_record_disagrees')
            proof['earlier_native_form'] = initial
            proof['initial_form_two_decimal_amount'] = '2.79000000000'
            proof['later_notice_full_precision_amount'] = amount
            proof['initial_form_payment_date'] = '2023-12-31'
            proof['later_dated_notice_payment_authority'] = exact_date(payment[1])
            proof['no_earlier_form_deadline_used_as_actual_payment'] = True
        events.append(event(binding, record, exact_date(payment[1]), amount, 'DIVIDENDO', None,
                            document['index']['record']['Data_Entrega'], [proof]))
    notice = corpus.document('1034546')
    text = norm(' '.join(notice['pages']))
    amount = re.search(r'r\$\s*(\d+,\d+)\s+por acao on\b', text)
    form = form_reading(corpus.document('1060668'))
    require(amount is not None and binding.cnpj in ''.join(text.split()), 'EQMA_2026_class_amount_missing')
    candidates = [r for r in form['rows'] if Decimal(r['gross_decimal']) == Decimal(decimal_cell(amount[1]))]
    require(len(candidates) == 1 and candidates[0]['isin'] == binding.isin
            and form['record_date'] == form['approval_date'] == '2026-04-28', 'EQMA_native_ON_ISIN_join_disagrees')
    selected = candidates[0]
    events.append(event(binding, form['record_date'], selected['payment_date'], selected['gross_decimal'],
        'DIVIDENDO', form['approval_date'], form['received_on'],
        [{'class_notice': notice, 'native_form': form, 'native_row': selected,
          'ordinary_amount_literal': amount[0], 'preliminary_deadline_not_used_as_payment_date': True}]))
    return {'identity': asdict(binding), 'events': sorted(events, key=lambda e: e['payment_date']),
        'cash_schedule_recovered': True, 'unresolved_in_window_notice_rows': [],
        'source_payment_observation_start': min(e['payment_date'] for e in events),
        'independent_cash_source_count': 1,
        'cash_arithmetic_replay_is_not_an_independent_second_cash_source': True,
        'ordinary_class_is_literal_in_historical_notices': True,
        'historical_ticker_alias_emitted': False,
        'past_scheduled_is_not_settlement': True,
        'sources': [e['evidence'][0].get('native_notice', {}).get('source') for e in events
                    if e['evidence'][0].get('native_notice')],
        'outside_window_conflict_not_used': {'sequence': '388749', 'payment_date': '2021-05-14',
            'issue': 'notice_paragraph_says_2020_record_but_table_says_2021_record',
            'converted_to_zero_or_resolved_by_guess': False}}


def expected_cash(calendar):
    expected = defaultdict(Decimal)
    for row in calendar['events']:
        expected[date.fromisoformat(row['record_date'])] += Decimal(row['gross_per_share_decimal'])
    return {key: float(value) for key, value in expected.items()}


def receipts_in(value):
    if isinstance(value, dict):
        if all(key in value for key in ('file', 'sha256', 'retrieved_at')):
            yield value
        for nested in value.values():
            yield from receipts_in(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from receipts_in(nested)


def payment_history(calendar, *, coverage_start=START):
    from .collect_market_data import DividendHistory
    require(calendar['cash_schedule_recovered'], 'incomplete_calendar_cannot_supply_history')
    history = DividendHistory(CUTOFF, len(calendar['events']), basis='payment')
    history.installments, history.semantic_events = [], []
    for number, row in enumerate(calendar['events']):
        if row['payment_date'] < coverage_start.isoformat() and row['record_date'] < coverage_start.isoformat():
            continue
        semantic = {**row, 'source_row': number, 'available_as_of': row['published_at'] <= CUTOFF.isoformat()}
        history.semantic_events.append(semantic)
        history.installments.append({'ticker': row['ticker'], 'record_date': row['record_date'],
            'payment_date': row['payment_date'], 'gross_per_unit': float(Decimal(row['gross_per_share_decimal'])),
            'label': row['label'], 'source_row': number, 'reported_status': row['status'],
            'published_at': row['published_at'], 'status': 'pending_reconciliation',
            'primary_declaration_id': None, 'primary_evidence': row['evidence']})
    history.coverage_start = date.fromisoformat(calendar['source_payment_observation_start'])
    timestamps = [r['retrieved_at'] for r in receipts_in(calendar['events']) if r['retrieved_at'] is not None]
    require(bool(timestamps), 'actual_calendar_retrieval_receipt_missing')
    history.source_observed_at = max(timestamps, key=datetime.fromisoformat)
    history.source_issues = []
    history.primary_declaration_matches = 0
    require(history.coverage_start <= START and history.installments, 'calendar_does_not_span_window_start')
    return history


def cash_replay(calendar):
    from .share_basis import reconcile_payments
    from .inventory_candidates import _qualify_history
    ticker = calendar['identity']['ticker']
    original = payment_history(calendar)
    paid, ledger, adjustments = reconcile_payments(ticker, original, expected_cash(calendar), [], CUTOFF,
        evidence_provider='native_documentary_calendar')
    _qualify_history(paid, ledger, original, CUTOFF, START)
    return {'actual_functions': ['reconcile_payments', '_qualify_history'], 'passes_arithmetic_and_date_gates': True,
        'ledger': ledger, 'ledger_rows': len(ledger), 'future_installments': paid.future_events,
        'declaration_groups': adjustments, 'paid_observations': [[a.isoformat(), b.isoformat(), c] for a, b, c in paid],
        'independent_ultimate_cash_authors_verified': False,
        'two_publication_channels_examined': ticker == 'B3SA3',
        'B3_cash_channels_are_issuer_RI_and_CVM_not_independent_ultimate_authors': True,
        'EQMA_single_primary_channel_not_promoted_to_independent_corroboration': ticker == 'EQMA3B',
        'source_pass_or_model_admission_granted': False}


def actual_source_replay(corpus, calendars):
    from . import inventory_candidates as core
    from . import issuer_independent_evidence as independent
    from .market_inventory import load_inventory
    root = corpus.root
    inventory = load_inventory(root)
    paths = [root / 'current.json', root / 'inventory/current.json']
    before = {str(p): p.read_bytes() for p in paths if p.is_file()}
    old_identity, old_payments, old_evidence = core._identity, core._payments, independent.load_evidence
    def identity_hook(row, cache, cutoff):
        if row['ticker'] not in TARGETS:
            return old_identity(row, cache, cutoff)
        binding = corpus.current_binding(row['ticker'])
        require((row['cvm'], row['share_class'], cutoff) == (binding.cvm, binding.share_class, CUTOFF)
                and row.get('isin') in (None, binding.isin), 'caller_current_identity_conflict')
        return binding, {binding.ticker: binding.isin}
    def payment_hook(ticker, cache, cutoff, primary=None, *, coverage_start=None, identity=None):
        if ticker not in TARGETS:
            return old_payments(ticker, cache, cutoff, primary, coverage_start=coverage_start, identity=identity)
        return payment_history(calendars[ticker], coverage_start=coverage_start or START), None
    def evidence_hook(folder, identity, cutoff):
        ticker = identity['ticker']
        if ticker not in TARGETS:
            return old_evidence(folder, identity, cutoff)
        return {'provider': 'primary_CVM_notices_and_existing_issuer_cash_cache',
            'expected_cash': expected_cash(calendars[ticker]), 'raw_quotes': {}, 'sources': [],
            'checks': {'cash_calendar_diagnostic_only': True, 'independent_raw_quote_evidence_missing': True}}
    output = []
    with patch.object(core, '_identity', identity_hook), patch.object(core, '_payments', payment_hook), \
            patch.object(independent, 'load_evidence', evidence_hook):
        for ticker in TARGETS:
            row = next(r for r in inventory['stocks'] if r['ticker'] == ticker)
            checks = {}
            try:
                candidate = core._one(row, core._Cache(root, inventory), CUTOFF, checks)
                output.append({'ticker': ticker, 'source_pass': True, 'checks': checks, 'ledger_rows': len(candidate['ledger'])})
            except ValueError as error:
                output.append({'ticker': ticker, 'source_pass': False, 'reason': str(error), 'checks': checks})
    require(core._identity is old_identity and core._payments is old_payments
            and independent.load_evidence is old_evidence, 'isolated_hooks_not_restored')
    return {'results': output, 'actual_core_function': 'inventory_candidates._one',
        'typed_identity_return_not_B3_JSON': True, 'process_local_hooks_restored': True,
        'pointers_unchanged': all(Path(p).read_bytes() == body for p, body in before.items()),
        'core_files_edited': False, 'prices_or_independent_anchors_fabricated': False,
        'calendar_hooks_are_reviewable_parent_integration_proposal': True}


def replay_prices(corpus, ticker):
    from .broad_stocks import split_price_history
    from .share_basis import reconcile_prices
    from .pending_all_prices import monthly_audit
    relative = f'raw/inventory/daily-prices/{ticker}.json'
    summary, _ = corpus.reader.read('raw/inventory/daily-summary.json', role='existing_daily_price_receipts')
    receipt = json.loads(summary)['instruments'][ticker]['source']
    require(receipt['file'] == relative, 'daily_price_receipt_path_disagrees')
    body, source = corpus.reader.read(relative, receipt['sha256'], url=receipt['url'],
        retrieved_at=receipt['retrieved_at'], role='unchanged_original_Yahoo_daily_file')
    prices, actions = split_price_history(body, ticker, CUTOFF, allow_recent_actions=True)
    try:
        reconciled, dates, mode = reconcile_prices(body, ticker, actions, {}, CUTOFF,
            evidence_provider='no_independent_price_source_available')
        result = {'passes': True, 'mode': mode, 'independent_dates': dates, 'months': len(reconciled)}
    except ValueError as error:
        result = {'passes': False, 'reason': str(error)}
    return {'source': asdict(source), 'literal_Yahoo_61_month_audit': monthly_audit(body, ticker),
        'existing_split_price_history_passes': True, 'parsed_months': len(prices),
        'observed_provider_actions': actions, 'actual_reconcile_prices_result': result,
        'independent_anchors_available': 0, 'comparison_prices_created': False,
        'prices_fitted_or_vendor_days_replaced': False}


def price_discovery(corpus):
    page_name = 'raw/primary-research/all-pending/prices/body-55509fee50c048e063b756c58f035c7b5f0172104acef56e0ed31b0efd2c40d8.bin'
    page, page_source = corpus.reader.read(page_name,
        '55509fee50c048e063b756c58f035c7b5f0172104acef56e0ed31b0efd2c40d8', role='existing_issuer_quote_configuration')
    bundle_name = 'raw/primary-research/all-pending/prices/body-19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0.bin'
    script, script_source = corpus.reader.read(bundle_name,
        '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0', role='issuer_published_quote_template')
    formula = 'let e=t.API_URL+"/"+t.STOCK_ID+"/live"'
    require(formula in script.decode() and b"c03e950e-5c92-11ea-b42f-0aed8756bb2a" in page,
            'published_live_discovery_template_disagrees')
    url = 'https://api.mziq.com/mzstockinfo/c03e950e-5c92-11ea-b42f-0aed8756bb2a/live'
    relative = f'{BASE}/response-{hashlib.sha256(url.encode()).hexdigest()}.json'
    receipt_body, _ = corpus.reader.read(relative, role='actual_published_live_discovery_receipt')
    receipt = json.loads(receipt_body)
    require(receipt['url'] == url and receipt['status'] == 200 and receipt['error'] is None,
            'live_configuration_discovery_not_successful')
    live, live_source = corpus.reader.source(receipt, 'actual_live_ticker_configuration')
    configured = [row['ticker'] for row in json.loads(live)['data']['tickers']]
    b3_page, b3_source = corpus.reader.read(B3_HTML, B3_HTML_SHA, role='cached_B3_quote_configuration_only')
    tree = html.fromstring(b3_page.decode())
    blocked_scripts = [url for url in tree.xpath('//script/@src') if 'stockinfoMZ.js' in url]
    return {'EQMA3B': {'FCA_published_issuer_site': 'www.ri.equatorialenergia.com.br',
            'actual_configured_tickers': configured, 'exact_EQMA3B_configured': 'EQMA3B' in configured,
            'page_source': asdict(page_source), 'script_source': asdict(script_source),
            'actual_live_response_source': asdict(live_source),
            'EQTL3_history_downloads_duplicated': 0, 'EQMA3B_history_URL_guessed': False},
        'B3SA3': {'cached_page_source': asdict(b3_source),
            'literal_published_stockinfo_id': '42c54fda-5979-11ea-a1aa-0aed8756bb2a',
            'publisher_script_urls_denied_by_user': blocked_scripts,
            'denied_scripts_fetched': False, 'alternate_CDN_or_endpoint_guessed': False,
            'verified_native_daily_request_available': False},
        'remaining_price_requirement': 'at_least_four_exact_dated_independent_raw_close_anchors_or_verified_native_history'}


def eqma_declaration_crosschecks(corpus, calendar):
    """Separate declaration clauses from the notices that fix payment dates."""
    result = []
    for sequence, page_number, amount, record in (
        ('497179', 3, '2.060000000000', '2021-08-24'),
        ('443875', 2, '1.25', '2021-11-25'),
        ('497179', 4, '2.030000000000', '2022-04-28')):
        document = corpus.document(sequence)
        page = norm(document['pages'][page_number - 1])
        if sequence == '443875':
            match = re.search(r'r\$\s*(1,25).{0,80}?por acao ordinaria', page)
        else:
            amount_literal = amount.replace('.', ',')
            match = re.search(r'dividendo total por acao\s+(' + re.escape(amount_literal) + ')', page)
        require(match is not None, 'EQMA_separate_declaration_amount_missing')
        cash = next(e for e in calendar['events'] if e['record_date'] == record)
        require(Decimal(cash['gross_per_share_decimal']) == Decimal(amount), 'EQMA_declaration_cash_notice_disagrees')
        result.append({'source': document['source'], 'index': document['index'], 'pdf_page': page_number,
            'literal_amount_clause': match[0], 'record_date_from_payment_notice': record,
            'gross_per_share_decimal': amount, 'matches_payment_notice': True,
            'declaration_deadline_not_used_as_payment_date': True})
    for sequence, record in (('781130', '2024-04-29'), ('962948', '2025-04-29'), ('1060668', '2026-04-28')):
        form = form_reading(corpus.document(sequence))
        cash = next(e for e in calendar['events'] if e['record_date'] == record)
        rows = [r for r in form['rows'] if r['isin'] == calendar['identity']['isin']]
        require(len(rows) == 1 and Decimal(rows[0]['gross_decimal']) == Decimal(cash['gross_per_share_decimal'])
                and form['record_date'] == record, 'EQMA_form_notice_amount_disagrees')
        result.append({'native_form': form, 'native_row': rows[0], 'record_date': record,
                       'matches_literal_ordinary_class_notice_amount': True})
    assembly = corpus.document('618546')
    paragraph = norm(assembly['pages'][2])
    match = re.search(r'montante total de r\$\s*458\.495\.250,38.{0,240}?acionistas da companhia nesta data', paragraph)
    require(match is not None, 'EQMA_2023_assembly_record_clause_missing')
    result.append({'source': assembly['source'], 'index': assembly['index'], 'pdf_page': 3,
        'literal_record_and_total_clause': match[0], 'record_date': '2023-04-28',
        'notice_total_literal': '458.495.250,39', 'assembly_total_literal': '458.495.250,38',
        'one_cent_issuer_document_total_disagreement_retained': True,
        'per_share_amount_computed_from_totals': False,
        'per_share_amount_authority_remains_visual_native_notice_table': True})
    initial = form_reading(corpus.document('618796'))
    result[-1]['earlier_native_ISIN_form'] = initial
    result[-1]['initial_form_precision_and_date_differ_from_later_payment_notice'] = True
    return result


def financial_diagnostics(corpus, calendars):
    from .market_inventory import load_inventory
    from .inventory_financials import prepare_financials
    from .collect_market_data import financial_features
    inventory = load_inventory(corpus.root)
    stocks = [r for r in inventory['stocks'] if r['ticker'] in TARGETS]
    statements, diagnostics, sources = prepare_financials(corpus.root, stocks, CUTOFF)
    output = []
    for row in stocks:
        calendar = calendars[row['ticker']]
        cash = cash_replay(calendar)
        observations = [(date.fromisoformat(p), date.fromisoformat(a), value)
                        for p, a, value in cash['paid_observations']]
        current = financial_features(statements, observations, CUTOFF,
            (row['cvm'], row['share_class'], row['sector_code']))
        native = [s for s in statements if s['cvm'] == row['cvm'] and s['share_class'] == row['share_class']]
        output.append({'ticker': row['ticker'], 'statement_count': len(native),
            'current_financial_features_on_documented_calendar_only': current,
            'conditional_diagnostic_not_model_admission': True})
    return {'results': output, 'sources': sources, 'diagnostics': diagnostics,
            'price_gate_not_waived': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--write-report', action='store_true')
    args = parser.parse_args()
    def no_network(*args, **kwargs):
        raise RuntimeError('offline_cash_and_price_replay_network_forbidden')
    with patch.object(socket.socket, 'connect', no_network), patch.object(socket.socket, 'connect_ex', no_network):
        corpus = Corpus(args.root)
        calendars = {'B3SA3': recover_b3_calendar(corpus), 'EQMA3B': recover_eqma_calendar(corpus)}
        report = {'policy_review': identity_policy_review(args.root),
            'calendars': calendars,
            'cash_replay': {ticker: cash_replay(calendar) for ticker, calendar in calendars.items()},
            'price_replay': {t: replay_prices(corpus, t) for t in TARGETS},
            'price_discovery': price_discovery(corpus),
            'EQMA_separate_declaration_crosschecks': eqma_declaration_crosschecks(corpus, calendars['EQMA3B']),
            'actual_source_replay': actual_source_replay(corpus, calendars),
            'conditional_financial_diagnostics': financial_diagnostics(corpus, calendars),
            'core_modified': False, 'production_integration': False, 'model_admissions': []}
    if args.write_report:
        body = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
        path = args.root / BASE / ('identity-cash-replay-' + hashlib.sha256(body).hexdigest() + '.json')
        _save(path, body)
        print(path)
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
