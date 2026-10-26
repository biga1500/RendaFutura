"""Offline identity, provenance, temporal-selection, and fail-closed checks."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date
import csv
import io
import json
import socket
import urllib.request
import zipfile

import pytest

from . import round2_security_identity as subject
from .bulk_pending_sources import approved


@pytest.fixture(scope='module')
def adapter():
    if not (subject.ROOT / subject.FCA_MANIFEST).is_file():
        pytest.skip('native cached CVM archives are not distributed with the repository')
    return subject.PrimarySecurityIdentityAdapter(subject.ROOT)


@pytest.fixture(scope='module')
def identities(adapter):
    return {ticker: adapter.evidence(ticker) for ticker in subject.TARGETS}


def filing(version, received, children, *, reference='2021-01-01', identifier=None):
    return {'master': {'CD_CVM': '021610', 'DT_REFER': reference, 'VERSAO': str(version),
                       'DT_RECEB': received, 'ID_DOC': identifier or str(version)},
            'children': children}


def test_received_master_selected_before_inspecting_available_children():
    rows = [filing(1, '2021-01-10', [{'Codigo_Negociacao': 'B3SA3'}]),
            filing(2, '2021-09-23', []),
            filing(3, '2022-03-07', [{'Codigo_Negociacao': 'B3SA3'}])]
    chosen = subject.select_fca(rows, 'B3SA3', date(2021, 9, 30))
    assert chosen['filing']['master']['VERSAO'] == '2'
    assert chosen['status'] == 'selected_filing_child_missing'
    assert not chosen['exact']
    assert subject.select_fca(rows, 'B3SA3', '2022-03-07')['filing']['master']['VERSAO'] == '3'


def test_future_reference_date_is_not_current_identity():
    rows = [filing(1, '2021-01-10', [{'Codigo_Negociacao': 'B3SA3'}]),
            filing(1, '2021-09-20', [], reference='2022-01-01', identifier='future')]
    assert subject.select_fca(rows, 'B3SA3', '2021-09-30')['filing']['master']['DT_REFER'] == '2021-01-01'


def test_received_master_tie_is_rejected():
    with pytest.raises(subject.IdentityPending, match='ambiguous_received_FCA_master'):
        subject.select_fca([filing(1, '2021-01-10', [], identifier='a'),
                            filing(1, '2021-01-10', [], identifier='b')], 'B3SA3', '2021-09-30')


def fca_zip(*, bad_target=False, unrelated=False):
    master = dict.fromkeys(subject.MASTER_FIELDS, '')
    master.update(CNPJ_CIA='09.346.601/0001-25', DT_REFER='2021-01-01', VERSAO='1',
                  CD_CVM='021610', ID_DOC='123', DT_RECEB='2021-05-31')
    child = dict.fromkeys(subject.SECURITY_FIELDS, '')
    child.update(CNPJ_Companhia=master['CNPJ_CIA'], Data_Referencia=master['DT_REFER'],
                 Versao='2' if bad_target else '1', ID_Documento='123', Codigo_Negociacao='B3SA3')
    children = [child]
    if unrelated:
        children.append({**child, 'CNPJ_Companhia': '00.000.000/0001-00', 'ID_Documento': '999'})
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        for member, fields, rows in (
            ('fca_cia_aberta_2021.csv', subject.MASTER_FIELDS, [master]),
            ('fca_cia_aberta_valor_mobiliario_2021.csv', subject.SECURITY_FIELDS, children)):
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=fields, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(member, text.getvalue().encode('latin1'))
    return body.getvalue()


def test_scoped_FCA_parser_still_rejects_target_child_version_conflict():
    with pytest.raises(subject.IdentityPending, match='target_FCA_child_identity_conflict'):
        subject.parse_target_fca(fca_zip(bad_target=True), 2021)


def test_unrelated_child_does_not_change_disjoint_target_evidence():
    masters, children = subject.parse_target_fca(fca_zip(unrelated=True), 2021)
    assert len(masters) == len(children) == 1
    assert children[0]['Codigo_Negociacao'] == 'B3SA3'


@pytest.mark.parametrize(('ticker', 'cvm', 'cnpj', 'isin'), [
    ('B3SA3', 21610, '09.346.601/0001-25', 'BRB3SAACNOR6'),
    ('EQMA3B', 16608, '06.272.793/0001-84', 'BREQMAACNOR8'),
])
def test_native_identity_is_typed_and_not_B3_payload(identities, ticker, cvm, cnpj, isin):
    item = identities[ticker]
    assert isinstance(item, subject.SecurityIdentityEvidence)
    assert (item.cvm, item.cnpj, item.share_class, item.isin) == (cvm, cnpj, 'ON', isin)
    assert item.aliases == ()
    assert item.effective_code_change_date is None
    assert not item.model_admission_granted and not item.production_integration
    assert not item.continuous_identity_window_verified
    assert not {'otherCodes', 'codeCVM', 'stockDividends'} & item.to_dict().keys()
    with pytest.raises(FrozenInstanceError):
        item.isin = 'invented'


def test_native_EQMA_binding_is_unique_decimal_join_not_ISIN_ending(identities):
    item = identities['EQMA3B']
    observation = item.observations[0]
    assert observation.observed_for_date == '2026-04-28'
    assert observation.available_on == '2026-06-18'
    assert observation.valid_from == observation.valid_until == '2026-04-28'
    proof = observation.literal_proof[-1]
    assert proof['ordinary_class_literal']['decimal'] == '0.82681917278'
    assert proof['ordinary_class_literal']['share_class'] == 'ON'
    assert proof['ISIN_row']['literal_isin'] == 'BREQMAAC\nNOR8'
    assert proof['ISIN_row']['locator'] == 'pdf_page=1:table_index=1:row_index=2'
    assert observation.literal_proof[1]['FCA']['master']['ID_DOC'] == '147208'
    assert observation.literal_proof[1]['FCA']['master']['DT_RECEB'] == '2025-05-09'


def test_B3_is_documented_sole_equity_join_and_native_protocol(identities):
    observation = identities['B3SA3'].observations[0]
    assert observation.observed_for_date == observation.available_on == '2026-09-22'
    assert observation.form_version == 3
    assert observation.literal_proof[-1]['join_is_documentary_inference'] is True
    assert observation.literal_proof[0]['native_form']['protocol_provento'] == '1570486'
    assert observation.literal_proof[0]['native_form']['rows'][0]['literal_isin'] == 'BRB3SAACN\nOR6'


@pytest.mark.parametrize('ticker', ('B3SA3', 'EQMA3B'))
def test_whole_five_year_window_is_not_granted(identities, ticker):
    with pytest.raises(subject.IdentityPending, match='whole_window_validity_not_documented'):
        identities[ticker].require_window(subject.WINDOW_START, subject.CUTOFF)


def test_snapshot_does_not_extend_one_day_into_future(identities):
    item = identities['B3SA3']
    assert item.require_window('2026-09-22', '2026-09-22') is item
    with pytest.raises(subject.IdentityPending, match='whole_window_validity_not_documented'):
        item.require_window('2026-09-22', '2026-09-23')


def test_earlier_availability_cutoff_rejects_later_publication(identities):
    with pytest.raises(subject.IdentityPending, match='whole_window_validity_not_documented'):
        identities['B3SA3'].require_window('2026-09-22', '2026-09-22', available_as_of='2026-09-21')


def test_retrospective_EQMA_form_cannot_supply_April_point_in_time_identity(identities):
    with pytest.raises(subject.IdentityPending, match='whole_window_validity_not_documented'):
        identities['EQMA3B'].require_window('2026-04-28', '2026-04-28', available_as_of='2026-09-30')


@pytest.mark.parametrize('ticker', ('B3SA3', 'EQMA3B'))
def test_missing_2021_superseded_child_is_visible(identities, ticker):
    observation = identities[ticker].fca_observations[0]
    assert observation['status'] == 'selected_filing_child_missing'
    assert observation['reported_securities'] == []
    assert 'FCA_selected_filing_child_missing' in identities[ticker].window_blockers


def test_ENMA_is_not_promoted_to_alias_or_1997_code_start(identities):
    item = identities['EQMA3B']
    year_2023 = item.fca_observations[2]
    assert year_2023['status'] == 'exact_code_not_reported'
    assert year_2023['reported_securities'][0]['row']['Codigo_Negociacao'] == 'ENMA3B'
    assert item.fca_observations[3]['reported_securities'][0]['row']['Codigo_Negociacao'] == 'EQMA3B'
    assert item.fca_observations[3]['listing_start_is_not_exact_code_effective_date']
    assert not item.aliases and item.effective_code_change_date is None


@pytest.mark.parametrize('identity', [
    {'ticker': 'B3SA3', 'cvm': 16608, 'share_class': 'ON'},
    {'ticker': 'B3SA3', 'cvm': 21610, 'share_class': 'PN'},
    {'ticker': 'B3SA3', 'cvm': 21610, 'share_class': 'ON', 'isin': 'BREQMAACNOR8'},
    {'ticker': 'B3SA3F', 'cvm': 21610, 'share_class': 'ON'},
    {'ticker': 'B3SA3', 'cvm': 21610, 'share_class': 'ON', 'cnpj': '06.272.793/0001-84'},
])
def test_caller_conflicts_are_rejected(adapter, identity):
    with pytest.raises(subject.IdentityPending, match='caller_exact_identity_disagrees'):
        adapter.evidence('B3SA3', identity=identity)


@pytest.mark.parametrize('ticker', ('B3SA3F', 'EQMA3BF', 'ENMA3B', 'EQTL3'))
def test_no_suffix_or_related_ticker_resolution(adapter, ticker):
    with pytest.raises(subject.IdentityPending, match='outside_disjoint_target_scope'):
        adapter.evidence(ticker)


def test_document_received_after_cutoff_is_unavailable(adapter):
    with pytest.raises(subject.IdentityPending, match='not_received_by_cutoff'):
        adapter.evidence('B3SA3', date(2026, 9, 21))
    with pytest.raises(subject.IdentityPending, match='not_received_by_cutoff'):
        adapter.evidence('EQMA3B', date(2026, 6, 17))


def test_evidence_nested_records_do_not_mutate_adapter(adapter):
    item = adapter.evidence('B3SA3')
    item.fca_observations[0]['master']['DT_RECEB'] = '1900-01-01'
    assert adapter.evidence('B3SA3').fca_observations[0]['master']['DT_RECEB'] == '2021-09-23'


def test_form_version_and_CVM_are_read_from_native_cells(adapter):
    body, indexed, _ = adapter._document('B3SA3', 'form', subject.CUTOFF)
    wrong_version = deepcopy(indexed)
    wrong_version['record']['Versao'] = '2'
    with pytest.raises(subject.IdentityPending, match='native_form_version_conflict'):
        subject.parse_native_form(body, wrong_version)
    wrong_cvm = deepcopy(indexed)
    wrong_cvm['record']['Codigo_CVM'] = '16608'
    with pytest.raises(subject.IdentityPending, match='native_form_CVM_protocol_conflict'):
        subject.parse_native_form(body, wrong_cvm)


def test_unique_join_rejects_equal_amounts_and_changed_dates(adapter):
    body, indexed, _ = adapter._document('EQMA3B', 'form', subject.CUTOFF)
    form = subject.parse_native_form(body, indexed)
    notice_body, notice_index, _ = adapter._document('EQMA3B', 'notice', subject.CUTOFF)
    pages = subject._pdf_pages(notice_body)
    equal = [p.replace('1,66749715208', '0,82681917278') for p in pages]
    with pytest.raises(subject.IdentityPending, match='ordinary_ISIN_amount_join_ambiguous'):
        subject.bind_eqma_class(form, equal, notice_index)
    rows = (form.rows[0], replace(form.rows[1], gross_per_unit_decimal=form.rows[0].gross_per_unit_decimal), form.rows[2])
    with pytest.raises(subject.IdentityPending, match='ordinary_ISIN_amount_join_ambiguous'):
        subject.bind_eqma_class(replace(form, rows=rows), pages, notice_index)
    with pytest.raises(subject.IdentityPending, match='notice_record_date_conflict'):
        subject.bind_eqma_class(replace(form, record_date='2026-04-29'), pages, notice_index)


def test_native_decimal_join_does_not_need_a_class_shaped_ISIN(adapter):
    body, indexed, _ = adapter._document('EQMA3B', 'form', subject.CUTOFF)
    form = subject.parse_native_form(body, indexed)
    notice_body, notice_index, _ = adapter._document('EQMA3B', 'notice', subject.CUTOFF)
    rows = (replace(form.rows[0], isin='BRZZZZAAAAA1'), *form.rows[1:])
    chosen, ordinary, _ = subject.bind_eqma_class(replace(form, rows=rows), subject._pdf_pages(notice_body), notice_index)
    assert chosen.isin == 'BRZZZZAAAAA1' and ordinary['share_class'] == 'ON'


@pytest.mark.parametrize('relative', ('../secret', 'raw/../../secret', 'raw\\file', 'raw/C:/file', 'raw//file'))
def test_unsafe_source_paths_are_rejected(tmp_path, relative):
    with pytest.raises(subject.IdentityPending, match='unsafe_evidence_path'):
        subject._Reader(tmp_path).read(relative)


def test_original_hash_and_manifest_tampering_are_rejected(tmp_path):
    target = tmp_path / subject.INDEX_MANIFEST
    target.parent.mkdir(parents=True)
    target.write_bytes(b'[]')
    with pytest.raises(subject.IdentityPending, match='original_hash_changed'):
        subject._index(subject._Reader(tmp_path))


def test_public_receipt_conflict_is_rejected(adapter, tmp_path):
    ticker = 'EQMA3B'
    sequence, _ = subject.REVIEWED[ticker]['form']
    indexed = next(r for r in adapter.index if r['sequence'] == sequence)
    receipt_name = f'{subject.BASE}/response-{subject.sha(indexed["record"]["Link_Download"].encode())}.json'
    destination = tmp_path / receipt_name
    destination.parent.mkdir(parents=True)
    receipt = json.loads((subject.ROOT / receipt_name).read_bytes())
    receipt['url'] = 'https://www.b3.com.br/invented'
    destination.write_text(json.dumps(receipt), encoding='utf-8')
    isolated = object.__new__(subject.PrimarySecurityIdentityAdapter)
    isolated.reader, isolated.index = subject._Reader(tmp_path), adapter.index
    with pytest.raises(subject.IdentityPending, match='public_HTTP_receipt_conflict'):
        isolated._document(ticker, 'form', subject.CUTOFF)


@pytest.mark.parametrize('origin', (
    'www.b3.com.br', 'sistemaswebb3-listados.b3.com.br', 'ri.b3.com.br',
    'bvmf.com.br', 'www.bovespa.com.br', 'bmfbovespa.com.br',
    'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br',
    'www.engie.com.br', 'cdn-sites-assets.mziq.com',
))
def test_collector_denies_all_user_named_origins(origin):
    assert not approved('https://' + origin + '/document.pdf')


def test_collection_only_uses_exact_published_CVM_index_link(adapter, monkeypatch):
    calls = []
    class Collector:
        def __init__(self, root, scope):
            assert scope == 'round2-identity'
        def fetch(self, url, discovery):
            assert urlsplit_host(url) == 'www.rad.cvm.gov.br'
            calls.append((url, discovery))
            return {'url': url}
    monkeypatch.setattr(subject, '_index', lambda reader: adapter.index)
    monkeypatch.setattr(subject, 'PublicSourceCollector', Collector)
    result = subject.collect_documents(subject.ROOT, ['1060668'])
    indexed = next(r for r in adapter.index if r['sequence'] == '1060668')
    assert result == [{'url': indexed['record']['Link_Download']}]
    assert calls[0][1]['native_record'] == indexed['record']
    with pytest.raises(subject.IdentityPending, match='not_unique_in_native_index'):
        subject.collect_documents(subject.ROOT, ['https://www.b3.com.br/'])
    assert len(calls) == 1


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


def test_offline_audit_does_not_network_write_or_approve(adapter, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline audit attempted network or source collection')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(urllib.request, 'build_opener', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subject, 'PublicSourceCollector', forbidden)
    owned = subject.ROOT / subject.BASE
    before = {p.name: subject.sha(p.read_bytes()) for p in owned.iterdir() if p.is_file()}
    report = subject.audit(subject.ROOT)
    after = {p.name: subject.sha(p.read_bytes()) for p in owned.iterdir() if p.is_file()}
    assert before == after
    assert report['model_admissions'] == [] and not report['integration_performed']
    assert report['B3_network_requests'] == report['denied_origin_requests'] == 0
    for ticker in subject.TARGETS:
        raw = report['raw_file_audit'][ticker]
        assert raw['daily_prices']['model_window_observed_month_count'] == 61
        assert raw['daily_prices']['existing_price_parser']['status'] == 'passed_existing_61_month_anchor_parser'
        assert not raw['daily_prices']['native_unadjusted_regime_verified']
        assert raw['daily_prices']['source']['retrieved_at'] is not None
        assert raw['payment_bundle']['actual_distribution_count'] is None
        assert raw['payment_bundle']['cached_event_row_count'] == 0
        assert not raw['independent_cash_pages'] and not raw['independent_price_anchors']
        assert raw['expected_next_current_bridge_blocker_after_parent_identity_integration'] == 'independent_cash_cache_missing'
    assert all(not r['used_to_fill_identity_window'] for r in report['earlier_native_form_observations'])


def test_primary_receipts_keep_retrieval_separate_from_publication(identities):
    for item in identities.values():
        original = item.observations[0].literal_proof[0]
        assert original['source']['retrieved_at'].startswith('2026-10-')
        assert original['native_form']['received_on'] < original['source']['retrieved_at'][:10]
        assert original['source']['sha256']
        assert 'csv_record=' in original['index_locator']
        assert not item.point_in_time_archive_verified
