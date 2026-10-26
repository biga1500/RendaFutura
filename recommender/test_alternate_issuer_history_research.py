"""Synthetic test fixtures are never research observations or model inputs."""

from copy import deepcopy
from datetime import datetime, timezone
import io
import urllib.error

import pytest

from . import alternate_issuer_history_research as research


def stock(ticker='ITSA4'):
    cvm, share_class, isin, _ = research.IDENTITIES[ticker]
    return {'ticker': ticker, 'cvm': cvm, 'share_class': share_class, 'isin': isin}


def original(root, prefix, body, extension='txt', url='https://provider.example/history'):
    name = research.save(root, 'fixture-' + prefix, body, extension)
    return {'file': name, 'sha256': research.sha(body), 'bytes': len(body),
            'url': url, 'content_type': 'text/plain'}


def quote_fixture(root, *, split=False, indexes=(0, 12, 36, 60), decimals='.'):
    identity = stock()
    months = sorted(research.shift_month(research.CUTOFF, -i) for i in range(61))
    actions = {}
    if split:
        stamp = int(datetime(2024, 1, 1, 13, tzinfo=timezone.utc).timestamp())
        actions[str(stamp)] = {'date': stamp, 'numerator': 2, 'denominator': 1, 'splitRatio': '2:1'}
    payload = {'chart': {'error': None, 'result': [{'meta': {'symbol': 'ITSA4.SA', 'currency': 'BRL'},
        'timestamp': [int(datetime(d.year, d.month, d.day, 13, tzinfo=timezone.utc).timestamp()) for d in months],
        'indicators': {'quote': [{'close': [10] * 61}]}, 'events': {'splits': actions}}]}}
    yahoo = original(root, 'Yahoo', research.dump(payload), 'json',
                      'https://query1.finance.yahoo.com/v8/finance/chart/ITSA4.SA')
    lines = ['Ticker;Date;RAW_CLOSE;Class;ISIN']
    for index in indexes:
        day = months[index]
        value = '20.00' if split and day.isoformat() < '2024-01-01' else '10.00'
        if decimals == ',':
            value = value.replace('.', ',')
        lines.append(f'ITSA4;{day};{value};PN;{identity["isin"]}')
    csv = original(root, 'export', ('\n'.join(lines) + '\n').encode(), 'csv')
    doc = original(root, 'documentation',
        b'Origin: Example Quotes supplies its own observations. RAW_CLOSE: unadjusted BRL per share.')
    contract = {'provider': 'Example Quotes', 'currency': 'BRL', 'date_role': 'trade',
        'date_format': '%Y-%m-%d', 'decimal': decimals, 'delimiter': ';',
        'columns': {'ticker': 'Ticker', 'date': 'Date', 'raw_close': 'RAW_CLOSE',
                    'share_class': 'Class', 'isin': 'ISIN'},
        'origin_documentation': {'source': doc, 'literal': 'Origin: Example Quotes supplies its own observations.'},
        'basis_documentation': {'source': doc, 'literal': 'RAW_CLOSE: unadjusted BRL per share.'}}
    return research.Reader(root), identity, yahoo, csv, contract


def event(day='2024-06-20', record='2024-06-05'):
    return {**stock(), 'payment_date': day, 'record_date': record,
        'payment_date_role': 'payment', 'record_date_role': 'entitlement_record',
        'approval_date': None, 'label': 'JCP', 'amount_tax_basis': 'explicit_gross',
        'gross_per_share_decimal': '0.04825383436', 'gross_reconstructed_from_net': False,
        'issues': []}


def previous_event():
    return event('2021-06-20', '2021-06-05')


@pytest.mark.parametrize('key,value', [('cvm', 3069), ('share_class', 'ON'), ('isin', 'BRITSAACNOR0')])
def test_security_identity_never_inferred_from_issuer_or_suffix(key, value):
    bad = stock()
    bad[key] = value
    with pytest.raises(ValueError, match='exact_CVM_class_ISIN_mismatch'):
        research.exact_identity(bad)


