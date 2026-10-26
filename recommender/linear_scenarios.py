"""Linear, per-asset cash-income scenarios without cross-asset rebalancing.

Factor channels are one real currency unit invested now and one real unit
contributed at each accumulation month end. ``wealth_factors`` has shape
(2, years + 1, scenarios, assets); annual income/loss factors have shape
(2, years, scenarios, assets). A single Treasury ledger may omit the asset
axis. Income is paid interest/dividends only: returned acquisition cost is
already reinvested by the originating ledger and must never enter income.

Factors must already include their applicable costs. Simulation/planning do
not charge fees or subtract capital losses again. All assets must use the
same scenario ordering, horizon, real numeraire, and end-month timing; shape
validation cannot establish economic alignment of independently sampled paths.
"""

from collections.abc import Mapping
import math

import numpy as np


FACTOR_KEYS = (
    "wealth_factors",
    "annual_income_factors",
    "annual_capital_loss_factors",
)
CHANNELS = ["initial_capital", "monthly_contribution"]


def _finite_array(value, name):
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite numeric values") from exc
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must contain finite numeric values")
    return result


def _factor_arrays(factors):
    if not isinstance(factors, Mapping) or any(key not in factors for key in FACTOR_KEYS):
        raise ValueError("factors must provide wealth, annual income and annual capital loss arrays")
    if "channels" in factors:
        channels = factors["channels"]
        if not isinstance(channels, (list, tuple)) or list(channels) != CHANNELS:
            raise ValueError("factor channels must be initial_capital and monthly_contribution, in that order")
    if factors.get("principal_counted_as_income", False):
        raise ValueError("principal must not be counted as retirement income")
    if factors.get("annual_rebalance_between_classes", False):
        raise ValueError("factors must not rebalance between assets or classes")
    arrays = []
    for key in FACTOR_KEYS:
        values = _finite_array(factors[key], key)
        if values.ndim == 3:
            values = values[..., None]
        if values.ndim != 4 or values.shape[0] != 2 or any(size < 1 for size in values.shape[1:]):
            raise ValueError(f"{key} must have shape (2, years, scenarios[, assets])")
        if np.any(values < 0):
            raise ValueError(f"{key} must be nonnegative")
        arrays.append(values)
    wealth, income, losses = arrays
    expected = (2, wealth.shape[1] - 1, wealth.shape[2], wealth.shape[3])
    if wealth.shape[1] < 2 or income.shape != expected or losses.shape != expected:
        raise ValueError("wealth, annual income and capital loss factor shapes must agree")
    if (not np.allclose(wealth[0, 0], 1, rtol=0, atol=1e-10)
            or np.any(wealth[1, 0] != 0)):
        raise ValueError("starting wealth must be one initial unit and zero contributions")
    return wealth, income, losses


def _plan_values(plan):
    try:
        ages = (plan.age, plan.retirement_age, plan.end_age)
        amounts = _finite_array(
            [plan.initial_capital, plan.monthly_contribution, plan.target_income,
             plan.annual_cost_percent], "plan amounts")
    except AttributeError as exc:
        raise ValueError("plan must provide ages, capital, contribution, income and annual cost") from exc
    if any(isinstance(age, (bool, np.bool_)) or not isinstance(age, (int, np.integer)) for age in ages):
        raise ValueError("plan ages must be integers")
    years = plan.end_age - plan.age
    accumulation = plan.retirement_age - plan.age
    if not 0 <= accumulation < years:
        raise ValueError("plan must include a nonnegative accumulation period and retirement years")
    if np.any(amounts < 0) or amounts[2] <= 0 or amounts[3] >= 100:
        raise ValueError("plan amounts must be nonnegative, income positive and annual cost below 100 percent")
    if amounts[2] > np.finfo(float).max / 12:
        raise ValueError("annual income target overflowed")
    return years, accumulation, amounts


def _inputs(weights, factors, plan):
    years, accumulation, amounts = _plan_values(plan)
    wealth, income, losses = _factor_arrays(factors)
    if wealth.shape[1] != years + 1:
        raise ValueError("factor horizon must match the plan's full years")
    weights = _finite_array(weights, "weights")
    if weights.ndim == 1:
        weights = weights[None, :]
    if (weights.ndim != 2 or not len(weights) or weights.shape[1] != wealth.shape[3]
            or np.any(weights < 0)
            or not np.allclose(weights.sum(axis=1), 1, rtol=0, atol=1e-8)):
        raise ValueError("weights must be nonnegative portfolio rows summing to one with one column per asset")
    return weights, wealth, income, losses, years, accumulation, amounts


