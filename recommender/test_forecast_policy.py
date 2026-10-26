from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest
from xgboost import XGBRegressor

from . import engine, forecast
from .contracts import Investor, Plan, RecommendationRequest
from .datasets import demo_dataset


def month_date(offset):
    year, month = divmod(2010 * 12 + offset, 12)
    return date(year, month + 1, 1).isoformat()


def make_rows(residual=0.25):
    rows = []
    # Asset-major order ensures a chronological split cannot use row positions.
    for sector, cash_scale in enumerate((2.0, 7.0, 40.0)):
        for month in range(160):
            cash = cash_scale * (1 + (month % 5) / 10)
            rows.append({
                "as_of": month_date(month),
                "published_at": month_date(month),
                "label_end": month_date(month + 12),
                "features": [cash, (month + 1) / 161, cash * 2, cash * 1.5,
                             0.4, 0.5, float(sector)],
                "target": cash * (1 + residual),
                "baseline": cash,
            })
    return rows


def validation_rows(rows, report):
    return [row for row in rows if report["validationStart"] <= row["as_of"]
            and row["label_end"] < report["testStart"]]


def use_baseline_validation_targets(rows, report):
    validation_dates = {row["as_of"] for row in validation_rows(rows, report)}
    return [row | {"target": row["baseline"]} if row["as_of"] in validation_dates
            else row for row in rows]


def errors(predictions, targets):
    residuals = np.asarray(predictions) - np.asarray(targets)
    return float(np.mean(np.abs(residuals))), float(np.sqrt(np.mean(residuals ** 2)))


@pytest.fixture(scope="module")
def reference_report():
    return forecast.fit_forecast(make_rows())[1]


@pytest.fixture
def forecast_trace(monkeypatch):
    events = []
    original_fit = forecast.CashForecast.fit
    original_predict = forecast.CashForecast.predict

    def fit(self, features, targets):
        events.append({"kind": "fit", "model": self,
                       "features": np.array(features, copy=True),
                       "targets": np.array(targets, copy=True)})
        return original_fit(self, features, targets)

    def predict(self, features):
        predictions = original_predict(self, features)
        events.append({"kind": "predict", "model": self,
                       "features": np.array(features, copy=True),
                       "predictions": np.array(predictions, copy=True)})
        return predictions

    monkeypatch.setattr(forecast.CashForecast, "fit", fit)
    monkeypatch.setattr(forecast.CashForecast, "predict", predict)
    return events


@pytest.mark.parametrize("residual", [0.0, 0.25], ids=["baseline", "xgboost"])
def test_fit_and_batch_predictions_are_deterministic(residual):
    rows = make_rows(residual)
    original_rows = deepcopy(rows)
    first, report = forecast.fit_forecast(rows)
    second, repeated_report = forecast.fit_forecast(rows)
    features = np.array([row["features"] for row in rows[::37]])
    original_features = features.copy()

    assert report == repeated_report
    assert first.method == second.method == report["selectedMethod"]
    assert report["selectedMethod"] == ("xgboost" if residual else "baseline")
    predictions = first.predict(features)
    assert predictions.shape == (len(features),)
    assert np.isfinite(predictions).all()
    assert np.all(predictions >= 0)
    np.testing.assert_array_equal(predictions, second.predict(features))
    np.testing.assert_array_equal(predictions, first.predict(features.tolist()))
    np.testing.assert_array_equal(predictions, [first.predict([row])[0] for row in features])
    np.testing.assert_array_equal(features, original_features)
    assert rows == original_rows


def test_validation_rejection_uses_previous_cash_even_when_xgboost_wins_test(reference_report):
    rows = use_baseline_validation_targets(make_rows(), reference_report)
    model, report = forecast.fit_forecast(rows)

    assert report["validationMae"] > report["validationBaselineMae"]
    assert report["xgboostMae"] < report["baselineMae"]
    assert report["selectedMethod"] == model.method == "baseline"
    assert report["mae"] == report["baselineMae"]
    assert report["beatsBaseline"] is False
    features = np.array([[cash, 0.9, 19, 11, 0.1, 0.2, 2]
                         for cash in (0.0, 0.001, 3.7, 1250.0)])
    np.testing.assert_array_equal(model.predict(features), features[:, 0])


def test_validation_tie_keeps_baseline():
    model, report = forecast.fit_forecast(make_rows(residual=0))
    assert report["validationMae"] == report["validationBaselineMae"]
    assert report["selectedMethod"] == model.method == "baseline"
    assert report["beatsBaseline"] is False


