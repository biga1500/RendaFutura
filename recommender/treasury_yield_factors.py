"""Offline, observed Treasury family yields and synchronized factor scenarios.

``load_treasury_yield_factors(folder, dates)`` returns JSON-safe monthly arrays
for the caller's consecutive month ends, including the preceding month's yield
anchor. Rates are annual fractions; IPCA and effective Selic are monthly
fractions, loaded through the existing verified BCB cache loader. A family
median is not a constant-tenor curve or any instrument's return/price history.

``yield_factor_scenarios(history, months, count, block, seed, indices=None,
index_dates=None)`` returns (count, months) NumPy arrays. Caller-supplied block
indices require the identical historical dates, so equities, IPCA, Selic and
yield shocks can share draws. Yield changes are additive annual-rate changes;
projected levels are recursively capped at the observed family envelope.
No prices, future offers, active pointers, downloads or files are created.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np

from .collect_market_data import month_end
from .macro_factors import load_macro_factors
from .market_inventory import market_root
from .official_treasury_bundle import MAX_BYTES, PRICE_URL, parse_price_history
from .simulation import block_indices


POLICY = 'RF-OBSERVED-TREASURY-FAMILY-YIELDS-1'
YIELD_KEYS = ('nominal_yield', 'real_yield', 'selic_spread')
FAMILY_KINDS = {
    'nominal_yield': ('Tesouro Prefixado', 'Tesouro Prefixado com Juros Semestrais'),
    'real_yield': ('Tesouro IPCA+', 'Tesouro IPCA+ com Juros Semestrais'),
    'selic_spread': ('Tesouro Selic',),
}
INSTRUMENT_FACTOR_POLICY = {
    **{kind: {'yield_factor': key, 'family_factor_proxy': True, 'mark_to_market': True}
       for key, kinds in FAMILY_KINDS.items() for kind in kinds},
    **{kind: {'yield_factor': 'real_yield', 'family_factor_proxy': True,
              'mark_to_market': True, 'proxy_source_family': 'IPCA',
              'observed_instrument_history': False}
       for kind in ('Tesouro Renda+', 'Tesouro Educa+')},
    'Tesouro Reserva': {'yield_factor': None, 'family_factor_proxy': False,
        'mark_to_market': False, 'accrual_factor': 'selic',
        'spread_inferred': False, 'observed_instrument_history': False},
}


def _month_dates(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 1224:
        raise ValueError('treasury_factor_dates_required')
    days = []
    for value in values:
        try:
            day = value if type(value) is date else date.fromisoformat(value)
        except (ValueError, TypeError) as error:
            raise ValueError('invalid_treasury_factor_month') from error
        if (type(day) is not date or day != month_end(day.year, day.month)
                or day > date.today() or not isinstance(value, (str, date))
                or isinstance(value, str) and value != day.isoformat()):
            raise ValueError('treasury_factors_require_observed_month_ends')
        if days and day.year * 12 + day.month != days[-1].year * 12 + days[-1].month + 1:
            raise ValueError('treasury_factor_months_must_be_consecutive')
        days.append(day)
    return days


def _previous_month(day):
    number = day.year * 12 + day.month - 2
    return month_end(number // 12, number % 12 + 1)


def _read_cache(path, root, limit):
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe_treasury_factor_cache')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('missing_or_oversized_treasury_factor_cache')
    content = path.read_bytes()
    if len(content) > limit:
        raise ValueError('oversized_treasury_factor_cache')
    return content


def _price_source(root):
    path = root / 'raw/tesouro-precos.csv'
    content = _read_cache(path, root, MAX_BYTES)
    digest = hashlib.sha256(content).hexdigest()
    # The official bundle retained the original download receipt for this cache.
    receipt = path.with_name(path.name + '.meta.json')
    if not receipt.exists():
        receipt = root / 'raw/official-treasury-bundle/price-history.csv.source.json'
    evidence = _read_cache(receipt, root, 64_000)
    metadata = json.loads(evidence)
    if (not isinstance(metadata, dict) or metadata.get('url') != PRICE_URL
            or metadata.get('sha256') != digest
            or 'bytes' in metadata and (type(metadata['bytes']) is not int
                                      or metadata['bytes'] != len(content))):
        raise ValueError('treasury_yield_source_integrity_or_identity_mismatch')
    return content, {'role': 'treasury_family_buy_yields', 'provider': 'Tesouro Transparente',
        'authority': 'primary', 'url': PRICE_URL, 'file': path.relative_to(root).as_posix(),
        'sha256': digest, 'bytes': len(content),
        'receipt_file': receipt.relative_to(root).as_posix(),
        'receipt_sha256': hashlib.sha256(evidence).hexdigest(),
        'retrieved_at': metadata.get('retrieved_at'),
        'origin': metadata.get('origin', 'existing_local_cache'),
        'hash_verified_against_cached_receipt': True,
        'point_in_time_archive_verified': False, 'revisions_may_exist': True}


def _aggregate(parsed, days, max_lag_days):
    wanted = {day.isoformat()[:7] for day in days}
    by_kind = {kind: key for key, kinds in FAMILY_KINDS.items() for kind in kinds}
    observations = {key: {} for key in YIELD_KEYS}
    excluded = {'other_kind': 0, 'missing_purchase_quote_or_yield': 0,
                'invalid_annual_yield': 0, 'matured_on_observation': 0}
    for (kind, maturity), rows in parsed['series'].items():
        for row in rows:
            if row['date'][:7] not in wanted:
                continue
            if kind not in by_kind:
                excluded['other_kind'] += 1
                continue
            price, rate = row['buy_price'], row['buy_rate']
            if price is None or price <= 0 or rate is None:
                excluded['missing_purchase_quote_or_yield'] += 1
                continue
            if not np.isfinite(rate) or not -100 < rate <= 1000:
                excluded['invalid_annual_yield'] += 1
                continue
            if maturity <= row['date']:
                excluded['matured_on_observation'] += 1
                continue
            key = by_kind[kind]
            observations[key].setdefault(row['date'], []).append({
                'kind': kind, 'maturity': maturity,
                'annual_buy_yield': float(Decimal(str(rate)) / 100)})
    levels = {key: [] for key in YIELD_KEYS}
    aggregates = []
    for end in days:
        prefix = end.isoformat()[:7]
        dates = [{day for day in observations[key] if day.startswith(prefix)} for key in YIELD_KEYS]
        common = set.intersection(*dates)
        if not common:
            raise ValueError('missing_observed_treasury_family_month_no_imputation:' + prefix)
        observed = max(common)
        lag = (end - date.fromisoformat(observed)).days
        if lag > max_lag_days:
            raise ValueError('stale_observed_treasury_family_month:' + prefix)
        details = {}
        for key in YIELD_KEYS:
            members = sorted(observations[key][observed], key=lambda row: (row['kind'], row['maturity']))
            median = float(np.median([row['annual_buy_yield'] for row in members]))
            levels[key].append(median)
            details[key] = {'annual_buy_yield': median, 'instrument_count': len(members),
                            'members': members}
        aggregates.append({'month': end.isoformat(), 'observation_date': observed,
                           'lag_calendar_days': lag, 'families': details})
    return levels, aggregates, excluded


@lru_cache(maxsize=2)
def _aggregate_verified_bytes(content, date_strings, max_lag_days):
    days = _month_dates(date_strings)
    parsed = parse_price_history(content, days[-1])
    values = _aggregate(parsed, [_previous_month(days[0]), *days], max_lag_days)
    diagnostics = {key: parsed[key] for key in
        ('row_count', 'included_row_count', 'rejected_row_count', 'rows_after_cutoff',
         'first_observation_date', 'last_observation_date', 'issues')}
    return values, diagnostics


def load_treasury_yield_factors(folder, dates, *, max_lag_days=7):
    """Read verified local Treasury/BCB sources; missing months fail, never fill.

    The raw price CSV needs its ``.meta.json`` download receipt, or the existing
    official bundle's ``price-history.csv.source.json`` with the same byte hash.
    Each month's aggregate uses one common quote date across PRE, IPCA and Selic,
    equal weight per available kind/maturity, not per day or traded quantity.
    The preceding month is mandatory: every returned shock has an observed base.
    """
    days = _month_dates(dates)
    if type(max_lag_days) is not int or not 0 <= max_lag_days <= 7:
        raise ValueError('treasury_factor_quote_lag_limit')
    root = market_root(folder)
    content, source = _price_source(root)
    prior = _previous_month(days[0])
    # Cache only immutable source bytes and compact aggregates, not the large
    # parsed daily book. Changed bytes/periods cannot reuse an old aggregate.
    aggregated, diagnostics = _aggregate_verified_bytes(content, tuple(day.isoformat() for day in days), max_lag_days)
    levels, aggregates, excluded = deepcopy(aggregated)
    macro, macro_digest = load_macro_factors(root)
    macro_days = _month_dates(macro['dates'])
    lookup = {day.isoformat(): index for index, day in enumerate(macro_days)}
    selected = [day.isoformat() for day in days]
    if any(day not in lookup for day in selected):
        raise ValueError('missing_observed_treasury_macro_month_no_imputation')
    output = {'schema_version': 1, 'policy': POLICY, 'dates': selected,
        'reference_date': selected[-1], 'prior_yield_date': prior.isoformat(),
        'yield_unit': 'fraction_per_year', 'macro_unit': 'fraction_per_month',
        'prior_levels': {key: levels[key][0] for key in YIELD_KEYS},
        **{key: levels[key][1:] for key in YIELD_KEYS},
        **{key + '_change': np.diff(levels[key]).tolist() for key in YIELD_KEYS},
        **{key: [macro[key][lookup[day]] for day in selected] for key in ('inflation', 'selic')},
        'monthly_aggregates': aggregates[1:], 'prior_aggregate': aggregates[0],
        'sources': [source, *macro['sources']], 'macro_cache_sha256': macro_digest,
        'aggregation': 'equal_instrument_median_last_common_quote_day_in_month',
        'max_quote_lag_calendar_days': max_lag_days,
        'composition_changes_may_affect_yield_changes': True,
        'constant_tenor_curve': False, 'instrument_factor_policy': deepcopy(INSTRUMENT_FACTOR_POLICY),
        'partial_months_included': False, 'missing_months_imputed': False,
        'bond_price_history_created': False, 'instrument_returns_created': False,
        'future_values_observed': False, 'point_in_time_archive_verified': False,
        'source_diagnostics': deepcopy(diagnostics),
        'excluded_from_family_aggregates': excluded,
        'limitations': ['Family composition and residual maturity change; these are not constant-tenor curves.',
            'Renda+ and Educa+ use an IPCA-family risk proxy, not observed instrument return history.',
            'Selic spread is an annual quote spread, not the effective monthly Selic accrual.',
            'Reserva has no mark-to-market factor; its accrual uses effective Selic only.',
            'Cached receipts check byte integrity, not point-in-time availability or later revisions.']}
    _validated_history(output)
    identity = {'policy': POLICY, 'dates': selected, 'max_lag_days': max_lag_days,
                'price_sha256': source['sha256'], 'macro_sha256': macro_digest}
    output['revision'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return output


def _validated_history(history):
    if (not isinstance(history, dict) or history.get('policy') != POLICY
            or history.get('schema_version') != 1
            or history.get('yield_unit') != 'fraction_per_year'
            or history.get('macro_unit') != 'fraction_per_month'):
        raise ValueError('invalid_treasury_factor_history_identity_or_units')
    days = _month_dates(history.get('dates'))
    arrays = {}
    for key in (*YIELD_KEYS, 'inflation', 'selic'):
        try:
            values = np.asarray(history[key], dtype=float)
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError('invalid_treasury_factor_values') from error
        if (values.shape != (len(days),) or not np.isfinite(values).all()
                or np.any(values <= -1) or key == 'selic' and np.any(values < 0)
                or key in YIELD_KEYS and np.any(values > 10)):
            raise ValueError('invalid_treasury_factor_values')
        arrays[key] = values
    anchors = history.get('prior_levels')
    if not isinstance(anchors, dict):
        raise ValueError('treasury_yield_prior_levels_required')
    for key in YIELD_KEYS:
        prior = anchors.get(key)
        try:
            changes = np.asarray(history.get(key + '_change'), dtype=float)
        except (ValueError, TypeError) as error:
            raise ValueError('invalid_treasury_yield_changes') from error
        if (type(prior) not in (int, float) or not np.isfinite(prior) or not -1 < prior <= 10
                or changes.shape != (len(days),) or not np.isfinite(changes).all()
                or not np.allclose(changes, np.diff(np.r_[prior, arrays[key]]), rtol=1e-12, atol=1e-15)):
            raise ValueError('treasury_yield_changes_disagree_with_observed_levels')
        arrays[key + '_change'] = changes
    return arrays


def yield_factor_scenarios(history, months, count, block, seed, *, indices=None, index_dates=None, padding_months=0):
    """Sample joint macro/shocks; bounded levels are model projections, not data.

    Return ``sampled_changes`` separately from ``applied_changes``: a historical
    shock hitting a projection bound is reduced, not represented as unchanged.
    Bounds include the prior anchor and all loaded monthly family medians.
    They are empirical modelling limits, not financial guarantees or a forecast.
    """
    arrays = _validated_history(history)
    length = len(history['dates'])
    if (type(months) is not int or not 1 <= months <= 1224
            or type(count) is not int or not 1 <= count <= 2000
            or type(block) is not int or not 1 <= block <= length
            or type(padding_months) is not int or not 0 <= padding_months <= 2):
        raise ValueError('treasury_factor_scenario_limit')
    # A dated payment/Following adjustment can fall just after the last mark.
    # Padding does not extend the user's planning or simulated-income horizon.
    months += padding_months
    if indices is None:
        if index_dates is not None:
            raise ValueError('treasury_factor_index_dates_without_indices')
        draws = block_indices(length, months, count, block, seed)
        draw_basis = 'generated_joint_block_indices'
    else:
        if (index_dates is None or [day.isoformat() for day in _month_dates(index_dates)]
                != [day.isoformat() for day in _month_dates(history['dates'])]):
            raise ValueError('treasury_equity_factor_index_dates_mismatch')
        draws = np.asarray(indices)
        if (draws.shape != (count, months) or draws.dtype.kind not in 'iu'
                or np.any(draws < 0) or np.any(draws >= length)):
            raise ValueError('invalid_synchronized_treasury_factor_indices')
        for start in range(0, months, block):
            chunk = draws[:, start:min(start + block, months)]
            if (np.any(chunk[:, 0] > length - block)
                    or np.any(np.diff(chunk.astype(np.int64), axis=1) != 1)):
                raise ValueError('synchronized_treasury_factor_indices_are_not_blocks')
        draws = draws.astype(np.int64, copy=True)
        draw_basis = 'caller_supplied_joint_block_indices_with_identical_dates'
    sampled, applied, bounds, hits, projected = {}, {}, {}, {}, {}
    for key in YIELD_KEYS:
        observed = np.r_[history['prior_levels'][key], arrays[key]]
        lower, upper = float(observed.min()), float(observed.max())
        raw = arrays[key + '_change'][draws]
        levels = np.empty((count, months), dtype=float)
        changes = np.empty_like(levels)
        previous = np.full(count, arrays[key][-1])
        hit_count = 0
        for month in range(months):
            unrestricted = previous + raw[:, month]
            current = np.clip(unrestricted, lower, upper)
            hit_count += int(np.count_nonzero((unrestricted < lower) | (unrestricted > upper)))
            levels[:, month] = current
            changes[:, month] = current - previous
            previous = current
        sampled[key], applied[key], projected[key] = raw, changes, levels
        bounds[key], hits[key] = [lower, upper], hit_count
    return {**projected, 'inflation': arrays['inflation'][draws], 'selic': arrays['selic'][draws],
        'indices': draws, 'sampled_changes': sampled, 'applied_changes': applied,
        'initial_levels': {key: float(arrays[key][-1]) for key in YIELD_KEYS},
        'projection_bounds': bounds, 'bound_hit_counts': hits,
        'projection_policy': 'recursive_additive_yield_shocks_clamped_to_observed_family_envelope',
        'bounds_are_financial_guarantees': False, 'drift_added': False,
        'basis': 'joint_block_bootstrap_observed_macro_and_family_yield_changes',
        'draw_basis': draw_basis, 'history_revision': history.get('revision'),
        'index_dates': list(history['dates']), 'yield_unit': 'fraction_per_year',
        'macro_unit': 'fraction_per_month', 'observed_period_start': history['dates'][0],
        'observed_period_end': history['dates'][-1],
        'future_values_observed': False, 'bond_price_history_created': False,
        'instrument_returns_created': False, 'constant_tenor_curve': False,
        'maturity_curve_extrapolation_performed': False,
        'instrument_factor_policy': deepcopy(history.get('instrument_factor_policy', INSTRUMENT_FACTOR_POLICY)),
        'sources': deepcopy(history.get('sources', []))}
