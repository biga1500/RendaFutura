from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import socket

import pytest

from .primary_cash_bridge import PrimaryCashBridge, primary_events


STOCK = {'ticker': 'TEST3', 'cvm': 123, 'isin': 'BRTESTACNOR0', 'share_class': 'ON'}
ROW = {'cvm': 123, 'isin': STOCK['isin'], 'record_date': '2026-07-10',
       'approval_date': '2026-07-01', 'payment_date': '2026-08-01',
       'gross_per_unit_decimal': '0.12345678901', 'source_table': 1, 'source_row': 2,
       'label': None, 'issues': [], 'status': 'past_scheduled'}
DOC = {'cvm': 123, 'pages': 1, 'published_at': '2026-07-02',
       'url': 'https://primary.example/doc1', 'sha256': 'a' * 64,
       'file': 'raw/original.pdf', 'sequence': '1', 'version': 1, 'events': [ROW]}
RI_SOURCE = {'id': 'ri:TEST3', 'sha256': 'b' * 64, 'file': 'raw/history.html',
             'url': 'https://issuer.example/history', 'retrieved_at': '2026-10-24T22:00:00-03:00'}
RI = {'ticker': 'TEST3', 'share_class': 'ON', 'label': 'JCP',
      'payment_date': ROW['payment_date'], 'record_date': ROW['record_date'],
      'approval_date': ROW['approval_date'], 'reported_per_share': .12345678901,
      'gross_per_share': .12345678901, 'amount_tax_basis': 'explicit_gross',
      'published_at': None, 'status': 'past_scheduled', 'issues': [],
      'source_ref': RI_SOURCE['id'], 'source_url': RI_SOURCE['url'], 'source_row': 'table:1:row:2'}
PREFIX = 'raw/primary-research/'


def write(folder, name, payload):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
    return path


def tree(folder, *, documents=None, history=None, registry=None):
    write(folder, 'primary-evidence.json', registry if registry is not None else {
        'schema_version': 1, 'instruments': [STOCK], 'counts': {'model_admissions': 0}})
    write(folder, PREFIX + 'cvm-documents/report.json', {
        'schema_version': 1, 'results': deepcopy([DOC] if documents is None else documents)})
    if history is not None:
        operating(folder, 'one', history)
    return folder


def operating(folder, name, rows, *, issues=None, alternatives=None):
    return write(folder, PREFIX + 'operating/operating-sources-' + name + '.json', {
        'schema_version': 1, 'sources': [RI_SOURCE], 'instruments': {'TEST3': {
            'ticker': 'TEST3', 'events': deepcopy(rows), 'issues': issues or [],
            'alternative_observations': alternatives or {}}}})


def read(folder, stock=None, cutoff='2026-10-24'):
    return primary_events(folder, STOCK if stock is None else stock, cutoff)


def events(result, kind):
    return [row for row in result['events'] if row['kind'] == kind]


def codes(result):
    return {row['code'] for row in result['diagnostics']}


def test_declaration_is_precise_and_not_paid_and_has_hash_provenance(tmp_path):
    tree(tmp_path)
    result = read(tmp_path, cutoff=date(2026, 10, 24))
    event, = result['events']
    assert event['gross_per_unit_decimal'] == '0.12345678901'
    assert event['gross_per_share'] is event['label'] is None
    assert event['status'] == 'past_scheduled'
    assert event['published_at'] == '2026-07-02'
    assert event['cash_model_eligible'] is event['is_settlement_confirmation'] is False
    assert event['history_complete'] is False
    reference, = event['evidence']
    assert reference['version'] == 1 and reference['sha256'] == DOC['sha256']
    assert reference['hash_basis'] == 'report_assertion'
    receipt = next(row for row in result['sources'] if row['role'] == 'cvm_declarations')
    assert receipt['sha256'] == hashlib.sha256((tmp_path / receipt['file']).read_bytes()).hexdigest()
    assert reference['report_sha256'] == receipt['sha256']
    json.dumps(result, allow_nan=False)


