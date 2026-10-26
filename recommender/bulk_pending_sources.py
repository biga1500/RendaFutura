"""Polite, immutable public-source acquisition for the selected pending codes.

Acquisition is evidence collection only, never model admission or confirmation
of settlement. No authentication, automatic redirects, retries or paid APIs.
"""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit
import urllib.error
import urllib.request

from protego import Protego


CACHE = 'raw/primary-research/all-pending'
USER_AGENT = 'RendaFutura-Academic/1.0'
MAX_BYTES = 16_000_000
MAX_REQUESTS = 200
DENIED = ('b3.com.br', 'bvmf.com.br', 'bmfbovespa.com.br', 'bovespa.com.br',
          'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br',
          'www.engie.com.br', 'cdn-sites-assets.mziq.com')


def approved(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username
                or parsed.password or parsed.fragment or '.' not in host
                or host.endswith(('.', '.local', '.localhost', '.internal'))):
            return False
        try:
            ipaddress.ip_address(host)
            return False
        except ValueError:
            pass
        decoded = unquote(unquote(url)).lower()
        return (not any(host == d or host.endswith('.' + d) for d in DENIED)
                and not re.search(r'(?i)(?:login|signin|signup|captcha|oauth|password|proxy)', decoded)
                and not any(re.search(r'(?i)token|secret|password|auth|api.?key|signature', key)
                            for key, _ in parse_qsl(parsed.query)))
    except (TypeError, ValueError):
        return False


def _save(path, value):
    body = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=True, indent=2, allow_nan=False).encode()
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_public_source_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def _safe(root, path):
    return (path.resolve().is_relative_to(root)
            and all(not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)()
                    for p in (path, *path.parents) if p.is_relative_to(root)))


def _stopped(value):
    if not isinstance(value, dict) or not isinstance(value.get('url'), str):
        return False
    status = value.get('status', value.get('http_status', value.get('httpStatus')))
    reason = str(value.get('error', '')) + ' ' + str(value.get('stop', ''))
    return (status in (401, 403, 429) or value.get('robots_allowed') is False
            or bool(re.search(r'(?i)captcha|challenge|robots.*disallow|HTTP.?403|HTTP.?429|HTTP.?401', reason)))


class PublicSourceCollector:
    def __init__(self, root, scope):
        self.root = Path(root).resolve()
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', scope):
            raise ValueError('invalid_pending_source_scope')
        self.base = self.root / CACHE / scope
        self.base.mkdir(parents=True, exist_ok=True)
        if not _safe(self.root, self.base):
            raise ValueError('unsafe_pending_source_folder')
        self.prior_stops = set()
        self._index_prior_stops()

    def _index_prior_stops(self):
        # Only public research metadata, never private projects or credentials.
        for path in (self.root / 'raw/primary-research').rglob('*.json'):
            if path.stat().st_size > 2_000_000 or not _safe(self.root, path):
                continue
            try:
                value = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            records = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
            if isinstance(value, dict):
                for key in ('responses', 'requests', 'results', 'sources'):
                    if isinstance(value.get(key), list):
                        records.extend(value[key])
            for record in records:
                if _stopped(record):
                    self.prior_stops.add(urlsplit(record['url']).hostname)

    def body(self, receipt):
        path = self.root / receipt['file']
        if not _safe(self.root, path) or path.stat().st_size > MAX_BYTES + 1:
            raise ValueError('unsafe_pending_original')
        content = path.read_bytes()
        if (len(content) != receipt['bytes']
                or hashlib.sha256(content).hexdigest() != receipt['sha256']):
            raise ValueError('pending_original_changed')
        return content

    def _cached(self, url):
        name = hashlib.sha256(url.encode()).hexdigest()
        path = self.base / ('response-' + name + '.json')
        if path.is_file():
            receipt = json.loads(path.read_bytes())
            if receipt.get('url') != url:
                raise ValueError('pending_cached_receipt_URL_mismatch')
            self.body(receipt)
            return receipt
        if (self.base / ('request-' + name + '.json')).exists():
            raise ValueError('interrupted_public_request_no_retry')
        return None

    def fetch(self, url, discovery=None):
        if not approved(url):
            raise ValueError('unapproved_pending_public_source')
        existing = self._cached(url)
        if existing:
            return existing
        host = urlsplit(url).hostname
        if host in self.prior_stops:
            raise ValueError('previously_stopped_origin_no_retry')
        is_robots = urlsplit(url).path == '/robots.txt'
        if not is_robots:
            robots = self.fetch('https://' + host + '/robots.txt', discovery={'kind': 'robots_policy'})
            if robots['status'] == 200 and not robots['error']:
                policy = Protego.parse(self.body(robots).decode('utf-8', errors='replace'))
                if not policy.can_fetch(url, USER_AGENT):
                    self.prior_stops.add(host)
                    raise ValueError('pending_public_source_disallowed_by_robots')
                delay = max(1, policy.crawl_delay(USER_AGENT) or 0)
                rate = policy.request_rate(USER_AGENT)
                if rate:
                    delay = max(delay, rate.seconds / rate.requests)
                if delay > 60:
                    raise ValueError('pending_source_rate_requires_later_run')
                time.sleep(delay)
            elif robots['status'] != 404 or robots['error']:
                raise ValueError('pending_robots_policy_unavailable_stop')
        if len(list(self.base.glob('request-*.json'))) >= MAX_REQUESTS:
            raise ValueError('pending_public_request_budget_exhausted')
        for path in (self.root / CACHE).glob('*/response-*.json'):
            prior = json.loads(path.read_bytes())
            if urlsplit(prior['url']).hostname == host and _stopped(prior):
                self.prior_stops.add(host)
                raise ValueError('pending_origin_stopped_in_another_group')
        identity = hashlib.sha256(url.encode()).hexdigest()
        started = datetime.now(timezone.utc).isoformat()
        request = {'url': url, 'started_at': started, 'discovery': discovery,
                   'scope': self.base.name, 'retries': 0, 'maximum_bytes': MAX_BYTES}
        _save(self.base / ('request-' + identity + '.json'), request)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        response, content, status, error, headers = None, b'', None, None, {}
        try:
            try:
                response = urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(
                    url, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}), timeout=25)
            except urllib.error.HTTPError as failure:
                response = failure
            status = response.status
            headers = dict(response.headers.items())
            content = response.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                error = 'response_size_limit'
            elif status in (401, 403, 429):
                error = 'access_stop_no_retry'
            elif 300 <= status < 400:
                error = 'redirect_recorded_not_automatically_followed'
            elif status != 200 and not (is_robots and status == 404):
                error = 'non_success_no_retry'
            elif re.search(rb'(?is)<title[^>]*>[^<]*(?:captcha|access denied|sign in|just a moment)', content):
                error = 'public_content_challenge_stop'
        except Exception as failure:
            error = type(failure).__name__
        finally:
            if response is not None:
                response.close()
        digest = hashlib.sha256(content).hexdigest()
        filename = 'body-' + digest + '.bin'
        _save(self.base / filename, content)
        result = {**request, 'status': status, 'error': error, 'bytes': len(content),
                  'file': (self.base / filename).relative_to(self.root).as_posix(), 'sha256': digest,
                  'retrieved_at': datetime.now(timezone.utc).isoformat(), 'published_at': None,
                  'headers': headers, 'location': headers.get('Location') or headers.get('location'),
                  'bank_settlement_verified': False, 'model_admission': False}
        _save(self.base / ('response-' + identity + '.json'), result)
        if _stopped(result):
            self.prior_stops.add(host)
        return result
