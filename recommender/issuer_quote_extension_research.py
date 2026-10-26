"""Isolated public RI quote research for LREN3/PSSA3; offline by default.

All new HTTP attempts share one persistent budget, including robots, redirects,
errors and interrupted calls. No production helper or dataset is imported.
"""

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import unicodedata
from fractions import Fraction
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import unquote, urljoin, urlsplit
import zipfile

from lxml import html


CACHE = 'raw/primary-research/issuer-quote-extension'
MAX_REQUESTS = 20
MAX_BYTES = 24_000_000
USER_AGENT = 'RendaFutura-Academic/1.0'
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
REVIEW_SHA = '89d2fcc002b04f0837413cdc1a8385470e9dcba5f649e0ecb5c395126ba5b073'
REVIEW = f'raw/primary-research/price-basis/review-2026-09-30-{REVIEW_SHA}.json'
TARGETS = {'LREN3': 8133, 'PSSA3': 16659}
DENIED = ('b3.com.br', 'bmfbovespa.com.br', 'bovespa.com.br', 'bvmf.com.br',
          'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br',
          'engie.com.br', 'cdn-sites-assets.mziq.com')
AUTH = re.compile(r'(?:^|[/?.&=_-])(auth|login|signin|sign-in|signup|sign-up|'
                  r'wp-login|wp-signup|register|oauth|authorize|authorization|'
                  r'password|private|token|api[-_]?key)(?:$|[/?.&=_-])', re.I)
BUNDLE_SHA = '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
ACQUISITION_REPORT_SHA = '11e460a3a6fb703f233ad05a88a66bbef33e7038742e5b95f2eaec21525f35f4'
PSSA_CONTEXT_SHA = 'fc0dfea8116d96a33dba1b932bc59a1e4438b6e596ccef3bf34e11729dd69c3f'
PSSA_IDENTITY = {'ticker': 'PSSA3', 'cvm': 16659, 'share_class': 'ON', 'isin': 'BRPSSAACNOR7'}
PSSA_ANCHOR_LABEL = 'independent_issuer_Enfoque_plus_cached_exchange_quotes'
HISTORY_PINS = {
    'LREN3': (13, '49e6f7881e897f4c61f016aeb272cd69b9ac0e221a07335365b928c73038952b'),
    'PSSA3': (15, '4529734ae6c4f02b35a6007234de2b11bb5fafca52c4938efd4da0be7b4474d6'),
}
IPE_PINS = {
    2021: 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7',
    2024: '65878657e487ba86b717e93f112a11cf08faf0356ae245ef6e0e72b0fbac80e2',
}
ACTION_PINS = {
    'LREN3': (
        {'ordinal': 20, 'sha256': '6cd0703739ae1bc7c1837d13dc14f62f9ecaddcc9caa9cbcddd246eabe715e45',
         'record_date': '2021-11-04', 'ex_date': '2021-11-05', 'new': 1, 'old': 10,
         'ratio_pages': [2], 'date_pages': [3],
         'ratio_text': '1(uma)novaacaoordinariaaseremitidaparacada10(dez)acoesordinariasexistentes',
         'native_ex_date': '05/11/2021'},
        {'ordinal': 18, 'sha256': '9e76659d6576ab98973b4d59d114701cc41650f83632888a7f7c23ca90e22832',
         'record_date': '2024-12-11', 'ex_date': '2024-12-12', 'new': 1, 'old': 10,
         'ratio_pages': [1], 'date_pages': [1],
         'ratio_text': '1(uma)novaacaoordinariaparacada10(dez)acoesordinarias',
         'native_ex_date': '12/12/2024'}),
    'PSSA3': (
        {'ordinal': 19, 'sha256': '8d45fdd70ab4551bdd425b3f4d68e2cfbc848c0c66a7951095f51a69d8f63f67',
         'record_date': '2021-10-20', 'ex_date': '2021-10-21', 'new': 1, 'old': 1,
         'ratio_pages': [1], 'date_pages': [1],
         'ratio_text': 'proporcaode1novaacaoparacada1acaodequeforemtitularesnestadata',
         'native_ex_date': '21.10.2021'},),
}
NOTICE_SELECTION = {
    ('LREN3', '2021-11-04'): ('Aviso aos Acionistas', '1'),
    ('LREN3', '2024-12-11'): ('Aviso aos Acionistas', '2'),
    ('PSSA3', '2021-10-20'): ('Fato Relevante', '1'),
}
LIBRARY_ONLY_REVIEW = frozenset((
    '0d1d334b201da03bf227c8e8773d0970fc6fc97701a939d378f297bf57be184d',
    '42bcdbc650894125c20d8a6b21fcd8933d7a91cd88cb28ef0e93deccc33306b2'))
START, END = '2021-09-01', '2026-09-30'
BR_TIME = timezone(timedelta(hours=-3))
PROTECTED = ('current.json', 'inventory/current.json',
             'raw/primary-research/registration/current.json',
             'raw/primary-research/macro/current.json')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def dump(obj):
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2,
                      allow_nan=False).encode('utf-8')


def safe_path(root, name):
    root = Path(root).resolve()
    if (not isinstance(name, str) or not name.startswith('raw/') or '\\' in name
            or ':' in name or '..' in name.split('/')):
        raise ValueError('unsafe_raw_path')
    path = root / name
    if (not path.resolve().is_relative_to(root)
            or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root))):
        raise ValueError('unsafe_raw_path')
    return path


def read(root, source):
    path = safe_path(root, source['file'])
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('source_too_large')
    body = path.read_bytes()
    if sha(body) != source['sha256'] or ('bytes' in source and len(body) != source['bytes']):
        raise ValueError('source_hash_or_size_mismatch')
    return body


def save(root, prefix, obj, *, raw=False):
    if not re.fullmatch(r'[a-z][a-z0-9-]*', prefix):
        raise ValueError('unsafe_output_prefix')
    body = obj if raw else dump(obj)
    name = f'{CACHE}/{prefix}-{sha(body)}.' + ('bin' if raw else 'json')
    path = safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_cache_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def records(root, prefix):
    result = []
    for path in safe_path(root, CACHE).glob(prefix + '-*.json'):
        body = path.read_bytes()
        if path.name != f'{prefix}-{sha(body)}.json':
            raise ValueError('receipt_hash_mismatch')
        result.append(json.loads(body))
    return sorted(result, key=lambda r: r.get('ordinal', 0))


def allowed(url):
    try:
        p = urlsplit(url)
        decoded = url
        for _ in range(6):
            updated = unquote(decoded)
            if updated == decoded:
                break
            decoded = updated
        decoded = decoded.lower()
        return bool(p.scheme in ('http', 'https') and p.hostname and '.' in p.hostname
                    and not p.username and not p.password and not p.fragment
                    and p.port in (None, 80 if p.scheme == 'http' else 443)
                    and not re.fullmatch(r'[\d.]+', p.hostname)
                    and not any(s in decoded for s in (*DENIED, 'yahoo', 'brapi', 'proxy',
                                                       'bmfbovespa', 'bovespa', 'bvmf'))
                    and not re.search(r'(?:^|[.-])b3(?:[.-]|$)', p.hostname)
                    and not AUTH.search(decoded))
    except (TypeError, ValueError):
        return False


def catalog(root):
    with zipfile.ZipFile(io.BytesIO(read(root, {'file': FCA, 'sha256': FCA_SHA}))) as archive:
        member = 'fca_cia_aberta_geral_2026.csv'
        rows = list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    result = {}
    for ticker, cvm in TARGETS.items():
        selected = [(n, row) for n, row in enumerate(rows, 2) if int(row['Codigo_CVM']) == cvm]
        if len(selected) != 1:
            raise ValueError('FCA_issuer_not_unique')
        line, row = selected[0]
        old, = [r for r in review['records'] if r['ticker'] == ticker]
        page = row['Pagina_Web'].strip()
        result[ticker] = {'role': 'FCA_declared_issuer_website', 'ticker': ticker,
            'cvm': cvm, 'issuer': row['Nome_Empresarial'], 'cnpj': row['CNPJ_Companhia'],
            'isin': old['isin'], 'share_class': old['share_class'],
            'url': page if '://' in page else 'https://' + page,
            'file': FCA, 'sha256': FCA_SHA, 'member': member, 'csv_line': line,
            'filing_id': row['ID_Documento'], 'review_file': REVIEW, 'review_sha256': REVIEW_SHA}
    return result


