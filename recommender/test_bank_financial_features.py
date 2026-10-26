from copy import deepcopy
from datetime import date
import json

import numpy as np
import pytest
from xgboost import XGBRegressor

from . import engine
from .collect_market_data import cvm_statements, financial_features, write_csv, write_json
from .contracts import Asset, Investor, Plan, RecommendationRequest
from .datasets import FEATURES, demo_dataset, read_csv, real_dataset
from .forecast import CashForecast, fit_forecast
from .portfolios import asset_priority
from .test_cvm_generalization import AS_OF, SPECS, dfp_archive, issuer
from .test_forecast_policy import make_rows
from .test_market_data import dataset_files


ABSENCE = 'industrial_fcf_not_applicable_to_financial_issuer'


def bank_issuer(*, profit_code='3.09.01', equity_code='2.08'):
    data = issuer()
    template = data['_DRE_con'][0]
    data['_DRE_con'][-1] |= {'CD_CONTA': profit_code,
                            'DS_CONTA': 'Atribuido a Socios da Empresa Controladora'}
    data['_DRE_con'].append(template | {'CD_CONTA': '3.09.02',
                                       'DS_CONTA': 'Atribuido a Socios Nao Controladores', 'VL_CONTA': 10})
    data['_BPP_con'] = [template | {'CD_CONTA': code, 'DS_CONTA': desc, 'VL_CONTA': amount}
                       for code, desc, amount in [
                           ('2', 'Passivo Total', 1000),
                           ('2.01', 'Passivos Financeiros ao Valor Justo', 100),
                           ('2.02', 'Passivos Financeiros ao Custo Amortizado', 200),
                           (equity_code, 'Patrimonio Liquido Consolidado', 150),
                           (equity_code + '.01', 'Patrimonio Liquido Atribuivel aos Controladores', 140),
                           (equity_code + '.02', 'Participacao dos Nao Controladores', 10)]]
    return data


def bank_asset(*, features=None, **changes):
    return Asset(ticker='BANK3', name='Test bank', asset_class='acoes', risk_level=2,
                 liquidity_days=2, minimum_years=5, price=20,
                 features=features if features is not None else [1, .5, 2, None, .5, .85, 2],
                 as_of=AS_OF.isoformat(), source='Synthetic bank fixture.', **changes)


@pytest.mark.parametrize('profit_code,equity_code', [('3.09.01', '2.08'), ('3.11.01', '2.07'), ('3.13.01', '2.03')])
def test_bank_accounts_use_attributable_profit_and_root_consolidated_equity(profit_code, equity_code):
    diagnostics = []
    rows = cvm_statements(dfp_archive(bank_issuer(profit_code=profit_code, equity_code=equity_code)), 2025,
                          specs=SPECS, diagnostics=diagnostics)
    assert len(rows) == 2
    for row, eps in zip(rows, [2, 4]):
        assert row['profit_per_share'] == eps
        assert row['free_cash_per_share'] is None
        assert row['debt_ratio'] == pytest.approx(.85)  # Not (2.01 + 2.02) / assets = .30.
        method = row['financial_method']
        assert method['accounts_scope'] == 'consolidated'
        assert method['profit_account'] == profit_code
        assert method['equity_account'] == equity_code
        assert method['liabilities_accounts'] == ['2', equity_code]
        assert method['capex_accounts'] == []
        assert method['fcf_absence_reason'] == ABSENCE
    assert all(r['status'] == 'included' and r['financial_method']['fcf_absence_reason'] == ABSENCE
               for r in diagnostics)
    json.dumps(rows + diagnostics, allow_nan=False)


def test_bank_does_not_require_or_relabel_operating_cash_or_capex():
    data = bank_issuer()
    data['_DFC_MI_con'] = []
    rows = cvm_statements(dfp_archive(data), 2025, specs=SPECS)
    assert len(rows) == 2 and all(r['free_cash_per_share'] is None for r in rows)
    assert cvm_statements(dfp_archive(data), 2025, specs={'TEST3': (12345, 'ON', 1)}) == []


