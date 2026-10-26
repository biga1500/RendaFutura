from copy import deepcopy
import csv
from datetime import date
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from . import inventory_financials as native
from . import remaining_financial_research as research
from .collect_market_data import DividendHistory, financial_features, stock_observations


ROOT = Path(__file__).parent / 'data/market'
CUTOFF = date(2026, 9, 30)


def _archive(tables):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, stream.getvalue().encode('latin1'))
    return output.getvalue()


@pytest.fixture(scope='module')
def original():
    manifest = research.read_review(ROOT)
    with zipfile.ZipFile(ROOT / manifest['native_archive']['file']) as archive:
        tables = {}
        for name in archive.namelist():
            rows = [row for row in native._rows(archive, name)
                    if row.get('CD_CVM') and int(row['CD_CVM']) == 20532]
            if rows:
                tables[name] = rows
    return manifest, tables


@pytest.fixture
def cache(tmp_path, monkeypatch, original):
    manifest, tables = deepcopy(original)
    root = tmp_path / research.RESEARCH_DIR
    root.mkdir(parents=True)
    local_proof = research._local_proof

    def resolve(folder, proof):
        path = folder / research.RESEARCH_DIR / proof['file']
        return local_proof(folder if path.is_file() else ROOT, proof)

    monkeypatch.setattr(research, '_local_proof', resolve)

    def publish():
        content = _archive(tables)
        archive_path = tmp_path / manifest['native_archive']['file']
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(content)
        manifest['native_archive']['sha256'] = hashlib.sha256(content).hexdigest()
        body = (json.dumps(manifest, indent=2) + '\n').encode()
        (tmp_path / research.RESEARCH_FILE).write_bytes(body)
        monkeypatch.setattr(research, 'RESEARCH_SHA256', hashlib.sha256(body).hexdigest())

    publish()
    with monkeypatch.context() as absent:
        absent.setattr(native, '_research', lambda *args: {})
        baseline = native.prepare_financials(tmp_path, research.SPECS, CUTOFF)
    assert all(financial_features(baseline[0], [], CUTOFF, spec) is None
               for spec in research.SPECS.values())
    return SimpleNamespace(folder=tmp_path, manifest=manifest, tables=tables,
                           publish=publish, baseline=baseline)


def _prepare(cache, specs=None, cutoff=CUTOFF, prepared=None):
    return research.prepare_remaining_financials(
        cache.folder, research.SPECS if specs is None else specs, cutoff,
        prepared=cache.baseline if prepared is None else prepared)


def _blocked(cache, **kwargs):
    before = deepcopy(cache.baseline)
    result = _prepare(cache, **kwargs)
    assert result[0] == before[0] and result[2] == before[2]
    assert cache.baseline == before
    assert result[1][-1]['status'] == 'excluded'
    assert result[1][-1]['kind'] == 'remaining_financial_research'
    return result[1][-1]


