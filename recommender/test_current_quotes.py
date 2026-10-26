from datetime import date, datetime, timezone
import json

import pytest

from .current_quotes import stock_quote


def fixture(price=25., day=date(2026, 10, 23), events=None):
    timestamp = int(datetime(day.year, day.month, day.day, 18, tzinfo=timezone.utc).timestamp())
    return json.dumps({'chart': {'result': [{'meta': {'symbol': 'TEST3.SA', 'currency': 'BRL',
                       'regularMarketPrice': price, 'regularMarketTime': timestamp}, 'events': events or {}}]}}).encode()


def test_recent_quote_is_not_the_historical_model_price():
    quote = stock_quote(fixture(), 'TEST3', date(2026, 9, 30), today=date(2026, 10, 24))
    assert quote['price'] == 25 and quote['as_of'] == '2026-10-23'
    assert quote['basis'] == 'last_observed_trade_not_order_offer'


@pytest.mark.parametrize('day', [date(2026, 9, 29), date(2026, 10, 25)])
def test_future_or_pre_model_quotes_are_rejected(day):
    with pytest.raises(ValueError):
        stock_quote(fixture(day=day), 'TEST3', date(2026, 9, 30), today=date(2026, 10, 24))


def test_stale_observation_is_preserved_but_cannot_be_used_as_current_purchase_quote():
    quote = stock_quote(fixture(day=date(2026, 10, 1)), 'TEST3', date(2026, 9, 30), today=date(2026, 10, 24))
    assert quote['price'] == 25
    assert quote['as_of'] == '2026-10-01'
    assert quote['purchase_available'] is False
    assert quote['basis'] == 'last_observed_trade_stale_not_current_purchase_reference'


def test_post_model_share_event_blocks_unreconciled_current_quote():
    stamp = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    with pytest.raises(ValueError, match='normalization'):
        stock_quote(fixture(events={'splits': {'one': {'date': stamp}}}), 'TEST3', date(2026, 9, 30), today=date(2026, 10, 24))


@pytest.mark.parametrize('price', [0, -1, True, float('nan')])
def test_invalid_current_quote_price_rejected(price):
    with pytest.raises(ValueError):
        stock_quote(fixture(price=price), 'TEST3', date(2026, 9, 30), today=date(2026, 10, 24))
