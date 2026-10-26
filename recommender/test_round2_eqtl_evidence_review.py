"""Independent, offline review of EQTL3's complete native price adapter.

Run with pytest, or as a module with --report to emit the one review artifact.
All mutations in negative tests are in memory; market evidence stays read only.
"""

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import socket
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from . import inventory_candidates as bridge
from . import pending_all_prices as research
from . import round2_native_prices as native
from .broad_stocks import split_price_history
from .collect_market_data import month_end, normalize, shift_month
from .market_inventory import load_inventory
from .security_registration import parse_fca_registry
from .share_basis import b3_evidence, factor_after, reconcile_native_prices, reconcile_prices


REPO = Path(__file__).resolve().parents[1]
MARKET = REPO / 'recommender/data/market'
REPORT = REPO.parent / 'work/recovery_round2_20261025/eqtl-independent-review.json'
START, CUTOFF = date(2021, 9, 30), date(2026, 9, 30)
IDENTITY = {'ticker': 'EQTL3', 'cvm': 20010, 'share_class': 'ON', 'isin': 'BREQTLACNOR0'}
YAHOO = 'raw/expanded/EQTL3-prices.json'
B3 = 'raw/expanded/EQTL3-b3-cash-1.json'
B3_SHA = '423d08c2eff46f73218d375c7375d90b7c6fd46844c7958423b4ff26e81cd18b'
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
NATIVE_SHA = '2d5481fdd983505b02b716f368983af2b766619e2d2f930343f15d4117baeb2d'
OUTCOME = ('raw/primary-research/all-pending/prices/outcome-'
           'c4f2958e3b7c9663904859eae8fd3c453b93c50c2e1b280924efffe8c2a1e089.json')
APRIL = date(2024, 4, 30)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def br_decimal(value):
    return Decimal(str(value).replace('.', '').replace(',', '.'))


def literal_b3(rows):
    """Independent reading: price date, entitlement date and amount differ."""
    expected, quotes, references = defaultdict(Decimal), {}, []
    for index, row in enumerate(rows):
        if row['typeStock'].strip() != 'ON' or normalize(row['corporateAction']) not in (
                'dividendo', 'jrs cap proprio'):
            continue
        record = datetime.strptime(row['lastDatePriorEx'], '%d/%m/%Y').date()
        expected[record] += br_decimal(row['valueCash']) / br_decimal(row['quotedPerShares'] or '1')
        text = row.get('dateClosingPricePriorExDate')
        if text in (None, '', '31/12/9999'):
            continue
        day = datetime.strptime(text, '%d/%m/%Y').date()
        value = br_decimal(row['closingPricePriorExDate'])
        if value <= 0:
            continue
        if day in quotes:
            assert quotes[day] == value, 'Conflicting literal B3 quote'
        quotes[day] = value
        if START <= day <= CUTOFF:
            references.append({'row_index': index, 'quote_date': day.isoformat(),
                'record_date': record.isoformat(), 'approval_date': row['dateApproval'],
                'literal_price': row['closingPricePriorExDate'],
                'literal_cash_per_record_share': row['valueCash']})
    return dict(expected), {d: float(v) for d, v in quotes.items()}, references


def collect():
    inventory = load_inventory(MARKET)
    stock = next(r for r in inventory['stocks'] if r['ticker'] == 'EQTL3')
    outcome = json.loads((MARKET / OUTCOME).read_bytes())
    receipt = outcome['receipt']
    discovery = receipt['discovery']
    body = (MARKET / receipt['file']).read_bytes()
    payload = json.loads(body, parse_float=Decimal)
    history = payload['data']['tickers'][0]['history']
    daily = {date.fromisoformat(r['date']): float(r['closed']) for r in history}
    yahoo_body = (MARKET / YAHOO).read_bytes()
    yahoo_result = json.loads(yahoo_body)['chart']['result'][0]
    yahoo_daily = {datetime.fromtimestamp(t, timezone.utc).date(): float(v)
        for t, v in zip(yahoo_result['timestamp'], yahoo_result['indicators']['quote'][0]['close'])
        if v is not None and datetime.fromtimestamp(t, timezone.utc).date() <= CUTOFF}
    _, actions = split_price_history(yahoo_body, 'EQTL3', CUTOFF, allow_recent_actions=True)
    cash_body = (MARKET / B3).read_bytes()
    cash = json.loads(cash_body)
    expected, quotes, references = literal_b3(cash['results'])
    monthly = {}
    for day, value in sorted(daily.items()):
        monthly[month_end(day.year, day.month)] = (day, value, value)
    return dict(inventory=inventory, stock=stock, receipt=receipt, discovery=discovery,
        body=body, payload=payload, history=history, daily=daily, monthly=monthly,
        yahoo_body=yahoo_body, yahoo_daily=yahoo_daily, actions=actions, cash=cash,
        cash_body=cash_body, expected=expected, quotes=quotes, references=references)


