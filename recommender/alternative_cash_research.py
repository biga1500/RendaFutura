"""Read-only alternative primary cash research for ASAI3, AGXY3 and ALPK3.

The default command is offline. Acquisition is explicit, direct CVM only, with
no proxy, redirects or retries and a persistent limit of twenty HTTP attempts
(including robots.txt). Outputs live only in raw/primary-research/alternative-cash
under the supplied market folder. Nothing here changes the parent cash gate.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlsplit
import zipfile

from . import cvm_proventos_documents as cvm


CACHE = 'raw/primary-research/alternative-cash'
TICKERS = ('ASAI3', 'AGXY3', 'ALPK3')
CUTOFF = '2026-09-30'
START = '2021-09-30'
MAX_REQUESTS = 20
MAX_BYTES = 12_000_000
ROBOT_URL = 'https://www.rad.cvm.gov.br/robots.txt'
USER_AGENT = 'RendaFutura-Academic/1.0'


def _dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _safe(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('unsafe_cache_reference')
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe_cache_reference')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('unsafe_cache_reference')
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError('symlink_cache_reference')
    return path


def _save(folder, name, body):
    if not name.startswith(CACHE + '/'):
        raise ValueError('outside_research_write_scope')
    path = _safe(folder, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def _save_json(folder, prefix, payload):
    body = _dump(payload)
    return _save(folder, f'{CACHE}/{prefix}-{_sha(body)}.json', body)


class Reader:
    """Hash actual original bytes, independently of assertions in other reports."""

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.sources = {}

    def read(self, name, role, expected=None, url=None):
        path = _safe(self.folder, name)
        if path.stat().st_size > 100_000_000:
            raise ValueError('oversized_original_cache')
        body = path.read_bytes()
        digest = _sha(body)
        if expected is not None and expected != digest:
            raise ValueError('original_content_hash_mismatch')
        receipt = self.sources.setdefault(name, {
            'file': name, 'sha256': digest, 'bytes': len(body),
            'hash_basis': 'actual_bytes_read', 'access': 'existing_cache_read_only',
            'roles': [], 'url': url})
        if digest != receipt['sha256']:
            raise ValueError('cache_changed_during_build')
        if role not in receipt['roles']:
            receipt['roles'].append(role)
        return body, receipt

    def json(self, name, role):
        body, receipt = self.read(name, role)
        return json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError('nonfinite_json'))), receipt


def scan_indices(reader, stocks):
    """Scan full annual native CSVs; record coverage separately from cash history.

    The whole 2021 index is retained, including pre-window publications that may
    schedule cash inside the window. Reference dates never replace delivery dates.
    Zero matching rows is an index observation, never zero distributions.
    """
    manifest, _ = reader.json('raw/primary-research/cvm-documents/index-sources.json', 'IPE_manifest')
    by_cvm = {stock['cvm']: stock['ticker'] for stock in stocks}
    documents, periods, diagnostics = [], [], []
    for year in range(2021, 2027):
        choices = [source for source in manifest if source.get('year') == year]
        if len(choices) != 1:
            diagnostics.append({'code': 'native_IPE_period_missing_or_ambiguous', 'year': year})
            continue
        source = choices[0]
        try:
            expected_url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
            if source.get('url') != expected_url or not re.fullmatch(r'[a-f0-9]{64}', source.get('sha256', '')):
                raise ValueError('native_IPE_source_identity_invalid')
            body, receipt = reader.read(source['file'], 'native_CVM_IPE', source['sha256'], source['url'])
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                member = f'ipe_cia_aberta_{year}.csv'
                if archive.namelist() != [member] or archive.getinfo(member).file_size > 100_000_000:
                    raise ValueError('invalid_native_IPE_archive')
                with archive.open(member) as stream:
                    rows = list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
            selected, period_documents = [], []
            for line, row in enumerate(rows, 2):
                code = row.get('Codigo_CVM', '')
                if not code.isdigit() or int(code) not in by_cvm:
                    continue
                published = cvm.day(row.get('Data_Entrega'))
                if not published or not published.startswith(str(year)):
                    raise ValueError('invalid_native_IPE_delivery_period')
                selected.append((line, row, published))
                if published > CUTOFF:
                    continue
                text = cvm.norm(' '.join(row.get(field, '') for field in ('Categoria', 'Tipo', 'Assunto')))
                if not any(word in text for word in ('provento', 'dividendo', 'capital proprio', 'jcp')):
                    continue
                sequence, version = cvm.document_id(row['Link_Download'])
                if int(row['Versao']) != version:
                    raise ValueError('native_IPE_version_mismatch')
                period_documents.append({'ticker': by_cvm[int(code)], 'cvm': int(code),
                    'issuer_name': row['Nome_Companhia'], 'published_at': published,
                    'reference_date': cvm.day(row['Data_Referencia']),
                    'sequence': sequence, 'version': version, 'url': row['Link_Download'],
                    'index_evidence': {'file': receipt['file'], 'sha256': receipt['sha256'],
                        'member': member, 'csv_line': line, 'raw_row': row},
                    'category': cvm.norm(row['Categoria'])})
            documents.extend(period_documents)
            periods.append({'year': year, 'file': receipt['file'], 'sha256': receipt['sha256'],
                'url': source['url'], 'member': member, 'encoding': 'latin1', 'delimiter': ';',
                'native_rows_scanned': len(rows), 'retrieved_at': source.get('retrieved_at'),
                'requested_delivery_period': {'start': f'{year}-01-01',
                    'end': min(f'{year}-12-31', CUTOFF)},
                'observed_global_delivery_min': min((r['Data_Entrega'] for r in rows), default=None),
                'observed_global_delivery_max': max((r['Data_Entrega'] for r in rows), default=None),
                'issuer_rows_before_cutoff': dict(Counter(by_cvm[int(r['Codigo_CVM'])]
                    for _, r, day in selected if day <= CUTOFF)),
                'index_scan_complete': True, 'cash_history_complete': False})
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            diagnostics.append({'code': 'native_IPE_period_unusable', 'year': year, 'error': str(error)})
    return sorted(documents, key=lambda d: (d['published_at'], d['sequence'])), periods, diagnostics


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('redirect_not_followed_no_second_request')


def _approved(url):
    try:
        parts = urlsplit(url)
        if (parts.hostname != 'www.rad.cvm.gov.br' or parts.scheme != 'https'
                or parts.port not in (None, 443) or parts.username or parts.password or parts.fragment):
            return False
        if url == ROBOT_URL:
            return True
        cvm.document_id(url)
        return True
    except ValueError:
        return False


class BoundedCVMFetcher:
    """One persistent budget for this research directory, counting robots too."""

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.requests = [json.loads(path.read_bytes())
                         for path in sorted((self.folder / CACHE).glob('request-*.json'))]
        self.responses = [json.loads(path.read_bytes())
                          for path in sorted((self.folder / CACHE).glob('response-*.json'))]
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _get(self, url):
        if not _approved(url):
            raise ValueError('unapproved_URL_no_request_sent')
        if any(row.get('http_status') in (401, 403, 429) for row in self.responses):
            raise ValueError('CVM_access_stop_no_request_sent')
        if url in {row['url'] for row in self.requests}:
            raise ValueError('URL_already_attempted_no_retry')
        if len(self.requests) >= MAX_REQUESTS:
            raise ValueError('research_request_budget_exhausted')
        request = {'url': url, 'ordinal': len(self.requests) + 1,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'proxy': False, 'redirects': False, 'automatic_retries': 0}
        _save_json(self.folder, 'request', request)
        self.requests.append(request)
        result = {**request, 'status': 'failed', 'http_status': None}
        body = b''
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                'Accept': 'text/plain' if url == ROBOT_URL else 'application/pdf',
                'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                result['http_status'] = response.status
                body = response.read(MAX_BYTES + 1)
                result['content_type'] = response.headers.get('Content-Type')
            if len(body) > MAX_BYTES:
                raise ValueError('response_size_limit')
            result['status'] = 'received'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, error=type(error).__name__)
            body = error.read(MAX_BYTES + 1)[:MAX_BYTES]
        except (OSError, ValueError) as error:
            result['error'] = str(error)
        digest = _sha(body)
        result.update(sha256=digest, bytes=len(body), retrieved_at=datetime.now(timezone.utc).isoformat())
        result['file'] = _save(self.folder, f'{CACHE}/body-{digest}.bin', body)
        _save_json(self.folder, 'response', result)
        self.responses.append(result)
        return body, result

    def acquire(self, document):
        url = document['url']
        if not _approved(url) or url == ROBOT_URL:
            raise ValueError('invalid_CVM_document_no_request_sent')
        sequence, version = cvm.document_id(url)
        expected_cvm = {'ASAI3': 25372, 'AGXY3': 25658, 'ALPK3': 24953}.get(document.get('ticker'))
        if (expected_cvm is None or document.get('cvm') != expected_cvm
                or document.get('sequence') != sequence or document.get('version') != version
                or not _day(document.get('published_at')) or document['published_at'] > CUTOFF):
            raise ValueError('document_outside_three_issuer_cutoff_scope_no_request_sent')
        if url in {row['url'] for row in self.requests}:
            raise ValueError('URL_already_attempted_no_retry')
        robots = next((row for row in self.responses if row['url'] == ROBOT_URL), None)
        if robots is None:
            robot_body, robots = self._get(ROBOT_URL)
        else:
            robot_body, _ = Reader(self.folder).read(robots['file'], 'robots', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(ROBOT_URL)
            parser.parse(robot_body.decode('utf-8', errors='strict').splitlines())
            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError('robots_disallow_no_document_request_sent')
            delay = parser.crawl_delay(USER_AGENT)
            if delay or parser.request_rate(USER_AGENT):
                raise ValueError('robots_rate_rule_requires_manual_spacing')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_access_denied')
        body, receipt = self._get(url)
        receipt = {**receipt, 'document': document,
                   'pdf_signature_verified': body.lstrip().startswith(b'%PDF-')}
        _save_json(self.folder, 'document', receipt)
        return receipt


def _stock_rows(reader):
    pointer, _ = reader.json('inventory/current.json', 'inventory_pointer_context_only')
    inventory, receipt = reader.json('inventory/' + pointer['snapshot'] + '/market.json', 'inventory_identity')
    if pointer.get('sha256') != receipt['sha256']:
        raise ValueError('inventory_pointer_hash_mismatch')
    stocks = [row for row in inventory['stocks'] if row.get('ticker') in TICKERS]
    if Counter(row['ticker'] for row in stocks) != Counter(TICKERS):
        raise ValueError('three_exact_inventory_identities_required')
    return sorted(stocks, key=lambda s: TICKERS.index(s['ticker']))


def _document_body(reader, document):
    sequence, version = document['sequence'], document['version']
    candidates = []
    for directory, pattern in (('cvm-documents', f'{sequence}-v{version}-*.pdf'),
                               ('cash-calendars', f'cvm{sequence}v{version}-*.pdf')):
        for path in (reader.folder / 'raw/primary-research' / directory).glob(pattern):
            digest = path.stem.rsplit('-', 1)[-1]
            candidates.append((path.relative_to(reader.folder).as_posix(), digest))
    for path in (reader.folder / CACHE).glob('document-*.json'):
        source, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'acquisition_receipt')
        if source.get('url') == document['url'] and source.get('status') == 'received':
            candidates.append((source['file'], source['sha256']))
    if not candidates:
        return None, None
    bodies = [reader.read(name, 'primary_CVM_original', digest, document['url']) for name, digest in candidates]
    if len({_sha(body) for body, _ in bodies}) != 1:
        raise ValueError('same_CVM_document_changed_bytes')
    return bodies[0]


def _day(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?', value):
        return None
    try:
        amount = Decimal(value)
        return value if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _native_day(value):
    text = str(value or '').strip()
    # Old ENET forms explicitly render .NET timestamps using AM/PM and US order.
    if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)', text):
        try:
            return datetime.strptime(text, '%m/%d/%Y %I:%M:%S %p').date().isoformat()
        except ValueError:
            return None
    return cvm.day(text)


def _observation(document, receipt, stock, kind, locator, **fields):
    event = {'ticker': stock['ticker'], 'cvm': stock['cvm'], 'isin': None,
        'share_class': None, 'identity_mapping_candidate': {
            'isin': stock['isin'], 'share_class': stock['share_class'],
            'basis': 'exact_cached_inventory_and_B3_identity_only_not_notice_text'},
        'kind': kind, 'label': None, 'record_date': None, 'payment_date': None,
        'approval_date': None, 'ex_date': None, 'gross_per_unit_decimal': None,
        'gross_per_share_decimal': None, 'reported_per_share_decimal': None,
        'amount_unit': None, 'amount_tax_basis': None, 'native_protocol': None,
        'published_at': document['published_at'], 'available_as_of': document['published_at'] <= CUTOFF,
        'document_sequence': document['sequence'], 'document_version': document['version'],
        'source': {'file': receipt['file'], 'sha256': receipt['sha256'],
            'url': document['url'], 'hash_basis': 'actual_original_bytes_read',
            'index_evidence': document['index_evidence']},
        'source_locator': locator, 'issues': [], 'is_settlement_confirmation': False,
        'paid_status_as_of_verified': False, 'cash_model_eligible': False,
        'history_complete': False, 'gross_reconstructed_from_net': False,
        'share_basis': 'as_declared_unreconciled', 'settlement_verified': False,
        'point_in_time_archive_verified': False, **fields}
    for field in ('record_date', 'payment_date', 'approval_date', 'ex_date'):
        if event[field] is not None and not _day(event[field]):
            raise ValueError('invalid_observation_' + field)
    for field in ('gross_per_unit_decimal', 'gross_per_share_decimal', 'reported_per_share_decimal'):
        if event[field] is not None and _decimal(event[field]) is None:
            raise ValueError('invalid_observation_' + field)
    payment = event['payment_date']
    event['status'] = 'announced' if payment and payment > CUTOFF else 'past_scheduled' if payment else 'date_unresolved'
    if not payment:
        event['issues'].append('payment_date_unresolved_deadline_not_payment')
    if not event['record_date']:
        event['issues'].append('record_date_unresolved')
    if payment and event['record_date'] and payment < event['record_date']:
        event['issues'].append('payment_before_record')
    event['id'] = 'alternative:' + _sha(_dump(event))
    return event


def parse_native_tables(tables, document, receipt, stock):
    """Native CVM rows, including explicit legacy US timestamps/dot decimals."""
    identities = {int(match) for table in tables for row in table for cell in row
                  for match in re.findall(r'\b(\d{6})IPE', str(cell or ''))}
    issuer = cvm.metadata_value(tables, 'denominacao social')
    if (document['cvm'] != stock['cvm'] or identities != {stock['cvm']}
            or not issuer or not cvm.norm(issuer).startswith(cvm.norm(document['issuer_name']).split(' em recuperacao')[0])):
        raise ValueError('native_document_issuer_not_exact')
    protocol = cvm.compact(cvm.metadata_value(tables, 'protocolo provento'))
    version = cvm.compact(cvm.metadata_value(tables, 'versao'))
    if not protocol.isdigit() or version != str(document['version']):
        raise ValueError('native_protocol_or_version_unresolved')
    record = _native_day(cvm.metadata_value(tables, 'ultimo dia de negociacao com direitos'))
    approval = _native_day(cvm.metadata_value(tables, 'data aprovacao'))
    sent = _native_day(cvm.metadata_value(tables, 'data envio'))
    if sent and sent > document['published_at']:
        raise ValueError('native_send_after_index_delivery')
    legacy = any(re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)', str(cell or ''))
                 for table in tables for row in table for cell in row)
    events = []
    for table_index, table in enumerate(tables):
        header = next((index for index, row in enumerate(table) if len(row) >= 7
            and 'isin' in cvm.norm(row[0]) and 'valor bruto' in cvm.norm(row[1])
            and 'data pagamento' in cvm.norm(row[6])), None)
        if header is None:
            continue
        for row_index, row in enumerate(table[header + 1:], header + 1):
            if len(row) < 7 or not cvm.compact(row[0]):
                continue
            isin = cvm.compact(row[0]).upper()
            if isin != stock['isin']:
                raise ValueError('native_row_ISIN_mismatch')
            raw_amount = cvm.compact(row[1])
            if legacy and re.fullmatch(r'\d+\.\d+', raw_amount):
                amount = _decimal(raw_amount)
            else:
                if '.' in raw_amount and ',' not in raw_amount:
                    raise ValueError('native_decimal_dialect_ambiguous')
                amount = str(cvm.gross(raw_amount))
            if amount is None:
                raise ValueError('native_gross_unresolved')
            events.append(_observation(document, receipt, stock, 'native_CVM_form', {
                'format': 'native_CVM_PDF_table', 'table': table_index, 'row': row_index,
                'raw_row': row, 'raw_record_date': cvm.metadata_value(tables, 'ultimo dia de negociacao com direitos'),
                'legacy_explicit_AM_PM_date_dialect': legacy},
                isin=isin, share_class=stock['share_class'],
                share_class_basis='exact_ISIN_to_cached_identity_mapping_not_in_form',
                record_date=record, approval_date=approval, payment_date=_native_day(row[6]),
                gross_per_unit_decimal=amount, amount_unit='BRL/ISIN-unit',
                amount_tax_basis='explicit_gross', native_protocol=protocol))
    return events, cvm.installment_rows(tables)


_MONTHS = {name: index for index, name in enumerate(('janeiro', 'fevereiro', 'marco',
    'abril', 'maio', 'junho', 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'), 1)}


def _dates_in_quote(quote):
    values = set()
    for match in re.finditer(r'\b(\d(?:\s?\d)?) de ([a-z]+) de (\d{4})\b', cvm.norm(quote)):
        day, month, year = match.groups()
        try:
            values.add(date(int(year), _MONTHS[month], int(day.replace(' ', ''))).isoformat())
        except (ValueError, KeyError):
            pass
    values.update(filter(None, (cvm.day(match) for match in re.findall(r'\b\d{2}/\d{2}/\d{4}\b', quote))))
    return values


def parse_reviewed_notice(pages, review, document, receipt, stock):
    """Check human-reviewed facts against the exact original PDF and field spans.

    Reviews do not insert an ISIN absent from prose, repair dates, infer gross
    from net, or establish history coverage. Fields and their source quotes are
    retained together for the parent reviewer.
    """
    if (review['sha256'] != receipt['sha256'] or review['sequence'] != document['sequence']
            or review['ticker'] != stock['ticker'] or document['cvm'] != stock['cvm']):
        raise ValueError('review_original_identity_or_hash_mismatch')
    cnpjs = {re.sub(r'\D', '', match) for match in re.findall(
        r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', '\n'.join(pages[:2]))}
    if stock.get('cnpj') not in cnpjs:
        raise ValueError('review_issuer_CNPJ_not_in_original')
    fields = {}
    allowed = {'label', 'share_class', 'record_date', 'payment_date', 'approval_date',
               'ex_date', 'gross_per_share_decimal', 'reported_per_share_decimal'}
    for field, proof in review['fields'].items():
        if field not in allowed or type(proof['page']) is not int or not 1 <= proof['page'] <= len(pages):
            raise ValueError('review_field_or_page_invalid')
        quote, value = proof['quote'], proof['value']
        normalized = cvm.norm(quote)
        if not normalized or normalized not in cvm.norm(pages[proof['page'] - 1]):
            raise ValueError('review_quote_not_in_original_page')
        if field.endswith('_date') and (not _day(value) or value not in _dates_in_quote(quote)):
            raise ValueError('review_date_not_explicit_in_quote')
        if field == 'payment_date' and (any(term in normalized for term in
                ('ex dividend', 'ex juros', 'ex direitos', 'ate o', 'prazo', 'fim do exercicio'))
                or not any(term in normalized for term in ('pagamento', 'sera efetuado',
                    'sera realizado', 'seja realizado', 'realizara'))):
            raise ValueError('review_payment_context_not_explicit')
        if field == 'record_date' and not any(term in normalized for term in
                ('acionaria', 'ac ionaria', 'registros')):
            raise ValueError('review_record_context_not_explicit')
        if field.endswith('_decimal'):
            numbers = {str(Decimal(token.replace(',', '.'))) for token in
                       re.findall(r'(?<![\d.,])\d+,\d+(?![\d.,])', quote)}
            if _decimal(value) is None or str(Decimal(value)) not in numbers:
                raise ValueError('review_amount_not_explicit_in_quote')
            if field == 'gross_per_share_decimal' and ('bruto' not in normalized or 'liquido' in normalized):
                raise ValueError('review_gross_not_explicit')
        if field == 'label' and not (value == 'JCP' and 'juros sobre capital proprio' in normalized
                                    or value == 'DIVIDENDO' and 'dividendos' in normalized):
            raise ValueError('review_cash_type_not_explicit')
        if field == 'share_class' and (value != 'ON' or 'acao ordinaria' not in normalized):
            raise ValueError('review_share_class_not_explicit')
        fields[field] = value
    return _observation(document, receipt, stock, 'reviewed_primary_issuer_notice', {
        'format': 'reviewed_PDF_field_spans', 'fields': review['fields']},
        review_notes=review.get('notes', []),
        payment_date_semantics='payment_start_with_custody_conditions' if fields.get('payment_date')
            and 'a partir' in cvm.norm(review['fields']['payment_date']['quote']) else 'scheduled_date',
        amount_unit='BRL/share', amount_tax_basis='explicit_gross' if fields.get('gross_per_share_decimal')
            else 'unspecified_not_reconstructed', **fields)


def annotate_conflicts(events):
    """Retain revisions, exact amount variants and all cross-source conflicts."""
    native = [event for event in events if event['native_protocol']]
    for event in native:
        revisions = [row for row in native if row['cvm'] == event['cvm']
                     and row['native_protocol'] == event['native_protocol']]
        max_version = max(row['document_version'] for row in revisions)
        event['superseded_native_version'] = event['document_version'] < max_version
        event['native_revision_group'] = f"{event['cvm']}:{event['native_protocol']}"
    conflicts = []
    records = {event['record_date'] for event in events if event['record_date']}
    for record in sorted(records):
        group = [event for event in events if event['record_date'] == record
                 and not event.get('superseded_native_version')]
        for field in ('payment_date', 'label', 'share_class'):
            values = sorted({event[field] for event in group if event[field] is not None})
            if len(values) > 1:
                conflicts.append({'code': 'primary_' + field + '_conflict', 'record_date': record,
                    'values': values, 'event_ids': [event['id'] for event in group]})
        amounts = {Decimal(event['gross_per_share_decimal'] or event['gross_per_unit_decimal'])
                   for event in group if event['gross_per_share_decimal'] or event['gross_per_unit_decimal']}
        if len(amounts) > 1:
            conflicts.append({'code': 'primary_exact_amount_variants_no_rounding_assumed',
                'record_date': record, 'values': sorted(str(amount) for amount in amounts),
                'event_ids': [event['id'] for event in group]})
    for conflict in conflicts:
        for event in events:
            if event['id'] in conflict['event_ids']:
                event['issues'].append(conflict['code'])
    return conflicts


def _network_audit(reader):
    requests, responses = [], []
    for prefix, target in (('request', requests), ('response', responses)):
        for path in sorted((reader.folder / CACHE).glob(prefix + '-*.json')):
            payload, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'network_' + prefix + '_receipt')
            target.append(payload)
            if prefix == 'response':
                reader.read(payload['file'], 'network_original_response', payload['sha256'], payload['url'])
    return {'new_HTTP_attempts': len(requests), 'maximum': MAX_REQUESTS,
        'budget_includes_robots': True, 'direct_CVM_only': all(_approved(row['url']) for row in requests),
        'proxies_used': False, 'redirects_followed': False, 'retries': 0,
        'http_status_counts': dict(Counter(str(row['http_status']) for row in responses)),
        'attempts': sorted(responses, key=lambda row: row['ordinal'])}


def build_report(folder):
    """Offline inspection; never invokes acquisition or imports the parent pipeline."""
    reader = Reader(folder)
    stocks = _stock_rows(reader)
    documents, periods, diagnostics = scan_indices(reader, stocks)
    baseline, _ = reader.json('raw/primary-research/cvm-documents/report.json', 'existing_primary_cash_bridge_input_report')
    review_path = reader.folder / CACHE / 'reviewed-notices.json'
    reviews, _ = reader.json(review_path.relative_to(reader.folder).as_posix(), 'reviewed_notice_field_spans')
    reviews = {review['sequence']: review for review in reviews['observations']}
    results = []
    for stock in stocks:
        detail, identity_receipt = reader.json(f"raw/expanded/b3-detail-{stock['cvm']}.json", 'existing_B3_identity_only')
        codes = {row['code']: row['isin'] for row in detail.get('otherCodes', [])}
        identity_ok = (str(detail.get('codeCVM')) == str(stock['cvm'])
            and codes.get(stock['ticker']) == stock.get('isin') and stock.get('share_class') == 'ON')
        stock = {**stock, 'cnpj': detail.get('cnpj')}
        issuer_documents = [document for document in documents if document['ticker'] == stock['ticker']]
        events, document_diagnostics = [], []
        for document in issuer_documents:
            try:
                body, source = _document_body(reader, document)
                if body is None:
                    document_diagnostics.append({'sequence': document['sequence'], 'code': 'primary_original_not_cached'})
                    continue
                if not body.lstrip().startswith(b'%PDF-'):
                    raise ValueError('original_response_not_PDF_no_cash_evidence')
                from pypdf import PdfReader
                import pdfplumber
                pdf_reader = PdfReader(io.BytesIO(body), strict=True)
                if pdf_reader.is_encrypted or not 1 <= len(pdf_reader.pages) <= 12:
                    raise ValueError('unsupported_original_PDF')
                pages = [page.extract_text() for page in pdf_reader.pages]
                if document['category'] == 'relatorio proventos':
                    with pdfplumber.open(io.BytesIO(body)) as pdf:
                        tables = [table for page in pdf.pages for table in page.extract_tables()]
                    native_events, installments = parse_native_tables(tables, document, source, stock)
                    events.extend(native_events)
                    if not native_events:
                        raise ValueError('native_PDF_without_proventos_rows')
                    if installments:
                        document_diagnostics.append({'sequence': document['sequence'],
                            'code': 'unmapped_installments_not_assigned_to_ISIN', 'rows': installments})
                elif document['sequence'] in reviews:
                    events.append(parse_reviewed_notice(pages, reviews[document['sequence']], document, source, stock))
                else:
                    document_diagnostics.append({'sequence': document['sequence'], 'code': 'original_requires_field_review'})
            except (OSError, ValueError, KeyError) as error:
                document_diagnostics.append({'sequence': document['sequence'], 'code': 'primary_document_unusable', 'error': str(error)})
        if not identity_ok:
            for event in events:
                event['issues'].append('cached_identity_mapping_conflict')
        conflicts = annotate_conflicts(events)
        secondary, _ = reader.json(f"raw/inventory/payments/{stock['ticker']}.json", 'existing_secondary_calendar_diagnostic_only')
        secondary_events = secondary.get('events', [])
        comparisons = []
        for row in secondary_events:
            record, payment = row.get('record_date'), row.get('payment_date')
            if not (record and START <= record <= CUTOFF or payment and START <= payment <= CUTOFF):
                continue
            matches = [event for event in events if event['record_date'] == record
                       and not event.get('superseded_native_version')]
            comparisons.append({'secondary_row': row, 'primary_event_ids': [event['id'] for event in matches],
                'primary_payment_dates': sorted({event['payment_date'] for event in matches if event['payment_date']}),
                'primary_gross_as_declared': sorted({event['gross_per_unit_decimal'] or event['gross_per_share_decimal']
                    for event in matches if event['gross_per_unit_decimal'] or event['gross_per_share_decimal']}),
                'secondary_not_used_to_fill_primary_fields': True,
                'share_adjustments_not_reconciled': True})
        missing = ['complete_primary_cash_history_or_explicit_no_distribution_evidence_for_entire_window',
                   'pre_2021_publications_and_non_keyword_notices_not_exhaustively_reviewed',
                   'share_basis_and_corporate_actions_reconciliation',
                   'settlement_not_proved_by_declared_or_paid_reported_status',
                   'parent_independent_cash_gate_requires_separate_integration']
        if conflicts:
            missing.append('primary_conflicts_require_reconciliation')
        if any(not event['isin'] for event in events):
            missing.append('notice_ISIN_absent_requires_exact_primary_identity_link')
        if any(not event['label'] for event in events):
            missing.append('native_form_cash_type_not_specified')
        if any(not item['primary_event_ids'] for item in comparisons):
            missing.append('secondary_entitlement_without_reviewed_primary_observation')
        field_gaps = []
        for event in events:
            absent = [field for field in ('isin', 'share_class', 'record_date', 'payment_date')
                      if event[field] is None]
            if event['gross_per_unit_decimal'] is None and event['gross_per_share_decimal'] is None:
                absent.append('explicit_gross_amount')
            if event['label'] is None:
                absent.append('cash_type')
            field_gaps.append({'event_id': event['id'], 'sequence': event['document_sequence'],
                'absent_fields': absent, 'issues': event['issues'],
                'superseded_native_version': event.get('superseded_native_version', False)})
        results.append({'identity': {key: stock.get(key) for key in
            ('ticker', 'cvm', 'cnpj', 'isin', 'share_class', 'name', 'identity_status', 'listing_start')},
            'identity_exact_cached_mapping': identity_ok, 'identity_source': identity_receipt,
            'documents': issuer_documents, 'events': events, 'conflicts': conflicts,
            'diagnostics': document_diagnostics, 'exact_missing': missing,
            'field_gaps': field_gaps,
            'annual_observation_coverage': [{'year': year,
                'observed_record_dates': sorted({event['record_date'] for event in events
                    if event['record_date'] and event['record_date'].startswith(str(year))}),
                'absence_proves_no_distribution': False, 'history_complete': False}
                for year in range(2021, 2027)],
            'secondary_comparison_only': comparisons,
            'existing_bridge_report_documents': [{key: row.get(key) for key in ('sequence', 'version', 'status', 'issues')}
                for row in baseline['results'] if row.get('cvm') == stock['cvm']],
            'history_complete': False, 'alternative_proof_complete': False,
            'cash_model_eligible': False, 'parent_pending_reason': stock.get('model_pending_reason')})
    network = _network_audit(reader)
    return {'schema_version': 1, 'scope': 'alternative_primary_cash_research_only',
        'window_start': START, 'cutoff': CUTOFF, 'tickers': list(TICKERS),
        'native_CVM_IPE_periods': periods, 'results': results, 'diagnostics': diagnostics,
        'native_IPE_selection': 'Full cached 2021..2026 CSVs scanned. Candidate documents selected '
            'by proventos/dividendos/capital proprio/JCP in category/type/subject; document contents '
            'of non-matching categories are not exhaustively reviewed. Pre-2021 indices not scanned. '
            'Index publication coverage does not prove a complete payment calendar.',
        'network_audit': network,
        'interpretation': 'Events are non-additive source observations, including revisions and conflicts. '
            'Native IPE index coverage is publication coverage, never proof of all cash payments or zero distributions.',
        'sources': list(reader.sources.values()), 'active_model_changed': False,
        'cash_model_eligible': False, 'history_complete': False, 'settlement_verified': False,
        'missing_is_zero': False, 'parent_integration_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire-sequences', nargs='+', default=[])
    parser.add_argument('--save-report', action='store_true')
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.acquire_sequences:
        reader = Reader(folder)
        documents, _, _ = scan_indices(reader, _stock_rows(reader))
        wanted = set(args.acquire_sequences)
        selected = [document for document in documents if document['sequence'] in wanted]
        if {document['sequence'] for document in selected} != wanted:
            raise ValueError('sequence_not_in_exact_three_issuer_native_indices')
        fetcher = BoundedCVMFetcher(folder)
        for document in selected:
            try:
                source = fetcher.acquire(document)
                print(json.dumps({key: source.get(key) for key in
                    ('url', 'ordinal', 'http_status', 'bytes', 'pdf_signature_verified', 'error')}), flush=True)
                if source['http_status'] in (401, 403, 429):
                    break
            except ValueError as error:
                print(json.dumps({'sequence': document['sequence'], 'error': str(error)}), flush=True)
                if 'already_attempted' not in str(error):
                    break
    else:
        report = build_report(folder)
        if args.save_report:
            print(_save_json(folder, 'report', report))
        else:
            print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
