"""Apply a one-year cash forecast, without double-counting historical payments."""

import numpy as np

from .simulation import block_indices


def forecast_paths(history, cash_history, inflation, assets, forecasts, model, method,
                   months, count, block, seed, *, scenario_indices=None):
    history = np.asarray(history, dtype=float)
    cash_history = np.asarray(cash_history, dtype=float)
    inflation = np.asarray(inflation, dtype=float)
    if (history.ndim != 2 or cash_history.shape != history.shape
            or history.shape[1] != len(assets) or inflation.shape != (len(history),)):
        raise ValueError("History, cash, inflation and assets have incompatible shapes.")
    # Always allocate an annual forecast over a complete sampled payment year.
    scenario_months = max(12, months) if forecasts else months
    if scenario_indices is None:
        indices = block_indices(len(history), scenario_months, count, block, seed)
    else:
        indices = np.asarray(scenario_indices)
        if (indices.shape != (count, scenario_months) or indices.dtype.kind not in 'iu'
                or np.any(indices < 0) or np.any(indices >= len(history))):
            raise ValueError('Invalid synchronized forecast scenario indices.')
    paths = (1 + history[indices]) / (1 + inflation[indices, None]) - 1
    cash_paths = cash_history[indices] / (1 + inflation[indices, None])
    if not forecasts:
        return paths, cash_paths
    horizon = min(12, months)
    inflation_factors = 1 + inflation[indices[:, :horizon]]
    price_only = 1 + history[indices[:, :horizon]] - cash_history[indices[:, :horizon]]
    if not np.isfinite(price_only).all() or np.any(price_only <= 0):
        raise ValueError('Forecast per-unit cash requires positive ex-cash stock price paths.')
    opening_price_factor = np.concatenate([np.ones((count, 1, len(assets))),
        np.cumprod(price_only, axis=1)[:, :-1]], axis=1)
    rng = np.random.default_rng((seed + 65537) % 2_000_000_001)
    calibration = getattr(model, "calibration", [])
    calibration_dates = sorted({row["date"] for row in calibration})
    pooled = {date: [] for date in calibration_dates}
    for row in calibration:
        pooled[row["date"]].append(row[method])
    # Shared historical calibration dates retain observed cross-company shocks.
    draws = rng.integers(len(calibration_dates), size=count) if calibration_dates else None
    for i, asset in enumerate(assets):
        if asset.ticker not in forecasts:
            continue
        residuals = {row["date"]: row[method] for row in calibration
                     if row["ticker"] == asset.ticker}
        errors = np.zeros(count)
        if residuals:
            values = np.array([residuals.get(date, np.nan) for date in calibration_dates])
            errors = values[draws].copy()
            missing = np.isnan(errors)
            # Missing dates are unknown errors, not evidence of a perfect forecast.
            errors[missing] = rng.choice(list(residuals.values()), size=int(missing.sum()))
        elif draws is not None:
            # Retain individual signed errors; averaging companies erases dispersion.
            for date_index, date in enumerate(calibration_dates):
                mask = draws == date_index
                errors[mask] = rng.choice(pooled[date], size=int(mask.sum()))
        scale = max(asset.features[0], 1.0)
        annual_yields = np.maximum(0, forecasts[asset.ticker] + errors * scale) / asset.price
        sampled = cash_history[indices[:, :12], i]
        totals = sampled.sum(axis=1)
        # Calendar weights follow sampled observed payments; empty years use
        # equal monthly weights, explicitly a scenario rather than a due-date.
        pattern = np.divide(sampled, totals[:, None],
                            out=np.full_like(sampled, 1 / 12), where=totals[:, None] > 0)
        # The forecast is money per share/unit, not a yield on future market
        # wealth. Share counts may change through reinvestment, prices do not
        # manufacture a larger per-share cash payment.
        replacement = pattern[:, :horizon] * annual_yields[:, None] / opening_price_factor[:, :, i] / inflation_factors
        original = cash_paths[:, :horizon, i].copy()
        cash_paths[:, :horizon, i] = replacement
        # Historical total return = price component + historical paid cash.
        # Replace that cash component; never add the whole forecast twice.
        paths[:, :horizon, i] += replacement - original
    return paths[:, :months], cash_paths[:, :months]
