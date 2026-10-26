"""Offline verification of exact Dexxos quote evidence and ordinary gates."""

from copy import deepcopy
from datetime import date
import json
import socket
from unittest.mock import patch

from lxml import html
import pytest

from . import round2_dexp_prices as evidence
from . import round2_share_actions as primary
from . import inventory_candidates as candidates
from . import inventory_financials as financials
from . import round2_dexp_eps as eps
from .share_basis import reconcile_prices


@pytest.fixture(autouse=True)
def offline():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network_forbidden')):
        yield


@pytest.fixture(scope='module')
def records():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')):
        return {ticker: evidence.diagnose(evidence.ROOT, primary.packet(evidence.ROOT)['targets'][ticker]['identity'])
                for ticker in evidence.TARGETS}


@pytest.fixture(scope='module')
def production_runs():
    original_patch = patch.object

    def no_source_mocks(target, attribute, *args, **kwargs):
        assert not (target is candidates and attribute in ('_one', 'reconcile_prices', '_payments'))
        assert not (target is financials and attribute == '_research')
        assert not (target is evidence and attribute == 'load_evidence')
        return original_patch(target, attribute, *args, **kwargs)

    with patch.object(patch, 'object', no_source_mocks):
        return (evidence.isolated_one(evidence.ROOT, 'DEXP3', baseline=True),
                evidence.isolated_one(evidence.ROOT, 'DEXP3'),
                evidence.isolated_one(evidence.ROOT, 'DEXP4'))


def test_exact_primary_bonus_not_price_fitted(records):
    for result in records.values():
        proof = result['primary_actions']
        assert proof['actions'] == [{'date': '2025-12-22', 'ratio': 1.125}]
        assert result['proof']['factors_fitted_to_prices'] is False
        assert result['proof']['used_regimes'][0]['factor_after'] == 1
        assert result['proof']['used_regimes'][1]['factor_after'] == 1.125


def test_all_original_exchange_quotes_and_all_post_bonus_rows_used(records):
    result = records['DEXP3']
    original, _, _, _ = evidence.original_context(evidence.ROOT, result['proof']['identity'])
    assert all(result['raw_quotes'][d] == v for d, v in original.items())
    proof = result['proof']
    assert proof['original_anchor_count_in_window'] == 7
    assert proof['in_window_provider_rows'] == 118
    assert len(proof['provider_observation_rows']) == 134
    assert len(proof['post_cutoff_rows_not_used']) == 16
    assert len(proof['independent_comparisons']) == 125
    assert proof['all_split_adjusted_conflicts'] == []
    assert len(proof['all_raw_conflicts']) == 7
    assert len(proof['original_quotes_outside_required_window_retained']) == 4
    assert proof['original_price_gate']['passed'] is True
    assert proof['original_price_gate']['mode'] == 'split_adjusted'


def test_all_61_model_months_preserved_and_no_native61_claim(records):
    for result in records.values():
        proof = result['proof']
        monthly = proof['model_monthly_inputs']
        assert len(monthly) == 61
        assert monthly[0]['month'] == '2021-09-30'
        assert monthly[-1]['month'] == '2026-09-30'
        assert sum(len(r['model_months']) for r in proof['used_regimes']) == 61
        assert all(r['all_independent_dates'] for r in proof['used_regimes'])
        assert proof['native61_issuer_history_recovered'] is False
        assert proof['native61_not_present_in_sealed_prior_index'] is True
        assert proof['provider_adjustment_flag_used_as_proof'] is False


def test_issuer_release_is_exact_ON_close_not_generic_PN_or_adjusted_history(records):
    release = records['DEXP3']['proof']['issuer_release']
    assert release['ticker'] == 'DEXP3' and release['share_class'] == 'ON'
    assert release['session_date'] == '2026-06-30' and release['literal_close'] == '7,11'
    assert release['page'] == 11 and release['published_at'] == '2026-08-11'
    assert release['historical_comparative_prices_rejected_as_raw'] is True
    assert release['used_as_extra_independent_gate_anchor'] is False
    assert release['upstream_names_reported_by_issuer'] == ['Infomoney', 'Investing.com']
    assert records['DEXP3']['proof']['issuer_corroborated_post_bonus_session_dates'] == ['2026-06-30']
    assert records['DEXP4']['proof']['issuer_corroborated_post_bonus_session_dates'] == []


