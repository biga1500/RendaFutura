from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import financial_scope_recheck as scope
from .collect_market_data import financial_features


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def wiz_proof():
    protected = [ROOT / 'current.json', ROOT / scope.additional.RESEARCH_FILE,
                 ROOT / 'raw/official-financials/dfp_cia_aberta_2025.zip',
                 Path(__file__).parent / 'inventory_financials.py',
                 Path(__file__).parent / 'prepare_inventory.py',
                 Path(__file__).parent / 'collect_market_data.py']
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
        result = scope._load_wiz_scope(ROOT, scope.CUTOFF)
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    return result


@pytest.fixture
def prepared(wiz_proof):
    record = deepcopy(wiz_proof[0])
    record.update(accounts_scope='consolidated', status='excluded',
                  profit_per_share=None, debt_ratio=None)
    older = dict(record, fiscal_end='2024-12-31', filing_id='historical-fixture',
                 published_at='2025-03-19')
    other = dict(record, cvm=99999, filing_id='unrelated-fixture')
    return [older, record, other], [
        {'kind': 'candidate', 'ticker': 'WIZC3', 'status': 'excluded',
         'reasons': ['basic_eps_absent_ambiguous_or_zero']}], []


@pytest.fixture
def cached_loader(monkeypatch, wiz_proof):
    monkeypatch.setattr(scope, '_load_wiz_scope', lambda *a: deepcopy(wiz_proof))


def _apply(prepared, **kwargs):
    return scope.apply_financial_scope_recheck(
        ROOT, {'WIZC3': scope.SPEC}, scope.CUTOFF, prepared,
        scope_requests=scope.SCOPE_REQUEST, **kwargs)


def test_no_request_is_an_exact_noop_without_any_source_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(scope, '_load_wiz_scope', lambda *a: pytest.fail('unexpected read'))
    original = ([], [], [])
    assert scope.apply_financial_scope_recheck(tmp_path, {}, date.today(), original) is original


@pytest.mark.parametrize('requests', [
    {'WIZC3': 'consolidated'}, {'WIZC3': True}, {'WIZC4': 'individual'},
    {'PINE4': 'individual'}, {'SIMH3': 'individual'}, {'WLMM3': 'individual'},
    {'WLMM4': 'individual'}, {'WIZC3': 'individual', 'WHRL4': 'individual'},
])
def test_request_cannot_authorize_other_scopes_or_conflicting_issuers(requests):
    with pytest.raises(ValueError, match='only_exact_explicit'):
        scope.apply_financial_scope_recheck(ROOT, {}, scope.CUTOFF, ([], [], []),
                                            scope_requests=requests)


@pytest.mark.parametrize('spec', [(23590, 'PN', 2), (23590, 'ON', 4), (1023, 'ON', 2),
                                (23590.0, 'ON', 2), (True, 'ON', 2), (23590, 'ON', 2.0)])
def test_candidate_identity_must_be_exact(spec):
    with pytest.raises(ValueError, match='exact_unique_wiz'):
        scope.apply_financial_scope_recheck(ROOT, {'WIZC3': spec}, scope.CUTOFF, ([], [], []),
                                            scope_requests=scope.SCOPE_REQUEST)


def test_duplicate_issuer_alias_is_not_authorized():
    with pytest.raises(ValueError, match='exact_unique_wiz'):
        scope.apply_financial_scope_recheck(
            ROOT, {'WIZC3': scope.SPEC, 'OTHER3': scope.SPEC}, scope.CUTOFF, ([], [], []),
            scope_requests=scope.SCOPE_REQUEST)


def test_unreviewed_cutoff_fails_before_reading_cache(tmp_path):
    with pytest.raises(ValueError, match='unreviewed_cutoff'):
        scope.apply_financial_scope_recheck(
            tmp_path, {'WIZC3': scope.SPEC}, date(2026, 10, 25), ([], [], []),
            scope_requests=scope.SCOPE_REQUEST)


