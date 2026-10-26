"""Exact old CVM notice research for ENMT/BEES; offline unless explicitly acquired.

Writes are confined to raw/primary-research/older-cash. No model, pointer,
exchange endpoint, proxy, redirect, or automatic retry is used.
"""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
import zipfile

from . import alternative_cash_research as shared
from . import cvm_proventos_documents as cvm


CACHE = 'raw/primary-research/older-cash'
CUTOFF = '2026-09-30'
MAX_REQUESTS = 20
MAX_BYTES = 12_000_000
REVIEW_SHA256 = '0b61ce77fba0886c237d54119d7231c872ca156bde638a2217f134d4df06214f'
TARGETS = {
    'ENMT3': {'cvm': 14605, 'share_class': 'ON', 'isin': 'BRENMTACNOR5'},
    'ENMT4': {'cvm': 14605, 'share_class': 'PN', 'isin': 'BRENMTACNPR2'},
    'BEES3': {'cvm': 1155, 'share_class': 'ON', 'isin': 'BRBEESACNOR9'},
    'BEES4': {'cvm': 1155, 'share_class': 'PN', 'isin': 'BRBEESACNPR6'},
}


def save_body(folder, name, body):
    root = Path(folder).resolve()
    path = shared._safe(root, CACHE + '/' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(root).as_posix()


def save(folder, prefix, payload):
    body = shared._dump(payload)
    return save_body(folder, f'{prefix}-{shared._sha(body)}.json', body)


def indexed_documents(reader):
    """Read ALL categories, including blank-subject revisions, with exact CSV rows."""
    manifest, _ = reader.json('raw/primary-research/cvm-documents/index-sources.json', 'IPE_manifest')
    documents, periods = [], []
    for year in range(2021, 2027):
        choices = [source for source in manifest if source.get('year') == year]
        if len(choices) != 1:
            raise ValueError('exact_six_annual_indices_required')
        source = choices[0]
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        if source['url'] != url:
            raise ValueError('wrong_native_index_origin')
        body, receipt = reader.read(source['file'], 'native_CVM_IPE_ALL_categories', source['sha256'], url)
        member = f'ipe_cia_aberta_{year}.csv'
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if archive.namelist() != [member] or archive.getinfo(member).file_size > 100_000_000:
                raise ValueError('invalid_native_index_archive')
            with archive.open(member) as stream:
                rows = list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
        count = Counter()
        for line, row in enumerate(rows, 2):
            if not row.get('Codigo_CVM', '').isdigit() or int(row['Codigo_CVM']) not in (14605, 1155):
                continue
            published = cvm.day(row['Data_Entrega'])
            if not published or not published.startswith(str(year)):
                raise ValueError('invalid_index_publication')
            if published > CUTOFF:
                continue
            code = int(row['Codigo_CVM'])
            sequence, version = cvm.document_id(row['Link_Download'])
            if int(row['Versao']) != version:
                raise ValueError('index_version_mismatch')
            count[str(code)] += 1
            documents.append({'cvm': code, 'sequence': sequence, 'version': version,
                'url': row['Link_Download'], 'published_at': published,
                'reference_date': cvm.day(row['Data_Referencia']), 'category': row['Categoria'],
                'subject': row['Assunto'], 'type': row['Tipo'],
                'index_evidence': {'file': receipt['file'], 'sha256': receipt['sha256'],
                    'member': member, 'csv_line': line, 'raw_row': row}})
        periods.append({'year': year, 'file': receipt['file'], 'sha256': receipt['sha256'],
            'url': url, 'member': member, 'native_rows_scanned': len(rows),
            'issuer_rows_before_cutoff': dict(count), 'all_categories': True,
            'index_scan_complete': True, 'cash_history_complete': False})
    return documents, periods


def identities(reader):
    pointer, _ = reader.json('inventory/current.json', 'inventory_identity_pointer_read_only')
    inventory, source = reader.json('inventory/' + pointer['snapshot'] + '/market.json', 'inventory_identity')
    if source['sha256'] != pointer['sha256']:
        raise ValueError('inventory_identity_hash_mismatch')
    result = {}
    for ticker, target in TARGETS.items():
        stocks = [s for s in inventory['stocks'] if s['ticker'] == ticker]
        if len(stocks) != 1 or any(stocks[0].get(k) != v for k, v in target.items()):
            raise ValueError('exact_inventory_identity_required')
        detail, registry = reader.json(f"raw/expanded/b3-detail-{target['cvm']}.json", 'cached_registry_no_network')
        codes = [row for row in detail['otherCodes'] if row['code'] == ticker]
        if (int(detail['codeCVM']) != target['cvm'] or len(codes) != 1
                or codes[0]['isin'] != target['isin']):
            raise ValueError('exact_cached_ISIN_mapping_required')
        result[ticker] = {'inventory': source, 'stock': stocks[0], 'registry': registry,
            'registry_security': codes[0], 'cnpj': detail['cnpj'],
            'contemporaneous_identity_archive_verified': False}
    return result


def document_body(reader, document):
    body, source = shared._document_body(reader, document)
    choices = [] if body is None else [(body, source)]
    for path in (reader.folder / CACHE).glob('document-*.json'):
        receipt, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'acquisition_receipt')
        if receipt['url'] == document['url'] and receipt['status'] == 'received':
            if receipt['document'] != document:
                raise ValueError('acquired_document_identity_changed')
            choices.append(reader.read(receipt['file'], 'primary_CVM_original', receipt['sha256'], receipt['url']))
    if len({shared._sha(body) for body, _ in choices}) > 1:
        raise ValueError('same_document_changed_original_bytes')
    return choices[0] if choices else (None, None)


