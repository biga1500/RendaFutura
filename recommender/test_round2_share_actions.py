"""Offline checks for literal action proof and strict full-window quote use."""

from copy import deepcopy
from datetime import date
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import round2_share_actions as provider


ROOT = provider.ROOT.resolve()


def identity(ticker='BMEB4'):
    cvm, share, isin = provider.IDENTITIES[ticker]
    return dict(ticker=ticker, cvm=cvm, share_class=share, isin=isin)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('round2 evidence loader attempted a network call')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def evidence():
    return {t: provider.inspect_evidence(ROOT, identity(t)) for t in provider.IDENTITIES}


@pytest.mark.parametrize('ticker,count', [
    ('ITSA3', 5), ('ITSA4', 5), ('BMEB3', 1), ('BMEB4', 1),
    ('DEXP3', 1), ('DEXP4', 1), ('IRBR3', 1),
])
def test_all_in_window_actions_have_literal_final_primary_proof(evidence, ticker, count):
    proof = evidence[ticker]['primary_actions']
    assert len(proof['actions']) == len(proof['readings']) == count
    assert not proof['blocked'] and not proof['factor_fitted_to_prices']
    assert not proof['record_or_credit_date_used_as_ex_date']
    assert [r['year'] for r in proof['class_history']] == list(range(2021, 2027))
    for reading in proof['readings']:
        assert reading['identity'] == identity(ticker)
        assert reading['published_at'] <= '2026-09-30'
        assert reading['literal_ratio_and_effective_date_verified']
        assert reading['receipt_file'] and reading['CVM_index']['csv_line'] >= 2
        assert len(reading['locators']) >= 2
        assert all(row['literal'] and row['text_start'] < row['text_end'] for row in reading['locators'])
        total = reading['total_share_factor']
        literal = reading['literal_ratio_units']
        expected = Fraction(literal['new_or_bonus_shares'], literal['old_shares'])
        if literal['bonus_is_additional']:
            expected += 1
        assert Fraction(total['numerator'], total['denominator']) == expected


def test_exact_five_itausa_dates_and_ratios_are_board_minutes_not_credit_dates(evidence):
    expected = [('2021-12-21', 1.05), ('2022-11-11', 1.1), ('2023-11-28', 1.05),
                ('2024-12-03', 1.05), ('2025-12-19', 1.02)]
    for ticker in ('ITSA3', 'ITSA4'):
        readings = evidence[ticker]['primary_actions']['readings']
        assert [(r['date'], float(Fraction(**r['total_share_factor']))) for r in readings] == expected
        assert readings[1]['version'] == '2'
        assert readings[-1]['source']['url'].endswith('26e834b2-7fca-13e0-f41a-dfebd48c7df9?origin=2')


def test_irbr_old_action_remains_outside_used_window_without_fabricated_proof(evidence):
    proof = evidence['IRBR3']['primary_actions']
    assert proof['actions'] == [{'date': '2023-01-25', 'ratio': 1 / 30}]
    assert proof['outside_window_observations_retained'] == [{'date': '2019-09-26', 'ratio': 3.0}]


def test_bmeb4_all61_and_both_primary_regimes_pass(evidence):
    data = evidence['BMEB4']
    assert data['blockers'] == []
    checks = data['checks']
    assert len(data['raw_quotes']) == checks['monthly_input_count'] == checks['native_month_count'] == 61
    assert checks['modes_agreeing_all61_dates_and_prices'] == ['split_adjusted']
    assert len(checks['used_regimes']) == 2
    assert {r['documented_factor_after'] for r in checks['used_regimes']} == {1, 2}
    assert all(r['all_dates_agree'] and r['all_split_adjusted_prices_agree'] for r in checks['used_regimes'])
    assert checks['monthly_conflicts'] == []
    assert len(data['daily']) == 1266
    assert all(type(day) is date and type(value) is float and value > 0 for day, value in data['raw_quotes'].items())


@pytest.mark.parametrize('ticker', ['BMEB3', 'IRBR3', 'ITSA3', 'ITSA4'])
def test_every_model_monthly_conflict_remains_blocking(evidence, monkeypatch, ticker):
    data = evidence[ticker]
    checks = data['checks']
    assert checks['monthly_input_count'] == checks['native_month_count'] == 61
    assert not checks['modes_agreeing_all61_dates_and_prices']
    assert checks['monthly_conflicts']
    assert checks['all_daily_regime_comparisons_retained']
    assert 'round2_all61_model_monthly_dates_or_prices_disagree' in data['blockers']
    assert not checks['favorable_months_selected']
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: deepcopy(data))
    with pytest.raises(ValueError, match='all61_model_monthly'):
        provider.load_evidence(ROOT, identity(ticker), provider.CUTOFF)


@pytest.mark.parametrize('ticker', ['DEXP3', 'DEXP4'])
def test_dexxos_unknown_native_history_is_neither_synthesized_nor_zero(evidence, ticker):
    data = evidence[ticker]
    assert data['daily'] == data['raw_quotes'] == {}
    assert data['blockers'][0] == 'round2_native_history_not_published_or_recovered'
    assert len(data['checks']['monthly_inputs']) == 61
    assert all(r['issuer_native_close'] is None for r in data['checks']['monthly_inputs'])


