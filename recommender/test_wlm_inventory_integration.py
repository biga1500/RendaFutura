"""Real WLM hook/source closure; no admission, snapshots, or policy overrides."""

from datetime import date
import hashlib
from pathlib import Path
import socket

import pytest

from . import inventory_candidates as parent
from . import wlm_native_calendar as wlm
from .market_inventory import load_inventory


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def inventory():
    original = load_inventory(ROOT)
    return {**original, 'stocks': [r for r in original['stocks'] if r['ticker'] in ('WLMM3','WLMM4')]}


@pytest.fixture(scope='module')
def trial(inventory):
    calls = {'price': {}, 'cash': {}, 'histories': {}}
    prices, payments = parent.reconcile_prices, parent.reconcile_payments
    cash_check, payment_reader = parent.check_cash_amounts, parent._payments
    active = {}
    def record_prices(*args, **kwargs):
        result = prices(*args, **kwargs)
        calls['price'][args[1]] = {'matches': result[1], 'regime': result[2]}
        return result
    def record_reader(ticker, *args, **kwargs):
        result = payment_reader(ticker, *args, **kwargs)
        active['ticker'] = ticker
        calls['histories'][ticker] = result[0]
        return result
    def record_cash_check(rows, comparable, cutoff, share_class):
        ticker = active['ticker']
        start = parent.shift_month(cutoff, -60)
        expected, _ = parent.b3_evidence(rows, share_class)
        groups = set(comparable)
        required = {d for d in expected if start <= d <= cutoff}
        calls['cash'][ticker] = {'missing_record_dates': sorted(d.isoformat() for d in required-groups),
            'missing_expected_gross': {d.isoformat():expected[d] for d in sorted(required-groups)},
            'history': calls['histories'][ticker], 'expected': expected, 'comparable': dict(comparable),
            'share_class': share_class, 'blocking_gate': 'unmodified_check_cash_amounts'}
        return cash_check(rows, comparable, cutoff, share_class)
    def forbidden(*a, **k):
        raise AssertionError('no_network_in_inventory_source_verification')
    pointer = (ROOT/'current.json').read_bytes()
    with pytest.MonkeyPatch.context() as patch:
        from . import wlm_cash2023_calendar as followon
        # This fixture deliberately exercises frozen v2. Production v3 cash
        # augmentation is covered without overrides in its own test module.
        patch.setattr(followon,'repair_full_inspected_calendar',wlm.repair_full_inspected_calendar)
        patch.setattr(followon,'canonical_independent_cash_rows',wlm.canonical_independent_cash_rows)
        patch.setattr(socket,'socket',forbidden)
        patch.setattr(parent,'reconcile_prices',record_prices)
        patch.setattr(parent,'_payments',record_reader)
        patch.setattr(parent,'check_cash_amounts',record_cash_check)
        candidates,pending,sources=parent.build_candidates(ROOT,inventory,wlm.CUTOFF)
    assert (ROOT/'current.json').read_bytes()==pointer
    assert parent.reconcile_prices is prices and parent.reconcile_payments is payments
    return {'candidates':candidates,'pending':pending,'sources':sources,'calls':calls}


def test_both_exact_codes_remain_blocked_by_unmodified_cash_gate_with_audit_preserved(trial):
    assert trial['candidates']==[]
    assert {r['ticker'] for r in trial['pending']}=={'WLMM3','WLMM4'}
    for row in trial['pending']:
        assert row['reason']=='B3_cash_event_missing_from_payment_table'
        audit=row['checks']['wlm_native_calendar_repair']
        assert audit['source_2022_group_corrected_from_primary']
        assert audit['original_issues'] and audit['original_vendor_JCP_group']
        assert audit['normal_cash_price_share_and_annual_gates_unchanged']
        assert not audit['parent_admission_performed'] and not audit['settlement_verified']
        history=trial['calls']['cash'][row['ticker']]['history']
        assert history.wlm_full_calendar_repair==audit
        assert history.source_issues and history.installments
        assert len(trial['calls']['price'][row['ticker']]['matches'])>=4
    preferred=next(r for r in trial['pending'] if r['ticker']=='WLMM4')
    assert preferred['checks']['wlm_source_class_review']['class_crosswalk']['legal_class_kept']=='PNA'


