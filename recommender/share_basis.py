"""Reconcile split/bonus scales against independent evidence.

The output uses units outstanding at the dataset cutoff. Payment dates remain
those reported by the payment source. Independent entitlement amounts validate
the whole installment group, including future installments, not settlement.
Legacy B3 and typed issuer/provider inputs use identical arithmetic and gates.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
import json
import math

from .collect_market_data import DividendHistory, month_end, normalize, shift_month


def factor_after(day, actions, end):
    factor = math.prod(row['ratio'] for row in actions if day < date.fromisoformat(row['date']) <= end)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError('invalid_cumulative_share_factor')
    return factor


def br_number(text):
    return float(str(text).replace('.', '').replace(',', '.'))


def b3_evidence(rows, share_class):
    amounts, quotes = defaultdict(float), {}
    for row in rows:
        if row.get('typeStock', '').strip() != share_class:
            continue
        if normalize(row.get('corporateAction', '')) not in ('dividendo', 'jrs cap proprio'):
            continue
        record = datetime.strptime(row['lastDatePriorEx'], '%d/%m/%Y').date()
        quota = br_number(row.get('quotedPerShares') or '1')
        amount = br_number(row['valueCash'])
        if not math.isfinite(quota) or quota <= 0 or not math.isfinite(amount) or amount < 0:
            raise ValueError('invalid_B3_cash_or_share_unit')
        amounts[record] += amount / quota
        quote = br_number(row.get('closingPricePriorExDate') or '0')
        if not math.isfinite(quote):
            raise ValueError('invalid_B3_reference_quote')
        quote_date = row.get('dateClosingPricePriorExDate')
        if quote_date in (None, '', '31/12/9999'):
            continue
        day = datetime.strptime(quote_date, '%d/%m/%Y').date()
        if quote > 0:
            if day in quotes and not math.isclose(quotes[day], quote, abs_tol=.015, rel_tol=.001):
                raise ValueError('conflicting_B3_reference_quotes')
            quotes[day] = quote
    return amounts, quotes


def reconcile_prices(content, ticker, actions, b3_quotes, cutoff, *, coverage_start=None, evidence_provider='B3'):
    result = json.loads(content)['chart']['result'][0]
    if result['meta'].get('symbol') != ticker + '.SA' or result['meta'].get('currency') != 'BRL':
        raise ValueError('price_identity_or_currency_mismatch')
    if any(date.fromisoformat(row['date']) > cutoff for row in actions):
        raise ValueError('post_cutoff_share_action_requires_reconciliation')
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): float(value)
             for stamp, value in zip(result['timestamp'], result['indicators']['quote'][0]['close'])
             if value is not None and datetime.fromtimestamp(stamp, timezone.utc).date() <= cutoff}
    return _reconcile_daily_prices(daily, actions, b3_quotes, cutoff,
        coverage_start=coverage_start, evidence_provider=evidence_provider)


def reconcile_native_prices(daily, actions, independent_quotes, cutoff, *, coverage_start=None,
                            evidence_provider='B3'):
    """Reconcile a typed native raw series without manufacturing vendor JSON."""
    if not isinstance(daily, dict) or any(type(day) is not date or day > cutoff for day in daily):
        raise ValueError('invalid_native_price_dates')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in daily.values()):
        raise ValueError('invalid_native_price_value')
    if any(date.fromisoformat(row['date']) > cutoff for row in actions):
        raise ValueError('post_cutoff_share_action_requires_reconciliation')
    return _reconcile_daily_prices(daily, actions, independent_quotes, cutoff,
        coverage_start=coverage_start, evidence_provider=evidence_provider, modes=('raw',))


def _reconcile_daily_prices(daily, actions, b3_quotes, cutoff, *, coverage_start=None,
                            evidence_provider='B3', modes=('split_adjusted', 'raw')):
    if any(not math.isfinite(value) or value <= 0 for value in daily.values()):
        raise ValueError('invalid_stock_price')
    start = coverage_start or shift_month(cutoff, -60)
    anchors = {day: value for day, value in b3_quotes.items() if start <= day <= cutoff}
    if len(anchors) < 4:
        raise ValueError('insufficient_independent_price_matches')
    accepted = []
    for mode in modes:
        valid = True
        for day, raw in anchors.items():
            factor = factor_after(day, actions, cutoff)
            expected = raw / factor if mode == 'split_adjusted' else raw
            if day not in daily or not math.isclose(daily[day], expected, rel_tol=.006, abs_tol=.015):
                valid = False
                break
        if valid:
            accepted.append(mode)
    if not accepted:
        raise ValueError('historical_quote_or_share_scale_disagrees_with_B3' if evidence_provider == 'B3'
                         else 'historical_quote_or_share_scale_disagrees_with_independent_source')
    prices = {}
    mode = accepted[0]
    for day, value in sorted(daily.items()):
        key = month_end(day.year, day.month)
        adjusted = value / factor_after(day, actions, cutoff) if mode == 'raw' else value
        prices[key] = (day, adjusted, adjusted)
    months = (cutoff.year - start.year) * 12 + cutoff.month - start.month
    if months < 60:
        raise ValueError('less_than_61_consecutive_prices')
    required = [shift_month(cutoff, -i) for i in range(months + 1)]
    if any(day not in prices for day in required):
        raise ValueError('less_than_61_consecutive_prices')
    if (cutoff - prices[cutoff][0]).days > 7:
        raise ValueError('stale_month_end_price')
    # A quote after the last action cannot establish how earlier cohorts were
    # stored. At least one independent raw quote must cover every used regime.
    observed = {round(factor_after(day, actions, cutoff), 12) for day in anchors}
    needed = {round(factor_after(prices[day][0], actions, cutoff), 12) for day in required}
    if not needed.issubset(observed):
        raise ValueError('unverified_share_price_regime_requires_raw_history')
    for previous, current in zip(sorted(prices), sorted(prices)[1:]):
        if current in required and not .2 < prices[current][2] / prices[previous][2] < 3:
            raise ValueError('unexplained_extreme_price_change')
    return prices, sorted(day.isoformat() for day in anchors), mode


def reconcile_payments(ticker, original, expected, actions, cutoff, *, coverage_start=None, evidence_provider='B3'):
    start = coverage_start or shift_month(cutoff, -60)
    groups = defaultdict(list)
    for row in original.installments:
        groups[date.fromisoformat(row['record_date'])].append(row)
    required = {day for day in expected if start <= day <= cutoff}
    required.update(day for day, rows in groups.items()
                    if any(start <= date.fromisoformat(row['payment_date']) <= cutoff for row in rows))
    if not required.issubset(groups) or not required.issubset(expected):
        raise ValueError('B3_cash_event_missing_from_payment_table' if evidence_provider == 'B3'
                         else 'independent_cash_event_missing_from_payment_table')
    if len(required) < 4:
        raise ValueError('insufficient_independent_cash_matches')
    history, ledger, adjustments = DividendHistory(cutoff, original.event_count), [], []
    bases = sorted({date.fromisoformat(row['date']) for row in actions if date.fromisoformat(row['date']) <= cutoff})
    for record in sorted(required):
        rows, declared = groups[record], expected[record]
        reported = sum(row['gross_per_unit'] for row in rows)
        basis_factors = {1.}
        basis_factors.update(factor_after(record, actions, basis) for basis in [*bases, cutoff] if basis >= record)
        possible = [factor for factor in basis_factors
                    if math.isclose(reported, declared / factor, rel_tol=.002, abs_tol=.00002)]
        if not possible or reported <= 0:
            raise ValueError('cash_amount_or_share_scale_disagrees_with_B3' if evidence_provider == 'B3'
                             else 'cash_amount_or_share_scale_disagrees_with_independent_source')
        # Preserve payment-source rounding instead of replacing its amounts
        # values. The adjustment must be explained by a reported factor chain.
        source_factor = min(possible, key=lambda f: abs(reported - declared / f))
        multiplier = source_factor / factor_after(record, actions, cutoff)
        for row in rows:
            payment = date.fromisoformat(row['payment_date'])
            if payment > cutoff:
                history.future_events += 1
                continue
            amount = row['gross_per_unit'] * multiplier
            if payment < start:
                continue
            history.append((payment, payment, amount))
            basis_label = ('reported_payment_date_B3_amount_and_share_factor_reconciled' if evidence_provider == 'B3'
                           else 'reported_payment_date_independent_amount_and_share_factor_reconciled')
            ledger.append({**row, 'gross_per_unit': amount, 'original_reported_per_unit': row['gross_per_unit'],
                           'share_basis_date': cutoff.isoformat(), 'share_normalization_factor': multiplier,
                           'basis': basis_label})
        declared_key = ('declared_B3_per_record_share' if evidence_provider == 'B3'
                        else 'declared_independent_per_record_share')
        adjustments.append({'record_date': record.isoformat(), declared_key: declared,
                            'reported_installment_sum': reported, 'source_adjustment_factor': source_factor,
                            'normalization_multiplier': multiplier})
    history.coverage_start = max(original.coverage_start, start)
    history.event_count = len(ledger)
    history.filter_counts = {'raw_events': original.event_count, 'accepted_events': len(ledger),
                             'future_payment_date': history.future_events, 'unresolved_events': 0}
    return history, ledger, adjustments
