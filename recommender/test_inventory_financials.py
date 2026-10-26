from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
import json
import urllib.request
import zipfile

import pytest

from .collect_market_data import DividendHistory, financial_features, stock_observations
from .inventory_financials import _annual_start, prepare_financials
from . import inventory_financials as financials
from .test_cvm_fundamentals import archive, filing


CUTOFF = date(2026, 9, 30)
SPECS = {'TEST3': (12345, 'ON', 0), 'TEST4': (12345, 'PN', 0)}


def annual(*, year=2025, published='2026-03-01', version=1, cvm=12345, scope='con'):
    return filing(fiscal=f'{year}-12-31', published=published, version=version,
                  cvm_id=cvm, scope=scope, start=f'{year}-01-01')


def cache(folder, *filings, year=2025, official=False, form='dfp', compact=False):
    name, content = next(iter(archive(*filings, form=form, year=year).items()))
    path = (folder / 'raw/official-financials' / name if official else
            folder / 'raw' / (f'cvm-{form}{year}.zip' if compact else f'cvm-{form}-{year}.zip'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def change_eps(row, value):
    next(r for r in row['tables']['DRE_con'] if r['DS_CONTA'] == 'ON')['VL_CONTA'] = value
    return row


def test_two_classes_keep_legacy_seven_features_and_exact_eps(tmp_path):
    path = cache(tmp_path, annual())
    candidates = [{'ticker': ticker, 'cvm': cvm, 'share_class': share, 'sector_code': sector}
                  for ticker, (cvm, share, sector) in SPECS.items()]
    statements, diagnostics, sources = prepare_financials(tmp_path, candidates, CUTOFF)
    payments = [(date(2026, 9, 1), date(2026, 8, 31), 1)]
    for ticker, eps, fcf in [('TEST3', 2, 1.2), ('TEST4', 4, 2.4)]:
        features, record = financial_features(statements, payments, CUTOFF, SPECS[ticker])
        assert features == pytest.approx([1, 1 / 12, eps, fcf, 1 / eps, .5, 0])
        assert record['period_type'] == 'annual' and record['annualized'] is False
    assert len([r for r in diagnostics if r['kind'] == 'candidate' and r['status'] == 'included']) == 2
    assert sources[0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert all(r['source_ref'] == sources[0]['id'] for r in statements)
    json.dumps((statements, diagnostics, sources), allow_nan=False)


def test_future_versions_and_future_fiscal_years_are_not_returned(tmp_path):
    cache(tmp_path, annual(), change_eps(annual(version=2, published='2026-08-01'), 4),
          change_eps(annual(version=3, published='2026-10-01'), 99),
          annual(year=2026, published='2026-09-01', version=4))
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert {r['version'] for r in statements} == {'1', '2'}
    assert all(r['published_at'] <= CUTOFF.isoformat() and r['fiscal_end'] <= CUTOFF.isoformat() for r in statements)
    assert financial_features(statements, [], date(2026, 7, 31), SPECS['TEST3'])[0][2] == 2
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][2] == 4


def test_missing_latest_eps_blocks_old_revision_only_after_publication(tmp_path):
    cache(tmp_path, annual(), change_eps(annual(version=2, published='2026-08-01'), 'NaN'))
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], date(2026, 7, 31), SPECS['TEST3'])[0][2] == 2
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert financial_features(statements, [], CUTOFF, SPECS['TEST4'])[0][2] == 4
    excluded = next(r for r in diagnostics if r['kind'] == 'candidate' and r['ticker'] == 'TEST3')
    assert excluded['status'] == 'excluded' and excluded['filing_id'].endswith('-2')


def test_itr_is_audited_but_partial_eps_is_never_annual_or_ttm(tmp_path):
    cache(tmp_path, annual())
    cache(tmp_path, change_eps(filing(), 99), year=2026, form='itr', official=True)
    statements, diagnostics, sources = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][2] == 2
    assert all(r['document_type'] == 'DFP' for r in statements)
    assert next(r for r in sources if r['document_type'] == 'ITR')['use'] == 'audit_only_not_annual_or_ttm'
    assert any('interim_flows_not_substituted_for_annual_model_features' in r['reasons']
               for r in diagnostics if r['kind'] == 'source')


@pytest.mark.parametrize('table,code', [('DRE_con', '3.99.01.01'), ('DFC_MI_con', '6.01'),
                                       ('DFC_MI_con', '6.02.01')])
def test_quarter_or_ytd_legs_cannot_enter_annual_features(tmp_path, table, code):
    row = annual()
    next(r for r in row['tables'][table] if r['CD_CONTA'] == code)['DT_INI_EXERC'] = '2025-07-01'
    cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'TEST3': SPECS['TEST3']}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert any('nonannual_or_mismatched_period' in r['reasons'] for r in diagnostics if r['kind'] == 'account')


def test_units_and_unresolved_cvm_never_reuse_same_issuer_eps(tmp_path):
    cache(tmp_path, annual())
    specs = {**SPECS, 'TEST11': (12345, 'ON', 0), 'UNKN3': (None, 'ON', 0), 'UNKN4': (12345, None, 0)}
    statements, diagnostics, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert {(r['cvm'], r['share_class']) for r in statements} == {(12345, 'ON'), (12345, 'PN')}
    excluded = {r['ticker'] for r in diagnostics if r['kind'] == 'candidate' and r['status'] == 'excluded'}
    assert excluded == {'TEST11', 'UNKN3', 'UNKN4'}


def test_financial_sector_has_real_eps_and_total_liabilities_but_no_fcf(tmp_path):
    cache(tmp_path, annual())
    spec = (12345, 'ON', 2)
    statements, _, _ = prepare_financials(tmp_path, {'BANK3': spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features == [0, 0, 2, None, 0, .5, 2]
    assert record['financial_method']['liabilities_method'] == 'total_passivo_less_consolidated_equity'
    assert record['financial_method']['fcf_absence_reason']
    json.dumps(statements, allow_nan=False)


def test_financial_sector_without_root_accounts_cannot_use_generic_liabilities(tmp_path):
    row = annual()
    row['tables']['BPP_con'] = [r for r in row['tables']['BPP_con'] if r['CD_CONTA'] in ('2.01', '2.02')]
    cache(tmp_path, row)
    spec = (12345, 'ON', 2)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'BANK3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert any('financial_root_balance_accounts_required' in r['reasons'] for r in diagnostics)


def test_missing_capex_stays_unavailable_for_nonfinancial_issuer(tmp_path):
    row = annual()
    row['tables']['DFC_MI_con'] = row['tables']['DFC_MI_con'][:1]
    cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def test_invalid_scale_is_local_and_does_not_discard_other_issuer(tmp_path):
    bad = annual()
    bad['tables']['BPA_con'][0]['ESCALA_MOEDA'] = 'UNKNOWN'
    cache(tmp_path, bad, annual(cvm=54321))
    specs = {'TEST3': SPECS['TEST3'], 'OTHR3': (54321, 'ON', 0)}
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['TEST3']) is None
    assert financial_features(statements, [], CUTOFF, specs['OTHR3']) is not None


def test_training_rows_keep_historical_availability_and_original_contract(tmp_path):
    cache(tmp_path, annual(year=2023, published='2024-03-01'),
          change_eps(annual(year=2023, published='2024-05-01', version=2), 4), year=2023, compact=True)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    dates = [date(2024, 1, 31), date(2024, 4, 30)]
    prices = {day: (day, 10, 10) for day in dates}
    payments = DividendHistory(CUTOFF, 2)
    payments.extend([(date(2024, 4, 1), date(2024, 4, 1), 1),
                     (date(2025, 4, 1), date(2025, 4, 1), 2)])
    rows, coverage = stock_observations('TEST3', prices, payments, statements, CUTOFF, spec=SPECS['TEST3'])
    assert len(rows) == 1 and rows[0]['as_of'] == '2024-04-30'
    assert rows[0]['published_at'] == '2024-03-01'
    assert rows[0]['profit_per_share'] == 2 and rows[0]['target'] == 2
    assert coverage['excluded_windows'][0]['as_of'] == '2024-01-31'


def test_official_cache_preferred_without_duplicate_versions_or_any_writes(tmp_path, monkeypatch):
    cache(tmp_path, annual())
    official = cache(tmp_path, change_eps(annual(), 4), official=True)
    cache(tmp_path, annual(year=2014, published='2015-03-01'), year=2014)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    original_specs = deepcopy(SPECS)

    def forbidden(*args, **kwargs):
        raise AssertionError('The financial adapter must remain offline.')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    statements, _, sources = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert len(sources) == 1 and sources[0]['file'] == official.relative_to(tmp_path).as_posix()
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][2] == 4
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert SPECS == original_specs


def test_corrupt_cache_does_not_erase_usable_other_year(tmp_path):
    cache(tmp_path, annual())
    (tmp_path / 'raw/cvm-dfp-2024.zip').write_bytes(b'not a zip')
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is not None
    assert any('invalid_cached_archive' in r['reasons'] for r in diagnostics if r['kind'] == 'source')


def test_empty_or_missing_cache_does_not_fall_back_to_default_universe(tmp_path):
    assert prepare_financials(tmp_path, {}, CUTOFF) == ([], [], [])
    statements, diagnostics, sources = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert not statements and not sources
    assert {r['ticker'] for r in diagnostics} == set(SPECS)
    assert all(r['status'] == 'excluded' for r in diagnostics)


def test_all_468_inventory_rows_are_evaluated_independently_of_pending_cash(tmp_path):
    cache(tmp_path, annual())
    candidates = [
        {'ticker': f'T{index:03d}3', 'cvm': 12345, 'share_class': 'ON' if index % 2 else 'PN',
         'sector_code': 0, 'model_ready': False,
         'payments': {'status': 'partial', 'history_complete': False, 'cash_model_eligible': False, 'events': []},
         'quote': {'status': 'not_found'}, 'history': {'status': 'not_found'}}
        for index in range(468)
    ]
    candidates[0]['cvm'] = None
    candidates[1].update(ticker='T00111', share_class=None, security_type='Units')
    original = deepcopy(candidates)
    statements, diagnostics, sources = prepare_financials(tmp_path, candidates, CUTOFF)
    coverage = [r for r in diagnostics if r['kind'] == 'candidate']
    assert len(coverage) == 468
    assert {r['ticker'] for r in coverage} == {r['ticker'] for r in candidates}
    assert sum(r['status'] == 'included' for r in coverage) == 466
    assert sum(r['status'] == 'excluded' for r in coverage) == 2
    assert statements and sources and candidates == original
    without_cash = [{key: value for key, value in row.items()
                     if key not in ('payments', 'quote', 'history', 'model_ready')} for row in candidates]
    assert prepare_financials(tmp_path, without_cash, CUTOFF) == (statements, diagnostics, sources)
    assert all('previous_cash' not in row and 'regularity' not in row and 'payout' not in row for row in statements)