@pytest.mark.parametrize('basis', ['net', 'unspecified_by_table', 'dividend_per_share', None])
def test_JCP_requires_explicit_gross_not_a_net_tax_conversion(basis):
    bad = event()
    bad['amount_tax_basis'] = basis
    with pytest.raises(ValueError, match='gross_tax_basis'):
        research.validate_cash_events(stock(), [previous_event(), bad])


def test_net_reconstructed_amount_is_rejected_even_when_labelled_gross():
    bad = event()
    bad['gross_reconstructed_from_net'] = True
    with pytest.raises(ValueError, match='net_to_gross'):
        research.validate_cash_events(stock(), [previous_event(), bad])


@pytest.mark.parametrize('key,value', [('payment_date_role', 'ex'),
    ('payment_date_role', 'approval'), ('record_date_role', 'payment'),
    ('record_date_role', 'ex')])
def test_ex_record_approval_and_payment_roles_cannot_be_swapped(key, value):
    bad = event()
    bad[key] = value
    with pytest.raises(ValueError, match='cash_date_role_mismatch'):
        research.validate_cash_events(stock(), [previous_event(), bad])


@pytest.mark.parametrize('day', [None, '', 'ate 31/12/2026', '2024-02-30', '01/02/2024', '2024-01-01T12:00:00'])
def test_missing_deadline_or_ambiguous_payment_date_never_becomes_paid(day):
    bad = event()
    bad['payment_date'] = day
    with pytest.raises(ValueError):
        research.validate_cash_events(stock(), [previous_event(), bad])


def test_first_cash_only_history_does_not_prove_pre_window_coverage():
    with pytest.raises(ValueError, match='pre_window_primary_cash_evidence_missing'):
        research.validate_cash_events(stock(), [event()])


def test_dates_and_future_installments_are_retained_without_settlement_claim():
    future = event('2027-01-04', '2026-11-30')
    events = [previous_event(), event(), future]
    before = deepcopy(events)
    result = research.validate_cash_events(stock(), events)
    assert events == before
    assert result['events'] == [event()]
    assert result['future_events'] == [future]
    assert not result['settlement_verified']
    assert not result['cash_history_complete']
    assert result['window_start'] == '2021-09-30'


def test_hash_corruption_of_original_is_rejected(tmp_path):
    source = original(tmp_path, 'document', b'original public text')
    research.safe_path(tmp_path, source['file']).write_bytes(b'changed public text')
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        research.Reader(tmp_path).read(source)


def test_reader_preserves_missing_acquisition_size_but_verifies_acquisition_hash(tmp_path):
    source = original(tmp_path, 'document', b'public original')
    del source['bytes']
    reader = research.Reader(tmp_path)
    assert reader.read(source) == b'public original'
    assert reader.sources[source['file']]['bytes'] == 15


@pytest.mark.parametrize('url', ['https://www.b3.com.br/data', 'https://sistemaswebb3-listados.b3.com.br/',
    'https://bvmf.bmfbovespa.com.br/', 'https://ri.azzas2154.com.br/', 'https://www.allos.com.br/',
    'https://ri.santander.com.br/', 'https://query1.finance.yahoo.com/history',
    'https://yahoo-proxy.example/', 'http://provider.example/', 'https://user:pass@provider.example/',
    'https://provider.example:4430/', 'https://127.0.0.1/', 'https://provider.example/data#private'])
def test_forbidden_network_routes_never_approved(url):
    assert not research.approved(url)


def test_embedded_or_encoded_exchange_proxy_target_is_forbidden():
    assert not research.approved('https://issuer.example/proxy?url=https%3A%2F%2Fwww.b3.com.br%2Fdata')


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'Infinity', False])
def test_unknown_nonfinite_or_nonpositive_values_never_become_zero(value):
    with pytest.raises(ValueError, match='invalid_positive_decimal'):
        research.positive(value)


