from datetime import date, timedelta

import numpy as np
import pytest

from .current_portfolio import (_current_stock, _yield_paths, contract_shortlist, drifting_risk_model)
from .contracts import Asset
from .test_treasury_market import offer
from .treasury_market import instrument_from_offer


def test_current_stock_uses_observed_quote_not_stale_model_price_or_new_features():
    asset = Asset(ticker='TEST3', name='Synthetic test stock', asset_class='acoes', risk_level=2,
        minimum_years=5, liquidity_days=2, price=10., as_of='2026-01-01',
        source='Synthetic quote test, not market data.', features=[1., .5, 2., 1., .5, .4, 1.],
        current_quote={'price': 25., 'as_of': date.today().isoformat(), 'source_url': 'https://example.test/quote',
            'purchase_available': True, 'basis': 'synthetic_test'})
    current = _current_stock(asset)
    assert current.price == 25. and asset.price == 10.
    assert current.features == asset.features
    stale = asset.model_copy(update={'current_quote': asset.current_quote.model_copy(
        update={'as_of': (date.today() - timedelta(days=8)).isoformat()})})
    with pytest.raises(ValueError, match='stale'):
        _current_stock(stale)


def test_shortlist_dated_family_match_is_independent_of_scenario_seed_and_keeps_risk():
    items = [instrument_from_offer(offer(maturity=day)) for day in ('2028-01-01', '2030-01-01', '2032-01-01')]
    selected = contract_shortlist(items, [0, 1, 2], date(2030, 6, 1))
    assert selected == [1]
    assert items[1].asset.risk_level == 1
    assert contract_shortlist(items, [0, 2], date(2030, 6, 1)) == [2]


def test_reserva_can_be_valued_but_no_purchase_is_prescribed_without_required_bank_access():
    item = instrument_from_offer(offer('Tesouro Reserva', maturity='2036-01-01',
        buy_rate=None, buy_rate_text='SELIC', isin='BRSTNCT1D008', catalogue_group='Tesouro24x7'))
    assert contract_shortlist([item], [0], date(2030, 1, 1)) == []


def test_factor_proxy_changes_are_anchored_at_actual_current_instrument_rate():
    item = instrument_from_offer(offer(buy_rate=7.))
    sampled = {'inflation': np.zeros((2, 4)), 'initial_levels': {'nominal_yield': .10},
               'nominal_yield': np.array([[.11, .12, .09, .10], [.09, .08, .10, .11]])}
    paths = _yield_paths(item, sampled, 3)
    np.testing.assert_allclose(paths, [[7, 8, 9, 6], [7, 6, 5, 7]])


def test_risk_uses_all_simulated_annual_states_not_only_initial_year():
    from types import SimpleNamespace
    wealth = np.ones((2, 3, 5, 2))
    wealth[1] = 0
    wealth[0, 2, :, 1] = [1, 2, 1, 2, 1]
    factors = {'wealth_factors': wealth, 'annual_income_factors': np.zeros((2, 2, 5, 2))}
    risk = drifting_risk_model(factors, SimpleNamespace(initial_capital=1, monthly_contribution=0,
                                                       age=30, retirement_age=31))
    assert risk([1, 0])[0] == 0
    assert risk([0, 1])[0] > .4


def test_actual_portfolio_drift_cannot_pass_a_limit_using_stale_initial_weights():
    from types import SimpleNamespace
    wealth = np.ones((2, 3, 4, 2))
    wealth[1] = 0
    wealth[0, 1, :, 0] = 10
    wealth[0, 2, :, 0] = [6, 14, 6, 14]
    factors = {'wealth_factors': wealth, 'annual_income_factors': np.zeros((2, 2, 4, 2))}
    risk = drifting_risk_model(factors, SimpleNamespace(initial_capital=100, monthly_contribution=0,
                                                       age=30, retirement_age=31))
    expected = np.std([-.4 * 10 / 11, .4 * 10 / 11, -.4 * 10 / 11, .4 * 10 / 11], ddof=1)
    assert risk([.5, .5])[0] == pytest.approx(expected)
    assert risk([.5, .5])[0] > .28


