"""Bounded CVM-only sidecar for missing cash evidence; never model admission.

Run with an explicit staged candidate audit. Existing inventories, snapshots,
collectors and candidate builders are read only. PDFs and run receipts use
exclusive creation; report.json is the sole replaceable, append-only report.
"""

import argparse
import base64
from collections import Counter, defaultdict, deque
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from . import cvm_proventos_documents as primary
from .market_inventory import load_inventory, market_root


CACHE = Path('raw/primary-research/cvm-documents')
STOP_HTTP = {401, 403, 429}
MAX_NEW_DOCUMENTS = 60
DOCUMENT_SECONDS = 30
REQUEST_INTERVAL = .5
MAX_ATTEMPTS = 2
MISSING_REASONS = {
    'independent_cash_events_cache_missing', 'independent_cash_cache_missing',
    'payments_cache_missing', 'independent_cash_evidence_does_not_cover_window_start',
    'in_window_payment_calendar_unavailable', 'in_window_payment_date_or_amount_unresolved',
    'B3_cash_event_missing_from_payment_table', 'insufficient_independent_cash_matches',
}


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _read(folder, path, receipts=None):
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > 100_000_000:
        raise ValueError('unsafe_research_input')
    body = path.read_bytes()
    if receipts is not None:
        receipts.append({'file': path.relative_to(folder).as_posix(),
                         'sha256': _sha(body), 'bytes': len(body)})
    return json.loads(body), body


def _identity(document):
    sequence, version = primary.document_id(document['url'])
    if (sequence, version) != (str(document['sequence']), document['version']):
        raise ValueError('document_identity_mismatch')
    return document['cvm'], sequence, version


def select_targets(inventory, audit, active_tickers=()):
    """Join by ticker, never by the staged inventory's positional indices."""
    ready = set(active_tickers) | {
        row['ticker'] for row in inventory['stocks'] if row.get('model_ready')}
    ready.update(row if isinstance(row, str) else row['ticker']
                 for row in audit.get('candidates', []))
    pending = {row['ticker']: row for row in audit['pending']}
    targets = []
    for stock in inventory['stocks']:
        ticker, cvm = stock['ticker'], stock.get('cvm')
        if (ticker in ready
                or stock.get('issuer_registration', {}).get('registration_blocks_current_purchase')
                or type(cvm) is not int or cvm <= 0
                or stock.get('share_class') not in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND')
                or stock.get('unit_composition') or ticker.endswith('11')):
            continue
        reason = pending.get(ticker, {}).get('reason')
        if reason not in MISSING_REASONS:
            if stock.get('payments', {}).get('status') != 'not_found':
                continue
            reason = 'inventory_payments_not_found'
        priority = (0 if reason in ('independent_cash_events_cache_missing',
                                    'independent_cash_cache_missing', 'payments_cache_missing')
                    else 1 if reason != 'inventory_payments_not_found' else 2)
        targets.append({'ticker': ticker, 'cvm': cvm, 'reason': reason,
                        'priority': priority, 'share_class': stock['share_class'],
                        'isin': stock.get('isin'),
                        'inventory_payment_status': stock.get('payments', {}).get('status'),
                        'audit_checks': deepcopy(pending.get(ticker, {}).get('checks', {}))})
    return sorted(targets, key=lambda row: (row['priority'], row['ticker']))


def existing_evidence(folder, report):
    """Hash actual cached bytes, retain archived revisions and skip downloaded IDs."""
    cache = folder / CACHE
    cached, verified = set(), {}
    for path in sorted(cache.glob('*.pdf')):
        match = re.fullmatch(r'(\d+)-v(\d+)-([a-f0-9]{64})\.pdf', path.name)
        if not match:
            continue
        if path.is_symlink() or path.stat().st_size > primary.MAX_BYTES:
            raise ValueError('unsafe_or_oversized_cache')
        body = path.read_bytes()
        if _sha(body) != match[3] or not body.lstrip().startswith(b'%PDF-'):
            raise ValueError('cache_hash_or_signature_mismatch')
        cached.add((match[1], int(match[2])))
        verified[path.relative_to(folder).as_posix()] = match[3]
    retained, known = [], set()
    reports = [report] + [_read(folder, path)[0] for path in sorted(cache.glob('documents-*.json'))]
    for payload in reports:
        for row in payload.get('results', []):
            if not row.get('sha256') or not row.get('file'):
                continue
            if verified.get(row['file']) != row['sha256']:
                raise ValueError('reported_pdf_not_verified')
            key = (*_identity(row), row['sha256'])
            if key not in known:
                known.add(key)
                retained.append(deepcopy(row))
    return cached, retained, verified