@pytest.mark.parametrize('split,decimal', [(False, '.'), (True, '.'), (True, ',')])
def test_price_reconciliation_uses_61_months_and_raw_anchors_without_approval(tmp_path, split, decimal):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path, split=split, decimals=decimal)
    result = research.reconcile_raw_quote_export(reader, identity, yahoo, csv, contract)
    assert len(result['prices']) == 61
    assert result['independent_raw_anchors'] == 4
    assert result['prices'][0]['month'] == '2021-09-30'
    assert result['primary_action_proof_still_required']
    assert not result['model_approved']
    assert result['authority'] == 'public_provider_not_exchange_certified'
    assert all(research.Decimal(p['normalized_close_decimal']) == 10 for p in result['prices'])


def test_four_anchors_in_latest_regime_cannot_validate_older_split_regime(tmp_path):
    args = quote_fixture(tmp_path, split=True, indexes=(36, 42, 48, 60))
    with pytest.raises(ValueError, match='every_used_share_regime'):
        research.reconcile_raw_quote_export(*args)


def test_three_anchors_cannot_pass_without_splits_either(tmp_path):
    args = quote_fixture(tmp_path, indexes=(0, 24, 60))
    with pytest.raises(ValueError, match='insufficient_independent_raw_price_anchors'):
        research.reconcile_raw_quote_export(*args)


def test_csv_for_another_class_is_not_reused_for_same_issuer(tmp_path):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    body = reader.read(csv).replace(b';PN;', b';ON;')
    csv = original(tmp_path, 'wrong-class', body, 'csv')
    with pytest.raises(ValueError, match='quote_exact_CVM_class_ISIN_mismatch'):
        research.reconcile_raw_quote_export(reader, identity, yahoo, csv, contract)


def test_Yahoo_wrapper_is_not_an_independent_provider(tmp_path):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    text = b'Example Quotes wraps Yahoo Finance quotes.'
    doc = original(tmp_path, 'wrapper-doc', text)
    contract['origin_documentation'] = {'source': doc, 'literal': text.decode()}
    with pytest.raises(ValueError, match='independent_upstream_origin_not_documented'):
        research.parse_raw_quote_csv(reader, identity, csv, contract)


@pytest.mark.parametrize('role', ['ex', 'payment', 'reference'])
def test_quote_date_must_be_trade_date(tmp_path, role):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    contract['date_role'] = role
    with pytest.raises(ValueError, match='quote_currency_or_date_role'):
        research.parse_raw_quote_csv(reader, identity, csv, contract)


def test_documented_adjusted_close_cannot_be_passed_as_raw(tmp_path):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    text = b'RAW_CLOSE: adjusted BRL per share for total return.'
    doc = original(tmp_path, 'adjusted-doc', text)
    contract['basis_documentation'] = {'source': doc, 'literal': text.decode()}
    with pytest.raises(ValueError, match='raw_unadjusted_price_basis_not_documented'):
        research.parse_raw_quote_csv(reader, identity, csv, contract)


def test_doc_literal_and_both_original_hashes_are_enforced(tmp_path):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    contract['basis_documentation']['literal'] = 'Invented unadjusted BRL RAW_CLOSE statement'
    with pytest.raises(ValueError, match='documentation_literal_not_found'):
        research.parse_raw_quote_csv(reader, identity, csv, contract)
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    research.safe_path(tmp_path, csv['file']).write_bytes(b'corrupted CSV')
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        research.parse_raw_quote_csv(reader, identity, csv, contract)


def test_HTTP403_persists_and_no_other_origin_is_requested(tmp_path):
    fetcher = research.Fetcher(tmp_path)

    class Denied:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden',
                {'Content-Type': 'text/plain'}, io.BytesIO(b'access denied'))

    fetcher.opener = Denied()
    with pytest.raises(ValueError, match='access_stop_HTTP_403'):
        fetcher._request('https://provider.example/robots.txt', {'role': 'fixture'})
    receipts = fetcher.history('response')
    assert receipts[0]['http_status'] == 403
    assert research.Reader(tmp_path).read(receipts[0]) == b'access denied'
    with pytest.raises(ValueError, match='access_stop_no_alternate_route'):
        research.Fetcher(tmp_path)._request('https://other.example/history', {'role': 'fixture'})
    assert len(fetcher.history('request')) == 1


