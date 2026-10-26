"""Offline primary gross/net, whole-group, and ordinary cash-gate checks."""

from copy import deepcopy
from datetime import date, datetime, timezone
import socket

import pytest

from . import round2_bmeb_cash as provider
from .inventory_candidates import _Cache
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page


ROOT = provider.ROOT.resolve()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('BMEB4 offline proof tried to access the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def proof():
    return provider.load_proof(ROOT, provider.IDENTITY)


@pytest.fixture(scope='module')
def parsed():
    return inspect_payment_page((ROOT / provider.PAYMENT_FILE).read_bytes(), 'BMEB4',
                               provider.CUTOFF, datetime(2026, 10, 25, 12, tzinfo=timezone.utc))


@pytest.fixture
def cache():
    return _Cache(ROOT, load_inventory(ROOT))


@pytest.fixture
def pinned_proof(monkeypatch, proof):
    monkeypatch.setattr(provider, 'load_proof', lambda *a, **k: deepcopy(proof))


def test_literal_PN_gross_net_and_date_roles_are_not_rate_inference(proof):
    fact, = proof['facts']
    assert fact['gross_per_share_decimal'] == '0.308633'
    assert fact['net_per_share_decimal'] == '0.262338'
    assert fact['label'] == 'JCP' and fact['share_class'] == 'PN'
    assert fact['isin'] == 'BRBMEBACNPR5'
    assert (fact['approval_date'], fact['published_at'], fact['record_date'], fact['ex_date'], fact['payment_date']) == (
        '2023-03-07', '2023-03-07', '2023-03-10', '2023-03-13', '2023-03-17')
    assert not fact['gross_reconstructed_from_net']
    grid = fact['source_locator']['normalized_grid']
    assert grid[2][1:] == ['R$ 0,308633', 'R$ 0,262338']
    assert grid[1][1:] == ['R$ 0,280576', 'R$ 0,238490']
    assert len(fact['source_locator']['date_and_type_clauses']) == 6
    assert '17 de mar' in fact['source_locator']['date_and_type_clauses'][1]['literal']


def test_primary_notice_is_byte_identical_at_CVM_and_issuer(proof):
    cvm_body, cvm = provider.source(ROOT, provider.NOTICE_URL)
    issuer_body, issuer = provider.source(ROOT, provider.ISSUER_NOTICE)
    assert cvm_body == issuer_body and cvm['sha256'] == issuer['sha256']
    assert cvm['receipt_sha256'] != issuer['receipt_sha256']
    document = proof['contract']['document']
    assert document['sequence'] == '594440' and document['version'] == 1
    assert document['index_evidence']['source_locator']['line'] == 25185
    assert proof['contract']['native_final_revision_verified']
    assert proof['contract']['cached_exchange_gross_amount_is_correct']
    assert not proof['contract']['amounts_derived_from_withholding_rate']
    assert not proof['contract']['bank_settlement_verified']


def test_board_minutes_and_issuer_gross_history_corroborate_the_selected_class(proof):
    assert proof['contract']['board_document']['sequence'] == '594437'
    assert len(proof['contract']['board_locators']) == 3
    assert proof['contract']['issuer_history']['literal'] == 'JCP Preferencial 0,308633 17/03/2023'
    assert 'Valor bruto' in proof['contract']['issuer_history']['header']
    assert proof['contract']['ON_rows_excluded_from_PN_group']
    assert proof['primary_actions']['actions'] == [{'date': '2021-12-03', 'ratio': 2.0}]


def test_group_repair_is_typed_and_original_sources_and_other_groups_are_preserved(parsed, cache):
    before = deepcopy(parsed)
    source_before = (ROOT / provider.PAYMENT_FILE).read_bytes()
    result = provider.apply_exact_repairs(parsed, provider.IDENTITY, provider.CUTOFF, cache)
    assert parsed == before
    assert (ROOT / provider.PAYMENT_FILE).read_bytes() == source_before
    assert [r for r in result['events'] if r.get('record_date') != provider.GROUP] == [
        r for r in before['events'] if r.get('record_date') != provider.GROUP]
    selected, = [r for r in result['events'] if r.get('record_date') == provider.GROUP]
    assert selected['gross_per_share'] == 0.308633
    assert selected['reported_original_fields'][0]['gross_per_share'] == 0.262338
    assert selected['published_at'] == '2023-03-07'
    assert selected['status'] == 'past_scheduled'
    assert not selected['settlement_verified'] and not selected['model_admitted']
    audit, = result['exact_cash_repairs'][provider.REPAIR_ID]['groups']
    assert audit['original_sum_decimal'] == '0.262338'
    assert audit['native_sum_decimal'] == '0.308633'


@pytest.mark.parametrize('field,value', [
    ('label', 'DIVIDENDO'), ('payment_date', '2023-03-16'), ('source_row', 'table:2:row:11'),
    ('gross_per_share', 0.308633), ('gross_per_share', 0.280576),
])
def test_any_changed_original_group_requires_fresh_primary_review(parsed, cache, pinned_proof, field, value):
    altered = deepcopy(parsed)
    event, = [r for r in altered['events'] if r.get('record_date') == provider.GROUP]
    event[field] = value
    with pytest.raises(ValueError, match='cash_repair_original_group_changed'):
        provider.apply_exact_repairs(altered, provider.IDENTITY, provider.CUTOFF, cache)


def test_complete_group_not_just_one_matching_row_is_required(parsed, cache, pinned_proof):
    altered = deepcopy(parsed)
    event, = [r for r in altered['events'] if r.get('record_date') == provider.GROUP]
    altered['events'].append(deepcopy(event))
    with pytest.raises(ValueError, match='cash_repair_original_group_changed'):
        provider.apply_exact_repairs(altered, provider.IDENTITY, provider.CUTOFF, cache)


def test_idempotency_does_not_trust_a_mutated_prior_audit(parsed, cache, pinned_proof):
    result = provider.apply_exact_repairs(parsed, provider.IDENTITY, provider.CUTOFF, cache)
    assert provider.apply_exact_repairs(result, provider.IDENTITY, provider.CUTOFF, cache) == result
    altered = deepcopy(result)
    event, = [r for r in altered['events'] if r.get('record_date') == provider.GROUP]
    event['gross_per_share'] = 0.262338
    altered['exact_cash_repairs'][provider.REPAIR_ID]['new_events'][0]['gross_per_share'] = 0.262338
    with pytest.raises(ValueError, match='bmeb_cash_reapplied_native_fact_or_contract_changed'):
        provider.apply_exact_repairs(altered, provider.IDENTITY, provider.CUTOFF, cache)


@pytest.mark.parametrize('field,value', [('ticker', 'BMEB3'), ('cvm', 1326), ('cvm', True),
                                      ('share_class', 'ON'), ('isin', 'BRBMEBACNOR8')])
def test_proof_never_crosses_issuer_or_share_class(field, value):
    identity = {**provider.IDENTITY, field: value}
    with pytest.raises(ValueError, match='bmeb_cash_exact_identity_and_cutoff_required'):
        provider.load_proof(ROOT, identity)


def test_other_cutoff_is_not_implicitly_authorized():
    with pytest.raises(ValueError, match='bmeb_cash_exact_identity_and_cutoff_required'):
        provider.load_proof(ROOT, provider.IDENTITY, date(2026, 8, 31))


@pytest.mark.parametrize('target', ['body', 'receipt'])
def test_original_notice_and_transport_receipt_are_both_pinned(tmp_path, target):
    _, ref = provider.source(ROOT, provider.NOTICE_URL)
    for relative in (ref['file'], ref['receipt_file']):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    path = tmp_path / ref['file' if target == 'body' else 'receipt_file']
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='round2_original_hash_changed'):
        provider.source(tmp_path, provider.NOTICE_URL)


