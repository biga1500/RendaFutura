"""Optional offline provider of harvest-compatible candidates from local imports.

Public API:
    load_historical_candidates(folder, cutoff, b3_registry) -> ProviderResult
    adapt_import_bundle(bundle, b3_registry) -> ProviderResult

folder=None is an explicit no-op. Otherwise load_import_bundle validates the
unmodified input. No network, cache writes, collector execution or activation.

b3_registry is an exact ticker-keyed projection of a B3 register supplied by the
caller. Every entry must contain ticker, name, cvm, share_class, isin, sector_code;
industry_classification is optional. These are NOT inferred from ticker suffixes,
manual names or other share classes. Ordinary shares only; units/aliases require
a separate reconciliation. The caller remains responsible for B3 provenance.

ProviderResult is (candidates, financial_statements, exclusions, manifest).
financial_statements is a DICT keyed by exact (cvm, share_class, sector_code),
NOT a flat list. For each stock the parent must use:
    spec = stock['statement_spec']
    statements = financial_statements[spec]
    financial_features(statements, stock['payments'], cutoff, spec)
    stock_observations(stock['ticker'], stock['prices'], stock['payments'],
                       statements, cutoff, spec=spec)
Do not concatenate these groups with official CVM versions: the version field
is a documented publication-order rank within the imported fiscal year, not an
invented official filing version. filing_id is the supplied source_ref.

Candidate keys include the harvest identity, prices, payments, ledger, source
URLs, share_actions, share_normalization, financial_basis_start, training_since,
independent_*_matches (empty: no independent B3 price/payment check is claimed),
plus statement_spec, observations and observation_coverage. Monthly price tuples
are (actual_quote_date, cutoff_unit_close, cutoff_unit_close): no dividend or
total-return adjustment. DividendHistory contains (payment_date, payment_date,
cutoff_unit_paid_amount), preserving observed cash dates. Future payments remain
separate and unchanged, never paid cash.

All four input tables must exist and have explicitly complete per-ticker
coverage spanning cutoff-60 months through cutoff. Empty actions are permitted
only with that explicit declaration; empty payments/fundamentals cannot supply
cash/accounts. No missing month or unknown share regime is filled. Action
coverage must include every basis date used in a conversion. Financial EPS/FCF
are normalized from each row's declared share_basis_date; ratios are unchanged.
Historical availability remains published_at; revised cutoff units do not claim
an independently verified point-in-time archive.

manifest is an in-memory receipt: source_manifest contains real input filenames,
hashes and source URLs; bundle_provenance retains all completeness declarations.
The B3 projection has its own fingerprint, not a fictitious cached file. Store
the full receipt when integrating; do not discard distinct file hashes merely
because several input files share a source URL. This provider does not fetch a
current purchase quote or certify a prediction.
"""

from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, TypeAlias

from .collect_market_data import (DividendHistory, financial_features, month_end,
                                  shift_month, stock_observations)
from .historical_import import ImportBundle, load_import_bundle
from .share_basis import factor_after


Spec: TypeAlias = tuple[int, str, int]
ProviderResult: TypeAlias = tuple[list[dict[str, Any]], dict[Spec, list[dict[str, Any]]],
                                 list[dict[str, str]], dict[str, Any]]


class HistoricalProviderError(ValueError):
    """Fixed diagnostic codes only; never echo untrusted rows or URLs."""


class _Rejected(HistoricalProviderError):
    pass


def _reject(reason):
    raise _Rejected(reason)


def _cutoff(value):
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        if type(parsed) is not date or parsed != month_end(parsed.year, parsed.month):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise HistoricalProviderError('cutoff_must_be_ISO_month_end') from None
    if parsed > date.today():
        raise HistoricalProviderError('future_cutoff')
    return parsed


def _identity(ticker, registry):
    entry = registry.get(ticker)
    if not isinstance(entry, Mapping) or entry.get('ticker') != ticker:
        _reject('exact_B3_ticker_mapping_unavailable')
    if not re.fullmatch(r'[A-Z]{4}[3-8]', ticker):
        _reject('ordinary_share_identity_required')
    name = entry.get('name')
    isin = entry.get('isin')
    cvm = entry.get('cvm')
    if isinstance(cvm, str) and re.fullmatch(r'[0-9]{1,12}', cvm):
        cvm = int(cvm)
    if (type(cvm) is not int or not 1 <= cvm <= 999_999_999_999 or not isinstance(name, str) or not name.strip()
            or len(name) > 500 or any(ord(c) < 32 for c in name)
            or not isinstance(isin, str) or not re.fullmatch(r'BR[A-Z0-9]{9}[0-9]', isin)):
        _reject('incomplete_B3_instrument_identity')
    share_class, sector = entry.get('share_class'), entry.get('sector_code')
    if share_class not in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'):
        _reject('unsupported_B3_share_class')
    if type(sector) is not int or not 0 <= sector <= 9:
        _reject('B3_sector_code_unavailable')
    industry = entry.get('industry_classification')
    if industry is not None and (not isinstance(industry, str) or len(industry) > 1000
                                 or any(ord(c) < 32 for c in industry)):
        _reject('invalid_B3_industry_classification')
    return {'ticker': entry['ticker'], 'name': name, 'cvm': cvm, 'share_class': share_class,
            'isin': isin, 'sector_code': sector,
            'industry_classification': industry}


