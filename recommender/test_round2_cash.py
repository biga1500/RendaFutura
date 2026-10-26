"""Offline normal-gate replay and invariants on recovered primary originals."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import round2_cash as recovery
from .inventory_candidates import _Cache, inspect_payment_page
from .market_inventory import load_inventory


ROOT = recovery.ROOT


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('round2 adapter attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def inventory():
    return load_inventory(ROOT)


@pytest.fixture(scope='module')
def identities(inventory):
    return {t: next(r for r in inventory['stocks'] if r['ticker'] == t) for t in recovery.TARGETS}


@pytest.fixture(scope='module')
def evidence(identities):
    return {t: recovery.inspect_evidence(ROOT, identity) for t, identity in identities.items()}


@pytest.fixture(scope='module')
def ordinary_gates():
    result = recovery.replay(ROOT, adapter=True)
    body = json.dumps(result, ensure_ascii=True, indent=2, default=str, allow_nan=False).encode()
    output = ROOT.parents[3] / 'work/recovery_round2_20261025'
    recovery.save_report(output / ('cash-normal-gates-' + hashlib.sha256(body).hexdigest() + '.json'), result)
    return {r['ticker']: r for r in result['targets']}


@pytest.mark.parametrize('ticker,count', [('ALLD3', 12), ('FESA3', 26), ('FESA4', 26), ('JSLG3', 11), ('HBOR3', 8)])
def test_frozen_native_evidence_and_every_source_hash(evidence, ticker, count):
    data = evidence[ticker]
    assert len(data['calendar']['events']) == count
    assert data['identity']['isin'] == recovery.TARGETS[ticker][2]
    for source in data['sources']:
        assert hashlib.sha256(recovery.read(ROOT, source)).hexdigest() == source['sha256']
    assert data['checks']['synthetic_exchange_responses'] is False
    assert data['checks']['settlement_verified'] is False


@pytest.mark.parametrize('ticker', list(recovery.TARGETS))
def test_exact_identity_is_required(evidence, identities, ticker):
    altered = {**identities[ticker], 'isin': 'BRWRONGCNOR1'}
    with pytest.raises(ValueError, match='exact_CVM_class_ISIN_mismatch'):
        recovery.inspect_evidence(ROOT, altered)
    altered = {**identities[ticker], 'cvm': 1}
    with pytest.raises(ValueError, match='exact_CVM_class_ISIN_mismatch'):
        recovery.load_evidence(ROOT, altered, recovery.CUTOFF)


@pytest.mark.parametrize('ticker', ['ALLD3', 'JSLG3', 'HBOR3'])
def test_every_month_uses_its_independent_last_actual_trade(evidence, ticker):
    proof = evidence[ticker]['quote_audit']
    assert proof['all61_compared'] and proof['monthly_inputs_agree61']
    checks = proof['monthly_comparisons']
    assert len(checks) == len({r['month'] for r in checks}) == 61
    assert checks[0]['month'] == '2021-09-30'
    assert checks[-1]['month'] == '2026-09-30'
    assert all(r['date_matches'] for r in checks)
    assert all(r['raw_price_matches'] for r in checks) or all(r['split_adjusted_price_matches'] for r in checks)
    assert len(proof['raw_quotes']) == 61
    assert proof['price_values_changed'] is proof['factors_fitted'] is False


def test_native_repeated_jsl_quote_series_are_equal_not_cherry_picked(evidence):
    proof = evidence['JSLG3']['quote_audit']
    assert proof['byte_equal_repeated_native_series_count'] == 2
    source = proof['sources'][0]
    rows = json.loads(recovery.read(ROOT, source))['data']['tickers']
    assert len(rows) == 2 and rows[0] == rows[1]


@pytest.mark.parametrize('ticker,share,expected,net', [
    ('FESA3', 'ON', '0.20221922581', '0.17188634194'),
    ('FESA4', 'PN', '0.22244114839', '0.18907497613'),
])
def test_ferbasa_native_unequal_installments_gross_net_and_date_roles(evidence, ticker, share, expected, net):
    rows = [r for r in evidence[ticker]['calendar']['events'] if r['record_date'] == '2025-11-05']
    assert len(rows) == 2
    assert rows[0]['payment_date'] == '2025-12-05'
    assert rows[1]['payment_date'] == '2026-06-12'
    assert rows[0]['gross_per_share_decimal'] == expected
    assert rows[0]['net_per_share_decimal'] == net
    assert rows[0]['share_class'] == share
    assert all(r['ex_date'] == '2025-11-06' for r in rows)
    assert Decimal(rows[0]['gross_per_share_decimal']) != Decimal(rows[1]['gross_per_share_decimal'])
    assert all(r['gross_reconstructed_from_net'] is False for r in rows)


def test_ferbasa_latest_may_2022_dates_and_notice_amount_conflict_retained(evidence):
    events = evidence['FESA4']['calendar']['events']
    rows = [r for r in events if r['record_date'] == '2022-04-26']
    assert [(r['payment_date'], r['gross_per_share_decimal']) for r in rows] == [
        ('2022-05-04', '0.51918535414'), ('2022-05-12', '1.05309082574')]
    row, = [r for r in events if r['record_date'] == '2021-12-07']
    assert row['gross_per_share_decimal'] == '0.24713369082'
    assert row['gross_per_share_decimal'] != '0.24763'


def test_helbor_partial_2026_payment_keeps_unresolved_remainder(evidence, identities):
    data = evidence['HBOR3']
    rows = [r for r in data['calendar']['events'] if r['record_date'] == '2026-01-05']
    assert len(rows) == 2
    paid, = [r for r in rows if r['payment_date']]
    unknown, = [r for r in rows if r['payment_date'] is None]
    assert paid['payment_date'] == '2026-05-29'
    assert paid['gross_per_share_decimal'] == '0.01884039452'
    assert unknown['gross_per_share_decimal'] == '0.1130423671'
    assert unknown['payment_date_role'] == 'deadline_not_promoted_to_payment'
    assert sum(Decimal(r['gross_per_share_decimal']) for r in rows) == Decimal('0.13188276162')
    assert any(r['record_date'] is None for r in data['calendar']['events'])
    with pytest.raises(ValueError, match='helbor_2023_final_v2_native_body_unavailable'):
        recovery.load_evidence(ROOT, identities['HBOR3'], recovery.CUTOFF)


def test_old_helbor_final_version_is_never_used_as_fallback():
    with pytest.raises(ValueError, match='higher_final_revision_unavailable_no_fallback'):
        recovery._cached_form(ROOT, '618119', 1)


def test_allied_repair_is_exact_idempotent_and_retains_original(inventory, identities):
    cache = _Cache(ROOT, inventory)
    body, observed = cache.read('raw/expanded/ALLD3-payments.html', 'payments')
    parsed = inspect_payment_page(body, 'ALLD3', recovery.CUTOFF, observed)
    original = deepcopy(parsed)
    fixed = recovery.apply_exact_repairs(parsed, identities['ALLD3'], recovery.CUTOFF, cache)
    assert parsed == original
    event, = [e for e in fixed['events'] if e['record_date'] == '2023-12-19']
    assert event['gross_per_share_decimal'] == '0.965452029'
    assert event['gross_per_share'] == .965452029
    assert event['payment_date'] == '2024-03-25' and event['published_at'] == '2024-03-11'
    assert event['reported_original_fields'][0]['gross_per_share'] == .482726
    assert fixed == recovery.apply_exact_repairs(fixed, identities['ALLD3'], recovery.CUTOFF, cache)
    altered = deepcopy(original)
    next(e for e in altered['events'] if e['record_date'] == '2023-12-19')['gross_per_share'] = .5
    with pytest.raises(ValueError, match='original_group_changed'):
        recovery.apply_exact_repairs(altered, identities['ALLD3'], recovery.CUTOFF, cache)
    assert hashlib.sha256(body).hexdigest() == recovery.ALLD_PAYMENT_SHA


def test_changed_original_bytes_fail_closed(tmp_path, evidence):
    source = evidence['ALLD3']['calendar']['events'][0]['source']
    target = tmp_path / source['file']
    target.parent.mkdir(parents=True)
    target.write_bytes(recovery.read(ROOT, source) + b'changed')
    with pytest.raises(ValueError, match='original_hash_changed'):
        recovery.read(tmp_path, source)


def test_actual_ordinary_one_and_current_financial_features(ordinary_gates):
    assert {t for t, r in ordinary_gates.items() if r['source_gate_pass']} == {'ALLD3', 'JSLG3'}
    for ticker in ('ALLD3', 'JSLG3'):
        result = ordinary_gates[ticker]
        assert result['checks']['independent_price_matches'] == 61
        assert result['checks']['independent_cash_matches'] >= 4
        latest = max(result['native_statements'], key=lambda r: (r['fiscal_end'], int(r['version'])))
        assert latest['fiscal_end'] == '2025-12-31'
        assert latest['profit_per_share'] is None
        assert result['current_financial'] is None
        assert result['current_financial_pass'] is False
    for ticker in ('FESA3', 'FESA4', 'HBOR3'):
        assert ordinary_gates[ticker]['source_gate_pass'] is False
        assert ordinary_gates[ticker]['current_financial_pass'] is False


def test_unavailable_ferbasa_quotes_stay_blocked_without_changing_normal_gates(evidence, identities):
    for ticker in ('FESA3', 'FESA4'):
        assert evidence[ticker]['quote_audit']['raw_quotes'] == {}
        assert evidence[ticker]['calendar']['blockers'] == []
        with pytest.raises(ValueError, match='widget'):
            recovery.load_evidence(ROOT, identities[ticker], recovery.CUTOFF)
