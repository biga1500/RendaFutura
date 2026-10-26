"""SLCE evidence fidelity and fail-closed, offline integration checks."""
from copy import deepcopy
from dataclasses import replace
from datetime import date
from fractions import Fraction
import json

import pytest

from . import slce_primary_actions as native
from .broad_stocks import split_price_history


@pytest.fixture(scope='module')
def proof():
    return native.proof_loader(dict(native.IDENTITY), native.CUTOFF)


@pytest.fixture(scope='module')
def quote_inputs():
    evidence = native.raw_quote_evidence(native.MARKET, dict(native.IDENTITY), native.CUTOFF, {})
    content = (native.MARKET / 'raw/expanded/SLCE3-prices.json').read_bytes()
    _, actions = split_price_history(content, 'SLCE3', native.CUTOFF, allow_recent_actions=True)
    return content, actions, evidence


@pytest.mark.parametrize('key,value', [
    ('ticker', 'SLCE4'), ('cvm', 20746), ('cvm', '20745'), ('cvm', True),
    ('share_class', 'PN'), ('isin', 'BRSLCEACNOR1'),
])
def test_exact_identity_is_required_before_reading_evidence(key, value):
    identity = {**native.IDENTITY, key: value}
    with pytest.raises(ValueError, match='exact_CVM_class_ISIN'):
        native.proof_loader(identity, native.CUTOFF, root='nonexistent')


@pytest.mark.parametrize('cutoff', [date(2026, 9, 29), date(2026, 10, 1), '2026-09-30'])
def test_cutoff_cannot_silently_reuse_another_review(cutoff):
    with pytest.raises(ValueError, match='exact_reviewed_cutoff'):
        native.proof_loader(dict(native.IDENTITY), cutoff, root='nonexistent')


def test_four_ratios_are_exact_native_share_count_fractions(proof):
    assert [a.total_factor for a in proof.actions] == [Fraction(11, 10), Fraction(11, 10),
                                                    Fraction(2), Fraction(9, 8)]
    assert all(not a.describe()['factor_fitted'] for a in proof.actions)
    assert all(a.describe()['payment_date'] is None for a in proof.actions)


def test_record_ex_credit_and_approval_are_distinct(proof):
    first, bonus, split, last = proof.actions
    assert first.record_date == '2021-12-30'
    assert first.ex_date == '2022-01-03'
    assert first.credit_date == '2022-01-05'
    assert bonus.approval_date == '2023-04-27'
    assert bonus.record_date == '2023-05-08'
    assert bonus.ex_date == '2023-05-09'
    assert bonus.credit_date is None
    assert split.record_date == split.approval_date == '2023-12-13'
    assert split.ex_date is None and split.credit_date is None
    assert 'D+3' in split.credit_terms
    assert last.record_date == '2025-12-30' and last.ex_date == '2026-01-02'


def test_proposal_alone_is_not_misrepresented_as_approval(proof):
    bonus = proof.actions[1]
    citations = {c['field']: c for c in bonus.citations}
    assert citations['record_and_ex_dates']['sha256'] == native.DOCUMENTS[608350]
    assert 'caso os acionistas aprovem' in citations['record_and_ex_dates']['native_text_checks']
    assert citations['approved_not_proposed']['sha256'] == native.DOCUMENTS[616714]
    assert citations['approved_ON_bonus_ratio']['page_one_based'] == 7


def test_publication_and_actual_retrieval_never_collapsed(proof):
    document = next(s for s in proof.sources if s['sha256'] == native.DOCUMENTS[616714])
    assert document['published_at'] == '2023-04-27'
    assert document['retrieved_at'].startswith('2026-10-25T')
    original_RI = next(s for s in proof.sources if s['sha256'] == native.RI_SPLIT_SHA)
    assert original_RI['document_date'] == '2023-12-13'
    assert original_RI['published_at'] is None
    assert not original_RI['point_in_time_archive_verified']
    assert all(native.read_source(native.MARKET, s) for s in proof.sources)


def test_missing_primary_ex_date_blocks_core_action_export(proof):
    assert not proof.complete
    assert proof.blockers == ('SLCE_primary_ex_date_unverified_2023_split',)
    with pytest.raises(ValueError, match='primary_ex_date_unverified'):
        proof.core_actions()
    with pytest.raises(ValueError, match='primary_ex_date_unverified'):
        proof.actions[2].core_action()


def test_strict_parent_adapter_does_not_reach_quotes_when_action_incomplete(monkeypatch, proof):
    monkeypatch.setattr(native, 'proof_loader', lambda *a, **k: proof)

    def forbidden(*a, **k):
        raise AssertionError('unresolved action must stop before quote supplement')

    monkeypatch.setattr(native, 'raw_quote_evidence', forbidden)
    with pytest.raises(ValueError, match='primary_ex_date_unverified'):
        native.get_slce_independent_raw_quotes(native.MARKET, dict(native.IDENTITY), native.CUTOFF, [], {})


@pytest.mark.parametrize('change', [
    {'kind': 'cash'}, {'new_shares': 0}, {'per_existing_shares': -1},
    {'ex_date': '2023-05-08'}, {'record_date': '2023-04-26'},
])
def test_invalid_or_relabelled_action_is_rejected(proof, change):
    with pytest.raises(ValueError):
        replace(proof.actions[1], **change)


