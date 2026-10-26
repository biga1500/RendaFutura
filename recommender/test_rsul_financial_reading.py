"""Independent RSUL contracts: real evidence is read-only, mutations are local."""

import builtins
from copy import deepcopy
import csv
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import urllib.request
import zipfile

import pytest
from pypdf import PdfReader

from . import inventory_financials as native
from . import preferred_class_binding as binding
from . import rsul_financial_reading as rsul
from .collect_market_data import financial_features


ROOT = Path(__file__).resolve().parent / 'data/market'
CUTOFF = date(2026, 9, 30)
SPEC = (13439, 'PNA', 5)
CONTAINER_SHA = '4932b8831f81f6a3673e3f59ba9ca3184dc74ecb46d3bc3a8ab7b861ac7d54f3'
PDF_SHA = 'abadd5f0c796525351893ecbaaf4a7960071b4a8b08b00e0340e81e160773134'
ARCHIVE_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
DRE_SHA = 'a86470a0b08a5baa09853ddf29cfda2f97ece3f09aca7b291a51e8b277bbdffa'
BASE = 'raw/primary-research/all-pending/parent'
URL = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
       '?CodigoInstituicao=1&NumeroSequencialDocumento=155874')
CONTAINER = f'{BASE}/body-{CONTAINER_SHA}.bin'
PDF = f'{BASE}/native-RSUL4-{PDF_SHA}.pdf'
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
RECEIPT = f'{BASE}/response-{hashlib.sha256(URL.encode()).hexdigest()}.json'
REVIEW = f'{BASE}/rsul-profit-root-review.json'
MASTER = 'dfp_cia_aberta_2025.csv'
DRE = 'dfp_cia_aberta_DRE_con_2025.csv'


@pytest.fixture(scope='module', autouse=True)
def offline_readonly():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('RSUL verification must remain offline')

        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def originals():
    return {name: (ROOT / name).read_bytes()
            for name in (CONTAINER, PDF, ANNUAL, RECEIPT, REVIEW)}


@pytest.fixture(scope='module')
def verified():
    # Both strict production loaders run against the untouched real cache.
    reading, sources = rsul.get_rsul_reading(ROOT, CUTOFF)
    proof = binding.class_binding(ROOT, {
        'ticker': 'RSUL4', 'cvm': 13439, 'share_class': 'PNA', 'isin': 'BRRSULACNPR3'}, CUTOFF)
    return reading, sources, proof


@pytest.fixture(scope='module')
def real_tables(originals):
    tables = {}
    with zipfile.ZipFile(io.BytesIO(originals[ANNUAL])) as archive:
        names = [MASTER, *(f'dfp_cia_aberta{suffix}_2025.csv'
                          for suffix in (*native.TABLES, *native.PRESENCE_ONLY))]
        for name in names:
            if name in archive.namelist():
                rows = [r for r in native._rows(archive, name) if int(r['CD_CVM']) == 13439]
                if rows:
                    tables[name] = rows
    return tables


def _pack(tables):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, stream.getvalue().encode('latin1'))
    return output.getvalue()


@pytest.fixture
def replay(monkeypatch, originals):
    """Inject bytes in memory; retain every production hash and identity check."""
    bodies = dict(originals)
    read_bytes = Path.read_bytes

    def read(path):
        if path.is_relative_to(ROOT):
            relative = path.relative_to(ROOT).as_posix()
            if relative in bodies:
                return bodies[relative]
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', read)
    return bodies


@pytest.fixture
def projected(tmp_path, monkeypatch, real_tables, verified, originals):
    tables = deepcopy(real_tables)
    reading, sources, proof = verified
    marker = tmp_path / REVIEW
    marker.parent.mkdir(parents=True)
    marker.write_bytes(originals[REVIEW])

    def pinned_reading(folder, cutoff):
        assert Path(folder) == tmp_path
        if cutoff != CUTOFF:
            raise ValueError('rsul_unreviewed_cutoff')
        return deepcopy((reading, sources))

    def pinned_binding(folder, identity, cutoff):
        assert Path(folder) == tmp_path
        assert identity == proof['identity'] and cutoff == CUTOFF
        return deepcopy(proof)

    # Only the projection substitutes loaders. The real positive uses both originals.
    monkeypatch.setattr(rsul, 'get_rsul_reading', pinned_reading)
    monkeypatch.setattr(binding, 'class_binding', pinned_binding)

    def publish():
        path = tmp_path / ANNUAL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_pack(tables))

    publish()
    return tmp_path, tables, publish


