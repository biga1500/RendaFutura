from copy import deepcopy
from datetime import date
import json
import urllib.error

import pytest

from . import current_quote_inventory as module
from .collect_market_data import write_json
from .market_inventory import load_inventory, publish_inventory
from .test_current_quote_sources import NOW, body, cache


def stock(ticker='TEST3', status='stale', as_of='2026-10-14'):
    return {'ticker': ticker, 'name': ticker, 'quote': {'value': 10 if as_of else None,
        'status': status, 'as_of': as_of, 'unit': 'BRL',
        'basis': 'last_observed_trade_not_executable_offer', 'source_url': 'https://example.test/old'},
        'history': {'observed_months': 120}, 'payments': {'events': [{'value': 1}]},
        'fundamentals': {'fields': {'earnings_per_share': {'value': 2}}},
        'model_ready': False, 'model_pending_reason': 'cash_pending'}


def inventory(rows=None):
    return {'schema_version': 1, 'as_of': '2026-10-24', 'collected_at': '2026-10-24T20:00:00Z',
        'stocks': rows or [stock()], 'treasury': [{'name': 'preserved'}],
        'sources': [{'file': 'old.json', 'sha256': 'preserved'}],
        'counts': {'quotes_available': 0, 'model_ready_stocks': 0, 'history_available': 1},
        'model_preparation': {'snapshot': 'model-one'}}


def prepare(tmp_path, rows=None):
    value = inventory(rows)
    publish_inventory(tmp_path, value)
    write_json(tmp_path / 'current.json', {'snapshot': 'snapshots/model-one'})
    snapshot = tmp_path / 'snapshots/model-one'
    snapshot.mkdir(parents=True)
    (snapshot / 'returns.csv').write_bytes(b'date,TEST3\n2026-09-30,.1\n')
    return value


def test_snapshot_changes_only_quote_receipts_and_quote_counts(tmp_path):
    original = inventory()
    untouched = deepcopy(original)
    cache(tmp_path)
    updated, decisions = module.sync_cached_quotes(tmp_path, original, now=NOW)
    assert original == untouched
    assert decisions == [{'ticker': 'TEST3', 'status': 'updated', 'previous_status': 'stale'}]
    quote = updated['stocks'][0]['quote']
    assert quote['previous_reference'] == original['stocks'][0]['quote']
    assert quote['status'] == 'indicative_snapshot'
    assert quote['snapshot_at'].startswith('2026-10-23')
    assert quote['retrieved_at'].startswith('2026-10-24')
    assert quote['last_trade_at'] is None
    assert quote['last_trade_as_of'] == '2026-10-14'
    assert quote['purchase_available'] is False
    assert quote['last_trade_confirmed'] is False
    assert quote['historical_reconciliation_evidence'] is False
    assert updated['counts']['quotes_available'] == 0
    assert updated['counts']['quotes_indicative_snapshots'] == 1
    assert updated['counts']['quotes_missing'] == 0
    assert len(updated['sources']) == 2
    assert updated['sources'][-1]['sha256'] == quote['source_sha256']
    for field in ('history', 'payments', 'fundamentals', 'model_ready', 'model_pending_reason'):
        assert updated['stocks'][0][field] == original['stocks'][0][field]
    for field in ('treasury', 'collected_at', 'as_of', 'model_preparation'):
        assert updated[field] == original[field]


def test_missing_quote_can_use_catalogue_snapshot_without_a_yahoo_reference(tmp_path):
    value = inventory([stock(status='source_http_error', as_of=None)])
    cache(tmp_path)
    updated, _ = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'][0]['quote']['value'] == 114.01
    assert updated['stocks'][0]['quote']['last_trade_at'] is None
    assert updated['stocks'][0]['quote']['last_trade_as_of'] is None


@pytest.mark.parametrize('previous', ['2026-10-23', '2026-10-24'])
def test_equal_or_older_snapshots_cannot_replace_quote(tmp_path, previous):
    cache(tmp_path)
    value = inventory([stock(as_of=previous)])
    updated, decisions = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'] == value['stocks']
    assert decisions[0]['status'] == 'not_newer'


def test_fresh_trade_quote_is_never_overwritten(tmp_path):
    cache(tmp_path)
    value = inventory([stock(status='available')])
    updated, decisions = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'] == value['stocks']
    assert decisions == []
    assert updated['counts']['quotes_available'] == 1


@pytest.mark.parametrize('status', ['available', 'stale'])
def test_newer_yahoo_quote_is_preserved_even_if_its_status_is_stale(tmp_path, status):
    cache(tmp_path)
    row = stock(status=status, as_of='2026-10-24')
    row['quote']['source_url'] = 'https://query1.finance.yahoo.com/v8/finance/chart/TEST3.SA'
    value = inventory([row])
    updated, _ = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'] == value['stocks']


