import calendar
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from .contracts import Investor, RecommendationRequest
from .datasets import FEATURES, dataset_revision, read_csv, real_dataset
from .treasury_analysis import current_data_revision
from .universe import preparation_summary, screen_universe


UNMATCHED = ['AURA33', 'MRSA5B', 'MRSA6B', 'EQPA7']


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def market(tmp_path):
    from .dividend_scope import import_scope

    snapshot = tmp_path / 'snapshots/test-scope'
    snapshot.mkdir(parents=True)
    serial = date.today().year * 12 + date.today().month - 2
    dates = []
    for offset in range(59, -1, -1):
        year, month = divmod(serial - offset, 12)
        month += 1
        dates.append(date(year, month, calendar.monthrange(year, month)[1]).isoformat())
    tickers = [f'T{i:03d}3' for i in range(464)] + ['AURA3', 'MRSA5', 'MRSA6', 'EQPA3']
    selected = frozenset(tickers[:100] + tickers[112:270] + UNMATCHED)
    stocks = [{'ticker': ticker, 'name': 'Synthetic scope fixture', 'model_ready': i < 112,
               'reason_codes': [] if i < 112 else ['missing_cached_source'],
               'source_review_reason_codes': [], 'quote': {'value': 10}}
              for i, ticker in enumerate(tickers)]
    assets = []
    for i, ticker in enumerate(tickers[:112] + [f'TD-{i:02d}' for i in range(37)]):
        stock = i < 112
        assets.append({'ticker': ticker, 'name': 'Synthetic scope fixture',
                       'asset_class': 'acoes' if stock else 'liquidez' if i == 112 else 'renda_fixa',
                       'risk_level': 2 if stock else 0 if i == 112 else 1,
                       'liquidity_days': 2 if stock else 1 if i == 112 else 30,
                       'minimum_years': 5 if stock else 0, 'price': 10,
                       'features': [1, .5, 2, 2, .5, .5, 0] if stock else None,
                       'annual_cash_yield': None if stock else 0,
                       'as_of': dates[-1], 'source': 'Synthetic unit test fixture, not market data.'})
    columns = [row['ticker'] for row in assets]
    write_csv(snapshot / 'returns.csv', ['date', 'inflation', *columns],
              [{'date': day, 'inflation': .001, **{ticker: (i + 1) / 10000 for i, ticker in enumerate(columns)}}
               for day in dates])
    write_csv(snapshot / 'cashflows.csv', ['date', *columns],
              [{'date': day, **{ticker: i / 100000 for i, ticker in enumerate(columns)}} for day in dates])
    observation = {'as_of': dates[-24], 'published_at': dates[-24], 'label_end': dates[-12],
                   **dict(zip(FEATURES, [1, .5, 2, 2, .5, .5, 0])), 'target': 1}
    write_csv(snapshot / 'observations.csv', ['ticker', *observation],
              [dict(ticker=ticker, **observation) for ticker in tickers[:112] + ['UNKN3', '', 'TD-00']])
    metadata = {'assets': assets, 'returns_include_distributions': True,
                'source': 'Synthetic scope fixture, not market data.', 'collected_at': 'test-collection',
                'cashflow_basis': 'reported_historical_cash_reconciled',
                'data_quality_policy': 'RF-CALENDAR-RECONCILED-1',
                'point_in_time_archive_verified': False, 'cash_settlement_verified': False,
                'cashflow_excludes_principal': True,
                'cashflow_source': {'url': 'https://example.invalid/test-fixture', 'sha256': '0' * 64},
                'cashflows_sha256': hashlib.sha256((snapshot / 'cashflows.csv').read_bytes()).hexdigest(),
                'collector_stocks': tickers[:112], 'expanded_stocks': [{'ticker': t} for t in tickers[:112]],
                'sources': [{'id': 'fixture-source', 'url': 'https://example.invalid/test-fixture'}],
                'stock_universe_preparation': {
                    'stocks': stocks, 'as_of': dates[-1], 'prepared_at': 'test-preparation',
                    'inventory_sha256': 'a' * 64, 'evaluated_stocks': 468, 'ready_stocks': 112,
                    'pending_stocks': 356, 'added_stocks': tickers[:112],
                    'removed_stocks': [], 'revalidated_stocks': []}}
    write_json(snapshot / 'assets.json', metadata)
    write_json(snapshot / 'collection-report.json', {'status': 'partial'})
    write_json(tmp_path / 'current.json', {'schema_version': 1, 'status': 'partial',
                                         'snapshot': 'snapshots/test-scope'})
    source = tmp_path / 'user-snapshot.tsv'
    source.write_text('Papel\tDiv.Yield\tOptional\n' + ''.join(
        f'{ticker}\t{("0,01%" if ticker == tickers[0] else "1,00%") if ticker in selected else "0,00%"}\tfixture\n'
        for ticker in tickers + UNMATCHED), encoding='utf-8')
    return SimpleNamespace(root=tmp_path, snapshot=snapshot, source=source, tickers=tickers,
                           selected=selected, metadata=metadata, activate=lambda: import_scope(tmp_path, source))


