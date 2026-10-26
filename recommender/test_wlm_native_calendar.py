"""Real-byte WLM proof and adversarial tests; no collection or model admission."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import socket

import pdfplumber
import pytest

from . import wlm_native_calendar as wlm


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def calendar():
    return wlm.inspect_evidence(ROOT, wlm.STOCK)


@pytest.fixture(scope='module')
def inputs(calendar):
    reader = wlm.EvidenceReader(ROOT)
    documents, _ = wlm.indexed_documents(reader, wlm.CUTOFF)
    with pdfplumber.open(ROOT / wlm.FORM) as pdf:
        tables = pdf.pages[0].extract_tables()
    payloads, _ = wlm.cached_sources(reader)
    parsed = wlm.inspector.inspect_payment_page(payloads['expanded/WLMM3-payments.html'], 'WLMM3',
        wlm.CUTOFF, datetime(2026, 10, 24, tzinfo=timezone.utc))
    return {'documents': documents, 'document': calendar['document'], 'tables': tables,
            'detail': payloads['expanded/b3-detail-11070.json'],
            'page': payloads['expanded/WLMM4-b3-cash-1.json'], 'parsed': parsed}


@pytest.fixture
def structural_calendar(calendar):
    # A structural transform fixture, not a substitute for real source review.
    # The public entrypoint always rebuilds proof from pinned originals.
    value = deepcopy(calendar)
    value['repair_allowed'] = True
    value['approval_revision_review'].update(
        status='verified_exact_current_approval_protocol', repair_allowed=True)
    return value


def test_real_native_is_four_explicit_gross_components_not_inferred_type(calendar):
    form = calendar['native_form']
    assert [(r['share_class'], r['gross_per_share_decimal'], r['payment_date']) for r in form['components']] == [
        ('ON', '0.03202000000', '2024-06-28'), ('PN', '0.03522000000', '2024-06-28'),
        ('ON', '0.63121000000', '2024-06-28'), ('PN', '0.69433000000', '2024-06-28')]
    assert form['gross_totals_by_class'] == {'ON': '0.66323000000', 'PN': '0.72955000000'}
    assert form['cash_kind'] is None
    assert form['native_protocol'] == '1228245'
    assert form['native_IPE'] == '011070IPE290420240104541110-27'
    assert form['approval_date'] == form['record_date'] == form['published_at'] == '2024-04-29'
    proof = calendar['cash_type_proof']
    assert proof['label'] == 'DIVIDENDO' and len(proof['located_rows']) == 4
    assert not proof['native_cash_kind_reported'] and not proof['kind_inferred_from_amount_or_status']
    assert not proof['gross_reconstructed_from_net']
    assert not calendar['settlement_verified'] and not calendar['point_in_time_archive_verified']
    assert not calendar['cash_model_eligible'] and not calendar['parent_admission_performed']


def test_all_actual_reader_receipts_close_and_six_indices_are_hashed(calendar):
    assert len(calendar['IPE_coverage']) == 6
    assert [r['issuer_rows_before_cutoff'] for r in calendar['IPE_coverage']] == [43, 52, 51, 57, 54, 44]
    sources = calendar['evidence_sources']
    assert len(sources) == 16
    for source in sources:
        body = (ROOT / source['file']).read_bytes()
        assert hashlib.sha256(body).hexdigest() == source['sha256']
        assert len(body) == source['bytes']
        assert source['access'] == 'existing_cache_read_only' and source['roles']
        assert 'retrieved_at' in source
    assert {r['sha256'] for r in calendar['IPE_coverage']} == set(wlm.IPE_HASHES.values())
    assert next(s for s in sources if s['file'] == wlm.FORM)['published_at'] == '2024-04-29'
    assert next(s for s in sources if s['file'] == wlm.FORM)['retrieved_at'] == '2026-10-25T11:18:44.874643+00:00'
    for source in calendar['cash_type_proof']['source_receipts']:
        assert source in sources


def test_real_latest_AGO_v2_review_allows_only_native_schedule_repair_without_changing_amounts(calendar, inputs):
    review = calendar['approval_revision_review']
    assert calendar['repair_allowed'] and review['repair_allowed']
    assert review['status'] == wlm.REVIEWED_V2_STATUS
    assert review['latest_approval_document']['sequence'] == '768600'
    assert review['latest_approval_document']['version'] == 2
    assert review['latest_approval_document']['published_at'] == '2024-05-29'
    assert review['unreviewed_latest_document_URL'] is None
    proof = review['reviewed_latest_approval']
    assert proof['whole_body_reviewed'] and proof['page_count'] == 8
    assert proof['mandatory_outstanding_total_BRL'] == '1229496.77'
    assert proof['additional_total_BRL'] == '24237822.41'
    assert not proof['payment_date_restated'] and not proof['individual_per_share_amounts_restated']
    assert not proof['unavailable_v1_body_equality_verified']
    assert not proof['native_fields_inferred_from_aggregate_totals']
    assert not proof['explicit_conflicting_cash_amendment_detected']
    assert not review['claim_later_revision_does_not_change_dividends']
    before = deepcopy(inputs['parsed'])
    repaired = wlm.repair_inspected_calendar(ROOT, wlm.STOCK, wlm.CUTOFF, inputs['parsed'])
    assert next(e for e in repaired['events'] if e['source_row'] == 'table:2:row:16')['payment_date'] == '2024-06-28'
    assert [e['gross_per_share'] for e in repaired['events']] == [e['gross_per_share'] for e in before['events']]
    assert inputs['parsed'] == before


def test_structural_atomic_transform_changes_only_one_date_retains_good_component_and_all_other_rows(inputs, structural_calendar):
    before = deepcopy(inputs['parsed'])
    out = wlm._repair_parsed(before, structural_calendar)
    assert before == inputs['parsed']
    assert len(out['events']) == len(before['events']) == 47
    old = {e['source_row']: e for e in before['events']}
    new = {e['source_row']: e for e in out['events']}
    changed = [key for key in old if old[key]['payment_date'] != new[key]['payment_date']]
    assert changed == ['table:2:row:16']
    assert new[changed[0]]['payment_date'] == '2024-06-28'
    assert old[changed[0]]['payment_date'] == '2024-04-28'
    for row in old:
        assert old[row]['gross_per_share'] == new[row]['gross_per_share']
        assert old[row]['record_date'] == new[row]['record_date']
        assert old[row]['label'] == new[row]['label']
        if row not in {'table:2:row:15', 'table:2:row:16'}:
            assert old[row] == new[row]
    assert new['table:2:row:15']['payment_date'] == '2024-06-28'
    assert all(new[r]['date_order_valid'] for r in ('table:2:row:15', 'table:2:row:16'))
    audit = out['native_calendar_repair']
    assert audit['matched_components'] == 2 and audit['changed_date_count'] == 1
    assert audit['original_group'] == [e for e in before['events'] if e['record_date'] == wlm.APPROVAL]
    assert audit['original_issues'] == before['report']['issues']
    assert audit['removed_issues'] == [{'code': 'payment_before_record_date', 'blocking': True, 'row': 'table:2:row:16'}]
    assert out['report']['issues'] == [i for i in before['report']['issues'] if i not in audit['removed_issues']] + audit['recomputed_added_issues']
    assert audit['recomputed_added_issues'] == [{'code': 'conflicting_event_amount', 'blocking': True, 'row': 'table:2:row:16'}]
    # Other genuine source ambiguities are not cleared to obtain an admission.
    assert out['status'] == 'partial'
    assert len([i for i in out['report']['issues'] if i['blocking']]) == 4
    assert not audit['gross_amounts_changed'] and not audit['settlement_verified']
    assert not audit['parent_admission_performed']


@pytest.mark.parametrize('fault', ['drop_ON', 'drop_PN', 'duplicate', 'unknown_ISIN', 'wrong_class',
    'net_header', 'foreign_currency', 'wrong_unit', 'extra_annex', 'missing_table', 'malformed_row',
    'zero', 'negative', 'NaN', 'float', 'wrong_period', 'wrong_year', 'missing_payment',
    'payment_before_record', 'update', 'installments', 'wrong_name', 'wrong_CNPJ', 'wrong_CVM',
    'wrong_IPE', 'wrong_protocol', 'wrong_version', 'wrong_record', 'wrong_publication'])
def test_native_table_adversarial_closed(inputs, fault):
    tables, doc = deepcopy(inputs['tables']), deepcopy(inputs['document'])
    if fault == 'drop_ON': tables[1].pop(2)
    elif fault == 'drop_PN': tables[1].pop(3)
    elif fault == 'duplicate': tables[1][3] = deepcopy(tables[1][2])
    elif fault == 'unknown_ISIN': tables[1][2][0] = 'BROTHERCNOR0'
    elif fault == 'wrong_class': tables[1][2][0] = 'BRWLMMACNPR3'
    elif fault == 'net_header': tables[1][1][1] = 'Valor Liquido (R$/Unidade)'
    elif fault == 'foreign_currency': tables[1][1][1] = 'Valor Bruto (USD/Unidade)'
    elif fault == 'wrong_unit': tables[1][1][1] = 'Valor Bruto (R$/Lote)'
    elif fault == 'extra_annex': tables[2].append(['28/06/2024', '0,01'])
    elif fault == 'missing_table': tables.pop()
    elif fault == 'malformed_row': tables[1][2].append('ignored')
    elif fault == 'zero': tables[1][2][1] = '0,00000'
    elif fault == 'negative': tables[1][2][1] = '-0,03202'
    elif fault == 'NaN': tables[1][2][1] = 'NaN'
    elif fault == 'float': tables[1][2][1] = .03202
    elif fault == 'wrong_period': tables[1][2][2] = 'Trimestral'
    elif fault == 'wrong_year': tables[1][2][3] = '2022'
    elif fault == 'missing_payment': tables[1][2][6] = ''
    elif fault == 'payment_before_record': tables[1][2][6] = '28/04/2024'
    elif fault == 'update': tables[1][2][4] = 'Sim'
    elif fault == 'installments': tables[1][2][5] = 'Parcelado'
    elif fault == 'wrong_name': doc['issuer_name'] = 'OUTRA S.A.'
    elif fault == 'wrong_CNPJ': doc['index_evidence']['raw_row']['CNPJ_Companhia'] = '33.229.024/0001-51'
    elif fault == 'wrong_CVM': doc['cvm'] = 9999
    elif fault == 'wrong_IPE': tables[0][1][1] = tables[0][1][1].replace('011070IPE', '009999IPE')
    elif fault == 'wrong_protocol': tables[0][7][0] = '1243874'
    elif fault == 'wrong_version': tables[0][7][1] = '2'
    elif fault == 'wrong_record': tables[0][5][1] = '30/04/2024'
    elif fault == 'wrong_publication': doc['published_at'] = '2026-10-01'
    with pytest.raises((wlm.ProofError, ValueError)):
        wlm.parse_native_tables(tables, doc, wlm.CUTOFF)


@pytest.mark.parametrize('fault', ['drop', 'duplicate', 'wrong_class', 'wrong_type', 'net_amount',
    'wrong_unit', 'ratio', 'wrong_approval', 'wrong_record', 'wrong_CVM', 'wrong_CNPJ', 'wrong_ISIN', 'pagination'])
def test_cash_kind_requires_whole_both_class_event_not_majority_or_amount_guess(inputs, calendar, fault):
    page, detail = deepcopy(inputs['page']), deepcopy(inputs['detail'])
    rows = [r for r in page['results'] if r['dateApproval'] == '29/04/2024' and r['lastDatePriorEx'] == '29/04/2024']
    if fault == 'drop': page['results'].remove(rows[0]); page['page']['totalRecords'] -= 1
    elif fault == 'duplicate': page['results'].append(deepcopy(rows[0])); page['page']['totalRecords'] += 1
    elif fault == 'wrong_class': rows[0]['typeStock'] = 'PN'
    elif fault == 'wrong_type': rows[0]['corporateAction'] = 'JRS CAP PROPRIO'
    elif fault == 'net_amount': rows[0]['valueCash'] = '0,027217'
    elif fault == 'wrong_unit': rows[0]['quotedPerShares'] = '1000'
    elif fault == 'ratio': rows[0]['ratio'] = '0,85'
    elif fault == 'wrong_approval': rows[0]['dateApproval'] = '28/04/2024'
    elif fault == 'wrong_record': rows[0]['lastDatePriorEx'] = '30/04/2024'
    elif fault == 'wrong_CVM': detail['codeCVM'] = '9999'
    elif fault == 'wrong_CNPJ': detail['cnpj'] = '33229024000151'
    elif fault == 'wrong_ISIN': detail['otherCodes'][0]['isin'] = 'BRWLMMACNPR3'
    elif fault == 'pagination': page['page']['totalPages'] = 2
    with pytest.raises(wlm.ProofError):
        wlm.prove_cash_kind(calendar['native_form'], detail, page)


@pytest.mark.parametrize('fault', ['drop_good_component', 'drop_bad_component', 'extra_same_record', 'gross',
    'net', 'wrong_label', 'wrong_ticker', 'wrong_ISIN', 'wrong_unit', 'wrong_approval', 'unrelated_good_date',
    'remove_date_issue', 'other_blocker', 'remove_malformed_flag', 'no_malformed_date', 'report_hash', 'cutoff', 'format'])
def test_repair_transform_rejects_partial_crossclass_or_unrelated_source_changes(inputs, structural_calendar, fault):
    parsed = deepcopy(inputs['parsed'])
    rows = [e for e in parsed['events'] if e['record_date'] == wlm.APPROVAL]
    good, bad = rows
    if fault == 'drop_good_component': parsed['events'].remove(good)
    elif fault == 'drop_bad_component': parsed['events'].remove(bad)
    elif fault == 'extra_same_record': parsed['events'].append(deepcopy(good))
    elif fault == 'gross': bad['gross_per_share'] = .63122
    elif fault == 'net': bad['reported_amount'] = .5365285
    elif fault == 'wrong_label': bad['label'] = 'JCP'
    elif fault == 'wrong_ticker': bad['ticker'] = 'WLMM4'
    elif fault == 'wrong_ISIN': bad['isin'] = 'BRWLMMACNPR3'
    elif fault == 'wrong_unit': bad['quoted_quantity'] = 1000
    elif fault == 'wrong_approval': bad['approval_date'] = '2024-04-28'
    elif fault == 'unrelated_good_date': good['payment_date'] = '2024-07-01'
    elif fault == 'remove_date_issue': parsed['report']['issues'] = [i for i in parsed['report']['issues'] if i['code'] != 'payment_before_record_date']
    elif fault == 'other_blocker': parsed['report']['issues'].append({'code': 'invalid_tax_basis', 'blocking': True, 'row': bad['source_row']})
    elif fault == 'remove_malformed_flag': bad['date_order_valid'] = True
    elif fault == 'no_malformed_date': bad['payment_date'] = '2024-06-28'
    elif fault == 'report_hash': parsed['report']['sha256'] = '0' * 64
    elif fault == 'cutoff': parsed['report']['as_of'] = '2026-10-01'
    elif fault == 'format': parsed['source_format'] = 'csv'
    before = deepcopy(parsed)
    with pytest.raises(wlm.ProofError):
        wlm._repair_parsed(parsed, structural_calendar)
    assert parsed == before


@pytest.mark.parametrize('fault', ['duplicate_form', 'later_form', 'changed_URL', 'latest_older_version'])
def test_latest_native_form_no_fallback(inputs, fault):
    documents = deepcopy(inputs['documents'])
    doc = next(d for d in documents if d['sequence'] == '753217')
    if fault == 'duplicate_form': documents.append(deepcopy(doc))
    elif fault == 'later_form':
        later = deepcopy(doc)
        later.update(sequence='999999', version=2, published_at='2024-05-30')
        later['url'] = later['url'].replace('753217', '999999').replace('numVersao=1', 'numVersao=2')
        documents.append(later)
    elif fault == 'changed_URL': doc['url'] = doc['url'].replace('numVersao=1', 'numVersao=2')
    elif fault == 'latest_older_version':
        newer = deepcopy(doc)
        newer['published_at'] = '2024-05-30'
        documents.append(newer)
    with pytest.raises(wlm.ProofError):
        wlm.select_form(documents, wlm.CUTOFF)


def test_revision_guard_finds_family_even_if_reference_date_changes(inputs, calendar):
    documents = deepcopy(inputs['documents'])
    later = next(d for d in documents if d['sequence'] == '768600')
    later['reference_date'] = '2024-05-29'
    later['index_evidence']['raw_row']['Data_Referencia'] = '2024-05-29'
    assert not wlm.approval_revision_review(documents, calendar['native_form'], wlm.CUTOFF)['repair_allowed']


def test_exact_current_approval_protocol_structural_positive(inputs, calendar):
    documents = deepcopy(inputs['documents'])
    doc = next(d for d in documents if d['sequence'] == '768600')
    doc.update(sequence='752971', version=1, published_at=wlm.APPROVAL)
    doc['url'] = doc['url'].replace('1243874', '1228245').replace('768600', '752971').replace('numVersao=2', 'numVersao=1')
    doc['index_evidence']['raw_row'].update(Protocolo_Entrega=calendar['native_form']['native_IPE'],
        Link_Download=doc['url'], Versao='1', Data_Entrega=wlm.APPROVAL)
    result = wlm.approval_revision_review(documents, calendar['native_form'], wlm.CUTOFF)
    assert result['repair_allowed'] and result['status'] == 'verified_exact_current_approval_protocol'
    assert result['unreviewed_latest_document_URL'] is None


@pytest.mark.parametrize('name', [wlm.IPE_MANIFEST, wlm.FORM, wlm.FORM.removesuffix('.pdf') + '-source.json',
    wlm.EXCHANGE_MANIFEST, wlm.PAYMENTS, 'raw/expanded/WLMM4-b3-cash-1.json',
    'raw/expanded/b3-detail-11070.json',
    wlm.APPROVAL_V2_FILE, wlm.APPROVAL_V2_RECEIPT, wlm.CACHE + '/request-approval-768600-v2.json',
    'raw/primary-research/cvm-documents/index-2024-' + wlm.IPE_HASHES[2024] + '.zip'])
def test_actual_byte_corruption_rejected_without_modifying_originals(monkeypatch, tmp_path, name):
    original = (ROOT / name).read_bytes()
    altered = tmp_path / 'corrupt.bin'
    altered.write_bytes(original + b' ')
    real_safe = wlm.shared._safe
    monkeypatch.setattr(wlm.shared, '_safe', lambda root, value: altered if value == name else real_safe(root, value))
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        wlm.inspect_evidence(ROOT, wlm.STOCK)
    assert (ROOT / name).read_bytes() == original


@pytest.mark.parametrize('key,value', [('ticker', 'WLMM4'), ('cvm', 9999), ('cvm', '11070'),
    ('share_class', 'PN'), ('isin', 'BRWLMMACNPR3')])
def test_exact_security_required(key, value):
    with pytest.raises(wlm.ProofError, match='exact_WLMM3_identity'):
        wlm.inspect_evidence(ROOT, {**wlm.STOCK, key: value})


def test_no_network_and_no_writes_in_public_repair(monkeypatch, inputs):
    def forbidden(*args, **kwargs):
        raise AssertionError('no_network_or_write_allowed')
    monkeypatch.setattr(socket, 'socket', forbidden)
    real_open = io.open
    def read_only(file, mode='r', *args, **kwargs):
        if any(token in mode for token in ('w', 'a', 'x', '+')):
            forbidden()
        return real_open(file, mode, *args, **kwargs)
    monkeypatch.setattr(io, 'open', read_only)
    repaired = wlm.repair_inspected_calendar(ROOT, wlm.STOCK, wlm.CUTOFF, inputs['parsed'])
    assert repaired['native_calendar_repair']['changed_date_count'] == 1


def test_report_writes_only_owned_scope_and_is_immutable_idempotent(tmp_path, calendar, monkeypatch):
    monkeypatch.setattr(wlm, 'inspect_evidence', lambda *a: deepcopy(calendar))
    report, name = wlm.build_report(tmp_path)
    before = (tmp_path / name).read_bytes()
    assert report['status'] == 'verified_pending_normal_parent_gates'
    assert report['network_requests'] == 0
    assert not report['parent_files_changed'] and not report['pointers_written']
    assert not report['thresholds_changed'] and not report['parent_admission_performed']
    _, again = wlm.build_report(tmp_path)
    assert again == name and (tmp_path / name).read_bytes() == before
    assert [p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()] == [name]


def test_frozen_v2_parent_hook_reaches_normal_cash_gate_without_relaxing_it(monkeypatch):
    from . import inventory_candidates as parent
    from . import wlm_cash2023_calendar as followon
    from .market_inventory import load_inventory

    # Retain the frozen v2 contract; the new missing-group augmentation has
    # separate real-source integration tests, not a changed v2 expectation.
    monkeypatch.setattr(followon, 'repair_full_inspected_calendar', wlm.repair_full_inspected_calendar)
    monkeypatch.setattr(followon, 'canonical_independent_cash_rows', wlm.canonical_independent_cash_rows)
    inventory = load_inventory(ROOT)
    row, = [r for r in inventory['stocks'] if r['ticker'] == 'WLMM3']
    scope = {**inventory, 'stocks': [row]}
    reconcile_cash, reconcile_prices = parent.reconcile_payments, parent.reconcile_prices
    candidates, pending, _ = parent.build_candidates(ROOT, scope, wlm.CUTOFF)
    assert candidates == [] and pending[0]['reason'] == 'B3_cash_event_missing_from_payment_table'
    assert pending[0]['checks']['wlm_native_calendar_repair']['source_2022_group_corrected_from_primary']
    assert parent.reconcile_payments is reconcile_cash and parent.reconcile_prices is reconcile_prices


@pytest.mark.parametrize('field', ['gross_per_share', 'payment_date', 'record_date', 'label', 'source_row', 'issues'])
def test_public_wrapper_reparses_exact_original_inspector_even_for_unmatched_rows(monkeypatch, inputs, structural_calendar, field):
    # Structural-only fixture; the real entrypoint independently hashes and
    # reviews all eight pages of the latest approval revision before any repair.
    monkeypatch.setattr(wlm, 'verified_calendar', lambda *args: deepcopy(structural_calendar))
    parsed = deepcopy(inputs['parsed'])
    if field == 'issues':
        parsed['report']['issues'].pop()
    else:
        parsed['events'][0][field] = 'unrelated-row-edited'
    before = deepcopy(parsed)
    with pytest.raises(wlm.ProofError, match='not_exact_original_inspector_output'):
        wlm.repair_inspected_calendar(ROOT, wlm.STOCK, wlm.CUTOFF, parsed)
    assert parsed == before


@pytest.mark.parametrize('fault', ['partial_body', 'blank_page', 'wrong_CNPJ', 'wrong_approval',
    'wrong_signature', 'mandatory_total', 'additional_total', 'missing_resolution', 'new_schedule',
    'new_payment_date', 'new_unit_amount', 'wrong_version', 'wrong_URL'])
def test_whole_v2_review_rejects_missing_pages_amended_fields_or_unknown_content(calendar, fault):
    with pdfplumber.open(ROOT / wlm.APPROVAL_V2_FILE) as pdf:
        pages = [p.extract_text() or '' for p in pdf.pages]
    document = deepcopy(calendar['approval_revision_review']['latest_approval_document'])
    if fault == 'partial_body': pages.pop()
    elif fault == 'blank_page': pages[6] = ''
    elif fault == 'wrong_CNPJ': pages[0] = pages[0].replace('33.228.024/0001-51', '33.229.024/0001-51')
    elif fault == 'wrong_approval': pages[0] = pages[0].replace('29 DE ABRIL DE 2024', '30 DE ABRIL DE 2024')
    elif fault == 'wrong_signature': pages[7] = pages[7].replace('29 de abril de 2024', '30 de abril de 2024')
    elif fault == 'mandatory_total': pages[2] = pages[2].replace('1.229.496,77', '1.229.496,78')
    elif fault == 'additional_total': pages[2] = pages[2].replace('24.237.822,41', '24.237.822,42')
    elif fault == 'missing_resolution': pages[2] = pages[2].replace('Item 5 da Ordem', 'Item X da Ordem')
    elif fault == 'new_schedule': pages[5] += ' Novo calendario de pagamento.'
    elif fault == 'new_payment_date': pages[5] += ' Pagamento em 28 de junho de 2024.'
    elif fault == 'new_unit_amount': pages[5] += ' Valor bruto 0,63121000000.'
    elif fault == 'wrong_version': document['version'] = 3
    elif fault == 'wrong_URL': document['url'] += '&unknown=1'
    with pytest.raises(wlm.ProofError):
        wlm.review_approval_pages(pages, document, calendar['native_form'])


def test_newer_approval_v3_never_uses_reviewed_v2_fallback(inputs, monkeypatch):
    docs = deepcopy(inputs['documents'])
    doc = next(d for d in docs if d['sequence'] == '768600')
    doc.update(sequence='999999', version=3, published_at='2024-06-01')
    doc['url'] = doc['url'].replace('768600', '999999').replace('numVersao=2', 'numVersao=3')
    doc['index_evidence']['raw_row'].update(Link_Download=doc['url'], Versao='3', Data_Entrega='2024-06-01',
        Protocolo_Entrega='011070IPE290420240304541110-27')
    monkeypatch.setattr(wlm, 'indexed_documents', lambda *args: (docs, []))
    with pytest.raises(wlm.ProofError, match='transport_or_exact_index_receipt_mismatch'):
        wlm.verified_calendar(ROOT, wlm.STOCK)


def test_approval_acquisition_already_attempted_never_retries(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('no_second_request')
    monkeypatch.setattr(wlm.urllib.request, 'build_opener', forbidden)
    with pytest.raises(wlm.ProofError, match='one_request_already_attempted_no_retry'):
        wlm.acquire_approval_v2(ROOT)


@pytest.mark.parametrize('http_status', [200, 302, 401, 403, 429])
def test_single_attempt_acquisition_immutable_audit_and_no_retry(tmp_path, inputs, monkeypatch, http_status):
    import urllib.error
    from email.message import Message

    calls = []
    doc = next(d for d in inputs['documents'] if d['sequence'] == '768600')
    monkeypatch.setattr(wlm, 'indexed_documents', lambda *args: ([doc], []))
    class Response:
        status = 200
        url = wlm.APPROVAL_V2_URL
        headers = {'Content-Type': 'text/html'}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit): return b'%PDF-test-fixture-only'
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if http_status != 200:
                raise urllib.error.HTTPError(request.full_url, http_status, 'fixture', Message(), io.BytesIO(b'fixture'))
            return Response()
    monkeypatch.setattr(wlm.urllib.request, 'build_opener', lambda *args: Opener())
    result, receipt = wlm.acquire_approval_v2(tmp_path)
    assert result['http_status'] == http_status and result['network_requests'] == 1
    assert result['access_stop'] is (http_status in (401, 403, 429))
    assert calls == [wlm.APPROVAL_V2_URL]
    assert json.loads((tmp_path / receipt).read_bytes()) == result
    assert all(p.relative_to(tmp_path).as_posix().startswith(wlm.CACHE + '/') for p in tmp_path.rglob('*') if p.is_file())
    with pytest.raises(wlm.ProofError, match='one_request_already_attempted_no_retry'):
        wlm.acquire_approval_v2(tmp_path)
    assert len(calls) == 1


@pytest.fixture(scope='module')
def full_calendars():
    return {ticker: wlm.verified_followon_calendar(ROOT, {
        'ticker': ticker, 'isin': isin, 'cvm': 11070, 'share_class': 'ON' if share == 'ON' else 'PNA'})
        for share, (ticker, isin) in wlm.CLASSES.items()}


@pytest.fixture(scope='module')
def full_inputs():
    return {ticker: wlm.inspector.inspect_payment_page(
        (ROOT / f'raw/expanded/{ticker}-payments.html').read_bytes(), ticker, wlm.CUTOFF,
        datetime(2026, 10, 24, tzinfo=timezone.utc)) for ticker in ('WLMM3', 'WLMM4')}


def test_native_FRE_latest_v3_exact_nested_attachment_and_class_specific_gross(full_calendars):
    for proof in full_calendars.values():
        native = proof['verified_native_FRE2022_schedule']
        assert native['latest']['raw']['ID_DOC'] == '125831'
        assert native['latest']['raw']['VERSAO'] == '3'
        assert native['published_at'] == '2023-05-05'
        assert native['protocol'] == '011070FRE202220220300125831-61'
        assert native['schedule']['gross_per_share_by_source_class'] == {'ON':'0.25287','PN':'0.27815'}
        assert native['schedule']['payment_date'] == '2022-06-29'
        assert len(native['schedule']['all_four_other_JCP_rows_retained_for_table_audit_not_used_as_record_dates']) == 4
        assert not native['schedule']['per_share_amounts_derived_from_aggregate_or_ratio']
        assert not native['older_FRE_v2_fallback_used'] and not native['settlement_verified']
        assert native['schedule_source']['derived_from']['entry_index'] == 103
        assert native['schedule_source']['derived_from']['sha256'] == wlm.NATIVE_FRE_SHA
        assert native['schedule_source']['retrieved_at'] is None
        assert native['class_A_annual_records'][1]['Classe_Acao'] == 'Preferencial Classe A'
        failed = proof['additional_issuer_FRE_acquisition']
        assert failed['http_status'] == 404 and failed['published_at'] is None
        assert not failed['calendar_proof'] and not failed['publication_inferred_from_URL']
        assert not failed['exact_CVM_blob_identity_verified']


@pytest.mark.parametrize('ticker,amount,sources', [('WLMM3',.20833868,39),('WLMM4',.22917255,40)])
def test_full_primary_groups_repaired_atomically_without_touching_unmatched_rows(
        full_calendars, full_inputs, monkeypatch, ticker, amount, sources):
    proof = full_calendars[ticker]
    monkeypatch.setattr(wlm, 'verified_followon_calendar', lambda *a: deepcopy(proof))
    before = deepcopy(full_inputs[ticker])
    out = wlm.repair_full_inspected_calendar(ROOT, proof, wlm.CUTOFF, before)
    assert before == full_inputs[ticker] and len(out['events']) == 46
    audit = out['wlm_full_calendar_repair']
    assert audit['original_issues'] == before['report']['issues']
    assert audit['original_vendor_JCP_group'][0]['record_date'] == '2024-07-15'
    assert audit['replacement_primary_JCP_group'][0]['record_date'] == '2024-08-12'
    assert audit['replacement_primary_JCP_group'][0]['payment_date'] == '2024-08-27'
    assert audit['replacement_primary_JCP_group'][0]['gross_per_share'] == amount
    assert not audit['vendor_amount_rescaled_or_net_reconstructed']
    assert audit['JCP_source_proof']['tax_document_deadline'] == '2024-07-15'
    assert audit['normal_cash_price_share_and_annual_gates_unchanged']
    assert not audit['parent_admission_performed'] and not audit['settlement_verified']
    assert len(audit['evidence_sources']) == sources
    mandatory = audit['replacement_primary_mandatory2022_group']
    assert len(mandatory) == 1 and mandatory[0]['payment_date'] == '2022-06-29'
    assert mandatory[0]['published_at'] == '2023-05-05'
    if ticker == 'WLMM3':
        assert len(audit['original_vendor_mandatory2022_group']) == 2
        assert mandatory[0]['gross_per_share'] == .25287
        assert audit['2022_vendor_rows_removed']
    else:
        assert audit['original_vendor_mandatory2022_group'] == []
        assert mandatory[0]['gross_per_share'] == .27815
        assert audit['source_2022_group_added_from_primary_not_zero_imputation']
    changed_records = {'2022-04-29','2024-07-15'}
    if ticker == 'WLMM3': changed_records.add('2024-04-29')
    unchanged = [r for r in before['events'] if r['record_date'] not in changed_records]
    assert all(r in out['events'] for r in unchanged)
    assert all(i in out['report']['issues'] for i in before['report']['issues']
        if i['blocking'] and i['row'] not in {'table:2:row:16','table:2:row:26'})


def test_full_evidence_actual_bytes_receipts_closed_and_no_fake_RI_body(full_calendars):
    for proof in full_calendars.values():
        sources = proof['evidence_sources']
        assert len({(s['file'], s['sha256'], s.get('url')) for s in sources}) == len(sources)
        for source in sources:
            body = (ROOT / source['file']).read_bytes()
            assert hashlib.sha256(body).hexdigest() == source['sha256'] and len(body) == source['bytes']
        assert proof['owned_source_acquisition_total_requests'] == 8
        assert proof['network_requests_in_verification'] == 0
        assert proof['unusable_JCP_notice']['status'] == 'unusable_HTML_shell_no_retry'


@pytest.mark.parametrize('fault', ['net_header','missing_PN','zero','negative','missing_JCP','extra_mandatory','deadline_only'])
def test_whole_FRE_table_rejects_partial_net_or_nonpositive_gross(full_calendars, fault):
    text = wlm.pdf_pages((ROOT / wlm.NATIVE_FRE_PDF).read_bytes(),1)[0][0]
    if fault == 'net_header': text = text.replace('BRUTO','LIQUIDO')
    elif fault == 'missing_PN': text = text.replace('0,27815','')
    elif fault == 'zero': text = text.replace('0,25287','0,00000')
    elif fault == 'negative': text = text.replace('0,25287','-0,25287')
    elif fault == 'missing_JCP': text = text.replace('29/03/21', 'Missing source row')
    elif fault == 'extra_mandatory': text = text.replace('Total 22.910','Saldo de dividendo obrigatorio sobre o lucro liquido do exercicio ajustado 9.710 0,25287 0,27815 29/06/2022 Total 22.910')
    elif fault == 'deadline_only': text = text.replace('29/06/2022','ate 29/06/2022')
    with pytest.raises(wlm.ProofError):
        wlm.parse_fre2022_schedule(text)


@pytest.mark.parametrize('key,value', [('ticker','OTHER4'),('share_class','PN'),('cvm',9999),
    ('cvm','11070'),('isin','BRWLMMACNOR6')])
def test_full_PNA_exact_security_never_guessed_from_suffix(key,value):
    stock = {'ticker':'WLMM4','cvm':11070,'share_class':'PNA','isin':'BRWLMMACNPR3',key:value}
    with pytest.raises(wlm.ProofError, match='exact_WLMM3_or_WLMM4_identity_required'):
        wlm.verified_followon_calendar(ROOT,stock)


@pytest.mark.parametrize('fault',['missing','duplicate','crossclass','gross','payment','record','kind','unit'])
def test_typed_independent_class_view_requires_full_original_page(full_calendars,monkeypatch,fault):
    proof = full_calendars['WLMM4']
    monkeypatch.setattr(wlm,'verified_followon_calendar',lambda *a: deepcopy(proof))
    rows = json.loads((ROOT/'raw/expanded/WLMM4-b3-cash-1.json').read_bytes())['results']
    before=deepcopy(rows)
    if fault=='missing': rows.pop()
    elif fault=='duplicate': rows.append(deepcopy(rows[0]))
    elif fault=='crossclass': rows[0]['typeStock']='PNB'
    elif fault=='gross': rows[0]['valueCash']='0,01'
    elif fault=='payment': rows[0]['datePayment']='01/01/2022'
    elif fault=='record': rows[0]['lastDatePriorEx']='01/01/2022'
    elif fault=='kind': rows[0]['corporateAction']='DIVIDENDO'
    elif fault=='unit': rows[0]['quotedPerShares']='1000'
    with pytest.raises(wlm.ProofError,match='entire_exact_original_cash_page'):
        wlm.canonical_independent_cash_rows(ROOT,proof,wlm.CUTOFF,rows)
    assert json.loads((ROOT/'raw/expanded/WLMM4-b3-cash-1.json').read_bytes())['results']==before


def test_class_crosswalk_is_label_only_no_amount_date_count_or_raw_file_change(full_calendars,monkeypatch):
    proof=full_calendars['WLMM4']
    monkeypatch.setattr(wlm,'verified_followon_calendar',lambda *a:deepcopy(proof))
    rows=json.loads((ROOT/'raw/expanded/WLMM4-b3-cash-1.json').read_bytes())['results']
    result=wlm.canonical_independent_cash_rows(ROOT,proof,wlm.CUTOFF,rows)
    assert len(result['cash_rows'])==len(rows)==108
    assert result['original_cash_rows']==rows and not result['raw_files_changed']
    assert result['class_crosswalk']['legal_class_kept']=='PNA'
    for original,typed in zip(rows,result['cash_rows']):
        assert {k:v for k,v in typed.items() if k not in ('reported_typeStock','class_normalization','typeStock')}=={
            k:v for k,v in original.items() if k!='typeStock'}
        assert typed['typeStock']==('PNA' if original['typeStock']=='PN' else original['typeStock'])


@pytest.mark.parametrize('file_glob', ['native-FRE2022-index-*.zip','native-FRE2022-latest-*.zip',
    'native-FRE125831-annex-103-*.pdf','followon-779969-*.pdf','followon-781239-*.pdf',
    'followon-506835-*.pdf','response-native-FRE2022-latest-*.json','request-native-FRE2022-index.json'])
def test_full_original_source_corruption_rejected(monkeypatch, tmp_path, file_glob):
    name,= (ROOT/wlm.CACHE).glob(file_glob)
    corrupt=tmp_path/'corrupt.bin'; corrupt.write_bytes(name.read_bytes()+b' ')
    safe=wlm.shared._safe
    monkeypatch.setattr(wlm.shared,'_safe',lambda root,p:corrupt if p==name.relative_to(ROOT).as_posix() else safe(root,p))
    with pytest.raises(ValueError,match='original_content_hash_mismatch'):
        wlm.verified_followon_calendar(ROOT,wlm.STOCK)


@pytest.mark.parametrize('fault',['drop_good','drop_bad','extra_group','crossclass','wrong_kind','net_gross',
    'wrong_unit','unmatched_issue','unmatched_amount','original_issues_removed'])
def test_full_repair_refuses_any_difference_from_exact_original_inspection(full_calendars,full_inputs,monkeypatch,fault):
    proof=full_calendars['WLMM3']; parsed=deepcopy(full_inputs['WLMM3'])
    monkeypatch.setattr(wlm,'verified_followon_calendar',lambda *a:deepcopy(proof))
    group=[r for r in parsed['events'] if r['record_date']=='2022-04-29']
    if fault=='drop_good': parsed['events'].remove(group[1])
    elif fault=='drop_bad': parsed['events'].remove(group[0])
    elif fault=='extra_group': parsed['events'].append(deepcopy(group[0]))
    elif fault=='crossclass': group[0]['isin']='BRWLMMACNPR3'
    elif fault=='wrong_kind': group[0]['label']='JCP'
    elif fault=='net_gross': group[0]['gross_per_share']*=.85
    elif fault=='wrong_unit': group[0]['quoted_quantity']=1000
    elif fault=='unmatched_issue': parsed['report']['issues'].append({'code':'unknown_tax_basis','blocking':True,'row':'table:2:row:2'})
    elif fault=='unmatched_amount': parsed['events'][0]['gross_per_share']=.01
    elif fault=='original_issues_removed': parsed['report']['issues']=[]
    before=deepcopy(parsed)
    with pytest.raises(wlm.ProofError,match='full_repair_requires_exact_original_inspector_output'):
        wlm.repair_full_inspected_calendar(ROOT,proof,wlm.CUTOFF,parsed)
    assert parsed==before


@pytest.mark.parametrize('fault',['newer_version','newer_filing','missing_preferred','preferred_class_changed','payment_conflict'])
def test_native_FRE_latest_master_and_whole_class_proof_fail_closed(monkeypatch,fault):
    original=wlm.csv.DictReader
    def altered(*a,**k):
        rows=list(original(*a,**k))
        if rows and 'ID_DOC' in rows[0] and fault.startswith('newer_'):
            current=next(r for r in rows if r['ID_DOC']=='125831')
            newer=deepcopy(current); newer['VERSAO']='4'; newer['DT_RECEB']='2023-05-06'
            if fault=='newer_filing': newer['ID_DOC']='999999'
            rows.append(newer)
        if rows and 'Especie_Acao' in rows[0]:
            target=next(r for r in rows if r['ID_Documento']=='125831'
                        and r['Data_Fim_Exercicio_Social']=='2021-12-31' and r['Especie_Acao']=='Preferencial')
            if fault=='missing_preferred': rows.remove(target)
            elif fault=='preferred_class_changed': target['Classe_Acao']='Preferencial Classe B'
            elif fault=='payment_conflict': target['Data_Pagamento_Dividendo']='2022-06-30'
        return iter(rows)
    monkeypatch.setattr(wlm.csv,'DictReader',altered)
    with pytest.raises(wlm.ProofError):
        wlm.read_latest_native_fre2022(wlm.EvidenceReader(ROOT))

@pytest.mark.parametrize('path',['NumeroSequencialDocumento','NumeroVersaoDocumento','CompanhiaAberta/CodigoCvm',
    'CompanhiaAberta/NumeroCnpjCompanhiaAberta','DataEntrega','ProtocoloEntrega'])
def test_native_FRE_outer_XML_exact_company_document_protocol_binding(monkeypatch,path):
    original=wlm.ET.fromstring
    def altered(*a,**k):
        root=original(*a,**k)
        if root.tag=='Documento': root.find(path).text='incorrect-identity'
        return root
    monkeypatch.setattr(wlm.ET,'fromstring',altered)
    with pytest.raises((wlm.ProofError,ValueError)):
        wlm.read_latest_native_fre2022(wlm.EvidenceReader(ROOT))