def test_real_getter_is_offline_and_returns_printed_class_eps(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline financial preparation attempted transport')

    monkeypatch.setattr(research.subprocess, 'run', forbidden)
    review, receipts = research.get_sanb_reading(ROOT, CUTOFF)
    assert tuple(review[field] for field in research.IDENTITY_FIELDS) == research.IDENTITY
    assert review['basic_eps'] == research._expected_eps()
    assert len(receipts) == 4
    assert sum(Decimal(row['numerator_brl']) for row in review['basic_eps']) == Decimal('12766035000')
    for entry in review['basic_eps']:
        assert Decimal(entry['reported_value']) / 1000 == Decimal(entry['value'])
        ratio = Decimal(entry['numerator_brl']) / Decimal(entry['weighted_shares'])
        assert ratio != Decimal(entry['value'])
        assert abs(ratio - Decimal(entry['value'])) < Decimal('0.000005')
    assert review['pdf_page'] == 192 and review['printed_page'] == 72
    manifest = research.read_review(ROOT)
    for filename, digest in manifest['immutable_prior_manifests'].items():
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == digest
    assert manifest['negative_findings'][0]['ticker'] == 'SIMH3'
    assert manifest['negative_findings'][0]['status'] == 'blocked'
    source = next(row for row in receipts if row['document_type'] == 'derived_complete_page_text')
    assert source['authority'] == 'derived_from_verified_primary_original'
    assert source['retrieved_at'] is None and source['http_date'] is None
    assert source['retrieval_clock'] is None
    assert source['retrieval_kind'] == 'local_text_extraction'
    assert source['url_role'] == 'underlying_primary_original'
    assert source['derived_from_source_refs'] == [review['primary_evidence']['source_ref']]
    assert all(row['collected_at'] is None for row in receipts)
    assert review['canonical_proof_scope']['nci_profit_brl'] == '199088000'
    assert review['canonical_proof_scope']['reported_basic_classes'] == ['ON', 'PN']
    assert review['forecast_use']['inference_policy'] == 'pooled_cross_company_model'
    assert review['forecast_use']['own_training_rows'] is None
    assert review['forecast_use']['issuer_specific_forecast_validation'] is False
    primary = next(row for row in receipts if row['document_type'] == 'native_DFP_download')
    assert primary['authority'] == 'primary'
    assert primary['retrieved_at'] and primary['http_date']
    assert primary['retrieved_at'][:10] != review['published_at']
    assert all(row['clock_use'] == 'provenance_only_not_publication_or_point_in_time_eligibility'
               for row in receipts)


def test_real_rows_recover_both_classes_with_whole_consolidated_accounts(cache):
    before = deepcopy(cache.baseline)
    statements, diagnostics, sources = _prepare(cache)
    for ticker, spec in research.SPECS.items():
        features, record = financial_features(statements, [], CUTOFF, spec)
        expected = 1.62922 if ticker == 'SANB3' else 1.79214
        assert features[2] == record['profit_per_share'] == expected
        assert features[3] is None and record['free_cash_per_share'] is None
        assert features[5] == pytest.approx((1270029465 - 126553144) / 1270029465)
        assert record['accounts_scope'] == 'consolidated'
        assert (record['filing_id'], record['version'], record['published_at']) == ('154968', '2', '2026-02-27')
        assert record['document_type'] == 'DFP' and record['period_type'] == 'annual'
        assert record['annualized'] is False
        method = record['financial_method']
        assert method['id'] == 'financial_no_industrial_fcf_v1'
        assert method['profit_account'] == '3.11.01'
        assert method['basic_eps_account'] == 'note:28.a:basic_' + spec[1]
        assert len(method['reported_basic_eps']) == 1
        assert method['reported_basic_eps'][0]['share_class'] == spec[1]
        assert all('_con_' in name for name in method['source_statements'])
    assert diagnostics[-1]['status'] == 'included'
    assert len(sources) == len(before[2]) + 4
    assert cache.baseline == before


def test_canonical_preparer_uses_getter_and_normal_annual_parser_for_both_classes(cache):
    statements, diagnostics, sources = native.prepare_financials(cache.folder, research.SPECS, CUTOFF)
    for ticker, spec in research.SPECS.items():
        features, record = financial_features(statements, [], CUTOFF, spec)
        assert features[2] == (1.62922 if ticker == 'SANB3' else 1.79214)
        assert features[3] is None
        assert record['accounts_scope'] == 'consolidated'
        assert (record['fiscal_end'], record['version'], record['filing_id']) == ('2025-12-31', '2', '154968')
        assert record['financial_method']['basic_eps_account'] == 'note:28.a:basic_' + spec[1]
        assert record['financial_method']['profit_account'] == '3.11.01'
        assert record['financial_method']['id'] == 'financial_no_industrial_fcf_v1'
    assert len([row for row in diagnostics if row.get('kind') == 'candidate'
                and row.get('ticker') in research.SPECS and row['status'] == 'included']) == 2
    derived = [row for row in sources if row.get('document_type') == 'derived_complete_page_text']
    assert len(derived) == 1 and derived[0]['authority'] != 'primary'


def test_canonical_unit_component_aliases_use_exact_issuer_class_and_sector(cache):
    specs = {'SANB11:component:0': (20532, 'ON', 2),
             'SANB11:component:1': (20532, 'PN', 2)}
    statements, diagnostics, _ = native.prepare_financials(cache.folder, specs, CUTOFF)
    for name, spec in specs.items():
        features, record = financial_features(statements, [], CUTOFF, spec)
        assert features[2] == (1.62922 if spec[1] == 'ON' else 1.79214)
        assert features[3] is None and record['filing_id'] == '154968'
        assert any(row.get('ticker') == name and row['status'] == 'included'
                   for row in diagnostics if row.get('kind') == 'candidate')


@pytest.mark.parametrize('specs', [
    {'SANB11:component:0': (20532, 'PNA', 2)},
    {'SANB11:component:0': (20532, 'ON', 1)},
    {'SANB11:component:0': (20531, 'ON', 2)},
    {'SANB3': (20532, 'PN', 2)},
    {'SANB4': (20532, 'ON', 2)},
])
def test_component_aliases_cannot_borrow_a_different_class_sector_or_issuer_note(cache, specs):
    statements, _, _ = native.prepare_financials(cache.folder, specs, CUTOFF)
    assert all(financial_features(statements, [], CUTOFF, spec) is None for spec in specs.values())


@pytest.mark.parametrize('failure', ['getter_error', 'corrupt_original'])
def test_canonical_preparer_fails_closed_when_getter_source_fails(cache, monkeypatch, failure):
    def unavailable(*args, **kwargs):
        raise ValueError('remaining_primary_proof_hash_mismatch')

    if failure == 'getter_error':
        monkeypatch.setattr(research, 'get_sanb_reading', unavailable)
    else:
        path = cache.folder / research.RESEARCH_DIR / cache.manifest['documents']['native_pdf']['file']
        path.write_bytes(b'%PDF-corrupted-original')
    statements, diagnostics, sources = native.prepare_financials(cache.folder, research.SPECS, CUTOFF)
    assert all(financial_features(statements, [], CUTOFF, spec) is None
               for spec in research.SPECS.values())
    assert all(row['status'] == 'excluded' for row in diagnostics
               if row.get('kind') == 'candidate' and row.get('ticker') in research.SPECS)
    assert any(row.get('kind') == 'source' and row.get('source_file') == research.RESEARCH_FILE
               and row['reasons'] == ['documentary_readings_unavailable'] for row in diagnostics)
    assert not any(row.get('document_type') == 'audited_native_annual_statements' for row in sources)


def test_receipt_audit_documents_fca_identity_and_zero_row_pooled_inference():
    root = ROOT / research.RESEARCH_DIR
    audit = json.loads((root / 'getter-receipt-audit-20261025-v2.json').read_bytes())
    assert audit['manifest_sha256'] == research.RESEARCH_SHA256
    assert audit['source_receipt_counts']['total'] == 4
    assert audit['source_receipt_counts']['primary'] == 3
    assert audit['source_receipt_counts']['derived_text'] == 1
    assert audit['selected_identity']['filing_id'] == '154968'
    assert audit['selected_identity']['version'] == '2'
    assert audit['selected_identity']['accounts_scope'] == 'consolidated'
    fca = audit['fca_exact_code_class_evidence']
    assert hashlib.sha256((ROOT / fca['file']).read_bytes()).hexdigest() == fca['sha256']
    assert fca['classes']['SANB3']['share_class'] == 'ON'
    assert fca['classes']['SANB4']['share_class'] == 'PN'
    assert {row['cnpj'] for row in fca['classes'].values()} == {'90.400.888/0001-42'}
    inference = audit['staged_forecast_inference']
    previous_body = (ROOT / inference['source_report']).read_bytes()
    assert hashlib.sha256(previous_body).hexdigest() == inference['source_report_sha256']
    assert inference['own_training_rows'] == json.loads(previous_body)['training_observations'] == 0
    assert inference['inference_policy'] == 'pooled_cross_company_model'
    assert inference['current_features_eligible'] is True
    assert inference['issuer_specific_forecast_validation'] is False
    assert inference['model_fit_or_prediction_performed'] is False


@pytest.mark.parametrize('field,value', [
    ('cvm', 1023), ('cnpj', '00.000.000/0001-91'), ('accounts_scope', 'individual'),
    ('fiscal_end', '2024-12-31'), ('version', '1'), ('filing_id', '154583'),
    ('published_at', '2026-02-04'), ('standard', 'BRGAAP'), ('eps_scope', 'individual'),
    ('period_type', 'quarterly'), ('full_dre', False), ('profit_account', '3.11'),
    ('parent_profit_brl', '12965123000'), ('correct_parent_profit_sign', True),
    ('individual_scope_archive_sha256', 'fake'), ('discard_basic_eps_accounts', ['3.99.01']),
])
def test_resealed_wrong_identity_scope_and_parent_are_rejected(cache, field, value):
    cache.manifest['readings'][0][field] = value
    cache.publish()
    _blocked(cache)


@pytest.mark.parametrize('field,value', [
    ('value', '1629.22'), ('value', '1.60261'), ('value', '1.79214'),
    ('reported_value', '1.62922'), ('share_class', 'PN'), ('account', '3.99.01'),
    ('account', 'note:28.b:diluted_ON'), ('eps_kind', 'diluted'),
    ('denominator_class', 'PN'), ('denominator_kind', 'year_end_issued'),
    ('numerator_scope', 'individual'), ('normalization_divisor', '1'),
    ('weighted_shares', '3804009'), ('numerator_brl', '12766035000'),
    ('shares_rounding', '10000000'), ('numerator_rounding_brl', '10000000'),
])
def test_resealed_eps_cannot_borrow_class_units_diluted_or_denominator(cache, field, value):
    cache.manifest['readings'][0]['basic_eps'][0][field] = value
    cache.publish()
    _blocked(cache)


@pytest.mark.parametrize('key', [
    'native_download', 'native_pdf', 'filing_xml', 'page_text', 'transport_request', 'transport_receipt',
])
def test_each_proof_tamper_blocks_without_partial_admission(cache, key):
    proof = cache.manifest['documents'][key]
    original = ROOT / research.RESEARCH_DIR / proof['file']
    path = cache.folder / research.RESEARCH_DIR / proof['file']
    path.write_bytes(original.read_bytes() + b'changed')
    _blocked(cache)


def test_document_path_traversal_blocks(cache):
    cache.manifest['documents']['native_pdf']['file'] = '../outside.pdf'
    cache.publish()
    assert _blocked(cache)['detail'] == 'local_proof_basename_required'


def test_manifest_tamper_without_repin_blocks(cache):
    path = cache.folder / research.RESEARCH_FILE
    path.write_bytes(path.read_bytes() + b' ')
    assert _blocked(cache)['detail'] == 'remaining_manifest_hash_mismatch'


@pytest.mark.parametrize('suffix,code,value', [
    ('DRE_con', '3.11.01', '12965123.0000000000'),
    ('DRE_con', '3.11.02', '0.0000000000'),
    ('BPA_con', '1', '88642122.0000000000'),
    ('BPP_con', '2.07', '95650292.0000000000'),
])
def test_even_resealed_whole_tables_cannot_change_native_roots(cache, suffix, code, value):
    rows = cache.tables[f'dfp_cia_aberta_{suffix}_2025.csv']
    row = next(row for row in rows if row['CD_CONTA'] == code and row['ORDEM_EXERC'] == '\u00daLTIMO')
    row['VL_CONTA'] = value
    current = [row for row in rows if row['ORDEM_EXERC'] == '\u00daLTIMO']
    digest = native._dre_fingerprint(current, full=True)
    review = cache.manifest['readings'][0]
    review['table_fingerprints']['_' + suffix] = digest
    if suffix == 'DRE_con':
        review['dre_sha256'] = digest
    cache.publish()
    assert _blocked(cache)['detail'] in (
        'native_parent_nci_reconciliation_failed', 'consolidated_balance_root_mismatch')


def test_incomplete_whole_scope_proof_blocks(cache):
    del cache.manifest['readings'][0]['table_fingerprints']['_BPP_con']
    cache.publish()
    assert _blocked(cache)['detail'] == 'whole_scope_proof_required'


def test_changed_cash_flow_or_period_cannot_be_silently_imported(cache):
    rows = cache.tables['dfp_cia_aberta_DFC_MD_con_2025.csv']
    next(row for row in rows if row['ORDEM_EXERC'] == '\u00daLTIMO')['DT_INI_EXERC'] = '2025-12-01'
    cache.publish()
    assert _blocked(cache)['detail'] == 'documented_whole_scope_cells_mismatch'


def test_newer_published_revision_blocks_older_note(cache):
    master = cache.tables['dfp_cia_aberta_2025.csv']
    latest = next(row for row in master if row['VERSAO'] == '2')
    master.append({**latest, 'VERSAO': '3', 'ID_DOC': 'new', 'DT_RECEB': '2026-08-01'})
    cache.publish()
    assert _blocked(cache)['detail'] == 'review_is_not_latest_exact_filing'


def test_newer_unpublished_revision_does_not_change_cutoff_selection(cache):
    master = cache.tables['dfp_cia_aberta_2025.csv']
    latest = next(row for row in master if row['VERSAO'] == '2')
    master.append({**latest, 'VERSAO': '3', 'ID_DOC': 'future', 'DT_RECEB': '2026-10-01'})
    cache.publish()
    assert _prepare(cache)[1][-1]['status'] == 'included'


def test_duplicate_latest_master_is_rejected(cache):
    master = cache.tables['dfp_cia_aberta_2025.csv']
    master.append(deepcopy(next(row for row in master if row['VERSAO'] == '2')))
    cache.publish()
    assert _blocked(cache)['detail'] == 'ambiguous_latest_filing'


def test_newer_same_year_mirror_cannot_hide_behind_preferred_cache(cache):
    master = cache.tables['dfp_cia_aberta_2025.csv']
    latest = next(row for row in master if row['VERSAO'] == '2')
    newer = {**latest, 'VERSAO': '3', 'ID_DOC': 'mirror-newer', 'DT_RECEB': '2026-08-01'}
    path = cache.folder / 'raw/cvm-dfp-2025.zip'
    path.write_bytes(_archive({'dfp_cia_aberta_2025.csv': [newer]}))
    assert _blocked(cache)['detail'] == 'review_is_not_latest_exact_filing'


def test_identical_latest_mirror_is_not_a_duplicate_filing(cache):
    source = cache.folder / cache.manifest['native_archive']['file']
    mirror = cache.folder / 'raw/cvm-dfp-2025.zip'
    mirror.write_bytes(source.read_bytes())
    assert _prepare(cache)[1][-1]['status'] == 'included'
    reading, _ = research.get_sanb_reading(cache.folder, CUTOFF)
    assert reading['latest_native_filing_guard']['files_scanned'] == [
        'raw/official-financials/dfp_cia_aberta_2025.zip', 'raw/cvm-dfp-2025.zip']


def test_conflicting_identity_in_same_year_mirror_is_rejected(cache):
    latest = next(row for row in cache.tables['dfp_cia_aberta_2025.csv'] if row['VERSAO'] == '2')
    mirror = cache.folder / 'raw/cvm-dfp-2025.zip'
    mirror.write_bytes(_archive({'dfp_cia_aberta_2025.csv': [{**latest, 'ID_DOC': 'conflicting'}]}))
    assert _blocked(cache)['detail'] == 'conflicting_latest_filing_across_caches'


def test_latest_guard_inspects_pre_2015_cache_files_too(cache):
    latest = next(row for row in cache.tables['dfp_cia_aberta_2025.csv'] if row['VERSAO'] == '2')
    mirror = cache.folder / 'raw/cvm-dfp-2014.zip'
    mirror.write_bytes(_archive({'dfp_cia_aberta_2014.csv': [latest]}))
    reading, _ = research.get_sanb_reading(cache.folder, CUTOFF)
    assert 'raw/cvm-dfp-2014.zip' in reading['latest_native_filing_guard']['files_scanned']


def test_before_publication_cannot_use_note(cache):
    assert _blocked(cache, cutoff=date(2026, 2, 26))['detail'] == 'filing_not_yet_published'


@pytest.mark.parametrize('spec', [(20532, 'PN', 2), (20532, 'ON', 4), (1023, 'ON', 2)])
def test_wrong_candidate_identity_does_not_recover(cache, spec):
    assert _blocked(cache, specs={'SANB3': spec})['detail'] == 'exact_sanb_candidate_identity_required'


def test_units_simh_wiz_and_bbas_are_untouched(cache, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unrelated candidates invoked Santander getter')

    monkeypatch.setattr(research, '_validated_sanb', forbidden)
    specs = {'SANB11': (20532, 'UNIT', 2), 'SIMH3': (25003, 'ON', 2),
             'WIZC3': (23590, 'ON', 2), 'BBAS3': (1023, 'ON', 2)}
    assert _prepare(cache, specs=specs) == cache.baseline


def test_only_requested_class_is_replaced_and_input_is_not_mutated(cache):
    result = _prepare(cache, specs={'SANB3': research.SPECS['SANB3']})
    before_pn = [row for row in cache.baseline[0] if row['share_class'] == 'PN']
    assert [row for row in result[0] if row['share_class'] == 'PN'] == before_pn
    assert financial_features(result[0], [], CUTOFF, research.SPECS['SANB3']) is not None
    assert financial_features(result[0], [], CUTOFF, research.SPECS['SANB4']) is None


def test_latest_note_does_not_backfill_past_training_or_create_unfinished_labels(cache):
    statements = _prepare(cache, specs={'SANB3': research.SPECS['SANB3']})[0]
    assert financial_features(statements, [], date(2026, 2, 26), research.SPECS['SANB3']) is None
    assert financial_features(statements, [], date(2026, 2, 28), research.SPECS['SANB3']) is not None
    prices = {date(2025, 9, 30): (1, 1, 1), date(2026, 2, 28): (1, 1, 1)}
    observations, coverage = stock_observations(
        'SANB3', prices, DividendHistory(CUTOFF, 0), statements, CUTOFF, spec=research.SPECS['SANB3'])
    assert observations == []
    assert coverage['excluded_by_reason']['financial_features_unavailable'] == 1


def test_unrelated_prepared_records_are_preserved_during_success(cache):
    prepared = deepcopy(cache.baseline)
    marker = {'cvm': 1023, 'share_class': 'ON', 'fiscal_end': '2025-12-31',
              'version': '1', 'filing_id': '154715', 'profit_per_share': 2.4}
    prepared[0].append(marker)
    before = deepcopy(prepared)
    result = _prepare(cache, specs={'SANB3': research.SPECS['SANB3']}, prepared=prepared)
    assert marker in result[0] and prepared == before
    assert financial_features(result[0], [], CUTOFF, research.SPECS['SANB3']) is not None


def test_newer_fiscal_year_blocks_previous_annual_note(cache):
    master = cache.tables['dfp_cia_aberta_2025.csv']
    latest = next(row for row in master if row['VERSAO'] == '2')
    rows = [{**latest, 'VERSAO': '1', 'DT_REFER': '2026-06-30',
             'DT_RECEB': '2026-08-01', 'ID_DOC': 'future-year'}]
    content = _archive({'dfp_cia_aberta_2026.csv': rows})
    (cache.folder / 'raw/official-financials/dfp_cia_aberta_2026.zip').write_bytes(content)
    assert _blocked(cache)['detail'] == 'review_is_not_latest_exact_filing'


@pytest.mark.parametrize('old,new', [
    ('1.000 a', '1 a'), ('1.629,22', '1.602,61'), ('6.197.567', '12.766.035'),
    ('3.804.009', '3.934.128'), ('Consolidadas', 'Individuais'),
    ('a) Lucro por A', 'b) Lucro por A'),
])
def test_actual_note_parser_rejects_adversarial_text(old, new):
    proof = research.read_review(ROOT)['documents']['page_text']
    text = json.loads((ROOT / research.RESEARCH_DIR / proof['file']).read_bytes())['pages']['192']
    assert old in text
    with pytest.raises(ValueError):
        research.parse_sanb_basic_note(text.replace(old, new))


def test_native_transport_refuses_a_second_attempt_without_transport(tmp_path, monkeypatch):
    root = tmp_path / research.RESEARCH_DIR
    root.mkdir(parents=True)
    (root / 'request-SANB3.json').write_text('{}')
    monkeypatch.setattr(research.subprocess, 'run', lambda *args, **kwargs: pytest.fail('network'))
    with pytest.raises(ValueError, match='exact_filing_already_attempted'):
        research.fetch_native(tmp_path, 'SANB3')


@pytest.mark.parametrize('status', [401, 403, 429])
def test_native_transport_stops_origin_after_denial(tmp_path, monkeypatch, status):
    root = tmp_path / research.RESEARCH_DIR
    root.mkdir(parents=True)
    (root / 'response-SANB3.json').write_text(json.dumps({'status': status}))
    monkeypatch.setattr(research.subprocess, 'run', lambda *args, **kwargs: pytest.fail('network'))
    with pytest.raises(ValueError, match='native_origin_stopped_after_denial'):
        research.fetch_native(tmp_path, 'SIMH3')


@pytest.mark.parametrize('body', [b'robots disallow', b'CAPTCHA required', b'login required'])
def test_native_transport_stops_after_robots_or_auth_html(tmp_path, monkeypatch, body):
    import base64

    response = {'status': 200, 'headers': {}, 'body': base64.b64encode(body).decode()}
    monkeypatch.setattr(research.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(response).encode()))
    receipt = research.fetch_native(tmp_path, 'SANB3')
    assert receipt['access_denied'] is True
    with pytest.raises(ValueError, match='native_origin_stopped_after_denial'):
        research.fetch_native(tmp_path, 'SIMH3')


def test_native_transport_rejects_any_other_issuer_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(research.subprocess, 'run', lambda *args, **kwargs: pytest.fail('network'))
    with pytest.raises(ValueError, match='unreviewed_issuer'):
        research.fetch_native(tmp_path, 'BBAS3')