@pytest.mark.parametrize('direct_snapshot', [False, True])
def test_prunes_only_unselected_stocks_columns_and_training_without_changing_files(market, direct_snapshot):
    original = real_dataset(market.snapshot)
    market.activate()
    before = {path: path.read_bytes() for path in market.root.rglob('*') if path.is_file()}
    data = real_dataset(market.snapshot if direct_snapshot else market.root)
    expected = [i for i, asset in enumerate(original.assets)
                if asset.asset_class != 'acoes' or asset.ticker in market.selected]
    assert [asset.ticker for asset in data.assets] == [original.assets[i].ticker for i in expected]
    assert sum(asset.asset_class == 'acoes' for asset in data.assets) == 100
    assert sum(asset.asset_class != 'acoes' for asset in data.assets) == 37
    np.testing.assert_array_equal(data.returns, original.returns[:, expected])
    np.testing.assert_array_equal(data.cash_returns, original.cash_returns[:, expected])
    np.testing.assert_array_equal(data.inflation, original.inflation)
    assert data.dates == original.dates and len(data.dates) == 60
    assert [asset for asset in data.assets if asset.asset_class != 'acoes'] == original.assets[112:]
    assert {row['ticker'] for row in data.observations} == set(market.tickers[:100]) | {'TD-00'}
    assert len(data.observations) == 101
    assert len(data.metadata['assets']) == 137
    assert data.metadata['collector_stocks'] == market.tickers[:100]
    assert [row['ticker'] for row in data.metadata['expanded_stocks']] == market.tickers[:100]
    assert {path: path.read_bytes() for path in before} == before