def _coverage(ticker, table, bundle, cutoff):
    provenance = bundle['provenance']
    if table in provenance['missing_tables']:
        _reject('missing_' + table + '_table')
    interval = provenance['source']['tickers'][ticker]['coverage'].get(table)
    if interval is None or interval.get('complete') is not True:
        _reject('incomplete_' + table + '_coverage')
    if interval['start'] > shift_month(cutoff, -60).isoformat() or interval['end'] < cutoff.isoformat():
        _reject('insufficient_' + table + '_coverage_window')
    return interval


def _actions(rows, cutoff):
    actions = []
    for row in rows:
        if row['effective_date'] > cutoff.isoformat():
            _reject('future_share_action')
        ratio = row['numerator'] / row['denominator']
        if not math.isfinite(ratio) or ratio <= 0:
            _reject('invalid_share_action_ratio')
        actions.append({'date': row['effective_date'], 'ratio': ratio, 'type': row['type'],
                        'numerator': row['numerator'], 'denominator': row['denominator']})
    return sorted(actions, key=lambda row: (row['date'], row['type']))


def _divisor(basis_date, actions, coverage, cutoff):
    if not coverage['start'] <= basis_date.isoformat() <= coverage['end']:
        _reject('share_action_coverage_does_not_cover_basis')
    try:
        return factor_after(basis_date, actions, cutoff)
    except ValueError:
        _reject('invalid_cumulative_share_factor')


def _scaled(value, divisor, *, positive=False):
    result = value / divisor
    if not math.isfinite(result) or (positive and result <= 0):
        _reject('nonfinite_or_invalid_normalized_value')
    return result


def _prices(rows, source, actions, action_coverage, cutoff):
    prices = {}
    for row in rows:
        day = date.fromisoformat(row['date'])
        if day > cutoff:
            _reject('future_training_price')
        divisor = (_divisor(day, actions, action_coverage, cutoff)
                   if source['price_basis'] == 'raw' else 1.)
        close = _scaled(row['close'], divisor, positive=True)
        month = month_end(day.year, day.month)
        if month not in prices or day > prices[month][0]:
            prices[month] = (day, close, close)
    required = [shift_month(cutoff, -i) for i in range(61)]
    if any(month not in prices for month in required):
        _reject('less_than_61_consecutive_prices')
    if (cutoff - prices[cutoff][0]).days > 7:
        _reject('stale_month_end_price')
    return dict(sorted(prices.items()))


def _payments(ticker, rows, future, source, actions, coverage, action_coverage, cutoff):
    if not rows:
        _reject('paid_payment_history_unavailable')
    history = DividendHistory(cutoff, len(rows) + len(future), basis='payment')
    history.coverage_start = date.fromisoformat(coverage['start'])
    history.future_events = len(future)
    # Block every feature/target window extending before declared cash coverage.
    history.issues.append({'event_index': -1, 'interval_start': date.min.isoformat(),
                           'interval_end': coverage['start'], 'reason': 'outside_declared_payment_coverage'})
    ledger = []
    for row in rows:
        if row['label'] not in ('DIVIDENDO', 'JCP'):
            _reject('unsupported_equity_payment_label')
        payment = date.fromisoformat(row['payment_date'])
        record = date.fromisoformat(row['record_date'])
        if payment > cutoff or record > payment:
            _reject('invalid_or_future_paid_payment')
        divisor = (_divisor(record, actions, action_coverage, cutoff)
                   if source['cash_share_basis'] == 'record_date' else 1.)
        amount = _scaled(row['gross_per_share'], divisor, positive=True)
        history.append((payment, payment, amount))
        ledger.append({'ticker': ticker, 'record_date': row['record_date'], 'payment_date': row['payment_date'],
                       'gross_per_unit': amount, 'original_reported_per_unit': row['gross_per_share'],
                       'label': row['label'], 'status': 'included', 'share_basis_date': cutoff.isoformat(),
                       'share_normalization_factor': 1 / divisor,
                       'basis': 'manual_observed_payment_date_declared_share_basis'})
    history.sort(key=lambda row: (row[0], row[2]))
    history.filter_counts = {'raw_events': history.event_count, 'accepted_events': len(rows),
                             'future_payment_date': len(future), 'unresolved_events': 0}
    history.payment_audit_counts = {'accepted': len(rows), 'future': len(future), 'unresolved': 0}
    return history, sorted(ledger, key=lambda row: (row['payment_date'], row['record_date'], row['label']))