def test_every_actual_proof_receipt_is_registered_including_multi_URL_and_failed_downloads(trial):
    sources={(s['file'],s['sha256'],s.get('url')):s for s in trial['sources']}
    assert len(sources)==len(trial['sources'])
    for source in sources.values():
        body=(ROOT/source['file']).read_bytes()
        assert len(body)==source['bytes'] and hashlib.sha256(body).hexdigest()==source['sha256']
    for row in trial['pending']:
        for source in row['checks']['wlm_native_calendar_repair']['evidence_sources']:
            actual=sources[source['file'],source['sha256'],source.get('url')]
            assert actual['retrieved_at']==source['retrieved_at']
            if 'published_at' in source: assert actual['published_at']==source['published_at']
    pdf=next(s for s in sources.values() if s['sha256']==wlm.NATIVE_FRE_PDF_SHA)
    assert pdf['retrieved_at'] is None and pdf['derived_from']['sha256']==wlm.NATIVE_FRE_SHA
    assert any(s.get('url','').startswith('https://www.wlm.com.br/') for s in sources.values()
               if isinstance(s.get('url'),str))


@pytest.mark.parametrize('ticker',['WLMM3','WLMM4'])
def test_payments_requires_provided_verified_identity_no_suffix_or_component_guess(inventory,ticker):
    cache=parent._Cache(ROOT,inventory)
    with pytest.raises(parent._Pending,match='wlm_exact_verified_identity_required'):
        parent._payments(ticker,cache,wlm.CUTOFF)


@pytest.mark.parametrize('key,value',[('isin','BRWLMMACNPR3'),('share_class','PN'),('cvm',9999)])
def test_payments_rejects_inconsistent_verified_security_without_repair(inventory,key,value):
    cache=parent._Cache(ROOT,inventory)
    stock={**wlm.STOCK,key:value}
    with pytest.raises(ValueError,match='exact_WLMM3_or_WLMM4_identity_required'):
        parent._payments('WLMM3',cache,wlm.CUTOFF,identity=stock)


@pytest.mark.parametrize('ticker',['WLMM3','WLMM4'])
def test_corrupt_native_proof_cannot_fall_back_to_unrepaired_vendor_or_alternate_route(inventory,monkeypatch,ticker):
    original=wlm.EvidenceReader.read
    def corrupt(self,name,*args,**kwargs):
        if name==wlm.NATIVE_FRE_PDF:
            raise wlm.ProofError('original_content_hash_mismatch')
        return original(self,name,*args,**kwargs)
    monkeypatch.setattr(wlm.EvidenceReader,'read',corrupt)
    scope={**inventory,'stocks':[r for r in inventory['stocks'] if r['ticker']==ticker]}
    candidates,pending,_=parent.build_candidates(ROOT,scope,wlm.CUTOFF)
    assert candidates==[] and pending[0]['reason']=='original_content_hash_mismatch'
    assert 'alternate_route_reason' not in pending[0]['checks']


def test_extended_payments_uses_same_verified_proof_without_relaxing_coverage(inventory,trial):
    row=next(r for r in inventory['stocks'] if r['ticker']=='WLMM3')
    cache=parent._Cache(ROOT,inventory)
    history,_=parent._payments('WLMM3',cache,wlm.CUTOFF,coverage_start=date(2021,9,30),
                               identity={**row,'isin':wlm.STOCK['isin']})
    assert history.wlm_full_calendar_repair['source_2022_group_corrected_from_primary']
    assert history.wlm_full_calendar_repair['evidence_sources']
    assert all(r['payment_date']>='2021-09-30' for r in history.installments)
    assert history.coverage_start<=date(2021,9,30)
    assert not history  # Calendar has not been qualified as paid cash by a failed reconciliation.


def test_unsupported_cutoff_never_uses_frozen_proof(inventory,monkeypatch):
    def forbidden(*a,**k): raise AssertionError('frozen_WLM_proof_must_not_run_for_another_cutoff')
    monkeypatch.setattr(wlm,'repair_full_inspected_calendar',forbidden)
    cache=parent._Cache(ROOT,inventory)
    with pytest.raises(parent._Pending,match='in_window_payment_date_or_amount_unresolved'):
        parent._payments('WLMM3',cache,date(2026,8,31),identity=wlm.STOCK)