def test_repeated_declarations_aggregate_without_summing_and_retain_versions(tmp_path):
    second = {**DOC, 'url': DOC['url'] + '/v2', 'sequence': '2', 'version': 2,
              'published_at': '2026-07-03', 'events': [{**ROW, 'gross_per_unit_decimal': '0.123456789010'}]}
    tree(tmp_path, documents=[DOC, DOC, second])
    result = read(tmp_path)
    event, = result['events']
    assert len(event['evidence']) == 2
    assert {ref['version'] for ref in event['evidence']} == {1, 2}
    assert event['gross_per_unit_decimal'] == ROW['gross_per_unit_decimal']
    assert event['published_at'] == '2026-07-02'
    assert not result['conflicts']


def test_distinct_decimals_beyond_context_precision_never_collapse(tmp_path):
    amounts = ['0.123456789012345678901234567891', '0.123456789012345678901234567892']
    tree(tmp_path, documents=[{**DOC, 'events': [{**ROW, 'gross_per_unit_decimal': amount}
                                              for amount in amounts]}])
    result = read(tmp_path)
    assert {e['gross_per_unit_decimal'] for e in result['events']} == set(amounts)
    assert len(result['conflicts']) == 1


def test_conflicting_revisions_are_retained_and_block_publication_recovery(tmp_path):
    revision = {**DOC, 'version': 2, 'url': DOC['url'] + '/v2',
                'events': [{**ROW, 'gross_per_unit_decimal': '0.2'}]}
    tree(tmp_path, documents=[DOC, revision], history=[RI])
    result = read(tmp_path)
    assert len(events(result, 'cvm_declaration')) == 2
    conflict, = result['conflicts']
    assert conflict['code'] == 'unresolved_declaration_variants'
    assert 'gross_per_unit_decimal' in conflict['fields']
    assert all(conflict['code'] in e['blockers'] for e in events(result, 'cvm_declaration'))
    assert events(result, 'issuer_ri_history')[0]['published_at'] is None


def test_document_same_url_changed_hash_is_an_explicit_blocker(tmp_path):
    tree(tmp_path, documents=[DOC, {**DOC, 'sha256': 'c' * 64}])
    result = read(tmp_path)
    assert 'same_document_url_changed_content' in codes(result)
    event, = result['events']
    assert len(event['evidence']) == 2
    assert 'same_document_url_changed_content' in event['blockers']


@pytest.mark.parametrize('field,value', [('record_date', '2026-07-11'),
                                        ('approval_date', '2026-07-03')])
def test_revisions_changing_one_date_are_explicit_conflicts(tmp_path, field, value):
    revision = {**DOC, 'version': 2, 'url': DOC['url'] + '/v2', 'events': [{**ROW, field: value}]}
    tree(tmp_path, documents=[DOC, revision])
    result = read(tmp_path)
    assert len(result['events']) == 2
    assert any(field in conflict['fields'] for conflict in result['conflicts'])


@pytest.mark.parametrize('change', [{'isin': None}, {'isin': 'BRTESTACNPR0'}, {'cvm': 999}, {'cvm': None}])
def test_CVM_requires_exact_supplied_CVM_and_ISIN(tmp_path, change):
    tree(tmp_path)
    assert not read(tmp_path, {**STOCK, **change})['events']


def test_document_event_cannot_override_document_issuer(tmp_path):
    tree(tmp_path, documents=[{**DOC, 'events': [{**ROW, 'cvm': 999}]}])
    result = read(tmp_path)
    assert not result['events']
    assert 'CVM_document_event_identity_conflict' in codes(result)


def test_cutoff_filters_publication_not_future_payment_and_keeps_unknown(tmp_path):
    future = {**DOC, 'events': [{**ROW, 'payment_date': '2027-08-01'}]}
    unpublished = {**DOC, 'published_at': None, 'url': DOC['url'] + '/unknown',
                   'events': [{**ROW, 'payment_date': None}]}
    later = {**DOC, 'published_at': '2026-10-25'}
    tree(tmp_path, documents=[future, unpublished, later])
    result = read(tmp_path)
    assert len(result['events']) == 2
    announced = next(e for e in result['events'] if e['payment_date'])
    assert announced['status'] == 'announced'
    unknown = next(e for e in result['events'] if e['payment_date'] is None)
    assert unknown['published_at'] is unknown['available_as_of'] is None
    assert 'publication_after_cutoff' in codes(result)