def test_all_required_groups_are_audited_and_normal_cash_gate_accepts_exact_gross(cache, monkeypatch):
    from . import inventory_candidates as candidates
    from .share_basis import reconcile_payments
    inventory = load_inventory(ROOT)
    row, = [r for r in inventory['stocks'] if r['ticker'] == 'BMEB4']
    with monkeypatch.context() as baseline:
        baseline.setattr(provider, 'apply_exact_repairs', lambda parsed, *a, **k: parsed)
        original, expected, _, action_list = provider._native_calendar(row, cache)
    before = provider.audit_groups(original, expected, action_list)
    assert before['required_groups'] == 11
    assert [r['record_date'] for r in before['conflicts']] == ['2023-03-10']
    corrected, cash, _, action_list = provider._native_calendar(row, cache)
    after = provider.audit_groups(corrected, cash, action_list)
    assert after['required_groups'] == before['required_groups'] == 11
    assert after['conflicts'] == []
    assert cash == expected
    paid, ledger, adjustments = reconcile_payments('BMEB4', corrected, cash, action_list, provider.CUTOFF)
    assert len(adjustments) == 11 and len(ledger) == 12
    assert paid and all(r['passes_unchanged_cash_gate_arithmetic'] for r in after['all_groups'])
    changed, = [r for r in ledger if r['record_date'] == provider.GROUP]
    assert changed['gross_per_unit'] == 0.308633


