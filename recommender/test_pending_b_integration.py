"""Real production dispatch and candidate gates against installed originals.

Only network entry points are patched to fail closed. Providers, parsers,
identity checks, reconciliation and financial functions are never replaced.
"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from . import inventory_candidates as candidates
from . import issuer_independent_evidence as dispatcher
from . import pending_b_independent as provider
from .broad_stocks import split_price_history
from .collect_market_data import financial_features, month_end, shift_month
from .inventory_financials import prepare_financials
from .market_inventory import load_inventory
from .share_basis import reconcile_prices


ROOT = (Path(__file__).parent / 'data/market').resolve()
CUTOFF = date(2026, 9, 30)
B_TICKERS = ('MTRE3', 'ARML3', 'RDOR3')
ITSA_TICKERS = ('ITSA3', 'ITSA4')


@pytest.fixture(scope='module', autouse=True)
def offline_read_only():
    protected = [ROOT / name for name in
                 ('current.json', 'inventory/current.json', 'stock-selection/current.json')]
    before = {path: path.read_bytes() for path in protected}
    functions = (dispatcher.load_evidence, dispatcher.validate_primary_actions,
                 candidates._one, candidates.reconcile_prices, candidates.reconcile_payments)

    def forbidden(*args, **kwargs):
        pytest.fail('Real dispatcher integration must not use the network')

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', forbidden)
        patch.setattr(socket.socket, 'connect_ex', forbidden)
        patch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
        yield
    assert functions == (dispatcher.load_evidence, dispatcher.validate_primary_actions,
                         candidates._one, candidates.reconcile_prices, candidates.reconcile_payments)
    assert all(path.read_bytes() == before[path] for path in protected)


@pytest.fixture(scope='module')
def inventory(offline_read_only):
    return load_inventory(ROOT)


@pytest.fixture(scope='module')
def stocks(inventory):
    return {ticker: next(row for row in inventory['stocks'] if row['ticker'] == ticker)
            for ticker in (*B_TICKERS, *ITSA_TICKERS)}


@pytest.fixture(scope='module')
def dispatched(stocks):
    return {ticker: dispatcher.load_evidence(ROOT, stocks[ticker], CUTOFF)
            for ticker in ('MTRE3', 'RDOR3', *ITSA_TICKERS)}


@pytest.fixture(scope='module')
def actual_one(inventory, stocks):
    outcomes = {}
    for ticker in (*B_TICKERS, *ITSA_TICKERS):
        cache, checks = candidates._Cache(ROOT, inventory), {}
        try:
            candidate = candidates._one(stocks[ticker], cache, CUTOFF, checks)
            outcomes[ticker] = {'candidate': candidate, 'error': None, 'checks': checks, 'cache': cache}
        except ValueError as error:
            outcomes[ticker] = {'candidate': None, 'error': str(error), 'checks': checks, 'cache': cache}
    return outcomes


def source_key(source):
    return source['file'], source['sha256'], source.get('url'), source.get('retrieved_at')


def assert_original(source):
    path = ROOT / source['file']
    assert path.resolve().is_relative_to(ROOT) and not path.is_symlink()
    body = path.read_bytes()
    assert hashlib.sha256(body).hexdigest() == source['sha256']
    if 'bytes' in source:
        assert len(body) == source['bytes']
    return body


@pytest.mark.parametrize('ticker', ['MTRE3', 'RDOR3'])
def test_real_dispatcher_selects_b_exact_roster_and_original_receipts(dispatched, stocks, ticker):
    assert dispatcher.load_evidence.__module__ == 'recommender.issuer_independent_evidence'
    data = dispatched[ticker]
    assert data['provider'] == provider.PROVIDER
    assert data['blockers'] == []
    assert data['checks']['exact_ISIN_binding'] == 'pinned_existing_exact_code_exchange_cache_not_suffix_inference'
    assert len(data['sources']) == 11
    assert all('b3-cash' not in source['file'] for source in data['sources'])
    by_url = {source.get('url'): source for source in data['sources']}
    spec = provider.TARGETS[ticker]
    for url, pins in ((spec['calendar_url'], spec['calendar']),
                      (spec['configuration_url'], spec['configuration']),
                      (provider.SCRIPT_URL, provider.SCRIPT)):
        source = by_url[url]
        assert source['sha256'] == pins[0]
        assert source['published_at'] is None
        assert datetime.fromisoformat(source['retrieved_at']).utcoffset() is not None
    for source in data['sources']:
        assert_original(source)
    history = data['checks']['issuer_CVM_CNPJ_and_ON_class_history']
    assert [row['year'] for row in history] == list(range(2021, 2027))
    assert all(row['native_row']['Codigo_Negociacao'] == ticker for row in history)
    assert all(provider.research.normalized(row['native_row']['Valor_Mobiliario']) == 'acoes ordinarias'
               for row in history)
    assert all(tuple(event[key] for key in ('ticker', 'cvm', 'share_class', 'isin')) ==
               tuple(stocks[ticker][key] for key in ('ticker', 'cvm', 'share_class', 'isin'))
               for event in data['calendar']['events'])


@pytest.mark.parametrize('ticker', ['MTRE3', 'RDOR3'])
def test_dispatched_quotes_are_every_native_month_end_not_an_agreeing_subset(dispatched, stocks, ticker):
    data = dispatched[ticker]
    sources = [source for source in data['sources']
               if source.get('url') and urlsplit(source['url']).path.endswith('/history')]
    source, = sources
    spec = provider.TARGETS[ticker]
    assert urlsplit(source['url']).netloc == 'api.mziq.com'
    assert urlsplit(source['url']).path == '/mzstockinfo/' + spec['stockinfo_id'] + '/history'
    query = parse_qs(urlsplit(source['url']).query)
    assert query['tickers'] == [ticker] and query['adjusted'] == ['false']
    assert query['from'] == ['2021-09-01'] and query['to'] == ['2026-09-30']
    payload = json.loads(assert_original(source))
    assert payload['success'] is True and payload['data']['source'] == 'Enfoque'
    assert payload['data']['tickers'][0]['ticker'] == ticker
    native_monthly = {}
    for row in sorted(payload['data']['tickers'][0]['history'], key=lambda row: row['date']):
        day = date.fromisoformat(row['date'])
        native_monthly[month_end(day.year, day.month)] = (day, row['closed'])
    required = {shift_month(CUTOFF, -offset) for offset in range(61)}
    assert set(native_monthly) == required
    assert data['raw_quotes'] == {day: value for day, value in native_monthly.values()}
    assert len(data['raw_quotes']) == 61
    content, _ = provider._price_source(ROOT, ticker)
    yahoo_monthly, actions = split_price_history(content, ticker, CUTOFF, allow_recent_actions=True)
    assert {yahoo_monthly[month][0] for month in required} == set(data['raw_quotes'])
    prices, checked, _ = reconcile_prices(content, ticker, actions, data['raw_quotes'], CUTOFF,
                                         evidence_provider=data['provider'])
    assert checked == sorted(day.isoformat() for day in data['raw_quotes']) and len(checked) == 61
    assert {prices[month][0] for month in required} == set(data['raw_quotes'])


@pytest.mark.parametrize('ticker,days', [
    ('MTRE3', ['2022-09-26']),
    ('ARML3', ['2021-10-20', '2023-12-01']),
    ('RDOR3', ['2022-09-26', '2023-12-01']),
])
def test_real_action_dispatch_keeps_intramonth_conflicts_and_all_monthly_checks(stocks, ticker, days):
    content, _ = provider._price_source(ROOT, ticker)
    _, actions = split_price_history(content, ticker, CUTOFF, allow_recent_actions=True)
    proof = dispatcher.validate_primary_actions(ROOT, stocks[ticker], CUTOFF, actions)
    assert actions == proof['observed_action_events'] == proof['actions'] == []
    assert proof['model_monthly_inputs_agree61'] and proof['model_monthly_input_count'] == 61
    assert len(proof['model_monthly_inputs']) == 61
    assert all(row['date_matches'] and row['price_matches'] for row in proof['model_monthly_inputs'])
    assert proof['model_monthly_mismatches'] == []
    assert proof['core_tolerances_unchanged'] == {'relative': .006, 'absolute_BRL': .015}
    assert [row['date'] for row in proof['other_daily_disagreements_retained_not_used']] == days
    assert [row['date'] for row in proof['daily_disagreements']] == days
    assert proof['FCA_exact_security_class_years_verified'] == list(range(2021, 2027))
    assert not proof['raw_full_window_agrees'] and not proof['blocked']
    assert not proof['empty_actions_inferred_from_price_adjustment']


def test_mtre_one_runs_real_dispatcher_and_every_normal_gate(actual_one, dispatched, stocks):
    result = actual_one['MTRE3']
    assert result['error'] is None
    row, data = result['candidate'], dispatched['MTRE3']
    assert candidates._one.__module__ == 'recommender.inventory_candidates'
    assert row['ticker'] == 'MTRE3'
    assert (row['cvm'], row['share_class'], row['isin']) == (24902, 'ON', 'BRMTREACNOR6')
    assert row['statement_spec'] == (24902, 'ON', stocks['MTRE3']['sector_code'])
    assert row['checks']['independent_issuer_evidence']['provider'] == provider.PROVIDER
    assert row['checks']['cash_pagination'] == 'full_native_issuer_table'
    assert row['checks']['independent_price_matches'] == len(row['independent_price_matches']) == 61
    assert row['independent_price_matches'] == sorted(day.isoformat() for day in data['raw_quotes'])
    assert row['share_actions'] == [] and len(row['ledger']) == 36
    assert len(row['independent_cash_matches']) == 36
    assert row['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert row['financially_validated'] is False
    assert not row['checks']['point_in_time_archive_verified']
    assert all(payment['reported_installment_sum'] > 0 and payment['normalization_multiplier'] == 1
               for payment in row['share_normalization']['payments'])
    assert all(payment['gross_per_unit'] > 0 and payment['share_normalization_factor'] == 1
               for payment in row['ledger'])
    assert all(payment['published_at'] is None for payment in row['ledger'])
    assert all(payment['basis'] == 'reported_payment_date_independent_amount_and_share_factor_reconciled'
               for payment in row['ledger'])


def test_mtre_parent_receipts_preserve_every_primary_and_action_source(actual_one, dispatched):
    result = actual_one['MTRE3']
    expected = [*dispatched['MTRE3']['sources'], *dispatched['MTRE3']['primary_actions']['sources']]
    receipts = result['cache'].all_receipts()
    actual = {source_key(source): source for source in receipts}
    assert {source_key(source) for source in expected} <= set(actual)
    for original in expected:
        retained = actual[source_key(original)]
        assert_original(retained)
        assert retained.get('published_at') == original.get('published_at')
        assert retained['retrieved_at'] == original.get('retrieved_at')
        assert retained['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
        assert retained['observed_at'] is not None
    assert all('b3-cash' not in source['file'] for source in receipts)


def test_mtre_current_financial_gate_uses_latest_annual_filing_without_fallback(actual_one, stocks):
    row = actual_one['MTRE3']['candidate']
    assert row is not None
    statements, audit, sources = prepare_financials(ROOT, [stocks['MTRE3']], CUTOFF)
    current = financial_features(statements, row['payments'], CUTOFF, row['statement_spec'])
    assert current is not None, audit
    features, filing = current
    eligible = [statement for statement in statements if statement['cvm'] == row['cvm']
                and statement['share_class'] == row['share_class'] and statement['published_at'] <= CUTOFF.isoformat()]
    latest = max(eligible, key=lambda statement: (statement['fiscal_end'], int(statement['version'])))
    assert filing == latest
    assert filing['fiscal_end'] == '2025-12-31' and filing['version'] == '1'
    assert filing['filing_id'] == '155123' and filing['published_at'] == '2026-03-05'
    assert filing['profit_per_share'] == .50815
    assert filing['fiscal_end'] >= row['financial_basis_start']
    assert len(features) == 7 and all(value is not None for value in features)
    assert sources


def test_armac_real_dispatch_and_one_keep_three_cash_blockers(actual_one, stocks):
    with pytest.raises(ValueError, match='^pending_b_cash_does_not_cover_window_start$'):
        dispatcher.load_evidence(ROOT, stocks['ARML3'], CUTOFF)
    result = actual_one['ARML3']
    assert result['candidate'] is None and result['error'] == 'pending_b_cash_does_not_cover_window_start'
    data = provider.inspect_evidence(ROOT, stocks['ARML3'], CUTOFF)
    assert data['blockers'] == ['pending_b_cash_does_not_cover_window_start',
                                'pending_b_payment_date_unresolved',
                                'pending_b_cash_gross_tax_basis_unresolved']
    assert min(data['expected_cash']) > shift_month(CUTOFF, -60)
    assert all(value > 0 for value in data['expected_cash'].values())
    assert sum(row['payment_date'] is None for row in data['calendar']['events']) == 3
    assert any(row['amount_tax_basis'] == 'unresolved' for row in data['calendar']['events'])


def test_rdor_one_keeps_rounded_native_cash_and_next_real_block(actual_one, dispatched):
    result = actual_one['RDOR3']
    assert result['candidate'] is None
    assert result['error'] == 'cash_amount_or_share_scale_disagrees_with_independent_source'
    checks = result['checks']['independent_issuer_evidence']
    assert checks['primary_actions']['model_monthly_inputs_agree61']
    data = dispatched['RDOR3']
    events = [row for row in data['calendar']['events'] if row['record_date'] == '2021-12-17']
    event, = events
    assert event['gross_per_share_decimal'] == '0.09' and Decimal(event['gross_per_share_decimal']) == Decimal('.09')
    assert data['expected_cash'][date(2021, 12, 17)] == .09
    assert event['payment_date'] == '2021-12-29' and event['label'] == 'JCP'
    assert event['amount_tax_basis'] == 'explicit_gross'
    paid, _ = candidates._payments('RDOR3', result['cache'], CUTOFF)
    comparison = [row for row in paid.installments if row['record_date'] == '2021-12-17']
    assert sum(row['gross_per_unit'] for row in comparison) == .094389
    assert sum(row['gross_per_unit'] for row in comparison) != data['expected_cash'][date(2021, 12, 17)]
    assert all(not row['gross_reconstructed_from_net'] for row in data['calendar']['events'])


@pytest.mark.parametrize('ticker', B_TICKERS)
@pytest.mark.parametrize('field,value', [('cvm', 1), ('share_class', 'PN'), ('isin', 'WRONG_EXACT_ISIN')])
def test_dispatch_rejects_wrong_exact_b_identity(stocks, ticker, field, value):
    identity = {**stocks[ticker], field: value}
    with pytest.raises(ValueError, match='^pending_b_exact_CVM_class_ISIN_mismatch$'):
        dispatcher.load_evidence(ROOT, identity, CUTOFF)


@pytest.mark.parametrize('ticker', ['MTRE4', 'MTRE3F', 'MTRE3.SA', 'ARML4', 'RDOR11'])
def test_dispatch_does_not_expand_roster_by_ticker_prefix(stocks, ticker):
    assert dispatcher.load_evidence(ROOT, {**stocks['MTRE3'], 'ticker': ticker}, CUTOFF) is None


@pytest.mark.parametrize('ticker', (*B_TICKERS, *ITSA_TICKERS))
def test_dispatch_does_not_reuse_evidence_for_another_cutoff(stocks, ticker):
    assert dispatcher.load_evidence(ROOT, stocks[ticker], date(2026, 8, 31)) is None


@pytest.mark.parametrize('ticker', ITSA_TICKERS)
def test_itsa_dispatch_retains_existing_provider_cash_prices_and_hashes(dispatched, actual_one, ticker):
    data = dispatched[ticker]
    assert data['provider'] == 'issuer_calendar_and_Enfoque' and data['provider'] != provider.PROVIDER
    assert data['checks']['cash_rows'] == 105
    assert data['checks']['window_cash_rows'] == 49
    assert data['checks']['pre_window_cash_rows'] == 54
    assert data['checks']['future_cash_rows'] == 2
    assert len(data['raw_quotes']) == 1268
    assert len({month_end(day.year, day.month) for day in data['raw_quotes']}) == 61
    assert len(data['sources']) == 4
    assert all(provider.CACHE not in source['file'] for source in data['sources'])
    for source in data['sources']:
        assert_original(source)
    assert min(data['expected_cash']) < shift_month(CUTOFF, -60)
    assert not data['checks']['synthetic_exchange_rows_created']
    assert not data['checks']['net_to_gross_inference']
    assert not data['checks']['point_in_time_archive_verified']
    result = actual_one[ticker]
    assert result['candidate'] is None
    assert result['error'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert result['checks']['independent_issuer_evidence']['provider'] == data['provider']


@pytest.mark.parametrize('ticker', ITSA_TICKERS)
def test_itsa_real_action_dispatch_does_not_reuse_b_empty_actions_or_weaken_primary_gate(stocks, ticker):
    from .itausa_primary_actions import primary_actions
    identity = stocks[ticker]
    before = deepcopy(identity)
    proof = primary_actions(ROOT, identity, CUTOFF)
    assert identity == before
    assert proof['blocked'] and len(proof['actions']) == 5
    assert [row['ratio_decimal'] for row in proof['actions']] == ['1.05', '1.1', '1.05', '1.05', '1.02']
    assert all(row['date'] is None and row['blocked'] for row in proof['actions'])
    with pytest.raises(ValueError, match='^issuer_primary_share_action_document_missing$'):
        dispatcher.validate_primary_actions(ROOT, identity, CUTOFF, [])


def test_real_build_candidates_partitions_all_five_without_provider_monkeypatch(inventory, stocks):
    selection = {**inventory, 'stocks': [stocks[ticker] for ticker in (*B_TICKERS, *ITSA_TICKERS)]}
    accepted, pending, receipts = candidates.build_candidates(ROOT, selection, CUTOFF)
    assert [row['ticker'] for row in accepted] == ['MTRE3']
    assert {row['ticker']: row['reason'] for row in pending} == {
        'ARML3': 'pending_b_cash_does_not_cover_window_start',
        'RDOR3': 'cash_amount_or_share_scale_disagrees_with_independent_source',
        'ITSA3': 'historical_quote_or_share_scale_disagrees_with_independent_source',
        'ITSA4': 'historical_quote_or_share_scale_disagrees_with_independent_source',
    }
    assert {row['inventory_index'] for row in accepted + pending} == set(range(5))
    assert all('b3-cash' not in source['file'] for source in receipts)
    assert any(source.get('url', '').startswith('https://api.mziq.com/mzstockinfo/')
               for source in receipts if source.get('url'))
    for source in receipts:
        assert_original(source)
