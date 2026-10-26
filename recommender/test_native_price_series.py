from datetime import date, datetime, timezone
import json
import math

import pytest

from .share_basis import reconcile_native_prices, reconcile_prices
from .test_share_basis import ACTIONS, CUTOFF, price_fixture
from .round2_native_prices import IDENTITY, load_evidence


def native_fixture(actions=ACTIONS):
    body, quotes = price_fixture('raw', actions)
    source = json.loads(body)['chart']['result'][0]
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): value
             for stamp, value in zip(source['timestamp'], source['indicators']['quote'][0]['close'])}
    return body, daily, quotes


@pytest.mark.parametrize('actions', [ACTIONS, [], [{'date': '2024-04-16', 'ratio': .1}]])
def test_native_series_uses_identical_raw_math_without_manufactured_response(actions):
    body, daily, quotes = native_fixture(actions)
    before = daily.copy()
    actual = reconcile_native_prices(daily, actions, quotes, CUTOFF)
    expected = reconcile_prices(body, 'TEST3', actions, quotes, CUTOFF)
    assert actual[:2] == expected[:2]
    assert actual[2] == 'raw' and before == daily
    assert all(math.isclose(row[2], 10) for row in actual[0].values())


@pytest.mark.parametrize('value', [True, False, None, '10', 0, -1, float('nan'), float('inf')])
def test_native_nonpositive_nonfinite_or_untyped_values_cannot_enter_model(value):
    _, daily, quotes = native_fixture()
    daily[min(daily)] = value
    with pytest.raises(ValueError, match='invalid_native_price_value|invalid_stock_price'):
        reconcile_native_prices(daily, ACTIONS, quotes, CUTOFF)


@pytest.mark.parametrize('key', ['2021-09-30', datetime(2021, 9, 30), date(2026, 10, 1)])
def test_native_date_contract_rejects_ambiguous_or_future_dates(key):
    _, daily, quotes = native_fixture()
    daily[key] = 10
    with pytest.raises(ValueError, match='invalid_native_price_dates'):
        reconcile_native_prices(daily, ACTIONS, quotes, CUTOFF)


def test_a_monthly_gap_is_not_interpolated_or_filled():
    _, daily, quotes = native_fixture()
    day = sorted(daily)[10]
    del daily[day]
    del quotes[day]
    with pytest.raises(ValueError, match='less_than_61_consecutive_prices'):
        reconcile_native_prices(daily, ACTIONS, quotes, CUTOFF)


def test_each_available_reference_is_checked_not_a_favorable_subset():
    _, daily, quotes = native_fixture()
    day = sorted(quotes)[10]
    quotes[day] *= 1.1
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees'):
        reconcile_native_prices(daily, ACTIONS, quotes, CUTOFF)


def test_four_independent_references_still_required():
    _, daily, quotes = native_fixture()
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        reconcile_native_prices(daily, ACTIONS, dict(list(quotes.items())[:3]), CUTOFF)


def test_pre_action_regime_needs_its_own_raw_reference():
    _, daily, quotes = native_fixture()
    quotes = {day: value for day, value in quotes.items() if day > date(2024, 4, 16)}
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_native_prices(daily, ACTIONS, quotes, CUTOFF)


def test_post_cutoff_action_cannot_be_applied():
    _, daily, quotes = native_fixture([])
    with pytest.raises(ValueError, match='post_cutoff_share_action_requires_reconciliation'):
        reconcile_native_prices(daily, [{'date': '2026-10-01', 'ratio': 2}], quotes, CUTOFF)


@pytest.mark.parametrize('identity,cutoff', [({**IDENTITY, 'cvm': 1}, CUTOFF),
    ({**IDENTITY, 'share_class': 'PN'}, CUTOFF), ({**IDENTITY, 'isin': 'BREQTLACNPR0'}, CUTOFF),
    (IDENTITY, date(2026, 8, 31))])
def test_pinned_native_provider_cannot_alias_identity_or_cutoff(tmp_path, identity, cutoff):
    with pytest.raises(ValueError, match='native_RI_exact_security_or_cutoff_mismatch'):
        load_evidence(tmp_path, identity, cutoff, [], {}, b'')


def test_native_provider_does_not_capture_other_stocks(tmp_path):
    assert load_evidence(tmp_path, {**IDENTITY, 'ticker': 'PETR4'}, CUTOFF, [], {}, b'') is None
