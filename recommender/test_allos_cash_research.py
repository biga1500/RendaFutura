from copy import deepcopy
import io
from pathlib import Path
import socket
import urllib.error

import pytest

from . import allos_cash_research as research


RI = 'https://allos.com.br/'


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('tests_must_not_use_live_network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


def facts():
    source = {'index_evidence': {'raw_row': {'Codigo_CVM': '22357', 'CNPJ_Companhia': '05.878.397/0001-32'}}}
    base = {'record_date': '2024-12-20', 'period_end': '2024-06-30', 'share_class': 'ON',
            'label': 'JCP', 'payment_date': '2025-01-07', 'amount': '0.119574197'}
    return {
        '841501': {'fields': base, 'published_at': '2024-12-17', 'source': deepcopy(source)},
        '841533': {'fields': deepcopy(base), 'published_at': '2024-12-17', 'source': deepcopy(source)},
        '842767': {'fields': {'label': 'JCP', 'revision_language': 'explicit_final_gross_amount',
                              'revision_of_publication': '2024-12-17', 'amount': '0.120110445',
                              'payment_date': '2024-01-07'},
                   'published_at': '2024-12-20', 'source': deepcopy(source)}}


def test_explicit_relationship_retains_both_years_and_exact_period():
    result = research.documentary_relationship(facts())
    assert result['record_date'] == '2024-12-20'
    assert result['period_end'] == '2024-06-30'
    assert result['share_class'] == 'ON'
    assert result['label'] == 'JCP'
    assert result['amended_gross_per_share_decimal'] == '0.120110445'
    assert result['board_scheduled_payment_date'] == '2025-01-07'
    assert result['amendment_printed_payment_date'] == '2024-01-07'
    assert result['year_conflict'] is True
    assert result['date_values_repaired'] is result['ISIN_printed_in_prose_notices'] is False


@pytest.mark.parametrize(('key', 'wrong'), [
    ('share_class', 'PN'), ('record_date', '2024-12-19'), ('period_end', '2023-12-31'),
    ('label', 'DIVIDENDO')])
def test_same_amount_cannot_bridge_wrong_class_record_type_or_period(key, wrong):
    rows = facts()
    rows['841501']['fields'][key] = wrong
    with pytest.raises(ValueError, match='exact_JCP_class_record_and_period'):
        research.documentary_relationship(rows)


@pytest.mark.parametrize('sequence', ['841501', '841533', '842767'])
@pytest.mark.parametrize('field', ['Codigo_CVM', 'CNPJ_Companhia'])
def test_each_original_requires_exact_issuer_identity(sequence, field):
    rows = facts()
    rows[sequence]['source']['index_evidence']['raw_row'][field] = '20567' if field == 'Codigo_CVM' else '62144175000120'
    with pytest.raises(ValueError, match='CVM_CNPJ_relationship'):
        research.documentary_relationship(rows)


@pytest.mark.parametrize('mutation', ['link', 'language', 'label', 'publication', 'base_amount', 'base_payment'])
def test_explicit_notice_link_is_required_before_accepting_amended_amount(mutation):
    rows = facts()
    if mutation == 'link':
        rows['842767']['fields']['revision_of_publication'] = '2024-11-18'
    elif mutation == 'language':
        rows['842767']['fields'].pop('revision_language')
    elif mutation == 'label':
        rows['842767']['fields']['label'] = 'DIVIDENDO'
    elif mutation == 'publication':
        rows['842767']['published_at'] = '2024-12-16'
    elif mutation == 'base_amount':
        rows['841533']['fields']['amount'] = '0.120110445'
    else:
        rows['841533']['fields']['payment_date'] = '2024-12-23'
    with pytest.raises(ValueError):
        research.documentary_relationship(rows)


def test_market_comparator_injection_cannot_select_amount_or_correct_year():
    for comparator in ('0.119574197', '0.120110445', '99.99'):
        rows = facts()
        rows['842767']['market_comparator'] = comparator
        result = research.documentary_relationship(rows)
        assert result['amended_gross_per_share_decimal'] == '0.120110445'
        assert result['amendment_printed_payment_date'] == '2024-01-07'
        assert result['selection_uses_market_amount'] is False


@pytest.mark.parametrize('url', [
    'https://www.b3.com.br/robots.txt', 'https://sistemaswebb3-listados.b3.com.br/data',
    'https://brapi.dev/api/quote/ALOS3', 'https://ri.azzas2154.com.br/',
    'https://proxy.example/?url=https://allos.com.br/', 'https://allos.com.br/?proxy=https://www.b3.com.br/',
    'https://allos.com.br.evil.example/', 'http://allos.com.br/', 'https://user@allos.com.br/',
    'https://allos.com.br:8080/', 'https://allos.com.br/#fragment'])
def test_forbidden_sources_and_proxies_never_reserve_a_request(tmp_path, url):
    with pytest.raises(ValueError, match='unapproved_URL'):
        research.Fetcher(tmp_path)._get(url)
    assert research.Fetcher(tmp_path).ledger('request') == []


def test_CDN_source_requires_a_parent_RI_discovery_and_never_an_exchange_link():
    url = 'https://api.mziq.com/mzfilemanager/v2/d/document'
    assert research.approved(url) is False
    discovery = [{'url': url, 'parent_url': RI}]
    assert research.approved(url, discovery) is True
    assert research.approved('https://api.mziq.com/robots.txt', discovery) is True
    assert research.approved(url, [{'url': url, 'parent_url': 'https://www.b3.com.br/'}]) is False


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_denial_stops_all_hosts_and_survives_restart(tmp_path, status):
    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(request.full_url, status, 'denied', {}, io.BytesIO(b'denied'))

    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = opener = Opener()
    fetcher._get(RI)
    with pytest.raises(ValueError, match='access_stop'):
        research.Fetcher(tmp_path).acquire('https://ri.allos.com.br/')
    assert opener.calls == len(fetcher.ledger('request')) == 1


def test_all_thirteen_attempts_count_even_without_success(tmp_path):
    for ordinal in range(1, 14):
        research.save_body(tmp_path, f'request-{ordinal:02d}.json', research.shared._dump({'url': RI + str(ordinal)}))
    with pytest.raises(ValueError, match='budget_exhausted'):
        research.Fetcher(tmp_path)._get(RI)


def test_TLS_failure_is_not_retried_or_interpreted_as_zero_cash(tmp_path):
    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.URLError('CERTIFICATE_VERIFY_FAILED')

    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = opener = Opener()
    body, receipt = fetcher._get(RI)
    assert body == b'' and receipt['status'] == 'failed'
    assert 'gross_per_share' not in receipt
    with pytest.raises(ValueError, match='already_attempted'):
        research.Fetcher(tmp_path)._get(RI)
    assert opener.calls == 1


def test_robots_disallow_prevents_the_page_request(tmp_path):
    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'text/plain'}

    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            return Response(b'User-agent: *\nDisallow: /\n')

    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = opener = Opener()
    with pytest.raises(ValueError, match='robots_disallow'):
        fetcher.acquire(RI)
    assert opener.calls == len(fetcher.ledger('request')) == 1


