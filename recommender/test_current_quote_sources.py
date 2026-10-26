from datetime import date, datetime, timezone
import hashlib
import json

import pytest

from . import current_quote_sources as module
from .collect_market_data import write_json
from .test_current_quotes import fixture

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def body(**updates):
    return json.dumps({'results': [{'symbol': 'TEST3', 'currency': 'BRL',
        'regularMarketPrice': 114.01, 'regularMarketTime': '2026-10-23T21:37:25Z', **updates}]}).encode()


def cache(tmp_path, content=None):
    content = content or body()
    digest = hashlib.sha256(content).hexdigest()
    raw = 'raw/current-quotes/TEST3-' + digest + '.json'
    source = raw[:-5] + '-source.json'
    (tmp_path / raw).parent.mkdir(parents=True)
    (tmp_path / raw).write_bytes(content)
    write_json(tmp_path / source, {'file': raw, 'sha256': digest, 'bytes': len(content),
        'url': 'https://brapi.dev/api/quote/TEST3', 'retrieved_at': '2026-10-24T23:30:00+00:00',
        'provider': 'brapi', 'authority': 'secondary', 'role': 'indicative_current_quote_snapshot',
        'snapshot_at': module.parse_snapshot(content, 'TEST3', now=NOW)['snapshot_at'],
        'last_trade_confirmed': False, 'historical_reconciliation_evidence': False})
    write_json(tmp_path / 'raw/current-quotes/TEST3-current.json', {'source': source})
    return raw


def test_snapshot_and_retrieval_cannot_refresh_the_known_last_trade(tmp_path):
    cache(tmp_path)
    quote, _ = module.load_snapshot(tmp_path, 'TEST3', date(2026, 9, 30),
        fixture(day=date(2026, 10, 14)), now=NOW)
    assert quote['basis'] == module.BASIS
    assert quote['snapshot_at'].startswith('2026-10-23')
    assert quote['last_trade_at'].startswith('2026-10-14')
    assert quote['retrieved_at'].startswith('2026-10-24')
    assert quote['last_trade_source_url'].startswith('https://query1.finance.yahoo.com/')


@pytest.mark.parametrize('updates', [{'symbol': 'OTHER3'}, {'currency': 'USD'},
    {'regularMarketPrice': -1}, {'regularMarketTime': '2026-10-26T00:00:00Z'},
    {'regularMarketTime': '2026-10-23T00:00:00'}, {'regularMarketPrice': True}])
def test_invalid_snapshot_identity_price_or_timestamp_cannot_be_used(updates):
    with pytest.raises(ValueError):
        module.parse_snapshot(body(**updates), 'TEST3', now=NOW)


@pytest.mark.parametrize('payload', [[], None, {'results': [[]]}, {'results': [None]}])
def test_non_object_provider_responses_are_controlled_validation_errors(payload):
    with pytest.raises(ValueError, match='result_identity'):
        module.parse_snapshot(json.dumps(payload).encode(), 'TEST3', now=NOW)


def test_alt_snapshot_cannot_bypass_share_event_normalization(tmp_path):
    cache(tmp_path)
    event = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
    original = fixture(day=date(2026, 10, 14), events={'splits': {'one': {'date': event}}})
    with pytest.raises(ValueError, match='share_event'):
        module.load_snapshot(tmp_path, 'TEST3', date(2026, 9, 30), original, now=NOW)


def test_corrupt_cached_snapshot_fails_integrity_check(tmp_path):
    raw = cache(tmp_path)
    (tmp_path / raw).write_bytes(body(regularMarketPrice=5.))
    with pytest.raises(ValueError, match='hash_mismatch'):
        module.load_snapshot(tmp_path, 'TEST3', date(2026, 9, 30), fixture(), now=NOW)


def test_old_provider_snapshot_remains_old_not_freshened_by_recent_retrieval(tmp_path):
    cache(tmp_path, body(regularMarketTime='2026-10-14T20:00:00Z'))
    quote, _ = module.load_snapshot(tmp_path, 'TEST3', date(2026, 9, 30),
        fixture(day=date(2026, 10, 14)), now=NOW)
    assert quote['as_of'] == '2026-10-14'
    assert quote['purchase_available'] is False


