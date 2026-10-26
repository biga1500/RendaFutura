"""Offline real-byte and adversarial proof tests; no parent admission or edits."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import urllib.request

import pdfplumber
import pytest

from . import pnvl_native_calendar as native


FOLDER = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def calendar():
    return native.verified_calendar(FOLDER, native.STOCK)


@pytest.fixture(scope='module')
def inputs(calendar):
    reader = native.EvidenceReader(FOLDER)
    docs, _ = native.exact.indexed_documents(reader)
    document = next(d for d in docs if d['sequence'] == '742120')
    body = native.exact.document_body(reader, document)[0]
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    originals = native.read_followon(reader)
    notice = native.review_notice((FOLDER / originals['741697']['source']['file']).read_bytes())
    detail, pages, _ = native.cached_exchange(reader)
    return {'document': document, 'documents': docs, 'tables': tables, 'notice': notice,
            'detail': detail, 'pages': pages, 'form': calendar['native_form']}


def frozen_state():
    repo = Path(__file__).parent
    files = [repo / name for name in ('primary_cash_exact_research.py', 'test_primary_cash_exact_research.py',
                                     'primary_cash_followon_research.py', 'test_primary_cash_followon_research.py')]
    for folder in ('primary-cash-exact', 'primary-cash-followon'):
        files += [p for p in (FOLDER / 'raw/primary-research' / folder).rglob('*') if p.is_file()]
    return {str(p): native.exact.shared._sha(p.read_bytes()) for p in files}


def test_real_native_calendar_is_complete_typed_and_not_admitted(calendar):
    assert native.validate_calendar_contract(calendar)
    assert [(e['payment_date'], e['gross_per_share_decimal'], e['label']) for e in calendar['events']] == [
        ('2024-08-30', '0.05911502000', 'JCP'), ('2025-03-31', '0.01880932000', 'JCP')]
    group = calendar['complete_groups'][0]
    assert group['id'] == 'PNVL3:2024-04-05'
    assert group['events'] == calendar['events'] and group['installment_count'] == 2
    assert sum(Decimal(e['gross_per_share_decimal']) for e in calendar['events']) == Decimal('0.07792434000')
    assert calendar['native_form']['cash_kind'] is None
    assert calendar['native_form']['native_protocol'] == '1216970'
    assert calendar['native_form']['native_IPE'] == '009342IPE280320240204537912-33'
    assert calendar['native_form']['sent_at'] == '2024-04-02'
    assert calendar['native_form']['published_at'] == '2024-04-03'
    assert group['source_chain']['latest_revisions']['notice']['sequence'] == '741697'
    assert group['source_chain']['latest_revisions']['board']['sequence'] == '741696'
    assert not calendar['parent_admission_performed'] and not calendar['financially_validated']
    assert not calendar['history_complete'] and not calendar['active_model_changed']
    assert not calendar['settlement_verified'] and not calendar['point_in_time_archive_verified']
    assert calendar['required_parent_gates'] == ['normal_atomic_replace_verified_primary_groups_expected_actions',
        'normal_cash_reconciliation', 'normal_price_reconciliation', 'normal_financial_features_and_training',
        'source_conflict_warning_in_flat_metadata_and_dataset']
    for event in calendar['events']:
        assert event['isin'] == 'BRPNVLACNOR9' and event['share_class'] == 'ON'
        assert event['payment_date_basis'] == 'explicit_calendar_date'
        assert event['corroborating_notice_date_basis'] == 'explicit_payment_start_date_not_settlement'
        assert not event['full_settlement_on_date_verified'] and not event['is_settlement_confirmation']
        assert not event['cash_model_eligible'] and not event['gross_reconstructed_from_net']
        assert event['amount_tax_basis'] == 'explicit_gross_exact_ISIN_unit'


def test_explicit_type_is_independent_full_event_proof_not_v1_inheritance(calendar):
    proof = calendar['complete_groups'][0]['cash_type_proof']
    assert proof['label'] == 'JCP'
    assert proof['basis'] == 'explicit_issuer_notice_plus_unique_complete_cached_exchange_event'
    assert len(proof['located_cached_rows']) == 2
    assert [r['row'] for r in proof['located_cached_rows']] == [25, 26]
    assert all(r['raw']['corporateAction'] == 'JRS CAP PROPRIO' for r in proof['located_cached_rows'])
    assert proof['issuer_notice_version'] is None
    assert not proof['issuer_notice_exact_CVM_v2_identity_verified']
    assert not proof['prior_version_type_inherited'] and not proof['kind_inferred_from_amount_or_status']
    assert set(proof['binding_terms']) == {'CVM', 'CNPJ', 'ticker', 'ISIN', 'share_class', 'approval_date',
        'record_date', 'each_gross_installment', 'whole_gross_sum', 'explicit_type'}


def test_rule_is_generic_and_has_no_reconciliation_selection_or_legal_claims(inputs):
    rule = native.modelling_priority_rule()
    assert not rule['selection_uses_cash_or_price_match']
    assert not rule['company_legal_precedence_claim'] and not rule['date_amendment_claim']
    assert not rule['RI_typo_claim'] and not rule['settlement_claim']
    data = deepcopy(inputs)
    stock = {'ticker': 'TEST3', 'cvm': 8888, 'isin': 'BRTESTACNOR1', 'share_class': 'ON'}
    doc = data['document']
    doc['cvm'], doc['issuer_name'] = 8888, 'OUTRA S.A.'
    doc['index_evidence']['raw_row']['Nome_Companhia'] = 'OUTRA S.A.'
    doc['index_evidence']['raw_row']['CNPJ_Companhia'] = '12.345.678/0001-90'
    data['tables'][0][1][0] = 'OUTRA S.A.'
    data['tables'][0][1][1] = data['tables'][0][1][1].replace('009342IPE', '008888IPE')
    data['tables'][1][2][0] = stock['isin']
    form = native.parse_native_tables(data['tables'], doc, stock, native.CUTOFF)
    notice = data['notice']
    notice.update(ticker='TEST3', cnpj='12345678000190', label='DIVIDENDO')
    detail = data['detail']
    detail.update(codeCVM='8888', cnpj=notice['cnpj'], otherCodes=[{'code': 'TEST3', 'isin': stock['isin']}])
    for row in data['pages'][0]['results']:
        if row['dateApproval'] == '28/03/2024' and row['lastDatePriorEx'] == '05/04/2024':
            row['corporateAction'] = 'DIVIDENDO'
    assert native.prove_cash_kind(form, notice, stock, detail, data['pages'])['label'] == 'DIVIDENDO'
    assert native.modelling_priority_rule() == rule


@pytest.mark.parametrize('fault', ['second_ISIN', 'wrong_ISIN', 'wrong_CVM', 'wrong_name', 'wrong_CNPJ',
    'net_header', 'net_annex', 'foreign_currency', 'lot_unit', 'float_total', 'zero_total', 'negative', 'NaN', 'partial', 'blank_annex',
    'extra_annex', 'malformed_row', 'duplicate_row', 'duplicate_date_other_amount', 'reverse_dates',
    'missing_date', 'pre_record', 'wrong_sum', 'float_annex', 'ambiguous_dot', 'wrong_version',
    'future_publication', 'sent_after_publication', 'record_before_approval', 'ambiguous_type'])
def test_native_form_fail_closed_adversarial_whole_annex_and_metadata(inputs, fault):
    data = deepcopy(inputs)
    t, doc = data['tables'], data['document']
    if fault == 'second_ISIN': t[1].append(deepcopy(t[1][2]))
    elif fault == 'wrong_ISIN': t[1][2][0] = 'BRWRONGCNOR1'
    elif fault == 'wrong_CVM': t[0][1][1] = t[0][1][1].replace('009342IPE', '008888IPE')
    elif fault == 'wrong_name': t[0][1][0] = 'OUTRA SA'
    elif fault == 'wrong_CNPJ': doc['index_evidence']['raw_row']['CNPJ_Companhia'] = ''
    elif fault == 'net_header': t[1][1][1] = 'Valor Liquido (R$/Unidade)'
    elif fault == 'net_annex': t[2][0][1] = 'Valor Liquido (R$/Unidade)'
    elif fault == 'foreign_currency': t[1][1][1] = 'Valor Bruto (USD/Unidade)'
    elif fault == 'lot_unit': t[1][1][1] = 'Valor Bruto (R$/Lote)'
    elif fault == 'float_total': t[1][2][1] = .07792434
    elif fault == 'zero_total': t[1][2][1] = '0,0'
    elif fault == 'negative': t[2][1][1] = '-0.05911502000'
    elif fault == 'NaN': t[2][1][1] = 'NaN'
    elif fault == 'partial': t[2].pop()
    elif fault == 'blank_annex': t[2] = t[2][:1]
    elif fault == 'extra_annex': t.append(deepcopy(t[2]))
    elif fault == 'malformed_row': t[2][1].append('ignored-extra-cell')
    elif fault == 'duplicate_row': t[2].append(deepcopy(t[2][1]))
    elif fault == 'duplicate_date_other_amount': t[2][2][0] = t[2][1][0]
    elif fault == 'reverse_dates': t[2][1:] = list(reversed(t[2][1:]))
    elif fault == 'missing_date': t[2][2][0] = ''
    elif fault == 'pre_record': t[2][1][0] = '01/04/2024'
    elif fault == 'wrong_sum': t[2][2][1] = '0.01827190000'
    elif fault == 'float_annex': t[2][2][1] = .01880932
    elif fault == 'ambiguous_dot': t[2][2][1] = '0.018'
    elif fault == 'wrong_version': t[0][7][1] = '1'
    elif fault == 'future_publication': doc['published_at'] = '2026-10-01'
    elif fault == 'sent_after_publication': t[0][7][2] = '04/04/2024'
    elif fault == 'record_before_approval': t[0][5][1] = '27/03/2024'
    elif fault == 'ambiguous_type': t.append([['Tipo de Provento'], ['DIVIDENDO / JCP']])
    with pytest.raises(native.ProofError):
        native.parse_native_tables(t, doc, native.STOCK, native.CUTOFF)


@pytest.mark.parametrize('fault', ['notice_v3', 'board_v3', 'notice_later_same_version', 'board_later_same_version',
    'other_later_category', 'revision_changed_reference_date', 'ambiguous_form', 'wrong_index_URL', 'wrong_index_CVM', 'wrong_full_IPE'])
def test_later_unknown_revisions_and_wrong_exact_index_bindings_fail_closed(inputs, fault):
    docs = deepcopy(inputs['documents'])
    notice = next(d for d in docs if d['sequence'] == '741697')
    board = next(d for d in docs if d['sequence'] == '741696')
    form = next(d for d in docs if d['sequence'] == '742120')
    if fault == 'notice_v3': notice['version'] = 3
    elif fault == 'board_v3': board['version'] = 3
    elif fault == 'notice_later_same_version': notice['published_at'] = '2024-04-04'
    elif fault == 'board_later_same_version': board['published_at'] = '2024-04-04'
    elif fault == 'other_later_category':
        other = deepcopy(notice)
        other.update(category='Fato Relevante', published_at='2024-04-04')
        docs.append(other)
    elif fault == 'revision_changed_reference_date':
        later = deepcopy(notice)
        later.update(sequence='999998', version=3, reference_date='2024-04-04', published_at='2024-04-04')
        later['index_evidence']['raw_row']['Protocolo_Entrega'] = '009342IPE280320240304537912-82'
        docs.append(later)
    elif fault == 'ambiguous_form': docs.append(deepcopy(form))
    elif fault == 'wrong_index_URL': form['url'] = form['url'].replace('numVersao=2', 'numVersao=1')
    elif fault == 'wrong_index_CVM': form['index_evidence']['raw_row']['Codigo_CVM'] = '8888'
    elif fault == 'wrong_full_IPE': board['index_evidence']['raw_row']['Protocolo_Entrega'] = '009342IPE280320240104537912-33'
    with pytest.raises(native.ProofError):
        native.select_revision_set(docs, native.STOCK, native.APPROVAL, native.CUTOFF)


def test_newer_native_form_without_reviewed_body_never_falls_back(inputs, monkeypatch):
    docs = deepcopy(inputs['documents'])
    later = deepcopy(next(d for d in docs if d['sequence'] == '742120'))
    later.update(sequence='999999', version=3, published_at='2024-04-04')
    later['url'] = later['url'].replace('numSequencia=742120', 'numSequencia=999999').replace('numVersao=2', 'numVersao=3')
    later['index_evidence']['raw_row'].update(Link_Download=later['url'], Versao='3', Data_Entrega='2024-04-04')
    docs.append(later)
    monkeypatch.setattr(native.exact, 'indexed_documents', lambda reader: (docs, []))
    with pytest.raises(native.ProofError, match='latest_native_form_not_reviewed'):
        native.verified_calendar(FOLDER, native.STOCK)


@pytest.mark.parametrize('key,value', [('native_protocol', '1215097'), ('native_IPE', '009342IPE280320240104537912-33'),
                                      ('version', 1)])
def test_native_body_must_reference_the_latest_exact_board_protocol(inputs, key, value):
    revisions, _ = native.select_revision_set(inputs['documents'], native.STOCK, native.APPROVAL, native.CUTOFF)
    form = deepcopy(inputs['form'])
    form[key] = value
    with pytest.raises(native.ProofError, match='latest_exact_board_protocol'):
        native.bind_native_protocol(form, revisions)


@pytest.mark.parametrize('fault', ['missing_kind', 'status_as_kind', 'ambiguous_kind', 'wrong_kind', 'notice_kind',
    'wrong_class', 'wrong_approval', 'wrong_record', 'wrong_ISIN', 'duplicate_ISIN_identity', 'wrong_CNPJ',
    'wrong_CVM', 'scaled_unit', 'wrong_gross', 'net_gross', 'missing_component', 'extra_component',
    'notice_partial', 'notice_net_total', 'native_kind_conflict', 'pagination_missing'])
def test_cash_kind_requires_explicit_unique_complete_bound_event(inputs, fault):
    data = deepcopy(inputs)
    rows = data['pages'][0]['results']
    row = rows[25]
    if fault == 'missing_kind': row['corporateAction'] = ''
    elif fault == 'status_as_kind': row['corporateAction'] = 'PAGO'
    elif fault == 'ambiguous_kind': row['corporateAction'] = 'DIVIDENDO / JRS CAP PROPRIO'
    elif fault == 'wrong_kind': row['corporateAction'] = 'DIVIDENDO'
    elif fault == 'notice_kind': data['notice']['label'] = 'DIVIDENDO'
    elif fault == 'wrong_class': row['typeStock'] = 'PN'
    elif fault == 'wrong_approval': row['dateApproval'] = '29/03/2024'
    elif fault == 'wrong_record': row['lastDatePriorEx'] = '08/04/2024'
    elif fault == 'wrong_ISIN': data['detail']['otherCodes'][0]['isin'] = 'BRWRONGCNOR1'
    elif fault == 'duplicate_ISIN_identity': data['detail']['otherCodes'].append(deepcopy(data['detail']['otherCodes'][0]))
    elif fault == 'wrong_CNPJ': data['detail']['cnpj'] = '00000000000000'
    elif fault == 'wrong_CVM': data['detail']['codeCVM'] = '8888'
    elif fault == 'scaled_unit': row['quotedPerShares'] = '1000'
    elif fault == 'wrong_gross': row['valueCash'] = '0,05911500'
    elif fault == 'net_gross': row['valueCash'] = '0,06623569'
    elif fault == 'missing_component': rows.pop(26)
    elif fault == 'extra_component': rows.append(deepcopy(row))
    elif fault == 'notice_partial': data['notice']['installments'].pop()
    elif fault == 'notice_net_total': data['notice']['gross_total_decimal'] = '0.06623569'
    elif fault == 'native_kind_conflict': data['form']['cash_kind'] = 'DIVIDENDO'
    elif fault == 'pagination_missing': data['pages'].pop()
    with pytest.raises(native.ProofError):
        native.prove_cash_kind(data['form'], data['notice'], native.STOCK, data['detail'], data['pages'])


def test_conflicting_unknown_RI_version_is_preserved_and_warning_is_mandatory(calendar):
    conflict = calendar['source_conflicts'][0]
    assert conflict['source_sha256'] == native.BOARD_SHA
    assert conflict['source_version'] is None and conflict['exact_CVM_v2_copy_identity_verified'] is False
    assert conflict['reported_prose_date'] == '2025-03-28'
    assert conflict['reported_table_date'] == conflict['model_declared_date'] == '2025-03-31'
    assert conflict['unresolved'] and conflict['resolution'] is None
    warnings = native.metadata_warnings(calendar)
    assert native.validate_warning_propagation(calendar, warnings)
    with pytest.raises(native.ProofError, match='warning_missing'):
        native.validate_warning_propagation(calendar, [])
    with pytest.raises(native.ProofError, match='warning_missing'):
        native.validate_warning_propagation(calendar, warnings * 2)
    for field in calendar['warning_propagation_contract']['preserve_fields']:
        altered = deepcopy(warnings)
        altered[0].pop(field)
        with pytest.raises(native.ProofError):
            native.validate_warning_propagation(calendar, altered)


@pytest.mark.parametrize('fault', ['partial_calendar', 'partial_group', 'numeric_gross', 'net', 'gross_up', 'wrong_class',
    'wrong_ISIN', 'wrong_label', 'start_as_settlement', 'drop_conflict', 'resolve_conflict', 'RI_version',
    'fake_v2_copy', 'wrong_SHA', 'typo', 'amendment', 'source_closure', 'priority'])
def test_calendar_cannot_bypass_atomic_group_or_unresolved_warning_contract(calendar, fault):
    c = deepcopy(calendar)
    if fault == 'partial_calendar': c['events'].pop()
    elif fault == 'partial_group': c['complete_groups'][0]['events'].pop()
    elif fault == 'numeric_gross': c['events'][0]['gross_per_share_decimal'] = .05911502
    elif fault == 'net': c['events'][0]['gross_per_share_decimal'] = '0.050247767'
    elif fault == 'gross_up': c['events'][0]['gross_reconstructed_from_net'] = True
    elif fault == 'wrong_class': c['events'][0]['share_class'] = 'PN'
    elif fault == 'wrong_ISIN': c['events'][0]['isin'] = 'BRWRONGCNOR1'
    elif fault == 'wrong_label': c['events'][0]['label'] = 'DIVIDENDO'
    elif fault == 'start_as_settlement': c['events'][0]['full_settlement_on_date_verified'] = True
    elif fault == 'drop_conflict': c['source_conflicts'] = []
    elif fault == 'resolve_conflict': c['source_conflicts'][0]['unresolved'] = False
    elif fault == 'RI_version': c['source_conflicts'][0]['source_version'] = 2
    elif fault == 'fake_v2_copy': c['source_conflicts'][0]['exact_CVM_v2_copy_identity_verified'] = True
    elif fault == 'wrong_SHA': c['source_conflicts'][0]['source_sha256'] = '0' * 64
    elif fault == 'typo': c['source_conflicts'][0]['typo_claim'] = True
    elif fault == 'amendment': c['source_conflicts'][0]['date_amendment_claim'] = True
    elif fault == 'source_closure': c['evidence_sources'] = []
    elif fault == 'priority': c['modelling_priority_rule']['selection_uses_cash_or_price_match'] = True
    with pytest.raises(native.ProofError):
        native.metadata_warnings(c)


def test_all_actual_original_index_and_type_reader_receipts_have_real_hashes(calendar):
    receipts = calendar['evidence_sources']
    keys = {(s['file'], s['url']) for s in receipts}
    assert len(keys) == len(receipts)
    for source in receipts:
        assert native.exact.shared._sha((FOLDER / source['file']).read_bytes()) == source['sha256']
        assert source['hash_basis'] == 'actual_bytes_read'
        assert source['roles']
    indices = [s for s in receipts if 'native_CVM_IPE_all_categories' in s['roles']]
    assert len(indices) == 6
    actual = {s['file']: s for s in receipts}
    assert actual['raw/primary-research/cvm-documents/index-sources.json']['url'] is None
    assert actual['expanded-source-audit.json']['url'] is None
    assert actual[native.FOLLOWON]['url'] is None
    assert actual['raw/expanded/PNVL3-b3-cash-1.json']['url'].startswith('https://sistemaswebb3-listados.b3.com.br/')
    assert actual['raw/expanded/PNVL3-b3-cash-2.json']['retrieved_at']
    assert any(s['sha256'] == native.FORM_SHA for s in receipts)
    assert any(s['sha256'] == native.BOARD_SHA for s in receipts)
    assert calendar['sources'][0]['retrieved_at'] == '2026-10-25T06:23:54.277204+00:00'
    assert calendar['sources'][0]['observed_at'] is None
    for pin in calendar['manifest_pins'].values():
        assert actual[pin['file']]['sha256'] == pin['sha256']


def test_actual_parent_group_predicate_accepts_full_group_and_rejects_partial(calendar):
    from .cash_calendar_research import _verified_primary_group_event
    assert all(_verified_primary_group_event(e, calendar) for e in calendar['events'])
    partial = deepcopy(calendar)
    partial['complete_groups'][0]['events'].pop()
    assert not any(_verified_primary_group_event(e, partial) for e in partial['events'])


def test_actual_parent_normal_atomic_replacement_still_requires_expected_match(calendar, inputs):
    from .cash_calendar_research import replace_verified_primary_groups
    from .collect_market_data import DividendHistory
    from .share_basis import b3_evidence
    original = DividendHistory(native.CUTOFF, 1)
    old = {'ticker': 'PNVL3', 'record_date': '2024-04-05', 'payment_date': '2025-03-28',
           'label': 'JCP', 'gross_per_unit': .07792434}
    original.installments = [old]
    original.semantic_events = [old]
    original.coverage_start = date(2021, 1, 1)
    before = deepcopy(original.__dict__)
    no_match = replace_verified_primary_groups(original, calendar, {date(2024, 4, 5): .01}, [], native.CUTOFF)
    assert no_match.installments == [old]
    expected, _ = b3_evidence([row for page in inputs['pages'] for row in page['results']], 'ON')
    assert expected[date(2024, 4, 5)] == pytest.approx(.07792434)
    replaced = replace_verified_primary_groups(original, calendar, expected, [], native.CUTOFF)
    assert [(r['payment_date'], r['gross_per_unit']) for r in replaced.installments] == [
        ('2024-08-30', .05911502), ('2025-03-31', .01880932)]
    assert len(replaced.research_group_corrections) == 1
    assert original.__dict__ == before
    assert not calendar['parent_admission_performed']


@pytest.mark.parametrize('name,operation', [
    ('raw/primary-research/cvm-documents/index-sources.json', 'indices'),
    (native.FOLLOWON, 'RI_chain'), ('expanded-source-audit.json', 'exchange_manifest')])
def test_changed_real_manifest_bytes_are_rejected_before_source_selection(tmp_path, name, operation):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FOLDER / name).read_bytes() + b' ')
    reader = native.EvidenceReader(tmp_path)
    function = {'indices': native.exact.indexed_documents, 'RI_chain': native.read_followon,
                'exchange_manifest': native.cached_exchange}[operation]
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        function(reader)


def test_actual_original_byte_tampering_cannot_be_hidden_by_receipt(tmp_path, calendar):
    source = calendar['sources'][0]
    path = tmp_path / source['file']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FOLDER / source['file']).read_bytes() + b' ')
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        native.EvidenceReader(tmp_path).read(source['file'], 'original', source['sha256'], source['url'])


def test_real_API_has_no_network_or_old_helper_report_writes(monkeypatch):
    before = frozen_state()
    def forbidden(*args, **kwargs):
        raise AssertionError('network_forbidden')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    first = native.verified_calendar(FOLDER, native.STOCK)
    assert first == native.verified_calendar(FOLDER, native.STOCK)
    assert frozen_state() == before


def test_missing_type_returns_concrete_blocked_report_and_no_calendar(tmp_path, monkeypatch):
    def blocked(reader, stock, cutoff):
        reader.read('original.bin', 'actual_cached_test_original')
        raise native.ProofError('cash_kind_missing_or_ambiguous_not_inferred')
    (tmp_path / 'original.bin').write_bytes(b'cached-owned-test')
    monkeypatch.setattr(native, '_verified_calendar', blocked)
    report, path = native.build_report(tmp_path)
    assert report['status'] == 'blocked_no_calendar' and report['calendar'] is None
    assert report['blocked_reason'] == 'cash_kind_missing_or_ambiguous_not_inferred'
    assert report['evidence_sources'][0]['file'] == 'original.bin'
    assert report['evidence_sources'][0]['url'] is None
    assert path.startswith(native.CACHE + '/') and (tmp_path / path).is_file()
    assert report['network_requests'] == 0 and not report['parent_admission_performed']


@pytest.mark.parametrize('field,value', [('ticker', 'PNVL4'), ('cvm', 8888), ('cvm', 9342.0),
                                      ('share_class', 'PN'), ('isin', 'BRWRONGCNOR1')])
def test_unreviewed_identity_cannot_reuse_calendar(field, value):
    stock = {**native.STOCK, field: value}
    with pytest.raises(native.ProofError, match='exact_security_identity'):
        native.verified_calendar(FOLDER, stock)


def test_unreviewed_cutoff_cannot_reuse_calendar():
    with pytest.raises(native.ProofError, match='cutoff'):
        native.verified_calendar(FOLDER, native.STOCK, date(2026, 10, 25))