def test_per_company_errors_use_the_same_temporal_test_rows():
    rows = [row | {"ticker": f"STOCK-{int(row['features'][6])}"} for row in make_rows()]
    _, report = forecast.fit_forecast(rows)
    assert sum(group["rows"] for group in report["byTicker"].values()) == report["testRows"]
    assert sum(group["rows"] * group["xgboostMae"] for group in report["byTicker"].values()) / report["testRows"] == pytest.approx(report["xgboostMae"])
    assert sum(group["rows"] * group["baselineMae"] for group in report["byTicker"].values()) / report["testRows"] == pytest.approx(report["baselineMae"])


@pytest.mark.parametrize("selected_method", ["xgboost", "baseline"])
def test_test_targets_cannot_change_method_or_validation_scores(selected_method, reference_report):
    rows = make_rows()
    if selected_method == "baseline":
        rows = use_baseline_validation_targets(rows, reference_report)
    baseline_test = [row | {"target": row["baseline"]}
                     if row["as_of"] >= reference_report["testStart"] else row for row in rows]
    xgboost_test = [row | {"target": row["baseline"] * 1.25}
                    if row["as_of"] >= reference_report["testStart"] else row for row in rows]
    first_model, first = forecast.fit_forecast(baseline_test)
    second_model, second = forecast.fit_forecast(xgboost_test)

    assert first["xgboostMae"] > first["baselineMae"]
    assert second["xgboostMae"] < second["baselineMae"]
    assert first_model.method == second_model.method == selected_method
    for key in ("selectedMethod", "parameters", "validationMae", "validationBaselineMae",
                "validationStart", "testStart", "trainRows", "validationRows", "testRows"):
        assert first[key] == second[key], key
    assert first["selectedMethod"] == selected_method
    if selected_method == "xgboost":
        assert first["beatsBaseline"] is False
        assert second["beatsBaseline"] is True


@pytest.mark.parametrize("selected_method", ["xgboost", "baseline"])
def test_report_scores_candidates_and_baseline_on_same_test_rows_before_refit(
        selected_method, reference_report, forecast_trace):
    rows = make_rows()
    if selected_method == "baseline":
        rows = use_baseline_validation_targets(rows, reference_report)
    rows = [row | {"target": row["baseline"] * (0.5 + (i % 4) * 0.5)}
            if row["as_of"] >= reference_report["testStart"] else row
            for i, row in enumerate(rows)]
    model, report = forecast.fit_forecast(rows)
    test = [row for row in rows if row["as_of"] >= report["testStart"]]
    features = np.array([row["features"] for row in test])
    targets = np.array([row["target"] for row in test])
    baseline = np.array([row["baseline"] for row in test])
    predictions = [(i, event) for i, event in enumerate(forecast_trace)
                   if event["kind"] == "predict" and event["model"] is model
                   and np.array_equal(event["features"], features)]
    assert len(predictions) == 1
    test_event_index, test_event = predictions[0]
    xgboost_predictions = test_event["predictions"]
    xgboost_mae, xgboost_rmse = errors(xgboost_predictions, targets)
    baseline_mae, baseline_rmse = errors(baseline, targets)
    selected = xgboost_predictions if selected_method == "xgboost" else baseline
    selected_mae, selected_rmse = errors(selected, targets)

    assert report["selectedMethod"] == selected_method
    assert report["testRows"] == len(test)
    assert report["xgboostMae"] == pytest.approx(xgboost_mae)
    assert report["xgboostRmse"] == pytest.approx(xgboost_rmse)
    assert report["baselineMae"] == pytest.approx(baseline_mae)
    assert report["mae"] == pytest.approx(selected_mae)
    assert report["rmse"] == pytest.approx(selected_rmse)
    assert report["beatsBaseline"] == (selected_mae < baseline_mae)
    if selected_method == "baseline":
        assert report["rmse"] == pytest.approx(baseline_rmse)

    validation = validation_rows(rows, report)
    validation_features = np.array([row["features"] for row in validation])
    validation_targets = np.array([row["target"] for row in validation])
    validation_predictions = [event["predictions"] for event in forecast_trace
                              if event["kind"] == "predict"
                              and np.array_equal(event["features"], validation_features)]
    assert validation_predictions
    assert report["validationMae"] == pytest.approx(
        min(errors(prediction, validation_targets)[0] for prediction in validation_predictions))
    assert report["validationBaselineMae"] == pytest.approx(
        errors([row["baseline"] for row in validation], validation_targets)[0])
    refit_indices = [i for i, event in enumerate(forecast_trace) if event["kind"] == "fit"
                     and event["model"] is model and len(event["targets"]) == len(rows)]
    assert len(refit_indices) == 1
    assert test_event_index < refit_indices[0]


