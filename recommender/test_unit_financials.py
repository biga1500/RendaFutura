from copy import deepcopy
from datetime import date
import csv
import hashlib
import io
import json
import urllib.request
import zipfile

import numpy as np
import pytest
from xgboost import XGBRegressor

from .collect_market_data import financial_features, stock_observations
from .contracts import validate_features
from .datasets import FEATURES
from .forecast import CashForecast
from .inventory_candidates import _Cache
from .inventory_financials import _dre_fingerprint, prepare_financials
from .test_inventory_financials import annual, cache
from .test_unit_candidates import CUTOFF, MARKET, cached_inventory, candidate, unit_fixture, write
from .unit_candidates import build_unit_candidate
from . import unit_financials as financials
from .unit_financials import _compose, _eps_research, prepare_unit_financials


def financial_fixture(folder):
    inventory = unit_fixture(folder)
    cache(folder, annual(cvm=123))
    return candidate(folder, inventory)


def bank_annual(*, year=2025, version=1, published='2026-03-01'):
    """Synthetic native cells with reconciled parent/NCI and bank balance roots."""
    data = annual(cvm=123, year=year, version=version, published=published)
    template = data['tables']['DRE_con'][0]
    data['tables']['DRE_con'].append({**template, 'CD_CONTA': '3.11.02',
        'DS_CONTA': 'Atribuido a Socios Nao Controladores', 'VL_CONTA': 10})
    data['tables']['BPP_con'] = [{**template, 'CD_CONTA': code, 'DS_CONTA': description, 'VL_CONTA': value}
        for code, description, value in (
            ('2', 'Passivo Total', 1000),
            ('2.01', 'Passivos Financeiros ao Valor Justo', 100),
            ('2.02', 'Passivos Financeiros ao Custo Amortizado', 200),
            ('2.07', 'Patrimonio Liquido Consolidado', 150),
            ('2.07.01', 'Patrimonio Liquido Atribuivel aos Controladores', 140),
            ('2.07.02', 'Participacao dos Nao Controladores', 10))]
    data['tables']['DFC_MI_con'] = []
    return data


def bank_fixture(folder, *, historical=False):
    inventory = unit_fixture(folder)
    inventory['stocks'][0]['sector_code'] = 2
    cache(folder, bank_annual())
    if historical:
        cache(folder, bank_annual(year=2024, published='2025-03-01'), year=2024)
    unit = candidate(folder, inventory)
    specs = {'TEST11:component:0': (123, 'ON', 2), 'TEST11:component:1': (123, 'PN', 2)}
    records, _, _ = prepare_financials(folder, specs, CUTOFF)
    return unit, records


def unit_diagnostic(diagnostics):
    return next(row for row in diagnostics if row['kind'] == 'candidate' and row.get('share_class') == 'UNIT')


def test_exact_classes_produce_actual_model_features_with_original_issuer_ratio(tmp_path, monkeypatch):
    unit = financial_fixture(tmp_path)
    before = deepcopy(unit)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('financial adapter fetched'))
    statements, diagnostics, sources = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert unit_diagnostic(diagnostics)['status'] == 'included'
    assert len(statements) == 1
    record = statements[0]
    assert record['profit_per_share'] == pytest.approx(2 + 2 * 4)
    assert record['free_cash_per_share'] == pytest.approx(1.2 + 2 * 2.4)
    assert record['debt_ratio'] == .5
    features, filing = financial_features(statements, unit['payments'], CUTOFF, unit['statement_spec'])
    assert features[2:4] == pytest.approx([10, 6])
    assert features[-2:] == [.5, 1]
    assert filing['filing_id'] == record['filing_id']
    assert unit['unit_composition'] == before['unit_composition']
    assert unit['share_actions'] == before['share_actions']
    assert all(source['file'].startswith('raw/') for source in sources)
    json.dumps((statements, diagnostics, sources), allow_nan=False)