def balanced_queue(documents, targets, cached, retained, limit=MAX_NEW_DOCUMENTS):
    if type(limit) is not int or not 1 <= limit <= MAX_NEW_DOCUMENTS:
        raise ValueError('invalid_new_document_limit')
    by_issuer = defaultdict(list)
    for target in targets:
        by_issuer[target['cvm']].append(target)
    buckets = defaultdict(lambda: defaultdict(list))
    seen = set()
    for document in documents:
        identity = _identity(document)
        if identity in seen or identity[0] not in by_issuer:
            continue
        seen.add(identity)
        if identity[1:] in cached:
            continue
        year = int(document['published_at'][:4])
        if 2021 <= year <= 2026:
            buckets[identity[0]][year].append(document)
    coverage = Counter((row['cvm'], int(row['published_at'][:4])) for row in retained)
    totals = Counter(row['cvm'] for row in retained)
    for years in buckets.values():
        for year, rows in years.items():
            years[year] = deque(sorted(rows, key=lambda d: (
                d['published_at'], int(d['sequence']), d['version'], d['url'])))
    issuers = sorted(buckets, key=lambda cvm: (
        min(t['priority'] for t in by_issuer[cvm]), totals[cvm],
        min(t['ticker'] for t in by_issuer[cvm]), cvm))
    selected_years, queue = Counter(), []
    # Every issuer gets one turn before another document from the same issuer.
    while issuers and len(queue) < limit:
        remaining = []
        for cvm in issuers:
            years = buckets[cvm]
            year = min(years, key=lambda y: (selected_years[y], coverage[cvm, y], y))
            document = years[year].popleft()
            queue.append({**deepcopy(document), 'queue_year': year,
                          'target_tickers': sorted(t['ticker'] for t in by_issuer[cvm]),
                          'target_reasons': sorted({t['reason'] for t in by_issuer[cvm]})})
            coverage[cvm, year] += 1
            selected_years[year] += 1
            if not years[year]:
                del years[year]
            if years:
                remaining.append(cvm)
            if len(queue) == limit:
                break
        issuers = remaining
    return queue


def _fetch_parse(document, as_of, timeout):
    """One public request, called in a killable child with a total wall deadline."""
    try:
        _identity(document)
        request = urllib.request.Request(document['url'], headers={
            'User-Agent': 'RendaFutura-Academic/1.0', 'Accept-Encoding': 'identity',
            'Accept': 'application/pdf,application/octet-stream;q=0.9'})
        with primary._open(request, timeout=timeout) as response:
            if primary.document_id(response.url) != primary.document_id(document['url']):
                raise ValueError('unexpected_document_redirect')
            code = response.status
            if code != 200:
                return {'status': 'source_error', 'http_status': code, 'events': [],
                        'retryable': code in (500, 502, 503, 504)}
            body = response.read(primary.MAX_BYTES + 1)
            headers = {key: response.headers.get(key) for key in
                       ('Content-Type', 'Content-Disposition', 'Content-Length')}
        if len(body) > primary.MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
            raise ValueError('response_is_not_a_pdf')
        try:
            parsed = primary.parse_pdf(body, document, date.fromisoformat(as_of))
        except Exception as error:
            parsed = {'status': 'parse_error', 'events': [],
                      'error_class': type(error).__name__, 'issues': ['pdf_parse_failed']}
        return {**parsed, 'sha256': _sha(body), 'bytes': len(body),
                'body_base64': base64.b64encode(body).decode('ascii'),
                'http_status': code, 'headers': headers, 'retryable': False}
    except Exception as error:
        code = getattr(error, 'code', None)
        reason = str(error) if type(error) is ValueError and re.fullmatch(r'[a-z_]+', str(error)) else None
        return {'status': 'source_error', 'events': [], 'http_status': code,
                'error_class': type(error).__name__, 'error_code': reason,
                'retryable': code in (500, 502, 503, 504) or
                    (code is None and isinstance(error, (OSError, urllib.error.URLError)))}


