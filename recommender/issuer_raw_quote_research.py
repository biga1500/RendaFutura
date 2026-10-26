"""Isolated official-RI quote discovery; offline unless explicitly fetching.

No production imports perform acquisition. Every HTTP attempt, including
robots and redirects, consumes the same persistent twenty-request budget.
Downloaded quote semantics are not inferred from agreement with Yahoo.
"""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
import zipfile

from . import alternate_issuer_history_research as prior


CACHE = 'raw/primary-research/issuer-raw-quotes'
MAX_REQUESTS = 20
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
REVIEW = ('raw/primary-research/price-basis/'
          'review-2026-09-30-89d2fcc002b04f0837413cdc1a8385470e9dcba5f649e0ecb5c395126ba5b073.json')
REVIEW_SHA = '89d2fcc002b04f0837413cdc1a8385470e9dcba5f649e0ecb5c395126ba5b073'
TARGETS = {'ANIM3': 23248, 'FRAS3': 6211, 'MRVE3': 20915, 'DOHL3': 5207,
           'BMEB4': 1325, 'DEXP3': 16632, 'LREN3': 8133, 'PSSA3': 16659,
           'SBSP3': 14443, 'SLCE3': 20745}
DENIED = ('b3.com.br', 'bmfbovespa.com.br', 'bovespa.com.br', 'bvmf.com.br',
          'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br',
          'engie.com.br', 'cdn-sites-assets.mziq.com')
AUTH = re.compile(r'(?:^|[/?.&=_-])(auth|login|signin|sign-in|signup|sign-up|'
                  r'wp-login|wp-signup|register|oauth|authorize|authorization|'
                  r'password|private|token|api[-_]?key)(?:$|[/?.&=_-])', re.I)
PROTECTED = ('current.json', 'inventory/current.json',
             'raw/primary-research/registration/current.json',
             'raw/primary-research/macro/current.json')
HELPERS = ('share_basis.py', 'inventory_candidates.py', 'price_basis_research.py',
           'alternate_issuer_history_research.py', 'itausa_vendor_history_research.py',
           'noncash_price_research.py')
# These complete public homepages contain CAPTCHA only in optional contact
# forms. Preserve the original conservative receipts and record this review.
CONTACT_FORM_REVIEW = frozenset((
    '27ffdee51983eb9653b4e19ab00ee1b015b8fac89bb15ceb3126b5aa414dda78',
    'e5b1cf6608c2acca87fd7a7ede4e2a945bdf7d72fc62612263e39d986be14813'))
BUNDLE_SHA = '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
HISTORY_PINS = {
    'FRAS3': (11, '4642c8d3631a34fb4e55e1ce20f8ed7d45646661903ca967305683557c6f4a2b'),
    'SBSP3': (17, 'bdc27921f145974cdb74ee551bea62b20bad419ea7aa4d24d981f6d5fc844fae'),
}
DAILY_PINS = {
    'FRAS3': '132673b2479cac00c9224a1c2afb6fa8fc4e1db087b7aa2921e6a86418170b14',
    'SBSP3': '1e51e8ea3d6813f0ec2518c776e3e743a1623d6f11fa1f2ea47381c87c52912c',
}
GRAPHIC_PIN = 'b5780a1dfd4d8528cfcfcdb8a5be7eec3ac5c9534ab42df49670f67d3ca3cd37'
SUPPLEMENT_SHA = '037b72a06c1927b7bc2522069a4e235c23ddb13aa4dcc6b0a73bc3a222a8d6cc'
INVENTORY_FILE = 'inventory/snapshots/20261025T091108-61865ff1/market.json'
INVENTORY_SHA = '2953e093a8450ed0a9766dbc480089c1a9300228c3564c1962e149398a61987c'
IPE_PINS = {
    2021: 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7',
    2022: '44e4a5bf88efa6696b9150f4b7fde3693cbf7ec79d511b65a62c0135ac674c5d',
    2023: 'ef1093983df55399b8ec02255eaa822d941ea797f881d00b8f61f23fb63fb1b0',
    2024: '65878657e487ba86b717e93f112a11cf08faf0356ae245ef6e0e72b0fbac80e2',
    2025: '22f38dff778a2b036f4b38a8f3aac2c860c2f58adf6f457784111f3684b28409',
    2026: 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
}
BR_TIME = timezone(timedelta(hours=-3))
WINDOW_START = date(2021, 9, 30)
CUTOFF = date(2026, 9, 30)


def effective_stop(receipt):
    if (receipt.get('stop_reason') == 'auth_captcha_or_access_denial_body'
            and receipt.get('http_status') == 200
            and receipt.get('sha256') in CONTACT_FORM_REVIEW):
        return None
    return receipt.get('stop_reason')


def challenge_body(body):
    if prior.sha(body) in CONTACT_FORM_REVIEW:
        return False
    if re.search(rb'(?i)(cf-chl-|verify you are human|access denied|wp-signup\.php)', body):
        return True
    if re.search(rb'(?is)<title[^>]*>[^<]*(captcha|access denied|just a moment|sign in)', body):
        return True
    # A complete RI page may carry an optional contact form. A standalone
    # CAPTCHA document without its public navigation remains a stop.
    if re.search(rb'(?i)(g-recaptcha|hcaptcha)', body):
        return not (b'<main' in body and b'cotacoes-e-graficos/' in body)
    return False


def allowed(url):
    try:
        p = urlsplit(url)
        decoded = unquote(unquote(url)).lower()
        return (p.scheme in ('https', 'http') and p.hostname and '.' in p.hostname
                and not p.username and not p.password and not p.fragment
                and p.port in (None, 80 if p.scheme == 'http' else 443)
                and not re.fullmatch(r'[\d.]+', p.hostname)
                and not any(word in decoded for word in
                            (*DENIED, 'yahoo', 'brapi', 'proxy', 'bmfbovespa', 'bovespa', 'bvmf'))
                and not re.search(r'(?:^|[.-])b3(?:[.-]|$)', p.hostname)
                and not AUTH.search(decoded))
    except ValueError:
        return False


def read(root, source):
    body = prior.safe_path(root, source['file']).read_bytes()
    if len(body) > prior.MAX_BYTES or prior.sha(body) != source['sha256']:
        raise ValueError('research_source_hash_or_size_mismatch')
    return body


def save(root, prefix, obj, *, raw=False):
    body = obj if raw else prior.dump(obj)
    name = f'{CACHE}/{prefix}-{prior.sha(body)}.' + ('bin' if raw else 'json')
    path = prior.safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def catalog(root):
    """Read-only triage. FCA maps an issuer to its own declared website."""
    source = {'file': FCA, 'sha256': FCA_SHA}
    with zipfile.ZipFile(io.BytesIO(read(root, source))) as z:
        member = 'fca_cia_aberta_geral_2026.csv'
        rows = list(csv.DictReader(io.StringIO(z.read(member).decode('latin1')), delimiter=';'))
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    results = []
    for ticker, cvm in TARGETS.items():
        selected = [(n, r) for n, r in enumerate(rows, 2) if int(r['Codigo_CVM']) == cvm]
        if len(selected) != 1:
            raise ValueError('FCA_issuer_row_not_unique:' + ticker)
        line, row = selected[0]
        page = row['Pagina_Web'].strip()
        url = page if '://' in page else 'https://' + page
        record, = [r for r in review['records'] if r['ticker'] == ticker]
        diag = record['diagnostic']
        results.append({'ticker': ticker, 'cvm': cvm, 'issuer': row['Nome_Empresarial'],
            'cnpj': row['CNPJ_Companhia'], 'url': url, 'reported_webpage': page,
            'scheme_policy': 'retain_explicit_scheme_else_HTTPS',
            'file': FCA, 'sha256': FCA_SHA, 'member': member, 'csv_line': line,
            'filing_id': row['ID_Documento'], 'review_file': REVIEW, 'review_sha256': REVIEW_SHA,
            'pending_reason': diag['remaining_reason'],
            'existing_quote_dates': [r['date'] for r in diag['anchors']],
            'missing_share_factors': diag['missing_independently_observed_share_factors'],
            'actions_diagnostic_only': diag['share_actions_reported_by_original_Yahoo']})
    return results


def protected(root):
    result = {name: prior.sha((Path(root) / name).read_bytes()) for name in PROTECTED}
    for name in HELPERS:
        path = Path(__file__).parent / name
        result['recommender/' + name] = prior.sha(path.read_bytes())
    return result


def records(root, prefix):
    rows = []
    for path in prior.safe_path(root, CACHE).glob(prefix + '-*.json'):
        body = path.read_bytes()
        if path.name != f'{prefix}-{prior.sha(body)}.json':
            raise ValueError('research_receipt_hash_mismatch')
        rows.append(json.loads(body))
    return sorted(rows, key=lambda r: r['ordinal'])


def links(root, receipt):
    body = read(root, receipt)
    result = prior.discover(body, receipt)
    if receipt.get('location'):
        result.append({'url': urljoin(receipt['url'], receipt['location']),
                       'label': 'HTTP Location', 'locator': 'response.headers.Location',
                       'discovered_in': receipt['file'], 'discovery_sha256': receipt['sha256']})
    for link in result:
        link['network_permitted'] = bool(allowed(link['url']))
    return result


