"""Offline source binding, complete negative coverage, and real-cache replay."""

from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.request
import zipfile

import pytest

from .dividend_scope import import_scope
from .security_registration import FCA_URL, MASTER_FIELDS, SECURITY_FIELDS
from .selected_identity_research import (FOCUS, OUTPUT, SCOPE_ID, YEARS, LocalSources,
                                          research, scan_fca, write_audit)


MARKET = Path(__file__).parent / 'data/market'
AS_OF = '2026-10-25'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network_forbidden_in_selected_identity_research')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)


def digest(body):
    return hashlib.sha256(body).hexdigest()


def master(**changes):
    return {'CNPJ_CIA': '00.000.000/0001-00', 'DT_REFER': '2026-01-01', 'VERSAO': '1',
            'CD_CVM': '123', 'ID_DOC': '100', 'DT_RECEB': '2026-03-01', **changes}


def security(**changes):
    return {**dict.fromkeys(SECURITY_FIELDS, ''), 'CNPJ_Companhia': '00.000.000/0001-00',
        'Data_Referencia': '2026-01-01', 'Versao': '1', 'ID_Documento': '100',
        'Nome_Empresarial': 'SYNTHETIC ISSUER', 'Codigo_Negociacao': 'AURA33',
        'Valor_Mobiliario': 'Acoes Ordinarias', 'Mercado': 'Bolsa', **changes}


def archive(year=2026, *, masters=None, securities=None, extra=None):
    stream = io.BytesIO()
    tables = [(f'fca_cia_aberta_{year}.csv', MASTER_FIELDS, masters or [master()]),
              (f'fca_cia_aberta_valor_mobiliario_{year}.csv', SECURITY_FIELDS,
               securities if securities is not None else [security()])]
    tables.extend(extra or [])
    with zipfile.ZipFile(stream, 'w') as zipped:
        for name, fields, rows in tables:
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=fields, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            zipped.writestr(name, text.getvalue().encode('latin1'))
    return stream.getvalue()


def save(root, relative, body):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def addressed(root, prefix, value):
    body = json.dumps(value, sort_keys=True).encode()
    return save(root, prefix + '-' + digest(body) + '.json', body)


def fixture_market(tmp_path, *, detail=None, securities=None):
    text = 'Papel\tDiv.Yield\n' + ''.join(t + '\t1,00%\n' for t in FOCUS) + 'ZERO3\t0,00%\n'
    source = save(tmp_path, 'supplied.tsv', text.encode())
    scope = import_scope(tmp_path, source)
    sources = []
    for year in YEARS:
        body = archive(year, securities=securities if year == 2026 else [])
        sha = digest(body)
        relative = f'raw/primary-research/units/fca_cia_aberta_{year}-{sha}.zip'
        save(tmp_path, relative, body)
        sources.append({'file': relative, 'sha256': sha, 'bytes': len(body),
                        'url': FCA_URL + f'fca_cia_aberta_{year}.zip',
                        'retrieved_at': '2026-10-25T00:30:00+00:00', 'authority': 'CVM_FCA'})
    addressed(tmp_path, 'raw/primary-research/units/historical-fca-evidence-2026-10-25', {'sources': sources})
    inventory = {'as_of': '2026-10-24', 'stocks': [], 'sources': []}
    body = json.dumps(inventory).encode()
    save(tmp_path, 'inventory/snapshots/pinned/market.json', body)
    save(tmp_path, 'inventory/current.json', json.dumps({'snapshot': 'snapshots/pinned', 'sha256': digest(body)}).encode())
    save(tmp_path, 'current.json', b'{"snapshot":"do-not-change"}')
    if detail is not None:
        body = json.dumps(detail).encode()
        save(tmp_path, 'raw/expanded/b3-detail-123.json', body)
        addressed(tmp_path, 'raw/primary-research/security-registration/security-registration-2026-10-25',
            {'as_of': AS_OF, 'records': [{'ticker': 'OTHER3', 'name': 'Do not emit'}], 'sources': [{
                'file': 'raw/expanded/b3-detail-123.json', 'sha256': digest(body),
                'original_receipt_verified': False, 'url': None, 'observed_at': None}]})
    return scope['id']


def hashes(root):
    return {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in root.rglob('*')
            if p.is_file() and not p.relative_to(root).as_posix().startswith(OUTPUT + '/')}


