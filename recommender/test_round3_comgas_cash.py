"""Owned Comgas source proposal tests; real caches, no network or core edits."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from . import round3_comgas_cash as subject
from . import inventory_candidates as native
from .market_inventory import load_inventory
from .payment_sources import inspect_payment_page


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def inventory():
    return load_inventory(ROOT)


@pytest.fixture(scope='module')
def proofs(inventory):
    with patch('socket.socket.connect', side_effect=AssertionError('offline_only')):
        return {t: subject.load_proof(i, subject.CUTOFF, native._Cache(ROOT, inventory))
                for t, i in subject.IDENTITIES.items()}


@pytest.fixture
def cache(inventory):
    return native._Cache(ROOT, inventory)


def parsed(ticker):
    return inspect_payment_page((ROOT / f'raw/expanded/{ticker}-payments.html').read_bytes(),
        ticker, subject.CUTOFF, datetime(2026, 10, 25, tzinfo=timezone.utc))


def apply(ticker, value, cache, proofs):
    with patch.object(subject, 'load_proof', return_value=deepcopy(proofs[ticker])):
        return subject.apply_exact_repairs(value, subject.IDENTITIES[ticker], subject.CUTOFF, cache)


@pytest.mark.parametrize('ticker', ['CGAS3', 'CGAS5'])
def test_real_proof_complete_native_scope_and_distinct_authority(proofs, ticker):
    proof = proofs[ticker]
    assert len(proof['facts']) == (12 if ticker == 'CGAS3' else 8)
    assert {x['year'] for x in proof['historical_registry']} == set(range(2021, 2027))
    assert {x['only_preferred_code'] for x in proof['historical_registry']} == {'CGAS5'}
    expected = {('600420', 1), ('670033', 1), ('739201', 1), ('794236', 1), ('1061747', 2), ('568344', 1)}
    if ticker == 'CGAS3':
        expected.update((sequence, 1) for sequence in subject.ON_NOTICE_SEQUENCES)
    assert {(x['sequence'], x['version']) for x in proof['selected_filings']} == expected
    for source in proof['sources']:
        assert hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
    assert any(s['authority'] == 'derived_source_catalogue_not_primary' for s in proof['sources'])
    assert any(s['authority'] == 'actual_acquisition_metadata_not_primary_content' for s in proof['sources'])
    assert not proof['contract']['model_admitted']
    assert not proof['contract']['settlement_verified']
    assert proof['contract']['own_training_rows_created'] == 0
    assert not proof['contract']['forecast_inference_performed']


@pytest.mark.parametrize('field,value', [
    ('ticker', 'CGAS4'), ('cvm', 15637), ('cvm', '15636'), ('share_class', 'PNA'),
    ('share_class', 'ON'), ('isin', 'BRCGASACNOR0'), ('isin', None),
])
def test_exact_preferred_identity_not_alias_or_suffix(cache, field, value):
    identity = {**subject.IDENTITIES['CGAS5'], field: value}
    with pytest.raises(ValueError, match='exact_security'):
        subject.load_proof(identity, subject.CUTOFF, cache)


def test_cutoff_not_generalized(cache):
    with pytest.raises(ValueError, match='exact_security'):
        subject.load_proof(subject.IDENTITIES['CGAS5'], date(2026, 10, 1), cache)


@pytest.mark.parametrize('ticker,expected', [('CGAS3', '0.116210115892'), ('CGAS5', '0.127831127481')])
def test_Dec2022_literal_gross_record_and_payment(proofs, ticker, expected):
    facts = [f for f in proofs[ticker]['facts'] if f['record_date'] == '2022-12-26']
    assert len(facts) == 1
    assert facts[0]['gross_per_share_decimal'] == expected
    assert facts[0]['payment_date'] == '2023-01-05'
    assert facts[0]['ex_date'] == '2022-12-27'
    assert facts[0]['label'] == 'JCP'


def test_PN_complete_missing_group_added_without_zero_or_source_mutation(cache, proofs):
    original = parsed('CGAS5')
    before = deepcopy(original)
    output = apply('CGAS5', original, cache, proofs)
    assert original == before
    added = [x for x in output['events'] if x.get('record_date') == '2022-12-26']
    assert len(added) == 1 and added[0]['gross_per_share'] == 0.127831127481
    assert not added[0]['settlement_verified'] and not added[0]['gross_reconstructed_from_net']
    assert output['report']['sha256'] == subject.PAYMENT_SHAS['CGAS5']
    assert output['exact_cash_repairs'][subject.REPAIR_ID]['independent_comparator_changed'] is False


def test_ON_whole_March_group_relocation_preserves_both_reported_dates(cache, proofs):
    original = parsed('CGAS3')
    output = apply('CGAS3', original, cache, proofs)
    assert not any(r.get('record_date') == '2023-03-31' for r in output['events'])
    group = [r for r in output['events'] if r.get('record_date') == '2023-03-24']
    assert {(r['label'], r['gross_per_share_decimal']) for r in group} == {
        ('DIVIDENDO', '2.95450718746'), ('JCP', '0.12707560066')}
    assert {r['isin'] for r in group} == {'BRCGASACNOR0'}
    audit = output['exact_cash_repairs'][subject.REPAIR_ID]
    assert audit['original_calendar_events_before_any_view'] == original['events']
    assert any(x.get('reported_original_record_date') == '2023-03-31'
               for x in group[1]['reported_original_fields'])


def test_clean_ON_notice_groups_repaired_but_conflicting_notices_not_used(cache, proofs):
    output = apply('CGAS3', parsed('CGAS3'), cache, proofs)
    for record, expected in (
        ('2021-12-09', {'0.217500654171', '10.861901298827'}),
        ('2022-09-23', {'0.150903390319'}), ('2024-11-12', {'3.323820585899'})):
        group = [r for r in output['events'] if r.get('record_date') == record]
        assert {r['gross_per_share_decimal'] for r in group} == expected
        assert all(r['share_class'] == 'ON' and r['isin'] == 'BRCGASACNOR0' for r in group)
    for record in ('2022-03-24', '2022-06-24', '2022-11-03'):
        assert not any(r.get('exact_cash_repair_id') for r in output['events'] if r.get('record_date') == record)
    assert {x['code'] for x in proofs['CGAS3']['unresolved_other_source_conflicts']} == {
        'unresolved_primary_schedule_vs_later_DFP_reported_payment',
        'unresolved_March2022_PNA_JCP_workbook_vs_cached_comparator',
        'unresolved_June2022_native_record_ex_date_condition'}


@pytest.mark.parametrize('ticker', ['CGAS3', 'CGAS5'])
def test_idempotent_immutable_view(cache, proofs, ticker):
    once = apply(ticker, parsed(ticker), cache, proofs)
    assert apply(ticker, once, cache, proofs) == once


@pytest.mark.parametrize('attack', ['changed_amount', 'extra_component', 'missing_component', 'wrong_hash'])
def test_whole_original_group_is_mandatory(cache, proofs, attack):
    value = parsed('CGAS5')
    row = next(r for r in value['events'] if r.get('record_date') == '2023-03-24')
    if attack == 'changed_amount':
        row['gross_per_share'] = 99.0
    elif attack == 'extra_component':
        value['events'].append(deepcopy(row))
    elif attack == 'missing_component':
        value['events'].remove(row)
    else:
        value['report']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='cash_repair_'):
        apply('CGAS5', value, cache, proofs)


def test_orphan_not_silently_dropped(cache, proofs):
    value = parsed('CGAS3')
    value['events'] = [r for r in value['events'] if r.get('record_date') != '2023-03-31']
    with pytest.raises(ValueError, match='original_group_changed'):
        apply('CGAS3', value, cache, proofs)


def test_no_primary_source_fallback(cache):
    with patch.object(subject, '_document', side_effect=ValueError('primary_source_failure')):
        with pytest.raises(ValueError, match='primary_source_failure'):
            subject.load_proof(subject.IDENTITIES['CGAS5'], subject.CUTOFF, cache)


@pytest.mark.parametrize('attack', ['second_PN_code', 'second_preferred_class', 'child_version', 'future_master'])
def test_registry_scans_all_equity_classes_not_just_selected(cache, attack):
    body = subject.read(cache.folder, subject.proof.FCA_FILE, subject.FCA_PINS[2026])
    tables = {kind: subject._table(body, f'fca_cia_aberta_{kind}2026.csv')
              for kind in ('', 'valor_mobiliario_')}
    rows = tables['valor_mobiliario_']
    selected = next(r for r in rows if r['Codigo_Negociacao'] == 'CGAS5')
    if attack.startswith('second'):
        new = deepcopy(selected)
        new['Codigo_Negociacao'] = 'CGAS6'
        if attack == 'second_preferred_class':
            new['Sigla_Classe_Acao_Preferencial'] = 'B'
        rows.append(new)
    elif attack == 'child_version':
        selected['Versao'] = '999'
    else:
        next(r for r in tables[''] if r['ID_DOC'] == selected['ID_Documento'])['DT_RECEB'] = '2027-01-01'
    with patch.object(subject, '_table', side_effect=lambda b, member:
            tables['valor_mobiliario_' if 'valor_mobiliario' in member else '']):
        with pytest.raises(ValueError, match='comgas_'):
            subject._registry_year(body, 2026)


def test_native_superseding_revision_in_another_index_blocks(cache):
    manifest = json.loads(subject.read(cache.folder,
        f'raw/primary-research/all-pending/{subject.SCOPE}/round3-comgas-manifest-{subject.MANIFEST_SHA}.json',
        subject.MANIFEST_SHA))
    indexes, _ = subject._indexes(cache, manifest)
    doc = next(x['document'] for x in manifest['inspected_originals'] if x['document']['sequence'] == '1061747')
    newer = deepcopy(doc['index_evidence']['raw_row'])
    newer.update(Versao='3', Data_Entrega='2026-09-01')
    indexes[2025].append(newer)
    with pytest.raises(ValueError, match='superseded_native'):
        subject._latest_index(doc, indexes)


def test_pdf_hash_tamper_blocks_no_extracted_text_fallback(cache):
    original = cache.read

    def changed(relative, role):
        body, stamp = original(relative, role)
        if relative.endswith('.pdf'):
            body += b'\nchanged'
        return body, stamp

    with patch.object(cache, 'read', changed):
        with pytest.raises(ValueError, match='hash|changed'):
            subject.load_proof(subject.IDENTITIES['CGAS5'], subject.CUTOFF, cache)


@pytest.fixture(scope='module')
def preferred_view(inventory):
    identity = subject.IDENTITIES['CGAS5']
    rows = native._cash_pages('CGAS5', {t: i['isin'] for t, i in subject.IDENTITIES.items()},
                              15636, native._Cache(ROOT, inventory))
    return subject.canonical_independent_cash_rows(ROOT, identity, subject.CUTOFF, rows)


def test_typed_source_label_view_exact_binding_no_B3_payload(preferred_view):
    view = preferred_view
    assert len(view['cash_rows']) == len(view['original_cash_rows']) == 172
    assert view['class_crosswalk']['only_preferred_class_in_each_FCA_2021_2026']
    assert not view['class_crosswalk']['suffix_inference']
    for old, new in zip(view['original_cash_rows'], view['cash_rows']):
        assert new['typeStock'] == ('PN' if old['typeStock'] == 'PNA' else old['typeStock'])
        assert all(new[k] == old[k] for k in old if k not in ('typeStock', 'closingPricePriorExDate'))
    old_PN = [r for r in view['original_cash_rows'] if r['typeStock'] == 'PN']
    assert len(old_PN) == 2
    assert {r['lastDatePriorEx'] for r in old_PN} == {'26/04/2001', '27/04/1998'}
    assert all(r in view['cash_rows'] for r in old_PN)
    assert not view['synthetic_exchange_rows_created']


@pytest.mark.parametrize('attack', ['valueCash', 'lastDatePriorEx', 'typeStock', 'isinCode', 'partial'])
def test_cash_view_rejects_any_unbound_source_change(preferred_view, attack):
    rows = deepcopy(preferred_view['original_cash_rows'])
    if attack == 'partial':
        rows.pop()
    else:
        row = next(r for r in rows if r['typeStock'] == 'PNA')
        row[attack] = {'valueCash': '99,1', 'lastDatePriorEx': '10/06/2026',
                      'typeStock': 'PNB', 'isinCode': 'BRCGASACNOR0'}[attack]
    with pytest.raises(ValueError, match='exact_complete_cached_cash_rows'):
        subject.canonical_independent_cash_rows(ROOT, subject.IDENTITIES['CGAS5'], subject.CUTOFF, rows)


def test_other_issuer_calendar_passthrough(cache):
    value = {'sentinel': 'untouched'}
    assert subject.apply_exact_repairs(value, {'ticker': 'ETER3'}, subject.CUTOFF, cache) is value


def test_real_one_positive_PN_and_ON_still_fail_closed(inventory, proofs):
    originals = {p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / 'raw/expanded').glob('CGAS*-b3-cash-*.json')}
    inspector, reviewer = native.inspect_payment_page, native._reviewed_cash_view
    for ticker in ('CGAS3', 'CGAS5'):
        stock = next(s for s in inventory['stocks'] if s['ticker'] == ticker)
        cache = native._Cache(ROOT, inventory)

        def inspect(body, code, cutoff, observed):
            return apply(code, inspector(body, code, cutoff, observed), cache, proofs)

        def review(identity, active, cutoff, rows, checks):
            if identity['ticker'] == 'CGAS5':
                # This is the actual production source view plus the stronger
                # all-class proof; no numerical, quantity or period gate patch.
                subject.canonical_independent_cash_rows(ROOT, identity, cutoff, rows)
            return reviewer(identity, active, cutoff, rows, checks)

        with patch.object(native, 'inspect_payment_page', inspect), patch.object(native, '_reviewed_cash_view', review), \
                patch('socket.socket.connect', side_effect=AssertionError('offline_only')):
            if ticker == 'CGAS3':
                with pytest.raises(ValueError, match='cash_amount_or_share_scale_disagrees_with_B3'):
                    native._one(stock, cache, subject.CUTOFF, {})
            else:
                candidate = native._one(stock, cache, subject.CUTOFF, {})
                assert len(candidate['prices']) >= 61
                assert candidate['share_class'] == 'PN'
                assert len(candidate['payments']) > 0
                assert candidate['financial_basis_start'] == '2014-01-01'
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in originals.items())


def test_output_path_cannot_write_production_or_other_work(tmp_path):
    with pytest.raises(ValueError, match='owned_work_folder'):
        subject.write_report(ROOT, tmp_path)


@pytest.fixture(scope='module')
def form_inputs(inventory):
    cache = native._Cache(ROOT, inventory)
    manifest = json.loads(subject.read(cache.folder,
        f'raw/primary-research/all-pending/{subject.SCOPE}/round3-comgas-manifest-{subject.MANIFEST_SHA}.json',
        subject.MANIFEST_SHA))
    indexes, _ = subject._indexes(cache, manifest)
    body, pages, doc, _ = subject._document(cache, manifest, indexes, '600420')
    workbook, _ = subject._workbook(cache, manifest)
    return body, pages, doc, workbook


@pytest.mark.parametrize('attack', ['extra_row', 'missing_row', 'wrong_ISIN', 'borrow_other_class',
    'wrong_cvm', 'wrong_record', 'wrong_payment', 'duplicated_row', 'partial_status', 'unmapped_annex'])
def test_native_whole_scope_adversarial(form_inputs, attack):
    body, pages, doc, workbook = form_inputs
    value = subject.cvm.parse_pdf(body, doc, subject.CUTOFF)
    rows = value['events']
    if attack == 'extra_row':
        rows.append(deepcopy(rows[0]))
    elif attack == 'missing_row':
        rows.pop()
    elif attack == 'wrong_ISIN':
        rows[0]['isin'] = 'BRXXXXACNPA0'
    elif attack == 'borrow_other_class':
        rows[0]['isin'], rows[1]['isin'] = rows[1]['isin'], rows[0]['isin']
    elif attack == 'wrong_cvm':
        rows[0]['cvm'] = 15637
    elif attack == 'wrong_record':
        rows[0]['record_date'] = '2023-03-31'
    elif attack == 'wrong_payment':
        rows[0]['payment_date'] = '2023-04-05'
    elif attack == 'duplicated_row':
        rows[-1] = deepcopy(rows[0])
    elif attack == 'partial_status':
        value['status'] = 'partial'
    else:
        value['unmapped_installment_rows'] = [{'unbound': True}]
    with patch.object(subject.cvm, 'parse_pdf', return_value=value):
        with pytest.raises(ValueError, match='comgas_'):
            subject._form_facts(body, pages, doc, subject.IDENTITIES['CGAS5'], workbook)


@pytest.mark.parametrize('attack', ['unknown_type', 'formula', 'duplicate_match', 'net_instead_of_gross'])
def test_workbook_is_type_crosscheck_not_gross_reconstruction(form_inputs, attack):
    body, pages, doc, original = form_inputs
    workbook = deepcopy(original)
    if attack == 'unknown_type':
        workbook[18]['C']['literal'] = 'not_JCP'
    elif attack == 'formula':
        workbook[18]['J']['formula'] = True
    elif attack == 'duplicate_match':
        workbook[999] = deepcopy(workbook[18])
    else:
        workbook[18]['J']['literal'] = '0.118815686612'
    with pytest.raises(ValueError, match='comgas_'):
        subject._form_facts(body, pages, doc, subject.IDENTITIES['CGAS5'], workbook)


def test_real_annual_and_training_gates_and_owned_report():
    work = ROOT.resolve().parents[3] / 'work/recovery_round2_20261025'
    path, digest, report = subject.write_report(ROOT, work)
    assert path.parent == work
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert report['manifest_sha256'] == subject.MANIFEST_SHA
    results = {r['identity']['ticker']: r for r in report['results']}
    assert results['CGAS5']['one_pass']
    assert results['CGAS5']['checks']['independent_cash_matches'] == 16
    assert results['CGAS5']['checks']['independent_price_matches'] == 16
    assert not results['CGAS3']['one_pass']
    assert {g['record_date'] for g in results['CGAS3']['remaining_common_window_cash_disagreements']} == {
        '2022-03-24', '2022-06-24', '2022-11-03'}
    for row in results.values():
        assert not row['financial_current_pass']
        assert not row['forecast_inference']
        assert row['training_rows'] == 0
        annual = row['latest_annual_audit']
        assert (annual['fiscal_end'], annual['version'], annual['filing_id'], annual['published_at'],
                annual['accounts_scope']) == ('2025-12-31', '1', '155069', '2026-03-04', 'individual')
        assert set(annual['reasons']) == {'basic_eps_absent_ambiguous_or_zero', 'capex_not_identified'}
    assert report['network_requests'] == report['B3_network_requests'] == 0
    assert report['api_mziq_global_401_stop_respected']
    assert not report['original_source_files_modified']
    assert not report['synthetic_B3_payload_written']
