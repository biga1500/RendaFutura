from copy import deepcopy
from types import SimpleNamespace

import numpy as np

from . import treasury_analysis as module
from .contracts import RecommendationRequest
from .test_treasury_market import offer
from .treasury_market import instrument_from_offer


def request(level=1):
    return RecommendationRequest(investor={'risk_level': level, 'knowledge': 'Intermediário',
        'withdrawal_years': 6, 'high_liquidity': False},
        plan={'age': 30, 'retirement_age': 50, 'end_age': 90, 'initial_capital': 50000,
              'monthly_contribution': 1500, 'target_income': 5000, 'annual_cost_percent': .5,
              'simulations': 500, 'seed': 42}, data_mode='real')


def test_no_current_source_never_promotes_legacy_or_fictitious_titles(tmp_path):
    data = SimpleNamespace(metadata={'stock_universe_preparation': {'prepared_at': 'test-revision'}})
    assert module.analyze_current_treasury(request(), data, tmp_path) is None
    assert module.current_data_revision(data, tmp_path) == 'test-revision'


def test_every_current_contract_gets_cash_scenario_but_not_false_optimization_claim(tmp_path, monkeypatch):
    source = tmp_path / 'raw/current/official-investir.json'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'test-fixture')
    macro = tmp_path / 'raw/primary-research/macro/current.json'
    macro.parent.mkdir(parents=True)
    macro.write_bytes(b'test-fixture')
    model = instrument_from_offer(offer())
    monkeypatch.setattr(module, 'read_current_instruments', lambda _: ([model], {'fixture': True}))
    history = {'dates': ['2026-08-31', '2026-09-30'], 'inflation': [0, 0], 'selic': [0, 0],
               'reference_date': '2026-09-30', 'sources': [{'fixture': True}]}
    monkeypatch.setattr(module, 'load_macro_factors', lambda _: (history, 'test-macro-hash'))
    monkeypatch.setattr(module, 'factor_scenarios', lambda history, months, count, block, seed:
        {'inflation': np.zeros((count, months)), 'selic': np.zeros((count, months)), 'future_values_observed': False})
    data = SimpleNamespace(metadata={'stock_universe_preparation': {'prepared_at': 'test-revision'}})
    result = module.analyze_current_treasury(request(0), data, tmp_path)
    assert result['cashScenariosCalculated'] == result['evaluatedContracts'] == 1
    assert result['compatibleContracts'] == result['optimizedContracts'] == 0
    row = result['rows'][0]
    assert row['income12MonthsReal'] == [0, 0, 0]
    assert row['principalCountedAsIncome'] is row['includedInPortfolioOptimization'] is False
    assert row['incomeHorizonOriginalLotReal'] == [200, 200, 200]
    assert row['reinvestmentIncluded'] is row['futurePaymentsObserved'] is False
    assert result['historicalPriceSeriesCreated'] is result['cashSettlementConfirmed'] is False
    assert result['scope'] == 'original_current_quote_lot_only_not_full_reinvestment_portfolio'
    prior_revision = result['dataRevision']
    source.write_bytes(b'new-current-quotes')
    assert module.current_data_revision(data, tmp_path) != prior_revision