def primary_notice(root, ticker, reference_date, *, assembly=False):
    category, version = NOTICE_SELECTION[(ticker, reference_date)]
    if assembly:
        if (ticker, reference_date) != ('LREN3', '2021-11-04'):
            raise ValueError('unreviewed_assembly_selection')
        category, version = 'Assembleia', '1'
    year = int(reference_date[:4])
    source = {'file': f'raw/primary-research/cvm-documents/index-{year}-{IPE_PINS[year]}.zip',
              'sha256': IPE_PINS[year]}
    with zipfile.ZipFile(io.BytesIO(read(root, source))) as archive:
        member = f'ipe_cia_aberta_{year}.csv'
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        selected = [(n, r) for n, r in enumerate(rows, 2)
                    if int(r['Codigo_CVM']) == TARGETS[ticker]
                    and r['Data_Referencia'] == reference_date and r['Categoria'] == category
                    and (not assembly or r['Especie'] == 'Ata')
                    and r['Versao'] == version and re.search('bonifica', r['Assunto'], re.I)]
    if len(selected) != 1:
        raise ValueError('primary_notice_index_not_unique')
    line, row = selected[0]
    if row['CNPJ_Companhia'] != catalog(root)[ticker]['cnpj']:
        raise ValueError('primary_notice_issuer_mismatch')
    return {'role': 'cached_CVM_IPE_exact_notice', 'ticker': ticker,
            'url': row['Link_Download'], 'index': source, 'member': member,
            'csv_line': line, 'row': row, **({'assembly_minutes': True} if assembly else {})}


def classify_body(body):
    if re.search(rb'(?i)(cf-chl-|verify you are human|access denied|wp-signup\.php)', body):
        return 'public_content_access_challenge'
    if re.search(rb'(?is)<title[^>]*>[^<]*(captcha|access denied|just a moment|sign in)', body):
        return 'public_content_access_challenge'
    if not re.search(rb'(?i)(g-recaptcha|hcaptcha)', body):
        return 'no_challenge_detected'
    tree = html.fromstring(body, parser=html.HTMLParser(no_network=True))
    widgets = tree.xpath('//*[contains(@class,"g-recaptcha") or contains(@class,"h-captcha")]')
    if sha(body) in LIBRARY_ONLY_REVIEW:
        if widgets or not tree.xpath('//main') or len(tree.xpath('//a[@href]')) < 90:
            raise ValueError('reviewed_captcha_scope_evidence_changed')
        return 'reviewed_library_only_no_public_content_guard'
    forms_only = widgets and all(w.xpath('ancestor::form | ancestor::*[contains(@class,"custom_form")]')
                                for w in widgets)
    navigation = len(tree.xpath('//a[@href]')) > 20
    public_main = bool(tree.xpath('//main | //*[@id="content"] | //*[@id="main"]'))
    if forms_only and navigation and public_main:
        return 'optional_form_captcha_public_content_present_not_solved'
    return 'captcha_scope_unresolved_stop'


def effective_stop(receipt):
    if (receipt.get('http_status') == 200
            and receipt.get('stop_reason') == 'captcha_scope_unresolved_stop'
            and receipt.get('sha256') in LIBRARY_ONLY_REVIEW):
        return None
    return receipt.get('stop_reason')


def links(root, receipt):
    body = read(root, receipt)
    tree = html.fromstring(body, parser=html.HTMLParser(no_network=True)) if body.strip() else None
    result = []
    for tag, attr in (('a', 'href'), ('iframe', 'src'), ('script', 'src')):
        for n, node in enumerate(tree.xpath(f'//{tag}[@{attr}]') if tree is not None else [], 1):
            raw = node.get(attr)
            if not raw or raw.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
            result.append({'url': urljoin(receipt['url'], raw),
                           'locator': f'//{tag}[@{attr}][{n}]/@{attr}',
                           'label': ' '.join(node.text_content().split())[:120]})
    # React's public footer configuration can contain literal links before hydration.
    text = body.decode('utf-8', errors='replace')
    for match in re.finditer(r'''https?://[^\s<>"'`\\]+''', text):
        result.append({'url': match.group().replace('&amp;', '&'),
                       'locator': f'UTF8_literal_URL_offset:{match.start()}',
                       'label': 'literal public document URL'})
    if receipt.get('location'):
        result.append({'url': urljoin(receipt['url'], receipt['location']),
                       'locator': 'response.headers.Location', 'label': 'HTTP redirect'})
    return [{**r, 'allowed': allowed(r['url'])} for r in result]


def verify_chain(root, receipt, visited=None):
    visited = set() if visited is None else visited
    key = (receipt['url'], receipt['sha256'])
    if key in visited:
        raise ValueError('cyclic_source_chain')
    visited.add(key)
    read(root, receipt)
    if not allowed(receipt['url']) or effective_stop(receipt):
        raise ValueError('stopped_or_forbidden_chain')
    discovery = receipt['discovery']
    if discovery['role'] == 'FCA_declared_issuer_website':
        seed = catalog(root)[receipt['ticker']]
        if seed != discovery or seed['url'] != receipt['url']:
            raise ValueError('FCA_seed_mismatch')
    elif discovery['role'] == 'literal_official_link':
        parent = discovery['parent']
        verify_chain(root, parent, visited)
        if parent['ticker'] != receipt['ticker'] or not any(
                r['url'] == receipt['url'] and r['locator'] == discovery['locator']
                for r in links(root, parent)):
            raise ValueError('unproven_literal_link')
    elif discovery['role'] == 'published_widget_template':
        expected, proof = widget_request(root, receipt['ticker'], discovery['page'], discovery['bundle'],
            kind=discovery['kind'], configuration=discovery.get('configuration'))
        actual = discovery
        # The original /live receipt retained history planning defaults. Its wire
        # URL had no query. Preserve that receipt and normalize metadata only.
        if (discovery['kind'] == 'live' and receipt['ordinal'] == 14
                and receipt['sha256'] == '7b01bd04b53a1b2d1230b648a4376f4c4036de28e7b0951716abac91ec640670'
                and discovery['parameters'] == {'from': START, 'to': END, 'tickers': 'PSSA3', 'adjusted': False}):
            actual = {**discovery, 'parameters': {}}
        if expected != receipt['url'] or proof != actual:
            raise ValueError('widget_proof_mismatch')
    elif discovery['role'] == 'cached_CVM_IPE_exact_notice':
        expected = primary_notice(root, receipt['ticker'], discovery['row']['Data_Referencia'],
                                  assembly=discovery.get('assembly_minutes', False))
        if discovery != expected or receipt['url'] != expected['url']:
            raise ValueError('primary_notice_chain_mismatch')
    else:
        raise ValueError('unknown_source_chain')