def widget_request(root, ticker, quote_receipt, bundle_receipt, kind='graphic'):
    """Resolve the published JS template, never invent an API path or ticker."""
    page = read(root, quote_receipt).decode('utf-8')
    script = read(root, bundle_receipt).decode('utf-8')
    if bundle_receipt['sha256'] != BUNDLE_SHA or kind not in ('graphic', 'history'):
        raise ValueError('unreviewed_widget_bundle_or_method')
    if bundle_receipt['url'] not in {r['url'] for r in links(root, quote_receipt)}:
        raise ValueError('bundle_not_linked_by_official_quote_page')
    ids = set(re.findall(r'''(?:stockInfoId|stockinfoId)\s*[:=]\s*["']([0-9a-f-]{36})["']''', page))
    tickers = set(re.findall(r"tickers:\s*'([A-Z0-9,]+)'", page))
    if len(ids) != 1 or tickers != {ticker} or quote_receipt['ticker'] != ticker:
        raise ValueError('exact_widget_ticker_and_issuer_required')
    base, = re.findall(r'API_STOCKINFO_URL:"(https://[^\"]+)"', script)
    formula = ('t.API_URL+"/"+t.STOCK_ID+"/' + kind + '?from="')
    for literal in (formula, 't.ADJUSTED=!1', 'noProvents:"No"',
                    'yesWithProvents:"Yes, including dividends"',
                    '"&tickers="+t.TICKER', '"&adjusted="+t.ADJUSTED'):
        if literal not in script:
            raise ValueError('published_widget_formula_changed')
    url = f'{base}/{next(iter(ids))}/{kind}?from=2021-09-01&to=2026-09-30&tickers={ticker}&adjusted=false'
    return url, {'role': 'published_widget_JS_template_resolution',
        'official_quote_page': quote_receipt, 'bundle': bundle_receipt,
        'formula': formula, 'formula_utf8_character_offset': script.index(formula),
        'parameters': {'from': '2021-09-01', 'to': '2026-09-30',
                       'tickers': ticker, 'adjusted': False},
        'parameter_basis': 'published_date_controls_and_unadjusted_default; exact_issuer_widget_id_and_ticker',
        'no_guessed_path_or_authentication': True}


class Fetcher:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), prior.NoRedirect())

    def baseline(self):
        files = list(prior.safe_path(self.root, CACHE).glob('baseline-*.json'))
        if len(files) > 1:
            raise ValueError('ambiguous_baseline')
        if not files:
            save(self.root, 'baseline', {'protected': protected(self.root), 'triage': catalog(self.root)})
        else:
            body = files[0].read_bytes()
            if files[0].stem != 'baseline-' + prior.sha(body):
                raise ValueError('baseline_hash_mismatch')
            old, now = json.loads(body)['protected'], protected(self.root)
            changes = {name: {'before': digest, 'observed': now[name]}
                       for name, digest in old.items() if digest != now[name]}
            if changes:
                save(self.root, 'external-state-observation', {
                    'changes': changes, 'research_writes_to_these_paths': False,
                    'basis': 'concurrent_parent_state; research retains pinned FCA and prior review'})
                if any(name.startswith('recommender/') for name in changes):
                    raise ValueError('prior_helper_changed_review_before_fetch')
        save(self.root, 'classification-review', {
            'body_sha256s': sorted(CONTACT_FORM_REVIEW),
            'original_receipts_preserved': True, 'requests_retried': 0,
            'finding': 'Complete public issuer homepages; g-recaptcha belongs to optional contact/signup forms; no public-content challenge.'})

    def request(self, url, discovery, ticker):
        if not allowed(url) or ticker not in TARGETS:
            raise ValueError('forbidden_URL_no_request')
        self.baseline()
        if list(prior.safe_path(self.root, CACHE).glob('seal-*.json')):
            raise ValueError('research_acquisition_sealed_offline_only')
        attempts, responses = records(self.root, 'request'), records(self.root, 'response')
        if [r['ordinal'] for r in attempts] != list(range(1, len(attempts) + 1)):
            raise ValueError('invalid_request_ordinals')
        if len(attempts) != len(responses):
            raise ValueError('interrupted_attempt_no_network_retry')
        if any(effective_stop(r) and r['ticker'] == ticker for r in responses):
            raise ValueError('issuer_chain_stopped_no_alternate_route')
        host = urlsplit(url).hostname
        if any(effective_stop(r) and urlsplit(r['url']).hostname == host for r in responses):
            raise ValueError('origin_stopped_no_retry')
        for path in prior.safe_path(self.root, CACHE).glob('blocked-*.json'):
            block = json.loads(path.read_bytes())
            if block['ticker'] == ticker or urlsplit(block['url']).hostname == host:
                raise ValueError('recorded_robots_block_no_alternate_route')
        if url in {r['url'] for r in attempts}:
            raise ValueError('already_attempted_no_retry')
        if len(attempts) >= MAX_REQUESTS:
            raise ValueError('twenty_request_budget_exhausted')
        attempt = {'ordinal': len(attempts) + 1, 'url': url, 'ticker': ticker,
                   'discovery': discovery, 'started_at': datetime.now(timezone.utc).isoformat(),
                   'redirects': False, 'retries': 0, 'proxy': False, 'authentication': 'none'}
        save(self.root, 'request', attempt)
        body, status, headers, error = b'', None, {}, None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': prior.USER_AGENT,
                'Accept': '*/*', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                status, headers = response.status, response.headers
                body = response.read(prior.MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, headers, error = exc.code, exc.headers, 'HTTPError'
            body = exc.read(prior.MAX_BYTES + 1)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        location = headers.get('Location')
        stop = None
        if status in (401, 403, 429):
            stop = f'HTTP_{status}'
        elif location and not allowed(urljoin(url, location)):
            stop = 'redirect_to_forbidden_or_auth_URL'
        elif challenge_body(body):
            stop = 'auth_captcha_or_access_denial_body'
        elif len(body) > prior.MAX_BYTES:
            stop = 'response_size_limit'
        elif error and status is None:
            stop = 'transport_error_no_retry'
        receipt = {**attempt, 'http_status': status, 'error': error, 'location': location,
            'content_type': headers.get('Content-Type'), 'http_date': headers.get('Date'),
            'last_modified': headers.get('Last-Modified'), 'etag': headers.get('ETag'),
            'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'bytes': len(body), 'sha256': prior.sha(body), 'stop_reason': stop,
            'file': save(self.root, 'body', body, raw=True)}
        save(self.root, 'response', receipt)
        return body, receipt

    def get(self, url, ticker, source=None):
        if source is None:
            seed, = [r for r in catalog(self.root) if r['ticker'] == ticker]
            if url != seed['url']:
                raise ValueError('exact_FCA_seed_required')
            discovery = seed
        else:
            path = prior.safe_path(self.root, source)
            raw = path.read_bytes()
            if path.name != 'response-' + prior.sha(raw) + '.json':
                raise ValueError('discovery_receipt_not_hash_bound')
            receipt = json.loads(raw)
            if receipt['ticker'] != ticker or effective_stop(receipt):
                raise ValueError('discovery_issuer_or_stop_mismatch')
            matched = [r for r in links(self.root, receipt) if r['url'] == url]
            if not matched:
                raise ValueError('literal_official_source_link_required')
            discovery = {**matched[0], 'receipt': source}
        if not allowed(url):
            raise ValueError('forbidden_URL_no_request')
        return self.get_verified(url, ticker, discovery)

    def get_verified(self, url, ticker, discovery):
        if not allowed(url):
            raise ValueError('forbidden_URL_no_request')
        p = urlsplit(url)
        robots_url = f'{p.scheme}://{p.netloc}/robots.txt'
        old = [r for r in records(self.root, 'response') if r['url'] == robots_url]
        robots, robot_receipt = ((read(self.root, old[0]), old[0]) if old else
            self.request(robots_url, {'role': 'robots_for_documented_origin', 'target': url,
                                      'source': discovery}, ticker))
        if robot_receipt.get('stop_reason'):
            return robots, robot_receipt
        status = robot_receipt['http_status']
        if status == 200:
            policy = urllib.robotparser.RobotFileParser()
            policy.parse(robots.decode('utf-8', errors='replace').splitlines())
            if not policy.can_fetch(prior.USER_AGENT, url):
                save(self.root, 'blocked', {'url': url, 'ticker': ticker,
                    'reason': 'robots_disallows', 'robots': robot_receipt})
                raise ValueError('robots_disallows_no_request')
        elif status not in (404, 410):
            save(self.root, 'blocked', {'url': url, 'ticker': ticker,
                'reason': 'robots_unavailable_redirect_or_error', 'robots': robot_receipt})
            raise ValueError('robots_unavailable_no_redirect_or_fallback')
        if list(prior.safe_path(self.root, CACHE).glob('blocked-*.json')):
            blocks = [json.loads(x.read_bytes()) for x in prior.safe_path(self.root, CACHE).glob('blocked-*.json')]
            if any(x['ticker'] == ticker or urlsplit(x['url']).hostname == p.hostname for x in blocks):
                raise ValueError('recorded_robots_block_no_alternate_route')
        return self.request(url, discovery, ticker)


def verify_chain(root, receipt, visited=None):
    """Offline replay of literal links or the exact pinned public JS formula."""
    visited = set() if visited is None else visited
    key = (receipt['url'], receipt['sha256'])
    if key in visited:
        return
    visited.add(key)
    read(root, receipt)
    if not allowed(receipt['url']) or effective_stop(receipt):
        raise ValueError('stopped_or_forbidden_source_chain')
    discovery = receipt['discovery']
    role = discovery.get('role')
    if role == 'published_widget_JS_template_resolution':
        page, bundle = discovery['official_quote_page'], discovery['bundle']
        verify_chain(root, page, visited)
        verify_chain(root, bundle, visited)
        kind = urlsplit(receipt['url']).path.rsplit('/', 1)[-1]
        expected, actual = widget_request(root, receipt['ticker'], page, bundle, kind)
        if receipt['url'] != expected or discovery != actual:
            raise ValueError('API_formula_or_parameters_not_reproducible')
    elif discovery.get('receipt'):
        raw = prior.safe_path(root, discovery['receipt']).read_bytes()
        if Path(discovery['receipt']).name != 'response-' + prior.sha(raw) + '.json':
            raise ValueError('source_receipt_hash_mismatch')
        parent = json.loads(raw)
        verify_chain(root, parent, visited)
        if (discovery['discovered_in'] != parent['file']
                or discovery['discovery_sha256'] != parent['sha256']
                or receipt['url'] not in {r['url'] for r in links(root, parent)}):
            raise ValueError('unproven_official_literal_link')
    elif discovery.get('file') == FCA:
        seed, = [r for r in catalog(root) if r['ticker'] == receipt['ticker']]
        if discovery != seed or receipt['url'] != seed['url']:
            raise ValueError('official_FCA_origin_mismatch')
    else:
        raise ValueError('unknown_quote_source_chain')


def _reject_constant(value):
    raise ValueError('nonfinite_JSON_number:' + value)


def parse_payload(body, ticker, expected_sha, *, graphic=False):
    """Read raw values only. Decimal strings preserve source numeric precision."""
    if prior.sha(body) != expected_sha:
        raise ValueError('unreviewed_history_body_hash')
    data = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
    if data.get('success') is not True:
        raise ValueError('vendor_history_not_successful')
    data = data['data']['history'] if graphic else data['data']
    if data.get('source') != 'Enfoque' or data.get('link') != 'https://www.enfoque.com.br' or data.get('errors'):
        raise ValueError('vendor_authority_or_history_errors')
    if len(data['tickers']) != 1 or data['tickers'][0].get('ticker') != ticker:
        raise ValueError('exact_vendor_ticker_required')
    rows = data['tickers'][0]['history']
    result, dates = [], set()
    fields = ('var', 'high', 'low', 'open', 'closed', 'middle', 'vol', 'share', 'trade')
    for row in rows:
        if set(row) != {'date', *fields} or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['date']):
            raise ValueError('unreviewed_history_schema')
        day = date.fromisoformat(row['date'])
        if day.isoformat() in dates or not date(2021, 9, 1) <= day <= date(2026, 9, 30):
            raise ValueError('duplicate_or_out_of_range_trading_date')
        dates.add(day.isoformat())
        if any(isinstance(row[k], bool) or not isinstance(row[k], (int, Decimal)) for k in fields):
            raise ValueError('invalid_history_number')
        numbers = {k: Decimal(row[k]) for k in fields}
        if not all(n.is_finite() for n in numbers.values()):
            raise ValueError('nonfinite_history_number')
        if not (0 < numbers['low'] <= numbers['closed'] <= numbers['high']
                and numbers['low'] <= numbers['open'] <= numbers['high']
                and numbers['middle'] > 0 and numbers['share'] > 0
                and numbers['vol'] >= 0 and numbers['trade'] >= 0):
            raise ValueError('invalid_raw_OHLC_or_volume')
        result.append({'date': day.isoformat(), **{k: str(v) for k, v in numbers.items()}})
    return sorted(result, key=lambda r: r['date'])


