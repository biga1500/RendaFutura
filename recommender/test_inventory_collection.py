from datetime import date, datetime, timezone
from copy import deepcopy
import hashlib
import json

import pytest

from .collect_inventory import identities, parse_quote, payment_component


def quote_fixture(ticker='TEST3.SA', day=date(2026, 10, 23), closes=None):
    stamp = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return json.dumps({'chart': {'result': [{'meta': {'symbol': ticker, 'currency': 'BRL',
        'regularMarketTime': stamp, 'regularMarketPrice': 20}, 'timestamp': [stamp],
        'indicators': {'quote': [{'close': [20] if closes is None else closes}]}}]}}).encode()


def test_new_instrument_with_one_quote_is_catalogued_without_five_year_gate():
    result = parse_quote(quote_fixture(), 'TEST3', date(2026, 10, 24))
    assert result['quote']['value'] == 20 and result['history']['observed_months'] == 1
    assert result['history']['paid_cash_inferred_from_ex_dates'] is False


def test_stale_quote_retains_its_value_and_date_without_claiming_current():
    result = parse_quote(quote_fixture(day=date(2026, 9, 1)), 'TEST3', date(2026, 10, 24))
    assert result['quote']['status'] == 'stale' and result['quote']['as_of'] == '2026-09-01'


def test_daily_history_selection_does_not_filter_by_current_quote_availability(tmp_path, monkeypatch):
    from . import collect_inventory as module
    rows = [{'ticker': 'TEST3', 'quote': {'status': 'available'}},
            {'ticker': 'OLDX3', 'quote': {'status': 'stale'}},
            {'ticker': 'NONE3', 'quote': {'status': 'source_unavailable'}}]
    calls = []
    monkeypatch.setattr(module, 'collect_quotes', lambda *args, **kwargs: calls.append((args, kwargs)))
    module.collect_daily_histories(tmp_path, {'stocks': rows}, date(2026, 10, 24), workers=1)
    assert calls[0][0][1] == rows
    assert calls[0][1] == {'workers': 1, 'interval': '1d', 'reuse_existing': True}


def test_existing_daily_originals_are_not_refetched_or_changed_by_default(tmp_path, monkeypatch):
    from . import collect_inventory as module
    import os
    path = tmp_path / 'raw/inventory/daily-prices/TEST3.json'
    path.parent.mkdir(parents=True)
    original = quote_fixture(day=date(2026, 9, 1))
    path.write_bytes(original)
    os.utime(path, (1, 1))
    def unexpected_network(*args, **kwargs):
        raise AssertionError('Historical originals must not be silently overwritten.')
    monkeypatch.setattr(module.urllib.request, 'urlopen', unexpected_network)
    result = module.collect_daily_histories(tmp_path, {'stocks': [{'ticker': 'TEST3'}]},
        date(2026, 10, 24), workers=1)
    assert path.read_bytes() == original
    assert result['TEST3']['quote']['status'] == 'stale'
    assert result['TEST3']['history']['status'] == 'available'


def daily_receipt(tmp_path, day=date(2026, 10, 23)):
    relative = 'raw/inventory/daily-prices/TEST3.json'
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    body = quote_fixture(day=day)
    path.write_bytes(body)
    return {'file': relative, 'sha256': hashlib.sha256(body).hexdigest(),
        'url': 'https://query1.finance.yahoo.com/v8/finance/chart/TEST3.SA?range=10y&interval=1d&events=div%2Csplits'}


def test_catalogue_sync_reparses_history_without_promoting_model_readiness(tmp_path):
    from .collect_inventory import sync_daily_results
    source = daily_receipt(tmp_path)
    inventory = {'stocks': [{'ticker': 'TEST3', 'quote': {'status': 'source_unavailable'},
        'history': {'status': 'not_found'}, 'model_ready': False,
        'payments': {'status': 'not_found'}, 'model_pending_reason': 'independent_cash_cache_missing'}],
        'sources': [], 'counts': {'model_ready_stocks': 0}}
    original = deepcopy(inventory)
    results = {'TEST3': {'source': source, 'quote': {'status': 'available', 'value': 999999},
        'history': {'status': 'available', 'observed_months': 999}}}
    result = sync_daily_results(tmp_path, inventory, results, date(2026, 10, 25))
    assert inventory == original
    stock = result['stocks'][0]
    assert stock['history']['observed_months'] == 1
    assert stock['quote']['value'] == 20
    assert not stock['model_ready'] and stock['payments'] == original['stocks'][0]['payments']
    assert stock['model_pending_reason'] == 'independent_cash_cache_missing'
    assert result['counts']['model_ready_stocks'] == 0
    assert result['counts']['daily_histories_available'] == 1


