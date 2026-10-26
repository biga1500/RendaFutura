"""Offline, isolated recheck of explicit price dates; never publishes data.

Cash rights and dated price observations are different evidence. A missing
quote date (including 31/12/9999) cannot borrow the cash record date. This
adapter preserves every cash row and records each unusable price separately.
Only BRSR5/CEEB5 have a reviewed integration entry point. All price thresholds,
the five-year window and actual observations come from the existing engine.
"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from .collect_market_data import normalize
from .selected_price_research import (END, START, Evidence, daily_input,
                                      encoded, offline, require, safe_path)
from .share_basis import b3_evidence, br_number, factor_after, reconcile_prices


MARKET = 'recommender/data/market/'
SNAPSHOT = 'snapshots/universe-20261025T193657-68d47408/'
AUDIT_SHA = '49a1c18211ed7a2414532ac5c3f542e1161f1dc6391134acc8994df22bd213f6'
PREPARATION_SHA = 'a13bd81399048baf891b5e548dfbd77d86fc3923baaf89e8a613acfbfc9be879'
PRIOR = 'raw/primary-research/selected-prices/report-f0f6e29d235e5f8853a538b09d56edc39047e55e964881a744e466f78d143785.json'
PRIOR_SHA = 'f0f6e29d235e5f8853a538b09d56edc39047e55e964881a744e466f78d143785'
CHAPTER = 'work/chapter4_revision_20261025/price_recovery_report.json'
CHAPTER_SHA = '018cae726a0ffdbbcc0e904056b28e3a8f95cf697cbdd42141010793b7b9dc7e'
INVENTORY = 'inventory/snapshots/20261025T194421-7b0a98ee/market.json'
INVENTORY_SHA = '264c4bc8a58caa6672467f9b3acfffd8937273ab1277bdb70c3a3b45facd5c51'
REPORT = 'work/pending_recheck_20261025/price-report.json'
REASONS = frozenset((
    'historical_quote_or_share_scale_disagrees_with_B3',
    'historical_quote_or_share_scale_disagrees_with_independent_source',
    'insufficient_independent_price_matches',
    'unverified_share_price_regime_requires_raw_history',
    'conflicting_B3_reference_quotes'))
REVIEWED = {
    'BRSR5': (1210, '92702067000196', 'PNA', 'BRBRSRACNPA6'),
    'CEEB5': (14524, '15139629000194', 'PNA', 'BRCEEBACNPA6'),
}


def explicit_reference_view(rows, share_class):
    """Keep cash intact; retain undated prices as observations without dates.

    Clear only the quote value in a private compatibility view. Both existing
    price consumers then ignore it, while b3_evidence still reads its cash.
    Actual dated conflicts and malformed dates remain blocking errors.
    """
    view, excluded = deepcopy(rows), []
    for index, row in enumerate(view):
        if (row.get('typeStock', '').strip() != share_class
                or normalize(row.get('corporateAction', '')) not in ('dividendo', 'jrs cap proprio')):
            continue
        quote = br_number(row.get('closingPricePriorExDate') or '0')
        require(math.isfinite(quote), 'invalid_B3_reference_quote')
        text = row.get('dateClosingPricePriorExDate')
        if text not in (None, '', '31/12/9999'):
            datetime.strptime(text, '%d/%m/%Y')
            continue
        if quote > 0:
            excluded.append({'row_index': index, 'original_row': deepcopy(rows[index]),
                'raw_quote_BRL': quote, 'quote_date': None,
                'reason': 'unavailable_quote_date_not_a_dated_price_observation',
                'record_date_not_promoted': row['lastDatePriorEx']})
        row['closingPricePriorExDate'] = None
    cash, quotes = b3_evidence(view, share_class)
    return {'view': view, 'cash': cash, 'quotes': quotes, 'undated_prices': excluded}


class Context:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.evidence = Evidence(self.repo)
        self.prior = self.evidence.json(MARKET + PRIOR, PRIOR_SHA)
        self.chapter = self.evidence.json(CHAPTER, CHAPTER_SHA)
        self.audit = self.evidence.json(MARKET + SNAPSHOT + 'candidate-source-audit.json', AUDIT_SHA)
        self.preparation = self.evidence.json(
            MARKET + SNAPSHOT + 'stock-universe-preparation.json', PREPARATION_SHA)
        self.sources = {r['file']: r for r in self.prior['sources']}
        self.records = {r['ticker']: r for r in self.prior['records']}

    def read_source(self, source):
        name = MARKET + source['file']
        body = self.evidence.read(name, source['sha256'])
        # Dates are copied only from receipts, never from filesystem metadata.
        self.evidence.sources[name].update({k: source[k] for k in
            ('url', 'retrieved_at', 'published_at', 'authority', 'provider') if k in source})
        return body

    def inputs(self, ticker):
        record = self.records[ticker]
        bodies = {name: self.read_source(self.sources[name]) for name in record['source_files']}
        detail = json.loads(bodies[record['source_detail_file']])
        require(int(detail['codeCVM']) == record['cvm'] and detail['cnpj'] == record['cnpj']
                and [r['isin'] for r in detail['otherCodes'] if r['code'] == ticker] == [record['isin']],
                'price_recheck_exact_identity_mismatch')
        # Prior receipts include pages read for other classes of the issuer.
        # Follow _cash_pages' exact-code preference instead of concatenating
        # complete duplicate issuer responses into one pagination sequence.
        codes = {r['code'] for r in detail['otherCodes']}
        alternate = next((code for code in [ticker, *sorted(codes)]
            if f'raw/expanded/{code}-b3-cash-1.json' in bodies), None)
        require(alternate is not None, 'price_recheck_cash_pages_missing')
        prefix = f'raw/expanded/{alternate}-b3-cash-'
        pages = sorted((name for name in bodies if name.startswith(prefix)),
                       key=lambda name: int(name[len(prefix):-5]))
        rows = []
        for page, name in enumerate(pages, 1):
            data = json.loads(bodies[name])
            require(data['page']['pageNumber'] == page
                    and data['page']['totalPages'] == len(pages), 'price_recheck_pagination_mismatch')
            rows.extend(data['results'])
            require(data['page']['totalRecords'] == json.loads(bodies[pages[0]])['page']['totalRecords'],
                    'price_recheck_record_count_mismatch')
        require(len(rows) == data['page']['totalRecords'], 'price_recheck_record_count_mismatch')
        body = bodies[record['source_price_file']]
        daily, actions = daily_input(body, ticker)
        return record, rows, body, daily, actions, pages


def reviewed_inputs(repo, identity, cutoff=END, *, context=None):
    """Hash-pinned integration result for two exact classes, in memory only.

    Consumers can use cash_rows with their existing cash and price functions.
    These returned prices pass reconcile_prices, but are not model admissions.
    No original row or cached provider payload is modified.
    """
    ticker = identity.get('ticker')
    require(ticker in REVIEWED and cutoff == END, 'outside_reviewed_price_recheck_scope')
    cvm, cnpj, share, isin = REVIEWED[ticker]
    require(identity.get('cvm') == cvm and identity.get('share_class') == share
            and identity.get('isin') == isin
            and (identity.get('cnpj') is None or identity['cnpj'] == cnpj),
            'price_recheck_exact_identity_mismatch')
    context = context or Context(repo)
    record, rows, body, _, actions, pages = context.inputs(ticker)
    typed = explicit_reference_view(rows, share)
    prices, matches, mode = reconcile_prices(body, ticker, actions, typed['quotes'], cutoff)
    return {'ticker': ticker, 'original_cash_rows': rows, 'cash_rows': typed['view'],
            'cash_amounts': typed['cash'], 'reference_quotes': typed['quotes'],
            'undated_prices': typed['undated_prices'], 'prices': prices,
            'price_matches': matches, 'price_mode': mode, 'actions': actions,
            'cash_files': pages, 'price_file': record['source_price_file']}


def replay_source_candidate(context, identity):
    """Run existing _one with a private parsed cache and original raw bytes."""
    from .inventory_candidates import _Cache, _one

    proof = reviewed_inputs(context.repo, identity, context=context)
    inventory = context.evidence.json(MARKET + INVENTORY, INVENTORY_SHA)
    rows = [r for r in inventory['stocks'] if r['ticker'] == identity['ticker']]
    require(len(rows) == 1 and all(rows[0][k] == identity[k] for k in ('cvm', 'share_class')),
            'price_recheck_inventory_identity_mismatch')

    class ReplayCache(_Cache):
        def read(self, relative, role):
            body, observed = super().read(relative, role)
            if role == 'payments':
                stamp = self.receipts[relative].get('retrieved_at')
                require(stamp is not None, 'payment_retrieval_date_unproven_no_mtime_substitution')
                observed = datetime.fromisoformat(stamp)
                require(observed.utcoffset() is not None, 'payment_retrieval_timezone_unproven')
                self.bodies[relative] = body, observed
            return body, observed

    cache = ReplayCache(context.repo / MARKET, inventory)
    offset = 0
    for name in proof['cash_files']:
        data = cache.json(name, 'independent_cash')
        size = len(data['results'])
        require(data['results'] == proof['original_cash_rows'][offset:offset + size],
                'price_recheck_original_cash_rows_mismatch')
        cache.parsed[name] = {**data, 'results': deepcopy(proof['cash_rows'][offset:offset + size])}
        offset += size
    checks = {}
    try:
        candidate = _one(rows[0], cache, END, checks)
        outcome = {'source_candidate_recovered': True, 'remaining_reason': None,
            'ledger_rows': len(candidate['ledger']),
            'independent_price_matches': len(candidate['independent_price_matches']),
            'independent_cash_matches': len(candidate['independent_cash_matches']),
            'readiness_scope': candidate['readiness_scope']}
    except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
        outcome = {'source_candidate_recovered': False, 'remaining_reason': str(error)}
    for source in cache.all_receipts():
        context.read_source(source)
    return {**outcome, 'checks': checks, 'financial_validation_performed': False,
            'model_admission': False, 'published': False,
            'payment_observation_basis': 'existing_source_retrieved_at_not_file_mtime'}


def _trial(body, ticker, actions, rows, share):
    try:
        _, quotes = b3_evidence(rows, share)
        prices, matches, mode = reconcile_prices(body, ticker, actions, quotes, END)
        return {'passed': True, 'independent_dates': len(matches), 'mode': mode,
                'required_months': sum(START <= d <= END for d in prices)}
    except ValueError as error:
        return {'passed': False, 'reason': str(error)}


def _record(context, pending):
    ticker = pending['ticker']
    if ticker in ('ITSA3', 'ITSA4'):
        return {'ticker': ticker, 'active_reason': pending['reason'],
            'action': 'retain_prior_primary_price_conflicts_without_reopening_exhausted_branch',
            'prior_report': CHAPTER, 'price_case_resolved': False,
            'reexecuted': False}
    record, rows, body, daily, actions, _ = context.inputs(ticker)
    baseline = _trial(body, ticker, actions, rows, record['share_class'])
    excluded, conflicts, mismatches, typed = [], [], [], None
    try:
        typed = explicit_reference_view(rows, record['share_class'])
        excluded = typed['undated_prices']
        trial = _trial(body, ticker, actions, typed['view'], record['share_class'])
        for day, quote in sorted(typed['quotes'].items()):
            if not START <= day <= END:
                continue
            value, factor = daily.get(day), factor_after(day, actions, END)
            raw_ok = value is not None and math.isclose(value, quote, rel_tol=.006, abs_tol=.015)
            adjusted_ok = value is not None and math.isclose(value, quote / factor, rel_tol=.006, abs_tol=.015)
            if not (raw_ok or adjusted_ok):
                mismatches.append({'date': day.isoformat(), 'cached_close_BRL': value,
                    'literal_reference_close_BRL': quote, 'reported_factor_not_primary_proof': factor,
                    'split_expected_BRL': quote / factor})
    except ValueError as error:
        trial = {'passed': False, 'reason': str(error)}
        conflicts = record['diagnostic']['conflicting_explicit_quote_dates']
    resolved = trial['passed']
    action = ('core_parser_already_resolves_price_case' if resolved and baseline['passed'] else
              'apply_explicit_date_price_parser_in_isolated_adapter' if resolved else
              'retain_real_dated_price_conflict' if mismatches else
              'retain_distinct_date_minimum' if trial.get('reason') == 'insufficient_independent_price_matches' else
              'retain_missing_raw_share_regime')
    outcome = {'ticker': ticker, 'cvm': record['cvm'], 'isin': record['isin'],
        'share_class': record['share_class'], 'active_reason': pending['reason'],
        'baseline': baseline, 'explicit_date_trial': trial, 'action': action,
        'price_case_resolved': resolved, 'reexecuted': True,
        'cash_rows_preserved': True, 'original_files_modified': False,
        'undated_prices_preserved_as_undated': excluded,
        'explicit_date_mismatches': mismatches, 'explicit_date_conflicts': conflicts,
        'reported_actions_not_new_primary_proof': actions,
        'remaining_unobserved_regimes': record['diagnostic']['missing_explicitly_observed_regimes']
            if not trial['passed'] else [],
        'source_files': [MARKET + f for f in record['source_files']]}
    if ticker in REVIEWED:
        outcome['source_replay'] = replay_source_candidate(context, record)
    if ticker == 'LREN3':
        focus = next(r for r in context.prior['focused_candidate_evidence_closures'] if r['ticker'] == ticker)
        for action_row in focus['primary_actions']:
            context.read_source(action_row['receipt'])
        outcome['primary_action_context'] = {
            'verification': 'reuse_prior_primary_reading_after_original_body_hash_checks',
            'actions': focus['primary_actions'],
            'complete_Yahoo_comparison_already_audited': True,
            'material_conflicts': focus['material_daily_source_conflicts'],
            'action_ratios_alone_resolve_price_conflicts': False}
    return outcome


def build_report(repo):
    with offline():
        context = Context(repo)
        protected = [MARKET + 'current.json', MARKET + 'inventory/current.json',
            'recommender/broad_stocks.py', 'recommender/inventory_candidates.py',
            'recommender/price_basis_research.py', 'recommender/share_basis.py']
        protected += [p.relative_to(context.repo).as_posix() for p in
                      (context.repo / MARKET / SNAPSHOT).iterdir() if p.is_file()]
        before = {name: hashlib.sha256(context.evidence.read(name)).hexdigest() for name in protected}
        pending = [r for r in context.audit['pending'] if r['reason'] in REASONS]
        require(len({r['ticker'] for r in pending}) == len(pending), 'duplicate_price_recheck_pending')
        results = [_record(context, r) for r in sorted(pending, key=lambda r: r['ticker'])]
        for name, source in context.evidence.sources.items():
            require(hashlib.sha256(safe_path(context.repo, name).read_bytes()).hexdigest() == source['sha256'],
                    'price_recheck_input_changed_during_run')
        recovered = [r['ticker'] for r in results if r['price_case_resolved']]
        source_recovered = [r['ticker'] for r in results if r.get('source_replay', {}).get('source_candidate_recovered')]
        return {'schema_version': 1, 'task': 'offline_price_parser_and_share_scale_recheck',
            'snapshot': SNAPSHOT.rstrip('/'), 'window_start': START.isoformat(), 'cutoff': END.isoformat(),
            'required_monthly_prices': 61, 'required_monthly_returns': 60,
            'prior_work_read': [CHAPTER, MARKET + PRIOR],
            'new_work': 'Reexecute price gate with explicit quote dates and run two private source-builder trials. Missing quote dates never borrow cash-right dates.',
            'parser_failure': {'functions': ['share_basis.b3_evidence', 'broad_stocks.check_b3_prices'],
                'expression': 'dateClosingPricePriorExDate or lastDatePriorEx',
                'sentinel_failure': 'Different undated quotes collide on 9999-12-31.',
                'cash_and_quote_dates_are_distinct': True},
            'integration_entry_point': 'recommender.price_recheck.reviewed_inputs(repo, exact_identity)',
            'integration_status': 'explicit_quote_dates_enabled_in_core_independent_checks_retained',
            'scope_count': len(results), 'pending_reason_counts': dict(Counter(r['reason'] for r in pending)),
            'price_cases_resolved': recovered, 'price_cases_resolved_count': len(recovered),
            'source_candidates_recovered': source_recovered,
            'source_candidates_recovered_count': len(source_recovered),
            'model_admissions': 0, 'financial_validation_performed': False,
            'active_base_changed': False, 'thresholds_changed': False,
            'network_requests': 0, 'denied_origin_retries': 0, 'prices_or_payments_invented': 0,
            'fitted_share_factors': 0, 'mtime_used_as_publication_or_retrieval': False,
            'records': results, 'protected_files_before_and_after_sha256': before,
            'all_read_inputs_hash_verified_unchanged': True,
            'sources': sorted(context.evidence.sources.values(), key=lambda r: r['file'])}


def write_report(repo, report):
    path = safe_path(repo, REPORT)
    body = encoded(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'existing_price_report_will_not_be_overwritten')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    report = build_report(args.repo)
    path = write_report(args.repo, report) if args.report else None
    print(json.dumps({'report': str(path) if path else None,
        'price_cases_resolved': report['price_cases_resolved'],
        'source_candidates_recovered': report['source_candidates_recovered'],
        'model_admissions': 0}, ensure_ascii=True))


if __name__ == '__main__':
    main()
