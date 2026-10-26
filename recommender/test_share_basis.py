from datetime import date, datetime, timezone
import json

import pytest

from .collect_market_data import DividendHistory, shift_month
from .share_basis import factor_after, reconcile_payments, reconcile_prices


CUTOFF = date(2026, 9, 30)
ACTIONS = [{'date': '2024-04-16', 'ratio': 2.}]


def price_fixture(mode, actions=ACTIONS):
    days = [shift_month(CUTOFF, -i) for i in reversed(range(61))]
    quotes = {d: 10 * factor_after(d, actions, CUTOFF) for d in days}
    values = [quotes[d] if mode == 'raw' else 10. for d in days]
    content = json.dumps({'chart': {'result': [{'meta': {'symbol': 'TEST3.SA', 'currency': 'BRL'},
                     'timestamp': [int(datetime(d.year, d.month, d.day, 16, tzinfo=timezone.utc).timestamp()) for d in days],
                     'indicators': {'quote': [{'close': values}]}}]}}).encode()
    return content, quotes


@pytest.mark.parametrize('mode', ['raw', 'split_adjusted'])
def test_raw_and_split_adjusted_prices_reconcile_to_same_share_units(mode):
    content, quotes = price_fixture(mode)
    prices, matches, detected = reconcile_prices(content, 'TEST3', ACTIONS, quotes, CUTOFF)
    assert detected == mode and len(matches) == 61
    assert all(value[2] == 10 for value in prices.values())


def test_missing_pre_action_evidence_is_not_guessed_from_latest_quote():
    content, quotes = price_fixture('split_adjusted')
    quotes = {day: value for day, value in quotes.items() if day > date(2024, 4, 16)}
    with pytest.raises(ValueError, match='unverified_share_price_regime'):
        reconcile_prices(content, 'TEST3', ACTIONS, quotes, CUTOFF)


def test_dividend_reinvested_or_wrong_factor_prices_are_rejected():
    content, quotes = price_fixture('split_adjusted')
    r = json.loads(content)
    r['chart']['result'][0]['indicators']['quote'][0]['close'][10] = 8
    with pytest.raises(ValueError, match='share_scale_disagrees'):
        reconcile_prices(json.dumps(r).encode(), 'TEST3', ACTIONS, quotes, CUTOFF)


def payment_fixture(source_adjusted):
    original = DividendHistory(CUTOFF, 6)
    original.coverage_start = date(2015, 1, 1)
    original.installments = []
    expected = {}
    for year in range(2021, 2027):
        record = date(year, 2, 1)
        amount = 1. if record < date(2024, 4, 16) else .5
        expected[record] = amount
        factor = factor_after(record, ACTIONS, CUTOFF) if source_adjusted else 1
        original.installments.append({'ticker': 'TEST3', 'record_date': record.isoformat(),
                                     'payment_date': date(year, 3, 1).isoformat(),
                                     'gross_per_unit': amount / factor, 'label': 'Dividendos', 'status': 'included'})
    return original, expected


@pytest.mark.parametrize('adjusted', [False, True])
def test_original_or_already_adjusted_cash_not_adjusted_twice(adjusted):
    original, expected = payment_fixture(adjusted)
    history, ledger, adjustments = reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF)
    assert all(amount == .5 for _, _, amount in history)
    assert all(row['gross_per_unit'] == .5 for row in ledger)
    assert all(row['share_basis_date'] == '2026-09-30' for row in ledger)
    assert len(adjustments) >= 4


def test_future_installment_validates_right_but_does_not_become_paid_cash():
    original, expected = payment_fixture(False)
    row = original.installments[-1]
    row['gross_per_unit'] = .25
    original.installments.append({**row, 'payment_date': '2026-12-01', 'gross_per_unit': .25})
    history, ledger, _ = reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF)
    assert sum(amount for day, _, amount in history if day.year == 2026) == .25
    assert history.future_events == 1
    assert all(row['payment_date'] <= '2026-09-30' for row in ledger)


def test_newest_wrong_payment_is_not_repaired_by_forcing_B3_total():
    original, expected = payment_fixture(False)
    original.installments[-1]['gross_per_unit'] = 5000
    with pytest.raises(ValueError, match='cash_amount_or_share_scale'):
        reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF)


def test_reverse_split_normalization_does_not_create_a_market_jump():
    actions = [{'date': '2024-05-27', 'ratio': .1}]
    content, quotes = price_fixture('raw', actions)
    prices, _, _ = reconcile_prices(content, 'TEST3', actions, quotes, CUTOFF)
    assert all(value[2] == 10 for value in prices.values())