def test_latest_missing_class_blocks_older_complete_filing(tmp_path):
    unit = financial_fixture(tmp_path)
    bad = annual(cvm=123, version=2, published='2026-08-01')
    bad['tables']['DRE_con'] = [row for row in bad['tables']['DRE_con'] if row['DS_CONTA'] != 'PN']
    cache(tmp_path, annual(cvm=123), bad)
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert len(statements) == 2
    assert statements[0]['profit_per_share'] == 10
    assert statements[-1]['profit_per_share'] is None
    assert unit_diagnostic(diagnostics)['status'] == 'excluded'
    assert financial_features(statements, unit['payments'], CUTOFF, unit['statement_spec']) is None


def class_records(folder):
    records, _, _ = prepare_financials(folder, {'a': (123, 'ON', 1), 'b': (123, 'PN', 1)}, CUTOFF)
    return records


@pytest.mark.parametrize('field,value', [
    ('filing_id', 'different-filing'), ('version', '2'), ('fiscal_end', '2024-12-31'),
    ('published_at', '2026-08-01'), ('accounts_scope', 'individual'), ('source_ref', 'different-archive'),
    ('period_type', 'ytd'), ('annualized', True),
])
def test_component_filings_cannot_be_mixed(tmp_path, field, value):
    unit = financial_fixture(tmp_path)
    records = class_records(tmp_path)
    records[1][field] = value
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['profit_per_share'] is None and result['free_cash_per_share'] is None
    assert 'unit_incompatible_component_filings_or_periods' in result['unavailable_reasons']


def test_both_per_share_flows_normalize_and_issuer_debt_ratio_does_not(tmp_path):
    unit = financial_fixture(tmp_path)
    unit['share_actions'] = [{'date': '2026-04-01', 'ratio': 2.}]
    result = _compose(unit, unit['unit_composition'], class_records(tmp_path), CUTOFF)
    assert result['profit_per_share'] == 5
    assert result['free_cash_per_share'] == 3
    assert result['debt_ratio'] == .5
    assert result['financial_method']['post_period_share_factor'] == 2


def test_in_period_weighted_average_denominator_cannot_be_guessed(tmp_path):
    unit = financial_fixture(tmp_path)
    unit['share_actions'] = [{'date': '2025-04-01', 'ratio': 1.1}]
    result = _compose(unit, unit['unit_composition'], class_records(tmp_path), CUTOFF)
    assert result['profit_per_share'] is None
    assert result['unavailable_reasons'] == ['unit_in_period_weighted_share_denominator_unverified']


