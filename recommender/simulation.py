import numpy as np

from .contracts import Plan


def block_indices(length, months, count, block, seed):
    rng = np.random.default_rng(seed)
    if block > length:
        raise ValueError("Bloco maior que o histórico.")
    starts = rng.integers(0, length - block + 1, size=(count, (months + block - 1) // block))
    return (starts[..., None] + np.arange(block)).reshape(count, -1)[:, :months]


def bootstrap_paths(returns, inflation, months: int, count: int, block: int, seed: int):
    indices = block_indices(len(returns), months, count, block, seed)
    # One set of block indices keeps observed cross-asset/inflation dependence.
    nominal = returns[indices]
    return (1 + nominal) / (1 + inflation[indices, None]) - 1


def bootstrap_cash_paths(cash_returns, inflation, months, count, block, seed):
    indices = block_indices(len(cash_returns), months, count, block, seed)
    return cash_returns[indices] / (1 + inflation[indices, None])


def simulate_distributions(weights, paths, cash_paths, plan, detailed):
    weights = np.atleast_2d(weights)
    accumulation = 12 * (plan.retirement_age - plan.age)
    candidates, scenarios, assets = len(weights), paths.shape[0], paths.shape[2]
    holdings = np.broadcast_to(weights[:, None, :] * plan.initial_capital, (candidates, scenarios, assets)).copy()
    cost_factor = (1 - plan.annual_cost_percent / 100) ** (1 / 12)
    success = np.ones((candidates, scenarios), dtype=bool)
    deficits = np.zeros((candidates, scenarios))
    annual_income = np.zeros((candidates, scenarios))
    capacity = np.full((candidates, scenarios), np.inf)
    charts = [(0, np.full(scenarios, plan.initial_capital))] if detailed else []
    retirement_wealth = None
    for month in range(paths.shape[1]):
        if month < accumulation:
            holdings *= (1 + paths[None, :, month, :]) * cost_factor
            holdings += weights[:, None, :] * plan.monthly_contribution
        else:
            cash = holdings * cash_paths[None, :, month, :]
            annual_income += cash.sum(axis=2)
            # Total returns already include the cash paid out; remove it once.
            holdings = np.maximum(0, holdings * (1 + paths[None, :, month, :]) - cash) * cost_factor
        total = holdings.sum(axis=2)
        if (month + 1) % 12 == 0:
            if month >= accumulation:
                shortfall = np.maximum(0, 12 * plan.target_income - annual_income)
                deficits += shortfall
                success &= shortfall == 0
                capacity = np.minimum(capacity, annual_income / 12)
                annual_income.fill(0)
            # Rebalancing reallocates principal; it does not pay retirement income.
            holdings = total[..., None] * weights[:, None, :]
        if month + 1 == accumulation:
            retirement_wealth = total.copy()
        if detailed and (month + 1) % 12 == 0:
            charts.append((month + 1, total[0].copy()))
    output = {"success": success.mean(axis=1),
              "unfundedIncomeFraction": deficits.mean(axis=1) / (12 * (plan.end_age - plan.retirement_age) * plan.target_income)}
    if detailed:
        frequency = float(output["success"][0]);n = scenarios
        denominator = 1 + 1.96**2 / n
        center = (frequency + 1.96**2 / (2*n)) / denominator
        half = 1.96 * np.sqrt(frequency * (1-frequency) / n + 1.96**2 / (4*n*n)) / denominator
        output.update({"successFrequency": frequency,
                       "samplingInterval95": [float(max(0, center-half)), float(min(1, center+half))],
                       "retirementWealth": np.percentile(retirement_wealth[0], [10, 50, 90]).tolist(),
                       "finalWealth": np.percentile(total[0], [10, 50, 90]).tolist(),
                       "monthlyIncomeCapacity": np.percentile(capacity[0], [10, 50, 90]).tolist(),
                       "meanUnfundedIncomeFraction": float(output["unfundedIncomeFraction"][0]),
                       "medianDeficit": float(np.median(deficits[0])),
                       "depletionFrequency": float(np.mean(total[0] <= 0)),
                       "incomeCoveragePeriod": "annual_average",
                       "chart": [{"age": plan.age + month / 12, "p10": float(q[0]), "p50": float(q[1]), "p90": float(q[2])}
                                 for month, values in charts for q in [np.percentile(values, [10, 50, 90])]]})
        output.pop("unfundedIncomeFraction")
    return output


def monthly_income_capacity(weights, paths, retirement_wealth, annual_cost_percent):
    """Maximum constant real withdrawal on each path, allowing principal drawdown."""
    weights = np.asarray(weights, dtype=float)
    holdings = np.broadcast_to(weights, (len(paths), len(weights))).copy()
    monthly_cost_factor = (1 - annual_cost_percent / 100) ** (1 / 12)
    factors = np.empty(paths.shape[:2], dtype=float)
    # Proportional withdrawals do not change the portfolio's relative weights.
    for month in range(paths.shape[1]):
        holdings *= (1 + paths[:, month, :]) * monthly_cost_factor
        total = holdings.sum(axis=1)
        factors[:, month] = total
        holdings = np.divide(holdings, total[:, None], out=np.zeros_like(holdings), where=total[:, None] > 0)
        if (month + 1) % 12 == 0:
            holdings[:] = weights
    annuity = np.zeros(len(paths))
    with np.errstate(over="ignore", divide="ignore"):
        for month in range(paths.shape[1] - 1, -1, -1):
            annuity = (1 + annuity) / factors[:, month]
    income = np.divide(retirement_wealth, annuity, out=np.zeros_like(annuity), where=annuity > 0)
    if not np.isfinite(income).all() or np.any(income < 0):
        raise ValueError("Os dados nao permitem estimar uma renda mensal finita.")
    return income


def simulate(weights, paths, plan: Plan, detailed=False, *, cash_paths=None):
    if plan.income_mode == "distributions_only":
        if cash_paths is None:
            raise ValueError("Faltam pagamentos verificados para simular renda sem vender o patrimonio.")
        if (cash_paths.shape != paths.shape or not np.isfinite(cash_paths).all()
                or np.any(cash_paths < 0) or np.any(cash_paths > 1 + paths)):
            raise ValueError("Pagamentos incompativeis com os retornos totais dos cenarios.")
        return simulate_distributions(weights, paths, cash_paths, plan, detailed)
    weights = np.atleast_2d(weights)
    accumulation = 12 * (plan.retirement_age - plan.age)
    months = paths.shape[1]
    candidates, scenarios, assets = len(weights), paths.shape[0], paths.shape[2]
    holdings = np.broadcast_to(weights[:, None, :] * plan.initial_capital, (candidates, scenarios, assets)).copy()
    success = np.ones((candidates, scenarios), dtype=bool)
    deficits = np.zeros((candidates, scenarios))
    depleted = np.full((candidates, scenarios), -1, dtype=int)
    annual_cost = plan.annual_cost_percent / 100
    monthly_cost = 1 - (1 - annual_cost) ** (1 / 12)
    charts = []
    retirement_wealth = None
    if detailed:
        charts.append((0, np.full(scenarios, plan.initial_capital)))
    for month in range(months):
        holdings *= 1 + paths[None, :, month, :]
        holdings *= 1 - monthly_cost
        total = holdings.sum(axis=2)
        if month < accumulation:
            holdings += weights[:, None, :] * plan.monthly_contribution
        else:
            paid = np.minimum(total, plan.target_income)
            shortfall = plan.target_income - paid
            success &= shortfall == 0
            deficits += shortfall
            newly_depleted = (shortfall > 0) & (depleted < 0)
            depleted[newly_depleted] = month
            scale = np.divide(total - paid, total, out=np.zeros_like(total), where=total > 0)
            holdings *= scale[..., None]
        total = holdings.sum(axis=2)
        if (month + 1) % 12 == 0:
            holdings = total[..., None] * weights[:, None, :]
        if month + 1 == accumulation:
            retirement_wealth = total.copy()
        if detailed and ((month + 1) % 12 == 0 or month + 1 == months):
            charts.append((month + 1, total[0].copy()))
    retirement_months = months - accumulation
    output = {"success": success.mean(axis=1),
              "unfundedIncomeFraction": deficits.mean(axis=1) / (retirement_months * plan.target_income)}
    if detailed:
        frequency = float(output["success"][0])
        n = scenarios
        denominator = 1 + 1.96**2 / n
        center = (frequency + 1.96**2 / (2*n)) / denominator
        half = 1.96 * np.sqrt(frequency * (1-frequency) / n + 1.96**2 / (4*n*n)) / denominator
        output.update({
            "successFrequency": frequency,
            "samplingInterval95": [float(max(0, min(frequency, center-half))), float(min(1, max(frequency, center+half)))],
            "retirementWealth": np.percentile(retirement_wealth[0], [10, 50, 90]).tolist(),
            "finalWealth": np.percentile(total[0], [10, 50, 90]).tolist(),
            "medianDeficit": float(np.median(deficits[0])),
            "depletionFrequency": float(np.mean(depleted[0] >= 0)),
            "monthlyIncomeCapacity": np.percentile(monthly_income_capacity(
                weights[0], paths[:, accumulation:, :], retirement_wealth[0], plan.annual_cost_percent), [10, 50, 90]).tolist(),
            "meanUnfundedIncomeFraction": float(output["unfundedIncomeFraction"][0]),
            "chart": [{"age": plan.age + month / 12, "p10": float(q[0]), "p50": float(q[1]), "p90": float(q[2])}
                      for month, values in charts for q in [np.percentile(values, [10, 50, 90])]],
        })
        output.pop("unfundedIncomeFraction")
    return output