def pinned_history(root, ticker):
    if ticker not in HISTORY_PINS:
        raise ValueError('ticker_outside_pinned_raw_history')
    ordinal, digest = HISTORY_PINS[ticker]
    receipt, = [r for r in records(root, 'response') if r['ordinal'] == ordinal]
    if receipt['sha256'] != digest or receipt['http_status'] != 200:
        raise ValueError('history_response_pin_mismatch')
    verify_chain(root, receipt)
    page = read(root, receipt['discovery']['official_quote_page'])
    if b"currencySymbol: 'R$'" not in page:
        raise ValueError('issuer_currency_unit_not_verified')
    rows = parse_payload(read(root, receipt), ticker, digest)
    if len(rows) != 1268 or rows[0]['date'] != '2021-09-01' or rows[-1]['date'] != '2026-09-30':
        raise ValueError('pinned_coverage_changed')
    if ticker == 'FRAS3':
        graphic, = [r for r in records(root, 'response') if r['ordinal'] == 10]
        verify_chain(root, graphic)
        if graphic['sha256'] != GRAPHIC_PIN or parse_payload(
                read(root, graphic), ticker, GRAPHIC_PIN, graphic=True) != rows:
            raise ValueError('chart_and_history_raw_values_disagree')
    return rows, receipt


def compare_existing_daily(root, ticker, rows):
    """Diagnostic only: apply the PRIOR reported actions, never fit a factor.

    The prior action list is not primary corporate-action proof. Disagreement
    remains explicit even when most prices happen to agree.
    """
    source = {'file': f'raw/expanded/{ticker}-prices.json', 'sha256': DAILY_PINS[ticker]}
    result = json.loads(read(root, source), parse_float=Decimal)['chart']['result'][0]
    if result['meta']['symbol'] != ticker + '.SA' or result['meta']['currency'] != 'BRL':
        raise ValueError('cached_daily_identity_mismatch')
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date().isoformat(): Decimal(value)
             for stamp, value in zip(result['timestamp'], result['indicators']['quote'][0]['close'])
             if value is not None}
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    old, = [r for r in review['records'] if r['ticker'] == ticker]
    actions = old['diagnostic']['share_actions_reported_by_original_Yahoo']
    regimes, matches, mismatches, missing = {}, 0, [], []
    for row in rows:
        factor = Decimal(1)
        for action in actions:
            if row['date'] < action['date'] <= '2026-09-30':
                factor *= Decimal(str(action['ratio']))
        regimes.setdefault(str(factor), {'first_date': row['date'], 'last_date': row['date'], 'observations': 0})
        regimes[str(factor)]['last_date'] = row['date']
        regimes[str(factor)]['observations'] += 1
        if row['date'] not in daily:
            missing.append(row['date'])
            continue
        expected = Decimal(row['closed']) / factor
        delta = abs(expected - daily[row['date']])
        tolerance = max(Decimal('.015'), Decimal('.006') * abs(expected))
        if delta <= tolerance:
            matches += 1
        else:
            mismatches.append({'date': row['date'], 'independent_raw_close_BRL': row['closed'],
                'prior_reported_action_factor': str(factor), 'raw_divided_by_prior_factor': str(expected),
                'cached_Yahoo_close': str(daily[row['date']]), 'absolute_difference': str(delta),
                'tolerance': str(tolerance)})
    monthly = {}
    for row in rows:
        monthly[row['date'][:7]] = {'date': row['date'], 'raw_close_BRL': row['closed']}
    if len(monthly) != 61:
        raise ValueError('not_61_raw_month_ends')
    return {'source': source, 'prior_action_list_diagnostic_only': actions,
        'primary_action_ratios_verified': False, 'fitted_scalars': [],
        'raw_source_modified_or_adjusted': False, 'daily_matches': matches,
        'daily_mismatches': mismatches, 'missing_cached_daily_dates': missing,
        'daily_mismatches_in_model_window': sum(r['date'] >= '2021-09-30' for r in mismatches),
        'raw_observation_regimes_using_prior_factors': regimes,
        'monthly_raw_anchors': list(monthly.values()),
        'model_admission_granted': False,
        'blocker': 'unresolved_independent_raw_vs_existing_daily_close_disagreement'}