def test_real_native_scope_uses_printed_eps_and_individual_balance(wiz_proof):
    record, sources = wiz_proof
    values, _ = financial_features([record], [], scope.CUTOFF, scope.SPEC)
    assert values == pytest.approx([0, 0, 1.25745, None, 0, 688049 / 1385841, 2])
    assert values[2] != 201076000 / 159907282
    assert values[5] != pytest.approx((2434061 - 1296137) / 2434061)
    assert record['financial_method']['liabilities_method'] == 'total_passivo_less_individual_equity'
    assert record['financial_method']['scope_recheck']['balance_roots_brl'] == {
        'assets': '1385841000', 'total_passivo': '1385841000', 'equity': '697792000',
        'current_liabilities': '271001000', 'noncurrent_liabilities': '417048000'}
    assert all(source['retrieved_at'] is None for source in sources)
    assert all(source['mtime_is_retrieval_or_publication'] is False for source in sources)
    ids = {source['id'] for source in sources}
    assert record['source_ref'] in ids
    assert record['financial_method']['primary_evidence'][0]['source_ref'] in ids
    json.dumps(wiz_proof, allow_nan=False)


def test_explicit_adapter_is_atomic_preserves_other_issuers_and_excludes_unreviewed_history(
        prepared, cached_loader):
    before = deepcopy(prepared)
    result = _apply(prepared)
    assert prepared == before
    assert [row for row in result[0] if row['cvm'] == 99999] == [before[0][2]]
    wiz = [row for row in result[0] if scope._is_wiz(row)]
    assert len(wiz) == 1 and wiz[0]['accounts_scope'] == 'individual'
    assert financial_features(result[0], [], date(2026, 3, 18), scope.SPEC) is None
    assert financial_features(result[0], [], date(2026, 3, 19), scope.SPEC)[0][2] == 1.25745
    assert result[1][0] == before[1][0]
    assert len(result[1][-1]['excluded_history']) == 1
    assert result[1][-1]['model_admission_granted'] is False
    assert result[1][-1]['production_integration_enabled'] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('fault', ['duplicate', 'new_version', 'wrong_filing', 'conflicting_id', 'missing'])
def test_prepared_latest_cannot_borrow_old_or_ambiguous_scope(prepared, fault, monkeypatch):
    monkeypatch.setattr(scope, '_load_wiz_scope', lambda *a: pytest.fail('premature source read'))
    if fault == 'duplicate':
        prepared[0].append(deepcopy(prepared[0][1]))
    elif fault == 'new_version':
        prepared[0].append(dict(prepared[0][1], version='2', filing_id='unreviewed-fixture'))
    elif fault == 'wrong_filing':
        prepared[0][1]['filing_id'] = 'unreviewed-fixture'
    elif fault == 'conflicting_id':
        prepared[0].append(dict(prepared[0][1], filing_id='unreviewed-fixture'))
    else:
        prepared[0][:] = [prepared[0][2]]
    before = deepcopy(prepared)
    with pytest.raises(ValueError, match='exact_unique_latest_prepared'):
        _apply(prepared)
    assert prepared == before


@pytest.mark.parametrize('fault', ['scope', 'balance', 'eps', 'fcf', 'statements', 'numerator',
                                 'weighted_shares', 'liability_label'])
def test_even_a_usable_but_mixed_or_modified_record_is_rejected(prepared, wiz_proof, monkeypatch, fault):
    record, sources = deepcopy(wiz_proof)
    if fault == 'scope':
        record['accounts_scope'] = 'consolidated'
    elif fault == 'balance':
        record['debt_ratio'] = (2434061 - 1296137) / 2434061
    elif fault == 'eps':
        record['profit_per_share'] = 201076000 / 159907282
    elif fault == 'fcf':
        record['free_cash_per_share'] = 0
    elif fault == 'statements':
        record['financial_method']['source_statements'][0] = 'dfp_cia_aberta_DRE_con_2025.csv'
    elif fault == 'liability_label':
        record['financial_method']['liabilities_method'] = 'total_passivo_less_consolidated_equity'
    else:
        field = 'numerator_brl' if fault == 'numerator' else 'weighted_shares'
        record['financial_method']['reported_basic_eps'][0][field] = '1'
    monkeypatch.setattr(scope, '_load_wiz_scope', lambda *a: (record, sources))
    before = deepcopy(prepared)
    with pytest.raises(ValueError):
        _apply(prepared)
    assert prepared == before