def stack_factors(factor_sets):
    """Concatenate stock batches and/or single-asset ledgers in weight order.

    The caller must preserve shared scenario identities and cost assumptions.
    Input dictionaries/arrays are not modified. Income during accumulation is
    ignored by simulation; originating ledgers must already reinvest that cash.
    """
    arrays = [_factor_arrays(factors) for factors in factor_sets]
    if not arrays:
        raise ValueError("at least one asset factor set is required")
    for entry in arrays[1:]:
        if any(values.shape[:3] != reference.shape[:3]
               for values, reference in zip(entry, arrays[0])):
            raise ValueError("all assets must share the same horizon and scenario count")
    result = {key: np.concatenate([entry[index] for entry in arrays], axis=-1)
              for index, key in enumerate(FACTOR_KEYS)}
    result.update(channels=CHANNELS.copy(), principal_counted_as_income=False,
                  annual_rebalance_between_classes=False)
    return result


def _weighted_channels(values, weights):
    # Only annual scenario buffers are allocated, never candidate/month cubes.
    return np.einsum("cyna,a->cyn", values, weights)


def _funded_values(channels, initial_capital, monthly_contribution):
    with np.errstate(over="ignore", invalid="ignore"):
        result = channels[0] * initial_capital + channels[1] * monthly_contribution
    if not np.isfinite(result).all():
        raise ValueError("funded factors overflowed; scenario outputs must be finite")
    return result


def _interval95(frequency, count):
    denominator = 1 + 1.96**2 / count
    center = (frequency + 1.96**2 / (2 * count)) / denominator
    half = 1.96 * np.sqrt(
        frequency * (1 - frequency) / count + 1.96**2 / (4 * count**2)) / denominator
    return [float(max(0, min(frequency, center - half))),
            float(min(1, max(frequency, center + half)))]


def simulate_factors(weights, factors, plan, detailed=False):
    """Return the existing distributions-only ``simulate`` response schema.

    Each candidate allocates initial capital and contributions with its fixed
    weight vector. Holdings may drift: there is no later transfer across assets.
    Every retirement year's cash must meet the annual target, without selling
    principal or carrying excess income into another year's income test.
    Detailed percentiles/chart describe the first candidate, as in ``simulate``.
    Depletion is final wealth at zero, not a failure to reach the income target.
    """
    weights, wealth, income, _, years, accumulation, amounts = _inputs(weights, factors, plan)
    initial, contribution, target, _ = amounts
    count = wealth.shape[2]
    annual_target = 12 * target
    frequencies = np.empty(len(weights))
    unfunded = np.empty(len(weights))
    detail = None
    for index, portfolio in enumerate(weights):
        annual_income = _funded_values(
            _weighted_channels(income[:, accumulation:], portfolio), initial, contribution)
        shortfalls = np.maximum(annual_target - annual_income, 0)
        # Divide before summing to keep even very large finite inputs bounded.
        deficit_fractions = (shortfalls / annual_target).mean(axis=0)
        frequency = float(np.mean(np.all(annual_income >= annual_target, axis=0)))
        frequencies[index] = frequency
        unfunded[index] = deficit_fractions.mean()
        if detailed and index == 0:
            values = _funded_values(_weighted_channels(wealth, portfolio), initial, contribution)
            capacity = annual_income.min(axis=0) / 12
            with np.errstate(over="ignore"):
                deficits = shortfalls.sum(axis=0)
            if not np.isfinite(deficits).all():
                raise ValueError("cumulative income deficit overflowed")
            quantiles = np.percentile(values, [10, 50, 90], axis=1)
            detail = {
                "successFrequency": frequency,
                "samplingInterval95": _interval95(frequency, count),
                "retirementWealth": quantiles[:, accumulation].tolist(),
                "finalWealth": quantiles[:, -1].tolist(),
                "monthlyIncomeCapacity": np.percentile(capacity, [10, 50, 90]).tolist(),
                "meanUnfundedIncomeFraction": float(unfunded[index]),
                "medianDeficit": float(np.percentile(deficits, 50)),
                "depletionFrequency": float(np.mean(values[-1] <= 0)),
                "incomeCoveragePeriod": "annual_average",
                "chart": [{"age": plan.age + year, "p10": float(q[0]),
                           "p50": float(q[1]), "p90": float(q[2])}
                          for year, q in enumerate(quantiles.T)],
            }
    if detailed:
        return {"success": frequencies, **detail}
    return {"success": frequencies, "unfundedIncomeFraction": unfunded}