def eps_review_fixture(folder, monkeypatch, **changes):
    """Invented test-only disclosure; none of these bytes are production proof."""
    unit = financial_fixture(folder)
    row = annual(cvm=123)
    next(cell for cell in row['tables']['DRE_con'] if cell['DS_CONTA'] == 'PN')['VL_CONTA'] = 2
    source = cache(folder, row)
    # The generic financial fixture omits the issuer identifier required by a
    # documentary binding, so add it to this test's synthetic master table.
    output = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, 'w') as enriched:
        for member in original.namelist():
            body = original.read(member)
            if member == 'dfp_cia_aberta_2025.csv':
                masters = list(csv.DictReader(io.StringIO(body.decode('latin1')), delimiter=';'))
                for master in masters:
                    master['CNPJ_CIA'] = '00.000.000/0001-00'
                stream = io.StringIO()
                writer = csv.DictWriter(stream, fieldnames=list(masters[0]), delimiter=';')
                writer.writeheader()
                writer.writerows(masters)
                body = stream.getvalue().encode('latin1')
            enriched.writestr(member, body)
    source.write_bytes(output.getvalue())
    unit['share_actions'] = [{'date': '2025-04-01', 'ratio': 1.1}]
    with zipfile.ZipFile(source) as archive:
        with archive.open('dfp_cia_aberta_DRE_con_2025.csv') as stream:
            cells = list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
    pdf = b'%PDF-SYNTHETIC-TEST-ONLY common annual class denominator'
    pdf_file = 'raw/primary-research/units/test-only-dfp.pdf'
    pdf_url = 'https://example.invalid/SYNTHETIC-TEST-ONLY.pdf'
    write(folder, pdf_file, pdf)
    catalog = json.dumps({'url': pdf_url}).encode()
    catalog_file = 'raw/primary-research/units/test-only-catalog.json'
    write(folder, catalog_file, catalog)
    reading = {'cvm': 123, 'cnpj': '00.000.000/0001-00', 'fiscal_end': '2025-12-31', 'version': '1',
        'filing_id': '123-2025-12-31-1', 'published_at': '2026-03-01',
        'accounts_scope': 'consolidated', 'document': 'test', 'pdf_page': 1,
        'denominator': 'one_class_share_annual_weighted_average',
        'dre_sha256': _dre_fingerprint(cells), 'profit_brl': 100000,
        'weighted_average_shares': {'ON': 30000, 'PN': 20000}, 'weighted_average_quantum': 1,
        'note_basic_eps': '2.00', 'classes': {
            'ON': {'account': '3.99.01.01', 'reported_eps': '2', 'decimal_places': 2},
            'PN': {'account': '3.99.01.02', 'reported_eps': '2', 'decimal_places': 2}},
        'in_period_actions': unit['share_actions'], **changes}
    manifest = {'schema_version': 1, 'readings': [reading], 'structured_source': {
        'file': source.relative_to(folder).as_posix(), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'url': 'https://example.invalid/SYNTHETIC-TEST-ONLY.zip'},
        'documents': {'test': {'file': pdf_file, 'sha256': hashlib.sha256(pdf).hexdigest(), 'url': pdf_url,
            'catalog_file': catalog_file, 'catalog_sha256': hashlib.sha256(catalog).hexdigest(),
            'catalog_url': 'https://example.invalid/SYNTHETIC-TEST-ONLY-catalog'}}}
    body = json.dumps(manifest).encode()
    write(folder, financials.EPS_RESEARCH_FILE, body)
    monkeypatch.setattr(financials, 'EPS_RESEARCH_SHA256', hashlib.sha256(body).hexdigest())
    return unit


def reviewed(folder, unit):
    audit = []
    reviews = _eps_research(_Cache(folder, {'stocks': [unit]}), [(unit, unit['unit_composition'])], CUTOFF, audit)
    return next(iter(reviews.values()), None), audit


def test_verified_annual_class_basis_preserves_reported_eps_and_existing_fcf(tmp_path, monkeypatch):
    unit = eps_review_fixture(tmp_path, monkeypatch)
    proof, audit = reviewed(tmp_path, unit)
    assert proof and not audit
    records = class_records(tmp_path)
    before = deepcopy(records)
    record = _compose(unit, unit['unit_composition'], records, CUTOFF, denominator_proof=proof)
    assert record['status'] == 'included'
    assert record['profit_per_share'] == 6
    assert record['free_cash_per_share'] == pytest.approx(3.6)
    assert records == before
    assert record['financial_method']['denominator'] == 'reported_basic_class_share_common_annual_weighted_average'
    assert record['financial_method']['basic_EPS_denominator_evidence']['point_in_time_archive_verified'] is False


@pytest.mark.parametrize('changes,reason', [
    ({'filing_id': 'OTHER'}, 'unit_EPS_review_filing_identity_mismatch'),
    ({'version': '2'}, 'unit_EPS_review_filing_identity_mismatch'),
    ({'dre_sha256': '0' * 64}, 'unit_EPS_review_DRE_mismatch'),
    ({'denominator': 'one_UNIT'}, 'unit_EPS_review_denominator_unverified'),
    ({'accounts_scope': 'individual'}, 'unit_EPS_review_denominator_unverified'),
    ({'profit_brl': 200000}, 'unit_EPS_review_profit_mismatch'),
    ({'weighted_average_shares': {'ON': 10000, 'PN': 10000}}, 'unit_EPS_note_denominator_disagrees'),
    ({'note_basic_eps': '1.50'}, 'unit_EPS_note_denominator_disagrees'),
])
def test_review_cannot_override_filing_scope_numerator_or_share_units(tmp_path, monkeypatch, changes, reason):
    unit = eps_review_fixture(tmp_path, monkeypatch, **changes)
    proof, audit = reviewed(tmp_path, unit)
    assert proof is None and any(reason in row['reasons'] for row in audit)
    result = _compose(unit, unit['unit_composition'], class_records(tmp_path), CUTOFF, denominator_proof=proof)
    assert result['profit_per_share'] is None


