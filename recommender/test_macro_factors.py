from datetime import date
import json

import numpy as np
import pytest

from .macro_factors import align_factors, factor_scenarios, parse_monthly


def test_percent_units_partial_month_exclusion_and_deflation():
    values = parse_monthly(json.dumps([{'data': '01/08/2026', 'valor': '-0.32'},
        {'data': '01/09/2026', 'valor': '0.82'}, {'data': '01/10/2026', 'valor': '1.0'}]).encode(), date(2026, 9, 30))
    assert values == {'2026-08-31': -.0032, '2026-09-30': .0082}


@pytest.mark.parametrize('rows', [[], [{'data': '01/09/2026', 'valor': 'NaN'}],
    [{'data': '01/09/2026', 'valor': '-100'}], [{'data': 'bad', 'valor': '1'}],
    [{'data': '01/09/2026', 'valor': '1'}, {'data': '30/09/2026', 'valor': '1'}]])
def test_invalid_or_duplicate_observations_are_not_used(rows):
    with pytest.raises(ValueError):
        parse_monthly(json.dumps(rows).encode(), date(2026, 9, 30))


def test_negative_selic_is_not_deflation():
    with pytest.raises(ValueError):
        parse_monthly(b'[{"data":"01/09/2026","valor":"-0.1"}]', date(2026, 9, 30), nonnegative=True)


def test_missing_month_is_not_backfilled_or_interpolated():
    with pytest.raises(ValueError, match='no_imputation'):
        align_factors({'2026-08-31': 0}, {'2026-08-31': 0, '2026-09-30': .01}, date(2026, 8, 1), date(2026, 9, 30))


def test_joint_draws_are_reproducible_and_not_claimed_as_history():
    history = {'dates': ['2026-07-31', '2026-08-31', '2026-09-30'],
        'inflation': [.002, -.0032, .0082], 'selic': [.0122, .0109, .0108]}
    a, b = [factor_scenarios(history, 120, 500, 3, 42) for _ in range(2)]
    np.testing.assert_array_equal(a['inflation'], b['inflation'])
    np.testing.assert_array_equal(a['selic'], b['selic'])
    pairs = set(zip(history['inflation'], history['selic']))
    assert all(pair in pairs for pair in zip(a['inflation'].flat, a['selic'].flat))
    assert a['inflation'].shape == (500, 120)
    assert a['future_values_observed'] is a['bond_price_history_created'] is False