@pytest.mark.parametrize('problem', ['missing', 'zero', 'invalid', 'ambiguous', 'diluted_only'])
def test_bank_missing_or_invalid_basic_eps_remains_excluded_per_class(problem):
    data = bank_issuer()
    eps = data['_DRE_con'][0]
    if problem == 'missing':
        data['_DRE_con'].remove(eps)
    elif problem == 'zero':
        eps['VL_CONTA'] = 0
    elif problem == 'invalid':
        eps['VL_CONTA'] = 'NaN'
    elif problem == 'ambiguous':
        data['_DRE_con'].append(eps | {'CD_CONTA': '3.99.01.03'})
    else:
        eps['CD_CONTA'] = '3.99.02.01'
    diagnostics = []
    rows = cvm_statements(dfp_archive(data), 2025, specs=SPECS, diagnostics=diagnostics)
    assert [r['share_class'] for r in rows] == ['PN']
    assert 'basic_eps_absent_ambiguous_or_zero' in diagnostics[0]['reasons']


@pytest.mark.parametrize('problem', ['no_profit', 'ambiguous_profit', 'no_equity', 'ambiguous_equity',
                                    'no_total', 'invalid_total', 'invalid_equity', 'unbalanced', 'wrong_total_description'])
def test_bank_balance_and_profit_never_fall_back_to_guessed_accounts(problem):
    data = bank_issuer()
    profit = data['_DRE_con'][2]
    equity = data['_BPP_con'][3]
    if problem == 'no_profit':
        profit['DS_CONTA'] = 'Lucro Consolidado Total'
    elif problem == 'ambiguous_profit':
        data['_DRE_con'].append(profit | {'CD_CONTA': '3.11.01'})
    elif problem == 'no_equity':
        data['_BPP_con'].remove(equity)
    elif problem == 'ambiguous_equity':
        data['_BPP_con'].append(equity | {'CD_CONTA': '2.07'})
    elif problem == 'no_total':
        data['_BPP_con'].pop(0)
    elif problem == 'invalid_total':
        data['_BPP_con'][0]['VL_CONTA'] = 'Infinity'
    elif problem == 'invalid_equity':
        equity['VL_CONTA'] = ''
    elif problem == 'unbalanced':
        data['_BPP_con'][0]['VL_CONTA'] = 1100
    else:
        data['_BPP_con'][0]['DS_CONTA'] = 'Passivo Circulante'
    diagnostics = []
    assert cvm_statements(dfp_archive(data), 2025, specs=SPECS, diagnostics=diagnostics) == []
    assert all(r['status'] == 'excluded' for r in diagnostics)
    assert all(r['financial_method']['liabilities_method'] == 'total_passivo_less_consolidated_equity'
               for r in diagnostics)


def test_bank_features_keep_disclosure_and_class_filters_and_signed_earnings():
    records = cvm_statements(dfp_archive(bank_issuer()), 2025, specs=SPECS)
    assert financial_features(records, [], date(2026, 3, 1), SPECS['TEST3']) is None
    features, _ = financial_features(records, [(AS_OF, AS_OF, 1)], AS_OF, SPECS['TEST3'])
    assert features == [1, 1 / 12, 2, None, .5, .85, 2]
    assert financial_features(records, [], AS_OF, (12345, 'ON', 1)) is None
    assert financial_features(records, [], AS_OF, (12345, 'PNA', 2)) is None
    data = bank_issuer()
    data['_DRE_con'][0]['VL_CONTA'] = -2
    data['_DRE_con'][2]['VL_CONTA'] = -100
    records = cvm_statements(dfp_archive(data), 2025, specs={'TEST3': SPECS['TEST3']})
    assert financial_features(records, [(AS_OF, AS_OF, 1)], AS_OF, SPECS['TEST3'])[0][4] == -.5


@pytest.mark.parametrize('index', [0, 1, 2, 4, 5, 6])
def test_asset_rejects_missing_features_outside_bank_fcf(index):
    features = [1, .5, 2, 1, .5, .85, 2]
    features[index] = None
    with pytest.raises(ValueError):
        bank_asset(features=features)


@pytest.mark.parametrize('fcf,sector', [(None, 1), (float('nan'), 2), (float('inf'), 2)])
def test_asset_external_missing_contract_is_null_financial_fcf_only(fcf, sector):
    with pytest.raises(ValueError):
        bank_asset(features=[1, .5, 2, fcf, .5, .85, sector])


def test_bank_asset_null_serializes_and_industrial_features_stay_compatible():
    asset = bank_asset()
    assert json.loads(asset.model_dump_json())['features'][3] is None
    assert bank_asset(features=[1, .5, 2, 1, .5, .85, 1]).features[3] == 1


