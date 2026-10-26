import csv
from datetime import date
import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

from . import cvm_fundamentals as cvm


AS_OF = date(2026, 10, 24)
SPECS = {'TEST3': (12345, 'ON', 0), 'TEST4': (12345, 'PN', 0)}
MASTER = ['CD_CVM', 'DT_REFER', 'VERSAO', 'DT_RECEB', 'ID_DOC']
FIELDS = ['CD_CVM', 'DT_REFER', 'VERSAO', 'ORDEM_EXERC', 'MOEDA', 'ESCALA_MOEDA',
          'DT_INI_EXERC', 'DT_FIM_EXERC', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA']


def filing(*, fiscal='2026-06-30', published='2026-08-10', version=1, cvm_id=12345, scope='con', start='2026-01-01'):
    common = {'CD_CVM': str(cvm_id), 'DT_REFER': fiscal, 'VERSAO': str(version),
              'ORDEM_EXERC': '\u00daLTIMO', 'MOEDA': 'REAL', 'ESCALA_MOEDA': 'MIL',
              'DT_INI_EXERC': start, 'DT_FIM_EXERC': fiscal}
    entries = {
        'BPA': [('1', 'Ativo Total', 1000)],
        'BPP': [('2', 'Passivo Total', 1000),
                ('2.01', 'Passivo Circulante', 200), ('2.02', 'Passivo Nao Circulante', 300),
                ('2.03', 'Patrimonio Liquido Consolidado' if scope == 'con' else 'Patrimonio Liquido', 500)],
        'DRE': [('3.01', 'Receita de Venda de Bens e/ou Servicos', 300),
                ('3.11', 'Lucro/Prejuizo Consolidado do Periodo' if scope == 'con' else 'Lucro/Prejuizo do Periodo', 110),
                ('3.11.01', 'Atribuido a Socios da Empresa Controladora', 100),
                ('3.99.01.01', 'ON', 2), ('3.99.01.02', 'PN', 4)],
        'DFC_MI': [('6.01', 'Caixa Liquido Atividades Operacionais', 80),
                   ('6.02.01', 'Aquisicao de Imobilizado e Intangivel', -20)],
    }
    return {'master': {'CD_CVM': str(cvm_id), 'DT_REFER': fiscal, 'VERSAO': str(version),
                       'DT_RECEB': published, 'ID_DOC': f'{cvm_id}-{fiscal}-{version}'},
            'tables': {f'{table}_{scope}': [common | {'CD_CONTA': code, 'DS_CONTA': desc, 'VL_CONTA': value}
                                            for code, desc, value in rows] for table, rows in entries.items()}}


def archive(*filings, form='itr', year=2026):
    output = io.BytesIO()
    tables = {'': [item['master'] for item in filings]}
    for item in filings:
        for table, rows in item['tables'].items():
            tables.setdefault('_' + table, []).extend(rows)
    with zipfile.ZipFile(output, 'w') as zipped:
        for table, rows in tables.items():
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=FIELDS if table else MASTER, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            zipped.writestr(f'{form}_cia_aberta{table}_{year}.csv', text.getvalue().encode('latin1'))
    return {f'{form}_cia_aberta_{year}.zip': output.getvalue()}


def fields(data, ticker='TEST3', specs=SPECS, as_of=AS_OF):
    return cvm.parse_archives(data, specs, as_of)['instruments'][ticker]['fields']


def test_fields_are_independent_and_eps_is_exact_class_and_unscaled():
    row = filing()
    row['tables'].pop('DFC_MI_con')
    row['tables']['DRE_con'] = [r for r in row['tables']['DRE_con'] if r['CD_CONTA'].startswith('3.99')]
    result = cvm.parse_archives(archive(row), SPECS, AS_OF)
    on, pn = result['instruments'].values()
    assert on['status'] == 'partial'
    assert on['fields']['basic_eps']['value'] == 2
    assert pn['fields']['basic_eps']['value'] == 4
    assert on['fields']['total_assets']['value'] == 1000000
    assert on['fields']['total_liabilities']['value'] == 500000
    assert on['fields']['liabilities_to_assets']['value'] == .5
    assert on['fields']['revenue']['status'] == 'missing'
    assert on['fields']['free_cash_flow']['value'] is None
    assert on['fields']['operating_cash_flow']['value'] is None
    json.dumps(result, allow_nan=False)


def test_itr_ytd_does_not_become_annual_eps_and_prefers_ytd_to_single_quarter():
    row = filing()
    row['tables']['DRE_con'] += [r | {'DT_INI_EXERC': '2026-04-01', 'VL_CONTA': 1}
                                 for r in row['tables']['DRE_con']]
    result = fields(archive(row))
    eps = result['basic_eps']
    assert eps['value'] == 2 and eps['period_type'] == 'ytd'
    assert (eps['period_start'], eps['period_end']) == ('2026-01-01', '2026-06-30')
    assert eps['annualized'] is False
    assert result['free_cash_flow']['value'] == 60000
    assert result['free_cash_flow']['period_type'] == 'ytd'
    assert all('price_earnings' not in key and 'ttm' not in key for key in result)


@pytest.mark.parametrize('start,kind', [('2025-01-01', 'annual'), ('2025-07-01', 'reported_flow')])
def test_dfp_flow_duration_is_explicit(start, kind):
    result = fields(archive(filing(fiscal='2025-12-31', published='2026-03-01', start=start), form='dfp', year=2025))
    assert result['basic_eps']['period_type'] == kind


def test_single_quarter_is_preserved_without_annualization():
    result = fields(archive(filing(start='2026-04-01')))
    assert result['basic_eps']['period_type'] == 'quarter'
    assert result['basic_eps']['value'] == 2


@pytest.mark.parametrize('share_class,ticker', [('ON', 'TEST11'), ('UNT', 'TEST11'), ('PNB', 'TEST6')])
def test_generic_or_wrong_class_eps_never_applies_to_units_or_other_classes(share_class, ticker):
    row = filing()
    row['tables']['DRE_con'].append(row['tables']['DRE_con'][0] | {
        'CD_CONTA': '3.99.01', 'DS_CONTA': 'Lucro Basico por Acao', 'VL_CONTA': 999})
    result = fields(archive(row), ticker=ticker, specs={ticker: (12345, share_class, 0)})
    assert result['basic_eps']['value'] is None
    assert result['total_assets']['value'] == 1000000


def test_unknown_class_and_unresolved_cvm_preserve_records_without_generic_eps():
    specs = {'TEST11': ('012345', None, 2), 'UNKNOWN3': (None, 'ON', 0)}
    result = cvm.parse_archives(archive(filing()), specs, AS_OF)['instruments']
    assert result['TEST11']['cvm'] == 12345 and result['TEST11']['share_class'] == 'UNKNOWN'
    assert result['TEST11']['fields']['basic_eps']['value'] is None
    assert result['TEST11']['fields']['total_assets']['value'] == 1000000
    assert result['UNKNOWN3']['identity_status'] == 'unresolved_cvm'
    assert result['UNKNOWN3']['status'] == 'unavailable'
    assert result['UNKNOWN3']['fields']['total_assets']['reason'] == 'unresolved_cvm'


def test_unresolved_only_batch_does_not_download_or_create_cache(tmp_path, monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError('An unresolved issuer cannot be looked up by guessing.')

    monkeypatch.setattr(cvm.urllib.request, 'urlopen', forbid)
    result = cvm.collect_fundamentals(tmp_path, {'UNKNOWN3': (None, None, 0)}, AS_OF)
    assert result['sources'] == []
    assert not list(tmp_path.iterdir())


def test_missing_eps_does_not_remove_profit_balance_or_cash():
    row = filing()
    row['tables']['DRE_con'] = [r for r in row['tables']['DRE_con'] if not r['CD_CONTA'].startswith('3.99')]
    result = fields(archive(row))
    assert result['basic_eps']['status'] == 'missing'
    assert result['attributable_profit']['value'] == 100000
    assert result['net_income']['value'] == 110000
    assert result['operating_cash_flow']['value'] == 80000
    assert result['free_cash_flow']['value'] == 60000  # Aggregate BRL, no inferred shares.


def test_zero_eps_is_reported_not_replaced_and_never_divides_into_a_ratio():
    row = filing()
    row['tables']['DRE_con'][-2]['VL_CONTA'] = 0
    result = fields(archive(row))
    assert result['basic_eps']['value'] == 0
    assert result['basic_eps']['status'] == 'available'
    assert result['free_cash_flow']['unit'] == 'BRL'


def test_latest_published_version_is_selected_without_old_version_fill():
    first = filing(version=1)
    revision = filing(version=2, published='2026-09-01')
    revision['tables']['DRE_con'] = [r for r in revision['tables']['DRE_con'] if r['DS_CONTA'] != 'ON']
    data = archive(first, revision)
    early = fields(data, as_of='2026-08-31')
    later = fields(data)
    assert early['basic_eps']['value'] == 2 and early['basic_eps']['version'] == 1
    assert later['basic_eps']['value'] is None
    assert later['total_assets']['version'] == 2


def test_future_publication_and_fiscal_period_are_not_used():
    data = archive(filing(published='2026-11-01'), filing(fiscal='2026-12-31', published='2026-12-31'))
    result = cvm.parse_archives(data, SPECS, AS_OF)
    assert all(r['status'] == 'unavailable' for r in result['instruments'].values())


def test_earlier_fiscal_field_keeps_own_filing_and_period_without_mixing_derivations():
    annual = filing(fiscal='2025-12-31', published='2026-03-01', start='2025-01-01')
    interim = filing()
    interim['tables'].pop('BPA_con')
    interim['tables']['DRE_con'] = [r for r in interim['tables']['DRE_con'] if r['DS_CONTA'] != 'ON']
    data = archive(annual, form='dfp', year=2025) | archive(interim)
    result = fields(data)
    assert result['basic_eps']['period_end'] == '2025-12-31'
    assert result['basic_eps']['period_type'] == 'annual'
    assert result['basic_eps']['is_latest_filing'] is False
    assert result['total_liabilities']['period_end'] == '2026-06-30'
    assert result['liabilities_to_assets']['period_end'] == '2025-12-31'
    assert result['liabilities_to_assets']['document_type'] == 'DFP'


def test_consolidated_preferred_and_individual_fields_never_fill_it():
    consolidated = filing()
    consolidated['tables'].pop('DRE_con')
    individual = filing(scope='ind')
    result = cvm.parse_archives(archive(consolidated, individual), SPECS, AS_OF)['instruments']['TEST3']
    assert result['scope'] == 'consolidated'
    assert result['fields']['basic_eps']['value'] is None
    assert result['fields']['total_assets']['scope'] == 'consolidated'
    assert result['fields']['equity']['value'] == 500000


def test_individual_only_scope_is_explicit():
    result = cvm.parse_archives(archive(filing(scope='ind')), SPECS, AS_OF)['instruments']['TEST3']
    assert result['scope'] == 'individual'
    assert result['scope_selection'] == 'individual_only_no_consolidated_accounts'
    assert all(r['scope'] == 'individual' for r in result['fields'].values())
    assert result['fields']['equity']['value'] == 500000


def test_financial_liabilities_use_root_passivo_less_equity_and_no_industrial_fcf():
    row = filing()
    row['tables']['BPP_con'][1]['DS_CONTA'] = 'Passivos Financeiros ao Valor Justo'
    row['tables']['BPP_con'][2]['DS_CONTA'] = 'Passivos Financeiros ao Custo Amortizado'
    row['tables']['BPP_con'][-1]['CD_CONTA'] = '2.08'
    row['tables']['DRE_con'][2]['CD_CONTA'] = '3.09.01'
    result = fields(archive(row), specs={'TEST3': (12345, 'ON', 2)})
    assert result['total_liabilities']['value'] == 500000
    assert result['attributable_profit']['value'] == 100000
    assert result['operating_cash_flow']['value'] == 80000
    assert result['capex']['value'] == -20000
    assert result['free_cash_flow']['value'] is None
    assert result['free_cash_flow']['status'] == 'not_applicable'


@pytest.mark.parametrize('bad', ['', 'NaN', 'Infinity', 'invalid', '1e999'])
def test_bad_values_in_one_field_do_not_discard_other_fields(bad):
    row = filing()
    row['tables']['DRE_con'][-2]['VL_CONTA'] = bad
    result = cvm.parse_archives(archive(row), SPECS, AS_OF)
    on = result['instruments']['TEST3']['fields']
    assert on['basic_eps']['status'] == 'invalid'
    assert on['total_assets']['value'] == 1000000
    assert result['instruments']['TEST4']['fields']['basic_eps']['value'] == 4
    json.dumps(result, allow_nan=False)


def test_required_period_and_numeric_scale_are_validated_per_field():
    row = filing()
    row['tables']['DRE_con'][-2]['DT_INI_EXERC'] = ''
    row['tables']['BPA_con'][0]['ESCALA_MOEDA'] = 'UNKNOWN'
    result = fields(archive(row))
    assert result['basic_eps']['status'] == 'invalid'
    assert result['total_assets']['reason'] == 'unsupported_currency_scale'
    assert result['equity']['status'] == 'available'


@pytest.mark.parametrize('description', ['Investimentos', 'Aquisicao de Ativos Financeiros', 'Aquisicao e Alienacao de Imobilizado'])
def test_unidentified_or_mixed_capex_never_becomes_zero_or_operating_fcf(description):
    row = filing()
    row['tables']['DFC_MI_con'][-1]['DS_CONTA'] = description
    result = fields(archive(row))
    assert result['operating_cash_flow']['value'] == 80000
    assert result['capex']['value'] is None
    assert result['free_cash_flow']['value'] is None


def test_capex_leaf_aggregation_and_incompatible_flow_periods():
    row = filing()
    parent = row['tables']['DFC_MI_con'][-1]
    row['tables']['DFC_MI_con'] += [parent | {'CD_CONTA': '6.02.01.01', 'VL_CONTA': -15},
                                    parent | {'CD_CONTA': '6.02.01.02', 'VL_CONTA': -5}]
    assert fields(archive(row))['capex']['value'] == -20000
    for account in row['tables']['DFC_MI_con'][1:]:
        account['DT_INI_EXERC'] = '2026-04-01'
    result = fields(archive(row))
    assert result['free_cash_flow']['value'] is None
    assert result['free_cash_flow']['reason'] == 'incompatible_periods_or_statements'


def test_direct_cash_method_is_supported_without_mixing_presentations():
    row = filing()
    row['tables']['DFC_MD_con'] = row['tables'].pop('DFC_MI_con')
    result = fields(archive(row))
    assert result['operating_cash_flow']['value'] == 80000
    assert result['free_cash_flow']['value'] == 60000
    assert all(r['statement'] == 'DFC_MD' for r in result['free_cash_flow']['accounts'])


def test_source_hashes_and_field_references_are_reproducible():
    data = archive(filing())
    result = cvm.parse_archives(data, SPECS, AS_OF)
    source = result['sources'][0]
    assert source['sha256'] == hashlib.sha256(next(iter(data.values()))).hexdigest()
    assert source['url'].startswith('https://dados.cvm.gov.br/')
    assert result['instruments']['TEST3']['fields']['basic_eps']['source_ref'] == source['id']


def test_broken_archive_does_not_erase_other_source_fields():
    data = archive(filing()) | {'dfp_cia_aberta_2025.zip': b'not a zip'}
    result = cvm.parse_archives(data, SPECS, AS_OF)
    assert result['instruments']['TEST3']['fields']['total_assets']['value'] == 1000000
    assert result['sources'][1]['status'] == 'invalid'


def test_wrapper_writes_only_separate_official_cache_and_reuses_it(tmp_path, monkeypatch):
    data = archive(filing()) | archive(filing(fiscal='2025-12-31', published='2026-03-01', start='2025-01-01'), form='dfp', year=2025)
    calls = []

    class Response(io.BytesIO):
        pass

    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert timeout == 90 and request.full_url.startswith('https://dados.cvm.gov.br/')
        return Response(data[request.full_url.rsplit('/', 1)[-1]])

    monkeypatch.setattr(cvm.urllib.request, 'urlopen', urlopen)
    first = cvm.collect_fundamentals(tmp_path, SPECS, AS_OF)
    assert len(calls) == 2
    assert all(r['retrieved_at'] and r['sha256'] for r in first['sources'])
    second = cvm.collect_fundamentals(tmp_path, SPECS, AS_OF)
    assert first == second and len(calls) == 2
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file()} == {
        'raw/official-financials/dfp_cia_aberta_2025.zip', 'raw/official-financials/itr_cia_aberta_2026.zip'}
    cvm.collect_fundamentals(tmp_path, SPECS, AS_OF, refresh=True)
    assert len(calls) == 4


def test_wrapper_source_failure_preserves_partial_instruments(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.URLError('Unavailable')

    monkeypatch.setattr(cvm.urllib.request, 'urlopen', fail)
    result = cvm.collect_fundamentals(tmp_path, SPECS, AS_OF)
    assert set(result['instruments']) == set(SPECS)
    assert all(r['status'] == 'unavailable' for r in result['sources'])
    assert not list(tmp_path.rglob('*.zip'))
    json.dumps(result, allow_nan=False)
