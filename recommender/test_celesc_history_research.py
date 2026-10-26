from copy import deepcopy
import hashlib
import json
import socket

import pytest

from . import celesc_history_research as research


@pytest.fixture(scope='module')
def report():
    return research.audit()


def test_real_issuer_price_series_covers_61_anchors_and_matches_1268_daily_quotes(report):
    prices = report['price_concordance']
    assert prices['source_rows'] == prices['compared_daily_quotes'] == 1268
    assert prices['consecutive_monthly_anchors'] == 61
    assert prices['first_date'] == '2021-09-01' and prices['last_date'] == '2026-09-30'
    assert prices['conflicts'] == [] and prices['reported_adjusted_request'] is False
    assert prices['absence_of_all_corporate_actions_proven'] is False


def test_real_primary_workbook_keeps_date_conflicts_without_repairs_or_tax_inference(report):
    cash = report['cash_workbook']
    assert cash['source_updated_date'] == '2026-09-25'
    assert cash['window_rows'] == 41
    assert {issue['row'] for issue in cash['issues'] if issue['in_cash_window']} == {24, 26}
    rows = {row['row']: row for row in cash['events']}
    assert rows[24]['reported_ex_date'] == '2026-09-25'
    assert rows[26]['reported_ex_date'] == '2025-05-26'
    assert rows[26]['approval_date'] == '2025-06-18'
    assert all(row['record_date'] is None and row['tax_basis'] is None
               and row['settlement_confirmed'] is False for row in rows.values())
    assert cash['future_rows'] > 0
    assert cash['record_date_derived_from_ex_date'] is False
    assert cash['gross_inferred_from_amount'] is False
    assert report['model_admissions'] == [] and not report['missing_data_imputed']


@pytest.mark.parametrize('value', ['2026', '25/09/2026', '2026-09-25', '2026-09-25 12:00:00', True])
def test_date_roles_are_not_guessed_from_ambiguous_or_partial_values(value):
    with pytest.raises(ValueError, match='unrecognized_workbook_date'):
        research._day(value)


def originals():
    root = research.ROOT
    price = (root / research.CACHE / ('original-' + research.HISTORY_SHA + '.bin')).read_bytes()
    secondary = root / 'raw/expanded/CLSC4-prices.json'
    if not secondary.exists():
        secondary = root / 'raw/inventory/daily-prices/CLSC4.json'
    return json.loads(price), secondary.read_bytes()


@pytest.mark.parametrize('change', ['ticker', 'duplicate_day', 'missing_month', 'nan', 'future'])
def test_wrong_security_incomplete_history_and_invalid_prices_fail(change):
    payload, secondary = originals()
    payload = deepcopy(payload)
    rows = payload['data']['tickers'][0]['history']
    if change == 'ticker':
        payload['data']['tickers'][0]['ticker'] = 'CLSC3'
    elif change == 'duplicate_day':
        rows.append(deepcopy(rows[0]))
    elif change == 'missing_month':
        payload['data']['tickers'][0]['history'] = [row for row in rows if not row['date'].startswith('2021-09')]
    elif change == 'nan':
        rows[0]['closed'] = float('nan')
    else:
        rows[0]['date'] = '2026-10-01'
    with pytest.raises(ValueError):
        research.inspect_prices(json.dumps(payload).encode(), secondary)


def test_material_price_conflict_is_reported_not_rescaled_to_pass():
    payload, secondary = originals()
    payload['data']['tickers'][0]['history'][0]['closed'] *= 2
    result = research.inspect_prices(json.dumps(payload).encode(), secondary)
    assert len(result['conflicts']) == 1
    assert result['conflicts'][0]['issuer_feed'] != result['conflicts'][0]['secondary']


def test_audit_is_offline_source_hashed_and_never_changes_live_pointers(monkeypatch, report):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    paths = [research.ROOT / 'current.json', research.ROOT / 'inventory/current.json']
    before = {str(path): path.read_bytes() for path in paths}
    again = research.audit()
    assert again == report and again['network_calls_by_audit'] == 0
    for source in again['sources']:
        assert hashlib.sha256((research.ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
    assert all(path.read_bytes() == before[str(path)] for path in paths)
    assert all(row['published_at'] <= '2026-09-30' and row['cvm'] == 2461
               for row in again['candidate_primary_notices_for_date_conflicts'])
