from copy import deepcopy
import hashlib
import json
import socket

import pytest

from . import published_issuer_quotes as quotes


@pytest.fixture(scope='module')
def receipts():
    return {row['ticker']: row for row in [json.loads(path.read_bytes())
            for path in sorted((quotes.ROOT / quotes.CACHE).glob('response-*.json'))]}


@pytest.fixture(scope='module')
def reports():
    return {ticker: quotes.inspect_alignment(quotes.ROOT, ticker) for ticker in ('ITSA3', 'ITSA4')}


def test_two_real_independent_series_have_full_required_window(receipts):
    assert set(receipts) == {'ITSA3', 'ITSA4'}
    for ticker, receipt in receipts.items():
        assert receipt['status'] == 200 and receipt['authentication'] == 'none'
        assert not receipt['proxy'] and not receipt['redirects'] and not receipt['automatic_retries']
        assert not receipt['issuer_bundle_version_verified'] and not receipt['requested_adjusted']
        rows = quotes.quote_rows(quotes.read_original(quotes.ROOT, receipt), ticker)
        assert len(rows) == 1268 and len({day.strftime('%Y-%m') for day in rows}) == 61
        assert min(rows).isoformat() == '2021-09-01'
        assert max(rows).isoformat() == '2026-09-30'


@pytest.mark.parametrize('ticker,count', [('ITSA3', 489), ('ITSA4', 490)])
def test_actual_historical_disagreements_are_retained_not_fitted_or_force_accepted(reports, ticker, count):
    report = reports[ticker]
    assert len(report['conflicts']) == count
    assert report['conflicts'][0]['date'] == '2021-09-01'
    assert report['conflicts'][-1]['date'] == '2023-08-17'
    assert report['normal_price_gate']['status'] == 'blocked'
    assert not report['extra_factors_fitted'] and not report['missing_prices_created']
    assert not report['corporate_action_type_inferred_from_price_ratio'] and not report['model_admitted']


@pytest.mark.parametrize('change', ['ticker', 'provider', 'error', 'month', 'duplicate', 'nan', 'future', 'bool'])
def test_wrong_provider_or_invalid_history_cannot_supply_independent_quotes(receipts, change):
    payload = json.loads(quotes.read_original(quotes.ROOT, receipts['ITSA4']))
    history = payload['data']['tickers'][0]['history']
    if change == 'ticker':
        payload['data']['tickers'][0]['ticker'] = 'ITSA3'
    elif change == 'provider':
        payload['data']['source'] = 'Unknown'
    elif change == 'error':
        payload['data']['errors'] = ['partial']
    elif change == 'month':
        payload['data']['tickers'][0]['history'] = [row for row in history if not row['date'].startswith('2021-09')]
    elif change == 'duplicate':
        history.append(deepcopy(history[0]))
    elif change == 'nan':
        history[0]['closed'] = float('nan')
    elif change == 'future':
        history[0]['date'] = '2026-10-01'
    else:
        history[0]['closed'] = True
    with pytest.raises(ValueError):
        quotes.quote_rows(json.dumps(payload).encode(), 'ITSA4')


def test_inspection_is_offline_does_not_change_live_assets_or_claim_unknown_source_dates(monkeypatch, reports):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    protected = [quotes.ROOT / 'current.json', quotes.ROOT / 'inventory/current.json',
                 quotes.ROOT / 'stock-selection/current.json']
    before = {path: path.read_bytes() for path in protected}
    assert quotes.inspect_alignment(quotes.ROOT, 'ITSA4') == reports['ITSA4']
    assert all(path.read_bytes() == before[path] for path in protected)
    for source in reports['ITSA4']['sources']:
        assert hashlib.sha256((quotes.ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
    assert reports['ITSA4']['sources'][1]['retrieved_at'] is None


@pytest.mark.parametrize('ticker', ['ITSA3', 'ITSA4'])
def test_research_respects_selected_positive_yield_scope(monkeypatch, ticker):
    monkeypatch.setattr(quotes, 'allowed_tickers', lambda folder: frozenset({'PETR4'}))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    with pytest.raises(ValueError, match='issuer_quote_outside_selected_scope'):
        quotes.inspect_alignment(quotes.ROOT, ticker)


def test_original_receipt_cannot_substitute_other_bytes(receipts):
    receipt = {**receipts['ITSA3'], 'sha256': '0' * 64}
    with pytest.raises(ValueError, match='provider_original_changed'):
        quotes.read_original(quotes.ROOT, receipt)
