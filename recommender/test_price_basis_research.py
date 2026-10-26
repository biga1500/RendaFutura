from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from .collect_market_data import shift_month
from .price_basis_research import (
    CUTOFF, START, FCA, FCA_HASH, REVIEWS, _daily_diagnostic, _fca_identity,
    _path, _reference_view, reviewed_inputs, replay_candidate,
)
from .share_basis import b3_evidence


ROOT = Path(__file__).parent / 'data/market'


def identity(ticker):
    return {'ticker': ticker, **{k: REVIEWS[ticker][k] for k in ('cvm', 'isin', 'share_class')}}


def row(day, quote='10,00', share='ON'):
    return {'typeStock': share, 'corporateAction': 'DIVIDENDO', 'quotedPerShares': '1',
            'valueCash': '0,5', 'lastDatePriorEx': day.strftime('%d/%m/%Y'),
            'dateClosingPricePriorExDate': day.strftime('%d/%m/%Y'),
            'closingPricePriorExDate': quote}


def test_old_unknown_dates_are_not_conflated_and_cash_is_preserved():
    rows = [row(date(2011, 4, 29), '58,92'), row(date(2014, 8, 12), '45,47')]
    for r in rows:
        r['dateClosingPricePriorExDate'] = '31/12/9999'
    before = deepcopy(rows)
    direct_cash, direct_quotes = b3_evidence(rows, 'ON')
    assert not direct_quotes
    assert direct_cash == {date(2011, 4, 29): .5, date(2014, 8, 12): .5}
    typed, audit = _reference_view(rows, 'ON', START, CUTOFF)
    cash, quotes = b3_evidence(typed, 'ON')
    assert rows == before and not quotes
    assert cash == {date(2011, 4, 29): .5, date(2014, 8, 12): .5}
    assert [r['original_row'] for r in audit] == before
    assert all(r['reference_date'] is None for r in audit)
    assert all(r['closingPricePriorExDate'] is None for r in typed)


def test_real_same_day_conflicts_are_never_selected_or_suppressed():
    rows = [row(date(2025, 5, 5)), row(date(2025, 5, 5), '12,00')]
    typed, audit = _reference_view(rows, 'ON', START, CUTOFF)
    assert not audit
    with pytest.raises(ValueError, match='conflicting_B3_reference_quotes'):
        b3_evidence(typed, 'ON')


def test_unknown_date_for_in_window_right_stays_blocked():
    r = row(date(2024, 1, 1))
    r['dateClosingPricePriorExDate'] = '31/12/9999'
    with pytest.raises(ValueError, match='unknown_reference_date_for_required_cash_right'):
        _reference_view([r], 'ON', START, CUTOFF)


def test_wrong_class_does_not_supply_an_anchor():
    rows = [row(date(2024, 1, 1), share='PN')]
    typed, audit = _reference_view(rows, 'ON', START, CUTOFF)
    assert not audit and not b3_evidence(typed, 'ON')[1]


def daily_fixture(*, wrong_split=False, include_pre_split=True, unmatched=False, bad_close=False,
                  split_adjusted=False):
    days = [shift_month(CUTOFF, -i) for i in reversed(range(61))]
    split_day = date(2024, 1, 15)
    closes = [20. if d < split_day and not split_adjusted else 10. for d in days]
    rows = [row(d, '20,00' if d < split_day else '10,00') for d in days
            if include_pre_split or d > split_day]
    if unmatched:
        rows.append(row(date(2025, 4, 15)))
    r = {'meta': {'symbol': 'TEST3.SA', 'currency': 'BRL', 'dataGranularity': '1d'},
         'timestamp': [int(datetime(d.year, d.month, d.day, 16, tzinfo=timezone.utc).timestamp()) for d in days],
         'events': {'splits': {'event': {
             'date': int(datetime(2024, 1, 15, 16, tzinfo=timezone.utc).timestamp()),
             'numerator': 3 if wrong_split else 2, 'denominator': 1}}},
         'indicators': {'quote': [{'close': closes}], 'adjclose': [{'adjclose': closes[:]}]}}
    if bad_close:
        r['indicators']['quote'][0]['close'][4] = 16.
    return json.dumps({'chart': {'result': [r], 'error': None}}).encode(), rows


def test_original_daily_closes_reconcile_without_imputed_months():
    raw, rows = daily_fixture()
    result = _daily_diagnostic(raw, 'TEST3', rows, 'ON')
    assert result['price_history_reproduced']
    assert result['mode'] == 'raw'
    assert len(result['required_monthly_prices']) == 61
    assert all(r['close_in_cutoff_units'] == 10 for r in result['required_monthly_prices'])


@pytest.mark.parametrize('option,reason', [
    ('wrong_split', 'historical_quote_or_share_scale_disagrees_with_B3'),
    ('unmatched', 'historical_quote_or_share_scale_disagrees_with_B3'),
    ('bad_close', 'historical_quote_or_share_scale_disagrees_with_B3'),
])
def test_wrong_split_unmatched_anchor_and_correct_adjusted_close_cannot_pass(option, reason):
    options = {option: True}
    if option == 'wrong_split':
        options['split_adjusted'] = True
    raw, rows = daily_fixture(**options)
    result = _daily_diagnostic(raw, 'TEST3', rows, 'ON')
    assert not result['price_history_reproduced'] and result['remaining_reason'] == reason


def test_post_split_matches_do_not_prove_an_unmatched_earlier_regime():
    raw, rows = daily_fixture(include_pre_split=False)
    result = _daily_diagnostic(raw, 'TEST3', rows, 'ON')
    assert result['valid_independent_quote_dates'] > 4
    assert result['missing_independently_observed_share_factors'] == [2.]
    assert result['remaining_reason'] == 'unverified_share_price_regime_requires_raw_history'


