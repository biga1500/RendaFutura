"""Actual-cache offline checks for the two-target recovery and core replays."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
import socket

import pytest

from . import round2_b3_eqma_recovery as recovery


@pytest.fixture(scope='module')
def corpus():
    if not (recovery.ROOT / recovery.B3_HTML).is_file():
        pytest.skip('primary cached originals are not distributed with the repository')
    return recovery.Corpus()


@pytest.fixture(scope='module')
def calendars(corpus):
    return {'B3SA3': recovery.recover_b3_calendar(corpus),
            'EQMA3B': recovery.recover_eqma_calendar(corpus)}


def test_real_core_ordinary_identity_gate_has_no_blanket_five_year_rule():
    audit = recovery.identity_policy_review()
    assert audit['ordinary_share_unchanged_five_year_identity_requirement'] is False
    assert 'require_window' not in audit['core_function']['literal_source']
    assert audit['core_function']['line'] > 0
    for actual in audit['actual_existing_cache_examples']:
        assert actual['identity_pass']
        assert not actual['historical_FCA_tables_read']
        assert len(actual['files_read']) == 1
        assert actual['files_read'][0]['file'].startswith('raw/expanded/b3-detail-')


def test_optional_continuity_claim_is_not_an_admission_requirement(corpus, monkeypatch):
    def not_a_core_requirement(*args, **kwargs):
        pytest.fail('recovery incorrectly imposed unchanged five-year identity')
    monkeypatch.setattr(recovery.identity_research.SecurityIdentityEvidence, 'require_window', not_a_core_requirement)
    assert corpus.current_binding('B3SA3').isin == 'BRB3SAACNOR6'
    assert corpus.current_binding('EQMA3B').isin == 'BREQMAACNOR8'


@pytest.mark.parametrize(('ticker', 'cvm', 'cnpj', 'isin'), (
    ('B3SA3', 21610, '09.346.601/0001-25', 'BRB3SAACNOR6'),
    ('EQMA3B', 16608, '06.272.793/0001-84', 'BREQMAACNOR8')))
def test_current_binding_is_native_typed_and_emits_no_alias(corpus, ticker, cvm, cnpj, isin):
    binding = corpus.current_binding(ticker)
    assert (binding.cvm, binding.cnpj, binding.share_class, binding.isin) == (cvm, cnpj, 'ON', isin)
    assert binding.historical_aliases == ()
    assert not binding.blanket_unchanged_five_year_identity_gate
    with pytest.raises(FrozenInstanceError):
        binding.isin = 'fabricated'


def test_B3_whole_native_calendar_has_no_unresolved_in_window_row(calendars):
    calendar = calendars['B3SA3']
    assert calendar['cash_schedule_recovered']
    assert len(calendar['events']) == 40
    assert not calendar['unresolved_issuer_rows']
    assert calendar['source_payment_observation_start'] == '2021-04-08'
    assert calendar['pre_window_unbound_source_rows_not_used_as_model_cash']
    assert len([e for e in calendar['events'] if e['payment_date'] > '2026-09-30']) == 3


def test_B3_early_revised_amount_uses_notice_not_old_RI_rounding(calendars):
    event = next(e for e in calendars['B3SA3']['events']
                 if e['record_date'] == '2021-09-28' and e['label'] == 'DIVIDENDO')
    assert event['gross_per_share_decimal'] == '0.15039690'
    assert event['evidence'][1]['issuer_row']['displayed_gross_decimal'] == '0.1506'
    assert any(c['kind'] == 'early_issuer_amount_superseded_by_native_notice'
               for c in calendars['B3SA3']['source_conflicts'])


def test_B3_latest_gross_version_supersedes_erroneous_net_form(calendars):
    event = next(e for e in calendars['B3SA3']['events'] if e['record_date'] == '2025-09-23')
    assert event['gross_per_share_decimal'] == '0.07829198340'
    assert event['evidence'][0]['native_form']['sequence'] == '952631'
    assert event['evidence'][0]['native_form']['version'] == 4
    assert Decimal(event['gross_per_share_decimal']) != Decimal('0.06654430274')


def test_B3_installment_rounding_is_read_not_equal_division(calendars):
    parts = [e for e in calendars['B3SA3']['events'] if e['record_date'] == '2025-12-30'
             and e['gross_per_share_decimal'].startswith('0.074485')]
    assert [e['gross_per_share_decimal'] for e in parts] == [
        '0.07448513949', '0.07448513949', '0.07448513948', '0.07448513948']
    assert sum(Decimal(e['gross_per_share_decimal']) for e in parts) == Decimal('0.29794055794')
    assert parts[-1]['payment_date'] == '2026-10-07' and parts[-1]['status'] == 'announced'
    separate = [e for e in calendars['B3SA3']['events'] if e['record_date'] == '2025-12-30'
                and e['gross_per_share_decimal'] == '0.08252953455']
    assert len(separate) == 1 and separate[0]['payment_date'] == '2026-01-12'


def test_signed_today_date_is_not_replaced_by_CVM_resubmission_date(corpus):
    reading = recovery.b3_notice_reading(corpus.document('454594'))
    assert reading['record_date'] == reading['signed_on'] == '2021-12-20'
    assert reading['received_on'] == '2021-12-30'
    assert reading['payment_date'] == '2021-12-30'


def test_EQMA_historical_class_dates_and_amounts_are_explicit(calendars):
    events = calendars['EQMA3B']['events']
    assert [(e['record_date'], e['payment_date'], Decimal(e['gross_per_share_decimal'])) for e in events] == [
        ('2021-08-24', '2021-09-30', Decimal('2.06')),
        ('2021-11-25', '2021-12-14', Decimal('1.25')),
        ('2022-04-28', '2022-06-23', Decimal('2.03')),
        ('2023-04-28', '2023-12-18', Decimal('2.792571337')),
        ('2024-04-29', '2024-07-15', Decimal('2.38')),
        ('2025-04-29', '2025-11-07', Decimal('2.73975972023')),
        ('2026-04-28', '2026-06-22', Decimal('0.82681917278'))]
    assert not calendars['EQMA3B']['historical_ticker_alias_emitted']
    assert all(e['cnpj'] == '06.272.793/0001-84' and e['share_class'] == 'ON' for e in events)


def test_EQMA_embedded_table_is_visual_native_proof_not_total_division(calendars):
    event = next(e for e in calendars['EQMA3B']['events'] if e['record_date'] == '2023-04-28')
    proof = event['evidence'][0]
    assert proof['visual_reading']['literal_row'] == ['Ordinarias', '28/04/2023', '2,792571337']
    assert not proof['visual_reading']['amount_computed_from_total']
    assert proof['earlier_native_form']['rows'][0]['isin'] == 'BREQMAACNOR8'
    assert proof['initial_form_two_decimal_amount'] == '2.79000000000'
    assert proof['initial_form_payment_date'] == '2023-12-31'
    assert proof['later_dated_notice_payment_authority'] == '2023-12-18'


def test_EQMA_source_conflict_outside_window_is_not_imputed(calendars):
    conflict = calendars['EQMA3B']['outside_window_conflict_not_used']
    assert conflict['sequence'] == '388749'
    assert not conflict['converted_to_zero_or_resolved_by_guess']
    assert not any(e['payment_date'] == '2021-05-14' for e in calendars['EQMA3B']['events'])


def test_actual_reconcile_payments_and_qualification_keep_future_rows(calendars):
    b3 = recovery.cash_replay(calendars['B3SA3'])
    eqma = recovery.cash_replay(calendars['EQMA3B'])
    assert (b3['ledger_rows'], b3['future_installments']) == (37, 3)
    assert (eqma['ledger_rows'], eqma['future_installments']) == (7, 0)
    assert all(e['payment_date'] <= '2026-09-30' for e in b3['ledger'])
    assert not b3['source_pass_or_model_admission_granted']
    assert not eqma['independent_ultimate_cash_authors_verified']


def test_calendar_observation_timestamp_comes_from_real_receipts(calendars):
    for calendar in calendars.values():
        expected = max((r['retrieved_at'] for r in recovery.receipts_in(calendar['events'])
                        if r['retrieved_at']), key=recovery.datetime.fromisoformat)
        assert recovery.payment_history(calendar).source_observed_at == expected


def test_incomplete_calendar_never_becomes_a_payment_history(calendars):
    calendar = deepcopy(calendars['B3SA3'])
    calendar['cash_schedule_recovered'] = False
    with pytest.raises(ValueError, match='incomplete_calendar'):
        recovery.payment_history(calendar)


def test_native_form_version_is_checked_against_exact_index(corpus):
    document = corpus.document('1060668')
    document['index']['record']['Versao'] = '999'
    with pytest.raises(ValueError, match='native_form_version_disagrees'):
        recovery.form_reading(document)


def test_missing_final_form_cannot_fall_back_to_old_revision(corpus, monkeypatch):
    actual = corpus.document
    def missing(sequence):
        if sequence == '809547':
            raise ValueError('original unavailable')
        return actual(sequence)
    monkeypatch.setattr(corpus, 'document', missing)
    with pytest.raises(ValueError, match='latest_reviewed_native_form_missing'):
        recovery.recover_b3_calendar(corpus)


@pytest.mark.parametrize('ticker', ('B3SA3', 'EQMA3B'))
def test_actual_price_reconciliation_fails_without_fabricated_anchors(corpus, ticker):
    price = recovery.replay_prices(corpus, ticker)
    assert price['literal_Yahoo_61_month_audit']['observed_required_months'] == 61
    assert price['actual_reconcile_prices_result'] == {
        'passes': False, 'reason': 'insufficient_independent_price_matches'}
    assert price['independent_anchors_available'] == 0
    assert not price['comparison_prices_created'] and not price['prices_fitted_or_vendor_days_replaced']
    assert price['source']['retrieved_at']


def test_exact_issuer_widget_response_is_not_promoted_from_EQTL_to_EQMA(corpus):
    result = recovery.price_discovery(corpus)
    assert result['EQMA3B']['actual_configured_tickers'] == ['EQTL3']
    assert not result['EQMA3B']['exact_EQMA3B_configured']
    assert result['EQMA3B']['EQTL3_history_downloads_duplicated'] == 0
    assert not result['B3SA3']['denied_scripts_fetched']
    assert not result['B3SA3']['alternate_CDN_or_endpoint_guessed']


def test_separate_EQMA_primary_filings_corroborate_historical_cash(corpus, calendars):
    checks = recovery.eqma_declaration_crosschecks(corpus, calendars['EQMA3B'])
    assert len(checks) == 7
    assert checks[0]['gross_per_share_decimal'] == '2.060000000000'
    assert checks[1]['gross_per_share_decimal'] == '1.25'
    assert checks[2]['gross_per_share_decimal'] == '2.030000000000'
    assert checks[-1]['one_cent_issuer_document_total_disagreement_retained']
    assert not checks[-1]['per_share_amount_computed_from_totals']


def test_real_one_replay_is_offline_restores_hooks_and_reaches_price_gate(corpus, calendars, monkeypatch):
    from . import inventory_candidates as core
    from . import issuer_independent_evidence as independent
    actual = (core._identity, core._payments, independent.load_evidence)
    def forbidden(*args, **kwargs):
        pytest.fail('offline source replay tried network or original-file writes')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    original_hash = hashlib.sha256((recovery.ROOT / recovery.B3_HTML).read_bytes()).hexdigest()
    result = recovery.actual_source_replay(corpus, calendars)
    assert (core._identity, core._payments, independent.load_evidence) == actual
    assert result['typed_identity_return_not_B3_JSON'] and result['pointers_unchanged']
    assert not result['core_files_edited']
    assert all(r['reason'] == 'insufficient_independent_price_matches' for r in result['results'])
    assert all(r['checks']['payment_calendar'] == 'window_spanning_reported_dates' for r in result['results'])
    assert hashlib.sha256((recovery.ROOT / recovery.B3_HTML).read_bytes()).hexdigest() == original_hash
