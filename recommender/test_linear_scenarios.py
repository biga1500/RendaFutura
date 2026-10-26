"""Synthetic, deterministic tests; no network or historical-engine changes."""

import json

import numpy as np
import pytest

from .contracts import Plan
from .linear_scenarios import (
    FACTOR_KEYS,
    build_stock_factors,
    inverse_planning_factors,
    simulate_factors,
    stack_factors,
)
from .reinvestment_ledger import LotKernel, reinvestment_factors
from .simulation import simulate


def settings(**changes):
    values = dict(age=30, retirement_age=31, end_age=33, initial_capital=1000,
                  monthly_contribution=10, target_income=10, annual_cost_percent=0,
                  seed=12, simulations=500, income_mode="distributions_only")
    return Plan(**(values | changes))


def factors_for(plan, *, count=3, assets=1):
    years = plan.end_age - plan.age
    accumulation = plan.retirement_age - plan.age
    wealth = np.zeros((2, years + 1, count, assets))
    wealth[0] = 1
    for year in range(1, years + 1):
        wealth[1, year] = 12 * min(year, accumulation)
    return {"wealth_factors": wealth,
            "annual_income_factors": np.zeros((2, years, count, assets)),
            "annual_capital_loss_factors": np.zeros((2, years, count, assets))}


def periodic_treasury(*, count, months=36, accumulation_months=12, rate=.005):
    def provider(entry, remaining, scenario_count):
        return LotKernel("coupon_fixture", 1,
                         np.full((scenario_count, remaining), rate),
                         np.zeros((scenario_count, remaining)),
                         np.ones((scenario_count, remaining)),
                         "current_quote_contract_projection" if entry == 0
                         else "hypothetical_same_class_future_contract",
                         "https://example.invalid/synthetic-coupon-contract")
    return reinvestment_factors(provider, kind="coupon_fixture", risk_level=1,
                               months=months, accumulation_months=accumulation_months, count=count)


def assert_json_finite(value):
    json.dumps(value, allow_nan=False, default=lambda item: item.tolist())


def test_hybrid_composition_matches_independent_stock_and_treasury_ledgers():
    plan = settings()
    returns = np.empty((4, 36, 2))
    returns[:, :, 0] = .01
    returns[:, :, 1] = .003
    cash = returns.copy()
    stocks = build_stock_factors(returns, cash, plan)
    treasury = periodic_treasury(count=4)
    combined = stack_factors([stocks, treasury])
    assert combined["wealth_factors"].shape == (2, 4, 4, 3)
    assert combined["annual_income_factors"].shape == (2, 3, 4, 3)
    assert np.count_nonzero(combined["annual_income_factors"][:, 0]) == 0
    weights = np.array([.3, .2, .5])
    result = simulate_factors(weights, combined, plan, detailed=True)
    expected_wealth = np.einsum("cyna,a,c->yn", combined["wealth_factors"], weights, [1000, 10])
    expected_income = np.einsum("cyna,a,c->yn", combined["annual_income_factors"], weights, [1000, 10])
    assert result["retirementWealth"] == pytest.approx([expected_wealth[1, 0]] * 3)
    assert result["finalWealth"] == pytest.approx([expected_wealth[-1, 0]] * 3)
    assert result["monthlyIncomeCapacity"] == pytest.approx([expected_income[1:, 0].min() / 12] * 3)
    assert result["chart"][0] == {"age": 30, "p10": 1000., "p50": 1000., "p90": 1000.}
    assert len(result["chart"]) == 4
    assert_json_finite(result)


def test_detailed_response_matches_existing_simulate_schema():
    plan = settings()
    paths = np.full((3, 36, 1), .01)
    stock = build_stock_factors(paths, paths, plan)
    actual = simulate_factors([1], stock, plan, detailed=True)
    expected = simulate([1], paths, plan, detailed=True, cash_paths=paths)
    assert actual.keys() == expected.keys()
    for key in expected:
        if key not in {"chart", "incomeCoveragePeriod"}:
            np.testing.assert_allclose(actual[key], expected[key], rtol=1e-12, atol=1e-12)
    assert actual["incomeCoveragePeriod"] == expected["incomeCoveragePeriod"]
    for actual_point, expected_point in zip(actual["chart"], expected["chart"]):
        assert actual_point == pytest.approx(expected_point)
    compact = simulate_factors([[1], [1]], stock, plan)
    assert set(compact) == {"success", "unfundedIncomeFraction"}
    assert compact["success"].shape == (2,)
    np.testing.assert_allclose(compact["unfundedIncomeFraction"], actual["meanUnfundedIncomeFraction"])