@pytest.mark.parametrize('target,reason', [
    ('manifest', 'unit_EPS_review_manifest_hash_mismatch'),
    ('PDF', 'unit_EPS_primary_document_hash_mismatch'),
    ('archive', 'unit_EPS_structured_source_hash_mismatch'),
])
def test_immutable_eps_proofs_fail_closed_on_changed_bytes(tmp_path, monkeypatch, target, reason):
    unit = eps_review_fixture(tmp_path, monkeypatch)
    path = (tmp_path / financials.EPS_RESEARCH_FILE if target == 'manifest' else
            tmp_path / 'raw/primary-research/units/test-only-dfp.pdf' if target == 'PDF' else
            next((tmp_path / 'raw').glob('cvm-dfp-*.zip')))
    path.write_bytes(path.read_bytes() + b'changed')
    proof, audit = reviewed(tmp_path, unit)
    assert proof is None and any(reason in row['reasons'] for row in audit)


def test_review_does_not_cover_new_bonus_events_or_future_publication(tmp_path, monkeypatch):
    unit = eps_review_fixture(tmp_path, monkeypatch)
    proof, _ = reviewed(tmp_path, unit)
    unit['share_actions'][0]['ratio'] = 1.2
    result = _compose(unit, unit['unit_composition'], class_records(tmp_path), CUTOFF, denominator_proof=proof)
    assert result['profit_per_share'] is None
    unit = eps_review_fixture(tmp_path, monkeypatch, published_at='2026-10-01')
    proof, audit = reviewed(tmp_path, unit)
    assert proof is None and not audit


def test_common_denominator_review_cannot_infer_unequal_class_profit_allocation(tmp_path, monkeypatch):
    classes = {'ON': {'account': '3.99.01.01', 'reported_eps': '2', 'decimal_places': 2},
               'PN': {'account': '3.99.01.02', 'reported_eps': '2.001', 'decimal_places': 2}}
    unit = eps_review_fixture(tmp_path, monkeypatch, classes=classes)
    proof, audit = reviewed(tmp_path, unit)
    assert proof is None
    assert any('unit_EPS_class_profit_allocation_not_common' in row['reasons'] for row in audit)


def test_financial_flow_refuses_injected_actions_and_missing_normalization_proof(tmp_path):
    unit = financial_fixture(tmp_path)
    unit['share_actions'] = [{'date': '2026-04-01', 'ratio': 2.}]
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert not statements
    assert unit_diagnostic(diagnostics)['reasons'] == ['unit_financial_actions_disagree_with_cached_unit_history']
    unit['share_actions'] = []
    unit['share_normalization'].pop('share_basis_date')
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert statements[0]['profit_per_share'] is None
    assert 'unit_component_denominator_normalization_unverified' in unit_diagnostic(diagnostics)['reasons']


def test_financial_issuer_missing_comparable_never_manufactures_industrial_fcf(tmp_path):
    unit = financial_fixture(tmp_path)
    unit['sector_code'] = 2
    # Keep this negative fixture explicitly without the bank roots, independent
    # of the generic annual fixture's now-populated consolidated balance sheet.
    missing_roots = annual(cvm=123)
    missing_roots['tables']['BPP_con'] = [row for row in missing_roots['tables']['BPP_con']
                                          if row['CD_CONTA'] in ('2.01', '2.02')]
    cache(tmp_path, missing_roots)
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert statements[0]['free_cash_per_share'] is None
    assert statements[0]['profit_per_share'] is None
    assert unit_diagnostic(diagnostics)['status'] == 'excluded'
    assert 'unit_component_annual_accounts_unavailable' in unit_diagnostic(diagnostics)['reasons']
    assert any('financial_root_balance_accounts_required' in row.get('reasons', [])
               for row in diagnostics if row.get('unit_component_diagnostic'))


