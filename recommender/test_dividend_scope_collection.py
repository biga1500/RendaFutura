"""Collector boundaries enforce the imported stock scope without live sources."""

from copy import deepcopy
from datetime import date
import hashlib
import io
import json
import socket
from unittest.mock import Mock
from urllib.parse import urlparse

import pytest

from . import collect_income_data as income
from . import collect_inventory as inventory
from . import collect_market_data as market
from . import cvm_fundamentals, payment_sources
from .dividend_scope import OUTSIDE_REASON, import_scope, scope_metadata
from .test_inventory_collection import quote_fixture


AS_OF = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def forbid_live_sources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Live network and credentials are forbidden in collection tests.')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(market, 'brapi_token', forbidden)


def configure_scope(folder, selected=('PETR4',)):
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / 'supplied.tsv'
    tickers = ('PETR4', 'PETR3', 'VALE3', 'MISS3')
    source.write_bytes(('Papel\tDiv.Yield\n' + ''.join(
        ticker + ('\t1,25%\n' if ticker in selected else '\t0,00%\n')
        for ticker in tickers)).encode('utf-8'))
    return import_scope(folder, source)


def collector_folder(folder, use_snapshot):
    if use_snapshot:
        snapshot = folder / 'snapshots/model1'
        snapshot.mkdir(parents=True, exist_ok=True)
        return snapshot
    return folder


@pytest.mark.parametrize('interval', ['1mo', '1d'])
@pytest.mark.parametrize('use_snapshot', [False, True])
def test_quote_requests_use_exact_selected_ticker_only(tmp_path, monkeypatch, interval, use_snapshot):
    configure_scope(tmp_path)
    requested = []

    def request_quote(request, **kwargs):
        requested.append(request.full_url)
        ticker = urlparse(request.full_url).path.rsplit('/', 1)[-1]
        return io.BytesIO(quote_fixture(ticker=ticker))

    monkeypatch.setattr(inventory.urllib.request, 'urlopen', request_quote)
    monkeypatch.setattr(inventory.time, 'sleep', lambda _: None)
    records = [{'ticker': ticker} for ticker in ('PETR4', 'PETR3', 'VALE3', 'PETR4F', 'PETR4.SA', 'petr4')]
    original = deepcopy(records)
    result = inventory.collect_quotes(collector_folder(tmp_path, use_snapshot), records, AS_OF,
                                      workers=1, interval=interval)
    assert records == original
    assert list(result) == ['PETR4']
    assert result['PETR4']['quote']['status'] == 'available'
    assert requested == [f'https://query1.finance.yahoo.com/v8/finance/chart/PETR4.SA?range=10y&interval={interval}&events=div%2Csplits']
    cache = tmp_path / ('raw/inventory/daily-prices' if interval == '1d' else 'raw/inventory/quotes')
    assert [path.name for path in cache.iterdir()] == ['PETR4.json']


def test_unconfigured_quote_collector_keeps_all_tickers(tmp_path, monkeypatch):
    requested = []

    def request_quote(request, **kwargs):
        ticker = urlparse(request.full_url).path.rsplit('/', 1)[-1]
        requested.append(ticker)
        return io.BytesIO(quote_fixture(ticker=ticker))

    monkeypatch.setattr(inventory.urllib.request, 'urlopen', request_quote)
    monkeypatch.setattr(inventory.time, 'sleep', lambda _: None)
    result = inventory.collect_quotes(tmp_path, [{'ticker': 'PETR4'}, {'ticker': 'VALE3'}], AS_OF, workers=1)
    assert set(result) == {'PETR4', 'VALE3'}
    assert requested == ['PETR4.SA', 'VALE3.SA']


def test_daily_entrypoint_filters_before_delegating_without_freshness_gate(tmp_path, monkeypatch):
    configure_scope(tmp_path)
    rows = [{'ticker': 'PETR4', 'quote': {'status': 'source_unavailable'}},
            {'ticker': 'VALE3', 'quote': {'status': 'available'}}]
    quote_collector = Mock(return_value={})
    monkeypatch.setattr(inventory, 'collect_quotes', quote_collector)
    inventory.collect_daily_histories(tmp_path, {'stocks': rows}, AS_OF, workers=1)
    quote_collector.assert_called_once_with(tmp_path, rows[:1], AS_OF, workers=1,
                                            interval='1d', reuse_existing=True)