def public_cache(folder, *filings, year=2025):
    # Column order and balance/flow distinction from CVM's public DFP ZIPs.
    common = ['CNPJ_CIA', 'DT_REFER', 'VERSAO', 'DENOM_CIA', 'CD_CVM']
    master_fields = common + ['CATEG_DOC', 'ID_DOC', 'DT_RECEB', 'LINK_DOC']
    tables = {'': [r['master'] for r in filings]}
    for item in filings:
        for table, rows in item['tables'].items():
            tables.setdefault('_' + table, []).extend(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as zipped:
        for suffix, rows in tables.items():
            fields = (master_fields if not suffix else common + [
                'GRUPO_DFP', 'MOEDA', 'ESCALA_MOEDA', 'ORDEM_EXERC',
                *(['DT_INI_EXERC'] if suffix.startswith(('_DRE', '_DFC', '_DRA', '_DVA')) else []),
                'DT_FIM_EXERC', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA', 'ST_CONTA_FIXA'])
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=';')
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, '') for field in fields})
            zipped.writestr(f'dfp_cia_aberta{suffix}_{year}.csv', stream.getvalue().encode('latin1'))
    path = folder / 'raw' / f'cvm-dfp-{year}.zip'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(output.getvalue())
    return path


def replace_accounts(item, table, entries):
    template = item['tables'][table][0]
    item['tables'][table] = [template | {'CD_CONTA': code, 'DS_CONTA': desc, 'VL_CONTA': value}
                             for code, desc, value in entries]
    return item


def weg_public_filing():
    item = annual(cvm=5410, published='2026-02-25')
    item['master']['ID_DOC'] = '154872'
    replace_accounts(item, 'DRE_con', [
        ('3.11.01', 'Atribu\u00eddo a S\u00f3cios da Empresa Controladora', '6376219.0000000000'),
        ('3.99.01.01', 'ON', '1.5197100000'), ('3.99.02.01', 'ON', '1.5194000000')])
    replace_accounts(item, 'DFC_MI_con', [
        ('6.01', 'Caixa L\u00edquido Atividades Operacionais', '6451033.0000000000'),
        ('6.02.02', 'Imobilizado', '-2563450.0000000000'),
        ('6.02.03', 'Intang\u00edvel', '-127871.0000000000'),
        ('6.02.04', 'Recebimento na venda de Ativo Imobilizado e Intang\u00edvel', '49783.0000000000'),
        ('6.02.09', 'Aquisi\u00e7\u00e3o de empresa - combina\u00e7\u00e3o de neg\u00f3cio (l\u00edquido do caixa)', '-200575.0000000000'),
        ('6.02.11', 'Aplica\u00e7\u00f5es financeiras mantidas at\u00e9 o vencimento', '-75379.0000000000')])
    replace_accounts(item, 'BPA_con', [('1', 'Ativo Total', '42645030.0000000000')])
    replace_accounts(item, 'BPP_con', [
        ('2', 'Passivo Total', '42645030.0000000000'),
        ('2.01', 'Passivo Circulante', '17386401.0000000000'),
        ('2.02', 'Passivo N\u00e3o Circulante', '6705265.0000000000')])
    for rows in [*item['tables'].values(), [item['master']]]:
        for row in rows:
            row.update(CNPJ_CIA='84.429.695/0001-11', DENOM_CIA='WEG S.A.', CD_CVM='005410')
    return item


def test_public_weg_asset_only_capex_preserves_values_and_source_cells(tmp_path):
    path = public_cache(tmp_path, weg_public_filing())
    original = path.read_bytes()
    spec = (5410, 'ON', 5)
    statements, diagnostics, sources = prepare_financials(tmp_path, {'WEGE3': spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features[2:4] == pytest.approx([
        1.51971, (6451033 - 2563450 - 127871) / (6376219 / 1.51971)])
    assert features[5] == pytest.approx((17386401 + 6705265) / 42645030)
    assert record['financial_method']['capex_accounts'] == ['6.02.02', '6.02.03']
    evidence = record['financial_method']['account_readings']
    assert [(r['description'], r['reported_value']) for r in evidence] == [
        ('Imobilizado', '-2563450.0000000000'), ('Intang\u00edvel', '-127871.0000000000')]
    assert all(r['statement'] == 'DFC_MI_con' for r in evidence)
    assert path.read_bytes() == original
    assert sources[0]['sha256'] == hashlib.sha256(original).hexdigest()
    assert diagnostics[-1]['status'] == 'included'


@pytest.mark.parametrize('description', [
    'No Imobilizado', 'No Intang\u00edvel', 'Acr\u00e9scimo de imobilizado',
    'Acr\u00e9scimo de intang\u00edvel',
    'Aplica\u00e7\u00f5es no imobilizado, intang\u00edvel e ativo contratual - Infra-estrutura em constru\u00e7\u00e3o'])
def test_public_capex_labels_are_investing_outflows_only(tmp_path, description):
    row = annual()
    row['tables']['DFC_MI_con'][1]['DS_CONTA'] = description
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][3] == pytest.approx(1.2)


@pytest.mark.parametrize('description,value', [
    ('Imobilizado', 20), ('Intang\u00edvel', 'NaN'),
    ('Aquisi\u00e7\u00e3o de Bens para o Ativo Imobilizado', '12618.0000000000'),
    ('Aplica\u00e7\u00f5es Financeiras', -20), ('Aumento de Capital em Controladas', -20),
    ('Aquisi\u00e7\u00e3o de participa\u00e7\u00e3o societ\u00e1ria - coligadas', -20),
    ('Recebimento na venda de Ativo Imobilizado e Intang\u00edvel', -20),
    ('Imobilizado arrendado', -20)])
def test_unusable_or_unrelated_investing_cells_do_not_become_capex(tmp_path, description, value):
    row = annual()
    row['tables']['DFC_MI_con'][1].update(DS_CONTA=description, VL_CONTA=value)
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def test_public_direct_cash_flow_uses_same_filing_and_records_original_table(tmp_path):
    row = annual()
    row['tables']['DFC_MD_con'] = row['tables'].pop('DFC_MI_con')
    row['tables']['DFC_MD_con'][1]['DS_CONTA'] = 'Imobilizado'
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, SPECS['TEST3'])
    assert features[3] == pytest.approx(1.2)
    assert all(r['statement'] == 'DFC_MD_con' for r in record['financial_method']['account_readings'])


@pytest.mark.parametrize('invalid', ['numeric', 'currency', 'period', 'scale'])
def test_invalid_indirect_cash_flow_never_falls_back_to_direct_or_older_version(tmp_path, invalid):
    latest = annual(version=2, published='2026-08-01')
    latest['tables']['DFC_MD_con'] = deepcopy(latest['tables']['DFC_MI_con'])
    latest['tables']['DFC_MI_con'][0].update({
        'numeric': {'VL_CONTA': 'NaN'}, 'currency': {'MOEDA': 'DOLAR'},
        'period': {'DT_INI_EXERC': '2025-07-01'}, 'scale': {'ESCALA_MOEDA': 'UNKNOWN'}}[invalid])
    public_cache(tmp_path, annual(), latest)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert financial_features(statements, [], date(2026, 7, 31), SPECS['TEST3'])[0][3] == pytest.approx(1.2)


@pytest.mark.parametrize('table,code', [('DRE_con', '3.99.01.01'), ('BPA_con', '1'),
                                      ('BPP_con', '2.01'), ('DFC_MI_con', '6.02.01')])
@pytest.mark.parametrize('reverse', [False, True])
def test_conflicting_same_code_cells_block_latest_revision_in_either_order(tmp_path, table, code, reverse):
    latest = annual(version=2, published='2026-08-01')
    rows = latest['tables'][table]
    original = next(r for r in rows if r['CD_CONTA'] == code)
    rows.insert(0, original | {'VL_CONTA': 'NaN'})
    if reverse:
        rows.reverse()
    public_cache(tmp_path, annual(), latest)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert financial_features(statements, [], date(2026, 7, 31), SPECS['TEST3']) is not None
    assert any('conflicting_duplicate_account' in r['reasons'] for r in diagnostics)


@pytest.mark.parametrize('eps', [('3.7300000000', '0.0000000000'),
                                ('1.2734700000', '1.2744000000'), ('2', '2')])
def test_two_basic_eps_codes_for_same_class_remain_ambiguous_even_when_equal(tmp_path, eps):
    row = annual()
    replace_accounts(row, 'DRE_con', [
        ('3.11.01', 'Atribu\u00eddo a S\u00f3cios da Empresa Controladora', 100),
        ('3.99.01.01', 'ON', eps[0]), ('3.99.01.02', 'ON', eps[1])])
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'TEST3': SPECS['TEST3']}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert 'basic_eps_absent_ambiguous_or_zero' in diagnostics[-1]['reasons']


@pytest.mark.parametrize('entries', [
    [('3.99', 'Lucro por A\u00e7\u00e3o (R$/A\u00e7\u00e3o)', '2.4000000000'),
     ('3.99.01', 'Lucro B\u00e1sico por A\u00e7\u00e3o', '0.0000000000'),
     ('3.99.02', 'Lucro Dilu\u00eddo por A\u00e7\u00e3o', '0.0000000000')],
    [('3.99.01', 'Lucro B\u00e1sico por A\u00e7\u00e3o', '4.6200000000')],
    [('3.99.01.01', 'PN', '-2.5000000000')],
    [('3.99.01.01', 'ON', '0.0000000000')]])