def load(evidence, **kwargs):
    arguments = dict(folder=MARKET, identity=deepcopy(IDENTITY), cutoff=CUTOFF,
        observed_actions=deepcopy(evidence['actions']),
        independent_quotes=deepcopy(evidence['quotes']),
        comparison_content=evidence['yahoo_body'])
    arguments.update(kwargs)
    return native.load_evidence(**arguments)


def fingerprints(evidence):
    files = {YAHOO, B3, FCA, OUTCOME, 'raw/expanded/b3-detail-20010.json',
             evidence['receipt']['file'], evidence['discovery']['page']['file'],
             evidence['discovery']['bundle']['file'],
             'raw/expanded/EQTL3-payments.html'}
    files.update(p.relative_to(MARKET).as_posix() for p in MARKET.glob('**/current.json'))
    return {name: sha((MARKET / name).read_bytes()) for name in sorted(files)}


@pytest.fixture(autouse=True)
def offline():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'create_connection', side_effect=AssertionError('network_forbidden')), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network_forbidden')):
        yield


@pytest.fixture(scope='module')
def evidence():
    return collect()


def test_exact_CVM_class_ISIN_have_primary_bindings(evidence):
    assert {k: evidence['stock'][k] for k in IDENTITY} == IDENTITY
    fca = (MARKET / FCA).read_bytes()
    assert sha(fca) == FCA_SHA
    masters, securities = parse_fca_registry(fca, 2026)
    filings = {r['ID_DOC']: r for r in masters if int(r['CD_CVM']) == 20010}
    security, = [r for r in securities if r['ID_Documento'] == '157743'
                  and r['Codigo_Negociacao'] == 'EQTL3']
    assert security['CNPJ_Companhia'] == filings['157743']['CNPJ_CIA'] == '03.220.438/0001-73'
    assert normalize(security['Valor_Mobiliario']) == 'acoes ordinarias'
    assert security['Sigla_Classe_Acao_Preferencial'] == ''
    detail = json.loads((MARKET / native.DETAIL).read_bytes())
    assert int(detail['codeCVM']) == 20010
    assert detail['otherCodes'] == [{'code': 'EQTL3', 'isin': 'BREQTLACNOR0'}]
    assert detail['cnpj'] == '03220438000173'


def test_primary_feed_proof_is_request_evidence_not_server_adjustment_attestation(evidence):
    receipt, discovery = evidence['receipt'], evidence['discovery']
    assert sha(evidence['body']) == NATIVE_SHA == receipt['sha256']
    page = research.read_source(MARKET, discovery['page'])
    bundle = research.read_source(MARKET, discovery['bundle'])
    requested, proof = research.published_history_request(
        page, discovery['page'], bundle, discovery['bundle'], 'EQTL3')
    assert requested == receipt['url'] == native.HISTORY_URL
    assert parse_qs(urlsplit(requested).query) == {
        'from': ['2021-09-01'], 'to': ['2026-09-30'],
        'tickers': ['EQTL3'], 'adjusted': ['false']}
    assert proof['adjusted'] is False
    assert b"tickers: 'EQTL3'" in page and b"currencySymbol: 'R$'" in page
    assert b'MZStockInfoHistory.initialize(historyOptions' in page
    assert b't.ADJUSTED=!1' in bundle and b'"&adjusted="+t.ADJUSTED' in bundle
    assert evidence['payload']['data']['source'] == 'Enfoque'
    assert evidence['payload']['data']['link'] == 'https://www.enfoque.com.br'
    assert set(evidence['payload']) == {'success', 'data'}
    assert set(evidence['payload']['data']) == {'tickers', 'indexes', 'errors', 'source', 'link'}
    assert set(evidence['payload']['data']['tickers'][0]) == {'ticker', 'history'}
    result = load(evidence)
    quote_source = next(r for r in result['sources'] if r['sha256'] == NATIVE_SHA)
    assert quote_source['vendor_currency_field_present'] is False
    assert 'R$_and_exact_B3_ISIN' in quote_source['currency_basis']


