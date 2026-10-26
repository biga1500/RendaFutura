"""Offline source replay and adversarial tests for isolated property research."""

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket

import pytest

from . import round2_property_cash as research
from .inventory_candidates import _Cache, inspect_payment_page
from .market_inventory import load_inventory


FOLDER = Path(__file__).parent / 'data/market'


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network forbidden in property evidence tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.fixture(scope='module')
def csed():
    return next(s for s in load_inventory(FOLDER)['stocks'] if s['ticker'] == 'CSED3')


@pytest.fixture(scope='module')
def sealed():
    stock = next(s for s in load_inventory(FOLDER)['stocks'] if s['ticker'] == 'CSED3')
    return research.packet(FOLDER, stock, research.CUTOFF)[1]


@pytest.fixture(scope='module')
def native_quotes(sealed):
    receipt = research.attachment(sealed, lambda r: '/candles/interday?' in r['url'])
    body = research.read(FOLDER.resolve(), receipt['file'], receipt['sha256'], receipt['bytes'])
    return json.loads(body), receipt


@pytest.fixture(scope='module')
def cash_repair(csed):
    cache = _Cache(FOLDER, load_inventory(FOLDER))
    return research.load_csed_repair(FOLDER, csed, research.CUTOFF, cache)


def cash_page(*, ticker='CSED3', record='30/04/2021', amount='0,1882',
              label='DIVIDENDO', payment='28/05/2021', quantity='1'):
    return (f'<html><p><strong>{ticker}</strong></p><p>Ultimo dia com</p>'
        '<table><tr><th>Data</th><th>Valor</th><th>Tipo</th>'
        '<th>Data de Pagamento</th><th>Por quantas acoes</th></tr>'
        f'<tr><td>{record}</td><td>{amount}</td><td>{label}</td>'
        f'<td>{payment}</td><td>{quantity}</td></tr></table></html>').encode()


def test_literal_cash_dates_quantity_and_precision():
    event, = research.parse_calendar(cash_page(amount='1,8820', quantity='10'), 'CSED3')
    assert event['record_date'] == '2021-04-30'
    assert event['payment_date'] == '2021-05-28'
    assert event['quoted_quantity_decimal'] == '10'
    assert Decimal(event['gross_per_share_decimal']) == Decimal('0.1882')
    assert event['original_reported_fields'] == ['30/04/2021', '1,8820', 'DIVIDENDO', '28/05/2021', '10']
    assert event['published_at'] is None and not event['settlement_verified']


@pytest.mark.parametrize('change', [
    {'ticker': 'CURY3'}, {'record': '31/02/2021'}, {'payment': '-'},
    {'payment': '29/04/2021'}, {'amount': '0'}, {'amount': '-0,1'},
    {'amount': 'NaN'}, {'amount': 'Infinity'}, {'quantity': '0'},
    {'quantity': '-1'}, {'quantity': 'NaN'}, {'label': 'REST CAP DIN'},
    {'label': 'BONIFICACAO'}, {'label': 'RENDIMENTO'},
])
def test_cash_adversarial_rows_fail_closed(change):
    with pytest.raises(ValueError):
        research.parse_calendar(cash_page(**change), 'CSED3')


def test_JCP_is_not_implicitly_gross_or_net():
    event, = research.parse_calendar(cash_page(label='JRS CAP PROPRIO'), 'CSED3')
    assert event['label'] == 'JCP'
    assert event['gross_per_share_decimal'] is None
    assert event['amount_tax_basis'] == 'JCP_gross_or_net_unresolved'


def test_duplicate_and_missing_date_role_cannot_be_hidden():
    body = cash_page()
    row = body[body.index(b'<tr><td>'):body.index(b'</table>')]
    with pytest.raises(ValueError, match='duplicate'):
        research.parse_calendar(body.replace(b'</table>', row + b'</table>'), 'CSED3')
    with pytest.raises(ValueError, match='com_date_role'):
        research.parse_calendar(body.replace(b'Ultimo dia com', b'Pagamento'), 'CSED3')


@pytest.mark.parametrize('field,value', [
    ('ticker', 'CURY3'), ('cvm', 25100), ('share_class', 'PN'),
    ('isin', 'BRLAVVACNOR4'), ('sector_code', 2),
])
def test_exact_identity_sector_and_cutoff(csed, field, value):
    with pytest.raises(ValueError, match='exact_security'):
        research.packet(FOLDER, {**csed, field: value}, research.CUTOFF)
    with pytest.raises(ValueError, match='cutoff'):
        research.packet(FOLDER, csed, date(2026, 8, 31))


def test_real_cash_sources_do_not_silently_drop_awkward_rows(sealed):
    expected = {'CURY3': 19, 'MDNE3': 10, 'VITT3': 14, 'CSED3': 8}
    for ticker, count in expected.items():
        r = sealed['cash_receipts'][ticker]
        body = research.read(FOLDER.resolve(), r['file'], r['sha256'], r['bytes'])
        assert len(research.parse_calendar(body, ticker)) == count
    for ticker, reason in (('LAVV3', 'date_or_quantity'), ('MELK3', 'non_income')):
        r = sealed['cash_receipts'][ticker]
        body = research.read(FOLDER.resolve(), r['file'], r['sha256'], r['bytes'])
        with pytest.raises(ValueError, match=reason):
            research.parse_calendar(body, ticker)