def test_daily_sync_labels_excluded_receipts_without_reading_them(tmp_path):
    configure_scope(tmp_path)
    old = {'stocks': [{'ticker': 'VALE3', 'quote': {'status': 'available', 'value': 20},
                       'history': {'status': 'available', 'observed_months': 61}}],
           'sources': [], 'counts': {}}
    result = inventory.sync_daily_results(tmp_path, old, {'VALE3': {
        'source': {'file': 'excluded-file-that-must-not-be-read', 'url': 'invalid'}}}, AS_OF)
    row = result['stocks'][0]
    assert row['daily_history_collection']['status'] == 'outside_selected_scope'
    assert row['daily_history_collection']['reason'] == OUTSIDE_REASON
    assert row['quote'] == old['stocks'][0]['quote']
    assert result['counts']['daily_histories_available'] == 0


def test_excluded_payment_component_does_not_inspect_cached_or_primary_rows(tmp_path, monkeypatch):
    configure_scope(tmp_path)
    cache = tmp_path / 'raw/expanded/VALE3-payments.html'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'This excluded cache must never be inspected.')
    inspect = Mock(side_effect=AssertionError('Excluded payment lookup.'))
    monkeypatch.setattr(payment_sources, 'inspect_payment_page', inspect)
    component, sources = inventory.payment_component(tmp_path, 'VALE3', AS_OF, {'events': [None]}, [])
    assert component['status'] == 'outside_selected_scope'
    assert component['reason'] == OUTSIDE_REASON
    assert component['event_count'] is None and not sources
    inspect.assert_not_called()
    assert not (tmp_path / 'raw/inventory/payments').exists()


