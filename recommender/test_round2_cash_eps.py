"""Real native originals, exact-current guards, and ordinary offline gates."""
from copy import deepcopy
from datetime import date
import hashlib
import json
import socket

import pytest

from . import round2_cash_eps as eps
from .market_inventory import load_inventory


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('EPS reading attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def tables():
    return eps.current_tables()


@pytest.fixture(scope='module')
def identities():
    stocks = load_inventory(eps.ROOT)['stocks']
    return [next(r for r in stocks if r['ticker'] == t) for t in eps.TARGETS]


@pytest.fixture(scope='module')
def readings():
    return eps.get_cash_eps_readings(eps.ROOT, eps.CUTOFF)


@pytest.fixture(scope='module')
def gates():
    before = eps.replay(adapter=False)
    after = eps.replay(adapter=True)
    report = {'baseline': before, 'documented': after}
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode()).hexdigest()
    eps.cash.save_report(eps.REPORTS / ('cash-eps-test-gates-' + digest + '.json'), report)
    return before, after


def test_latest_native_filings_not_older_fallback(tables):
    masters, rows, _ = tables
    for ticker in eps.TARGETS:
        assert tuple(masters[ticker][k] for k in ('VERSAO', 'ID_DOC', 'DT_RECEB')) == eps.EXPECTED[ticker]
        assert masters[ticker]['DT_REFER'] == '2025-12-31'
        assert rows[ticker]['DRE_con'] and rows[ticker]['DRE_ind']
        assert rows[ticker]['BPA_con'] and rows[ticker]['DFC_MI_con']


def test_actual_pdf_semantics_and_immutable_review(readings):
    proof = json.loads(eps.cash.read(eps.ROOT, {'file': eps.REVIEW_FILE, 'sha256': eps.REVIEW_SHA}))
    assert proof['findings']['ALLD3']['genuine_absence'] is False
    assert proof['findings']['JSLG3']['genuine_absence'] is False
    assert 'Controladora e Consolidado' in proof['literal_pages']['ALLD3']['50']
    assert '30.1 - - 0,28724 0,72865' in proof['literal_pages']['JSLG3']['25']
    assert proof['policy']['scope_mix'] is proof['policy']['eps_inferred'] is False
    for source in readings[1]:
        assert hashlib.sha256(eps.cash.read(eps.ROOT, source)).hexdigest() == source['sha256']
        assert source['point_in_time_archive_verified'] is False


def test_allied_literal_parent_numerator_and_basic_eps(tables, readings):
    reading = next(r for r in readings[0] if r['ticker'] == 'ALLD3')
    assert reading['accounts_scope'] == 'consolidated'
    assert reading['profit_account'] == '3.11'
    assert reading['basic_eps'] == [{
        'account': '3.99.01.01', 'share_class': 'ON', 'value': '3.5109',
        'numerator_brl': '332712000', 'weighted_shares': '94766696',
        'numerator_rounding_brl': '500', 'shares_rounding': '0.5'}]
    eps.native._checked_note_eps(tables[1]['ALLD3']['DRE_con'], reading)


def test_jsl_preserves_native_eps_without_individual_denominator(readings):
    reading = next(r for r in readings[0] if r['ticker'] == 'JSLG3')
    assert reading['accounts_scope'] == 'consolidated'
    assert reading['discard_basic_eps_accounts'] == ['3.99.01.02']
    assert reading['basic_eps'] == []
    assert 'profit_account' not in reading
    assert 'parent_profit_brl' not in reading
    assert 'weighted_shares' not in json.dumps(reading)
    assert reading['primary_evidence']['pdf_page'] == 25


def test_production_hook_is_read_only(identities, tables, monkeypatch):
    monkeypatch.setattr(eps, 'current_tables', lambda *args: tables)
    monkeypatch.setattr(eps, '_save', lambda *args: pytest.fail('production hook wrote a file'))
    readings, _ = eps.get_cash_eps_readings(eps.ROOT, eps.CUTOFF, identities)
    assert {r['ticker'] for r in readings} == set(eps.TARGETS)


def test_counterfactual_validates_proof_before_suppression(monkeypatch):
    def invalid(*args, **kwargs):
        raise ValueError('invalid_primary_proof')
    monkeypatch.setattr(eps, 'get_cash_eps_readings', invalid)
    monkeypatch.setattr(eps.native, 'prepare_financials',
                        lambda *args: pytest.fail('counterfactual bypassed evidence validation'))
    with pytest.raises(ValueError, match='invalid_primary_proof'):
        eps.replay(adapter=False)


def test_counterfactual_requires_actual_production_hook(readings, monkeypatch):
    monkeypatch.setattr(eps, 'get_cash_eps_readings', lambda *args: readings)
    monkeypatch.setattr(eps.native, 'prepare_financials', lambda *args: ([], [], []))
    with pytest.raises(ValueError, match='production_cash_eps_hook_not_exercised'):
        eps.replay(adapter=False)


