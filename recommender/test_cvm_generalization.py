import csv
from datetime import date
import io
import json
import zipfile

import pytest

from .collect_market_data import (STOCKS, DividendHistory, cvm_statements,
                                  financial_features, stock_observations)


SPECS = {'TEST3': (12345, 'ON', 2), 'TEST4': (12345, 'PN', 2)}
AS_OF = date(2026, 3, 31)
CUTOFF = date(2027, 3, 31)


def issuer(cvm=12345, *, version='1', published_at='2026-03-05', overrides=None):
    common = dict(CD_CVM=str(cvm).zfill(6), DT_REFER='2025-12-31', VERSAO=version,
                  DT_RECEB=published_at, ID_DOC=f'{cvm}-{version}', ORDEM_EXERC='\u00daLTIMO',
                  MOEDA='REAL', ESCALA_MOEDA='MIL')
    entries = {'': [('', '', '')],
               '_DRE_con': [('3.99.01.01', 'ON', 2), ('3.99.01.02', 'PN', 4),
                            ('3.11.01', 'Lucro atribuivel a controladora', 100)],
               '_DFC_MI_con': [('6.01', 'Caixa operacional', 80),
                               ('6.02.01', 'Aquisicao de imobilizado e intangivel', -20)],
               '_BPA_con': [('1', 'Ativo total', 1000)],
               '_BPP_con': [('2.01', 'Passivo circulante', 200), ('2.02', 'Passivo nao circulante', 300)]}
    return {suffix: [common | dict(CD_CONTA=code, DS_CONTA=description,
                                  VL_CONTA=(overrides or {}).get(code, value))
                     for code, description, value in rows]
            for suffix, rows in entries.items()}


def dfp_archive(*issuers):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for suffix in ('', '_DRE_con', '_DFC_MI_con', '_BPA_con', '_BPP_con'):
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(issuer()[''][0]), delimiter=';')
            writer.writeheader()
            for data in issuers:
                writer.writerows(data[suffix])
            archive.writestr(f'dfp_cia_aberta{suffix}_2025.csv', stream.getvalue().encode('latin1'))
    return buffer.getvalue()


def test_two_classes_of_requested_issuer_have_independent_eps_and_features():
    diagnostics = []
    other = {'OTHER3': (54321, 'ON', 3)}
    rows = cvm_statements(dfp_archive(issuer(), issuer(54321), issuer(9512)), 2025,
                          specs=SPECS | other, diagnostics=diagnostics)
    assert {(r['cvm'], r['share_class']) for r in rows} == {
        (12345, 'ON'), (12345, 'PN'), (54321, 'ON')}
    dividends = [(date(2026, 3, 10), date(2026, 3, 1), 1)]
    for symbol, eps, fcf in [('TEST3', 2, 1.2), ('TEST4', 4, 2.4)]:
        features, record = financial_features(rows, dividends, AS_OF, SPECS[symbol])
        assert record['share_class'] == SPECS[symbol][1]
        assert features == pytest.approx([1, 1 / 12, eps, fcf, 1 / eps, .5, 2])
    assert all(r['status'] == 'included' for r in diagnostics)
    assert {(r['cvm'], r['share_class']) for r in diagnostics} == {
        (12345, 'ON'), (12345, 'PN'), (54321, 'ON')}


def test_default_specs_preserve_original_universe_and_values():
    content = dfp_archive(issuer(9512), issuer(4170), issuer())
    rows = cvm_statements(content, 2025)
    assert rows == cvm_statements(content, 2025, specs=STOCKS)
    assert rows == cvm_statements(content, 2025, specs=None)
    assert {(r['cvm'], r['share_class']) for r in rows} == {(9512, 'PN'), (4170, 'ON')}
    assert next(r for r in rows if r['cvm'] == 9512)['profit_per_share'] == 4
    assert next(r for r in rows if r['cvm'] == 4170)['profit_per_share'] == 2
    assert STOCKS == {'PETR4': (9512, 'PN', 0), 'VALE3': (4170, 'ON', 1)}


def test_empty_specs_do_not_fall_back_and_duplicate_classes_do_not_duplicate_rows():
    content = dfp_archive(issuer())
    diagnostics = []
    assert cvm_statements(content, 2025, specs={}, diagnostics=diagnostics) == []
    assert diagnostics == []
    rows = cvm_statements(content, 2025, specs=SPECS | {'ALIAS3': (12345, 'ON', 4)})
    assert len(rows) == 2


