"""Offline integration checks against actual immutable issuer/CVM originals."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from . import pending_a_independent as provider


MARKET = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def trials():
    if not (MARKET / provider.BASE / 'pending-a-bemobi-extra-documents.json').is_file():
        pytest.skip('actual local research originals are not installed')
    with patch('socket.create_connection', side_effect=AssertionError('offline replay attempted network')), \
            patch('socket.socket.connect', side_effect=AssertionError('offline replay attempted network')):
        yield {ticker: provider.isolated_trial(MARKET, ticker) for ticker in provider.IDENTITIES}


@pytest.mark.parametrize('ticker', tuple(provider.IDENTITIES))
def test_every_fixed_monthly_anchor_and_same_real_trade_day_pass(ticker, trials):
    proof = trials[ticker]['primary_action_and_monthly_price_gate']
    assert len(proof['price_matches']) == 61
    assert proof['same_actual_last_trade_dates'] is True
    assert proof['actual_anchor_count'] == 61
    assert proof['required_minimum_anchor_count'] == 4
    assert proof['unchanged_relative_epsilon'] == .006
    assert proof['unchanged_absolute_epsilon'] == .015
    assert all(not row['is_model_monthly_anchor'] for row in proof['off_grid_daily_discrepancies'])
    assert proof['original_daily_sources_retained'] is True


def test_off_grid_conflicts_are_audited_not_conveniently_dropped(trials):
    assert {ticker: len(trial['primary_action_and_monthly_price_gate']['off_grid_daily_discrepancies'])
            for ticker, trial in trials.items()} == {'BLAU3': 3, 'BMOB3': 1, 'CXSE3': 2}
    assert trials['BMOB3']['primary_action_and_monthly_price_gate']['off_grid_daily_discrepancies'][0]['trade_date'] == '2021-10-20'


@pytest.mark.parametrize('ticker', tuple(provider.IDENTITIES))
def test_generic_ON_is_bound_by_all_six_native_FCA_filings(ticker, trials):
    proof = trials[ticker]['evidence_audit']['checks']['class_proof']
    assert proof['identity'] == provider.IDENTITIES[ticker]
    assert proof['sole_equity_class_across_received_FCA_2021_2026'] is True
    assert {row['year'] for row in proof['years']} == set(range(2021, 2027))
    assert all(row['equity_classes'] == ['ON'] and row['equity_codes'] == [ticker] for row in proof['years'])


def test_bemobi_has_12_components_not_HTML_totals_plus_PDFs(trials):
    audit = trials['BMOB3']['evidence_audit']
    events = audit['calendar']['events']
    assert len(events) == 12
    assert sum(row['record_date'] is None for row in events) == 2
    assert audit['calendar']['HTML_mixed_totals_not_added'] == ['2025-01-07', '2025-12-22']
    totals = audit['expected_cash']
    assert totals['2024-12-17'] == pytest.approx(.64384496)
    assert totals['2025-12-11'] == pytest.approx(1.590555)
    assert '2022-03-21' not in totals
    assert all(row['record_date_derived_from_ex_date'] is False for row in events)


def test_bemobi_exact_2026_gross_values_and_primary_date_revision(trials):
    events = trials['BMOB3']['evidence_audit']['calendar']['events']
    keyed = {row['record_date']: row for row in events if row['record_date']}
    assert keyed['2026-05-15']['gross_per_share_decimal'] == '0.18818727094'
    assert keyed['2026-08-14']['gross_per_share_decimal'] == '0.19178292'
    assert keyed['2025-08-21']['gross_per_share_decimal'] == '0.35551784'
    assert keyed['2025-08-21']['payment_date'] == '2025-09-01'
    conflict = trials['BMOB3']['evidence_audit']['calendar']['source_conflicts'][1]
    assert conflict['HTML'] == '2025-12-01' and conflict['CVM_version_2'] == '2025-09-01'


def test_deadline_remains_positive_unknown_payment_and_tax_conflict(trials):
    checks = trials['BLAU3']['evidence_audit']['checks']
    deadline, = checks['unresolved_payments']
    assert deadline['payment_date'] is None
    assert deadline['payment_deadline'] == '2028-12-15'
    assert deadline['gross_per_share_decimal'] == '0.56280720444'
    assert checks['deadline_converted_to_payment_date'] is False
    assert checks['native_tax_label_conflicts'][0]['net_gross_factor_applied'] is False
    action, = trials['BLAU3']['primary_action_and_monthly_price_gate']['actions']
    assert (action['date'], action['ratio'], action['credit_date']) == ('2026-01-06', 1.3, '2026-01-08')


@pytest.mark.parametrize('observed', ([], [{'date': '2026-01-08', 'ratio': 1.3}],
                                    [{'date': '2026-01-06', 'ratio': 1.03}]))
def test_wrong_or_missing_real_primary_bonus_is_rejected(observed):
    with pytest.raises(ValueError, match='pending_a_primary_actions_disagree'):
        provider.validate_primary_actions(MARKET, provider.IDENTITY, provider.CUTOFF, observed)


def test_fixed_grid_rejects_a_real_but_non_final_trade_date():
    identity = provider.IDENTITIES['BMOB3']
    actual = deepcopy(provider.load_native_prices(MARKET, identity))
    actual['monthly_anchors'][0]['trade_date'] = '2021-09-29'
    with patch.object(provider, 'load_native_prices', return_value=actual), \
            pytest.raises(ValueError, match='pending_a_native_and_model_last_trade_disagree'):
        provider.native_quote_proof(MARKET, identity, provider.CUTOFF, [])


def test_scope_identity_and_hash_guards():
    assert provider.load_evidence(MARKET, {'ticker': 'OTHER3'}, provider.CUTOFF) is None
    assert provider.load_evidence(MARKET, provider.IDENTITY, date(2026, 8, 31)) is None
    with pytest.raises(ValueError, match='pending_a_exact_CVM_class_ISIN_mismatch'):
        provider.load_evidence(MARKET, dict(provider.IDENTITY, cvm=25500), provider.CUTOFF)
    source = dict(provider.TEMPLATE_SOURCE, sha256='0' * 64)
    with pytest.raises(ValueError, match='pending_a_original_integrity_failure'):
        provider.read_original(MARKET, source)


def test_replays_restore_only_two_hooks_and_preserve_real_blocks(trials):
    for trial in trials.values():
        assert trial['candidate'] is False and trial['model_admission'] is False
        assert trial['hooks_restored'] and trial['protected_pointer_hashes_unchanged']
        assert trial['only_monkeypatched_hooks'] == [
            'issuer_independent_evidence.load_evidence', 'issuer_independent_evidence.validate_primary_actions']
    for ticker in ('BMOB3', 'CXSE3'):
        assert trials[ticker]['unchanged_secondary_calendar_gate']['reason'] == 'payments_do_not_cover_common_return_window'


def test_normal_financials_are_not_an_admission_or_empty_cash_zero(trials):
    current = trials['BMOB3']['normal_financial_diagnostic_only']
    assert current['normal_financial_current_gate_passed'] is True
    assert current['reported_EPS'] == 1.86 and str(current['filing_id']) == '155508'
    assert current['financial_current'][0] > 1.9
    assert current['candidate_financial_admission'] is False
    blau = trials['BLAU3']['normal_financial_diagnostic_only']
    assert blau['financial_current'] is None and blau['training_rows'] == 0
    assert blau['normal_financial_basis_gate_passed'] is False
    assert blau['financial_basis_start'] == '2026-01-06' and blau['fiscal_end'] == '2025-12-31'
    assert trials['CXSE3']['normal_financial_diagnostic_only']['financial_current'] is None


def test_incomplete_bemobi_audit_is_never_returned_as_safe_contract():
    with pytest.raises(ValueError, match='pending_a_BMOB_no_native_cash_entitlement_at_window_start'):
        provider.load_evidence(MARKET, provider.IDENTITIES['BMOB3'], provider.CUTOFF)