def _prepare(projected, specs=None):
    return native.prepare_financials(projected[0], specs or {'RSUL4': SPEC}, CUTOFF)


def test_real_native_hashes_members_master_and_full_DRE(originals, real_tables, verified):
    for name, digest in ((CONTAINER, CONTAINER_SHA), (PDF, PDF_SHA), (ANNUAL, ARCHIVE_SHA)):
        assert hashlib.sha256(originals[name]).hexdigest() == digest
    with zipfile.ZipFile(io.BytesIO(originals[CONTAINER])) as archive:
        assert archive.read('155874_013439_25102026195251.pdf') == originals[PDF]
        assert '013439DFP31-12-2025v1.xml' in archive.namelist()
    master, = real_tables[MASTER]
    assert {key: master[key] for key in ('CD_CVM', 'CNPJ_CIA', 'DT_REFER', 'VERSAO', 'ID_DOC')} == {
        'CD_CVM': '013439', 'CNPJ_CIA': '85.778.074/0001-06', 'DT_REFER': '2025-12-31',
        'VERSAO': '1', 'ID_DOC': '155874'}
    assert master['DT_RECEB'][:10] == '2026-03-31'
    rows = [r for r in real_tables[DRE] if r['ORDEM_EXERC'] == '\u00daLTIMO']
    assert native._dre_fingerprint(rows, full=True) == DRE_SHA
    by_code = {r['CD_CONTA']: r for r in rows}
    assert Decimal(by_code['3.11']['VL_CONTA']) * 1000 == Decimal('64448000')
    assert all(Decimal(by_code[code]['VL_CONTA']) == 0 for code in ('3.11.01', '3.11.02'))
    assert by_code['3.99.01.02']['DS_CONTA'] == 'PN'
    assert Decimal(by_code['3.99.01.02']['VL_CONTA']) == Decimal('11.67921')
    reading, sources, _ = verified
    assert (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'],
            reading['accounts_scope'], reading['profit_account'], reading['parent_profit_brl']) == (
                13439, '2025-12-31', '1', '155874', 'consolidated', '3.11', '64448000')
    assert reading['full_dre'] is True and reading['dre_sha256'] == DRE_SHA
    assert reading['manifest_sha256'] == hashlib.sha256(originals[REVIEW]).hexdigest()
    source, = sources
    assert reading['primary_evidence']['source_ref'] == source['id'] == 'primary-financials:' + PDF_SHA
    assert source['url'] == URL and source['file'] == PDF and source['sha256'] == PDF_SHA
    assert source['bytes'] == len(originals[PDF])
    assert source['original_container'] == CONTAINER and source['original_container_sha256'] == CONTAINER_SHA
    assert source['retrieved_at'] == json.loads(originals[RECEIPT])['retrieved_at']
    assert source['published_at'] == '2026-03-31' and source['point_in_time_archive_verified'] is False
    assert 'basic_eps' not in reading and 'weighted_shares' not in reading


def test_actual_pdf_note26_and_consolidated_DRE_report_the_same_profit_and_native_EPS(originals):
    reader = PdfReader(io.BytesIO(originals[PDF]))
    note = ' '.join(reader.pages[58].extract_text().split())
    dre = ' '.join(reader.pages[19].extract_text().split())
    assert re.search(r'26\. LUCRO \(PREJU.ZO\) POR A..O', note)
    assert 'Controladora Consolidado 31/12/2025 31/12/2024 31/12/2025 31/12/2024' in note
    assert re.search(r'Resultado atribu.vel aos acionistas da Companhia 64\.448 58\.562 64\.448 58\.732', note)
    assert '3.576.903' in note and '2.495.225' in note
    assert dre.startswith('DFs Consolidadas / Demonstra') and '(Reais Mil)' in dre
    assert re.search(r'3\.11 Lucro/Preju.zo Consolidado do Per.odo 64\.448', dre)
    assert '3.99.01.02 PN 11,67921' in dre
    assert '3.11.01' not in dre and '3.11.02' not in dre