@pytest.mark.parametrize('problem', ['missing', 'ambiguous', 'zero', 'diluted_only'])
def test_eps_failure_blocks_only_affected_class_without_fallback(problem):
    data = issuer()
    eps = data['_DRE_con'][0]
    if problem == 'missing':
        data['_DRE_con'].remove(eps)
    elif problem == 'ambiguous':
        data['_DRE_con'].append(eps | {'CD_CONTA': '3.99.01.03'})
    elif problem == 'zero':
        eps['VL_CONTA'] = 0
    else:
        eps['CD_CONTA'] = '3.99.02.01'
    diagnostics = []
    rows = cvm_statements(dfp_archive(data), 2025, specs=SPECS, diagnostics=diagnostics)
    assert [r['share_class'] for r in rows] == ['PN']
    blocked = next(r for r in diagnostics if r['share_class'] == 'ON')
    assert blocked['status'] == 'excluded'
    assert 'basic_eps_absent_ambiguous_or_zero' in blocked['reasons']
    assert financial_features(rows, [], AS_OF, SPECS['TEST3']) is None


@pytest.mark.parametrize('bad', ['', None, 'invalid', 'NaN', 'Infinity', '-Infinity', '1e999'])
def test_invalid_eps_never_contaminates_other_class_or_issuer(bad):
    diagnostics = []
    content = dfp_archive(issuer(overrides={'3.99.01.01': bad}), issuer(54321))
    rows = cvm_statements(content, 2025, specs=SPECS | {'OTHER3': (54321, 'ON', 3)},
                          diagnostics=diagnostics)
    assert {(r['cvm'], r['share_class']) for r in rows} == {(12345, 'PN'), (54321, 'ON')}
    blocked = next(r for r in diagnostics if r['cvm'] == 12345 and r['share_class'] == 'ON')
    assert blocked['invalid_numeric_accounts'] == ['3.99.01.01']
    assert 'invalid_numeric_accounts' in blocked['reasons']
    json.dumps(rows + diagnostics, allow_nan=False)


@pytest.mark.parametrize('code', ['3.11.01', '6.01', '1', '2.01', '2.02', '6.02.01'])
@pytest.mark.parametrize('bad', ['', 'invalid', 'NaN', '-Infinity', '1e308'])
def test_invalid_aggregate_values_are_never_imputed(code, bad):
    diagnostics = []
    rows = cvm_statements(dfp_archive(issuer(overrides={code: bad})), 2025,
                          specs=SPECS, diagnostics=diagnostics)
    assert rows == []
    assert all(code in r['invalid_numeric_accounts'] for r in diagnostics)
    assert all(r['status'] == 'excluded' for r in diagnostics)
    json.dumps(diagnostics, allow_nan=False)


@pytest.mark.parametrize('code', ['3.11.01', '6.01', '1', '2.01', '2.02'])
def test_missing_required_account_blocks_both_classes(code):
    data = issuer()
    for suffix in data:
        data[suffix] = [r for r in data[suffix] if r['CD_CONTA'] != code]
    diagnostics = []
    assert cvm_statements(dfp_archive(data), 2025, specs=SPECS, diagnostics=diagnostics) == []
    assert all(r['missing_accounts'] == [code] for r in diagnostics)


@pytest.mark.parametrize('capex', ['absent', 'generic_investments', 'financial_assets', 'inflow', 'zero', 'explicit'])
def test_banking_operating_cash_requires_explicit_nonpositive_capex(capex):
    data = issuer(99999)
    capex_row = data['_DFC_MI_con'][1]
    if capex == 'absent':
        data['_DFC_MI_con'].remove(capex_row)
    elif capex == 'generic_investments':
        capex_row['DS_CONTA'] = 'Investimentos'
    elif capex == 'financial_assets':
        capex_row['DS_CONTA'] = 'Aquisicao de ativos financeiros'
    elif capex == 'inflow':
        capex_row['VL_CONTA'] = 20
    elif capex == 'zero':
        capex_row['VL_CONTA'] = 0
    diagnostics = []
    rows = cvm_statements(dfp_archive(data), 2025, specs={'BANK3': (99999, 'ON', 5)},
                          diagnostics=diagnostics)
    if capex == 'explicit':
        assert rows[0]['free_cash_per_share'] == pytest.approx(1.2)
    elif capex == 'zero':
        assert rows[0]['free_cash_per_share'] == pytest.approx(1.6)
    else:
        assert rows == []
        assert 'capex_not_identified' in diagnostics[0]['reasons']


def test_capex_leaves_are_not_double_counted_or_partially_filled():
    data = issuer()
    parent = data['_DFC_MI_con'][1]
    data['_DFC_MI_con'].extend([
        parent | {'CD_CONTA': '6.02.01.01', 'VL_CONTA': -15},
        parent | {'CD_CONTA': '6.02.01.02', 'VL_CONTA': -5}])
    rows = cvm_statements(dfp_archive(data), 2025, specs=SPECS)
    assert [r['free_cash_per_share'] for r in rows] == pytest.approx([1.2, 2.4])
    data['_DFC_MI_con'][-1]['VL_CONTA'] = 'NaN'
    assert cvm_statements(dfp_archive(data), 2025, specs=SPECS) == []


