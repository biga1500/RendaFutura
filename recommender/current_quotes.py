"""Observed quotes are separate from the monthly model's price reference."""

from datetime import date, datetime, timezone
import json
import math


def stock_quote(content, ticker, cutoff, *, today=None):
    today = today or date.today()
    result = json.loads(content)['chart']['result'][0]
    meta = result['meta']
    source = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA'
    if meta.get('symbol') != ticker + '.SA' or meta.get('currency') != 'BRL':
        raise ValueError('Current quote ticker or currency mismatch.')
    timestamp, price = meta.get('regularMarketTime'), meta.get('regularMarketPrice')
    if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError('Current quote timestamp missing.')
    day = datetime.fromtimestamp(timestamp, timezone.utc).date()
    if not cutoff <= day <= today:
        raise ValueError('Current quote pre-model or future dated.')
    if not isinstance(price, (float, int)) or isinstance(price, bool) or not math.isfinite(price) or price <= 0:
        raise ValueError('Current quote price missing.')
    for split in result.get('events', {}).get('splits', {}).values():
        if cutoff < datetime.fromtimestamp(split['date'], timezone.utc).date() <= day:
            raise ValueError('A share event after the model cutoff requires new normalization.')
    fresh = (today - day).days <= 7
    return {'price': float(price), 'as_of': day.isoformat(), 'source_url': source,
            'purchase_available': fresh, 'basis': 'last_observed_trade_not_order_offer' if fresh else
                'last_observed_trade_stale_not_current_purchase_reference',
            'last_trade_at': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            'last_trade_source_url': source,
            'minimum_fraction': 1., 'minimum_investment': None}