@pytest.mark.parametrize('key,value', [('cvm', 1326), ('cvm', True), ('share_class', 'ON'), ('isin', 'BRBMEBACNOR8')])
def test_identity_cannot_cross_classes_or_issuers(key, value):
    stock = identity()
    stock[key] = value
    with pytest.raises(ValueError, match='round2_exact_CVM_class_ISIN_mismatch'):
        provider.inspect_evidence(ROOT, stock)


def test_other_cutoffs_and_tickers_do_not_use_this_provider():
    assert provider.load_evidence(ROOT, {'ticker': 'EQTL3'}, provider.CUTOFF) is None
    assert provider.load_evidence(ROOT, identity(), date(2026, 8, 31)) is None


def test_changed_index_bytes_are_not_sealed_implicitly(tmp_path):
    relative = f'{provider.CACHE}/index-{provider.INDEX_SHA}.json'
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes((ROOT / relative).read_bytes() + b' ')
    with pytest.raises(ValueError, match='round2_original_hash_changed'):
        provider.packet(tmp_path)


@pytest.mark.parametrize('target', ['body', 'receipt'])
def test_changed_native_original_or_receipt_is_rejected(tmp_path, target):
    spec = provider.packet(ROOT)['targets']['BMEB4']['quotes'][0]
    for relative in (spec['file'], spec['receipt_file']):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / relative).read_bytes())
    altered = tmp_path / spec['file' if target == 'body' else 'receipt_file']
    altered.write_bytes(altered.read_bytes() + b' ')
    with pytest.raises(ValueError, match='round2_original_hash_changed'):
        provider.checked_source(tmp_path, spec)


@pytest.mark.parametrize('relative', ['../a', '/raw/a', 'C:/raw/a', 'raw/../a', 'raw\\a', 'raw/./a', 'raw//a'])
def test_paths_cannot_escape_or_alias_pinned_originals(tmp_path, relative):
    with pytest.raises(ValueError, match='round2_unsafe_evidence_path'):
        provider.read(tmp_path, relative, '0' * 64)


def test_both_paid_subscription_notices_and_documentary_discrepancies_are_preserved():
    proof = provider.documentary_mismatches(ROOT)
    assert len(proof['discrepancies']) == 3
    assert [(r['ex_rights_date'], r['subscription_percent_literal']) for r in proof['subscription_rights']] == [
        ('2023-08-18', '1,390757436%'), ('2025-02-18', '1,3766678%')]
    assert all(r['price_adjustment_factor'] is None and r['split_factor'] is None for r in proof['subscription_rights'])
    assert proof['discrepancies'][0]['calendar_decimal'] != proof['discrepancies'][0]['final_notice_decimal']
    assert proof['discrepancies'][1]['ordinary'] + proof['discrepancies'][1]['preferred'] == 516407471


def test_native_61_quotes_cannot_hide_any_original_independent_quote_conflict(evidence, monkeypatch):
    data = deepcopy(evidence['BMEB4'])
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: deepcopy(data))
    day = date(2026, 9, 30)
    with pytest.raises(ValueError, match='round2_original_independent_quote_conflict'):
        provider.load_evidence(ROOT, identity(), provider.CUTOFF, independent_quotes={day: data['daily'][day] + 10})


def test_observed_action_ratio_and_comparison_bytes_must_match(evidence, monkeypatch):
    monkeypatch.setattr(provider, 'inspect_evidence', lambda *args: deepcopy(evidence['BMEB4']))
    with pytest.raises(ValueError, match='round2_caller_action_list_disagrees'):
        provider.load_evidence(ROOT, identity(), provider.CUTOFF, observed_actions=[])
    with pytest.raises(ValueError, match='round2_caller_comparison_hash_changed'):
        provider.load_evidence(ROOT, identity(), provider.CUTOFF, comparison_content=b'{}')


def test_one_bad_month_or_unproven_date_cannot_be_discarded(evidence):
    data = provider.packet(ROOT)
    spec = data['targets']['BMEB4']
    prices, actions = provider._comparison(ROOT, spec)
    native = dict(evidence['BMEB4']['daily'])
    month = date(2026, 9, 30)
    native[month] += 10
    audit = provider._quote_audit(ROOT, spec, native, actions, prices)
    assert not audit['modes_agreeing_all61_dates_and_prices']
    assert any(r['month'] == month.isoformat() for r in audit['monthly_conflicts'])
    del native[month]
    audit = provider._quote_audit(ROOT, spec, native, actions, prices)
    assert not audit['modes_agreeing_all61_dates_and_prices']
    assert any(not r['date_matches'] for r in audit['monthly_inputs'])


def test_active_module_never_creates_exchange_JSON_or_owns_eqtl():
    assert 'EQTL3' not in provider.IDENTITIES
    source = Path(provider.__file__).read_text()
    assert 'urllib.request' not in source
    assert 'reconcile_native_prices(' not in source
    assert all(not r['EPS_or_financial_scope_modified'] for r in (
        provider.inspect_evidence(ROOT, identity('BMEB4')),))
