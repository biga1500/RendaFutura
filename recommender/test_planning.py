import numpy as np
import pytest

from .contracts import Plan
from .planning import income_factors, inverse_planning
from .simulation import simulate


def plan(**changes):
    return Plan(income_mode="distributions_only", age=30, retirement_age=31, end_age=33,
                initial_capital=0, monthly_contribution=100, target_income=1000,
                annual_cost_percent=0, seed=42, simulations=500).model_copy(update=changes)


def test_closed_form_income_capital_and_monthly_contribution():
    paths = np.full((100, 36, 1), .01)
    result = inverse_planning([1], paths, paths, plan())
    annuity = sum(1.01**month for month in range(12))
    assert result["requiredRetirementCapital"] == pytest.approx(100000)
    assert result["requiredMonthlyContribution"] == pytest.approx(100000 / annuity)
    assert result["usesPrincipalForIncome"] is False
    assert result["portfolioReoptimized"] is False


def test_initial_capital_reduces_required_contribution_without_changing_risk():
    paths = np.full((100, 36, 1), .01)
    original = inverse_planning([1], paths, paths, plan())
    funded = inverse_planning([1], paths, paths, plan(initial_capital=100000))
    assert funded["requiredMonthlyContribution"] == 0
    assert funded["requiredRetirementCapital"] == original["requiredRetirementCapital"]


def test_zero_cash_has_no_finite_income_solution_even_with_appreciation():
    result = inverse_planning([1], np.full((100, 36, 1), .03), np.zeros((100, 36, 1)), plan())
    assert result["status"] == "no_finite_solution"
    assert result["requiredRetirementCapital"] is None
    assert result["requiredMonthlyContribution"] is None
    assert result["requiredInitialCapitalWithCurrentContribution"] is None


@pytest.mark.parametrize("zero_count,finite", [(20, True), (21, False)])
def test_attainment_uses_actual_scenario_order_statistic(zero_count, finite):
    paths = np.full((100, 36, 1), .01)
    cash = paths.copy()
    cash[:zero_count] = 0
    result = inverse_planning([1], paths, cash, plan(), attainment=.8)
    assert (result["requiredRetirementCapital"] is not None) is finite
    assert result["zeroIncomeScenarioFraction"] == pytest.approx(zero_count / 100)


def test_inverse_factors_match_the_full_cash_only_simulation():
    rng = np.random.default_rng(42)
    paths = rng.uniform(.001, .02, (100, 36, 2))
    cash = rng.uniform(0, .003, paths.shape)
    settings = plan(initial_capital=10000, monthly_contribution=500, annual_cost_percent=.5)
    weights = [.6, .4]
    capital, contribution, income = income_factors(weights, paths, cash, settings)
    expected = (capital * settings.initial_capital + contribution * settings.monthly_contribution) * income
    actual = simulate(weights, paths, settings, detailed=True, cash_paths=cash)
    np.testing.assert_allclose(np.percentile(expected, [10, 50, 90]), actual["monthlyIncomeCapacity"])


def test_higher_cost_or_target_cannot_reduce_required_contribution():
    paths = np.full((100, 36, 1), .01)
    reference = inverse_planning([1], paths, paths, plan())
    costlier = inverse_planning([1], paths, paths, plan(annual_cost_percent=2))
    doubled = inverse_planning([1], paths, paths, plan(target_income=2000))
    assert costlier["requiredMonthlyContribution"] > reference["requiredMonthlyContribution"]
    assert doubled["requiredMonthlyContribution"] == pytest.approx(reference["requiredMonthlyContribution"] * 2)