@pytest.mark.parametrize('selection', [None, ('PETR4', 'MISS3'), ('MISS3',)])
@pytest.mark.parametrize('saved_quotes', [False, True])
def test_monthly_inventory_retains_468_identities_and_scopes_every_lookup(
        tmp_path, monkeypatch, selection, saved_quotes):
    scoped = selection is not None
    if scoped:
        configure_scope(tmp_path, selection)
    tickers = ['PETR4', *[f'T{index:03}3' for index in range(467)]]
    (tmp_path / 'catalog.json').write_text(json.dumps({'stocks': [
        {'ticker': ticker, 'name': ticker} for ticker in tickers]}), encoding='utf-8')
    detail = tmp_path / 'raw/expanded/b3-detail-fixture.json'
    detail.parent.mkdir(parents=True)
    detail.write_text(json.dumps({'codeCVM': '9512', 'otherCodes': [
        {'code': 'PETR4', 'isin': 'BRPETRACNPR6'}, {'code': 'T0003', 'isin': 'BRTESTACNOR1'}]}), encoding='utf-8')
    snapshot = collector_folder(tmp_path, True)
    (tmp_path / 'current.json').write_text(json.dumps({'snapshot': 'snapshots/model1'}), encoding='utf-8')
    (snapshot / 'assets.json').write_text(json.dumps({
        'assets': [{'ticker': 'PETR4'}], 'reference_date': '2026-09-30', 'bonds': []}), encoding='utf-8')
    (snapshot / 'collection-report.json').write_text('{}', encoding='utf-8')
    expected = [ticker for ticker in tickers if not scoped or ticker in selection]
    lookups = {}

    def cached_registry(url, name, raw, sources, refresh):
        lookups['registry_url'] = url
        sources.append({'url': url, 'file': name})
        return b'Official shared registry fixture.'

    def quotes(folder, records, as_of):
        lookups['quotes'] = [row['ticker'] for row in records]
        return {row['ticker']: {'quote': {'status': 'available', 'value': 20},
                                'history': {'status': 'available', 'observed_months': 61}}
                for row in records}

    def fundamentals(folder, specs, as_of, **kwargs):
        lookups['financials'] = list(specs)
        return {'sources': [], 'instruments': {ticker: {'fields': {}} for ticker in specs}}

    def primary(folder, specs, as_of, **kwargs):
        lookups['primary'] = [row['ticker'] for row in specs]
        return {'manifest': [], 'by_ticker': {}}

    def payment(folder, ticker, *args):
        lookups.setdefault('payments', []).append(ticker)
        return {'status': 'not_found', 'event_count': None}, []

    from . import official_treasury_bundle
    treasury = Mock(return_value={'instruments': [], 'sources': [], 'issues': []})
    monkeypatch.setattr(inventory, 'cached_source', cached_registry)
    monkeypatch.setattr(inventory, 'parse_fca', lambda *args: {'instruments': {}, 'issues': []})
    monkeypatch.setattr(inventory, 'collect_quotes', quotes)
    monkeypatch.setattr(cvm_fundamentals, 'collect_fundamentals', fundamentals)
    monkeypatch.setattr(payment_sources, 'collect_primary_payments', primary)
    monkeypatch.setattr(inventory, 'payment_component', payment)
    monkeypatch.setattr(official_treasury_bundle, 'collect_treasury_bundle', treasury)
    monkeypatch.setattr(inventory, 'publish_inventory', Mock(side_effect=AssertionError('No publication.')))
    if saved_quotes:
        summary = tmp_path / 'raw/inventory/quote-summary.json'
        summary.parent.mkdir(parents=True)
        summary.write_text(json.dumps({'as_of': AS_OF.isoformat(), 'instruments': quotes(
            tmp_path, [{'ticker': ticker} for ticker in tickers], AS_OF)}), encoding='utf-8')
        del lookups['quotes']
    result = inventory.collect_inventory(snapshot, AS_OF, activate=False)
    assert len(result['stocks']) == result['counts']['stocks'] == 468
    assert {row['ticker'] for row in result['stocks']} == set(tickers)
    assert set(lookups['financials']) == set(lookups.get('payments', [])) == set(expected)
    assert set(lookups['primary']) == (set(expected) & {'PETR4', 'T0003'})
    assert lookups.get('quotes') == (None if saved_quotes or not expected else sorted(expected))
    assert lookups['registry_url'] == 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip'
    treasury.assert_called_once_with(tmp_path, AS_OF, refresh=False)
    if scoped:
        assert result['dividend_scope'] == scope_metadata(tmp_path)
        for row in result['stocks']:
            if row['ticker'] in expected:
                assert row['quote']['status'] == 'available'
                continue
            for name in ('quote', 'history', 'fundamentals', 'payments'):
                assert row[name]['status'] == 'outside_selected_scope'
                assert row[name]['reason'] == OUTSIDE_REASON
            assert row['quote']['value'] is None and row['payments']['event_count'] is None
            assert row['history']['observed_months'] is None
    else:
        assert 'dividend_scope' not in result
        assert all(row['quote']['status'] == 'available' for row in result['stocks'])


class SharedSourcesReached(Exception):
    pass