def build_report(root):
    """Entirely offline replay, with every HTTP success/failure in the report."""
    attempts, responses = records(root, 'request'), records(root, 'response')
    if len(attempts) != len(responses) or len(attempts) > MAX_REQUESTS:
        raise ValueError('request_budget_or_receipt_incomplete')
    for n, (attempt, receipt) in enumerate(zip(attempts, responses), 1):
        if attempt['ordinal'] != n or any(receipt.get(k) != v for k, v in attempt.items()):
            raise ValueError('request_response_ledger_disagrees')
        if not allowed(receipt['url']):
            raise ValueError('forbidden_request_in_ledger')
        read(root, receipt)
    proofs = {}
    for ticker in HISTORY_PINS:
        rows, receipt = pinned_history(root, ticker)
        check = compare_existing_daily(root, ticker, rows)
        seed, = [r for r in catalog(root) if r['ticker'] == ticker]
        proofs[ticker] = {'identity': seed, 'status': 'independent_unadjusted_vendor_raw_history_cached',
            'rows': len(rows), 'start': rows[0]['date'], 'end': rows[-1]['date'],
            'raw_daily_observations': rows, 'response': receipt,
            'authority': 'Enfoque via issuer-published MZ Stock Info; vendor assertion, not issuer-originated trading data',
            'Yahoo_independence_basis': 'API explicitly names Enfoque; no Yahoo endpoint/library used for acquisition; upstream supplier audit unavailable',
            'unit': 'BRL per reported trading share; issuer currencySymbol R$, OHLC, vol/share corroboration',
            'date_basis': 'API ISO trading-date field retained literally; not payment/ex/publication dates; no timestamp conversion',
            'price_basis': 'published false default and no-provents option; adjusted=false in exact API request; closed field without transformation',
            'corporate_actions': 'Raw historical trading units retained; no split/bonus/dividend/reinvestment correction applied. API supplies no corporate-action ledger. Primary ratios remain unverified.',
            'source_publication_time': None, 'point_in_time_archive_verified': False,
            'existing_history_diagnostic': check}
    sabesp = {r['date']: r for r in proofs['SBSP3']['raw_daily_observations']}
    arithmetic = []
    for day in ('2026-04-28', '2026-04-29'):
        row = sabesp[day]
        arithmetic.append({'date': day, 'raw_close_BRL': row['closed'],
            'reported_turnover_BRL': row['vol'], 'reported_share_quantity': row['share'],
            'turnover_divided_by_share_quantity': str(Decimal(row['vol']) / Decimal(row['share'])),
            'reported_middle_BRL': row['middle']})
    baseline_path, = prior.safe_path(root, CACHE).glob('baseline-*.json')
    baseline_raw = baseline_path.read_bytes()
    if baseline_path.stem != 'baseline-' + prior.sha(baseline_raw):
        raise ValueError('baseline_hash_mismatch')
    baseline = json.loads(baseline_raw)['protected']
    now = protected(root)
    return {'schema_version': 1, 'cutoff': '2026-09-30', 'acquisition_requests': len(attempts),
        'producer_helper_sha256': prior.sha(Path(__file__).read_bytes()),
        'test_module_sha256': prior.sha(Path(__file__).with_name('test_issuer_raw_quote_research.py').read_bytes()),
        'maximum_requests': MAX_REQUESTS, 'unused_budget': MAX_REQUESTS - len(attempts),
        'requests': responses, 'network_requests_during_report': 0,
        'failed_HTTP_receipts': [r for r in responses if r['http_status'] is None or r['http_status'] >= 400],
        'denied_origins_never_requested': list(DENIED), 'B3_network_requests': 0,
        'BRAPI_network_requests': 0, 'model_admissions': 0, 'production_parser_installed': False,
        'cash_events_created': 0, 'annual_dividend_aggregates_used_as_dated_cash': False,
        'raw_proofs': proofs, 'SBSP_native_share_scale_math': arithmetic,
        'other_researched': {
            'BMEB4': {'status': 'blocked_exact_security_not_published_in_quote_config',
                'reason': 'Official quote/history page configures BMEB3 only; no BMEB4 substitution or shared-class assumption.',
                'source_ordinal': 6},
            'DEXP3': {'status': 'no_history_download_discovered_on_official_homepage',
                'reason': 'Empty stockInfoId; no quote/history link in the cached landing page. Deeper site not exhaustively searched.',
                'source_ordinal': 13},
            'SBSP3_primary_actions': {'status': 'separate_primary_action_ledger_unverified',
                'source_ordinal': 18,
                'reason': 'Notices page uses dynamic filemanager; its page-specific executable is on previously denied cdn-sites-assets.mziq.com. No request or relocated copy attempted.'}},
        'not_network_researched': [t for t in TARGETS if t not in {r['ticker'] for r in responses}],
        'protected_state': {'before': baseline, 'after_observed': now,
            'concurrent_external_changes': [k for k in baseline if baseline[k] != now[k]],
            'research_writes_to_production': False},
        'contact_form_classifier_correction': {'body_sha256s': sorted(CONTACT_FORM_REVIEW),
            'original_conservative_receipts_preserved': True, 'retries': 0,
            'basis': 'public homepage HTML; CAPTCHA belongs to optional contact forms, not public quote access'}}


def local_source(root, relative, digest=None, **metadata):
    """Receipt for a local input, including inventory paths outside raw/."""
    base = Path(root).resolve()
    path = base / relative
    if ('\\' in relative or ':' in relative or '..' in relative.split('/')
            or not path.resolve().is_relative_to(base)
            or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(base))):
        raise ValueError('unsafe_offline_input_path')
    body = path.read_bytes()
    actual = prior.sha(body)
    if digest is not None and actual != digest:
        raise ValueError('offline_input_pin_mismatch')
    return body, {'file': relative, 'sha256': actual, 'bytes': len(body),
                  'access': 'existing_cache_read_only', **metadata}


def cached_yahoo(root, ticker):
    body = read(root, {'file': f'raw/expanded/{ticker}-prices.json', 'sha256': DAILY_PINS[ticker]})
    result = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)['chart']['result'][0]
    if result['meta']['symbol'] != ticker + '.SA' or result['meta']['currency'] != 'BRL':
        raise ValueError('cached_daily_identity_mismatch')
    quote = result['indicators']['quote'][0]
    if any(len(values) != len(result['timestamp']) for values in quote.values()):
        raise ValueError('cached_daily_field_length_mismatch')
    days = {}
    for index, stamp in enumerate(result['timestamp']):
        utc = datetime.fromtimestamp(stamp, timezone.utc)
        local = utc.astimezone(BR_TIME)
        key = utc.date().isoformat()
        if key in days:
            raise ValueError('duplicate_cached_daily_date')
        days[key] = {'date': key, 'utc_timestamp': stamp, 'utc_datetime': utc.isoformat(),
                     'local_datetime': local.isoformat(), 'local_date': local.date().isoformat(),
                     **{field: values[index] for field, values in quote.items()}}
    actions = [{'date': datetime.fromtimestamp(row['date'], timezone.utc).date().isoformat(),
                'ratio': str(Decimal(str(row['numerator'])) / Decimal(str(row['denominator']))),
                'reported_event': {key: str(value) if isinstance(value, Decimal) else value
                                   for key, value in row.items()}}
               for row in result.get('events', {}).get('splits', {}).values()]
    return days, actions, result


def action_factor(day, actions):
    return_value = Decimal(1)
    for action in actions:
        if day < action['date'] <= CUTOFF.isoformat():
            return_value *= Decimal(action['ratio'])
    return return_value


def exchange_action_reading(root, ticker, rows, yahoo_actions):
    if ticker == 'FRAS3':
        return [], {'status': 'no_cached_exchange_action_ledger',
                    'absence_of_actions_primary_verified': False}
    relative = 'raw/expanded/b3-supplement-SBSP.json'
    body, source = local_source(root, relative, SUPPLEMENT_SHA)
    supplement, = json.loads(body)
    if (supplement['codeCVM'] != '14443' or supplement['code'] != 'SBSP'
            or supplement['numberCommonShares'] == ''):
        raise ValueError('exchange_action_identity_mismatch')
    dates = sorted(r['date'] for r in rows)
    actions = []
    for index, event in enumerate(supplement['stockDividends']):
        cum = datetime.strptime(event['lastDatePrior'], '%d/%m/%Y').date()
        if not WINDOW_START < cum <= CUTOFF:
            continue
        if event['assetIssued'] != 'BRSBSPACNOR5' or event['isinCode'] != 'BRSBSPACNOR5':
            raise ValueError('cross_security_exchange_action')
        if event['label'] not in ('BONIFICACAO', 'DESDOBRAMENTO'):
            raise ValueError('unreviewed_exchange_action_kind')
        ex, = [a for a in yahoo_actions if a['date'] == next(d for d in dates if d > cum.isoformat())]
        percent = Decimal(event['factor'].replace('.', '').replace(',', '.'))
        ratio = Decimal(1) + percent / 100
        actions.append({'date': ex['date'], 'ratio': str(ratio), 'last_cum_date': cum.isoformat(),
            'kind': event['label'], 'percent_additional_shares': str(percent),
            'Yahoo_rounded_ratio': ex['ratio'], 'source_row': index, 'original_row': event,
            'source': source, 'primary_native_CVM_ratio_verified': False,
            'effective_date_basis': 'Yahoo_event_date_equals_first_cached_session_after_exchange_last_cum; native_ex_date_not_verified'})
    return sorted(actions, key=lambda r: r['date']), {'status': 'exact_cached_exchange_percentage_reading',
        'source': source, 'ratio_math': '1 + reported_additional_shares_percent / 100',
        'primary_native_CVM_ratio_verified': False}