def test_failed_history_download_never_erases_existing_quote_or_proves_delisting(tmp_path):
    from .collect_inventory import sync_daily_results
    inventory = {'stocks': [{'ticker': 'TEST3', 'quote': {'value': 20, 'status': 'available'},
        'history': {'status': 'available', 'observed_months': 60}}], 'counts': {}, 'sources': []}
    result = sync_daily_results(tmp_path, inventory, {'TEST3': {'quote': {
        'status': 'source_http_error', 'http_status': 404}, 'history': {'status': 'not_fetched'}}}, date(2026, 10, 25))
    assert result['stocks'][0]['quote'] == inventory['stocks'][0]['quote']
    assert result['stocks'][0]['history'] == inventory['stocks'][0]['history']
    assert result['stocks'][0]['daily_history_collection']['absence_is_not_delisting_or_zero_distribution']


def test_catalogue_sync_rejects_changed_original_or_wrong_identity(tmp_path):
    from .collect_inventory import sync_daily_results
    source = daily_receipt(tmp_path)
    inventory = {'stocks': [{'ticker': 'TEST3', 'quote': {}, 'history': {}}], 'counts': {}, 'sources': []}
    result = {'TEST3': {'source': source}}
    source['url'] = source['url'].replace('TEST3.SA', 'OTHER3.SA')
    with pytest.raises(ValueError, match='identity'):
        sync_daily_results(tmp_path, inventory, result, date(2026, 10, 25))
    source = daily_receipt(tmp_path)
    result['TEST3']['source'] = source
    inventory['sources'] = [{**source, 'sha256': 'a' * 64}]
    with pytest.raises(ValueError, match='original_changed'):
        sync_daily_results(tmp_path, inventory, result, date(2026, 10, 25))
    inventory['sources'] = []
    (tmp_path / source['file']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash_mismatch'):
        sync_daily_results(tmp_path, inventory, result, date(2026, 10, 25))


def test_wrong_identity_or_future_prices_are_rejected():
    with pytest.raises(ValueError):
        parse_quote(quote_fixture(ticker='OTHER3.SA'), 'TEST3', date(2026, 10, 24))
    with pytest.raises(ValueError):
        parse_quote(quote_fixture(day=date(2026, 10, 25)), 'TEST3', date(2026, 10, 24))


def test_catalogue_preserves_units_missing_identity_and_fractional_aliases(tmp_path):
    (tmp_path / 'catalog.json').write_text(json.dumps({'stocks': [
        {'ticker': 'TEST3', 'name': 'Company'}, {'ticker': 'TEST3F'},
        {'ticker': 'TEST11', 'name': 'Other structure'}]}), encoding='utf-8')
    rows = identities(tmp_path)
    assert len(rows) == 2 and rows[0]['aliases'] == ['TEST3F']
    assert rows[1]['ticker'] == 'TEST11' and rows[1]['share_class'] is None
    assert rows[0]['identity_status'] == 'not_resolved'


def test_primary_document_discovery_never_becomes_a_cash_observation(tmp_path):
    primary = {'events': [], 'documents': [{'url': 'https://www.rad.cvm.gov.br/document',
        'published_at': '2026-10-20', 'status': 'document_reference_only'}]}
    result, sources = payment_component(tmp_path, 'TEST3', date(2026, 10, 24), primary, [])
    assert result['status'] == 'partial'
    assert result['event_count'] is None and result['events'] == []
    assert result['primary_document_count'] == 1
    assert not result['primary_documents_are_cash_observations']
    assert not result['cash_model_eligible'] and not sources


def test_changed_table_identifier_is_parsed_without_minimum_payment_gate(tmp_path):
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    path.parent.mkdir(parents=True)
    path.write_text('<title>TEST3</title><table id="new-format"><tr>'
        '<th>Tipo</th><th>Data com</th><th>Pagamento</th><th>Valor</th></tr>'
        '<tr><td>JCP</td><td>01/06/2026</td><td>20/06/2026</td><td>0,25</td></tr></table>')
    result, sources = payment_component(tmp_path, 'TEST3', date(2026, 10, 24), None, [])
    assert result['event_count'] == 1
    assert result['authority'] == 'secondary'
    assert result['events'][0]['gross_per_share'] == .25
    assert result['events'][0]['status'] == 'past_scheduled'
    assert sources[0]['sha256'] and not result['cash_model_eligible']


def test_primary_cash_rows_use_exact_download_source_and_do_not_mix_secondary_rows(tmp_path):
    primary = {'status': 'available', 'events': [{'gross_per_share': .5}], 'documents': [],
        'field_status': {}, 'report': {'requested_window': {'start': '2025-10-25', 'end': '2026-10-24'},
            'issues': [], 'history_complete': False},
        'source_results': [{'events': [], 'report': {'sha256': 'index'}},
                           {'events': [{'gross_per_share': .5}], 'report': {'sha256': 'issuer-xlsx'}}]}
    result, _ = payment_component(tmp_path, 'PETR4', date(2026, 10, 24), primary,
        [{'sha256': 'issuer-xlsx', 'url': 'https://issuer.example/exact-file.xlsx', 'authority': 'primary'}])
    assert result['source_url'] == 'https://issuer.example/exact-file.xlsx'
    assert result['event_count'] == 1 and result['authority'] == 'primary'
    assert not result['cash_model_eligible']
