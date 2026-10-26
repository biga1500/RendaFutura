"""Offline adversarial tests for the isolated exact-security quote getter."""
from copy import deepcopy
from datetime import date
import hashlib
import json
import socket
from urllib.parse import urlsplit

from lxml import html
import pytest

from . import round3_identity_prices as prices
from .share_basis import reconcile_prices


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('tests_must_not_access_network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


@pytest.fixture(scope='module')
def identities():
    return {s: {k: getattr(prices.binding(prices.ROOT, s), k)
                for k in ('ticker', 'cvm', 'share_class', 'isin')} for s in prices.TARGETS}


@pytest.fixture(scope='module')
def records(identities):
    return {s: prices.diagnose(prices.ROOT, identities[s]) for s in prices.TARGETS}


@pytest.mark.parametrize('ticker', prices.TARGETS)
def test_exact_native_identity_and_all_rows_retained(ticker, records):
    evidence = records[ticker]
    proof = evidence['proof']
    assert proof['identity']['ticker'] == ticker
    assert proof['identity']['isin'].startswith('BR')
    assert proof['identity']['cnpj']
    assert proof['native_ISIN_not_inferred_from_suffix']
    assert len(proof['all_native_observations']) == 134
    assert len(proof['all_61_model_months']) == 61
    assert len(proof['all_window_comparisons']) + len(proof['future_observations_not_used']) + len(
        proof['pre_window_observations_not_used']) == 134
    assert proof['all_observations_retained']
    assert not proof['favorable_dates_selected']
    assert not proof['factors_fitted_to_quotes']
    assert not proof['provider_bytes_or_days_replaced']
    assert not proof['adjclose_used']
    assert not proof['gate_tolerance_modified']
    assert not proof['independent_ultimate_upstream_verified']
    assert not proof['point_in_time_archive_verified']
    assert not proof['five_year_unchanged_identity_requirement_added']


@pytest.mark.parametrize('ticker', ('B3SA3', 'BAUH4', 'BMIN3', 'PEAB3'))
def test_production_getter_passes_only_real_core_gate(ticker, records, identities):
    result = prices.load_evidence(prices.ROOT, identities[ticker])
    assert result['proof']['ordinary_gate']['passed']
    assert not result['proof']['conflicts']
    body, _ = prices.provider_source(prices.ROOT, ticker)
    months, dates, mode = reconcile_prices(body, ticker, result['proof']['observed_provider_actions'],
                                          result['raw_quotes'], prices.CUTOFF, evidence_provider=prices.PROVIDER)
    assert len(dates) >= 4 and len(months) >= 61
    assert mode == result['proof']['ordinary_gate']['mode']
    regimes = result['proof']['regimes']
    assert sum(len(r['model_months']) for r in regimes) == 61
    assert all(r['independent_dates'] for r in regimes if r['model_months'])
    assert result == records[ticker]


@pytest.mark.parametrize('ticker', ('EQMA3B', 'BMIN4', 'CEGR3', 'PEAB4'))
def test_getter_fails_closed_with_all_conflicting_dates(ticker, records, identities):
    proof = records[ticker]['proof']
    assert not proof['ordinary_gate']['passed']
    assert proof['ordinary_gate']['reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert proof['conflicts']
    assert all(date.fromisoformat(r['date']) in records[ticker]['raw_quotes'] for r in proof['conflicts'])
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_independent_source'):
        prices.load_evidence(prices.ROOT, identities[ticker])


def test_eqma_two_literal_non_month_end_conflicts(records):
    proof = records['EQMA3B']['proof']
    assert len(proof['all_window_comparisons']) == 124
    assert [(r['date'], r['close_decimal']) for r in proof['conflicts']] == [
        ('2026-01-06', '30.99'), ('2025-11-13', '31.99')]
    assert [round(r['provider_close'], 2) for r in proof['conflicts']] == [30.01, 31.50]
    assert all(r['provider_OHLCV'] and r['provider_timestamp'] and r['xpath'] for r in proof['conflicts'])
    assert proof['observed_provider_actions'] == []
    assert proof['identity']['isin'] == 'BREQMAACNOR8'


def test_b3_pre_window_action_retained_not_fitted(records):
    proof = records['B3SA3']['proof']
    assert len(proof['all_window_comparisons']) == 118
    assert proof['observed_provider_actions'] == [{'date': '2021-05-17', 'ratio': 3.0}]
    assert proof['window_actions'] == []
    assert proof['regimes'][0]['factor_after'] == 1
    assert len(proof['regimes']) == 1
    assert proof['identity']['isin'] == 'BRB3SAACNOR6'


@pytest.mark.parametrize('field,value', [('cvm', 16608), ('cvm', True), ('share_class', 'PN'),
    ('isin', 'BREQMAACNOR8'), ('cnpj', '06.272.793/0001-84')])
def test_cross_issuer_or_class_identity_rejected(field, value, identities):
    identity = {**identities['B3SA3'], field: value}
    with pytest.raises(ValueError, match='exact_native'):
        prices.load_evidence(prices.ROOT, identity)


@pytest.mark.parametrize('ticker', ('ENMA3B', 'EQTL3', 'B3SA4', 'BMIN', 'PLPL3', 'UNKNOWN'))
def test_no_alias_or_sibling_instrument(ticker):
    assert prices.load_evidence(prices.ROOT, {'ticker': ticker}) is None


@pytest.mark.parametrize('cutoff', [date(2026, 10, 31), date(2025, 9, 30), '2026-09-30'])
def test_cutoff_strictness(cutoff, identities):
    with pytest.raises(ValueError, match='cutoff_outside_pinned_window'):
        prices.load_evidence(prices.ROOT, identities['B3SA3'], cutoff)


def test_changed_provider_bytes_and_actions_rejected(identities):
    with pytest.raises(ValueError, match='caller_provider_bytes_changed'):
        prices.load_evidence(prices.ROOT, identities['B3SA3'], comparison_content=b'{}')
    with pytest.raises(ValueError, match='caller_actions_changed'):
        prices.load_evidence(prices.ROOT, identities['B3SA3'], observed_actions=[])


def test_existing_same_day_conflict_not_overwritten(records, identities):
    day = date(2026, 9, 30)
    original = {day: records['B3SA3']['raw_quotes'][day] + 1}
    result = prices.diagnose(prices.ROOT, identities['B3SA3'], independent_quotes=original)
    assert result['raw_quotes'][day] == original[day]
    conflict, = result['proof']['independent_source_conflicts']
    assert conflict['date'] == day.isoformat()
    assert not result['proof']['ordinary_gate']['passed']
    with pytest.raises(ValueError, match='identity_prices_independent_sources_disagree'):
        prices.load_evidence(prices.ROOT, identities['B3SA3'], independent_quotes=original)


def test_existing_distinct_conflicting_date_retained(identities):
    day = date(2025, 1, 2)
    result = prices.diagnose(prices.ROOT, identities['B3SA3'], independent_quotes={day: 999.0})
    assert result['raw_quotes'][day] == 999.0
    assert not result['proof']['ordinary_gate']['passed']


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, 0, True, '13.00'])
def test_invalid_existing_quote_rejected(bad, identities):
    with pytest.raises(ValueError, match='invalid_original_independent_quote'):
        prices.load_evidence(prices.ROOT, identities['B3SA3'], independent_quotes={date(2026, 9, 30): bad})