def _statements(rows, identity, actions, action_coverage, cutoff):
    if not rows:
        _reject('financial_statements_unavailable')
    output, versions = [], Counter()
    for row in sorted(rows, key=lambda r: (r['fiscal_end'], r['published_at'])):
        if (row['cvm'], row['share_class'], row['sector_code']) != (
                identity['cvm'], identity['share_class'], identity['sector_code']):
            _reject('financial_statement_spec_disagrees_with_B3')
        if row['published_at'] > cutoff.isoformat() or row['fiscal_end'] > row['published_at']:
            _reject('future_or_invalid_financial_publication')
        basis = date.fromisoformat(row['share_basis_date'])
        if basis > cutoff:
            _reject('future_financial_share_basis')
        divisor = _divisor(basis, actions, action_coverage, cutoff)
        eps = _scaled(row['eps'], divisor)
        fcf = None if row['free_cash_per_share'] is None else _scaled(row['free_cash_per_share'], divisor)
        if fcf is None and identity['sector_code'] != 2:
            _reject('nonfinancial_FCF_unavailable')
        versions[row['fiscal_end']] += 1
        output.append({'cvm': identity['cvm'], 'share_class': identity['share_class'],
                       'sector_code': identity['sector_code'], 'fiscal_end': row['fiscal_end'],
                       'published_at': row['published_at'], 'version': str(versions[row['fiscal_end']]),
                       'filing_id': row['source_ref'], 'profit_per_share': eps, 'free_cash_per_share': fcf,
                       'debt_ratio': row['liabilities_to_assets'], 'share_basis_date': cutoff.isoformat(),
                       'financial_method': {'id': 'manual_declared_per_share_normalized_v1',
                                            'accounts_scope': row['scope'], 'source_ref': row['source_ref'],
                                            'version_basis': 'imported_publication_order_not_official_CVM_version',
                                            'original_share_basis_date': row['share_basis_date'],
                                            'normalization_multiplier': 1 / divisor,
                                            'fcf_absence_reason': 'financial_sector_declared_not_applicable' if fcf is None else None}})
    return output


def _candidate(ticker, identity, tables, bundle, cutoff):
    source = bundle['provenance']['source']
    declared = source['tickers'][ticker]
    if 'cvm' in declared and (declared['cvm'], declared['share_class']) != (identity['cvm'], identity['share_class']):
        _reject('declared_issuer_or_share_class_disagrees_with_B3')
    coverage = {table: _coverage(ticker, table, bundle, cutoff)
                for table in ('prices', 'payments', 'actions', 'fundamentals')}
    actions = _actions(tables['actions'], cutoff)
    prices = _prices(tables['prices'], source, actions, coverage['actions'], cutoff)
    history, ledger = _payments(ticker, tables['payments'], tables['future_payments'], source,
                                actions, coverage['payments'], coverage['actions'], cutoff)
    statements = _statements(tables['fundamentals'], identity, actions, coverage['actions'], cutoff)
    spec = (identity['cvm'], identity['share_class'], identity['sector_code'])
    if financial_features(statements, history, cutoff, spec) is None:
        _reject('current_financial_features_unavailable')
    cash_start = history.coverage_start
    training_since = max(shift_month(month_end(cash_start.year, cash_start.month), 12),
                         date.fromisoformat(coverage['fundamentals']['start']))
    financial_start = min(row['fiscal_end'] for row in statements)
    observations, observation_coverage = stock_observations(ticker, prices, history, statements, cutoff, spec=spec)
    observations = [row for row in observations if date.fromisoformat(row['as_of']) >= training_since
                    and row['fiscal_end'] >= financial_start]
    candidate = {**identity, 'statement_spec': spec, 'prices': prices, 'payments': history, 'ledger': ledger,
                 'future_payments': tables['future_payments'], 'share_actions': actions,
                 'independent_cash_matches': [], 'independent_price_matches': [],
                 'payment_coverage_start': coverage['payments']['start'],
                 'financial_basis_start': financial_start, 'training_since': training_since,
                 'payment_url': source['source_url'], 'price_url': source['source_url'],
                 'share_normalization': {'share_basis_date': cutoff.isoformat(),
                                         'price_source_basis': source['price_basis'],
                                         'cash_source_basis': source['cash_share_basis'],
                                         'price_output_basis': 'cutoff_share_units_no_dividend_reinvestment',
                                         'actions': actions, 'independently_reconciled': False,
                                         'calendar_basis': 'reported_payment_dates_not_ex_dates'},
                 'observations': observations, 'observation_coverage': observation_coverage}
    return candidate, spec, statements