def test_generic_root_eps_preferred_subclass_and_zero_do_not_supply_exact_class(tmp_path, entries):
    row = annual()
    replace_accounts(row, 'DRE_con', [('3.11.01', 'Atribuido a Socios da Empresa Controladora', 100), *entries])
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, {'TEST3': SPECS['TEST3']}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def test_zero_attributable_profit_cannot_be_replaced_with_consolidated_profit(tmp_path):
    row = annual()
    next(r for r in row['tables']['DRE_con'] if r['CD_CONTA'] == '3.11.01')['VL_CONTA'] = '0.0000000000'
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def test_public_bank_root_codes_keep_financial_method_and_no_industrial_fcf(tmp_path):
    row = annual(cvm=22616, version=2, published='2026-03-24')
    replace_accounts(row, 'DRE_con', [
        ('3.09.01', 'Atribu\u00eddo a S\u00f3cios da Empresa Controladora', '16005964.0000000000'),
        ('3.99.01.01', 'ON', 2)])
    replace_accounts(row, 'BPA_con', [('1', 'Ativo Total', '809301519.0000000000')])
    replace_accounts(row, 'BPP_con', [
        ('2', 'Passivo Total', '809301519.0000000000'),
        ('2.01', 'Passivos Financeiros ao Valor Justo atrav\u00e9s do Resultado', '45337313.0000000000'),
        ('2.02', 'Outros Passivos Financeiros Designados ao Valor Justo atrav\u00e9s do Resultado', 0),
        ('2.08', 'Patrim\u00f4nio L\u00edquido Consolidado', '80293552.0000000000')])
    public_cache(tmp_path, row)
    spec = (22616, 'ON', 2)
    statements, _, _ = prepare_financials(tmp_path, {'BANK3': spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features[3] is None
    assert features[5] == pytest.approx((809301519 - 80293552) / 809301519)
    assert record['financial_method']['profit_account'] == '3.09.01'
    assert record['financial_method']['equity_account'] == '2.08'
    # Actual BPAC 2025 cells have no class EPS; root balances cannot fill that gap.
    replace_accounts(row, 'DRE_con', [
        ('3.09.01', 'Atribu\u00eddo a S\u00f3cios da Empresa Controladora', '16005964.0000000000'),
        ('3.99', 'Lucro por A\u00e7\u00e3o - (R$ / A\u00e7\u00e3o)', '0.0000000000'),
        ('3.99.01', 'Lucro B\u00e1sico por A\u00e7\u00e3o', '0.0000000000'),
        ('3.99.02', 'Lucro Dilu\u00eddo por A\u00e7\u00e3o', '0.0000000000')])
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'BPAC3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert diagnostics[-1]['reasons'] == ['basic_eps_absent_ambiguous_or_zero']


def test_direct_and_indirect_cash_flows_are_selected_per_issuer_without_mixing(tmp_path):
    direct = annual(cvm=54321)
    direct['tables']['DFC_MD_con'] = direct['tables'].pop('DFC_MI_con')
    direct['tables']['DFC_MD_con'][0]['VL_CONTA'] = 90
    public_cache(tmp_path, annual(), direct)
    specs = {'TEST3': SPECS['TEST3'], 'OTHR3': (54321, 'ON', 0)}
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['TEST3'])[0][3] == pytest.approx(1.2)
    assert financial_features(statements, [], CUTOFF, specs['OTHR3'])[0][3] == pytest.approx(1.4)


def test_preferred_generic_eps_never_implies_an_exact_preferred_subclass(tmp_path):
    public_cache(tmp_path, annual())
    spec = (12345, 'PNA', 0)
    statements, _, _ = prepare_financials(tmp_path, {'TEST5': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None


def test_identical_public_duplicate_rows_preserve_original_financial_values(tmp_path):
    row = annual()
    for table, cells in row['tables'].items():
        row['tables'][table] = [cell for original in cells for cell in (original, deepcopy(original))]
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][3] == pytest.approx(1.2)
    assert not any('conflicting_duplicate_account' in r['reasons'] for r in diagnostics)


@pytest.mark.parametrize('end,start', [
    ('2025-02-28', '2024-03-01'), ('2024-02-29', '2023-03-01'),
    ('2024-02-28', '2023-03-01'), ('2025-12-31', '2025-01-01')])
def test_exact_annual_start_handles_february_and_leap_years(end, start):
    assert _annual_start(date.fromisoformat(end)) == date.fromisoformat(start)


def test_public_march_to_february_flow_is_annual_but_latest_zero_eps_still_blocks(tmp_path):
    row = filing(fiscal='2025-02-28', start='2024-03-01', published='2025-05-08')
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is not None
    assert not any('nonannual_or_mismatched_period' in r['reasons'] for r in diagnostics)
    latest = change_eps(deepcopy(row), 0)
    latest['master'].update(VERSAO='2', ID_DOC='latest')
    for rows in latest['tables'].values():
        for cell in rows:
            cell['VERSAO'] = '2'
    public_cache(tmp_path, row, latest)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def individual_filing(**kwargs):
    item = annual(scope='ind', **kwargs)
    replace_accounts(item, 'DRE_ind', [
        ('3.11', 'Lucro/Preju\u00edzo do Per\u00edodo', 100),
        ('3.99.01.01', 'ON', 2), ('3.99.01.02', 'PN', 4)])
    return item


def individual_public_fixtures():
    # Selected original 2025 DFP cells; SAPR reports PN before ON.
    issuers = [
        (1694, '59048.0000000000', [('ON', '129.8469600000')], '59374.0000000000',
         ('6.02.03', 'Adi\u00e7\u00f5es ao Imobilizado', '-148.0000000000'),
         ('230132.0000000000', '19942.0000000000', '19927.0000000000'), '2026-03-27'),
        (14869, '393974.0000000000', [('ON', '2.8249000000'), ('PNA', '1.6505000000'), ('PNB', '0.0896000000')],
         '1899127.0000000000', ('6.02.01', 'Adi\u00e7\u00f5es para ativo contratual e imobilizado', '-2129592.0000000000'),
         ('16214965.0000000000', '5564801.0000000000', '5188764.0000000000'), '2026-02-24'),
        (11398, '-66637.0000000000', [('ON', '-2.3000000000'), ('PNA', '-2.3000000000'), ('PNB', '-2.3000000000')],
         '54451.0000000000', ('6.02.01', 'Aquisi\u00e7\u00e3o de bens do ativo imobilizado e intangivel', '-43565.0000000000'),
         ('927579.0000000000', '286747.0000000000', '70764.0000000000'), '2026-03-13'),
        (18627, '2079568.0000000000', [('PN', '1.4191000000'), ('ON', '1.2900900000')], '7060384.0000000000',
         ('6.02.01', 'Aplica\u00e7\u00e3o no Imobilizado e Intang\u00edvel', '-2615418.0000000000'),
         ('26465924.0000000000', '6024748.0000000000', '8093395.0000000000'), '2026-02-26')]
    filings = []
    for cvm, profit, classes, cfo, capex, balances, published in issuers:
        item = individual_filing(cvm=cvm, published=published)
        replace_accounts(item, 'DRE_ind', [
            ('3.11', 'Lucro/Preju\u00edzo do Per\u00edodo', profit),
            *[(f'3.99.01.{index:02d}', share, eps) for index, (share, eps) in enumerate(classes, 1)]])
        replace_accounts(item, 'DFC_MI_ind', [
            ('6.01', 'Caixa L\u00edquido Atividades Operacionais', cfo), capex])
        replace_accounts(item, 'BPA_ind', [('1', 'Ativo Total', balances[0])])
        replace_accounts(item, 'BPP_ind', [
            ('2', 'Passivo Total', balances[0]), ('2.01', 'Passivo Circulante', balances[1]),
            ('2.02', 'Passivo N\u00e3o Circulante', balances[2])])
        filings.append(item)
    return filings


def test_seven_real_individual_only_codes_use_original_root_eps_and_scope(tmp_path):
    filings = individual_public_fixtures()
    path = public_cache(tmp_path, annual(cvm=54321), *filings)
    before = path.read_bytes()
    specs = {'BMKS3': (1694, 'ON', 4), 'COCE3': (14869, 'ON', 9), 'COCE5': (14869, 'PNA', 9),
             'CRPG5': (11398, 'PNA', 1), 'CRPG6': (11398, 'PNB', 1),
             'SAPR3': (18627, 'ON', 9), 'SAPR4': (18627, 'PN', 9)}
    statements, diagnostics, sources = prepare_financials(tmp_path, specs, CUTOFF)
    for ticker, spec in specs.items():
        features, record = financial_features(statements, [], CUTOFF, spec)
        source = next(f for f in filings if int(f['master']['CD_CVM']) == spec[0])
        dre = source['tables']['DRE_ind']
        eps_row = next(r for r in dre if r['DS_CONTA'] == spec[1])
        profit = float(dre[0]['VL_CONTA'])
        eps = float(eps_row['VL_CONTA'])
        cfo, capex = [float(r['VL_CONTA']) for r in source['tables']['DFC_MI_ind']]
        assert features[2:4] == pytest.approx([eps, (cfo + capex) / (profit / eps)])
        bpp = source['tables']['BPP_ind']
        assert features[5] == pytest.approx(sum(float(r['VL_CONTA']) for r in bpp[1:]) / float(bpp[0]['VL_CONTA']))
        method = record['financial_method']
        assert record['accounts_scope'] == method['accounts_scope'] == 'individual'
        assert method['profit_account'] == '3.11'
        assert method['basic_eps_account'] == eps_row['CD_CONTA']
        assert all(name.endswith('_ind_2025.csv') for name in method['source_statements'])
        assert all(r['accounts_scope'] == 'individual' and r['statement'].endswith('_ind')
                   for r in method['account_readings'])
        assert next(r for r in diagnostics if r.get('ticker') == ticker)['status'] == 'included'
    assert path.read_bytes() == before
    assert sources[0]['sha256'] == hashlib.sha256(before).hexdigest()
    json.dumps((statements, diagnostics, sources), allow_nan=False)


@pytest.mark.parametrize('reverse', [False, True])
def test_consolidated_preference_is_deterministic_before_looking_at_values(tmp_path, reverse):
    con, ind = annual(), individual_filing()
    ind['tables']['DRE_ind'][1]['VL_CONTA'] = 99
    items = [con, ind]
    if reverse:
        items.reverse()
    public_cache(tmp_path, *items)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, SPECS['TEST3'])
    assert features[2:4] == pytest.approx([2, 1.2])
    assert record['financial_method']['accounts_scope'] == 'consolidated'
    assert all(name.endswith('_con_2025.csv') for name in record['financial_method']['source_statements'])
    assert not any(r.get('accounts_scope') == 'individual' for r in diagnostics)


@pytest.mark.parametrize('bad', ['missing', 'zero', 'invalid', 'different_class'])
def test_invalid_consolidated_class_eps_cannot_be_replaced_with_individual_eps(tmp_path, bad):
    con = annual()
    eps = next(r for r in con['tables']['DRE_con'] if r['DS_CONTA'] == 'ON')
    if bad == 'missing':
        con['tables']['DRE_con'].remove(eps)
    else:
        eps.update({'zero': {'VL_CONTA': 0}, 'invalid': {'VL_CONTA': 'NaN'},
                    'different_class': {'DS_CONTA': 'PNA'}}[bad])
    public_cache(tmp_path, con, individual_filing())
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    features, record = financial_features(statements, [], CUTOFF, SPECS['TEST4'])
    assert features[2] == 4
    assert record['financial_method']['accounts_scope'] == 'consolidated'


@pytest.mark.parametrize('table', ['DRE_con', 'BPA_con', 'BPP_con', 'DFC_MI_con', 'DFC_MD_con'])
@pytest.mark.parametrize('bad', ['numeric', 'currency', 'period', 'scale', 'order'])
def test_any_consolidated_core_presence_blocks_individual_even_if_every_cell_is_invalid(tmp_path, table, bad):
    con = annual()
    template = con['tables'][table if table != 'DFC_MD_con' else 'DFC_MI_con'][0]
    cell = template | {
        'numeric': {'VL_CONTA': 'NaN'}, 'currency': {'MOEDA': 'DOLAR'},
        'period': {'DT_FIM_EXERC': '2025-09-30'}, 'scale': {'ESCALA_MOEDA': 'UNKNOWN'},
        'order': {'ORDEM_EXERC': 'PEN\u00daLTIMO'}}[bad]
    con['tables'] = {table: [cell]}
    public_cache(tmp_path, con, individual_filing())
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert {r['accounts_scope'] for r in statements} == {'consolidated'}
    assert not any(r.get('accounts_scope') == 'individual' for r in diagnostics)


@pytest.mark.parametrize('missing', ['DRE_con', 'BPA_con', 'BPP_con', 'DFC_MI_con'])
def test_partial_consolidated_filing_never_borrows_a_table_from_individual(tmp_path, missing):
    con = annual()
    con['tables'].pop(missing)
    public_cache(tmp_path, con, individual_filing())
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert {r['accounts_scope'] for r in statements} == {'consolidated'}


def test_consolidated_scope_presence_is_checked_beyond_model_account_tables(tmp_path):
    con = annual()
    con['tables'] = {'DVA_con': [con['tables']['DRE_con'][0]]}
    public_cache(tmp_path, con, individual_filing())
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    assert {r['accounts_scope'] for r in statements} == {'consolidated'}
    assert any(r.get('kind') == 'scope' and r['source_statements'] == ['dfp_cia_aberta_DVA_con_2025.csv']
               for r in diagnostics)


@pytest.mark.parametrize('prior_scope', ['con', 'ind'])
def test_scope_selection_is_per_version_and_invalid_latest_never_reuses_previous_scope(tmp_path, prior_scope):
    prior = annual() if prior_scope == 'con' else individual_filing()
    latest = (individual_filing(version=2, published='2026-08-01') if prior_scope == 'con' else
              annual(version=2, published='2026-08-01'))
    suffix = 'ind' if prior_scope == 'con' else 'con'
    latest['tables'].pop('DRE_' + suffix)
    public_cache(tmp_path, prior, latest)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], date(2026, 7, 31), SPECS['TEST3']) is not None
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    last = max(statements, key=lambda r: int(r['version']))
    assert last['accounts_scope'] == ('individual' if suffix == 'ind' else 'consolidated')


