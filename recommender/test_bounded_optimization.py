"""Synthetic expanded universes: no network, credentials or market-data claims."""

import csv
from datetime import date
import json
from types import SimpleNamespace

import numpy as np
import pytest

from . import engine
from .contracts import Asset, Investor, Plan, RecommendationRequest
from .datasets import Dataset, MAX_REAL_ASSETS, real_dataset
from .portfolios import asset_priority


SOURCE = "Synthetic bounded-universe test fixture; not market data."


def expanded_dataset(stock_count=100, anchor_count=9, with_exclusions=True):
    reference = date.today().replace(day=1)
    dates = []
    for offset in range(59, -1, -1):
        year, month = divmod(reference.year * 12 + reference.month - 1 - offset, 12)
        dates.append(date(year, month + 1, 1).isoformat())
    stocks = [Asset(ticker=f"STOCK{i:03d}", name=f"Synthetic stock {i}", asset_class="acoes",
                    risk_level=1, minimum_years=5, liquidity_days=2, price=20,
                    features=[1 + i / 100, .8, 4, 3, .4, .3, float(i % 8)],
                    as_of=dates[-1], source=SOURCE) for i in range(stock_count)]
    excluded = []
    if with_exclusions:
        for ticker, updates in [("RISK", {"risk_level": 2}), ("TERM", {"minimum_years": 7}),
                                ("COMPLEX", {"complex": True}), ("NO-FEATURES", {"features": None})]:
            excluded.append(stocks[0].model_copy(update={"ticker": ticker, **updates}))
    anchors, bonds = [], []
    for i in range(anchor_count):
        liquid, coupon = i < 2, 2 <= i < 6
        kind = "Tesouro Selic" if liquid else "Tesouro IPCA+ com Juros Semestrais" if coupon else "Tesouro Prefixado"
        ticker = f"TD-{i:02d}"
        anchors.append(Asset(ticker=ticker, name=f"Synthetic {kind} {i}",
                             asset_class="liquidez" if liquid else "renda_fixa", risk_level=0,
                             minimum_years=0, liquidity_days=1, price=1000,
                             annual_cash_yield=.04 if coupon else 0, as_of=dates[-1], source=SOURCE))
        bonds.append({"ticker": ticker, "kind": kind, "maturity": f"{reference.year + i + 1}-01-15"})
    # Exclusions precede every anchor, so eligible-local positions differ from dataset columns.
    assets = stocks + excluded + anchors
    history = np.array([.004 + (.0002 + i * .00001) * np.sin(np.arange(60) + i)
                        for i in range(len(assets))]).T
    cash = np.zeros_like(history)
    for i, asset in enumerate(assets):
        if asset.asset_class == "acoes":
            cash[np.arange(60) % 12 == i % 12, i] = .03
        elif asset.annual_cash_yield:
            cash[np.arange(60) % 6 == 1, i] = .02
    return Dataset(assets, history, np.full(60, .002), dates, [], "real", SOURCE,
                   {"bonds": bonds, "returns_include_distributions": True, "source": SOURCE,
                    "forecast_target": {"id": "cash_payments_12m_v2", "event_basis": "payment_date"}}, cash)


def recommendation(income_mode="distributions_only", **plan_updates):
    settings = dict(age=30, retirement_age=31, end_age=34, initial_capital=100000,
                    monthly_contribution=100, target_income=100, annual_cost_percent=0,
                    simulations=500, seed=42, income_mode=income_mode)
    return RecommendationRequest(
        investor=Investor(risk_level=1, knowledge="Intermedi\u00e1rio", withdrawal_years=6,
                          high_liquidity=False),
        plan=Plan(**(settings | plan_updates)), data_mode="real")


def install_forecast(monkeypatch, dataset, events):
    def predict(features):
        features = np.asarray(features)
        events.append(("forecast", len(features)))
        return features[:, 0] * 1.5

    model = SimpleNamespace(predict=predict, predict_xgboost=predict, calibration=[])
    monkeypatch.setattr(engine, "get_dataset", lambda _: dataset)
    monkeypatch.setattr(engine, "get_forecast", lambda *args, **kwargs: (
        model, {"selectedMethod": "xgboost", "xgboostMae": .1, "baselineMae": .2}))
    monkeypatch.setattr(engine, "coverage_summary", lambda _: {"status": "synthetic_test"})


