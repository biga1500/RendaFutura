"""Indicative provider snapshots, never a fabricated fresh trade or history.

The model override requires a stale, already reconciled equity reference.
The catalogue can read a validated receipt without historical model evidence.
The provider snapshot, retrieval and known last-trade times stay separate.
Snapshot quotes do not become independent historical reconciliation evidence.
"""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.request

from .collect_market_data import write_json
from .credentials import brapi_token
from .market_inventory import market_root

MAX_BYTES = 2_000_000
BASIS = 'provider_snapshot_not_confirmed_trade_or_executable_order'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', value):
        raise ValueError('invalid_current_quote_symbol')
    return value


def parse_snapshot(body, ticker, *, now=None):
    ticker = _ticker(ticker)
    now = now or datetime.now(timezone.utc)
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError('current_quote_snapshot_size_limit')
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError('current_quote_snapshot_result_identity')
    records = payload.get('results')
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError('current_quote_snapshot_result_identity')
    row = records[0]
    if not isinstance(row, dict):
        raise ValueError('current_quote_snapshot_result_identity')
    if row.get('symbol') != ticker or row.get('currency') != 'BRL':
        raise ValueError('current_quote_snapshot_symbol_or_currency_mismatch')
    price = row.get('regularMarketPrice')
    if type(price) not in (int, float) or not math.isfinite(price) or price <= 0:
        raise ValueError('current_quote_snapshot_price_invalid')
    try:
        stamp = datetime.fromisoformat(row['regularMarketTime'].replace('Z', '+00:00'))
    except (KeyError, AttributeError, ValueError) as error:
        raise ValueError('current_quote_snapshot_time_invalid') from error
    if stamp.tzinfo is None or stamp > now:
        raise ValueError('current_quote_snapshot_future_or_naive_time')
    return {'price': float(price), 'as_of': stamp.date().isoformat(),
        'source_url': 'https://brapi.dev/api/quote/' + ticker,
        'purchase_available': (now.date() - stamp.date()).days <= 7,
        'basis': BASIS, 'snapshot_at': stamp.isoformat(),
        'last_trade_at': None, 'last_trade_source_url': None, 'retrieved_at': None,
        'minimum_fraction': 1., 'minimum_investment': None}


def collect(folder, ticker):
    folder, ticker = market_root(folder), _ticker(ticker)
    key = brapi_token()
    if not key:
        raise ValueError('current_quote_provider_credential_missing')
    url = 'https://brapi.dev/api/quote/' + ticker
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key,
        'Accept': 'application/json', 'User-Agent': 'RendaFutura-Academic/1.0'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=30) as response:
        if response.url != url:
            raise ValueError('current_quote_source_redirect_not_allowed')
        body = response.read(MAX_BYTES + 1)
    quote = parse_snapshot(body, ticker)
    digest = hashlib.sha256(body).hexdigest()
    target = folder / 'raw/current-quotes'
    target.mkdir(parents=True, exist_ok=True)
    raw = target / (ticker + '-' + digest + '.json')
    if not raw.exists():
        with raw.open('xb') as stream:
            stream.write(body)
    elif raw.read_bytes() != body:
        raise ValueError('current_quote_immutable_source_conflict')
    receipt = {'file': raw.relative_to(folder).as_posix(), 'sha256': digest, 'bytes': len(body),
        'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'authority': 'secondary', 'provider': 'brapi', 'role': 'indicative_current_quote_snapshot',
        'snapshot_at': quote['snapshot_at'], 'last_trade_confirmed': False,
        'historical_reconciliation_evidence': False}
    receipt_path = target / (ticker + '-' + digest + '-source.json')
    if not receipt_path.exists():
        write_json(receipt_path, receipt)
    write_json(target / (ticker + '-current.json'), {'source': receipt_path.relative_to(folder).as_posix()})
    return quote


