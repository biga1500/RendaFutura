import hashlib
import io
import json
import socket
import urllib.error

import pytest
from protego import Protego

from . import selected_issuer_sources as research
from .dividend_scope import import_scope


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Live network forbidden'))


def configure(tmp_path):
    source = tmp_path / 'selection.tsv'
    source.write_text('Papel\tDiv.Yield\nCLSC4\t1,00%\nCLSC3\t0,00%\n', encoding='utf-8')
    import_scope(tmp_path, source)


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def mock_wire(monkeypatch, responses):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(research.urllib.request, 'build_opener', lambda *a, **k: Opener())
    return calls


@pytest.mark.parametrize('url', ['https://www.b3.com.br/data', 'https://cdn-sites-assets.mziq.com/a.js',
    'https://api.mziq.com/login', 'https://api.mziq.com/a?api_key=secret',
    'https://api.mziq.com/a?signature=secret', 'http://ri.celesc.com.br/',
    'https://account:secret@api.mziq.com/a'])
def test_unapproved_or_credential_source_never_requested(tmp_path, monkeypatch, url):
    configure(tmp_path)
    calls = mock_wire(monkeypatch, [])
    with pytest.raises(ValueError, match='unapproved'):
        research.acquire(tmp_path, url)
    assert not calls


def test_longest_robots_rule_preserves_theme_exception_without_allowing_admin():
    parser = Protego.parse('User-agent: *\nAllow: /\nDisallow: /wp-admin/\nDisallow: /wp-content/\nAllow: /wp-content/themes/example/\n')
    assert parser.can_fetch('https://ri.celesc.com.br/wp-content/themes/example/main.js', research.USER_AGENT)
    assert not parser.can_fetch('https://ri.celesc.com.br/wp-admin/admin-ajax.php', research.USER_AGENT)
    assert not parser.can_fetch('https://ri.celesc.com.br/wp-content/private.js', research.USER_AGENT)


def test_actual_robots_path_denial_precedes_request(tmp_path, monkeypatch):
    configure(tmp_path)
    calls = mock_wire(monkeypatch, [Response(b'User-agent: *\nDisallow: /servicos-aos-investidores/\n')])
    research.acquire(tmp_path, 'https://ri.celesc.com.br/robots.txt')
    with pytest.raises(ValueError, match='disallowed_by_robots'):
        research.acquire(tmp_path, research.SEEDS['celesc-dividends'])
    assert len(calls) == 1


@pytest.mark.parametrize('status', [401, 403, 429, 302])
def test_access_or_redirect_retains_original_and_stops_same_host(tmp_path, monkeypatch, status):
    configure(tmp_path)
    calls = mock_wire(monkeypatch, [Response(b'User-agent: *\nAllow: /'),
        urllib.error.HTTPError(research.SEEDS['celesc-dividends'], status, 'stop',
                              {'Location': 'https://api.mziq.com/alternate'}, io.BytesIO(b'original refusal'))])
    research.acquire(tmp_path, 'https://ri.celesc.com.br/robots.txt')
    result = research.acquire(tmp_path, research.SEEDS['celesc-dividends'])
    assert result['status'] == status and result['stop']
    assert research.source_body(tmp_path, result) == b'original refusal'
    with pytest.raises(ValueError, match='previously_stopped'):
        research.acquire(tmp_path, research.SEEDS['celesc-quotes'])
    assert len(calls) == 2


def test_repeated_or_interrupted_request_never_retried(tmp_path, monkeypatch):
    configure(tmp_path)
    calls = mock_wire(monkeypatch, [Response(b'User-agent: *\nAllow: /')])
    url = 'https://ri.celesc.com.br/robots.txt'
    research.acquire(tmp_path, url)
    with pytest.raises(ValueError, match='already_attempted'):
        research.acquire(tmp_path, url)
    base = tmp_path / research.CACHE
    (base / 'request-002.json').write_text(json.dumps({'ordinal': 2, 'url': research.SEEDS['celesc-quotes']}))
    with pytest.raises(ValueError, match='interrupted_or_inconsistent'):
        research.acquire(tmp_path, research.SEEDS['celesc-quotes'])
    assert len(calls) == 1


def test_incomplete_body_fails_without_followup(tmp_path, monkeypatch):
    configure(tmp_path)
    mock_wire(monkeypatch, [Response(b'User-agent: *\nAllow: /'), Response(b'short', headers={'Content-Length': '999'})])
    research.acquire(tmp_path, 'https://ri.celesc.com.br/robots.txt')
    result = research.acquire(tmp_path, research.SEEDS['celesc-dividends'])
    assert result['stop'] == 'incomplete_source_body'


def test_unrecorded_or_private_parent_reference_not_read(tmp_path, monkeypatch):
    configure(tmp_path)
    calls = mock_wire(monkeypatch, [])
    parent = {'file': '../private-source', 'status': 200, 'stop': None}
    with pytest.raises(ValueError, match='not_in_actual_ledger'):
        research.acquire(tmp_path, 'https://api.mziq.com/document', parent=parent)
    assert not calls
    with pytest.raises(ValueError, match='unsafe_issuer_original_reference'):
        research.source_body(tmp_path, parent)


def test_json_catalogue_links_are_read_from_structured_fields_not_text():
    value = {'data': {'document_metas': [{'file_url': 'https://filemanager-cdn.mziq.com/example.xlsx',
        'file_title': 'History', 'description': 'https://api.mziq.com/unrelated'}]}}
    refs = research.links(json.dumps(value).encode(), 'https://api.mziq.com/catalogue')
    assert len(refs) == 1 and refs[0]['locator'] == '$.data.document_metas[0].file_url'


def test_real_cached_sources_preserve_pointers_and_pin_published_templates():
    root = research.Path(__file__).parent / 'data/market'
    pointers = {str(path): path.read_bytes() for path in (root / 'current.json', root / 'inventory/current.json')}
    base, requests, responses = research.ledger(root)
    assert len(requests) == len(responses) == research.MAX_REQUESTS == 12
    assert not any(row['stop'] for row in responses)
    for receipt in responses:
        assert hashlib.sha256(research.source_body(root, receipt)).hexdigest() == receipt['sha256']
    url, data = research.catalog_request(root, responses[1], responses[3])
    assert data == {'categoryInternalNames': ['dividendos'], 'language': 'pt_BR', 'published': True}
    assert url == requests[6]['url']
    assert research.history_request(root, responses[2], responses[8]) == requests[11]['url']
    assert all(path.read_bytes() == pointers[str(path)] for path in (root / 'current.json', root / 'inventory/current.json'))