def _bounded_attempt(document, as_of, remaining):
    payload = _json({'document': document, 'as_of': as_of, 'timeout': remaining})
    try:
        process = subprocess.run([sys.executable, '-B', '-m',
            'recommender.collect_missing_primary', '--worker'], input=payload.encode('ascii'),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=remaining,
            cwd=Path(__file__).resolve().parent.parent, check=False)
        if process.returncode:
            return {'status': 'source_error', 'events': [], 'retryable': False,
                    'error_code': 'document_worker_failed'}
        return json.loads(process.stdout)
    except subprocess.TimeoutExpired:
        return {'status': 'source_error', 'events': [], 'retryable': False,
                'error_code': 'document_total_timeout'}


def acquire(queue, as_of):
    results, requests, last_finished = [], 0, None
    stopped = None
    for document in queue:
        started = time.monotonic()
        attempts = []
        for _ in range(MAX_ATTEMPTS):
            delay = 0 if last_finished is None else max(0, REQUEST_INTERVAL - (time.monotonic() - last_finished))
            if delay:
                time.sleep(delay)
            remaining = DOCUMENT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                outcome = {'status': 'source_error', 'events': [], 'error_code': 'document_total_timeout'}
                break
            requests += 1
            outcome = _bounded_attempt(document, as_of.isoformat(), remaining)
            last_finished = time.monotonic()
            attempts.append({key: outcome.get(key) for key in
                             ('http_status', 'error_class', 'error_code', 'retryable')})
            if outcome.get('http_status') in STOP_HTTP:
                stopped = outcome['http_status']
                break
            if not outcome.get('retryable'):
                break
        results.append({**document, **outcome, 'attempts': attempts,
                        'retrieved_at': datetime.now(timezone.utc).isoformat(),
                        'elapsed_seconds': round(time.monotonic() - started, 3)})
        print(_json({'component': 'missing_primary', 'processed': len(results),
                     'cvm': document['cvm'], 'year': document['queue_year'],
                     'status': outcome['status'], 'event_rows': len(outcome['events']),
                     'http_status': outcome.get('http_status')}), flush=True)
        if stopped:
            break
    return results, requests, stopped


def _immutable(path, body):
    with path.open('xb') as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _document_key(row):
    return (*_identity(row), row.get('sha256'))


def _fact_key(event):
    fields = ('cvm', 'isin', 'record_date', 'approval_date', 'payment_date')
    amount = event.get('gross_per_unit_decimal')
    return (*[event.get(field) for field in fields], Decimal(amount) if amount is not None else None)


def merge_report(previous, retained, additions, run, report_file):
    merged = deepcopy(previous)
    rows = merged.setdefault('results', [])
    known = {_document_key(row) for row in rows}
    for row in retained + additions:
        key = _document_key(row)
        if key not in known:
            rows.append(deepcopy(row))
            known.add(key)
    merged.update(schema_version=1, parser_version='cvm_native_pdf_v2',
        collection_mode='append_only_missing_primary', as_of=run['as_of'],
        collected_at=run['collected_at'], discovered_documents=run['discovered_documents'],
        attempted_documents=len(rows), valid_documents=sum(bool(r.get('pages')) for r in rows),
        structured_documents=sum(bool(r.get('events')) for r in rows),
        event_rows=sum(len(r.get('events', [])) for r in rows),
        network_requests=run['network_requests'], report_file=report_file,
        events_are_declarations_not_settled_cash=True, type_not_guessed=True,
        active_model_changed=False, active_inventory_changed=False,
        point_in_time_archive_verified=False)
    merged.setdefault('missing_primary_runs', []).append({
        'file': report_file, 'counts': run['counts'], 'baseline': run['baseline']})
    assert rows[:len(previous.get('results', []))] == previous.get('results', [])
    return merged