def test_zero_capital_and_contributions_produce_no_income_or_nan():
    plan = settings(initial_capital=0, monthly_contribution=0)
    paths = np.full((2, 36, 1), .01)
    factors = build_stock_factors(paths, paths, plan)
    result = simulate_factors([1], factors, plan, detailed=True)
    assert result["successFrequency"] == 0
    assert result["meanUnfundedIncomeFraction"] == 1
    assert result["monthlyIncomeCapacity"] == [0, 0, 0]
    assert result["medianDeficit"] == 240
    assert result["retirementWealth"] == result["finalWealth"] == [0, 0, 0]
    assert result["depletionFrequency"] == 1
    assert_json_finite(result)


def test_zero_cash_does_not_convert_appreciation_or_capital_to_income():
    plan = settings(initial_capital=10000, monthly_contribution=0)
    paths = np.full((2, 36, 1), .01)
    result = simulate_factors([1], build_stock_factors(paths, np.zeros_like(paths), plan), plan, True)
    assert result["finalWealth"] == pytest.approx([10000 * 1.01**36] * 3)
    assert result["monthlyIncomeCapacity"] == [0, 0, 0]
    assert result["successFrequency"] == 0
    assert result["depletionFrequency"] == 0


def test_principal_repayments_are_reinvested_not_retirement_income():
    plan = settings(initial_capital=1000, monthly_contribution=0)
    def provider(entry, remaining, count):
        cash = np.ones((count, remaining))
        cash[:, 1:] = 0
        values = np.zeros_like(cash)
        return LotKernel("principal_fixture", 0, cash, cash.copy(), values,
                         "current_quote_contract_projection" if entry == 0
                         else "hypothetical_same_class_future_contract",
                         "https://example.invalid/synthetic-principal-contract")
    factors = reinvestment_factors(provider, kind="principal_fixture", risk_level=0,
                                   months=36, accumulation_months=12, count=3)
    result = simulate_factors([1], factors, plan, True)
    assert result["retirementWealth"] == result["finalWealth"] == [1000] * 3
    assert result["monthlyIncomeCapacity"] == [0] * 3
    assert result["meanUnfundedIncomeFraction"] == 1
    requirements = inverse_planning_factors([1], factors, plan)
    assert requirements["requiredMonthlyContribution"] is None
    assert requirements["requiredInitialCapitalWithCurrentContribution"] is None
    assert requirements["status"] == "no_finite_solution"
    assert requirements["zeroIncomeScenarioFraction"] == 1
    assert_json_finite(requirements)


def test_capital_losses_are_not_subtracted_from_wealth_twice():
    plan = settings(initial_capital=1000, monthly_contribution=0)
    factors = factors_for(plan)
    factors["wealth_factors"][0, 2:] = .8
    factors["annual_capital_loss_factors"][0, 1] = .2
    result = simulate_factors([1], factors, plan, True)
    assert result["finalWealth"] == [800] * 3
    assert result["monthlyIncomeCapacity"] == [0] * 3


def test_worst_income_year_is_checked_after_combining_assets_and_channels():
    plan = settings(initial_capital=100, monthly_contribution=10, target_income=10)
    factors = factors_for(plan, count=1, assets=2)
    income = factors["annual_income_factors"]
    income[0, 1:, 0, :] = [[2.4, 0], [0, 2.4]]
    income[1, 1:, 0, :] = [[0, 24], [24, 0]]
    combined = simulate_factors([.5, .5], factors, plan, True)
    assert combined["successFrequency"] == 1
    assert combined["monthlyIncomeCapacity"] == [20] * 3
    first_only = simulate_factors([1, 0], factors, plan, True)
    assert first_only["monthlyIncomeCapacity"] == [20] * 3
    # An amount below the target in one year cannot be covered by another year.
    factors["annual_income_factors"][0, 2, 0, :] = 0
    factors["annual_income_factors"][1, 2, 0, :] = 0
    combined = simulate_factors([.5, .5], factors, plan, True)
    assert combined["successFrequency"] == 0
    assert combined["monthlyIncomeCapacity"] == [0] * 3
    assert combined["medianDeficit"] == 120


