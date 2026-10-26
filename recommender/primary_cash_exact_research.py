"""Isolated, exact native cash-notice research. Production inputs are read-only.

Default execution is offline. Acquisition is opt-in, exact indexed CVM URLs
only, bounded to twelve direct requests including robots, without redirects,
proxies or retries. All generated files stay in this helper's raw directory.
"""

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
import zipfile
from urllib.parse import parse_qs, urlsplit

from . import alternative_cash_research as shared
from . import cvm_proventos_documents as cvm


CACHE = 'raw/primary-research/primary-cash-exact'
SNAPSHOT = 'snapshots/universe-20261025T094443-a67674b9'
AUDIT_SHA = '91a48f825920484ec3f2d0b37b21a31eaee1b03c9142240fd18e84aedf5f51b2'
REVIEW_SHA = '9dbda3201f121764a9d9ff6942e021e139ed00b46381a9cafe3396f01b776b4f'
IPE_MANIFEST_SHA = '0d5f66f63001963adf3355f103c4fae241435ebf5f22bbfeb75693f072dedc14'
NATIVE_HASHES = {
    '613044': '289bfe65938593b339669bd394760270a6b3f4a5beeb7d77ab20fbf5749d753b',
    '613045': '13c2940a33c6c553763da921e408d31f10c99cab6c46289e449468cb67a4d518',
    '700514': '52a999d3b2cd17c1c6d72c596d3025c9d9ef6580ddd9ad4181a240ed67d1a203',
    '742120': '054d45dcb7270d7ee621616d28eb342dba1c8fa074ce5204309c7237b0b965f2',
}
CUTOFF = date(2026, 9, 30)
MAX_REQUESTS = 12
MAX_BYTES = 12_000_000
TARGETS = {
    'PNVL3': {'cvm': 9342, 'share_class': 'ON', 'isin': 'BRPNVLACNOR9',
              'record_date': '2024-04-05', 'period': ('2024-03-01', '2024-05-31')},
    'EZTC3': {'cvm': 20770, 'share_class': 'ON', 'isin': 'BREZTCACNOR0',
              'record_date': '2022-05-17', 'period': ('2022-05-01', '2022-06-30')},
    'AFLT3': {'cvm': 22179, 'share_class': 'ON', 'isin': 'BRAFLTACNOR1',
              'record_date': '2023-04-20', 'period': ('2023-04-01', '2023-06-30')},
}
REASONS = {'cash_amount_or_share_scale_disagrees_with_B3',
           'B3_cash_event_missing_from_payment_table',
           'in_window_payment_date_or_amount_unresolved'}


