from datetime import date

import numpy as np
import pytest

from .reinvestment_ledger import reinvestment_factors
from .test_treasury_cash_paths import factors
from .test_treasury_market import offer
from .treasury_cash_paths import anniversary, initial_lot_cash_paths
from .treasury_lot_kernels import TreasuryLotProvider
from .treasury_market import instrument_from_offer
from .treasury_valuation import price_contract


def provider(model=None, months=72, inflation=0., selic=0., rate=10., count=5, cost=0.):
    return TreasuryLotProvider(model or instrument_from_offer(offer()),
        factors(months=months + 2, count=count, inflation=inflation, selic=selic),
        np.full((count, months + 1), rate), months=months, annual_cost_percent=cost)


def test_original_lot_has_exactly_the_existing_dated_real_cash_and_principal():
    model = instrument_from_offer(offer())
    p = provider(model, inflation=.005)
    kernel = p(0, 72, 5)
    initial = initial_lot_cash_paths(model, factors(months=72, count=5, inflation=.005))
    np.testing.assert_allclose(kernel.gross_cash,
        (initial['economic_income_real'] + initial['acquisition_cost_released_real']) / model.asset.price)
    np.testing.assert_allclose(kernel.acquisition_cost_released,
        np.broadcast_to(initial['acquisition_cost_released_real'] / model.asset.price, (5, 72)))
    assert not p.provenance['future_offers_observed']


def test_fixed_monthly_ex_cash_pv_matches_quantlib_price_not_total_return_plus_coupon():
    model = instrument_from_offer(offer())
    p = provider(model)
    kernel = p(0, 72, 5)
    for month in (1, 12, 24):
        day = anniversary(model.settlement, month)
        if model.calendar.is_business_day(day):
            price = float(price_contract(model.spec, day, 10., calendar=model.calendar)['unit_price'])
            np.testing.assert_allclose(kernel.ex_cash_market_value[:, month - 1], price / model.asset.price)
    assert not kernel.ex_cash_market_value[:, -1].any()


def test_future_lot_has_same_family_risk_and_does_not_reuse_original_expired_contract():
    model = instrument_from_offer(offer())
    p = provider(model, months=120)
    kernel = p(70, 50, 5)
    assert kernel.kind == model.offer['kind']
    assert kernel.risk_level == model.asset.risk_level
    assert kernel.basis == 'hypothetical_same_class_future_contract'
    assert np.all(kernel.ex_cash_market_value[:, 0] > 0)
    assert np.allclose(kernel.acquisition_cost_released.sum(axis=1), 1)
    assert np.all(kernel.gross_cash.sum(axis=1) > 1)


def test_november_future_coupon_purchase_receives_next_january_and_july_payments():
    model = instrument_from_offer(offer('Tesouro Prefixado com Juros Semestrais', maturity='2031-01-01',
        rate_basis='nominal_annual', buy_price=1000.))
    p = provider(model, months=120, rate=10.)
    lot = p(1, 119, 5)
    assert np.count_nonzero(lot.gross_cash[0, :12]) == 2
    purchase = anniversary(model.settlement, 1)
    from .treasury_valuation import contract_for_offer
    spec = contract_for_offer({**model.offer, 'maturity': '2032-01-01'}, purchase)
    expected = float(price_contract(spec, purchase, 10., calendar=model.calendar)['unit_price'])
    _, _, values = p._template(1)
    np.testing.assert_allclose(values[:, 1], expected)


@pytest.mark.parametrize('kind,maturity', [('Tesouro Educa+', '2031-12-15'),
                                          ('Tesouro Renda+', '2054-12-15'),
                                          ('Tesouro IPCA+ com Juros Semestrais', '2035-05-15')])
