from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

import pytest

from . import financial_additional_research as additional
from . import inventory_financials as native
from .collect_market_data import financial_features


CUTOFF = date(2026, 9, 30)
SPEC = (23590, 'ON', 2)
ROOT = Path(__file__).parent / 'data/market'


def _rows(scope, table, entries, version='1'):
    common = {'CD_CVM': '023590', 'DT_REFER': '2025-12-31', 'VERSAO': version,
              'ORDEM_EXERC': '\u00daLTIMO', 'MOEDA': 'REAL', 'ESCALA_MOEDA': 'MIL',
              'DT_INI_EXERC': '2025-01-01', 'DT_FIM_EXERC': '2025-12-31'}
    return [{**common, 'CD_CONTA': code, 'DS_CONTA': desc, 'VL_CONTA': str(value)}
            for code, desc, value in entries]


def _fixture_tables():
    tables = {'': [{'CD_CVM': '023590', 'DT_REFER': '2025-12-31', 'VERSAO': '1',
                     'ID_DOC': '155497', 'DT_RECEB': '2026-03-19',
                     'CNPJ_CIA': '42.278.473/0001-03'}]}
    for scope, assets, equity, profit in [('con', 2434061, 1296137, 367710),
                                         ('ind', 1385841, 697792, 201076)]:
        dre = [('3.11', 'Lucro/Prejuizo do Periodo', profit)]
        if scope == 'con':
            dre += [('3.11.01', 'Atribuido a Socios da Empresa Controladora', 201076),
                    ('3.11.02', 'Atribuido a Socios Nao Controladores', 166634),
                    ('3.99.01', 'Lucro Basico por Acao', 0)]
        else:
            dre += [('3.99.01.01', 'ON', '1.25745'), ('3.99.02.01', 'ON', '1.25745')]
        entries = {
            'DRE': dre,
            'BPA': [('1', 'Ativo Total', assets)],
            'BPP': [('2', 'Passivo Total', assets), ('2.01', 'Passivo Circulante', 271001),
                    ('2.02', 'Passivo Nao Circulante', assets - equity - 271001),
                    ('2.03', 'Patrimonio Liquido Consolidado' if scope == 'con' else
                     'Patrimonio Liquido', equity)],
            'DFC_MI': [('6.01', 'Caixa Liquido Atividades Operacionais', 135704 if scope == 'ind' else 518243),
                       ('6.02.01', 'Aquisicao de Imobilizado', -533 if scope == 'ind' else -2044)],
        }
        tables.update({f'_{table}_{scope}': _rows(scope, table, cells) for table, cells in entries.items()})
    return tables


def _archive(tables):
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        for suffix, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else native.COLUMNS,
                                    delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(f'dfp_cia_aberta{suffix}_2025.csv', stream.getvalue().encode('latin1'))
    return body.getvalue()


@pytest.fixture
def scoped_cache(tmp_path, monkeypatch):
    manifest = json.loads((ROOT / additional.RESEARCH_FILE).read_bytes())
    tables = _fixture_tables()
    review = manifest['scoped_readings'][0]
    review['dre_sha256'] = native._dre_fingerprint(tables['_DRE_ind'], full=True)
    review['consolidated_dre_sha256'] = native._dre_fingerprint(tables['_DRE_con'], full=True)
    proof = manifest['documents']['wiz_native_v1']
    pdf = b'%PDF-1.7\nfixture only\n%%EOF\n'
    proof.update(file='fixture.pdf', bytes=len(pdf), sha256=hashlib.sha256(pdf).hexdigest())
    path = tmp_path / additional.RESEARCH_FILE
    path.parent.mkdir(parents=True)
    (path.parent / proof['file']).write_bytes(pdf)

    def publish(*, refresh_dre=False):
        if refresh_dre:
            review['dre_sha256'] = native._dre_fingerprint(tables['_DRE_ind'], full=True)
            review['consolidated_dre_sha256'] = native._dre_fingerprint(tables['_DRE_con'], full=True)
        body = _archive(tables)
        archive_path = tmp_path / manifest['native_archive']['file']
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(body)
        manifest['native_archive']['sha256'] = hashlib.sha256(body).hexdigest()
        encoded = json.dumps(manifest, ensure_ascii=True).encode()
        path.write_bytes(encoded)
        monkeypatch.setattr(additional, 'RESEARCH_SHA256', hashlib.sha256(encoded).hexdigest())

    publish()
    return tmp_path, tables, manifest, publish


