"""Forward cash from a current acquisition lot; no simulated historical prices.

Monthly macro factors are interpolated geometrically between purchase-date
anniversaries. This explicit scenario convention is not the official daily VNA
publication convention. Cost basis is allocated pro rata to remaining face.
Signed economic losses remain signed. Released principal must be passed to a
separate same-class reinvestment ledger, never added to retirement income.
"""

import calendar as month_calendar
from datetime import date
import math

import numpy as np

from .treasury_contracts import nominal_cash_schedule
from .treasury_market import TreasuryInstrument


def anniversary(day, months):
    number = day.year * 12 + day.month - 1 + months
    year, month = divmod(number, 12)
    return date(year, month + 1, min(day.day, month_calendar.monthrange(year, month + 1)[1]))


def _position(settlement, payment):
    month = (payment.year - settlement.year) * 12 + payment.month - settlement.month
    if anniversary(settlement, month) > payment:
        month -= 1
    lower, upper = anniversary(settlement, month), anniversary(settlement, month + 1)
    return month, (payment - lower).days / (upper - lower).days


def _factor(levels, settlement, payment):
    month, fraction = _position(settlement, payment)
    if month < 0 or month + int(fraction > 0) >= levels.shape[1]:
        raise ValueError('factor_path_does_not_cover_payment_date')
    if fraction == 0:
        return levels[:, month]
    return levels[:, month] * np.power(levels[:, month + 1] / levels[:, month], fraction)


def initial_lot_cash_paths(instrument, factors, *, quantity=1.):
    if not isinstance(instrument, TreasuryInstrument) or not math.isfinite(quantity) or quantity <= 0:
        raise ValueError('invalid_current_treasury_lot')
    inflation, selic = (np.asarray(factors[key], dtype=float) for key in ('inflation', 'selic'))
    if (inflation.ndim != 2 or selic.shape != inflation.shape or not np.isfinite(inflation).all()
            or not np.isfinite(selic).all() or np.any(inflation <= -1) or np.any(selic < 0)):
        raise ValueError('invalid_macro_scenario_factors')
    count, months = inflation.shape
    if not 1 <= count <= 2000 or not 1 <= months <= 1224 or factors.get('future_values_observed') is not False:
        raise ValueError('invalid_macro_scenario_scope')
    levels = {'IPCA': np.column_stack([np.ones(count), np.cumprod(1 + inflation, axis=1)]),
              'SELIC': np.column_stack([np.ones(count), np.cumprod(1 + selic, axis=1)])}
    gross, coupons, real_income = [np.zeros((count, months)) for _ in range(3)]
    basis_return = np.zeros(months)
    nominal_income = np.zeros_like(gross)
    original_cost = quantity * instrument.asset.price
    settlement = instrument.settlement
    end = anniversary(settlement, months)
    if instrument.spec is None:
        maturity = instrument.calendar.following(date.fromisoformat(instrument.offer['maturity']))
        rows = [{'adjusted_payment_date': maturity.isoformat(), 'amortized_face_principal': '1000',
                 'coupon_interest': '0', 'contractual_date': instrument.offer['maturity']}]
        nominal_start, indexer = instrument.asset.price, 'SELIC'
    else:
        spec = instrument.spec
        maturity = date.fromisoformat(spec['maturity'])
        rows = nominal_cash_schedule(spec, settlement, maturity, '1000',
            business_day_adjuster=instrument.calendar.following,
            calendar_source={key: instrument.calendar.provenance[key] for key in ('library', 'version', 'source_url')})
        rows = [row for row in rows if date.fromisoformat(row['adjusted_payment_date']) > settlement]
        nominal_start = float(instrument.nominal['value']) if instrument.nominal else 1000.
        indexer = spec['terms']['indexer']
    if not rows:
        raise ValueError('current_treasury_lot_has_no_remaining_contractual_payments')
    remaining_face = math.fsum(float(row['amortized_face_principal']) / 1000 for row in rows)
    if not math.isclose(remaining_face, 1, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError('current_lot_requires_full_remaining_face_schedule')
    dates, returned = [], 0.
    for position, row in enumerate(rows):
        payment = date.fromisoformat(row['adjusted_payment_date'])
        if payment > end:
            break
        month, fraction = _position(settlement, payment)
        slot = max(0, month - int(fraction == 0))
        if slot >= months:
            raise ValueError('payment_slot_outside_scenario_horizon')
        principal_fraction = float(row['amortized_face_principal']) / 1000
        coupon_fraction = float(row['coupon_interest']) / 1000
        allocated = original_cost * principal_fraction
        if position == len(rows) - 1:
            allocated = original_cost - returned
        index = _factor(levels[indexer], settlement, payment) if indexer else np.ones(count)
        price_level = _factor(levels['IPCA'], settlement, payment)
        face = quantity * nominal_start * index * principal_fraction
        coupon = quantity * nominal_start * index * coupon_fraction
        paid = face + coupon
        gross[:, slot] += paid
        coupons[:, slot] += coupon
        nominal_income[:, slot] += paid - allocated
        real_income[:, slot] += paid / price_level - allocated
        basis_return[slot] += allocated
        returned += allocated
        dates.append({'contractual_date': row['contractual_date'], 'scenario_payment_date': payment.isoformat(),
            'scenario_month': slot + 1, 'settled': None, 'face_fraction': principal_fraction,
            'coupon_fraction': coupon_fraction, 'allocated_acquisition_cost': allocated})
    return {'gross_nominal_cash': gross, 'coupon_nominal_cash': coupons,
        'economic_income_nominal': nominal_income, 'economic_income_real': real_income,
        'acquisition_cost_released_real': basis_return, 'original_acquisition_cost': original_cost,
        'remaining_acquisition_cost': original_cost - returned, 'payment_calendar': dates,
        'principal_counted_as_income': False, 'negative_economic_income_clipped': False,
        'price_history_created': False, 'future_payments_observed': False,
        'post_maturity_income_generated': False, 'reinvestment_included': False,
        'basis': 'current_quote_initial_lot_contractual_cash_with_joint_macro_bootstrap',
        'macro_interpolation': 'geometric_between_purchase_month_anniversaries_not_official_daily_VNA',
        'index_anchor_basis': instrument.nominal['basis'] if instrument.nominal else
            'quoted_current_capital_unit' if instrument.spec is None else 'fixed_face_BRL',
        'cost_allocation_policy': 'pro_rata_remaining_face_not_individual_tax_or_effective_interest',
        'cashflow_dates': 'QuantLib_Brazil_Settlement_Following_projections_not_settlement_confirmation'}