def test_invalid_duplicate_capex_remains_unavailable():
    data = issuer()
    capex = data['_DFC_MI_con'][1]
    data['_DFC_MI_con'].insert(1, capex | {'VL_CONTA': 'NaN'})
    diagnostics = []
    assert cvm_statements(dfp_archive(data), 2025, specs=SPECS, diagnostics=diagnostics) == []
    assert all(r['invalid_numeric_accounts'] == ['6.02.01'] for r in diagnostics)


@pytest.mark.parametrize('overrides', [
    {'3.11.01': '1e308', '3.99.01.01': '1e-308'},
    {'6.01': '1.7e308', '6.02.01': -1, '3.11.01': 1},
    {'2.01': '1.7e308', '2.02': '1.7e308'},
])
def test_nonfinite_derived_shares_or_features_are_excluded(overrides):
    data = issuer(overrides=overrides)
    for rows in data.values():
        for row in rows:
            row['ESCALA_MOEDA'] = 'UNIDADE'
    diagnostics = []
    assert cvm_statements(dfp_archive(data), 2025, specs={'TEST3': SPECS['TEST3']},
                          diagnostics=diagnostics) == []
    assert any(reason.startswith('invalid_') for reason in diagnostics[0]['reasons'])
    json.dumps(diagnostics, allow_nan=False)


def test_negative_profit_and_eps_remain_signed_and_unknown_scales_still_fail():
    data = issuer(overrides={'3.11.01': -100, '3.99.01.01': -2, '3.99.01.02': -4})
    rows = cvm_statements(dfp_archive(data), 2025, specs=SPECS)
    features, _ = financial_features(rows, [(AS_OF, AS_OF, 1)], AS_OF, SPECS['TEST3'])
    assert features[2] == -2 and features[4] == -.5
    data['_BPA_con'][0]['ESCALA_MOEDA'] = 'UNKNOWN'
    with pytest.raises(ValueError, match='Escala monetaria'):
        cvm_statements(dfp_archive(data), 2025, specs=SPECS)


def test_payout_overflow_does_not_emit_nonfinite_features():
    data = issuer(overrides={'3.11.01': '1e-310', '3.99.01.01': '1e-310'})
    for rows in data.values():
        for row in rows:
            row['ESCALA_MOEDA'] = 'UNIDADE'
    records = cvm_statements(dfp_archive(data), 2025, specs={'TEST3': SPECS['TEST3']})
    assert len(records) == 1
    assert financial_features(records, [(AS_OF, AS_OF, 1)], AS_OF, SPECS['TEST3']) is None


def test_features_filter_class_before_version_and_disclosure_selection():
    latest = issuer(version='2', published_at='2026-04-01', overrides={'3.99.01.01': 8})
    content = dfp_archive(issuer(), latest)
    rows = cvm_statements(content, 2025, specs=SPECS)
    on, pn = SPECS.values()
    assert financial_features(rows, [], AS_OF, on)[1]['version'] == '1'
    assert financial_features(rows, [], date(2026, 4, 30), on)[0][2] == 8
    assert financial_features(rows, [], date(2026, 4, 30), pn)[0][2] == 4
    assert financial_features(rows, [], date(2026, 3, 1), on) is None
    assert financial_features(rows, [], date(2028, 1, 31), on) is None
    pn_only = [r for r in rows if r['share_class'] == 'PN']
    assert financial_features(pn_only, [], AS_OF, on) is None
    legacy = {k: v for k, v in rows[0].items() if k != 'share_class'}
    assert financial_features([legacy], [], AS_OF, on)[1] is legacy
    assert financial_features([legacy], [], AS_OF, pn)[1] is legacy


def test_observations_accept_dynamic_spec_and_preserve_default_call():
    statements = cvm_statements(dfp_archive(issuer(), issuer(9512)), 2025, specs=SPECS | STOCKS)
    dividends = DividendHistory(CUTOFF, 2, basis='record_date')
    dividends.extend([(date(2026, 3, 10), date(2026, 3, 1), 1),
                      (date(2027, 3, 10), date(2027, 3, 1), 2)])
    prices = {AS_OF: ()}
    for symbol in SPECS:
        rows, coverage = stock_observations(symbol, prices, dividends, statements, CUTOFF,
                                            spec=SPECS[symbol])
        assert len(rows) == 1 and coverage['included_windows'] == 1
        row = rows[0]
        assert row['ticker'] == symbol and row['target'] == 2 and row['previous_cash'] == 1
        assert row['profit_per_share'] == (2 if symbol == 'TEST3' else 4)
        assert row['forecast_target_id'] == 'rights_by_record_date_v1'
        assert row['features_available_at'] <= row['as_of']
    assert stock_observations('PETR4', prices, dividends, statements, CUTOFF) == stock_observations(
        'PETR4', prices, dividends, statements, CUTOFF, spec=STOCKS['PETR4'])
