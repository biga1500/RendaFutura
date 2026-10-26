import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .api import app
from .contracts import Investor, Plan, RecommendationRequest
from .datasets import get_dataset
from .engine import bounds_for, compute, eligible_assets, project_weights
from .forecast import get_forecast
from .simulation import bootstrap_paths, simulate


def plan(**overrides):
    values = dict(age=30, retirement_age=31, end_age=32, initial_capital=1200,
                  monthly_contribution=100, target_income=100, annual_cost_percent=0, seed=42)
    return Plan(**(values | overrides))


def investor(level=1):
    return Investor(risk_level=level, knowledge='Intermediário', withdrawal_years=6, high_liquidity=False)


def test_cash_conservation_and_retirement_boundary():
    out = simulate([.4, .6], np.zeros((3,24,2)), plan(), detailed=True)
    assert out['retirementWealth'] == [2400]*3
    assert out['finalWealth'] == [1200]*3
    assert out['successFrequency'] == 1


def test_unfunded_goal_not_successful():
    out = simulate([1], np.zeros((3,24,1)), plan(initial_capital=0, monthly_contribution=0), detailed=True)
    assert out['successFrequency'] == 0
    assert out['depletionFrequency'] == 1
    assert out['medianDeficit'] == 1200


def test_joint_bootstrap_reproducible_and_inflation_adjusted():
    history = np.column_stack([np.arange(20)/1000, np.arange(20)/1000])
    inflation = history[:,0].copy()
    one = bootstrap_paths(history, inflation, 24, 3, 6, 42)
    np.testing.assert_array_equal(one, bootstrap_paths(history, inflation, 24, 3, 6, 42))
    np.testing.assert_allclose(one, 0)


def test_limits_are_not_relaxed():
    dataset = get_dataset('demo')
    indices, excluded = eligible_assets(dataset, investor(0))
    assets = [dataset.assets[i] for i in indices]
    assert all(a.risk_level == 0 for a in assets)
    assert excluded
    upper, matrix, bounds, feasible, _, _ = bounds_for(assets,0)
    weights = project_weights(np.full(len(assets),2),upper,matrix,bounds,feasible)
    assert weights.sum() == pytest.approx(1)
    assert np.all(weights >= 0)
    assert np.all(matrix @ weights <= bounds + 1e-7)
    with pytest.raises(ValueError,match='viável'):
        bounds_for([a for a in dataset.assets if a.asset_class == 'acoes'],1)


def test_temporal_validation_purges_forward_labels():
    _, report = get_forecast('demo')
    assert report['maxTrainLabelEnd'] < report['validationStart']
    assert report['maxValidationLabelEnd'] < report['testStart']
    assert report['mae'] >= 0 and report['testRows'] > 0
    assert report['financiallyValidated'] is False


def test_full_engine_demo_constraints_and_impossible_goal():
    request = RecommendationRequest(investor=investor(), plan=plan(age=30, retirement_age=50,end_age=90,
                                    initial_capital=0,monthly_contribution=1500,target_income=100000),data_mode='demo')
    result = compute(request,generations=3,population=8,search_count=8)
    assert sum(a['weight'] for a in result['allocation']) == pytest.approx(1,abs=1e-6)
    equities = [a for a in result['allocation'] if a['assetClass'] == 'acoes']
    assert sum(a['weight'] for a in equities) <= .4 + 1e-7
    assert all(a['weight'] <= .25 + 1e-7 for a in equities)
    assert result['portfolio']['annualVolatility'] <= .16 + 1e-7
    assert result['simulation']['successFrequency'] == 0
    assert result['model'] is not None
    assert result['financiallyValidated'] is False and result['dataMode'] == 'demo'


@pytest.mark.parametrize('overrides',[{'end_age':31},{'target_income':float('nan')},{'monthly_contribution':-1}])
def test_invalid_plans(overrides):
    with pytest.raises(ValidationError):
        plan(**overrides)


def test_service_auth_validation_and_body_limits(monkeypatch):
    token = 'test-only-not-production-token-123456789'
    monkeypatch.setenv('RECOMMENDER_TOKEN',token)
    monkeypatch.delenv('RF_DATA_DIR',raising=False)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/status').status_code == 401
        headers = {'Authorization':'Bearer '+token}
        assert client.get('/status',headers=headers).json()['realDataAvailable'] is False
        assert client.post('/recommend',headers=headers,json={'email':'personal@example.com'}).status_code == 422
        assert client.post('/recommend',headers=headers,content='x'*4100).status_code == 413
        assert client.get('/docs').status_code == 404