def test_exact_native_bank_components_compose_eps_with_null_fcf_and_issuer_debt(tmp_path, monkeypatch):
    unit, components = bank_fixture(tmp_path)
    before = deepcopy((unit, components))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('bank UNIT fetched'))
    assert {row['financial_method']['id'] for row in components} == {'financial_no_industrial_fcf_v1'}
    assert all(row['status'] == 'included' and row['free_cash_per_share'] is None for row in components)
    statements, diagnostics, sources = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    record, = statements
    assert unit_diagnostic(diagnostics)['status'] == record['status'] == 'included'
    assert record['profit_per_share'] == 10  # 1 deposited ON * 2 + 2 PN * 4.
    assert record['free_cash_per_share'] is None
    assert record['debt_ratio'] == .85  # Issuer balance ratio, never deposit-weighted.
    assert record['accounts_scope'] == 'consolidated'
    method = record['financial_method']
    assert method['FCF_method'] == 'not_applicable_to_financial_issuer'
    assert method['fcf_absence_reason'] == 'industrial_fcf_not_applicable_to_financial_issuer'
    assert method['basic_EPS_source'] == 'derived_sum_of_reported_class_basic_EPS_not_reported_UNIT_EPS'
    assert (unit, components) == before
    features, _ = financial_features(statements, unit['payments'], CUTOFF, (123, 'UNIT', 2))
    assert len(features) == 7 and features[2:4] == [10, None] and features[5:] == [.85, 2.]
    assert validate_features(features) == features
    json.dumps((statements, diagnostics, sources), allow_nan=False)


def test_bank_null_fcf_reaches_stock_observations_and_xgboost_without_zero_imputation(tmp_path):
    unit, _ = bank_fixture(tmp_path, historical=True)
    statements, _, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    rows, coverage = stock_observations(unit['ticker'], unit['prices'], unit['payments'],
                                        statements, CUTOFF, (123, 'UNIT', 2))
    assert rows and coverage['included_windows'] == len(rows)
    assert all(row['free_cash_per_share'] is None and row['profit_per_share'] == 10 for row in rows)
    features = np.asarray([[row[key] for key in FEATURES] for row in rows], dtype=float)
    targets = np.asarray([row['target'] for row in rows], dtype=float)
    transformed, _ = CashForecast.transform(features)
    assert np.isnan(features[:, 3]).all() and np.isnan(transformed[:, 3]).all()
    model = CashForecast(XGBRegressor(n_estimators=3, max_depth=1, n_jobs=1, random_state=41))
    model.fit(features, targets)
    assert np.isfinite(model.predict(features)).all()


@pytest.mark.parametrize('value', [0, 1, -1, False, True, float('nan'), float('inf'), 'None'])
def test_bank_fcf_must_be_explicit_null_not_zero_finite_nan_or_text(tmp_path, value):
    unit, records = bank_fixture(tmp_path)
    records[1]['free_cash_per_share'] = value
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['profit_per_share'] is None and result['status'] == 'excluded'
    assert 'unit_financial_FCF_must_be_not_applicable' in result['unavailable_reasons']


def test_bank_missing_fcf_field_is_not_inferred_as_not_applicable(tmp_path):
    unit, records = bank_fixture(tmp_path)
    del records[1]['free_cash_per_share']
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None