def test_exact_legal_type_class_and_market_ignore_symbol_suffix():
    body = archive(securities=[security(Codigo_Negociacao='MRSA5B', Valor_Mobiliario='Acoes Preferenciais',
        Sigla_Classe_Acao_Preferencial='C', Classe_Acao_Preferencial='Classe C', Mercado='Balcao Organizado')])
    result = scan_fca(body, 2026, FOCUS, AS_OF)
    record = result['exact_security_records'][0]
    assert record['legal_security_type'] == 'preferred_share' and record['share_class'] == 'PNC'
    assert record['security']['Mercado'] == 'Balcao Organizado'
    assert record['filing'] == {k: master()[k] for k in MASTER_FIELDS if k != 'CNPJ_CIA'}
    assert record['master_csv_record'] == 2 and record['csv_record'] == 2
    assert record['source_binding_valid'] and record['received_and_referenced_by_as_of']


@pytest.mark.parametrize('kind,expected', [('BDR Patrocinado Nivel III', 'bdr'),
    ('Acoes Ordinarias', 'ordinary_share'), ('Units', 'unit'),
    ('Bonus de Subscricao', 'subscription_warrant'), ('Debentures', 'other_reported_security')])
def test_legal_types_are_source_reported(kind, expected):
    result = scan_fca(archive(securities=[security(Valor_Mobiliario=kind)]), 2026, FOCUS, AS_OF)
    assert result['exact_security_records'][0]['legal_security_type'] == expected


def test_all_versions_and_future_deliveries_remain_dated_and_separate():
    first = master()
    second = master(ID_DOC='101', VERSAO='2', DT_RECEB='2026-10-26')
    body = archive(masters=[first, second], securities=[security(), security(ID_Documento='101', Versao='2')])
    rows = scan_fca(body, 2026, FOCUS, AS_OF)['exact_security_records']
    assert [r['filing']['VERSAO'] for r in rows] == ['1', '2']
    assert [r['filing']['DT_RECEB'] for r in rows] == ['2026-03-01', '2026-10-26']
    assert [r['received_and_referenced_by_as_of'] for r in rows] == [True, False]


@pytest.mark.parametrize('changes,field', [({'CNPJ_Companhia': '99.999.999/0001-99'}, 'CNPJ_Companhia'),
    ({'Data_Referencia': '2025-01-01'}, 'Data_Referencia'), ({'Versao': '2'}, 'Versao'),
    ({'ID_Documento': '999'}, 'CNPJ_Companhia')])
def test_child_master_identity_mismatch_never_establishes_type(changes, field):
    scan = scan_fca(archive(securities=[security(**changes)]), 2026, FOCUS, AS_OF)
    row = scan['exact_security_records'][0]
    assert scan['strict_parser_error'] == 'security_FCA_child_identity_mismatch'
    assert field in scan['join_issues'][0]['mismatched_fields']
    assert not row['source_binding_valid'] and row['legal_security_type'] is None and row['cvm'] is None


def test_unrelated_bad_join_does_not_hide_complete_search_and_PII_is_not_emitted():
    extra = [('fca_cia_aberta_dri_2026.csv', ('Nome', 'Email', 'Descricao'), [
        {'Nome': 'PERSON_NOT_FOR_OUTPUT', 'Email': 'secret@example.invalid', 'Descricao': 'MRSA6B'}])]
    body = archive(securities=[security(Codigo_Negociacao='OTHER3', CNPJ_Companhia='invalid')], extra=extra)
    scan = scan_fca(body, 2026, FOCUS, AS_OF)
    assert len(scan['members']) == 3 and not scan['exact_security_records']
    assert scan['members'][-1]['exact_token_row_matches']['MRSA6B'] == 1
    assert not scan['join_issues'][0]['affects_selected_exact_code']
    encoded = json.dumps(scan)
    assert 'PERSON_NOT_FOR_OUTPUT' not in encoded and 'secret@example.invalid' not in encoded


def test_suffixes_case_related_issuer_and_ISIN_are_not_aliases():
    rows = [security(Codigo_Negociacao=t) for t in ('MRSA5BF', 'MRSA6BF', 'AURA33.SA', 'aura33')]
    scan = scan_fca(archive(securities=rows), 2026, FOCUS, AS_OF)
    assert not scan['exact_security_records']
    assert all(not any(member['exact_token_row_matches'].values()) for member in scan['members'])


def test_complete_machine_audit_always_contains_four_negative_results(tmp_path):
    scope = fixture_market(tmp_path, securities=[])
    report = research(tmp_path, expected_scope=scope)
    assert report['FCA_coverage']['complete'] and report['FCA_coverage']['csv_members_scanned'] == 12
    assert [r['ticker'] for r in report['records']] == list(FOCUS)
    assert all(r['status'] == 'unresolved_in_existing_sources' for r in report['records'])
    assert all(r['isin'] is None and r['legal_security_type'] is None for r in report['records'])
    assert all(not r['whole_security_delisting_verified'] for r in report['records'])
    assert report['network_requests'] == report['b3_network_requests'] == 0