def test_individual_direct_cash_flow_keeps_actual_source_table_and_scope(tmp_path):
    row = individual_filing()
    row['tables']['DFC_MD_ind'] = row['tables'].pop('DFC_MI_ind')
    row['tables']['DFC_MD_ind'][1]['DS_CONTA'] = 'Imobilizado'
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, SPECS['TEST3'])
    assert features[3] == pytest.approx(1.2)
    method = record['financial_method']
    assert method['accounts_scope'] == 'individual'
    assert any(r['statement'] == 'DFC_MD_ind' for r in method['account_readings'])
    assert not any('_con' in name for name in method['source_statements'])


def test_individual_bank_method_and_diagnostics_never_claim_consolidated_equity(tmp_path):
    row = individual_filing()
    public_cache(tmp_path, row)
    spec = (12345, 'ON', 2)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'BANK3': spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features[2:4] == [2, None]
    assert features[5] == .5
    method = record['financial_method']
    assert method['accounts_scope'] == 'individual'
    assert method['profit_account'] == '3.11'
    assert method['liabilities_method'] == 'total_passivo_less_individual_equity'
    assert any(r['description'] == 'Patrimonio Liquido' and r['statement'] == 'BPP_ind'
               for r in method['account_readings'])
    row['tables']['BPP_ind'] = [r for r in row['tables']['BPP_ind'] if r['CD_CONTA'] != '2.03']
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'BANK3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert 'individual_equity_absent_or_ambiguous' in diagnostics[-1]['reasons']


@pytest.mark.parametrize('bad', ['currency', 'period', 'scale', 'eps'])
def test_individual_scope_keeps_strict_currency_period_scale_and_eps_validation(tmp_path, bad):
    row = individual_filing()
    target = row['tables']['DRE_ind'][1] if bad == 'eps' else row['tables']['DFC_MI_ind'][0]
    target.update({'currency': {'MOEDA': 'DOLAR'}, 'period': {'DT_INI_EXERC': '2025-07-01'},
                   'scale': {'ESCALA_MOEDA': 'UNKNOWN'}, 'eps': {'VL_CONTA': '0.0000000000'}}[bad])
    public_cache(tmp_path, row)
    statements, _, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None


def test_missing_individual_profit_diagnostic_uses_actual_root_code(tmp_path):
    row = individual_filing()
    row['tables']['DRE_ind'] = [r for r in row['tables']['DRE_ind'] if r['CD_CONTA'] != '3.11']
    public_cache(tmp_path, row)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    filing_audit = next(r for r in diagnostics if r.get('kind') == 'filing')
    assert filing_audit['financial_method']['profit_account'] == '3.11'
    assert filing_audit['missing_accounts'] == ['3.11']
    assert filing_audit['accounts_scope'] == 'individual'


REVIEW_CASES = [
    ('AGRO3', 20036, '2025-06-30', '2024-07-01', '2025-09-03', '151759',
     138019, 1.3855, 1.3780, 'c6c0422897aabe9a94a94276b5ebf928c14613f8d6135826becef634daa0dcd2'),
    ('CSMG3', 19445, '2025-12-31', '2025-01-01', '2026-02-25', '154917',
     1415744, 3.73, 3.73, 'ae0c41b898ea8ec8aba758332e18972d45c757ddfa2840d50ad53dde34b6adf6'),
    ('MOVI3', 23825, '2025-12-31', '2025-01-01', '2026-03-23', '155600',
     318364, .9232, .9216, '56712e91bb8e35daad5d41adc345cbdc5dc589fcef6ed8a098fe065fa377d344'),
    ('SEER3', 23221, '2025-12-31', '2025-01-01', '2026-03-25', '155688',
     214436, 1.6841, None, 'cbed06f125c3678b562189a5e10edc358a32df220d85546d92593f9936ba71dc')]


def reviewed_filing(case):
    ticker, cvm, end, start, published, filing_id, profit, eps, diluted, digest = case
    item = filing(fiscal=end, start=start, published=published, cvm_id=cvm)
    item['master']['ID_DOC'] = filing_id
    entries = [
        ('3.11', 'Lucro/Preju\u00edzo Consolidado do Per\u00edodo', profit),
        ('3.11.01', 'Atribu\u00eddo a S\u00f3cios da Empresa Controladora', profit if ticker == 'CSMG3' else 0),
        ('3.11.02', 'Atribu\u00eddo a S\u00f3cios N\u00e3o Controladores', 0),
        ('3.99', 'Lucro por A\u00e7\u00e3o - (Reais / A\u00e7\u00e3o)', 0),
        ('3.99.01', 'Lucro B\u00e1sico por A\u00e7\u00e3o', 0), ('3.99.01.01', 'ON', eps),
        *([('3.99.01.02', 'ON', 0)] if ticker == 'CSMG3' else []),
        ('3.99.02', 'Lucro Dilu\u00eddo por A\u00e7\u00e3o', 0),
        *([('3.99.02.01', 'ON', diluted)] if diluted is not None else [])]
    replace_accounts(item, 'DRE_con', [(code, desc, f'{value:.10f}') for code, desc, value in entries])
    assert financials._dre_fingerprint(item['tables']['DRE_con']) == digest
    return item


def reviewed_cache(folder, monkeypatch, *cases):
    # Hermetic proof bytes exercise receipt verification; source figures above are original CVM cells.
    proof_bytes = b'fixture for a reviewed primary document'
    proof_digest = hashlib.sha256(proof_bytes).hexdigest()
    documents, readings = {}, []
    research = folder / financials.RESEARCH_FILE
    research.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        ticker, cvm, end, _, published, filing_id, _, _, _, digest = case
        proof = research.parent / f'{ticker}-{proof_digest}.pdf'
        proof.write_bytes(proof_bytes)
        documents[ticker] = {'file': proof.name, 'sha256': proof_digest,
                             'url': 'https://example.test/primary/' + ticker, 'issuer': ticker}
        readings.append({'cvm': cvm, 'fiscal_end': end, 'version': '1', 'filing_id': filing_id,
                         'published_at': published, 'dre_sha256': digest, 'document': ticker,
                         'pdf_page': 1, 'note': 'Reviewed EPS numerator',
                         **({'discard_basic_eps_accounts': ['3.99.01.02']} if ticker == 'CSMG3' else
                            {'profit_account': '3.11'})})
    body = json.dumps({'documents': documents, 'readings': readings}).encode()
    research.write_bytes(body)
    monkeypatch.setattr(financials, 'RESEARCH_SHA256', hashlib.sha256(body).hexdigest())
    return research


