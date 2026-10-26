from copy import deepcopy
from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import pytest

from . import engine, forecast
from .api import app
from .collect_income_data import verified_raw_prices
from .contracts import Investor, Plan, RecommendationRequest
from .datasets import demo_dataset
from .forecast_paths import forecast_paths
from .simulation import bootstrap_cash_paths, bootstrap_paths
from .test_forecast_policy import make_rows


def fixture():
    data = deepcopy(demo_dataset())
    data.mode = "real"
    data.cash_returns = np.tile([a.features[0] / a.price / 12 if a.features else a.annual_cash_yield / 12
                                 for a in data.assets], (len(data.dates), 1))
    return data


def test_forecast_replaces_cash_component_only_for_first_year():
    data = fixture()
    assets = data.assets
    forecasts = {a.ticker: a.features[0] * 2 for a in assets if a.features}
    returns, cash = forecast_paths(data.returns, data.cash_returns, data.inflation,
                                   assets, forecasts, None, "xgboost", 36, 20, 6, 42)
    original_returns = bootstrap_paths(data.returns, data.inflation, 36, 20, 6, 42)
    original_cash = bootstrap_cash_paths(data.cash_returns, data.inflation, 36, 20, 6, 42)
    np.testing.assert_allclose(returns - cash, original_returns - original_cash, atol=1e-15)
    np.testing.assert_array_equal(returns[:, 12:], original_returns[:, 12:])
    np.testing.assert_array_equal(cash[:, 12:], original_cash[:, 12:])
    assert np.all(cash[:, :12, 4] > original_cash[:, :12, 4])
    assert np.all(cash <= 1 + returns)
    repeated = forecast_paths(data.returns, data.cash_returns, data.inflation,
                              assets, forecasts, None, "xgboost", 36, 20, 6, 42)
    np.testing.assert_array_equal(returns, repeated[0])
    np.testing.assert_array_equal(cash, repeated[1])


def test_external_joint_indices_are_used_even_when_treasury_requires_extra_payment_padding():
    from .simulation import block_indices
    data = fixture()
    indices = block_indices(len(data.dates), 38, 20, 6, 42)[:, :36]
    returns, cash = forecast_paths(data.returns, data.cash_returns, data.inflation, data.assets,
        {}, None, 'baseline', 36, 20, 6, 42, scenario_indices=indices)
    np.testing.assert_array_equal(returns, (1 + data.returns[indices]) / (1 + data.inflation[indices, None]) - 1)
    np.testing.assert_array_equal(cash, data.cash_returns[indices] / (1 + data.inflation[indices, None]))
    with pytest.raises(ValueError, match='synchronized'):
        forecast_paths(data.returns, data.cash_returns, data.inflation, data.assets,
            {}, None, 'baseline', 36, 20, 6, 42, scenario_indices=indices[:, :35])


def test_price_doubling_does_not_double_a_fixed_per_share_cash_forecast():
    from .contracts import Asset
    from .linear_scenarios import build_stock_factors
    asset = Asset(ticker='TEST3', name='Synthetic fixed per-share cash', asset_class='acoes', risk_level=2,
        minimum_years=0, liquidity_days=2, price=100., as_of='2026-09-30',
        source='Synthetic arithmetic test, not market data.', features=[10, .5, 20, 15, .5, .3, 1])
    history, cash = np.zeros((12, 1)), np.zeros((12, 1))
    history[0], history[11], cash[11] = 1., .1, .1
    indices = np.tile(np.arange(12), (2, 2))
    paths, payouts = forecast_paths(history, cash, np.zeros(12), [asset], {'TEST3': 10.}, None,
        'xgboost', 24, 2, 3, 42, scenario_indices=indices)
    assert payouts[0, 11, 0] == pytest.approx(.05)
    plan = Plan(income_mode='distributions_only', age=30, retirement_age=31, end_age=32,
        initial_capital=100, monthly_contribution=0, target_income=1, annual_cost_percent=0,
        seed=42, simulations=500, block_months=3)
    factors = build_stock_factors(paths, payouts, plan)
    np.testing.assert_allclose(100 * factors['wealth_factors'][0, 1, :, 0], 210.)


def test_calibration_is_unchanged_when_test_labels_change():
    rows = make_rows()
    first, report = forecast.fit_forecast(rows)
    changed = [r | {"target": 1e6} if r["as_of"] >= report["testStart"] else r for r in rows]
    second, new_report = forecast.fit_forecast(changed)
    assert first.calibration == second.calibration
    assert report["uncertaintyBasis"] == "validation_residuals_only"
    assert new_report["xgboostMae"] != report["xgboostMae"]
    assert max(r["date"] for r in first.calibration) < report["testStart"]