def test_inverse_planning_solves_each_year_before_taking_scenario_requirement():
    plan = settings(initial_capital=100, monthly_contribution=5, target_income=10)
    factors = factors_for(plan, count=1)
    factors["annual_income_factors"][0, 1:, 0, 0] = [.2, .8]
    factors["annual_income_factors"][1, 1:, 0, 0] = [10, 2]
    result = inverse_planning_factors([1], factors, plan)
    # Year 1 requires (120 - 20)/10 = 10; year 2 (120 - 80)/2 = 20.
    assert result["requiredMonthlyContribution"] == 20
    assert result["monthlyContributionGap"] == 15
    # At the existing contribution: max((120 - 50)/.2, (120 - 10)/.8).
    assert result["requiredInitialCapitalWithCurrentContribution"] == 350
    assert result["currentPlanSuccessFrequency"] == 0
    assert result["requiredRetirementCapital"] is None
    assert result["requiredRetirementCapitalBasis"] == "not_identifiable_from_current_age_cohort_channels"
    assert result["requiredRetirementCapitalNullReason"]
    assert result["status"] == "estimated"
    assert result["usesPrincipalForIncome"] is False
    assert result["portfolioReoptimized"] is False
    funded = plan.model_copy(update={"monthly_contribution": 20})
    assert simulate_factors([1], factors, funded, True)["successFrequency"] == 1
    below = plan.model_copy(update={"monthly_contribution": 19.99})
    assert simulate_factors([1], factors, below, True)["successFrequency"] == 0
    assert_json_finite(result)


def test_inverse_planning_does_not_multiply_independent_minima():
    plan = settings(initial_capital=100, monthly_contribution=5, target_income=10)
    factors = factors_for(plan, count=2)
    factors["annual_income_factors"][0, 1:, :, 0] = [[1.2, 0], [0, 1.2]]
    factors["annual_income_factors"][1, 1:, :, 0] = [[0, 24], [24, 0]]
    result = inverse_planning_factors([1], factors, plan, attainment=1)
    assert result["requiredMonthlyContribution"] == 5
    assert result["requiredInitialCapitalWithCurrentContribution"] == 100
    assert result["currentPlanSuccessFrequency"] == 1
    assert result["zeroIncomeScenarioFraction"] == 0


def test_attainment_is_an_order_statistic_of_complete_scenario_paths():
    plan = settings(initial_capital=0, monthly_contribution=0, target_income=10)
    factors = factors_for(plan, count=5)
    for scenario, requirement in enumerate([10, 20, 30, 40, np.inf]):
        factors["annual_income_factors"][1, 1:, scenario, 0] = 120 / requirement
    result = inverse_planning_factors([1], factors, plan, attainment=.8)
    assert result["requiredMonthlyContribution"] == 40
    funded = plan.model_copy(update={"monthly_contribution": 40})
    assert simulate_factors([1], factors, funded, True)["successFrequency"] == .8
    result = inverse_planning_factors([1], factors, plan, attainment=1)
    assert result["requiredMonthlyContribution"] is None
    assert result["requiredInitialCapitalWithCurrentContribution"] is None
    assert result["zeroIncomeScenarioFraction"] == .2
    assert_json_finite(result)


def test_zero_channel_is_allowed_when_fixed_channel_already_meets_target():
    plan = settings(initial_capital=100, monthly_contribution=0, target_income=10)
    factors = factors_for(plan)
    factors["annual_income_factors"][0, 1:] = 1.2
    result = inverse_planning_factors([1], factors, plan)
    assert result["requiredMonthlyContribution"] == 0
    assert result["monthlyContributionGap"] == 0
    assert result["currentPlanSuccessFrequency"] == 1


def test_independent_retirement_unit_is_required_to_estimate_retirement_capital():
    plan = settings(initial_capital=100, monthly_contribution=0, target_income=10)
    factors = factors_for(plan, count=3, assets=2)
    factors["annual_income_factors"][0, 1:] = 1
    independent = np.empty((2, 3, 2))
    independent[0] = [.06, .12]
    independent[1] = [.12, .06]
    result = inverse_planning_factors([.5, .5], factors, plan,
                                      retirement_unit_income_factors=independent)
    assert result["requiredRetirementCapital"] == pytest.approx(120 / .09)
    assert result["requiredRetirementCapitalBasis"] == "independently_modeled_retirement_start_unit_annual_income"
    assert result["requiredRetirementCapitalNullReason"] is None
    weighted = independent @ np.array([.5, .5])
    assert inverse_planning_factors([.5, .5], factors, plan,
                                   retirement_unit_income_factors=weighted) == result
    independent[1] = 0
    result = inverse_planning_factors([.5, .5], factors, plan,
                                      retirement_unit_income_factors=independent)
    assert result["requiredRetirementCapital"] is None
    assert result["requiredRetirementCapitalNullReason"] == "no_finite_solution_at_requested_attainment"
    assert_json_finite(result)