def test_real_FCA_binding_is_exact_PNA_without_computing_EPS(verified):
    _, _, proof = verified
    assert proof['identity'] == {'ticker': 'RSUL4', 'cvm': 13439,
                                 'share_class': 'PNA', 'isin': 'BRRSULACNPR3'}
    assert proof['registered_class'] == 'PNA' and proof['financial_source_class'] == 'PN'
    assert [r['year'] for r in proof['historical_registry']] == list(range(2021, 2027))
    assert proof['only_preferred_class_in_each_received_FCA'] is True
    assert proof['EPS_computed'] is False and proof['numeric_values_changed'] is False
    assert proof['original_files_modified'] is False
    for source in proof['sources']:
        assert hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']


def _assert_recovered(result):
    records, diagnostics, sources = result
    features, record = financial_features(records, [], CUTOFF, SPEC)
    assert len(features) == 7 and features[2] == 11.67921
    assert features[3] == pytest.approx(7.5785836984856)
    assert features[5] == pytest.approx(0.39577315086376086)
    assert record['share_class'] == 'PNA' and record['filing_id'] == '155874'
    assert record['fiscal_end'] == '2025-12-31' and record['version'] == '1'
    assert record['published_at'] == '2026-03-31' and record['accounts_scope'] == 'consolidated'
    assert record['document_type'] == 'DFP' and record['period_type'] == 'annual'
    assert record['annualized'] is False and record['status'] == 'included'
    method = record['financial_method']
    assert method['profit_account'] == '3.11' and method['basic_eps_account'] == '3.99.01.02'
    assert method['source_share_class'] == 'PN' and method['registered_share_class'] == 'PNA'
    assert method['documentary_reading']['original_dre_sha256'] == DRE_SHA
    assert method['primary_evidence'][0]['pdf_page'] == 59
    assert method['reported_basic_eps'] == []
    assert all('_con_' in name for name in method['source_statements'])
    assert any(r.get('ticker') == 'RSUL4' and r['status'] == 'included' for r in diagnostics)
    assert any(s['sha256'] == PDF_SHA for s in sources)
    json.dumps(result, allow_nan=False)


def test_real_production_integration_recovers_previous_invalid_features(monkeypatch, originals):
    monkeypatch.setattr(native, '_caches', lambda folder, cutoff: [(('dfp', 2025), ROOT / ANNUAL)])
    with monkeypatch.context() as prior:
        prior.setattr(native, '_research', lambda *args: {})
        baseline = native.prepare_financials(ROOT, {'RSUL4': SPEC}, CUTOFF)
    assert financial_features(baseline[0], [], CUTOFF, SPEC) is None
    assert any('invalid_inferred_shares_or_assets' in r.get('reasons', []) for r in baseline[1])
    _assert_recovered(native.prepare_financials(ROOT, {'RSUL4': SPEC}, CUTOFF))
    assert all((ROOT / name).read_bytes() == body for name, body in originals.items())


def test_projection_matches_the_real_positive(projected):
    _assert_recovered(_prepare(projected))


@pytest.mark.parametrize('name', [CONTAINER, PDF, ANNUAL])
def test_changed_native_bytes_rejected_by_original_hashes(replay, name):
    replay[name] += b'changed'
    with pytest.raises(ValueError, match='^rsul_evidence_changed$'):
        rsul.get_rsul_reading(ROOT, CUTOFF)


@pytest.mark.parametrize(('field', 'value'), [
    ('url', URL.replace('155874', '155875')), ('file', PDF), ('status', 403),
    ('error', 'access_stop_no_retry'), ('sha256', '0' * 64), ('bytes', 1)])
def test_changed_native_transport_proof_rejected(replay, field, value):
    receipt = json.loads(replay[RECEIPT])
    receipt[field] = value
    replay[RECEIPT] = json.dumps(receipt).encode()
    with pytest.raises(ValueError, match='^rsul_original_transport_proof_changed$'):
        rsul.get_rsul_reading(ROOT, CUTOFF)


