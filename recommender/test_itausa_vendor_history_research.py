"""No live requests. Exercise the authorization boundary and response receipts."""

import io
from pathlib import Path
import urllib.error

import pytest

from . import itausa_vendor_history_research as research


CDN = 'https://cdn-sites-assets.mziq.com'
SCRIPT = CDN + '/public-stockinfo.js'
ISSUER = 'https://ri.itausa.com.br/quotes/'


@pytest.fixture
def fetcher(tmp_path, monkeypatch):
    requests = []
    urls = ['https://ri.itausa.com.br/robots.txt', ISSUER,
        'https://www.ferbasa.com.br/robots.txt', 'https://www.ferbasa.com.br/quotes/',
        CDN + '/robots.txt', 'https://www.engie.com.br/robots.txt']
    for ordinal, url in enumerate(urls, 1):
        request = {'ordinal': ordinal, 'url': url}
        research.prior.save(tmp_path, 'request', research.prior.dump(request))
        body = b'User-agent: *\nDisallow:\n' if ordinal in (1, 3) else b''
        if ordinal == 2:
            body = f'<script src="{SCRIPT}"></script>'.encode()
        source = {**request, 'http_status': {5: 302, 6: 403}.get(ordinal, 200),
            'sha256': research.prior.sha(body), 'bytes': len(body),
            'file': research.prior.save(tmp_path, 'body', body, 'bin'),
            'content_type': 'text/html' if ordinal == 2 else 'text/plain'}
        research.prior.save(tmp_path, 'response', research.prior.dump(source))
        requests.append(source)
    monkeypatch.setattr(research, 'load_prior', lambda root:
        (research.prior.Reader(root), {'requests': requests}))
    return research.Fetcher(tmp_path)


def discovery(fetcher):
    return dict(fetcher.cached(ISSUER)[1])


class Responses:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        status, headers, body = next(self.responses)
        if status >= 300:
            raise urllib.error.HTTPError(request.full_url, status, 'fixture', headers, io.BytesIO(body))
        response = io.BytesIO(body)
        response.status, response.headers = status, headers
        return response


@pytest.mark.parametrize('url', [
    'https://www.engie.com.br/robots.txt', 'https://ri.engie.com.br/any',
    'https://ri.itausa.com.br/engie/data', 'https://ri.itausa.com.br/?next=EGIE3',
    'https://www.b3.com.br/', 'https://ri.azzas2154.com.br/', 'https://allos.com.br/',
    'https://ri.santander.com.br/', 'https://query1.finance.yahoo.com/',
    'https://other.mziq.com/history', 'https://cdn.mziq.com/login',
    CDN + '/?api_key=public-looking', CDN + '/?token=x', CDN + '/wp-signup.php?new=site',
    CDN + '/?next=https%253A%252F%252Fwww.engie.com.br',
    'http://ri.itausa.com.br/', 'https://user:pass@cdn.mziq.com/',
    'https://cdn.mziq.com:444/', 'https://cdn.mziq.com/file#private',
])
def test_forbidden_origins_paths_credentials_and_alternatives(fetcher, url):
    opener = Responses()
    fetcher.opener = opener
    with pytest.raises(ValueError, match='forbidden_or_unallowlisted'):
        fetcher.request(url, {})
    assert not opener.calls
    assert not fetcher.history('request')


@pytest.mark.parametrize('status', [401, 403, 429])
def test_new_denial_stops_all_hosts_with_original_receipt(fetcher, status):
    fetcher.opener = Responses((status, {'Content-Type': 'text/plain'}, b'denied'))
    with pytest.raises(ValueError, match=f'access_stop_HTTP_{status}'):
        fetcher.request(SCRIPT, {})
    receipt, = fetcher.history('response')
    assert fetcher.reader.read(receipt) == b'denied'
    assert receipt['http_status'] == status
    assert receipt['combined_ordinal'] == 7
    with pytest.raises(ValueError, match='access_stop_no_further_network'):
        research.Fetcher(fetcher.root).request('https://www.ferbasa.com.br/history', {})
    assert len(fetcher.opener.calls) == 1