def test_stock_channels_use_end_month_contributions_and_charge_fees_monthly():
    plan = settings(initial_capital=1000, monthly_contribution=10, annual_cost_percent=2)
    paths = np.full((1, 36, 1), .01)
    factors = build_stock_factors(paths, paths, plan)
    cost = .98 ** (1 / 12)
    growth = 1.01 * cost
    initial = growth**12
    contributions = sum(growth**month for month in range(12))
    assert factors["wealth_factors"][0, 1, 0, 0] == pytest.approx(initial)
    assert factors["wealth_factors"][1, 1, 0, 0] == pytest.approx(contributions)
    assert factors["wealth_factors"][0, 2, 0, 0] == pytest.approx(initial * .98)
    assert factors["annual_income_factors"][0, 1, 0, 0] == pytest.approx(
        initial * .01 * sum(cost**month for month in range(12)))
    assert np.count_nonzero(factors["annual_income_factors"][:, 0]) == 0
    assert np.count_nonzero(factors["annual_capital_loss_factors"]) == 0


def test_stock_factors_do_not_rebalance_at_year_end():
    plan = settings(initial_capital=100, monthly_contribution=0, target_income=1)
    paths = np.empty((1, 36, 2))
    paths[:, :, 0] = .02
    paths[:, :, 1] = 0
    cash = np.zeros_like(paths)
    factors = build_stock_factors(paths, cash, plan)
    result = simulate_factors([.5, .5], factors, plan, True)
    assert result["finalWealth"] == pytest.approx([50 * 1.02**36 + 50] * 3)
    rebalanced = simulate([.5, .5], paths, plan, True, cash_paths=cash)
    assert result["finalWealth"][1] > rebalanced["finalWealth"][1]


def test_dividend_is_subtracted_once_and_existing_income_is_not_reinvested_after_retirement():
    plan = settings(initial_capital=1000, monthly_contribution=0, target_income=1)
    paths = np.full((1, 36, 1), .01)
    factors = build_stock_factors(paths, paths, plan)
    capital = 1.01**12
    assert factors["wealth_factors"][0, :, 0, 0] == pytest.approx([1, capital, capital, capital])
    assert factors["annual_income_factors"][0, :, 0, 0] == pytest.approx([0, capital * .12, capital * .12])


def test_price_loss_can_leave_zero_income_without_forcing_principal_sale():
    plan = settings(initial_capital=1000, monthly_contribution=0)
    paths = np.zeros((1, 36, 1))
    paths[:, 12, :] = -1
    factors = build_stock_factors(paths, np.zeros_like(paths), plan)
    result = simulate_factors([1], factors, plan, True)
    assert result["retirementWealth"] == [1000] * 3
    assert result["finalWealth"] == [0] * 3
    assert result["depletionFrequency"] == 1
    assert result["medianDeficit"] == 240


def test_income_in_accumulation_rows_is_ignored_not_spent_again():
    plan = settings()
    factors = factors_for(plan)
    factors["annual_income_factors"][:, 0] = 100000
    result = simulate_factors([1], factors, plan, True)
    assert result["monthlyIncomeCapacity"] == [0] * 3
    assert result["meanUnfundedIncomeFraction"] == 1


def test_inputs_are_not_mutated():
    plan = settings()
    paths = np.full((2, 36, 2), .01)
    returns_before = paths.copy()
    factors = build_stock_factors(paths, paths, plan)
    before = {key: values.copy() for key, values in factors.items() if key in FACTOR_KEYS}
    weights = np.array([[.6, .4], [.2, .8]])
    old_weights = weights.copy()
    simulate_factors(weights, factors, plan, True)
    inverse_planning_factors(weights[0], factors, plan)
    stack_factors([factors, factors])
    np.testing.assert_array_equal(paths, returns_before)
    np.testing.assert_array_equal(weights, old_weights)
    for key in FACTOR_KEYS:
        np.testing.assert_array_equal(factors[key], before[key])


