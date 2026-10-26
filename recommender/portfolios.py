"""Income-oriented portfolio candidates inspired by the local dividend analyzer."""

from datetime import date

import numpy as np
from scipy.optimize import linprog, minimize


def asset_priority(assets, real_history, cash_history, dates, annual_cash):
    observed = min(36, len(dates))
    months = np.array([date.fromisoformat(value).month for value in dates[-observed:]])
    rows = []
    for index, asset in enumerate(assets):
        recurring, consistency = [], []
        if cash_history is not None:
            for month in range(1, 13):
                payments = cash_history[-observed:, index][months == month]
                if len(payments) and np.mean(payments > 0) >= .5:
                    recurring.append(month)
                    consistency.append(float(np.mean(payments > 0)))
        alerts = []
        if asset.asset_class == "acoes" and asset.features is not None:
            _, _, earnings, free_cash, payout, _, _ = asset.features
            if earnings <= 0:
                alerts.append("Lucro por acao nao positivo na referencia.")
            if free_cash is None:
                alerts.append("FCL industrial indisponivel para instituicao financeira; nao avaliado como zero.")
            elif free_cash <= 0:
                alerts.append("Fluxo de caixa livre por acao nao positivo.")
            if payout > 1:
                alerts.append("Proventos anteriores superiores ao lucro de referencia.")
        volatility = float(np.std(real_history[:, index], ddof=1) * np.sqrt(12))
        rows.append({"ticker": asset.ticker, "name": asset.name, "assetClass": asset.asset_class,
                     "riskLevel": asset.risk_level, "annualVolatility": volatility,
                     "cashYield12m": float(annual_cash[index]), "qualityAlerts": alerts,
                     "historicalPaymentMonths": recurring,
                     "historicalConsistency": float(np.mean(consistency)) if consistency else None,
                     "paymentHistoryMonths": observed if cash_history is not None else 0})
    rows.sort(key=lambda row: (row["riskLevel"], len(row["qualityAlerts"]),
                               row["annualVolatility"], -row["cashYield12m"], row["ticker"]))
    return [row | {"safetyOrder": index + 1} for index, row in enumerate(rows)]


def payment_calendar(assets, weights, ranking):
    active = {asset.ticker for asset, weight in zip(assets, weights) if weight > 1e-6}
    calendar = [{"month": month, "tickers": [row["ticker"] for row in ranking
                if row["ticker"] in active and month in row["historicalPaymentMonths"]]}
                for month in range(1, 13)]
    return calendar, sum(bool(row["tickers"]) for row in calendar)


def candidate_seeds(assets, ranking, annual_cash, upper, matrix, bounds, feasible,
                    volatility_of, maximum_volatility, project):
    constraints = [{"type": "eq", "fun": lambda weights: weights.sum() - 1},
                   {"type": "ineq", "fun": lambda weights: bounds - matrix @ weights}]
    limits = list(zip(np.zeros(len(assets)), upper))
    safe = minimize(lambda weights: float(volatility_of(weights)[0]) ** 2, feasible,
                    method="SLSQP", bounds=limits, constraints=constraints,
                    options={"maxiter": 150, "ftol": 1e-12})
    seeds = [feasible.copy()]
    if safe.success:
        seeds.append(safe.x)
    income = minimize(lambda weights: -float(weights @ annual_cash), seeds[-1],
                      method="SLSQP", bounds=limits,
                      constraints=[*constraints, {"type": "ineq", "fun": lambda weights:
                                   maximum_volatility - float(volatility_of(weights)[0])}],
                      options={"maxiter": 150, "ftol": 1e-12})
    if income.success:
        seeds.append(income.x)
    # Greedy monthly-coverage combinations remain small even for a larger universe.
    by_ticker = {asset.ticker: index for index, asset in enumerate(assets)}
    liquid = {index for index, asset in enumerate(assets) if asset.asset_class == "liquidez"}
    selected, covered = set(liquid), set()
    while len(selected) < min(7, len(assets)):
        choices = [row for row in ranking if by_ticker[row["ticker"]] not in selected]
        if not choices:
            break
        row = min(choices, key=lambda item: (-len(set(item["historicalPaymentMonths"]) - covered),
                                             item["safetyOrder"], -item["cashYield12m"]))
        selected.add(by_ticker[row["ticker"]])
        covered.update(row["historicalPaymentMonths"])
        if len(selected) < min(3, len(assets)):
            continue
        subset_upper = np.array([limit if index in selected else 0 for index, limit in enumerate(upper)])
        subset = linprog(np.zeros(len(assets)), A_ub=matrix, b_ub=bounds,
                         A_eq=np.ones((1, len(assets))), b_eq=[1],
                         bounds=list(zip(np.zeros(len(assets)), subset_upper)), method="highs")
        if subset.success:
            target = np.array([1 / len(selected) if index in selected else 0 for index in range(len(assets))])
            seeds.append(project(target, subset_upper, matrix, bounds, subset.x))
    return feasible_unique(seeds, upper, matrix, bounds, volatility_of, maximum_volatility)


def feasible_unique(weights, upper, matrix, bounds, volatility_of, maximum_volatility):
    accepted = []
    for candidate in weights:
        candidate = np.asarray(candidate, dtype=float)
        if (not np.isfinite(candidate).all() or abs(candidate.sum() - 1) > 1e-6
                or np.any(candidate < -1e-8) or np.any(candidate > upper + 1e-7)
                or np.any(matrix @ candidate > bounds + 1e-7)
                or float(volatility_of(candidate)[0]) > maximum_volatility + 1e-7):
            continue
        candidate = np.maximum(candidate, 0)
        candidate[candidate < 1e-12] = 0
        candidate /= candidate.sum()
        if (np.any(candidate > upper + 1e-7) or np.any(matrix @ candidate > bounds + 1e-7)
                or float(volatility_of(candidate)[0]) > maximum_volatility + 1e-7):
            continue
        if not any(np.allclose(candidate, existing, atol=1e-6, rtol=0) for existing in accepted):
            accepted.append(candidate)
    return np.asarray(accepted)


def comparison_choices(candidates, volatility, annual_cash, preferred):
    cash = candidates @ annual_cash
    safety = int(np.lexsort((-cash, volatility))[0])
    income = int(np.lexsort((volatility, -cash))[0])
    roles = [(safety, "seguranca", "Prioridade em segurança relativa")]
    if income != safety:
        roles.append((income, "proventos", "Prioridade em proventos"))
    if preferred not in [row[0] for row in roles]:
        roles.append((int(preferred), "equilibrio", "Equilíbrio entre risco e renda"))
    roles.sort(key=lambda row: (volatility[row[0]], -cash[row[0]], row[1]))
    selected_id = next(row[1] for row in roles if row[0] == preferred)
    return roles, selected_id