def test_production_candidate_uses_all61_months_and_primary_cash_without_test_dispatch(cache):
    from .inventory_candidates import _one
    row, = [r for r in load_inventory(ROOT)['stocks'] if r['ticker'] == 'BMEB4']
    checks = {}
    candidate = _one(row, cache, provider.CUTOFF, checks)
    assert checks['independent_price_matches'] == 61
    assert checks['independent_raw_quote_evidence']['whole_61_month_comparison_gate_passed']
    assert checks['independent_raw_quote_evidence']['all_original_independent_quotes']
    assert all(r['agrees'] for r in checks['independent_raw_quote_evidence']['all_original_independent_quotes'])
    assert checks['primary_share_action_evidence']['actions'] == [{'date': '2021-12-03', 'ratio': 2.0}]
    assert provider.REPAIR_ID in checks['exact_cash_repairs']
    assert len(candidate['ledger']) == 12


def test_audit_captures_every_conflict_and_pre_window_record_paid_inside_window():
    from .collect_market_data import DividendHistory
    history = DividendHistory(provider.CUTOFF, 3)
    history.installments = [
        {'record_date': '2021-09-29', 'payment_date': '2021-10-15', 'gross_per_unit': 0.1},
        {'record_date': '2023-03-10', 'payment_date': '2023-03-17', 'gross_per_unit': 0.2},
        {'record_date': '2024-03-10', 'payment_date': '2024-03-17', 'gross_per_unit': 0.3},
    ]
    expected = {date(2021, 9, 29): 0.15, date(2023, 3, 10): 0.25}
    audit = provider.audit_groups(history, expected, [])
    assert audit['required_groups'] == len(audit['conflicts']) == 3
    assert {r['record_date'] for r in audit['conflicts']} == {'2021-09-29', '2023-03-10', '2024-03-10'}
    assert audit['conflicts'][-1]['independent_gross_amount'] is None
    assert not audit['favorable_groups_selected'] and not audit['net_to_gross_reconstruction']


def test_caller_cannot_relabel_the_original_parsed_security(parsed, cache, pinned_proof):
    altered = deepcopy(parsed)
    event, = [r for r in altered['events'] if r.get('record_date') == provider.GROUP]
    event['ticker'] = 'BMEB3'
    with pytest.raises(ValueError, match='bmeb_cash_parsed_group_security_mismatch'):
        provider.apply_exact_repairs(altered, provider.IDENTITY, provider.CUTOFF, cache)
