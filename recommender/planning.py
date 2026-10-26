"""Inverse cash-income planning on the same portfolio and simulated scenarios."""

import math

import numpy as np


def income_factors(weights, paths, cash_paths, plan):
    weights = np.asarray(weights, dtype=float)
    count = len(paths)
    accumulation = 12 * (plan.retirement_age - plan.age)
    cost = (1 - plan.annual_cost_percent / 100) ** (1 / 12)
    capital = np.broadcast_to(weights, (count, len(weights))).copy()
    contribution = np.zeros_like(capital)
    for month in range(accumulation):
        factor = (1 + paths[:, month]) * cost
        capital *= factor
        contribution = contribution * factor + weights
        if (month + 1) % 12 == 0:
            capital = capital.sum(axis=1)[:, None] * weights
            contribution = contribution.sum(axis=1)[:, None] * weights
    capital_growth, contribution_growth = capital.sum(axis=1), contribution.sum(axis=1)
    holdings = np.broadcast_to(weights, (count, len(weights))).copy()
    annual = np.zeros(count)
    income_per_capital = np.full(count, np.inf)
    for month in range(accumulation, paths.shape[1]):
        paid = holdings * cash_paths[:, month]
        annual += paid.sum(axis=1)
        holdings = np.maximum(0, holdings * (1 + paths[:, month]) - paid) * cost
        if (month + 1) % 12 == 0:
            income_per_capital = np.minimum(income_per_capital, annual / 12)
            annual.fill(0)
            holdings = holdings.sum(axis=1)[:, None] * weights
    if not all(np.isfinite(values).all() for values in (capital_growth, contribution_growth, income_per_capital)):
        raise ValueError("Os cenarios nao permitem calcular fatores de planejamento finitos.")
    return capital_growth, contribution_growth, income_per_capital


def inverse_planning(weights, paths, cash_paths, plan, *, attainment=.8):
    if not 0 < attainment <= 1:
        raise ValueError("O criterio de atendimento precisa estar entre zero e um.")
    capital, contribution, income = income_factors(weights, paths, cash_paths, plan)
    required = np.divide(plan.target_income, income, out=np.full_like(income, np.inf), where=income > 0)
    contribution_gap = np.maximum(0, required - plan.initial_capital * capital)
    monthly = np.divide(contribution_gap, contribution, out=np.full_like(income, np.inf), where=contribution > 0)
    monthly[contribution_gap == 0] = 0
    capital_gap = np.maximum(0, required - plan.monthly_contribution * contribution)
    initial = np.divide(capital_gap, capital, out=np.full_like(income, np.inf), where=capital > 0)
    initial[capital_gap == 0] = 0

    def empirical_requirement(values):
        value = float(np.sort(values)[math.ceil(attainment * len(values)) - 1])
        return value if np.isfinite(value) else None

    target_capital = empirical_requirement(required)
    monthly_required = empirical_requirement(monthly)
    return {"status": "estimated" if target_capital is not None and monthly_required is not None else "no_finite_solution",
            "criterion": "empirical_scenario_attainment", "attainmentCriterion": attainment,
            "scenarioCount": len(paths), "requiredRetirementCapital": target_capital,
            "requiredMonthlyContribution": monthly_required,
            "requiredInitialCapitalWithCurrentContribution": empirical_requirement(initial),
            "currentMonthlyContribution": plan.monthly_contribution,
            "monthlyContributionGap": max(0, monthly_required - plan.monthly_contribution) if monthly_required is not None else None,
            "currentPlanSuccessFrequency": float(np.mean((plan.initial_capital * capital + plan.monthly_contribution * contribution) * income >= plan.target_income)),
            "zeroIncomeScenarioFraction": float(np.mean(income <= 0)),
            "portfolioReoptimized": False, "usesPrincipalForIncome": False,
            "taxBasis": "gross_before_individual_taxes", "contributionTiming": "end_of_month_real_purchasing_power"}