def test_real_cash_getters_keep_tax_and_window_obstacles(csed):
    evidence = research.load_cash_evidence(FOLDER, csed, research.CUTOFF)
    assert len(evidence['events']) == 8
    assert evidence['checks']['first_entitlement_date'] == '2021-04-30'
    assert not evidence['checks']['gross_reconstructed_from_net']
    assert not evidence['checks']['ultimate_upstream_independence_verified']
    with pytest.raises(ValueError, match='JCP_gross_or_net'):
        research.load_cash_evidence(FOLDER, research.identity('VITT3'), research.CUTOFF)
    with pytest.raises(ValueError, match='window_start'):
        research.load_evidence(FOLDER, research.identity('MDNE3'), research.CUTOFF)


def test_issuer_literal_template_and_native_closes(csed, sealed):
    result = research.load_quote_observations(FOLDER, csed, research.CUTOFF)
    assert result['daily_observations'] == 1268
    assert result['monthly_observations'] == 61
    assert not result['numeric_values_changed']
    assert result['share_adjustment_basis'] == 'not_declared_by_API_do_not_assume_raw'
    assert result['monthly_prices'][0]['date'] == '2021-09-30'
    assert result['monthly_prices'][0]['close'] == 5.6
    assert result['monthly_prices'][-1]['close'] == 5.21
    assert any(s['authority'] == 'derived_immutable_source_binding_not_primary' for s in result['sources'])
    assert any(s['authority'].endswith('share_basis_unverified') for s in result['sources'])


@pytest.mark.parametrize('fault', ['symbol', 'duplicate', 'missing_month', 'nonfinite', 'bool',
    'nonpositive', 'OHLC', 'date_role', 'post_cutoff'])
def test_native_quote_adversarial_inputs(native_quotes, fault):
    original, receipt = native_quotes
    rows = deepcopy(original)
    if fault == 'symbol': rows[0]['symbol'] = 'CURY3'
    elif fault == 'duplicate': rows.append(deepcopy(rows[0]))
    elif fault == 'missing_month': rows = [r for r in rows if not r['date'].startswith('2021-09')]
    elif fault == 'nonfinite': rows[0]['close_price'] = float('nan')
    elif fault == 'bool': rows[0]['close_price'] = True
    elif fault == 'nonpositive': rows[0]['close_price'] = 0
    elif fault == 'OHLC': rows[0]['high_price'] = 1
    elif fault == 'date_role': rows[0]['candle_time'] = '2021-09-02T00:00:00-03:00'
    elif fault == 'post_cutoff': rows[-1]['date'] = '2026-10-01T00:00:00'
    with pytest.raises(ValueError):
        research.parse_csed_history(json.dumps(rows).encode(), receipt)


def test_wrong_widget_template_not_replaced_by_borrowed_ticker(sealed):
    page = research.attachment(sealed, lambda r: r['url'] == research.CSED_PAGE)
    component = research.attachment(sealed, lambda r: r['url'] == research.CSED_COMPONENT)
    p = research.read(FOLDER.resolve(), page['file'], page['sha256'], page['bytes'])
    c = research.read(FOLDER.resolve(), component['file'], component['sha256'], component['bytes'])
    with pytest.raises(ValueError, match='literal_history'):
        research.csed_widget_request(p, c.replace(b'a("CSED3",_h(V),_h(Q))', b'a("CURY3",_h(V),_h(Q))'))
    with pytest.raises(ValueError, match='public_API_base'):
        research.csed_widget_request(p.replace(b'https://api.solutions-ir.com', b'https://api.example.com'), c)


def test_missing_or_changed_actual_receipt_cannot_supply_getter(csed, monkeypatch):
    original = research.bound_source
    def failed(root, record, authority):
        if authority.startswith('secondary_Fundamentus'):
            raise ValueError('actual_source_failure')
        return original(root, record, authority)
    monkeypatch.setattr(research, 'bound_source', failed)
    with pytest.raises(ValueError, match='actual_source_failure'):
        research.load_evidence(FOLDER, csed, research.CUTOFF)


def test_unknown_share_adjustment_is_not_fit_to_make_real_getter_pass(csed):
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees'):
        research.load_evidence(FOLDER, csed, research.CUTOFF)


def parsed_csed():
    path = FOLDER / 'raw/expanded/CSED3-payments.html'
    body = path.read_bytes()
    assert hashlib.sha256(body).hexdigest() == research.PAYMENT_SHA
    return inspect_payment_page(body, 'CSED3', research.CUTOFF, datetime(2026, 10, 25, tzinfo=timezone.utc))