@pytest.mark.parametrize('mutation,error', [('title', 'native_title_security_mismatch'),
    ('form', 'native_form_security_mismatch'), ('schema', 'native_daily_schema_changed'),
    ('duplicate', 'duplicate_or_unordered_session'), ('price', 'invalid_native_price'),
    ('OHLC', 'native_OHLC_inconsistent'), ('inactive', 'inactive_or_unknown_native_session'),
    ('remove', 'pinned_native_session_count_changed')])
def test_native_parser_adversarial(mutation, error):
    body, _ = prices.source(prices.ROOT, 'B3SA3')
    tree = html.fromstring(body.decode('utf8'))
    table = next(t for t in tree.xpath('//table') if t.xpath('.//tr') and
        [c.text_content().strip() for c in t.xpath('.//tr')[0].xpath('./td|./th')] == prices.HEADERS)
    rows = table.xpath('.//tr')[1:]
    if mutation == 'title':
        tree.xpath('//title')[0].text = 'ibovX - EQMA3B.'
    elif mutation == 'form':
        tree.xpath('//input[@id="ContentPlaceHolderCentro_txtAcoesBovespa"]')[0].set('value', 'B3SA4')
    elif mutation == 'schema':
        table.xpath('.//tr')[0].xpath('./td|./th')[3].text = 'Media'
    elif mutation == 'duplicate':
        rows[1].xpath('./td')[0].text = rows[0].xpath('./td')[0].text_content()
    elif mutation == 'price':
        rows[0].xpath('./td')[3].text = 'NaN'
    elif mutation == 'OHLC':
        rows[0].xpath('./td')[3].text = '999,00'
    elif mutation == 'inactive':
        rows[0].xpath('./td')[8].text = '0'
    elif mutation == 'remove':
        rows[-1].getparent().remove(rows[-1])
    with pytest.raises(ValueError, match=error):
        prices.parse_ibovx(html.tostring(tree, encoding='utf8'), 'B3SA3')