def save(folder, name, body):
    """Exclusive immutable writes, confined even when a caller passes a path."""
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
        raise ValueError('invalid_research_filename')
    path = shared._safe(Path(folder).resolve(), CACHE + '/' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(Path(folder).resolve()).as_posix()


def save_json(folder, prefix, value):
    body = shared._dump(value)
    return save(folder, prefix + '-' + shared._sha(body) + '.json', body)


def pinned_audit(reader):
    body, source = reader.read(SNAPSHOT + '/candidate-source-audit.json', 'pinned_staged_audit', AUDIT_SHA)
    audit = json.loads(body)
    for ticker in TARGETS:
        rows = [r for r in audit['pending'] if r['ticker'] == ticker]
        if len(rows) != 1 or rows[0]['reason'] not in REASONS:
            raise ValueError('target_not_pending_in_pinned_staged_snapshot')
    return audit, source


def indexed_documents(reader):
    """All categories and blank-subject versions, with exact CSV line provenance."""
    body, _ = reader.read('raw/primary-research/cvm-documents/index-sources.json',
                          'IPE_manifest', IPE_MANIFEST_SHA)
    manifest = json.loads(body)
    documents, coverage = [], []
    for year in range(2021, 2027):
        sources = [s for s in manifest if s.get('year') == year]
        if len(sources) != 1:
            raise ValueError('six_exact_native_IPE_indices_required')
        source = sources[0]
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        if source['url'] != url:
            raise ValueError('wrong_native_index_origin')
        body, receipt = reader.read(source['file'], 'native_CVM_IPE_all_categories', source['sha256'], url)
        member = f'ipe_cia_aberta_{year}.csv'
        counts = Counter()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if archive.namelist() != [member] or archive.getinfo(member).file_size > 100_000_000:
                raise ValueError('invalid_native_index_archive')
            with archive.open(member) as stream:
                rows = csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
                for line, row in enumerate(rows, 2):
                    if not row['Codigo_CVM'].isdigit():
                        continue
                    code = int(row['Codigo_CVM'])
                    target = next((t for t in TARGETS.values() if t['cvm'] == code), None)
                    if target is None:
                        continue
                    published = cvm.day(row['Data_Entrega'])
                    if not published or not published.startswith(str(year)):
                        raise ValueError('invalid_native_delivery')
                    if published > CUTOFF.isoformat():
                        continue
                    sequence, version = cvm.document_id(row['Link_Download'])
                    if version != int(row['Versao']):
                        raise ValueError('index_version_mismatch')
                    counts[str(code)] += 1
                    documents.append({'cvm': code, 'issuer_name': row['Nome_Companhia'],
                        'sequence': sequence, 'version': version, 'url': row['Link_Download'],
                        'published_at': published, 'reference_date': cvm.day(row['Data_Referencia']),
                        'category': row['Categoria'], 'type': row['Tipo'], 'subject': row['Assunto'],
                        'index_evidence': {'file': receipt['file'], 'sha256': receipt['sha256'],
                            'url': url, 'member': member, 'csv_line': line, 'raw_row': row}})
        coverage.append({'year': year, 'file': receipt['file'], 'sha256': receipt['sha256'],
            'issuer_rows': dict(counts), 'all_categories': True, 'cash_history_complete': False})
    return documents, coverage


def document_body(reader, document):
    body, source = shared._document_body(reader, document)
    choices = [] if body is None else [(body, source)]
    for path in (reader.folder / CACHE).glob('document-*.json'):
        receipt, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'acquisition_receipt')
        if receipt['url'] == document['url'] and receipt['status'] == 'received':
            if receipt['document'] != document:
                raise ValueError('acquired_document_index_identity_changed')
            choices.append(reader.read(receipt['file'], 'primary_CVM_original', receipt['sha256'], receipt['url']))
    if len({shared._sha(body) for body, _ in choices}) > 1:
        raise ValueError('same_native_document_changed_bytes')
    return choices[0] if choices else (None, None)


