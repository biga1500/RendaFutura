"""Isolated PINE4/ALOS3 cash revisions. Offline by default; no parent writes.

Only exact indexed CVM originals are acquired, directly, without redirects,
proxies or retries. The persistent twenty-request budget includes robots.txt.
Source chronology and explicit revision language determine precedence; market
amounts are diagnostic inputs only. Schedules never prove settlement or PIT.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser

from . import alternative_cash_research as shared
from . import cvm_proventos_documents as cvm


CACHE = 'raw/primary-research/revised-cash'
MAX_REQUESTS = 20
MAX_BYTES = 12_000_000
CUTOFF = '2026-09-30'
REVIEW_SHA256 = 'b10efa2a84ca82396f2205ac4d29be82bec46d44b745fb41812c877f9058bf92'
ROBOT_URL = shared.ROBOT_URL
TARGETS = {
    'PINE4': {'cvm': 20567, 'share_class': 'PN', 'isin': 'BRPINEACNPR8',
              'cnpj': '62144175000120', 'record_date': '2025-02-19', 'period_end': '2024-12-31'},
    'ALOS3': {'cvm': 22357, 'share_class': 'ON', 'isin': 'BRALOSACNOR5',
              'cnpj': '05878397000132', 'record_date': '2024-12-20', 'period_end': '2024-06-30'},
}


def save(folder, prefix, payload):
    body = shared._dump(payload)
    return save_body(folder, f'{prefix}-{shared._sha(body)}.json', body)


def save_body(folder, name, body):
    path = shared._safe(Path(folder).resolve(), CACHE + '/' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(Path(folder).resolve()).as_posix()


def indexed_documents(reader):
    stocks = [{'ticker': ticker, **target} for ticker, target in TARGETS.items()]
    documents, periods, diagnostics = shared.scan_indices(reader, stocks)
    if diagnostics:
        raise ValueError('native_IPE_scan_incomplete:' + str(diagnostics))
    selected = []
    for document in documents:
        record = TARGETS[document['ticker']]['record_date']
        reference = document['reference_date']
        if reference and abs((date.fromisoformat(reference) - date.fromisoformat(record)).days) <= 40:
            selected.append(document)
    return selected, periods


class Fetcher:
    """Persistent bounded acquisition, including failed attempts and access stops."""

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), shared._NoRedirect())

    def ledger(self, prefix):
        reader = shared.Reader(self.folder)
        return [reader.json(path.relative_to(self.folder).as_posix(), 'network_receipt')[0]
                for path in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def _get(self, url):
        if not shared._approved(url):
            raise ValueError('unapproved_URL_no_request_sent')
        requests, responses = self.ledger('request'), self.ledger('response')
        if any(row.get('http_status') in (401, 403, 429) for row in responses):
            raise ValueError('access_stop_no_request_sent')
        if any(row['url'] == url for row in requests):
            raise ValueError('already_attempted_no_retry')
        if len(requests) >= MAX_REQUESTS:
            raise ValueError('request_budget_exhausted')
        if url != ROBOT_URL and cvm.document_id(url)[0] == '863440':
            raise ValueError('known_non_PDF_863440_no_retry')
        attempt = {'url': url, 'ordinal': len(requests) + 1,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'proxy': False, 'redirects': False, 'retries': 0}
        # Exclusive ordinal reservation prevents concurrent callers sharing a budget slot.
        reservation = shared._safe(self.folder, f'{CACHE}/request-{len(requests) + 1:02d}.json')
        reservation.parent.mkdir(parents=True, exist_ok=True)
        with reservation.open('xb') as stream:
            stream.write(shared._dump(attempt))
        result = {**attempt, 'status': 'failed', 'http_status': None}
        body = b''
        try:
            req = urllib.request.Request(url, headers={'User-Agent': shared.USER_AGENT,
                'Accept': 'text/plain' if url == ROBOT_URL else 'application/pdf',
                'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
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
        digest = shared._sha(body)
        result.update(sha256=digest, bytes=len(body), retrieved_at=datetime.now(timezone.utc).isoformat())
        result['file'] = save_body(self.folder, f'body-{digest}.bin', body)
        save(self.folder, 'response', result)
        return body, result

    def acquire(self, document):
        documents, _ = indexed_documents(shared.Reader(self.folder))
        if document not in documents:
            raise ValueError('exact_native_index_document_required')
        if document['sequence'] == '863440':
            raise ValueError('known_non_PDF_863440_no_retry')
        cached, _ = document_body(shared.Reader(self.folder), document)
        if cached is not None:
            raise ValueError('already_cached_no_new_request')
        robots = next((r for r in self.ledger('response') if r['url'] == ROBOT_URL), None)
        if robots is None:
            body, robots = self._get(ROBOT_URL)
        else:
            body, _ = shared.Reader(self.folder).read(robots['file'], 'robots', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(ROBOT_URL)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(shared.USER_AGENT, document['url']):
                raise ValueError('robots_disallow_no_document_request_sent')
            if parser.crawl_delay(shared.USER_AGENT) or parser.request_rate(shared.USER_AGENT):
                raise ValueError('robots_rate_requires_manual_spacing')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_access_denied')
        body, receipt = self._get(document['url'])
        receipt = {**receipt, 'document': document,
                   'pdf_signature_verified': body.lstrip().startswith(b'%PDF-')}
        save(self.folder, 'document', receipt)
        return receipt


def document_body(reader, document):
    body, receipt = shared._document_body(reader, document)
    candidates = [] if body is None else [(body, receipt)]
    for path in (reader.folder / CACHE).glob('document-*.json'):
        source, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'acquisition_receipt')
        if source['url'] == document['url'] and source['status'] == 'received':
            candidates.append(reader.read(source['file'], 'primary_CVM_original', source['sha256'], source['url']))
    if len({shared._sha(body) for body, _ in candidates}) > 1:
        raise ValueError('same_document_changed_original_bytes')
    return candidates[0] if candidates else (None, None)


def pdf_pages(body):
    from pypdf import PdfReader
    if not body.lstrip().startswith(b'%PDF-') or len(body) > MAX_BYTES:
        raise ValueError('original_response_not_PDF')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 12:
        raise ValueError('unsupported_original_PDF')
    return [page.extract_text() or '' for page in pdf.pages]


def native_observation(body, document, source, stock):
    """Retain actual records and periods, even when the IPE reference differs."""
    import pdfplumber
    pdf_pages(body)
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [table for page in pdf.pages for table in page.extract_tables()]
    identities = {int(match) for table in tables for row in table for cell in row
                  for match in re.findall(r'\b(\d{6})IPE', str(cell or ''))}
    if identities != {stock['cvm']} or document['cvm'] != stock['cvm']:
        raise ValueError('native_CVM_identity_mismatch')
    if cvm.compact(cvm.metadata_value(tables, 'versao')) != str(document['version']):
        raise ValueError('native_version_mismatch')
    parsed = cvm.parse_tables(tables, document, date.fromisoformat(CUTOFF))
    choices = [event for event in parsed['events'] if event['isin'] == stock['isin']]
    if len(choices) != 1:
        raise ValueError('exact_native_ISIN_row_required')
    event = choices[0]
    return {**event, 'ticker': stock['ticker'], 'share_class': stock['share_class'],
        'source': source, 'index_evidence': document['index_evidence'],
        'native_protocol': cvm.compact(cvm.metadata_value(tables, 'protocolo provento')),
        'raw_record_date': cvm.metadata_value(tables, 'ultimo dia de negociacao com direitos'),
        'raw_sent_date': cvm.metadata_value(tables, 'data envio'),
        'record_matches_target': event['record_date'] == stock['record_date'],
        'reference_date_is_not_record_date': True,
        'cash_model_eligible': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False}


def _quote_dates(quote, locale):
    values = shared._dates_in_quote(quote)
    pattern = '%m/%d/%Y' if locale == 'en-US' else '%d/%m/%Y'
    for token in re.findall(r'\b\d{2}[/\.]\d{2}[/\.]\d{4}\b', quote):
        try:
            values.add(datetime.strptime(token.replace('.', '/'), pattern).date().isoformat())
        except ValueError:
            pass
    return values


def reviewed_facts(pages, review, document, source, stock):
    """Validate field spans against hashed originals; preserve source date errors."""
    indexed = document['index_evidence']['raw_row']
    if (review['sha256'] != source['sha256'] or review['sequence'] != document['sequence']
            or review['version'] != document['version'] or document['cvm'] != stock['cvm']
            or int(indexed['Codigo_CVM']) != stock['cvm']
            or re.sub(r'\D', '', indexed['CNPJ_Companhia']) != stock['cnpj']
            or cvm.document_id(document['url']) != (document['sequence'], document['version'])):
        raise ValueError('review_original_index_identity_mismatch')
    allowed = {'record_date', 'payment_date', 'ex_date', 'approval_date', 'period_end',
               'document_date', 'revision_of_publication', 'amount', 'previous_amount',
               'share_class', 'label', 'revision_language', 'period_language', 'portuguese_record_date'}
    fields = {}
    for field, proof in review['fields'].items():
        if field not in allowed or type(proof['page']) is not int or not 1 <= proof['page'] <= len(pages):
            raise ValueError('invalid_review_field_or_page')
        quote, value = proof['quote'], proof['value']
        normalized = cvm.norm(quote)
        if not normalized or normalized not in cvm.norm(pages[proof['page'] - 1]):
            raise ValueError('review_quote_not_in_original')
        if field.endswith('_date') or field in ('period_end', 'revision_of_publication'):
            if not shared._day(value) or value not in _quote_dates(quote, proof.get('locale')):
                raise ValueError('review_date_not_in_quote')
            if proof.get('locale') == 'en-US' and field in ('record_date', 'payment_date'):
                row = re.search(r'\bIOE\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})', quote)
                column = 1 if field == 'record_date' else 3
                if not row or datetime.strptime(row[column], '%m/%d/%Y').date().isoformat() != value:
                    raise ValueError('cash_table_date_column_mismatch')
        if field == 'record_date' and not any(term in normalized for term in ('registros', 'acionaria', 'cut off')):
            raise ValueError('record_context_not_explicit')
        if field == 'payment_date' and (not any(term in normalized for term in ('pagamento', 'pagos', 'credito', 'payment'))
                or any(term in normalized for term in ('ex dividend', 'ex juros', 'data ex', 'ate o', 'prazo'))):
            raise ValueError('payment_context_not_explicit')
        if field in ('amount', 'previous_amount'):
            numbers = {Decimal(token.replace(',', '.')) for token in
                       re.findall(r'(?<![\d.,])\d+[,.]\d+(?!\d|[.,]\d)', quote)}
            if shared._decimal(value) is None or Decimal(value) not in numbers:
                raise ValueError('amount_not_explicit_in_quote')
            if 'liquido' in normalized and 'bruto' not in normalized:
                raise ValueError('net_amount_cannot_become_gross')
        if field == 'label' and not (value == 'JCP' and 'juros sobre' in normalized
                                    or value == 'DIVIDENDO' and 'dividendos' in normalized):
            raise ValueError('cash_label_not_explicit')
        if field == 'share_class' and not (value == 'ON' and ('ordinaria' in normalized or 'common share' in normalized)
                                          or value == 'PN' and 'preferencial' in normalized):
            raise ValueError('share_class_not_explicit')
        if field == 'share_class' and value != stock['share_class']:
            raise ValueError('review_share_class_identity_mismatch')
        if field == 'revision_language' and not any(term in normalized for term in ('ajustados', 'ajustado', 'passou a ser')):
            raise ValueError('explicit_revision_language_required')
        fields[field] = value
    return {'sequence': document['sequence'], 'version': document['version'],
        'published_at': document['published_at'], 'fields': fields,
        'source': {**source, 'url': document['url'], 'index_evidence': document['index_evidence']},
        'field_spans': review['fields'], 'notes': review.get('notes', [])}


def audit_chain(base, amendment, *, corroboration=None):
    """An explicit chain, never nearest-amount or highest-version selection.

    The amended source must identify the base publication or approval and the
    cash type. Missing class/ISIN on prose is a labelled inherited mapping, not
    a claim that the prose contains those identifiers.
    """
    target = TARGETS.get(base.get('ticker'))
    if target is None or any(base.get(k) != target[k] for k in target):
        raise ValueError('base_exact_security_record_and_period_required')
    if (not shared._day(base.get('payment_date')) or base['payment_date'] < base['record_date']
            or not shared._day(base.get('approval_date')) or shared._decimal(base.get('amount')) is None):
        raise ValueError('invalid_base_schedule')
    fields = amendment['fields']
    if not fields.get('revision_language') or fields.get('label') != base['label']:
        raise ValueError('explicit_same_cash_type_revision_required')
    if amendment['published_at'] <= base['published_at']:
        raise ValueError('revision_not_after_base_publication')
    linked = (fields.get('revision_of_publication') is not None
              and fields['revision_of_publication'] == base['published_at']
              or fields.get('approval_date') is not None and fields['approval_date'] == base['approval_date'])
    if not linked:
        raise ValueError('revision_base_publication_or_approval_link_required')
    if fields.get('previous_amount') is not None and Decimal(fields['previous_amount']) != Decimal(base['amount']):
        raise ValueError('revision_previous_amount_mismatch')
    for field in ('cvm', 'isin', 'share_class', 'record_date', 'period_end'):
        value = fields.get(field)
        if value is not None and value != base[field]:
            raise ValueError('revision_identity_or_period_mismatch:' + field)
    evidence = amendment['source']['index_evidence']['raw_row']
    if (int(evidence['Codigo_CVM']) != base['cvm']
            or re.sub(r'\D', '', evidence['CNPJ_Companhia']) != base['cnpj']):
        raise ValueError('revision_index_issuer_mismatch')
    if shared._decimal(fields.get('amount')) is None:
        raise ValueError('revision_amount_required')
    conflicts, warnings = [], []
    amendment_payment = fields.get('payment_date')
    if amendment_payment and amendment_payment != base['payment_date']:
        conflicts.append({'code': 'primary_payment_date_conflict',
            'base_payment_date': base['payment_date'], 'amendment_payment_date': amendment_payment,
            'amendment_payment_before_record': amendment_payment < base['record_date'],
            'source_values_not_repaired': True})
    if fields.get('document_date') and fields['document_date'] < base['approval_date']:
        warnings.append({'code': 'amendment_body_date_precedes_cited_approval',
            'raw_document_date': fields['document_date'], 'index_delivery': amendment['published_at'],
            'no_body_date_repair': True})
        if corroboration is None:
            conflicts.append({'code': 'dated_board_corroboration_required'})
    if corroboration is not None:
        facts = corroboration['fields']
        raw = corroboration['source']['index_evidence']['raw_row']
        if (facts.get('amount') != fields['amount'] or facts.get('previous_amount') != base['amount']
                or facts.get('approval_date') != base['approval_date']
                or facts.get('period_end') != base['period_end']
                or facts.get('share_class') != base['share_class'] or facts.get('label') != base['label']
                or int(raw['Codigo_CVM']) != base['cvm']
                or re.sub(r'\D', '', raw['CNPJ_Companhia']) != base['cnpj']
                or not facts.get('document_date') or facts['document_date'] != corroboration['published_at']):
            raise ValueError('board_revision_chain_mismatch')
    return {'ticker': base['ticker'], 'cvm': base['cvm'], 'isin': base['isin'],
        'share_class': base['share_class'], 'record_date': base['record_date'],
        'period_end': base['period_end'], 'label': base['label'],
        'gross_per_share_decimal': fields['amount'],
        'amount_tax_basis': 'explicit_gross_base_preserved_by_explicit_revision',
        'previous_gross_per_share_decimal': base['amount'],
        'amount_revision_verified': True,
        'payment_date': None if conflicts else amendment_payment or base['payment_date'],
        'base_schedule_payment_date': base['payment_date'],
        'ex_date': base.get('ex_date'), 'published_at': amendment['published_at'],
        'target_schedule_audited': not conflicts, 'history_complete': False,
        'status': 'remaining_primary_conflict' if conflicts else 'audited_target_revision',
        'selection_basis': 'explicit_primary_revision_chain_and_dated_board_not_market_amount',
        'identity_basis': base['identity_basis'],
        'amendment_ISIN_present': False, 'amendment_record_present': 'record_date' in fields,
        'amendment_mapping': 'inherited_from_explicitly_linked_base_not_reported_in_amendment',
        'base': base, 'amendment': amendment, 'corroboration': corroboration,
        'remaining_conflicts': conflicts, 'source_anomalies': warnings,
        'cash_model_eligible': False, 'active_model_changed': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False,
        'gross_reconstructed_from_net': False, 'independent_gate_run': False}


def identities(reader):
    from .security_registration import parse_fca_registry
    pointer, _ = reader.json('inventory/current.json', 'inventory_pointer_read_only')
    inventory, receipt = reader.json('inventory/' + pointer['snapshot'] + '/market.json', 'inventory_identity')
    if receipt['sha256'] != pointer['sha256']:
        raise ValueError('inventory_hash_mismatch')
    choices = [s for s in inventory['sources'] if s.get('url') ==
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip']
    if len(choices) != 1:
        raise ValueError('exact_FCA_source_required')
    source = choices[0]
    body, fca_receipt = reader.read(source['file'], 'primary_CVM_FCA_identity', source['sha256'], source['url'])
    masters, securities = parse_fca_registry(body, 2026)
    result = {}
    for ticker, target in TARGETS.items():
        stocks = [s for s in inventory['stocks'] if s['ticker'] == ticker]
        if len(stocks) != 1 or any(stocks[0].get(k) != target[k] for k in ('cvm', 'isin', 'share_class')):
            raise ValueError('inventory_exact_identity_required')
        rows = [r for r in securities if r['Codigo_Negociacao'] == ticker
                and re.sub(r'\D', '', r['CNPJ_Companhia']) == target['cnpj']]
        if len(rows) != 1:
            raise ValueError('exact_FCA_ticker_and_CNPJ_required')
        row = rows[0]
        filing = [r for r in masters if r['ID_DOC'] == row['ID_Documento']]
        expected_type = 'acoes ordinarias' if target['share_class'] == 'ON' else 'acoes preferenciais'
        if (len(filing) != 1 or int(filing[0]['CD_CVM']) != target['cvm']
                or filing[0]['DT_RECEB'] > CUTOFF or cvm.norm(row['Valor_Mobiliario']) != expected_type
                or row['Data_Inicio_Negociacao'] > target['record_date']
                or row['Data_Fim_Negociacao'] and row['Data_Fim_Negociacao'] < target['record_date']):
            raise ValueError('FCA_CVM_class_or_episode_mismatch')
        detail, cached = reader.json(f"raw/expanded/b3-detail-{target['cvm']}.json", 'existing_B3_identity_read_only')
        codes = [r for r in detail['otherCodes'] if r['code'] == ticker]
        if (str(detail['codeCVM']) != str(target['cvm']) or detail['cnpj'] != target['cnpj']
                or len(codes) != 1 or codes[0]['isin'] != target['isin']):
            raise ValueError('cached_exact_ISIN_mapping_mismatch')
        result[ticker] = {'primary_FCA': fca_receipt, 'FCA_master_row': filing[0],
            'FCA_security_row': row, 'cached_ISIN_mapping': cached,
            'ISIN_not_reported_in_FCA': True, 'contemporaneous_identity_archive_verified': False}
    return result


def verified_pine_calendar(folder, stock, cutoff):
    """Replay three pinned originals read-only; no calendar/model approval."""
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    empty = {'ticker': stock.get('ticker'), 'cvm': stock.get('cvm'), 'share_class': stock.get('share_class'),
             'as_of': cutoff.isoformat(), 'events': [], 'sources': [], 'diagnostics': [], 'history_complete': False}
    if stock.get('ticker') != 'PINE4' or cutoff != date.fromisoformat(CUTOFF):
        return empty
    target = TARGETS['PINE4']
    if any(stock.get(key) != target[key] for key in ('cvm', 'share_class', 'isin')):
        raise ValueError('revision_calendar_exact_identity_required')
    reader = shared.Reader(folder)
    reviews, receipt = reader.json(CACHE + '/reviewed-notices.json', 'reviewed_original_field_spans')
    if receipt['sha256'] != REVIEW_SHA256:
        raise ValueError('revision_calendar_review_hash_mismatch')
    identity = identities(reader)['PINE4']
    documents, _ = indexed_documents(reader)
    indexed = {row['sequence']: row for row in documents if row['ticker'] == 'PINE4'}
    readings = {row['sequence']: row for row in reviews['documents']}
    facts, native = {}, None
    for sequence in ('858761', '863361', '863360'):
        document = indexed[sequence]
        if date.fromisoformat(document['published_at']) > cutoff:
            raise ValueError('revision_calendar_publication_after_cutoff')
        body, source = document_body(reader, document)
        if body is None:
            raise ValueError('revision_calendar_original_missing')
        if sequence == '858761':
            native = native_observation(body, document, source, {'ticker': 'PINE4', **target})
        else:
            facts[sequence] = reviewed_facts(pdf_pages(body), readings[sequence], document, source,
                                             {'ticker': 'PINE4', **target})
    if (native['issues'] or native['record_date'] != target['record_date']
            or native['fiscal_year'] != '2024' or cvm.compact(native['reference_period']).lower() != 'complementar'):
        raise ValueError('revision_calendar_original_period_or_record_mismatch')
    base = {'ticker': 'PINE4', **{key: target[key] for key in ('cvm', 'isin', 'share_class', 'cnpj')},
        'record_date': native['record_date'], 'payment_date': native['payment_date'],
        'approval_date': native['approval_date'], 'amount': str(Decimal(native['gross_per_unit_decimal']).normalize()),
        'period_end': target['period_end'], 'label': 'DIVIDENDO', 'published_at': native['published_at'],
        'identity_basis': identity, 'native_evidence': native}
    revision = audit_chain(base, facts['863361'], corroboration=facts['863360'])
    if not revision['target_schedule_audited'] or revision['remaining_conflicts']:
        raise ValueError('revision_calendar_primary_conflict')
    sources = [{**source, 'id': 'primary-revision:' + source['sha256'],
                'observed_at': datetime.fromtimestamp((reader.folder / source['file']).stat().st_mtime,
                                                       timezone.utc).isoformat(),
                'observed_at_basis': 'local_original_file_mtime_not_verified_network_retrieval',
                'authority': ('primary_CVM_explicit_revision_not_settlement'
                              if (source.get('url') or '').startswith(('https://www.rad.cvm.gov.br/', 'https://dados.cvm.gov.br/'))
                              else 'local_evidence_manifest_or_existing_registry_cache')}
               for source in reader.sources.values() if source['file'].startswith('raw/')]
    amendment = facts['863361']['source']
    source_ref = 'primary-revision:' + amendment['sha256']
    event = {'ticker': 'PINE4', 'cvm': target['cvm'], 'share_class': target['share_class'], 'isin': target['isin'],
        'record_date': revision['record_date'], 'payment_date': revision['payment_date'],
        'label': revision['label'], 'reported_per_share_decimal': revision['gross_per_share_decimal'],
        'gross_per_share_decimal': revision['gross_per_share_decimal'],
        'amount_tax_basis': revision['amount_tax_basis'], 'published_at': revision['published_at'],
        'available_as_of': True, 'approval_date': base['approval_date'], 'ex_date': revision['ex_date'],
        'status': 'past_scheduled', 'source_ref': source_ref,
        'primary_declaration_id': source_ref + ':2025-02-19',
        'source_locator': {'format': 'CVM_explicit_revision_chain',
            'base_sequence': '858761', 'amendment_sequence': '863361', 'corroboration_sequence': '863360'},
        'revision_chain_verified': True, 'revision_audit': revision,
        'is_settlement_confirmation': False, 'point_in_time_archive_verified': False, 'issues': []}
    return {**empty, 'events': [event], 'sources': sources, 'active_model_changed': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False}


def protected_state(folder):
    root = Path(folder).resolve()
    code = Path(__file__).parent
    paths = [root / 'current.json', root / 'inventory/current.json',
             *(code / name for name in ('cash_calendar_research.py', 'inventory_candidates.py',
                'market_inventory.py', 'prepare_inventory.py', 'collect_market_data.py'))]
    return {str(path): shared._sha(path.read_bytes()) for path in paths if path.is_file()}


def build_report(folder, *, audit=None):
    """Offline target audit. Only the returned content-addressed report is written."""
    protected_before = protected_state(folder)
    reader = shared.Reader(folder)
    identity = identities(reader)
    documents, periods = indexed_documents(reader)
    reviews, _ = reader.json(CACHE + '/reviewed-notices.json', 'reviewed_original_field_spans')
    facts, native, diagnostics = {}, {}, []
    reviewed = {r['sequence']: r for r in reviews['documents']}
    for document in documents:
        sequence = document['sequence']
        if sequence not in {*reviewed, '858761', '851744', '842850', '841736', '858516', '863440'}:
            continue
        stock = {'ticker': document['ticker'], **TARGETS[document['ticker']]}
        body, source = document_body(reader, document)
        if body is None:
            diagnostic = {'sequence': sequence, 'code': 'original_not_available_no_retry',
                          'url': document['url'], 'known_non_PDF': sequence == '863440'}
            for path in (reader.folder / 'raw/primary-research/cash-calendars').glob(f'cvm{sequence}v*-failure.json'):
                failure, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'previous_unusable_response_receipt')
                if failure.get('url') == document['url']:
                    failed, failed_source = reader.read(failure['file'], 'previous_non_PDF_original', failure['sha256'], failure['url'])
                    diagnostic.update(previous_receipt=failure, previous_original=failed_source,
                                      previous_signature_is_PDF=failed.lstrip().startswith(b'%PDF-'))
            diagnostics.append(diagnostic)
            continue
        try:
            pages = pdf_pages(body)
            if sequence in reviewed:
                facts[sequence] = reviewed_facts(pages, reviewed[sequence], document, source, stock)
            if document['category'] == 'relatorio proventos':
                native[sequence] = native_observation(body, document, source, stock)
        except ValueError as error:
            diagnostics.append({'sequence': sequence, 'code': str(error), 'source': source})
    missing = sorted(set(reviewed) - facts.keys())
    if missing or '858761' not in native:
        raise ValueError('required_primary_evidence_unusable:' + json.dumps({
            'missing_reviews': missing, 'diagnostics': diagnostics}, default=str))
    pine = native['858761']
    pine_base = {'ticker': 'PINE4', **{k: TARGETS['PINE4'][k] for k in ('cvm', 'isin', 'share_class', 'cnpj')},
        'record_date': pine['record_date'], 'payment_date': pine['payment_date'],
        'approval_date': pine['approval_date'], 'amount': pine['gross_per_unit_decimal'],
        'period_end': '2024-12-31', 'label': 'DIVIDENDO', 'published_at': pine['published_at'],
        'identity_basis': identity['PINE4'], 'native_evidence': pine}
    if (pine['record_date'] != TARGETS['PINE4']['record_date'] or pine['fiscal_year'] != '2024'
            or cvm.compact(pine['reference_period']).lower() != 'complementar'):
        raise ValueError('PINE_original_record_or_period_mismatch')
    # A numeric scale difference is not a source revision; normalize only Decimal equality.
    pine_base['amount'] = str(Decimal(pine_base['amount']).normalize())
    allos = facts['841501']
    allos_base = {'ticker': 'ALOS3', **{k: TARGETS['ALOS3'][k] for k in ('cvm', 'isin', 'share_class', 'cnpj')},
        **{k: allos['fields'][k] for k in ('record_date', 'payment_date', 'ex_date',
                                          'approval_date', 'period_end', 'amount', 'label')},
        'published_at': allos['published_at'], 'identity_basis': identity['ALOS3'],
        'board_evidence': allos}
    if allos_base['record_date'] != TARGETS['ALOS3']['record_date']:
        raise ValueError('ALOS_board_record_mismatch')
    original_notice = facts['841533']
    if any(original_notice['fields'][k] != allos_base[k] for k in ('amount', 'label', 'record_date', 'payment_date')):
        raise ValueError('ALOS_board_and_linked_notice_mismatch')
    allos_base['linked_notice'] = original_notice
    events = [audit_chain(pine_base, facts['863361'], corroboration=facts['863360']),
              audit_chain(allos_base, facts['842767'])]
    portuguese_record = original_notice['fields']['portuguese_record_date']
    if portuguese_record != allos_base['record_date']:
        events[1]['source_anomalies'].append({'code': 'base_notice_record_year_language_conflict',
            'portuguese_record_date': portuguese_record,
            'english_and_board_record_date': allos_base['record_date'],
            'no_printed_value_repaired': True,
            'evidence': original_notice['field_spans']})
    # Comparators enter only after source selection and are never passed to audit_chain.
    for event in events:
        ticker = event['ticker']
        calendar, _ = reader.json(f'raw/inventory/payments/{ticker}.json', 'existing_calendar_diagnostic_only')
        rows = [r for r in calendar['selected_cash_source']['events']
                if r.get('record_date') == event['record_date']]
        independent = []
        for path in sorted((reader.folder / 'raw/expanded').glob(ticker + '-b3-cash-*.json')):
            payload, receipt = reader.json(path.relative_to(reader.folder).as_posix(), 'existing_B3_cash_diagnostic_only')
            for index, row in enumerate(payload['results']):
                if row.get('typeStock') == event['share_class'] and cvm.day(row.get('lastDatePriorEx')) == event['record_date']:
                    independent.append({'source': receipt, 'row_index': index, 'raw_row': row})
        event['comparison_only'] = {'original_calendar_rows': rows,
            'original_calendar_sum_decimal': str(sum((Decimal(str(r['gross_per_share'])) for r in rows), Decimal(0))),
            'independent_rows': independent, 'selection_uses_comparator_values': False}
    fetcher = Fetcher(folder)
    responses = sorted(fetcher.ledger('response'), key=lambda row: row['ordinal'])
    for response in responses:
        reader.read(response['file'], 'acquisition_original_response', response['sha256'], response['url'])
    baseline = None
    if audit is not None:
        path = Path(audit)
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError('unsafe_baseline_audit')
        body = path.read_bytes()
        rows = json.loads(body)
        selected = [r for r in rows if r.get('ticker') in TARGETS]
        if Counter(r['ticker'] for r in selected) != Counter(TARGETS.keys()):
            raise ValueError('exact_two_baseline_audit_targets_required')
        for row in selected:
            target = TARGETS[row['ticker']]
            if (row['cvm'] != target['cvm'] or len(row['mismatches']) != 1
                    or row['mismatches'][0]['record_date'] != target['record_date']):
                raise ValueError('baseline_audit_scope_mismatch')
        baseline = {'file': str(path.resolve()), 'sha256': shared._sha(body), 'bytes': len(body),
                    'selected_rows': selected, 'role': 'comparison_context_only_after_primary_selection'}
    protected_after = protected_state(folder)
    if protected_before != protected_after:
        raise ValueError('parent_state_changed_during_audit_no_report_written')
    report = {'schema': 'isolated-revised-cash-v1', 'as_of': CUTOFF,
        'created_at': datetime.now(timezone.utc).isoformat(), 'target_scope': TARGETS,
        'baseline_audit': baseline,
        'events': events, 'native_observations': list(native.values()),
        'supporting_reviews': list(facts.values()), 'document_diagnostics': diagnostics,
        'native_IPE_index_periods': periods, 'index_candidates': documents,
        'version_policy': 'no_global_protocol_maximum_across_distinct_records_or_periods',
        'requests': {'new_HTTP_attempts': len(fetcher.ledger('request')), 'maximum': MAX_REQUESTS,
            'includes_robots': True, 'status_counts': dict(Counter(str(r['http_status']) for r in responses)),
            'attempts': responses, 'proxy_used': False, 'redirects_followed': False, 'retries': 0},
        'sources': list(reader.sources.values()),
        'protected_parent_files': {'before': protected_before, 'after': protected_after,
                                   'unchanged_during_execution': True},
        'remaining_conflicts': [{'ticker': e['ticker'], 'record_date': e['record_date'], **c}
            for e in events for c in e['remaining_conflicts']],
        'full_history_audited': False, 'cash_model_eligible': False, 'parent_pointers_changed': False,
        'pipeline_changed': False, 'active_model_changed': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}
    return save(folder, 'report', report), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', nargs='+', help='Exact indexed document sequence(s); opt-in network')
    parser.add_argument('--list', action='store_true', help='Offline exact native index candidates')
    parser.add_argument('--audit', type=Path, help='Optional original mismatch audit; comparison only')
    args = parser.parse_args()
    reader = shared.Reader(args.data_dir)
    documents, periods = indexed_documents(reader)
    if args.acquire:
        fetcher = Fetcher(args.data_dir)
        for sequence in args.acquire:
            choices = [row for row in documents if row['sequence'] == sequence]
            if len(choices) != 1:
                raise ValueError('indexed_sequence_missing_or_ambiguous')
            result = fetcher.acquire(choices[0])
            print(json.dumps({key: result.get(key) for key in ('url', 'http_status', 'pdf_signature_verified', 'file')}), flush=True)
            if result.get('http_status') in (401, 403, 429):
                break
    elif args.list:
        print(json.dumps({'documents': documents, 'index_periods': len(periods)}))
    else:
        path, report = build_report(args.data_dir, audit=args.audit)
        print(json.dumps({'report': path, 'requests': report['requests']['new_HTTP_attempts'],
            'events': [{k: e[k] for k in ('ticker', 'record_date', 'payment_date',
                'gross_per_share_decimal', 'status')} for e in report['events']],
            'remaining_conflicts': report['remaining_conflicts']}))


if __name__ == '__main__':
    main()
