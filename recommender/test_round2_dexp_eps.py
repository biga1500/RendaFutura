"""Offline exact-current EPS, class, scope and ordinary-parser checks."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from . import inventory_financials as native
from . import inventory_candidates as candidates
from . import round2_dexp_eps as eps
from . import round2_dexp_prices as prices
from . import round2_share_actions as primary
from .collect_market_data import cvm_statements
from .market_inventory import load_inventory


@pytest.fixture(autouse=True)
def offline():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network_forbidden')):
        yield


@pytest.fixture(scope='module')
def proof():
    return eps.build_proof()


@pytest.fixture(scope='module')
def gates():
    original_patch = patch.object

    def checked_patch(target, attribute, *args, **kwargs):
        assert not (target is native and attribute == '_research')
        assert not (target is candidates and attribute in ('_one', 'reconcile_prices', '_payments'))
        assert not (target is prices and attribute == 'load_evidence')
        return original_patch(target, attribute, *args, **kwargs)

    with patch.object(patch, 'object', checked_patch):
        return eps.replay(adapter=False), eps.replay()


def test_exact_current_master_not_latest_usable_older_filing(proof):
    master = proof['latest_master']
    assert (master['CD_CVM'], master['CNPJ_CIA'], master['DT_REFER'], master['VERSAO'],
            master['ID_DOC'], master['DT_RECEB']) == (
        '016632', '02.193.750/0001-52', '2025-12-31', '1', '155652', '2026-03-24')
    assert proof['old_filing_or_revision_fallback'] is False


def test_original_native_package_members_and_immutable_transport(proof):
    members, container = eps.package_members()
    assert container['sha256'] == '24996eb39cb5b6e5b116816c6530469d3038dec2894d2c3f7214b0537b2a2c76'
    assert members['pdf']['sha256'] == '9fe7a7929b325e22971f2bad9611c1eb1a7fcdce07f4ff13ca2d9b7e6cc239ac'
    assert members['xml']['original_zip_member'] == '016632DFP31-12-2025v1.xml'
    assert members == proof['native_members']
    assert all(r['original_container']['receipt_file'] == container['receipt_file'] for r in members.values())


def test_annual_audited_pdf_is_separately_indexed_primary_same_year(proof):
    row = proof['audited_annual_index']['native_row']
    assert row['Data_Referencia'] == '2025-12-31' and row['Data_Entrega'] == '2026-03-24'
    assert row['Codigo_CVM'] == '16632' and row['Versao'] == '1'
    assert proof['audited_annual_index']['csv_line'] == 2989
    _, ref = eps.transport(eps.ROOT, 'audit')
    assert ref['sha256'] == '46de939a237f20ffb3dadb2e016fab4795737554613d22f2365b60493e71a077'


def test_primary_note_class_numerators_weighted_counts_and_EPS(proof):
    reading, = proof['readings']
    assert reading['parent_profit_brl'] == '143653000'
    assert reading['profit_allocations_brl'] == {'ON': '136449000', 'PN': '7204000'}
    assert [r['weighted_shares'] for r in reading['basic_eps']] == ['102602353', '5416933']
    assert all(r['value'] == '1.3299' and r['account'].startswith('note:2.5_basic_')
               for r in reading['basic_eps'])
    assert sum(Decimal(r['numerator_brl']) for r in reading['basic_eps']) == Decimal('143653000')
    assert sum(Decimal(r['weighted_shares']) for r in reading['basic_eps']) == Decimal('108019286')
    _, rows, _ = eps.current_tables()
    native._checked_note_eps(rows, reading)


def test_same_consolidated_scope_not_individual_CSV_EPS_transfer(proof):
    assert proof['DRE_scope_selected'] == 'consolidated'
    assert proof['individual_DRE_used_as_consolidated'] is False
    assert proof['DRE_controladora_only_weighted_share_row_used_for_consolidated_denominator'] is False
    assert proof['weighted_share_source'] == (
        'note_2.5_individual_AND_consolidated_scope_header_audited_page61_native_page64')
    assert proof['readings'][0]['dre_sha256'] == eps.DRE_SHA
    assert proof['readings'][0]['full_dre'] is True
    # Both the audited DRE consolidated EPS column and the combined-scope note
    # are required, not equality with the individual DRE alone.
    assert any(r['page'] == 30 and 'Consolidado' in r['literal'] for r in proof['audited_literal_readings'])
    assert any(r['page'] == 61 and 'individuais e consolidadas' in r['literal']
               for r in proof['audited_literal_readings'])
    assert {r['page'] for r in proof['native_literal_readings']} == {18, 63, 64}


def test_literal_locators_bind_text_offsets_and_page_hashes(proof):
    from pypdf import PdfReader
    sources = {s['file']: s for s in proof['sources']}
    readers = {}
    for locator in proof['native_literal_readings'] + proof['audited_literal_readings']:
        reference = sources[locator['file']]
        reader = readers.setdefault(locator['file'], PdfReader(io.BytesIO(
            primary.read(eps.ROOT, reference['file'], reference['sha256']))))
        text = reader.pages[locator['page'] - 1].extract_text()
        assert text[locator['text_start']:locator['text_end']] == locator['literal']
        assert primary.sha(text.encode('utf8')) == locator['page_text_sha256']


def test_registered_PNA_has_primary_unique_PN_binding_not_suffix_inference(proof):
    binding = proof['preferred_class_binding']
    assert binding['identity'] == eps.IDENTITIES['DEXP4']
    assert binding['registered_class'] == 'PNA' and binding['financial_source_class'] == 'PN'
    assert binding['only_preferred_class_in_each_received_FCA'] is True
    assert len(binding['historical_registry']) == 6
    assert [r['share_class'] for r in proof['readings'][0]['basic_eps']] == ['ON', 'PN']


def test_production_hook_returns_one_issuer_review_for_two_security_identities(proof):
    readings, sources = eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, list(eps.IDENTITIES.values()))
    review, = readings
    assert review['filing_id'] == '155652' and review['accounts_scope'] == 'consolidated'
    assert review['manifest_sha256'] == eps.REVIEW_SHA
    assert review['manifest_file'] == eps.review_file()
    assert len(review['basic_eps']) == 2
    assert sources == proof['sources']


def test_actual_production_research_dispatch_exact_classes_sector_one():
    specs = {'DEXP3': (16632, 'ON', 1), 'DEXP4': (16632, 'PNA', 1)}
    diagnostics, sources = [], []
    with patch.object(eps, 'get_dexp_eps_readings', wraps=eps.get_dexp_eps_readings) as hook:
        reviews = native._research(eps.ROOT, specs, eps.CUTOFF, diagnostics, sources)
    hook.assert_called_once_with(eps.ROOT, eps.CUTOFF, list(eps.IDENTITIES.values()))
    review = reviews[(16632, '2025-12-31', '1', '155652')]
    assert review['manifest_sha256'] == eps.REVIEW_SHA
    assert [r['share_class'] for r in review['basic_eps']] == ['ON', 'PN']
    assert any(r['sha256'] == eps.PINS['audit'][0] for r in sources)


@pytest.mark.parametrize('spec', [(16631, 'ON', 1), (16632, 'PN', 1), (16632, 'ON', 5)])
def test_production_research_does_not_expand_identity_or_sector_scope(spec):
    with patch.object(eps, 'get_dexp_eps_readings', side_effect=AssertionError('wrong_dispatch')) as hook:
        native._research(eps.ROOT, {'DEXP3': spec}, eps.CUTOFF, [], [])
    hook.assert_not_called()


def test_counterfactual_validates_primary_proof_before_suppression(monkeypatch):
    def invalid(*args, **kwargs):
        raise ValueError('invalid_primary_proof')

    monkeypatch.setattr(eps, 'get_dexp_eps_readings', invalid)
    monkeypatch.setattr(native, 'prepare_financials',
                        lambda *args: pytest.fail('unvalidated_counterfactual'))
    with pytest.raises(ValueError, match='invalid_primary_proof'):
        eps.replay(adapter=False)


def test_counterfactual_requires_actual_production_reader_call(monkeypatch):
    monkeypatch.setattr(native, 'prepare_financials', lambda *args: ([], [], []))
    with pytest.raises(ValueError, match='production_dexp_eps_hook_not_exercised'):
        eps.replay(adapter=False)


def test_actual_production_one_then_financial_before_after(gates):
    before, after = (r['records']['DEXP3'] for r in gates)
    assert before['source_candidate'] and after['source_candidate']
    assert before['financial_current_pass'] is False
    assert after['financial_current_pass'] is True
    assert before['financial_statement_current_pass'] is False
    assert after['financial_statement_current_pass'] is True
    assert before['pending_reason'] == 'ordinary_current_financial_unavailable'
    assert after['pending_reason'] is None
    assert all(r.get('profit_per_share') is None for r in before['latest_2025_records'])
    current, = after['latest_2025_records']
    assert (current['filing_id'], current['published_at'], current['fiscal_end'],
            current['accounts_scope'], current['share_class'], current['profit_per_share']) == (
        '155652', '2026-03-24', '2025-12-31', 'consolidated', 'ON', 1.3299)
    assert before['training_rows_after_normal_gates'] == after['training_rows_after_normal_gates'] == 0
    assert after['same_current_financial_features'][0][2] == 1.3299
    assert after['current_financial_call_used_candidate_cash'] is True


def test_production_quote_source_all_125_refs_and_required_window_unchanged(gates):
    before, after = (r['records']['DEXP3'] for r in gates)
    for row in (before, after):
        assert row['price_matches'] == 125
        assert row['cash_matches'] == 7 and row['ledger_rows'] == 10
        assert row['price_history_months'] == 120
        assert row['financial_basis_start'] == '2025-12-22'
        # The original cash gate requires a complete preceding 12-month window.
        assert row['training_since'] == '2022-09-30'
        assert row['source_checks']['prices'] == '61_consecutive_monthly_anchors'
        assert row['source_checks']['independent_price_matches'] == 125
        assert row['source_checks']['independent_raw_quote_evidence']['original_price_gate']['passed'] is True
        assert row['source_receipts']
    assert before['source_checks'] == after['source_checks']
    assert before['source_receipts'] == after['source_receipts']


def test_production_DEXP4_stays_source_blocked_with_PNA_EPS_only(gates):
    before, after = (r['records']['DEXP4'] for r in gates)
    for row in (before, after):
        assert row['source_candidate'] is False
        assert row['pending_reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
        assert row['financial_current_pass'] is None
        assert row['financial_features_called'] is False
        assert row['same_current_financial_features'] is None
        assert row['source_and_current_financial_pass'] is False
    assert before['financial_statement_current_pass'] is False
    assert after['financial_statement_current_pass'] is True
    record, = after['latest_2025_records']
    assert record['share_class'] == 'PNA' and record['profit_per_share'] == 1.3299
    assert record['financial_method']['source_share_class'] == 'PN'


def test_replay_changes_only_getter_for_counterfactual_never_core_or_evidence(gates):
    before, after = gates
    assert before['counterfactual']['proof_validated_before_suppression'] is True
    assert before['counterfactual']['production_reader_calls'] == 1
    assert before['counterfactual']['review_sha256'] == eps.REVIEW_SHA
    assert before['financial_hooks_overridden'] == ['get_dexp_eps_readings']
    assert after['counterfactual'] is None and after['financial_hooks_overridden'] == []
    for run in gates:
        assert run['production_research_unmodified'] is run['production_price_hooks_unmodified'] is True
        assert run['normal_one_called'] is run['normal_prepare_financials_called'] is True
        assert run['network_requests'] == 0
        assert run['cash_functions_patched'] is run['gates_relaxed'] is False
        assert run['original_caches_modified'] is run['model_publication_performed'] is False
        assert run['unknown_candidate_cash_replaced_with_zero'] is False
        assert run['protected_fingerprints_before'] == run['protected_fingerprints_after']


def test_parity_rejects_snapshot_outside_market_snapshots(tmp_path):
    with pytest.raises(ValueError, match='published_parity_snapshot_required'):
        eps.production_parity(snapshot=tmp_path)


@pytest.mark.parametrize('change', ['drop_old_equity', 'include_DEXP'])
def test_parity_cannot_shrink_or_replace_the_published_112_baseline(monkeypatch, change):
    target = eps.ROOT / 'snapshots/universe-20261026T010202-74f8d227/assets.json'
    original = Path.read_bytes
    altered = json.loads(original(target))
    if change == 'drop_old_equity':
        altered['assets'] = [r for r in altered['assets'] if r['ticker'] != 'BMEB4']
    else:
        row = next(r for r in altered['assets'] if r['ticker'] == 'BMEB4')
        row['ticker'] = 'DEXP3'

    def changed(path):
        return eps.primary.encoded(altered) if path == target else original(path)

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='exact_published_112_baseline_required'):
        eps.production_parity()


def test_ledger_audit_retains_documentary_difference_without_changing_inputs():
    old = {'ticker': 'BMEB4', 'record_date': '2023-03-10', 'payment_date': '2023-03-17',
           'source_row': 'primary:test', 'gross_per_unit': 0.308633,
           'primary_evidence': [{'file': 'original', 'sha256': 'a' * 64}]}
    new = deepcopy(old)
    new['primary_evidence'][0]['revision_search'] = {'through': '2026-09-30'}
    saved = deepcopy((old, new))
    audit = eps.ledger_comparison([old], [new])
    assert audit['ledger_inputs_equal'] is True and audit['ledger_equal'] is False
    change, = audit['ledger_evidence_only_changes']
    assert change['before_primary_evidence'] == old['primary_evidence']
    assert change['after_primary_evidence'] == new['primary_evidence']
    assert change['changed_fields'] == ['primary_evidence']
    assert (old, new) == saved


@pytest.mark.parametrize('field,value', [('gross_per_unit', 0.262338),
    ('payment_date', '2023-03-18'), ('share_normalization_factor', 1.125)])
def test_ledger_numeric_date_and_basis_mismatches_are_not_documentary(field, value):
    old = {'ticker': 'BMEB4', 'record_date': '2023-03-10', 'payment_date': '2023-03-17',
           'source_row': 'primary:test', 'gross_per_unit': 0.308633,
           'share_normalization_factor': 1.0, 'primary_evidence': []}
    changed = {**old, field: value}
    audit = eps.ledger_comparison([old], [changed])
    assert audit['ledger_inputs_equal'] is False and audit['ledger_equal'] is False
    assert audit['ledger_evidence_only_changes'] == []


@pytest.mark.parametrize('field,value', [('cvm', 16631), ('cvm', True),
    ('share_class', 'PN'), ('isin', 'BRDEXPACNPR8')])
def test_exact_security_mismatch_fails_closed(field, value):
    identity = deepcopy(eps.IDENTITIES['DEXP3'])
    identity[field] = value
    with pytest.raises(ValueError, match='exact_CVM_class_ISIN'):
        eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, [identity])


def test_outside_scope_duplicate_and_cutoff_contract():
    assert eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, [{'ticker': 'EQTL3'}]) == ([], [])
    with pytest.raises(ValueError, match='duplicate_requested_security'):
        eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, [eps.IDENTITIES['DEXP3']] * 2)
    with pytest.raises(ValueError, match='exact_cutoff'):
        eps.get_dexp_eps_readings(eps.ROOT, date(2026, 8, 31))


def test_changed_frozen_review_cannot_be_read():
    with patch.object(eps, 'REVIEW_SHA', '0' * 64):
        with pytest.raises(ValueError):
            eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF)


def test_pinned_receipt_hash_cannot_be_replaced():
    pins = deepcopy(eps.PINS)
    pins['package'] = (*pins['package'][:2], '0' * 64)
    with patch.object(eps, 'PINS', pins):
        with pytest.raises(ValueError, match='hash_changed'):
            eps.transport(eps.ROOT, 'package')


def test_recomputed_proof_must_equal_reviewed_immutable_proof(proof):
    changed = deepcopy(proof)
    changed['reported_EPS']['ON'] = '1.4111'
    with patch.object(eps, 'build_proof', return_value=changed):
        with pytest.raises(ValueError, match='frozen_proof_differs'):
            eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF)


def test_no_fallback_when_later_current_revision_appears():
    original = native._rows

    def changed(archive, member):
        rows = list(original(archive, member))
        if member == 'dfp_cia_aberta_2025.csv':
            master = next(r for r in rows if int(r['CD_CVM']) == 16632)
            rows.append({**master, 'VERSAO': '2', 'ID_DOC': '999999', 'DT_RECEB': '2026-03-25'})
        return rows

    with patch.object(native, '_rows', changed):
        with pytest.raises(ValueError, match='latest_filing_changed_no_older_fallback'):
            eps.current_tables()


@pytest.mark.parametrize('field,value', [('value', '1.4111'), ('weighted_shares', '116481187'),
    ('weighted_shares', '108019286'), ('numerator_brl', '143653000')])
def test_wrong_period_year_end_or_pooled_denominator_not_accepted(proof, field, value):
    review = deepcopy(proof['readings'][0])
    review['basic_eps'][0][field] = value
    with pytest.raises(ValueError):
        native._checked_note_eps(eps.current_tables()[1], review)


def test_consolidated_parent_numerator_must_not_be_replaced_by_prior_total(proof):
    review = deepcopy(proof['readings'][0])
    review['parent_profit_brl'] = '153199000'
    with pytest.raises(ValueError, match='documented_parent_profit_mismatch'):
        native._checked_note_eps(eps.current_tables()[1], review)


def test_actual_annual_archive_and_parser_accept_only_documented_note_eps(proof):
    inventory = load_inventory(eps.ROOT)
    sector = next(r['sector_code'] for r in inventory['stocks'] if r['ticker'] == 'DEXP3')
    specs = {'DEXP3': (16632, 'ON', sector), 'DEXP4': (16632, 'PN', sector)}
    content = primary.read(eps.ROOT, eps.ANNUAL, eps.ANNUAL_SHA)
    review = deepcopy(proof['readings'][0])
    review.update(manifest_file=eps.review_file(), manifest_sha256=eps.REVIEW_SHA)
    key = tuple(review[k] for k in ('cvm', 'fiscal_end', 'version', 'filing_id'))
    audit = []
    filtered, contexts = native._annual_archive(content, 2025, specs, eps.CUTOFF, audit, {key: review})
    rows = cvm_statements(filtered, 2025, specs=specs)
    assert {r['share_class'] for r in rows} == {'ON', 'PN'}
    assert all(r['filing_id'] == '155652' and r['profit_per_share'] == 1.3299 for r in rows)
    assert contexts[(16632, '2025-12-31', '1')]['accounts_scope'] == 'consolidated'
    assert contexts[(16632, '2025-12-31', '1')]['reasons'] == []
    assert primary.sha(content) == eps.ANNUAL_SHA
    recognized = [d for d in audit if d.get('kind') == 'account' and d.get('status') == 'recognized'
                  and d.get('account', '').startswith('note:')]
    assert {d['account'] for d in recognized} == {'note:2.5_basic_ON', 'note:2.5_basic_PN'}


def test_unreviewed_archive_zero_roots_are_not_class_EPS():
    specs = {'DEXP3': (16632, 'ON', 5)}
    audit = []
    content = primary.read(eps.ROOT, eps.ANNUAL, eps.ANNUAL_SHA)
    filtered, _ = native._annual_archive(content, 2025, specs, eps.CUTOFF, audit, {})
    assert cvm_statements(filtered, 2025, specs=specs) == []


def test_EPS_proof_does_not_resolve_DEXP4_price_conflict():
    before = prices.fingerprints(eps.ROOT)
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees'):
        prices.load_evidence(eps.ROOT, eps.IDENTITIES['DEXP4'], eps.CUTOFF)
    assert prices.fingerprints(eps.ROOT) == before


def test_sources_and_original_caches_remain_read_only():
    before = prices.fingerprints(eps.ROOT)
    readings, _ = eps.get_dexp_eps_readings(eps.ROOT, eps.CUTOFF, [eps.IDENTITIES['DEXP3']])
    assert readings and prices.fingerprints(eps.ROOT) == before


def test_no_PIT_or_model_admission_overclaim(proof):
    assert proof['PIT_status'] == 'experimental_retrospective_evidence_not_certified'
    for flag in ('individual_DRE_used_as_consolidated', 'price_factors_or_market_prices_used_to_derive_EPS',
                 'old_filing_or_revision_fallback', 'gates_relaxed', 'point_in_time_archive_verified', 'model_admission'):
        assert proof[flag] is False
    assert proof['reported_EPS_copied_not_computed'] is True
    assert proof['weights_copied_not_reconstructed'] is True
