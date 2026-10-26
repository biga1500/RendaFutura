"""Offline contract and failure-preservation tests against pinned originals."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import pending_b_independent as provider


ROOT = Path(__file__).parent / 'data/market'


def identity(ticker='MTRE3'):
    cvm, share_class, isin = provider.TARGETS[ticker]['identity']
    return dict(ticker=ticker, cvm=cvm, share_class=share_class, isin=isin)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline adapter attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def evidence():
    return {ticker: provider.inspect_evidence(ROOT, identity(ticker), provider.CUTOFF)
            for ticker in provider.TARGETS}


@pytest.mark.parametrize('ticker,count', [('MTRE3', 40), ('RDOR3', 25)])
def test_exact_identity_is_not_legacy_whitelist_blocked(evidence, ticker, count):
    data = evidence[ticker]
    assert data['blockers'] == []
    assert len(data['calendar']['events']) == count
    checks = data['checks']
    assert checks['pre_window_cash_rows'] > 0
    assert checks['window_cash_rows'] > 0
    assert checks['independent_monthly_prices'] == 61
    assert checks['independent_daily_prices'] == 1268
    assert checks['model_monthly_raw_quotes'] == len(data['raw_quotes']) == 61
    assert checks['adjusted_parameter'] is False
    assert checks['published_widget_config_and_template_verified']
    history = checks['issuer_CVM_CNPJ_and_ON_class_history']
    assert [row['year'] for row in history] == list(range(2021, 2027))
    assert all(row['native_row']['Codigo_Negociacao'] == ticker for row in history)
    assert all(row['native_row']['Valor_Mobiliario'] == 'Acoes Ordinarias'
               or provider.research.normalized(row['native_row']['Valor_Mobiliario']) == 'acoes ordinarias'
               for row in history)
    assert all(type(key) is date and type(value) is float and value > 0
               for key, value in data['expected_cash'].items())
    assert all(type(key) is date and type(value) in (int, float) and value > 0
               for key, value in data['raw_quotes'].items())
    assert all(row['sha256'] == hashlib.sha256((ROOT / row['file']).read_bytes()).hexdigest()
               for row in data['sources'])
    assert not checks['payment_dates_inferred']
    assert not checks['record_dates_derived_from_ex_dates']
    assert not checks['zero_cash_events_created']
    assert not checks['net_to_gross_inference']


@pytest.mark.parametrize('ticker', ['MTRE3', 'ARML3', 'RDOR3'])
def test_native_decimal_installments_aggregate_once_without_rounding(evidence, ticker):
    data = evidence[ticker]
    events = data['calendar']['events']
    seen, totals = set(), {}
    for event in events:
        key = tuple(event[k] for k in ('label', 'record_date', 'payment_date', 'gross_per_share_decimal'))
        assert key not in seen
        seen.add(key)
        assert type(event['gross_per_share_decimal']) is str
        day = date.fromisoformat(event['record_date'])
        totals[day] = totals.get(day, Decimal(0)) + Decimal(event['gross_per_share_decimal'])
        assert event['source_locator'] and event['native_cells']
        assert event['published_at'] is None
    assert data['expected_cash'] == {day: float(value) for day, value in totals.items()}


def test_armac_preserves_all_three_cash_blockers_and_unknown_dates(evidence, monkeypatch):
    data = evidence['ARML3']
    assert data['blockers'] == [
        'pending_b_cash_does_not_cover_window_start',
        'pending_b_payment_date_unresolved',
        'pending_b_cash_gross_tax_basis_unresolved',
    ]
    assert sum(row['payment_date'] is None for row in data['calendar']['events']) == 3
    assert min(data['expected_cash']) > date(2021, 9, 30)
    assert all(value > 0 for value in data['expected_cash'].values())
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: data)
    with pytest.raises(ValueError, match='pending_b_cash_does_not_cover_window_start'):
        provider.load_evidence(ROOT, identity('ARML3'), provider.CUTOFF)


@pytest.mark.parametrize('ticker,days', [
    ('MTRE3', ['2022-09-26']),
    ('ARML3', ['2021-10-20', '2023-12-01']),
    ('RDOR3', ['2022-09-26', '2023-12-01']),
])
def test_intramonth_conflicts_retained_but_not_used_as_monthly_model_inputs(evidence, ticker, days):
    proof = evidence[ticker]['primary_actions']
    assert proof['actions'] == []
    assert not proof['blocked']
    assert not proof['raw_full_window_agrees']
    assert proof['model_monthly_inputs_agree61']
    assert proof['model_monthly_input_count'] == len(proof['model_monthly_inputs']) == 61
    assert proof['model_monthly_mismatches'] == []
    assert all(row['date_matches'] and row['price_matches'] for row in proof['model_monthly_inputs'])
    assert proof['mismatch_days'] == len(days)
    assert [row['date'] for row in proof['daily_disagreements']] == days
    assert [row['date'] for row in proof['other_daily_disagreements_retained_not_used']] == days
    assert not set(days).intersection(day.isoformat() for day in evidence[ticker]['raw_quotes'])
    assert proof['compared_days'] > 1200
    assert proof['observed_action_events'] == proof['actions']
    assert proof['FCA_exact_security_class_years_verified'] == list(range(2021, 2027))
    assert not proof['empty_actions_inferred_from_price_adjustment']
    assert proof['core_tolerances_unchanged'] == {'relative': .006, 'absolute_BRL': .015}
    assert not proof['price_or_action_values_modified']


@pytest.mark.parametrize('key,value', [('cvm', 1), ('share_class', 'PN'), ('isin', 'BRMTREACNPR1')])
def test_exact_issuer_security_binding_cannot_be_overridden(key, value):
    stock = identity()
    stock[key] = value
    with pytest.raises(ValueError, match='pending_b_exact_CVM_class_ISIN_mismatch'):
        provider.load_evidence(ROOT, stock, provider.CUTOFF)


def test_unsupported_scope_or_cutoff_returns_none():
    assert provider.load_evidence(ROOT, {'ticker': 'ITSA4'}, provider.CUTOFF) is None
    assert provider.load_evidence(ROOT, identity(), date(2026, 8, 31)) is None


@pytest.mark.parametrize('target', ['receipt', 'body'])
def test_changed_receipt_or_original_bytes_fail_hash(tmp_path, target):
    spec = provider.TARGETS['MTRE3']
    source, body = provider._source(ROOT.resolve(), spec['calendar_url'], spec['calendar'])
    receipt_file = provider.CACHE + '/response-' + hashlib.sha256(spec['calendar_url'].encode()).hexdigest() + '.json'
    for relative in (receipt_file, source['file']):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    changed = tmp_path / (receipt_file if target == 'receipt' else source['file'])
    changed.write_bytes(changed.read_bytes() + b' ')
    with pytest.raises(ValueError, match='pending_b_original_hash_changed'):
        provider._source(tmp_path.resolve(), spec['calendar_url'], spec['calendar'])
    assert hashlib.sha256(body).hexdigest() == spec['calendar'][0]


def native_event(payment, **changes):
    return {**identity(), 'record_date': payment, 'payment_date': payment,
            'record_date_role': 'entitlement_record', 'payment_date_role': 'payment',
            'approval_date': None, 'label': 'DIVIDENDO', 'amount_tax_basis': 'dividend_per_share',
            'gross_per_share_decimal': '0.1000000000001', 'gross_reconstructed_from_net': False,
            'issues': [], **changes}


def native_events():
    return [native_event('2021-09-29'), native_event('2025-09-30')]


def test_own_validator_requires_pre_window_and_in_window_cash():
    events = native_events() + [native_event('2026-12-31')]
    assert provider._validate_calendar(identity(), events, provider.CUTOFF) == {
        'pre_window_cash_rows': 1, 'window_cash_rows': 1, 'future_cash_rows': 1}
    with pytest.raises(ValueError, match='pending_b_pre_window_or_in_window_cash_missing'):
        provider._validate_calendar(identity(), events[1:], provider.CUTOFF)
    with pytest.raises(ValueError, match='pending_b_pre_window_or_in_window_cash_missing'):
        provider._validate_calendar(identity(), events[:1], provider.CUTOFF)


@pytest.mark.parametrize('changes,reason', [
    ({'cvm': 1}, 'cash_exact_identity_disagrees'),
    ({'share_class': 'PN'}, 'cash_exact_identity_disagrees'),
    ({'isin': 'BRMTREACNPR1'}, 'cash_exact_identity_disagrees'),
    ({'record_date_role': 'ex_date'}, 'cash_date_role_disagrees'),
    ({'payment_date_role': 'approval'}, 'cash_date_role_disagrees'),
    ({'payment_date': '20250930'}, 'exact_readable_date_required'),
    ({'payment_date': '2025-W40-2'}, 'exact_readable_date_required'),
    ({'record_date': '2025-10-01'}, 'payment_precedes_record_date'),
    ({'approval_date': '2025-10-01'}, 'payment_precedes_approval'),
    ({'label': 'OUTRO'}, 'cash_gross_tax_basis_unresolved'),
    ({'label': 'JCP'}, 'cash_gross_tax_basis_unresolved'),
    ({'amount_tax_basis': 'net'}, 'cash_gross_tax_basis_unresolved'),
    ({'gross_reconstructed_from_net': True}, 'native_cash_value_unusable'),
    ({'gross_per_share_decimal': True}, 'native_decimal_string_required'),
    ({'gross_per_share_decimal': 0.1}, 'native_decimal_string_required'),
    ({'gross_per_share_decimal': 'NaN'}, 'native_cash_value_unusable'),
    ({'gross_per_share_decimal': 'Infinity'}, 'native_cash_value_unusable'),
    ({'gross_per_share_decimal': '0'}, 'native_cash_value_unusable'),
    ({'gross_per_share_decimal': '-0.1'}, 'native_cash_value_unusable'),
    ({'gross_per_share_decimal': 'broken'}, 'native_cash_value_unusable'),
    ({'issues': ['unresolved']}, 'native_cash_value_unusable'),
])
def test_own_validator_preserves_other_semantic_errors(changes, reason):
    events = native_events()
    events[1].update(changes)
    with pytest.raises(ValueError, match='pending_b_' + reason):
        provider._validate_calendar(identity(), events, provider.CUTOFF)


def test_duplicate_native_installment_rejected_in_validator_and_parser(monkeypatch):
    events = native_events()
    with pytest.raises(ValueError, match='pending_b_duplicate_native_cash_installment'):
        provider._validate_calendar(identity(), [*events, deepcopy(events[1])], provider.CUTOFF)
    spec = provider.TARGETS['MTRE3']
    source, body = provider._source(ROOT.resolve(), spec['calendar_url'], spec['calendar'])
    facts = provider.research.parse_supported_table(body, source, identity())
    monkeypatch.setattr(provider.research, 'parse_supported_table', lambda *args: facts[:-1] + [facts[0]])
    with pytest.raises(ValueError, match='pending_b_duplicate_native_cash_installment'):
        provider._cash(body, source, identity())


@pytest.mark.parametrize('ticker', ['MTRE3', 'ARML3', 'RDOR3'])
def test_empty_action_list_requires_all_61_monthly_inputs_and_keeps_daily_audit(evidence, monkeypatch, ticker):
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: evidence[ticker])
    proof = provider.validate_primary_actions(ROOT, identity(ticker), provider.CUTOFF, [])
    assert proof['model_monthly_inputs_agree61']
    assert proof['other_daily_disagreements_retained_not_used']
    assert not proof['raw_full_window_agrees']


def daily_quotes(ticker):
    digest = provider.TARGETS[ticker]['quotes'][0]
    body = provider._read(ROOT.resolve(), provider.CACHE + '/body-' + digest + '.bin', digest)
    return {date.fromisoformat(row['date']): row['closed']
            for row in json.loads(body)['data']['tickers'][0]['history']}


def test_missing_entire_month_never_cherry_picks_other_agreeing_dates():
    quotes = daily_quotes('MTRE3')
    quotes = {day: value for day, value in quotes.items() if (day.year, day.month) != (2022, 9)}
    with pytest.raises(ValueError, match='pending_b_independent_quotes_less_than_61_months'):
        provider._monthly_quotes(quotes, provider.CUTOFF)


def test_conflicting_real_monthly_anchor_blocks_even_when_earlier_days_agree(evidence, monkeypatch):
    quotes = daily_quotes('MTRE3')
    day = min(evidence['MTRE3']['raw_quotes'])
    quotes[day] *= 1.1
    monthly = provider._monthly_quotes(quotes, provider.CUTOFF)
    assert len(monthly) == 61
    assert monthly[day] == quotes[day]
    proof = provider._raw_window(ROOT.resolve(), identity(), quotes, provider.CUTOFF,
        evidence['MTRE3']['checks']['issuer_CVM_CNPJ_and_ON_class_history'])
    assert proof['blocked'] and not proof['model_monthly_inputs_agree61']
    assert proof['model_monthly_input_count'] == 61
    assert proof['model_monthly_mismatches'][0]['issuer_native_date'] == day.isoformat()
    assert proof['model_monthly_mismatches'][0]['date_matches']
    assert not proof['model_monthly_mismatches'][0]['price_matches']
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: {'primary_actions': proof})
    with pytest.raises(ValueError, match='pending_b_model_monthly_inputs_disagree'):
        provider.validate_primary_actions(ROOT, identity(), provider.CUTOFF, [])
    from .share_basis import reconcile_prices
    content, _ = provider._price_source(ROOT.resolve(), 'MTRE3')
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_independent_source'):
        reconcile_prices(content, 'MTRE3', [], monthly, provider.CUTOFF, evidence_provider=provider.PROVIDER)


def test_date_mismatch_in_monthly_anchor_blocks_even_when_native_price_matches(evidence, monkeypatch):
    quotes = daily_quotes('MTRE3')
    day = min(evidence['MTRE3']['raw_quotes'])
    value = quotes.pop(day)
    previous = max(d for d in quotes if (d.year, d.month) == (day.year, day.month))
    quotes[previous] = value
    proof = provider._raw_window(ROOT.resolve(), identity(), quotes, provider.CUTOFF,
        evidence['MTRE3']['checks']['issuer_CVM_CNPJ_and_ON_class_history'])
    failure = proof['model_monthly_mismatches'][0]
    assert failure['comparison_date'] == day.isoformat()
    assert failure['issuer_native_date'] == previous.isoformat()
    assert not failure['date_matches'] and failure['price_matches']
    assert proof['blocked'] and not proof['model_monthly_inputs_agree61']
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: {'primary_actions': proof})
    with pytest.raises(ValueError, match='pending_b_model_monthly_inputs_disagree'):
        provider.validate_primary_actions(ROOT, identity(), provider.CUTOFF, [])


@pytest.mark.parametrize('ticker', ['MTRE3', 'ARML3', 'RDOR3'])
def test_normal_core_price_gate_checks_all_61_real_monthly_dates(evidence, ticker):
    from .share_basis import reconcile_prices
    content, _ = provider._price_source(ROOT.resolve(), ticker)
    data = evidence[ticker]
    prices, checked, mode = reconcile_prices(content, ticker, data['primary_actions']['actions'],
        data['raw_quotes'], provider.CUTOFF, evidence_provider=provider.PROVIDER)
    assert len(checked) == 61
    assert checked == sorted(day.isoformat() for day in data['raw_quotes'])
    assert {prices[provider.month_end(day.year, day.month)][0] for day in data['raw_quotes']} == set(data['raw_quotes'])
    assert mode in ('split_adjusted', 'raw')


def test_nonempty_action_list_requires_actual_primary_documents(monkeypatch):
    actions = [{'date': '2022-09-26', 'factor': 2}]
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: {
        'primary_actions': {'actions': actions, 'raw_full_window_agrees': True, 'blocked': False}})
    with pytest.raises(ValueError, match='pending_b_primary_action_documents_required'):
        provider.validate_primary_actions(ROOT, identity(), provider.CUTOFF, actions)
    with pytest.raises(ValueError, match='pending_b_observed_action_list_disagrees'):
        provider.validate_primary_actions(ROOT, identity(), provider.CUTOFF, [])