@pytest.mark.parametrize('collector', [market, income], ids=['market', 'income'])
@pytest.mark.parametrize('selected', [None, ('PETR4',), ('VALE3',), ('MISS3',)])
def test_legacy_stock_sources_and_cvm_specs_are_scoped_but_shared_sources_stay(
        tmp_path, monkeypatch, collector, selected):
    if selected is not None:
        configure_scope(tmp_path, selected)
    calls = []

    def cached_source(url, name, raw, sources, refresh, **kwargs):
        calls.append((url, name, raw, refresh))
        sources.append({'file': name, 'url': url})
        if name == 'petr-ri.html':
            return (b'<a href="https://example.test/?src=https%3A%2F%2Ffilemanager-cdn.mziq.com%2Fhistory.xlsx">'
                    b'Download do arquivo em Excel</a>')
        if name == 'bcb-ipca.json':
            return b'[]'
        return b'Mocked public source.'

    def stop(*args):
        raise SharedSourcesReached

    parse_statements = Mock(return_value=[])
    monkeypatch.setattr(collector, 'cached_source', cached_source)
    monkeypatch.setattr(collector, 'stock_history', lambda *args, **kwargs: ({}, {}, []))
    monkeypatch.setattr(collector, 'cvm_statements', parse_statements)
    if collector is market:
        monkeypatch.setattr(collector, 'load_reconciliations', lambda *args: {})
        monkeypatch.setattr(collector, 'treasury_history', stop)
    else:
        monkeypatch.setattr(collector, 'petrobras_payments', lambda *args: ([], []))
        monkeypatch.setattr(collector, 'vale_payments', lambda *args: ([], []))
        monkeypatch.setattr(collector, 'verified_raw_prices', lambda *args: {})
        monkeypatch.setattr(collector, 'treasury_prices', lambda *args: {})
        monkeypatch.setattr(collector, 'treasury_payments', lambda *args: {})
        monkeypatch.setattr(collector, 'select_bonds', stop)
    with pytest.raises(SharedSourcesReached):
        collector.collect(tmp_path, refresh=True, activate=False)
    expected = {ticker: spec for ticker, spec in market.STOCKS.items()
                if selected is None or ticker in selected}
    names = {name for _, name, _, _ in calls}
    assert {name for name in names if name.startswith('brapi-') and name.endswith('.json')
            and not name.startswith(('brapi-raw-', 'brapi-v2-'))} == {
                f'brapi-{ticker}.json' for ticker in expected}
    assert parse_statements.call_count == date.today().year - 2010
    for call in parse_statements.call_args_list:
        assert call.kwargs.get('specs', market.STOCKS) == expected
    for year in range(2010, date.today().year):
        assert next(call for call in calls if call[1] == f'cvm-dfp-{year}.zip') == (
            f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip',
            f'cvm-dfp-{year}.zip', tmp_path / 'raw', collector is market)
    assert 'bcb-ipca.json' in names and 'tesouro-precos.csv' in names
    if collector is income:
        assert ('petr-ri.html' in names) == ('PETR4' in expected)
        assert ('petr-history.xlsx' in names) == ('PETR4' in expected)
        assert ('vale-ri.html' in names) == ('VALE3' in expected)
        assert ('vale-jcp-2020.html' in names) == ('VALE3' in expected)
        assert ('brapi-v2-VALE3.json' in names) == ('VALE3' in expected)
        assert {name for name in names if name.startswith('brapi-raw-prices-')} == {
            f'brapi-raw-prices-{ticker}.json' for ticker in expected}
        assert 'treasury-coupons.csv' in names
    assert not (tmp_path / 'current.json').exists()
    assert not (tmp_path / 'snapshots').exists()


@pytest.mark.parametrize('entrypoint', ['quotes', 'daily', 'monthly', 'identities', 'payments', 'market', 'income'])
@pytest.mark.parametrize('invalid_selection', ['PETR4', ['PETR4', True], {'PETR4': True}])
def test_invalid_configured_selection_types_block_before_any_request(
        tmp_path, monkeypatch, entrypoint, invalid_selection):
    configure_scope(tmp_path)
    base = tmp_path / 'stock-selection'
    pointer_path = base / 'current.json'
    pointer = json.loads(pointer_path.read_bytes())
    manifest = json.loads((base / pointer['manifest']).read_bytes())
    manifest['selected_tickers'] = invalid_selection
    content = json.dumps(manifest).encode('utf-8')
    digest = hashlib.sha256(content).hexdigest()
    pointer.update(manifest=f'manifests/{digest}.json', sha256=digest)
    (base / pointer['manifest']).write_bytes(content)
    pointer_path.write_text(json.dumps(pointer), encoding='utf-8')
    request = Mock(side_effect=AssertionError('Invalid policy reached a source.'))
    monkeypatch.setattr(inventory.urllib.request, 'urlopen', request)
    monkeypatch.setattr(inventory, 'cached_source', request)
    monkeypatch.setattr(market, 'cached_source', request)
    monkeypatch.setattr(income, 'cached_source', request)
    invoke = {
        'quotes': lambda: inventory.collect_quotes(tmp_path, [{'ticker': 'PETR4'}], AS_OF),
        'daily': lambda: inventory.collect_daily_histories(tmp_path, {'stocks': [{'ticker': 'PETR4'}]}, AS_OF),
        'monthly': lambda: inventory.collect_inventory(tmp_path, AS_OF, activate=False),
        'identities': lambda: inventory.collect_identities(tmp_path, AS_OF),
        'payments': lambda: inventory.payment_component(tmp_path, 'PETR4', AS_OF, None, []),
        'market': lambda: market.collect(tmp_path, activate=False),
        'income': lambda: income.collect(tmp_path, activate=False),
    }
    with pytest.raises(ValueError, match='dividend_scope'):
        invoke[entrypoint]()
    request.assert_not_called()
    assert not (tmp_path / 'raw').exists()