def b3_reference_audit(root, ticker, rows, daily, actions):
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    record, = [r for r in review['records'] if r['ticker'] == ticker]
    source, = [s for s in record['sources'] if s['file'] == f'raw/expanded/{ticker}-b3-cash-1.json']
    payload = json.loads(read(root, source))
    page = payload['page']
    if (page['pageNumber'] != 1 or page['totalPages'] != 1
            or page['totalRecords'] != len(payload['results'])):
        raise ValueError('offline_reference_pagination_incomplete')
    enfoque = {r['date']: r for r in rows}
    observations, unique = [], {}
    for index, event in enumerate(payload['results']):
        observation = {'source_row': index, 'original_row': event, 'source': source,
                       'accepted_as_raw_anchor': False}
        observations.append(observation)
        if event.get('typeStock', '').strip() != record['share_class']:
            observation['exclusion'] = 'different_share_class'
            continue
        value = event.get('dateClosingPricePriorExDate')
        if not value:
            observation['exclusion'] = 'no_explicit_quote_date_no_record_date_substitution'
            continue
        day = datetime.strptime(value, '%d/%m/%Y').date()
        observation['explicit_quote_date'] = day.isoformat()
        if not WINDOW_START <= day <= CUTOFF:
            observation['exclusion'] = 'outside_model_window'
            continue
        if event['quotedPerShares'] != '1':
            observation['exclusion'] = 'historic_lot_unit_not_verified'
            continue
        raw = Decimal(event['closingPricePriorExDate'].replace('.', '').replace(',', '.'))
        if raw <= 0:
            observation['exclusion'] = 'nonpositive_reference'
            continue
        key = day.isoformat()
        if key in unique and unique[key] != raw:
            raise ValueError('conflicting_cached_exchange_raw_quotes')
        unique[key] = raw
        actual = Decimal(enfoque[key]['closed']) if key in enfoque else None
        factor = action_factor(key, actions)
        yahoo = daily.get(key, {}).get('close')
        reconstructed = yahoo * factor if yahoo is not None else None
        tolerance = max(Decimal('.015'), abs(raw) * Decimal('.006'))
        observation.update(accepted_as_raw_anchor=True, raw_B3_close_BRL=str(raw),
            Enfoque_close_BRL=str(actual) if actual is not None else None,
            Yahoo_close_as_stored=str(yahoo) if yahoo is not None else None,
            Yahoo_reconstructed_trading_unit_close=str(reconstructed) if reconstructed is not None else None,
            conditional_cached_action_factor=str(factor),
            Enfoque_exact_match=actual == raw,
            Enfoque_existing_gate_match=actual is not None and abs(actual - raw) <= tolerance,
            Yahoo_existing_gate_match=reconstructed is not None and abs(reconstructed - raw) <= tolerance,
            existing_raw_price_tolerance=str(tolerance))
    accepted = [r for r in observations if r['accepted_as_raw_anchor']]
    return {'source': source, 'complete_cached_pagination': True, 'all_rows': observations,
            'distinct_dates': len(unique), 'exact_Enfoque_matches': sum(enfoque[d]['closed'] == str(v)
                or Decimal(enfoque[d]['closed']) == v for d, v in unique.items()),
            'all_accepted_Enfoque_gate_matches': bool(accepted) and all(r['Enfoque_existing_gate_match'] for r in accepted),
            'accepted_dates': sorted(unique), 'cash_payments_inferred': False}


def daily_conflict_audit(rows, yahoo, yahoo_actions, exchange_actions):
    """Every session and OHLC field, without data repair or scalar estimation."""
    audit = []
    previous = None
    for row in rows:
        day = row['date']
        other = yahoo.get(day)
        if other is None or other.get('close') is None:
            audit.append({'date': day, 'classification': 'missing_comparator_not_filled'})
            previous = row
            continue
        yf = action_factor(day, yahoo_actions)
        ef = action_factor(day, exchange_actions)
        fields = {}
        for enfoque_name, yahoo_name in (('open', 'open'), ('high', 'high'), ('low', 'low'), ('closed', 'close')):
            actual = Decimal(row[enfoque_name])
            stored = other.get(yahoo_name)
            reconstructed = stored * yf if stored is not None else None
            exact_action_value = stored * ef if stored is not None else None
            fields[enfoque_name] = {'Enfoque_raw': str(actual),
                'Yahoo_stored': str(stored) if stored is not None else None,
                'Yahoo_times_reported_action_factor': str(reconstructed) if reconstructed is not None else None,
                'Yahoo_times_exchange_action_factor': str(exact_action_value) if exact_action_value is not None else None,
                'difference_raw_units': str(actual - reconstructed) if reconstructed is not None else None,
                'observed_Yahoo_to_Enfoque_ratio_not_used_for_correction': str(stored / actual) if stored is not None else None,
                'compatible_with_printed_cent_precision': reconstructed is not None and abs(actual - reconstructed) <= Decimal('.005'),
                'existing_gate_match': reconstructed is not None and abs(actual - reconstructed) <= max(Decimal('.015'), actual * Decimal('.006'))}
        close = fields['closed']
        ohl = [fields[k]['compatible_with_printed_cent_precision'] for k in ('open', 'high', 'low')]
        # This interval tests proportional bar shape only. No value from it
        # enters action_factor(), normalized prices, or any admission gate.
        scale_lower = max(Decimal(f['Yahoo_times_reported_action_factor']) / (Decimal(f['Enfoque_raw']) + Decimal('.005'))
                          for f in fields.values())
        scale_upper = min(Decimal(f['Yahoo_times_reported_action_factor']) / (Decimal(f['Enfoque_raw']) - Decimal('.005'))
                          for f in fields.values())
        proportional = scale_lower <= scale_upper
        if close['compatible_with_printed_cent_precision']:
            classification = 'same_close_with_printed_cent_and_binary_float_precision'
        elif all(ohl):
            classification = 'isolated_close_only_conflict_cause_unproven'
        elif not any(ohl):
            classification = ('all_OHLC_rescaled_without_validated_action' if proportional
                              else 'all_OHLC_conflict_not_fully_proportional')
        else:
            classification = 'multiple_field_conflict_cause_unproven'
        returns = None
        if previous is not None:
            raw_return = (Decimal(row['closed']) / Decimal(previous['closed']) - 1) * 100
            alternate_return = (other['close'] * yf / Decimal(previous['closed']) - 1) * 100
            returns = {'previous_date': previous['date'], 'previous_Enfoque_close': previous['closed'],
                       'Enfoque_reported_var_percent': row['var'],
                       'return_using_Enfoque_closes_percent': str(raw_return),
                       'return_using_comparator_close_percent': str(alternate_return)}
        neighbors = []
        if not close['compatible_with_printed_cent_precision']:
            keys = sorted(yahoo)
            position = keys.index(day)
            for key in keys[max(0, position - 1):position + 2]:
                quote = yahoo[key]
                factor = action_factor(key, yahoo_actions)
                neighbors.append({'date': key, 'stored_close': str(quote['close']),
                    'reconstructed_close': str(quote['close'] * factor),
                    'all_OHLC_equal_Enfoque_target_within_cent': all(
                        quote[y] is not None and abs(quote[y] * factor - Decimal(row[e])) <= Decimal('.005')
                        for e, y in (('open', 'open'), ('high', 'high'), ('low', 'low'), ('closed', 'close')))})
        audit.append({'date': day, 'classification': classification, 'fields': fields,
            'all_OHLC_fields_differ_beyond_printed_cent': not any(ohl) and not close['compatible_with_printed_cent_precision'],
            'OHLC_proportional_shape_check': {'common_scale_feasible_with_printed_cent_precision': proportional,
                'lower_bound': str(scale_lower), 'upper_bound': str(scale_upper),
                'descriptive_interval_never_used_to_correct_prices': True},
            'close_difference_severity': ('printed_cent_or_float_precision' if close['compatible_with_printed_cent_precision']
                else 'real_price_difference_within_unchanged_gate' if close['existing_gate_match']
                else 'outside_unchanged_gate'),
            'Yahoo_action_factor': str(yf), 'exchange_action_factor_conditional': str(ef),
            'same_date_in_UTC_and_Sao_Paulo': other['date'] == other['local_date'] == day,
            'Yahoo_timestamp_UTC': other['utc_datetime'], 'Yahoo_timestamp_local': other['local_datetime'],
            'Enfoque_volume_shares': row['share'], 'Yahoo_volume_shares_as_stored': str(other.get('volume')),
            'Enfoque_volume_times_Yahoo_action_factor': str(Decimal(row['share']) * yf),
            'reported_return_self_check': returns, 'neighbor_date_tests': neighbors,
            'specific_closing_type_proved': False, 'provider_revision_proved': False})
        previous = row
    return audit


def month_end_audit(rows, daily, yahoo_actions, exchange_actions, references):
    from .collect_market_data import month_end, shift_month

    monthly, yahoo_monthly = {}, {}
    for row in rows:
        day = date.fromisoformat(row['date'])
        monthly[month_end(day.year, day.month)] = row
    for key, row in sorted(daily.items()):
        day = date.fromisoformat(key)
        if day <= CUTOFF and row.get('close') is not None:
            yahoo_monthly[month_end(day.year, day.month)] = row
    result = []
    previous_adjusted = None
    for month in (shift_month(CUTOFF, -i) for i in reversed(range(61))):
        row = monthly.get(month)
        if row is None:
            result.append({'month': month.isoformat(), 'status': 'missing_no_fill'})
            continue
        day, raw = row['date'], Decimal(row['closed'])
        factor = action_factor(day, exchange_actions)
        yf = action_factor(day, yahoo_actions)
        normalized = raw / factor
        yahoo = daily.get(day, {}).get('close')
        ymonth = yahoo_monthly.get(month, {})
        diff = normalized - yahoo if yahoo is not None else None
        tolerance = max(Decimal('.015'), abs(normalized) * Decimal('.006'))
        factor_dates = [d for d in references['accepted_dates'] if action_factor(d, exchange_actions) == factor]
        ratio = normalized / previous_adjusted if previous_adjusted is not None else None
        result.append({'month': month.isoformat(), 'observed_on': day, 'raw_Enfoque_close': str(raw),
            'calculation_scope': 'non_admissible_sensitivity_to_unverified_cached_action_ratios',
            'cached_exchange_factor_conditional_not_primary_verified': str(factor),
            'Yahoo_reported_factor': str(yf), 'Enfoque_cutoff_unit_close_conditional': str(normalized),
            'same_date_Yahoo_close': str(yahoo) if yahoo is not None else None,
            'Yahoo_month_end_observed_on': ymonth.get('date'), 'same_month_end_observation_date': ymonth.get('date') == day,
            'normalized_difference': str(diff) if diff is not None else None,
            'compatible_with_printed_cent_precision': yahoo is not None and abs(raw - yahoo * factor) <= Decimal('.005'),
            'existing_price_tolerance': str(tolerance),
            'existing_price_gate_match': diff is not None and abs(diff) <= tolerance,
            'B3_independent_anchor_dates_in_regime': factor_dates,
            'consecutive_month_ratio_conditional': str(ratio) if ratio is not None else None,
            'unchanged_extreme_price_gate_pass': ratio is None or Decimal('.2') < ratio < 3})
        previous_adjusted = normalized
    return result