def test_keeps_468_catalogue_with_scope_separate_from_profile_and_missing_history(market):
    from .dividend_scope import OUTSIDE_REASON, scope_metadata

    market.activate()
    data = real_dataset(market.root)
    investor = Investor(risk_level=0, withdrawal_years=6, high_liquidity=False, knowledge='Intermedi\u00e1rio')
    screened = screen_universe(data, investor, [])
    counts = screened['counts']
    assert counts['stocks'] == 468 and counts['readyStocks'] == 100
    assert counts['selectedScopeStocks'] == 258 and counts['excludedBySelectionStocks'] == 210
    assert counts['pendingDataStocks'] == 158 and counts['excludedByProfileStocks'] == 258
    selection = screened['dividendYieldSelection']
    assert selection == {**scope_metadata(market.root), 'matchedCatalogTickersCount': 258,
                         'unmatchedTickers': sorted(UNMATCHED), 'outsideScopeStocks': 210,
                         'selectedReadyStocks': 100, 'selectedPendingStocks': 158}
    assert selection['selectedTickersCount'] == 262 and selection['sourceDate'] is None
    assert selection['dividendYieldIsPaymentHistory'] is selection['selectionIsPointInTimeBacktest'] is False
    outside = [row for row in screened['stocks'] if not row['selectionCompatible']]
    assert len(outside) == 210
    assert all(row['dataReasonCodes'] == [OUTSIDE_REASON] and row['selectionReasons'] for row in outside)
    assert all(row['profileReasons'] == [] and row['profileCompatible'] is None for row in outside)
    assert all(not row['dataReady'] and not row['eligible'] for row in outside)
    assert all(row['predictedCashPerShare'] is None for row in outside)
    selected_pending = [row for row in screened['stocks'] if row['selectionCompatible'] and not row['dataReady']]
    assert len(selected_pending) == 158
    assert all(row['dataReasonCodes'] == ['missing_cached_source'] and row['selectionReasons'] == []
               for row in selected_pending)
    assert not set(UNMATCHED) & {row['ticker'] for row in screened['stocks']}
    prep = data.metadata['stock_universe_preparation']
    assert (prep['evaluated_stocks'], prep['ready_stocks'], prep['pending_stocks']) == (468, 100, 158)
    summary = preparation_summary(data)
    assert (summary['selectedScopeStocks'], summary['excludedBySelectionStocks'], summary['pendingDataStocks']) == (258, 210, 158)
    assert summary['dividendYieldSelection'] == selection


def test_no_scope_retains_legacy_assets_columns_training_and_revision(market):
    data = real_dataset(market.root)
    assert len(data.assets) == 149 and data.returns.shape == data.cash_returns.shape == (60, 149)
    assert len(data.observations) == 115
    assert 'stock_selection' not in data.metadata
    assert dataset_revision(data) == current_data_revision(data, market.root) == 'test-preparation'


def test_selected_pending_stock_explains_native_eps_conflict_without_repair(market):
    from .universe import DATA_REASONS

    market.activate()
    code = 'native_financial_eps_source_conflict'
    market.metadata['stock_universe_preparation']['stocks'][112]['reason_codes'] = [code]
    write_json(market.snapshot / 'assets.json', market.metadata)
    data = real_dataset(market.root)
    investor = Investor(risk_level=2, withdrawal_years=6, high_liquidity=False, knowledge='Intermedi\u00e1rio')
    result = screen_universe(data, investor, [])
    row = next(row for row in result['stocks'] if row['ticker'] == market.tickers[112])
    assert row['selectionCompatible'] is True and row['selectionReasons'] == []
    assert row['dataReady'] is False and row['predictedCashPerShare'] is None
    assert row['dataReasonCodes'] == [code] and row['dataReasons'] == [DATA_REASONS[code]]


@pytest.mark.parametrize('treasury_sources', [False, True])
def test_manifest_id_changes_revision_even_with_same_selection_and_no_treasury(market, treasury_sources):
    from .dividend_scope import import_scope

    market.activate()
    if treasury_sources:
        for name in ('raw/current/official-investir.json', 'raw/primary-research/macro/current.json'):
            path = market.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic revision source')
    first = real_dataset(market.root)
    first_revision = current_data_revision(first, market.root)
    new_source = market.root / 'second-user-snapshot.tsv'
    new_source.write_text(market.source.read_text(encoding='utf-8').replace('0,01%', '0,02%'), encoding='utf-8')
    import_scope(market.root, new_source)
    second = real_dataset(market.root)
    assert first.metadata['stock_selection']['selectedTickers'] == second.metadata['stock_selection']['selectedTickers']
    assert first.metadata['stock_selection']['id'] != second.metadata['stock_selection']['id']
    assert dataset_revision(first) != dataset_revision(second)
    assert first_revision != current_data_revision(second, market.root)
    assert first_revision == current_data_revision(first, market.root)
    for data in (first, second):
        without_preparation = SimpleNamespace(metadata={'stock_selection': data.metadata['stock_selection']})
        assert dataset_revision(without_preparation) == current_data_revision(without_preparation, market.root)
        assert dataset_revision(without_preparation) is not None