@pytest.mark.parametrize('case', REVIEW_CASES, ids=[case[0] for case in REVIEW_CASES])
def test_documented_real_filing_keeps_reported_class_eps_and_same_scope_profit(tmp_path, monkeypatch, case):
    item = reviewed_filing(case)
    reviewed_cache(tmp_path, monkeypatch, case)
    path = public_cache(tmp_path, item)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    ticker, cvm, _, _, _, filing_id, profit, eps, _, digest = case
    spec = (cvm, 'ON', 0)
    statements, diagnostics, sources = prepare_financials(tmp_path, {ticker: spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features[2:4] == pytest.approx([eps, 60 / (profit / eps)])
    assert record['filing_id'] == filing_id and record['accounts_scope'] == 'consolidated'
    method = record['financial_method']
    assert method['profit_account'] == ('3.11.01' if ticker == 'CSMG3' else '3.11')
    assert method['basic_eps_account'] == '3.99.01.01'
    assert method['documentary_reading']['original_dre_sha256'] == digest
    evidence = method['primary_evidence'][0]
    assert any(s['id'] == evidence['source_ref'] and s['authority'] == 'primary' for s in sources)
    if ticker == 'CSMG3':
        assert any('documented_duplicate_basic_eps_placeholder' in r['reasons']
                   and r['reported_value'] == '0.0000000000' for r in diagnostics)
    else:
        assert any('documented_same_scope_parent_profit_root' in r['reasons']
                   and r['reported_value'] == f'{profit:.10f}' for r in diagnostics)
    assert diagnostics[-1]['status'] == 'included'
    assert path.read_bytes() == before[path.relative_to(tmp_path)]
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    json.dumps((statements, diagnostics, sources), allow_nan=False)


@pytest.mark.parametrize('case', REVIEW_CASES, ids=[case[0] for case in REVIEW_CASES])
def test_documented_readings_require_the_verified_primary_proof(tmp_path, monkeypatch, case):
    public_cache(tmp_path, reviewed_filing(case))
    ticker, cvm = case[:2]
    spec = (cvm, 'ON', 0)
    assert financial_features(prepare_financials(tmp_path, {ticker: spec}, CUTOFF)[0], [], CUTOFF, spec) is None
    reviewed_cache(tmp_path, monkeypatch, case)
    next((tmp_path / financials.RESEARCH_FILE).parent.glob('*.pdf')).write_bytes(b'changed primary bytes')
    statements, diagnostics, _ = prepare_financials(tmp_path, {ticker: spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert any('documentary_readings_unavailable' in r['reasons'] for r in diagnostics)


@pytest.mark.parametrize('bad', ['manifest', 'missing_document', 'filing_id', 'version', 'publication',
                                  'eps_value', 'profit_value', 'nci', 'description', 'period', 'scale',
                                  'currency', 'extra_class', 'duplicate', 'individual'])
def test_review_never_authorizes_changed_identity_scope_or_original_cells(tmp_path, monkeypatch, bad):
    case = REVIEW_CASES[0]
    item = reviewed_filing(case)
    research = reviewed_cache(tmp_path, monkeypatch, case)
    root, attributable, nci, *rest = item['tables']['DRE_con']
    eps = next(r for r in rest if r['CD_CONTA'] == '3.99.01.01')
    if bad == 'manifest':
        research.write_bytes(research.read_bytes() + b' ')
    elif bad == 'missing_document':
        next(research.parent.glob('*.pdf')).unlink()
    elif bad == 'filing_id':
        item['master']['ID_DOC'] = 'unreviewed'
    elif bad == 'publication':
        item['master']['DT_RECEB'] = '2025-09-04'
    elif bad == 'version':
        item['master']['VERSAO'] = '2'
        for rows in item['tables'].values():
            for row in rows:
                row['VERSAO'] = '2'
    elif bad == 'individual':
        item['tables'] = {table.replace('_con', '_ind'): rows for table, rows in item['tables'].items()}
    elif bad == 'extra_class':
        item['tables']['DRE_con'].append(eps | {'CD_CONTA': '3.99.01.02', 'DS_CONTA': 'PN'})
    elif bad == 'duplicate':
        item['tables']['DRE_con'].append(eps | {'VL_CONTA': '9'})
    else:
        target, changes = {
            'eps_value': (eps, {'VL_CONTA': '1.3780000000'}),
            'profit_value': (root, {'VL_CONTA': '999999.0000000000'}),
            'nci': (nci, {'VL_CONTA': '10.0000000000'}),
            'description': (root, {'DS_CONTA': 'Different profit concept'}),
            'period': (eps, {'DT_INI_EXERC': '2025-01-01'}),
            'scale': (root, {'ESCALA_MOEDA': 'UNIDADE'}),
            'currency': (root, {'MOEDA': 'DOLAR'})}[bad]
        target.update(changes)
    public_cache(tmp_path, item)
    spec = (case[1], 'ON', 0)
    statements, _, _ = prepare_financials(tmp_path, {case[0]: spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None


def test_documented_old_revision_cannot_unblock_a_new_unreviewed_revision(tmp_path, monkeypatch):
    case = REVIEW_CASES[0]
    prior = reviewed_filing(case)
    latest = deepcopy(prior)
    latest['master'].update(VERSAO='2', ID_DOC='unreviewed', DT_RECEB='2026-08-01')
    for rows in latest['tables'].values():
        for row in rows:
            row['VERSAO'] = '2'
    reviewed_cache(tmp_path, monkeypatch, case)
    public_cache(tmp_path, prior, latest)
    spec = (case[1], 'ON', 0)
    statements, _, _ = prepare_financials(tmp_path, {case[0]: spec}, CUTOFF)
    assert financial_features(statements, [], date(2026, 7, 31), spec) is not None
    assert financial_features(statements, [], CUTOFF, spec) is None


def test_damaged_proof_is_local_to_its_filing(tmp_path, monkeypatch):
    cases = REVIEW_CASES[:2]
    research = reviewed_cache(tmp_path, monkeypatch, *cases)
    next(research.parent.glob('AGRO3-*.pdf')).write_bytes(b'broken proof')
    public_cache(tmp_path, *[reviewed_filing(case) for case in cases])
    specs = {case[0]: (case[1], 'ON', 0) for case in cases}
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['AGRO3']) is None
    assert financial_features(statements, [], CUTOFF, specs['CSMG3']) is not None


@pytest.mark.parametrize('scope', ['con', 'ind'])
@pytest.mark.parametrize('cash_method', ['MI', 'MD'])
@pytest.mark.parametrize('label', ['Imobilizado', 'Aquisi\u00e7\u00e3o de Bens para o Ativo Imobilizado'])
def test_positive_capex_leg_cannot_be_ignored_to_compute_partial_fcf(tmp_path, scope, cash_method, label):
    row = annual() if scope == 'con' else individual_filing()
    table = f'DFC_MI_{scope}'
    cells = row['tables'][table]
    cells.append(cells[-1] | {'CD_CONTA': '6.02.02', 'DS_CONTA': label, 'VL_CONTA': '12618.0000000000'})
    if cash_method == 'MD':
        row['tables'][f'DFC_MD_{scope}'] = row['tables'].pop(table)
    path = public_cache(tmp_path, row)
    before = path.read_bytes()
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3']) is None
    diagnostic = next(r for r in diagnostics
                      if 'positive_capex_requires_primary_cash_outflow_proof' in r['reasons'])
    assert diagnostic['reported_value'] == '12618.0000000000'
    assert diagnostic['statement'] == f'DFC_{cash_method}_{scope}'
    assert path.read_bytes() == before


@pytest.mark.parametrize('description,value', [
    ('Recebimento pela venda de imobilizado, investimento e intang\u00edveis', 69729),
    ('Venda de imobilizado e investimentos', 600),
    ('Transf. Investimentos para Ativo Intang\u00edvel e Imobilizado p/Incorpora\u00e7\u00e3o', 112047)])
def test_real_sale_receipts_and_transfers_are_not_positive_cash_acquisitions(tmp_path, description, value):
    item = annual()
    rows = item['tables']['DFC_MI_con']
    rows.append(rows[-1] | {'CD_CONTA': '6.02.02', 'DS_CONTA': description, 'VL_CONTA': value})
    public_cache(tmp_path, item)
    statements, diagnostics, _ = prepare_financials(tmp_path, SPECS, CUTOFF)
    assert financial_features(statements, [], CUTOFF, SPECS['TEST3'])[0][3] == pytest.approx(1.2)
    assert not any('positive_capex_requires_primary_cash_outflow_proof' in r['reasons'] for r in diagnostics)


NOTE_CASES = {
    'CAML': (24228, '147180', '2025-05-08', 216950, 217001, 51, 3,
             [('CAML3', 'ON', '0.6361', 216950000, 341071232, '0')]),
    'CEBR': (14451, '155793', '2026-03-27', 185575, 235310, 49735, 9,
             [('CEBR3', 'ON', '2.47296', 88831000, 35921000, '500'),
              ('CEBR5', 'PNA', '2.47296', 16235000, 6565000, '500'),
              ('CEBR6', 'PNB', '2.72026', 80509000, 29596000, '500')]),
    'CSUD': (20044, '155277', '2026-03-11', 106051, 106051, 0, 2,
             [('CSUD3', 'ON', '2.6046', 106051000, 40717000, '500')]),
    'ECOR': (19453, '155429', '2026-03-17', 885851, 866818, -19033, 5,
             [('ECOR3', 'ON', '1.27347', 885851000, 695621000, '500')]),
    'EMAE': (16993, '157077', '2026-05-11', -133356, -133356, 0, 9,
             [('EMAE4', 'PN', '-3.74', -83293000, 22241700, '0')]),
    'QUAL': (22497, '154937', '2026-02-26', 10665, 17049, 6384, 2,
             [('QUAL3', 'ON', '0.03767', 10665000, 283122458, '0')]),
    'SCAR': (13781, '155335', '2026-03-12', -64366, -48442, 15924, 2,
             [('SCAR3', 'ON', '-1.1252', -64366000, 57202000, '500')]),
    'TGMA': (20800, '155185', '2026-03-09', 242963, 242963, 0, 5,
             [('TGMA3', 'ON', '3.68', 242963000, 65937772, '0')]),
    'USIM': (14320, '154757', '2026-02-13', -3078192, -2910034, 168158, 1,
             [('USIM5', 'PNA', '-2.50', -1320880000, 528208632, '0'),
              ('USIM6', 'PNB', '-2.50', -1320880000, 528208632, '0')])}


def note_filing(name):
    cvm, filing_id, published, profit, total, nci, sector, classes = NOTE_CASES[name]
    item = (filing(fiscal='2025-02-28', start='2024-03-01', published=published, cvm_id=cvm)
            if name == 'CAML' else annual(cvm=cvm, published=published))
    item['master']['ID_DOC'] = filing_id
    cells = [('3.11', 'Lucro/Prejuizo Consolidado do Periodo', total),
             ('3.11.01', 'Atribuido a Socios da Empresa Controladora',
              0 if name in ('CSUD', 'TGMA') else abs(profit) if name == 'SCAR' else profit),
             ('3.11.02', 'Atribuido a Socios Nao Controladores', nci),
             ('3.99.01', 'Lucro Basico por Acao', 0), ('3.99.02', 'Lucro Diluido por Acao', 0)]
    original = {'ECOR': [('ON', '1.27347'), ('ON', '1.2744')],
                'QUAL': [('ON', '-0.00895'), ('ON', '0.04662')],
                'USIM': [('ON', '-2.50'), ('PN', '-2.50')],
                'TGMA': [('ON', '3.68')], 'CSUD': []}.get(name, [(c[1], '0') for c in classes])
    cells.extend((f'3.99.01.{i:02}', share, value) for i, (share, value) in enumerate(original, 1))
    cells.extend((f'3.99.02.{i:02}', share, value) for i, (share, value) in enumerate(original, 1))
    replace_accounts(item, 'DRE_con', cells)
    entries = []
    for ticker, share, eps, numerator, count, rounding in classes:
        entry = {'share_class': share, 'value': eps, 'numerator_brl': str(numerator),
                 'numerator_rounding_brl': '500', 'weighted_shares': str(count), 'shares_rounding': rounding,
                 'account': '3.99.01.01' if name in ('ECOR', 'TGMA') else f'note:basic:{share}'}
        if name in ('CEBR', 'EMAE', 'USIM'):
            entry['allocation'] = 'PN' if name == 'USIM' else share
        if name == 'USIM':
            entry['pool_components'] = {'PNA': '528142371', 'PNB': '66261'}
        entries.append(entry)
    review = {'cvm': cvm, 'filing_id': filing_id, 'published_at': published,
              'fiscal_end': item['master']['DT_REFER'], 'version': '1', 'full_dre': True,
              'dre_sha256': financials._dre_fingerprint(item['tables']['DRE_con'], full=True),
              'parent_profit_brl': str(profit * 1000), 'basic_eps': entries,
              'document': name, 'pdf_page': 1, 'note': 'Exact-filing printed basic EPS and weighted shares'}
    if name in ('CSUD', 'TGMA'):
        review['profit_account'] = '3.11'
    if name == 'SCAR':
        review['correct_parent_profit_sign'] = True
    if name == 'ECOR':
        review.update(discard_basic_eps_accounts=['3.99.01.02'],
                      discard_reason='documented_continuing_operations_eps_not_total')
    allocations = {'CEBR': {'ON': '88831000', 'PNA': '16235000', 'PNB': '80509000'},
                   'EMAE': {'ON': '-50063000', 'PN': '-83293000'},
                   'USIM': {'ON': '-1757312000', 'PN': '-1320880000'}}
    if name in allocations:
        review['profit_allocations_brl'] = allocations[name]
    specs = {ticker: (cvm, share, sector) for ticker, share, *_ in classes}
    return item, review, specs


def note_cache(folder, monkeypatch, review):
    research = folder / 'raw/primary-research/financials/test-notes.json'
    research.parent.mkdir(parents=True, exist_ok=True)
    content = b'exact annual primary document test fixture'
    proof = research.parent / 'note-proof.pdf'
    proof.write_bytes(content)
    document = {'file': proof.name, 'sha256': hashlib.sha256(content).hexdigest(),
                'url': 'https://example.test/primary/exact-annual', 'issuer': review['document']}
    body = json.dumps({'documents': {review['document']: document}, 'readings': [review]}).encode()
    research.write_bytes(body)
    monkeypatch.setattr(financials, 'ADDITIONAL_RESEARCH',
                        ((research.relative_to(folder).as_posix(), hashlib.sha256(body).hexdigest()),))
    return research, proof


@pytest.mark.parametrize('name,ticker', [(name, row[0]) for name, case in NOTE_CASES.items() for row in case[-1]])
def test_exact_reported_basic_eps_recoveries_keep_class_scope_and_legacy_fcf(tmp_path, monkeypatch, name, ticker):
    item, review, specs = note_filing(name)
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, review)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    statements, diagnostics, sources = prepare_financials(tmp_path, specs, CUTOFF)
    spec = specs[ticker]
    entry = next(e for e in review['basic_eps'] if e['share_class'] == spec[1])
    features, record = financial_features(statements, [], CUTOFF, spec)
    assert features[2] == float(entry['value'])
    profit = NOTE_CASES[name][3]
    assert features[3] == (None if spec[2] == 2 else pytest.approx(60 / (profit / features[2])))
    assert record['filing_id'] == review['filing_id']
    assert record['accounts_scope'] == record['financial_method']['accounts_scope'] == 'consolidated'
    method = record['financial_method']
    assert method['basic_eps_account'] == entry['account']
    assert method['profit_account'] == ('3.11' if name in ('CSUD', 'TGMA') else '3.11.01')
    assert method['reported_basic_eps'] == [entry]
    assert method['documentary_reading']['original_dre_sha256'] == review['dre_sha256']
    assert any(s['id'] == method['primary_evidence'][0]['source_ref'] and s['authority'] == 'primary' for s in sources)
    assert all('note_' not in r['account'] for r in method.get('account_readings', []))
    if name == 'SCAR':
        correction = next(r for r in diagnostics if any('parent_loss_sign' in reason for reason in r['reasons']))
        assert float(correction['reported_value']) == 64366
    if name == 'ECOR':
        assert any('documented_continuing_operations_eps_not_total' in r['reasons'] for r in diagnostics)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    json.dumps((statements, diagnostics, sources), allow_nan=False)


@pytest.mark.parametrize('name', NOTE_CASES)
@pytest.mark.parametrize('bad', ['missing_proof', 'changed_proof', 'changed_manifest', 'new_version',
                                  'different_filing', 'publication', 'changed_dre', 'individual_scope'])
def test_note_readings_never_escape_exact_primary_identity_or_scope(tmp_path, monkeypatch, name, bad):
    item, review, specs = note_filing(name)
    research, proof = note_cache(tmp_path, monkeypatch, review)
    if bad == 'missing_proof':
        proof.unlink()
    elif bad == 'changed_proof':
        proof.write_bytes(b'not the pinned primary document')
    elif bad == 'changed_manifest':
        research.write_bytes(research.read_bytes() + b' ')
    elif bad == 'new_version':
        item['master']['VERSAO'] = '2'
        for rows in item['tables'].values():
            for row in rows:
                row['VERSAO'] = '2'
    elif bad == 'different_filing':
        item['master']['ID_DOC'] = 'unreviewed'
    elif bad == 'publication':
        item['master']['DT_RECEB'] = '2026-08-01'
    elif bad == 'changed_dre':
        item['tables']['DRE_con'][0]['VL_CONTA'] = '123'
    else:
        item['tables'] = {key.replace('_con', '_ind'): rows for key, rows in item['tables'].items()}
    public_cache(tmp_path, item)
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    for spec in specs.values():
        assert financial_features(statements, [], CUTOFF, spec) is None


@pytest.mark.parametrize('field,value', [('value', '0'), ('value', 'NaN'), ('value', 'Infinity'),
                                       ('value', '0.03749'), ('value', '-0.00895'),
                                       ('weighted_shares', '0'), ('weighted_shares', '-1'),
                                       ('weighted_shares', '284472117'), ('weighted_shares', 'NaN'),
                                       ('numerator_brl', '17049000'), ('numerator_brl', '0'),
                                       ('numerator_rounding_brl', '-1'), ('shares_rounding', '-1'),
                                       ('shares_rounding', '283122458'), ('share_class', 'UNIT')])
def test_note_eps_crosschecks_reject_zero_diluted_component_or_wrong_denominator(tmp_path, monkeypatch, field, value):
    item, review, specs = note_filing('QUAL')
    review['basic_eps'][0][field] = value
    note_cache(tmp_path, monkeypatch, review)
    public_cache(tmp_path, item)
    statements, diagnostics, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['QUAL3']) is None
    assert any('documented_basic_eps_crosscheck_failed' in r['reasons'] for r in diagnostics)


@pytest.mark.parametrize('bad', ['parent_numerator', 'class_allocation', 'allocation_sum', 'pool_total', 'pool_class'])
def test_class_allocations_and_preferred_pool_must_reconcile_without_copying_on_eps(tmp_path, monkeypatch, bad):
    item, review, specs = note_filing('USIM')
    entry = review['basic_eps'][0]
    if bad == 'parent_numerator':
        entry['numerator_brl'] = review['parent_profit_brl']
    elif bad == 'class_allocation':
        entry['allocation'] = 'ON'
    elif bad == 'allocation_sum':
        review['profit_allocations_brl']['ON'] = '-1'
    elif bad == 'pool_total':
        entry['pool_components']['PNB'] = '0'
    else:
        entry['pool_components'] = {'PN': '528208632'}
    note_cache(tmp_path, monkeypatch, review)
    public_cache(tmp_path, item)
    statements, diagnostics, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert all(financial_features(statements, [], CUTOFF, spec) is None for spec in specs.values())
    assert any('documented_basic_eps_crosscheck_failed' in r['reasons'] for r in diagnostics)


def test_note_allocation_does_not_automatically_prove_an_unlisted_share_class(tmp_path, monkeypatch):
    item, review, specs = note_filing('EMAE')
    specs['EMAE3'] = (16993, 'ON', 9)
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, review)
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['EMAE4'])[0][2] == -3.74
    assert financial_features(statements, [], CUTOFF, specs['EMAE3']) is None


@pytest.mark.parametrize('bad', ['consolidated_total', 'nci', 'same_positive_sign'])
def test_documented_parent_loss_sign_requires_current_consolidated_reconciliation(tmp_path, monkeypatch, bad):
    item, review, specs = note_filing('SCAR')
    if bad == 'same_positive_sign':
        review['parent_profit_brl'] = '64366000'
    else:
        code = '3.11' if bad == 'consolidated_total' else '3.11.02'
        next(r for r in item['tables']['DRE_con'] if r['CD_CONTA'] == code)['VL_CONTA'] = '0'
        review['dre_sha256'] = financials._dre_fingerprint(item['tables']['DRE_con'], full=True)
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, review)
    statements, diagnostics, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['SCAR3']) is None
    assert any('documented_basic_eps_crosscheck_failed' in r['reasons'] for r in diagnostics)