def test_temporal_folds_purge_overlapping_labels_before_model_fitting(forecast_trace):
    rows = make_rows()
    _, report = forecast.fit_forecast(rows)
    train = [row for row in rows if row["label_end"] < report["validationStart"]]
    validation = validation_rows(rows, report)
    test = [row for row in rows if row["as_of"] >= report["testStart"]]
    fits = [event for event in forecast_trace if event["kind"] == "fit"]
    assert len(fits) >= 2
    expected_train = {tuple(row["features"]): row["target"] for row in train}
    for event in fits[:-1]:
        assert len(event["targets"]) == len(train)
        assert {tuple(features) for features in event["features"]} == set(expected_train)
        np.testing.assert_array_equal(event["targets"],
                                      [expected_train[tuple(features)] for features in event["features"]])
    np.testing.assert_array_equal(fits[-1]["features"], [row["features"] for row in rows])
    np.testing.assert_array_equal(fits[-1]["targets"], [row["target"] for row in rows])
    assert report["trainRows"] == len(train)
    assert report["validationRows"] == len(validation)
    assert report["testRows"] == len(test)
    assert report["maxTrainLabelEnd"] == max(row["label_end"] for row in train)
    assert report["maxValidationLabelEnd"] == max(row["label_end"] for row in validation)
    assert report["maxTrainLabelEnd"] < report["validationStart"]
    assert report["maxValidationLabelEnd"] < report["testStart"]
    assert any(row["label_end"] == report["validationStart"] for row in rows)
    assert any(row["as_of"] >= report["validationStart"]
               and row["label_end"] == report["testStart"] for row in rows)
    assert len(train) + len(validation) + len(test) < len(rows)


def test_xgboost_learns_residuals_normalized_by_previous_cash(monkeypatch):
    rows = make_rows()
    rows = [row | {"target": row["baseline"] * (1.1 + (i % 7) / 100)}
            for i, row in enumerate(rows)]
    by_identity = {(row["features"][1], row["features"][6]): row for row in rows}
    calls = []
    original_fit = XGBRegressor.fit

    def fit(self, features, targets, *args, **kwargs):
        calls.append((np.array(features, copy=True), np.array(targets, copy=True)))
        return original_fit(self, features, targets, *args, **kwargs)

    monkeypatch.setattr(XGBRegressor, "fit", fit)
    model, _ = forecast.fit_forecast(rows)
    assert isinstance(model.regressor, XGBRegressor)
    assert calls
    for features, targets in calls:
        originals = [by_identity[(values[1], values[6])] for values in features]
        cash = np.array([row["baseline"] for row in originals])
        expected = (np.array([row["target"] for row in originals]) - cash) / cash
        np.testing.assert_allclose(targets, expected)


@pytest.mark.parametrize("residual", [-2.0, 0.25])
def test_xgboost_prediction_restores_cash_units_and_is_finite_at_zero(residual, monkeypatch):
    model, report = forecast.fit_forecast(make_rows())
    assert report["selectedMethod"] == "xgboost"
    features = np.array([[cash, 0.8, cash * 2, cash * 1.5, 0.4, 0.5, 1]
                         for cash in (0.0, 1e-12, 0.2, 2.0, 200.0)])
    monkeypatch.setattr(model.regressor, "predict", lambda values: np.full(len(values), residual))
    predictions = model.predict(features)

    assert predictions.shape == (len(features),)
    assert np.isfinite(predictions).all()
    assert np.all(predictions >= 0)
    np.testing.assert_allclose(predictions[-2:], np.maximum(0, features[-2:, 0] * (1 + residual)))


@pytest.mark.parametrize("reference_source", ["returns", "asset"])
def test_dataset_refit_uses_only_labels_known_at_earliest_reference(reference_source, forecast_trace):
    dataset = demo_dataset()
    reference = dataset.dates[-25]
    if reference_source == "returns":
        dataset.dates = dataset.dates[:-24]
    else:
        dataset.assets[0] = dataset.assets[0].model_copy(update={"as_of": reference})
    known = [row for row in dataset.observations if row["label_end"] <= reference]
    assert len(known) < len(dataset.observations)
    assert any(row["label_end"] == reference for row in known)

    _, report = forecast.dataset_forecast(dataset)
    fits = [event for event in forecast_trace if event["kind"] == "fit"]
    np.testing.assert_array_equal(fits[-1]["features"], [row["features"] for row in known])
    np.testing.assert_array_equal(fits[-1]["targets"], [row["target"] for row in known])
    assert report["testRows"] == sum(row["as_of"] >= report["testStart"] for row in known)
    assert report["maxTrainLabelEnd"] < report["validationStart"]
    assert report["maxValidationLabelEnd"] < report["testStart"]