def test_old_declared_paid_flag_cannot_turn_CVM_into_a_payment(tmp_path):
    tree(tmp_path, documents=[{**DOC, 'events': [{**ROW, 'status': 'paid_reported'}]}])
    assert read(tmp_path)['events'][0]['status'] == 'past_scheduled'


def test_exact_ticker_registry_CVM_supports_RI_without_ISIN_or_tax_guess(tmp_path):
    history = {**RI, 'gross_per_share': None, 'amount_tax_basis': 'unspecified_by_table'}
    tree(tmp_path, history=[history], documents=[])
    result = read(tmp_path, {**STOCK, 'isin': None, 'share_class': None})
    event, = result['events']
    assert event['cvm'] == STOCK['cvm'] and event['isin'] is None
    assert event['gross_per_share'] is event['net_per_share'] is None
    assert event['reported_per_share'] == '0.12345678901'
    assert event['amount_tax_basis'] == 'unspecified_by_table'
    assert event['published_at'] is event['available_as_of'] is None


def test_registry_does_not_supply_missing_ISIN_or_invent_ticker_class(tmp_path):
    history = {key: value for key, value in RI.items() if key != 'share_class'}
    tree(tmp_path, history=[history])
    event, = read(tmp_path, {**STOCK, 'isin': None, 'share_class': None})['events']
    assert event['share_class'] is event['isin'] is None


def test_RI_requires_exact_registry_CVM_when_issuer_not_in_source(tmp_path):
    tree(tmp_path, history=[RI], documents=[], registry={'instruments': [{**STOCK, 'cvm': 999}]})
    result = read(tmp_path)
    assert not result['events']
    assert 'registry_ticker_CVM_conflict' in codes(result)
    assert 'RI_exact_ticker_CVM_unresolved' in codes(result)


def test_RI_with_explicit_CVM_does_not_need_registry_identity(tmp_path):
    tree(tmp_path, history=[{**RI, 'cvm': STOCK['cvm']}], documents=[], registry={})
    event, = read(tmp_path)['events']
    assert event['kind'] == 'issuer_ri_history'


@pytest.mark.parametrize('change', [{'ticker': 'TEST4'}, {'cvm': 999},
                                    {'share_class': 'PN'}, {'isin': 'BRTESTACNPR0'}])
def test_RI_identity_conflicts_never_map_by_suffix(tmp_path, change):
    tree(tmp_path, documents=[], history=[{**RI, **change}])
    assert not read(tmp_path)['events']


def test_explicit_net_is_not_grossed_up_or_matched_to_gross(tmp_path):
    history = {**RI, 'gross_per_share': None, 'net_per_share': .1,
               'reported_per_share': .1, 'amount_tax_basis': 'explicit_net'}
    tree(tmp_path, history=[history])
    event, = events(read(tmp_path), 'issuer_ri_history')
    assert event['gross_per_share'] is None and event['net_per_share'] == '0.1'
    assert event['published_at'] is None and not event['declaration_matches']


def test_unique_CVM_match_recovers_only_publication_without_dates_or_status(tmp_path):
    tree(tmp_path, history=[{**RI, 'record_date': None}])
    event, = events(read(tmp_path), 'issuer_ri_history')
    assert event['published_at'] == '2026-07-02'
    assert event['publication_basis'] == 'unique_primary_declaration_match'
    assert event['record_date'] is None
    assert event['status'] == 'past_scheduled' and event['cash_model_eligible'] is False


@pytest.mark.parametrize('change', [{'gross_per_share': .123456789},
                                    {'record_date': '2026-07-11'},
                                    {'issues': ['conflicting_primary_payment_dates']}])
def test_rounding_dates_and_issues_prevent_publication_recovery(tmp_path, change):
    tree(tmp_path, history=[{**RI, **change}])
    assert events(read(tmp_path), 'issuer_ri_history')[0]['published_at'] is None