class Fetcher:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())

    def ledger(self, prefix):
        return [json.loads(p.read_bytes()) for p in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def _get(self, url):
        if not shared._approved(url):
            raise ValueError('unapproved_URL_no_request_sent')
        requests, responses = self.ledger('request'), self.ledger('response')
        if any(r.get('access_stop') for r in responses):
            raise ValueError('access_stop_no_request_sent')
        if url in {r['url'] for r in requests}:
            raise ValueError('already_attempted_no_retry')
        if len(requests) >= MAX_REQUESTS:
            raise ValueError('request_budget_exhausted')
        attempt = {'url': url, 'ordinal': len(requests) + 1, 'proxy': False,
            'redirects': False, 'retries': 0, 'started_at': datetime.now(timezone.utc).isoformat()}
        save(self.folder, f'request-{len(requests) + 1:02d}.json', shared._dump(attempt))
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
            body = error.read(MAX_BYTES + 1)[:MAX_BYTES]
        except (OSError, ValueError) as error:
            result['error'] = str(error)
            body = body[:MAX_BYTES]
        challenge = any(token in body.lower() for token in (
            b'captcha', b'cf-chl-', b'access denied', b'authentication required', b'login required'))
        result['access_stop'] = result['http_status'] in (401, 403, 429) or challenge
        result.update(sha256=shared._sha(body), bytes=len(body), retrieved_at=datetime.now(timezone.utc).isoformat())
        result['file'] = save(self.folder, 'body-' + result['sha256'] + '.bin', body)
        save_json(self.folder, 'response', result)
        return body, result

    def acquire(self, document):
        reader = shared.Reader(self.folder)
        documents, _ = indexed_documents(reader)
        if document not in documents:
            raise ValueError('exact_native_index_document_required')
        if document_body(reader, document)[0] is not None:
            return {'status': 'already_cached_no_request', 'sequence': document['sequence']}
        robots = next((r for r in self.ledger('response') if r['url'] == shared.ROBOT_URL), None)
        if robots is None:
            body, robots = self._get(shared.ROBOT_URL)
        else:
            body, _ = reader.read(robots['file'], 'robots', robots['sha256'])
        if robots.get('access_stop'):
            raise ValueError('robots_denied_access_stop')
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(shared.ROBOT_URL)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(shared.USER_AGENT, document['url']):
                save_json(self.folder, 'response', {'url': document['url'], 'access_stop': True,
                    'status': 'robots_disallow_no_document_request_sent'})
                raise ValueError('robots_disallow_access_stop')
            if parser.crawl_delay(shared.USER_AGENT) or parser.request_rate(shared.USER_AGENT):
                raise ValueError('robots_requires_spacing_no_request_sent')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable')
        body, receipt = self._get(document['url'])
        receipt = {**receipt, 'document': document, 'pdf_signature': body.lstrip().startswith(b'%PDF-')}
        save_json(self.folder, 'document', receipt)
        return receipt


def pdf_pages(body):
    from pypdf import PdfReader
    if len(body) > MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
        raise ValueError('native_document_not_PDF')
    reader = PdfReader(io.BytesIO(body), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 40:
        raise ValueError('unsupported_native_PDF')
    return [page.extract_text() or '' for page in reader.pages]


def norm(text):
    folded = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+([,.;:])', r'\1', ' '.join(folded.split()))


def protected_state(folder):
    root = Path(folder).resolve()
    repo = Path(__file__).parent
    paths = [repo / name for name in ('inventory_candidates.py', 'cash_calendar_research.py',
        'test_cash_calendar_research.py', 'older_cash_notice_research.py', 'test_older_cash_notice_research.py')]
    paths += [root / name for name in ('current.json', 'inventory/current.json')]
    for name in (SNAPSHOT, 'raw/primary-research/cash-calendars', 'raw/primary-research/older-cash'):
        paths.extend(p for p in (root / name).rglob('*') if p.is_file())
    return {str(p): shared._sha(p.read_bytes()) for p in sorted(paths) if p.is_file()}


def reviewed(reader, documents):
    body, _ = reader.read(CACHE + '/reviewed-notices.json', 'reviewed_original_field_spans', REVIEW_SHA)
    result = json.loads(body)['documents']
    for seq, review in result.items():
        document = documents[seq]
        body, source = document_body(reader, document)
        if source is None or source['sha256'] != review['sha256']:
            raise ValueError('reviewed_original_hash_mismatch:' + seq)
        pages = pdf_pages(body)
        if type(review['page']) is not int or not 1 <= review['page'] <= len(pages):
            raise ValueError('invalid_review_page')
        if any(not span or norm(span) not in norm(pages[review['page'] - 1])
               for span in review['spans'].values()):
            raise ValueError('reviewed_span_missing_from_original:' + seq)
        review.update(source={**source, 'index_evidence': document['index_evidence']}, document=document)
    return result


def native(reader, document, target):
    """Require one exact ISIN row before mapping an entire annex to that ISIN."""
    import pdfplumber
    body, source = document_body(reader, document)
    if source is None or source['sha256'] != NATIVE_HASHES[document['sequence']]:
        raise ValueError('native_original_hash_mismatch')
    pdf_pages(body)
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [table for page in pdf.pages for table in page.extract_tables()]
    identities = {int(m) for t in tables for row in t for cell in row
                  for m in re.findall(r'\b(\d{6})IPE', str(cell or ''))}
    issuer = cvm.metadata_value(tables, 'denominacao social')
    issuer_key = lambda value: re.sub(r'[^a-z0-9]', '', norm(value or ''))
    if (identities != {target['cvm']} or document['cvm'] != target['cvm']
            or issuer_key(issuer) != issuer_key(document['issuer_name'])):
        raise ValueError('native_CVM_identity_mismatch')
    record = cvm.day(cvm.metadata_value(tables, 'ultimo dia de negociacao com direitos'))
    approval = cvm.day(cvm.metadata_value(tables, 'data aprovacao'))
    version = cvm.compact(cvm.metadata_value(tables, 'versao'))
    protocol = cvm.compact(cvm.metadata_value(tables, 'protocolo provento'))
    sent = cvm.day(cvm.metadata_value(tables, 'data envio'))
    if (record != target['record_date'] or version != str(document['version'])
            or not protocol.isdigit() or not approval or not sent or sent > document['published_at']):
        raise ValueError('native_record_version_or_publication_mismatch')
    rows = []
    for ti, table in enumerate(tables):
        headers = [i for i, row in enumerate(table) if len(row) == 7
            and 'isin' in cvm.norm(row[0]) and 'valor bruto' in cvm.norm(row[1])]
        for header in headers:
            rows.extend((ti, ri, row) for ri, row in enumerate(table) if ri > header and cvm.compact(row[0]))
    if len(rows) != 1:
        raise ValueError('single_explicit_ISIN_required_for_annex_mapping')
    ti, ri, row = rows[0]
    if len(row) != 7 or cvm.compact(row[0]) != target['isin'] or cvm.norm(row[4]) != 'nao':
        raise ValueError('native_ISIN_or_monetary_update_unresolved')
    total = cvm.gross(row[1])
    annex = cvm.installment_rows(tables)
    form = cvm.norm(row[5])
    if form == 'parcelado':
        installments = [{'payment_date': a['payment_date'], 'gross': a['gross_per_unit_decimal'],
                         'locator': {'table': a['source_table'], 'row': a['source_row']}} for a in annex]
        if not installments or any(not a['payment_date'] or not a['gross']
                                   or Decimal(a['gross']) <= 0 for a in installments):
            raise ValueError('entire_native_installment_annex_required')
        if len({(a['payment_date'], a['gross']) for a in installments}) != len(installments):
            raise ValueError('duplicate_native_installment_row')
        if sum((Decimal(a['gross']) for a in installments), Decimal(0)) != total:
            raise ValueError('native_installment_sum_disagrees_with_gross_total')
    elif form == 'a vista' and not annex and cvm.day(row[6]):
        installments = [{'payment_date': cvm.day(row[6]), 'gross': str(total), 'locator': {'table': ti, 'row': ri}}]
    else:
        raise ValueError('native_payment_form_unresolved')
    if total <= 0 or any(date.fromisoformat(a['payment_date']) < date.fromisoformat(record) for a in installments):
        raise ValueError('invalid_native_amount_or_date_order')
    return {'record_date': record, 'approval_date': approval, 'gross_total_decimal': str(total),
        'installments': installments, 'native_protocol': protocol, 'version': int(version),
        'fiscal_year': cvm.compact(row[3]), 'period': cvm.norm(row[2]),
        'source': {**source, 'index_evidence': document['index_evidence']}, 'document': document,
        'amount_tax_basis': 'explicit_gross_exact_ISIN_unit',
        'annex_mapping_basis': 'only_one_explicit_ISIN_in_entire_form',
        'settlement_verified': False}


def _notice_protocol(document):
    return parse_qs(urlsplit(document['url']).query)['numProtocolo'][0]


def _require_equal(actual, expected, code):
    if actual != expected:
        raise ValueError(code)


def evidence(folder):
    reader = shared.Reader(folder)
    audit, audit_source = pinned_audit(reader)
    docs, coverage = indexed_documents(reader)
    documents = {d['sequence']: d for d in docs}
    reviews = reviewed(reader, documents)
    forms = {seq: native(reader, documents[seq], TARGETS['PNVL3' if seq == '742120' else 'AFLT3'])
             for seq in NATIVE_HASHES}
    # Pin the precise body facts as well as the source bytes. Independent cash
    # data never determines which installment, revision or declaration to use.
    a, old, new = (forms[seq] for seq in ('613044', '613045', '700514'))
    for form in (a, old, new):
        _require_equal((form['approval_date'], form['record_date'], form['fiscal_year'], form['period']),
                       ('2023-04-20', '2023-04-20', '2022', 'anual'), 'native_entitlement_period_mismatch')
    for form, seq in ((old, '613025'), (new, '700211')):
        _require_equal(form['native_protocol'], _notice_protocol(documents[seq]), 'native_notice_protocol_link_mismatch')
    _require_equal(Decimal(a['gross_total_decimal']), Decimal('0.1485691041'), 'minimum_amount_mismatch')
    _require_equal(Decimal(old['gross_total_decimal']), Decimal('0.3316423104'), 'old_additional_amount_mismatch')
    _require_equal(Decimal(new['gross_total_decimal']), Decimal('0.3316423104'), 'new_additional_amount_mismatch')
    _require_equal([a['installments'][0]['payment_date'], old['installments'][0]['payment_date'],
                    new['installments'][0]['payment_date']],
                   ['2023-05-04', '2023-12-31', '2023-12-27'], 'native_payment_revision_mismatch')
    return {'reader': reader, 'audit': audit, 'audit_source': audit_source, 'documents': documents,
            'coverage': coverage, 'reviews': reviews, 'forms': forms}


def verified_calendar(folder, stock, cutoff=CUTOFF):
    if cutoff != CUTOFF:
        raise ValueError('exact_research_cutoff_required')
    ticker = stock.get('ticker')
    if ticker != 'AFLT3':
        raise ValueError('primary_revision_body_unavailable_no_patch:' + str(ticker))
    target = TARGETS[ticker]
    if any(stock.get(k) != target[k] for k in ('cvm', 'share_class', 'isin')):
        raise ValueError('exact_security_identity_required')
    ev = evidence(folder)
    sources = []
    for seq in ('613022', '613025', '700211', '613044', '613045', '700514'):
        fact = ev['reviews'].get(seq) or ev['forms'][seq]
        source = fact['source']
        sources.append({**source, 'id': 'primary-cash-exact:' + source['sha256'],
            'authority': 'primary_CVM_original_not_settlement',
            'observed_at': datetime.fromtimestamp((Path(folder) / source['file']).stat().st_mtime,
                                                 timezone.utc).isoformat(),
            'observed_at_basis': 'local_file_mtime_not_network_retrieval',
            'settlement_verified': False, 'point_in_time_archive_verified': False})
    rows = [('minimum', '0.1485691041', '2023-05-04', '613022'),
            ('additional_immediate', '0.1140650017', '2023-05-04', '613022'),
            ('additional_deferred', '0.3316423104', '2023-12-27', '700211')]
    events = []
    for component, amount, payment, seq in rows:
        source = ev['reviews'][seq]['source']
        events.append({'ticker': ticker, 'cvm': target['cvm'], 'isin': target['isin'],
            'share_class': target['share_class'], 'record_date': target['record_date'],
            'complete_group_id': 'AFLT3:2023-04-20', 'target_group_complete': True,
            'review_manifest_sha256': REVIEW_SHA, 'verified_by_exact_cash_loader': True,
            'approval_date': '2023-04-20', 'label': 'DIVIDENDO', 'component': component,
            'reported_per_share_decimal': amount, 'gross_per_share_decimal': amount,
            'amount_tax_basis': 'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows',
            'gross_reconstructed_from_net': False, 'share_basis': 'reported_unverified',
            'payment_date': payment, 'published_at': ev['documents'][seq]['published_at'],
            'available_as_of': True, 'status': 'past_scheduled',
            'payment_date_basis': 'explicit_payment_start_date_not_settlement',
            'full_settlement_on_date_verified': False, 'payment_claim_basis': 'primary_declared_start_not_settled_cash',
            'source_ref': 'primary-cash-exact:' + source['sha256'],
            'source_locator': {'format': 'exact_reviewed_native_entitlement_group',
                'sequence': seq, 'page': 1, 'field_spans': ev['reviews'][seq]['spans']},
            'primary_declaration_id': 'primary-cash-exact:AFLT3:2023-04-20:' + component,
            'issues': [], 'cash_model_eligible': False, 'is_settlement_confirmation': False,
            'point_in_time_archive_verified': False})
    return {'ticker': ticker, 'cvm': target['cvm'], 'share_class': target['share_class'], 'isin': target['isin'],
        'as_of': CUTOFF.isoformat(), 'events': events, 'sources': sources,
        'evidence_sources': deepcopy(list(ev['reader'].sources.values())),
        'review_manifest_sha256': REVIEW_SHA, 'index_manifest_sha256': IPE_MANIFEST_SHA,
        'verified_by_exact_cash_loader': True,
        'manifest_pins': {
            'review': {'file': CACHE + '/reviewed-notices.json', 'sha256': REVIEW_SHA},
            'CVM_IPE_indices': {'file': 'raw/primary-research/cvm-documents/index-sources.json',
                                'sha256': IPE_MANIFEST_SHA},
            'staged_selection': {'file': SNAPSHOT + '/candidate-source-audit.json', 'sha256': AUDIT_SHA}},
        'complete_groups': [{'id': 'AFLT3:2023-04-20', 'record_date': target['record_date'],
            'ticker': ticker, 'cvm': target['cvm'], 'share_class': target['share_class'], 'isin': target['isin'],
            'component_count': 3, 'installment_count': 3, 'payment_date_count': 2,
            'gross_total_decimal': '0.5942764162', 'target_group_complete': True,
            'events': deepcopy(events), 'source_chain': {
                'initial_two_components': '613022', 'deferred_third_component': '613025',
                'deferred_payment_start_update': '700211', 'initial_native_forms': ['613044', '613045'],
                'updated_native_form': '700514', 'updated_native_protocol': '1175485',
                'superseded_payment_start': '2023-12-31', 'current_payment_start': '2023-12-27',
                'basis': 'same_issuer_class_record_approval_fiscal_period_amount_and_exact_native_notice_protocol'}}],
        'history_complete': False, 'active_model_changed': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False}


def apply_verified_patch(folder, stock, cutoff, original):
    """Re-verify original bytes, then replace one complete group in memory only."""
    from .cash_calendar_research import augment_payment_history
    calendar = verified_calendar(folder, stock, cutoff)
    record = TARGETS[stock['ticker']]['record_date']
    old = [deepcopy(row) for row in original.installments if row['record_date'] == record]
    if not old or any(row.get('ticker') != stock['ticker'] for row in old):
        raise ValueError('original_target_group_identity_required')
    result = deepcopy(original)
    result.installments = [row for row in result.installments if row['record_date'] != record]
    result.semantic_events = [row for row in result.semantic_events if row['record_date'] != record]
    result.event_count -= len(old)
    result = augment_payment_history(result, calendar)
    result.research_group_corrections = deepcopy(getattr(original, 'research_group_corrections', [])) + [{
        'record_date': record, 'old_calendar_rows': old, 'new_primary_rows': deepcopy(calendar['events']),
        'complete_group': deepcopy(calendar['complete_groups'][0]), 'settlement_verified': False,
        'old_source_conflict_preserved_in_audit': True,
        'reason': 'exact_primary_full_group_with_explicit_payment_start_revision',
        'unchanged_parent_reconciliation_required': True}]
    return result


def build_report(folder):
    before = protected_state(folder)
    ev = evidence(folder)
    reader, docs = ev['reader'], ev['documents']
    blocked_sources = {}
    for seq in ('741697', '741696', '509579', '613007'):
        body, source = document_body(reader, docs[seq])
        if body is None or body.lstrip().startswith(b'%PDF-'):
            raise ValueError('blocked_source_state_changed_research_review_required:' + seq)
        from lxml import html
        tree = html.fromstring(body)
        visible = ' '.join(tree.xpath('//body//text()[not(ancestor::script) and not(ancestor::style)]')).strip()
        if visible:
            raise ValueError('download_response_has_unreviewed_visible_content:' + seq)
        blocked_sources[seq] = {**source, 'index_evidence': docs[seq]['index_evidence'],
            'classification': 'HTTP_200_empty_ENET_download_shell_no_document_body',
            'not_a_denial_or_zero_distribution': True, 'no_retry_or_alternate_path_attempted': True}
    pn = ev['forms']['742120']
    report = {'schema_version': 1, 'snapshot': SNAPSHOT, 'audit_source': ev['audit_source'],
        'as_of': CUTOFF.isoformat(), 'selection': 'structured_selective_from_exact_pinned_pending_audit',
        'selected': list(TARGETS), 'index_coverage': ev['coverage'],
        'results': [
            {'ticker': 'AFLT3', 'status': 'verified_complete_group_patch_available_in_memory_only',
             'calendar': verified_calendar(folder, {'ticker': 'AFLT3', **TARGETS['AFLT3']}),
             'optional_assembly_body_unavailable': blocked_sources['613007'],
             'optional_assembly_not_used_for_missing_facts': True},
            {'ticker': 'PNVL3', 'status': 'blocked_no_patch',
             'reason': 'revised_notice_and_revised_board_bodies_not_delivered',
             'exact_missing_sequences': ['741697', '741696'],
             'native_v2_schedule_observed': pn,
             'original_notice': ev['reviews']['739895'],
             'original_notice_installment_sum_decimal': '0.0773869',
             'original_notice_stated_total_decimal': '0.07792434',
             'secondary_installment_sum_decimal': '0.077386',
             'native_v2_gross_total_decimal': '0.07792434000',
             'diagnosis': 'Native v2 maps two gross installments to the single explicit ISIN. The original notice has an inconsistent second installment; revised notice and board v2 were indexed but returned empty shells. Their type/class/revision text cannot be checked. No substitution from v1, gross-up, or first-installment-only patch.',
             'required_to_unblock': 'Readable exact indexed 741697 and 741696 bodies, or an explicit primary issuer correction proving the same full entitlement and both gross installments.'},
            {'ticker': 'EZTC3', 'status': 'blocked_no_patch',
             'reason': 'later_indexed_cash_notice_body_not_delivered', 'exact_missing_sequences': ['509579'],
             'base_notice': ev['reviews']['505848'], 'intermediate_revision': ev['reviews']['509117'],
             'base_amount_decimal': '0.112467632', 'intermediate_amount_decimal': '0.11256951000',
             'cached_independent_amount_decimal': '0.11269711721',
             'diagnosis': '23-May notice explicitly updates the 12-May dividend. A later 25-May cash notice is indexed but its body is absent. Matching the intermediate value within existing tolerance would not establish the final revision. No older-readable fallback.',
             'required_to_unblock': 'Readable exact indexed 509579 body or an explicit primary issuer copy proving the final value and inheritance of the 17-May record and 31-May payment schedule.'}],
        'unavailable_originals': blocked_sources, 'source_receipts': list(reader.sources.values()),
        'network': {'budget': MAX_REQUESTS, 'attempts': len(Fetcher(folder).ledger('request')),
            'responses': Fetcher(folder).ledger('response'), 'proxies': False, 'redirects': False, 'retries': 0},
        'known_independent_conflicts_not_repaired': ['BEES3/4:2022-12-29_vs_2022-12-30', 'ALOS3:January_7_2024_vs_2025'],
        'thresholds_changed': False, 'active_model_changed': False, 'pointers_written': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False,
        'protected_files_before': before}
    _require_equal(protected_state(folder), before, 'protected_production_inputs_changed_during_research')
    report['protected_files_unchanged_during_build'] = True
    return report


def trial_source_gate(folder):
    """Explicit dry-run adapter scope; all production gates remain unchanged."""
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from .market_inventory import load_inventory
    inventory = load_inventory(folder)
    stock = next(s for s in inventory['stocks'] if s['ticker'] == 'AFLT3')
    original_reader = candidates._payments

    def reader(ticker, cache, cutoff, primary=None, *, coverage_start=None):
        original, url = original_reader(ticker, cache, cutoff, primary, coverage_start=coverage_start)
        if ticker != 'AFLT3':
            raise ValueError('dry_run_adapter_scope_violation')
        return apply_verified_patch(cache.folder, stock, cutoff, original), url

    before = protected_state(folder)
    with patch.object(candidates, '_payments', reader):
        accepted, pending, sources = candidates.build_candidates(
            folder, {**inventory, 'stocks': [stock]}, CUTOFF)
    _require_equal(protected_state(folder), before, 'production_inputs_changed_during_dry_run')
    if pending or len(accepted) != 1:
        return {'status': 'source_gate_pending', 'pending': pending, 'thresholds_changed': False}
    row = accepted[0]
    return {'status': 'unchanged_candidate_source_gates_passed', 'ticker': 'AFLT3',
        'independent_cash_matches': len(row['independent_cash_matches']),
        'independent_price_matches': len(row['independent_price_matches']),
        'target_ledger': [r for r in row['ledger'] if r['record_date'] == '2023-04-20'],
        'readiness_scope': row['readiness_scope'], 'financially_validated': row['financially_validated'],
        'source_receipts': sources, 'thresholds_changed': False, 'production_integration_applied': False,
        'protected_files_unchanged': True, 'settlement_verified': False}


def render_sources(folder):
    """Render every page used by the review, strictly inside the owned raw folder."""
    import pypdfium2 as pdfium
    reader = shared.Reader(folder)
    documents = {d['sequence']: d for d in indexed_documents(reader)[0]}
    for seq in ('613022', '613025', '700211', '613044', '613045', '700514', '739895', '742120', '505848', '509117'):
        body, _ = document_body(reader, documents[seq])
        pdf = pdfium.PdfDocument(body)
        for index in range(len(pdf)):
            page = pdf[index]
            bitmap = page.render(scale=1.25)
            output = io.BytesIO()
            bitmap.to_pil().save(output, format='PNG')
            save(folder, f'page-{seq}-{index + 1}.png', output.getvalue())
            bitmap.close()
            page.close()
        pdf.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', nargs='+', default=[])
    parser.add_argument('--inspect', nargs='+', default=[])
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--trial', action='store_true')
    args = parser.parse_args()
    reader = shared.Reader(args.folder)
    pinned_audit(reader)
    documents, coverage = indexed_documents(reader)
    selected = {d['sequence']: d for d in documents}
    if args.report:
        report = build_report(args.folder)
        if args.trial:
            report['source_gate_dry_run'] = trial_source_gate(args.folder)
        output = save_json(args.folder, 'report', report)
        print(json.dumps({'report': output, 'results': [{k: r[k] for k in ('ticker', 'status')} for r in report['results']]}))
    if args.render:
        render_sources(args.folder)
    if args.acquire:
        fetcher = Fetcher(args.folder)
        for sequence in args.acquire:
            result = fetcher.acquire(selected[sequence])
            print(json.dumps({k: result.get(k) for k in ('status', 'http_status', 'pdf_signature', 'access_stop', 'sequence')}), flush=True)
            if result.get('access_stop'):
                break
    for sequence in args.inspect:
        document = selected[sequence]
        body, source = document_body(reader, document)
        print(json.dumps({'document': document, 'source': source}, ensure_ascii=True), flush=True)
        print(json.dumps(pdf_pages(body) if body and body.lstrip().startswith(b'%PDF-')
                         else {'status': 'original_not_PDF_no_fallback', 'bytes': len(body or b'')}, ensure_ascii=True, indent=2), flush=True)
    if args.trial and not args.report:
        trial = trial_source_gate(args.folder)
        print(json.dumps({'trial': save_json(args.folder, 'trial', trial), 'status': trial['status']}))
    if not any((args.acquire, args.inspect, args.report, args.render, args.trial)):
        shortlist = [d for d in documents if any(t['cvm'] == d['cvm']
            and t['period'][0] <= d['published_at'] <= t['period'][1] for t in TARGETS.values())]
        print(json.dumps(shortlist, ensure_ascii=True, indent=2))