def test_complete_native_series_and_real_month_end_observations(evidence):
    rows, inactive = research.vendor_rows(evidence['body'], 'EQTL3')
    assert len(rows) == len(evidence['history']) == len(evidence['daily']) == 1268
    assert inactive == []
    assert sum(START <= d <= CUTOFF for d in evidence['daily']) == 1248
    assert {d for d in evidence['daily'] if START <= d <= CUTOFF} == {
        d for d in evidence['yahoo_daily'] if START <= d <= CUTOFF}
    assert all(d.weekday() < 5 for d in evidence['daily'])
    result = load(evidence)
    assert result['daily'] == evidence['daily']
    assert result['prices'] == evidence['monthly']
    assert len(result['prices']) == 61
    assert max((month - values[0]).days for month, values in result['prices'].items()) == 3
    assert result['prices'][date(2021, 10, 31)][0] == date(2021, 10, 29)
    assert result['prices'][date(2024, 2, 29)][0] == date(2024, 2, 29)
    assert result['prices'][APRIL] == (APRIL, 30.59, 30.59)


def test_every_literal_B3_reference_is_used_and_old_source_fails(evidence):
    assert sha(evidence['cash_body']) == B3_SHA
    assert evidence['cash']['page']['totalPages'] == 1
    assert len(evidence['cash']['results']) == evidence['cash']['page']['totalRecords']
    _, core_quotes = b3_evidence(evidence['cash']['results'], 'ON')
    assert evidence['quotes'] == core_quotes
    required = {d: v for d, v in evidence['quotes'].items() if START <= d <= CUTOFF}
    assert len(required) == 7
    assert all(evidence['daily'][d] == value for d, value in required.items())
    assert evidence['yahoo_daily'][APRIL] == 29.516376495361328
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_B3'):
        reconcile_prices(evidence['yahoo_body'], 'EQTL3', evidence['actions'],
                         evidence['quotes'], CUTOFF)
    proof = load(evidence)['proof']
    assert proof['all_independent_price_dates'] == sorted(d.isoformat() for d in required)
    assert len(proof['exchange_comparisons']) == 7
    assert all(r['agrees'] for r in proof['exchange_comparisons'])


def test_price_dates_are_never_replaced_by_record_approval_or_placeholder(evidence):
    row = deepcopy(evidence['cash']['results'][0])
    row.update(lastDatePriorEx='01/05/2024', dateApproval='25/04/2024',
        dateClosingPricePriorExDate='30/04/2024', closingPricePriorExDate='30,59')
    amounts, quotes = b3_evidence([row], 'ON')
    assert set(amounts) == {date(2024, 5, 1)}
    assert quotes == {APRIL: 30.59}
    row['dateClosingPricePriorExDate'] = '31/12/9999'
    assert b3_evidence([row], 'ON')[1] == {}
    row['dateClosingPricePriorExDate'] = '31/04/2024'
    with pytest.raises(ValueError):
        b3_evidence([row], 'ON')


def test_conflicts_retained_and_comparison_source_cannot_drive_price_selection(evidence):
    result = load(evidence)
    proof = result['proof']
    conflicts = [d.isoformat() for d, v in sorted(evidence['daily'].items())
        if START <= d <= CUTOFF and not math.isclose(
            v, evidence['yahoo_daily'][d], rel_tol=.006, abs_tol=.015)]
    assert len(conflicts) == 11
    assert [r['date'] for r in proof['comparison_all_daily_conflicts']] == conflicts
    assert len(proof['comparison_model_monthly_conflicts']) == 1
    assert proof['comparison_model_monthly_conflicts'][0]['observed_on'] == APRIL.isoformat()
    altered = json.loads(evidence['yahoo_body'])
    closes = altered['chart']['result'][0]['indicators']['quote'][0]['close']
    closes[:] = [1. if value is not None else None for value in closes]
    changed = load(evidence, comparison_content=json.dumps(altered).encode())
    assert changed['daily'] == result['daily'] and changed['prices'] == result['prices']
    assert len(changed['proof']['comparison_all_daily_conflicts']) == 1248
    assert proof['days_or_prices_mixed_between_feeds'] is False
    assert proof['prices_fitted_or_rewritten'] is False