def collect(folder, audit_path, as_of, *, max_new_documents=MAX_NEW_DOCUMENTS):
    if as_of > date.today() or type(max_new_documents) is not int or not 1 <= max_new_documents <= MAX_NEW_DOCUMENTS:
        raise ValueError('invalid_collection_limits')
    folder = market_root(folder)
    receipts = []
    inventory = load_inventory(folder)
    ip, _ = _read(folder, folder / 'inventory/current.json', receipts)
    _read(folder, folder / 'inventory' / ip['snapshot'] / 'market.json', receipts)
    pointer, _ = _read(folder, folder / 'current.json', receipts)
    snapshot = pointer['snapshot']
    if not re.fullmatch(r'snapshots/[A-Za-z0-9-]+', snapshot):
        raise ValueError('unsafe_active_snapshot')
    assets, _ = _read(folder, folder / snapshot / 'assets.json', receipts)
    active = {row['ticker'] for row in assets['assets'] if row.get('asset_class') == 'acoes'}
    audit, _ = _read(folder, Path(audit_path).resolve(), receipts)
    cache = folder / CACHE
    previous, baseline = _read(folder, cache / 'report.json')
    cached, retained, verified = existing_evidence(folder, previous)
    targets = select_targets(inventory, audit, active)
    documents = primary.discover(folder, inventory, as_of)
    queue = balanced_queue(documents, targets, cached, retained, max_new_documents)
    available = Counter(d['cvm'] for d in documents if (str(d['sequence']), d['version']) not in cached)
    for target in targets:
        target['uncached_index_documents'] = available[target['cvm']]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    baseline_path = cache / ('missing-primary-baseline-' + run_id + '.json')
    _immutable(baseline_path, baseline)
    results, requests, stopped = acquire(queue, as_of)
    for result in results:
        encoded = result.pop('body_base64', None)
        result.pop('retryable', None)
        if encoded is None:
            continue
        body = base64.b64decode(encoded, validate=True)
        if (_sha(body) != result['sha256'] or len(body) != result['bytes']
                or len(body) > primary.MAX_BYTES or not body.lstrip().startswith(b'%PDF-')):
            raise ValueError('download_receipt_content_mismatch')
        filename = f"{result['sequence']}-v{result['version']}-{result['sha256']}.pdf"
        path = cache / filename
        _immutable(path, body)
        result['file'] = path.relative_to(folder).as_posix()
        result['acquisition'] = {'status': 'downloaded_pdf_bytes_verified',
            'http_status': result.pop('http_status', None), 'headers': result.pop('headers', {})}
        for event in result['events']:
            event.update(cash_model_eligible=False, financially_confirmed=False,
                         is_settlement_confirmation=False, paid_status_as_of_verified=False,
                         point_in_time_archive_verified=False)
    downloaded = [r for r in results if r.get('file')]
    old_facts = {_fact_key(e) for r in retained for e in r.get('events', [])}
    new_facts = {_fact_key(e) for r in downloaded for e in r['events']}
    by_issuer = []
    for cvm in sorted({r['cvm'] for r in results}):
        rows = [r for r in results if r['cvm'] == cvm]
        by_issuer.append({'cvm': cvm, 'tickers': sorted({t for r in rows for t in r['target_tickers']}),
            'attempted': len(rows), 'downloaded': sum(bool(r.get('file')) for r in rows),
            'event_rows': sum(len(r['events']) for r in rows)})
    counts = {'prior_report_rows': len(previous['results']), 'verified_existing_pdf_files': len(verified),
        'target_instruments': len(targets), 'target_issuers': len({t['cvm'] for t in targets}),
        'target_issuers_with_uncached_documents': len({t['cvm'] for t in targets if t['uncached_index_documents']}),
        'queued_documents': len(queue), 'attempted_new_documents': len(results),
        'new_downloaded_documents': len(downloaded), 'new_valid_pdf_documents': sum(bool(r.get('pages')) for r in downloaded),
        'new_structured_documents': sum(bool(r['events']) for r in downloaded),
        'new_event_rows': sum(len(r['events']) for r in downloaded),
        'distinct_new_declaration_facts': len(new_facts - old_facts),
        'new_issuer_count': len({r['cvm'] for r in downloaded}),
        'new_unmapped_installment_rows': sum(len(r.get('unmapped_installment_rows', [])) for r in downloaded),
        'source_errors': sum(r['status'] == 'source_error' for r in results), 'model_admissions': 0}
    report_file = (CACHE / ('documents-missing-primary-' + run_id + '.json')).as_posix()
    run = {'schema_version': 1, 'parser_version': 'cvm_native_pdf_v2',
        'as_of': as_of.isoformat(), 'collected_at': datetime.now(timezone.utc).isoformat(),
        'collection_mode': 'balanced_missing_primary_bounded', 'discovered_documents': len(documents),
        'baseline': {'file': baseline_path.relative_to(folder).as_posix(), 'sha256': _sha(baseline)},
        'input_receipts': receipts, 'limits': {'max_new_documents': max_new_documents,
            'document_total_timeout_seconds': DOCUMENT_SECONDS, 'max_attempts_per_document': MAX_ATTEMPTS,
            'request_interval_seconds': REQUEST_INTERVAL, 'concurrency': 1},
        'network_requests': requests, 'stopped_http_status': stopped,
        'not_attempted_after_stop': len(queue) - len(results), 'counts': counts,
        'by_issuer': by_issuer,
        'downloaded_by_publication_year': dict(sorted(Counter(r['published_at'][:4] for r in downloaded).items())),
        'event_states': dict(Counter(e['status'] for r in downloaded for e in r['events'])),
        'targets': targets, 'queue': queue, 'results': results,
        'events_are_declarations_not_settled_cash': True, 'point_in_time_archive_verified': False,
        'active_model_changed': False, 'active_inventory_changed': False,
        'limitations': ['Declarations and scheduled dates do not establish paid cash or settlement.',
            'Source rows and revisions are not additive cash; exact ISIN-unit amounts are retained.',
            'Full payment history, independent cash matching, share basis and historical status availability remain unverified.',
            'Index years without matching proventos documents are not evidence of zero payments.']}
    # Re-read before merging so concurrent append-only work is retained, too.
    current, current_bytes = _read(folder, cache / 'report.json')
    if Counter(map(_json, previous['results'])) - Counter(map(_json, current['results'])):
        raise ValueError('earlier_report_rows_missing_before_merge')
    merged = merge_report(current, retained, results, run, report_file)
    counts['merged_report_rows'] = len(merged['results'])
    counts['merged_event_rows'] = merged['event_rows']
    counts['preserved_baseline_rows'] = len(previous['results'])
    _immutable(folder / report_file, (_json(run) + '\n').encode('ascii'))
    merged_body = (json.dumps(merged, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode('ascii')
    merged_file = cache / ('missing-primary-merged-' + run_id + '.json')
    _immutable(merged_file, merged_body)
    if (cache / 'report.json').read_bytes() != current_bytes:
        raise ValueError('report_changed_during_merge_retry_from_immutable_run')
    with tempfile.NamedTemporaryFile(dir=cache, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(merged_body)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, cache / 'report.json')
    finally:
        temporary.unlink(missing_ok=True)
    print(_json({'report_file': report_file, 'counts': counts,
                 'network_requests': requests, 'stopped_http_status': stopped}), flush=True)
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=primary.ROOT / 'recommender/data/market')
    parser.add_argument('--candidate-audit', type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    parser.add_argument('--max-new-documents', type=int, default=MAX_NEW_DOCUMENTS)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        payload = json.load(sys.stdin)
        print(_json(_fetch_parse(payload['document'], payload['as_of'], payload['timeout'])))
    else:
        if args.candidate_audit is None:
            parser.error('--candidate-audit is required')
        collect(args.data_dir, args.candidate_audit, args.as_of, max_new_documents=args.max_new_documents)


if __name__ == '__main__':
    main()