def test_missing_or_changed_pinned_review_fails_closed(tmp_path):
    with pytest.raises((OSError, ValueError)):
        scope._load_wiz_scope(tmp_path, scope.CUTOFF)


def test_rechecked_receipt_replaces_unverified_mtime_claim(prepared, wiz_proof, cached_loader):
    source = deepcopy(wiz_proof[1][-2])
    prepared[2].append(dict(source, retrieved_at=source['file_mtime_utc']))
    after = _apply(prepared)
    matches = [row for row in after[2] if row['id'] == source['id']]
    assert len(matches) == 1 and matches[0]['retrieved_at'] is None
    assert prepared[2][0]['retrieved_at'] is not None


def test_receipt_alias_does_not_leave_the_record_source_ref_dangling(prepared, wiz_proof, cached_loader):
    archive = next(row for row in wiz_proof[1] if row['id'] == wiz_proof[0]['source_ref'])
    prepared[2].append(dict(archive, id='caller-alias-fixture'))
    result = _apply(prepared)
    refs = {row['id'] for row in result[2]}
    assert 'caller-alias-fixture' in refs and wiz_proof[0]['source_ref'] in refs


def test_receipt_recheck_preserves_existing_provider_and_use_metadata(prepared, wiz_proof, cached_loader):
    source = deepcopy(next(row for row in wiz_proof[1] if row['file'].endswith('2015.zip')))
    prepared[2].append(dict(source, provider='CVM', year=2015, use='annual_model_features'))
    result = _apply(prepared)
    updated = next(row for row in result[2] if row['id'] == source['id'])
    assert updated['provider'] == 'CVM' and updated['year'] == 2015
    assert updated['use'] == 'annual_model_features'
    assert updated['retrieved_at'] is None


def test_real_native_blocked_record_passes_through_the_actual_explicit_adapter(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    original = ROOT / 'raw/official-financials/dfp_cia_aberta_2025.zip'
    content = original.read_bytes()
    diagnostics = []
    filtered, contexts = scope.native._annual_archive(
        content, 2025, {'WIZC3': scope.SPEC}, scope.CUTOFF, diagnostics)
    parsed = scope.cvm_statements(filtered, 2025, specs={'WIZC3': scope.SPEC}, diagnostics=diagnostics)
    assert parsed == []
    excluded = next(row for row in diagnostics if row.get('filing_id') == '155497'
                    and row.get('share_class') == 'ON')
    assert excluded['status'] == 'excluded'
    blocked = {field: excluded[field] for field in (
        'cvm', 'share_class', 'fiscal_end', 'version', 'filing_id', 'published_at', 'financial_method')}
    blocked.update(profit_per_share=None, free_cash_per_share=None, debt_ratio=None,
                   accounts_scope=contexts[(23590, '2025-12-31', '1')]['accounts_scope'])
    prepared = [blocked], diagnostics, []
    before = deepcopy(prepared)
    assert scope.financial_features(prepared[0], [], scope.CUTOFF, scope.SPEC) is None
    after = _apply(prepared)
    assert prepared == before
    assert scope.financial_features(after[0], [], scope.CUTOFF, scope.SPEC)[0][2] == 1.25745
    assert after[0][0]['accounts_scope'] == 'individual'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == hashlib.sha256(content).hexdigest()


def test_slice_comes_from_active_snapshot_instead_of_historical_bsli_audit():
    snapshot = json.loads((ROOT / 'snapshots' / scope.SNAPSHOT / 'collection-report.json').read_bytes())
    pending = scope._snapshot_pending(snapshot)
    assert len(pending) == 8 and 'BSLI3' not in pending
    assert pending['WLMM3'] == 'native_financial_eps_source_conflict'
    assert pending['WLMM4'] == 'financial_features_unavailable'


def test_normal_source_and_financial_replay_keeps_five_year_gates_and_no_own_training_rows(wiz_proof, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    result = scope.followon.replay_wiz_source_gates(ROOT, [wiz_proof[0]])
    assert result['ordinary_one_source_route_passed']
    assert result['normal_financial_features_and_share_basis_passed']
    assert result['checks']['prices'] == '61_consecutive_monthly_anchors'
    assert result['monthly_price_anchors'] == 120
    assert result['training_rows_after_unchanged_parent_filters'] == 0
    assert result['automatic_admission'] is False
