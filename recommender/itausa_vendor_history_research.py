"""Itausa/Ferbasa public-widget research, offline unless --fetch is explicit.

This separate scope is authorized after the old collector stopped. It never
changes that collector or uses it for networking. Every new denial stops this
scope, including other allowed hosts. Only literal public links are fetched.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import unquote, urljoin, urlsplit

from . import alternate_issuer_history_research as prior


CACHE = 'raw/primary-research/itausa-vendor-history'
MAX_REQUESTS = 14
MAX_COMBINED = 20
ACCESS_STOP = (401, 403, 429)
REDIRECTS = (301, 302, 303, 307, 308)
REPORT_NAME = ('raw/primary-research/alternate-issuer-history/'
    'report-eba0aaa2ba61dc7403509050f1a7703089157423222b6dfc395aec5f58897ba3.json')
REPORT_SHA = 'eba0aaa2ba61dc7403509050f1a7703089157423222b6dfc395aec5f58897ba3'
IDENTITIES = {t: prior.IDENTITIES[t] for t in ('ITSA3', 'ITSA4', 'FESA3', 'FESA4')}
# Each CDN is literally linked from one of the two cached official quote pages.
HOSTS = frozenset(('ri.itausa.com.br', 'www.ferbasa.com.br',
                  'cdn-sites-assets.mziq.com', 'cdn.mziq.com'))
DENIED_WORDS = (*prior.DENIED, 'engie', 'egie3', 'yahoo', 'yfinance', 'proxy')
AUTH_PATH = re.compile(r'(?:^|[/?.&=_-])(auth|login|signin|sign-in|signup|sign-up|'
                      r'wp-login|wp-signup|register|oauth|authorize|authorization|'
                      r'password|private|token|api[-_]?key)(?:$|[/?.&=_-])', re.I)
# Actual before-work byte hashes captured for this focused continuation.
PROTECTED_BEFORE = {
    'current.json': '3263e42556e2eadb9afd1fa5e9d45cbd382d456b6d5fa315c6992b449e200ba4',
    'inventory/current.json': 'e7dae0117387ab9a47edfb5ab53c5cfdafc2541ef588128c962e4f0f7c169b9d',
    'raw/primary-research/registration/current.json':
        '35d5fd979244a42b22f2367e386b2b7841e33713d662ae16dd3a3c6ae90125d7',
    'raw/primary-research/macro/current.json':
        '58acbaa39a39b45ddbf6d7ab57c66999c7e7ce2c7e9748091917dbbfa2aef280',
}
PRIOR_HELPER_SHA = '30f5e67bd9b5b9247194dde937ce9cfa33d4fb083b6df95471f423709d36c434'


def allowed(url):
    try:
        parts = urlsplit(url)
        decoded = unquote(unquote(url)).lower()
        return (parts.scheme == 'https' and parts.hostname in HOSTS
            and parts.port in (None, 443) and not parts.username and not parts.password
            and not parts.fragment and not any(w in decoded for w in DENIED_WORDS)
            and not AUTH_PATH.search(decoded))
    except ValueError:
        return False


def save(root, prefix, body, extension='json'):
    if not re.fullmatch(r'[a-z0-9-]+', prefix) or extension not in ('json', 'bin', 'csv', 'md'):
        raise ValueError('unsafe_output_name')
    name = f'{CACHE}/{prefix}-{prior.sha(body)}.{extension}'
    path = prior.safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def ledger(root, cache, prefix):
    result = []
    for path in sorted((Path(root) / cache).glob(prefix + '-*.json')):
        body = path.read_bytes()
        if path.name != f'{prefix}-{prior.sha(body)}.json':
            raise ValueError('request_ledger_hash_mismatch')
        result.append(json.loads(body))
    ordinals = [r['ordinal'] for r in result]
    if sorted(ordinals) != list(range(1, len(result) + 1)):
        raise ValueError('nonconsecutive_or_duplicate_request_ledger')
    return sorted(result, key=lambda r: r['ordinal'])


def load_prior(root):
    reader = prior.Reader(root)
    body = reader.read({'file': REPORT_NAME, 'sha256': REPORT_SHA}, 'prior_hash_bound_review')
    return reader, json.loads(body)


def links(reader, source):
    # Never enumerate the reader's receipt scanner or private reference files.
    return [{**link, 'network_permitted': allowed(link['url'])} for link in
        prior.discover(reader.read(source, 'literal_public_widget_discovery'), source)]


def protected_state(root):
    checks = [{'file': name, 'before_sha256': expected,
               'after_sha256': prior.sha((Path(root) / name).read_bytes())}
              for name, expected in PROTECTED_BEFORE.items()]
    checks.append({'file': 'recommender/alternate_issuer_history_research.py',
        'before_sha256': PRIOR_HELPER_SHA,
        'after_sha256': prior.sha(Path(prior.__file__).read_bytes())})
    if any(row['before_sha256'] != row['after_sha256'] for row in checks):
        raise ValueError('protected_pointer_or_prior_helper_changed')
    return checks


class Fetcher:
    """Append-only, no cookies/auth/proxies/retries, explicit redirect handling."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.reader, self.baseline = load_prior(root)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), prior.NoRedirect())

    def history(self, prefix):
        return ledger(self.root, CACHE, prefix)

    def prior_history(self, prefix):
        return ledger(self.root, prior.CACHE, prefix)

    def cached(self, url):
        # The denied issuer's body is never opened, even when sharing a ledger.
        if not allowed(url):
            raise ValueError('forbidden_or_unallowlisted_no_request_sent')
        matches = [r for r in self.prior_history('response') + self.history('response') if r['url'] == url]
        if not matches:
            return None
        receipt = matches[-1]
        return self.reader.read(receipt, 'cached_public_HTTP_original'), receipt

    def guard(self, url):
        if not allowed(url):
            raise ValueError('forbidden_or_unallowlisted_no_request_sent')
        previous = self.prior_history('request')
        responses = self.prior_history('response')
        if len(previous) != 6 or len(responses) != 6:
            raise ValueError('prior_six_request_baseline_changed')
        if responses != self.baseline['requests'] or any(
                any(response.get(k) != v for k, v in request.items())
                for request, response in zip(previous, responses)):
            raise ValueError('prior_request_baseline_mismatch')
        current = self.history('request')
        current_responses = self.history('response')
        if any((self.root / CACHE).glob('stop-*.json')):
            raise ValueError('recorded_scope_stop_no_further_network')
        if len(current) != len(current_responses):
            raise ValueError('incomplete_attempt_requires_offline_review_no_retry')
        denials = [r for r in responses if allowed(r['url'])] + current_responses
        if any(r['http_status'] in ACCESS_STOP for r in denials):
            raise ValueError('access_stop_no_further_network_or_alternate_route')
        if url in {r['url'] for r in current}:
            raise ValueError('already_attempted_no_retry')
        if len(current) >= MAX_REQUESTS or len(previous) + len(current) >= MAX_COMBINED:
            raise ValueError('public_request_budget_exhausted')
        if any(r['url'] == url for r in previous):
            old = next(r for r in responses if r['url'] == url)
            if not (url == 'https://cdn-sites-assets.mziq.com/robots.txt'
                    and old['http_status'] == 302 and not old.get('location')):
                raise ValueError('prior_attempt_no_retry')
        return len(current) + 1

    def request(self, url, discovery):
        ordinal = self.guard(url)
        attempt = {'ordinal': ordinal, 'combined_ordinal': 6 + ordinal, 'url': url,
            'started_at': datetime.now(timezone.utc).isoformat(), 'discovery': discovery,
            'automatic_redirects': False, 'automatic_retries': 0, 'proxy': False,
            'authentication': 'none', 'method': 'GET'}
        save(self.root, 'request', prior.dump(attempt))
        body, status, error, headers = b'', None, None, {}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': prior.USER_AGENT,
                'Accept': '*/*', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                status, headers = response.status, response.headers
                body = response.read(prior.MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, error, headers = exc.code, 'HTTPError', exc.headers
            body = exc.read(prior.MAX_BYTES + 1)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        receipt = {**attempt, 'http_status': status, 'error': error,
            'content_type': headers.get('Content-Type'), 'location': headers.get('Location'),
            'http_date': headers.get('Date'), 'last_modified': headers.get('Last-Modified'),
            'etag': headers.get('ETag'), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'bytes': len(body), 'sha256': prior.sha(body), 'file': save(self.root, 'body', body, 'bin')}
        save(self.root, 'response', prior.dump(receipt))
        if status in ACCESS_STOP:
            raise ValueError(f'access_stop_HTTP_{status}_no_further_network')
        if len(body) > prior.MAX_BYTES:
            raise ValueError('response_size_limit')
        return body, receipt

    def follow(self, url, discovery, refresh_missing_location=False):
        chain, seen = [], set()
        for _ in range(5):
            if url in seen or not allowed(url):
                raise ValueError('redirect_loop_auth_or_unallowlisted_stop')
            seen.add(url)
            cached = None if refresh_missing_location else self.cached(url)
            refresh_missing_location = False
            body, receipt = cached if cached else self.request(url, discovery)
            chain.append(receipt)
            if receipt['http_status'] in ACCESS_STOP:
                raise ValueError('cached_access_denial_stop')
            if receipt['http_status'] not in REDIRECTS:
                return body, receipt, chain
            location = receipt.get('location')
            if not location:
                raise ValueError('redirect_location_missing_stop')
            target = urljoin(url, location)
            if not allowed(target):
                raise ValueError('redirect_auth_or_unallowlisted_stop')
            # Redirects may remain on the originating CDN or named issuer only.
            if urlsplit(target).hostname != urlsplit(url).hostname and urlsplit(target).hostname not in (
                    'ri.itausa.com.br', 'www.ferbasa.com.br'):
                raise ValueError('redirect_not_same_official_CDN_or_issuer_stop')
            discovery = {'role': 'ordinary_public_redirect', 'from_url': url,
                'source': receipt, 'literal_location': location}
            url = target
        raise ValueError('redirect_hop_limit_stop')

    def get(self, url, source):
        if not allowed(source.get('url', '')) or source.get('http_status', 200) != 200:
            raise ValueError('successful_allowlisted_discovery_source_required')
        found = [link for link in links(self.reader, source) if link['url'] == url]
        if not found or not allowed(url):
            raise ValueError('literal_allowlisted_public_link_required')
        if not any(source == r for r in self.prior_history('response') + self.history('response')):
            raise ValueError('recorded_original_response_discovery_required')
        robots_url = 'https://' + urlsplit(url).netloc + '/robots.txt'
        cached = self.cached(robots_url)
        refresh = bool(cached and cached[1]['http_status'] == 302 and not cached[1].get('location'))
        robots_body, robots, chain = self.follow(robots_url,
            {'role': 'robots_for_literal_public_widget', 'link': found[0],
             'prior_302_missing_location_refresh': refresh}, refresh_missing_location=refresh)
        if robots['http_status'] == 200:
            if 'html' in (robots.get('content_type') or '').lower() or robots_body.lstrip().startswith(b'<'):
                raise ValueError('robots_redirect_returned_HTML_policy_unknown_stop')
            policy = urllib.robotparser.RobotFileParser()
            policy.parse(robots_body.decode('utf-8-sig', errors='replace').splitlines())
            if not policy.can_fetch(prior.USER_AGENT, url):
                raise ValueError('robots_disallows_no_request_sent')
        elif robots['http_status'] not in (404, 410):
            raise ValueError('robots_unavailable_stop')
        return self.follow(url, {'role': 'literal_public_widget', 'link': found[0],
                                'robots_chain': chain})


def build_report(root):
    """Read hashed originals; retain unresolved prices, cash and regime gates."""
    fetcher = Fetcher(root)
    reader, baseline = fetcher.reader, fetcher.baseline
    results = []
    for item in baseline['results']:
        ticker = item['ticker']
        if ticker not in IDENTITIES:
            continue
        identity = {'ticker': ticker, **item['expected_identity']}
        prior.exact_identity(identity)
        source = next(s for s in baseline['sources'] if
                      s['file'] == f'raw/inventory/daily-prices/{ticker}.json')
        window = prior.inspect_price_window(reader.read(source, 'cached_Yahoo_comparison_only'), ticker)
        cash_source = item['native_cash']['source']
        cash = prior.calendars.inspect_calendar(reader.read(cash_source, 'native_full_cash_calendar'),
            IDENTITIES[ticker][3], ticker, prior.CUTOFF, cash_source)
        if len(cash['events']) != item['cash_rows']:
            raise ValueError('cached_native_cash_row_count_changed')
        results.append({'identity': identity, 'price_window': window,
            'cash_rows': len(cash['events']), 'cash_source': cash_source,
            'cash_issues': dict(Counter(i for event in cash['events'] for i in event['issues'])),
            'cash_history_complete': False, 'share_basis_verified': False,
            'independent_raw_anchors': 0, 'candidate': None, 'model_approved': False,
            'pending': item['pending']})
    responses = fetcher.history('response')
    attempts = fetcher.history('request')
    originals, discoveries = [], []
    widget_configs = []
    for source in baseline['requests'] + responses:
        if not allowed(source['url']):
            continue
        body = reader.read(source, 'public_widget_HTTP_original')
        originals.append(source)
        if source['http_status'] == 200 and any(t in (source.get('content_type') or '')
                for t in ('html', 'javascript', 'text/plain')):
            discoveries.extend(links(reader, source))
        if source['http_status'] == 200 and 'html' in (source.get('content_type') or ''):
            text = body.decode('utf-8-sig')
            # Literal public UI options only; do not evaluate JS or invent an API contract.
            if 'MZIQ_StockInfo' in text:
                widget_configs.append({'source': source,
                    'public_widget_ids': sorted(set(re.findall(
                        r'''(?:stockinfoId\s*:|stockInfoId\s*=)\s*["']([a-f0-9-]{36})["']''', text))),
                    'literal_tickers': sorted(set(re.findall(r'''tickers:\s*['"](ITSA[34]|FESA[34])['"]''', text))),
                    'public_history_template_present': bool(re.search(r'''type:\s*['"]history['"]''', text)),
                    'literal_history_columns': re.findall(r'columns:\s*(\[[^\]]+\])', text),
                    'observed_vendor': 'MZ / MZIQ_StockInfo',
                    'raw_or_adjusted_price_basis_documented': False,
                    'upstream_independence_from_Yahoo_documented': False,
                    'public_widget_id_is_not_authentication': True})
    blockers = []
    for response in responses:
        if response['http_status'] in REDIRECTS:
            target = urljoin(response['url'], response.get('location') or '')
            if allowed(target) and any(r['url'] == target for r in responses):
                continue
            blockers.append({'code': 'robots_redirect_to_unallowlisted_CMS_signup' if
                'wp-signup.php' in target else 'redirect_requires_review',
                'source': response, 'redirect_target': target,
                'target_allowlisted': allowed(target),
                'target_followed': any(r['url'] == target for r in responses),
                'http_access_denial': False,
                'robots_policy_established': False})
        if response['http_status'] in ACCESS_STOP:
            blockers.append({'code': 'HTTP_access_stop', 'source': response,
                             'http_access_denial': True})
    stops = []
    for path in sorted((Path(root) / CACHE).glob('stop-*.json')):
        body = path.read_bytes()
        if path.name != f'stop-{prior.sha(body)}.json':
            raise ValueError('scope_stop_hash_mismatch')
        stops.append(json.loads(body))
    return {'schema_version': 1, 'cutoff': prior.CUTOFF.isoformat(),
        'prior_review': {'file': REPORT_NAME, 'sha256': REPORT_SHA,
            'previously_verified_sources': len(baseline['sources'])},
        'results': results, 'sources': list(reader.sources.values()),
        'discoveries': discoveries, 'widget_configs': widget_configs, 'requests': responses,
        'blockers': blockers, 'protected_state': protected_state(root),
        'new_request_attempts': len(attempts), 'maximum_new_requests': MAX_REQUESTS,
        'prior_request_attempts': len(fetcher.prior_history('request')),
        'combined_request_attempts': 6 + len(attempts), 'maximum_combined_requests': MAX_COMBINED,
        'status_counts': dict(Counter(str(r['http_status']) for r in responses)),
        'stops': stops, 'network_stopped': bool(stops or blockers),
        'unused_new_request_budget': MAX_REQUESTS - len(attempts),
        'history_export_requested': any(re.search(r'export|history', urlsplit(r['url']).path, re.I)
                                        for r in responses),
        'widget_script_requested': any(urlsplit(r['url']).path.endswith('.js') for r in responses),
        'production_price_parser_installed': False,
        'access_stopped': any(r['http_status'] in ACCESS_STOP for r in responses),
        'authority': 'primary_issuer_hosted_secondary_vendor_not_exchange_certified',
        'source_basis': {'widget': 'literal_MZIQ_StockInfo_config_in_cached_official_issuer_HTML',
            'vendor_history': 'not_requested_robots_redirect_to_unallowlisted_CMS_signup',
            'raw_price_basis': 'unknown_no_publisher_documentation_or_export_obtained',
            'Yahoo': 'cached_comparison_only_not_independent_raw_price_proof',
            'cash': 'reparsed_native_full_issuer_calendars_no_settlement_claim',
            'identity': 'exact_CVM_class_ISIN_in_hash_bound_prior_review'},
        'price_basis': 'unresolved_no_raw_export_validated',
        'independent_of_Yahoo': None, 'point_in_time_archive_verified': False,
        'observation_basis': 'retrospective_public_research',
        'settlement_verified': False, 'financial_gates': 'unchanged',
        'active_pointers_changed': False, 'model_admissions': 0,
        'network_scope': sorted(HOSTS), 'denied_origins': list(DENIED_WORDS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='recommender/data/market')
    parser.add_argument('--fetch')
    parser.add_argument('--source-receipt')
    args = parser.parse_args()
    if args.fetch:
        if not args.source_receipt:
            parser.error('--fetch requires --source-receipt')
        source = json.loads(prior.safe_path(args.folder, args.source_receipt).read_bytes())
        try:
            _, receipt, chain = Fetcher(args.folder).get(args.fetch, source)
            print(json.dumps({'receipt': receipt, 'redirect_chain': [r['url'] for r in chain]}))
        except ValueError as exc:
            failure = {'reason': str(exc), 'requested_url': args.fetch,
                       'recorded_at': datetime.now(timezone.utc).isoformat()}
            name = save(args.folder, 'stop', prior.dump(failure))
            print(json.dumps({'stop': failure, 'file': name}))
            raise SystemExit(2)
    else:
        report = build_report(args.folder)
        name = save(args.folder, 'report', prior.dump(report))
        print(json.dumps({'file': name, 'new_requests': report['new_request_attempts'],
            'combined_requests': report['combined_request_attempts'],
            'status_counts': report['status_counts'], 'model_admissions': 0}))


if __name__ == '__main__':
    main()
