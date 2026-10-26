"""Bounded public RI source discovery for the user-selected stock universe.

Preserves originals and a persistent request ledger. Discovery is not calendar
coverage, model approval, bank settlement or historical availability.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit
import urllib.error
import urllib.request

from lxml import html
from protego import Protego

from .dividend_scope import allowed_tickers


CACHE = 'raw/primary-research/selected-issuer-sources'
MAX_REQUESTS = 12
MAX_BYTES = 8_000_000
USER_AGENT = 'RendaFutura-Academic/1.0'
SEEDS = {
    'celesc-dividends': 'https://ri.celesc.com.br/servicos-aos-investidores/dividendos/',
    'celesc-quotes': 'https://ri.celesc.com.br/servicos-aos-investidores/cotacoes-e-graficos/',
    'celesc-downloads': 'https://ri.celesc.com.br/informacoes-financeiras/central-de-downloads/',
}
HOSTS = {'ri.celesc.com.br', 'api.mziq.com', 'apicatalog.mziq.com', 'cdn.mziq.com',
         'filemanager-cdn.mziq.com', 'www.mzweb.com.br', 'mzweb.com.br'}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def approved(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in HOSTS
                and not parsed.username and not parsed.password and not parsed.fragment
                and parsed.port in (None, 443)
                and not any(re.search(r'(?i)token|secret|password|auth|api.?key|signature', key)
                            for key, _ in parse_qsl(parsed.query))
                and not re.search(r'(?i)(?:login|signup|captcha|password|proxy|oauth|authorize)', url))
    except (TypeError, ValueError):
        return False


def _save(base, name, body):
    path = base / name
    if path.exists():
        _require(not path.is_symlink() and path.read_bytes() == body, 'immutable_issuer_source_changed')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path


def _dump(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def source_body(folder, receipt):
    relative = receipt.get('file')
    _require(isinstance(relative, str) and re.fullmatch(CACHE + r'/original-[a-f0-9]{64}\.(?:html|bin)', relative),
             'unsafe_issuer_original_reference')
    root = Path(folder).resolve()
    path = root / relative
    _require(not path.is_symlink() and path.resolve().is_relative_to(root)
             and path.stat().st_size <= MAX_BYTES + 1, 'unsafe_issuer_original_file')
    body = path.read_bytes()
    _require(hashlib.sha256(body).hexdigest() == receipt.get('sha256')
             and len(body) == receipt.get('bytes'), 'issuer_original_hash_or_length_changed')
    return body


def links(body, url):
    if body.lstrip().startswith((b'{', b'[')):
        values = []

        def visit(value, locator='$'):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('link_url', 'permalink', 'file_url') and isinstance(item, str) and approved(item):
                        values.append({'url': item, 'locator': locator + '.' + key,
                                       'text': value.get('file_title', '')})
                    visit(item, locator + '.' + key)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    visit(item, f'{locator}[{index}]')
        visit(json.loads(body))
        return values
    tree = html.fromstring(body, parser=html.HTMLParser(no_network=True))
    values = []
    for tag, attribute in (('a', 'href'), ('script', 'src'), ('iframe', 'src')):
        for index, node in enumerate(tree.xpath(f'//{tag}[@{attribute}]'), 1):
            raw = node.get(attribute)
            absolute = urljoin(url, raw)
            if approved(absolute):
                values.append({'url': absolute, 'locator': f'//{tag}[@{attribute}][{index}]',
                               'text': ' '.join(node.text_content().split())[:160]})
    return values


def catalog_request(folder, page, script):
    base, _, responses = ledger(folder)
    _require(page in responses and script in responses, 'catalogue_proofs_not_in_actual_ledger')
    bodies = []
    for receipt in (page, script):
        body = source_body(folder, receipt)
        _require(receipt['status'] == 200 and not receipt['stop']
                 and hashlib.sha256(body).hexdigest() == receipt['sha256'], 'catalogue_source_changed')
        bodies.append(body.decode('utf-8'))
    page_text, script_text = bodies
    _require(page['url'] == SEEDS['celesc-dividends']
             and script['url'] == 'https://ri.celesc.com.br/wp-content/themes/mziq_celesc_ri/js/mziq.cmsint.js'
             and any(row['url'] == script['url'] for row in links(
                 (Path(folder) / script['parent']['file']).read_bytes(), script['parent']['url'])),
             'catalogue_wrong_public_page_or_library')
    company = re.findall(r'var fmId = "([a-f0-9-]{36})";', page_text)
    api = re.findall(r'var fmBase = "(https://api\.mziq\.com/mzfilemanager)";', page_text)
    _require(company == ['137b4414-3d0c-493e-8b59-0d02bc3e4072'] and len(api) == 1
             and "internal_name: 'dividendos'" in page_text
             and 'getAllYears: true' in page_text and "language: 'pt_BR'" in page_text
             and "BASE_API_URL + '/company/' + companyId + '/filter/categories/meta'" in script_text
             and 'categoryInternalNames: loadCategories(_this.config.categories)' in script_text,
             'catalogue_public_template_changed')
    return api[0] + '/company/' + company[0] + '/filter/categories/meta', {
        'categoryInternalNames': ['dividendos'], 'language': 'pt_BR', 'published': True}


def history_request(folder, page, bundle):
    _, _, responses = ledger(folder)
    _require(page in responses and bundle in responses, 'history_proofs_not_in_actual_ledger')
    bodies = []
    for receipt in (page, bundle):
        body = source_body(folder, receipt)
        _require(receipt['status'] == 200 and not receipt['stop']
                 and hashlib.sha256(body).hexdigest() == receipt['sha256'], 'history_source_changed')
        bodies.append(body.decode('utf-8'))
    page_text, script = bodies
    _require(page['url'] == SEEDS['celesc-quotes'] and bundle['url'] in {
        row['url'] for row in links(bodies[0].encode(), page['url'])}, 'widget_not_published_by_issuer')
    identifiers = set(re.findall(r'''stockinfoId:\s*'([a-f0-9-]{36})' ''', page_text + ' ', re.X))
    tickers = set(re.findall(r"tickers:\s*'([A-Z0-9,]+)'", page_text))
    _require(identifiers == {'c7953488-6081-11e9-87e6-0a3f429ecfb6'} and tickers == {'CLSC4'}
             and bundle['sha256'] == '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
             and all(token in script for token in (
                 'API_STOCKINFO_URL:"https://api.mziq.com/mzstockinfo"',
                 't.API_URL+"/"+t.STOCK_ID+"/history?from="', 't.ADJUSTED=!1',
                 '"&tickers="+t.TICKER', '"&adjusted="+t.ADJUSTED',
                 'yesWithProvents:"Yes, including dividends"')), 'unreviewed_public_history_template')
    return ('https://api.mziq.com/mzstockinfo/' + next(iter(identifiers))
            + '/history?from=2021-09-01&to=2026-09-30&tickers=CLSC4&adjusted=false')


def ledger(folder):
    base = Path(folder).resolve() / CACHE
    base.mkdir(parents=True, exist_ok=True)
    _require(not base.is_symlink() and base.resolve().is_relative_to(Path(folder).resolve()), 'unsafe_issuer_source_folder')
    requests = [json.loads(path.read_bytes()) for path in sorted(base.glob('request-*.json'))]
    responses = [json.loads(path.read_bytes()) for path in sorted(base.glob('response-*.json'))]
    return base, requests, responses


def acquire(folder, url, *, parent=None, catalogue=None, history=None):
    selected = allowed_tickers(folder)
    _require(selected is not None and bool(selected & {'CLSC3', 'CLSC4'}), 'Celesc_not_in_configured_positive_DY_scope')
    _require(approved(url), 'unapproved_public_issuer_source')
    base, requests, responses = ledger(folder)
    _require([row['ordinal'] for row in requests] == list(range(1, len(requests) + 1))
             and [row['ordinal'] for row in responses] == list(range(1, len(responses) + 1))
             and len(requests) == len(responses), 'interrupted_or_inconsistent_issuer_source_ledger')
    _require(len(requests) < MAX_REQUESTS, 'issuer_source_request_budget_exhausted')
    _require(not any(row['url'] == url for row in requests), 'issuer_source_already_attempted_no_retry')
    host = urlsplit(url).hostname
    _require(not any(urlsplit(row['url']).hostname == host and row.get('stop') for row in responses),
             'issuer_source_host_previously_stopped')
    is_robots = urlsplit(url).path == '/robots.txt'
    request_json = None
    if not is_robots:
        if history is not None:
            _require(catalogue is None and parent is None
                     and url == history_request(folder, history['page'], history['bundle']),
                     'unapproved_history_request')
        if catalogue is not None:
            expected_url, request_json = catalog_request(folder, catalogue['page'], catalogue['script'])
            _require(url == expected_url and parent is None, 'unapproved_catalogue_request')
        _require(url in SEEDS.values() or parent is not None or catalogue is not None or history is not None,
                 'observed_public_link_required')
        if parent is not None:
            _require(parent in responses, 'issuer_link_parent_not_in_actual_ledger')
            body = source_body(folder, parent)
            _require(hashlib.sha256(body).hexdigest() == parent['sha256']
                     and parent['status'] == 200 and not parent.get('stop')
                     and any(row['url'] == url for row in links(body, parent['url'])), 'issuer_link_parent_not_verified')
        robots = next((row for row in responses if row['url'] == f'https://{host}/robots.txt'), None)
        _require(robots is not None, 'issuer_host_robots_not_checked')
        if robots['status'] == 200:
            body = source_body(folder, robots)
            _require(hashlib.sha256(body).hexdigest() == robots['sha256'], 'robots_original_changed')
            parser = Protego.parse(body.decode('utf-8', errors='replace'))
            _require(parser.can_fetch(url, USER_AGENT), 'issuer_source_disallowed_by_robots')
            delay = parser.crawl_delay(USER_AGENT) or 0
            rate = parser.request_rate(USER_AGENT)
            if rate:
                delay = max(delay, rate.seconds / rate.requests)
            same_host = [row for row in requests if urlsplit(row['url']).hostname == host]
            if same_host:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(same_host[-1]['started_at'])).total_seconds()
                wait = max(0, delay - elapsed)
                _require(wait <= 60, 'issuer_source_rate_requires_later_run')
                time.sleep(wait)
        else:
            _require(robots['status'] == 404, 'issuer_host_robots_unavailable_stop')
    ordinal = len(requests) + 1
    request_record = {'ordinal': ordinal, 'url': url, 'parent': parent,
                      'started_at': datetime.now(timezone.utc).isoformat(), 'retries': 0,
                      'maximum_bytes': MAX_BYTES, 'scope': 'CLSC3_CLSC4_positive_DY_source_discovery'}
    if catalogue is not None:
        request_record.update(catalogue=catalogue, request_json=request_json,
                              method='POST_public_read_only_document_filter')
    if history is not None:
        request_record.update(history=history, method='GET_public_widget_history',
                              adjusted=False, tickers=['CLSC4'])
    _save(base, f'request-{ordinal:03d}.json', _dump(request_record))

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    response, body, failure, status = None, b'', None, None
    try:
        try:
            response = urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url,
                data=_dump(request_json) if request_json is not None else None,
                headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity',
                         **({'Content-Type': 'application/json'} if request_json is not None else {})}), timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        status = response.status
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            failure = 'response_size_limit'
        elif response.headers.get('Content-Length') is not None and len(body) != int(response.headers['Content-Length']):
            failure = 'incomplete_source_body'
        elif status in (401, 403, 429) or 300 <= status < 400:
            failure = 'access_or_redirect_stop_no_follow'
        elif status != 200 and not (is_robots and status == 404):
            failure = 'non_success_no_retry'
        elif re.search(rb'(?is)<title[^>]*>[^<]*(?:captcha|access denied|sign in|just a moment)', body):
            failure = 'public_content_challenge_stop'
    except Exception as error:
        failure = type(error).__name__
    finally:
        if response is not None:
            response.close()
    digest = hashlib.sha256(body).hexdigest()
    suffix = '.html' if b'<html' in body[:3000].lower() else '.bin'
    name = f'original-{digest}{suffix}'
    _save(base, name, body)
    result = {**request_record, 'status': status, 'stop': failure,
              'completed_at': datetime.now(timezone.utc).isoformat(),
              'sha256': digest, 'bytes': len(body), 'file': CACHE + '/' + name,
              'headers': dict(response.headers.items()) if response is not None else {},
              'location': response.headers.get('Location') if response is not None else None,
              'settlement_confirmed': False, 'model_admission': False}
    _save(base, f'response-{ordinal:03d}.json', _dump(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--seed', choices=SEEDS)
    parser.add_argument('--robots', choices=sorted(HOSTS))
    parser.add_argument('--links', type=Path)
    args = parser.parse_args()
    if args.links:
        receipt = json.loads(args.links.read_bytes())
        body = source_body(args.folder, receipt)
        _require(hashlib.sha256(body).hexdigest() == receipt['sha256'], 'source_hash_mismatch')
        print(json.dumps(links(body, receipt['url']), ensure_ascii=True))
    else:
        _require(bool(args.seed) != bool(args.robots), 'choose_one_source')
        url = SEEDS[args.seed] if args.seed else f'https://{args.robots}/robots.txt'
        result = acquire(args.folder, url)
        print(json.dumps({'ordinal': result['ordinal'], 'url': result['url'], 'status': result['status'],
                          'stop': result['stop'], 'file': result['file'], 'bytes': result['bytes']}))


if __name__ == '__main__':
    main()