def adapt_import_bundle(bundle: ImportBundle, b3_registry: Mapping[str, Mapping[str, Any]]) -> ProviderResult:
    """Adapt an unchanged load_import_bundle result; isolate exclusions per ticker."""
    cutoff = _cutoff(bundle['cutoff'])
    if not isinstance(b3_registry, Mapping):
        raise HistoricalProviderError('B3_registry_must_be_exact_ticker_map')
    provenance = bundle['provenance']
    source = provenance['source']
    if bundle['schema_version'] != 1 or provenance['cash_imputed'] is not False or provenance['share_adjustments_applied'] is not False:
        raise HistoricalProviderError('unmodified_validated_import_bundle_required')
    if source['price_basis'] not in ('raw', 'split_adjusted') or source['cash_share_basis'] not in ('record_date', 'adjusted'):
        raise HistoricalProviderError('unsupported_or_total_return_share_basis')
    for key, adjusted in (('price_basis', 'split_adjusted'), ('cash_share_basis', 'adjusted')):
        if source[key] == adjusted and source.get(key + '_date') != cutoff.isoformat():
            raise HistoricalProviderError('adjusted_share_basis_must_equal_cutoff')
    by_ticker = {ticker: {table: [] for table in ('prices', 'payments', 'actions', 'fundamentals', 'future_payments')}
                 for ticker in source['tickers']}
    for table in ('prices', 'payments', 'actions', 'fundamentals', 'future_payments'):
        for row in bundle[table]:
            if row['ticker'] not in by_ticker:
                raise HistoricalProviderError('unmodified_validated_import_bundle_required')
            by_ticker[row['ticker']][table].append(row)
    identities, exclusions = {}, []
    for ticker in sorted(by_ticker):
        try:
            identities[ticker] = _identity(ticker, b3_registry)
        except _Rejected as error:
            exclusions.append({'ticker': ticker, 'reason': str(error)})
    isin_counts = Counter(row['isin'] for row in identities.values())
    spec_counts = Counter((row['cvm'], row['share_class'], row['sector_code']) for row in identities.values())
    candidates, statements_by_spec = [], {}
    for ticker, identity in identities.items():
        spec = (identity['cvm'], identity['share_class'], identity['sector_code'])
        try:
            if isin_counts[identity['isin']] > 1 or spec_counts[spec] > 1:
                _reject('ambiguous_B3_instrument_identity')
            candidate, spec, statements = _candidate(ticker, identity, by_ticker[ticker], bundle, cutoff)
            candidates.append(candidate)
            statements_by_spec[spec] = statements
        except _Rejected as error:
            exclusions.append({'ticker': ticker, 'reason': str(error)})
    projection = {ticker: identity for ticker, identity in sorted(identities.items())}
    fingerprint = hashlib.sha256(json.dumps(projection, sort_keys=True, ensure_ascii=True,
                                           allow_nan=False, separators=(',', ':')).encode('utf8')).hexdigest()
    manifest = {'schema_version': 1, 'provider': 'manual_history_v1', 'status': 'adapted',
                'cutoff': cutoff.isoformat(), 'b3_registry_sha256': fingerprint,
                'identity_basis': 'caller_supplied_exact_B3_registry',
                'statement_grouping': 'exact_cvm_share_class_sector_code',
                'must_not_merge_official_versions': True, 'financially_validated': False,
                'independently_reconciled': False, 'bundle_provenance': provenance,
                'source_manifest': [{**entry, 'url': source['source_url'], 'retrieved_at': source['retrieved_at'],
                                     'role': 'manual_history_input'} for entry in provenance['input_files']],
                'candidate_count': len(candidates), 'excluded_count': len(exclusions)}
    return candidates, statements_by_spec, sorted(exclusions, key=lambda row: row['ticker']), manifest


def load_historical_candidates(folder: str | Path | None, cutoff: str | date,
                               b3_registry: Mapping[str, Mapping[str, Any]]) -> ProviderResult:
    """Optional file entry point. None means not configured, with no file access."""
    cutoff = _cutoff(cutoff)
    if folder is None:
        return [], {}, [], {'schema_version': 1, 'provider': 'manual_history_v1',
                            'status': 'not_configured', 'cutoff': cutoff.isoformat(),
                            'source_manifest': [], 'candidate_count': 0, 'excluded_count': 0}
    return adapt_import_bundle(load_import_bundle(folder, cutoff), b3_registry)
