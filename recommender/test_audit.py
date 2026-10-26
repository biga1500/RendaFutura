import csv
from datetime import date
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pymoo.algorithms.moo.nsga2 import NSGA2
from xgboost import XGBRegressor

from . import engine, forecast
from .api import app, slots
from .contracts import RecommendationRequest
from .datasets import FEATURES, demo_dataset, get_dataset, real_dataset
from .simulation import bootstrap_paths, simulate
from .test_engine import investor, plan


TOKEN = "audit-only-token-no-production-secret-123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def real_files(tmp_path, monkeypatch):
    dataset = demo_dataset()
    current = date.today().replace(day=1)
    month_number = current.year * 12 + current.month - 1
    dates = []
    for offset in range(len(dataset.dates) - 1, -1, -1):
        year, month = divmod(month_number - offset, 12)
        dates.append(date(year, month + 1, 1).isoformat())
    remap = dict(zip(dataset.dates, dates))
    metadata = {"assets": [asset.model_dump() | {"as_of": dates[-1]} for asset in dataset.assets],
                "source": "Synthetic audit fixture; not production market data.",
                "returns_include_distributions": True}
    (tmp_path / "assets.json").write_text(json.dumps(metadata), encoding="utf-8")
    with (tmp_path / "returns.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["date", "inflation", *[a.ticker for a in dataset.assets]])
        writer.writerows([day, inflation, *returns] for day, inflation, returns
                         in zip(dates, dataset.inflation, dataset.returns))
    with (tmp_path / "observations.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["as_of", "published_at", "label_end", *FEATURES, "target"])
        writer.writerows([remap[row["as_of"]], remap[row["published_at"]], remap[row["label_end"]],
                          *row["features"], row["target"]] for row in dataset.observations)
    monkeypatch.setenv("RF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECOMMENDER_TOKEN", TOKEN)
    return tmp_path


def rewrite_csv(path, change):
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = reader.fieldnames
        rows = list(reader)
    change(rows)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def assert_portfolio(result, level):
    allocation = result["allocation"]
    assert sum(a["weight"] for a in allocation) == pytest.approx(1, abs=1e-6)
    assert all(a["weight"] >= -1e-8 for a in allocation)
    assert all(a["weight"] <= 0.25 + 1e-7 for a in allocation if a["assetClass"] == "acoes")
    assert sum(a["weight"] for a in allocation if a["assetClass"] == "acoes") <= [0, .4, .65][level] + 1e-7
    assert sum(a["weight"] for a in allocation if a["assetClass"] == "liquidez") >= [.25, .15, .1][level] - 1e-7
    assert result["portfolio"]["annualVolatility"] <= [.08, .16, .28][level] + 1e-7
    asset_classes = {a["ticker"]: a["assetClass"] for a in allocation}
    for alternative in result["alternatives"]:
        weights = alternative["weights"]
        assert sum(a["percent"] for a in weights) == pytest.approx(100, abs=1e-4)
        assert all(a["percent"] >= -1e-6 for a in weights)
        assert all(a["percent"] <= 25 + 1e-5 for a in weights if asset_classes[a["ticker"]] == "acoes")
        assert sum(a["percent"] for a in weights if asset_classes[a["ticker"]] == "acoes") <= [0, 40, 65][level] + 1e-5
        assert sum(a["percent"] for a in weights if asset_classes[a["ticker"]] == "liquidez") >= [25, 15, 10][level] - 1e-5
        assert alternative["annualVolatility"] <= [.08, .16, .28][level] + 1e-7
    simulation = result["simulation"]
    assert 0 <= simulation["samplingInterval95"][0] <= simulation["successFrequency"]
    assert simulation["successFrequency"] <= simulation["samplingInterval95"][1] <= 1
    for point in simulation["chart"]:
        assert 0 <= point["p10"] <= point["p50"] <= point["p90"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("level", [0, 1, 2], ids=["conservador", "moderado", "arrojado"])
def test_actual_algorithms_profiles_and_reproducibility(level, monkeypatch):
    optimization_runs, samples = [], []
    original_minimize = engine.minimize
    original_bootstrap = engine.bootstrap_paths

    def run_minimize(problem, algorithm, *args, **kwargs):
        assert isinstance(algorithm, NSGA2)
        assert problem.n_obj == 3 and problem.n_ieq_constr == 1
        result = original_minimize(problem, algorithm, *args, **kwargs)
        optimization_runs.append(result)
        return result

    def sample(*args):
        paths = original_bootstrap(*args)
        samples.append((args[-1], paths.shape))
        return paths

    monkeypatch.setattr(engine, "minimize", run_minimize)
    monkeypatch.setattr(engine, "bootstrap_paths", sample)
    request = RecommendationRequest(investor=investor(level), data_mode="demo",
                                    plan=plan(age=35, retirement_age=60, end_age=90,
                                              initial_capital=100000, monthly_contribution=1500,
                                              target_income=3000, simulations=500))
    first = engine.compute(request, generations=3, population=8, search_count=8)
    second = engine.compute(request, generations=3, population=8, search_count=8)
    assert first == second
    assert_portfolio(first, level)
    assert len(optimization_runs) == 2
    assert all(run.algorithm.evaluator.n_eval > 8 for run in optimization_runs)
    assert samples[:2] == [(42, (8, 660, len(first["allocation"]))),
                           (104771, (500, 660, len(first["allocation"])))]
    assert first["parameters"]["finalSeed"] != first["parameters"]["searchSeed"]
    if level == 0:
        assert first["model"] is None
        assert "XGBoost" not in first["methods"]
    else:
        assert first["model"]["trainRows"] > 0
        assert all(a["predictedCashPerShare"] is not None for a in first["allocation"] if a["assetClass"] == "acoes")
    print(json.dumps({"profile": level, "evaluations": optimization_runs[0].algorithm.evaluator.n_eval,
                      "success": first["simulation"]["successFrequency"], "volatility": first["portfolio"]["annualVolatility"],
                      "equity": sum(a["weight"] for a in first["allocation"] if a["assetClass"] == "acoes"),
                      "model": first["model"]}, allow_nan=False))


@pytest.mark.parametrize("level", [0, 1, 2])
def test_default_pipeline_via_authenticated_api(level, monkeypatch):
    monkeypatch.setenv("RECOMMENDER_TOKEN", TOKEN)
    runs = []
    original_minimize = engine.minimize

    def optimize(*args, **kwargs):
        result = original_minimize(*args, **kwargs)
        runs.append(result)
        return result

    monkeypatch.setattr(engine, "minimize", optimize)
    request = RecommendationRequest(investor=investor(level), data_mode="demo",
                                    plan=plan(age=35, retirement_age=60, end_age=90,
                                              initial_capital=100000, monthly_contribution=1500,
                                              target_income=8000, annual_cost_percent=1, simulations=1000))
    with TestClient(app) as client:
        response = client.post("/recommend", headers=HEADERS, json=request.model_dump())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["parameters"]["generations"] == 24
    assert result["parameters"]["population"] == 32
    assert result["parameters"]["searchScenarios"] == 96
    assert result["financiallyValidated"] is False
    assert len(runs) == 1 and runs[0].algorithm.evaluator.n_eval == 768
    assert_portfolio(result, level)
    print(json.dumps({"api_status": response.status_code, "profile": level, "nsga_evaluations": 768,
                      "generations": 24, "population": 32, "search_scenarios": 96, "final_scenarios": 1000,
                      "months": 660, "success": result["simulation"]["successFrequency"],
                      "volatility": result["portfolio"]["annualVolatility"],
                      "equity": sum(a["weight"] for a in result["allocation"] if a["assetClass"] == "acoes"),
                      "liquidity": sum(a["weight"] for a in result["allocation"] if a["assetClass"] == "liquidez")},
                     allow_nan=False))


@pytest.mark.parametrize("level", [0, 1, 2], ids=["conservador", "moderado", "arrojado"])
def test_impossible_goal_reports_failure_without_changing_profile(level):
    request = RecommendationRequest(investor=investor(level), data_mode="demo",
                                    plan=plan(age=35, retirement_age=60, end_age=90,
                                              initial_capital=0, monthly_contribution=0,
                                              target_income=1000000, simulations=500))
    result = engine.compute(request, generations=3, population=8, search_count=8)
    assert_portfolio(result, level)
    assert result["simulation"]["successFrequency"] == 0
    assert result["simulation"]["depletionFrequency"] == 1
    assert result["simulation"]["medianDeficit"] == 360000000
    assert any("Sem capital" in warning for warning in result["warnings"])
    assert any("80%" in warning for warning in result["warnings"])
    assert result["parameters"]["equityMaximum"] == [0, .4, .65][level]


@pytest.mark.parametrize("level", [0, 1, 2])
def test_volatility_infeasibility_rejected_without_relaxing_limits(level, monkeypatch):
    dataset = demo_dataset()
    dataset.assets = dataset.assets[:2]
    dataset.returns = np.tile(np.array([[-.5, -.5], [.5, .5]]), (100, 1))
    dataset.inflation = np.zeros(200)
    monkeypatch.setattr(engine, "get_dataset", lambda mode: dataset)
    with pytest.raises(ValueError, match="volatilidade"):
        engine.compute(RecommendationRequest(investor=investor(level), plan=plan(simulations=500),
                                             data_mode="demo"), generations=3, population=8, search_count=8)


def test_single_eligible_asset_preserves_profile_without_claiming_optimization(monkeypatch):
    dataset = demo_dataset()
    dataset.assets = [dataset.assets[0], dataset.assets[-1]]
    monkeypatch.setattr(engine, "get_dataset", lambda mode: dataset)
    result = engine.compute(RecommendationRequest(investor=investor(0), plan=plan(), data_mode="demo"))
    assert len(result["allocation"]) == 1
    assert result["allocation"][0]["weight"] == 1
    assert "NSGA-II" not in result["methods"]
    assert result["parameters"]["generations"] == 0
    assert result["model"] is None


def test_no_eligible_assets_rejected_without_changing_profile(monkeypatch):
    dataset = demo_dataset()
    dataset.assets = [dataset.assets[-1]]
    monkeypatch.setattr(engine, "get_dataset", lambda mode: dataset)
    with pytest.raises(ValueError, match="Poucos ativos"):
        engine.compute(RecommendationRequest(investor=investor(0), plan=plan(), data_mode="demo"))


@pytest.mark.parametrize("changes,excluded_class,reason", [
    ({"risk_level": 0}, "acoes", "Risco"),
    ({"withdrawal_years": 0}, "acoes", "Prazo"),
    ({"high_liquidity": True}, "renda_fixa", "Liquidez"),
])
def test_content_filter_each_suitability_dimension(changes, excluded_class, reason):
    dataset = demo_dataset()
    profile = investor(2).model_copy(update=changes)
    indices, excluded = engine.eligible_assets(dataset, profile)
    assert all(dataset.assets[i].asset_class != excluded_class for i in indices)
    assert any(reason in item for asset in excluded for item in asset["reasons"])


def test_content_filter_complexity_and_missing_cash_data():
    dataset = demo_dataset()
    dataset.assets[0] = dataset.assets[0].model_copy(update={"complex": True})
    dataset.assets[1] = dataset.assets[1].model_copy(update={"annual_cash_yield": None})
    dataset.assets[4] = dataset.assets[4].model_copy(update={"features": None})
    indices, excluded = engine.eligible_assets(dataset, investor(2))
    assert not {0, 1, 4} & set(indices)
    assert len(excluded) == 3
    advanced = investor(2).model_copy(update={"knowledge": "Avan\u00e7ado"})
    assert 0 in engine.eligible_assets(dataset, advanced)[0]


def test_xgboost_really_fits_purged_data_and_refits_only_known_labels(monkeypatch):
    dataset = demo_dataset()
    reference = dataset.dates[-25]
    dataset.dates = dataset.dates[:-24]
    rows = [row for row in dataset.observations if row["label_end"] <= reference]
    calls = []
    original_fit = XGBRegressor.fit

    def fit(self, x, y, *args, **kwargs):
        calls.append((np.array(x), np.array(y)))
        return original_fit(self, x, y, *args, **kwargs)

    monkeypatch.setattr(XGBRegressor, "fit", fit)
    monkeypatch.setattr(forecast, "get_dataset", lambda mode: dataset)
    model, report = forecast.get_forecast("real")
    train = [r for r in rows if r["label_end"] < report["validationStart"]]
    assert isinstance(model.regressor, XGBRegressor)
    assert model.regressor.get_booster().num_boosted_rounds() == 100
    assert len(calls) == 3
    features = np.array([r["features"] for r in train])
    transformed, scale = model.transform(features)
    np.testing.assert_array_equal(calls[0][0], transformed)
    np.testing.assert_array_equal(calls[1][1], (np.array([r["target"] for r in train]) - features[:, 0]) / scale)
    np.testing.assert_array_equal(calls[2][0], model.transform(np.array([r["features"] for r in rows]))[0])
    assert len(rows) < len(dataset.observations)
    assert report["maxTrainLabelEnd"] < report["validationStart"]
    assert report["maxValidationLabelEnd"] < report["testStart"]


def test_test_labels_do_not_select_xgboost_hyperparameters():
    rows = demo_dataset().observations
    _, report = forecast.fit_forecast(rows)
    poisoned = [row | {"target": row["target"] + 1000000} if row["as_of"] >= report["testStart"] else row
                for row in rows]
    _, changed = forecast.fit_forecast(poisoned)
    assert changed["parameters"] == report["parameters"]
    assert changed["mae"] > 999000


def test_too_short_training_history_is_rejected():
    with pytest.raises(ValueError, match="60 datas"):
        forecast.fit_forecast(demo_dataset().observations[:30])


def test_training_history_with_no_purged_validation_window_is_rejected():
    rows = demo_dataset().observations[:60]
    with pytest.raises(ValueError, match="janelas"):
        forecast.fit_forecast(rows)


def test_simulation_matches_closed_form_with_costs_and_no_double_cash():
    monthly_return = .01
    annual_cost = 2
    effective = (1 + monthly_return) * (1 - annual_cost / 100) ** (1 / 12)
    initial, contribution, income = 10000, 250, 500
    retirement = initial * effective ** 12 + contribution * (effective ** 12 - 1) / (effective - 1)
    final = retirement * effective ** 12 - income * (effective ** 12 - 1) / (effective - 1)
    result = simulate([1], np.full((5, 24, 1), monthly_return),
                      plan(initial_capital=initial, monthly_contribution=contribution,
                           target_income=income, annual_cost_percent=annual_cost), detailed=True)
    np.testing.assert_allclose(result["retirementWealth"], retirement)
    np.testing.assert_allclose(result["finalWealth"], final)
    assert result["successFrequency"] == 1


def test_annual_rebalance_uses_current_total_and_target_weights():
    paths = np.zeros((2, 24, 2))
    paths[:, 0, 0] = 1
    paths[:, 12, 1] = 1
    result = simulate([.5, .5], paths, plan(initial_capital=1000, monthly_contribution=0,
                                          target_income=1), detailed=True)
    np.testing.assert_allclose(result["retirementWealth"], 1500)
    np.testing.assert_allclose(result["finalWealth"], 2250 - 12)


def test_positive_unfunded_income_below_half_cent_is_still_a_failure():
    result = simulate([1], np.zeros((2, 24, 1)), plan(initial_capital=0, monthly_contribution=0,
                                                    target_income=.001), detailed=True)
    assert result["successFrequency"] == 0
    assert result["depletionFrequency"] == 1


@pytest.mark.parametrize("block", [3, 6, 12])
def test_bootstrap_keeps_joint_history_and_contiguous_blocks(block):
    history = np.column_stack([np.arange(60) / 1000, np.arange(60) / 500])
    inflation = np.arange(60) / 10000
    paths = bootstrap_paths(history, inflation, 25, 4, block, 42)
    other = bootstrap_paths(history, inflation, 25, 4, block, 43)
    assert not np.array_equal(paths, other)
    expected = (1 + history) / (1 + inflation[:, None]) - 1
    for path in paths:
        matched = np.argmin(np.abs(expected[:, 0, None] - path[:, 0]), axis=0)
        np.testing.assert_allclose(path, expected[matched])
        for start in range(0, 25, block):
            np.testing.assert_array_equal(np.diff(matched[start:start + block]), 1)


def test_real_loader_executes_pipeline_on_explicitly_synthetic_files(real_files):
    dataset = real_dataset(real_files)
    assert dataset.mode == "real" and "Synthetic" in dataset.source
    assert dataset.returns.shape == (200, 10)
    assert len(dataset.observations) == 984
    request = RecommendationRequest(investor=investor(2), plan=plan(simulations=500), data_mode="real")
    result = engine.compute(request, generations=3, population=8, search_count=8)
    assert_portfolio(result, 2)
    assert result["model"]["trainRows"] > 0
    assert result["financiallyValidated"] is False


def test_real_dataset_does_not_survive_removed_configuration(real_files, monkeypatch):
    assert get_dataset("real").mode == "real"
    monkeypatch.delenv("RF_DATA_DIR")
    with pytest.raises(ValueError, match="configurada"):
        get_dataset("real")


def test_real_dataset_reloads_changed_files(real_files):
    first = get_dataset("real")
    rewrite_csv(real_files / "returns.csv", lambda rows: rows[-1].update({"CAIXA-A": "0.123"}))
    second = get_dataset("real")
    assert first.returns[-1, 0] != second.returns[-1, 0]
    assert second.returns[-1, 0] == .123


def test_real_forecast_reloads_changed_labels(real_files):
    first, _ = forecast.get_forecast("real")
    prediction = first.predict(np.array([get_dataset("real").assets[4].features]))
    rewrite_csv(real_files / "observations.csv", lambda rows: [row.update({"target": "100"}) for row in rows])
    second, _ = forecast.get_forecast("real")
    assert second.predict(np.array([get_dataset("real").assets[4].features]))[0] > prediction[0] + 50


@pytest.mark.parametrize("column,value", [("CAIXA-A", "nan"), ("CAIXA-A", "inf"),
                                          ("CAIXA-A", "-1"), ("inflation", "nan"), ("inflation", "-1")])
def test_bad_returns_rejected(real_files, column, value):
    rewrite_csv(real_files / "returns.csv", lambda rows: rows[0].update({column: value}))
    with pytest.raises(ValueError):
        real_dataset(real_files)


@pytest.mark.parametrize("change", ["duplicate", "gap", "future", "stale"])
def test_bad_monthly_dates_rejected(real_files, change):
    def mutate(rows):
        if change == "duplicate":
            rows[1]["date"] = rows[0]["date"]
        elif change == "gap":
            rows.pop(30)
        else:
            for row in rows:
                old = date.fromisoformat(row["date"])
                row["date"] = old.replace(year=old.year + (5 if change == "future" else -5)).isoformat()
    rewrite_csv(real_files / "returns.csv", mutate)
    with pytest.raises(ValueError):
        real_dataset(real_files)


@pytest.mark.parametrize("column,value", [("published_at", "2099-01-01"), ("label_end", "2099-01-01"),
                                          ("target", "nan"), ("previous_cash", "-1"),
                                          ("as_of", "2015-01-01")])
def test_bad_or_leaking_training_rows_rejected(real_files, column, value):
    rewrite_csv(real_files / "observations.csv", lambda rows: rows[0].update({column: value}))
    with pytest.raises(ValueError):
        real_dataset(real_files)


@pytest.mark.parametrize("change", ["stale_asset", "future_asset", "incomplete_features", "nan_features",
                                    "duplicate_ticker", "missing_total_returns", "missing_source"])
def test_invalid_asset_metadata_rejected(real_files, change):
    path = real_files / "assets.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if change == "stale_asset":
        metadata["assets"][0]["as_of"] = "2010-01-01"
    elif change == "future_asset":
        metadata["assets"][0]["as_of"] = "2099-01-01"
    elif change == "incomplete_features":
        metadata["assets"][4]["features"] = [1, 2]
    elif change == "nan_features":
        metadata["assets"][4]["features"][0] = float("nan")
    elif change == "duplicate_ticker":
        metadata["assets"][1]["ticker"] = metadata["assets"][0]["ticker"]
    elif change == "missing_total_returns":
        metadata["returns_include_distributions"] = False
    else:
        metadata["source"] = ""
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError):
        real_dataset(real_files)


def test_missing_training_examples_rejected_for_stocks(real_files):
    (real_files / "observations.csv").unlink()
    request = RecommendationRequest(investor=investor(2), plan=plan(), data_mode="real")
    with pytest.raises(ValueError, match="60 datas"):
        engine.compute(request)


@pytest.mark.parametrize("change", ["missing", "duplicate", "noncanonical_date"])
def test_bad_csv_headers_or_date_format_rejected(real_files, change):
    path = real_files / "returns.csv"
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    if change == "missing":
        rows[0][1] = "not_inflation"
    elif change == "duplicate":
        rows[0][-1] = rows[0][-2]
    else:
        rows[1][0] = rows[1][0].replace("-", "")
    with path.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows(rows)
    with pytest.raises(ValueError):
        real_dataset(real_files)


@pytest.mark.parametrize("metadata", [[], {"assets": None}, {"assets": [{}]}])
def test_bad_metadata_becomes_422_not_internal_error(real_files, metadata):
    (real_files / "assets.json").write_text(json.dumps(metadata), encoding="utf-8")
    with TestClient(app) as client:
        response = client.post("/recommend", headers=HEADERS,
                               json=RecommendationRequest(investor=investor(), plan=plan(), data_mode="real").model_dump())
    assert response.status_code == 422


def test_truncated_csv_becomes_422_not_internal_error(real_files):
    with (real_files / "returns.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    rows[1] = rows[1][:-1]
    with (real_files / "returns.csv").open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows(rows)
    with TestClient(app) as client:
        response = client.post("/recommend", headers=HEADERS,
                               json=RecommendationRequest(investor=investor(), plan=plan(), data_mode="real").model_dump())
    assert response.status_code == 422


def test_missing_real_data_never_falls_back_to_demo(monkeypatch):
    monkeypatch.setenv("RECOMMENDER_TOKEN", TOKEN)
    monkeypatch.delenv("RF_DATA_DIR", raising=False)
    with TestClient(app) as client:
        response = client.post("/recommend", headers=HEADERS,
                               json=RecommendationRequest(investor=investor(), plan=plan(), data_mode="real").model_dump())
    assert response.status_code == 422


def test_status_tracks_invalidated_real_base(real_files):
    with TestClient(app) as client:
        assert client.get("/status", headers=HEADERS).json()["realDataAvailable"] is True
        (real_files / "assets.json").write_text("[]", encoding="utf-8")
        assert client.get("/status", headers=HEADERS).json()["realDataAvailable"] is False


def test_failed_request_releases_engine_slot(real_files):
    payload = RecommendationRequest(investor=investor(), plan=plan(), data_mode="real").model_dump()
    (real_files / "assets.json").write_text("[]", encoding="utf-8")
    with TestClient(app) as client:
        assert client.post("/recommend", headers=HEADERS, json=payload).status_code == 422
    assert slots.acquire(blocking=False)
    slots.release()


def test_busy_engine_rejects_request_and_recovers(monkeypatch):
    monkeypatch.setenv("RECOMMENDER_TOKEN", TOKEN)
    payload = RecommendationRequest(investor=investor(), plan=plan(), data_mode="demo").model_dump()
    assert slots.acquire(blocking=False)
    try:
        with TestClient(app) as client:
            assert client.post("/recommend", headers=HEADERS, json=payload).status_code == 429
    finally:
        slots.release()
    assert slots.acquire(blocking=False)
    slots.release()