@pytest.mark.parametrize('calendar_start', [False, True])
def test_current_portfolio_preserves_forecast_and_planning_in_complete_response(monkeypatch, calendar_start):
    import json
    from types import SimpleNamespace
    from . import current_portfolio as module
    from .contracts import RecommendationRequest
    from .datasets import Dataset
    stock = Asset(ticker='TEST3', name='Synthetic response fixture', asset_class='acoes', risk_level=2,
        minimum_years=0, liquidity_days=2, price=10., as_of=date.today().isoformat(),
        source='Synthetic test, not real market data.', features=[.6, .5, 2., 1., .3, .4, 1.],
        current_quote={'price': 10., 'as_of': date.today().isoformat(), 'source_url': 'https://example.test/quote',
                       'purchase_available': True, 'basis': 'synthetic_test'})
    preparation = {'stocks': [{'ticker': 'TEST3', 'name': stock.name, 'model_ready': True, 'reason_codes': []}],
        'ready_stocks': 1, 'evaluated_stocks': 1, 'pending_stocks': 0, 'as_of': '2026-09-30',
        'prepared_at': '2026-10-23T00:00:00Z', 'inventory_sha256': '0' * 64}
    data = Dataset([stock], np.full((3, 1), .01), np.zeros(3),
        ['2026-07-31', '2026-08-31', '2026-09-30'], [], 'real', 'Synthetic fixture, not market data.',
        {'stock_universe_preparation': preparation}, np.full((3, 1), .005))
    if calendar_start:
        data.metadata['cash_calendar_date_qualifications'] = [
            {'ticker': stock.ticker, 'date_basis': 'explicit_payment_start_date_not_settlement'}]
    item = instrument_from_offer(offer('Tesouro Selic', maturity='2030-03-01', rate_basis='spread_over_selic'))
    receipt = {'url': 'https://example.test/quote', 'sha256': '0' * 64, 'retrieved_at': '2026-10-23T00:00:00Z'}
    monkeypatch.setattr(module, 'read_current_instruments', lambda _: ([item], receipt))
    monkeypatch.setattr(module, 'load_treasury_yield_factors', lambda *args: {
        'inflation': [0., 0., 0.], 'revision': 'synthetic-factors', 'sources': []})
    def sample(history, months, count, block, seed, **kwargs):
        length = months + kwargs.get('padding_months', 0)
        return {'inflation': np.zeros((count, length)), 'selic': np.full((count, length), .005),
            'selic_spread': np.full((count, length), .07), 'initial_levels': {'selic_spread': .07},
            'future_values_observed': False, 'projection_policy': 'synthetic_test_only'}
    monkeypatch.setattr(module, 'yield_factor_scenarios', sample)
    monkeypatch.setattr(module, 'analyze_current_treasury', lambda *args: {'rows': [{
        'ticker': item.asset.ticker, 'income12MonthsReal': [0., 0., 0.]}]})
    monkeypatch.setattr(module, 'get_forecast', lambda *args, **kwargs: (SimpleNamespace(
        predict=lambda values: np.full(len(values), .6), predict_xgboost=lambda values: np.full(len(values), .7)),
        {'selectedMethod': 'baseline', 'baselineMae': .1, 'xgboostMae': .2}))
    monkeypatch.setattr(module, 'coverage_summary', lambda _: {'counts': {}, 'gaps': []})
    monkeypatch.setattr(module, 'current_data_revision', lambda _: 'synthetic-current-revision')
    request = RecommendationRequest(data_mode='real', forecast_policy='xgboost_experimental',
        investor={'risk_level': 2, 'knowledge': 'Avan\u00e7ado', 'withdrawal_years': 6, 'high_liquidity': False},
        plan={'income_mode': 'distributions_only', 'age': 30, 'retirement_age': 31, 'end_age': 33,
            'initial_capital': 1000, 'monthly_contribution': 50, 'target_income': 1,
            'annual_cost_percent': 0, 'simulations': 500, 'block_months': 3, 'seed': 42})
    result = module.compute_current(request, data, generations=2, population=8, search_count=8)
    assert result['model']['appliedMethod'] == 'xgboost'
    assert result['model']['usedForIncomeSimulation'] is True
    assert result['parameters']['annualRebalance'] is False
    assert result['rollover']['contractualRedemptionPricing'] is True
    assert result['currentTreasuryAnalysis']['portfolioReinvestmentIncluded'] is True
    assert any('inicio declarado de pagamento' in warning for warning in result['warnings']) is calendar_start
    assert result['planning']['requiredRetirementCapitalBasis'] == 'independently_modeled_retirement_start_unit_annual_income'
    assert all(row['planning']['requiredRetirementCapitalNullReason'] !=
        'independent_retirement_start_unit_income_factors_not_supplied' for row in result['portfolios'])
    json.dumps(result, allow_nan=False)