def test_previous_denied_body_is_never_read(fetcher, monkeypatch):
    read = fetcher.reader.read

    def read_only_allowed(receipt, *args):
        assert 'engie' not in receipt.get('url', '')
        return read(receipt, *args)

    monkeypatch.setattr(fetcher.reader, 'read', read_only_allowed)
    assert fetcher.cached('https://ri.itausa.com.br/robots.txt')[1]['http_status'] == 200
    with pytest.raises(ValueError, match='forbidden'):
        fetcher.cached('https://www.engie.com.br/robots.txt')


def test_ordinary_robots_redirect_is_counted_then_real_policy_checked(fetcher):
    fetcher.opener = Responses(
        (302, {'Location': '/public-robots.txt'}, b''),
        (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nDisallow:\n'),
        (200, {'Content-Type': 'application/javascript'}, b'public widget'))
    _, receipt, chain = fetcher.get(SCRIPT, discovery(fetcher))
    assert receipt['http_status'] == 200
    assert len(chain) == 1
    receipts = fetcher.history('response')
    assert [r['http_status'] for r in receipts] == [302, 200, 200]
    assert receipts[0]['location'] == '/public-robots.txt'
    assert receipts[1]['discovery']['source']['sha256'] == receipts[0]['sha256']
    assert receipts[2]['discovery']['robots_chain'] == receipts[:2]
    assert all(not r['automatic_redirects'] and r['automatic_retries'] == 0 for r in receipts)


@pytest.mark.parametrize('location', ['https://other.example/robots.txt',
    'https://www.engie.com.br/robots.txt', '/login', '/?api_key=secret',
    'https://cdn.mziq.com/robots.txt'])
def test_redirect_never_authorizes_auth_or_new_origin(fetcher, location):
    fetcher.opener = Responses((302, {'Location': location}, b''))
    with pytest.raises(ValueError, match='redirect_.*stop'):
        fetcher.get(SCRIPT, discovery(fetcher))
    assert len(fetcher.opener.calls) == 1
    assert len(fetcher.history('request')) == 1


def test_redirected_HTML_does_not_count_as_robots_permission(fetcher):
    fetcher.opener = Responses((302, {'Location': '/'}, b''),
        (200, {'Content-Type': 'text/html'}, b'<html>Homepage</html>'))
    with pytest.raises(ValueError, match='policy_unknown_stop'):
        fetcher.get(SCRIPT, discovery(fetcher))
    assert len(fetcher.opener.calls) == 2


def test_robots_disallow_keeps_widget_unrequested(fetcher):
    fetcher.opener = Responses((200, {'Content-Type': 'text/plain'}, b'User-agent: *\nDisallow: /\n'))
    with pytest.raises(ValueError, match='robots_disallows'):
        fetcher.get(SCRIPT, discovery(fetcher))
    assert len(fetcher.history('request')) == 1


def test_exact_literal_and_discovery_hash_required(fetcher):
    source = discovery(fetcher)
    with pytest.raises(ValueError, match='literal_allowlisted_public_link_required'):
        fetcher.get(CDN + '/guessed-prices.csv', source)
    source['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        fetcher.get(SCRIPT, source)
    assert not fetcher.history('request')


def test_self_asserted_response_receipt_cannot_authorize_a_link(fetcher):
    source = discovery(fetcher)
    source['http_date'] = 'invented'
    with pytest.raises(ValueError, match='recorded_original_response_discovery_required'):
        fetcher.get(SCRIPT, source)
    assert not fetcher.history('request')


def test_combined_budget_and_no_retry_are_persistent(fetcher):
    for index in range(14):
        fetcher.opener = Responses((200, {}, b'public'))
        fetcher.request(CDN + f'/linked-{index}.js', {'fixture': True})
    with pytest.raises(ValueError, match='budget_exhausted'):
        research.Fetcher(fetcher.root).request(CDN + '/linked-14.js', {})
    assert len(fetcher.history('request')) == 14
    assert fetcher.history('response')[-1]['combined_ordinal'] == 20


def test_incomplete_attempt_cannot_be_retried(fetcher):
    research.save(fetcher.root, 'request', research.prior.dump({'ordinal': 1, 'url': SCRIPT}))
    with pytest.raises(ValueError, match='incomplete_attempt'):
        fetcher.request(SCRIPT, {})


def test_ledger_tampering_fails_closed(fetcher):
    fetcher.opener = Responses((403, {}, b'denied'))
    with pytest.raises(ValueError):
        fetcher.request(SCRIPT, {})
    path, = (fetcher.root / research.CACHE).glob('response-*.json')
    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match='ledger_hash_mismatch'):
        fetcher.request('https://www.ferbasa.com.br/another', {})


def test_recorded_stop_cannot_resume_on_another_host(fetcher):
    research.save(fetcher.root, 'stop', research.prior.dump({'reason': 'redirect_auth_or_unallowlisted_stop'}))
    with pytest.raises(ValueError, match='recorded_scope_stop'):
        fetcher.request('https://www.ferbasa.com.br/another', {})


def test_output_never_escapes_scoped_cache(tmp_path):
    with pytest.raises(ValueError, match='unsafe_output_name'):
        research.save(tmp_path, '../current', b'{}')
    name = research.save(tmp_path, 'review', b'{}')
    assert name.startswith(research.CACHE + '/')
    assert (tmp_path / name).read_bytes() == b'{}'


def test_actual_cache_reparses_without_network_writes_or_admission(monkeypatch):
    root = Path(__file__).parent / 'data/market'
    if not (root / research.REPORT_NAME).exists():
        pytest.skip('local public research originals not installed')

    def forbidden_request(*args, **kwargs):
        raise AssertionError('offline review attempted network')

    monkeypatch.setattr(research.Fetcher, 'request', forbidden_request)
    # Replay is offline against today's pointers, not a new acquisition under
    # the immutable historical acquisition baseline. Recorded access stops stay.
    current_pointers = {name: research.prior.sha((root / name).read_bytes())
                        for name in research.PROTECTED_BEFORE}
    monkeypatch.setattr(research, 'PROTECTED_BEFORE', current_pointers)
    before = sorted(p.name for p in (root / research.CACHE).iterdir())
    report = research.build_report(root)
    assert before == sorted(p.name for p in (root / research.CACHE).iterdir())
    assert {r['identity']['ticker'] for r in report['results']} == set(research.IDENTITIES)
    assert report['prior_review']['previously_verified_sources'] == 34
    assert not report['model_admissions']
    assert not report['point_in_time_archive_verified']
    assert not report['settlement_verified']
    assert all(r['price_window']['monthly_anchors'] == 61 for r in report['results'])
    assert all(r['independent_raw_anchors'] == 0 and r['candidate'] is None for r in report['results'])
    for result in report['results']:
        itausa = result['identity']['ticker'].startswith('ITSA')
        assert len(result['price_window']['required_regimes']) == (6 if itausa else 2)
        assert result['cash_rows'] == (105 if itausa else 62)
        assert not result['cash_history_complete']
        assert not result['share_basis_verified']
    assert len(report['widget_configs']) == 2
    assert all(w['public_history_template_present'] for w in report['widget_configs'])
    assert all(not w['raw_or_adjusted_price_basis_documented'] for w in report['widget_configs'])
    assert all(c['before_sha256'] == c['after_sha256'] for c in report['protected_state'])
    if report['stops']:
        with pytest.raises(ValueError, match='recorded_scope_stop_no_further_network'):
            research.Fetcher(root).guard(SCRIPT)
