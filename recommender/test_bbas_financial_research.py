from copy import deepcopy
import csv
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.request
import zipfile

import pytest

from . import bbas_financial_research as bbas
from . import inventory_financials as native
from .collect_market_data import financial_features


ROOT = Path(__file__).parent / 'data/market'
CUTOFF = date(2026, 9, 30)
SPEC = (1023, 'ON', 2)


def _archive(tables):
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        for name, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, stream.getvalue().encode('latin1'))
    return body.getvalue()


@pytest.fixture(scope='module')
def real_projection():
    """One issuer's unchanged real rows keep adversarial tests small and local."""
    review = bbas.read_review(ROOT)
    with zipfile.ZipFile(ROOT / review['native_archive']['file']) as archive:
        tables = {}
        for name in archive.namelist():
            rows = [row for row in native._rows(archive, name)
                    if row.get('CD_CVM') and int(row['CD_CVM']) == 1023]
            if rows:
                tables[name] = rows
    return review, tables


@pytest.fixture
def cache(tmp_path, monkeypatch, real_projection):
    manifest, tables = deepcopy(real_projection)
    root = tmp_path / bbas.RESEARCH_DIR
    root.mkdir(parents=True)
    local_proof = bbas._local_proof

    def resolve(folder, proof):
        path = folder / bbas.RESEARCH_DIR / proof['file']
        return local_proof(folder if path.is_file() else ROOT, proof)

    monkeypatch.setattr(bbas, '_local_proof', resolve)

    def publish():
        content = _archive(tables)
        path = tmp_path / manifest['native_archive']['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        manifest['native_archive']['sha256'] = hashlib.sha256(content).hexdigest()
        body = (json.dumps(manifest, indent=2) + '\n').encode()
        (tmp_path / bbas.RESEARCH_FILE).write_bytes(body)
        monkeypatch.setattr(bbas, 'RESEARCH_SHA256', hashlib.sha256(body).hexdigest())

    publish()
    with monkeypatch.context() as unavailable:
        unavailable.setattr(native, '_research', lambda *args: {})
        baseline = native.prepare_financials(tmp_path, {'BBAS3': SPEC}, CUTOFF)
    assert financial_features(baseline[0], [], CUTOFF, SPEC) is None
    return tmp_path, manifest, tables, publish, baseline


def _prepare(cache, *, spec=SPEC, cutoff=CUTOFF, prepared=None):
    folder, _, _, _, baseline = cache
    return bbas.prepare_bbas_financials(folder, {'BBAS3': spec}, cutoff,
                                       prepared=baseline if prepared is None else prepared)


def _blocked(cache, **kwargs):
    baseline = deepcopy(cache[4])
    result = _prepare(cache, **kwargs)
    assert result[0] == baseline[0]
    assert result[2] == baseline[2]
    assert financial_features(result[0], [], CUTOFF, SPEC) is None
    assert result[1][-1]['kind'] == 'bbas_research' and result[1][-1]['status'] == 'excluded'
    return result[1][-1]


def test_real_review_hash_original_member_and_exact_filing_getter_offline(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail('offline getter attempted network or subprocess')

    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    monkeypatch.setattr(bbas.subprocess, 'run', no_network)
    manifest = bbas.read_review(ROOT)
    reading, receipts = bbas.get_bbas_reading(ROOT, CUTOFF)
    assert tuple(reading[field] for field in bbas.IDENTITY_FIELDS) == bbas.IDENTITY
    assert reading['pdf_page'] == 300
    assert reading['primary_evidence']['source_ref'].endswith(
        '4b496749c486d49c4109ce1c279c7bcba1bf73163db627f3dffc23395698628f')
    assert {row['sha256'] for row in receipts} == {
        proof['sha256'] for proof in manifest['documents'].values()}
    by_hash = {row['sha256']: row for row in receipts}
    evidence = by_hash[manifest['documents']['page_evidence']['sha256']]
    assert evidence['authority'] == 'derived_from_verified_primary_originals'
    assert evidence['retrieved_at'] is None and evidence['retrieval_clock'] is None
    assert evidence['observed_at'] == evidence['local_file_mtime']
    assert evidence['observed_at_basis'] == 'local_file_mtime_not_extraction_or_publication_time'
    assert evidence['clock_use'] == 'provenance_only_not_publication_or_point_in_time_eligibility'
    assert evidence['derived_from_source_refs'] == [
        'primary-financials:' + manifest['documents'][name]['sha256']
        for name in ('native_pdf', 'ri_ifrs')]
    assert all(row['authority'] == 'primary' for row in receipts if row is not evidence)
    for key, number in [('ri_ifrs', 4), ('native_pdf', 5), ('ri_widget', 2)]:
        receipt = by_hash[manifest['documents'][key]['sha256']]
        response = json.loads((ROOT / bbas.RESEARCH_DIR / f'response-{number:02d}.json').read_bytes())
        headers = {key.lower(): value for key, value in response['headers'].items()}
        assert receipt['retrieved_at'] == response['completed_at']
        assert receipt['retrieval_started_at'] == response['started_at']
        assert receipt['retrieval_clock'] == 'client_wall_clock_from_transport_receipt'
        assert receipt['observed_at'] == response['completed_at']
        assert receipt['observed_at_basis'] == 'collector_client_wall_clock_completion'
        assert receipt['http_date'] == headers.get('date')
        assert receipt['http_last_modified'] == headers.get('last-modified')
        assert receipt['local_file_mtime'] is not None
    entry = reading['basic_eps'][0]
    ratio = Decimal(entry['numerator_brl']) / Decimal(entry['weighted_shares'])
    assert Decimal('2.395') < ratio < Decimal('2.405')
    assert ratio != Decimal(entry['value'])
    previous = ROOT / manifest['supersedes_only_bbas_finding']['prior_manifest']
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == \
        manifest['supersedes_only_bbas_finding']['prior_sha256']
    folder = ROOT / bbas.RESEARCH_DIR
    assert len(list(folder.glob('request-*.json'))) == 5
    assert [json.loads((folder / f'response-{i:02d}.json').read_bytes())['status']
            for i in range(1, 6)] == [200] * 5


def test_real_rows_admit_printed_basic_on_only_and_preserve_consolidated_contract(cache):
    before = deepcopy(cache[4])
    records, diagnostics, sources = _prepare(cache)
    features, record = financial_features(records, [], CUTOFF, SPEC)
    assert features[2] == 2.4 and features[3] is None
    assert features[5] == pytest.approx(2261575837 / 2455143253)
    assert record['filing_id'] == '154715' and record['version'] == '1'
    assert record['accounts_scope'] == 'consolidated'
    assert record['document_type'] == 'DFP' and record['period_type'] == 'annual'
    assert record['annualized'] is False
    method = record['financial_method']
    assert method['id'] == 'financial_no_industrial_fcf_v1'
    assert method['profit_account'] == '3.11.01'
    assert method['basic_eps_account'] == 'note:30.f:basic_ON'
    assert method['reported_basic_eps'][0]['value'] == '2.40'
    assert all('_ind_' not in name for name in method['source_statements'])
    assert cache[4] == before
    assert len(sources) == len(before[2]) + 7
    assert diagnostics[-1]['status'] == 'included'
    assert next(row for row in diagnostics if row.get('kind') == 'candidate')['status'] == 'included'


def test_canonical_native_preparer_applies_verified_note_through_unchanged_financial_gates(cache):
    records, diagnostics, sources = native.prepare_financials(cache[0], {'BBAS3': SPEC}, CUTOFF)
    features, record = financial_features(records, [], CUTOFF, SPEC)
    assert features[2] == 2.4 and features[3] is None
    assert record['accounts_scope'] == 'consolidated'
    assert record['filing_id'] == '154715'
    assert record['financial_method']['basic_eps_account'] == 'note:30.f:basic_ON'
    assert any(row.get('ticker') == 'BBAS3' and row.get('status') == 'included' for row in diagnostics)
    assert any(row['document_type'] == 'native_DFP_download' for row in sources)


@pytest.mark.parametrize(('field', 'value'), [
    ('ticker', 'BBAS4'), ('cvm', 1024), ('cnpj', '00.000.000/0001-92'),
    ('share_class', 'PN'), ('sector_code', 4), ('accounts_scope', 'individual'),
    ('fiscal_end', '2024-12-31'), ('version', '2'), ('filing_id', '144874'),
    ('published_at', '2026-02-10'), ('eps_scope', 'individual'),
    ('standard', 'BRGAAP'), ('period_type', 'interim'), ('full_dre', False),
])
def test_wrong_identity_scope_class_version_or_period_cannot_admit(cache, field, value):
    cache[1]['readings'][0][field] = value
    cache[3]()
    _blocked(cache)


@pytest.mark.parametrize(('field', 'value'), [
    ('share_class', 'PN'), ('denominator_class', 'PN'), ('eps_kind', 'diluted'),
    ('denominator_kind', 'year_end_issued'), ('numerator_scope', 'consolidated_total'),
    ('weighted_shares', '5730834040'), ('weighted_shares', '5708463641'),
    ('weighted_shares', '5708696.148'), ('weighted_shares', '0'),
    ('shares_rounding', '10000000'), ('value', '2.399518847188781924288887551'),
    ('value', '4.62'), ('account', '3.99'), ('account', '3.99.01'),
    ('account', '3.99.02.01'), ('numerator_rounding_brl', '5000000000'),
    ('numerator_brl', '16781938000'), ('numerator_brl', '3083814000'),
    ('numerator_brl', '18151787000'),
])
def test_generic_diluted_standalone_nci_or_wrong_denominator_cannot_admit(cache, field, value):
    cache[1]['readings'][0]['basic_eps'][0][field] = value
    cache[3]()
    _blocked(cache)


@pytest.mark.parametrize('profit', ['16781938000', '3083814000', '18151787000'])
def test_changing_both_note_profit_and_numerator_to_wrong_scope_still_fails(cache, profit):
    review = cache[1]['readings'][0]
    review['parent_profit_brl'] = profit
    review['basic_eps'][0]['numerator_brl'] = profit
    cache[3]()
    _blocked(cache)


@pytest.mark.parametrize('account', ['3.11', '3.11.02'])
def test_profit_account_cannot_alias_total_or_nci(cache, account):
    cache[1]['readings'][0]['profit_account'] = account
    cache[3]()
    _blocked(cache)


def test_note_crosscheck_independently_rejects_nci_and_wrong_rounding(real_projection):
    manifest, tables = deepcopy(real_projection)
    review = manifest['readings'][0]
    rows = [row for row in tables['dfp_cia_aberta_DRE_con_2025.csv']
            if row['ORDEM_EXERC'] == '\u00daLTIMO']
    native._checked_note_eps(rows, review)
    review['basic_eps'][0]['numerator_brl'] = '16781938000'
    with pytest.raises(ValueError, match='documented_eps_numerator_mismatch'):
        native._checked_note_eps(rows, review)
    review['basic_eps'][0]['numerator_brl'] = '13698124000'
    review['basic_eps'][0]['value'] = '2.41'
    with pytest.raises(ValueError, match='fails_weighted_share_crosscheck'):
        native._checked_note_eps(rows, review)


@pytest.mark.parametrize('proof_key', ['native_pdf', 'native_download', 'ri_ifrs', 'ri_catalogue',
                                     'ri_index', 'ri_widget', 'page_evidence'])
def test_each_original_or_evidence_hash_is_required(cache, proof_key):
    folder, manifest, _, _, _ = cache
    path = folder / bbas.RESEARCH_DIR / manifest['documents'][proof_key]['file']
    original = ROOT / bbas.RESEARCH_DIR / manifest['documents'][proof_key]['file']
    path.write_bytes(original.read_bytes() + b'corrupt')
    assert _blocked(cache)['detail'] == 'bbas_primary_document_hash_mismatch'


def test_manifest_tamper_without_repin_fails(cache):
    path = cache[0] / bbas.RESEARCH_FILE
    path.write_bytes(path.read_bytes() + b' ')
    assert _blocked(cache)['detail'] == 'bbas_manifest_hash_mismatch'


def test_document_traversal_is_rejected(cache):
    cache[1]['documents']['native_pdf']['file'] = '../elsewhere.pdf'
    cache[3]()
    assert _blocked(cache)['detail'] == 'primary_document_must_be_local_basename'


def test_changed_native_dre_is_not_reinterpreted_even_if_archive_resealed(cache):
    rows = cache[2]['dfp_cia_aberta_DRE_con_2025.csv']
    next(row for row in rows if row['CD_CONTA'] == '3.11.01'
         and row['ORDEM_EXERC'] == '\u00daLTIMO')['VL_CONTA'] = '16781938'
    cache[3]()
    assert _blocked(cache)['detail'] == 'documented_original_consolidated_dre_mismatch'


def test_resealed_dre_with_wrong_nci_reconciliation_fails(cache):
    rows = cache[2]['dfp_cia_aberta_DRE_con_2025.csv']
    current = [row for row in rows if row['ORDEM_EXERC'] == '\u00daLTIMO']
    next(row for row in current if row['CD_CONTA'] == '3.11.02')['VL_CONTA'] = '0'
    cache[1]['readings'][0]['dre_sha256'] = native._dre_fingerprint(current, full=True)
    cache[3]()
    assert _blocked(cache)['detail'] == 'native_parent_nci_reconciliation_failed'


def test_newer_native_version_blocks_older_review_and_preserves_unavailable_latest(cache):
    master = cache[2]['dfp_cia_aberta_2025.csv']
    master.append({**master[0], 'VERSAO': '2', 'ID_DOC': '999999', 'DT_RECEB': '2026-03-01'})
    cache[3]()
    baseline = native.prepare_financials(cache[0], {'BBAS3': SPEC}, CUTOFF)
    result = _prepare(cache, prepared=baseline)
    assert result[0] == baseline[0]
    assert financial_features(result[0], [], CUTOFF, SPEC) is None
    assert result[1][-1]['detail'] == 'review_is_not_latest_exact_native_filing'


def test_publication_cutoff_and_wrong_candidate_do_not_admit(cache):
    assert _blocked(cache, cutoff=date(2026, 2, 10))['detail'] == 'filing_not_yet_published'
    assert _blocked(cache, spec=(1023, 'PN', 2))['detail'] == 'exact_bbas_candidate_identity_required'
    at_publication = _prepare(cache, cutoff=date(2026, 2, 11))
    assert financial_features(at_publication[0], [], date(2026, 2, 11), SPEC) is not None


def test_unrelated_candidates_and_caller_prepared_result_are_preserved(cache):
    baseline = deepcopy(cache[4])
    other = {**baseline[0][0], 'cvm': 20567, 'share_class': 'PN', 'filing_id': 'pine-unchanged'}
    baseline[0].append(other)
    before = deepcopy(baseline)
    result = bbas.prepare_bbas_financials(cache[0], {'BBAS3': SPEC, 'PINE4': (20567, 'PN', 2)},
                                          CUTOFF, prepared=baseline)
    assert baseline == before
    assert [row for row in result[0] if row['cvm'] == 20567] == [other]
    assert financial_features(result[0], [], CUTOFF, SPEC) is not None


def test_budget_reserves_attempts_and_stops_at_fourteen_including_robots(tmp_path, monkeypatch):
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout=b'')

    monkeypatch.setattr(bbas.subprocess, 'run', failed)
    for _ in range(14):
        assert bbas.fetch_public(tmp_path, 'https://ri.bb.com.br/robots.txt')['status'] is None
    with pytest.raises(ValueError, match='public_request_budget_exhausted'):
        bbas.fetch_public(tmp_path, 'https://ri.bb.com.br/another')
    assert len(calls) == 14


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_entire_origin_and_persists(tmp_path, monkeypatch, status):
    monkeypatch.setattr(bbas.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({'status': status, 'headers': {}, 'body': ''}).encode()))
    assert bbas.fetch_public(tmp_path, 'https://ri.bb.com.br/robots.txt')['status'] == status
    with pytest.raises(ValueError, match='origin_stopped_after_denial'):
        bbas.fetch_public(tmp_path, 'https://ri.bb.com.br/different-path')
    assert len(list((tmp_path / bbas.RESEARCH_DIR).glob('request-*.json'))) == 1


@pytest.mark.parametrize('url', [
    'https://sistemaswebb3-listados.b3.com.br/', 'https://ri.azzas2154.com.br/',
    'https://allos.com.br/', 'https://www.santander.com.br/',
    'http://ri.bb.com.br/', 'https://user:password@ri.bb.com.br/',
    'https://api.mziq.com/mzfilemanager/v2/d/guessed',
])
def test_forbidden_origins_and_undiscovered_mz_urls_never_make_a_request(tmp_path, monkeypatch, url):
    monkeypatch.setattr(bbas.subprocess, 'run', lambda *a, **k: pytest.fail('forbidden request'))
    with pytest.raises(ValueError):
        bbas.fetch_public(tmp_path, url)
    assert not list((tmp_path / bbas.RESEARCH_DIR).glob('request-*.json'))


def test_immutable_artifacts_cannot_be_overwritten(tmp_path):
    path = tmp_path / 'original.pdf'
    bbas._append(path, b'%PDF-original')
    bbas._append(path, b'%PDF-original')
    with pytest.raises(ValueError, match='immutable_artifact_conflict'):
        bbas._append(path, b'%PDF-replacement')
    assert path.read_bytes() == b'%PDF-original'