@pytest.mark.parametrize('key,value', [('cvm', 1), ('share_class', 'PN'),
                                     ('isin', 'BREQTLACNOR1')])
def test_wrong_security_is_rejected(evidence, key, value):
    with pytest.raises(ValueError, match='native_RI_exact_security_or_cutoff_mismatch'):
        load(evidence, identity={**IDENTITY, key: value})


def test_other_ticker_and_wrong_cutoff_or_scope(evidence):
    assert load(evidence, identity={**IDENTITY, 'ticker': 'EQTL3F'}) is None
    with pytest.raises(ValueError, match='native_RI_exact_security_or_cutoff_mismatch'):
        load(evidence, cutoff=date(2026, 8, 31))
    with patch('recommender.dividend_scope.allowed_tickers', return_value=frozenset()):
        with pytest.raises(ValueError, match='native_RI_outside_selected_scope'):
            load(evidence)


@pytest.mark.parametrize('day', ['2021-09-01', '2024-04-30', '2026-09-30', '2026-10-01'])
def test_new_action_inside_or_after_native_window_blocks(evidence, day):
    with pytest.raises(ValueError, match='native_RI_new_primary_action_review_required'):
        load(evidence, observed_actions=[{'date': day, 'ratio': 1.1}])


def test_2019_action_is_retained_but_cannot_rescale_native_window(evidence):
    assert evidence['actions'] == [{'date': '2019-11-28', 'ratio': 5.0}]
    assert all(factor_after(d, evidence['actions'], CUTOFF) == 1 for d in evidence['daily'])
    result = load(evidence)
    assert result['prices'] == load(evidence, observed_actions=[])['prices']
    assert result['proof']['observed_provider_actions_before_native_window'] == evidence['actions']
    assert result['proof']['observed_provider_actions_in_native_window'] == []


@pytest.mark.parametrize('change', ['duplicate_date', 'wrong_ticker', 'invalid_date', 'negative_price'])
def test_vendor_row_failures_are_not_coalesced_or_filled(evidence, change):
    payload = json.loads(evidence['body'])
    security = payload['data']['tickers'][0]
    if change == 'duplicate_date':
        security['history'].append(deepcopy(security['history'][0]))
    elif change == 'wrong_ticker':
        security['ticker'] = 'EQTL3F'
    elif change == 'invalid_date':
        security['history'][0]['date'] = '2026-09-31'
    else:
        security['history'][0]['closed'] = -1
    with pytest.raises(ValueError):
        research.vendor_rows(json.dumps(payload).encode(), 'EQTL3')


def test_pinned_hashes_and_request_bindings_fail_closed(evidence):
    with pytest.raises(ValueError, match='native_RI_source_integrity_failure'):
        native.read(MARKET.resolve(), evidence['receipt']['file'], '0' * 64)
    for suffix in ('&adjusted=true', '&tickers=EQTL3F'):
        receipt = {**evidence['receipt'], 'url': native.HISTORY_URL + suffix}
        with pytest.raises(ValueError, match='native_history_request_proof_mismatch'):
            research.verified_native_history(MARKET, IDENTITY, IDENTITY, receipt,
                evidence['discovery']['page'], evidence['discovery']['bundle'])


def test_an_unfavorable_seventh_anchor_is_not_dropped(evidence):
    bad = deepcopy(evidence['quotes'])
    bad[APRIL] = evidence['yahoo_daily'][APRIL]
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_B3'):
        load(evidence, independent_quotes=bad)
    short = dict(list((d, v) for d, v in bad.items() if START <= d <= CUTOFF)[:3])
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        load(evidence, independent_quotes=short)


@pytest.mark.parametrize('case,reason', [
    ('month_gap', 'less_than_61_consecutive_prices'),
    ('stale_cutoff', 'stale_month_end_price'),
    ('extreme', 'unexplained_extreme_price_change'),
    ('uncovered_regime', 'unverified_share_price_regime_requires_raw_history'),
])
def test_native_core_retains_coverage_extreme_and_regime_gates(evidence, case, reason):
    daily, actions = deepcopy(evidence['daily']), []
    if case == 'month_gap':
        daily = {d: v for d, v in daily.items() if (d.year, d.month) != (2023, 7)}
    elif case == 'stale_cutoff':
        daily = {d: v for d, v in daily.items() if d <= date(2026, 9, 22)}
    elif case == 'extreme':
        daily[date(2023, 7, 31)] = 1000.
    else:
        actions = [{'date': '2022-01-03', 'ratio': 1.1}]
    with pytest.raises(ValueError, match=reason):
        reconcile_native_prices(daily, actions, evidence['quotes'], CUTOFF)