def test_published_note_eps_never_repairs_nonannual_brb_latest_revision_period(tmp_path, monkeypatch):
    item, review, specs = note_filing('CSUD')
    for rows in item['tables'].values():
        for row in rows:
            row.update(CD_CVM='14206', DT_REFER='2024-12-31', DT_FIM_EXERC='2024-12-31',
                       DT_INI_EXERC='2024-12-01', VERSAO='3')
    item['master'].update(CD_CVM='14206', DT_REFER='2024-12-31', VERSAO='3',
                          ID_DOC='149667', DT_RECEB='2025-06-30')
    review.update(cvm=14206, fiscal_end='2024-12-31', version='3', filing_id='149667', published_at='2025-06-30',
                  dre_sha256=financials._dre_fingerprint(item['tables']['DRE_con'], full=True))
    public_cache(tmp_path, item, year=2024)
    note_cache(tmp_path, monkeypatch, review)
    spec = (14206, 'ON', 2)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'BSLI3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert any('nonannual_or_mismatched_period' in r['reasons'] for r in diagnostics)


def test_note_eps_never_bypasses_positive_capex_or_changes_financial_sector_contract(tmp_path, monkeypatch):
    item, review, specs = note_filing('CAML')
    item['tables']['DFC_MI_con'][-1]['VL_CONTA'] = '12618'
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, review)
    statements, diagnostics, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(statements, [], CUTOFF, specs['CAML3']) is None
    assert any('positive_capex_requires_primary_cash_outflow_proof' in r['reasons'] for r in diagnostics)


def test_latest_unreviewed_revision_cannot_fall_back_to_note_eps(tmp_path, monkeypatch):
    prior, review, specs = note_filing('CAML')
    latest = deepcopy(prior)
    latest['master'].update(VERSAO='2', ID_DOC='unreviewed', DT_RECEB='2026-08-01')
    for rows in latest['tables'].values():
        for row in rows:
            row['VERSAO'] = '2'
    public_cache(tmp_path, prior, latest)
    note_cache(tmp_path, monkeypatch, review)
    statements, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    spec = specs['CAML3']
    assert financial_features(statements, [], date(2026, 7, 31), spec)[0][2] == .6361
    assert financial_features(statements, [], CUTOFF, spec) is None


TELECOM_CASES = {
    'TIMS3': (24929, '154688', '2026-02-10', 4311984, '1.79', '2413569000', 13440069, -4541495),
    'VIVT3': (17671, '154821', '2026-02-23', 6167906, '1.91', '3223658000', 20717402, -9457557)}


def telecom_filing(ticker):
    cvm, filing_id, published, profit, eps, weighted, cfo, capex = TELECOM_CASES[ticker]
    scope = 'ind' if ticker == 'TIMS3' else 'con'
    item = annual(cvm=cvm, published=published, scope=scope)
    item['master']['ID_DOC'] = filing_id
    cells = ([('3.11', 'Lucro/Prejuizo do Periodo', profit)] if scope == 'ind' else [
        ('3.11', 'Lucro/Prejuizo Consolidado do Periodo', 6177543),
        ('3.11.01', 'Atribuido a Socios da Empresa Controladora', profit),
        ('3.11.02', 'Atribuido a Socios Nao Controladores', 9637)])
    cells.extend([('3.99', 'Lucro por Acao', 0), ('3.99.01', 'Lucro Basico por Acao', 0),
                  ('3.99.02', 'Lucro Diluido por Acao', 0)])
    if scope == 'ind':
        cells.extend([('3.99.01.01', 'ON', eps), ('3.99.02.01', 'ON', '1.78')])
    replace_accounts(item, f'DRE_{scope}', cells)
    replace_accounts(item, f'DFC_MI_{scope}', [
        ('6.01', 'Caixa Liquido Atividades Operacionais', cfo),
        ('6.02.02' if scope == 'ind' else '6.02.01', 'Adicoes ao imobilizado e intangivel', capex)])
    items = [item]
    if scope == 'ind':
        template = annual(cvm=cvm, published=published)
        template['master']['ID_DOC'] = filing_id
        for rows in template['tables'].values():
            for row in rows:
                row['VL_CONTA'] = '0.0000000000'
        items.insert(0, template)
    review = {'cvm': cvm, 'filing_id': filing_id, 'published_at': published,
              'fiscal_end': '2025-12-31', 'version': '1', 'full_dre': True,
              'accounts_scope': 'individual' if scope == 'ind' else 'consolidated',
              'dre_sha256': financials._dre_fingerprint(item['tables'][f'DRE_{scope}'], full=True),
              'parent_profit_brl': str(profit * 1000), 'document': ticker, 'pdf_page': 95 if scope == 'ind' else 101,
              'note': 'Exact 2025 audited basic ON EPS and weighted ordinary shares',
              'basic_eps': [{'share_class': 'ON', 'value': eps, 'numerator_brl': str(profit * 1000),
                             'numerator_rounding_brl': '500', 'weighted_shares': weighted,
                             'shares_rounding': '500', 'account': '3.99.01.01' if scope == 'ind' else
                             'note:24i:total_basic_eps:ON'}]}
    if scope == 'ind':
        review['profit_account'] = '3.11'
    return items, review, (cvm, 'ON', 8)