def _requirements(available, fixed_income, annual_target):
    gap = np.maximum(annual_target - fixed_income, 0)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        required = np.divide(gap, available, out=np.full_like(available, np.inf), where=available > 0)
    required[gap == 0] = 0
    # Division can round just below the true boundary; confirm with the same
    # multiply/add used by simulation rather than lowering the income target.
    for _ in range(3):
        with np.errstate(over="ignore", invalid="ignore"):
            underfunded = (available * required + fixed_income < annual_target) & np.isfinite(required)
        if not np.any(underfunded):
            break
        required[underfunded] = np.nextafter(required[underfunded], np.inf)
    # Solve each year's inequality first, then take the worst year per scenario.
    return required.max(axis=0)


def _empirical_requirement(values, attainment):
    value = float(np.sort(values)[math.ceil(attainment * len(values)) - 1])
    return value if np.isfinite(value) else None


def inverse_planning_factors(weights, factors, plan, *, attainment=.8,
                             retirement_unit_income_factors=None):
    """Solve annual income inequalities using the same cohorts as simulation.

    For each scenario, required funding is the maximum across retirement years
    of each year's residual target divided by that year's funding-channel
    income. The empirical order statistic then achieves the requested fraction
    of *whole scenario paths*, not separate per-year marginal percentiles.

    A current-age initial unit and a stream of contributions own different lot
    cohorts at retirement. Neither identifies income from a freshly invested
    retirement-age unit. ``requiredRetirementCapital`` therefore stays null,
    unless the caller supplies independently modeled annual real income per
    unit newly invested at retirement. That optional array is either
    (retirement_years, scenarios, assets) or already weighted
    (retirement_years, scenarios); it must use the same scenario identities.
    Null for an unidentifiable retirement unit is not a funding failure.
    """
    if (isinstance(attainment, (bool, np.bool_)) or not isinstance(attainment, (int, float, np.number))
            or not np.isfinite(attainment) or not 0 < attainment <= 1):
        raise ValueError("attainment must be finite, greater than zero and at most one")
    weights, _, income, _, years, accumulation, amounts = _inputs(weights, factors, plan)
    if len(weights) != 1:
        raise ValueError("inverse planning requires exactly one portfolio")
    initial_capital, contribution, target, _ = amounts
    channels = _weighted_channels(income[:, accumulation:], weights[0])
    capital_income, contribution_income = channels
    count = income.shape[2]
    annual_target = 12 * target
    fixed_capital = _funded_values(channels, initial_capital, 0)
    fixed_contribution = _funded_values(channels, 0, contribution)
    current = _funded_values(channels, initial_capital, contribution)
    monthly_required = _empirical_requirement(
        _requirements(contribution_income, fixed_capital, annual_target), attainment)
    initial_required = _empirical_requirement(
        _requirements(capital_income, fixed_contribution, annual_target), attainment)
    target_capital = None
    capital_basis = "not_identifiable_from_current_age_cohort_channels"
    capital_null_reason = "independent_retirement_start_unit_income_factors_not_supplied"
    if retirement_unit_income_factors is not None:
        unit = _finite_array(retirement_unit_income_factors, "retirement unit income factors")
        if np.any(unit < 0):
            raise ValueError("retirement unit income must be nonnegative")
        if unit.ndim == 3 and unit.shape[2] == income.shape[3]:
            unit = np.einsum("yna,a->yn", unit, weights[0])
        if unit.shape != (years - accumulation, count) or not np.isfinite(unit).all():
            raise ValueError("retirement unit income must be nonnegative with matching retirement years and scenarios")
        target_capital = _empirical_requirement(
            _requirements(unit, np.zeros_like(unit), annual_target), attainment)
        capital_basis = "independently_modeled_retirement_start_unit_annual_income"
        capital_null_reason = None if target_capital is not None else "no_finite_solution_at_requested_attainment"
    return {
        "status": "estimated" if monthly_required is not None or initial_required is not None else "no_finite_solution",
        "criterion": "empirical_scenario_attainment", "attainmentCriterion": float(attainment),
        "scenarioCount": count, "requiredRetirementCapital": target_capital,
        "requiredRetirementCapitalBasis": capital_basis,
        "requiredRetirementCapitalNullReason": capital_null_reason,
        "requiredMonthlyContribution": monthly_required,
        "requiredInitialCapitalWithCurrentContribution": initial_required,
        "currentMonthlyContribution": float(contribution),
        "monthlyContributionGap": max(0., monthly_required - contribution) if monthly_required is not None else None,
        "currentPlanSuccessFrequency": float(np.mean(np.all(current >= annual_target, axis=0))),
        # A structural zero: both funding channels pay zero in at least one year.
        "zeroIncomeScenarioFraction": float(np.mean(np.any(
            (capital_income == 0) & (contribution_income == 0), axis=0))),
        "portfolioReoptimized": False, "usesPrincipalForIncome": False,
        "taxBasis": "gross_before_individual_taxes",
        "contributionTiming": "end_of_month_real_purchasing_power",
    }