def _prepare(folder, *, candidates=None, requests=None, cutoff=CUTOFF):
    return additional.prepare_additional_financials(
        folder, {'WIZC3': SPEC} if candidates is None else candidates, cutoff,
        scope_requests={'WIZC3': 'individual'} if requests is None else requests)


def test_default_policy_has_no_implicit_individual_or_generic_manifest_admission(scoped_cache):
    folder, _, manifest, _ = scoped_cache
    before = native.prepare_financials(folder, {'WIZC3': SPEC}, CUTOFF)
    after = _prepare(folder, requests={})
    assert before == after
    assert manifest['readings'] == []
    assert financial_features(after[0], [], CUTOFF, SPEC) is None


def test_explicit_whole_individual_scope_produces_real_contract_and_keeps_fcf_null(scoped_cache, monkeypatch):
    folder, _, manifest, _ = scoped_cache
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network forbidden'))
    before = {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    statements, diagnostics, sources = _prepare(folder)
    features, record = financial_features(statements, [], CUTOFF, SPEC)
    assert features == pytest.approx([0, 0, 1.25745, None, 0, 688049 / 1385841, 2], nan_ok=False)
    assert features[5] != pytest.approx((2434061 - 1296137) / 2434061)
    assert record['accounts_scope'] == 'individual' and record['status'] == 'included'
    assert record['filing_id'] == '155497' and record['version'] == '1'
    assert record['profit_per_share'] == 1.25745 and record['free_cash_per_share'] is None
    method = record['financial_method']
    assert method['id'] == 'financial_no_industrial_fcf_v1'
    assert method['profit_account'] == '3.11' and method['basic_eps_account'] == '3.99.01.01'
    assert method['native_consolidated_core_present'] is True
    assert method['scope_selection'] == 'explicit_reviewed_whole_individual_scope'
    assert all('_ind_' in name for name in method['source_statements'])
    assert method['reported_basic_eps'] == manifest['scoped_readings'][0]['basic_eps']
    assert method['documentary_reading']['native_archive_sha256'] == manifest['native_archive']['sha256']
    assert any(row['kind'] == 'candidate' and row['status'] == 'included' for row in diagnostics)
    assert any(row['authority'] == 'primary' and row['file'].endswith('fixture.pdf') for row in sources)
    assert before == {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    json.dumps((statements, diagnostics, sources), allow_nan=False)


@pytest.mark.parametrize('spec', [(23590, 'PN', 2), (23590, 'PNA', 2), (23590, 'ON', 4), (1023, 'ON', 2)])
def test_wrong_class_sector_or_cvm_cannot_borrow_wiz_proof(scoped_cache, spec):
    folder, _, _, _ = scoped_cache
    statements, diagnostics, _ = _prepare(folder, candidates={'WIZC3': spec})
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert any(row.get('detail') == 'exact_candidate_identity_required' for row in diagnostics)


@pytest.mark.parametrize('ticker,scope', [('WIZC3', 'consolidated'), ('PINE4', 'individual'), ('BBAS3', 'individual')])
def test_no_scope_request_can_authorize_consolidated_eps_or_unreviewed_issuers(scoped_cache, ticker, scope):
    folder, _, _, _ = scoped_cache
    statements, diagnostics, _ = _prepare(folder, requests={ticker: scope})
    assert financial_features(statements, [], CUTOFF, SPEC) is None
    assert any(row.get('detail') == 'unreviewed_scope_request' for row in diagnostics)


@pytest.mark.parametrize('fault', ['manifest', 'pdf', 'archive', 'missing_pdf', 'missing_manifest'])
def test_changed_or_missing_immutable_primary_proof_fails_closed(scoped_cache, fault):
    folder, _, manifest, _ = scoped_cache
    path = (folder / additional.RESEARCH_FILE if 'manifest' in fault else
            folder / manifest['native_archive']['file'] if fault == 'archive' else
            folder / Path(additional.RESEARCH_FILE).parent / 'fixture.pdf')
    if fault.startswith('missing'):
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b'changed')
    statements, diagnostics, _ = _prepare(folder)
    assert financial_features(statements, [], CUTOFF, SPEC) is None
    assert any(row['kind'] in ('source', 'additional_scope') and row['status'] == 'excluded'
               for row in diagnostics)


@pytest.mark.parametrize('fault', ['scope', 'id', 'version', 'cnpj', 'published', 'dre', 'con_dre'])
def test_review_never_escapes_exact_latest_native_identity_or_scope(scoped_cache, fault):
    folder, _, manifest, publish = scoped_cache
    review = manifest['scoped_readings'][0]
    field, value = {
        'scope': ('eps_scope', 'consolidated'), 'id': ('filing_id', 'other'),
        'version': ('version', '2'), 'cnpj': ('cnpj', '00.000.000/0001-91'),
        'published': ('published_at', '2026-03-18'), 'dre': ('dre_sha256', '0' * 64),
        'con_dre': ('consolidated_dre_sha256', '0' * 64),
    }[fault]
    review[field] = value
    publish()
    statements, _, _ = _prepare(folder)
    assert financial_features(statements, [], CUTOFF, SPEC) is None


@pytest.mark.parametrize('fault', ['denominator', 'numerator', 'nci', 'diluted', 'nonannual', 'missing_assets', 'balance'])
def test_no_repair_by_inventing_eps_periods_roots_or_partial_scope(scoped_cache, fault):
    folder, tables, manifest, publish = scoped_cache
    entry = manifest['scoped_readings'][0]['basic_eps'][0]
    if fault == 'denominator':
        entry['weighted_shares'] = '1'
    elif fault == 'numerator':
        entry['numerator_brl'] = '367710000'
    elif fault == 'nci':
        tables['_DRE_con'][2]['VL_CONTA'] = '1'
    elif fault == 'diluted':
        entry['account'] = '3.99.02.01'
    elif fault == 'nonannual':
        for row in tables['_DRE_ind']:
            row['DT_INI_EXERC'] = '2025-12-01'
    elif fault == 'missing_assets':
        tables['_BPA_ind'] = []
    else:
        tables['_BPP_ind'][0]['VL_CONTA'] = '2434061'
    publish(refresh_dre=True)
    statements, _, _ = _prepare(folder)
    assert financial_features(statements, [], CUTOFF, SPEC) is None


def test_unreviewed_later_revision_retains_null_instead_of_reusing_valid_v1(scoped_cache):
    folder, tables, _, publish = scoped_cache
    latest = deepcopy(tables[''][0])
    latest.update(VERSAO='2', ID_DOC='199999', DT_RECEB='2026-08-01')
    tables[''].append(latest)
    publish()
    statements, diagnostics, _ = _prepare(folder)
    assert financial_features(statements, [], CUTOFF, SPEC) is None
    latest_record = max(statements, key=lambda row: int(row['version']))
    assert latest_record['version'] == '2' and latest_record['profit_per_share'] is None
    assert any(row.get('detail') == 'review_is_not_latest_native_filing' for row in diagnostics)
    earlier, _, _ = _prepare(folder, cutoff=date(2026, 7, 31))
    assert financial_features(earlier, [], date(2026, 7, 31), SPEC)[0][2] == 1.25745


def test_availability_starts_at_exact_native_publication(scoped_cache):
    folder, _, _, _ = scoped_cache
    before, _, _ = _prepare(folder, cutoff=date(2026, 3, 18))
    current, _, _ = _prepare(folder, cutoff=date(2026, 3, 19))
    assert financial_features(before, [], date(2026, 3, 18), SPEC) is None
    assert financial_features(current, [], date(2026, 3, 19), SPEC)[0][2] == 1.25745


def test_explicit_scope_does_not_change_an_unrelated_available_candidate(scoped_cache):
    folder, tables, _, publish = scoped_cache
    other = deepcopy(tables[''][0])
    other.update(CD_CVM='12345', ID_DOC='other')
    tables[''].append(other)
    for suffix in list(tables):
        if not suffix:
            continue
        extra = [{**row, 'CD_CVM': '12345'} for row in tables[suffix]]
        if suffix == '_DRE_con':
            extra.append({**extra[0], 'CD_CONTA': '3.99.01.01', 'DS_CONTA': 'ON', 'VL_CONTA': '2'})
        tables[suffix].extend(extra)
    publish()
    specs = {'WIZC3': SPEC, 'OTHER3': (12345, 'ON', 2)}
    before, _, _ = _prepare(folder, candidates=specs, requests={})
    after, _, _ = _prepare(folder, candidates=specs)
    assert financial_features(before, [], CUTOFF, specs['OTHER3']) is not None
    assert [row for row in before if row['cvm'] == 12345] == [row for row in after if row['cvm'] == 12345]
    assert financial_features(after, [], CUTOFF, SPEC) is not None


def test_real_pinned_review_and_exact_native_scope_admissions(monkeypatch):
    from . import bbas_financial_research as bbas
    from . import remaining_financial_research as sanb
    # Keep the historical v5 absence audit separate from later filing recoveries.
    monkeypatch.setattr(bbas, 'RESEARCH_FILE', bbas.RESEARCH_DIR + '/not-installed-fixture.json')
    monkeypatch.setattr(sanb, 'RESEARCH_FILE', sanb.RESEARCH_DIR + '/not-installed-fixture.json')
    manifest = additional.read_review(ROOT)
    assert manifest['readings'] == [] and len(manifest['findings']) == 8
    masters, dres = {}, {}
    for year, filename in [(2025, manifest['native_archive']['file']), (2024, 'raw/cvm-dfp-2024.zip')]:
        with zipfile.ZipFile(ROOT / filename) as archive:
            masters[year] = list(native._rows(archive, f'dfp_cia_aberta_{year}.csv'))
            dres[year] = list(native._rows(archive, f'dfp_cia_aberta_DRE_con_{year}.csv'))
    for finding in manifest['findings']:
        year = int(finding['fiscal_end'][:4])
        matches = [row for rows in masters.values() for row in rows
                   if int(row['CD_CVM']) == finding['cvm'] and row['DT_RECEB'][:10] <= '2026-09-30']
        latest = max(matches, key=lambda row: (row['DT_REFER'], int(row['VERSAO'])))
        assert (latest['DT_REFER'], latest['VERSAO'], latest['ID_DOC'], latest['DT_RECEB'][:10]) == tuple(
            finding[key] for key in ('fiscal_end', 'version', 'filing_id', 'published_at'))
        current = [row for row in dres[year] if int(row['CD_CVM']) == finding['cvm']
                   and row['VERSAO'] == finding['version'] and row['ORDEM_EXERC'] == '\u00daLTIMO']
        assert native._dre_fingerprint(current, full=True) == finding['full_dre_sha256']
    specs = {row['ticker']: (row['cvm'], row['share_class'], row['sector_code'])
             for row in manifest['findings']}
    specs['PINE4'] = (20567, 'PN', 2)
    default, _, _ = _prepare(ROOT, candidates=specs, requests={})
    assert all(financial_features(default, [], CUTOFF, spec) is None for spec in specs.values())
    scoped, diagnostics, _ = _prepare(ROOT, candidates=specs)
    available = {ticker for ticker, spec in specs.items()
                 if financial_features(scoped, [], CUTOFF, spec) is not None}
    assert available == {'WIZC3'}
    features, record = financial_features(scoped, [], CUTOFF, SPEC)
    assert features[2] == 1.25745 and features[3] is None
    assert features[5] == pytest.approx(688049 / 1385841)
    assert record['accounts_scope'] == 'individual' and record['filing_id'] == '155497'
    assert any(row['kind'] == 'candidate' and row.get('ticker') == 'PINE4'
               and row['status'] == 'excluded' for row in diagnostics)