def telecom_cache(folder, monkeypatch, ticker):
    items, review, spec = telecom_filing(ticker)
    archive_path = public_cache(folder, *items)
    if ticker == 'TIMS3':
        review['individual_scope_archive_sha256'] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    research, proof = note_cache(folder, monkeypatch, review)
    return items, review, spec, archive_path, research, proof


@pytest.mark.parametrize('ticker', TELECOM_CASES)
def test_latest_telecom_primary_reading_recovers_exact_scope_basic_eps_and_fcf(tmp_path, monkeypatch, ticker):
    items, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, ticker)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    statements, diagnostics, sources = prepare_financials(tmp_path, {ticker: spec}, CUTOFF)
    features, record = financial_features(statements, [], CUTOFF, spec)
    _, filing_id, published, profit, eps, _, cfo, capex = TELECOM_CASES[ticker]
    assert features[2:4] == pytest.approx([float(eps), (cfo + capex) / (profit / float(eps))])
    assert record['filing_id'] == filing_id and record['version'] == '1' and record['published_at'] == published
    method = record['financial_method']
    assert record['accounts_scope'] == method['accounts_scope'] == review['accounts_scope']
    assert method['reported_basic_eps'] == review['basic_eps']
    assert method['basic_eps_account'] == review['basic_eps'][0]['account']
    suffix = '_ind_2025.csv' if ticker == 'TIMS3' else '_con_2025.csv'
    assert all(name.endswith(suffix) for name in method['source_statements'])
    if ticker == 'TIMS3':
        assert method['profit_account'] == '3.11'
        assert method['scope_selection'] == 'documented_individual_only_filing_zero_consolidated_templates'
        assert method['documentary_reading']['individual_scope_archive_sha256'] == review['individual_scope_archive_sha256']
        assert not any(r.get('accounts_scope') == 'consolidated' for r in diagnostics)
    else:
        assert method['profit_account'] == '3.11.01'
        assert method['scope_selection'] == 'consolidated_core_present'
    assert diagnostics[-1]['status'] == 'included'
    assert any(s['id'] == method['primary_evidence'][0]['source_ref'] for s in sources)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    json.dumps((statements, diagnostics, sources), allow_nan=False)


@pytest.mark.parametrize('ticker', TELECOM_CASES)
@pytest.mark.parametrize('bad', ['missing_proof', 'changed_proof', 'changed_manifest', 'filing_id',
                                  'version', 'publication', 'fiscal_end', 'scope'])