def test_ambiguous_declarations_do_not_recover_publication(tmp_path):
    another = {**DOC, 'url': DOC['url'] + '/another',
               'events': [{**ROW, 'approval_date': '2026-07-03', 'record_date': '2026-07-11'}]}
    tree(tmp_path, documents=[DOC, another], history=[{**RI, 'approval_date': None, 'record_date': None}])
    result = read(tmp_path)
    event, = events(result, 'issuer_ri_history')
    assert event['published_at'] is None
    assert 'ambiguous_publication_match' in codes(result)


def test_no_CVM_unit_to_share_match_without_explicit_stock_class(tmp_path):
    tree(tmp_path, history=[RI])
    assert events(read(tmp_path, {**STOCK, 'share_class': None}), 'issuer_ri_history')[0]['published_at'] is None


def test_alternative_histories_and_source_issues_survive(tmp_path):
    tree(tmp_path, documents=[])
    other = {**RI, 'payment_date': '2026-08-02'}
    operating(tmp_path, 'one', [RI], issues=[{'code': 'conflicting_primary_payment_dates'}],
              alternatives={'english': {'events': [other]}})
    result = read(tmp_path)
    assert len(result['events']) == 2
    assert result['source_issues'][0]['issue']['code'] == 'conflicting_primary_payment_dates'
    assert result['conflicts'][0]['code'] == 'unresolved_RI_variants'
    assert any(ref['alternative'] == 'english' for e in result['events'] for ref in e['evidence'])


def test_paid_report_is_retained_without_historical_or_settlement_claim(tmp_path):
    tree(tmp_path, documents=[], history=[{**RI, 'status': 'paid_reported'}])
    event, = read(tmp_path)['events']
    assert event['status'] == 'paid_reported'
    assert event['paid_status_as_of_verified'] is event['is_settlement_confirmation'] is False
    assert event['published_at'] is None
    assert 'payment_status_historical_availability_unverified' in event['blockers']


def test_bank_supporting_notice_is_not_additional_paid_cash(tmp_path):
    tree(tmp_path, documents=[])
    calendar = {**RI, 'cvm': STOCK['cvm'], 'declaration_source_ref': 'notice',
                'published_at': '2026-07-02'}
    notice = {**RI, 'cvm': STOCK['cvm'], 'declaration_only': True, 'source_ref': 'notice',
              'published_at': '2026-07-02'}
    payload = {'results': [{'cvm': STOCK['cvm'], 'source': RI_SOURCE, 'events': [calendar]}],
               'declaration_documents': [{'cvm': STOCK['cvm'], 'source': {**RI_SOURCE, 'sha256': 'notice'},
                                         'events': [notice]}]}
    write(tmp_path, PREFIX + 'banks/ledger-one.json', payload)
    write(tmp_path, PREFIX + 'banks/ledger-two.json', payload)
    result = read(tmp_path)
    history, = events(result, 'issuer_ri_history')
    declaration, = events(result, 'issuer_ri_declaration')
    assert history['published_at'] == declaration['published_at'] == '2026-07-02'
    assert len(history['evidence']) == len(declaration['evidence']) == 2
    assert result['counts']['publication_recovered'] == 1
    assert result['counts']['paid_reported'] == 0


def test_prior_publication_enrichment_is_revalidated(tmp_path):
    tree(tmp_path, documents=[], history=[{**RI, 'declaration_source_ref': 'missing',
                                          'published_at': '2026-07-02'}])
    event, = read(tmp_path)['events']
    assert event['published_at'] is None and event['available_as_of'] is None


def test_unmapped_installments_are_issuer_blockers_never_mapped_or_summed(tmp_path):
    installment = {'isin': None, 'payment_date': '2026-08-01',
                   'gross_per_unit_decimal': '0.04', 'source_table': 2, 'source_row': 1}
    tree(tmp_path, documents=[{**DOC, 'unmapped_installment_rows': [installment]}])
    result = read(tmp_path, {**STOCK, 'isin': None})
    assert not result['events']
    row, = result['unmapped_installment_rows']
    assert row['isin'] is None and row['gross_per_unit_decimal'] == '0.04'
    assert row['code'] == 'unmapped_installment_blocks_cash'
    assert any(b['code'] == row['code'] for b in result['blockers'])


