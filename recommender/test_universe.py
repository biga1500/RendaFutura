from copy import deepcopy

import numpy as np
import pytest

from .contracts import Asset, Investor
from .datasets import Dataset
from .universe import preparation_summary, screen_universe


def fixture():
    assets = [Asset(ticker='T0003', name='Synthetic fixture', asset_class='acoes', risk_level=2,
        liquidity_days=2, minimum_years=5, price=10, as_of='2026-09-30', source='Synthetic unit test fixture'),
        Asset(ticker='T0013', name='Synthetic fixture two', asset_class='acoes', risk_level=2,
        liquidity_days=2, minimum_years=5, price=20, as_of='2026-09-30', source='Synthetic unit test fixture')]
    rows = [{'ticker': f'T{i:03d}3', 'name': 'Synthetic unit test fixture', 'model_ready': i < 2,
             'quote': {'value': None}, 'reason_codes': [] if i < 2 else ['missing_cached_source']}
            for i in range(468)]
    metadata = {'stock_universe_preparation': {'stocks': rows, 'as_of': '2026-10-24',
        'prepared_at': '2026-10-24T12:00:00+00:00', 'inventory_sha256': 'a' * 64}}
    return Dataset(assets, np.zeros((1, 2)), np.zeros(1), ['2026-09-30'], [], 'real', 'Test fixture', metadata)


def investor(level=2, years=6):
    return Investor(risk_level=level, withdrawal_years=years, high_liquidity=False, knowledge='Intermediário')


def test_all_468_are_processed_without_imputing_466_histories():
    data = fixture()
    before = deepcopy(data.metadata)
    result = screen_universe(data, investor(), [0, 1], {'T0003': 0, 'T0013': 2})
    assert result['counts'] == {'stocks': 468, 'readyStocks': 2, 'pendingDataStocks': 466, 'annualAccountsStocks': 0,
        'profileCompatibleStocks': 468, 'excludedByProfileStocks': 0, 'eligibleStocks': 2, 'forecastedStocks': 2}
    assert all(row['predictedCashPerShare'] is None for row in result['stocks'][2:])
    assert all(not row['eligible'] and row['dataReasons'] for row in result['stocks'][2:])
    assert data.returns.shape == (1, 2) and data.metadata == before
    assert result['missingDataAreZero'] is result['unpreparedAssetsAllocated'] is False


def test_profile_rejection_does_not_change_data_readiness():
    result = screen_universe(fixture(), investor(0), [])
    assert result['counts']['readyStocks'] == 2
    assert result['counts']['excludedByProfileStocks'] == 468
    assert result['counts']['eligibleStocks'] == result['counts']['forecastedStocks'] == 0
    assert result['stocks'][0]['dataReasons'] == []
    assert 'Risco' in result['stocks'][0]['profileReasons'][0]


def test_short_horizon_has_a_profile_reason_even_with_available_data():
    result = screen_universe(fixture(), investor(years=1), [])
    assert 'Prazo' in result['stocks'][0]['profileReasons'][0]
    assert result['stocks'][0]['dataReady'] is True


@pytest.mark.parametrize('fault', ['duplicate', 'missing_prepared', 'wrong_ready_flag', 'bad_ticker'])
def test_inconsistent_universe_is_rejected(fault):
    data = fixture()
    rows = data.metadata['stock_universe_preparation']['stocks']
    if fault == 'duplicate':
        rows.append(rows[0].copy())
    elif fault == 'missing_prepared':
        rows.pop(0)
    elif fault == 'wrong_ready_flag':
        rows[0]['model_ready'] = False
    else:
        rows[2]['ticker'] = '../secret'
    with pytest.raises(ValueError):
        screen_universe(data, investor(), [0, 1])


@pytest.mark.parametrize('ticker,indices', [('T0023', [0, 1]), ('T0003', [])])
def test_forecasts_cannot_be_assigned_to_unprepared_or_ineligible_stocks(ticker, indices):
    with pytest.raises(ValueError, match='Previsao'):
        screen_universe(fixture(), investor(), indices, {ticker: 1})


def test_summary_and_legacy_dataset():
    data = fixture()
    assert preparation_summary(data)['evaluatedStocks'] == 468
    data.metadata = {}
    assert preparation_summary(data) is None
    assert screen_universe(data, investor(), []) is None