def test_twenty_attempt_budget_counts_incomplete_and_robots_requests(tmp_path):
    for index in range(20):
        research.save(tmp_path, 'request', research.dump({'ordinal': index + 1,
            'url': f'https://provider.example/robots.txt?n={index}'}))
    with pytest.raises(ValueError, match='twenty_public_request_budget_exhausted'):
        research.Fetcher(tmp_path)._request('https://provider.example/history', {})


def test_request_ledger_tampering_cannot_reset_a_denial_or_budget(tmp_path):
    name = research.save(tmp_path, 'response', research.dump({'http_status': 403}))
    research.safe_path(tmp_path, name).write_bytes(research.dump({'http_status': 200}))
    with pytest.raises(ValueError, match='request_ledger_hash_mismatch'):
        research.Fetcher(tmp_path)._request('https://provider.example/history', {})


def test_discovery_requires_actual_original_bytes_and_literal_link(tmp_path):
    body = b'<script src="https://provider.example/history.js"></script>'
    source = original(tmp_path, 'site', body, 'html', 'https://issuer.example/quotes')
    source['content_type'] = 'text/html'
    receipt = research.save(tmp_path, 'fixture-receipt', research.dump(source))
    links = research.discover(body, source)
    assert links[0]['url'] == 'https://provider.example/history.js'
    with pytest.raises(ValueError, match='literal_public_link_discovery_required'):
        research.Fetcher(tmp_path).get('https://provider.example/guessed-history.csv', receipt)
    source['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='discovery_source_hash_mismatch'):
        research.discover(body, source)


def test_redirects_are_not_automatically_followed():
    assert research.NoRedirect().redirect_request(None, None, 302, '', {},
                                                  'https://alternate.example/') is None


def test_native_form_for_wrong_class_cannot_repair_calendar_record_date():
    identity = stock('FESA4')
    calendar = {'events': [{'payment_date': '2024-06-20', 'approval_date': '2024-05-28',
        'reported_per_share_decimal': '0.05307921780', 'record_date': None}]}
    fact = {'cvm': 3069, 'isin': 'BRFESAACNOR8', 'payment_date': '2024-06-20',
        'approval_date': '2024-05-28', 'record_date': '2024-06-05',
        'gross_per_unit_decimal': '0.05307921780', 'issues': []}
    documents = [{'events': [fact], 'source': {'fixture': True}}]
    assert research.declaration_matches(identity, calendar, documents) == []
    fact['isin'] = identity['isin']
    result = research.declaration_matches(identity, calendar, documents)
    assert len(result) == 1
    assert result[0]['record_date'] == '2024-06-05'
    assert not result[0]['cash_model_eligible']
    assert calendar['events'][0]['record_date'] is None


def test_calendar_rounding_is_preserved_as_a_conflict_not_repaired():
    identity = stock('FESA4')
    calendar = {'events': [{'payment_date': '2024-06-20', 'approval_date': '2024-05-28',
        'reported_per_share_decimal': '0.05308', 'record_date': None}]}
    fact = {'cvm': 3069, 'isin': identity['isin'], 'payment_date': '2024-06-20',
        'approval_date': '2024-05-28', 'record_date': '2024-06-05',
        'gross_per_unit_decimal': '0.05307921780', 'issues': []}
    assert research.declaration_matches(identity, calendar,
        [{'events': [fact], 'source': {'fixture': True}}]) == []


def test_month_missing_from_Yahoo_is_not_backfilled(tmp_path):
    reader, identity, yahoo, csv, contract = quote_fixture(tmp_path)
    payload = research.json.loads(reader.read(yahoo))
    result = payload['chart']['result'][0]
    result['timestamp'].pop(10)
    result['indicators']['quote'][0]['close'].pop(10)
    yahoo = original(tmp_path, 'missing-month', research.dump(payload), 'json')
    with pytest.raises(ValueError, match='less_than_61_consecutive_monthly_anchors'):
        research.reconcile_raw_quote_export(reader, identity, yahoo, csv, contract)
