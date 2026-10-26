"""Dated Treasury lots for an explicit same-family reinvestment hypothesis.

The first lot uses the observed current quote. Later lots are hypothetical
contracts with the same family, coupon/amortization rules and an annual roll of
the original maturity. They are not future observed offers. Rates, IPCA and
Selic are caller-supplied joint scenarios; no price history is manufactured.
Valuation uses the existing contractual schedule and QuantLib Business/252.
"""

from copy import deepcopy
from datetime import date
import math

import numpy as np

from .reinvestment_ledger import LotKernel
from .treasury_cash_paths import _factor, _position, anniversary
from .treasury_contracts import nominal_cash_schedule
from .treasury_market import TreasuryInstrument
from .treasury_valuation import contract_for_offer


class TreasuryLotProvider:
    def __init__(self, instrument, factors, yields, *, months, annual_cost_percent=0., annual_valuations=False):
        if not isinstance(instrument, TreasuryInstrument):
            raise ValueError('current_treasury_instrument_required')
        if type(months) is not int or not 12 <= months <= 1224 or months % 12:
            raise ValueError('invalid_contract_scenario_horizon')
        inflation, selic = (np.asarray(factors[key], dtype=float) for key in ('inflation', 'selic'))
        yields = np.asarray(yields, dtype=float)
        if (inflation.ndim != 2 or selic.shape != inflation.shape or inflation.shape[1] < months + 1
                or yields.shape != (inflation.shape[0], months + 1)
                or not all(np.isfinite(a).all() for a in (inflation, selic, yields))
                or np.any(inflation <= -1) or np.any(selic < 0) or np.any(yields <= -100)
                or factors.get('future_values_observed') is not False):
            raise ValueError('invalid_joint_treasury_scenario_factors')
        if not math.isfinite(annual_cost_percent) or not 0 <= annual_cost_percent < 100:
            raise ValueError('invalid_annual_contract_cost')
        self.instrument, self.months, self.count = instrument, months, len(inflation)
        if type(annual_valuations) is not bool:
            raise ValueError('invalid_contract_valuation_frequency')
        self.annual_valuations = annual_valuations
        self.yields = yields
        self.levels = {'IPCA': np.column_stack([np.ones(self.count), np.cumprod(1 + inflation, axis=1)]),
                       'SELIC': np.column_stack([np.ones(self.count), np.cumprod(1 + selic, axis=1)])}
        self.cost_factor = (1 - annual_cost_percent / 100) ** (1 / 12)
        self.cache = {}
        self.dates = [anniversary(instrument.settlement, month) for month in range(months + 1)]
        self.business_days = np.array([instrument.calendar.business_days(instrument.settlement, day)
                                       for day in self.dates])
        self.provenance = {'initial_lot': 'observed_current_purchase_quote_not_executed',
            'future_lots': 'hypothetical_same_family_original_tenor_annual_maturity_roll',
            'future_offers_observed': False, 'price_history_created': False,
            'cash_basis': 'gross_before_individual_taxes_monthly_effective_plan_cost',
            'valuation': 'contractual_full_dirty_pv_Business252_QuantLib_calendar',
            'index_interpolation': 'geometric_purchase_month_anniversaries_not_official_daily_VNA',
            'principal_policy': 'original_real_acquisition_cost_pro_rata_face',
            'market_marks': 'annual_year_end_and_purchase' if annual_valuations else 'monthly',
            'source_url': instrument.offer['source_url']}

    def _rows(self, roll):
        instrument = self.instrument
        maturity = anniversary(date.fromisoformat(instrument.offer['maturity']), 12 * roll)
        if instrument.spec is None:
            return [{'contractual_date': maturity.isoformat(),
                     'adjusted_payment_date': instrument.calendar.following(maturity).isoformat(),
                     'amortized_face_principal': '1000', 'coupon_interest': '0'}], 'SELIC'
        offer = deepcopy(instrument.offer)
        offer['maturity'] = maturity.isoformat()
        offer.pop('conversion_date', None)
        offer.pop('first_coupon_date', None)
        # Roll the hypothetical maturity, not the purchase-window start. A
        # November purchase still owns the following January/July coupons.
        spec = contract_for_offer(offer, instrument.settlement)
        rows = nominal_cash_schedule(spec, instrument.settlement, maturity, '1000',
            business_day_adjuster=instrument.calendar.following,
            calendar_source={key: instrument.calendar.provenance[key]
                             for key in ('library', 'version', 'source_url')})
        return rows, spec['terms']['indexer']

    def _template(self, roll):
        if roll in self.cache:
            return self.cache[roll]
        instrument, months, count = self.instrument, self.months, self.count
        rows, indexer = self._rows(roll)
        start = instrument.settlement
        dates = self.dates
        nominal = (instrument.asset.price if instrument.spec is None else
                   float(instrument.nominal['value']) if instrument.nominal else 1000.)
        levels = self.levels[indexer][:, :months + 1] if indexer else np.ones((count, months + 1))
        gross = np.zeros((count, months))
        face = np.zeros(months)
        marks = np.ones(months + 1, dtype=bool)
        if self.annual_valuations:
            marks[:] = False
            marks[::12] = True
            marks[max(0, 12 * (roll - 1) + 1):12 * roll + 1] = True
        market = np.full((count, months + 1), np.nan)
        market[:, marks] = 0
        for row in rows:
            payment = date.fromisoformat(row['adjusted_payment_date'])
            principal = float(row['amortized_face_principal']) / 1000
            cash_fraction = principal + float(row['coupon_interest']) / 1000
            if payment <= dates[-1]:
                month, fraction = _position(start, payment)
                slot = month - int(fraction == 0)
                if slot >= 0:
                    index = _factor(self.levels[indexer], start, payment) if indexer else np.ones(count)
                    price_level = _factor(self.levels['IPCA'], start, payment)
                    gross[:, slot] += nominal * cash_fraction * index / price_level
                    face[slot] += principal
            # A discounted indexed reference is marked with the contemporaneous
            # scenario index, never the future realized payment index.
            positions = np.flatnonzero(marks & (np.arange(months + 1) < int(np.searchsorted(dates, payment))))
            if len(positions):
                if instrument.spec is None:
                    market[:, positions] += nominal * cash_fraction * levels[:, positions]
                else:
                    payment_days = instrument.calendar.business_days(start, payment)
                    times = (payment_days - self.business_days[positions]) / 252
                    discount = np.exp(-np.log1p(self.yields[:, positions] / 100) * times[None])
                    market[:, positions] += nominal * cash_fraction * levels[:, positions] * discount
        if not np.isfinite(market[:, marks]).all() or np.any(market[:, marks] < 0):
            raise ValueError('nonfinite_contract_market_valuation')
        value = (gross, face, market)
        self.cache = {roll: value}
        return value

    def __call__(self, entry, remaining, count):
        if (type(entry) is not int or not 0 <= entry < self.months
                or remaining != self.months - entry or count != self.count):
            raise ValueError('contract_kernel_scope_mismatch')
        # New future purchases preserve the original tenor within an annual band.
        # This does not assert that any particular future title will be issued.
        roll = math.ceil(entry / 12)
        gross, face, values = self._template(roll)
        inflation = self.levels['IPCA']
        purchase = (np.full(count, self.instrument.asset.price) if entry == 0 else values[:, entry])
        if np.any(purchase <= 0):
            raise ValueError('future_contract_has_no_positive_reference_purchase_value')
        quantity = inflation[:, entry] / purchase
        real_cash = gross[:, entry:] * quantity[:, None]
        real_market = values[:, entry + 1:] * quantity[:, None] / inflation[:, entry + 1:self.months + 1]
        # This effective plan-cost assumption proportionately reduces holdings;
        # no individual Brazilian taxation or official custody tier is implied.
        costs = self.cost_factor ** np.arange(1, remaining + 1)
        real_cash *= costs[None]
        real_market *= costs[None]
        released = np.broadcast_to(face[entry:], (count, remaining)) * costs[None]
        valuation_months = None
        if self.annual_valuations:
            valuation_months = tuple(range((11 - entry) % 12, remaining, 12))
            unused = np.ones(remaining, dtype=bool)
            unused[list(valuation_months)] = False
            real_market[:, unused] = np.nan
        return LotKernel(self.instrument.offer['kind'], self.instrument.asset.risk_level,
            real_cash, released.copy(), real_market,
            'current_quote_contract_projection' if entry == 0 else 'hypothetical_same_class_future_contract',
            self.instrument.offer['source_url'], valuation_months)