@pytest.mark.parametrize("weights", [[], [0], [-1], [2], [np.nan], [np.inf], [[1, 0]], [[[1]]]])
def test_invalid_weights_fail_closed(weights):
    plan = settings()
    with pytest.raises(ValueError):
        simulate_factors(weights, factors_for(plan), plan)
    with pytest.raises(ValueError):
        inverse_planning_factors(weights, factors_for(plan), plan)


@pytest.mark.parametrize("key", FACTOR_KEYS)
@pytest.mark.parametrize("invalid", [np.nan, np.inf, -.1])
def test_invalid_factor_values_fail_closed(key, invalid):
    plan = settings()
    factors = factors_for(plan)
    factors[key][0, -1, 0, 0] = invalid
    with pytest.raises(ValueError):
        simulate_factors([1], factors, plan)
    with pytest.raises(ValueError):
        inverse_planning_factors([1], factors, plan)


@pytest.mark.parametrize("key", FACTOR_KEYS)
def test_mismatched_factor_shape_is_rejected(key):
    plan = settings()
    factors = factors_for(plan)
    factors[key] = factors[key][:, :-1]
    with pytest.raises(ValueError, match="shape|horizon"):
        simulate_factors([1], factors, plan)


@pytest.mark.parametrize("attainment", [0, -1, 1.1, np.nan, np.inf, True, "0.8"])
def test_invalid_attainment_rejected(attainment):
    plan = settings()
    with pytest.raises(ValueError, match="attainment"):
        inverse_planning_factors([1], factors_for(plan), plan, attainment=attainment)


@pytest.mark.parametrize("mutation", [
    {"initial_capital": np.nan}, {"monthly_contribution": -1}, {"target_income": 0},
    {"annual_cost_percent": 100}, {"end_age": 30}, {"age": 30.5},
])
def test_invalid_plan_values_rejected(mutation):
    plan = settings().model_copy(update=mutation)
    with pytest.raises(ValueError):
        simulate_factors([1], factors_for(settings()), plan)


@pytest.mark.parametrize("shape", [(2, 24, 1), (0, 36, 1), (2, 36, 0), (2, 36)])
def test_invalid_stock_shapes_rejected(shape):
    plan = settings()
    paths = np.zeros(shape)
    with pytest.raises(ValueError, match="shape"):
        build_stock_factors(paths, paths, plan)


@pytest.mark.parametrize("returns,cash", [(np.nan, 0), (np.inf, 0), (-1.01, 0),
                                          (0, -.1), (0, np.inf), (0, 1.1), (-.9, .2)])
def test_invalid_stock_returns_or_cash_rejected(returns, cash):
    plan = settings()
    with pytest.raises(ValueError):
        build_stock_factors(np.full((1, 36, 1), returns), np.full((1, 36, 1), cash), plan)


def test_stack_rejects_missing_arrays_unequal_horizon_and_rebalancing():
    plan = settings()
    factors = factors_for(plan)
    with pytest.raises(ValueError):
        stack_factors([])
    with pytest.raises(ValueError):
        stack_factors([{}])
    with pytest.raises(ValueError):
        stack_factors([factors, factors_for(settings(end_age=34))])
    with pytest.raises(ValueError):
        stack_factors([factors, factors_for(plan, count=4)])
    with pytest.raises(ValueError, match="principal"):
        stack_factors([factors | {"principal_counted_as_income": True}])
    with pytest.raises(ValueError, match="rebalance"):
        stack_factors([factors | {"annual_rebalance_between_classes": True}])
    with pytest.raises(ValueError, match="channels"):
        stack_factors([factors | {"channels": ["monthly_contribution", "initial_capital"]}])


def test_inverse_rejects_multiple_portfolios_and_invalid_independent_units():
    plan = settings()
    factors = factors_for(plan)
    with pytest.raises(ValueError, match="one portfolio"):
        inverse_planning_factors([[1], [1]], factors, plan)
    for unit in [np.zeros((3, 3)), np.zeros((2, 3, 2)), np.full((2, 3), np.nan),
                 np.full((2, 3), -1)]:
        with pytest.raises(ValueError):
            inverse_planning_factors([1], factors, plan, retirement_unit_income_factors=unit)