@pytest.mark.parametrize('previous', ['2026-10-23', '2026-10-23T21:37:25+00:00'])
def test_conflicting_price_with_same_date_or_snapshot_timestamp_does_not_replace(tmp_path, previous):
    cache(tmp_path)
    row = stock(as_of='2026-10-23')
    if 'T' in previous:
        row['quote'].update(snapshot_at=previous, status='stale_indicative_snapshot')
    assert row['quote']['value'] != 114.01
    value = inventory([row])
    updated, decisions = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'] == value['stocks']
    assert decisions[0]['status'] == 'not_newer'


def test_not_found_without_yahoo_preserves_unknown_trade_and_false_readiness(tmp_path):
    cache(tmp_path)
    row = stock(status='not_found', as_of=None)
    row['quote'] = {'value': None, 'status': 'not_found'}
    original = inventory([row])
    updated, _ = module.sync_cached_quotes(tmp_path, original, now=NOW)
    quote = updated['stocks'][0]['quote']
    assert quote['status'] == 'indicative_snapshot'
    assert quote['previous_reference'] == {'value': None, 'status': 'not_found'}
    assert quote['last_trade_at'] is None
    assert quote['last_trade_as_of'] is None
    assert quote['last_trade_source_url'] is None
    assert updated['stocks'][0]['model_ready'] is False
    assert updated['stocks'][0]['model_pending_reason'] == 'cash_pending'


def test_future_retrieval_clock_cannot_refresh_catalogue_quote(tmp_path):
    raw = cache(tmp_path)
    path = tmp_path / (raw[:-5] + '-source.json')
    receipt = json.loads(path.read_bytes())
    receipt['retrieved_at'] = '2026-10-25T00:00:01+00:00'
    write_json(path, receipt)
    original = inventory()
    updated, decisions = module.sync_cached_quotes(tmp_path, original, now=NOW)
    assert updated['stocks'] == original['stocks']
    assert decisions[0]['status'] == 'cache_rejected'


def test_stale_snapshot_remains_stale_despite_recent_retrieval(tmp_path):
    cache(tmp_path, body(regularMarketTime='2026-10-15T20:00:00Z'))
    updated, _ = module.sync_cached_quotes(tmp_path, inventory(), now=NOW)
    assert updated['stocks'][0]['quote']['status'] == 'stale_indicative_snapshot'
    assert updated['counts']['quotes_indicative_snapshots'] == 0
    assert updated['counts']['quotes_stale_indicative_snapshots'] == 1


def test_post_inventory_snapshot_and_corrupt_receipts_preserve_all_fields(tmp_path):
    raw = cache(tmp_path)
    value = inventory()
    value['as_of'] = '2026-10-22'
    updated, decisions = module.sync_cached_quotes(tmp_path, value, now=NOW)
    assert updated['stocks'] == value['stocks']
    assert decisions[0]['status'] == 'cache_rejected'
    (tmp_path / raw).write_bytes(b'{}')
    updated, _ = module.sync_cached_quotes(tmp_path, inventory(), now=NOW)
    assert updated['stocks'] == inventory()['stocks']


@pytest.mark.parametrize('code', [401, 403, 429, 302])
def test_provider_stops_at_denial_and_reuses_all_caches_first(tmp_path, monkeypatch, code):
    prepare(tmp_path, [stock('AAAA3', as_of=None), stock('BBBB3', as_of=None), stock()])
    cache(tmp_path)
    calls = []
    def denied(folder, ticker):
        calls.append(ticker)
        raise urllib.error.HTTPError('https://brapi.dev/api/quote/' + ticker, code, 'denied', {}, None)
    monkeypatch.setattr(module, 'collect', denied)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    model = module.protected_state(tmp_path)
    pointer = (tmp_path / 'inventory/current.json').read_bytes()
    _, report = module.acquire(tmp_path, now=NOW)
    assert calls == ['AAAA3']
    assert report['requests'] == 1
    assert report['reused_caches'] == 1
    assert report['not_requested'] == 1
    assert report['stopped'] == {'ticker': 'AAAA3', 'http_status': code}
    assert report['counts_after']['quotes_indicative_snapshots'] == 1
    assert module.protected_state(tmp_path) == model
    assert (tmp_path / 'inventory/current.json').read_bytes() == pointer


def test_request_ceiling_and_serialized_interval(tmp_path, monkeypatch):
    prepare(tmp_path, [stock(f'T{i:03}3', as_of=None) for i in range(84)])
    ticks, sleeps, calls = [0.], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        ticks[0] += seconds
    def absent(folder, ticker):
        calls.append((ticker, ticks[0]))
        raise urllib.error.HTTPError('https://brapi.dev/api/quote/' + ticker, 404, 'missing', {}, None)
    monkeypatch.setattr(module.time, 'monotonic', lambda: ticks[0])
    monkeypatch.setattr(module.time, 'sleep', sleep)
    monkeypatch.setattr(module, 'collect', absent)
    _, report = module.acquire(tmp_path, now=NOW)
    assert len(calls) == report['requests'] == 82
    assert report['not_requested'] == 2
    assert all(b[1] - a[1] >= .6 - 1e-10 for a, b in zip(calls, calls[1:]))
    assert report['stopped'] is None
    with pytest.raises(ValueError, match='budget'):
        module.acquire(tmp_path, max_requests=83)


