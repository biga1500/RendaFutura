from copy import deepcopy
from datetime import date

import numpy as np
import pytest

from .test_treasury_market import offer
from .treasury_cash_paths import anniversary, initial_lot_cash_paths
from .treasury_market import instrument_from_offer


def factors(months=60, inflation=0., selic=0., count=500):
    return {'inflation': np.full((count, months), inflation), 'selic': np.full((count, months), selic),
            'future_values_observed': False}


def test_anniversary_clamps_month_end_without_changing_following_month_basis():
    assert anniversary(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert anniversary(date(2026, 1, 31), 2) == date(2026, 3, 31)


def test_fixed_redemption_is_only_profit_above_actual_acquisition_cost():
    model = instrument_from_offer(offer(buy_price=800.))
    result = initial_lot_cash_paths(model, factors(), quantity=2.)
    np.testing.assert_allclose(result['gross_nominal_cash'].sum(axis=1), 2000)
    np.testing.assert_allclose(result['economic_income_nominal'].sum(axis=1), 400)
    np.testing.assert_allclose(result['economic_income_real'].sum(axis=1), 400)
    assert result['acquisition_cost_released_real'].sum() == 1600
    assert result['remaining_acquisition_cost'] == pytest.approx(0)


def test_before_redemption_zero_cash_is_contractual_not_missing_history_imputation():
    result = initial_lot_cash_paths(instrument_from_offer(offer()), factors(months=12))
    assert not np.any(result['gross_nominal_cash'])
    assert result['remaining_acquisition_cost'] == 800
    assert result['price_history_created'] is result['future_payments_observed'] is False


def test_premium_purchase_loss_remains_negative_not_a_positive_coupon():
    model = instrument_from_offer(offer(buy_price=1200.))
    result = initial_lot_cash_paths(model, factors())
    np.testing.assert_allclose(result['economic_income_real'].sum(axis=1), -200)
    assert result['negative_economic_income_clipped'] is False


def test_ipca_nominal_adjustment_is_not_spendable_real_income():
    model = instrument_from_offer(offer('Tesouro IPCA+', maturity='2030-05-15',
        rate_basis='real_above_ipca', buy_price=3000.))
    result = initial_lot_cash_paths(model, factors(inflation=.01))
    assert np.all(result['economic_income_nominal'].sum(axis=1) > result['economic_income_real'].sum(axis=1))
    assert result['index_anchor_basis'] == 'derived_from_price_yield'


def test_amortizing_installments_reconcile_principal_and_interest_separately():
    model = instrument_from_offer(offer('Tesouro Educa+', maturity='2031-12-15',
        rate_basis='real_above_ipca', buy_price=4000.))
    result = initial_lot_cash_paths(model, factors(months=72))
    assert len(result['payment_calendar']) == 60
    assert result['acquisition_cost_released_real'].sum() == pytest.approx(4000)
    np.testing.assert_allclose(result['economic_income_nominal'].sum(axis=1),
        result['gross_nominal_cash'].sum(axis=1) - 4000)
    assert result['principal_counted_as_income'] is False


def test_reserva_no_mtm_still_does_not_report_unredeemed_interest_as_paid_cash():
    model = instrument_from_offer(offer('Tesouro Reserva', maturity='2036-01-01',
        buy_rate=None, buy_rate_text='SELIC', isin='BRSTNCT1D008', catalogue_group='Tesouro24x7'))
    result = initial_lot_cash_paths(model, factors(months=120, selic=.01))
    assert len(result['payment_calendar']) == 1
    assert not np.any(result['coupon_nominal_cash'])
    assert np.all(result['economic_income_real'].sum(axis=1) > 0)
    assert result['acquisition_cost_released_real'].sum() == pytest.approx(800)
    assert not result['post_maturity_income_generated']


def test_unknown_or_invalid_macro_inputs_do_not_generate_cash():
    model = instrument_from_offer(offer())
    for changes in ({'future_values_observed': True}, {'selic': np.full((500, 60), -1.)},
                    {'inflation': np.full((500, 60), np.nan)}):
        with pytest.raises(ValueError):
            initial_lot_cash_paths(model, {**factors(), **changes})
