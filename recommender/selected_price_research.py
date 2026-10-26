"""Offline diagnostics for the positive-DY selection; never activates a dataset.

Original observations, date conflicts and rejected hypotheses remain evidence.
Only this module's content-addressed reports may be written. No acquisitions,
factor fitting, nearest-session matches, provider JSON creation or model gates.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import zipfile

from .collect_market_data import month_end, normalize, shift_month
from .dividend_scope import allowed_tickers, load_scope
from .share_basis import b3_evidence, br_number, factor_after, reconcile_prices


CACHE = 'raw/primary-research/selected-prices'
SNAPSHOT = 'snapshots/universe-20261025T124510-fee60f24'
INVENTORY = 'inventory/snapshots/20261025T115301-eca97bfa/market.json'
START, END = date(2021, 9, 30), date(2026, 9, 30)
REASONS = frozenset((
    'historical_quote_or_share_scale_disagrees_with_B3',
    'unverified_share_price_regime_requires_raw_history',
    'insufficient_independent_price_matches', 'conflicting_B3_reference_quotes'))
FOCUS = ('LREN3', 'BMEB4', 'DEXP3')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('ascii')


def safe_path(root, relative):
    require(isinstance(relative, str) and relative and '\\' not in relative
            and ':' not in relative and not PurePosixPath(relative).is_absolute()
            and '..' not in relative.split('/'), 'unsafe_selected_price_path')
    root = Path(root).resolve()
    path = root / relative
    require(path.resolve().is_relative_to(root)
            and not any(p.is_symlink() for p in (path, *path.parents)
                        if p.is_relative_to(root)), 'unsafe_selected_price_path')
    return path


class Evidence:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}

    def read(self, relative, expected=None):
        path = safe_path(self.root, relative)
        require(path.stat().st_size <= 30_000_000, 'selected_price_input_too_large')
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        require(expected is None or expected == digest, 'selected_price_input_hash_mismatch')
        old = self.sources.get(relative)
        require(old is None or old['sha256'] == digest, 'selected_price_input_changed_during_read')
        self.sources[relative] = {'file': relative, 'sha256': digest, 'bytes': len(body),
                                  'access': 'existing_cache_read_only'}
        return body

    def json(self, relative, expected=None):
        return json.loads(self.read(relative, expected))

    def ref(self, relative):
        return dict(self.sources[relative])


@contextmanager
def offline():
    """Make accidental acquisition through existing helpers fail before I/O."""
    def denied(*args, **kwargs):
        raise AssertionError('selected_price_research_network_forbidden')
    with patch('socket.socket.connect', denied), patch('socket.socket.connect_ex', denied), \
            patch('socket.create_connection', denied), patch('socket.getaddrinfo', denied), \
            patch('urllib.request.OpenerDirector.open', denied):
        yield


def selected_pending(audit, allowed):
    require(allowed is not None and len(allowed) == 262,
            'required_positive_DY_selected262_scope_unavailable')
    result = [r for r in audit['pending'] if r['ticker'] in allowed and r['reason'] in REASONS]
    require(len({r['ticker'] for r in result}) == len(result), 'duplicate_selected_pending_code')
    require('PSSA3' not in {r['ticker'] for r in result}, 'solved_PSSA_must_not_be_researched')
    return sorted(result, key=lambda r: r['ticker'])


def daily_input(body, ticker):
    result = json.loads(body)['chart']['result'][0]
    meta = result['meta']
    require(meta.get('symbol') == ticker + '.SA' and meta.get('currency') == 'BRL'
            and meta.get('dataGranularity') == '1d', 'exact_daily_identity_or_frequency_mismatch')
    stamps, closes = result['timestamp'], result['indicators']['quote'][0]['close']
    require(len(stamps) == len(closes), 'daily_price_length_mismatch')
    days = [datetime.fromtimestamp(stamp, timezone.utc).date() for stamp in stamps]
    require(len(days) == len(set(days)), 'duplicate_daily_price_dates')
    daily = {d: float(v) for d, v in zip(days, closes) if v is not None and d <= END}
    require(all(math.isfinite(v) and v > 0 for v in daily.values()), 'invalid_daily_price')
    actions = []
    for event in result.get('events', {}).get('splits', {}).values():
        ratio = float(event['numerator']) / float(event['denominator'])
        require(math.isfinite(ratio) and ratio > 0, 'invalid_reported_action_ratio')
        actions.append({'date': datetime.fromtimestamp(event['date'], timezone.utc).date().isoformat(),
                        'ratio': ratio})
    require(all(date.fromisoformat(a['date']) <= END for a in actions), 'post_cutoff_action')
    return daily, sorted(actions, key=lambda a: a['date'])


def brday(value):
    return datetime.strptime(value, '%d/%m/%Y').date()


def reference_rows(rows, share):
    observations, groups, unknown = [], defaultdict(list), []
    for index, row in enumerate(rows):
        if (row.get('typeStock', '').strip() != share
                or normalize(row.get('corporateAction', '')) not in ('dividendo', 'jrs cap proprio')):
            continue
        quote = br_number(row.get('closingPricePriorExDate') or '0')
        require(math.isfinite(quote), 'nonfinite_independent_reference')
        if quote <= 0:
            continue
        record = brday(row['lastDatePriorEx'])
        text = row.get('dateClosingPricePriorExDate')
        explicit = brday(text) if text and text != '31/12/9999' else None
        observation = {'row_index': index, 'original_row': row,
            'raw_close_BRL': quote, 'record_date': record.isoformat(),
            'explicit_quote_date': explicit.isoformat() if explicit else None,
            'parent_fallback_date': (explicit or (brday(text) if text else record)).isoformat(),
            'accepted_as_explicit_dated_anchor': explicit is not None and START <= explicit <= END,
            'record_date_substituted_by_sidecar': False}
        observations.append(observation)
        if explicit is None:
            unknown.append(observation)
        else:
            groups[explicit].append(observation)
    conflicts = []
    for day, members in sorted(groups.items()):
        if any(not math.isclose(a['raw_close_BRL'], b['raw_close_BRL'], rel_tol=.001, abs_tol=.015)
               for a in members for b in members):
            conflicts.append({'date': day.isoformat(), 'observations': members})
    return observations, groups, unknown, conflicts


def diagnose(body, ticker, rows, share):
    daily, actions = daily_input(body, ticker)
    observations, groups, unknown, conflicts = reference_rows(rows, share)
    fallback_groups = defaultdict(list)
    for observation in observations:
        day = date.fromisoformat(observation['parent_fallback_date'])
        value = daily.get(day)
        factor = factor_after(day, actions, END)
        observation['parent_fallback_comparison_not_accepted_evidence'] = {
            'date': day.isoformat(), 'daily_close_BRL': value, 'reported_factor': factor,
            'split_adjusted_expected_BRL': observation['raw_close_BRL'] / factor,
            'split_adjusted_match': value is not None and math.isclose(
                value, observation['raw_close_BRL'] / factor, rel_tol=.006, abs_tol=.015)}
        fallback_groups[day].append(observation)
    parent_conflicts = [{'date': day.isoformat(), 'observations': members,
        'is_unavailable_date_sentinel': day == date(9999, 12, 31)}
        for day, members in sorted(fallback_groups.items())
        if any(not math.isclose(a['raw_close_BRL'], b['raw_close_BRL'], rel_tol=.001, abs_tol=.015)
               for a in members for b in members)]
    anchors = []
    for day, members in sorted(groups.items()):
        if not START <= day <= END:
            continue
        value = daily.get(day)
        factor = factor_after(day, actions, END)
        anchors.append({'date': day.isoformat(), 'daily_close_BRL': value,
            'reported_cumulative_share_factor': factor,
            'raw_reference_values_BRL': sorted({r['raw_close_BRL'] for r in members}),
            'observations': members,
            'all_raw_values_match': value is not None and all(math.isclose(
                value, r['raw_close_BRL'], rel_tol=.006, abs_tol=.015) for r in members),
            'all_split_adjusted_values_match': value is not None and all(math.isclose(
                value, r['raw_close_BRL'] / factor, rel_tol=.006, abs_tol=.015) for r in members)})
    monthly = {}
    for day, value in sorted(daily.items()):
        monthly[month_end(day.year, day.month)] = (day, value)
    months, regimes = [], {}
    observed_factors = {round(r['reported_cumulative_share_factor'], 12) for r in anchors}
    for i in reversed(range(61)):
        month = shift_month(END, -i)
        row = {'month': month.isoformat(), 'observed_on': None, 'stored_close_BRL': None}
        if month in monthly:
            day, value = monthly[month]
            factor = round(factor_after(day, actions, END), 12)
            row.update(observed_on=day.isoformat(), stored_close_BRL=value,
                       reported_cumulative_share_factor=factor)
            regime = regimes.setdefault(factor, {'reported_factor': factor, 'monthly_dates': [],
                'explicit_independent_quote_dates': [], 'evidence_status': 'reported_actions_only'})
            regime['monthly_dates'].append(day.isoformat())
        months.append(row)
    for factor, regime in regimes.items():
        regime['explicit_independent_quote_dates'] = [r['date'] for r in anchors
            if round(r['reported_cumulative_share_factor'], 12) == factor]
    try:
        _, parent_quotes = b3_evidence(rows, share)
        _, matches, mode = reconcile_prices(body, ticker, actions, parent_quotes, END)
        baseline = {'status': 'passed_unchanged_price_gate', 'mode': mode, 'matches': len(matches)}
    except ValueError as error:
        baseline = {'status': 'blocked_by_unchanged_price_gate', 'reason': str(error)}
    # This reviewed view handles only old sentinel dates, as in the prior review.
    # It cannot supply a missing date, turn a record date into a quote or choose a conflict.
    from .price_basis_research import _reference_view
    corrections = []
    try:
        cleaned, corrections = _reference_view(rows, share, START, END)
        _, quotes = b3_evidence(cleaned, share)
        _, matches, mode = reconcile_prices(body, ticker, actions, quotes, END)
        sentinel_trial = {'status': 'passed_unchanged_price_gate', 'matches': len(matches), 'mode': mode}
    except ValueError as error:
        sentinel_trial = {'status': 'blocked_by_unchanged_price_gate', 'reason': str(error)}
    return {'reported_Yahoo_actions_not_primary_verified': actions,
        'baseline_replay': baseline, 'old_sentinel_only_trial': sentinel_trial,
        'old_sentinel_corrections_applied_to_trial_only': corrections,
        'reference_observations': observations, 'unknown_quote_date_rows': unknown,
        'parent_reference_conflicts_including_sentinels': parent_conflicts,
        'conflicting_explicit_quote_dates': conflicts, 'explicit_anchors': anchors,
        'explicit_anchor_count': len(anchors), 'minimum_explicit_anchor_deficit': max(0, 4 - len(anchors)),
        'all_reference_rows_raw_compatible': bool(anchors) and all(r['all_raw_values_match'] for r in anchors),
        'all_reference_rows_split_compatible': bool(anchors) and all(r['all_split_adjusted_values_match'] for r in anchors),
        'required_61_monthly_anchors': months,
        'missing_months': [r['month'] for r in months if r['observed_on'] is None],
        'share_regimes': list(regimes.values()),
        'missing_explicitly_observed_regimes': [v for k, v in regimes.items() if k not in observed_factors],
        'latest_observed_date': max(daily).isoformat(),
        'date_policy': 'literal quote dates only; parent record-date fallback separately exposed; no nearest dates',
        'price_basis_established': False, 'source_candidate_closed': False, 'financially_validated': False}


def next_work(record):
    diagnostic, ticker = record['diagnostic'], record['ticker']
    tasks = []
    missing = diagnostic['missing_explicitly_observed_regimes']
    for regime in missing:
        tasks.append({'task': 'verify primary action chain, then find exact-code raw price observations',
            'ticker': ticker, 'reported_factor_not_an_approved_correction': regime['reported_factor'],
            'required_regime_first_month_end_session': regime['monthly_dates'][0],
            'required_regime_last_month_end_session': regime['monthly_dates'][-1],
            'safe_source': 'already cached issuer/CVM action body and native exact-code daily export',
            'network_authorized_in_this_phase': False})
    mismatches = [a for a in diagnostic['explicit_anchors']
                  if not a['all_split_adjusted_values_match']]
    if mismatches:
        tasks.append({'task': 'resolve exact-date raw closing-price and price-regime provenance',
            'ticker': ticker, 'dates_and_values': mismatches,
            'safe_source': 'cached issuer native daily export or licensed raw archive with revision/closing-type metadata',
            'factor_fitting_or_replacing_single_dates_allowed': False})
    if diagnostic['minimum_explicit_anchor_deficit']:
        tasks.append({'task': 'supply independent exact-code observations to reach unchanged four-date minimum',
            'ticker': ticker, 'additional_distinct_dates_minimum': diagnostic['minimum_explicit_anchor_deficit'],
            'required_range': [START.isoformat(), END.isoformat()],
            'safe_source': 'native issuer/vendor historical export in existing cache; no class substitution'})
    if diagnostic['unknown_quote_date_rows']:
        tasks.append({'task': 'obtain literal quote dates for undated reference values before using them as anchors',
            'ticker': ticker, 'raw_values_and_right_dates': [
                {'raw_close_BRL': r['raw_close_BRL'], 'record_date_not_quote_date': r['record_date'],
                 'source_row': r['row_index']} for r in diagnostic['unknown_quote_date_rows']],
            'record_date_fallback_is_not_new_price_evidence': True})
    return tasks


def register_tree(evidence, value):
    """Register raw dependencies named by existing acquisition proof receipts."""
    if isinstance(value, dict):
        name, digest = value.get('file'), value.get('sha256')
        if isinstance(name, str) and isinstance(digest, str):
            evidence.read(name, digest)
            evidence.sources[name].update({k: value[k] for k in ('url', 'retrieved_at', 'authority') if k in value})
        for child in value.values():
            register_tree(evidence, child)
    elif isinstance(value, list):
        for child in value:
            register_tree(evidence, child)


def primary_catalog(evidence, identities):
    """Index metadata is a source-work queue, never an action ratio or ex-date."""
    from .issuer_raw_quote_research import IPE_PINS
    from .alternate_issuer_history_research import Reader
    expected = {r['cvm']: r for r in identities}
    public_receipts = list(Reader(evidence.root).receipts())
    evidence.public_receipts = public_receipts
    selected, by_url = [], {}
    files = [p.relative_to(evidence.root).as_posix() for p in (evidence.root / 'raw').rglob('*')
             if p.is_file() and p.suffix.lower() in ('.pdf', '.bin', '.html', '.htm')]
    for year, digest in IPE_PINS.items():
        relative = 'raw/primary-research/cvm-documents/index-%s-%s.zip' % (year, digest)
        body = evidence.read(relative, digest)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            member = 'ipe_cia_aberta_%s.csv' % year
            require(archive.namelist().count(member) == 1
                    and archive.getinfo(member).file_size <= 100_000_000, 'unsafe_CVM_index_table')
            rows = list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
        for line, row in enumerate(rows, 2):
            cvm = int(row['Codigo_CVM'])
            if cvm not in expected:
                continue
            identity = expected[cvm]
            require(re.sub(r'\D', '', row['CNPJ_Companhia']) == identity['cnpj'], 'CVM_index_CNPJ_mismatch')
            query = parse_qs(urlsplit(row['Link_Download']).query)
            sequence, = query['numSequencia']
            version, = query['numVersao']
            require(int(version) == int(row['Versao']), 'CVM_index_URL_version_mismatch')
            record = {'ticker': identity['ticker'], 'cvm': cvm, 'cnpj': identity['cnpj'],
                'sequence': sequence, 'version': int(version), 'index_row': row,
                'csv_line': line, 'member': member, 'index_source': evidence.ref(relative),
                'url': row['Link_Download'], 'published_at': row['Data_Entrega'],
                'after_cutoff': row['Data_Entrega'] > END.isoformat(),
                'ratio_or_ex_date_proven_by_index': False}
            by_url[row['Link_Download']] = record
            text = normalize(' '.join(row[k] for k in ('Assunto', 'Tipo', 'Categoria')))
            if not any(w in text for w in ('bonifica', 'desdobr', 'grupamento')):
                continue
            pattern = re.compile(r'(?<!\d)' + sequence + r'(?!\d)')
            record['matching_body_filenames_unverified'] = [f for f in files if pattern.search(Path(f).name)]
            record['matching_public_content_receipts'] = [r for r in public_receipts
                if r['url'] == row['Link_Download']]
            register_tree(evidence, record['matching_public_content_receipts'])
            record['native_action_verified'] = False
            selected.append(record)
    for record in selected:
        row = record['index_row']
        versions = [r['version'] for r in by_url.values() if r['cvm'] == record['cvm']
            and all(r['index_row'][k] == row[k] for k in ('Data_Referencia', 'Categoria', 'Tipo', 'Especie'))]
        record['latest_version_in_same_index_context'] = max(versions)
        record['latest_version_context_eligible'] = record['version'] == max(versions)
    return selected, by_url


def verify_action_index(receipt, index_rows, cvm, cnpj):
    require(receipt['url'] in index_rows, 'action_document_not_in_cached_index')
    record = index_rows[receipt['url']]
    require(record['cvm'] == cvm and record['cnpj'] == cnpj and not record['after_cutoff'],
            'action_document_primary_identity_or_cutoff_mismatch')
    row = record['index_row']
    versions = [r['version'] for r in index_rows.values() if r['cvm'] == cvm
        and all(r['index_row'][k] == row[k] for k in ('Data_Referencia', 'Categoria', 'Tipo', 'Especie'))]
    require(record['version'] == max(versions), 'superseded_primary_action_document')
    return record


def lren_closure(evidence, record, rows, index_rows):
    from . import issuer_quote_extension_research as prior

    ticker = record['ticker']
    require((ticker, record['cvm'], record['isin'], record['share_class']) ==
            ('LREN3', 8133, 'BRLRENACNOR1', 'ON'), 'LREN_exact_identity_mismatch')
    history, receipt, schema = prior.pinned_history(evidence.root, ticker)
    actions = prior.primary_actions(evidence.root, ticker)
    register_tree(evidence, [receipt, actions])
    evidence.read(prior.FCA, prior.FCA_SHA)
    identity = prior.security_identity(evidence.root, ticker)
    index_checks = [verify_action_index(a['receipt'], index_rows, 8133, '92754738000162') for a in actions]
    chain = [{'date': a['ex_date'], 'ratio': float(a['total_factor_decimal'])} for a in actions]
    observed = record['diagnostic']['reported_Yahoo_actions_not_primary_verified']
    require([a for a in observed if START < date.fromisoformat(a['date']) <= END] == chain,
            'primary_and_Yahoo_action_chain_disagree')
    reference = prior.reference_comparison(evidence.root, ticker, history)
    daily = prior.daily_comparison(evidence.root, ticker, history, actions)
    native = prior.native_basis_checks(history, actions)
    native_quotes = {date.fromisoformat(r['date']): float(r['closed']) for r in history
                     if START <= date.fromisoformat(r['date']) <= END}
    _, quotes = b3_evidence(rows, record['share_class'])
    combined = dict(native_quotes)
    conflicts = []
    for day, value in quotes.items():
        if day in combined and not math.isclose(value, combined[day], rel_tol=.001, abs_tol=.015):
            conflicts.append({'date': day.isoformat(), 'cached_B3_close': value,
                              'native_vendor_close': combined[day]})
        combined[day] = value
    body = evidence.read(record['source_price_file'])
    try:
        prices, matches, mode = reconcile_prices(body, ticker, chain, combined, END)
        trial = {'status': 'passed_unchanged_price_gate', 'independent_dates': len(matches),
                 'required_months': sum(START <= m <= END for m in prices), 'mode': mode}
    except ValueError as error:
        trial = {'status': 'blocked_by_unchanged_price_gate', 'reason': str(error)}
    substantive = [r for r in daily['daily_strict_mismatches']
                   if not r['within_existing_point_six_percent_band']]
    monthly_dates = {r['observed_on'] for r in record['diagnostic']['required_61_monthly_anchors']}
    for conflict in daily['daily_strict_mismatches']:
        conflict['used_as_required_month_end'] = conflict['date'] in monthly_dates
    return {'ticker': ticker, 'status': 'blocked', 'priority_basis':
        'cached native exact-code daily history and primary action bodies cover the missing early regime',
        'exact_FCA_identity': identity, 'native_history_source': receipt, 'native_history_schema': schema,
        'primary_actions': actions, 'latest_primary_index_checks': index_checks,
        'primary_action_chain_verified': True, 'cached_B3_comparison': reference,
        'native_unit_checks': native, 'all_daily_source_differences': daily['daily_strict_mismatches'],
        'material_daily_source_conflicts': substantive,
        'overlapping_native_B3_conflicts': conflicts,
        'unchanged_gate_trial_using_all_native_dates': trial,
        'independent_native_dates_in_model_window': len(native_quotes),
        'all_daily_differences_retained': True, 'favorable_date_subset_selected': False,
        'source_candidate_closed': False, 'financially_validated': False,
        'remaining_blockers': ['unresolved_exact_date_native_vendor_vs_cached_Yahoo_close_disagreements'],
        'next_safe_source_work': [{'task': 'resolve native closing-type/revision provenance for every listed conflict',
            'dates': [r['date'] for r in daily['daily_strict_mismatches']],
            'material_dates': [r['date'] for r in substantive],
            'native_raw_month_end_conflicts': [r['date'] for r in substantive if r['date'] in monthly_dates],
            'source': 'cached vendor revision/closing-type records or separately authorized native issuer export',
            'no_network_in_this_phase': True}],
        'provider': 'Enfoque via issuer-published MZ widget',
        'ultimate_upstream_independence_verified': False}


def focus_closures(evidence, records, identities, cash_rows):
    from .issuer_raw_quote_research import records as receipts

    catalog, index_rows = primary_catalog(evidence, identities)
    result = []
    for ticker in FOCUS:
        record = next(r for r in records if r['ticker'] == ticker)
        if ticker == 'LREN3':
            result.append(lren_closure(evidence, record, cash_rows[ticker], index_rows))
            continue
        receipt, = [r for r in receipts(evidence.root, 'response')
                     if r['ordinal'] == (6 if ticker == 'BMEB4' else 13)]
        require(receipt['ticker'] == ticker, 'focus_original_receipt_identity_mismatch')
        register_tree(evidence, receipt)
        body = evidence.read(receipt['file'], receipt['sha256'])
        text = body.decode('utf-8')
        discovered_links = []
        if ticker == 'BMEB4':
            require(set(re.findall(r"tickers:\s*'([A-Z0-9,]+)'", text)) == {'BMEB3'},
                    'cached_BMEB_quote_configuration_changed')
            from lxml import html
            tree = html.fromstring(body)
            discovered_links = [node.get('data-url') for node in tree.xpath('//*[@data-url]')
                                if ''.join(node.itertext()).strip() == ticker and node.get('data-url')]
            discovery = 'native published widget configures BMEB3 only; BMEB4 not substituted'
        else:
            require(re.search(r'''stockInfoId\s*[:=]\s*(["'])\1''', text) is not None,
                    'cached_DEXP_empty_widget_configuration_changed')
            discovery = 'cached issuer homepage has empty stockInfoId; no exact-code history discovered there'
        supplement_file = 'raw/expanded/b3-supplement-%s.json' % ticker[:4]
        supplement, = evidence.json(supplement_file)
        require(int(supplement['codeCVM']) == record['cvm'], 'focus_supplement_CVM_mismatch')
        raw_actions = [a for a in supplement['stockDividends'] if a['isinCode'] == record['isin']
                       and START <= brday(a['lastDatePrior']) <= END]
        require(all(a['assetIssued'] == record['isin'] for a in raw_actions), 'cross_security_action')
        documents = [r for r in catalog if r['ticker'] == ticker]
        result.append({'ticker': ticker, 'status': 'blocked', 'priority_basis':
            'all existing dated reference prices agree; exactly one monthly share regime lacks independent observations',
            'source_candidate_closed': False, 'financially_validated': False,
            'original_cached_discovery': discovery, 'discovery_source': receipt,
            'literal_exact_code_links_discovered_in_original_body_not_requested': discovered_links,
            'raw_cached_exchange_action_rows_not_primary_CVM_proof': raw_actions,
            'raw_action_source': evidence.ref(supplement_file),
            'reported_Yahoo_actions_not_primary_verified': record['diagnostic']['reported_Yahoo_actions_not_primary_verified'],
            'native_CVM_action_index_candidates': documents,
            'native_action_ratio_or_ex_date_verified': False,
            'cached_body_search_scope': 'all raw PDF/bin/HTML names with exact sequence boundaries; prior discovery receipt body also inspected',
            'remaining_blockers': ['native_CVM_action_body_ratio_and_ex_date_not_verified',
                                   'independent_exact_code_raw_quotes_missing_for_one_monthly_regime'],
            'next_safe_source_work': record['next_safe_source_work'] + [{
                'task': 'review latest exact CVM/CNPJ native action bodies before applying any ratio',
                'sequences_to_review': [r['sequence'] for r in documents if r['latest_version_context_eligible']],
                'index_subject_is_not_ratio_evidence': True, 'no_network_in_this_phase': True}]})
        if discovered_links:
            cached_tabs = [r for r in evidence.public_receipts if r['url'] in discovered_links]
            register_tree(evidence, cached_tabs)
            result[-1]['matching_existing_public_BMEB4_tab_receipts'] = cached_tabs
            result[-1]['next_safe_source_work'].append({
                'task': 'locate an existing cached body for this literal BMEB4 tab; acquisition needs a separately authorized phase',
                'literal_discovered_URLs': discovered_links,
                'no_URL_ticker_parameter_substitution': True, 'network_requests_made': 0})
    return result


def build_report(root):
    from .inventory_candidates import _Cache, _cash_pages

    evidence = Evidence(root)
    pointer_before = {name: evidence.read(name) for name in ('current.json', 'inventory/current.json')}
    allowed = allowed_tickers(root)
    scope = load_scope(root)
    require(scope is not None, 'positive_DY_scope_required')
    evidence.read('stock-selection/current.json')
    evidence.read('stock-selection/manifests/' + scope['id'] + '.json', scope['id'])
    evidence.read('stock-selection/' + scope['source_file'], scope['source_sha256'])
    audit = evidence.json(SNAPSHOT + '/candidate-source-audit.json')
    preparation = evidence.json(SNAPSHOT + '/stock-universe-preparation.json')
    pending = selected_pending(audit, allowed)
    inv = evidence.json(INVENTORY, preparation['inventory_sha256'])
    scoped = {**inv, 'stocks': [r for r in inv['stocks'] if r['ticker'] in allowed]}
    cache = _Cache(root, scoped)
    stocks = {r['ticker']: r for r in scoped['stocks']}
    records, identities, cash_rows = [], [], {}
    for item in pending:
        ticker = item['ticker']
        identity = stocks[ticker]
        detail_file = 'raw/expanded/b3-detail-%s.json' % identity['cvm']
        detail = cache.json(detail_file, 'selected_exact_issuer_identity')
        evidence.read(detail_file, cache.receipts[detail_file]['sha256'])
        require(int(detail['codeCVM']) == identity['cvm'], 'selected_exact_issuer_mismatch')
        codes = {r['code']: r['isin'] for r in detail['otherCodes']}
        require(ticker in codes and identity.get('isin') in (None, codes[ticker]),
                'selected_exact_ISIN_mismatch')
        price_file = 'raw/expanded/%s-prices.json' % ticker
        if not safe_path(root, price_file).is_file():
            price_file = 'raw/inventory/daily-prices/%s.json' % ticker
        body, _ = cache.read(price_file, 'selected_original_daily_prices')
        evidence.read(price_file, cache.receipts[price_file]['sha256'])
        rows = _cash_pages(ticker, codes, identity['cvm'], cache)
        share = 'UNT' if ticker.endswith('11') else identity['share_class']
        record = {'ticker': ticker, 'cvm': identity['cvm'], 'isin': codes[ticker],
            'share_class': share, 'cnpj': re.sub(r'\D', '', detail['cnpj']), 'baseline_reason': item['reason'],
            'source_price_file': price_file, 'source_detail_file': detail_file,
            'source_files': [price_file, detail_file, *[name for name in cache.receipts
                if any(name.startswith('raw/expanded/%s-b3-cash-' % code) for code in codes)]],
            'diagnostic': diagnose(body, ticker, rows, share)}
        record['next_safe_source_work'] = next_work(record)
        records.append(record)
        if ticker in FOCUS:
            identities.append(record)
            cash_rows[ticker] = rows
    for source in cache.receipts.values():
        evidence.read(source['file'], source['sha256'])
        evidence.sources[source['file']].update({k: source.get(k) for k in ('url', 'retrieved_at', 'role')})
    from .price_basis_research import _fca_identity
    from .issuer_quote_extension_research import FCA, FCA_SHA, REVIEW, REVIEW_SHA
    fca = evidence.read(FCA, FCA_SHA)
    for identity in identities:
        identity['exact_FCA_identity_proof'] = _fca_identity(fca, identity['ticker'], identity, END)
    prior = evidence.json(REVIEW, REVIEW_SHA)
    prior_records = {r['ticker']: r for r in prior['records'] if r['ticker'] in {r['ticker'] for r in records}}
    for record in records:
        previous = prior_records[record['ticker']]
        record['prior_price_basis_review'] = {'source': evidence.ref(REVIEW),
            'previous_reason': previous['remaining_reason'],
            'previous_anchor_count_including_parent_date_fallbacks': len(previous['diagnostic']['anchors']),
            'no_prior_threshold_or_factor_correction_imported': True}
    followon_file = ('raw/primary-research/issuer-raw-quotes/offline-followon-'
        'c87e873a5404884c3afbce980fb0c3ca9169703481dccd5a338ca75b624ebfd7.json')
    followon = evidence.json(followon_file, 'c87e873a5404884c3afbce980fb0c3ca9169703481dccd5a338ca75b624ebfd7')
    for record in records:
        if record['ticker'] in followon['results']:
            previous = followon['results'][record['ticker']]
            record['prior_native_history_review_retained'] = {
                'source': evidence.ref(followon_file), 'remaining_blockers': previous['blockers'],
                'direct_month_end_conflicts': previous['direct_month_end_conflicts'],
                'primary_contradictions_must_remain_blocked': True}
    closures = focus_closures(evidence, records, identities, cash_rows)
    pointer_after = {name: safe_path(root, name).read_bytes() for name in pointer_before}
    modified = [s['file'] for s in evidence.sources.values() if hashlib.sha256(
        safe_path(root, s['file']).read_bytes()).hexdigest() != s['sha256']]
    return {'schema_version': 1, 'scope': 'selected262_pending_prices_offline_sidecar',
        'selection_id': scope['id'], 'allowed_tickers_count': len(allowed),
        'baseline_snapshot': SNAPSHOT, 'baseline_inventory_sha256': preparation['inventory_sha256'],
        'baseline_inventory_file': INVENTORY,
        'baseline_model_ready_stocks': sum(r['model_ready'] for r in preparation['stocks']),
        'baseline_actual_treasury_records': len(inv['treasury']),
        'window_start': START.isoformat(), 'cutoff': END.isoformat(),
        'required_monthly_anchors': 61, 'required_monthly_returns': 60,
        'pending_reason_counts': dict(Counter(r['baseline_reason'] for r in records)),
        'records': records, 'focused_candidate_evidence_closures': closures,
        'focus_selection_basis': 'complete cached native history first, then single missing regime with all dated anchors compatible',
        'sources': sorted(evidence.sources.values(), key=lambda r: r['file']),
        'input_files_changed_during_report': modified,
        'protected_pointers_unchanged': all(pointer_before[n] == pointer_after[n] for n in pointer_before),
        'network_requests': 0, 'B3_or_exchange_or_proxy_requests': 0,
        'raw_cache_overwritten': False, 'thresholds_changed': False, 'prices_imputed': 0,
        'nearest_dates_used': False, 'factors_fitted': False, 'adjusted_close_used': False,
        'source_candidates_closed': [], 'financial_validation_performed': False,
        'source_stage_is_not_financial_validation': True,
        'model_admissions': 0, 'goal_complete': False,
        'shared_core_files_written': False, 'pointers_or_snapshots_written': False,
        'producer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'test_sha256': hashlib.sha256(Path(__file__).with_name('test_selected_price_research.py').read_bytes()).hexdigest(),
        'helper_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('share_basis.py', 'inventory_candidates.py', 'dividend_scope.py',
                         'price_basis_research.py', 'issuer_quote_extension_research.py',
                         'issuer_raw_quote_research.py', 'alternate_issuer_history_research.py')}}


def write_report(root, report):
    body = encoded(report)
    relative = CACHE + '/report-' + hashlib.sha256(body).hexdigest() + '.json'
    path = safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_selected_price_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    with offline():
        report = build_report(args.root)
    if args.report:
        print(write_report(args.root, report))
    print(json.dumps({'pending': report['pending_reason_counts'],
        'source_candidates_closed': report['source_candidates_closed'], 'network_requests': 0}, indent=2))


if __name__ == '__main__':
    main()