def test_telecom_reading_never_escapes_exact_latest_filing_or_primary_proof(tmp_path, monkeypatch, ticker, bad):
    items, review, spec, archive_path, research, proof = telecom_cache(tmp_path, monkeypatch, ticker)
    if bad == 'missing_proof':
        proof.unlink()
    elif bad == 'changed_proof':
        proof.write_bytes(b'unreviewed primary bytes')
    elif bad == 'changed_manifest':
        research.write_bytes(research.read_bytes() + b' ')
    else:
        field, value = {'filing_id': ('ID_DOC', 'unreviewed'), 'version': ('VERSAO', '2'),
                        'publication': ('DT_RECEB', '2026-08-01'), 'fiscal_end': ('DT_REFER', '2024-12-31'),
                        'scope': ('DT_RECEB', review['published_at'])}[bad]
        for item in items:
            item['master'][field] = value
            if bad == 'scope':
                item['tables'] = {key.rsplit('_', 1)[0] + ('_ind' if key.endswith('_con') else '_con'): rows
                                  for key, rows in item['tables'].items()}
            for rows in item['tables'].values():
                for row in rows:
                    if field in row:
                        row[field] = value
        public_cache(tmp_path, *items)
    statements, _, _ = prepare_financials(tmp_path, {ticker: spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None


@pytest.mark.parametrize('ticker', TELECOM_CASES)
def test_unreviewed_telecom_latest_version_never_reuses_proven_prior_revision(tmp_path, monkeypatch, ticker):
    items, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, ticker)
    latest = deepcopy(items)
    for item in latest:
        item['master'].update(VERSAO='2', ID_DOC='unreviewed', DT_RECEB='2026-08-01')
        for rows in item['tables'].values():
            for row in rows:
                row['VERSAO'] = '2'
    path = public_cache(tmp_path, *items, *latest)
    if ticker == 'TIMS3':
        review['individual_scope_archive_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        note_cache(tmp_path, monkeypatch, review)
    statements, _, _ = prepare_financials(tmp_path, {ticker: spec}, CUTOFF)
    assert financial_features(statements, [], date(2026, 7, 31), spec)[0][2] == float(TELECOM_CASES[ticker][4])
    assert financial_features(statements, [], CUTOFF, spec) is None


@pytest.mark.parametrize('table', ['DRE_con', 'BPA_con', 'BPP_con', 'DFC_MI_con'])
def test_tims_scope_proof_is_invalidated_by_any_change_to_native_archive(tmp_path, monkeypatch, table):
    items, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, 'TIMS3')
    items[0]['tables'][table][0]['VL_CONTA'] = '1'
    public_cache(tmp_path, *items)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'TIMS3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert {r['accounts_scope'] for r in statements} == {'consolidated'}
    assert not any(r.get('accounts_scope') == 'individual' for r in diagnostics)


@pytest.mark.parametrize('bad', ['missing', 'wrong_archive', 'wrong_dre', 'wrong_denominator', 'diluted_value',
                                  'nonannual', 'different_currency', 'different_scale'])
def test_tims_template_exception_is_not_a_generic_zero_consolidated_fallback(tmp_path, monkeypatch, bad):
    items, review, spec, path, _, _ = telecom_cache(tmp_path, monkeypatch, 'TIMS3')
    if bad == 'missing':
        review.pop('individual_scope_archive_sha256')
    elif bad == 'wrong_archive':
        review['individual_scope_archive_sha256'] = '0' * 64
    elif bad == 'wrong_dre':
        review['dre_sha256'] = '0' * 64
    elif bad == 'wrong_denominator':
        review['basic_eps'][0]['weighted_shares'] = '1'
    elif bad == 'diluted_value':
        review['basic_eps'][0]['value'] = '1.78'
    else:
        target = items[1]['tables']['DRE_ind'][0]
        target.update({'nonannual': {'DT_INI_EXERC': '2025-07-01'},
                       'different_currency': {'MOEDA': 'DOLAR'},
                       'different_scale': {'ESCALA_MOEDA': 'UNKNOWN'}}[bad])
        path = public_cache(tmp_path, *items)
        review['individual_scope_archive_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    note_cache(tmp_path, monkeypatch, review)
    statements, _, _ = prepare_financials(tmp_path, {'TIMS3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None


def test_tims_scope_exception_cannot_fill_missing_individual_tables_from_zero_templates(tmp_path, monkeypatch):
    items, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, 'TIMS3')
    items[1]['tables'].pop('DFC_MI_ind')
    path = public_cache(tmp_path, *items)
    review['individual_scope_archive_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    note_cache(tmp_path, monkeypatch, review)
    statements, _, _ = prepare_financials(tmp_path, {'TIMS3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None


@pytest.mark.parametrize('ticker', TELECOM_CASES)
@pytest.mark.parametrize('share', ['PN', 'PNA'])
def test_telecom_proof_only_authorizes_explicitly_reported_on_not_another_class(tmp_path, monkeypatch, ticker, share):
    _, _, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, ticker)
    requested = (spec[0], share, spec[2])
    statements, _, _ = prepare_financials(tmp_path, {ticker: requested}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, requested) is None


def test_telecom_documentary_reading_preserves_an_unrelated_available_filing(tmp_path, monkeypatch):
    items, review, spec = telecom_filing('TIMS3')
    path = public_cache(tmp_path, *items, annual())
    review['individual_scope_archive_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    specs = {'TIMS3': spec, 'TEST3': SPECS['TEST3']}
    baseline, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    note_cache(tmp_path, monkeypatch, review)
    recovered, _, _ = prepare_financials(tmp_path, specs, CUTOFF)
    assert financial_features(baseline, [], CUTOFF, spec) is None
    assert financial_features(recovered, [], CUTOFF, spec) is not None
    assert [r for r in baseline if r['cvm'] == 12345] == [r for r in recovered if r['cvm'] == 12345]


@pytest.mark.parametrize('ticker', TELECOM_CASES)
def test_telecom_financials_remain_unavailable_before_native_publication(tmp_path, monkeypatch, ticker):
    _, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, ticker)
    published = date.fromisoformat(review['published_at'])
    previous = date.fromordinal(published.toordinal() - 1)
    before, _, _ = prepare_financials(tmp_path, {ticker: spec}, previous)
    current, _, _ = prepare_financials(tmp_path, {ticker: spec}, published)
    assert financial_features(before, [], previous, spec) is None
    assert financial_features(current, [], published, spec)[0][2] == float(TELECOM_CASES[ticker][4])


@pytest.mark.parametrize('changes', [
    {'value': '0'}, {'value': '1.69'}, {'numerator_brl': '6177543000'}, {'weighted_shares': '3286264000'}])
def test_vivt_reading_never_uses_zero_total_profit_or_restated_comparative_eps(tmp_path, monkeypatch, changes):
    _, review, spec, _, _, _ = telecom_cache(tmp_path, monkeypatch, 'VIVT3')
    review['basic_eps'][0].update(changes)
    note_cache(tmp_path, monkeypatch, review)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'VIVT3': spec}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, spec) is None
    assert any('documented_basic_eps_crosscheck_failed' in row['reasons'] for row in diagnostics)


PINE_V3_DRE = [
    ('3.01', 'Receitas de Intermedia\u00e7\u00e3o Financeira', '4773522.0000000000'),
    ('3.01.01', 'Receitas com juros e similares', '4773522.0000000000'),
    ('3.02', 'Despesas de Intermedia\u00e7\u00e3o Financeira', '-3398049.0000000000'),
    ('3.02.01', 'Despesas de Intermedia\u00e7\u00e3o Financeira', '-3398049.0000000000'),
    ('3.03', 'Resultado Bruto de Intermedia\u00e7\u00e3o Financeira', '1375473.0000000000'),
    ('3.04', 'Outras Despesas e Receitas Operacionais', '-814466.0000000000'),
    ('3.04.01', 'Despesa de Provis\u00e3o para Perda Esperada para Risco de Cr\u00e9dito', '-456815.0000000000'),
    ('3.04.02', 'Receitas de Presta\u00e7\u00e3o de Servi\u00e7os', '146197.0000000000'),
    ('3.04.03', 'Despesas com Pessoal', '-277508.0000000000'),
    ('3.04.04', 'Outras Despesas de Administrativas', '-195894.0000000000'),
    ('3.04.05', 'Despesas Tribut\u00e1rias', '-68264.0000000000'),
    ('3.04.06', 'Outras Receitas Operacionais', '101100.0000000000'),
    ('3.04.07', 'Outras Despesas Operacionais', '-87897.0000000000'),
    ('3.04.07.01', 'Ganhos (perdas) com ativos e passivos financeiros (l\u00edquidos)', '-21894.0000000000'),
    ('3.04.07.02', 'Deprecia\u00e7\u00f5es e amortiza\u00e7\u00f5es', '-23141.0000000000'),
    ('3.04.07.03', '(Provis\u00f5es)/Revers\u00f5es l\u00edquidas', '3874.0000000000'),
    ('3.04.07.04', 'Resultado na aliena\u00e7\u00e3o de bens', '-10399.0000000000'),
    ('3.04.07.05', 'Despesas de tarifas e comiss\u00f5es', '-36337.0000000000'),
    ('3.04.08', 'Resultado da Equival\u00eancia Patrimonial', '24615.0000000000'),
    ('3.05', 'Resultado antes dos Tributos sobre o Lucro', '561007.0000000000'),
    ('3.06', 'Imposto de Renda e Contribui\u00e7\u00e3o Social sobre o Lucro', '-128129.0000000000'),
    ('3.06.01', 'Corrente', '-24078.0000000000'),
    ('3.06.02', 'Diferido', '-104051.0000000000'),
    ('3.07', 'Lucro ou Preju\u00edzo das Opera\u00e7\u00f5es Continuadas', '432878.0000000000'),
    ('3.08', 'Resultado L\u00edquido das Opera\u00e7\u00f5es Descontinuadas', '0.0000000000'),
    ('3.09', 'Lucro ou Preju\u00edzo antes das Participa\u00e7\u00f5es e Contribui\u00e7\u00f5es Estatut\u00e1rias', '432878.0000000000'),
    ('3.10', 'Participa\u00e7\u00f5es nos Lucros e Contribui\u00e7\u00f5es Estatut\u00e1rias', '0.0000000000'),
    ('3.11', 'Lucro ou Preju\u00edzo L\u00edquido Consolidado do Per\u00edodo', '432878.0000000000'),
    ('3.11.01', 'Atribu\u00eddo aos S\u00f3cios da Empresa Controladora', '430515.0000000000'),
    ('3.11.02', 'Atribu\u00eddo aos S\u00f3cios n\u00e3o Controladores', '2363.0000000000'),
    ('3.99', 'Lucro por A\u00e7\u00e3o (R$/A\u00e7\u00e3o)', '372446.0000000000'),
    ('3.99.01', 'Lucro B\u00e1sico por A\u00e7\u00e3o', '186223.0000000000'),
    ('3.99.02', 'Lucro Dilu\u00eddo por A\u00e7\u00e3o', '186223.0000000000')]
PINE_SPEC = (20567, 'PN', 2)
PINE_V3_DRE_SHA256 = '2faeab62e885403a4642b3c0201121f0a587b25aaefaa1c3814c07da5d4b3f99'


def pine_v3_filing():
    item = annual(cvm=20567, version=3, published='2026-03-31')
    item['master']['ID_DOC'] = '155965'
    replace_accounts(item, 'DRE_con', PINE_V3_DRE)
    replace_accounts(item, 'BPA_con', [('1', 'Ativo Total', '31013108.0000000000')])
    replace_accounts(item, 'BPP_con', [('2', 'Passivo Total', '31013108.0000000000'),
                                       ('2.07', 'Patrim\u00f4nio L\u00edquido Consolidado', '1436601.0000000000')])
    item['tables'].pop('DFC_MI_con')
    assert financials._dre_fingerprint(item['tables']['DRE_con'], full=True) == PINE_V3_DRE_SHA256
    return item


def proposed_pine_reading():
    # Literal issuer IFRS p18 figures, deliberately NOT an authorized reading.
    return {'document': 'PINE4', 'cvm': 20567, 'share_class': 'PN', 'fiscal_end': '2025-12-31',
            'version': '3', 'filing_id': '155965', 'published_at': '2026-03-31',
            'accounts_scope': 'consolidated', 'full_dre': True, 'dre_sha256': PINE_V3_DRE_SHA256,
            'pdf_page': 18, 'note': 'Explicit PN EPS, but class allocations include NCI',
            'parent_profit_brl': '430515000',
            'profit_allocations_brl': {'ON': '221909000', 'PN': '210969000'},
            'basic_eps': [{'share_class': 'PN', 'value': '1.86', 'numerator_brl': '210969000',
                           'numerator_rounding_brl': '500', 'weighted_shares': '113288564',
                           'shares_rounding': '0', 'allocation': 'PN',
                           'account': 'note:annual_ifrs:basic_eps:PN'}]}


def test_pine_exact_native_v3_is_blocked_with_bank_roots_and_null_not_zero_fcf(tmp_path):
    path = public_cache(tmp_path, pine_v3_filing())
    before = path.read_bytes()
    statements, diagnostics, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None
    latest = statements[-1]
    assert latest['filing_id'] == '155965' and latest['version'] == '3'
    assert latest['published_at'] == '2026-03-31' and latest['fiscal_end'] == '2025-12-31'
    assert latest['accounts_scope'] == 'consolidated'
    assert latest['profit_per_share'] is None and latest['free_cash_per_share'] is None
    method = latest['financial_method']
    assert method['id'] == 'financial_no_industrial_fcf_v1'
    assert method['profit_account'] == '3.11.01' and method['equity_account'] == '2.07'
    assert diagnostics[-1]['reasons'] == ['basic_eps_absent_ambiguous_or_zero']
    assert path.read_bytes() == before


def test_pine_reported_pn_eps_ratio_does_not_resolve_class_allocations_including_nci(tmp_path, monkeypatch):
    item = pine_v3_filing()
    reading = proposed_pine_reading()
    assert 1.855 < 210969000 / 113288564 < 1.865
    assert 221909000 + 210969000 - 430515000 == 2363000
    with pytest.raises(ValueError, match='documented_class_allocations_do_not_reconcile'):
        financials._checked_note_eps(item['tables']['DRE_con'], reading)
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, reading)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None
    assert statements[-1]['free_cash_per_share'] is None
    assert any('documented_basic_eps_crosscheck_failed' in row['reasons'] for row in diagnostics)


@pytest.mark.parametrize('bad', ['ignore_allocations', 'use_total_profit', 'use_individual_profit', 'zero_pn'])
def test_pine_cannot_be_recovered_by_discarding_the_numerator_discrepancy(tmp_path, monkeypatch, bad):
    item = pine_v3_filing()
    reading = proposed_pine_reading()
    if bad == 'ignore_allocations':
        reading.pop('profit_allocations_brl')
    elif bad == 'use_total_profit':
        reading['parent_profit_brl'] = '432878000'
    elif bad == 'use_individual_profit':
        reading['parent_profit_brl'] = '443594000'
    else:
        reading['basic_eps'][0]['value'] = '0'
    public_cache(tmp_path, item)
    note_cache(tmp_path, monkeypatch, reading)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None
    assert any('documented_basic_eps_crosscheck_failed' in row['reasons'] for row in diagnostics)


@pytest.mark.parametrize('value', ['186223.0000000000', '1.8622300000', '1.8600000000'])
def test_pine_generic_basic_root_never_becomes_pn_by_magnitude_or_rescaling(tmp_path, value):
    item = pine_v3_filing()
    next(row for row in item['tables']['DRE_con'] if row['CD_CONTA'] == '3.99.01')['VL_CONTA'] = value
    public_cache(tmp_path, item)
    statements, _, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None


def test_pine_an_on_leaf_cannot_authorize_pn_even_when_the_printed_amounts_are_equal(tmp_path):
    item = pine_v3_filing()
    template = item['tables']['DRE_con'][-1]
    item['tables']['DRE_con'].append(template | {'CD_CONTA': '3.99.01.01', 'DS_CONTA': 'ON', 'VL_CONTA': '1.86'})
    public_cache(tmp_path, item)
    statements, _, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None


def test_pine_v1_v2_without_core_are_not_substitutes_for_the_unavailable_v3(tmp_path):
    masters = []
    for version, filing_id, published in [(1, '154785', '2026-02-18'), (2, '154814', '2026-02-20')]:
        item = annual(cvm=20567, version=version, published=published)
        item['master']['ID_DOC'] = filing_id
        item['tables'] = {}
        masters.append(item)
    public_cache(tmp_path, *masters, pine_v3_filing())
    statements, diagnostics, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None
    assert all(row['profit_per_share'] is None for row in statements)
    assert diagnostics[-1]['filing_id'] == '155965' and diagnostics[-1]['accounts_scope'] == 'consolidated'


def test_pine_individual_scope_cannot_repair_its_present_consolidated_core(tmp_path):
    # A deliberately usable individual fixture must still not be borrowed.
    ind = individual_filing(cvm=20567, version=3, published='2026-03-31')
    ind['master']['ID_DOC'] = '155965'
    replace_accounts(ind, 'DRE_ind', [('3.11', 'Lucro/Prejuizo do Periodo', '443594.0000000000'),
                                     ('3.99.01.01', 'PN', '1.90833')])
    public_cache(tmp_path, pine_v3_filing(), ind)
    statements, diagnostics, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert financial_features(statements, [], CUTOFF, PINE_SPEC) is None
    assert all(row['accounts_scope'] == 'consolidated' for row in statements)
    assert not any(row.get('accounts_scope') == 'individual' for row in diagnostics)


def test_negative_pine_review_has_no_authorized_eps_reading_or_effect_on_statements(tmp_path, monkeypatch):
    public_cache(tmp_path, pine_v3_filing())
    baseline, _, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    path = tmp_path / 'raw/primary-research/financials/negative-pine-review.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({'documents': {}, 'readings': [], 'findings': [proposed_pine_reading()]}).encode()
    path.write_bytes(body)
    monkeypatch.setattr(financials, 'ADDITIONAL_RESEARCH',
                        ((path.relative_to(tmp_path).as_posix(), hashlib.sha256(body).hexdigest()),))
    reviewed, _, _ = prepare_financials(tmp_path, {'PINE4': PINE_SPEC}, CUTOFF)
    assert baseline == reviewed
    assert financial_features(reviewed, [], CUTOFF, PINE_SPEC) is None