def test_literal_final_notice_repairs_whole_group_without_changing_source(csed, cash_repair, monkeypatch):
    parsed = parsed_csed()
    before = deepcopy(parsed)
    cache = _Cache(FOLDER, load_inventory(FOLDER))
    monkeypatch.setattr(research, 'load_csed_repair', lambda *args: deepcopy(cash_repair))
    result = research.apply_exact_repairs(parsed, csed, research.CUTOFF, cache)
    assert parsed == before
    original, = [e for e in parsed['events'] if e['record_date'] == '2022-04-29']
    event, = [e for e in result['events'] if e['record_date'] == '2022-04-29']
    assert original['payment_date'] == '2022-05-28'
    assert event['payment_date'] == '2022-05-27'
    assert event['gross_per_share_decimal'] == '0.1309509891304993'
    assert event['isin'] == 'BRCSEDACNOR9' and event['share_class'] == 'ON'
    assert not event['settlement_verified'] and not event['model_admitted']
    audit = result['exact_cash_repairs'][research.REPAIR_ID]
    assert audit['source_contract']['initial_payment_by_deadline_not_exact_payment_date'] == '2022-05-28'
    assert not audit['source_bytes_modified'] and not audit['independent_comparator_changed']
    assert audit['groups'][0]['reported_original_fields'] == [original]
    assert not any(e['record_date'].startswith('2021') for e in result['events'])
    assert research.apply_exact_repairs(result, csed, research.CUTOFF, cache) == result


@pytest.mark.parametrize('fault', ['extra_group_member', 'changed_source_sha', 'changed_original_date'])
def test_repair_does_not_override_changed_whole_group(csed, cash_repair, monkeypatch, fault):
    parsed = parsed_csed()
    group = next(e for e in parsed['events'] if e['record_date'] == '2022-04-29')
    if fault == 'extra_group_member': parsed['events'].append(deepcopy(group))
    elif fault == 'changed_source_sha': parsed['report']['sha256'] = '0' * 64
    elif fault == 'changed_original_date': group['payment_date'] = '2022-05-27'
    monkeypatch.setattr(research, 'load_csed_repair', lambda *args: deepcopy(cash_repair))
    with pytest.raises(ValueError):
        research.apply_exact_repairs(parsed, csed, research.CUTOFF, _Cache(FOLDER, load_inventory(FOLDER)))


def test_exact_latest_index_guard_is_not_bypassed(csed, monkeypatch):
    def blocked(*args, **kwargs):
        raise ValueError('cash_repair_latest_native_revision_required_no_fallback')
    monkeypatch.setattr(research.proof, 'index_document', blocked)
    with pytest.raises(ValueError, match='latest_native_revision'):
        research.load_csed_repair(FOLDER, csed, research.CUTOFF, _Cache(FOLDER, load_inventory(FOLDER)))


def test_in_window_action_proof_is_not_invented(csed):
    with pytest.raises(ValueError, match='in_window_action'):
        research.validate_primary_actions(FOLDER, csed, research.CUTOFF,
            [{'date': '2025-01-01', 'ratio': 2}])
    assert research.load_evidence(FOLDER, {'ticker': 'ZZZZ3'}, research.CUTOFF) is None


def test_report_writer_cannot_escape_owned_work_scope(tmp_path):
    with pytest.raises(ValueError, match='report_scope'):
        research.save_report(tmp_path / 'property-not-owned.json', {})


@pytest.mark.parametrize('operation', ['acquire_cash', 'acquire_quotes', 'acquire_published_assets',
                                     'acquire_followon', 'acquire_csed_final'])
def test_frozen_acquisition_cannot_open_a_collector(operation, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('collector must not be constructed after sealing')
    monkeypatch.setattr(research, 'PublicSourceCollector', forbidden)
    with pytest.raises(ValueError, match='acquisition_frozen'):
        getattr(research, operation)(FOLDER)


def test_changed_actual_transport_metadata_is_not_accepted(sealed, tmp_path):
    record = sealed['cash_receipts']['CSED3']
    source = FOLDER / record['file']
    copy = tmp_path / record['file']
    copy.parent.mkdir(parents=True)
    copy.write_bytes(source.read_bytes())
    relative = copy.parent / ('response-' + hashlib.sha256(record['url'].encode()).hexdigest() + '.json')
    relative.write_text(json.dumps({**record, 'retrieved_at': '2026-01-01T00:00:00+00:00'}), encoding='utf-8')
    with pytest.raises(ValueError, match='transport_receipt_changed'):
        research.bound_source(tmp_path, record, 'test_actual_original')


def test_bound_original_and_transport_hash_closure(sealed):
    for record in sealed['attachments']:
        path = FOLDER / record['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']
        assert path.stat().st_size == record['bytes']
        if record['status'] == 200 and not record['error']:
            body, refs = research.bound_source(FOLDER.resolve(), record, 'test_actual_original')
            assert len(body) == record['bytes']
            assert refs[1]['url'] is None
            assert refs[1]['authority'] == 'actual_HTTP_transport_receipt_not_primary_content'
