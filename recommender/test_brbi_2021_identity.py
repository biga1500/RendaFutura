"""Offline independent proof of BRBI11's contemporaneous 2021 IPO identity."""

import builtins
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest
from pypdf import PdfReader

from . import brbi_2021_identity as research


ROOT = Path(__file__).resolve().parent / 'data/market'
PRICING_SHA = 'a1dbefc80d09ed446a8f460ff722c574f11508a1f2851dd36dab6c1e5355f9dc'
IDENTITY = {'ticker': 'BRBI11', 'cvm': 25860, 'cnpj': '10.739.356/0001-03',
            'isin': 'BRBRBICDAM10', 'components': {'ON': 1, 'PN': 2}}


@pytest.fixture(scope='module', autouse=True)
def offline_readonly():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('BRBI audit tests cannot access the network')

        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache must remain read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def report():
    return research.audit(ROOT)


@pytest.fixture(scope='module')
def pricing():
    content = (ROOT / research.PRICING_FILE).read_bytes()
    assert hashlib.sha256(content).hexdigest() == PRICING_SHA
    record, _ = research.pricing_index_record(ROOT)
    return [page.extract_text() for page in PdfReader(io.BytesIO(content)).pages], record


def test_original_2021_document_binds_composition_identity_and_publication(report):
    assert report['identity'] == IDENTITY
    assert report['status'] == 'primary_2021_unit_identity_evidence'
    assert report['document_date'] == report['published_at'] == '2021-06-17'
    assert date.fromisoformat(report['published_at']) < date(2021, 9, 30)
    assert report['publication_basis'] == 'exact_hashed_CVM_IPE_2021_protocol_record'
    assert report['IPE_record']['Protocolo_Entrega'] == '025860IPE170620210104436414-56'
    assert report['IPE_record']['Data_Entrega'] == '2021-06-17'
    assert report['pdf_pages'] == {'identity_and_composition': 1, 'indicative_timetable': 7, 'signature': 10}
    primary = next(s for s in report['sources'] if s['sha256'] == PRICING_SHA)
    assert primary['authority'] == 'primary' and primary['bytes'] == 1493160
    assert primary['retrieved_at'].startswith('2026-10-25')
    assert report['published_at'] != primary['retrieved_at'][:10]
    assert all(hashlib.sha256((ROOT / s['file']).read_bytes()).hexdigest() == s['sha256']
               for s in report['sources'])
    json.dumps(report, allow_nan=False)


def test_pre_IPO_FCA_absence_is_not_five_year_history_failure(report):
    observation = report['pre_IPO_FCA_2021']
    master = observation['master']
    assert (master['ID_DOC'], master['VERSAO'], master['DT_RECEB']) == ('104100', '2', '2021-05-13')
    assert master['DT_RECEB'] < report['published_at']
    child, = observation['security_rows']
    assert research._text(child['Valor_Mobiliario']) == 'valor mobiliario nao registrado'
    assert child['Codigo_Negociacao'] == child['Composicao_BDR_Unit'] == ''
    assert observation['missing_annual_unit_row_proves_insufficient_five_year_history'] is False
    assert report['annual_FCA_gap_resolved_documentarily'] is True
    assert report['five_year_history_insufficient'] is None
    assert report['five_year_price_and_cash_history_verified'] is False
    assert report['five_year_unchanged_composition_continuity_independently_verified'] is False


def test_announced_schedule_is_preserved_without_claiming_execution_or_admission(report):
    assert report['scheduled_first_trade'] == '2021-06-21'
    assert report['scheduled_settlement'] == '2021-06-22'
    assert report['schedule_is_indicative'] is True
    for field in ('actual_first_trade_independently_verified', 'bank_settlement_verified',
                  'point_in_time_archive_verified', 'numerical_features_manufactured',
                  'model_admitted', 'production_integration', 'unit_candidates_modified'):
        assert report[field] is False
    assert report['admissions'] == []


def test_user_supplied_later_PDF_is_ITR_and_2022_composition_is_not_borrowed(report):
    later = report['user_supplied_later_document']
    assert later['actual_document_type'] == 'ITR_2022_03_31'
    assert later['2021_IPO_pricing_date_reported'] == '2021-06-17'
    assert later['composition_paragraph_relates_to_2022_follow_on'] is True
    assert later['used_as_2021_composition_authority'] is False


@pytest.mark.parametrize(('page', 'old', 'new'), [
    (0, 'br advisory partners participacoes s.a.', 'other company s.a.'),
    (0, '10.739.356/0001-03', '10.739.356/0001-04'), (0, '25860', '25861'),
    (0, 'brbrbicdam10', 'brbrbicdam11'), (0, 'brbi11', 'brbi12'),
    (0, 'cada um de 1 (uma)', 'cada um de 2 (uma)'),
    (0, 'e 2 (duas) acoes preferenciais', 'e 3 (duas) acoes preferenciais'),
    (0, 'em 21 de junho de 2021', 'em 21 de outubro de 2021'),
    (6, '21/06/2021', '21/10/2021'), (6, 'meramente indicativas', 'definitivas'),
    (9, '17 de junho de 2021', '17 de junho de 2022')])