def raw_unit_corroboration(rows, actions, reference_audit):
    observations = []
    for row in rows:
        average = Decimal(row['vol']) / Decimal(row['share'])
        observations.append({'date': row['date'], 'turnover': row['vol'], 'share_quantity': row['share'],
            'turnover_per_reported_share': str(average), 'reported_average_price': row['middle'],
            'average_minus_reported_middle': str(average - Decimal(row['middle'])),
            'low': row['low'], 'high': row['high'], 'raw_close': row['closed'],
            'turnover_per_share_inside_raw_OHLC_range': Decimal(row['low']) <= average <= Decimal(row['high']),
            'conditional_action_factor': str(action_factor(row['date'], actions))})
    return {'daily_observations': observations,
        'all_turnover_per_share_inside_raw_OHLC': all(r['turnover_per_share_inside_raw_OHLC_range'] for r in observations),
        'independent_exact_B3_quote_dates': reference_audit['exact_Enfoque_matches'],
        'adjusted_false_alone_treated_as_proof': False,
        'scope': 'raw OHLC/monetary-turnover/share-count arithmetic, actual split-boundary observations, and available same-date B3 quotes together; no native ratio ledger inferred'}


def cached_primary_ratio_audit(root):
    """Discover native proofs offline; index descriptions never become ratios."""
    from .collect_market_data import normalize

    sources, candidates, indexed_documents = [], [], {}
    native_receipts = {}
    for receipt in prior.Reader(root).receipts():
        parts = urlsplit(receipt['url'])
        if parts.hostname != 'www.rad.cvm.gov.br' or parts.path != '/ENET/frmDownloadDocumento.aspx':
            continue
        query = parse_qs(parts.query)
        sequence = query.get('numSequencia', [None])[0]
        version = query.get('numVersao', [None])[0]
        if sequence and version:
            native_receipts.setdefault((sequence, version), []).append(receipt)
    raw_files = [p for p in (Path(root) / 'raw').rglob('*')
                 if p.is_file() and p.suffix.lower() in ('.pdf', '.bin', '.html', '.htm')]
    for year, digest in IPE_PINS.items():
        name = f'raw/primary-research/cvm-documents/index-{year}-{digest}.zip'
        body, source = local_source(root, name, digest,
            url=f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip')
        sources.append(source)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            member = f'ipe_cia_aberta_{year}.csv'
            table = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            for line, row in enumerate(table, 2):
                cvm = int(row['Codigo_CVM'])
                if cvm not in (6211, 14443):
                    continue
                expected_cnpj = '88610126000129' if cvm == 6211 else '43776517000180'
                if re.sub(r'\D', '', row['CNPJ_Companhia']) != expected_cnpj:
                    raise ValueError('primary_index_exact_CVM_CNPJ_mismatch')
                indexed_documents[row['Link_Download']] = {'row': row, 'source': source,
                                                         'member': member, 'csv_line': line}
                text = normalize(' '.join(row[k] for k in ('Assunto', 'Tipo', 'Categoria')))
                action = any(word in text for word in ('bonifica', 'desdobr', 'subscri', 'aumento de capital', 'capital social'))
                nearby_fras = cvm == 6211 and '2022-03-20' <= row['Data_Entrega'] <= '2022-04-20'
                if not action and not (nearby_fras and row['Categoria'] in ('Fato Relevante', 'Aviso aos Acionistas')):
                    continue
                sequence = parse_qs(urlsplit(row['Link_Download']).query)['numSequencia'][0]
                pattern = re.compile(r'(?<!\d)' + re.escape(sequence) + r'(?!\d)')
                files = [p.relative_to(root).as_posix() for p in raw_files if pattern.search(p.name)]
                receipt_matches = []
                for receipt in native_receipts.get((sequence, row['Versao']), []):
                    if receipt['url'] == row['Link_Download']:
                        original = read(root, receipt)
                        receipt_matches.append({**receipt, 'body_magic': original[:8].hex(),
                                                'valid_PDF_header': original.startswith(b'%PDF-')})
                candidates.append({'cvm': cvm, 'sequence': sequence, 'version': int(row['Versao']),
                    'published_at': row['Data_Entrega'], 'category': row['Categoria'], 'type': row['Tipo'],
                    'subject': row['Assunto'], 'url': row['Link_Download'], 'index_source': source,
                    'member': member, 'csv_line': line, 'index_row': row,
                    'published_after_cutoff': row['Data_Entrega'] > CUTOFF.isoformat(),
                    'matching_cached_body_filenames': files, 'native_ratio_verified': False,
                    'matching_content_receipts': receipt_matches,
                    'status': 'body_not_cached_index_is_not_ratio_proof' if not files and not receipt_matches else 'cached_body_requires_review'})
    # Inspect the actual cached target native PDFs, independently of earlier
    # parsed reports. These are cash declaration forms, not ratio declarations.
    report_body, report_source = local_source(root, 'raw/primary-research/cvm-documents/report.json')
    native = []
    import pdfplumber
    for document in json.loads(report_body)['results']:
        if document['cvm'] not in (6211, 14443) or not document.get('file', '').endswith('.pdf'):
            continue
        body, receipt = local_source(root, document['file'], document['sha256'], url=document['url'])
        index_match = indexed_documents.get(document['url'])
        if index_match is None or int(index_match['row']['Codigo_CVM']) != document['cvm']:
            raise ValueError('cached_native_document_not_bound_to_target_IPE_row')
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            pages = [{'page': n, 'text': page.extract_text() or ''} for n, page in enumerate(pdf.pages, 1)]
        text = normalize(' '.join(p['text'] for p in pages))
        native.append({'cvm': document['cvm'], 'sequence': document['sequence'], 'source': receipt,
            'pages': pages, 'source_report': report_source, 'exact_IPE_index_row': index_match,
            'contains_ratio_action_terms': any(w in text for w in ('bonificacao', 'desdobramento')),
            'contains_cash_amount_unit_heading': 'r$/unidade' in text,
            'native_ratio_verified': False})
    requested = {
        'SBSP3_2025_12_bonus': ('984181', '984073'),
        'SBSP3_2026_03_bonus': ('1017747',),
        'SBSP3_2026_04_split': ('1034745', '1033944', '1022240'),
        'FRAS3_2022_offering_context_not_proven_scale_cause': ('485715', '487408', '489923', '490919'),
    }
    required = {name: [r for r in candidates if r['sequence'] in sequences]
                for name, sequences in requested.items()}
    return {'index_sources': sources, 'index_candidates': candidates, 'cached_native_forms': native,
        'required_native_proof_candidates': required,
        'native_action_ratio_readings': [], 'index_metadata_used_as_native_ratio': False,
        'status': 'required_native_action_bodies_not_cached',
        'body_filename_search_scope': 'all existing raw PDF/HTML/bin filenames with exact numeric sequence boundaries plus public research content receipts bound to exact CVM URL/sequence/version',
        'body_filenames_searched': len(raw_files), 'new_HTTP_requests': 0}


def ordinary_cash_replay(root, inventory, identities, actions_by_ticker):
    """Run unchanged source/cash checks; no price substitution into production."""
    from .inventory_candidates import _Cache, _identity, _cash_pages, _payments, _qualify_history
    from .share_basis import b3_evidence, reconcile_payments
    from .cash_calendar_research import apply_research_calendar, deduplicate_independent_rows

    cache, output = _Cache(root, inventory), {}
    for identity in identities:
        ticker = identity['ticker']
        result = {'conditional_on_primary_action_verification': True,
                  'model_admission_granted': False, 'candidate_constructed': False}
        output[ticker] = result
        try:
            _, codes = _identity(identity, cache, CUTOFF)
            result['identity_gate'] = 'passed_unchanged_exact_CVM_class_ISIN'
            if cache.security_registry is not None:
                status = cache.security_registry.status(identity, cache.registration_reference or CUTOFF)
                result['security_registration'] = {k: status[k] for k in (
                    'status', 'blocks_current_purchase', 'block_reasons', 'exchange_trading_verified')}
            rows = _cash_pages(ticker, codes, identity['cvm'], cache)
            rows, duplicate_corrections = deduplicate_independent_rows(identity, cache, CUTOFF, rows)
            result['duplicate_corrections'] = duplicate_corrections
            expected, _ = b3_evidence(rows, identity['share_class'])
            result['independent_cash_before_window_start'] = any(day <= WINDOW_START for day in expected)
            primary = cache.primary.primary_events(identity, CUTOFF) if cache.primary else {}
            original, _ = _payments(ticker, cache, CUTOFF, primary)
            actions = [{'date': a['date'], 'ratio': float(a['ratio'])} for a in actions_by_ticker[ticker]]
            original = apply_research_calendar(identity, cache, CUTOFF, original, rows, actions)
            result['calendar_installments'] = len(original.installments)
            calendar_dates = {date.fromisoformat(r['record_date']) for r in original.installments}
            required = {day for day in expected if WINDOW_START <= day <= CUTOFF}
            required.update(date.fromisoformat(r['record_date']) for r in original.installments
                            if WINDOW_START <= date.fromisoformat(r['payment_date']) <= CUTOFF)
            result['required_cash_right_dates'] = sorted(d.isoformat() for d in required)
            result['calendar_rights_missing_independent_cash'] = sorted(d.isoformat() for d in required - set(expected))
            result['exchange_rights_missing_calendar'] = sorted(d.isoformat() for d in required - calendar_dates)
            paid, ledger, adjustments = reconcile_payments(ticker, original, expected, actions, CUTOFF)
            _qualify_history(paid, ledger, original, CUTOFF, WINDOW_START)
            result.update(status='passed_unchanged_cash_checks_conditionally', ledger_rows=len(ledger),
                independent_cash_matches=len(adjustments), settlement_confirmed=False,
                point_in_time_archive_verified=False)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            result.update(status='blocked_by_existing_gate', reason=str(error))
    return output, list(cache.receipts.values())


def financial_basis_context(ticker, primary_audit):
    """Preserve the user's forwarded audit without redoing its financial work."""
    common = {'source': 'human_user_forwarded_Linnaeus_readonly_financial_audit',
        'independently_recomputed_by_this_price_helper': False,
        'financial_policy_changed': False, 'generic_date_gate_lowered': False,
        'included_action_IDs_known': False, 'EPS_normalization_authorized': False,
        'financial_features_or_stock_observations_relaxed': False,
        'financial_eligibility_inferred_from_price_checks': False,
        'ordinary_parent_replay_required_before_integration': True,
        'CPC41_paragraph_64_guard': 'EPS may already reflect an action before authorization; fiscal end or authorization date alone cannot establish its share basis. Require exact filing/class and included action IDs before any conversion.'}
    if ticker == 'FRAS3':
        return {**common, 'status': 'FY2025_annual_valid_per_forwarded_audit_price_root_cause_still_pending',
            'forwarded_FY2025_annual_valid': True,
            'Yahoo_action_list_is_empty': True,
            'split_factor_invented_for_price_conflict': False,
            'focus': 'independent raw price validation and early 141-session OHLC conflict; no inferred split or EPS factor'}
    emae = [r for r in primary_audit['index_candidates']
            if r['cvm'] == 14443 and 'EMAE' in r['subject'].upper()]
    return {**common, 'status': 'separate_financial_attribution_and_EPS_action_basis_unresolved',
        'forwarded_findings': ['FY2025 consolidated DFP v2 parent/NCI profit lines zero despite nonzero consolidated total profit',
            '2026-03-20 Yahoo bonus ratio rounded; native exact bonus ratio and ex-date not proved',
            '2026-04-29 5:1 split native body and effective-date proof absent',
            'possible EMAE incorporation/capital changes require economic and per-share classification'],
        'EMAE_capital_change_index_context_not_body_proof': emae,
        'required_evidence': ['exact FY2025 consolidated attribution reading for parent and NCI',
            'native same-security action ratios and dates',
            'EPS note/authorization and included action IDs, including any pre-authorization restatement',
            'classification of EMAE/capital changes before assuming they multiply existing holders units'],
        'blind_factor_after_conversion_of_FY2025_EPS_permitted': False}


def build_offline_followon(root):
    """No acquisition paths. Enfoque is evaluated as one complete primary series."""
    from .price_basis_research import _fca_identity

    before = protected(root)
    attempts_before = records(root, 'request')
    if len(attempts_before) != 18 or not list(prior.safe_path(root, CACHE).glob('seal-*.json')):
        raise ValueError('offline_followon_requires_sealed_eighteen_request_cache')
    body, inventory_source = local_source(root, INVENTORY_FILE, INVENTORY_SHA)
    inventory = json.loads(body)
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    results, identities, action_sets = {}, [], {}
    for ticker in HISTORY_PINS:
        identity, = [r for r in inventory['stocks'] if r['ticker'] == ticker]
        reviewed, = [r for r in review['records'] if r['ticker'] == ticker]
        seed, = [r for r in catalog(root) if r['ticker'] == ticker]
        exact = {**reviewed, 'cnpj': re.sub(r'\D', '', seed['cnpj'])}
        fca = _fca_identity(read(root, {'file': FCA, 'sha256': FCA_SHA}), ticker, exact, CUTOFF)
        rows, receipt = pinned_history(root, ticker)
        daily, yahoo_actions, yahoo_payload = cached_yahoo(root, ticker)
        actions, action_reading = exchange_action_reading(root, ticker, rows, yahoo_actions)
        references = b3_reference_audit(root, ticker, rows, daily, actions)
        audit = daily_conflict_audit(rows, daily, yahoo_actions, actions)
        months = month_end_audit(rows, daily, yahoo_actions, actions, references)
        conflicts = [r for r in audit if not r.get('fields', {}).get('closed', {}).get('compatible_with_printed_cent_precision')]
        scaled = [r for r in audit if r.get('all_OHLC_fields_differ_beyond_printed_cent')]
        scale_description = None
        if scaled:
            ratios = [Decimal(f['observed_Yahoo_to_Enfoque_ratio_not_used_for_correction'])
                      for r in scaled for f in r['fields'].values()]
            scale_description = {'first': scaled[0]['date'], 'last': scaled[-1]['date'],
                'sessions': len(scaled), 'sessions_in_model_window': sum(r['date'] >= WINDOW_START.isoformat() for r in scaled),
                'observed_OHLC_Yahoo_over_Enfoque_min': str(min(ratios)),
                'observed_OHLC_Yahoo_over_Enfoque_max': str(max(ratios)),
                'proportional_OHLC_sessions': sum(r['OHLC_proportional_shape_check']['common_scale_feasible_with_printed_cent_precision'] for r in scaled),
                'nonproportional_residual_dates': [r['date'] for r in scaled
                    if not r['OHLC_proportional_shape_check']['common_scale_feasible_with_printed_cent_precision']],
                'ratio_range_is_descriptive_not_an_approved_action_or_correction': True,
                'first_following_session': next((r['date'] for r in audit if r['date'] > scaled[-1]['date']), None),
                'Yahoo_action_on_first_following_session': any(a['date'] == next(
                    (r['date'] for r in audit if r['date'] > scaled[-1]['date']), None) for a in yahoo_actions)}
        regimes = {}
        for row in rows:
            factor = str(action_factor(row['date'], actions))
            regime = regimes.setdefault(factor, {'first': row['date'], 'last': row['date'],
                'daily_observations': 0, 'B3_anchor_dates': [], 'month_ends': []})
            regime['last'] = row['date']
            regime['daily_observations'] += 1
        for factor, regime in regimes.items():
            regime['B3_anchor_dates'] = [d for d in references['accepted_dates'] if str(action_factor(d, actions)) == factor]
            regime['month_ends'] = [m['month'] for m in months if m.get('cached_exchange_factor_conditional_not_primary_verified') == factor]
        needed_regimes = [k for k, v in regimes.items() if v['month_ends']]
        missing_regimes = [k for k in needed_regimes if not regimes[k]['B3_anchor_dates']]
        blockers = (['insufficient_independent_price_matches', 'unexplained_pre_2022_03_29_OHLC_scale',
                     'no_primary_proof_of_unchanged_share_units_for_early_cohort'] if ticker == 'FRAS3' else
                    ['native_CVM_bonus_split_ratio_and_ex_date_bodies_not_cached'])
        combined_numeric_pass = (references['distinct_dates'] >= 4
            and references['all_accepted_Enfoque_gate_matches']
            and all(m.get('existing_price_gate_match') and m.get('same_month_end_observation_date')
                    and m.get('unchanged_extreme_price_gate_pass') for m in months))
        substantive = [r for r in audit if r.get('close_difference_severity') == 'outside_unchanged_gate']
        monthly_by_date = {m['observed_on']: m['month'] for m in months if 'observed_on' in m}
        results[ticker] = {'identity': identity, 'FCA_exact_identity_proof': fca, 'primary_price_source': receipt,
            'primary_source_selection': 'entire issuer-published Enfoque raw history selected for review before fit; no date-wise provider switching',
            'B3_reference_audit': references, 'cached_exchange_action_reading': action_reading,
            'exchange_actions_conditional': actions, 'Yahoo_actions_diagnostic': yahoo_actions,
            'daily_comparison': audit, 'conflict_dates': [r['date'] for r in conflicts],
            'substantive_conflict_dates': [r['date'] for r in substantive],
            'real_differences_within_existing_gate': [r['date'] for r in audit
                if r.get('close_difference_severity') == 'real_price_difference_within_unchanged_gate'],
            'conflict_impact_on_monthly_inputs': [{'date': r['date'],
                'used_as_month_end': r['date'] in monthly_by_date,
                'month': monthly_by_date.get(r['date']), 'severity': r['close_difference_severity']}
                for r in conflicts],
            'classification_counts': dict(Counter(r['classification'] for r in audit)),
            'systematic_OHLC_cohort_description': scale_description,
            'raw_unit_body_corroboration': raw_unit_corroboration(rows, actions, references),
            'all_61_month_ends': months, 'regimes': regimes,
            'uniform_price_gates': {'identity': 'passed_exact_FCA_CVM_class_and_pinned_quote_page',
                'positive_finite_unique_daily_prices': True, 'consecutive_month_ends': len(months) == 61 and all('observed_on' in m for m in months),
                'stale_cutoff_gate_pass': (CUTOFF - date.fromisoformat(rows[-1]['date'])).days <= 7,
                'minimum_independent_B3_dates_four': references['distinct_dates'] >= 4,
                'all_available_B3_quotes_match_Enfoque': references['all_accepted_Enfoque_gate_matches'],
                'all_regimes_have_B3_raw_anchor': not missing_regimes,
                'missing_B3_regimes': missing_regimes,
                'combined_B3_and_Yahoo_regime_checks_pass_numerically_conditionally': combined_numeric_pass,
                'all_month_end_Yahoo_checks_pass_conditionally': all(m.get('existing_price_gate_match') for m in months),
                'all_month_end_dates_agree': all(m.get('same_month_end_observation_date') for m in months),
                'normalized_extreme_change_gate_pass_conditionally': all(m.get('unchanged_extreme_price_gate_pass') for m in months),
                'primary_action_basis_verified': False, 'full_price_candidate_approved': False},
            'decision': 'retain_Enfoque_as_complete_raw_primary_candidate_pending_specific_evidence',
            'daily_Yahoo_disagreement_is_automatic_source_veto': False,
            'conditional_combined_source_price_checks': {
                'unchanged_minimum_four_B3_raw_dates_match': references['distinct_dates'] >= 4 and references['all_accepted_Enfoque_gate_matches'],
                'minimum_anchors_and_all_monthly_regimes_numeric_pass': combined_numeric_pass,
                'Yahoo_month_end_checks_cover_all_used_regimes': all(m.get('existing_price_gate_match') for m in months),
                'new_regime_Yahoo_check_dates': [m['observed_on'] for m in months
                    if not m.get('B3_independent_anchor_dates_in_regime') and m.get('existing_price_gate_match')],
                'not_an_admission': True,
                'B3_only_regime_gap_is_automatic_source_veto': False,
                'qualification': 'mechanical checks can support Enfoque as the full primary series; native action ratios/ex dates and ordinary cash/financial gates remain necessary; upstream relationship unresolved'},
            'direct_month_end_conflicts': [m['month'] for m in months if not m.get('existing_price_gate_match')],
            'blockers': blockers, 'model_admission_granted': False,
            'Yahoo_metadata': {k: yahoo_payload['meta'].get(k) for k in ('symbol', 'currency', 'dataGranularity', 'exchangeTimezoneName')}}
        identities.append(identity)
        action_sets[ticker] = actions
    native = cached_primary_ratio_audit(root)
    cash, cash_sources = ordinary_cash_replay(root, inventory, identities, action_sets)
    for ticker, result in results.items():
        result['unchanged_cash_gate_replay'] = cash[ticker]
        result['financial_and_training_gates'] = financial_basis_context(ticker, native)
        if ticker == 'SBSP3':
            result['blockers'].extend(['forwarded_FY2025_consolidated_profit_attribution_unresolved',
                'EPS_included_action_IDs_and_possible_pre_authorization_restatement_unresolved',
                'EMAE_incorporation_capital_change_basis_unresolved'])
        if cash[ticker]['status'] == 'blocked_by_existing_gate':
            result['blockers'].append('unchanged_cash_gate:' + cash[ticker]['reason'])
    after = protected(root)
    if attempts_before != records(root, 'request'):
        raise ValueError('offline_followon_request_ledger_changed')
    return {'schema_version': 2, 'scope': 'offline_Enfoque_primary_source_followon',
        'cutoff': CUTOFF.isoformat(), 'window_start': WINDOW_START.isoformat(), 'results': results,
        'inventory_source': inventory_source, 'primary_action_body_audit': native, 'cash_read_sources': cash_sources,
        'provenance': {'Enfoque': 'RI page -> pinned MZ widget -> MZ API explicitly declaring Enfoque',
            'Yahoo': 'separately cached Yahoo chart quote OHLC and its reported actions; adjusted-close dividend-total-return field never consumed',
            'B3': 'previously cached exchange reference quotes and stock-action percentages; no HTTP',
            'Enfoque_Yahoo_same_upstream_assumed': False, 'Enfoque_Yahoo_distinct_upstream_verified': False,
            'upstream_relationship': 'unknown; neither agreement nor different endpoint branding proves distinct data suppliers'},
        'closing_type_and_revision_conclusion': 'FRAS early all-OHLC cohort is scale-like but has two nonproportional residual sessions; neither a pure scalar correction nor closing precision/type explains everything. Action cause unverified. Additional within-gate price differences are recorded, not called rounding. SBSP material isolated close-only conflict is internally consistent in Enfoque but specific closing type/revision is unproved.',
        'thresholds_changed': False, 'fitted_price_factors': [], 'missing_prices_filled': 0,
        'cash_annual_aggregates_promoted': False, 'production_changes': False, 'model_admissions': 0,
        'point_in_time_archive_verified': False, 'new_HTTP_requests': 0, 'total_sealed_HTTP_attempts': len(attempts_before),
        'coordination_scope': {'owned_price_names': ['FRAS3', 'SBSP3'],
            'LREN3_PSSA3_KLBN_research_deferred_to_user_named_Leibniz_agent': True,
            'Linnaeus_financial_audit_forwarded_by_user_preserved_not_reimplemented': True},
        'policy': {'generic_date_gate_changed': False,
            'generic_date_gate_lowering_current_admissions_per_forwarded_audit': 0,
            'price_sensitivity_values_are_not_admissible_normalized_prices_or_EPS': True},
        'producer_helper_sha256': prior.sha(Path(__file__).read_bytes()),
        'test_module_sha256': prior.sha(Path(__file__).with_name('test_issuer_raw_quote_research.py').read_bytes()),
        'protected_before': before, 'protected_after': after,
        'external_changes_during_replay': [name for name in before if before[name] != after[name]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='recommender/data/market')
    parser.add_argument('--triage', action='store_true')
    parser.add_argument('--fetch-seed', choices=TARGETS)
    parser.add_argument('--fetch')
    parser.add_argument('--ticker', choices=TARGETS)
    parser.add_argument('--source')
    parser.add_argument('--inspect')
    parser.add_argument('--fetch-widget', choices=TARGETS)
    parser.add_argument('--quote-receipt')
    parser.add_argument('--bundle-receipt')
    parser.add_argument('--kind', choices=('graphic', 'history'), default='graphic')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--seal', action='store_true')
    parser.add_argument('--offline-followon', action='store_true')
    args = parser.parse_args()
    if args.offline_followon:
        report = build_offline_followon(args.folder)
        path = save(args.folder, 'offline-followon', report)
        print(json.dumps({'report': path, 'new_HTTP_requests': 0,
            'results': {t: {'B3_dates': r['B3_reference_audit']['distinct_dates'],
                           'month_end_conflicts': r['direct_month_end_conflicts'],
                           'blockers': r['blockers']} for t, r in report['results'].items()}}, indent=2))
    elif args.report or args.seal:
        report = build_report(args.folder)
        path = save(args.folder, 'report', report)
        if args.seal:
            save(args.folder, 'seal', {'requests_consumed': report['acquisition_requests'],
                'report': path, 'network_closed': True})
        print(json.dumps({'report': path, 'requests': report['acquisition_requests'],
            'proofs': {t: {'rows': p['rows'], 'mismatches': len(p['existing_history_diagnostic']['daily_mismatches'])}
                       for t, p in report['raw_proofs'].items()}}, indent=2))
    elif args.fetch_widget:
        quote, bundle = [json.loads(prior.safe_path(args.folder, name).read_bytes())
                         for name in (args.quote_receipt, args.bundle_receipt)]
        url, discovery = widget_request(args.folder, args.fetch_widget, quote, bundle, args.kind)
        _, receipt = Fetcher(args.folder).get_verified(url, args.fetch_widget, discovery)
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
    elif args.fetch_seed or args.fetch:
        ticker = args.fetch_seed or args.ticker
        url = args.fetch or next(r['url'] for r in catalog(args.folder) if r['ticker'] == ticker)
        _, receipt = Fetcher(args.folder).get(url, ticker, args.source)
        print(json.dumps(receipt, ensure_ascii=True, indent=2))
    elif args.inspect:
        receipt = json.loads(prior.safe_path(args.folder, args.inspect).read_bytes())
        print(json.dumps(links(args.folder, receipt), ensure_ascii=True, indent=2))
    else:
        print(json.dumps(catalog(args.folder), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