@pytest.mark.parametrize('value,anchor,accepted', [
    (1., 1.0149, True), (1., 1.0151, False),
    (100., 100.59, True), (100., 100.62, False),
])
def test_native_core_keeps_both_absolute_and_relative_tolerances(evidence, value, anchor, accepted):
    daily = {d: value for d in evidence['daily']}
    quotes = {d: anchor for d in evidence['quotes'] if START <= d <= CUTOFF}
    if accepted:
        assert reconcile_native_prices(daily, [], quotes, CUTOFF)[2] == 'raw'
    else:
        with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_B3'):
            reconcile_native_prices(daily, [], quotes, CUTOFF)


def candidate_probe(evidence):
    before = fingerprints(evidence)
    independent_expected, independent_quotes = b3_evidence(evidence['cash']['results'], 'ON')
    calls, cash_checks = [], []
    original_reconcile, original_check = bridge.reconcile_payments, bridge.check_cash_amounts

    def paid(ticker, original, expected, actions, cutoff, **kwargs):
        assert expected == independent_expected and actions == evidence['actions']
        assert kwargs.get('evidence_provider') == 'B3'
        installments = deepcopy(original.installments)
        answer = original_reconcile(ticker, original, expected, actions, cutoff, **kwargs)
        assert original.installments == installments
        calls.append((installments, deepcopy(answer[1]), deepcopy(answer[2])))
        return answer

    def checked(rows, comparable, cutoff, share):
        assert rows == evidence['cash']['results']
        cash_checks.append(deepcopy(comparable))
        return original_check(rows, comparable, cutoff, share)

    checks, cache = {}, bridge._Cache(MARKET, evidence['inventory'])
    with patch.object(bridge, 'reconcile_payments', paid), \
         patch.object(bridge, 'check_cash_amounts', checked):
        candidate = bridge._one(deepcopy(evidence['stock']), cache, CUTOFF, checks)
    assert fingerprints(evidence) == before
    assert len(calls) == len(cash_checks) == 1
    assert candidate['prices'] == evidence['monthly']
    assert len(candidate['prices']) == 61 and len(candidate['ledger']) == 10
    assert candidate['independent_price_matches'] == sorted(
        d.isoformat() for d in independent_quotes if START <= d <= CUTOFF)
    assert candidate['price_source_file'] == evidence['receipt']['file']
    assert candidate['current_quote_source_file'] == YAHOO
    assert candidate['price_url'] == native.HISTORY_URL
    assert candidate['financially_validated'] is False
    financial_fields = ('record_date', 'payment_date', 'gross_per_unit', 'label',
                        'original_reported_per_unit', 'share_normalization_factor', 'share_basis_date')
    assert [{k: r[k] for k in financial_fields} for r in candidate['ledger']] == [
        {k: r[k] for k in financial_fields} for r in calls[0][1]]
    assert all(r['normalization_multiplier'] == 1. for r in calls[0][2])
    original_rows = {(r['record_date'], r['payment_date'], r['gross_per_unit']) for r in calls[0][0]}
    assert all((r['record_date'], r['payment_date'], r['gross_per_unit']) in original_rows
               for r in candidate['ledger'])
    assert cache.parsed[B3] == evidence['cash']
    return candidate, calls[0], before


def test_actual_candidate_uses_native_prices_and_preserves_cash_and_current_quote(evidence):
    candidate_probe(evidence)