def test_dataset_round_trip_keeps_null_bank_fcf_and_csv_blank(tmp_path):
    dataset_files(tmp_path)
    metadata = json.loads((tmp_path / 'assets.json').read_text(encoding='utf-8'))
    metadata['assets'][0].update(asset_class='acoes', features=bank_asset().features)
    write_json(tmp_path / 'assets.json', metadata)
    rows = read_csv(tmp_path / 'observations.csv', FEATURES)
    rows[0].update(free_cash_per_share=None, sector=2)
    write_csv(tmp_path / 'observations.csv', list(rows[0]), rows)
    loaded = real_dataset(tmp_path)
    assert loaded.assets[0].features[3] is None
    assert loaded.observations[0]['features'][3] is None
    json.dumps(loaded.assets[0].model_dump(), allow_nan=False)
    rows[0]['sector'] = 1
    write_csv(tmp_path / 'observations.csv', list(rows[0]), rows)
    with pytest.raises(ValueError, match='financial-sector FCF'):
        real_dataset(tmp_path)


@pytest.mark.parametrize('token', ['NaN', 'nan', 'Infinity', 'null'])
def test_dataset_rejects_nonfinite_or_implicit_csv_tokens(tmp_path, token):
    dataset_files(tmp_path)
    rows = read_csv(tmp_path / 'observations.csv', FEATURES)
    rows[0].update(free_cash_per_share=token, sector=2)
    write_csv(tmp_path / 'observations.csv', list(rows[0]), rows)
    with pytest.raises(ValueError):
        real_dataset(tmp_path)


def test_xgboost_uses_native_missing_bank_fcf_without_imputation():
    features = [[1, .5, 2, None, .5, .85, 2], [2, .5, 3, 1.5, .6, .5, 1]]
    transformed, scale = CashForecast.transform(features)
    assert np.isnan(transformed[0, 3])
    assert transformed[1, 3] == .75
    model = CashForecast(XGBRegressor(n_estimators=4, max_depth=1, n_jobs=1, random_state=41))
    model.fit(features, [1.2, 2.2])
    assert np.isfinite(model.predict(features)).all()
    assert features[0][3] is None
    assert CashForecast(None, method='baseline').predict(features).tolist() == [1, 2]


@pytest.mark.parametrize('index,sector', [(0, 2), (1, 2), (2, 2), (4, 2), (5, 2), (6, 2), (3, 1)])
def test_model_rejects_native_missing_outside_financial_fcf(index, sector):
    features = [[1, .5, 2, 1, .5, .85, sector]]
    features[0][index] = float('nan')
    with pytest.raises(ValueError):
        CashForecast._features(features)


def test_forecast_metrics_and_inputs_remain_json_safe_with_mixed_bank_rows():
    rows = make_rows()
    for row in rows:
        if row['features'][6] == 2:
            row['features'][3] = None
    original = deepcopy(rows)
    model, metrics = fit_forecast(rows)
    assert rows == original
    assert np.isfinite(model.predict([rows[-1]['features']])).all()
    json.dumps({'metrics': metrics, 'rows': rows}, allow_nan=False)


def test_bank_ranking_and_full_engine_result_keep_null_fcf(monkeypatch):
    data = demo_dataset()
    bank = data.assets[4]
    bank.features[3], bank.features[6] = None, 2
    records = cvm_statements(dfp_archive(bank_issuer()), 2025, specs=SPECS)
    bank.financial_method = records[0]['financial_method']
    for row in data.observations:
        if row['features'][6] == 2:
            row['features'][3] = None
    ranking = asset_priority([bank], data.returns[:, [4]], None, data.dates, [0.1])
    assert any('indisponivel' in alert for alert in ranking[0]['qualityAlerts'])
    assert not any('nao positivo' in alert for alert in ranking[0]['qualityAlerts'])
    monkeypatch.setattr(engine, 'get_dataset', lambda mode: data)
    request = RecommendationRequest(investor=Investor(risk_level=2, knowledge='Avançado',
                                                       withdrawal_years=6, high_liquidity=False),
                                    plan=Plan(age=30, retirement_age=31, end_age=33, initial_capital=100000,
                                              monthly_contribution=500, target_income=1000,
                                              annual_cost_percent=0, seed=42, simulations=500), data_mode='demo')
    result = engine.compute(request, generations=3, population=8, search_count=8)
    analysis = next(row for row in result['assetAnalysis'] if row['ticker'] == bank.ticker)
    assert analysis['eligible'] is True
    assert analysis['indicators']['freeCashFlowPerShare'] is None
    assert analysis['indicators']['freeCashFlowAbsenceReason'] == ABSENCE
    assert analysis['financialMethod']['accounts_scope'] == 'consolidated'
    json.dumps(result, allow_nan=False)
