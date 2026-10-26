from types import SimpleNamespace

import numpy as np
import pytest

from .forecast import CashForecast, fit_forecast
from .forecast_paths import forecast_paths
from .simulation import bootstrap_cash_paths, bootstrap_paths
from .test_forecast_policy import make_rows


def asset(ticker="A", previous_cash=10.0, price=100.0):
    return SimpleNamespace(ticker=ticker, price=price,
                           features=[previous_cash, 0.8, 20, 15, 0.4, 0.5, 1])


def scenarios(assets, forecasts, calibration=(), *, months=12, count=800,
              cash_history=None, inflation=None, method="xgboost", block=6):
    if cash_history is None:
        cash_history = np.zeros((24, len(assets)))
    if inflation is None:
        inflation = np.zeros(len(cash_history))
    history = np.asarray(cash_history, dtype=float) + 0.01
    model = SimpleNamespace(calibration=calibration, method="baseline")
    return forecast_paths(history, cash_history, inflation, assets, forecasts,
                          model, method, months, count, block, 42)


def forecast_cash_at_original_price(cash, inflation=0.):
    # The fixture's ex-cash nominal price grows 1% each month. Reconstruct
    # money per original unit; summing market-value yields would change units.
    opening_prices = np.power(1.01, np.arange(cash.shape[1]))
    return (cash[:, :, 0] * opening_prices[None] * (1 + inflation)).sum(axis=1)


def test_baseline_scores_use_the_same_previous_cash_as_inference():
    rows = make_rows(residual=0)
    model, reference = fit_forecast(rows)
    _, changed = fit_forecast([row | {"baseline": row["baseline"] + 100} for row in rows])

    assert model.method == "baseline"
    for key in ("mae", "rmse", "baselineMae", "validationBaselineMae", "selectedMethod"):
        assert changed[key] == reference[key], key
    assert reference["mae"] == 0


@pytest.mark.parametrize("targets", [[[1], [2]], [1, np.nan], [1, -1]])
def test_fit_rejects_invalid_targets_before_the_regressor(targets):
    calls = []
    regressor = SimpleNamespace(fit=lambda *args: calls.append(args))
    model = CashForecast(regressor)
    with pytest.raises(ValueError, match="targets"):
        model.fit([asset().features, asset().features], targets)
    assert not calls


@pytest.mark.parametrize("features", [
    [1, 0.8, 2, 1.5, 0.4, 0.5, 1],
    [[1, 0.8, 2, 1.5, 0.4, 0.5]],
    [[np.nan, 0.8, 2, 1.5, 0.4, 0.5, 1]],
    [[-1, 0.8, 2, 1.5, 0.4, 0.5, 1]],
])
def test_baseline_prediction_rejects_invalid_feature_batches(features):
    with pytest.raises(ValueError, match="features"):
        CashForecast(None, method="baseline").predict(features)


@pytest.mark.parametrize("residuals", [(-0.5, 0.5), (0.2, 0.6)])
def test_ragged_calibration_resamples_observed_errors_without_zero_imputation(residuals):
    calibration = [
        {"date": "2020-01-01", "ticker": "A", "xgboost": residuals[0]},
        {"date": "2020-02-01", "ticker": "A", "xgboost": residuals[1]},
        {"date": "2020-03-01", "ticker": "OTHER", "xgboost": 0},
        {"date": "2020-04-01", "ticker": "OTHER", "xgboost": 0},
    ]
    _, cash = scenarios([asset()], {"A": 10}, calibration)
    annual = forecast_cash_at_original_price(cash)
    expected = (1 + np.asarray(residuals)) / 10

    np.testing.assert_allclose(np.unique(annual), expected)
    assert annual.mean() == pytest.approx(expected.mean(), abs=0.004)


