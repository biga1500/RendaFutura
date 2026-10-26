"""Offline issuer cash/quote contracts; synthetic quotes never enter real proofs."""

import builtins
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest
from pypdf import PdfReader

from . import brbi_issuer_data as issuer


ROOT = Path(__file__).resolve().parent / 'data/market'
PINS = ('b884f03facc241ccebd46ffd5f14a6865f8fbb7cb1a0031f073f6063afbdb341',
        'a9d19373af5d60e2b95454168aba07081f082e8920efd4bb6f2db749692c6385',
        '13401ae16e0d63763303865b864272bf8d5602f8b6c2d112f0dab5c444ffcc8d')
QUOTE_FIXTURE = b'''{"success":true,"data":{"source":"Enfoque","link":"https://www.enfoque.com.br",
"errors":[],"indexes":[],"tickers":[{"ticker":"BRBI11","history":[{"date":"2021-09-30",
"var":0.1,"high":13,"low":12,"open":12.3,"closed":12.34567890123456789,
"middle":12.4,"vol":1234.5,"share":100,"trade":10}]}]}}'''


@pytest.fixture(scope='module', autouse=True)
def offline_readonly():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('BRBI issuer tests must remain offline')

        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def actual():
    return issuer.get_brbi_issuer_data(ROOT)


@pytest.fixture(scope='module')
def notices():
    result = []
    for digest in PINS:
        body = (ROOT / issuer.BASE / f'body-{digest}.bin').read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest
        result.append([p.extract_text() for p in PdfReader(io.BytesIO(body)).pages])
    return result


def test_real_literal_UNIT_amounts_record_ex_and_payment_dates_keep_printed_precision(actual):
    expected = [
        ('0.81', date(2022, 3, 23), date(2022, 3, 24), date(2022, 3, 30)),
        ('0.78', date(2024, 12, 4), date(2024, 12, 5), date(2024, 12, 27)),
        ('0.18', date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 28))]
    assert len(actual['cash_per_UNIT']) == 3
    assert [e['source_sha256'] for e in actual['cash_per_UNIT']] == list(PINS)
    for event, values in zip(actual['cash_per_UNIT'], expected):
        assert tuple(event[k] for k in ('gross_per_UNIT', 'record_date', 'ex_date', 'payment_date')) == values
        assert all(type(event[k]) is date for k in ('record_date', 'ex_date', 'payment_date', 'published_at'))
        assert event['ticker'] == 'BRBI11' and event['isin'] == 'BRBRBICDAM10' and event['cvm'] == 25860
        assert event['effective_composition'] == {'ON': 1, 'PN': 2}
        assert event['amount_basis'] == 'explicit_native_per_UNIT'
        assert event['arithmetic_conversion_applied'] is False and event['net_per_UNIT'] is None
        assert event['status'] == 'issuer_announced_payment' and event['bank_settlement_verified'] is False
    assert actual['cash_per_UNIT'][1]['cash_kind'] == 'extraordinary_dividend'


def test_real_timeout_and_partial_calendar_are_exposed_without_fabricating_quotes_or_eligibility(actual):
    assert actual['quotes_raw'] == []
    diagnostic, = [d for d in actual['diagnostics'] if d['kind'] == 'raw_quotes']
    assert diagnostic['status'] == 'unavailable' and diagnostic['error'] == 'TimeoutError'
    assert diagnostic['http_status'] is None and diagnostic['automatic_retry'] is False
    coverage, = [d for d in actual['diagnostics'] if d['kind'] == 'cash_coverage']
    assert coverage['status'] == 'partial'
    assert coverage['unsampled_dividend_notices_in_reviewed_IPE_years']
    assert coverage['additional_unreviewed_years'] == [2021, 2023, 2025]
    qualifications = actual['qualifications']
    assert qualifications['cash_calendar_complete'] is False
    assert qualifications['all_61_monthly_quote_anchors_verified'] is False
    assert qualifications['cash_is_announced_payment_not_settlement_confirmation'] is True
    for field in ('UNIT_amounts_derived_from_component_multiplication', 'corporate_action_adjustments_applied',
                  'share_action_ledger_verified', 'financial_features_validated',
                  'retirement_main_income_recommended', 'point_in_time_archive_verified'):
        assert qualifications[field] is False
    assert actual['B3_response_fabricated'] is False and actual['unit_candidates_modified'] is False
    assert actual['model_admitted'] is False and actual['admissions'] == []