def widget_request(root, ticker, page, bundle, *, kind='history', configuration=None):
    if ticker not in TARGETS or page['ticker'] != ticker:
        raise ValueError('unconfigured_ticker')
    verify_chain(root, page)
    page_text = read(root, page).decode('utf-8')
    script = read(root, bundle).decode('utf-8')
    if bundle['sha256'] != BUNDLE_SHA or bundle['http_status'] != 200 or bundle.get('stop_reason'):
        raise ValueError('unreviewed_widget_bundle')
    if bundle['url'] not in {r['url'] for r in links(root, page)}:
        raise ValueError('bundle_not_published_by_quote_page')
    ids = set(re.findall(r'''(?:stockInfoId|stockinfoId)\s*[:=]\s*["']([0-9a-f-]{36})["']''', page_text))
    configured = set(re.findall(r"tickers:\s*'([A-Z0-9,]+)'", page_text))
    if len(ids) != 1 or kind not in ('live', 'history'):
        raise ValueError('exact_configured_ticker_and_issuer_required')
    if kind == 'history' and configured != {ticker}:
        if not configuration:
            raise ValueError('exact_configured_ticker_and_issuer_required')
        verify_chain(root, configuration)
        config_proof = configuration['discovery']
        data = json.loads(read(root, configuration))
        if (config_proof.get('kind') != 'live' or config_proof['page'] != page
                or configuration['ticker'] != ticker or configuration['http_status'] != 200
                or data.get('success') is not True
                or [r['ticker'] for r in data['data']['tickers']] != [ticker]
                or data['data'].get('errors') or data['data'].get('source') != 'Enfoque'
                or data['data'].get('link') != 'https://www.enfoque.com.br'):
            raise ValueError('public_live_configuration_ticker_mismatch')
    bases = re.findall(r'API_STOCKINFO_URL:"(https://[^\"]+)"', script)
    formula = 't.API_URL+"/"+t.STOCK_ID+"/' + ('live"' if kind == 'live' else 'history?from="')
    if len(bases) != 1 or not all(s in script for s in (formula, 't.ADJUSTED=!1',
            'noProvents:"No"', 'yesWithProvents:"Yes, including dividends"',
            '"&tickers="+t.TICKER', '"&adjusted="+t.ADJUSTED')):
        raise ValueError('unreviewed_widget_formula')
    url = f'{bases[0]}/{next(iter(ids))}/{kind}'
    if kind == 'history':
        url += f'?from={START}&to={END}&tickers={ticker}&adjusted=false'
    if not allowed(url):
        raise ValueError('forbidden_widget_URL')
    return url, {'role': 'published_widget_template', 'page': page, 'bundle': bundle,
        'kind': kind, 'configuration': configuration,
        'formula': formula, 'formula_character_offset': script.index(formula),
        'parameters': ({'from': START, 'to': END, 'tickers': ticker, 'adjusted': False}
                       if kind == 'history' else {})}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class Fetcher:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def baseline(self):
        if not records(self.root, 'baseline'):
            state = {p: sha((self.root / p).read_bytes()) for p in PROTECTED}
            for name in ('issuer_raw_quote_research.py', 'test_issuer_raw_quote_research.py'):
                state['recommender/' + name] = sha(Path(__file__).with_name(name).read_bytes())
            save(self.root, 'baseline', {'protected_readonly': state, 'identity': catalog(self.root),
                'staged_snapshot': 'universe-20261025T100829-718298e9',
                'active_count_user_reported': 108, 'staged_count_user_reported': 110})

    def _wire(self, url, ticker, discovery):
        folder = safe_path(self.root, CACHE)
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / 'acquisition.lock'
        try:
            stream = lock.open('x')
        except FileExistsError as exc:
            raise ValueError('concurrent_or_interrupted_acquisition_lock') from exc
        try:
            with stream:
                return self._wire_locked(url, ticker, discovery)
        finally:
            lock.unlink()

    def _wire_locked(self, url, ticker, discovery):
        if ticker not in TARGETS or not allowed(url):
            raise ValueError('forbidden_URL_or_ticker_no_request')
        self.baseline()
        if records(self.root, 'seal'):
            raise ValueError('acquisition_sealed')
        attempts, responses = records(self.root, 'request'), records(self.root, 'response')
        if [r['ordinal'] for r in attempts] != list(range(1, len(attempts) + 1)):
            raise ValueError('invalid_request_ordinals')
        if len(attempts) != len(responses):
            raise ValueError('interrupted_request_consumed_no_retry')
        host = urlsplit(url).hostname
        for record in responses + records(self.root, 'blocked'):
            if effective_stop(record) and (record['ticker'] == ticker
                    or urlsplit(record['url']).hostname == host):
                raise ValueError('denied_chain_or_origin_no_alternate_route')
        if url in {r['url'] for r in attempts}:
            raise ValueError('already_attempted_no_retry')
        if len(attempts) >= MAX_REQUESTS:
            raise ValueError('twenty_request_budget_exhausted')
        attempt = {'ordinal': len(attempts) + 1, 'ticker': ticker, 'url': url,
            'discovery': discovery, 'started_at': datetime.now(timezone.utc).isoformat(),
            'automatic_redirects': False, 'retries': 0, 'proxy': False, 'authentication': 'none'}
        save(self.root, 'request', attempt)
        body, status, headers, error = b'', None, {}, None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                'Accept': '*/*', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                status, headers = response.status, response.headers
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, headers, error = exc.code, exc.headers, 'HTTPError'
            body = exc.read(MAX_BYTES + 1)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        classification = classify_body(body) if body else 'empty_body'
        stop = None
        location = headers.get('Location')
        if status in (401, 403, 429):
            stop = f'HTTP_{status}'
        elif location and not allowed(urljoin(url, location)):
            stop = 'forbidden_or_auth_redirect'
        elif classification in ('public_content_access_challenge', 'captcha_scope_unresolved_stop'):
            stop = classification
        elif len(body) > MAX_BYTES:
            stop = 'size_limit'
        elif error and status is None:
            stop = 'transport_failure_no_retry'
        receipt = {**attempt, 'http_status': status, 'error': error, 'location': location,
            'content_type': headers.get('Content-Type'), 'etag': headers.get('ETag'),
            'last_modified': headers.get('Last-Modified'), 'http_date': headers.get('Date'),
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'bytes': len(body),
            'sha256': sha(body), 'body_classification': classification, 'stop_reason': stop,
            'file': save(self.root, 'body', body, raw=True)}
        save(self.root, 'response', receipt)
        return receipt

    def get(self, url, ticker, parent=None, *, widget=None, kind='history', configuration=None,
            notice_date=None, assembly=False):
        if ticker not in TARGETS or not allowed(url):
            raise ValueError('forbidden_URL_or_ticker_no_request')
        if notice_date:
            discovery = primary_notice(self.root, ticker, notice_date, assembly=assembly)
            if url != discovery['url']:
                raise ValueError('exact_CVM_index_notice_required')
        elif widget:
            expected, discovery = widget_request(self.root, ticker, *widget,
                                                  kind=kind, configuration=configuration)
            if url != expected:
                raise ValueError('unpublished_widget_request')
        elif parent is None:
            discovery = catalog(self.root)[ticker]
            if url != discovery['url']:
                raise ValueError('exact_FCA_seed_required')
        else:
            verify_chain(self.root, parent)
            matches = [r for r in links(self.root, parent) if r['url'] == url]
            if parent['ticker'] != ticker or not matches:
                raise ValueError('literal_official_link_required')
            discovery = {'role': 'literal_official_link', 'parent': parent,
                         'locator': matches[0]['locator']}
        p = urlsplit(url)
        robots_url = f'{p.scheme}://{p.netloc}/robots.txt'
        robots_seen = set()
        while True:
            if robots_url in robots_seen:
                raise ValueError('robots_redirect_loop')
            robots_seen.add(robots_url)
            previous = [r for r in records(self.root, 'response') if r['url'] == robots_url]
            robot = previous[0] if previous else self._wire(robots_url, ticker,
                {'role': 'robots_for_discovered_target', 'target': url, 'source': discovery})
            if robot['stop_reason']:
                return robot
            if robot['http_status'] in (301, 302, 307, 308) and robot.get('location'):
                robots_url = urljoin(robots_url, robot['location'])
                continue
            if robot['http_status'] == 200:
                policy = urllib.robotparser.RobotFileParser()
                policy.parse(read(self.root, robot).decode('utf-8', errors='replace').splitlines())
                if policy.can_fetch(USER_AGENT, url):
                    break
                reason = 'robots_disallows'
            elif robot['http_status'] in (404, 410):
                break
            else:
                reason = 'robots_unavailable'
            save(self.root, 'blocked', {'url': url, 'ticker': ticker, 'stop_reason': reason, 'robots': robot})
            raise ValueError(reason)
        return self._wire(url, ticker, discovery)