@pytest.mark.parametrize("income_mode", ["distributions_only", "total_return"])
def test_full_forecast_precedes_bounded_rollover_scenarios_and_search(monkeypatch, income_mode):
    dataset, events = expanded_dataset(), []
    install_forecast(monkeypatch, dataset, events)

    def record(name, original):
        def wrapped(assets, *args, **kwargs):
            events.append((name, len(assets)))
            assert events[0] == ("forecast", 100)
            assert len(assets) <= 24
            return original(assets, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(engine, "bounds_for", record("bounds", engine.bounds_for))
    monkeypatch.setattr(engine, "rollover_schedule", record("rollover", engine.rollover_schedule))
    original_priority = engine.asset_priority

    def rank(assets, *args):
        events.append(("rank", len(assets)))
        return original_priority(assets, *args)

    monkeypatch.setattr(engine, "asset_priority", rank)
    original_bootstrap, original_forecast = engine.bootstrap_paths, engine.forecast_paths

    def bootstrap(history, *args):
        events.append(("scenarios", history.shape[1]))
        assert history.shape[1] == 24
        return original_bootstrap(history, *args)

    def forecast_paths(history, cash_history, inflation, assets, *args):
        events.append(("scenarios", len(assets)))
        assert history.shape[1] == cash_history.shape[1] == len(assets) == 24
        for i, asset in enumerate(assets):
            column = next(j for j, original in enumerate(dataset.assets) if original.ticker == asset.ticker)
            np.testing.assert_array_equal(history[:, i], dataset.returns[:, column])
            np.testing.assert_array_equal(cash_history[:, i], dataset.cash_returns[:, column])
        return original_forecast(history, cash_history, inflation, assets, *args)

    monkeypatch.setattr(engine, "bootstrap_paths", bootstrap)
    monkeypatch.setattr(engine, "forecast_paths", forecast_paths)
    original_minimize = engine.minimize

    def minimize(problem, *args, **kwargs):
        assert problem.n_var == 24
        return original_minimize(problem, *args, **kwargs)

    monkeypatch.setattr(engine, "minimize", minimize)
    result = engine.compute(recommendation(income_mode), generations=2, population=8, search_count=4)
    assert events[:3] == [("forecast", 100), ("rank", 109), ("bounds", 24)]
    assert sum(name == "scenarios" for name, _ in events) == 2
    summary = result["construction"]["optimizationUniverse"]
    assert result["engineVersion"] == "RF-MOTOR-1.13"
    assert summary["selectedTickers"] == summary["admittedTickers"]
    assert summary["counts"]["eligibleAssets"] == 109
    assert summary["counts"]["optimizedAssets"] == 24
    assert summary["counts"]["deferredAssets"] == 85
    assert summary["eligibleAssets"] == 109
    assert summary["eligibleStocks"] == summary["forecastedStocks"] == 100
    assert summary["optimizedAssets"] == 24 and summary["optimizedStocks"] == 15
    assert summary["preservedAnchors"] == 9 and summary["deferredAssets"] == 85
    assert summary["usesSimulationOutcomes"] is False
    admitted = set(summary["admittedTickers"])
    assert "STOCK099" in admitted  # The highest forecast yield is beyond the old loader cap.
    assert {a.ticker for a in dataset.assets if a.asset_class != "acoes"} <= admitted
    assert len(result["allocation"]) == 24
    assert {row["ticker"] for row in result["allocation"]} == admitted
    deferred = {row["ticker"] for row in result["deferredFromOptimization"]}
    excluded = {row["ticker"] for row in result["excluded"]}
    assert excluded == {"RISK", "TERM", "COMPLEX", "NO-FEATURES"}
    assert len(deferred) == 85 and not deferred & (admitted | excluded)
    analysis = {row["ticker"]: row for row in result["assetAnalysis"]}
    assert len(analysis) == 113
    assert sum(row["eligible"] for row in analysis.values()) == 109
    assert sum(row["optimized"] for row in analysis.values()) == 24
    for row in result["deferredFromOptimization"]:
        assert row["code"] == "bounded_optimization_shortlist" and row["reasons"]
        assert analysis[row["ticker"]]["eligible"] is True
        assert analysis[row["ticker"]]["optimized"] is False
        assert analysis[row["ticker"]]["exclusionReasons"] == []
        assert analysis[row["ticker"]]["optimizationDeferralReasons"] == row["reasons"]
    for asset in dataset.assets[:100]:
        assert analysis[asset.ticker]["predictedCashPerShare"] == pytest.approx(asset.features[0] * 1.5)
    assert len(result["assetRanking"]) == 109
    assert all(row["historicalPaymentMonths"] for row in result["assetRanking"] if row["assetClass"] == "acoes")
    assert result["model"]["forecastedAssets"] == 100
    assert any(event["referenceTicker"] == "TD-00" and event["toTicker"] == "TD-01"
               for event in result["rollover"]["events"])
    for portfolio in [result, *result["portfolios"]]:
        coverage = portfolio["coverage"]
        assert coverage["catalogAssets"] == 113 and coverage["eligibleAssets"] == 109
        assert coverage["optimizedAssets"] == 24 and coverage["deferredAssets"] == 85
        allocation = portfolio["allocation"]
        assert {row["ticker"] for row in allocation} == admitted
        assert coverage["allocatedAssets"] == sum(row["weight"] > 1e-6 for row in allocation)
        assert sum(row["weight"] for row in allocation) == pytest.approx(1)
        assert all(row["weight"] >= 0 for row in allocation)
        assert all(row["weight"] <= .25 + 1e-7 for row in allocation if row["assetClass"] == "acoes")
        assert sum(row["weight"] for row in allocation if row["assetClass"] == "acoes") <= .4 + 1e-7
        assert sum(row["weight"] for row in allocation if row["assetClass"] == "liquidez") >= .15 - 1e-7
        assert portfolio["portfolio"]["annualVolatility"] <= .16 + 1e-7
    assert all({row["ticker"] for row in alternative["weights"]} == admitted for alternative in result["alternatives"])
    assert all(row["source"] == SOURCE for row in result["allocation"])
    assert result["financiallyValidated"] is False
    json.dumps(result, allow_nan=False)


def test_shortlist_combines_safety_income_calendar_and_sectors_with_local_positions():
    dataset = expanded_dataset(40, with_exclusions=False)
    assets, ranking = dataset.assets, []
    for i, asset in enumerate(assets):
        if i < 40:
            asset.features[6] = 1 if i == 39 else 2 if i == 38 else 0
        ranking.append({"ticker": asset.ticker, "assetClass": asset.asset_class, "safetyOrder": i + 1,
                        "cashYield12m": 20 if i == 37 else 1,
                        "historicalPaymentMonths": [6] if i == 39 else [1]})
    positions = engine.optimization_shortlist(assets, ranking)
    expected = {a.ticker for a in assets[:12] + assets[37:]}
    assert {assets[i].ticker for i in positions} == expected
    assert len(positions) == 24 and positions == sorted(positions)
    reversed_assets = list(reversed(assets))
    reversed_positions = engine.optimization_shortlist(reversed_assets, list(reversed(ranking)))
    assert {reversed_assets[i].ticker for i in reversed_positions} == expected


def test_admission_is_independent_of_seed_goal_and_any_simulation_outcome(monkeypatch):
    dataset, events, admissions = expanded_dataset(), [], []
    install_forecast(monkeypatch, dataset, events)

    class BeforeScenarios(Exception):
        pass

    def capture(assets, *args):
        admissions.append(tuple(asset.ticker for asset in assets))
        raise BeforeScenarios

    def forbidden(*args, **kwargs):
        pytest.fail("Admission cannot inspect Monte Carlo paths or optimization results")

    monkeypatch.setattr(engine, "rollover_schedule", capture)
    monkeypatch.setattr(engine, "bootstrap_paths", forbidden)
    monkeypatch.setattr(engine, "forecast_paths", forbidden)
    monkeypatch.setattr(engine, "minimize", forbidden)
    monkeypatch.setattr(engine, "simulate", forbidden)
    for seed, target, simulations in [(42, 100, 500), (42, 100, 500), (7, 1000000, 2000)]:
        with pytest.raises(BeforeScenarios):
            engine.compute(recommendation(seed=seed, target_income=target, simulations=simulations))
    assert admissions[0] == admissions[1] == admissions[2]
    assert len(admissions[0]) == 24
    assert sum(event == ("forecast", 100) for event in events) == 3


def test_twelve_anchors_preserved_and_excess_anchors_fail_explicitly():
    dataset = expanded_dataset(40, 12, with_exclusions=False)
    ranking = asset_priority(dataset.assets, dataset.returns, dataset.cash_returns,
                             dataset.dates, np.ones(52))
    selected = engine.optimization_shortlist(dataset.assets, ranking)
    assert len(selected) == 24 and set(range(40, 52)) <= set(selected)
    dataset.assets.append(dataset.assets[-1].model_copy(update={"ticker": "EXTRA-ANCHOR"}))
    with pytest.raises(ValueError, match="12 ancoras"):
        engine.optimization_shortlist(dataset.assets, ranking)


def test_small_universe_keeps_every_eligible_asset_and_existing_counts(monkeypatch):
    dataset, events = expanded_dataset(5), []
    install_forecast(monkeypatch, dataset, events)
    result = engine.compute(recommendation(), generations=2, population=8, search_count=4)
    assert result["coverage"]["catalogAssets"] == 18
    assert result["coverage"]["eligibleAssets"] == result["coverage"]["optimizedAssets"] == 14
    assert len(result["allocation"]) == len(result["assetRanking"]) == 14
    assert result["coverage"]["status"] == "limited_catalog"
    assert result["deferredFromOptimization"] == []
    for portfolio in result["portfolios"]:
        assert portfolio["coverage"]["eligibleAssets"] == portfolio["coverage"]["optimizedAssets"] == 14


def test_missing_payments_reports_full_eligibility_without_claiming_optimization(monkeypatch):
    dataset = expanded_dataset()
    dataset.cash_returns = None
    monkeypatch.setattr(engine, "get_dataset", lambda _: dataset)
    monkeypatch.setattr(engine, "coverage_summary", lambda _: {})

    def forbidden(*args, **kwargs):
        pytest.fail("Missing real payments must stop forecasting and portfolio optimization")

    monkeypatch.setattr(engine, "get_forecast", forbidden)
    monkeypatch.setattr(engine, "rollover_schedule", forbidden)
    result = engine.compute(recommendation())
    assert result["status"] == "income_data_unavailable"
    assert result["coverage"]["catalogAssets"] == 113
    assert result["coverage"]["eligibleAssets"] == 109
    assert result["coverage"]["optimizedAssets"] == 0
    assert len(result["assetAnalysis"]) == 113
    assert sum(row["eligible"] for row in result["assetAnalysis"]) == 109
    assert not any(row["optimized"] for row in result["assetAnalysis"])
    assert "simulation" not in result


@pytest.mark.parametrize("asset_count", [31, 109, MAX_REAL_ASSETS])
def test_real_loader_accepts_expanded_universe_without_truncating(tmp_path, asset_count):
    dataset = expanded_dataset(asset_count - 9, with_exclusions=False)
    metadata = dataset.metadata | {"assets": [asset.model_dump() for asset in dataset.assets]}
    (tmp_path / "assets.json").write_text(json.dumps(metadata), encoding="utf-8")
    with (tmp_path / "returns.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["date", "inflation", *[a.ticker for a in dataset.assets]])
        writer.writerows([day, inflation, *returns] for day, inflation, returns in
                         zip(dataset.dates, dataset.inflation, dataset.returns))
    loaded = real_dataset(tmp_path)
    assert len(loaded.assets) == asset_count
    assert loaded.returns.shape == (60, asset_count)
    assert loaded.assets[-1].ticker == dataset.assets[-1].ticker
    np.testing.assert_array_equal(loaded.returns, dataset.returns)


@pytest.mark.parametrize("duplicate", [False, True])
def test_real_loader_retains_finite_limit_and_unique_ticker_requirement(tmp_path, duplicate):
    dataset = expanded_dataset(1, with_exclusions=False)
    template = dataset.assets[0].model_dump()
    count = 31 if duplicate else MAX_REAL_ASSETS + 1
    assets = [template | {"ticker": "DUPLICATE" if duplicate else f"STOCK{i}"} for i in range(count)]
    (tmp_path / "assets.json").write_text(json.dumps({"assets": assets}), encoding="utf-8")
    with pytest.raises(ValueError, match="2 a 1000 ativos distintos"):
        real_dataset(tmp_path)