def test_original_ENET_HTML_is_not_a_PDF_document():
    row, = native.primary_index_candidates(native.MARKET, {20745}, sequences={616722})
    body, receipt = native.cached(native.MARKET, url=row['url'])
    assert receipt['status'] == 200
    assert not body.startswith(b'%PDF')
    with pytest.raises(ValueError, match='expected_original_PDF_not_ENET_HTML'):
        native.pdf_pages(body)


def test_revised_dividend_notice_cannot_stand_in_for_bonus_notice():
    body, receipt = native.cached(native.MARKET,
        sha256='c680f379ad5883cde2def52f1e150d31c6bc3c8ea8c696bc8066537839693ebd')
    pages = native.pdf_pages(body)
    with pytest.raises(ValueError, match='semantic_field_mismatch'):
        native._citation(pages, receipt, 1, 'bonus_ratio', ('1 nova acao para cada 10 acoes',))


def test_hash_tampering_cannot_be_silently_reparsed(monkeypatch):
    real_read = native.read_source

    def changed(root, receipt):
        if receipt['sha256'] == native.DOCUMENTS[616714]:
            return real_read(root, {**receipt, 'sha256': '0' * 64})
        return real_read(root, receipt)

    monkeypatch.setattr(native, 'read_source', changed)
    with pytest.raises(ValueError, match='hash_mismatch'):
        native.proof_loader(dict(native.IDENTITY), native.CUTOFF)


def test_complete_monthly_grid_uses_all_61_real_observation_dates(quote_inputs):
    content, actions, evidence = quote_inputs
    audit = native.monthly_input_audit(content, evidence['native_rows'], actions)
    assert len(audit['anchors']) == 61 and audit['all_61_pass']
    assert audit['relative_tolerance'] == .006 and audit['absolute_tolerance'] == .015
    assert not audit['favorable_subset_selected']
    october_2021 = next(a for a in audit['anchors'] if a['month'] == '2021-10-31')
    assert october_2021['actual_observed_on'] == '2021-10-29'
    assert evidence['proof']['independent_window_dates'] == 1248


def test_one_missing_monthly_native_date_fails_whole_grid(quote_inputs):
    content, actions, evidence = quote_inputs
    rows = [r for r in evidence['native_rows'] if r['date'] != '2021-10-29']
    audit = native.monthly_input_audit(content, rows, actions)
    assert len(audit['anchors']) == 61 and not audit['all_61_pass']
    assert sum(not a['passes_unchanged_epsilon'] for a in audit['anchors']) == 1


def test_one_conflicting_monthly_native_close_is_retained_and_fails(quote_inputs):
    content, actions, evidence = quote_inputs
    rows = deepcopy(evidence['native_rows'])
    for row in rows:
        if row['date'] == '2021-10-29':
            row['native_close_decimal'] = '9999.99'
    audit = native.monthly_input_audit(content, rows, actions)
    assert not audit['all_61_pass']
    assert next(a for a in audit['anchors'] if a['actual_observed_on'] == '2021-10-29')[
        'Enfoque_original_raw_close_decimal'] == '9999.99'


def test_non_input_intramonth_difference_does_not_change_monthly_validation(quote_inputs):
    content, actions, evidence = quote_inputs
    rows = deepcopy(evidence['native_rows'])
    for row in rows:
        if row['date'] == '2021-10-28':
            row['native_close_decimal'] = '9999.99'
    assert native.monthly_input_audit(content, rows, actions)['all_61_pass']
    assert next(r for r in rows if r['date'] == '2021-10-28')['native_close_decimal'] == '9999.99'


def test_cash_reference_quote_is_not_erased_to_obtain_a_pass(quote_inputs):
    content, actions, evidence = quote_inputs
    observed = date(2021, 10, 29)
    with pytest.raises(ValueError, match='existing_exchange_native_quote_conflict'):
        native.raw_quote_evidence(native.MARKET, dict(native.IDENTITY), native.CUTOFF, {observed: 9999.99})


def test_offline_real_parent_one_and_financial_gates(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('test attempted network')

    monkeypatch.setattr(native.urllib.request.OpenerDirector, 'open', no_network)
    report = native.isolated_parent_trial()
    assert report['network_requests_during_trial'] == report['B3_network_requests'] == 0
    assert report['strict_candidate_count'] == 0
    assert report['strict_pending'][0]['reason'] == 'SLCE_primary_ex_date_unverified_2023_split'
    assert report['all_61_monthly_input_validation']['all_61_pass']
    price_calls = [c for c in report['reconcile_calls'] if c['kind'] == 'quote_only_non_admissible_diagnostic']
    assert price_calls[0]['status'] == 'passed_unchanged_price_gate'
    assert price_calls[0]['independent_dates'] == 1248
    assert price_calls[0]['required_monthly_prices'] == 61
    assert report['diagnostic_pending'][0]['reason'] == 'cash_amount_or_share_scale_disagrees_with_B3'
    assert report['cash_amount_or_scale_conflicts']
    annual, = report['financial_gates']['annual_candidate_audit']
    assert annual['filing_id'] == '155268' and annual['fiscal_end'] == '2025-12-31'
    assert annual['reasons'] == ['capex_not_identified']
    assert not report['financial_gates']['old_filings_fallback_used']
    assert not report['financial_gates']['EPS_rescaled']
    assert not report['model_admissions'] and not report['production_files_written']
    assert report['temporary_hooks_removed']