def test_wrong_issuer_security_ratio_or_dated_evidence_fails_closed(pricing, page, old, new):
    pages, record = deepcopy(pricing)
    pages[page] = research._text(pages[page])
    assert old in pages[page]
    pages[page] = pages[page].replace(old, new)
    with pytest.raises(ValueError):
        research.parse_pricing(pages, record)


@pytest.mark.parametrize(('field', 'value'), [
    ('Codigo_CVM', '25861'), ('CNPJ_Companhia', '00.000.000/0001-00'),
    ('Protocolo_Entrega', 'other'), ('Versao', '2'), ('Link_Download', 'https://other.example/annual.pdf'),
    ('Data_Referencia', '2020-06-17'), ('Data_Entrega', '2021-10-01')])
def test_exact_CVM_metadata_cannot_be_replaced_with_another_filing(pricing, field, value):
    pages, record = deepcopy(pricing)
    record[field] = value
    with pytest.raises(ValueError):
        research.parse_pricing(pages, record)


def test_missing_pages_and_conflicting_composition_are_rejected(pricing):
    pages, record = deepcopy(pricing)
    with pytest.raises(ValueError, match='brbi_unreviewed_pricing_document'):
        research.parse_pricing(pages[:-1], record)
    pages[0] += '\ncada um de 2 (uma) acao ordinaria e 3 (duas) acoes preferenciais'
    with pytest.raises(ValueError, match='brbi_printed_2021_composition_changed'):
        research.parse_pricing(pages, record)


@pytest.mark.parametrize('relative', [research.PRICING_FILE, research.IPE_FILE, research.FCA_FILE,
    f'{research.BASE}/body-{research.ITR_SHA}.bin'])
def test_changed_original_bytes_are_rejected_without_repinning_hashes(monkeypatch, relative):
    target = ROOT / relative
    original_read = Path.read_bytes

    def changed(path):
        content = original_read(path)
        return content + b'changed' if path == target else content

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='^brbi_evidence_changed$'):
        research.audit(ROOT)


@pytest.mark.parametrize(('field', 'value'), [
    ('url', 'https://other.example/annual.pdf'), ('file', '../outside.pdf'),
    ('sha256', '0' * 64), ('bytes', 1), ('status', 403), ('error', 'access_stop_no_retry'),
    ('scope', 'other'), ('retries', 1)])
def test_transport_receipt_cannot_substitute_identity_or_success(monkeypatch, field, value):
    receipt = ROOT / research.BASE / f'response-{research._sha(research.PRICING_URL.encode())}.json'
    original_read = Path.read_bytes
    changed = json.loads(original_read(receipt))
    changed[field] = value

    def read(path):
        return json.dumps(changed).encode() if path == receipt else original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(ValueError, match='^brbi_original_transport_proof_changed$'):
        research.audit(ROOT)


@pytest.mark.parametrize('cutoff', [date(2021, 9, 30), date(2026, 8, 31), '2026-09-30'])
def test_unreviewed_cutoff_is_rejected_before_any_source_read(monkeypatch, cutoff):
    monkeypatch.setattr(research, '_local', lambda *args: pytest.fail('cutoff must fail first'))
    with pytest.raises(ValueError, match='^brbi_unreviewed_cutoff$'):
        research.audit(ROOT, cutoff)


def test_collection_is_confined_to_brbi_scope_and_the_two_discovered_primary_URLs(monkeypatch):
    calls = []

    class FakeCollector:
        def __init__(self, root, scope):
            assert root == ROOT and scope == 'brbi'

        def fetch(self, url, discovery):
            calls.append((url, discovery))
            return {'url': url}

    monkeypatch.setattr(research, 'PublicSourceCollector', FakeCollector)
    receipts = research.collect(ROOT)
    assert [r['url'] for r in receipts] == [research.PRICING_URL, research.ITR_URL]
    assert calls[0][1]['protocol'] == '025860IPE170620210104436414-56'


def test_report_is_content_addressed_idempotent_and_does_not_create_a_pointer(tmp_path, report):
    first = research.write_report(tmp_path, report)
    assert research.write_report(tmp_path, report) == first
    content = first.read_bytes()
    assert first.name == f'identity-report-{hashlib.sha256(content).hexdigest()}.json'
    assert json.loads(content) == report
    assert list((tmp_path / research.BASE).iterdir()) == [first]