def test_redirect_is_recorded_and_never_followed_automatically(tmp_path):
    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(request.full_url, 301, 'redirect',
                                        {'Location': RI + 'robots.txt'}, io.BytesIO(b''))

    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = opener = Opener()
    _, receipt = fetcher._get('https://www.allos.com.br/robots.txt')
    assert receipt['http_status'] == 301 and receipt['location'] == RI + 'robots.txt'
    assert opener.calls == 1
    assert research._NoFollow().redirect_request(None, None, 301, '', {}, RI) is None


def test_immutable_outputs_stay_inside_new_sidecar_scope(tmp_path):
    with pytest.raises(ValueError, match='unsafe_cache_reference'):
        research.save_body(tmp_path, '../../revised-cash/forbidden.json', b'{}')
    research.save_body(tmp_path, 'original.bin', b'original')
    with pytest.raises(ValueError, match='immutable_allos'):
        research.save_body(tmp_path, 'original.bin', b'changed')
    with pytest.raises(ValueError, match='content_hash_mismatch'):
        research.Reader(tmp_path).read(research.CACHE + '/original.bin', 'original', '0' * 64)


def test_real_sidecar_is_offline_read_only_and_leaves_the_conflict(monkeypatch):
    folder = Path(__file__).parent / 'data/market'
    if not (folder / research.CACHE).is_dir():
        pytest.skip('local research originals and access receipts are not distributed')

    def forbidden(*args, **kwargs):
        pytest.fail('documentary_sidecar must not acquire or write')

    monkeypatch.setattr(research.Fetcher, '_get', forbidden)
    monkeypatch.setattr(research, 'save', forbidden)
    monkeypatch.setattr(research, 'save_body', forbidden)
    result = research.documentary_sidecar(folder)
    assert result['research_status'] == 'stopped_access_denied'
    assert result['documentary_payment_date'] is None
    assert result['new_RI_payment_rows'] is None
    assert result['relationship']['amended_gross_per_share_decimal'] == '0.120110445'
    assert result['relationship']['amendment_printed_payment_date'] == '2024-01-07'
    assert result['identity']['ISIN'] == 'BRALOSACNOR5'
    assert result['network']['attempts_used'] <= 13
    assert result['network']['denial_sources'][0]['http_status'] == 403
    for field in ('documentary_payment_effected_verified', 'bank_settlement_certified',
                  'settlement_verified', 'point_in_time_archive_verified', 'cash_model_eligible',
                  'financials_changed', 'parent_model_changed', 'market_cash_comparator_read',
                  'ex_dates_used_as_payment'):
        assert result[field] is False
    assert result['protected_files']['before'] == result['protected_files']['after']
    assert result['remaining_conflicts'][0]['board_payment_date'] == '2025-01-07'
