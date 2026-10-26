"""Exercise real staged data and all four algorithms without publishing it."""

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recommender.contracts import RecommendationRequest
from recommender.datasets import real_dataset
from recommender.engine import compute
from recommender.treasury_analysis import current_data_revision
from recommender.dividend_scope import allowed_tickers, scope_metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('personas', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--current-contracts', action='store_true')
    args = parser.parse_args()
    os.environ['RF_DATA_DIR'] = str(args.snapshot.resolve())
    data = real_dataset(args.snapshot)
    preparation = data.metadata['stock_universe_preparation']
    assert data.metadata['data_quality_policy'] == 'RF-CALENDAR-RECONCILED-1'
    assert data.metadata['cashflow_basis'] == 'reported_historical_cash_reconciled'
    assert data.metadata['cash_settlement_verified'] is False
    assert data.metadata['point_in_time_archive_verified'] is False
    assert len(preparation['stocks']) == len({row['ticker'] for row in preparation['stocks']}) == 468
    stocks = {asset.ticker for asset in data.assets if asset.asset_class == 'acoes'}
    selected = allowed_tickers(args.snapshot)
    if selected is not None:
        assert stocks.issubset(selected)
        training_allowed = selected | {asset.ticker for asset in data.assets if asset.asset_class != 'acoes'}
        assert all(row.get('ticker') in training_allowed for row in data.observations)
    assert {row['ticker'] for row in preparation['stocks'] if row['model_ready']} == stocks
    assert all(row['preparation_basis'] == 'rebuilt_from_reconciled_sources'
               for row in preparation['stocks'] if row['model_ready'])
    ledger = json.loads((args.snapshot / 'payment-ledger.json').read_text(encoding='utf-8'))
    stock_rows = [row for row in ledger if row['ticker'] in stocks]
    assert all(row['payment_date'] <= data.dates[-1] for row in stock_rows)
    assert all(row['point_in_time_archive_verified'] is False for row in stock_rows)
    assert all(row['availability_basis'] != 'payment_date_retrospective_observation_not_publication'
               or row.get('published_at') is None for row in stock_rows)
    personas = json.loads(args.personas.read_text(encoding='utf-8'))['personas']
    audit = {'snapshot': args.snapshot.name, 'reference': data.dates[-1],
        'real_assets': len(data.assets), 'stocks': len(stocks), 'same_policy_for_all_stocks': True,
        'source_status_counts': dict(Counter(row.get('reported_payment_status') for row in stock_rows)),
        'scope': 'synthetic_profiles_real_market_data_operational_not_financial_validation', 'cases': []}
    audit['dividendYieldSelection'] = scope_metadata(args.snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for persona in personas:
        profile, answers = persona['result'], persona['answers']
        investor = {'risk_level': ['Conservador', 'Moderado', 'Arrojado'].index(profile['profile']),
            'knowledge': profile['knowledge'],
            'withdrawal_years': [0, 1, 3, 6][['Em menos de 1 ano', 'Entre 1 e 3 anos',
                'Entre 3 e 5 anos', 'Depois de 5 anos'].index(answers['withdrawal'])],
            'high_liquidity': answers['liquidity'] == 'Muito provavel' or answers['liquidity'] == 'Muito prov\u00e1vel'}
        request = RecommendationRequest(investor=investor, data_mode='real', forecast_policy='xgboost_experimental',
            plan={'income_mode': 'distributions_only', 'age': persona['age'],
                'retirement_age': persona['retirementAge'], 'end_age': 90,
                'initial_capital': persona['capital'], 'monthly_contribution': persona['contribution'],
                'target_income': persona['target'], 'annual_cost_percent': .5,
                'simulations': 500, 'block_months': 6, 'seed': 42})
        if args.current_contracts:
            from recommender.current_portfolio import compute_current
            result = compute_current(request, data, generations=10, population=32, search_count=64)
        else:
            result = compute(request, generations=10, population=32, search_count=64)
        assert result['status'] == 'ready'
        assert result['universeScreening']['counts']['readyStocks'] == len(stocks)
        assert result['incomeAnalysis']['usesPrincipal'] is False
        assert result['rollover']['principalCountedAsIncome'] is False
        assert result['financiallyValidated'] is False
        assert result['dataRevision'] == current_data_revision(data)
        treasury = result.get('currentTreasuryAnalysis')
        if treasury:
            assert treasury['evaluatedContracts'] == treasury['cashScenariosCalculated'] == len(treasury['rows']) == 37
            assert treasury['historicalPriceSeriesCreated'] is False
            assert treasury['cashSettlementConfirmed'] is False
            assert all(row['principalCountedAsIncome'] is False and row['reinvestmentIncluded'] is False
                       for row in treasury['rows'])
            if args.current_contracts:
                assert treasury['optimizedContracts'] > 0
                assert treasury['portfolioReinvestmentIncluded'] is True
                assert sum(row['includedInPortfolioOptimization'] for row in treasury['rows']) == treasury['optimizedContracts']
                assert result['parameters']['annualRebalance'] is False
            else:
                assert treasury['optimizedContracts'] == 0
                assert all(row['includedInPortfolioOptimization'] is False for row in treasury['rows'])
        assert math.isclose(sum(row['weight'] for row in result['allocation']), 1, abs_tol=1e-6)
        assert result['portfolio']['annualVolatility'] <= result['parameters']['annualVolatilityMaximum'] + 1e-6
        valid = {asset.ticker for asset in data.assets if asset.asset_class == 'acoes'} | {
            row['ticker'] for row in result['assetAnalysis'] if row['ticker'].startswith('TD-')}
        assert all(row['ticker'] in valid for row in result['allocation'])
        if result['universeScreening']['counts']['eligibleStocks']:
            assert 'XGBoost' in result['methods'] and result['model']['usedForIncomeSimulation']
        audit['cases'].append({'id': persona['id'], 'profile': profile['profile'], 'result': result})
        args.output.write_text(json.dumps(audit, ensure_ascii=True, allow_nan=False), encoding='utf-8')
        print(json.dumps({'persona': persona['id'], 'profile': profile['profile'],
            'eligible_stocks': result['universeScreening']['counts']['eligibleStocks'],
            'methods': result['methods'], 'passed': True}), flush=True)
    assert len(audit['cases']) == 8
    audit['completed'] = True
    args.output.write_text(json.dumps(audit, ensure_ascii=True, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