def test_exact_cache_mapping_keeps_source_period_without_decoding_ISIN(tmp_path):
    detail = {'codeCVM': '123', 'companyName': 'SYNTHETIC ISSUER', 'hasBDR': True,
        'status': 'A', 'market': 'BOLSA', 'lastDate': '23/10/2026 23:21:12',
        'otherCodes': [{'code': 'EQPA7', 'isin': 'BREQPAACNPC0'},
                       {'code': 'MRSA5BF', 'isin': 'BRTESTACNOR0'}]}
    scope = fixture_market(tmp_path, detail=detail, securities=[])
    report = research(tmp_path, expected_scope=scope)
    result = next(r for r in report['records'] if r['ticker'] == 'EQPA7')
    assert result['isin'] == 'BREQPAACNPC0' and result['cvm'] == 123
    assert result['legal_security_type'] is None and result['share_class'] is None
    assert result['reported_primary_markets'] == [] and not result['current_trading_verified']
    mapping = result['cached_exact_code_mappings'][0]
    assert mapping['locator'] == 'otherCodes[0]'
    assert mapping['source_reported_lastDate'] == detail['lastDate']
    assert not mapping['source_reported_lastDate_is_publication_date']
    assert mapping['source']['hash_matched_prior_receipt']
    assert not mapping['source']['original_download_receipt_verified']
    assert next(r for r in report['records'] if r['ticker'] == 'MRSA5B')['isin'] is None
    assert all(not r['existing_security_report_exact_records'] for r in report['records'])


def test_missing_year_is_incomplete_not_a_claim_of_exhaustive_absence(tmp_path):
    scope = fixture_market(tmp_path, securities=[])
    history = next((tmp_path / 'raw/primary-research/units').glob('historical-fca-evidence-*.json'))
    doc = json.loads(history.read_bytes())
    missing = doc['sources'].pop(0)
    (tmp_path / missing['file']).unlink()
    history.unlink()
    addressed(tmp_path, 'raw/primary-research/units/historical-fca-evidence-2026-10-25', doc)
    report = research(tmp_path, expected_scope=scope)
    assert report['FCA_coverage']['missing_years'] == [2021]
    assert not report['FCA_coverage']['complete']
    assert all(not r['FCA_search_complete_for_2021_2026'] for r in report['records'])


def test_scope_is_required_pinned_and_positive_only(tmp_path):
    scope = fixture_market(tmp_path)
    with pytest.raises(ValueError, match='scope_missing_or_changed'):
        research(tmp_path)
    with pytest.raises(ValueError, match='outside_positive_DY_scope'):
        research(tmp_path, expected_scope=scope, tickers=['ZERO3'])
    with pytest.raises(ValueError, match='outside_positive_DY_scope'):
        research(tmp_path, expected_scope=scope, tickers=['MRSA5BF'])
    (tmp_path / 'stock-selection/current.json').unlink()
    with pytest.raises(ValueError, match='scope_missing_or_changed'):
        research(tmp_path, expected_scope=scope)


@pytest.mark.parametrize('target', ['archive', 'manifest', 'cache'])
def test_source_tampering_fails_closed(tmp_path, target):
    scope = fixture_market(tmp_path, detail={'codeCVM': '123', 'otherCodes': []})
    base = tmp_path / 'raw/primary-research/units'
    path = next(base.glob('*.zip')) if target == 'archive' else next(base.glob('*.json')) if target == 'manifest' else tmp_path / 'raw/expanded/b3-detail-123.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        research(tmp_path, expected_scope=scope)


def test_missing_FCA_receipt_is_not_replaced_by_a_filename_or_computed_hash(tmp_path):
    scope = fixture_market(tmp_path)
    next((tmp_path / 'raw/primary-research/units').glob('*.json')).unlink()
    with pytest.raises(ValueError, match='official_FCA_receipt_missing'):
        research(tmp_path, expected_scope=scope)


def test_conflicting_types_and_ISINs_stay_unresolved(tmp_path):
    rows = [security(), security(Valor_Mobiliario='Acoes Preferenciais', Sigla_Classe_Acao_Preferencial='A')]
    detail = {'codeCVM': '123', 'otherCodes': [
        {'code': 'AURA33', 'isin': 'BRTESTACNOR0'}, {'code': 'AURA33', 'isin': 'BRTESTACNPA4'}]}
    scope = fixture_market(tmp_path, securities=rows, detail=detail)
    result = research(tmp_path, expected_scope=scope)['records'][0]
    assert result['status'] == 'conflicting_exact_evidence'
    assert result['legal_security_type'] is None and result['share_class'] is None and result['isin'] is None
    assert len(result['FCA_exact_code_records']) == 2