def test_receipt_hash_tamper_rejected(monkeypatch):
    monkeypatch.setitem(prices.PINS, 'B3SA3', (prices.PINS['B3SA3'][0], '0' * 64))
    with pytest.raises(ValueError, match='round2_original_hash_changed'):
        prices.source(prices.ROOT, 'B3SA3')


@pytest.mark.parametrize('ticker', prices.TARGETS)
def test_literal_proof_locator_receipt_and_retrieval(ticker, records):
    ref = records[ticker]['sources'][0]
    body = prices.read(prices.ROOT, ref['file'], ref['sha256'])
    tree = html.fromstring(body.decode('utf8'))
    assert ref['retrieved_at'].startswith('2026-10-26T')
    assert ref['receipt_sha256'] == prices.PINS[ticker][1]
    receipt = json.loads(prices.read(prices.ROOT, ref['receipt_file'], ref['receipt_sha256']))
    assert receipt['status'] == 200 and receipt['retries'] == 0 and receipt['error'] is None
    for row in records[ticker]['proof']['all_native_observations']:
        node, = tree.xpath(row['xpath'])
        assert [' '.join(c.text_content().split()) for c in node.xpath('./td')] == row['literal_cells']


def test_scope_network_audit_no_denied_hosts():
    requests = list((prices.ROOT / prices.CACHE).glob('request-*.json'))
    assert requests
    for path in requests:
        item = json.loads(path.read_bytes())
        assert item['scope'] == prices.SCOPE and item['retries'] == 0
        assert urlsplit(item['url']).hostname in ('ibovx.com.br', 'bolsadata.com.br')
    assert not any((prices.ROOT / 'raw/expanded').glob('b3-detail-21610.json'))
    assert not any((prices.ROOT / 'raw/expanded').glob('b3-detail-16608.json'))


def test_real_quote_gate_still_requires_four_dates_and_all_regimes(records):
    body, _ = prices.provider_source(prices.ROOT, 'B3SA3')
    actions = records['B3SA3']['proof']['observed_provider_actions']
    anchors = dict(list(records['B3SA3']['raw_quotes'].items())[:3])
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        reconcile_prices(body, 'B3SA3', actions, anchors, prices.CUTOFF, evidence_provider=prices.PROVIDER)
    artificial = actions + [{'date': '2026-01-02', 'ratio': 1.01}]
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_prices(body, 'B3SA3', artificial, records['B3SA3']['raw_quotes'], prices.CUTOFF,
                         evidence_provider=prices.PROVIDER)


def test_isolated_source_replay_does_not_waive_next_gate():
    before = prices.protected(prices.ROOT)
    replay = prices.isolated_replay()
    b3, eqma = replay['results']
    assert b3['ticker'] == 'B3SA3'
    assert b3['quote_gate_calls'][0]['gate']['passed']
    assert not b3['source_candidate']
    assert b3['pending_reason'] == 'issuer_primary_share_action_document_missing'
    assert eqma['ticker'] == 'EQMA3B'
    assert not eqma['quote_gate_calls'][0]['gate']['passed']
    assert eqma['pending_reason'] == 'historical_quote_or_share_scale_disagrees_with_independent_source'
    assert replay['protected_before'] == replay['protected_after'] == before
    assert prices.protected(prices.ROOT) == before
    assert not replay['primary_action_validator_patched']
    assert not replay['financial_gate_patched']
    assert not replay['production_integration']
    assert not replay['model_admissions']
    assert replay['network_calls'] == 0
