"""Actual-cache proof replay and acquisition boundaries, with no live HTTP."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from . import issuer_raw_quote_research as research


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('test_attempted_live_HTTP')
    monkeypatch.setattr(research.urllib.request.OpenerDirector, 'open', forbidden)


@pytest.mark.parametrize('ticker', ['FRAS3', 'SBSP3'])
def test_actual_cached_raw_histories_and_complete_official_chains(ticker):
    rows, receipt = research.pinned_history(ROOT, ticker)
    assert len(rows) == 1268
    assert rows[0]['date'] == '2021-09-01'
    assert rows[-1]['date'] == '2026-09-30'
    assert 'adjusted=false' in receipt['url']
    assert receipt['discovery']['parameters']['adjusted'] is False
    assert len({r['date'] for r in rows}) == 1268
    expected = '22.6' if ticker == 'FRAS3' else '27.43'
    assert rows[-1]['closed'] == expected


def test_native_share_scale_across_sabesp_split_is_preserved():
    rows, _ = research.pinned_history(ROOT, 'SBSP3')
    by_date = {r['date']: r for r in rows}
    assert by_date['2026-04-28']['closed'] == '167'
    assert by_date['2026-04-29']['closed'] == '32.99'
    assert by_date['2022-04-28']['closed'] == '46.3'
    assert by_date['2026-04-28']['vol'] == '409378176'
    assert by_date['2026-04-28']['share'] == '2461800'


def test_changed_real_cache_is_not_silently_parsed():
    _, digest = research.HISTORY_PINS['FRAS3']
    body = (ROOT / research.CACHE / f'body-{digest}.bin').read_bytes()
    changed = body.replace(b'22.6', b'99.6', 1)
    assert changed != body
    with pytest.raises(ValueError, match='unreviewed_history_body_hash'):
        research.parse_payload(changed, 'FRAS3', digest)


@pytest.mark.parametrize('mutation,reason', [
    ('wrong_ticker', 'exact_vendor_ticker'),
    ('duplicate_date', 'duplicate_or_out_of_range'),
    ('non_iso_date', 'unreviewed_history_schema'),
    ('invalid_ohlc', 'invalid_raw_OHLC'),
    ('bool_price', 'invalid_history_number'),
    ('vendor_error', 'vendor_authority_or_history_errors'),
])
def test_parser_rejects_bad_financial_semantics(mutation, reason):
    _, digest = research.HISTORY_PINS['FRAS3']
    data = json.loads((ROOT / research.CACHE / f'body-{digest}.bin').read_bytes())
    ticker = data['data']['tickers'][0]
    row = ticker['history'][0]
    if mutation == 'wrong_ticker':
        ticker['ticker'] = 'FRAS4'
    elif mutation == 'duplicate_date':
        ticker['history'].append(deepcopy(row))
    elif mutation == 'non_iso_date':
        row['date'] = '30/09/2026'
    elif mutation == 'invalid_ohlc':
        row['closed'] = row['high'] + 1
    elif mutation == 'bool_price':
        row['closed'] = True
    elif mutation == 'vendor_error':
        data['data']['errors'] = [{'status': 'missing'}]
    body = json.dumps(data).encode()
    with pytest.raises(ValueError, match=reason):
        research.parse_payload(body, 'FRAS3', research.prior.sha(body))


def test_actual_disagreements_remain_visible_without_fitted_factors():
    fras, _ = research.pinned_history(ROOT, 'FRAS3')
    sabesp, _ = research.pinned_history(ROOT, 'SBSP3')
    a = research.compare_existing_daily(ROOT, 'FRAS3', fras)
    b = research.compare_existing_daily(ROOT, 'SBSP3', sabesp)
    assert a['daily_matches'] == 1127
    assert len(a['daily_mismatches']) == 141
    assert a['daily_mismatches_in_model_window'] == 121
    assert a['daily_mismatches'][-1]['date'] == '2022-03-28'
    assert a['daily_mismatches'][-1]['independent_raw_close_BRL'] == '13.7'
    assert b['daily_matches'] == 1267
    assert len(b['daily_mismatches']) == 1
    assert b['daily_mismatches'][0]['date'] == '2026-07-23'
    assert b['daily_mismatches'][0]['independent_raw_close_BRL'] == '29'
    assert b['daily_mismatches'][0]['cached_Yahoo_close'] == '28.75'
    for result in (a, b):
        assert len(result['monthly_raw_anchors']) == 61
        assert not result['fitted_scalars']
        assert result['model_admission_granted'] is False
        assert result['primary_action_ratios_verified'] is False
        assert result['raw_source_modified_or_adjusted'] is False


@pytest.mark.parametrize('url', [
    'https://sistemaswebb3-listados.b3.com.br/anything',
    'https://www.bmfbovespa.com.br/anything',
    'https://www.bovespa.com.br/anything',
    'https://www.bvmf.com.br/anything',
    'https://ri.azzas2154.com.br/',
    'https://allos.com.br/',
    'https://www.santander.com.br/ri/',
    'https://www.engie.com.br/',
    'https://cdn-sites-assets.mziq.com/robots.txt',
    'https://api.mziq.com/login',
    'https://cms-backend.mziq.com/wp-signup.php?new=anything',
    'https://brapi.dev/api/quote/FRAS3?range=5y',
    'https://example.invalid/?target=https%253A%252F%252Fb3.com.br',
])
def test_denied_or_auth_or_proxy_URLs_are_never_allowed(url):
    assert not research.allowed(url)


def test_no_cross_class_quote_substitution():
    receipts = research.records(ROOT, 'response')
    page, = [r for r in receipts if r['ordinal'] == 6]
    bundle, = [r for r in receipts if r['ordinal'] == 8]
    with pytest.raises(ValueError, match='exact_widget_ticker_and_issuer'):
        research.widget_request(ROOT, 'BMEB4', page, bundle)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_denials_stop_the_entire_issuer_chain(tmp_path, monkeypatch, status):
    monkeypatch.setattr(research.Fetcher, 'baseline', lambda self: None)
    attempt = {'ordinal': 1, 'url': 'https://example.invalid/robots.txt', 'ticker': 'FRAS3'}
    research.save(tmp_path, 'request', attempt)
    research.save(tmp_path, 'response', {**attempt, 'http_status': status,
        'stop_reason': f'HTTP_{status}'})
    with pytest.raises(ValueError, match='issuer_chain_stopped'):
        research.Fetcher(tmp_path).request('https://example.invalid/another', {}, 'FRAS3')
    assert len(research.records(tmp_path, 'request')) == 1


def test_persistent_twenty_request_budget_includes_failed_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(research.Fetcher, 'baseline', lambda self: None)
    for n in range(1, 21):
        attempt = {'ordinal': n, 'url': f'https://example.invalid/{n}', 'ticker': 'FRAS3'}
        research.save(tmp_path, 'request', attempt)
        research.save(tmp_path, 'response', {**attempt, 'http_status': 404, 'stop_reason': None})
    with pytest.raises(ValueError, match='twenty_request_budget_exhausted'):
        research.Fetcher(tmp_path).request('https://example.invalid/new', {}, 'FRAS3')
    assert len(research.records(tmp_path, 'request')) == 20


def test_interrupted_request_remains_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(research.Fetcher, 'baseline', lambda self: None)
    research.save(tmp_path, 'request', {'ordinal': 1, 'url': 'https://example.invalid/', 'ticker': 'FRAS3'})
    with pytest.raises(ValueError, match='interrupted_attempt_no_network_retry'):
        research.Fetcher(tmp_path).request('https://example.invalid/new', {}, 'FRAS3')


def test_contact_forms_are_not_public_content_challenges():
    for digest in research.CONTACT_FORM_REVIEW:
        body = (ROOT / research.CACHE / f'body-{digest}.bin').read_bytes()
        assert b'g-recaptcha' in body
        tree = research.prior.html.fromstring(body)
        widgets = tree.xpath('//*[contains(concat(" ", normalize-space(@class), " "), " g-recaptcha ")]')
        assert widgets and all(widget.xpath('ancestor::form | ancestor::div[contains(@class, "custom_form")]')
                               for widget in widgets)
        assert len(tree.xpath('//a[@href]')) > 20
        assert not research.challenge_body(body)
    assert research.challenge_body(b'<title>Just a moment</title><div>checking</div>')
    assert research.challenge_body(b'<div class="g-recaptcha">Prove access</div>')


def test_readonly_report_keeps_failures_and_no_cash_or_model_admission():
    report = research.build_report(ROOT)
    assert report['acquisition_requests'] == 18
    assert report['unused_budget'] == 2
    assert len(report['failed_HTTP_receipts']) == 1
    assert report['failed_HTTP_receipts'][0]['ordinal'] == 9
    assert report['failed_HTTP_receipts'][0]['http_status'] == 404
    assert report['B3_network_requests'] == report['BRAPI_network_requests'] == 0
    assert report['cash_events_created'] == report['model_admissions'] == 0
    assert report['network_requests_during_report'] == 0
    assert not report['annual_dividend_aggregates_used_as_dated_cash']
    assert not report['production_parser_installed']


def test_completed_acquisition_is_sealed(tmp_path, monkeypatch):
    monkeypatch.setattr(research.Fetcher, 'baseline', lambda self: None)
    research.save(tmp_path, 'seal', {'network_closed': True})
    with pytest.raises(ValueError, match='research_acquisition_sealed_offline_only'):
        research.Fetcher(tmp_path).request('https://example.invalid/', {}, 'FRAS3')


@pytest.fixture(scope='module')
def followon():
    from unittest.mock import patch

    with patch.object(research.urllib.request.OpenerDirector, 'open',
                      side_effect=AssertionError('offline_followon_attempted_HTTP')):
        return research.build_offline_followon(ROOT)


def test_followon_serializes_and_keeps_acquisition_sealed(followon):
    assert json.loads(json.dumps(followon))['new_HTTP_requests'] == 0
    assert followon['total_sealed_HTTP_attempts'] == 18
    assert len(research.records(ROOT, 'request')) == 18
    assert followon['thresholds_changed'] is False
    assert followon['fitted_price_factors'] == []
    assert followon['missing_prices_filled'] == 0
    assert followon['production_changes'] is False
    assert followon['model_admissions'] == 0
    assert followon['point_in_time_archive_verified'] is False
    assert followon['cash_annual_aggregates_promoted'] is False


def test_B3_reference_dates_classes_units_and_exact_values(followon):
    fras = followon['results']['FRAS3']['B3_reference_audit']
    sabesp = followon['results']['SBSP3']['B3_reference_audit']
    assert fras['distinct_dates'] == 0
    assert len(fras['all_rows']) == 16
    assert not any(r['accepted_as_raw_anchor'] for r in fras['all_rows'])
    assert any(r.get('exclusion') == 'no_explicit_quote_date_no_record_date_substitution'
               for r in fras['all_rows'])
    assert sabesp['distinct_dates'] == sabesp['exact_Enfoque_matches'] == 6
    assert sabesp['accepted_dates'] == ['2022-04-28', '2023-04-28', '2024-04-25',
                                        '2025-04-29', '2025-12-23', '2026-03-19']
    accepted = [r for r in sabesp['all_rows'] if r['accepted_as_raw_anchor']]
    assert len(accepted) == 7  # Two different cash rights share one raw quote date.
    assert all(r['original_row']['typeStock'] == 'ON' and r['original_row']['quotedPerShares'] == '1'
               for r in accepted)
    assert all(r['Enfoque_exact_match'] and r['Yahoo_existing_gate_match'] for r in accepted)


def test_exchange_percentages_are_exact_and_not_called_native_proof(followon):
    actions = followon['results']['SBSP3']['exchange_actions_conditional']
    assert [a['date'] for a in actions] == ['2025-12-26', '2026-03-20', '2026-04-29']
    assert [Decimal(a['ratio']) for a in actions] == [Decimal('1.029646975'), Decimal('1.00160980322'), Decimal(5)]
    assert [a['last_cum_date'] for a in actions] == ['2025-12-23', '2026-03-19', '2026-04-28']
    assert all(a['primary_native_CVM_ratio_verified'] is False for a in actions)
    assert Decimal(actions[0]['Yahoo_rounded_ratio']) != Decimal(actions[0]['ratio'])
    assert research.action_factor('2026-04-28', actions) == 5
    assert research.action_factor('2026-04-29', actions) == 1


def test_all_61_month_ends_and_each_regime_are_audited(followon):
    results = followon['results']
    for row in results.values():
        months = row['all_61_month_ends']
        assert len(months) == len({r['month'] for r in months}) == 61
        assert months[0]['month'] == '2021-09-30'
        assert months[-1]['month'] == '2026-09-30'
        assert all(r['same_month_end_observation_date'] for r in months)
        assert all(r['unchanged_extreme_price_gate_pass'] for r in months)
    assert results['SBSP3']['direct_month_end_conflicts'] == []
    assert results['FRAS3']['direct_month_end_conflicts'] == [
        '2021-09-30', '2021-10-31', '2021-11-30', '2021-12-31', '2022-01-31', '2022-02-28']
    regimes = list(results['SBSP3']['regimes'].values())
    assert len(regimes) == 4
    assert sum(r['daily_observations'] for r in regimes) == 1268
    assert sorted(len(r['month_ends']) for r in regimes) == [1, 3, 6, 51]
    assert sum(bool(r['B3_anchor_dates']) for r in regimes) == 2
    combined = results['SBSP3']['conditional_combined_source_price_checks']
    assert combined['minimum_anchors_and_all_monthly_regimes_numeric_pass'] is True
    assert combined['B3_only_regime_gap_is_automatic_source_veto'] is False
    assert len(combined['new_regime_Yahoo_check_dates']) == 7
    assert results['FRAS3']['conditional_combined_source_price_checks']['minimum_anchors_and_all_monthly_regimes_numeric_pass'] is False


def test_actual_raw_volume_body_math_is_checked_beyond_adjusted_flag(followon):
    for ticker, row in followon['results'].items():
        evidence = row['raw_unit_body_corroboration']
        assert len(evidence['daily_observations']) == 1268
        assert evidence['all_turnover_per_share_inside_raw_OHLC']
        assert evidence['adjusted_false_alone_treated_as_proof'] is False
        assert evidence['independent_exact_B3_quote_dates'] == (6 if ticker == 'SBSP3' else 0)
    evidence = followon['results']['SBSP3']['raw_unit_body_corroboration']['daily_observations']
    before = next(r for r in evidence if r['date'] == '2026-04-28')
    assert Decimal(before['turnover_per_reported_share']).quantize(Decimal('.01')) == Decimal('166.29')


def test_fras_systematic_cohort_is_not_excused_as_rounding(followon):
    fras = followon['results']['FRAS3']
    systematic = [r for r in fras['daily_comparison'] if r['all_OHLC_fields_differ_beyond_printed_cent']]
    assert len(systematic) == 141
    assert systematic[0]['date'] == '2021-09-01'
    assert systematic[-1]['date'] == '2022-03-28'
    assert all(all(not f['compatible_with_printed_cent_precision'] for f in r['fields'].values())
               for r in systematic)
    assert len(fras['substantive_conflict_dates']) == 141
    assert fras['real_differences_within_existing_gate'] == ['2022-09-26']
    assert not any(r['specific_closing_type_proved'] or r['provider_revision_proved'] for r in systematic)
    description = fras['systematic_OHLC_cohort_description']
    assert description['sessions'] == 141 and description['sessions_in_model_window'] == 121
    assert description['first_following_session'] == '2022-03-29'
    assert description['Yahoo_action_on_first_following_session'] is False
    assert description['ratio_range_is_descriptive_not_an_approved_action_or_correction'] is True
    assert description['proportional_OHLC_sessions'] == 139
    assert description['nonproportional_residual_dates'] == ['2021-09-29', '2021-10-20']
    assert all(r['OHLC_proportional_shape_check']['descriptive_interval_never_used_to_correct_prices'] for r in systematic)


def test_small_real_provider_differences_are_not_labeled_float_noise(followon):
    sabesp = followon['results']['SBSP3']
    assert sabesp['substantive_conflict_dates'] == ['2026-07-23']
    assert sabesp['real_differences_within_existing_gate'] == ['2021-10-20', '2022-09-12']
    assert sabesp['conflict_dates'] == ['2021-10-20', '2022-09-12', '2026-07-23']
    for record in sabesp['daily_comparison']:
        if record['date'] in sabesp['real_differences_within_existing_gate']:
            assert not record['fields']['closed']['compatible_with_printed_cent_precision']
            assert record['fields']['closed']['existing_gate_match']


def test_sabesp_isolated_close_conflict_has_no_proven_date_type_or_revision_explanation(followon):
    sabesp = followon['results']['SBSP3']
    conflict = next(r for r in sabesp['daily_comparison'] if r['date'] == '2026-07-23')
    assert conflict['fields']['closed']['Enfoque_raw'] == '29'
    assert conflict['fields']['closed']['Yahoo_stored'] == '28.75'
    assert conflict['classification'] == 'isolated_close_only_conflict_cause_unproven'
    assert all(conflict['fields'][field]['compatible_with_printed_cent_precision'] for field in ('open', 'high', 'low'))
    assert conflict['same_date_in_UTC_and_Sao_Paulo']
    assert not any(n['all_OHLC_equal_Enfoque_target_within_cent'] for n in conflict['neighbor_date_tests'])
    assert conflict['specific_closing_type_proved'] is conflict['provider_revision_proved'] is False
    check = conflict['reported_return_self_check']
    assert check['Enfoque_reported_var_percent'] == '-0.99'
    assert abs(Decimal(check['return_using_Enfoque_closes_percent']) + Decimal('.99')) < Decimal('.001')
    assert Decimal(check['return_using_comparator_close_percent']) < Decimal('-1.8')
    impact = next(r for r in sabesp['conflict_impact_on_monthly_inputs'] if r['date'] == '2026-07-23')
    assert impact['used_as_month_end'] is False
    assert sabesp['daily_Yahoo_disagreement_is_automatic_source_veto'] is False


def test_no_provider_timezone_shift_is_used_to_explain_disagreement(followon):
    for result in followon['results'].values():
        assert all(row['same_date_in_UTC_and_Sao_Paulo'] for row in result['daily_comparison'])


def test_IPE_candidates_are_not_native_ratio_body_proof(followon):
    native = followon['primary_action_body_audit']
    assert len(native['index_sources']) == 6
    assert len(native['index_candidates']) == 38
    assert all(not r['matching_cached_body_filenames'] for r in native['index_candidates'])
    assert all(not r['matching_content_receipts'] for r in native['index_candidates'])
    assert native['native_action_ratio_readings'] == []
    assert not native['index_metadata_used_as_native_ratio']
    needed = native['required_native_proof_candidates']
    assert {r['sequence'] for r in needed['SBSP3_2026_04_split']} == {'1022240', '1033944', '1034745'}
    assert {r['sequence'] for r in needed['SBSP3_2026_03_bonus']} == {'1017747'}
    assert {r['sequence'] for r in needed['SBSP3_2025_12_bonus']} == {'984073', '984181'}
    assert len(native['cached_native_forms']) == 5
    assert all(not r['native_ratio_verified'] and not r['contains_ratio_action_terms'] for r in native['cached_native_forms'])
    assert all(r['pages'] and r['source']['sha256'] for r in native['cached_native_forms'])
    assert all(int(r['exact_IPE_index_row']['row']['Codigo_CVM']) == r['cvm'] for r in native['cached_native_forms'])


def test_no_same_or_distinct_upstream_is_assumed(followon):
    provenance = followon['provenance']
    assert provenance['Enfoque_Yahoo_same_upstream_assumed'] is False
    assert provenance['Enfoque_Yahoo_distinct_upstream_verified'] is False
    assert 'unknown' in provenance['upstream_relationship']


def test_uniform_cash_gates_still_block_both_cases(followon):
    for result in followon['results'].values():
        cash = result['unchanged_cash_gate_replay']
        assert cash['status'] == 'blocked_by_existing_gate'
        assert cash['reason'] == 'B3_cash_event_missing_from_payment_table'
        assert cash['calendar_rights_missing_independent_cash'] or cash['exchange_rights_missing_calendar']
        assert not cash['candidate_constructed']
        assert not result['uniform_price_gates']['full_price_candidate_approved']
        assert not result['model_admission_granted']
        assert result['financial_and_training_gates']['financial_features_or_stock_observations_relaxed'] is False


def test_numeric_month_end_audit_agrees_with_unchanged_legacy_gate(followon):
    from .share_basis import reconcile_prices

    ticker = 'SBSP3'
    result = followon['results'][ticker]
    raw = research.read(ROOT, {'file': f'raw/expanded/{ticker}-prices.json', 'sha256': research.DAILY_PINS[ticker]})
    actions = [{'date': a['date'], 'ratio': float(a['ratio'])} for a in result['exchange_actions_conditional']]
    quotes = {date.fromisoformat(r['observed_on']): float(r['raw_Enfoque_close']) for r in result['all_61_month_ends']}
    prices, matches, mode = reconcile_prices(raw, ticker, actions, quotes, research.CUTOFF)
    assert mode == 'split_adjusted' and len(matches) == 61
    assert all(date.fromisoformat(r['month']) in prices for r in result['all_61_month_ends'])
    # Numerical replay is not a replacement for native ratio or source proof.
    assert result['uniform_price_gates']['primary_action_basis_verified'] is False


def test_missing_primary_month_is_not_filled_from_Yahoo(followon):
    result = followon['results']['SBSP3']
    rows, _ = research.pinned_history(ROOT, 'SBSP3')
    rows = [r for r in rows if not r['date'].startswith('2022-02')]
    daily, yahoo_actions, _ = research.cached_yahoo(ROOT, 'SBSP3')
    audit = research.month_end_audit(rows, daily, yahoo_actions,
                                     result['exchange_actions_conditional'], result['B3_reference_audit'])
    assert next(r for r in audit if r['month'] == '2022-02-28') == {
        'month': '2022-02-28', 'status': 'missing_no_fill'}


def test_forwarded_financial_audit_does_not_authorize_EPS_or_date_gate_changes(followon):
    fras = followon['results']['FRAS3']['financial_and_training_gates']
    sabesp = followon['results']['SBSP3']['financial_and_training_gates']
    assert fras['forwarded_FY2025_annual_valid'] is True
    assert fras['Yahoo_action_list_is_empty'] is True
    assert fras['split_factor_invented_for_price_conflict'] is False
    assert sabesp['status'] == 'separate_financial_attribution_and_EPS_action_basis_unresolved'
    assert len(sabesp['required_evidence']) == 4
    assert sabesp['EMAE_capital_change_index_context_not_body_proof']
    assert sabesp['blind_factor_after_conversion_of_FY2025_EPS_permitted'] is False
    for context in (fras, sabesp):
        assert context['independently_recomputed_by_this_price_helper'] is False
        assert context['EPS_normalization_authorized'] is False
        assert context['included_action_IDs_known'] is False
        assert context['generic_date_gate_lowered'] is False
        assert context['financial_eligibility_inferred_from_price_checks'] is False
    assert followon['policy']['generic_date_gate_changed'] is False
    assert followon['policy']['price_sensitivity_values_are_not_admissible_normalized_prices_or_EPS'] is True
    assert followon['coordination_scope']['owned_price_names'] == ['FRAS3', 'SBSP3']
    assert followon['coordination_scope']['LREN3_PSSA3_KLBN_research_deferred_to_user_named_Leibniz_agent'] is True
    assert all(r['calculation_scope'] == 'non_admissible_sensitivity_to_unverified_cached_action_ratios'
               for value in followon['results'].values() for r in value['all_61_month_ends'])