@pytest.mark.parametrize('damage', ['pointer', 'manifest', 'missing_source'])
def test_invalid_configured_scope_fails_closed(market, damage):
    scope = market.activate()
    base = market.root / 'stock-selection'
    if damage == 'pointer':
        write_json(base / 'current.json', {'schema_version': 1, 'sha256': 'invalid'})
    elif damage == 'manifest':
        with (base / 'manifests' / (scope['id'] + '.json')).open('ab') as stream:
            stream.write(b' ')
    else:
        (base / scope['source_file']).unlink()
    with pytest.raises(ValueError):
        real_dataset(market.root)


def test_scope_prunes_excluded_numeric_cells_before_loading_but_keeps_cash_integrity(market):
    market.activate()
    rows = read_csv(market.snapshot / 'returns.csv', ['date'])
    rows[0][market.tickers[100]] = 'not-a-return'
    write_csv(market.snapshot / 'returns.csv', list(rows[0]), rows)
    observations = read_csv(market.snapshot / 'observations.csv', ['ticker'])
    observations[100]['target'] = 'not-a-label'
    write_csv(market.snapshot / 'observations.csv', list(observations[0]), observations)
    assert real_dataset(market.root).returns.shape == (60, 137)
    with (market.snapshot / 'cashflows.csv').open('ab') as stream:
        stream.write(b'\n')
    with pytest.raises(ValueError, match='Integridade'):
        real_dataset(market.root)


def test_positive_snapshot_yield_never_creates_cash_payments(market):
    market.activate()
    data = real_dataset(market.root)
    np.testing.assert_array_equal(data.cash_returns[:, 0], 0)
    (market.snapshot / 'cashflows.csv').unlink()
    assert real_dataset(market.root).cash_returns is None


def test_selected_training_still_rejects_temporal_leakage(market):
    market.activate()
    rows = read_csv(market.snapshot / 'observations.csv', ['ticker'])
    rows[0]['published_at'] = rows[0]['label_end']
    write_csv(market.snapshot / 'observations.csv', list(rows[0]), rows)
    with pytest.raises(ValueError, match='vazamento temporal'):
        real_dataset(market.root)


def test_engine_market_metadata_exposes_selection_source(market, monkeypatch):
    from . import engine

    market.activate()
    data = real_dataset(market.root)
    monkeypatch.setenv('RF_DATA_DIR', str(market.root))
    monkeypatch.delenv('RF_CURRENT_CONTRACT_PORTFOLIOS', raising=False)
    monkeypatch.setattr(engine, 'get_dataset', lambda _: data)
    monkeypatch.setattr(engine, 'coverage_summary', lambda _: {})
    request = RecommendationRequest(data_mode='real',
        investor={'risk_level': 0, 'knowledge': 'Intermedi\u00e1rio', 'withdrawal_years': 6, 'high_liquidity': False},
        plan={'age': 30, 'retirement_age': 31, 'end_age': 32, 'initial_capital': 1200,
              'monthly_contribution': 100, 'target_income': 100, 'annual_cost_percent': 0,
              'simulations': 500, 'seed': 42})
    result = engine.compute(request, generations=1, population=4, search_count=4)
    selection, = [source for source in result['marketData']['sources'] if source.get('kind') == 'stock_selection']
    assert selection['id'] == data.metadata['stock_selection']['id']
    assert selection['policy'] == data.metadata['stock_selection']['rule']
    assert selection['sourceDate'] is None and selection['sourceSHA256']
    assert result['dataRevision'] == dataset_revision(data)
    assert result['universeScreening']['counts']['excludedBySelectionStocks'] == 210


def test_scope_loading_does_not_reenter_preparer_or_cause_import_cycle(market):
    market.activate()
    result = subprocess.run([sys.executable, '-c',
        'from pathlib import Path; import sys; from recommender.prepare_inventory import prepare; '
        'from recommender.datasets import real_dataset; '
        'assert len(real_dataset(Path(sys.argv[1])).assets) == 137', str(market.snapshot)],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