def test_proof_retains_scientific_limits(evidence):
    proof = load(evidence)['proof']
    assert 'comparison_conflicts_resolved_by' not in proof
    assert proof['comparison_monthly_conflict_priority_basis'] == (
        'native_RI_and_exchange_agree_on_the_single_conflicting_model_month')
    assert proof['comparison_other_daily_conflicts_status'] == (
        'retained_without_independent_third_quote_resolution')
    quote_dates = {d for d in evidence['quotes'] if START <= d <= CUTOFF}
    other_conflicts = [r for r in proof['comparison_all_daily_conflicts']
                      if date.fromisoformat(r['date']) not in quote_dates]
    assert len(other_conflicts) == 10
    assert not any(r['is_required_monthly_date'] for r in other_conflicts)
    for field in ('source_action_absence_certified', 'ultimate_upstream_independence_verified',
                  'settlement_verified', 'point_in_time_archive_verified'):
        assert proof[field] is False


class Results:
    def __init__(self):
        self.rows = []
        self.exit_code = None

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or report.failed:
            self.rows.append({'test': report.nodeid, 'phase': report.when,
                'outcome': report.outcome,
                **({'failure': report.longreprtext} if report.failed else {})})


def methodological_review():
    path = REPO.parent / 'work/recovery_round2_20261025/native-price-diagnostics.json'
    diagnostic = next(r for r in json.loads(path.read_bytes())['records'] if r['ticker'] == 'LREN3')
    rows, _ = research.vendor_rows(research.read_source(MARKET, diagnostic['source']), 'LREN3')
    daily = {date.fromisoformat(r['date']): float(r['native_close_decimal']) for r in rows}
    actions = [{'date': r['date'], 'ratio': float(r['original_provider_row']['numerator']) /
        float(r['original_provider_row']['denominator'])} for r in diagnostic['provider_actions']]
    cash = json.loads((MARKET / 'raw/expanded/LREN3-b3-cash-1.json').read_bytes())
    assert cash['page']['totalPages'] == 1 and len(cash['results']) == cash['page']['totalRecords']
    _, quotes = b3_evidence(cash['results'], 'ON')
    quotes = {d: v for d, v in quotes.items() if START <= d <= CUTOFF}
    regimes = sorted({round(factor_after(d, actions, CUTOFF), 12) for d in quotes})
    try:
        reconcile_native_prices(daily, actions, quotes, CUTOFF)
        gate = 'passed'
    except ValueError as error:
        gate = str(error)
    august = date(2024, 8, 30)
    yahoo_body = (MARKET / diagnostic['source_file']).read_bytes()
    result = json.loads(yahoo_body)['chart']['result'][0]
    yahoo = {datetime.fromtimestamp(t, timezone.utc).date(): v
        for t, v in zip(result['timestamp'], result['indicators']['quote'][0]['close']) if v is not None}
    return {'scope': 'review_only_no_admission_or_gate_change',
        'conclusion': 'Independent literal raw boundary quotes covering every regime can provide equivalent basis evidence; a request adjusted=false and action notices alone cannot.',
        'necessary_distinctions': [
            'A primary action notice proves the entitlement ratio and actual ex-date, not how a quote vendor stores prior prices.',
            'A raw boundary quote must be an independently corroborated observed close for the exact ticker/class/ISIN/date/currency. A theoretical ex-price, bonus tax cost, prior-record date, or the same vendor response is not that evidence.',
            'An all-raw feed and a feed adjusted for shares but not cash may both satisfy a no-cash-provents request unless the provider contract explicitly distinguishes these cases.',
            'Independent observations on both sides of each action must cover all used cumulative share factors. The minimum count and existing tolerance, month-gap and extreme gates still apply.',
            'Small action ratios and real market moves can conceal incorrect normalization under broad extreme-return gates; price continuity alone cannot prove the stored basis.',
            'Source priority needs an explicit, reusable evidence policy chosen independently of favorable prices or model results. Full-series selection avoids day mixing but does not by itself justify that selection.',
            'A specific material source conflict remains a separate correctness question even when the action basis is established. Keep both original values, seek an independent same-session close or documented vendor revision, and retain any unresolved qualification.'
        ],
        'concrete_counterexample': {'true_pre_bonus_raw': 110, 'bonus_total_factor': 1.1,
            'true_cutoff_unit_price': 100,
            'server_mislabeled_or_already_share_adjusted_value': 100,
            'wrong_result_if_normalized_as_raw_again': 100 / 1.1,
            'why_latest_anchors_and_action_notices_do_not_catch_it':
                'Every post-bonus quote can match B3 while all earlier prices were already divided by 1.1. Four latest-regime anchors and a correct primary bonus notice leave the earlier stored basis unidentified.'},
        'LREN_cached_facts': {'native_source_file': diagnostic['source']['file'],
            'native_sha256': diagnostic['source']['sha256'],
            'B3_cash_sha256': sha((MARKET / 'raw/expanded/LREN3-b3-cash-1.json').read_bytes()),
            'cached_B3_quote_count': len(quotes), 'B3_covered_factors': regimes,
            'all_available_B3_quotes_exactly_equal_native': all(daily[d] == v for d, v in quotes.items()),
            'required_factors': [1.0, 1.1, 1.21], 'missing_factor': 1.21,
            'missing_regime': '2021-09 native window through 2021-11-04',
            'current_native_gate': gate,
            'August_conflict': {'date': august.isoformat(), 'native_raw': daily[august],
                'factor': factor_after(august, actions, CUTOFF),
                'native_cutoff_unit_close': daily[august] / factor_after(august, actions, CUTOFF),
                'Yahoo_close': yahoo[august],
                'known_bonus_chain_resolves_conflict': False},
            'needed_cached_proof': [
                'Independently literal raw close before 2021-11-05 in the earliest regime, bound to the exact security and actual session.',
                'Verified primary ratio, same-class scope and ex-date for both bonuses, with no omitted in-window actions.',
                'Independent 2024-08-30 close or documented provider correction to resolve the month-end source conflict.'
            ]}}