def test_future_filing_is_found_without_establishing_available_identity(tmp_path):
    scope = fixture_market(tmp_path, securities=[])
    body = archive(masters=[master(DT_RECEB='2026-10-26')])
    path = tmp_path / 'raw/inventory/fca_cia_aberta_2026.zip'
    save(tmp_path, path.relative_to(tmp_path).as_posix(), body)
    addressed(tmp_path, 'raw/primary-research/security-registration/security-registration-2026-10-25', {
        'sources': [{'file': path.relative_to(tmp_path).as_posix(), 'sha256': digest(body),
                     'url': FCA_URL + path.name}], 'records': []})
    result = research(tmp_path, expected_scope=scope)['records'][0]
    assert result['FCA_exact_code_found'] and result['legal_security_type'] is None and result['cvm'] is None
    assert result['FCA_exact_code_records'][0]['filing']['DT_RECEB'] == '2026-10-26'


def test_write_is_content_addressed_idempotent_and_only_in_owned_folder(tmp_path):
    scope = fixture_market(tmp_path, securities=[])
    before = hashes(tmp_path)
    report = research(tmp_path, expected_scope=scope)
    path = write_audit(tmp_path, report)
    assert path.parent == tmp_path / OUTPUT
    assert digest(path.read_bytes()) in path.name and json.loads(path.read_bytes()) == report
    assert write_audit(tmp_path, deepcopy(report)) == path
    assert hashes(tmp_path) == before
    assert not (path.parent / 'current.json').exists()
    assert len(list(path.parent.iterdir())) == 1


@pytest.mark.parametrize('relative', ['../outside', 'raw/../outside', 'C:/outside', '/outside', 'raw\\outside'])
def test_source_paths_cannot_escape_root(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe_path'):
        LocalSources(tmp_path).read(relative)


@pytest.mark.skipif(not (MARKET / 'stock-selection/current.json').exists(), reason='real local cache unavailable')
def test_real_full_2021_2026_cache_replay_preserves_selected_scope_and_no_PII():
    report = research(MARKET)
    assert report['scope']['id'] == SCOPE_ID and report['scope']['selected_count'] == 262
    assert report['FCA_coverage'] == {'expected_years': list(YEARS), 'covered_years': list(YEARS),
        'complete': True, 'distinct_archives': 6, 'csv_members_scanned': 60,
        'security_rows_scanned': 6005, 'missing_years': []}
    assert sum(len(a['sources']) for a in report['FCA_archives']) == 7
    assert len(report['existing_exchange_cache_coverage']) == 288
    assert all(not row['FCA_exact_code_found'] for row in report['records'])
    by_code = {row['ticker']: row for row in report['records']}
    assert by_code['EQPA7']['isin'] == 'BREQPAACNPC0' and by_code['EQPA7']['cvm'] == 18309
    mapping = by_code['EQPA7']['cached_exact_code_mappings'][0]
    assert mapping['source']['sha256'] == 'a33f3b1a48e491c1e439749c2d3e8ca401bfaac8b650fdca682204e36d2e8831'
    assert mapping['locator'] == 'otherCodes[3]'
    assert all(by_code[t]['isin'] is None for t in ('AURA33', 'MRSA5B', 'MRSA6B'))
    assert all(row['legal_security_type'] is None and row['share_class'] is None for row in report['records'])
    assert not any(row['whole_security_delisting_verified'] or row['model_admission_granted'] for row in report['records'])
    bad = next(a for a in report['FCA_archives'] if a['year'] == 2023)
    assert bad['join_issues'] == [{'code': 'FCA_child_identity_mismatch',
        'member': 'fca_cia_aberta_valor_mobiliario_2023.csv', 'csv_record': 616,
        'filing_id': '131922', 'mismatched_fields': ['CNPJ_Companhia'], 'affects_selected_exact_code': False}]
    assert all(not c['exact_selected_records'] for c in report['existing_security_report_coverage'])
    assert all(not any(m['exact_token_row_matches'].values()) for a in report['FCA_archives'] for m in a['members'])
    encoded = json.dumps(report)
    assert 'CNPJ_CIA' not in encoded and 'Email' not in encoded and 'CPF' not in encoded