@pytest.mark.parametrize(('field', 'value'), [
    ('cvm', 13440), ('fiscal_end', '2024-12-31'), ('version', '2'), ('filing_id', '155875'),
    ('published_at', '2026-04-01'), ('accounts_scope', 'individual'), ('profit_account', '3.11.02'),
    ('parent_profit_brl', '64448'), ('full_dre', False), ('dre_sha256', '0' * 64),
    ('pdf_page', 20), ('basic_eps', [{'share_class': 'PN', 'value': '25.82853'}])])
def test_changed_local_review_cannot_authorize_a_reading(replay, field, value):
    review = json.loads(replay[REVIEW])
    review[field] = value
    replay[REVIEW] = json.dumps(review).encode()
    with pytest.raises(ValueError, match='^rsul_local_reading_changed$'):
        rsul.get_rsul_reading(ROOT, CUTOFF)


@pytest.mark.parametrize('cutoff', [date(2026, 3, 30), date(2026, 8, 31), date(2026, 10, 31)])
def test_unreviewed_cutoff_rejected_before_any_file_read(monkeypatch, cutoff):
    monkeypatch.setattr(Path, 'read_bytes', lambda *args: pytest.fail('cutoff must fail first'))
    with pytest.raises(ValueError, match='^rsul_unreviewed_cutoff$'):
        rsul.get_rsul_reading(ROOT, cutoff)


@pytest.mark.parametrize(('field', 'value'), [
    ('CD_CVM', '013440'), ('DT_REFER', '2024-12-31'), ('VERSAO', '2'),
    ('ID_DOC', '155875'), ('DT_RECEB', '2026-04-01'), ('CNPJ_CIA', '00.000.000/0001-00')])
def test_strict_loader_independently_checks_master_identity(monkeypatch, replay, field, value):
    original_rows = native._rows

    def changed_rows(archive, name):
        for row in original_rows(archive, name):
            if name == MASTER and int(row['CD_CVM']) == 13439:
                row = {**row, field: value}
            yield row

    monkeypatch.setattr(native, '_rows', changed_rows)
    with pytest.raises(ValueError, match='^rsul_latest_filing_changed$'):
        rsul.get_rsul_reading(ROOT, CUTOFF)


@pytest.mark.parametrize('spec', [
    (13440, 'PNA', 5), (13439, 'PNB', 5), (13439, 'PNU', 5), (13439, 'PNA', 2)])
def test_research_requires_the_reviewed_issuer_class_and_sector(projected, monkeypatch, spec):
    monkeypatch.setattr(rsul, 'get_rsul_reading', lambda *args: pytest.fail('wrong identity invoked loader'))
    diagnostics, sources = [], []
    assert native._research(projected[0], {'RSUL4': spec}, CUTOFF, diagnostics, sources) == {}
    assert diagnostics == [] and sources == []


@pytest.mark.parametrize('share', ['ON', 'PN', 'PNA'])
def test_same_scope_root_reading_is_independent_of_the_source_class_label(projected, share):
    diagnostics, sources = [], []
    reviews = native._research(projected[0], {'RSUL': (13439, share, 5)}, CUTOFF, diagnostics, sources)
    assert set(reviews) == {(13439, '2025-12-31', '1', '155874')}
    assert reviews[13439, '2025-12-31', '1', '155874']['profit_account'] == '3.11'
    assert diagnostics == [] and len(sources) == 1


def test_PN_to_exact_FCA_PNA_bridge_preserves_all_seven_numeric_features(projected):
    pna = financial_features(_prepare(projected)[0], [], CUTOFF, SPEC)
    pn_spec = (13439, 'PN', 5)
    pn = financial_features(_prepare(projected, {'RSUL4': pn_spec})[0], [], CUTOFF, pn_spec)
    assert pn is not None and pna is not None
    assert pn[0] == pna[0] and pn[0][2] == 11.67921
    assert pn[1]['share_class'] == 'PN' and pna[1]['share_class'] == 'PNA'
    assert pn[1]['financial_method']['basic_eps_account'] == pna[1]['financial_method']['basic_eps_account']