@pytest.mark.parametrize('code', [401, 403, 429])
def test_basic_access_denial_blocks_second_cli_run_but_cached_publication_remains_allowed(tmp_path, monkeypatch, capsys, code):
    prepare(tmp_path, [stock('AAAA3', as_of=None), stock()])
    cache(tmp_path)
    calls = []
    def denied(folder, ticker):
        calls.append(ticker)
        raise urllib.error.HTTPError('https://brapi.dev/api/quote/' + ticker, code, 'denied', {}, None)
    monkeypatch.setattr(module, 'collect', denied)
    monkeypatch.setattr(module, 'active_model_preparations', lambda: [])
    assert module.main(['collect', '--data-dir', str(tmp_path)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['requests'] == 1
    assert module.main(['collect', '--data-dir', str(tmp_path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second['requests'] == 0
    assert second['reused_caches'] == 1
    assert second['stopped'] == {'ticker': 'AAAA3', 'http_status': code, 'previous_run': True}
    assert calls == ['AAAA3']
    assert module.main(['publish-inventory', '--data-dir', str(tmp_path), '--report', second['report']]) == 0
    assert load_inventory(tmp_path)['counts']['quotes_indicative_snapshots'] == 1


def test_denied_dividends_endpoint_does_not_block_basic_quotes(tmp_path, monkeypatch):
    prepare(tmp_path)
    denial = tmp_path / 'raw/current-quotes/basic-access-denials/unrelated.json'
    denial.parent.mkdir(parents=True)
    write_json(denial, {'schema_version': 1, 'provider': 'brapi', 'http_status': 403,
                       'endpoint': 'https://brapi.dev/api/quote/{ticker}?dividends=true', 'ticker': 'TEST3'})
    calls = []
    def absent(folder, ticker):
        calls.append(ticker)
        raise urllib.error.HTTPError('https://brapi.dev/api/quote/' + ticker, 404, 'absent', {}, None)
    monkeypatch.setattr(module, 'collect', absent)
    _, report = module.acquire(tmp_path, now=NOW)
    assert report['stopped'] is None
    assert calls == ['TEST3']
    assert report['requests'] == 1


def test_invalid_existing_cache_is_not_requested_again(tmp_path, monkeypatch):
    prepare(tmp_path)
    raw = cache(tmp_path)
    (tmp_path / raw).write_bytes(b'corrupt')
    monkeypatch.setattr(module, 'collect', lambda *_: pytest.fail('network repeated'))
    _, report = module.acquire(tmp_path, now=NOW)
    assert report['requests'] == 0
    assert report['outcomes'] == [{'ticker': 'TEST3', 'status': 'cache_rejected'}]


def test_explicit_offline_cli_publishes_only_inventory(tmp_path, monkeypatch, capsys):
    original = prepare(tmp_path)
    cache(tmp_path)
    monkeypatch.setattr(module, 'active_model_preparations', lambda: [])
    monkeypatch.setattr(module, 'collect', lambda *_: pytest.fail('network during publication'))
    report_path, report = module.acquire(tmp_path, now=NOW)
    assert module.main(['publish-inventory', '--data-dir', str(tmp_path), '--report', str(report_path)]) == 0
    assert json.loads(capsys.readouterr().out)['published_inventory_only'] is True
    published = load_inventory(tmp_path)
    assert published['stocks'][0]['quote']['status'] == 'indicative_snapshot'
    assert published['stocks'][0]['model_ready'] == original['stocks'][0]['model_ready']
    assert module.protected_state(tmp_path) == report['protected_state']
    assert json.loads(report_path.read_bytes())['published'] is False


@pytest.mark.parametrize('changed', ['model', 'inventory', 'candidate', 'active_prepare'])
def test_publish_refuses_concurrent_or_changed_inputs(tmp_path, monkeypatch, changed):
    prepare(tmp_path)
    cache(tmp_path)
    report_path, _ = module.acquire(tmp_path, now=NOW)
    monkeypatch.setattr(module, 'active_model_preparations', lambda: [123] if changed == 'active_prepare' else [])
    if changed == 'model':
        (tmp_path / 'snapshots/model-one/returns.csv').write_bytes(b'changed')
    elif changed == 'inventory':
        publish_inventory(tmp_path, inventory([stock('OTHER3')]))
    elif changed == 'candidate':
        pointer = tmp_path / 'raw/current-quotes/TEST3-current.json'
        pointer.write_bytes(b'{}')
    before = (tmp_path / 'inventory/current.json').read_bytes()
    with pytest.raises(ValueError):
        module.publish_from_report(tmp_path, report_path, now=NOW)
    assert (tmp_path / 'inventory/current.json').read_bytes() == before


def test_publication_requires_explicit_report_and_operation_lock(tmp_path):
    with pytest.raises(SystemExit):
        module.main(['publish-inventory', '--data-dir', str(tmp_path)])
    with module._operation_lock(tmp_path):
        with pytest.raises(FileExistsError):
            with module._operation_lock(tmp_path):
                pytest.fail('concurrent operation allowed')