def test_unseen_ticker_preserves_pooled_dispersion_instead_of_averaging_it_away():
    calibration = [{"date": "2020-01-01", "ticker": ticker, "xgboost": residual}
                   for ticker, residual in [("B", -0.5), ("C", 0.5)]]
    _, cash = scenarios([asset()], {"A": 10}, calibration)
    np.testing.assert_allclose(np.unique(forecast_cash_at_original_price(cash)), [0.05, 0.15])


def test_shared_dates_keep_observed_cross_company_shocks():
    calibration = [{"date": day, "ticker": ticker, "xgboost": residual}
                   for day, residual in [("2020-01-01", -0.5), ("2020-02-01", 0.5)]
                   for ticker in ("A", "B")]
    args = ([asset(), asset("B", previous_cash=20)], {"A": 10, "B": 20}, calibration)
    first = scenarios(*args)
    repeated = scenarios(*args)

    np.testing.assert_array_equal(first[0], repeated[0])
    np.testing.assert_array_equal(first[1], repeated[1])
    np.testing.assert_allclose(first[1][:, :, 1], first[1][:, :, 0] * 2)


@pytest.mark.parametrize("previous_cash,forecast", [(0.0, 0.0), (0.2, 0.2)])
@pytest.mark.parametrize("method", ["baseline", "xgboost"])
def test_zero_and_small_payments_use_cash_units_and_the_requested_policy(previous_cash, forecast, method):
    calibration = [{"date": day, "ticker": "A", method: residual}
                   for day, residual in [("2020-01-01", -0.5), ("2020-02-01", 0.5)]]
    _, cash = scenarios([asset(previous_cash=previous_cash)], {"A": forecast},
                        calibration, method=method)
    expected = np.maximum(0, forecast + np.array([-0.5, 0.5])) / 100
    np.testing.assert_allclose(np.unique(forecast_cash_at_original_price(cash)), expected)
    assert np.isfinite(cash).all()
    assert (cash >= 0).all()


def test_partial_year_keeps_the_full_year_payment_pattern():
    history = np.zeros((12, 1))
    history[8] = 0.1
    kwargs = dict(cash_history=history, count=8, block=12)
    short = scenarios([asset()], {"A": 20}, months=5, **kwargs)
    full = scenarios([asset()], {"A": 20}, months=12, **kwargs)

    np.testing.assert_array_equal(short[0], full[0][:, :5])
    np.testing.assert_array_equal(short[1], full[1][:, :5])
    np.testing.assert_array_equal(short[1], 0)
    np.testing.assert_allclose(full[1][:, 8, 0] * 1.01 ** 8, 0.2)


def test_integer_zero_history_replaces_only_first_year_cash_in_real_units():
    cash_history = np.zeros((24, 2), dtype=int)
    inflation = np.full(24, 0.01)
    returns, cash = scenarios([asset(), asset("B")], {"A": 12},
                             cash_history=cash_history, inflation=inflation, months=24)
    history = cash_history + 0.01
    original_returns = bootstrap_paths(history, inflation, 24, 800, 6, 42)
    original_cash = bootstrap_cash_paths(cash_history, inflation, 24, 800, 6, 42)

    np.testing.assert_allclose(forecast_cash_at_original_price(cash[:, :12], .01), 0.12)
    np.testing.assert_allclose(returns - cash, original_returns - original_cash, atol=1e-15)
    np.testing.assert_array_equal(returns[:, 12:], original_returns[:, 12:])
    np.testing.assert_array_equal(cash[:, 12:], original_cash[:, 12:])
    np.testing.assert_array_equal(returns[:, :, 1], original_returns[:, :, 1])
    assert np.all(cash <= 1 + returns)


def test_forecast_paths_reject_misaligned_cash_history():
    with pytest.raises(ValueError, match="shape"):
        forecast_paths(np.zeros((24, 2)), np.zeros((24, 1)), np.zeros(24),
                       [asset(), asset("B")], {"A": 10}, None,
                       "baseline", 12, 4, 6, 42)
