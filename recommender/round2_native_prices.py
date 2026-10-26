"""Whole native RI price series corroborated by every cached exchange anchor.

The original comparison feed is retained, including disagreements. No Yahoo
response is manufactured, no dates are mixed across feeds, and no price is fit.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from . import pending_all_prices as research
from .collect_market_data import month_end, shift_month
from .share_basis import reconcile_native_prices


CUTOFF = date(2026, 9, 30)
IDENTITY = {'ticker': 'EQTL3', 'cvm': 20010, 'share_class': 'ON', 'isin': 'BREQTLACNOR0'}
OUTCOME_SHA = 'c4f2958e3b7c9663904859eae8fd3c453b93c50c2e1b280924efffe8c2a1e089'
OUTCOME = 'raw/primary-research/all-pending/prices/outcome-' + OUTCOME_SHA + '.json'
DETAIL = 'raw/expanded/b3-detail-20010.json'
DETAIL_SHA = 'd04d77d669b28a572e21fe4afe13422a2f96c7ac84d3a25327d90fa7662e83bf'
NATIVE_SHA = '2d5481fdd983505b02b716f368983af2b766619e2d2f930343f15d4117baeb2d'
PAGE_SHA = '55509fee50c048e063b756c58f035c7b5f0172104acef56e0ed31b0efd2c40d8'
BUNDLE_SHA = '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
PAGE_URL = 'https://ri.equatorialenergia.com.br/en/investor-services/quotes-and-charts/'
HISTORY_URL = ('https://api.mziq.com/mzstockinfo/c03e950e-5c92-11ea-b42f-0aed8756bb2a/history'
               '?from=2021-09-01&to=2026-09-30&tickers=EQTL3&adjusted=false')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(root, relative, digest, size=None):
    require(isinstance(relative, str) and relative.startswith('raw/')
            and '\\' not in relative and ':' not in relative
            and not set(relative.split('/')) & {'', '.', '..'}, 'native_RI_unsafe_source')
    path = root / relative
    require(path.resolve().is_relative_to(root) and all(not node.is_symlink()
            and not getattr(node, 'is_junction', lambda: False)()
            for node in (path, *path.parents) if node.is_relative_to(root)), 'native_RI_unsafe_source')
    require(path.stat().st_size <= 20_000_000, 'native_RI_source_size_limit')
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == digest and (size is None or len(body) == size),
            'native_RI_source_integrity_failure')
    return body


def receipt_source(root, source, digest):
    require(source['sha256'] == digest and source.get('status', source.get('http_status')) == 200
            and not source.get('error'), 'native_RI_receipt_contract_changed')
    body = read(root, source['file'], digest, source['bytes'])
    observed = datetime.fromisoformat(source['retrieved_at'])
    require(observed.tzinfo is not None and observed <= datetime.now(timezone.utc),
            'native_RI_invalid_retrieval_time')
    return body, {key: source.get(key) for key in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}


def load_evidence(folder, identity, cutoff, observed_actions, independent_quotes, comparison_content):
    if identity.get('ticker') != IDENTITY['ticker']:
        return None
    root = Path(folder).resolve()
    require({key: identity.get(key) for key in IDENTITY} == IDENTITY and cutoff == CUTOFF,
            'native_RI_exact_security_or_cutoff_mismatch')
    from .dividend_scope import allowed_tickers
    require(identity['ticker'] in (allowed_tickers(root) or ()), 'native_RI_outside_selected_scope')
    require(all(date.fromisoformat(action['date']) < date(2021, 9, 1)
                for action in observed_actions), 'native_RI_new_primary_action_review_required')
    detail = json.loads(read(root, DETAIL, DETAIL_SHA))
    require(int(detail['codeCVM']) == IDENTITY['cvm']
            and detail['otherCodes'] == [{'code': 'EQTL3', 'isin': IDENTITY['isin']}]
            and not detail['hasBDR'], 'native_RI_cached_identity_conflict')
    outcome_body = read(root, OUTCOME, OUTCOME_SHA)
    outcome = json.loads(outcome_body)
    receipt = outcome['receipt']
    discovery = receipt['discovery']
    require(receipt['url'] == HISTORY_URL and discovery['ticker'] == 'EQTL3'
            and discovery['adjusted'] is False and discovery['page']['url'] == PAGE_URL,
            'native_RI_published_request_mismatch')
    page, page_source = receipt_source(root, discovery['page'], PAGE_SHA)
    _, bundle_source = receipt_source(root, discovery['bundle'], BUNDLE_SHA)
    _, quote_source = receipt_source(root, receipt, NATIVE_SHA)
    require(b"'#eqtl3 .price').html('R$ '" in page, 'native_RI_BRL_display_not_verified')
    native = research.verified_native_history(root, IDENTITY, IDENTITY, receipt,
        discovery['page'], discovery['bundle'])
    daily = {date.fromisoformat(row['date']): float(row['native_close_decimal']) for row in native['rows']}
    require(len(daily) == len(native['rows']), 'native_RI_duplicate_date')
    prices, matches, mode = reconcile_native_prices(daily, observed_actions, independent_quotes, cutoff)
    required = {shift_month(cutoff, -i) for i in range(61)}
    require(required <= prices.keys(), 'native_RI_monthly_window_missing')
    references = [{'date': day.isoformat(), 'exchange_price': value, 'native_price': daily.get(day),
                   'agrees': day in daily and math.isclose(value, daily[day], rel_tol=.006, abs_tol=.015)}
                  for day, value in sorted(independent_quotes.items())
                  if shift_month(cutoff, -60) <= day <= cutoff]
    require(len(references) >= 4 and all(row['agrees'] for row in references),
            'native_RI_exchange_quote_disagreement')
    audit = research.monthly_audit(comparison_content, identity['ticker'])
    monthly = research.compare_vendor(audit, native['rows'])
    comparison = research.full_daily_comparison(comparison_content, identity['ticker'], native['rows'], audit)
    quote_source.update(authority='issuer_published_Enfoque_unadjusted_history',
        currency='BRL', currency_basis='literal_RI_EQTL3_display_R$_and_exact_B3_ISIN',
        vendor_currency_field_present=False)
    sources = [page_source, bundle_source, quote_source,
        {'file': OUTCOME, 'sha256': OUTCOME_SHA, 'bytes': len(outcome_body), 'url': None,
         'retrieved_at': None, 'authority': 'immutable_request_and_discovery_receipt'},
        {'file': DETAIL, 'sha256': DETAIL_SHA, 'bytes': (root / DETAIL).stat().st_size,
         'url': None, 'retrieved_at': None, 'authority': 'existing_exact_exchange_identity_cache'}]
    return {'daily': daily, 'prices': prices, 'sources': sources, 'price_source_file': receipt['file'],
        'price_url': HISTORY_URL, 'proof': {
            'identity': dict(IDENTITY), 'provider': 'issuer_published_Enfoque', 'currency': 'BRL',
            'source_priority_rule': 'whole_literal_native_unadjusted_RI_history_corroborated_by_all_exchange_anchors',
            'raw_source_mode': mode, 'all_independent_price_dates': matches,
            'exchange_comparisons': references, 'all_available_exchange_quotes_match': True,
            'model_monthly_inputs': [{'month': month.isoformat(), 'observed_on': prices[month][0].isoformat(),
                                      'native_close_BRL': prices[month][2]} for month in sorted(required)],
            'model_monthly_input_count': len(required), 'native_daily_observations': len(daily),
            'comparison_source_sha256': hashlib.sha256(comparison_content).hexdigest(),
            'comparison_model_monthly_conflicts': monthly['native_month_end_disagreements'],
            'comparison_all_daily_conflicts': comparison['raw_disagreements'],
            'comparison_monthly_conflict_priority_basis': 'native_RI_and_exchange_agree_on_the_single_conflicting_model_month',
            'comparison_other_daily_conflicts_status': 'retained_without_independent_third_quote_resolution',
            'days_or_prices_mixed_between_feeds': False, 'prices_fitted_or_rewritten': False,
            'observed_provider_actions_before_native_window': observed_actions,
            'observed_provider_actions_in_native_window': [], 'source_action_absence_certified': False,
            'ultimate_upstream_independence_verified': False, 'settlement_verified': False,
            'point_in_time_archive_verified': False}}
