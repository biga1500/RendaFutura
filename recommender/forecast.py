from functools import lru_cache

import numpy as np
from xgboost import XGBRegressor

from .datasets import FEATURES, get_dataset


class CashForecast:
    def __init__(self, regressor, method="xgboost"):
        self.regressor = regressor
        self.method = method

    @staticmethod
    def _features(values):
        values = np.asarray(values, dtype=float)
        if values.ndim != 2 or values.shape[1] != len(FEATURES):
            raise ValueError("features must be a finite batch with seven columns and nonnegative previous cash.")
        missing = np.isnan(values)
        allowed = np.zeros(values.shape, dtype=bool)
        allowed[:, 3] = values[:, 6] == 2
        if (np.isinf(values).any() or np.any(missing & ~allowed) or np.any(values[:, 0] < 0)):
            raise ValueError("features must be finite; only financial-sector FCF may be missing.")
        return values

    @staticmethod
    def transform(values):
        values = CashForecast._features(values)
        scale = np.maximum(values[:, 0], 1.0)
        transformed = values.copy()
        transformed[:, [0, 2, 3]] /= scale[:, None]
        return transformed, scale

    def fit(self, features, targets):
        features = np.asarray(features, dtype=float)
        transformed, scale = self.transform(features)
        targets = np.asarray(targets, dtype=float)
        if (targets.shape != (len(features),) or not np.isfinite(targets).all()
                or np.any(targets < 0)):
            raise ValueError("targets must be a finite nonnegative vector matching the feature rows.")
        self.regressor.fit(transformed, (targets - features[:, 0]) / scale)
        return self

    def predict(self, features):
        features = self._features(features)
        if self.method == "baseline":
            return np.maximum(0, features[:, 0])
        return self.predict_xgboost(features)

    def predict_xgboost(self, features):
        features = np.asarray(features, dtype=float)
        transformed, scale = self.transform(features)
        return np.maximum(0, features[:, 0] + self.regressor.predict(transformed) * scale)


def fit_forecast(rows: list[dict]):
    dates = sorted({row["as_of"] for row in rows})
    if len(dates) < 60:
        raise ValueError("O XGBoost exige pelo menos 60 datas de exemplos históricos.")
    validation_start = dates[int(len(dates) * 0.65)]
    test_start = dates[int(len(dates) * 0.85)]
    train = [r for r in rows if r["label_end"] < validation_start]
    validation = [r for r in rows if validation_start <= r["as_of"] and r["label_end"] < test_start]
    test = [r for r in rows if r["as_of"] >= test_start]
    if min(len(train), len(validation), len(test)) < 12:
        raise ValueError("Poucos exemplos após remover janelas que invadem validação ou teste.")

    def arrays(data):
        return np.asarray([r["features"] for r in data], dtype=float), np.asarray([r["target"] for r in data])

    x_train, y_train = arrays(train)
    x_val, y_val = arrays(validation)
    candidates = []
    for depth in (2, 3):
        model = CashForecast(XGBRegressor(objective="reg:squarederror", n_estimators=100, max_depth=depth,
                             learning_rate=0.05, reg_lambda=2, random_state=41, n_jobs=1))
        model.fit(x_train, y_train)
        val_mae = float(np.mean(np.abs(np.maximum(0, model.predict(x_val)) - y_val)))
        candidates.append((val_mae, model, model.predict(x_val)))
    validation_mae, model, validation_predictions = min(candidates, key=lambda item: item[0])
    validation_baseline_mae = float(np.mean(np.abs(x_val[:, 0] - y_val)))
    selected_method = "xgboost" if validation_mae < validation_baseline_mae else "baseline"
    x_test, y_test = arrays(test)
    xgboost_predictions = model.predict(x_test)
    baseline = np.maximum(0, x_test[:, 0])
    predictions = xgboost_predictions if selected_method == "xgboost" else baseline
    mae = float(np.mean(np.abs(predictions - y_test)))
    baseline_mae = float(np.mean(np.abs(baseline - y_test)))
    metrics = {"mae": mae, "rmse": float(np.sqrt(np.mean((predictions - y_test) ** 2))),
               "baselineMae": baseline_mae, "beatsBaseline": mae < baseline_mae,
               "xgboostMae": float(np.mean(np.abs(xgboost_predictions - y_test))),
               "xgboostRmse": float(np.sqrt(np.mean((xgboost_predictions - y_test) ** 2))),
               "selectedMethod": selected_method,
               "validationMae": validation_mae, "validationBaselineMae": validation_baseline_mae,
               "selectionBasis": "validation_only", "targetTransform": "scaled_residual_previous_cash",
               "trainRows": len(train), "validationRows": len(validation), "testRows": len(test),
               "validationStart": validation_start, "testStart": test_start,
               "maxTrainLabelEnd": max(r["label_end"] for r in train),
               "maxValidationLabelEnd": max(r["label_end"] for r in validation),
               "parameters": model.regressor.get_params()["max_depth"],
               "financiallyValidated": False}
    metrics["byTicker"] = {}
    for ticker in sorted({r["ticker"] for r in test if r.get("ticker")}):
        mask = np.array([r.get("ticker") == ticker for r in test])
        metrics["byTicker"][ticker] = {
            "rows": int(mask.sum()),
            "xgboostMae": float(np.mean(np.abs(xgboost_predictions[mask] - y_test[mask]))),
            "baselineMae": float(np.mean(np.abs(baseline[mask] - y_test[mask]))),
        }
    # Calibration never sees test labels. Preserve cross-company date alignment.
    model.calibration = []
    for row, predicted in zip(validation, validation_predictions):
        scale = max(row["features"][0], 1.0)
        model.calibration.append({"date": row["as_of"], "ticker": row.get("ticker", ""),
                                  "xgboost": float((row["target"] - predicted) / scale),
                                  "baseline": float((row["target"] - row["features"][0]) / scale)})
    metrics["uncertaintyBasis"] = "validation_residuals_only"
    metrics["uncertaintyRows"] = len(validation)
    metrics["overlappingTestWindows"] = True
    metrics["testInterpretation"] = "Retrospective forecast errors, not independent trials or portfolio validation."
    # Refit for inference using only labels known at the dataset reference date.
    x, y = arrays(rows)
    model.fit(x, y)
    model.method = selected_method
    return model, metrics


def dataset_forecast(dataset):
    reference = min([dataset.dates[-1], *[a.as_of for a in dataset.assets]])
    return fit_forecast([row for row in dataset.observations if row["label_end"] <= reference])


@lru_cache(maxsize=1)
def demo_forecast():
    return dataset_forecast(get_dataset("demo"))


def get_forecast(mode: str, *, dataset=None):
    if mode == "demo" and (dataset is None or dataset is get_dataset("demo")):
        return demo_forecast()
    # Real files and their model must belong to the same freshly loaded snapshot.
    return dataset_forecast(dataset if dataset is not None else get_dataset(mode))