def test_catalogue_cache_does_not_require_yahoo_or_infer_a_trade(tmp_path):
    cache(tmp_path)
    quote, source = module.load_cached_snapshot(tmp_path, 'TEST3', as_of=date(2026, 10, 24), now=NOW)
    assert quote['last_trade_at'] is None
    assert quote['last_trade_source_url'] is None
    assert quote['retrieved_at'] == source['retrieved_at']
    with pytest.raises((ValueError, KeyError, TypeError)):
        module.load_snapshot(tmp_path, 'TEST3', date(2026, 9, 30), b'{}', now=NOW)


@pytest.mark.parametrize('field,value', [('bytes', 1), ('bytes', True), ('provider', 'other'),
    ('snapshot_at', '2026-10-24T00:00:00+00:00'), ('historical_reconciliation_evidence', True),
    ('last_trade_confirmed', True), ('retrieved_at', '2026-10-23T00:00:00+00:00'),
    ('retrieved_at', '2026-10-26T00:00:00+00:00'), ('retrieved_at', '2026-10-24T00:00:00')])
def test_receipt_length_semantics_and_clocks_are_validated(tmp_path, field, value):
    raw = cache(tmp_path)
    receipt = tmp_path / (raw[:-5] + '-source.json')
    source = json.loads(receipt.read_bytes())
    source[field] = value
    write_json(receipt, source)
    with pytest.raises(ValueError):
        module.load_cached_snapshot(tmp_path, 'TEST3', now=NOW)


def test_snapshot_must_not_postdate_inventory(tmp_path):
    cache(tmp_path)
    with pytest.raises(ValueError, match='after_inventory'):
        module.load_cached_snapshot(tmp_path, 'TEST3', as_of=date(2026, 10, 22), now=NOW)


def test_source_filename_must_match_actual_digest(tmp_path):
    raw = cache(tmp_path)
    source_path = raw[:-5] + '-source.json'
    wrong = source_path.replace(raw.split('-')[-1][:-5], '0' * 64)
    (tmp_path / wrong).write_bytes((tmp_path / source_path).read_bytes())
    write_json(tmp_path / 'raw/current-quotes/TEST3-current.json', {'source': wrong})
    with pytest.raises(ValueError, match='filename_mismatch'):
        module.load_cached_snapshot(tmp_path, 'TEST3', now=NOW)


def test_cache_pointer_cannot_escape_receipt_directory(tmp_path):
    cache(tmp_path)
    write_json(tmp_path / 'raw/current-quotes/TEST3-current.json', {'source': '../outside.json'})
    with pytest.raises(ValueError, match='pointer_identity'):
        module.load_cached_snapshot(tmp_path, 'TEST3', now=NOW)


def test_collect_uses_basic_endpoint_no_proxy_no_redirect_and_immutable_receipts(tmp_path, monkeypatch):
    class Response:
        url = 'https://brapi.dev/api/quote/TEST3'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == module.MAX_BYTES + 1
            return body()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == Response.url
            assert request.get_header('Authorization') == 'Bearer test-secret'
            assert timeout == 30
            return Response()

    def opener(*handlers):
        assert any(isinstance(h, module.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
        assert any(isinstance(h, module._NoRedirect) for h in handlers)
        return Opener()

    monkeypatch.setattr(module, 'brapi_token', lambda: 'test-secret')
    monkeypatch.setattr(module.urllib.request, 'build_opener', opener)
    module.collect(tmp_path, 'TEST3')
    originals = {path.name: path.read_bytes() for path in (tmp_path / 'raw/current-quotes').glob('TEST3-*-source.json')}
    module.collect(tmp_path, 'TEST3')
    assert originals == {path.name: path.read_bytes() for path in (tmp_path / 'raw/current-quotes').glob('TEST3-*-source.json')}
    assert module._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test') is None