@pytest.mark.parametrize('failure', [OSError('missing'), ValueError('changed'), KeyError('member'),
                                    zipfile.BadZipFile('corrupt')])
def test_research_loader_failure_keeps_previous_invalid_features(projected, monkeypatch, failure):
    def unavailable(*args):
        raise failure

    monkeypatch.setattr(rsul, 'get_rsul_reading', unavailable)
    records, diagnostics, sources = _prepare(projected)
    assert financial_features(records, [], CUTOFF, SPEC) is None
    assert any(r.get('reasons') == ['documentary_readings_unavailable'] for r in diagnostics)
    assert not any(s['sha256'] == PDF_SHA for s in sources)


@pytest.mark.parametrize(('account', 'field', 'value'), [
    ('3.11', 'VL_CONTA', '64449.0000000000'), ('3.11.01', 'VL_CONTA', '1'),
    ('3.11.02', 'VL_CONTA', '1'), ('3.99.01.02', 'VL_CONTA', '0'),
    ('3.99.01.02', 'VL_CONTA', '25.82853'), ('3.99.01.02', 'DS_CONTA', 'PNA'),
    ('3.01', 'VL_CONTA', '420461.0000000000'), ('3.11', 'MOEDA', 'USD'),
    ('3.11', 'ESCALA_MOEDA', 'UNIDADE'), ('3.11', 'DT_INI_EXERC', '2025-07-01')])
def test_full_DRE_mutation_cannot_borrow_the_review_or_manufacture_EPS(
        projected, monkeypatch, account, field, value):
    row = next(r for r in projected[1][DRE]
               if r['ORDEM_EXERC'] == '\u00daLTIMO' and r['CD_CONTA'] == account)
    row[field] = value
    projected[2]()
    records, diagnostics, _ = _prepare(projected)
    assert any('documented_reading_original_cells_mismatch' in r.get('reasons', []) for r in diagnostics)
    assert not any('documented_same_scope_parent_profit_root' in r.get('reasons', []) for r in diagnostics)
    assert not any('documentary_reading' in r['financial_method'] for r in records)
    with monkeypatch.context() as untreated:
        untreated.setattr(native, '_research', lambda *args: {})
        baseline = _prepare(projected)
    assert financial_features(records, [], CUTOFF, SPEC) == financial_features(baseline[0], [], CUTOFF, SPEC)
    if account != '3.11.01':
        assert financial_features(records, [], CUTOFF, SPEC) is None


def test_missing_preferred_EPS_is_not_replaced_from_profit_or_share_counts(projected):
    projected[1][DRE] = [r for r in projected[1][DRE] if r['CD_CONTA'] != '3.99.01.02']
    projected[2]()
    records, diagnostics, _ = _prepare(projected)
    assert financial_features(records, [], CUTOFF, SPEC) is None
    assert all(r['profit_per_share'] is None for r in records)
    assert any('basic_eps_absent_ambiguous_or_zero' in r.get('reasons', []) for r in diagnostics)


@pytest.mark.parametrize(('field', 'value'), [('ID_DOC', '155875'), ('VERSAO', '2'),
    ('DT_RECEB', '2026-04-01'), ('DT_REFER', '2024-12-31')])
def test_other_filing_or_period_does_not_receive_the_root_reading(projected, field, value):
    projected[1][MASTER][0][field] = value
    projected[2]()
    records, diagnostics, _ = _prepare(projected)
    assert financial_features(records, [], CUTOFF, SPEC) is None
    assert not any('documented_same_scope_parent_profit_root' in r.get('reasons', []) for r in diagnostics)


def test_profit_root_proof_cannot_replace_missing_FCA_class_binding(projected, monkeypatch):
    def unavailable(*args):
        raise ValueError('preferred_class_exact_identity_required')

    monkeypatch.setattr(binding, 'class_binding', unavailable)
    records, diagnostics, _ = _prepare(projected)
    assert financial_features(records, [], CUTOFF, SPEC) is None
    assert any(r.get('kind') == 'class_evidence' and r['status'] == 'excluded' for r in diagnostics)