def test_missing_or_corrupt_report_has_diagnostic_not_synthetic_zero(tmp_path):
    result = read(tmp_path)
    assert not result['events'] and result['history_complete'] is False
    assert 'report_unavailable_or_invalid' in codes(result)
    tree(tmp_path)
    path = tmp_path / (PREFIX + 'cvm-documents/report.json')
    path.write_text('{broken', encoding='utf-8')
    result = read(tmp_path)
    assert not result['events'] and 'report_unavailable_or_invalid' in codes(result)


@pytest.mark.parametrize('payload', [{'results': 'invalid'}, {'results': [None]},
                                    {'results': [{'events': None}]}])
def test_wrong_report_shapes_are_diagnostics(tmp_path, payload):
    tree(tmp_path)
    write(tmp_path, PREFIX + 'cvm-documents/report.json', payload)
    result = read(tmp_path)
    assert not result['events']
    assert 'report_unavailable_or_invalid' in codes(result)


def test_invalid_amount_and_date_stay_unknown_with_blockers(tmp_path):
    tree(tmp_path, documents=[{**DOC, 'events': [{**ROW, 'gross_per_unit_decimal': '-2',
                                                'payment_date': '2026-02-30'}]}])
    event, = read(tmp_path)['events']
    assert event['gross_per_unit_decimal'] is event['payment_date'] is None
    assert 'invalid_gross_per_unit_decimal' in event['blockers']
    assert 'invalid_payment_date' in event['blockers']


def test_loader_reads_once_per_build_and_cannot_perform_network_or_writes(tmp_path, monkeypatch):
    tree(tmp_path, history=[RI])
    original_read = Path.read_bytes
    reads = []

    def counted(path):
        reads.append(path)
        return original_read(path)

    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected I/O')

    monkeypatch.setattr(Path, 'read_bytes', counted)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    loader = PrimaryCashBridge(tmp_path)
    assert len(reads) == 3
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.setattr(Path, 'glob', forbidden)
    before = deepcopy(STOCK)
    first = loader.primary_events(STOCK, '2026-10-24')
    for index in range(468):
        loader.primary_events({**STOCK, 'ticker': f'TEST{index}', 'cvm': index + 1000}, '2026-10-24')
    assert first == primary_events(tmp_path, STOCK, '2026-10-24', loader=loader)
    assert STOCK == before
    first['events'][0]['evidence'][0]['raw_event']['payment_date'] = '1900-01-01'
    assert first != loader.primary_events(STOCK, '2026-10-24')


def test_refresh_and_new_instances_rehash_changed_reports(tmp_path):
    tree(tmp_path)
    loader = PrimaryCashBridge(tmp_path)
    before = loader.primary_events(STOCK, '2026-10-24')
    write(tmp_path, PREFIX + 'cvm-documents/report.json', {'results': []})
    assert loader.primary_events(STOCK, '2026-10-24') == before
    assert not PrimaryCashBridge(tmp_path).primary_events(STOCK, '2026-10-24')['events']
    assert loader.refresh() is loader
    after = loader.primary_events(STOCK, '2026-10-24')
    assert not after['events'] and after['sources'] != before['sources']


@pytest.mark.parametrize('cutoff', ['2026-02-30', '2026-10', None, 20261024])
def test_invalid_cutoff_is_rejected(tmp_path, cutoff):
    tree(tmp_path)
    with pytest.raises(ValueError, match='cutoff'):
        read(tmp_path, cutoff=cutoff)


def test_loader_cannot_be_reused_for_a_different_folder(tmp_path):
    tree(tmp_path)
    with pytest.raises(ValueError, match='loader_folder_mismatch'):
        primary_events(tmp_path / 'other', STOCK, '2026-10-24', loader=PrimaryCashBridge(tmp_path))
