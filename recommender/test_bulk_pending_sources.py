"""Public acquisition contracts using fake transports and temporary caches only."""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.error
import urllib.request

import pytest

from . import bulk_pending_sources as bulk


ORIGIN = 'https://issuer.example'
ROBOTS = ORIGIN + '/robots.txt'
URL = ORIGIN + '/annual.pdf'


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.content = body
        self.status = status
        self.headers = headers or {}
        self.read_limits = []
        self.closed = False

    def read(self, limit):
        self.read_limits.append(limit)
        return self.content[:limit]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self):
        self.routes = {ROBOTS: Response(status=404)}
        self.calls = []
        self.handlers = []
        self.delays = []

    def build_opener(self, handler):
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        self.handlers.append(handler)
        return self

    def open(self, request, timeout):
        self.calls.append((request.full_url, dict(request.header_items()), timeout))
        if request.full_url not in self.routes:
            pytest.fail('unexpected fake transport request: ' + request.full_url)
        response = self.routes[request.full_url]
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def urls(self):
        return [url for url, _, _ in self.calls]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('collector tests cannot use a real transport')

    for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                        (socket, 'create_connection'), (socket, 'getaddrinfo'),
                        (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
        monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(bulk.urllib.request, 'build_opener', fake.build_opener)
    monkeypatch.setattr(bulk.time, 'sleep', fake.delays.append)
    return fake


@pytest.fixture
def collector(tmp_path, transport):
    return bulk.PublicSourceCollector(tmp_path, 'unit')


@pytest.mark.parametrize('host', [
    'b3.com.br', 'bvmf.com.br', 'bmfbovespa.com.br', 'bovespa.com.br',
    'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br',
    'www.engie.com.br', 'cdn-sites-assets.mziq.com'])
@pytest.mark.parametrize('prefix', ['', 'sub.'])
def test_denied_domains_and_their_subdomains_never_open_transport(collector, transport, host, prefix):
    url = f'https://{prefix}{host.upper()}/annual.pdf'
    assert bulk.approved(url) is False
    with pytest.raises(ValueError, match='^unapproved_pending_public_source$'):
        collector.fetch(url)
    assert transport.calls == [] and list(collector.base.iterdir()) == []


@pytest.mark.parametrize('url', [
    URL, 'https://dados.cvm.gov.br/dados/DFP/2025.zip',
    'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?NumeroSequencialDocumento=155874',
    'https://issuer.example:443/report.pdf?year=2025',
    'https://notb3.com.br/report.pdf', 'https://b3.com.br.public.example/report.pdf'])
def test_public_HTTPS_sources_are_allowed_without_overmatching_domain_suffixes(url):
    assert bulk.approved(url) is True


@pytest.mark.parametrize('url', [
    'http://issuer.example/report.pdf', 'ftp://issuer.example/report.pdf',
    'https://issuer.example:8443/report.pdf', 'https://issuer.example:invalid/report.pdf',
    'https://user:pass@issuer.example/report.pdf', 'https://issuer.example/report.pdf#section',
    'https://localhost/report.pdf', 'https://a.local/report.pdf',
    'https://a.localhost/report.pdf', 'https://a.internal/report.pdf',
    'https://127.0.0.1/report.pdf', 'https://[::1]/report.pdf',
    'https://issuer.example/login/report.pdf', 'https://issuer.example/%256cogin',
    'https://issuer.example/signin', 'https://issuer.example/captcha',
    'https://issuer.example/oauth', 'https://issuer.example/proxy/report.pdf',
    'https://issuer.example/report?access_token=x', 'https://issuer.example/report?api_key=x',
    'https://issuer.example/report?signature=x', 'https://issuer.example/report?secret=x',
    'https://issuer.example/report?authorization=x', None, 'not a URL'])
def test_credentials_challenges_local_addresses_and_unsafe_URLs_are_rejected(url):
    assert bulk.approved(url) is False


def test_trailing_dot_hosts_cannot_bypass_domain_or_local_address_restrictions():
    urls = ['https://b3.com.br./annual.pdf', 'https://sub.b3.com.br./annual.pdf',
            'https://127.0.0.1./annual.pdf']
    assert [url for url in urls if bulk.approved(url)] == []


@pytest.mark.parametrize('scope', ['../escape', '', 'UPPER', 'a/b', 'a_b', 'a' * 42])
def test_scope_is_a_local_lowercase_name(tmp_path, scope):
    with pytest.raises(ValueError, match='^invalid_pending_source_scope$'):
        bulk.PublicSourceCollector(tmp_path, scope)
    assert not (tmp_path / bulk.CACHE).exists()


def test_success_records_exact_hash_bytes_discovery_and_exclusive_cache(collector, transport):
    body = b'%PDF-1.7\npublic annual report\n'
    response = Response(body, headers={'Content-Type': 'application/pdf'})
    transport.routes[URL] = response
    discovery = {'kind': 'issuer_link', 'source': 'https://issuer.example/reports'}
    receipt = collector.fetch(URL, discovery=discovery)
    assert transport.urls == [ROBOTS, URL]
    assert transport.delays == []
    assert response.closed and response.read_limits == [bulk.MAX_BYTES + 1]
    assert receipt['status'] == 200 and receipt['error'] is None
    assert receipt['url'] == URL and receipt['discovery'] == discovery
    assert receipt['scope'] == 'unit' and receipt['retries'] == 0
    assert receipt['sha256'] == _digest(body) and receipt['bytes'] == len(body)
    assert receipt['file'] == f'{bulk.CACHE}/unit/body-{_digest(body)}.bin'
    assert collector.body(receipt) == body
    assert receipt['published_at'] is None
    assert receipt['model_admission'] is False and receipt['bank_settlement_verified'] is False
    for _, headers, timeout in transport.calls:
        assert {key.lower(): value for key, value in headers.items()} == {
            'user-agent': bulk.USER_AGENT, 'accept-encoding': 'identity'}
        assert timeout == 25
    snapshot = {p.name: p.read_bytes() for p in collector.base.iterdir()}
    assert collector.fetch(URL) == receipt
    assert bulk.PublicSourceCollector(collector.root, 'unit').fetch(URL) == receipt
    assert transport.urls == [ROBOTS, URL]
    assert snapshot == {p.name: p.read_bytes() for p in collector.base.iterdir()}


@pytest.mark.parametrize('status', [401, 403, 429])
def test_HTTP_access_stops_are_cached_and_stop_the_origin_without_retry(collector, transport, status):
    transport.routes[URL] = urllib.error.HTTPError(URL, status, 'stop', {}, io.BytesIO(b'denied'))
    receipt = collector.fetch(URL)
    assert receipt['status'] == status and receipt['error'] == 'access_stop_no_retry'
    assert collector.fetch(URL) == receipt
    for scope in ('unit', 'other'):
        restarted = bulk.PublicSourceCollector(collector.root, scope)
        with pytest.raises(ValueError, match='^previously_stopped_origin_no_retry$'):
            restarted.fetch(ORIGIN + '/different.pdf')
    assert transport.urls == [ROBOTS, URL]


@pytest.mark.parametrize('title', ['CAPTCHA', 'Access Denied', 'Sign In', 'Just a Moment'])
def test_HTTP200_challenge_page_stops_further_requests(collector, transport, title):
    transport.routes[URL] = Response(f'<html><title>{title}</title></html>'.encode())
    receipt = collector.fetch(URL)
    assert receipt['error'] == 'public_content_challenge_stop'
    with pytest.raises(ValueError, match='^previously_stopped_origin_no_retry$'):
        collector.fetch(ORIGIN + '/different.pdf')
    assert transport.urls == [ROBOTS, URL]


@pytest.mark.parametrize('status', [404, 500, 503])
def test_non_success_is_recorded_once_without_retry(collector, transport, status):
    transport.routes[URL] = Response(b'not available', status=status)
    receipt = collector.fetch(URL)
    assert receipt['status'] == status and receipt['error'] == 'non_success_no_retry'
    assert collector.fetch(URL) == receipt
    assert bulk.PublicSourceCollector(collector.root, 'unit').fetch(URL) == receipt
    assert transport.urls == [ROBOTS, URL]


def test_transport_exception_is_recorded_and_not_retried(collector, transport):
    transport.routes[URL] = urllib.error.URLError('offline')
    receipt = collector.fetch(URL)
    assert receipt['status'] is None and receipt['error'] == 'URLError'
    assert receipt['bytes'] == 0 and collector.body(receipt) == b''
    assert collector.fetch(URL) == receipt
    assert transport.urls == [ROBOTS, URL]


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirect_is_recorded_and_handler_refuses_automatic_follow(collector, transport, status):
    target = 'https://b3.com.br/restricted.pdf'
    transport.routes[URL] = Response(b'redirect', status, {'Location': target})
    receipt = collector.fetch(URL)
    assert receipt['status'] == status and receipt['location'] == target
    assert receipt['error'] == 'redirect_recorded_not_automatically_followed'
    assert transport.urls == [ROBOTS, URL]
    handler = transport.handlers[-1]
    assert handler.redirect_request(urllib.request.Request(URL), None, status, 'redirect', {}, target) is None
    assert collector.fetch(URL) == receipt
    assert transport.urls == [ROBOTS, URL]


def test_robots_disallow_stops_before_target_request_and_persists_policy(collector, transport):
    transport.routes[ROBOTS] = Response(b'User-agent: *\nDisallow: /private/\n')
    target = ORIGIN + '/private/report.pdf'
    with pytest.raises(ValueError, match='^pending_public_source_disallowed_by_robots$'):
        collector.fetch(target)
    assert transport.urls == [ROBOTS]
    restarted = bulk.PublicSourceCollector(collector.root, 'unit')
    with pytest.raises(ValueError, match='^pending_public_source_disallowed_by_robots$'):
        restarted.fetch(target)
    assert transport.urls == [ROBOTS]
    assert not (collector.base / f'request-{_digest(target.encode())}.json').exists()


@pytest.mark.parametrize(('policy', 'delay'), [
    (b'User-agent: *\nDisallow: /private/\nAllow: /private/public/\n', 1),
    (b'User-agent: *\nCrawl-delay: 3\n', 3),
    (b'User-agent: *\nRequest-rate: 2/10\n', 5),
    (b'User-agent: *\nDisallow: /\nUser-agent: RendaFutura-Academic\nAllow: /\n', 1)])
def test_robots_allow_precedence_agent_group_and_polite_rate(collector, transport, policy, delay):
    target = ORIGIN + '/private/public/report.pdf'
    transport.routes[ROBOTS] = Response(policy)
    transport.routes[target] = Response(b'public')
    assert collector.fetch(target)['error'] is None
    assert transport.urls == [ROBOTS, target] and transport.delays == [delay]


def test_robots_rate_over_one_minute_stops_without_target_or_sleep(collector, transport):
    transport.routes[ROBOTS] = Response(b'User-agent: *\nCrawl-delay: 61\n')
    with pytest.raises(ValueError, match='^pending_source_rate_requires_later_run$'):
        collector.fetch(URL)
    assert transport.urls == [ROBOTS] and transport.delays == []


@pytest.mark.parametrize('status', [301, 401, 403, 429, 500])
def test_unavailable_robots_never_authorizes_target(collector, transport, status):
    transport.routes[ROBOTS] = Response(b'unavailable', status)
    with pytest.raises(ValueError, match='^pending_robots_policy_unavailable_stop$'):
        collector.fetch(URL)
    assert transport.urls == [ROBOTS]


@pytest.mark.parametrize(('size', 'error'), [(8, None), (9, 'response_size_limit'), (20, 'response_size_limit')])
def test_maximum_bytes_is_bounded_during_read_and_hashed_as_recorded(
        collector, transport, monkeypatch, size, error):
    monkeypatch.setattr(bulk, 'MAX_BYTES', 8)
    response = Response(b'x' * size)
    transport.routes[URL] = response
    receipt = collector.fetch(URL)
    observed = b'x' * min(size, 9)
    assert response.read_limits == [9] and response.closed
    assert receipt['error'] == error and receipt['maximum_bytes'] == 8
    assert receipt['bytes'] == len(observed) and receipt['sha256'] == _digest(observed)
    assert collector.body(receipt) == observed
    assert collector.fetch(URL) == receipt and transport.urls == [ROBOTS, URL]


def test_request_budget_reserves_robots_as_well_as_target(collector, transport, monkeypatch):
    monkeypatch.setattr(bulk, 'MAX_REQUESTS', 1)
    with pytest.raises(ValueError, match='^pending_public_request_budget_exhausted$'):
        collector.fetch(URL)
    assert transport.urls == [ROBOTS]
    assert len(list(collector.base.glob('request-*.json'))) == 1


@pytest.mark.parametrize('wrapper', [None, 'responses', 'requests', 'results', 'sources'])
def test_prior_stop_metadata_in_any_group_blocks_new_origin_requests(tmp_path, transport, wrapper):
    stopped = {'url': URL, 'status': 429, 'error': 'access_stop_no_retry'}
    value = [stopped] if wrapper is None else {wrapper: [stopped]}
    _write(tmp_path / 'raw/primary-research/old/evidence.json', value)
    collector = bulk.PublicSourceCollector(tmp_path, 'unit')
    with pytest.raises(ValueError, match='^previously_stopped_origin_no_retry$'):
        collector.fetch(ORIGIN + '/new.pdf')
    assert transport.calls == []


def test_new_stop_in_another_group_is_seen_by_existing_collector(collector, transport):
    collector.fetch(ROBOTS)
    _write(collector.root / bulk.CACHE / 'other/response-stop.json', {
        'url': ORIGIN + '/other.pdf', 'status': 403})
    with pytest.raises(ValueError, match='^pending_origin_stopped_in_another_group$'):
        collector.fetch(URL)
    assert transport.urls == [ROBOTS]


def test_interrupted_request_marker_cannot_be_retried(collector, transport):
    _write(collector.base / f'request-{_digest(URL.encode())}.json', {'url': URL})
    with pytest.raises(ValueError, match='^interrupted_public_request_no_retry$'):
        collector.fetch(URL)
    assert transport.calls == []


def test_cache_save_is_idempotent_but_never_overwrites_conflicting_bytes(tmp_path):
    path = tmp_path / 'immutable.bin'
    bulk._save(path, b'original')
    bulk._save(path, b'original')
    with pytest.raises(ValueError, match='^immutable_public_source_conflict$'):
        bulk._save(path, b'changed')
    assert path.read_bytes() == b'original'


def test_cache_creation_uses_exclusive_open_even_when_another_writer_wins(tmp_path, monkeypatch):
    path = _write(tmp_path / 'raced.bin', b'first writer')
    exists = Path.exists
    monkeypatch.setattr(Path, 'exists', lambda p: False if p == path else exists(p))
    with pytest.raises(FileExistsError):
        bulk._save(path, b'second writer')
    assert path.read_bytes() == b'first writer'


@pytest.mark.parametrize('fault', ['body_bytes', 'body_missing', 'receipt_bytes', 'receipt_hash', 'receipt_json'])
def test_corrupt_or_missing_cache_fails_closed_without_new_transport(collector, transport, fault):
    transport.routes[URL] = Response(b'original')
    receipt = collector.fetch(URL)
    response_path = collector.base / f'response-{_digest(URL.encode())}.json'
    body_path = collector.root / receipt['file']
    if fault == 'body_bytes':
        body_path.write_bytes(b'changed!')
    elif fault == 'body_missing':
        body_path.unlink()
    elif fault == 'receipt_json':
        response_path.write_bytes(b'{invalid')
    else:
        changed = deepcopy(receipt)
        changed['bytes' if fault == 'receipt_bytes' else 'sha256'] = 999 if fault == 'receipt_bytes' else '0' * 64
        response_path.write_text(json.dumps(changed))
    before = list(transport.calls)
    with pytest.raises((OSError, ValueError)):
        collector.fetch(URL)
    assert transport.calls == before


def test_cache_body_cannot_escape_root_even_with_matching_hash(collector, tmp_path, transport):
    outside = _write(tmp_path.parent / (tmp_path.name + '-outside.bin'), b'outside')
    receipt = {'file': '../' + outside.name, 'bytes': 7, 'sha256': _digest(b'outside')}
    try:
        with pytest.raises(ValueError, match='^unsafe_pending_original$'):
            collector.body(receipt)
        assert transport.calls == [] and outside.read_bytes() == b'outside'
    finally:
        outside.unlink()


@pytest.mark.parametrize('stage', ['request', 'body', 'response'])
def test_cache_write_error_is_not_converted_into_a_retry(collector, transport, monkeypatch, stage):
    collector.fetch(ROBOTS)
    transport.routes[URL] = Response(b'original')
    save = bulk._save

    def failing_save(path, value):
        if path.name.startswith(stage + '-'):
            raise PermissionError('temporary cache denied')
        return save(path, value)

    with monkeypatch.context() as denied:
        denied.setattr(bulk, '_save', failing_save)
        with pytest.raises(PermissionError, match='temporary cache denied'):
            collector.fetch(URL)
    assert transport.urls == ([ROBOTS] if stage == 'request' else [ROBOTS, URL])
    if stage != 'request':
        with pytest.raises(ValueError, match='^interrupted_public_request_no_retry$'):
            collector.fetch(URL)
        assert transport.urls == [ROBOTS, URL]


def test_cached_receipt_for_a_different_URL_must_be_rejected(collector, transport, monkeypatch):
    transport.routes[URL] = Response(b'original')
    receipt = collector.fetch(URL)
    receipt['url'] = ORIGIN + '/different.pdf'
    _write(collector.base / f'response-{_digest(URL.encode())}.json', receipt)
    monkeypatch.setattr(collector, 'body', lambda *args: pytest.fail('receipt URL must be checked before body'))
    with pytest.raises(ValueError, match='^pending_cached_receipt_URL_mismatch$'):
        collector.fetch(URL)
    assert transport.urls == [ROBOTS, URL]
