"""Bounded BRAPI BASIC acquisition and explicit, offline inventory publication."""

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import uuid

from .current_quote_sources import BASIS, collect, load_cached_snapshot
from .market_inventory import load_inventory, market_root, publish_inventory

MAX_REQUESTS = 82
INTERVAL_SECONDS = .6
STOP_HTTP = {401, 403, 429}
BASIC_ENDPOINT = 'https://brapi.dev/api/quote/{ticker}'
PENDING = {'stale', 'stale_indicative_snapshot', 'source_http_error', 'source_unavailable',
           'source_rate_limited', 'not_found', 'not_fetched', 'unavailable', 'missing',
           'invalid', 'invalid_format'}
CACHE_ERRORS = (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(',', ':')).encode()).hexdigest()


def _eligible(row):
    quote = row['quote']
    return quote.get('status') in PENDING or (not quote.get('status') and quote.get('value') is None)


def _cached(folder, ticker):
    # Even an invalid existing receipt is inspected locally without a repeat request.
    return (folder / 'raw/current-quotes' / (ticker + '-current.json')).exists()


def _basic_access_stop(folder):
    for path in sorted((folder / 'raw/current-quotes/basic-access-denials').glob('*.json')):
        if path.is_symlink() or path.stat().st_size > 10_000:
            raise ValueError('unsafe_basic_access_denial_receipt')
        receipt = json.loads(path.read_bytes())
        if (receipt.get('schema_version') == 1 and receipt.get('provider') == 'brapi'
                and receipt.get('endpoint') == BASIC_ENDPOINT and receipt.get('http_status') in STOP_HTTP):
            return {'ticker': receipt['ticker'], 'http_status': receipt['http_status'], 'previous_run': True}
    return None


def _record_basic_denial(folder, ticker, code):
    target = folder / 'raw/current-quotes/basic-access-denials' / (uuid.uuid4().hex + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='ascii') as stream:
        json.dump({'schema_version': 1, 'provider': 'brapi', 'endpoint': BASIC_ENDPOINT,
                   'ticker': ticker, 'http_status': code,
                   'observed_at': datetime.now(timezone.utc).isoformat()}, stream)


def _newer(quote, old):
    previous = old.get('snapshot_at') or old.get('last_trade_at') or old.get('as_of')
    if not previous:
        return old.get('value') is None
    if len(previous) == 10:
        return quote['as_of'] > date.fromisoformat(previous).isoformat()
    stamp = datetime.fromisoformat(previous.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('previous_quote_time_naive')
    return datetime.fromisoformat(quote['snapshot_at']) > stamp


def sync_cached_quotes(folder, inventory, *, now=None):
    """Only quote fields, receipt registration and quote counts can change."""
    folder = market_root(folder)
    as_of = date.fromisoformat(inventory['as_of'])
    updated = deepcopy(inventory)
    sources = updated.setdefault('sources', [])
    decisions = []
    for row in updated['stocks']:
        if not _eligible(row):
            continue
        ticker, old = row['ticker'], row['quote']
        try:
            quote, source = load_cached_snapshot(folder, ticker, as_of=as_of, now=now)
            if quote is None:
                decisions.append({'ticker': ticker, 'status': 'no_cache'})
                continue
            if not _newer(quote, old):
                decisions.append({'ticker': ticker, 'status': 'not_newer'})
                continue
            existing = [item for item in sources if item.get('file') == source['file']]
            if any(item != source for item in existing):
                raise ValueError('inventory_original_receipt_conflict')
            fresh = (as_of - date.fromisoformat(quote['as_of'])).days <= 7
            known_trade = old.get('basis', '').startswith('last_observed_trade_')
            row['quote'] = {
                'value': quote['price'], 'unit': 'BRL', 'as_of': quote['as_of'],
                'status': 'indicative_snapshot' if fresh else 'stale_indicative_snapshot',
                'basis': BASIS, 'provider': 'brapi', 'source_url': source['url'],
                'source_file': source['file'], 'source_sha256': source['sha256'],
                'source_receipt': source['file'][:-5] + '-source.json',
                'snapshot_at': quote['snapshot_at'], 'retrieved_at': source['retrieved_at'],
                'last_trade_at': old.get('last_trade_at'),
                'last_trade_as_of': old.get('last_trade_as_of') or (old.get('as_of') if known_trade else None),
                'last_trade_source_url': old.get('last_trade_source_url') or (old.get('source_url') if known_trade else None),
                'last_trade_confirmed': False, 'purchase_available': False,
                'historical_reconciliation_evidence': False, 'previous_reference': deepcopy(old),
            }
            if not existing:
                sources.append(deepcopy(source))
            decisions.append({'ticker': ticker, 'status': 'updated', 'previous_status': old.get('status')})
        except CACHE_ERRORS:
            decisions.append({'ticker': ticker, 'status': 'cache_rejected'})
    quotes = Counter(row['quote'].get('status') for row in updated['stocks'])
    updated.setdefault('counts', {}).update(
        quotes_available=quotes['available'], quotes_indicative_snapshots=quotes['indicative_snapshot'],
        quotes_stale_indicative_snapshots=quotes['stale_indicative_snapshot'], quotes_stale=quotes['stale'],
        quotes_missing=sum(count for status, count in quotes.items()
                           if status not in {'available', 'indicative_snapshot', 'stale_indicative_snapshot', 'stale'}))
    return updated, decisions


def protected_state(folder):
    """Fingerprint root data and every file of the active economic snapshot."""
    folder = market_root(folder)
    files = list(folder.glob('*'))
    pointer = folder / 'current.json'
    if pointer.is_file():
        relative = json.loads(pointer.read_bytes()).get('snapshot')
        if relative:
            snapshot = (folder / relative).resolve()
            if not snapshot.is_relative_to(folder / 'snapshots'):
                raise ValueError('unsafe_active_model_snapshot')
            files.extend(snapshot.rglob('*'))
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(files) if path.is_file()}