def test_cash_prediction_uncertainty_changes_scenarios_without_test_data():
    data = fixture()
    forecasts = {a.ticker: a.features[0] for a in data.assets if a.features}
    model = SimpleNamespace(calibration=[{"date": f"2020-{m:02d}-01", "ticker": a.ticker,
                                         "xgboost": residual, "baseline": 0}
                                        for a in data.assets if a.features
                                        for m, residual in [(1, -0.5), (2, 0.5)]])
    _, cash = forecast_paths(data.returns, data.cash_returns, data.inflation,
                             data.assets, forecasts, model, "xgboost", 24, 100, 6, 42)
    assert np.std(cash[:, :12, 4].sum(axis=1)) > 0.01
    assert np.isfinite(cash).all() and (cash >= 0).all()


def test_xgboost_explicit_experiment_changes_nsga_objectives_and_income(monkeypatch):
    data = fixture()
    indices = [0, 4, 5]
    data.assets = [data.assets[i] for i in indices]
    data.cash_returns = data.cash_returns[:, indices]
    data.cash_returns[:, 0] = 0
    data.assets[0] = data.assets[0].model_copy(update={"annual_cash_yield": 0})
    data.returns = data.cash_returns.copy()
    monkeypatch.setattr(engine, "get_dataset", lambda mode: data)
    model = SimpleNamespace(method="baseline", calibration=[],
                            predict=lambda x: np.asarray(x)[:, 0],
                            predict_xgboost=lambda x: np.asarray(x)[:, 0] * 3)
    metrics = {"selectedMethod": "baseline", "mae": 1, "baselineMae": 1, "xgboostMae": 2}
    monkeypatch.setattr(engine, "get_forecast", lambda *args, **kwargs: (model, metrics))
    request = RecommendationRequest(investor=Investor(risk_level=2, knowledge="Avançado",
                                                     withdrawal_years=6, high_liquidity=False),
                                    plan=Plan(income_mode="distributions_only", age=30, retirement_age=31, end_age=33, initial_capital=100000,
                                              monthly_contribution=500, target_income=1000,
                                              annual_cost_percent=0, seed=42, simulations=500), data_mode="real")
    outcomes, objectives = [], []
    original_minimize = engine.minimize

    def record(problem, *args, **kwargs):
        w = np.ones((1, len(data.assets))) / len(data.assets)
        objectives.append(problem.evaluate(w, return_values_of=["F"])[0, 2])
        return original_minimize(problem, *args, **kwargs)

    monkeypatch.setattr(engine, "minimize", record)
    for policy in ("validation", "xgboost_experimental"):
        outcomes.append(engine.compute(request.model_copy(update={"forecast_policy": policy}),
                                       generations=3, population=8, search_count=8))
    assert objectives[1] < objectives[0]
    assert outcomes[0]["model"]["appliedMethod"] == "baseline"
    assert outcomes[1]["model"]["appliedMethod"] == "xgboost"
    assert outcomes[1]["model"]["appliedTestMae"] == 2
    assert outcomes[1]["model"]["usedForIncomeSimulation"] is True
    assert outcomes[0]["simulation"]["monthlyIncomeCapacity"] != outcomes[1]["simulation"]["monthlyIncomeCapacity"]
    for result in outcomes:
        assert result["incomeAnalysis"]["usesPrincipal"] is False
        assert sum(a["weight"] for a in result["allocation"]) == pytest.approx(1)
        assert sum(a["weight"] for a in result["allocation"] if a["assetClass"] == "acoes") <= 0.650001
    assert metrics == {"selectedMethod": "baseline", "mae": 1, "baselineMae": 1, "xgboostMae": 2}


def test_engine_rejects_direct_browser_cross_origin_even_with_valid_token(monkeypatch):
    monkeypatch.setenv("RECOMMENDER_TOKEN", "x" * 40)
    with TestClient(app) as client:
        assert client.get("/status").status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 40, "Origin": "https://untrusted.example"}
        response = client.get("/status", headers=headers)
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers
        preflight = client.options("/recommend", headers={"Origin": "https://untrusted.example",
                                                        "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 403


def test_unadjusted_close_must_be_explicit_not_inferred_from_close():
    rows = []
    for offset in range(61):
        year, month = divmod(2020 * 12 + offset, 12)
        day = date(year, month + 1, 15)
        rows.append({"date": datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp(),
                     "rawClose": 30, "close": 15, "adjustedClose": 15})
    payload = {"results": [{"data": {"historicalDataPrice": rows}}]}
    parsed = verified_raw_prices(json.dumps(payload), date(2025, 1, 31))
    assert list(parsed.values())[-1][2] == 30
    rows[0]["rawClose"] = None
    with pytest.raises(ValueError, match="rawClose"):
        verified_raw_prices(json.dumps(payload), date(2025, 1, 31))
