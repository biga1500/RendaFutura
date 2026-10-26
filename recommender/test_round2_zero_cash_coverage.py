"""Offline evidence checks and counterfactual tests of typed interval semantics.

Synthetic typed inputs exercise the proposed gate only. They do not authorize
actual coverage or create payment rows for DESK3/BMOB3.
"""

import builtins
from dataclasses import asdict, replace
from datetime import date
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from . import round2_zero_cash_coverage as zero


@pytest.fixture(scope='module', autouse=True)
def offline_read_only():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('round2 zero verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
                            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(zero.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def verified():
    return zero.get_zero_cash_coverage()


def _synthetic_reviewed_contract(**changes):
    """Counterfactual loader output, never installed as an actual issuer proof."""
    proof = zero.PrimaryPeriodCoverageProof(
        **zero.IDENTITIES['DESK3'], period_start='2021-09-30', period_end='2022-12-21',
        basis=zero.CoverageBasis.PAID_CASH, components=('dividends', 'jcp'),
        scope='issuer_individual', source_refs=('synthetic-reviewed-source-for-interval-test',),
        primary_literal_review_verified=True, boundary_rights_resolved=True,
        gross_nonnetted=True, zero_basis='explicit_primary_no_payments_or_entitlements')
    return replace(proof, **changes)


def _decision(proofs, **kwargs):
    return zero.leading_gap_decision(zero.IDENTITIES['DESK3'], date(2022, 12, 22),
                                    zero.CoverageBasis.PAID_CASH, proofs, **kwargs)


def _mutate(monkeypatch, relative, transform):
    target = zero.ROOT / relative
    original = Path.read_bytes

    def read(path):
        body = original(path)
        return transform(body) if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', read)


def test_real_evidence_is_typed_and_never_exports_zero_payment_events(verified):
    assert len(verified['observations']) == 11
    assert all(type(row) is zero.PrimaryPeriodObservation for row in verified['observations'])
    assert verified['approved_period_coverage_proofs'] == ()
    assert verified['synthetic_cash_events'] == () and verified['model_admission'] is False
    assert all(not row.usable_as_leading_window_coverage for row in verified['observations'])
    assert all('gross_per_share' not in asdict(row) and 'payment_date' not in asdict(row)
               for row in verified['observations'])


def test_DESK_literal_non_distribution_is_not_extended_after_30_June(verified):
    statement, = [row for row in verified['observations'] if row.measure.startswith('explicit_non_')]
    assert statement.ticker == 'DESK3' and statement.components == ('dividends',)
    assert statement.period_start == statement.period_end == '2021-06-30'
    assert statement.period_end < zero.START.isoformat()
    assert statement.boundary_rights_resolved is False


def test_nonzero_payables_are_snapshots_not_zero_paid_cash(verified):
    balances = [row for row in verified['observations'] if 'payable_snapshot' in row.measure]
    assert {(row.ticker, row.period_end, row.reported_value) for row in balances} == {
        ('DESK3', '2021-12-31', '1482'), ('DESK3', '2022-03-31', '1482'),
        ('DESK3', '2022-09-30', '1482'), ('BMOB3', '2021-12-31', '17895')}
    assert all(not row.usable_as_leading_window_coverage for row in balances)


def test_BMOB_exact_native_prior_payments_remain_named_prewindow_evidence(verified):
    rows = [row for row in verified['observations'] if row.measure == 'named_prewindow_payee_not_public_holder_cash']
    assert {row.reported_value for row in rows} == {'178232', '8486'}
    assert all(row.period_start == row.period_end == '2021-04-26' for row in rows)
    source = verified['sources']['bmob-native-2021']
    assert source['original_container_sha256'] == zero.PINS['bmob-native-2021']
    assert source['sha256'] == '12deff57ef47b74a11908c4f3322d62e0160c1b36f22bc778bb032526f830119'
    assert source['original_zip_member'].startswith('112613_025500_')
    assert source['bank_settlement_verified'] is False


def test_annual_individual_rows_do_not_turn_missing_JCP_or_paid_lines_into_zero():
    evidence = zero.annual_source_observations()
    desk = evidence['issuers']['DESK3']['individual_distribution_rows']
    desk_paid = desk['dfp_cia_aberta_DFC_MI_ind_2021.csv']
    assert [r['DS_CONTA'] for r in desk_paid] == ['Dividendos recebidos']
    assert evidence['issuers']['DESK3']['absence_of_any_paid_component_is_not_zero'] is True
    bmob = evidence['issuers']['BMOB3']['individual_distribution_rows']
    assert bmob['dfp_cia_aberta_DFC_MI_ind_2021.csv'][0]['VL_CONTA'] == '-186718.0000000000'


def test_proposed_full_primary_coverage_preserves_actual_first_event_and_61_prices():
    result = _decision((_synthetic_reviewed_contract(),))
    assert result['leading_gap_covered'] is True and result['uncovered_intervals'] == []
    assert result['first_actual_date'] == '2022-12-22'
    assert result['window_start'] == '2021-09-30'
    assert result['first_actual_event_moved'] is False and result['synthetic_cash_events'] == []
    assert result['required_monthly_price_anchors'] == 61 and result['model_admission'] is False


def test_payment_and_entitlement_proofs_are_not_interchangeable():
    proof = _synthetic_reviewed_contract(basis=zero.CoverageBasis.ENTITLEMENT)
    assert _decision((proof,))['leading_gap_covered'] is False
    result = zero.leading_gap_decision(zero.IDENTITIES['DESK3'], date(2022, 4, 29),
        zero.CoverageBasis.ENTITLEMENT, (replace(proof, period_end='2022-04-28'),))
    assert result['leading_gap_covered'] is True


@pytest.mark.parametrize(('changes', 'reason'), [
    ({'ticker': 'BMOB3'}, 'wrong_issuer_or_security'),
    ({'cnpj': '00.000.000/0001-00'}, 'wrong_issuer_or_security'),
    ({'share_class': 'PN'}, 'wrong_issuer_or_security'),
    ({'isin': 'BRBMOBACNOR7'}, 'wrong_issuer_or_security'),
    ({'scope': 'consolidated'}, 'issuer_individual_scope_not_proved'),
    ({'components': ('dividends',)}, 'both_components_not_explicit'),
    ({'primary_literal_review_verified': False}, 'literal_primary_review_unavailable'),
    ({'source_refs': ()}, 'literal_primary_review_unavailable'),
    ({'boundary_rights_resolved': False}, 'opening_payables_or_pending_rights_unresolved'),
    ({'gross_nonnetted': False}, 'gross_zero_not_explicit_or_aggregate_netted'),
    ({'zero_basis': 'no_rows_found'}, 'gross_zero_not_explicit_or_aggregate_netted'),
])
def test_incomplete_or_wrong_scope_contract_cannot_qualify(changes, reason):
    result = _decision((_synthetic_reviewed_contract(**changes),))
    assert result['leading_gap_covered'] is False
    assert reason in result['rejected_proof_reasons']


def test_adjacent_intervals_cover_but_one_day_hole_is_retained():
    first = _synthetic_reviewed_contract(period_end='2021-12-31')
    later = _synthetic_reviewed_contract(period_start='2022-01-01')
    assert _decision((first, later))['leading_gap_covered'] is True
    result = _decision((first, replace(later, period_start='2022-01-02')))
    assert result['uncovered_intervals'] == [('2022-01-01', '2022-01-01')]


def test_empty_history_observation_or_fake_zero_event_is_not_a_coverage_proof(verified):
    assert _decision(())['leading_gap_covered'] is False
    for item in (verified['observations'][0], {'payment_date': '2021-09-30', 'gross_per_share': 0}):
        with pytest.raises(ValueError, match='^round2_zero_event_is_not_period_proof$'):
            _decision((item,))


def test_five_year_start_cannot_be_moved_to_first_distribution():
    with pytest.raises(ValueError, match='^round2_zero_fixed_window_required$'):
        _decision((_synthetic_reviewed_contract(),), start=date(2022, 12, 22))


@pytest.mark.parametrize('source', ('desk-2021', 'desk-2q2021', 'bmob-native-2021'))
def test_modified_original_bytes_rejected(monkeypatch, source):
    receipt = json.loads((zero.ROOT / zero.BASE / f'source-{source}.json').read_bytes())
    _mutate(monkeypatch, receipt['file'], lambda body: body + b'changed')
    with pytest.raises(ValueError, match='^round2_zero_source_changed$'):
        zero.document(source)


def test_denied_transport_cannot_become_literal_primary_evidence(monkeypatch):
    def denied(body):
        receipt = json.loads(body)
        receipt.update(status=403, error='access_stop_no_retry')
        return json.dumps(receipt).encode()

    _mutate(monkeypatch, f'{zero.BASE}/source-desk-2021.json', denied)
    with pytest.raises(ValueError, match='^round2_zero_source_changed$'):
        zero.document('desk-2021')


def test_unmodified_real_one_and_payments_stay_blocked_without_network():
    result = zero.legacy_gate_replay()
    for ticker in zero.IDENTITIES:
        assert result[ticker]['_one']['passed'] is False
        assert result[ticker]['_one']['checks']['prices'] == '61_consecutive_monthly_anchors'
        assert result[ticker]['_payments']['reason'] == 'payments_do_not_cover_common_return_window'


def test_report_keeps_BMOB_missing_dates_and_DESK_DDM_basis_unapproved():
    report = json.loads((zero.REPORTS / 'financial-zero-handoff.json').read_bytes())
    assert report['legacy_before'] == report['legacy_after']
    assert report['approved_period_coverage_proofs'] == [] and report['synthetic_cash_events'] == []
    assert report['findings']['BMOB3']['prior_calendar']['missing_primary_record_dates'] == 2
    assert report['findings']['BMOB3']['entitlement']['first_actual_record_date'] is None
    assert report['unapproved_secondary_DESK']['approved_for_model'] is False
    assert report['unapproved_secondary_DESK']['adjusted_notice_literal_present'] is True
    assert report['required_monthly_price_anchors'] == 61
    assert report['production_cash_gate_changed'] is False
    assert report['network']['robots_stop']['origin_retried_after_stop'] is False