def test_indexed_families_separate_coupon_and_amortization_with_real_cost_basis(kind, maturity):
    model = instrument_from_offer(offer(kind, maturity=maturity,
        rate_basis='real_above_ipca', buy_price=3000.))
    p = provider(model, months=120, inflation=.005, rate=10.)
    kernel = p(0, 120, 5)
    assert kernel.acquisition_cost_released.sum(axis=1).max() <= 1 + 1e-8
    initial = initial_lot_cash_paths(model, factors(months=120, count=5, inflation=.005))
    np.testing.assert_allclose(kernel.gross_cash,
        (initial['economic_income_real'] + initial['acquisition_cost_released_real']) / model.asset.price)


def test_scenario_yield_changes_market_value_not_fixed_contractual_cash():
    model = instrument_from_offer(offer())
    low, high = provider(model, rate=5.)(0, 72, 5), provider(model, rate=20.)(0, 72, 5)
    np.testing.assert_allclose(low.gross_cash, high.gross_cash)
    assert np.all(low.ex_cash_market_value[:, :24] > high.ex_cash_market_value[:, :24])


def test_premium_loss_cannot_create_withdrawable_income_or_reinvest_unavailable_principal():
    model = instrument_from_offer(offer(buy_price=1200.))
    p = provider(model, months=120, rate=0.)
    result = reinvestment_factors(p, kind=model.offer['kind'], risk_level=model.asset.risk_level,
        months=120, accumulation_months=0, count=5)
    assert not result['annual_income_factors'][0].any()
    assert np.all(result['annual_capital_loss_factors'][0].sum(axis=0) > 0)
    assert np.all(result['wealth_factors'][0, -1] <= 1000 / 1200 + 1e-8)


def test_reserve_no_mtm_tracks_selic_and_retains_bank_restriction():
    model = instrument_from_offer(offer('Tesouro Reserva', maturity='2036-01-01',
        buy_rate=None, buy_rate_text='SELIC', isin='BRSTNCT1D008', catalogue_group='Tesouro24x7'))
    p = provider(model, selic=.01)
    kernel = p(0, 72, 5)
    np.testing.assert_allclose(kernel.ex_cash_market_value[:, :12],
                               np.broadcast_to(1.01 ** np.arange(1, 13), (5, 12)))
    assert not kernel.gross_cash.any()
    assert model.pricing['institution_restriction'] == 'Banco do Brasil'


def test_effective_plan_cost_reduces_positions_without_making_a_cash_dividend():
    plain = provider()(0, 72, 5)
    costly = provider(cost=1.)(0, 72, 5)
    assert np.all(costly.ex_cash_market_value[:, :12] < plain.ex_cash_market_value[:, :12])
    assert costly.gross_cash.sum() < plain.gross_cash.sum()
    assert costly.acquisition_cost_released.sum() < plain.acquisition_cost_released.sum()


def test_missing_macro_padding_or_invalid_yields_fail_closed():
    model = instrument_from_offer(offer())
    with pytest.raises(ValueError, match='joint_treasury'):
        TreasuryLotProvider(model, factors(months=72, count=5), np.zeros((5, 73)), months=72)
    with pytest.raises(ValueError, match='scope'):
        provider()(0, 60, 5)


def test_sparse_annual_valuation_gives_identical_ledger_without_invented_monthly_marks():
    model = instrument_from_offer(offer('Tesouro Educa+', maturity='2031-12-15',
        rate_basis='real_above_ipca', buy_price=3000.))
    full = provider(model, months=120, inflation=.003)
    sparse = TreasuryLotProvider(model, factors(months=122, count=5, inflation=.003),
        np.full((5, 121), 10.), months=120, annual_valuations=True)
    outputs = [reinvestment_factors(p, kind=model.offer['kind'], risk_level=model.asset.risk_level,
        months=120, accumulation_months=60, count=5) for p in (full, sparse)]
    for field in ('wealth_factors', 'annual_income_factors', 'annual_capital_loss_factors'):
        np.testing.assert_allclose(outputs[0][field], outputs[1][field], rtol=1e-12)
    kernel = sparse(13, 107, 5)
    assert np.isnan(kernel.ex_cash_market_value[:, 0]).all()
    assert np.isfinite(kernel.ex_cash_market_value[:, 10]).all()
