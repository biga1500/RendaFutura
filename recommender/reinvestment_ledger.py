"""Two-channel forward lot ledger for same-family, same-risk reinvestment.

The provider supplies dated/scenario-valued lot kernels, not invented historical
prices. Each kernel is normalized to one real currency unit invested. Ex-cash
market values must exclude every scheduled cash payment. Initial capital and
one-unit monthly contributions are propagated separately for linear planning.
Cash received at period end is reinvested at the next monthly period's start.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LotKernel:
    kind: str
    risk_level: int
    gross_cash: np.ndarray
    acquisition_cost_released: np.ndarray
    ex_cash_market_value: np.ndarray
    basis: str
    source_url: str
    valuation_months: tuple[int, ...] | None = None


def _validate_kernel(kernel, kind, risk_level, count, remaining, entry):
    if not isinstance(kernel, LotKernel) or kernel.kind != kind or kernel.risk_level != risk_level:
        raise ValueError('reinvestment_must_preserve_verified_family_and_risk')
    allowed = {'current_quote_contract_projection'} if entry == 0 else {'hypothetical_same_class_future_contract'}
    if kernel.basis not in allowed or not kernel.source_url.startswith('https://'):
        raise ValueError('future_lot_requires_explicit_scenario_basis_and_source')
    arrays = [np.asarray(values, dtype=float) for values in
        (kernel.gross_cash, kernel.acquisition_cost_released, kernel.ex_cash_market_value)]
    if any(values.shape != (count, remaining) for values in arrays):
        raise ValueError('invalid_lot_kernel_shape_or_value')
    if any(not np.isfinite(values).all() or np.any(values < 0) for values in arrays[:2]):
        raise ValueError('invalid_lot_kernel_shape_or_value')
    valuations = kernel.valuation_months
    if valuations is None:
        mask = np.ones(remaining, dtype=bool)
    else:
        required = set(range((11 - entry) % 12, remaining, 12))
        if (not isinstance(valuations, tuple) or len(set(valuations)) != len(valuations)
                or any(type(month) is not int or not 0 <= month < remaining for month in valuations)
                or not required.issubset(set(valuations))):
            raise ValueError('annual_wealth_requires_dated_year_end_valuations')
        mask = np.zeros(remaining, dtype=bool)
        mask[list(valuations)] = True
    if (not np.isfinite(arrays[2][:, mask]).all() or np.any(arrays[2][:, mask] < 0)
            or not np.isnan(arrays[2][:, ~mask]).all()):
        raise ValueError('invalid_or_undisclosed_partial_market_valuations')
    if np.any(arrays[1].sum(axis=1) > 1 + 1e-8):
        raise ValueError('lot_cannot_release_more_than_its_original_acquisition_cost')
    return arrays


def reinvestment_factors(provider, *, kind, risk_level, months, accumulation_months, count,
                        include_retirement_unit=False):
    if (type(months) is not int or not 12 <= months <= 1224 or months % 12
            or type(accumulation_months) is not int or not 0 <= accumulation_months < months
            or accumulation_months % 12 or type(count) is not int or not 1 <= count <= 2000):
        raise ValueError('invalid_reinvestment_horizon')
    if not callable(provider) or not isinstance(kind, str) or not kind or risk_level not in (0, 1, 2):
        raise ValueError('invalid_reinvestment_contract_provider')
    if type(include_retirement_unit) is not bool:
        raise ValueError('invalid_retirement_unit_channel')
    # Channels are [one initial real currency unit, one unit contributed monthly].
    channels = 3 if include_retirement_unit else 2
    market = np.zeros((channels, count, months))
    principal = np.zeros_like(market)
    income = np.zeros_like(market)
    losses = np.zeros_like(market)
    pending = np.zeros((channels, count))
    pending[0] = 1
    wealth_years = [np.broadcast_to(np.array([1., 0., *([0.] if include_retirement_unit else [])])[:, None],
                                    (channels, count)).copy()]
    income_years, weighted_income_years, loss_years = [], [], []
    annual_income, annual_loss = np.zeros((channels, count)), np.zeros((channels, count))
    annual_weighted_income = np.zeros((channels, count))
    original_lots, future_lots = 0, 0
    for month in range(months):
        if include_retirement_unit and month == accumulation_months:
            pending[2] += 1
        invested = pending.copy()
        pending.fill(0)
        if np.any(invested):
            kernel = provider(month, months - month, count)
            gross, released, values = _validate_kernel(kernel, kind, risk_level, count, months - month, month)
            # A premium-loss receipt cannot distribute unavailable principal.
            capital_paid = np.minimum(gross, released)
            interest_paid = np.maximum(gross - released, 0)
            capital_lost = np.maximum(released - gross, 0)
            # Providers may omit unused intra-year marks as NaN, never as fake
            # zero prices. Every year-end consumed below must be valued.
            market[:, :, month:] += invested[:, :, None] * values[None]
            principal[:, :, month:] += invested[:, :, None] * capital_paid[None]
            income[:, :, month:] += invested[:, :, None] * interest_paid[None]
            losses[:, :, month:] += invested[:, :, None] * capital_lost[None]
            original_lots += month == 0
            future_lots += month > 0
        if month < accumulation_months:
            pending += principal[:, :, month] + income[:, :, month]
            pending[1] += 1
        else:
            pending += principal[:, :, month]
            annual_income += income[:, :, month]
            annual_weighted_income += income[:, :, month] * ((11 - month % 12) / 12)
            annual_loss += losses[:, :, month]
        if (month + 1) % 12 == 0:
            wealth_years.append(market[:, :, month] + pending)
            income_years.append(annual_income.copy())
            weighted_income_years.append(annual_weighted_income.copy())
            loss_years.append(annual_loss.copy())
            annual_income.fill(0)
            annual_weighted_income.fill(0)
            annual_loss.fill(0)
    result = {'wealth_factors': np.stack(wealth_years, axis=1)[:2],
        'annual_income_factors': np.stack(income_years, axis=1)[:2],
        'annual_weighted_income_factors': np.stack(weighted_income_years, axis=1)[:2],
        'annual_capital_loss_factors': np.stack(loss_years, axis=1)[:2],
        'original_lots': original_lots, 'hypothetical_future_lots': future_lots,
        'channels': ['initial_capital', 'monthly_contribution'],
        'principal_counted_as_income': False, 'reinvestment_kind': kind,
        'reinvestment_risk_level': risk_level,
        'reinvestment_timing': 'cash_at_period_end_reinvested_next_month_start',
        'future_contracts_observed_or_executed': False, 'annual_rebalance_between_classes': False,
        'tax_or_cost_basis': 'provider_must_supply_gross_or_cost_adjusted_kernels_explicitly'}
    if include_retirement_unit:
        result['retirement_unit_annual_income_factors'] = np.stack(income_years, axis=1)[2, accumulation_months // 12:]
        result['retirement_unit_basis'] = 'one_real_unit_newly_invested_at_retirement_in_hypothetical_same_family_lot'
    return result
