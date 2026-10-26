from dataclasses import replace

import numpy as np
import pytest

from .reinvestment_ledger import LotKernel, reinvestment_factors


def provider(month, remaining, count, *, rate=0.):
    # Synthetic one-period bond: one unit of principal plus explicit interest.
    gross = np.zeros((count, remaining))
    released = np.zeros_like(gross)
    gross[:, 0], released[:, 0] = 1 + rate, 1
    return LotKernel('test_family', 0, gross, released, np.zeros_like(gross),
        'current_quote_contract_projection' if month == 0 else 'hypothetical_same_class_future_contract',
        'https://example.test/synthetic-contract')


def run(callback=provider, **updates):
    return reinvestment_factors(callback, kind='test_family', risk_level=0,
        **{'months': 36, 'accumulation_months': 12, 'count': 5, **updates})


def test_zero_interest_never_creates_income_from_reinvested_principal():
    result = run()
    np.testing.assert_allclose(result['wealth_factors'][0], 1)
    np.testing.assert_allclose(result['wealth_factors'][1, 1:], 12)
    assert not np.any(result['annual_income_factors'])
    assert result['hypothetical_future_lots'] == 35
    assert result['principal_counted_as_income'] is False


def test_interest_accumulates_before_retirement_and_is_withdrawn_after():
    result = run(lambda *args: provider(*args, rate=.01))
    at_retirement = 1.01 ** 12
    np.testing.assert_allclose(result['wealth_factors'][0, 1:], at_retirement)
    np.testing.assert_allclose(result['annual_income_factors'][0, 0], 0)
    np.testing.assert_allclose(result['annual_income_factors'][0, 1:], at_retirement * .01 * 12)
    expected_contributions = sum(1.01 ** month for month in range(12))
    np.testing.assert_allclose(result['wealth_factors'][1, 1:], expected_contributions)


def test_realized_loss_does_not_release_more_cash_than_was_received():
    def loss_provider(*args):
        kernel = provider(*args)
        return replace(kernel, gross_cash=kernel.gross_cash * .9)
    result = run(loss_provider, accumulation_months=0)
    assert not np.any(result['annual_income_factors'])
    assert np.all(result['annual_capital_loss_factors'][0] > 0)
    np.testing.assert_allclose(result['wealth_factors'][0, -1], .9 ** 36)


@pytest.mark.parametrize('change', [lambda k: replace(k, kind='different'),
    lambda k: replace(k, risk_level=1), lambda k: replace(k, source_url='http://example.test'),
    lambda k: replace(k, gross_cash=np.full_like(k.gross_cash, np.nan)),
    lambda k: replace(k, acquisition_cost_released=k.acquisition_cost_released * 2)])
def test_incompatible_or_invalid_reinvestment_contract_is_not_used(change):
    with pytest.raises(ValueError):
        run(lambda *args: change(provider(*args)))


def test_future_lot_cannot_be_misrepresented_as_an_observed_current_offer():
    def wrong_basis(month, remaining, count):
        return replace(provider(month, remaining, count), basis='current_quote_contract_projection')
    with pytest.raises(ValueError, match='future_lot'):
        run(wrong_basis)


def test_initial_capital_and_contributions_remain_separate_linear_channels():
    result = run(lambda *args: provider(*args, rate=.01))
    total = 1000 * result['wealth_factors'][0, -1] + 100 * result['wealth_factors'][1, -1]
    expected = 1000 * 1.01 ** 12 + 100 * sum(1.01 ** month for month in range(12))
    np.testing.assert_allclose(total, expected)


def test_partial_valuation_cannot_omit_a_year_end_or_treat_missing_as_zero():
    def incomplete(month, remaining, count):
        kernel = provider(month, remaining, count)
        return replace(kernel, valuation_months=())
    with pytest.raises(ValueError, match='year_end'):
        run(incomplete)


def test_independent_retirement_unit_is_not_the_preexisting_accumulated_capital():
    plain = run(lambda *args: provider(*args, rate=.01))
    with_unit = run(lambda *args: provider(*args, rate=.01), include_retirement_unit=True)
    np.testing.assert_allclose(plain['wealth_factors'], with_unit['wealth_factors'])
    np.testing.assert_allclose(plain['annual_income_factors'], with_unit['annual_income_factors'])
    np.testing.assert_allclose(with_unit['retirement_unit_annual_income_factors'], .01 * 12)
    assert np.all(with_unit['annual_income_factors'][0, 1:] > with_unit['retirement_unit_annual_income_factors'])