def test_real_RI_page_and_JS_bind_exact_BRBI11_unadjusted_vendor_request(actual):
    proof = actual['widget_request_proof']
    assert proof['official_quote_page']['url'] == issuer.PAGE_URL
    assert proof['official_quote_page']['sha256'] == 'a1620979ab962534e5ae842175696cad43bea718632308cf7f66f653602f9f37'
    assert proof['parameters'] == {'from': '2021-09-01', 'to': '2026-09-30', 'tickers': 'BRBI11', 'adjusted': False}
    assert proof['no_guessed_path_or_authentication'] is True
    history, = [s for s in actual['sources'] if s['url'] == issuer.HISTORY_URL]
    assert issuer.WIDGET_ID in history['url']
    assert history['error'] == 'TimeoutError' and history['bytes'] == 0
    assert history['sha256'] == hashlib.sha256(b'').hexdigest()
    for source in actual['sources']:
        assert hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']


@pytest.mark.parametrize(('old', 'new'), [
    ('br advisory partners participacoes s.a.', 'other company s.a.'),
    ('10.739.356/0001-03', '00.000.000/0001-00'), ('brbi11', 'other11'),
    ('1 (uma) acao ordinaria', '2 (uma) acao ordinaria'),
    ('2 (duas) acoes preferenciais', '3 (duas) acoes preferenciais'),
    ('r$0,81', 'r$0,00'), ('por unit.', 'por acao.'),
    ('23 de marco de 2022', '31 de marco de 2022'),
    ('24 de marco de 2022', '22 de marco de 2022'),
    ('30 de marco de 2022', '30 de marco de 2027'),
    ('sera realizado em uma unica parcela no dia', 'data nao informada'),
    ('sao paulo, 18 de marco de 2022', 'sao paulo, 19 de marco de 2022')])
def test_cash_identity_composition_per_UNIT_basis_and_explicit_dates_fail_closed(notices, old, new):
    pages = [issuer.identity._text(page) for page in notices[0]]
    assert any(old in page for page in pages)
    pages = [page.replace(old, new) for page in pages]
    with pytest.raises(ValueError):
        issuer.parse_cash_notice(pages, date(2022, 3, 18))


def test_per_share_amount_is_not_multiplied_when_literal_UNIT_amount_is_absent(notices):
    pages = [issuer.identity._text(page).replace('correspondente a r$0,81', 'valor unitario nao informado')
             for page in notices[0]]
    assert 'r$0,27' in pages[0]
    with pytest.raises(ValueError, match='^brbi_issuer_literal_UNIT_cash_missing_or_ambiguous$'):
        issuer.parse_cash_notice(pages, date(2022, 3, 18))


def test_all_printed_UNIT_digits_are_preserved_without_quantizing_or_matching_component_cash(notices):
    pages = [issuer.identity._text(page).replace('r$0,81', 'r$0,810000123456') for page in notices[0]]
    event = issuer.parse_cash_notice(pages, date(2022, 3, 18))
    assert event['gross_per_UNIT'] == '0.810000123456'
    assert event['printed_per_UNIT'] == '0,810000123456'
    assert event['arithmetic_conversion_applied'] is False


@pytest.mark.parametrize('published', [date(2026, 10, 1), '2022-03-18', None])
def test_unknown_untyped_or_future_publication_is_not_replaced_by_payment_or_file_clock(notices, published):
    with pytest.raises(ValueError, match='^brbi_issuer_cash_publication_invalid$'):
        issuer.parse_cash_notice(notices[0], published)


def test_AGO_date_notice_is_rejected_as_cash_evidence():
    relative = issuer.BASE + '/body-73e75a5c9d8675e42eb8e2f45ebd19572e055029ef2d4f01bc2474e73a59d8a0.bin'
    pages = [p.extract_text() for p in PdfReader(io.BytesIO((ROOT / relative).read_bytes())).pages]
    with pytest.raises(ValueError):
        issuer.parse_cash_notice(pages, date(2022, 1, 11))


def test_cancelled_notice_is_not_a_paid_event(notices):
    pages = deepcopy(notices[0])
    pages[-1] += '\nPagamento suspenso'
    with pytest.raises(ValueError, match='^brbi_issuer_cash_cancelled$'):
        issuer.parse_cash_notice(pages, date(2022, 3, 18))