def load_cached_snapshot(folder, ticker, *, as_of=None, now=None):
    """Read original bytes and receipt; never infer a trade from provider time."""
    folder, ticker = market_root(folder), _ticker(ticker)
    now = now or datetime.now(timezone.utc)
    pointer = folder / 'raw/current-quotes' / (ticker + '-current.json')
    if not pointer.is_file():
        return None, None
    def read(relative, limit):
        path = folder / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(folder)
                or not path.is_file() or path.stat().st_size > limit):
            raise ValueError('unsafe_current_quote_snapshot_cache')
        return path.read_bytes()
    if (pointer.is_symlink() or not pointer.resolve().is_relative_to(folder)
            or pointer.stat().st_size > 10_000):
        raise ValueError('unsafe_current_quote_snapshot_pointer')
    source_path = json.loads(pointer.read_bytes())['source']
    prefix = r'raw/current-quotes/' + ticker + r'-[a-f0-9]{64}'
    if not re.fullmatch(prefix + r'-source\.json', source_path):
        raise ValueError('current_quote_source_pointer_identity')
    source = json.loads(read(source_path, 10_000))
    if (not re.fullmatch(prefix + r'\.json', source.get('file', ''))
            or source.get('url') != 'https://brapi.dev/api/quote/' + ticker):
        raise ValueError('current_quote_source_identity')
    body = read(source['file'], MAX_BYTES)
    if hashlib.sha256(body).hexdigest() != source.get('sha256'):
        raise ValueError('current_quote_source_hash_mismatch')
    digest = source['sha256']
    if (source['file'] != f'raw/current-quotes/{ticker}-{digest}.json'
            or source_path != f'raw/current-quotes/{ticker}-{digest}-source.json'
            or type(source.get('bytes')) is not int or source['bytes'] != len(body)):
        raise ValueError('current_quote_source_bytes_or_filename_mismatch')
    quote = parse_snapshot(body, ticker, now=now)
    if (source.get('provider') != 'brapi' or source.get('authority') != 'secondary'
            or source.get('role') != 'indicative_current_quote_snapshot'
            or source.get('last_trade_confirmed') is not False
            or source.get('historical_reconciliation_evidence') is not False
            or source.get('snapshot_at') != quote['snapshot_at']):
        raise ValueError('current_quote_receipt_semantics_invalid')
    if as_of is not None and date.fromisoformat(quote['as_of']) > as_of:
        raise ValueError('current_quote_snapshot_after_inventory')
    retrieved = datetime.fromisoformat(source['retrieved_at'])
    if (retrieved.tzinfo is None or retrieved > now
            or retrieved < datetime.fromisoformat(quote['snapshot_at'])):
        raise ValueError('current_quote_retrieval_time_invalid')
    quote['retrieved_at'] = source['retrieved_at']
    return quote, source


def load_snapshot(folder, ticker, cutoff, known_price_content, *, now=None):
    quote, source = load_cached_snapshot(folder, ticker, now=now)
    if quote is None:
        return None, None
    if date.fromisoformat(quote['as_of']) < cutoff:
        raise ValueError('current_quote_snapshot_pre_model')
    known = json.loads(known_price_content)['chart']['result'][0]
    if known['meta'].get('symbol') != ticker + '.SA' or known['meta'].get('currency') != 'BRL':
        raise ValueError('known_quote_identity_mismatch')
    last = datetime.fromtimestamp(known['meta']['regularMarketTime'], timezone.utc)
    if last > datetime.fromisoformat(quote['snapshot_at']):
        raise ValueError('provider_snapshot_older_than_known_trade')
    for event in known.get('events', {}).get('splits', {}).values():
        if cutoff < datetime.fromtimestamp(event['date'], timezone.utc).date() <= date.fromisoformat(quote['as_of']):
            raise ValueError('post_model_share_event_blocks_snapshot_override')
    quote.update(last_trade_at=last.isoformat(), retrieved_at=source['retrieved_at'],
                 last_trade_source_url='https://query1.finance.yahoo.com/v8/finance/chart/' + ticker + '.SA')
    return quote, source


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('ticker')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    quote = collect(args.data_dir, args.ticker)
    print(json.dumps({key: quote[key] for key in ('price', 'as_of', 'source_url', 'basis')}, ensure_ascii=True))