def build_stock_factors(total_real_return_paths, cash_real_paths, plan, *, include_retirement_unit=False):
    """Build independent stock channels without a candidate/month memory cube.

    Inputs share shape (scenarios, 12 * years, assets). Returns include cash;
    cash paths are payouts per unit of opening holdings in the same real
    numeraire. Before retirement total returns reinvest all distributions. After
    retirement cash is recorded as income and subtracted exactly once from
    total returns. End-month contributions stop at retirement. Fees reduce
    holdings monthly, after returns/payouts and before new contributions.

    Stock price losses are reflected in market wealth, not booked as realized
    capital losses: this helper never sells principal to fund income. Its loss
    factor array is therefore zero. Treasury capital repayment/loss accounting
    belongs in the supplied stateful lot ledger, not in this stock helper.
    """
    years, accumulation, amounts = _plan_values(plan)
    returns = _finite_array(total_real_return_paths, "total real returns")
    cash = _finite_array(cash_real_paths, "real cash payments")
    if (returns.ndim != 3 or any(size < 1 for size in returns.shape)
            or returns.shape[1] != 12 * years or cash.shape != returns.shape):
        raise ValueError("stock paths must share shape (scenarios, 12 * plan years, assets)")
    if np.any(returns < -1) or np.any(cash < 0) or np.any(cash > 1 + returns):
        raise ValueError("cash must be nonnegative and not exceed available total-return value")
    count, months, assets = returns.shape
    if type(include_retirement_unit) is not bool:
        raise ValueError('invalid retirement unit channel')
    channels = 3 if include_retirement_unit else 2
    holdings = np.zeros((channels, count, assets))
    holdings[0] = 1
    wealth = np.empty((channels, years + 1, count, assets))
    wealth[:, 0] = holdings
    income = np.zeros((channels, years, count, assets))
    annual_income = np.zeros_like(holdings)
    annual_weighted_income = np.zeros_like(holdings)
    weighted_income = np.zeros_like(income)
    cost_factor = (1 - amounts[3] / 100) ** (1 / 12)
    for month in range(months):
        if include_retirement_unit and month == 12 * accumulation:
            holdings[2] = 1
        with np.errstate(over="ignore", invalid="ignore"):
            if month < 12 * accumulation:
                holdings *= (1 + returns[:, month]) * cost_factor
                holdings[1] += 1
            else:
                paid = holdings * cash[:, month]
                annual_income += paid
                annual_weighted_income += paid * ((11 - month % 12) / 12)
                holdings *= (1 + returns[:, month] - cash[:, month]) * cost_factor
        if not np.isfinite(holdings).all() or not np.isfinite(annual_income).all():
            raise ValueError("stock factor propagation overflowed")
        if (month + 1) % 12 == 0:
            year = (month + 1) // 12
            wealth[:, year] = holdings
            income[:, year - 1] = annual_income
            weighted_income[:, year - 1] = annual_weighted_income
            annual_income.fill(0)
            annual_weighted_income.fill(0)
    result = {"wealth_factors": wealth[:2], "annual_income_factors": income[:2],
            "annual_weighted_income_factors": weighted_income[:2],
            "annual_capital_loss_factors": np.zeros_like(income[:2]),
            "channels": CHANNELS.copy(), "principal_counted_as_income": False,
            "annual_rebalance_between_classes": False,
            "annual_cost_percent": float(amounts[3]),
            "contribution_timing": "end_of_month_real_purchasing_power"}
    if include_retirement_unit:
        result['retirement_unit_annual_income_factors'] = income[2, accumulation:]
        result['retirement_unit_basis'] = 'one_real_unit_invested_at_retirement_no_prior_accumulation'
    return result
