from datetime import date
import hashlib
import socket

import pytest

from . import issuer_independent_evidence as evidence
from .inventory_candidates import build_candidates
from .market_inventory import load_inventory
from .published_issuer_quotes import ROOT
from .share_basis import reconcile_payments, reconcile_prices
from .test_share_basis import ACTIONS, CUTOFF, payment_fixture, price_fixture


@pytest.fixture(scope='module')
def inventory():
    original = load_inventory(ROOT)
    return {**original, 'stocks': [row for row in original['stocks'] if row['ticker'] in ('ITSA3', 'ITSA4')]}


@pytest.mark.parametrize('ticker', ['ITSA3', 'ITSA4'])
def test_typed_primary_cash_and_public_quotes_have_full_window_without_fake_exchange_rows(inventory, ticker):
    identity = next(row for row in inventory['stocks'] if row['ticker'] == ticker)
    data = evidence.load_evidence(ROOT, identity, CUTOFF)
    assert data['checks']['cash_rows'] == 105
    assert data['checks']['window_cash_rows'] == 49
    assert data['checks']['future_cash_rows'] == 2
    assert data['checks']['pre_window_cash_rows'] == 54
    assert len(data['raw_quotes']) == 1268 and min(data['expected_cash']) < date(2021, 9, 30)
    assert not data['checks']['synthetic_exchange_rows_created']
    assert not data['checks']['net_to_gross_inference'] and not data['checks']['record_dates_derived_from_ex_dates']
    assert not data['checks']['settlement_verified'] and not data['checks']['point_in_time_archive_verified']
    assert len(data['sources']) == 4
    assert all(hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
               for source in data['sources'])


def test_real_parent_consumes_new_evidence_but_rejects_unexplained_price_regimes(inventory, monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    paths = [ROOT / 'current.json', ROOT / 'inventory/current.json']
    before = {path: path.read_bytes() for path in paths}
    candidates, pending, sources = build_candidates(ROOT, inventory, CUTOFF)
    assert candidates == [] and {row['ticker'] for row in pending} == {'ITSA3', 'ITSA4'}
    assert {row['reason'] for row in pending} == {'historical_quote_or_share_scale_disagrees_with_independent_source'}
    assert all(row['checks']['independent_issuer_evidence']['cash_rows'] == 105 for row in pending)
    assert any(source.get('url', '').startswith('https://api.mziq.com/mzstockinfo/') for source in sources
               if source.get('url'))
    assert not any('b3-cash' in source['file'] for source in sources)
    assert all(path.read_bytes() == before[path] for path in paths)


def test_wrong_exact_share_class_or_user_selection_is_not_repaired(inventory, monkeypatch):
    identity = inventory['stocks'][0]
    with pytest.raises(ValueError, match='issuer_independent_exact_security_mismatch'):
        evidence.load_evidence(ROOT, {**identity, 'isin': 'BRFAKEACNOR0'}, CUTOFF)
    from . import dividend_scope
    monkeypatch.setattr(dividend_scope, 'allowed_tickers', lambda folder: frozenset({'PETR4'}))
    with pytest.raises(ValueError, match='issuer_independent_outside_selected_scope'):
        evidence.load_evidence(ROOT, identity, CUTOFF)


@pytest.mark.parametrize('mode', ['raw', 'split_adjusted'])
def test_provider_independent_price_math_is_identical_to_legacy_gate(mode):
    content, quotes = price_fixture(mode)
    baseline = reconcile_prices(content, 'TEST3', ACTIONS, quotes, CUTOFF)
    assert reconcile_prices(content, 'TEST3', ACTIONS, quotes, CUTOFF,
                            evidence_provider='issuer_calendar_and_Enfoque') == baseline


@pytest.mark.parametrize('adjusted', [False, True])
def test_provider_independent_cash_uses_same_math_with_truthful_provenance(adjusted):
    original, expected = payment_fixture(adjusted)
    baseline, old_ledger, old_adjustments = reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF)
    current, ledger, adjustments = reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF,
                                                     evidence_provider='issuer_calendar_and_Enfoque')
    assert current == baseline
    assert [row['gross_per_unit'] for row in ledger] == [row['gross_per_unit'] for row in old_ledger]
    assert [row['declared_independent_per_record_share'] for row in adjustments] == [
        row['declared_B3_per_record_share'] for row in old_adjustments]
    assert all('declared_B3_per_record_share' not in row for row in adjustments)
    assert all(row['basis'] == 'reported_payment_date_independent_amount_and_share_factor_reconciled' for row in ledger)


def test_provider_independent_cash_still_blocks_wrong_amount_and_missing_event():
    original, expected = payment_fixture(False)
    original.installments[-1]['gross_per_unit'] = 5000
    with pytest.raises(ValueError, match='cash_amount_or_share_scale_disagrees_with_independent_source'):
        reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF, evidence_provider='issuer')
    original.installments.pop()
    with pytest.raises(ValueError, match='independent_cash_event_missing_from_payment_table'):
        reconcile_payments('TEST3', original, expected, ACTIONS, CUTOFF, evidence_provider='issuer')
