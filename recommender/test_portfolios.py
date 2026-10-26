from copy import deepcopy

import numpy as np
import pytest

from . import engine
from .contracts import RecommendationRequest
from .datasets import demo_dataset
from .portfolios import asset_priority, comparison_choices, feasible_unique, payment_calendar
from .test_engine import investor, plan


@pytest.fixture(scope="module")
def comparison():
    data = deepcopy(demo_dataset())
    data.assets = [asset.model_copy(update={"annual_cash_yield": 0})
                   if asset.asset_class == "liquidez" else asset for asset in data.assets]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engine, "get_dataset", lambda mode: data)
        return engine.compute(RecommendationRequest(
            investor=investor(2), data_mode="demo", forecast_policy="xgboost_experimental",
            plan=plan(income_mode="distributions_only", age=30, retirement_age=50,
                      end_age=60, initial_capital=100000, monthly_contribution=1000)),
            generations=3, population=8, search_count=8)


def test_portfolios_are_distinct_feasible_and_ordered_by_relative_safety(comparison):
    portfolios = comparison["portfolios"]
    assert 2 <= len(portfolios) <= 3
    vols = [portfolio["portfolio"]["annualVolatility"] for portfolio in portfolios]
    assert vols == sorted(vols)
    assert comparison["selectedPortfolioId"] in {portfolio["id"] for portfolio in portfolios}
    unique = set()
    for rank, portfolio in enumerate(portfolios, 1):
        assert portfolio["safetyRank"] == rank
        allocation = portfolio["allocation"]
        assert sum(asset["weight"] for asset in allocation) == pytest.approx(1)
        equity = sum(asset["weight"] for asset in allocation if asset["assetClass"] == "acoes")
        liquid = sum(asset["weight"] for asset in allocation if asset["assetClass"] == "liquidez")
        assert equity <= .6500001 and liquid >= .0999999
        assert all(asset["weight"] <= .2500001 for asset in allocation if asset["assetClass"] == "acoes")
        assert portfolio["portfolio"]["annualVolatility"] <= .2800001
        weights = tuple(sorted((asset["ticker"], round(asset["weight"], 6)) for asset in allocation))
        assert weights not in unique
        unique.add(weights)
        assert portfolio["incomeAnalysis"]["monthlyCapacity"] == portfolio["simulation"]["monthlyIncomeCapacity"]
        assert portfolio["incomeAnalysis"]["usesPrincipal"] is False
    chosen = next(portfolio for portfolio in portfolios if portfolio["id"] == comparison["selectedPortfolioId"])
    assert chosen["allocation"] == comparison["allocation"]
    assert chosen["simulation"] == comparison["simulation"]


def test_each_portfolio_is_evaluated_on_the_same_independent_final_market_paths(monkeypatch):
    calls = []
    original = engine.simulate

    def record(weights, paths, settings, detailed=False, *, cash_paths=None):
        if detailed:
            calls.append((id(paths), id(cash_paths), np.asarray(weights).copy()))
        return original(weights, paths, settings, detailed=detailed, cash_paths=cash_paths)

    monkeypatch.setattr(engine, "simulate", record)
    result = engine.compute(RecommendationRequest(investor=investor(2), data_mode="demo",
        plan=plan(income_mode="distributions_only")), generations=3, population=8, search_count=8)
    assert len(calls) == len(result["portfolios"])
    assert len({call[:2] for call in calls}) == 1
    assert result["construction"]["commonFinalSeed"] == result["parameters"]["finalSeed"]
    assert result["construction"]["safetyIsGuarantee"] is False


def test_historical_calendar_counts_observed_payments_not_ex_dates():
    assets = demo_dataset().assets[:2]
    dates = [f"{year}-{month:02d}-01" for year in (2024, 2025) for month in range(1, 13)]
    cash = np.zeros((24, 2))
    cash[[0, 12], 0] = .01
    cash[5, 1] = .01  # One of two Junes meets the explicit 50% threshold.
    rows = asset_priority(assets, np.zeros((24, 2)), cash, dates, [.1, .2])
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker[assets[0].ticker]["historicalPaymentMonths"] == [1]
    assert by_ticker[assets[1].ticker]["historicalPaymentMonths"] == [6]
    calendar, covered = payment_calendar(assets, [1, 0], rows)
    assert covered == 1 and calendar[0]["tickers"] == [assets[0].ticker]
    assert calendar[5]["tickers"] == []
    assert len(calendar) == 12


def test_quality_alerts_do_not_treat_government_bonds_as_companies():
    data = deepcopy(demo_dataset())
    data.assets[4].features[4] = 2
    rows = asset_priority(data.assets, data.returns, None, data.dates, np.zeros(len(data.assets)))
    by_ticker = {row["ticker"]: row for row in rows}
    assert any("lucro" in alert for alert in by_ticker[data.assets[4].ticker]["qualityAlerts"])
    assert not by_ticker[data.assets[0].ticker]["qualityAlerts"]
    assert all(row["historicalPaymentMonths"] == [] for row in rows)


def test_one_feasible_composition_is_not_duplicated_as_three_portfolios():
    roles, selected = comparison_choices(np.array([[1.]]), np.array([.01]), np.array([0.]), 0)
    assert len(roles) == 1 and selected == "seguranca"


def test_more_dividend_income_does_not_override_the_safety_sort():
    roles, _ = comparison_choices(np.array([[1, 0], [0, 1], [.5, .5]]),
                                  np.array([.01, .2, .1]), np.array([0., .1]), 2)
    assert [role[1] for role in roles] == ["seguranca", "equilibrio", "proventos"]


def test_numerical_dust_cannot_manufacture_cash_income_or_duplicate_portfolios():
    weights = feasible_unique([[1 - 1e-15, 1e-15], [1, 0]], np.ones(2),
                               np.zeros((1, 2)), np.zeros(1),
                               lambda values: np.atleast_2d(values).sum(axis=1) * .01, .1)
    np.testing.assert_array_equal(weights, [[1, 0]])
    np.testing.assert_array_equal(weights @ [0, .1], [0])