@pytest.mark.parametrize("selected_method,beats_baseline", [
    ("xgboost", False), ("xgboost", True), ("baseline", False),
], ids=["xgboost-loses-test", "xgboost-wins-test", "selected-baseline"])
@pytest.mark.parametrize("mode", ["demo", "real"])
def test_engine_applies_fixed_policy_without_selecting_on_test_scores(
        selected_method, beats_baseline, mode, monkeypatch):
    dataset = demo_dataset()
    dataset.mode = mode
    baseline_mae, xgboost_mae = 3.0, 1.0 if beats_baseline else 7.0
    selected_mae = xgboost_mae if selected_method == "xgboost" else baseline_mae
    metrics = {"selectedMethod": selected_method, "beatsBaseline": beats_baseline,
               "mae": selected_mae, "rmse": selected_mae + 1,
               "xgboostMae": xgboost_mae, "xgboostRmse": xgboost_mae + 1,
               "baselineMae": baseline_mae, "validationMae": 1.0,
               "validationBaselineMae": 2.0, "testRows": 18}
    original_metrics = deepcopy(metrics)
    prediction_calls = []

    def predict(features):
        prediction_calls.append(np.array(features, copy=True))
        return np.asarray(features)[:, 0] * 1.25

    model = SimpleNamespace(predict=predict, method=selected_method)
    monkeypatch.setattr(engine, "get_dataset", lambda mode: dataset)
    monkeypatch.setattr(engine, "get_forecast", lambda mode, *, dataset: (model, metrics))
    apply_xgboost = selected_method == "xgboost"
    expected_cash = {
        asset.ticker: (asset.features[0] * (1.25 if apply_xgboost else 1)) / asset.price
        if asset.asset_class == "acoes" else asset.annual_cash_yield
        for asset in dataset.assets
    }
    objectives = []
    original_minimize = engine.minimize

    def minimize(problem, *args, **kwargs):
        weights = np.full((1, len(dataset.assets)), 1 / len(dataset.assets))
        objective_values = problem.evaluate(weights, return_values_of=["F"])
        objectives.append(float(objective_values[0, 2]))
        assert objective_values[0, 2] == pytest.approx(
            -sum(expected_cash.values()) / len(dataset.assets))
        return original_minimize(problem, *args, **kwargs)

    monkeypatch.setattr(engine, "minimize", minimize)
    request = RecommendationRequest(
        investor=Investor(risk_level=2, knowledge="Intermedi\u00e1rio", withdrawal_years=6,
                          high_liquidity=False),
        plan=Plan(age=30, retirement_age=31, end_age=32, initial_capital=10000,
                  monthly_contribution=100, target_income=100, annual_cost_percent=0,
                  seed=42, simulations=500),
        data_mode=mode,
    )
    result = engine.compute(request, generations=2, population=8, search_count=4)

    assert objectives
    assert bool(prediction_calls) == apply_xgboost
    assert metrics == original_metrics
    report = result["model"]
    for key, value in original_metrics.items():
        assert report[key] == value, key
    assert report["appliedMethod"] == ("xgboost" if apply_xgboost else "baseline")
    assert report["appliedTestMae"] == (selected_mae if apply_xgboost else baseline_mae)
    assets_by_ticker = {asset.ticker: asset for asset in dataset.assets}
    for allocation in result["allocation"]:
        asset = assets_by_ticker[allocation["ticker"]]
        assert allocation["cashYield12m"] == pytest.approx(expected_cash[asset.ticker])
        if asset.asset_class == "acoes":
            assert allocation["predictedCashPerShare"] == pytest.approx(
                expected_cash[asset.ticker] * asset.price)
    assert result["portfolio"]["cashYield12m"] == pytest.approx(
        sum(item["weight"] * expected_cash[item["ticker"]] for item in result["allocation"]))
    for alternative in result["alternatives"]:
        assert alternative["cashYield12m"] == pytest.approx(
            sum(item["percent"] / 100 * expected_cash[item["ticker"]]
                for item in alternative["weights"]))