class Fetcher:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())

    def ledger(self, prefix):
        return [json.loads(path.read_bytes()) for path in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def _get(self, url):
        if not shared._approved(url):
            raise ValueError('unapproved_URL_no_request_sent')
        requests, responses = self.ledger('request'), self.ledger('response')
        if any(r.get('http_status') in (401, 403, 429) for r in responses):
            raise ValueError('access_stop_no_request_sent')
        if url in {r['url'] for r in requests}:
            raise ValueError('already_attempted_no_retry')
        if len(requests) >= MAX_REQUESTS:
            raise ValueError('request_budget_exhausted')
        attempt = {'url': url, 'ordinal': len(requests) + 1,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'proxy': False, 'redirects': False, 'retries': 0}
        reservation = shared._safe(self.folder, f'{CACHE}/request-{len(requests) + 1:02d}.json')
        reservation.parent.mkdir(parents=True, exist_ok=True)
        with reservation.open('xb') as stream:
            stream.write(shared._dump(attempt))
        result, body = {**attempt, 'status': 'failed', 'http_status': None}, b''
        try:
            request = urllib.request.Request(url, headers={'User-Agent': shared.USER_AGENT,
                'Accept': 'text/plain' if url == shared.ROBOT_URL else 'application/pdf',
                'Accept-Encoding': 'identity'})
            with self.opener.open(request, timeout=25) as response:
                result.update(http_status=response.status, content_type=response.headers.get('Content-Type'))
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('response_size_limit')
            result['status'] = 'received'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, error=type(error).__name__)
            try:
                body = error.read(MAX_BYTES + 1)[:MAX_BYTES]
            except OSError as read_error:
                result['body_read_error'] = type(read_error).__name__
        except (OSError, ValueError) as error:
            result['error'] = str(error)
            body = body[:MAX_BYTES]
        digest = shared._sha(body)
        result.update(sha256=digest, bytes=len(body), retrieved_at=datetime.now(timezone.utc).isoformat())
        result['file'] = save_body(self.folder, f'body-{digest}.bin', body)
        save(self.folder, 'response', result)
        return body, result

    def acquire(self, document):
        documents, _ = indexed_documents(shared.Reader(self.folder))
        if document not in documents:
            raise ValueError('exact_native_index_document_required')
        if document_body(shared.Reader(self.folder), document)[0] is not None:
            raise ValueError('already_cached_no_new_request')
        robots = next((r for r in self.ledger('response') if r['url'] == shared.ROBOT_URL), None)
        if robots is None:
            body, robots = self._get(shared.ROBOT_URL)
        else:
            body, _ = shared.Reader(self.folder).read(robots['file'], 'robots', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(shared.ROBOT_URL)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(shared.USER_AGENT, document['url']):
                raise ValueError('robots_disallow_no_request_sent')
            if parser.crawl_delay(shared.USER_AGENT) or parser.request_rate(shared.USER_AGENT):
                raise ValueError('robots_rate_requires_manual_spacing')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_denied')
        body, receipt = self._get(document['url'])
        receipt = {**receipt, 'document': document, 'pdf_signature_verified': body.lstrip().startswith(b'%PDF-')}
        save(self.folder, 'document', receipt)
        return receipt


def pdf_pages(body):
    from pypdf import PdfReader
    if len(body) > MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
        raise ValueError('original_response_not_PDF')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 30:
        raise ValueError('unsupported_PDF')
    return [page.extract_text() or '' for page in pdf.pages]


def _text(value):
    folded = unicodedata.normalize('NFKD', value.replace('\u00ba', '').replace('\u00b0', '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(folded.split())


def _dates(value):
    result = set()
    for token in re.findall(r'\b\d{2}[/\.]\d{2}[/\.]\d{2}(?:\d{2})?\b', value):
        token = token.replace('.', '/')
        parsed = datetime.strptime(token, '%d/%m/%Y' if len(token) == 10 else '%d/%m/%y')
        result.add(parsed.date().isoformat())
    months = ('janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro').split()
    for number, month in enumerate(months, 1):
        for day, year in re.findall(r'\b(\d{1,2}) de ' + month + r' de (\d{4})\b', _text(value)):
            result.add(date(int(year), number, int(day)).isoformat())
    return result


def _classes(value):
    value = _text(value)
    on = bool(re.search(r'ordinari|\bon\b', value))
    pn = bool(re.search(r'preferenci|\bpn\b', value))
    return [name for name, found in (('ON', on), ('PN', pn)) if found]


def reviewed_facts(pages, review, document, source):
    """Replay role-specific original spans, rejecting net and date-role substitution."""
    for key in ('cvm', 'sequence', 'version', 'reference_date', 'published_at'):
        if review[key] != document[key]:
            raise ValueError('review_document_identity_mismatch:' + key)
    if review['sha256'] != source['sha256']:
        raise ValueError('review_original_hash_mismatch')
    if review['index_sha256'] != document['index_evidence']['sha256']:
        raise ValueError('review_index_hash_mismatch')
    raw = document['index_evidence']['raw_row']
    if (int(raw['Codigo_CVM']) != document['cvm'] or raw['Link_Download'] != document['url']
            or int(raw['Versao']) != document['version'] or cvm.day(raw['Data_Entrega']) != document['published_at']):
        raise ValueError('review_index_identity_mismatch')
    spans = {}
    for key, span in review.get('spans', {}).items():
        page = span['page']
        quote = span['quote']
        if type(page) is not int or not 1 <= page <= len(pages) or len(quote) < 8:
            raise ValueError('invalid_page_span')
        if _text(quote) not in _text(pages[page - 1]):
            raise ValueError('original_quote_not_found:' + key)
        spans[key] = quote
    if 'identity' in spans:
        expected = re.sub(r'\D', '', raw['CNPJ_Companhia'])
        if expected not in re.sub(r'\D', '', spans['identity']):
            raise ValueError('notice_CNPJ_mismatch')
    elif document['cvm'] == 1155:
        if 'banestes' not in _text(spans.get('issuer', '')):
            raise ValueError('notice_issuer_name_required')
    else:
        raise ValueError('notice_identity_span_required')
    result = []
    for fact in review.get('facts', []):
        refs = fact['evidence']
        selected = {field: spans[name] for field, name in refs.items()}
        label_quote = _text(selected['label'])
        if fact['label'] == 'DIVIDENDO':
            if 'dividend' not in label_quote:
                raise ValueError('dividend_label_not_proved')
        elif fact['label'] == 'JCP':
            if not re.search(r'juros sobre o capital proprio|\bjcp\b', label_quote):
                raise ValueError('JCP_label_not_proved')
        else:
            raise ValueError('cash_label_not_supported_principal_not_income')
        amount_quote = _text(selected['amount'])
        if any(word in amount_quote for word in ('liquido', 'principal', 'restituicao', 'reducao de capital')):
            raise ValueError('net_or_principal_span_not_gross_income')
        tokens = re.findall(r'(?<![\d.,])\d+(?:\.\d{3})*,\d+(?!\d)', amount_quote)
        amounts = {cvm.gross(token) for token in tokens}
        if Decimal(fact['amount']) not in amounts or Decimal(fact['amount']) <= 0:
            raise ValueError('exact_gross_per_share_not_in_span')
        if 'acao' not in amount_quote:
            raise ValueError('per_share_amount_required')
        if fact['label'] == 'JCP' and 'bruto' not in amount_quote:
            raise ValueError('JCP_requires_explicit_gross_no_net_inference')
        if fact['class_scope'] != _classes(selected['class_scope']):
            raise ValueError('exact_notice_class_scope_required')
        for field in ('record_date', 'payment_date', 'approval_date'):
            if _dates(selected[field]) != {fact[field]}:
                raise ValueError('exact_role_date_required:' + field)
        if not re.search(r'posicao|data\s*-\s*base', _text(selected['record_date'])):
            raise ValueError('record_date_role_required')
        if not re.search(r'pag(?:ament|os|o)|efetuad', _text(selected['payment_date'])):
            raise ValueError('payment_date_role_required')
        if fact.get('ex_date'):
            if _dates(selected['ex_date']) != {fact['ex_date']} or 'a partir' not in _text(selected['ex_date']):
                raise ValueError('exact_ex_date_role_required')
        if not fact['approval_date'] <= fact['record_date'] <= fact['payment_date']:
            raise ValueError('cash_chronology_invalid')
        result.append({**fact, 'source': source, 'document': document,
            'source_locator': {'format': 'reviewed_CVM_original_narrative',
                'sequence': document['sequence'], 'version': document['version'],
                'field_spans': {field: review['spans'][name] for field, name in refs.items()}},
            'gross_reconstructed_from_net': False})
    return {'document': document, 'source': source, 'spans': review.get('spans', {}), 'facts': result}


def monthly_rows(body, document, review):
    """Read each separate entitlement/payment column in the 2022 gross ON+PN table."""
    import pdfplumber
    rows = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table_number, table in enumerate(page.extract_tables()):
                months = 'janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro'.split()
                first = next((i for i, row in enumerate(table)
                              if any(cvm.norm(cell) == 'janeiro' for cell in row)), None)
                if first is None:
                    continue
                columns = [i for i, cell in enumerate(table[first]) if str(cell or '').strip()]
                if len(columns) not in (5, 6):
                    continue
                raw_header = [' '.join(str(row[i] or '') for row in table[:first]) for i in columns]
                header = [cvm.norm(cell) for cell in raw_header]
                if ('data base' not in header[1] or 'direito' not in header[1]
                        or 'ex direito' not in header[2] or 'pagamento' not in header[3]
                        or 'valor bruto' not in header[-1] or _classes(raw_header[-1]) != ['ON', 'PN']):
                    continue
                for row_number, raw_row in enumerate(table[first:], first):
                    if any(cvm.norm(cell) == 'total' for cell in raw_row):
                        continue
                    row = [raw_row[i] for i in columns]
                    if cvm.norm(row[0]) not in months:
                        raise ValueError('monthly_calendar_unrecognized_row')
                    record, ex, payment = map(cvm.day, row[1:4])
                    if not record or not ex or not payment or not record < ex <= payment:
                        raise ValueError('monthly_calendar_date_columns_invalid')
                    if not record.startswith('2022-'):
                        raise ValueError('monthly_calendar_wrong_fiscal_year')
                    rows.append({'label': 'JCP', 'class_scope': ['ON', 'PN'],
                        'amount': str(cvm.gross(row[-1])), 'record_date': record,
                        'ex_date': ex, 'payment_date': payment, 'approval_date': document['reference_date'],
                        'source_locator': {'format': 'reviewed_CVM_original_gross_calendar',
                            'sequence': document['sequence'], 'version': document['version'],
                            'page': page_number, 'table': table_number, 'row': row_number,
                            'raw_header': raw_header, 'raw_row': raw_row, 'columns': columns},
                        'payment_date_basis': 'explicit_calendar_date', 'period': row[0] + '/2022'})
    if len(rows) != 12 or len({row['record_date'] for row in rows}) != 12:
        raise ValueError('exact_twelve_month_calendar_required')
    if 'juros sobre o capital proprio' not in _text(review['spans']['label']['quote']):
        raise ValueError('monthly_JCP_label_required')
    return rows


def native_facts(body, document, source, *, locale='pt-BR'):
    """Exact ISIN cells and printed locale; no amounts or dates from comparators."""
    import pdfplumber
    tables = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            tables.extend((page_number, number, table) for number, table in enumerate(page.extract_tables()))
    flat = [table for _, _, table in tables]
    identities = {int(m) for table in flat for row in table for cell in row
                  for m in re.findall(r'\b(\d{6})IPE', str(cell or ''))}
    if identities != {document['cvm']} or cvm.compact(cvm.metadata_value(flat, 'versao')) != str(document['version']):
        raise ValueError('native_identity_or_version_mismatch')
    def native_date(value):
        token = str(value or '').split()[0]
        return datetime.strptime(token, '%m/%d/%Y' if locale == 'en-US' else '%d/%m/%Y').date().isoformat()
    record = native_date(cvm.metadata_value(flat, 'ultimo dia de negociacao com direitos'))
    approved = native_date(cvm.metadata_value(flat, 'data aprovacao'))
    sent = native_date(cvm.metadata_value(flat, 'data envio'))
    if sent > document['published_at']:
        raise ValueError('native_sent_after_publication')
    events = []
    for page, table_number, table in tables:
        if table and 'dia de pagamento das parcelas' in cvm.norm(table[0][0]):
            if any(str(cell or '').strip() for row in table[1:] for cell in row):
                raise ValueError('native_installment_annex_requires_separate_review')
        header = next((i for i, row in enumerate(table) if len(row) == 7
            and 'isin' in cvm.norm(row[0]) and 'valor bruto' in cvm.norm(row[1])
            and 'data pagamento' in cvm.norm(row[6])), None)
        if header is None:
            continue
        for row_number, row in enumerate(table[header + 1:], header + 1):
            if len(row) != 7 or not re.fullmatch(r'BR[A-Z0-9]{9}\d', cvm.compact(row[0])):
                continue
            token = cvm.compact(row[1])
            if not re.fullmatch(r'\d+[.,]\d{4,}', token):
                raise ValueError('native_exact_decimal_required')
            if cvm.norm(row[5]) not in ('a vista', 'v') or cvm.norm(row[4]) not in ('nao', 'n'):
                raise ValueError('native_schedule_or_update_requires_separate_review')
            events.append({'isin': cvm.compact(row[0]), 'amount': str(Decimal(token.replace(',', '.'))),
                'record_date': record, 'payment_date': cvm.day(row[6]), 'approval_date': approved,
                'period': row[2], 'fiscal_year': cvm.compact(row[3]),
                'native_protocol': cvm.compact(cvm.metadata_value(flat, 'protocolo provento')),
                'source': source, 'document': document,
                'source_locator': {'format': 'native_CVM_exact_ISIN_gross', 'page': page,
                    'table': table_number, 'row': row_number, 'raw_row': row}})
    if not events or len({r['isin'] for r in events}) != len(events):
        raise ValueError('one_native_row_per_ISIN_required')
    return events


def audit_revision(prior, current):
    """A reviewed revision stays inside one issuer, reference, category and type."""
    old, new = prior['document'], current['document']
    for field in ('cvm', 'reference_date', 'category', 'type'):
        if old[field] != new[field]:
            raise ValueError('revision_document_family_mismatch:' + field)
    if (old['index_evidence']['raw_row']['CNPJ_Companhia'] != new['index_evidence']['raw_row']['CNPJ_Companhia']
            or old['version'] >= new['version'] or old['published_at'] > new['published_at']):
        raise ValueError('revision_identity_or_chronology_mismatch')
    return {'prior_document': old, 'current_document': new,
        'prior_source': prior['source'], 'current_source': current['source'],
        'prior_spans': prior['spans'], 'current_spans': current['spans'],
        'old_source_conflict_preserved': True, 'revision_chain_verified': True,
        'selection_uses_independent_values': False}


def read_evidence(folder):
    """Rehash every original, including superseded and unusable responses."""
    reader = shared.Reader(folder)
    manifest, review_source = reader.json(CACHE + '/reviewed-notices.json', 'pinned_review_manifest')
    if review_source['sha256'] != REVIEW_SHA256 or manifest['cutoff'] != CUTOFF:
        raise ValueError('review_manifest_hash_or_cutoff_mismatch')
    documents, periods = indexed_documents(reader)
    identity = identities(reader)
    reviews, native, monthly, originals, diagnostics = {}, {}, {}, {}, []
    for sequence, pin in manifest['originals'].items():
        matches = [d for d in documents if d['sequence'] == sequence]
        if len(matches) != 1:
            raise ValueError('one_pinned_index_document_required')
        document = matches[0]
        for key in ('cvm', 'version', 'reference_date', 'published_at'):
            if document[key] != pin[key]:
                raise ValueError('pinned_document_identity_mismatch:' + key)
        if document['index_evidence']['sha256'] != pin['index_sha256']:
            raise ValueError('pinned_index_original_hash_mismatch')
        body, source = document_body(reader, document)
        if body is None or source['sha256'] != pin['sha256']:
            raise ValueError('pinned_document_original_hash_mismatch:' + sequence)
        originals[sequence] = {'document': document, 'source': source, 'review': pin}
        if pin.get('unusable_reason'):
            if body.lstrip().startswith(b'%PDF-'):
                raise ValueError('unusable_original_classification_changed')
            diagnostics.append({'sequence': sequence, 'code': pin['unusable_reason'], 'source': source})
            continue
        pages = pdf_pages(body)
        if sequence in manifest['reviews']:
            review = {**pin, **manifest['reviews'][sequence], 'sequence': sequence}
            reviews[sequence] = reviewed_facts(pages, review, document, source)
            if pin.get('monthly_2022'):
                monthly[sequence] = [{**row, 'source': source, 'document': document}
                                     for row in monthly_rows(body, document, review)]
        if pin.get('native'):
            native[sequence] = native_facts(body, document, source, locale=pin.get('locale', 'pt-BR'))
        if pin.get('exclusion_reason') or pin.get('anomaly'):
            diagnostics.append({'sequence': sequence, 'code': pin.get('exclusion_reason') or pin['anomaly'],
                'source': source, 'no_original_field_repaired': True})
    cash_view = lambda rows: [(r['record_date'], r['ex_date'], r['payment_date'], Decimal(r['amount'])) for r in rows]
    if cash_view(monthly['468095']) != cash_view(monthly['459966']):
        raise ValueError('primary_monthly_calendars_disagree')
    revisions = [{**audit_revision(reviews[chain['prior']], reviews[chain['current']]), **chain}
                 for chain in manifest['revision_chains']]
    # Same reference date does not join two distinct native provento protocols.
    if ({r['native_protocol'] for r in native['565624']} != {'1040604'}
            or {r['native_protocol'] for r in native['613540']} != {'1040658'}
            or any(r['fiscal_year'] != '2023' for r in native['613540'])):
        raise ValueError('separate_Banestes_native_protocols_required')
    return {'reader': reader, 'manifest': manifest, 'reviews': reviews, 'native': native,
        'monthly': monthly, 'originals': originals, 'revisions': revisions,
        'identity': identity, 'periods': periods, 'diagnostics': diagnostics}


def _native_for_stock(rows, ticker, narrative):
    target = TARGETS[ticker]
    matches = [r for r in rows if r['isin'] == target['isin'] and r['document']['cvm'] == target['cvm']]
    if len(matches) != 1:
        raise ValueError('one_exact_class_ISIN_native_row_required')
    row = matches[0]
    if target['share_class'] not in narrative['class_scope']:
        raise ValueError('native_row_not_covered_by_notice_class')
    for field in ('record_date', 'payment_date', 'approval_date'):
        if row[field] != narrative[field]:
            raise ValueError('native_and_notice_role_date_mismatch:' + field)
    if row['payment_date'] is None or Decimal(row['amount']) <= 0:
        raise ValueError('native_exact_payment_and_gross_required')
    return row


def _event(ticker, fact, group_id, *, native=None, narrative_components=None):
    target = TARGETS[ticker]
    if target['share_class'] not in fact['class_scope'] or fact['document']['cvm'] != target['cvm']:
        raise ValueError('exact_event_class_and_CVM_required')
    value = native or fact
    source, document = value['source'], value['document']
    published = max(document['published_at'], fact['document']['published_at'])
    return {'ticker': ticker, **target, 'record_date': value['record_date'],
        'payment_date': value['payment_date'], 'ex_date': fact.get('ex_date'),
        'approval_date': value['approval_date'], 'label': fact['label'],
        'reported_per_share_decimal': value['amount'], 'gross_per_share_decimal': value['amount'],
        'amount_tax_basis': 'explicit_gross_exact_ISIN_unit' if native else
            'explicit_gross_per_share' if fact['label'] == 'JCP' else 'dividend_per_share',
        'label_basis': 'reviewed_primary_notice_or_board_not_native_form_heading',
        'gross_reconstructed_from_net': False, 'share_basis': 'reported_unverified',
        'published_at': published, 'available_as_of': published <= CUTOFF,
        'publication_basis': 'latest_required_primary_IPE_delivery_day_not_archive_timestamp',
        'status': 'past_scheduled', 'source_ref': 'older-cash:' + source['sha256'],
        'primary_declaration_id': group_id + ':' + source['sha256'], 'complete_group_id': group_id,
        'source_locator': {'format': 'CVM_reviewed_old_notice_complete_group',
            'amount_and_dates': value['source_locator'], 'income_nature_and_class': fact['source_locator'],
            'narrative_components': [r['source_locator'] for r in narrative_components or [fact]],
            'native_amount_is_class_total_not_inferred_component_sum': bool(native)},
        'payment_date_basis': fact['payment_date_basis'],
        'payment_claim_basis': 'primary_declared_schedule_not_settled_cash',
        'issues': [], 'cash_model_eligible': False, 'is_settlement_confirmation': False,
        'point_in_time_archive_verified': False}


def complete_groups(evidence, ticker):
    """Emit full target groups. No independent amounts enter source selection."""
    reviews, native, monthly = (evidence[key] for key in ('reviews', 'native', 'monthly'))
    groups = []
    if ticker.startswith('ENMT'):
        march = reviews['480362']['facts'][0]
        groups.append([_event(ticker, march, ticker + ':2022-03-22')])
        if ticker == 'ENMT4':
            august = reviews['658922']['facts'][0]
            form = _native_for_stock(native['658317'], ticker, august)
            if Decimal(form['amount']) != Decimal(august['amount']):
                raise ValueError('August_exact_notice_and_native_amount_disagree')
            groups.append([_event(ticker, august, ticker + ':2023-08-15', native=form)])
        components = [r for r in reviews['686045']['facts'] if TARGETS[ticker]['share_class'] in r['class_scope']]
        common = reviews['686045']['facts'][1]
        form = _native_for_stock(native['686059'], ticker, common)
        if form['native_protocol'] != '1160631' or form['fiscal_year'] != '2023' or _text(form['period']) != '3 trimestre':
            raise ValueError('November_exact_native_declaration_period_required')
        # The native form reports each class total directly. Rounded notice
        # components are retained, never subtracted across classes or grossed up.
        groups.append([_event(ticker, common, ticker + ':2023-11-14', native=form,
                              narrative_components=components)])
    elif ticker.startswith('BEES'):
        intermediate = reviews['517975']['facts'][0]
        june = [r for r in monthly['468095'] if r['record_date'] == '2022-06-30']
        if len(june) != 1:
            raise ValueError('June_monthly_installment_required')
        monthly_fact = {**june[0], 'source_locator': {**june[0]['source_locator'],
            'label_span': reviews['468095']['spans']['label']}}
        groups.append([_event(ticker, intermediate, ticker + ':2022-06-30'),
                       _event(ticker, monthly_fact, ticker + ':2022-06-30')])
        december = reviews['565334']['facts'][0]
        form = _native_for_stock(native['565624'], ticker, december)
        if Decimal(form['amount']) != Decimal(december['amount']):
            raise ValueError('December_exact_notice_and_native_amount_disagree')
        groups.append([_event(ticker, december, ticker + ':2022-12-15')])
    else:
        raise ValueError('unsupported_exact_target_ticker')
    return [{'id': events[0]['complete_group_id'], 'ticker': ticker,
        'record_date': events[0]['record_date'], 'events': events,
        'gross_total_decimal': str(sum((Decimal(r['gross_per_share_decimal']) for r in events), Decimal(0))),
        'installment_count': len(events), 'target_group_complete': True,
        'entire_cash_history_complete': False, 'independent_reconciliation_required': True}
        for events in groups]


def _sources(evidence):
    sources = []
    reader = evidence['reader']
    for source in reader.sources.values():
        if not source['file'].startswith('raw/'):
            continue
        sources.append({**source, 'id': 'older-cash:' + source['sha256'],
            'observed_at': datetime.fromtimestamp((reader.folder / source['file']).stat().st_mtime,
                                                  timezone.utc).isoformat(),
            'observed_at_basis': 'local_file_mtime_not_verified_network_retrieval',
            'authority': 'primary_CVM_original_not_settlement' if (source.get('url') or '').startswith(
                ('https://www.rad.cvm.gov.br/', 'https://dados.cvm.gov.br/')) else 'local_review_or_existing_registry_cache',
            'settlement_verified': False, 'point_in_time_archive_verified': False})
    return sources


@dataclass(frozen=True, init=False)
class OlderCashEvidence:
    """Immutable build snapshot. Construction verifies the actual source bytes.

    Each adapter call decodes a fresh view, so caller mutations cannot leak
    into another stock. The parent may retain one instance for its build.
    """

    folder: Path
    review_sha256: str
    _payload: bytes = field(repr=False)
    _payload_sha256: str = field(repr=False)

    def __init__(self, folder):
        evidence = read_evidence(folder)
        payload = shared._dump({**{k: v for k, v in evidence.items() if k != 'reader'},
                                'preloaded_sources': _sources(evidence)})
        object.__setattr__(self, 'folder', Path(folder).resolve())
        object.__setattr__(self, 'review_sha256', REVIEW_SHA256)
        object.__setattr__(self, '_payload', payload)
        object.__setattr__(self, '_payload_sha256', shared._sha(payload))

    def snapshot_for(self, folder):
        if Path(folder).resolve() != self.folder:
            raise ValueError('preloaded_evidence_exact_folder_required')
        if self.review_sha256 != REVIEW_SHA256 or shared._sha(self._payload) != self._payload_sha256:
            raise ValueError('preloaded_evidence_hash_mismatch')
        return json.loads(self._payload)


def load_older_cash_evidence(folder):
    """Verify once per parent build; retain this frozen object for all four stocks."""
    return OlderCashEvidence(folder)


def verified_older_cash_calendar(folder, stock, cutoff, *, evidence=None):
    """Parent adapter: read-only, exact identity, pinned originals, complete groups.

    The parent must explicitly review the returned record corrections and use
    its normal complete-group independent gross reconciliation afterwards.
    This function neither mutates the old history nor grants model admission.
    """
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    ticker = stock.get('ticker')
    empty = {'ticker': ticker, 'cvm': stock.get('cvm'), 'share_class': stock.get('share_class'),
        'events': [], 'sources': [], 'diagnostics': [], 'history_complete': False,
        'as_of': cutoff.isoformat(), 'active_model_changed': False, 'cash_model_eligible': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}
    if ticker not in TARGETS:
        return empty
    if any(stock.get(key) != value for key, value in TARGETS[ticker].items()):
        raise ValueError('older_calendar_exact_identity_required')
    if cutoff.isoformat() != CUTOFF:
        return {**empty, 'diagnostics': [{'code': 'reviewed_target_cutoff_required'}]}
    if evidence is None:
        evidence = load_older_cash_evidence(folder)
    if type(evidence) is not OlderCashEvidence:
        raise ValueError('verified_frozen_evidence_required_not_raw_dictionary')
    evidence = evidence.snapshot_for(folder)
    groups = complete_groups(evidence, ticker)
    for group in groups:
        for event in group['events']:
            event.update(target_group_complete=group['target_group_complete'],
                verified_by_older_cash_loader=True, review_manifest_sha256=REVIEW_SHA256)
    corrections = [r for r in evidence['manifest']['parent_integration']['record_relocations'] if ticker in r['tickers']]
    return {**empty, 'events': [event for group in groups for event in group['events']],
        'complete_groups': groups, 'sources': evidence['preloaded_sources'],
        'verified_by_older_cash_loader': True, 'review_manifest_sha256': REVIEW_SHA256,
        'record_corrections_for_parent_review': corrections,
        'revision_audits': evidence['revisions'] if ticker.startswith('ENMT') else [],
        'diagnostics': evidence['diagnostics'],
        'remaining_gaps': [r for r in evidence['manifest']['remaining_gaps'] if ticker in r['tickers']]}


def build_report(folder):
    evidence = read_evidence(folder)
    fetcher = Fetcher(folder)
    requests, responses = fetcher.ledger('request'), fetcher.ledger('response')
    if len(requests) > MAX_REQUESTS or len({r['url'] for r in requests}) != len(requests):
        raise ValueError('request_budget_or_retry_violation')
    for response in responses:
        evidence['reader'].read(response['file'], 'original_HTTP_response', response['sha256'], response['url'])
    report = {'schema': 'older-cash-recovered-groups-v1', 'as_of': CUTOFF,
        'review_sha256': REVIEW_SHA256, 'created_at': datetime.now(timezone.utc).isoformat(),
        'groups': [group for ticker in TARGETS for group in complete_groups(evidence, ticker)],
        'identity_evidence': evidence['identity'], 'primary_originals': evidence['originals'],
        'reviewed_narratives': evidence['reviews'], 'native_observations': evidence['native'],
        'complete_2022_monthly_calendars': evidence['monthly'], 'revision_audits': evidence['revisions'],
        'IPE_ALL_category_coverage': evidence['periods'], 'diagnostics': evidence['diagnostics'],
        'remaining_gaps': evidence['manifest']['remaining_gaps'],
        'parent_integration': evidence['manifest']['parent_integration'],
        'adapter_contract': {
            'construction': 'evidence = load_older_cash_evidence(folder)',
            'per_stock': 'verified_older_cash_calendar(folder, stock, cutoff, evidence=evidence)',
            'frozen_type': 'OlderCashEvidence', 'hash_pins_verified_at_construction': True,
            'returned_views_are_independent': True,
            'required_event_flags': ['verified_by_older_cash_loader', 'target_group_complete'],
            'required_event_fields': ['complete_group_id', 'review_manifest_sha256'],
            'recognized_format': 'CVM_reviewed_old_notice_complete_group',
            'standalone_unchecked_rows_not_verified': True},
        'requests': {'new_HTTP_attempts': len(requests), 'maximum': MAX_REQUESTS,
            'includes_robots': True, 'attempts': responses, 'proxy_used': False,
            'redirects_followed': False, 'retries': 0},
        'sources': _sources(evidence), 'selection_uses_independent_values': False,
        'active_model_changed': False, 'history_complete': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False, 'cash_model_eligible': False}
    path = save(folder, 'report', report)
    return report, path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default=str(Path(__file__).parent / 'data/market'))
    parser.add_argument('--acquire', nargs='*')
    parser.add_argument('--inspect', nargs='*')
    args = parser.parse_args()
    reader = shared.Reader(args.folder)
    documents, periods = indexed_documents(reader)
    if args.acquire:
        for sequence in args.acquire:
            matches = [d for d in documents if d['sequence'] == sequence]
            if len(matches) != 1:
                raise ValueError('one_exact_index_document_required')
            receipt = Fetcher(args.folder).acquire(matches[0])
            print(json.dumps({k: receipt[k] for k in ('url', 'status', 'http_status', 'sha256', 'file')}, ensure_ascii=True))
    if args.inspect:
        for sequence in args.inspect:
            document = next(d for d in documents if d['sequence'] == sequence)
            body, source = document_body(reader, document)
            print(json.dumps({'document': document, 'source': source,
                'pages': pdf_pages(body) if body else None}, ensure_ascii=True, indent=2))
    if not args.acquire and not args.inspect:
        report, path = build_report(args.folder)
        print(json.dumps({'report': path, 'requests': report['requests']['new_HTTP_attempts'],
            'groups': [{k: group[k] for k in ('id', 'gross_total_decimal', 'installment_count')}
                       for group in report['groups']], 'remaining_gaps': report['remaining_gaps']}, indent=2))


if __name__ == '__main__':
    main()