def test_large_finite_inputs_cannot_produce_nonfinite_outputs():
    plan = settings().model_copy(update={"initial_capital": 1e308})
    factors = factors_for(settings())
    factors["annual_income_factors"][0, 1:] = 100
    with pytest.raises(ValueError, match="overflow"):
        simulate_factors([1], factors, plan, True)
    with pytest.raises(ValueError, match="overflow"):
        inverse_planning_factors([1], factors, plan)


def test_finite_target_that_overflows_annualization_is_rejected():
    plan = settings().model_copy(update={"target_income": 1e308})
    with pytest.raises(ValueError, match="overflow"):
        simulate_factors([1], factors_for(settings()), plan)
    with pytest.raises(ValueError, match="overflow"):
        inverse_planning_factors([1], factors_for(settings()), plan)


def test_negative_retirement_unit_cannot_be_hidden_by_asset_aggregation():
    plan = settings()
    factors = factors_for(plan, assets=2)
    unit = np.empty((2, 3, 2))
    unit[:, :, 0] = -.1
    unit[:, :, 1] = .2
    with pytest.raises(ValueError, match="nonnegative"):
        inverse_planning_factors([.5, .5], factors, plan, retirement_unit_income_factors=unit)


def test_inverse_requirement_really_attains_target_despite_float_rounding():
    rng = np.random.default_rng(41)
    plan = settings(initial_capital=1, monthly_contribution=0, target_income=10,
                    end_age=61)
    factors = factors_for(plan, count=500)
    factors["annual_income_factors"][0, 1:, :, 0] = rng.uniform(0, 120, (30, 500))
    factors["annual_income_factors"][1, 1:, :, 0] = rng.uniform(.001, 100, (30, 500))
    for attainment in (.2, .8, 1):
        result = inverse_planning_factors([1], factors, plan, attainment=attainment)
        funded = plan.model_copy(update={"monthly_contribution": result["requiredMonthlyContribution"]})
        assert simulate_factors([1], factors, funded, True)["successFrequency"] >= attainment


def test_large_finite_deficit_median_does_not_overflow_averaging():
    plan = settings(end_age=32, initial_capital=0, monthly_contribution=0)
    factors = factors_for(plan, count=2)
    unbounded = plan.model_copy(update={"target_income": 1e307})
    result = simulate_factors([1], factors, unbounded, True)
    assert result["medianDeficit"] == 1.2e308
    assert_json_finite(result)


def test_randomized_direct_monthly_composition_matches_factors_without_rebalance():
    rng = np.random.default_rng(45)
    plan = settings(initial_capital=5000, monthly_contribution=120, annual_cost_percent=.8,
                    end_age=35, retirement_age=32, target_income=20)
    count, months, assets = 9, 60, 3
    returns = rng.uniform(-.03, .04, (count, months, assets))
    cash = rng.uniform(0, .009, returns.shape)
    weights = np.array([.2, .3, .5])
    factors = build_stock_factors(returns, cash, plan)
    result = simulate_factors(weights, factors, plan, True)
    holdings = np.broadcast_to(5000 * weights, (count, assets)).copy()
    income = np.zeros(count)
    annual = []
    wealth = [holdings.sum(axis=1)]
    cost = (1 - .008) ** (1 / 12)
    for month in range(months):
        if month < 24:
            holdings *= (1 + returns[:, month]) * cost
            holdings += 120 * weights
        else:
            paid = holdings * cash[:, month]
            income += paid.sum(axis=1)
            holdings = (holdings * (1 + returns[:, month]) - paid) * cost
        if (month + 1) % 12 == 0:
            wealth.append(holdings.sum(axis=1))
            if month >= 24:
                annual.append(income.copy())
                income.fill(0)
    wealth = np.stack(wealth)
    annual = np.stack(annual)
    assert result["retirementWealth"] == pytest.approx(np.percentile(wealth[2], [10, 50, 90]))
    assert result["finalWealth"] == pytest.approx(np.percentile(wealth[-1], [10, 50, 90]))
    assert result["monthlyIncomeCapacity"] == pytest.approx(np.percentile(annual.min(axis=0) / 12, [10, 50, 90]))
    assert result["successFrequency"] == pytest.approx(np.mean(np.all(annual >= 240, axis=0)))
    assert result["meanUnfundedIncomeFraction"] == pytest.approx(np.maximum(240 - annual, 0).mean() / 240)
