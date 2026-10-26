"""PNVL production-hook regression; no live preparation, publication or network."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.request

import numpy as np
import pytest

from . import cash_calendar_research as research
from . import inventory_candidates as candidates
from . import pnvl_native_calendar as native
from .market_inventory import load_inventory


FOLDER = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def real_parent():
    inventory = load_inventory(FOLDER)
    stock = next(row for row in inventory['stocks'] if row['ticker'] == 'PNVL3')
    cache = candidates._Cache(FOLDER, inventory)
    checks = {}
    row = candidates._one(stock, cache, native.CUTOFF, checks)
    return {'inventory': inventory, 'stock': stock, 'cache': cache, 'candidate': row, 'checks': checks,
            'calendar': cache.pnvl_native_calendars['PNVL3'], 'receipts': cache.all_receipts()}


@pytest.fixture(scope='module')
def flat(real_parent):
    metadata = research.native_calendar_metadata([real_parent['candidate']], {'PNVL3'}, real_parent['receipts'])
    return {**metadata, 'sources': real_parent['receipts']}


def test_real_unmodified_one_and_build_candidates_keep_every_normal_gate(real_parent):
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials
    row = real_parent['candidate']
    assert candidates._payments.__module__ == 'recommender.inventory_candidates'
    assert research.apply_research_calendar.__module__ == 'recommender.cash_calendar_research'
    assert len(row['independent_cash_matches']) == 17
    assert len(row['independent_price_matches']) == 17
    target = [r for r in row['ledger'] if r['record_date'] == '2024-04-05']
    assert [(r['payment_date'], r['gross_per_unit'], r['label']) for r in target] == [
        ('2024-08-30', .05911502, 'JCP'), ('2025-03-31', .01880932, 'JCP')]
    assert all(r['payment_date_basis'] == 'explicit_calendar_date' for r in target)
    assert all(r['full_settlement_on_date_verified'] is False and not r['point_in_time_archive_verified'] for r in target)
    assert row['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert not row['financially_validated']
    accepted, pending, receipts = candidates.build_candidates(FOLDER,
        {**real_parent['inventory'], 'stocks': [real_parent['stock']]}, native.CUTOFF)
    assert not pending, pending
    assert len(accepted) == 1 and accepted[0]['ticker'] == 'PNVL3'
    assert len(accepted[0]['independent_cash_matches']) == 17
    assert len(accepted[0]['independent_price_matches']) == 17
    expected = {(s['file'], s['sha256'], s.get('url')) for s in real_parent['calendar']['evidence_sources']}
    assert len(expected) == 115
    assert expected <= {(s['file'], s['sha256'], s.get('url')) for s in receipts}
    statements, audit, _ = prepare_financials(FOLDER, [real_parent['stock']], native.CUTOFF)
    current = financial_features(statements, row['payments'], native.CUTOFF, row['statement_spec'])
    assert current is not None, audit
    features, filing = current
    assert date.fromisoformat(filing['fiscal_end']) >= date.fromisoformat(row['financial_basis_start'])
    training, _ = stock_observations('PNVL3', row['prices'], row['payments'], statements,
                                     native.CUTOFF, spec=row['statement_spec'])
    eligible = [r for r in training if date.fromisoformat(r['as_of']) >= row['training_since']
                and date.fromisoformat(r['fiscal_end']) >= date.fromisoformat(row['financial_basis_start'])]
    assert eligible
    print(json.dumps({'entry_point': '_one_and_build_candidates_no_monkeypatches',
        'outcome': 'candidate_normal_financial_features_and_training_available', 'cash_matches': 17,
        'price_matches': 17, 'filing_id': filing['filing_id'], 'fiscal_end': filing['fiscal_end'],
        'eligible_training_rows': len(eligible), 'features': features,
        'native_group_installments': len(target), 'all_115_receipts_registered': True,
        'live_dataset_or_pointer_written': False}, sort_keys=True))


def test_all_115_receipt_URL_SHA_and_nullable_retrieval_observation_are_preserved(real_parent):
    actual = {(s['file'], s.get('url')): s for s in real_parent['receipts']}
    for original in real_parent['calendar']['evidence_sources']:
        receipt = actual[original['file'], original.get('url')]
        assert receipt['sha256'] == original['sha256']
        assert receipt['retrieved_at'] == original.get('retrieved_at')
        assert receipt['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
        assert receipt['observed_at'] is not None
    same_bytes = [s for s in real_parent['calendar']['evidence_sources']
                  if s['sha256'] == '82add82b6fee2a89c06caa31eb983cd4f312f11cbe0e4e1ffbcdc78604f93e21']
    assert len(same_bytes) == 2 and same_bytes[0]['url'] != same_bytes[1]['url']
    assert all((s['file'], s['url']) in actual for s in same_bytes)


def test_checks_flat_metadata_and_warning_preserve_unresolved_conflict(flat, real_parent):
    checks = real_parent['checks']['cash_calendar_research']
    assert checks['source_conflicts'] == real_parent['calendar']['source_conflicts']
    assert checks['modelling_priority_rule'] == native.modelling_priority_rule()
    native.validate_warning_propagation(real_parent['calendar'], checks['source_conflict_warnings'])
    warning, = research.calendar_conflict_engine_warnings(flat, ['PNVL3'])
    assert 'datas programadas' in warning and 'formulario nativo exato da CVM' in warning
    assert '28/03/2025' in warning and '31/03/2025' in warning and 'sem resolucao' in warning
    assert 'nao comprova liquidacao bancaria' in warning and 'point-in-time' in warning
    assert 'inicio declarado' not in warning
    assert len(flat['cash_calendar_evidence_receipts']) == 115
    assert flat['cash_calendar_source_conflicts'][0]['source_sha256'] == native.BOARD_SHA
    assert flat['cash_calendar_source_conflicts'][0]['source_version'] is None


def test_metadata_is_only_exported_for_ready_stocks(real_parent):
    metadata = research.native_calendar_metadata([real_parent['candidate']], set(), real_parent['receipts'])
    assert all(not value for value in metadata.values())
    assert research.calendar_conflict_engine_warnings(metadata, []) == []


def test_isolated_preparation_exports_complete_proof_after_real_financial_gates(real_parent, tmp_path, monkeypatch):
    from . import inventory_financials, prepare_inventory
    from .market_inventory import publish_inventory
    from .test_prepare_inventory import baseline
    financials = inventory_financials.prepare_financials(FOLDER, [real_parent['stock']], native.CUTOFF)
    baseline(tmp_path, monkeypatch)
    inventory = {**deepcopy(real_parent['inventory']), 'stocks': [deepcopy(real_parent['stock'])],
                 'counts': {'stocks': 1}}
    publish_inventory(tmp_path, inventory)
    pointer = (tmp_path / 'current.json').read_bytes()

    def reconciled(root, supplied, cutoff):
        assert root == tmp_path and supplied == inventory and cutoff == native.CUTOFF
        return [deepcopy(real_parent['candidate'])], [], deepcopy(real_parent['receipts'])

    monkeypatch.setattr(candidates, 'build_candidates', reconciled)
    monkeypatch.setattr(inventory_financials, 'prepare_financials', lambda *args: deepcopy(financials))
    snapshot, report = prepare_inventory.prepare(tmp_path)
    metadata = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    assert metadata['stock_universe_preparation']['ready_stocks'] == 1
    row, = metadata['stock_universe_preparation']['stocks']
    assert row['ticker'] == 'PNVL3' and row['model_ready']
    assert row['source_checks']['cash_calendar_research']['source_conflicts'] == real_parent['calendar']['source_conflicts']
    assert row['source_checks']['cash_calendar_research']['modelling_priority_rule'] == native.modelling_priority_rule()
    assert metadata['cash_calendar_date_qualifications'] == []
    assert len(metadata['cash_calendar_evidence_receipts']) == 115
    identities = {(s['file'], s['sha256'], s.get('url')) for s in metadata['sources']}
    assert {(s['file'], s['sha256'], s.get('url')) for s in real_parent['calendar']['evidence_sources']} <= identities
    assert research.calendar_conflict_engine_warnings(metadata, ['PNVL3'])
    assert report['expanded_stocks'][0]['training_observations'] > 0
    assert (tmp_path / 'current.json').read_bytes() == pointer


@pytest.mark.parametrize('fault', ['missing_warning', 'missing_conflict', 'missing_priority', 'missing_type',
                                 'changed_type', 'missing_index_receipt', 'duplicate_receipt'])
def test_parent_candidate_checks_cannot_drop_warning_priority_or_explicit_type(real_parent, fault):
    checks = deepcopy(real_parent['checks']['cash_calendar_research'])
    if fault == 'missing_warning': checks['source_conflict_warnings'] = []
    elif fault == 'missing_conflict': checks.pop('source_conflicts')
    elif fault == 'missing_priority': checks.pop('modelling_priority_rule')
    elif fault == 'missing_type': checks['native_calendar_contract']['complete_groups'][0].pop('cash_type_proof')
    elif fault == 'missing_index_receipt':
        checks['native_calendar_contract']['evidence_sources'].pop()
    elif fault == 'duplicate_receipt':
        sources = checks['native_calendar_contract']['evidence_sources']
        sources[-1] = deepcopy(sources[0])
    elif fault == 'changed_type':
        calendar = checks['native_calendar_contract']
        for event in calendar['events']: event['label'] = 'DIVIDENDO'
        for event in calendar['complete_groups'][0]['events']: event['label'] = 'DIVIDENDO'
        calendar['complete_groups'][0]['cash_type_proof']['label'] = 'DIVIDENDO'
    with pytest.raises(ValueError):
        research.validate_native_calendar_checks(checks)


@pytest.mark.parametrize('fault', ['contracts', 'warnings', 'conflicts', 'priority', 'receipt_missing', 'receipt_SHA',
    'source_SHA', 'retrieval_mtime_imputed', 'RI_version', 'cash_type'])
def test_flat_metadata_fail_closed_before_either_engine(flat, fault):
    metadata = deepcopy(flat)
    if fault == 'contracts': metadata.pop('cash_calendar_native_contracts')
    elif fault == 'warnings': metadata.pop('cash_calendar_source_warnings')
    elif fault == 'conflicts': metadata.pop('cash_calendar_source_conflicts')
    elif fault == 'priority': metadata.pop('cash_calendar_modelling_priority_rules')
    elif fault == 'receipt_missing': metadata['cash_calendar_evidence_receipts'].pop()
    elif fault == 'receipt_SHA': metadata['cash_calendar_evidence_receipts'][0]['sha256'] = '0' * 64
    elif fault == 'source_SHA':
        for receipt in metadata['sources']:
            if receipt['sha256'] == native.BOARD_SHA: receipt['sha256'] = '0' * 64
    elif fault == 'retrieval_mtime_imputed':
        source = next(s for s in metadata['cash_calendar_evidence_receipts'] if s['retrieved_at'] is None)
        source['retrieved_at'] = source['observed_at']
    elif fault == 'RI_version': metadata['cash_calendar_source_conflicts'][0]['source_version'] = 2
    elif fault == 'cash_type': metadata['cash_calendar_native_contracts'][0]['events'][0]['label'] = 'DIVIDENDO'
    with pytest.raises(ValueError):
        research.calendar_conflict_engine_warnings(metadata, ['PNVL3'])


def test_bad_registered_original_hash_is_never_overridden_by_eligibility(real_parent):
    source = deepcopy(real_parent['calendar']['evidence_sources'][0])
    source['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='evidence_hash_mismatch'):
        real_parent['cache'].register_research_evidence(source)


@pytest.mark.parametrize('retrieved', [None, '2026-10-20T00:00:00+00:00'])
def test_generic_cache_never_promotes_filemtime_to_network_retrieval(tmp_path, retrieved):
    path = tmp_path / 'raw/source.bin'
    path.parent.mkdir()
    path.write_bytes(b'owned cache test')
    inventory = {'stocks': [], 'sources': [{'file': 'raw/source.bin', 'retrieved_at': retrieved}]}
    cache = candidates._Cache(tmp_path, inventory)
    _, observed = cache.read('raw/source.bin', 'test')
    receipt = cache.receipts['raw/source.bin']
    assert receipt['retrieved_at'] == retrieved
    assert receipt['observed_at'] == observed.isoformat()
    assert receipt['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'


def test_calendar_parser_accepts_explicit_local_observation_without_inventing_retrieval():
    from .test_cash_calendar_research import source as calendar_source
    content = b'<html>no table</html>'
    source = calendar_source(content)
    source['observed_at'] = source['retrieved_at']
    source['observed_at_basis'] = 'local_file_mtime_not_network_retrieval'
    source['retrieved_at'] = None
    result = research.inspect_calendar(content, 'itausa', 'ITSA4', native.CUTOFF, source)
    assert result['sources'][0]['retrieved_at'] is None


def test_calendar_source_selection_handles_unknown_retrieval_without_changing_receipt(tmp_path):
    from .test_cash_calendar_research import source as calendar_source
    content = b'<html>no table</html>'
    source = calendar_source(content)
    source.update(observed_at=source['retrieved_at'], retrieved_at=None,
                  observed_at_basis='local_file_mtime_not_network_retrieval')
    root = tmp_path / research.CACHE
    root.mkdir(parents=True)
    (tmp_path / source['file']).write_bytes(content)
    (root / ('itausa-' + source['sha256'] + '-source.json')).write_text(json.dumps(source), encoding='utf-8')
    result = research.load_calendar(tmp_path, 'ITSA4', native.CUTOFF)
    assert result['sources'] == [source]


def test_second_one_reuses_dedicated_verified_calendar(real_parent, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('second_expensive_native_verification')
    monkeypatch.setattr(native, 'verified_calendar', forbidden)
    row = candidates._one(real_parent['stock'], real_parent['cache'], native.CUTOFF, {})
    assert len(row['independent_cash_matches']) == 17


def test_actual_parent_path_performs_no_network(real_parent, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network_forbidden')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    accepted, pending, _ = candidates.build_candidates(FOLDER,
        {**real_parent['inventory'], 'stocks': [real_parent['stock']]}, native.CUTOFF)
    assert len(accepted) == 1 and not pending


def synthetic_dataset(flat):
    from .contracts import Asset
    from .datasets import Dataset
    stock = Asset(ticker='PNVL3', name='Synthetic engine warning fixture', asset_class='acoes', risk_level=2,
        minimum_years=0, liquidity_days=2, price=10., as_of=date.today().isoformat(),
        source='Synthetic fixture, not investment data.', features=[.6, .5, 2., 1., .3, .4, 1.],
        current_quote={'price': 10., 'as_of': date.today().isoformat(), 'source_url': 'https://example.test/quote',
                       'purchase_available': True, 'basis': 'synthetic_test'})
    preparation = {'stocks': [{'ticker': 'PNVL3', 'name': stock.name, 'model_ready': True, 'reason_codes': []}],
        'ready_stocks': 1, 'evaluated_stocks': 1, 'pending_stocks': 0, 'as_of': '2026-09-30',
        'prepared_at': '2026-10-23T00:00:00Z', 'inventory_sha256': '0' * 64}
    return Dataset([stock], np.full((3, 1), .01), np.zeros(3),
        ['2026-07-31', '2026-08-31', '2026-09-30'], [], 'real', 'Synthetic fixture, not market data.',
        {**deepcopy(flat), 'stock_universe_preparation': preparation}, np.full((3, 1), .005))


def request():
    from .contracts import RecommendationRequest
    return RecommendationRequest(data_mode='real',
        investor={'risk_level': 2, 'knowledge': 'Avan\u00e7ado', 'withdrawal_years': 6, 'high_liquidity': False},
        plan={'income_mode': 'distributions_only', 'age': 30, 'retirement_age': 31, 'end_age': 33,
            'initial_capital': 1000, 'monthly_contribution': 50, 'target_income': 1,
            'annual_cost_percent': 0, 'simulations': 500, 'block_months': 3, 'seed': 42})


def test_legacy_engine_complete_response_has_mandatory_plain_warning(flat, monkeypatch):
    from . import engine
    from .contracts import Asset
    data = synthetic_dataset(flat)
    data.assets.append(Asset(ticker='CASH-TEST', name='Synthetic liquid anchor', asset_class='liquidez',
        risk_level=0, minimum_years=0, liquidity_days=0, price=10., annual_cash_yield=.01,
        as_of=date.today().isoformat(), source='Synthetic fixture, not investment data.'))
    data.returns = np.column_stack([data.returns, np.full(3, .005)])
    data.cash_returns = np.column_stack([data.cash_returns, np.full(3, .001)])
    monkeypatch.delenv('RF_CURRENT_CONTRACT_PORTFOLIOS', raising=False)
    monkeypatch.setattr(engine, 'get_dataset', lambda _: data)
    monkeypatch.setattr(engine, 'coverage_summary', lambda _: {'counts': {}, 'gaps': []})
    monkeypatch.setattr(engine, 'get_forecast', lambda *args, **kwargs: (None,
        {'selectedMethod': 'baseline', 'baselineMae': .1, 'xgboostMae': .2}))
    result = engine.compute(request(), generations=2, population=8, search_count=8)
    assert research.calendar_conflict_engine_warnings(flat, ['PNVL3'])[0] in result['warnings']
    assert not any('inicio declarado de pagamento' in value for value in result['warnings'])


def test_current_engine_complete_response_has_mandatory_plain_warning(flat, monkeypatch):
    from . import current_portfolio as module
    from .test_treasury_market import offer
    from .treasury_market import instrument_from_offer
    data = synthetic_dataset(flat)
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
        predict=lambda values: np.full(len(values), .6)),
        {'selectedMethod': 'baseline', 'baselineMae': .1, 'xgboostMae': .2}))
    monkeypatch.setattr(module, 'coverage_summary', lambda _: {'counts': {}, 'gaps': []})
    monkeypatch.setattr(module, 'current_data_revision', lambda _: 'synthetic-current-revision')
    result = module.compute_current(request(), data, generations=2, population=8, search_count=8)
    assert research.calendar_conflict_engine_warnings(flat, ['PNVL3'])[0] in result['warnings']
    assert not any('inicio declarado de pagamento' in value for value in result['warnings'])


@pytest.mark.parametrize('engine_name', ['legacy', 'current'])
def test_both_engines_reject_missing_warning_before_models_or_sources(flat, monkeypatch, engine_name):
    from . import engine, current_portfolio
    data = synthetic_dataset(flat)
    data.metadata.pop('cash_calendar_source_warnings')
    if engine_name == 'legacy':
        monkeypatch.setattr(engine, 'get_dataset', lambda _: data)
        call = lambda: engine.compute(request(), generations=2, population=8, search_count=8)
    else:
        call = lambda: current_portfolio.compute_current(request(), data, generations=2, population=8, search_count=8)
    with pytest.raises(ValueError, match='flat_conflict_or_priority'):
        call()