def test_PNA_binding_uses_original_primary_registry_not_suffix(records):
    binding = records['DEXP4']['proof']['exact_preferred_class_binding']
    assert binding['identity'] == primary.packet(evidence.ROOT)['targets']['DEXP4']['identity']
    assert binding['registered_class'] == 'PNA' and binding['cash_source_class'] == 'PN'
    assert binding['only_preferred_class_in_each_received_FCA'] is True
    assert len(binding['historical_registry']) == 6
    assert binding['numeric_values_changed'] is False


def test_DEXP4_non_month_end_conflict_is_not_discarded(records):
    result = records['DEXP4']
    proof = result['proof']
    assert proof['in_window_provider_rows'] == 119
    assert len(proof['all_split_adjusted_conflicts']) == 1
    conflict, = proof['all_split_adjusted_conflicts']
    assert conflict['date'] == '2026-09-04'
    assert conflict['raw_quote'] == 7.50
    assert conflict['comparison_close'] == pytest.approx(7.40)
    assert conflict['documented_factor_after'] == 1
    assert conflict['is_model_monthly_date'] is False
    assert proof['original_price_gate']['passed'] is False
    assert proof['original_price_gate']['reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert result['prices'] is None
    assert conflict['original_comparison_json_index'] == 1479
    assert conflict['original_comparison_OHLCV']['high'] == pytest.approx(7.40)
    assert conflict['original_comparison_OHLCV']['volume'] == 3500


def test_third_publisher_all_rows_retained_but_not_falsely_official(records):
    third = records['DEXP4']['proof']['third_source_audit']
    assert len(third['observations']) == 50
    conflict, = third['conflicts']
    assert conflict['date'] == '2026-09-04' and conflict['close_decimal'] == '7.50'
    assert conflict['is_model_monthly_date'] is False
    assert third['official_exchange_authority_claimed'] is False
    assert third['upstream_independence_verified'] is False


def test_original_exchange_readings_keep_literal_quote_date_value_and_class(records):
    for ticker in evidence.TARGETS:
        proof = records[ticker]['proof']
        readings = proof['native_exchange_quote_readings']
        assert readings and all(r['json_pointer'].startswith('/results/') for r in readings)
        expected_class = 'ON' if ticker == 'DEXP3' else 'PN'
        assert all(r['native_row']['typeStock'].strip() == expected_class for r in readings)
        assert all(r['actual_quote_date_field'] == 'dateClosingPricePriorExDate' for r in readings)
        assert all(r['actual_quote_value_field'] == 'closingPricePriorExDate' for r in readings)


@pytest.mark.parametrize('field,value', [('cvm', 16633), ('cvm', True),
    ('share_class', 'PN'), ('isin', 'BRDEXPACNPR8')])
def test_exact_identity_failure(field, value):
    identity = deepcopy(primary.packet(evidence.ROOT)['targets']['DEXP3']['identity'])
    identity[field] = value
    with pytest.raises(ValueError):
        evidence.load_evidence(evidence.ROOT, identity, evidence.CUTOFF)


def test_unknown_ticker_not_routed_and_cutoff_fixed():
    assert evidence.load_evidence(evidence.ROOT, {'ticker': 'EQTL3'}, evidence.CUTOFF) is None
    identity = primary.packet(evidence.ROOT)['targets']['DEXP3']['identity']
    with pytest.raises(ValueError, match='exact_cutoff'):
        evidence.load_evidence(evidence.ROOT, identity, date(2026, 8, 31))


def test_caller_cannot_drop_original_anchor_or_change_feed(records):
    identity = records['DEXP3']['proof']['identity']
    quotes, _, _, content = evidence.original_context(evidence.ROOT, identity)
    removed = dict(quotes)
    removed.pop(next(iter(removed)))
    with pytest.raises(ValueError, match='caller_original_quote_set_changed'):
        evidence.load_evidence(evidence.ROOT, identity, evidence.CUTOFF, independent_quotes=removed)
    with pytest.raises(ValueError, match='caller_original_feed_changed'):
        evidence.load_evidence(evidence.ROOT, identity, evidence.CUTOFF, comparison_content=content + b' ')


def test_caller_cannot_change_or_omit_action(records):
    identity = records['DEXP3']['proof']['identity']
    with pytest.raises(ValueError, match='caller_action_list_changed'):
        evidence.load_evidence(evidence.ROOT, identity, evidence.CUTOFF, observed_actions=[])


@pytest.mark.parametrize('field,value', [(0, '23/10/2026'), (3, '900,00'), (8, '0')])
def test_duplicate_date_outside_OHLC_and_inactive_session_rejected(field, value):
    body, _, _ = evidence.source(evidence.ROOT, 'DEXP3')
    tree = html.fromstring(body.decode('utf8'))
    table = tree.xpath('//table')[1]
    row = table.xpath('.//tr')[2]
    row.xpath('./td')[field].text = value
    with pytest.raises(ValueError):
        evidence.parse_ibovx(html.tostring(tree, encoding='utf8'), 'DEXP3')


def test_literal_price_parser_does_not_infer_zero_or_accept_bool():
    for value in ('', '0,00', '7.40', 'NaN', '7,400', True):
        with pytest.raises(ValueError):
            evidence.br_decimal(value)


def test_literal_financial_zero_roots_are_unavailable_class_EPS_not_imputed_zero():
    context = [{'kind': 'filing', 'filing_id': '155652', 'fiscal_end': '2025-12-31'}]
    result = evidence.financial_blocker_rows(evidence.ROOT, context)
    assert result['usable_class_EPS_from_these_rows'] is None
    assert result['zero_roots_not_promoted_to_EPS'] is True
    assert result['printed_note_EPS_absence_certified'] is False
    assert [r['csv_line'] for r in result['native_current_period_rows']] == [2059, 2061, 2063]
    assert [r['native_row']['CD_CONTA'] for r in result['native_current_period_rows']] == [
        '3.99', '3.99.01', '3.99.02']
    assert all(r['native_row']['VL_CONTA'] == '0.0000000000'
               for r in result['native_current_period_rows'])


def test_source_body_hash_tamper_fails_without_cache_mutation():
    original = primary.read

    def tampered(root, relative, digest):
        if relative.endswith('receipt-impossible.json'):
            return b'{}'
        return original(root, relative, '0' * 64 if '/body-' in relative else digest)

    with patch.object(primary, 'read', tampered):
        with pytest.raises(ValueError, match='hash_changed'):
            evidence.source(evidence.ROOT, 'DEXP3')


def test_loader_fails_closed_for_actual_DEXP4_conflict(records):
    identity = records['DEXP4']['proof']['identity']
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees'):
        evidence.load_evidence(evidence.ROOT, identity, evidence.CUTOFF)


def test_unchanged_regime_gate_rejects_only_latest_regime(records):
    result = records['DEXP3']
    identity = result['proof']['identity']
    _, _, _, content = evidence.original_context(evidence.ROOT, identity)
    latest_only = {d:v for d,v in result['raw_quotes'].items() if d >= date(2025, 12, 22)}
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_prices(content, 'DEXP3', result['primary_actions']['observed_actions'], latest_only,
                         evidence.CUTOFF, evidence_provider=evidence.PROVIDER)


def test_actual_one_source_dispatch_keeps_original_cash_and_cache_unchanged():
    result = evidence.isolated_one(evidence.ROOT, 'DEXP3', with_financial=False)
    assert result['source_candidate'] is True
    assert result['normal_one_called'] is True
    assert result['share_mode'] == 'split_adjusted'
    assert result['price_source_file'] == 'raw/expanded/DEXP3-prices.json'
    assert result['prices'] >= 61 and result['cash_matches'] >= 4
    assert result['protected_hashes_before'] == result['protected_hashes_after']
    assert result['network_calls'] == 0
    assert result['cash_inspector_patched'] is False and result['cash_gate_patched'] is False
    assert result['price_gate_calls'] == []
    assert result['production_price_hook_unmodified'] is True
    assert result['production_research_unmodified'] is True
    assert result['financial_hooks_overridden'] == [] and result['counterfactual'] is None
    assert len(result['production_price_gate']['all_match_dates']) == 125
    assert result['production_price_gate']['required_months'] == 61
    assert result['checks']['independent_raw_quote_evidence']['original_price_gate']['passed'] is True
    assert result['checks']['independent_price_matches'] == 125


def test_ordinary_DEXP4_stays_pending_no_day_patch_or_favorable_subset():
    result = evidence.isolated_one(evidence.ROOT, 'DEXP4', with_financial=False)
    assert result['source_candidate'] is False
    assert result['pending_reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert result['price_gate_calls'] == []
    assert result['protected_hashes_before'] == result['protected_hashes_after']


def test_actual_financial_production_and_explicit_counterfactual(production_runs):
    before, after, _ = production_runs
    assert before['source_candidate'] is after['source_candidate'] is True
    assert before['financial_current_pass'] is False and after['financial_current_pass'] is True
    assert before['financial_replay']['current'] is None
    assert before['financial_replay']['exact_native_current_filing_blocker'] is not None
    assert after['financial_replay']['exact_native_current_filing_blocker'] is None
    assert before['counterfactual']['proof_validated_before_suppression'] is True
    assert before['counterfactual']['production_reader_calls'] == 1
    assert before['financial_hooks_overridden'] == ['get_dexp_eps_readings']
    assert after['counterfactual'] is None and after['financial_hooks_overridden'] == []
    record, = after['financial_replay']['latest_2025_records']
    assert (record['filing_id'], record['share_class'], record['profit_per_share']) == ('155652', 'ON', 1.3299)
    assert before['training_rows'] == after['training_rows'] == 0
    for key in ('prices', 'cash_matches', 'ledger_rows', 'production_price_gate', 'source_receipts', 'checks'):
        assert before[key] == after[key]
    for result in (before, after):
        assert result['protected_hashes_before'] == result['protected_hashes_after']
        assert result['production_price_hook_unmodified'] is result['production_research_unmodified'] is True


def test_blocked_DEXP4_does_not_get_cash_or_financial_features_substituted(production_runs):
    _, _, result = production_runs
    assert result['source_candidate'] is False and result['financial_current_pass'] is None
    assert result['pending_reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert result['financial_replay']['financial_features_called'] is False
    assert result['financial_replay']['current'] is None
    assert result['financial_replay']['unknown_cash_replaced_with_zero'] is False
    assert result['financial_replay']['exact_native_current_filing_blocker'] is None
    record, = result['financial_replay']['latest_2025_records']
    assert record['share_class'] == 'PNA' and record['profit_per_share'] == 1.3299


def test_counterfactual_is_explicit_and_validates_primary_EPS_first(monkeypatch):
    with pytest.raises(ValueError, match='baseline_requires_financial_diagnostic'):
        evidence.isolated_one(evidence.ROOT, 'DEXP3', baseline=True, with_financial=False)

    def invalid(*args, **kwargs):
        raise ValueError('invalid_primary_EPS_proof')

    monkeypatch.setattr(eps, 'get_dexp_eps_readings', invalid)
    monkeypatch.setattr(candidates, '_one', lambda *args: pytest.fail('candidate_before_proof_validation'))
    with pytest.raises(ValueError, match='invalid_primary_EPS_proof'):
        evidence.isolated_one(evidence.ROOT, 'DEXP3', baseline=True)


def test_counterfactual_requires_integrated_production_EPS_hook(monkeypatch):
    monkeypatch.setattr(financials, 'prepare_financials', lambda *args: ([], [], []))
    with pytest.raises(ValueError, match='production_EPS_hook_not_exercised'):
        evidence.isolated_one(evidence.ROOT, 'DEXP3', baseline=True)


def test_scientific_limits_are_explicit(records):
    for result in records.values():
        proof = result['proof']
        for key in ('upstream_independence_verified', 'point_in_time_archive_verified',
                    'primary_action_absence_certified', 'quote_cause_or_revision_chronology_proved',
                    'gates_relaxed', 'synthetic_exchange_rows_created', 'model_admission'):
            assert proof[key] is False
        assert proof['all_available_original_exchange_quotes_retained'] is True
        assert proof['all_new_provider_window_quotes_retained'] is True