@pytest.mark.parametrize('problem', [
    'sector', 'sector_bool', 'sector_float', 'mixed_method', 'unknown_method',
    'issuer', 'class', 'partial_classes', 'individual_scope', 'bank_roots',
    'different_profit_root', 'different_equity_root', 'wrong_liabilities_method',
    'missing_basic_account', 'diluted_account', 'unavailable_component',
])
def test_bank_eligibility_rejects_wrong_identity_scope_and_method_contract(tmp_path, problem):
    unit, records = bank_fixture(tmp_path)
    method = records[1]['financial_method']
    if problem in ('sector', 'sector_bool', 'sector_float'):
        unit['sector_code'] = {'sector': 1, 'sector_bool': True, 'sector_float': 2.}[problem]
    elif problem in ('mixed_method', 'unknown_method'):
        method['id'] = 'industrial_cash_capex_v1' if problem == 'mixed_method' else 'unknown'
    elif problem == 'issuer':
        for row in records:
            row['cvm'] = 456
    elif problem == 'class':
        records[1]['share_class'] = 'ON'
    elif problem == 'partial_classes':
        records.pop()
    elif problem == 'individual_scope':
        for row in records:
            row['accounts_scope'] = row['financial_method']['accounts_scope'] = 'individual'
    elif problem == 'bank_roots':
        for row in records:
            row['financial_method'].pop('equity_account')
    elif problem == 'different_profit_root':
        method['profit_account'] = '3.09.01'
    elif problem == 'different_equity_root':
        method['equity_account'] = '2.08'
        method['liabilities_accounts'] = ['2', '2.08']
    elif problem == 'wrong_liabilities_method':
        method['liabilities_method'] = 'current_plus_noncurrent'
    elif problem == 'missing_basic_account':
        method['basic_eps_account'] = None
    elif problem == 'diluted_account':
        method['basic_eps_account'] = '3.99.02.01'
    else:
        records[1]['status'] = 'excluded'
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None
    assert result['free_cash_per_share'] is None


@pytest.mark.parametrize('method', [None, {}, {'id': ['financial_no_industrial_fcf_v1']},
                                   {'id': 'unknown_method'}])
def test_malformed_or_unknown_component_methods_fail_closed(tmp_path, method):
    unit, records = bank_fixture(tmp_path)
    for record in records:
        record['financial_method'] = deepcopy(method)
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None
    assert 'unit_required_comparable_FCF_unavailable' in result['unavailable_reasons']


def test_financial_sector_cannot_accept_industrial_components_even_with_finite_fcf(tmp_path):
    unit = financial_fixture(tmp_path)
    records = class_records(tmp_path)
    unit['sector_code'] = 2
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None
    assert 'unit_financial_sector_requires_financial_component_method' in result['unavailable_reasons']


@pytest.mark.parametrize('field,value', [
    ('filing_id', 'different'), ('source_ref', 'different'), ('source_file', 'different'),
    ('version', '2'), ('published_at', '2026-08-01'), ('period_type', 'quarterly'),
    ('annualized', True), ('debt_ratio', .5),
])
def test_bank_components_must_keep_same_annual_filing_and_issuer_debt(tmp_path, field, value):
    unit, records = bank_fixture(tmp_path)
    records[1][field] = value
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None


@pytest.mark.parametrize('value', [None, 0, float('nan'), float('inf')])
def test_bank_missing_or_invalid_component_eps_cannot_be_hidden_by_other_class(tmp_path, value):
    unit, records = bank_fixture(tmp_path)
    records[1]['profit_per_share'] = value
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['status'] == 'excluded' and result['profit_per_share'] is None
    assert 'unit_component_basic_eps_unavailable' in result['unavailable_reasons']


def test_bank_post_period_factor_changes_eps_only_and_in_period_proof_is_still_required(tmp_path):
    unit, records = bank_fixture(tmp_path)
    before = deepcopy(records)
    unit['share_actions'] = [{'date': '2026-04-01', 'ratio': 2.}]
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['profit_per_share'] == 5 and result['free_cash_per_share'] is None
    assert result['debt_ratio'] == .85 and result['financial_method']['post_period_share_factor'] == 2
    unit['share_actions'] = [{'date': '2025-04-01', 'ratio': 1.1}]
    result = _compose(unit, unit['unit_composition'], records, CUTOFF)
    assert result['profit_per_share'] is None
    assert 'unit_in_period_weighted_share_denominator_unverified' in result['unavailable_reasons']
    assert records == before