def active_model_preparations():
    """Check active preparation processes immediately before publication."""
    if os.name == 'nt':
        command = ('Get-CimInstance Win32_Process -Filter "Name = \'python.exe\' OR Name = \'pythonw.exe\'" '
                   '| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, check=True, timeout=20)
        processes = json.loads(result.stdout or '[]')
        if isinstance(processes, dict):
            processes = [processes]
    else:
        processes = [{'ProcessId': int(path.name), 'CommandLine': (path / 'cmdline').read_bytes().decode(errors='replace')}
                     for path in Path('/proc').iterdir() if path.name.isdigit() and (path / 'cmdline').is_file()]
    return [row['ProcessId'] for row in processes
            if any(name in (row.get('CommandLine') or '') for name in
                   ('recommender.prepare_inventory', 'recommender/prepare_inventory.py', 'recommender\\prepare_inventory.py'))]


@contextmanager
def _operation_lock(folder):
    path = market_root(folder) / 'raw/current-quotes/.inventory-operation.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='ascii') as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        path.unlink()


def acquire(folder, *, max_requests=MAX_REQUESTS, now=None):
    if type(max_requests) is not int or not 0 <= max_requests <= MAX_REQUESTS:
        raise ValueError('current_quote_request_budget_invalid')
    folder = market_root(folder)
    with _operation_lock(folder):
        inventory = load_inventory(folder)
        protected = protected_state(folder)
        base_pointer = hashlib.sha256((folder / 'inventory/current.json').read_bytes()).hexdigest()
        eligible = [row for row in inventory['stocks'] if _eligible(row)]
        requests, reused, outcomes = 0, 0, []
        stopped = _basic_access_stop(folder)
        # Inspect all existing caches first, including those after a possible stop.
        missing = []
        for row in eligible:
            ticker = row['ticker']
            if not _cached(folder, ticker):
                missing.append(ticker)
                continue
            try:
                load_cached_snapshot(folder, ticker, as_of=date.fromisoformat(inventory['as_of']), now=now)
                reused += 1
                outcomes.append({'ticker': ticker, 'status': 'cache_reused'})
            except CACHE_ERRORS:
                outcomes.append({'ticker': ticker, 'status': 'cache_rejected'})
        next_request = 0.
        for ticker in missing:
            if stopped or requests >= max_requests:
                break
            time.sleep(max(0., next_request - time.monotonic()))
            requests += 1
            next_request = time.monotonic() + INTERVAL_SECONDS
            try:
                collect(folder, ticker)
                outcomes.append({'ticker': ticker, 'status': 'collected'})
            except urllib.error.HTTPError as error:
                outcomes.append({'ticker': ticker, 'status': 'http_error', 'http_status': error.code})
                error.close()
                if error.code in STOP_HTTP or 300 <= error.code < 400:
                    if error.code in STOP_HTTP:
                        _record_basic_denial(folder, ticker, error.code)
                    stopped = {'ticker': ticker, 'http_status': error.code}
                    break
            except CACHE_ERRORS:
                # Never serialize provider errors, URLs from exceptions or credentials.
                outcomes.append({'ticker': ticker, 'status': 'unavailable_or_invalid'})
            if requests % 10 == 0:
                print(json.dumps({'stage': 'brapi_basic', 'requests': requests, 'budget': max_requests}), flush=True)
        updated, decisions = sync_cached_quotes(folder, inventory, now=now)
        if protected != protected_state(folder):
            raise ValueError('economic_data_changed_during_collection')
        report = {
            'schema_version': 1, 'provider': 'brapi', 'endpoint': BASIC_ENDPOINT,
            'operation': 'collect_catalogue_quotes_only', 'started_from_as_of': inventory['as_of'],
            'created_at': datetime.now(timezone.utc).isoformat(), 'eligible_quotes': len(eligible),
            'max_requests': max_requests, 'requests': requests, 'interval_seconds': INTERVAL_SECONDS,
            'reused_caches': reused, 'not_requested': len(missing) - requests,
            'stopped': stopped, 'outcomes': outcomes, 'decisions': decisions,
            'counts_before': inventory['counts'], 'counts_after': updated['counts'],
            'base_inventory_sha256': _digest(inventory), 'base_pointer_sha256': base_pointer,
            'candidate_sha256': _digest(updated), 'protected_state': protected,
            'published': False,
        }
        target = folder / 'raw/current-quotes/reports' / (uuid.uuid4().hex + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2, allow_nan=False)
        return target, report


def publish_from_report(folder, report_path, *, now=None):
    """Offline operation: refuse a changed base, model, candidate or active prepare."""
    folder = market_root(folder)
    report = json.loads(Path(report_path).read_bytes())
    if (report.get('schema_version') != 1 or report.get('operation') != 'collect_catalogue_quotes_only'
            or report.get('provider') != 'brapi'):
        raise ValueError('current_quote_acquisition_report_invalid')
    with _operation_lock(folder):
        if active_model_preparations():
            raise ValueError('active_model_preparation_blocks_inventory_publish')
        inventory = load_inventory(folder)
        pointer = hashlib.sha256((folder / 'inventory/current.json').read_bytes()).hexdigest()
        if _digest(inventory) != report['base_inventory_sha256'] or pointer != report['base_pointer_sha256']:
            raise ValueError('inventory_changed_since_quote_collection')
        if protected_state(folder) != report['protected_state']:
            raise ValueError('economic_data_changed_since_quote_collection')
        updated, _ = sync_cached_quotes(folder, inventory, now=now)
        if _digest(updated) != report['candidate_sha256']:
            raise ValueError('quote_candidate_changed_since_collection')
        if active_model_preparations():
            raise ValueError('active_model_preparation_blocks_inventory_publish')
        if protected_state(folder) != report['protected_state']:
            raise ValueError('economic_data_changed_before_inventory_publish')
        snapshot = publish_inventory(folder, updated)
        return snapshot, updated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('preview', 'collect', 'publish-inventory'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--max-requests', type=int, default=MAX_REQUESTS)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if args.operation == 'publish-inventory' and not args.report:
        parser.error('publish-inventory requires --report')
    if args.operation == 'collect':
        path, report = acquire(args.data_dir, max_requests=args.max_requests)
        result = {key: report[key] for key in ('eligible_quotes', 'requests', 'reused_caches', 'not_requested', 'stopped', 'counts_after')}
        result.update(report=str(path), published=False)
    elif args.operation == 'publish-inventory':
        snapshot, updated = publish_from_report(args.data_dir, args.report)
        result = {'published_inventory_only': True, 'snapshot': str(snapshot), 'counts': updated['counts']}
    else:
        updated, decisions = sync_cached_quotes(args.data_dir, load_inventory(args.data_dir))
        result = {'published': False, 'counts': updated['counts'], 'decisions': dict(Counter(row['status'] for row in decisions))}
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
