"""Adversarial checks for the isolated exact-notice research, with no network."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import date
import ast
import inspect
import json
from pathlib import Path
import urllib.error

import pytest

from . import primary_cash_exact_research as research


FOLDER = Path(__file__).parent / 'data/market'
STOCK = {'ticker': 'AFLT3', **research.TARGETS['AFLT3']}


@pytest.fixture(scope='module')
def real_calendar():
    return research.verified_calendar(FOLDER, STOCK)


def test_full_native_entitlement_and_revised_start_dates(real_calendar):
    from decimal import Decimal
    events = real_calendar['events']
    assert [(r['gross_per_share_decimal'], r['payment_date']) for r in events] == [
        ('0.1485691041', '2023-05-04'), ('0.1140650017', '2023-05-04'),
        ('0.3316423104', '2023-12-27')]
    assert sum((Decimal(r['gross_per_share_decimal']) for r in events), Decimal(0)) == Decimal('0.5942764162')
    assert all(r['payment_date_basis'] == 'explicit_payment_start_date_not_settlement' for r in events)
    assert all(not r['is_settlement_confirmation'] and not r['cash_model_eligible'] for r in events)
    assert all(not r['gross_reconstructed_from_net'] for r in events)
    assert real_calendar['complete_groups'][0]['installment_count'] == 3
    assert real_calendar['complete_groups'][0]['source_chain']['superseded_payment_start'] == '2023-12-31'
    assert not real_calendar['history_complete']
    assert not real_calendar['active_model_changed']


def test_parent_contract_has_group_IDs_exact_format_tax_basis_and_every_actual_receipt(real_calendar):
    reader = research.shared.Reader(FOLDER)
    group = real_calendar['complete_groups'][0]
    assert group['id'] == 'AFLT3:2023-04-20'
    assert group['events'] == real_calendar['events']
    for event in real_calendar['events']:
        assert event['complete_group_id'] == group['id']
        assert event['source_locator']['format'] == 'exact_reviewed_native_entitlement_group'
        assert event['amount_tax_basis'] == 'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows'
        assert event['review_manifest_sha256'] == research.REVIEW_SHA
        assert event['target_group_complete'] is True
        assert event['verified_by_exact_cash_loader'] is True
    assert len(real_calendar['sources']) == 6
    closure = {source['file']: source for source in real_calendar['evidence_sources']}
    assert len(closure) == len(real_calendar['evidence_sources'])
    for pin in real_calendar['manifest_pins'].values():
        assert closure[pin['file']]['sha256'] == pin['sha256']
        assert closure[pin['file']]['url'] is None
    originals = [s for s in closure.values() if 'primary_CVM_original' in s['roles']]
    indices = [s for s in closure.values() if 'native_CVM_IPE_all_categories' in s['roles']]
    assert len(originals) == 10
    assert len(indices) == 6
    assert set(s['file'] for s in real_calendar['sources']).issubset(closure)
    for source in closure.values():
        body, actual = reader.read(source['file'], 'closure_bytes_check', source['sha256'])
        assert actual['bytes'] == source['bytes'] == len(body)
        if source['url']:
            assert source['url'].startswith(('https://dados.cvm.gov.br/', 'https://www.rad.cvm.gov.br/'))
            assert set(source['roles']) & {'primary_CVM_original', 'native_CVM_IPE_all_categories'}
        else:
            assert not set(source['roles']) & {'primary_CVM_original', 'native_CVM_IPE_all_categories'}


def test_exposed_receipts_are_an_independent_complete_reader_view(real_calendar):
    evidence = research.evidence(FOLDER)
    assert real_calendar['evidence_sources'] == list(evidence['reader'].sources.values())
    altered = deepcopy(real_calendar)
    altered['evidence_sources'][0]['roles'].clear()
    altered['events'][0]['complete_group_id'] = 'other-group'
    assert real_calendar['evidence_sources'][0]['roles']
    assert real_calendar['events'][0]['complete_group_id'] == 'AFLT3:2023-04-20'


def test_IPE_manifest_is_hash_pinned_before_any_index_selection(tmp_path):
    name = 'raw/primary-research/cvm-documents/index-sources.json'
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_bytes((FOLDER / name).read_bytes() + b' ')
    with pytest.raises(ValueError, match='hash_mismatch'):
        research.indexed_documents(research.shared.Reader(tmp_path))


def proposed_parent_predicate():
    """Compile only the parent's two requested allowlist additions in memory.

    The actual replacement, amount/action comparisons and candidate gates stay
    untouched. This is a proposed-hook simulation until the parent integrates.
    """
    from . import cash_calendar_research as calendar
    original = calendar._verified_primary_group_event
    tree = ast.parse(inspect.getsource(original))
    new_format = 'exact_reviewed_native_entitlement_group'
    new_basis = 'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows'

    class AllowOnlyRequestedTypes(ast.NodeTransformer):
        def visit_Compare(self, node):
            node = self.generic_visit(node)
            if (isinstance(node.left, ast.Name) and node.left.id == 'format_name'
                    and len(node.ops) == 1 and isinstance(node.ops[0], ast.NotEq)
                    and isinstance(node.comparators[0], ast.Constant)
                    and node.comparators[0].value == 'CVM_reviewed_old_notice_complete_group'):
                node.ops = [ast.NotIn()]
                node.comparators = [ast.Tuple(elts=[ast.Constant('CVM_reviewed_old_notice_complete_group'),
                                                   ast.Constant(new_format)], ctx=ast.Load())]
            return node

        def visit_Tuple(self, node):
            node = self.generic_visit(node)
            values = {item.value for item in node.elts if isinstance(item, ast.Constant)}
            if {'explicit_gross_exact_ISIN_unit', 'explicit_gross_per_share', 'dividend_per_share'} <= values:
                if new_basis not in values:
                    node.elts.append(ast.Constant(new_basis))
            return node

    tree = ast.fix_missing_locations(AllowOnlyRequestedTypes().visit(tree))
    namespace = dict(original.__globals__)
    exec(compile(tree, '<owned-test-only-parent-allowlist-simulation>', 'exec'), namespace)
    return namespace[original.__name__]


@pytest.mark.parametrize('fault', [None, 'group_ID', 'partial', 'first_only', 'wrong_total',
    'numeric_total', 'missing_source', 'wrong_class', 'net', 'gross_up', 'unknown_format', 'other_tax_basis'])
def test_proposed_complete_group_eligibility_keeps_all_other_parent_checks(real_calendar, fault):
    calendar = deepcopy(real_calendar)
    event = calendar['events'][0]
    group = calendar['complete_groups'][0]
    if fault == 'group_ID': event['complete_group_id'] = 'other-group'
    elif fault == 'partial': group['target_group_complete'] = False
    elif fault == 'first_only': group['events'] = group['events'][:1]
    elif fault == 'wrong_total': group['gross_total_decimal'] = '0.2626341058'
    elif fault == 'numeric_total': group['gross_total_decimal'] = .5942764162
    elif fault == 'missing_source': calendar['sources'] = []
    elif fault == 'wrong_class': event['share_class'] = 'PN'
    elif fault == 'net': event['gross_per_share_decimal'] = '0.126283738485'
    elif fault == 'gross_up': event['gross_reconstructed_from_net'] = True
    elif fault == 'unknown_format': event['source_locator']['format'] = 'unreviewed_other_notice'
    elif fault == 'other_tax_basis': event['amount_tax_basis'] = 'inferred_dividend_gross'
    assert proposed_parent_predicate()(event, calendar) is (fault is None)


def qualification_metadata(ledger):
    """Evaluate the actual flat-warning comprehension without staging or writes."""
    from . import prepare_inventory
    source = ast.parse(inspect.getsource(prepare_inventory))
    assignments = [node for node in ast.walk(source) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'calendar_qualifications' for target in node.targets)]
    assert len(assignments) == 1
    namespace = {'ledger': ledger}
    exec(compile(ast.fix_missing_locations(ast.Module(body=assignments, type_ignores=[])),
                 '<owned-test-only-flat-metadata-qualification>', 'exec'), namespace)
    return namespace['calendar_qualifications']


def test_flat_metadata_warns_on_all_three_explicit_starts_but_not_exact_schedules(real_calendar):
    starts = [{**event, 'gross_per_unit': float(event['gross_per_share_decimal'])}
              for event in real_calendar['events']]
    exact = {**starts[0], 'payment_date_basis': 'explicit_calendar_date', 'payment_date': '2023-05-05'}
    warnings = qualification_metadata(starts + [exact])
    assert len(warnings) == 3
    assert [r['payment_date'] for r in warnings] == ['2023-05-04', '2023-05-04', '2023-12-27']
    assert all(r['date_basis'] == 'explicit_payment_start_date_not_settlement' for r in warnings)
    assert all(r['full_settlement_on_date_verified'] is False for r in warnings)


def test_proposed_normal_replacement_hook_and_full_receipt_registration_pass_candidate_gates(monkeypatch, real_calendar):
    from . import cash_calendar_research as calendar
    from . import inventory_candidates as candidates
    from .market_inventory import load_inventory
    from .share_basis import b3_evidence
    inventory = load_inventory(FOLDER)
    stock = next(s for s in inventory['stocks'] if s['ticker'] == 'AFLT3')
    original_hook = calendar.apply_research_calendar
    monkeypatch.setattr(calendar, '_verified_primary_group_event', proposed_parent_predicate())

    def proposed_hook(identity, cache, cutoff, original, independent_rows, actions):
        result = original_hook(identity, cache, cutoff, original, independent_rows, actions)
        if identity['ticker'] == 'AFLT3':
            expected, _ = b3_evidence(independent_rows, identity['share_class'])
            result = calendar.replace_verified_primary_groups(result, real_calendar, expected, actions, cutoff)
            for source in real_calendar['evidence_sources']:
                cache.read(source['file'], 'exact_cash_evidence_closure')
                receipt = cache.receipts[source['file']]
                assert receipt['sha256'] == source['sha256']
                receipt['roles'] = list(source['roles'])
                receipt['url'] = source['url']
        return result

    monkeypatch.setattr(calendar, 'apply_research_calendar', proposed_hook)
    accepted, pending, receipts = candidates.build_candidates(FOLDER, {**inventory, 'stocks': [stock]}, research.CUTOFF)
    assert not pending, pending
    assert len(accepted) == 1
    row = accepted[0]
    target = [r for r in row['ledger'] if r['record_date'] == '2023-04-20']
    assert len(target) == 3
    assert [(r['payment_date'], r['gross_per_unit']) for r in target] == [
        ('2023-05-04', .1485691041), ('2023-05-04', .1140650017), ('2023-12-27', .3316423104)]
    assert all(r['payment_date_basis'] == 'explicit_payment_start_date_not_settlement' for r in target)
    assert all(r['full_settlement_on_date_verified'] is False for r in target)
    assert len(qualification_metadata(target)) == 3
    actual = {r['file']: r for r in receipts}
    for source in real_calendar['evidence_sources']:
        assert actual[source['file']]['sha256'] == source['sha256']
        assert actual[source['file']]['url'] == source['url']
    assert not row['financially_validated']
    result = {'schema_version': 1, 'status': 'proposed_normal_hook_passed_in_owned_tests_only',
        'API': 'verified_calendar(folder, stock, cutoff=date(2026,9,30))',
        'preferred_hook': 'apply_research_calendar: add verified calendar, then replace_verified_primary_groups(result, calendar, expected, actions, cutoff); register all evidence_sources',
        'parent_allowlist_simulation_only': ['exact_reviewed_native_entitlement_group',
            'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows'],
        'manifest_pins': real_calendar['manifest_pins'], 'calendar': real_calendar,
        'cash_matches': len(row['independent_cash_matches']), 'price_matches': len(row['independent_price_matches']),
        'target_ledger': target, 'flat_metadata_warnings': qualification_metadata(target),
        'all_actual_reader_receipts_registered': True, 'production_files_edited_by_this_test': False,
        'settlement_verified': False, 'thresholds_changed': False,
        'helper_sha256': research.shared._sha(Path(research.__file__).read_bytes()),
        'test_sha256': research.shared._sha(Path(__file__).read_bytes())}
    report = research.save_json(FOLDER, 'integration-audit', result)
    print('integration_audit=' + report)


def test_real_parent_integration_without_monkeypatches_financials_closure_and_five_start_warnings(real_calendar):
    from collections import Counter
    from . import cash_calendar_research as calendar
    from . import inventory_candidates as candidates
    from . import prepare_inventory
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory

    inventory = load_inventory(FOLDER)
    stocks = [s for s in inventory['stocks'] if s['ticker'] in ('AFLT3', 'ENMT3', 'ENMT4')]
    assert {s['ticker'] for s in stocks} == {'AFLT3', 'ENMT3', 'ENMT4'}
    parent_files = [Path(inspect.getsourcefile(module)) for module in (calendar, candidates, prepare_inventory)]
    before = {str(p): research.shared._sha(p.read_bytes()) for p in parent_files}
    assert candidates._payments.__module__ == 'recommender.inventory_candidates'
    assert calendar.apply_research_calendar.__module__ == 'recommender.cash_calendar_research'

    # These are the real entry points. No fixture replaces source loading,
    # group eligibility, cash/price comparison, annual features or training.
    accepted, pending, receipts = candidates.build_candidates(
        FOLDER, {**inventory, 'stocks': stocks}, research.CUTOFF)
    assert not pending, pending
    by_ticker = {row['ticker']: row for row in accepted}
    assert set(by_ticker) == {'AFLT3', 'ENMT3', 'ENMT4'}
    aflt = by_ticker['AFLT3']
    assert len(aflt['independent_cash_matches']) == 12
    assert len(aflt['independent_price_matches']) == 12
    assert aflt['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert not aflt['financially_validated']
    target = [r for r in aflt['ledger'] if r['record_date'] == '2023-04-20']
    assert [(r['payment_date'], r['gross_per_unit']) for r in target] == [
        ('2023-05-04', .1485691041), ('2023-05-04', .1140650017), ('2023-12-27', .3316423104)]
    assert all(r['payment_date_basis'] == 'explicit_payment_start_date_not_settlement' for r in target)
    assert all(r['full_settlement_on_date_verified'] is False for r in target)
    assert all(r['financial_confirmation'] == 'reported_calendar_independently_amount_reconciled_not_settlement_confirmed'
               for r in target)

    source_rows = {r['file']: r for r in receipts}
    originals = real_calendar['sources']
    indices = [r for r in real_calendar['evidence_sources'] if 'native_CVM_IPE_all_categories' in r['roles']]
    assert len(originals) == 6
    assert len(indices) == 6
    for required in originals + indices:
        actual = source_rows[required['file']]
        assert actual['sha256'] == required['sha256']
        assert actual['url'] == required['url']
        assert actual['retrieved_at'] is None
    for required in real_calendar['evidence_sources']:
        assert source_rows[required['file']]['sha256'] == required['sha256']
        assert source_rows[required['file']].get('url') == required['url']

    statements, financial_audit, financial_sources = prepare_financials(FOLDER, stocks, research.CUTOFF)
    financial_checks = {}
    for ticker, candidate in by_ticker.items():
        current = financial_features(statements, candidate['payments'], research.CUTOFF,
            (candidate['cvm'], candidate['share_class'], candidate['sector_code']))
        assert current is not None, (ticker, financial_audit)
        features, filing = current
        basis = date.fromisoformat(candidate['financial_basis_start'])
        assert date.fromisoformat(filing['fiscal_end']) >= basis
        assert filing['published_at'] <= research.CUTOFF.isoformat()
        training, _ = stock_observations(ticker, candidate['prices'], candidate['payments'],
            statements, research.CUTOFF, spec=candidate['statement_spec'])
        eligible_training = [r for r in training if date.fromisoformat(r['as_of']) >= candidate['training_since']
                             and date.fromisoformat(r['fiscal_end']) >= basis]
        assert eligible_training, ticker
        financial_checks[ticker] = {'features': features, 'filing_id': filing['filing_id'],
            'fiscal_end': filing['fiscal_end'], 'published_at': filing['published_at'],
            'financial_basis_start': basis.isoformat(), 'eligible_training_rows': len(eligible_training)}

    ledger = [r for candidate in accepted for r in candidate['ledger']]
    warnings = qualification_metadata(ledger)
    assert len(warnings) == 5
    assert Counter(r['ticker'] for r in warnings) == Counter({'AFLT3': 3, 'ENMT3': 1, 'ENMT4': 1})
    assert all(r['date_basis'] == 'explicit_payment_start_date_not_settlement' for r in warnings)
    assert all(r['full_settlement_on_date_verified'] is False for r in warnings)
    assert {str(p): research.shared._sha(p.read_bytes()) for p in parent_files} == before
    result = {'schema_version': 1, 'status': 'real_unmodified_parent_integration_passed',
        'monkeypatches': False, 'legacy_apply_verified_patch_used': False,
        'entry_point': 'recommender.inventory_candidates.build_candidates',
        'cash_matches': 12, 'price_matches': 12, 'target_ledger': target,
        'flat_metadata_warning_count': 5, 'flat_metadata_warnings': warnings,
        'financial_features_and_training': financial_checks,
        'financial_source_receipts': financial_sources,
        'all_six_originals_and_six_indices_present_with_verified_SHA_URL': True,
        'unknown_retrieval_timestamps_remain_null': True,
        'all_reader_receipts_present': True, 'evidence_sources': real_calendar['evidence_sources'],
        'manifest_pins': real_calendar['manifest_pins'], 'parent_file_hashes': before,
        'helper_sha256': research.shared._sha(Path(research.__file__).read_bytes()),
        'test_sha256': research.shared._sha(Path(__file__).read_bytes()),
        'parent_files_unchanged_during_test': True, 'pointers_written': False,
        'settlement_verified': False, 'thresholds_changed': False}
    report = research.save_json(FOLDER, 'real-parent-integration', result)
    print('real_parent_integration=' + report)


@pytest.mark.parametrize('field,value', [('ticker', 'AFLT4'), ('cvm', 9342),
    ('share_class', 'PN'), ('isin', 'BRPNVLACNOR9')])
def test_wrong_issuer_class_or_ISIN_cannot_request_patch(field, value):
    stock = {**STOCK, field: value}
    with pytest.raises(ValueError):
        research.verified_calendar(FOLDER, stock)


def test_different_cutoff_cannot_reuse_retrospective_patch():
    with pytest.raises(ValueError, match='cutoff'):
        research.verified_calendar(FOLDER, STOCK, date(2023, 5, 4))


@pytest.mark.parametrize('ticker', ['PNVL3', 'EZTC3', 'BEES3', 'BEES4', 'ALOS3', 'ENMT3'])
def test_unresolved_or_out_of_scope_security_has_no_patch(ticker):
    with pytest.raises(ValueError, match='no_patch'):
        research.verified_calendar(FOLDER, {'ticker': ticker, **research.TARGETS.get(ticker, {})})


@pytest.mark.parametrize('kind', ['original', 'review', 'index', 'audit'])
def test_changed_original_review_index_or_snapshot_hash_is_rejected(monkeypatch, kind):
    original_read = research.shared.Reader.read

    def altered(reader, name, role, expected=None, url=None):
        match = (kind == 'original' and role == 'primary_CVM_original'
                 or kind == 'review' and name.endswith('/reviewed-notices.json')
                 or kind == 'index' and role == 'native_CVM_IPE_all_categories'
                 or kind == 'audit' and role == 'pinned_staged_audit')
        if match:
            raise ValueError('original_content_hash_mismatch')
        return original_read(reader, name, role, expected, url)

    monkeypatch.setattr(research.shared.Reader, 'read', altered)
    with pytest.raises(ValueError, match='hash_mismatch'):
        research.verified_calendar(FOLDER, STOCK)


def test_real_reader_rejects_actual_tampered_original_bytes(tmp_path):
    reader = research.shared.Reader(FOLDER)
    docs = {d['sequence']: d for d in research.indexed_documents(reader)[0]}
    body, source = research.document_body(reader, docs['613044'])
    destination = tmp_path / source['file']
    destination.parent.mkdir(parents=True)
    destination.write_bytes(body + b'changed original')
    with pytest.raises(ValueError, match='hash_mismatch'):
        research.native(research.shared.Reader(tmp_path), docs['613044'], research.TARGETS['AFLT3'])


@pytest.fixture(scope='module')
def native_installments():
    import io
    import pdfplumber
    reader = research.shared.Reader(FOLDER)
    docs = {d['sequence']: d for d in research.indexed_documents(reader)[0]}
    body, _ = research.document_body(reader, docs['742120'])
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [table for page in pdf.pages for table in page.extract_tables()]
    return docs['742120'], tables


@pytest.mark.parametrize('fault', ['first_only', 'annex_absent', 'net_header', 'wrong_ISIN',
    'second_ISIN', 'missing_date', 'zero', 'duplicate', 'wrong_issuer', 'wrong_version'])
def test_native_annex_cannot_supply_partial_net_unmapped_or_wrong_identity_cash(native_installments, monkeypatch, fault):
    import pdfplumber
    from types import SimpleNamespace
    document, original_tables = native_installments
    tables = deepcopy(original_tables)
    if fault == 'first_only': tables[2].pop()
    elif fault == 'annex_absent': tables[2] = tables[2][:1]
    elif fault == 'net_header': tables[1][1][1] = 'Valor Liquido (R$/Unidade)'
    elif fault == 'wrong_ISIN': tables[1][2][0] = 'BRAFLTACNOR1'
    elif fault == 'second_ISIN': tables[1].append(['BRAFLTACNOR1', '0,1', 'Anual', '2024', 'Nao', 'Parcelado', ''])
    elif fault == 'missing_date': tables[2][2][0] = ''
    elif fault == 'zero': tables[2][2][1] = '0'
    elif fault == 'duplicate': tables[2].append(deepcopy(tables[2][1]))
    elif fault == 'wrong_issuer': tables[0][1][0] = 'Other Issuer S.A.'
    elif fault == 'wrong_version': tables[0][7][1] = '1'

    @contextmanager
    def altered_tables(*args, **kwargs):
        yield SimpleNamespace(pages=[SimpleNamespace(extract_tables=lambda: tables)])

    monkeypatch.setattr(pdfplumber, 'open', altered_tables)
    with pytest.raises(ValueError):
        research.native(research.shared.Reader(FOLDER), document, research.TARGETS['PNVL3'])


def test_mutating_returned_calendar_does_not_change_future_verified_view(real_calendar):
    result = deepcopy(real_calendar)
    result['events'][0]['reported_per_share_decimal'] = '0'
    result['complete_groups'][0]['events'] = []
    result['sources'] = []
    actual = research.verified_calendar(FOLDER, STOCK)
    assert actual['events'][0]['reported_per_share_decimal'] == '0.1485691041'
    assert len(actual['complete_groups'][0]['events']) == 3


@pytest.fixture(scope='module')
def source_history():
    from .inventory_candidates import _Cache, _payments
    from .market_inventory import load_inventory
    inventory = load_inventory(FOLDER)
    cache = _Cache(FOLDER, inventory)
    history, _ = _payments('AFLT3', cache, research.CUTOFF)
    return inventory, cache, history


def test_in_memory_patch_keeps_conflicts_other_entitlements_and_input_unchanged(source_history):
    _, _, original = source_history
    before = deepcopy(original.installments)
    result = research.apply_verified_patch(FOLDER, STOCK, research.CUTOFF, original)
    assert original.installments == before
    record = '2023-04-20'
    assert [r for r in result.installments if r['record_date'] != record] == [r for r in before if r['record_date'] != record]
    assert len([r for r in result.installments if r['record_date'] == record]) == 3
    correction = result.research_group_corrections[-1]
    assert correction['old_calendar_rows'] == [r for r in before if r['record_date'] == record]
    assert not correction['settlement_verified']
    assert correction['old_source_conflict_preserved_in_audit']


@pytest.mark.parametrize('fault', ['first_only', 'drop_middle', 'drop_last', 'zero', 'net',
    'wrong_class', 'wrong_record', 'stale_date'])
def test_parent_reconciliation_rejects_incomplete_or_changed_entitlement(source_history, fault):
    from .share_basis import b3_evidence, reconcile_payments
    from .inventory_candidates import _cash_pages
    inventory, cache, original = source_history
    detail = cache.json('raw/expanded/b3-detail-22179.json', 'cached_identity')
    rows = _cash_pages('AFLT3', {r['code']: r['isin'] for r in detail['otherCodes']}, 22179, cache)
    expected, _ = b3_evidence(rows, 'ON')
    result = research.apply_verified_patch(FOLDER, STOCK, research.CUTOFF, original)
    target = [r for r in result.installments if r['record_date'] == '2023-04-20']
    if fault == 'first_only':
        result.installments = [r for r in result.installments if r not in target[1:]]
    elif fault == 'drop_middle': result.installments.remove(target[1])
    elif fault == 'drop_last': result.installments.remove(target[2])
    elif fault == 'zero': target[2]['gross_per_unit'] = 0
    elif fault == 'net': target[2]['gross_per_unit'] *= .85
    elif fault == 'wrong_class':
        # Amount reconciliation alone cannot validate a class. The verified
        # adapter must reject it before any unchanged parent cash gate runs.
        with pytest.raises(ValueError, match='identity'):
            research.verified_calendar(FOLDER, {**STOCK, 'share_class': 'PN'})
        return
    elif fault == 'wrong_record': target[2]['record_date'] = '2023-04-19'
    elif fault == 'stale_date':
        target[2]['payment_date'] = '2023-12-31'
        # The parent amount gate is date-insensitive within a record group.
        # Fresh byte verification supplies the authoritative current start.
        verified = research.verified_calendar(FOLDER, STOCK)
        assert verified['events'][2]['payment_date'] == '2023-12-27'
        assert target[2]['payment_date'] != verified['events'][2]['payment_date']
        return
    with pytest.raises(ValueError):
        reconcile_payments('AFLT3', result, expected, [], research.CUTOFF)


def test_entire_unchanged_candidate_pipeline_passes_with_only_scoped_in_memory_adapter(monkeypatch):
    from . import inventory_candidates as candidates
    from .market_inventory import load_inventory
    before = research.protected_state(FOLDER)
    inventory = load_inventory(FOLDER)
    stock = next(s for s in inventory['stocks'] if s['ticker'] == 'AFLT3')
    original_reader = candidates._payments

    def reader(ticker, cache, cutoff, primary=None, *, coverage_start=None, identity=None):
        history, url = original_reader(ticker, cache, cutoff, primary,
                                       coverage_start=coverage_start, identity=identity)
        return research.apply_verified_patch(cache.folder, stock, cutoff, history), url

    monkeypatch.setattr(candidates, '_payments', reader)
    accepted, pending, sources = candidates.build_candidates(
        FOLDER, {**inventory, 'stocks': [stock]}, research.CUTOFF)
    assert not pending, pending
    assert len(accepted) == 1
    row = accepted[0]
    assert len(row['independent_cash_matches']) >= 4
    assert len(row['independent_price_matches']) >= 4
    assert not row['financially_validated']
    assert row['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    target = [r for r in row['ledger'] if r['record_date'] == '2023-04-20']
    assert len(target) == 3
    assert {r['payment_date'] for r in target} == {'2023-05-04', '2023-12-27'}
    assert all(r['payment_date_basis'] == 'explicit_payment_start_date_not_settlement' for r in target)
    assert all(r['full_settlement_on_date_verified'] is False for r in target)
    assert any(s['file'].startswith(research.CACHE + '/') for s in sources)
    assert research.protected_state(FOLDER) == before


def test_blocked_report_keeps_full_PNVL_annex_and_later_EZTC_revision(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('offline_report_attempted_network')
    monkeypatch.setattr(research.urllib.request.OpenerDirector, 'open', no_network)
    report = research.build_report(FOLDER)
    pn, ez = report['results'][1:]
    assert pn['status'] == ez['status'] == 'blocked_no_patch'
    assert pn['exact_missing_sequences'] == ['741697', '741696']
    assert [r['gross'] for r in pn['native_v2_schedule_observed']['installments']] == ['0.05911502000', '0.01880932000']
    assert ez['exact_missing_sequences'] == ['509579']
    assert ez['base_amount_decimal'] != ez['intermediate_amount_decimal']
    assert report['network']['attempts'] <= 12
    assert not report['thresholds_changed']
    assert not report['pointers_written']
    assert report['protected_files_unchanged_during_build']


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_fresh_fetcher_and_other_document_paths(tmp_path, monkeypatch, status):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'stop', {}, None)
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = Opener()
    fetcher._get(research.shared.ROBOT_URL)
    second = research.Fetcher(tmp_path)
    with pytest.raises(ValueError, match='access_stop'):
        second._get('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo=1&numSequencia=1&numVersao=1')
    assert calls == [research.shared.ROBOT_URL]


@pytest.mark.parametrize('url', ['https://ri.azzas2154.com.br/x', 'https://allos.com.br/',
    'https://www.santander.com.br/ri/', 'https://www.engie.com.br/',
    'https://cdn-sites-assets.mziq.com/x', 'https://www.b3.com.br/',
    'http://www.rad.cvm.gov.br/robots.txt', 'https://www.rad.cvm.gov.br.evil.test/',
    'https://user:secret@www.rad.cvm.gov.br/robots.txt'])
def test_denied_issuer_exchange_proxy_and_auth_URLs_never_requested(tmp_path, url):
    with pytest.raises(ValueError, match='unapproved'):
        research.Fetcher(tmp_path)._get(url)
    assert not list(tmp_path.rglob('request-*.json'))


@pytest.mark.parametrize('name', ['../outside.json', 'raw/production.json', 'C:/outside', 'x\\y'])
def test_writes_cannot_escape_new_raw_folder(tmp_path, name):
    with pytest.raises(ValueError):
        research.save(tmp_path, name, b'{}')


def test_existing_raw_evidence_is_immutable(tmp_path):
    name = research.save(tmp_path, 'proof.json', b'{}')
    assert name.startswith(research.CACHE + '/')
    with pytest.raises(ValueError, match='immutable'):
        research.save(tmp_path, 'proof.json', b'[]')


def test_persistent_budget_is_checked_before_any_network_call(tmp_path):
    for ordinal in range(1, research.MAX_REQUESTS + 1):
        research.save(tmp_path, f'request-{ordinal:02d}.json', research.shared._dump({'url': 'https://example.test/' + str(ordinal)}))
    with pytest.raises(ValueError, match='budget_exhausted'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)


def test_attempted_URL_is_not_retried_after_process_restart(tmp_path):
    research.save(tmp_path, 'request-01.json', research.shared._dump({'url': research.shared.ROBOT_URL}))
    with pytest.raises(ValueError, match='no_retry'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)


def test_prior_challenge_stops_all_other_native_document_paths(tmp_path):
    research.save_json(tmp_path, 'response', {'url': research.shared.ROBOT_URL, 'access_stop': True,
                                           'status': 'captcha_or_robots_denial'})
    with pytest.raises(ValueError, match='access_stop'):
        research.Fetcher(tmp_path)._get(research.shared.ROBOT_URL)