def test_bank_latest_partial_filing_still_prevents_older_complete_fallback(tmp_path):
    unit, _ = bank_fixture(tmp_path)
    latest = bank_annual(version=2, published='2026-08-01')
    latest['tables']['DRE_con'] = [row for row in latest['tables']['DRE_con'] if row['DS_CONTA'] != 'PN']
    cache(tmp_path, bank_annual(), latest)
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert statements[0]['profit_per_share'] == 10
    assert statements[-1]['profit_per_share'] is None
    assert unit_diagnostic(diagnostics)['status'] == 'excluded'
    assert financial_features(statements, unit['payments'], CUTOFF, (123, 'UNIT', 2)) is None


def test_bank_features_keep_publication_and_annual_age_gates(tmp_path):
    unit, _ = bank_fixture(tmp_path)
    statements, _, _ = prepare_unit_financials(tmp_path, [unit], CUTOFF)
    assert financial_features(statements, [], date(2026, 2, 28), (123, 'UNIT', 2)) is None
    assert financial_features(statements, [], date(2026, 3, 1), (123, 'UNIT', 2)) is not None
    assert financial_features(statements, [], date(2028, 1, 1), (123, 'UNIT', 2)) is None


def test_other_share_classes_are_not_reinterpreted_and_duplicate_units_are_pending(tmp_path):
    unit = financial_fixture(tmp_path)
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [{**unit, 'share_class': 'ON'}], CUTOFF)
    assert not statements and not [row for row in diagnostics if row['kind'] == 'candidate']
    statements, diagnostics, _ = prepare_unit_financials(tmp_path, [unit, deepcopy(unit)], CUTOFF)
    assert not statements
    assert all(row['reasons'] == ['unit_duplicate_candidate_identity'] for row in diagnostics if row['kind'] == 'candidate')


def test_actual_cached_financial_features_and_training_rows(monkeypatch):
    inventory = cached_inventory()
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cache audit fetched'))
    source_cache = _Cache(MARKET, inventory)
    recovered = [build_unit_candidate(row, source_cache, CUTOFF) for row in inventory['stocks']
                 if row['ticker'] in ('ALUP11', 'ENGI11', 'SAPR11', 'TAEE11')]
    statements, diagnostics, sources = prepare_unit_financials(MARKET, recovered, CUTOFF)
    verdicts = {row['ticker']: row for row in diagnostics
                if row['kind'] == 'candidate' and row.get('share_class') == 'UNIT'}
    assert {ticker for ticker, row in verdicts.items() if row['status'] == 'included'} == {
        'ALUP11', 'ENGI11', 'SAPR11', 'TAEE11'}
    observations = {}
    for unit in recovered:
        rows, _ = stock_observations(unit['ticker'], unit['prices'], unit['payments'], statements,
                                     CUTOFF, unit['statement_spec'])
        observations[unit['ticker']] = len(rows)
        available = financial_features(statements, unit['payments'], CUTOFF, unit['statement_spec'])
        assert available is not None
        if unit['ticker'] in ('ALUP11', 'ENGI11'):
            method = available[1]['financial_method']
            proof = method['basic_EPS_denominator_evidence']
            assert proof['manifest_sha256'] == financials.EPS_RESEARCH_SHA256
            assert available[0][2] == pytest.approx(3.7293372048 if unit['ticker'] == 'ALUP11' else 4.75)
            assert available[0][3] == pytest.approx(5.8790983408305655 if unit['ticker'] == 'ALUP11' else .903895523877605)
            current_basis_rows = [row for row in rows if row['fiscal_end'] >= unit['financial_basis_start']
                                  and date.fromisoformat(row['as_of']) >= unit['training_since']]
            assert not current_basis_rows
    assert observations == {'ALUP11': 18, 'ENGI11': 31, 'SAPR11': 37, 'TAEE11': 37}
    assert verdicts['ALUP11']['reasons'] == verdicts['ENGI11']['reasons'] == []
    assert len(statements) == 67
    json.dumps((statements, diagnostics, sources), allow_nan=False)