def _no_constant(value):
    raise ValueError('nonfinite_JSON_number:' + value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_JSON_key')
        result[key] = value
    return result


def parse_payload(body, ticker, expected_sha):
    if ticker not in TARGETS or sha(body) != expected_sha:
        raise ValueError('unreviewed_history_ticker_or_hash')
    data = json.loads(body, parse_float=Decimal, parse_constant=_no_constant,
                      object_pairs_hook=_unique_object)
    if set(data) != {'success', 'data'} or data['success'] is not True:
        raise ValueError('history_not_successful')
    data = data['data']
    if (set(data) != {'tickers', 'indexes', 'errors', 'source', 'link'}
            or data['source'] != 'Enfoque' or data['link'] != 'https://www.enfoque.com.br'
            or data['errors']):
        raise ValueError('unreviewed_vendor_schema_or_authority')
    if (len(data['tickers']) != 1 or data['tickers'][0].get('ticker') != ticker
            or set(data['tickers'][0]) != {'ticker', 'history'}):
        raise ValueError('exact_history_ticker_required')
    result, seen = [], set()
    fields = ('var', 'high', 'low', 'open', 'closed', 'middle', 'vol', 'share', 'trade')
    for row in data['tickers'][0]['history']:
        if (set(row) != {'date', *fields} or not isinstance(row['date'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['date'])):
            raise ValueError('unreviewed_history_row_schema')
        day = date.fromisoformat(row['date']).isoformat()
        if day in seen or not START <= day <= END:
            raise ValueError('duplicate_or_out_of_range_date')
        seen.add(day)
        if any(isinstance(row[k], bool) or not isinstance(row[k], (int, Decimal)) for k in fields):
            raise ValueError('invalid_history_number')
        numbers = {k: Decimal(row[k]) for k in fields}
        if not all(v.is_finite() for v in numbers.values()):
            raise ValueError('nonfinite_history_number')
        if not (0 < numbers['low'] <= numbers['closed'] <= numbers['high']
                and numbers['low'] <= numbers['open'] <= numbers['high']
                and numbers['middle'] > 0 and numbers['vol'] > 0 and numbers['share'] > 0
                and numbers['trade'] > 0
                and all(numbers[k] == numbers[k].to_integral_value() for k in ('share', 'trade'))):
            raise ValueError('invalid_OHLC_or_activity')
        result.append({'date': day, **{k: str(v) for k, v in numbers.items()}})
    if not result:
        raise ValueError('empty_history')
    return sorted(result, key=lambda r: r['date']), {
        'retained_auxiliary_index_series': [{'ticker': x['ticker'], 'rows': len(x['history'])}
                                           for x in data['indexes']],
        'auxiliary_indexes_used_as_security_quotes': False}


def pinned_history(root, ticker):
    ordinal, digest = HISTORY_PINS[ticker]
    receipt, = [r for r in records(root, 'response') if r['ordinal'] == ordinal]
    if receipt['sha256'] != digest or receipt['http_status'] != 200:
        raise ValueError('history_receipt_pin_mismatch')
    verify_chain(root, receipt)
    rows, schema = parse_payload(read(root, receipt), ticker, digest)
    if len(rows) != 1268 or rows[0]['date'] != START or rows[-1]['date'] != END:
        raise ValueError('pinned_coverage_changed')
    page = read(root, receipt['discovery']['page'])
    currency = (b"currencySymbol: 'R$'" in page if ticker == 'LREN3'
                else b'.ticker-price\').html("R$ "' in page)
    if not currency:
        raise ValueError('BRL_unit_not_published')
    return rows, receipt, schema


def _compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()).lower()


def primary_actions(root, ticker):
    from pypdf import PdfReader

    actions = []
    for pin in ACTION_PINS[ticker]:
        receipt, = [r for r in records(root, 'response') if r['ordinal'] == pin['ordinal']]
        if receipt['sha256'] != pin['sha256'] or receipt['http_status'] != 200:
            raise ValueError('primary_action_pin_mismatch')
        verify_chain(root, receipt)
        body = read(root, receipt)
        if not body.startswith(b'%PDF-'):
            raise ValueError('primary_action_not_PDF_despite_HTTP_status')
        pdf = PdfReader(io.BytesIO(body))
        wanted = sorted(set(pin['ratio_pages'] + pin['date_pages'] + [1]))
        text = {page: pdf.pages[page - 1].extract_text() for page in wanted}
        ratios = _compact(' '.join(text[p] for p in pin['ratio_pages']))
        dates = _compact(' '.join(text[p] for p in pin['date_pages']))
        identity = catalog(root)[ticker]
        if (identity['cnpj'] not in text[1] or pin['ratio_text'] not in ratios
                or pin['native_ex_date'] not in dates
                or not any(w in dates for w in ('ex-direito', 'ex-bonificacao'))):
            raise ValueError('primary_action_exact_ratio_date_or_identity_unproven')
        native_day = datetime.strptime(pin['native_ex_date'],
            '%d.%m.%Y' if '.' in pin['native_ex_date'] else '%d/%m/%Y').date().isoformat()
        if native_day != pin['ex_date']:
            raise ValueError('primary_BR_ex_date_mismatch')
        actions.append({'kind': 'share_bonus', 'record_date': pin['record_date'],
            'ex_date': native_day, 'new_shares': pin['new'], 'per_old_shares': pin['old'],
            'total_factor_numerator': pin['new'] + pin['old'], 'total_factor_denominator': pin['old'],
            'total_factor_decimal': str((Decimal(pin['new']) + pin['old']) / pin['old']),
            'receipt': receipt, 'ratio_pages_one_based': pin['ratio_pages'],
            'ex_date_pages_one_based': pin['date_pages'],
            'native_ex_date': pin['native_ex_date'], 'primary_ratios_verified': True,
            'settlement_or_fractional_cash_proof': False})
    return actions


def factor_after(day, actions):
    factor = Decimal(1)
    for action in actions:
        if day < action['ex_date'] <= END:
            factor *= Decimal(action['total_factor_numerator']) / Decimal(action['total_factor_denominator'])
    return factor


def security_identity(root, ticker):
    seed = catalog(root)[ticker]
    with zipfile.ZipFile(io.BytesIO(read(root, {'file': FCA, 'sha256': FCA_SHA}))) as archive:
        member = 'fca_cia_aberta_valor_mobiliario_2026.csv'
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        selected = [(n, r) for n, r in enumerate(rows, 2) if r['Codigo_Negociacao'] == ticker]
    if len(selected) != 1:
        raise ValueError('FCA_exact_security_not_unique')
    line, row = selected[0]
    if (row['CNPJ_Companhia'] != seed['cnpj'] or row['ID_Documento'] != seed['filing_id']
            or _compact(row['Valor_Mobiliario']) != 'acoesordinarias' or row['Data_Fim_Negociacao']):
        raise ValueError('FCA_exact_security_mismatch')
    return {'issuer': seed, 'security_row': row, 'member': member, 'csv_line': line,
            'file': FCA, 'sha256': FCA_SHA, 'native_share_class': 'ON'}


def cached_inputs(root, ticker):
    review = json.loads(read(root, {'file': REVIEW, 'sha256': REVIEW_SHA}))
    old, = [r for r in review['records'] if r['ticker'] == ticker]
    sources = old['sources']
    daily, = [s for s in sources if s['file'].endswith(f'/{ticker}-prices.json')]
    cash, = [s for s in sources if '-b3-cash-' in s['file']]
    detail, = [s for s in sources if '/b3-detail-' in s['file']]
    identity = catalog(root)[ticker]
    data = json.loads(read(root, detail))
    if (int(data['codeCVM']) != TARGETS[ticker]
            or re.sub(r'\D', '', identity['cnpj']) != data['cnpj']
            or [r['isin'] for r in data['otherCodes'] if r['code'] == ticker] != [identity['isin']]):
        raise ValueError('cached_B3_instrument_identity_mismatch')
    return daily, cash, detail


def _br_number(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:,\d+)?', value):
        raise ValueError('invalid_native_BR_number')
    return Decimal(value.replace(',', '.'))


def reference_comparison(root, ticker, rows):
    _, source, detail = cached_inputs(root, ticker)
    page = json.loads(read(root, source))
    if (page['page']['pageNumber'] != 1 or page['page']['totalPages'] != 1
            or page['page']['totalRecords'] != len(page['results'])):
        raise ValueError('incomplete_cached_reference_pagination')
    grouped, unavailable, other_classes = {}, [], []
    for index, row in enumerate(page['results']):
        if row['typeStock'].strip() != 'ON':
            other_classes.append({'row_index': index, 'original_row': row})
            continue
        day = row.get('dateClosingPricePriorExDate')
        value = row.get('closingPricePriorExDate')
        if not day or day == '31/12/9999' or not value:
            unavailable.append({'row_index': index, 'original_row': row})
            continue
        if not re.fullmatch(r'\d{2}/\d{2}/\d{4}', day):
            raise ValueError('invalid_native_BR_reference_date')
        day = datetime.strptime(day, '%d/%m/%Y').date().isoformat()
        if not START <= day <= END:
            continue
        if _br_number(row['quotedPerShares']) != 1:
            raise ValueError('reference_not_per_one_share')
        price = _br_number(value)
        if price <= 0:
            raise ValueError('nonpositive_reference_quote')
        grouped.setdefault(day, []).append({'row_index': index, 'raw_close_BRL': str(price),
                                            'original_row': row})
    vendor = {r['date']: Decimal(r['closed']) for r in rows}
    anchors = []
    for day, evidence in sorted(grouped.items()):
        values = {r['raw_close_BRL'] for r in evidence}
        observed = vendor.get(day)
        conflict = len({Decimal(v) for v in values}) != 1
        match = (not conflict and observed is not None
                 and abs(observed - Decimal(next(iter(values)))) <= Decimal('.005'))
        anchors.append({'date': day, 'all_reference_rows': evidence,
            'conflicting_reference_values': conflict, 'independent_raw_close_BRL': str(observed),
            'matches_all_reference_values': match})
    return {'source': source, 'identity_source': detail, 'anchors': anchors,
        'anchor_dates': len(anchors), 'matches': sum(a['matches_all_reference_values'] for a in anchors),
        'model_window_anchor_dates': sum(a['date'] >= '2021-09-30' for a in anchors),
        'conflicts': [a for a in anchors if a['conflicting_reference_values']],
        'mismatches': [a for a in anchors if not a['matches_all_reference_values']],
        'excluded_other_share_class_rows': other_classes,
        'unavailable_reference_rows_retained': unavailable, 'tolerance_BRL': '.005',
        'reference_date_policy': 'literal dateClosingPricePriorExDate DD/MM/YYYY; never move to record/ex/payment date'}


def daily_comparison(root, ticker, rows, actions):
    source, _, _ = cached_inputs(root, ticker)
    data = json.loads(read(root, source), parse_float=Decimal)['chart']['result'][0]
    if (data['meta']['symbol'] != ticker + '.SA' or data['meta']['currency'] != 'BRL'
            or data['meta']['exchangeTimezoneName'] != 'America/Sao_Paulo'):
        raise ValueError('Yahoo_identity_unit_or_timezone_mismatch')
    close = data['indicators']['quote'][0]['close']
    if len(close) != len(data['timestamp']):
        raise ValueError('Yahoo_timestamp_length_mismatch')
    daily = {}
    for stamp, value in zip(data['timestamp'], close):
        day = datetime.fromtimestamp(stamp, BR_TIME).date().isoformat()
        if day in daily:
            raise ValueError('duplicate_Yahoo_BR_trading_date')
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value <= 0:
                raise ValueError('invalid_Yahoo_close')
            daily[day] = Decimal(value)
    missing, mismatches, regimes, monthly = [], [], {}, {}
    broad_matches = strict_matches = 0
    for row in rows:
        day = row['date']
        factor = factor_after(day, actions)
        expected = Decimal(row['closed']) / factor
        regime = regimes.setdefault(str(factor), {'first': day, 'last': day, 'rows': 0})
        regime['last'], regime['rows'] = day, regime['rows'] + 1
        monthly[day[:7]] = {'date': day, 'raw_close_BRL': row['closed'],
            'primary_factor_after': str(factor), 'raw_divided_by_primary_factor': str(expected)}
        if day not in daily:
            missing.append(day)
            continue
        delta = abs(expected - daily[day])
        strict = delta <= Decimal('.015')
        broad = delta <= max(Decimal('.015'), abs(expected) * Decimal('.006'))
        strict_matches += strict
        broad_matches += broad
        if not strict:
            mismatches.append({'date': day, 'vendor_raw_close_BRL': row['closed'],
                'primary_factor_after': str(factor), 'raw_divided_by_primary_factor': str(expected),
                'cached_Yahoo_close': str(daily[day]), 'difference_BRL': str(delta),
                'within_existing_point_six_percent_band': broad})
    for row in monthly.values():
        day = row['date']
        row['cached_Yahoo_close'] = str(daily.get(day))
        row['strict_match'] = day in daily and abs(
            Decimal(row['raw_divided_by_primary_factor']) - daily[day]) <= Decimal('.015')
    if len(monthly) != 61:
        raise ValueError('incomplete_monthly_coverage')
    return {'source': source, 'BR_date_policy': 'Unix timestamps converted to fixed UTC-03 America/Sao_Paulo for 2021-2026',
        'price_field': 'indicators.quote.close; never adjclose',
        'formula': 'vendor raw close / product(primary new-total / old before ex-date)',
        'strict_tolerance_BRL_on_Yahoo_scale': '.015', 'daily_strict_matches': strict_matches,
        'daily_strict_mismatches': mismatches, 'missing_dates': missing,
        'existing_point_six_percent_band_matches': broad_matches,
        'existing_point_six_percent_band_mismatches': [x for x in mismatches if not x['within_existing_point_six_percent_band']],
        'regimes': regimes, 'monthly_raw_anchors': list(monthly.values()),
        'monthly_conflicts': [x for x in monthly.values() if not x['strict_match']],
        'fitted_factors': [], 'conflict_rows_replaced': [], 'model_admission_granted': False}


def native_basis_checks(rows, actions):
    results, violations = [], []
    dates = {r['date']: n for n, r in enumerate(rows)}
    wanted = {0, len(rows) - 1}
    for action in actions:
        if action['ex_date'] not in dates:
            raise ValueError('missing_action_boundary_observation')
        index = dates[action['ex_date']]
        wanted.update((index - 1, index))
    for n, row in enumerate(rows):
        average = Decimal(row['vol']) / Decimal(row['share'])
        within = Decimal(row['low']) - Decimal('.015') <= average <= Decimal(row['high']) + Decimal('.015')
        if not within:
            violations.append({'date': row['date'], 'turnover_over_shares': str(average)})
        if n in wanted:
            results.append({**row, 'turnover_over_share_quantity_BRL': str(average),
                'turnover_over_shares_within_native_OHLC': within,
                'primary_factor_after': str(factor_after(row['date'], actions))})
    return {'all_daily_turnover_share_OHLC_checks': len(rows), 'violations': violations,
        'boundary_and_endpoint_rows': results,
        'unit_basis': 'RI R$ display; vol/share quotient falls inside native per-share OHLC on every day',
        'field_semantics': {'vol': 'financial turnover, BRL', 'share': 'traded share quantity',
                            'trade': 'trade count', 'closed': 'native close, BRL per then-existing ON share'},
        'adjusted_false_alone_used_as_raw_proof': False,
        'corroboration': 'CVM exact bonus boundaries, cached B3 native anchors, all-row turnover/share arithmetic, Yahoo comparison using primary factors',
        'cash_dividend_adjustment_applied': False, 'source_values_transformed': False}


def _pssa_source_context(root):
    reference = {'file': f'{CACHE}/pssa-source-context-{PSSA_CONTEXT_SHA}.json',
                 'sha256': PSSA_CONTEXT_SHA}
    context = json.loads(read(root, reference))
    manifests = []
    for source in context['evidence_manifests']:
        name = source['file']
        if (not re.fullmatch(r'(?:inventory/)?snapshots/[A-Za-z0-9-]+/(?:market|assets)\.json', name)
                and name != 'raw/primary-research/cvm-documents/index-sources.json'):
            raise ValueError('unreviewed_source_context_manifest')
        path = Path(root).resolve() / name
        if (not path.resolve().is_relative_to(Path(root).resolve()) or path.is_symlink()
                or path.stat().st_size > MAX_BYTES):
            raise ValueError('unsafe_source_context_manifest')
        body = path.read_bytes()
        if sha(body) != source['sha256'] or len(body) != source['bytes']:
            raise ValueError('source_context_manifest_hash_mismatch')
        parsed = json.loads(body)
        manifests.extend(parsed if isinstance(parsed, list) else parsed['sources'])
    for source in context['sources']:
        read(root, source)
        if not any(all(candidate.get(k) == source[k] for k in ('file', 'sha256', 'url'))
                   for candidate in manifests):
            raise ValueError('source_context_URL_not_in_pinned_manifest')
    return context, reference


def _pssa_observed_action_chain(observed, primary):
    if not isinstance(observed, list) or len(observed) != len(primary):
        raise ValueError('PSSA_observed_primary_action_chain_mismatch')
    expected = {action['ex_date']: Fraction(action['total_factor_numerator'],
                                          action['total_factor_denominator']) for action in primary}
    actual = {}
    for action in observed:
        if (not isinstance(action, dict) or not isinstance(action.get('date'), str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', action['date'])
                or type(action.get('ratio')) not in (int, float)
                or not math.isfinite(action['ratio']) or action['ratio'] <= 0
                or action['date'] in actual):
            raise ValueError('PSSA_observed_primary_action_chain_mismatch')
        actual[action['date']] = Fraction(str(action['ratio']))
    if actual != expected:
        raise ValueError('PSSA_observed_primary_action_chain_mismatch')
    return [{**action, 'date': action['ex_date'],
             'ratio_numerator': action['total_factor_numerator'],
             'ratio_denominator': action['total_factor_denominator'],
             'ratio': float(expected[action['ex_date']])} for action in primary]


def get_pssa_independent_raw_quotes(root, identity, cutoff, observed_actions,
                                    existing_quotes, *, price_content):
    """Verified secondary RAW anchors; keep the caller's Yahoo bytes untouched.

    Call immediately before share_basis.reconcile_prices, passing its existing
    exchange quotes and observed actions. Register every returned source and
    use proof.source_label for the mixed anchor origin. No admission is made.
    """
    from .broad_stocks import split_price_history
    from .share_basis import b3_evidence

    if (not isinstance(identity, dict) or type(identity.get('cvm')) is not int
            or any(identity.get(k) != v for k, v in PSSA_IDENTITY.items())
            or identity.get('unit_composition') is not None):
        raise ValueError('PSSA_exact_identity_required_no_LREN_or_class_substitution')
    if type(cutoff) is not date or cutoff != date(2026, 9, 30):
        raise ValueError('PSSA_exact_reviewed_cutoff_required')
    if identity.get('cnpj') is not None and re.sub(r'\D', '', identity['cnpj']) != '02149205000169':
        raise ValueError('PSSA_exact_CNPJ_required')
    sealed = records(root, 'seal')
    attempts, current = records(root, 'request'), records(root, 'response')
    if (len(sealed) != 1 or not sealed[0]['network_closed']
            or len(attempts) != 20 or len(current) != 20):
        raise ValueError('PSSA_sealed_acquisition_ledger_required')
    report_ref = {'file': f'{CACHE}/report-{ACQUISITION_REPORT_SHA}.json',
                  'sha256': ACQUISITION_REPORT_SHA}
    report = json.loads(read(root, report_ref))
    frozen = {r['ordinal']: r for r in report['requests']}
    actual = {r['ordinal']: r for r in current}
    for ordinal in (5, 6, 7, 8, 12, 14, 15, 16, 19):
        if actual.get(ordinal) != frozen[ordinal]:
            raise ValueError('PSSA_publisher_config_or_ratio_receipt_changed')
        read(root, actual[ordinal])
        if any(actual[ordinal].get(k) != v for k, v in attempts[ordinal - 1].items()):
            raise ValueError('PSSA_acquisition_request_response_mismatch')
    security = security_identity(root, 'PSSA3')
    context, context_ref = _pssa_source_context(root)
    daily_source, exchange_source, _ = cached_inputs(root, 'PSSA3')
    if not isinstance(price_content, bytes) or price_content != read(root, daily_source):
        raise ValueError('PSSA_primary_Yahoo_bytes_must_remain_unchanged')
    _, cached_actions = split_price_history(price_content, 'PSSA3', cutoff, allow_recent_actions=True)
    primary = primary_actions(root, 'PSSA3')
    chain = _pssa_observed_action_chain(observed_actions, primary)
    _pssa_observed_action_chain(cached_actions, primary)
    rows, response, schema = pinned_history(root, 'PSSA3')
    native = native_basis_checks(rows, primary)
    reference = reference_comparison(root, 'PSSA3', rows)
    daily = daily_comparison(root, 'PSSA3', rows, primary)
    if (native['violations'] or reference['conflicts'] or reference['mismatches']
            or reference['matches'] != 19 or daily['missing_dates']
            or daily['existing_point_six_percent_band_mismatches']):
        raise ValueError('PSSA_unresolved_native_quote_or_price_disagreement')
    page = json.loads(read(root, exchange_source))
    _, cached_exchange_quotes = b3_evidence(page['results'], 'ON')
    if not isinstance(existing_quotes, dict) or any(
            type(day) is not date or type(value) not in (int, float)
            or not math.isfinite(value) or value <= 0 for day, value in existing_quotes.items()):
        raise ValueError('PSSA_existing_exchange_quotes_must_be_typed')
    native_quotes = {date.fromisoformat(row['date']): float(row['closed']) for row in rows}
    overlap = sorted(set(existing_quotes) & set(native_quotes))
    if any(Decimal(str(existing_quotes[day])) != Decimal(str(native_quotes[day])) for day in overlap):
        raise ValueError('PSSA_shared_exchange_Enfoque_anchor_disagreement')
    if existing_quotes != cached_exchange_quotes:
        raise ValueError('PSSA_existing_cached_exchange_quotes_must_be_preserved')
    if len(overlap) != 19:
        raise ValueError('PSSA_expected_nineteen_shared_anchors_missing')
    merged = dict(existing_quotes)
    merged.update({day: value for day, value in native_quotes.items() if day not in merged})

    sources = {}

    def register(source, role):
        body = read(root, source)
        if not source.get('url'):
            raise ValueError('PSSA_verified_source_URL_missing')
        previous = sources.get(source['file'])
        if previous and (previous['sha256'] != source['sha256'] or previous['url'] != source['url']):
            raise ValueError('PSSA_conflicting_source_registration')
        sources[source['file']] = {**source, 'bytes': len(body), 'role': role,
            'access': 'existing_cache_read_only', 'network_requests': 0}

    for source in context['sources']:
        register(source, 'pinned_identity_primary_prices_or_primary_action_index')
    register(exchange_source, 'cached_exchange_raw_reference_quotes')
    register(frozen[6], 'FCA_declared_issuer_homepage')
    register(frozen[8], 'official_issuer_quote_widget_publisher')
    register(frozen[14], 'public_exact_PSSA3_widget_configuration_not_historical_price')
    register(response, 'independent_issuer_Enfoque_raw_daily_quote_anchors')
    register(response['discovery']['bundle'], 'issuer_published_pinned_widget_program')
    register(frozen[19], 'primary_CVM_exact_bonus_ratio_and_ex_date')
    first, last = min(native_quotes), max(native_quotes)
    return {'quotes': merged, 'sources': sorted(sources.values(), key=lambda r: r['file']),
        'primary_actions': chain,
        'proof': {'identity': dict(PSSA_IDENTITY), 'cutoff': cutoff.isoformat(),
            'source_label': PSSA_ANCHOR_LABEL, 'security': security,
            'acquisition_report': report_ref, 'source_context': context_ref,
            'acquisition_requests': 20, 'acquisition_sealed': True,
            'new_network_requests': 0, 'primary_Yahoo_sha256': daily_source['sha256'],
            'primary_Yahoo_bytes_replaced': False, 'synthetic_provider_JSON_created': False,
            'native_Enfoque_quote_dates': len(native_quotes),
            'first_Enfoque_date': first.isoformat(), 'last_Enfoque_date': last.isoformat(),
            'existing_exchange_quote_dates_preserved': len(existing_quotes),
            'shared_exchange_Enfoque_dates': [day.isoformat() for day in overlap],
            'shared_anchor_prices_replaced': False, 'combined_quote_dates': len(merged),
            'native_basis_checks': native, 'schema': schema,
            'documented_Yahoo_differences': daily['daily_strict_mismatches'],
            'all_Enfoque_dates_within_unchanged_price_band': True,
            'normal_minimum_four_and_every_regime_checks_still_required': True,
            'price_band': {'relative': .006, 'absolute_BRL': .015},
            'observed_vendor': 'Enfoque',
            'licensing_contract_verified': False,
            'ultimate_upstream_independence_verified': False,
            'vendor_provenance_limit': 'Published API names Enfoque; licensing contract and ultimate upstream independence unverified',
            'price_factor_fitted': False, 'model_admission_granted': False,
            'LREN_adapter_enabled': False}}


def trial_pssa_parent_integration(root):
    """Offline, process-local hook trial; production functions/files stay intact."""
    from unittest.mock import patch

    from . import inventory_candidates as parent
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory

    cutoff = date(2026, 9, 30)
    inventory = load_inventory(root)
    row, = [r for r in inventory['stocks'] if r['ticker'] == 'PSSA3']
    scoped = {**inventory, 'stocks': [row]}
    protected_files = {name: Path(root) / name for name in PROTECTED}
    protected_files.update({f'recommender/{name}': Path(__file__).with_name(name) for name in (
        'share_basis.py', 'inventory_candidates.py', 'inventory_financials.py', 'collect_market_data.py')})
    before = {name: sha(path.read_bytes()) for name, path in protected_files.items()}
    real_reconcile = parent.reconcile_prices
    calls, extra_sources = [], {}

    def temporary_hook(content, ticker, actions, quotes, end, **kwargs):
        if ticker != 'PSSA3' or end != cutoff:
            raise ValueError('PSSA_trial_scope_mismatch')
        supplement = get_pssa_independent_raw_quotes(root, row, end, actions, quotes,
                                                    price_content=content)
        for source in supplement['sources']:
            extra_sources[source['file']] = source
        call = {'coverage_start': kwargs.get('coverage_start', date(2021, 9, 30)).isoformat(),
                'proof': supplement['proof'], 'primary_actions': supplement['primary_actions']}
        calls.append(call)
        try:
            result = real_reconcile(content, ticker, actions, supplement['quotes'], end, **kwargs)
        except ValueError as exc:
            call.update(status='blocked_by_unchanged_reconciler', reason=str(exc))
            raise
        prices, matches, mode = result
        call.update(status='passed_unchanged_reconciler', monthly_prices=len(prices),
                    independent_anchor_matches=len(matches), detected_mode=mode)
        return result

    def no_network(*args, **kwargs):
        raise AssertionError('offline_PSSA_trial_attempted_network')

    with patch.object(urllib.request.OpenerDirector, 'open', no_network), patch('socket.create_connection', no_network):
        base, base_pending, base_sources = parent.build_candidates(root, scoped, cutoff)
        installed = bool(base and base[0]['checks'].get('independent_raw_quote_evidence', {}).get(
            'source_label') == PSSA_ANCHOR_LABEL)
        if base and not installed:
            raise ValueError('PSSA_parent_candidate_missing_registered_anchor_proof')
        if installed:
            candidates, pending, candidate_sources = base, base_pending, base_sources
            calls.append({'status': 'installed_parent_hook_used_without_monkeypatch',
                          'proof': base[0]['checks']['independent_raw_quote_evidence'],
                          'primary_actions': base[0]['checks']['primary_share_action_evidence']})
        else:
            with patch.object(parent, 'reconcile_prices', temporary_hook):
                candidates, pending, candidate_sources = parent.build_candidates(root, scoped, cutoff)
        if parent.reconcile_prices is not real_reconcile:
            raise ValueError('temporary_hook_not_restored')
        statements, financial_audit, financial_sources = prepare_financials(root, [row], cutoff)
        annual = [r for r in financial_audit if r.get('kind') == 'candidate' and r.get('ticker') == 'PSSA3']
        financial = {'annual_candidate_audit': annual, 'statement_records': len(statements),
                     'financial_features_available': False, 'post_action_fiscal_basis_valid': False,
                     'training_observations_after_normal_filters': 0}
        candidate_summary = None
        if candidates:
            candidate, = candidates
            spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
            current = financial_features(statements, candidate['payments'], cutoff, spec)
            basis = date.fromisoformat(candidate['financial_basis_start'])
            training, coverage = stock_observations('PSSA3', candidate['prices'], candidate['payments'],
                                                   statements, cutoff, spec=spec)
            training = [r for r in training if date.fromisoformat(r['as_of']) >= candidate['training_since']
                        and date.fromisoformat(r['fiscal_end']) >= basis]
            financial.update(financial_features_available=current is not None,
                post_action_fiscal_basis_valid=bool(current and date.fromisoformat(current[1]['fiscal_end']) >= basis),
                training_observations_after_normal_filters=len(training),
                first_training_as_of=training[0]['as_of'] if training else None,
                last_training_as_of=training[-1]['as_of'] if training else None,
                stock_observations_coverage=coverage)
            if current:
                features, filing = current
                financial.update(features=features, latest_filing={k: filing.get(k) for k in (
                    'cvm', 'share_class', 'fiscal_end', 'published_at', 'filing_id',
                    'financial_method', 'source_ref', 'source_file')})
            candidate_summary = {k: candidate.get(k) for k in (
                'ticker', 'cvm', 'share_class', 'isin', 'sector_code', 'checks',
                'share_actions', 'financial_basis_start', 'price_url', 'price_source_file',
                'independent_cash_matches', 'independent_price_matches', 'readiness_scope',
                'financially_validated')}
            candidate_summary.update(monthly_prices=len(candidate['prices']),
                payment_events=len(candidate['payments']), reconciled_ledger_rows=len(candidate['ledger']),
                training_since=candidate['training_since'].isoformat(),
                temporary_anchor_source_label=PSSA_ANCHOR_LABEL)
    sources = {s['file']: s for s in [*candidate_sources, *financial_sources]}
    for name, source in extra_sources.items():
        previous = sources.get(name)
        if previous and (previous['sha256'] != source['sha256']
                         or previous.get('url') not in (None, source['url'])):
            raise ValueError('PSSA_trial_source_registry_disagreement')
        sources[name] = source
    after = {name: sha(path.read_bytes()) for name, path in protected_files.items()}
    sufficient = bool(candidate_summary and financial['financial_features_available']
                      and financial['post_action_fiscal_basis_valid'] and not pending)
    return {'schema_version': 1, 'scope': 'PSSA3_only_offline_parent_hook_trial',
        'cutoff': cutoff.isoformat(), 'baseline_candidates': len(base), 'baseline_pending': base_pending,
        'candidate_count_with_temporary_hook': len(candidates), 'pending_with_temporary_hook': pending,
        'candidate': candidate_summary, 'financial_gates': financial, 'reconcile_calls': calls,
        'source_hook_sufficient_for_existing_cash_and_financial_checks': sufficient,
        'recommended_hook': 'inventory_candidates._one immediately before reconcile_prices; '
            'supplement typed quotes, register getter sources, label independent issuer Enfoque plus cached exchange',
        'parent_metadata_required': ['register every getter source URL/hash',
            'retain primary action proof and four documented Yahoo differences',
            'label mixed independent anchors; do not call them all B3',
            'keep original Yahoo primary source file and bytes'],
        'sources': sorted(sources.values(), key=lambda r: r['file']),
        'network_requests': 0, 'acquisition_requests_remain': len(records(root, 'request')),
        'temporary_hook_removed': parent.reconcile_prices is real_reconcile,
        'parent_hook_already_installed': installed,
        'temporary_reconcile_hook_applied': not installed,
        'production_files_written': False, 'pointers_written': False, 'model_admissions': 0,
        'protected_before': before, 'protected_after_observed': after,
        'concurrent_external_changes': [k for k in before if before[k] != after[k]],
        'producer_sha256': sha(Path(__file__).read_bytes()),
        'test_sha256': sha(Path(__file__).with_name('test_issuer_quote_extension_research.py').read_bytes())}


def build_report(root):
    attempts, responses = records(root, 'request'), records(root, 'response')
    if len(attempts) != len(responses) or len(attempts) > MAX_REQUESTS:
        raise ValueError('incomplete_or_over_budget_ledger')
    for n, (attempt, receipt) in enumerate(zip(attempts, responses), 1):
        if attempt['ordinal'] != n or any(receipt.get(k) != v for k, v in attempt.items()):
            raise ValueError('request_response_ledger_mismatch')
        read(root, receipt)
        if not allowed(receipt['url']):
            raise ValueError('forbidden_request_in_ledger')
        if receipt['discovery']['role'] != 'robots_for_discovered_target':
            verify_chain(root, receipt)
    proofs = {}
    for ticker in TARGETS:
        rows, receipt, schema = pinned_history(root, ticker)
        actions = primary_actions(root, ticker)
        reference = reference_comparison(root, ticker, rows)
        daily = daily_comparison(root, ticker, rows, actions)
        native = native_basis_checks(rows, actions)
        blockers = []
        expected_anchors = 21 if ticker == 'LREN3' else 19
        if reference['anchor_dates'] != expected_anchors:
            blockers.append('incomplete_expected_cached_B3_anchor_coverage')
        if reference['mismatches']:
            blockers.append('cached_B3_native_reference_conflict')
        if daily['daily_strict_mismatches']:
            blockers.append('unresolved_Enfoque_vs_cached_Yahoo_daily_close_disagreements')
        if daily['missing_dates']:
            blockers.append('missing_cached_Yahoo_daily_dates')
        if native['violations']:
            blockers.append('native_turnover_quantity_unit_inconsistency')
        proofs[ticker] = {'identity': security_identity(root, ticker), 'history_response': receipt,
            'raw_daily_observations': rows, 'schema': schema, 'rows': len(rows),
            'first_date': rows[0]['date'], 'last_date': rows[-1]['date'],
            'primary_share_actions': actions, 'native_basis_checks': native,
            'cached_B3_reference_comparison': reference, 'cached_Yahoo_daily_comparison': daily,
            'raw_history_source_gap_recovered': (not native['violations'] and not reference['mismatches']
                                                  and reference['anchor_dates'] == expected_anchors),
            'full_existing_price_history_reproduced': not blockers,
            'remaining_blockers': blockers, 'model_admission_granted': False,
            'authority': 'Enfoque vendor via issuer-published MZ widget; not exchange-originated observations',
            'independence_limit': 'API names Enfoque, no Yahoo acquisition; upstream vendor supply chain not independently audited',
            'date_basis': 'literal ISO trading-date field; distinct from record, ex, credit, payment, publication dates',
            'publication_time': None, 'point_in_time_archive_proven': False,
            'cash_events_created': 0, 'production_writes': False}
    baseline, = records(root, 'baseline')
    before = baseline['protected_readonly']
    after = {}
    for name in before:
        path = (Path(__file__).parent.parent / name if name.startswith('recommender/')
                else Path(root) / name)
        after[name] = sha(path.read_bytes())
    empty = next(r for r in responses if r['ordinal'] == 17)
    return {'schema_version': 1, 'cutoff': END, 'model_start': '2021-09-30',
        'staged_snapshot_user_reported': 'universe-20261025T100829-718298e9',
        'acquisition_requests': len(attempts), 'maximum_requests': MAX_REQUESTS,
        'unused_budget': MAX_REQUESTS - len(attempts), 'requests': responses,
        'network_sealed': bool(records(root, 'seal')), 'network_requests_during_report': 0,
        'denied_origins_never_requested': list(DENIED),
        'B3_network_requests': 0, 'BRAPI_network_requests': 0, 'Yahoo_network_requests': 0,
        'model_admissions': 0, 'production_parser_installed': False,
        'optional_KLBN_research': 'not_started_budget_reserved_for_required_issuers',
        'live_configuration_use': {'ordinal': 14, 'actual_URL_query_parameters': {},
            'original_receipt_preserved': True, 'original_history_planning_defaults_not_wire_parameters': True,
            'historical_prices_taken_from_live_response': False},
        'raw_proofs': proofs, 'classification_reviews': records(root, 'classification-review'),
        'unavailable_primary_notice': {'receipt': empty,
            'classification': 'HTTP_200_public_download_form_without_notice_body_or_challenge',
            'notice_ratio_not_inferred_from_index_subject': True, 'same_URL_retried': False,
            'separate_indexed_assembly_minutes_used': True},
        'protected_state': {'before': before, 'after_observed': after,
            'concurrent_external_changes': [k for k in before if before[k] != after[k]],
            'writes_by_this_research': False},
        'producer_sha256': sha(Path(__file__).read_bytes()),
        'test_sha256': sha(Path(__file__).with_name('test_issuer_quote_extension_research.py').read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--catalog', action='store_true')
    parser.add_argument('--get')
    parser.add_argument('--ticker', choices=tuple(TARGETS))
    parser.add_argument('--parent-ordinal', type=int)
    parser.add_argument('--links', type=int)
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--trial-pssa', action='store_true')
    args = parser.parse_args()
    if args.trial_pssa:
        report = trial_pssa_parent_integration(args.root)
        print(save(args.root, 'pssa-integration-trial', report))
        print(json.dumps({k: report[k] for k in (
            'candidate_count_with_temporary_hook', 'pending_with_temporary_hook',
            'source_hook_sufficient_for_existing_cash_and_financial_checks',
            'network_requests', 'model_admissions')}, indent=2))
        print(json.dumps(report['financial_gates']['annual_candidate_audit'], indent=2))
    elif args.report:
        report = build_report(args.root)
        print(save(args.root, 'report', report))
        print(json.dumps({t: {'rows': p['rows'], 'blockers': p['remaining_blockers'],
            'B3_matches': p['cached_B3_reference_comparison']['matches'],
            'daily_conflicts': len(p['cached_Yahoo_daily_comparison']['daily_strict_mismatches'])}
            for t, p in report['raw_proofs'].items()}, indent=2))
    elif args.get:
        parents = [r for r in records(args.root, 'response') if r['ordinal'] == args.parent_ordinal]
        result = Fetcher(args.root).get(args.get, args.ticker, parents[0] if parents else None)
        print(json.dumps({k: v for k, v in result.items() if k != 'discovery'}, indent=2))
    elif args.links:
        receipt, = [r for r in records(args.root, 'response') if r['ordinal'] == args.links]
        print(json.dumps(links(args.root, receipt), indent=2))
    else:
        print(json.dumps(catalog(args.root), indent=2))


if __name__ == '__main__':
    main()