@pytest.mark.parametrize('field,value', [('cvm', 1), ('share_class', 'PN'), ('isin', 'BRWRONGCNOR1')])
def test_exact_identity_required(field, value, identities, tables, monkeypatch):
    monkeypatch.setattr(eps, 'current_tables', lambda *args: tables)
    bad = deepcopy(identities)
    bad[0][field] = value
    with pytest.raises(ValueError, match='exact_instrument_identity_required'):
        eps.get_cash_eps_readings(eps.ROOT, eps.CUTOFF, bad)


def test_unreviewed_cutoff_refused():
    with pytest.raises(ValueError, match='unreviewed_cutoff'):
        eps.get_cash_eps_readings(eps.ROOT, date(2026, 3, 1))


def test_changed_original_refused(tmp_path):
    path = tmp_path / eps.REVIEW_FILE
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match='original_hash_changed'):
        eps.get_cash_eps_readings(tmp_path, eps.CUTOFF)


def test_newer_filing_cannot_reuse_review(monkeypatch):
    original = eps.native._rows
    def rows(archive, member):
        result = list(original(archive, member))
        if member == 'dfp_cia_aberta_2025.csv':
            result = [{**r, 'VERSAO': '2', 'ID_DOC': '999999'} if int(r['CD_CVM']) == 25330 else r
                      for r in result]
        return result
    monkeypatch.setattr(eps.native, '_rows', rows)
    with pytest.raises(ValueError, match='latest_filing_changed_no_fallback'):
        eps.current_tables()


def test_full_dre_changes_refused(tables, monkeypatch):
    altered = deepcopy(tables)
    altered[1]['JSLG3']['DRE_con'][0]['VL_CONTA'] = '999999'
    monkeypatch.setattr(eps, 'current_tables', lambda *args: altered)
    with pytest.raises(ValueError, match='review_scope_or_latest_filing_changed'):
        eps.get_cash_eps_readings(eps.ROOT, eps.CUTOFF)


@pytest.mark.parametrize('field,value', [('value', '3.3961'), ('numerator_brl', '332712'),
                                         ('weighted_shares', '97968344')])
def test_normal_note_crosscheck_refuses_wrong_basis(tables, readings, field, value):
    reading = deepcopy(next(r for r in readings[0] if r['ticker'] == 'ALLD3'))
    reading['basic_eps'][0][field] = value
    with pytest.raises(ValueError):
        eps.native._checked_note_eps(tables[1]['ALLD3']['DRE_con'], reading)


@pytest.mark.parametrize('ticker,value,profit', [('ALLD3', 3.5109, 332712000), ('JSLG3', .28724, 81704000)])
def test_actual_normal_one_and_current_financial_gates(gates, tables, ticker, value, profit):
    before, after = [next(r for r in run['targets'] if r['ticker'] == ticker) for run in gates]
    assert before['source_gate_pass'] and not before['current_financial_pass']
    assert after['source_gate_pass'] and after['current_financial_pass']
    assert after['checks']['independent_price_matches'] == 61
    assert all(r['profit_per_share'] is None for r in before['latest_records'])
    assert len(after['latest_records']) == 1
    current = after['latest_records'][0]
    assert current['profit_per_share'] == value
    account = '3.11' if ticker == 'ALLD3' else '3.11.01'
    reported_profit = next(r for r in tables[1][ticker]['DRE_con'] if r['CD_CONTA'] == account)
    assert eps.native._brl(reported_profit) == profit
    assert current['fiscal_end'] == '2025-12-31'
    assert current['published_at'] == eps.EXPECTED[ticker][2]
    assert current['accounts_scope'] == 'consolidated'
    assert current['filing_id'] == eps.EXPECTED[ticker][1]
    assert after['current_financial'][1]['fiscal_end'] == '2025-12-31'
    assert gates[1]['network_calls'] == 0
    assert gates[1]['normal_gates_modified'] is gates[1]['cash_hooks_modified'] is False
    assert gates[1]['production_research_unmodified'] is True
    assert gates[1]['financial_hooks_overridden'] == []
    assert gates[1]['counterfactual'] is None
    assert gates[0]['production_research_unmodified'] is True
    assert gates[0]['financial_hooks_overridden'] == ['get_cash_eps_readings']
    assert gates[0]['counterfactual']['proof_validated_before_suppression'] is True
    assert gates[0]['counterfactual']['production_reader_calls'] == 1


@pytest.mark.parametrize('ticker', ['ALLD3', 'JSLG3'])
def test_all_61_independent_months_still_required(identities, ticker):
    identity = next(r for r in identities if r['ticker'] == ticker)
    audit = eps.cash.inspect_evidence(eps.ROOT, identity)['quote_audit']
    assert audit['all61_compared'] and audit['monthly_inputs_agree61']
    assert len(audit['monthly_comparisons']) == 61
    assert all(r['date_matches'] for r in audit['monthly_comparisons'])
    assert audit['factors_fitted'] is audit['price_values_changed'] is False