def test_synthetic_vendor_payload_preserves_raw_close_precision_and_date_type():
    quotes = issuer.parse_quotes(QUOTE_FIXTURE, hashlib.sha256(QUOTE_FIXTURE).hexdigest())
    assert quotes[0]['date'] == date(2021, 9, 30) and type(quotes[0]['date']) is date
    assert quotes[0]['closed'] == '12.34567890123456789'
    assert quotes[0]['open'] == '12.3'


@pytest.mark.parametrize('fault', ['ticker', 'vendor', 'date', 'duplicate_date', 'OHLC', 'numeric_string', 'zero_close'])
def test_synthetic_quote_wrong_security_provider_dates_or_values_cannot_enter_raw_quotes(fault):
    payload = json.loads(QUOTE_FIXTURE)
    data = payload['data']
    row = data['tickers'][0]['history'][0]
    if fault == 'ticker':
        data['tickers'][0]['ticker'] = 'LOGG3'
    elif fault == 'vendor':
        data['source'] = 'Yahoo'
    elif fault == 'date':
        row['date'] = '2026-10-01'
    elif fault == 'duplicate_date':
        data['tickers'][0]['history'].append(deepcopy(row))
    elif fault == 'OHLC':
        row['high'] = 10
    elif fault == 'numeric_string':
        row['closed'] = '12.34'
    else:
        row['closed'] = 0
    body = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        issuer.parse_quotes(body, hashlib.sha256(body).hexdigest())


def test_duplicate_quote_JSON_keys_are_rejected():
    body = QUOTE_FIXTURE.replace(b'"success":true', b'"success":false,"success":true')
    with pytest.raises(ValueError, match='^brbi_issuer_duplicate_JSON_key$'):
        issuer.parse_quotes(body, hashlib.sha256(body).hexdigest())


@pytest.mark.parametrize('relative', [
    f'{issuer.BASE}/body-{PINS[0]}.bin', f'{issuer.BASE}/body-{issuer.PAGE_SHA}.bin',
    f'{issuer.BASE}/body-{issuer.BUNDLE_SHA}.bin'])
def test_changed_native_notice_or_published_widget_bytes_fail_original_hash(monkeypatch, relative):
    original = Path.read_bytes

    def changed(path):
        body = original(path)
        return body + b'changed' if path == ROOT / relative else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='^brbi_evidence_changed$'):
        issuer.get_brbi_issuer_data(ROOT)


@pytest.mark.parametrize('cutoff', ['2026-09-30', date(2026, 8, 31)])
def test_unreviewed_cutoff_fails_before_identity_or_source_reads(monkeypatch, cutoff):
    monkeypatch.setattr(issuer.identity, 'audit', lambda *args: pytest.fail('wrong cutoff read sources'))
    with pytest.raises(ValueError, match='^brbi_issuer_unreviewed_cutoff$'):
        issuer.get_brbi_issuer_data(ROOT, cutoff)


def test_collection_uses_brbi_scope_exact_published_widget_and_only_one_history_call(monkeypatch):
    calls = []

    class FakeCollector:
        def __init__(self, root, scope):
            assert root == ROOT and scope == 'brbi'

        def fetch(self, url, discovery):
            calls.append(url)

    monkeypatch.setattr(issuer, 'PublicSourceCollector', FakeCollector)
    issuer.collect(ROOT)
    assert calls == [issuer.PAGE_URL, issuer.BUNDLE_URL, issuer.HISTORY_URL,
                     *(issuer.notice_url(n) for n in issuer.NOTICES)]
    assert calls.count(issuer.HISTORY_URL) == 1
    assert all('b3.com.br' not in url and 'yahoo' not in url for url in calls)


def test_report_is_content_addressed_and_date_serialization_does_not_mutate_typed_result(tmp_path, actual):
    before = deepcopy(actual)
    path = issuer.write_report(tmp_path, actual)
    assert issuer.write_report(tmp_path, actual) == path
    assert path.name == 'issuer-data-' + hashlib.sha256(path.read_bytes()).hexdigest() + '.json'
    result = json.loads(path.read_bytes())
    assert result['cash_per_UNIT'][0]['record_date'] == '2022-03-23'
    assert result['quotes_raw'] == [] and actual == before
    assert list((tmp_path / issuer.BASE).iterdir()) == [path]