def build_report(results):
    evidence = collect()
    result = load(evidence)
    quote_dates = {d for d in evidence['quotes'] if START <= d <= CUTOFF}
    uncorroborated = [r for r in result['proof']['comparison_all_daily_conflicts']
                     if date.fromisoformat(r['date']) not in quote_dates]
    prepared_file = MARKET / 'snapshots/universe-20261026T003014-a0ef10b6/assets.json'
    prepared_body = prepared_file.read_bytes()
    prepared = json.loads(prepared_body)
    prepared_row, = [r for r in prepared['stock_universe_preparation']['stocks']
                     if r['ticker'] == 'EQTL3']
    prepared_proof = prepared_row['source_checks']['native_price_source']
    monthly_parity = prepared_proof['model_monthly_inputs'] == result['proof']['model_monthly_inputs']
    try:
        candidate, cash, before = candidate_probe(evidence)
        integration = {'status': 'passed_actual_one', 'monthly_prices': len(candidate['prices']),
            'payment_rows': len(candidate['ledger']), 'price_source_file': candidate['price_source_file'],
            'current_quote_source_file': candidate['current_quote_source_file'],
            'all_cash_amounts_dates_unchanged_from_payment_input': True,
            'B3_cash_expected_and_rows_unchanged': True, 'source_fingerprints_before': before,
            'source_fingerprints_after': fingerprints(evidence),
            'payment_reconciliation': cash[2], 'financial_validation_performed_by_this_review': False}
    except Exception as error:
        integration = {'status': 'blocked', 'reason': f'{type(error).__name__}: {error}'}
    failures = [r for r in results.rows if r['outcome'] == 'failed']
    return {'schema_version': 1, 'reviewed_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'independent_offline_EQTL3_whole_native_price_source_review',
        'decision': ('acceptable_faithful_price_source_adapter_with_stated_limits'
                     if results.exit_code == 0 and not failures
                     and integration['status'] == 'passed_actual_one' else 'blocked'),
        'network_used': False, 'core_edits': False, 'pointers_written_by_review': False,
        'identity': IDENTITY,
        'identity_primary_binding': {'FCA_file': FCA, 'FCA_sha256': FCA_SHA,
            'filing_id': '157743', 'CVM': 20010, 'CNPJ': '03.220.438/0001-73',
            'security_csv_record': 143, 'exchange_detail': native.DETAIL,
            'isin': 'BREQTLACNOR0', 'class': 'ON'},
        'source_basis': {'raw_history_sha256': NATIVE_SHA,
            'request_adjusted_false_verified': True, 'server_adjusted_flag_present': False,
            'vendor_currency_field_present': False,
            'currency_basis': 'issuer EQTL3 history widget currencySymbol R$, exact B3 ISIN and seven literal B3 quotes',
            'request_and_response_semantics': 'history.closed is the literal daily closing field; adjusted=false is a published request contract, not an independent server attestation',
            'price_date_role': 'B3 dateClosingPricePriorExDate; never lastDatePriorEx, dateApproval or payment date',
            'complete_native_rows': 1268, 'native_rows_in_required_window': 1248,
            'unique_currency_or_date_conflicts_in_actual_pinned_bytes': [],
            'all_native_rows_used_without_fitting': result['daily'] == evidence['daily'],
            'all_month_ends_are_last_actual_observations': result['prices'] == evidence['monthly'],
            'maximum_actual_month_end_lag_days': 3},
        'B3_literal_references': evidence['references'],
        'native_proof': result['proof'], 'integration': integration,
        'resolved_proof_wording_finding': {
            'status': 'resolved_in_current_helper_output',
            'original_severity': 'low_provenance_wording_no_changed_model_price',
            'removed_generic_field_present': 'comparison_conflicts_resolved_by' in result['proof'],
            'monthly_conflict_priority_basis': result['proof']['comparison_monthly_conflict_priority_basis'],
            'other_daily_conflicts_status': result['proof']['comparison_other_daily_conflicts_status'],
            'finding': 'The current proof explicitly limits B3 corroboration to the single conflicting model month and keeps other daily disagreements unresolved.',
            'independently_corroborated_conflict_dates': ['2024-04-30'],
            'other_conflict_dates_without_a_third_same_session_quote': [r['date'] for r in uncorroborated],
            'other_conflicts_are_monthly_model_inputs': any(r['is_required_monthly_date'] for r in uncorroborated),
            'additional_wording_correction_required': False},
        'prepared_candidate_109_legacy_proof': {
            'snapshot': 'universe-20261026T003014-a0ef10b6',
            'assets_file_sha256': sha(prepared_body),
            'retained_legacy_field_value': prepared_proof.get('comparison_conflicts_resolved_by'),
            'current_qualified_fields_present_in_immutable_snapshot': all(k in prepared_proof for k in (
                'comparison_monthly_conflict_priority_basis', 'comparison_other_daily_conflicts_status')),
            'all_61_monthly_inputs_equal_current_helper': monthly_parity,
            'immutable_assets_unchanged_by_review': sha(prepared_file.read_bytes()) == sha(prepared_body),
            'interpretation': 'Prepared before the wording fix; the retained field explicitly refers to the conflicting month. Current helper outputs use the qualified fields. No immutable snapshot was rewritten.'},
        'resolved_integration_finding': {
            'original_failure': 'native_RI_new_primary_action_review_required on the real _one path',
            'cause': 'Legacy feed contains a five-for-one action dated 2019-11-28 outside the native window.',
            'current_result': 'Pre-window action retained in proof, no rescaling of 2021-2026 prices; new in-window or later action still blocks.'},
        'reviewed_code_sha256': {name: sha((REPO / 'recommender' / name).read_bytes()) for name in (
            'round2_native_prices.py', 'share_basis.py', 'pending_all_prices.py', 'inventory_candidates.py')},
        'methodological_review': methodological_review(),
        'scientific_limits': [
            'Seven exact B3 dates corroborate one observed in-window share regime, not every daily close.',
            'No in-window action is observed in retained provider data; absence of all possible actions is not certified.',
            'The 2019 five-for-one action precedes every native quote and has factor one throughout this price window; cash still uses its original reconciliation.',
            'Issuer publication establishes the public widget and request, not exchange origin, audited vendor supply chain, or independence from Yahoo upstream.',
            'No currency or adjusted flag is echoed by the history payload; BRL and no-provents basis rely on the pinned issuer configuration, request, and primary corroboration.',
            'Retrospectively retrieved prices are not a point-in-time archive; cause and revision chronology of Yahoo disagreements remain unproved.',
            'This review does not establish financial admission, historical individual XGBoost training observations, or cash settlement.'
        ],
        'tests': {'exit_code': results.exit_code,
                  'passed': sum(r['outcome'] == 'passed' for r in results.rows),
                  'failed': len(failures), 'results': results.rows}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    results = Results()
    status = pytest.main([str(Path(__file__).resolve()), '-q', '-p', 'no:cacheprovider'], plugins=[results])
    results.exit_code = int(status)
    if args.report:
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden')):
            report = build_report(results)
        REPORT.write_text(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        print(json.dumps({'report': str(REPORT), 'decision': report['decision'],
                          'passed': report['tests']['passed'], 'failed': report['tests']['failed']}))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