@pytest.mark.parametrize('field,value', [('isin', 'BRISAEACNPR9'), ('cvm', 15636),
                                        ('share_class', 'PN'), ('ticker', 'ISAE4')])
def test_wrong_class_or_related_instrument_is_rejected_before_cache_read(tmp_path, field, value):
    with pytest.raises(ValueError, match='instrument_identity_mismatch|outside_reviewed'):
        reviewed_inputs(tmp_path, identity('ISAE3') | {field: value})


def test_cgas_financial_class_is_not_silently_changed_to_source_label(tmp_path):
    with pytest.raises(ValueError, match='instrument_identity_mismatch'):
        reviewed_inputs(tmp_path, identity('CGAS5') | {'share_class': 'PNA'})


def test_parent_recovery_view_is_idempotent_but_rejects_unreviewed_rows():
    from .inventory_candidates import _Cache, _reviewed_cash_view
    from .market_inventory import load_inventory
    proof = reviewed_inputs(ROOT, identity('UGPA3'))
    cache = _Cache(ROOT, load_inventory(ROOT))
    first = _reviewed_cash_view(identity('UGPA3'), cache, CUTOFF, proof['original_cash_rows'], {})
    second = _reviewed_cash_view(identity('UGPA3'), cache, CUTOFF, first, {})
    assert first == second == proof['cash_rows']
    changed = deepcopy(first)
    changed[0]['valueCash'] = '999,00'
    with pytest.raises(ValueError, match='original_rows_mismatch'):
        _reviewed_cash_view(identity('UGPA3'), cache, CUTOFF, changed, {})


def test_other_cutoff_is_not_assumed_to_share_reviewed_basis(tmp_path):
    with pytest.raises(ValueError, match='unreviewed_price_cutoff'):
        reviewed_inputs(tmp_path, identity('UGPA3'), '2026-08-31')


@pytest.mark.parametrize('name', ['../current.json', 'C:/Windows/a', 'raw/../../a', 'raw\\a'])
def test_proofs_cannot_escape_market_root(tmp_path, name):
    with pytest.raises(ValueError, match='unsafe_price_proof_path'):
        _path(tmp_path, name)


@pytest.mark.parametrize('ticker,matches', [('ISAE3', 16), ('UGPA3', 10), ('CGAS5', 16)])
def test_cached_review_recovers_original_required_daily_anchors(ticker, matches):
    proof = reviewed_inputs(ROOT, identity(ticker))
    assert len(proof['price_matches']) == matches
    assert all(shift_month(CUTOFF, -i) in proof['prices'] for i in range(61))
    assert proof['price_mode'] == 'split_adjusted'
    for source in proof['receipts']:
        assert hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
    for change in proof['corrections']:
        before = change['original_row']
        after = proof['cash_rows'][change['row_index']]
        for key in ('lastDatePriorEx', 'valueCash', 'quotedPerShares'):
            assert before[key] == after[key]


@pytest.mark.parametrize('tamper', ['close', 'split'])
def test_pinned_original_price_tampering_cannot_be_repaired_by_anchors(tmp_path, tamper):
    ticker = 'UGPA3'
    for name in REVIEWS[ticker]['files']:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    price_file = next(p for p in REVIEWS[ticker]['files'] if p.endswith('-prices.json'))
    raw = json.loads((tmp_path / price_file).read_bytes())
    result = raw['chart']['result'][0]
    if tamper == 'close':
        result['indicators']['quote'][0]['close'][0] = 1.
    else:
        next(iter(result['events']['splits'].values()))['numerator'] = 999.
    (tmp_path / price_file).write_bytes(json.dumps(raw).encode())
    with pytest.raises(ValueError, match='unreviewed_price_source_hash'):
        reviewed_inputs(tmp_path, identity(ticker))


def test_exact_FCA_join_rejects_wrong_CNPJ():
    raw = (ROOT / FCA).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FCA_HASH
    bad = REVIEWS['ISAE3'] | {'cnpj': REVIEWS['CGAS5']['cnpj']}
    with pytest.raises(ValueError, match='FCA_identity_mismatch'):
        _fca_identity(raw, 'ISAE3', bad, CUTOFF)


@pytest.mark.parametrize('ticker,ledger,matches', [('ISAE3', 18, 16), ('UGPA3', 10, 10)])
def test_recovery_replays_complete_existing_source_builder(ticker, ledger, matches):
    candidate, audit = replay_candidate(ROOT, identity(ticker))
    assert audit['source_candidate_recovered']
    assert len(candidate['ledger']) == ledger
    assert len(candidate['independent_cash_matches']) == matches
    assert len(candidate['independent_price_matches']) == matches
    assert candidate['checks']['extended_training_history']['status'] == 'not_used'
    assert candidate['training_since'] == date(2022, 9, 30)
    assert all(date.fromisoformat(r['payment_date']) <= CUTOFF for r in candidate['ledger'])


def test_cgas_has_price_recovery_but_remains_blocked_on_cash_calendar():
    proof = reviewed_inputs(ROOT, identity('CGAS5'))
    assert {e['isin'] for e in proof['class_proof']['native_CVM_events']} == {'BRCGASACNPA3'}
    assert proof['class_proof']['annual_financial_share_class_retained'] == 'PN'
    candidate, audit = replay_candidate(ROOT, identity('CGAS5'))
    assert candidate is None and not audit['source_candidate_recovered']
    assert audit['remaining_reason'] == 'B3_cash_event_missing_from_payment_table'
