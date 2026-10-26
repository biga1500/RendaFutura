"""Bounded public-navigation follow-on for three unavailable exact CVM bodies.

No production writes, guessed download paths, source retries, proxies, browser
execution, authentication or earlier-version substitution. The independent
twelve-request baseline includes robots. An explicit, append-only v3 audit can
extend the same cumulative ledger to twenty attempts for PNVL only.
"""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
import urllib.error
import urllib.request
import urllib.robotparser
import zipfile

from lxml import html

from . import primary_cash_exact_research as exact


CACHE = 'raw/primary-research/primary-cash-followon'
MAX_REQUESTS = 12
MAX_BYTES = 12_000_000
TARGETS = ('741697', '741696', '509579')
DENIED = ('b3.com.br', 'bvmf.com.br', 'bovespa.com.br', 'azzas2154.com.br',
          'allos.com.br', 'santander.com.br', 'engie.com.br', 'cdn-sites-assets.mziq.com')
ISSUER_ROOTS = ('eztec.com.br', 'grupopanvel.com.br')
V3_MAX_REQUESTS = 20
V2_REPORT = CACHE + '/report-v2-580c72babaf8e0f505a633df44d5b76483a982ac170bbc69a9c699270b9f1a75.json'
PNVL_COMPANY = 'd19425c2-0ba6-490e-af3b-87fc04d4fc88'
PNVL_REVISED = {
    '741697': ('aviso_aos_acionistas', '2d719465-d282-4cf8-9e8a-ca1883829c76',
               '7e540107a41a83038c6ad02bad536ca03590626586f7da64378636c9f89c98fd'),
    '741696': ('reuniao_administracao', '990ddaba-3025-4ad6-8035-1586d903f48c',
               'ba54a72384c647067602e968198f492d198d3201ba75c43cd4cdad66339f2b61'),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def published_https(url):
    parts = urlsplit(url)
    if parts.scheme == 'http' and parts.port is None and not parts.username and not parts.password:
        return urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def document_base(tree, response_url):
    bases = tree.xpath('//base[@href]/@href')
    return urljoin(response_url, bases[0]) if bases else response_url


def save(folder, prefix, payload, *, extension=None):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', prefix):
        raise ValueError('unsafe_owned_filename')
    body = payload if isinstance(payload, bytes) else exact.shared._dump(payload)
    suffix = '.bin' if isinstance(payload, bytes) else '.json'
    if extension is not None:
        signatures = {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n'}
        if extension not in signatures or not isinstance(payload, bytes) or not payload.startswith(signatures[extension]):
            raise ValueError('invalid_owned_original_or_render_extension')
        suffix = extension
    name = CACHE + '/' + prefix + '-' + exact.shared._sha(body) + suffix
    path = exact.shared._safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_followon_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def approved(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
            or parts.port not in (None, 443) or not host
            or any(host == denied or host.endswith('.' + denied) for denied in DENIED)):
        return False
    if re.search(r'\.(?:docx?|xls[xm]?)(?:$|[?])', parts.path, re.I):
        return False
    return (any(host == root or host.endswith('.' + root) for root in ISSUER_ROOTS)
            or host in ('api.mziq.com', 'apicatalog.mziq.com', 'filemanager-cdn.mziq.com',
                        'ri-eztec2022.mz-sites.com'))


def budget_v3(folder, *, create=False):
    """The existing ledger is extended once, never replaced or reset."""
    folder = Path(folder).resolve()
    paths = list((folder / CACHE).glob('phase-v3-budget-*.json'))
    if not paths and not create:
        return None
    if not paths:
        report = json.loads((folder / V2_REPORT).read_bytes())
        if report['request_count'] != 12:
            raise ValueError('exact_twelve_request_v2_baseline_required')
        requests = [json.loads(p.read_bytes()) for p in (folder / CACHE).glob('request-*.json')]
        if sorted(r['ordinal'] for r in requests) != list(range(1, 13)):
            raise ValueError('v3_budget_must_be_appended_before_thirteenth_attempt')
        files = [p for prefix in ('request', 'response')
                 for p in (folder / CACHE).glob(prefix + '-*.json')]
        files += [folder / r['file'] for r in report['responses']]
        files.append(folder / V2_REPORT)
        baseline = {p.relative_to(folder).as_posix(): exact.shared._sha(p.read_bytes())
                    for p in sorted(set(files))}
        payload = {'schema_version': 3, 'phase': 'PNVL_exact_revised_bodies_only',
            'authorization': 'User explicitly extended the cumulative follow-on cap from 12 to 20, at most eight additional requests; preserve all prior stops, requests and receipts.',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'previous_limit': 12, 'cumulative_limit': 20, 'maximum_additional_requests': 8,
            'prior_request_count': 12, 'prior_ordinals': list(range(1, 13)),
            'targets': ['741697', '741696'], 'EZTC_stopped': True,
            'denial_stops_unchanged': [401, 403, 429, 'robots_disallow', 'authentication', 'captcha', 'denied_CDN'],
            'prior_files_sha256': baseline, 'counter_reset': False,
            'maximum_unselected_navigation_candidates': 3}
        save(folder, 'phase-v3-budget', payload)
        paths = list((folder / CACHE).glob('phase-v3-budget-*.json'))
    if len(paths) != 1:
        raise ValueError('one_explicit_v3_budget_audit_required')
    body = paths[0].read_bytes()
    if not paths[0].stem.endswith(exact.shared._sha(body)):
        raise ValueError('v3_budget_audit_hash_mismatch')
    audit = json.loads(body)
    if (audit['previous_limit'], audit['cumulative_limit'], audit['maximum_additional_requests'],
            audit['targets'], audit['EZTC_stopped'], audit['counter_reset'],
            audit['prior_request_count'], audit['prior_ordinals'],
            audit['maximum_unselected_navigation_candidates'], audit['denial_stops_unchanged']) != (
            12, 20, 8, ['741697', '741696'], True, False, 12, list(range(1, 13)), 3,
            [401, 403, 429, 'robots_disallow', 'authentication', 'captcha', 'denied_CDN']):
        raise ValueError('invalid_v3_budget_or_scope')
    for name, digest in audit['prior_files_sha256'].items():
        path = exact.shared._safe(folder, name)
        if exact.shared._sha(path.read_bytes()) != digest:
            raise ValueError('prior_v2_bytes_changed_no_request')
    return {**audit, 'file': paths[0].relative_to(folder).as_posix(), 'sha256': exact.shared._sha(body)}


def validate_attempts(requests, limit):
    if (len(requests) > limit or len({r['url'] for r in requests}) != len(requests)
            or sorted(r['ordinal'] for r in requests) != list(range(1, len(requests) + 1))):
        raise ValueError('followon_budget_or_retry_integrity_failure')


def historical_v2_report(folder):
    """The twelve-attempt result is an immutable historical snapshot, not live state."""
    body = exact.shared.Reader(folder).read(V2_REPORT, 'immutable_historical_v2_at_12_attempts',
        '580c72babaf8e0f505a633df44d5b76483a982ac170bbc69a9c699270b9f1a75')[0]
    report = json.loads(body)
    validate_attempts(report['requests'], MAX_REQUESTS)
    if report['request_count'] != 12 or report['maximum_requests'] != 12:
        raise ValueError('exact_twelve_request_v2_baseline_required')
    budget_v3(folder)
    return report, V2_REPORT


def pnvl_provenance(provenance):
    """Require the new origin or asset to descend from the PNVL public chain."""
    if provenance.get('kind') == 'robots_for_published_navigation':
        return pnvl_provenance(provenance.get('target_provenance', {}))
    source = provenance.get('source', {})
    host = urlsplit(source.get('url', '')).hostname or ''
    return host == 'grupopanvel.com.br' or host.endswith('.grupopanvel.com.br') or source.get('phase') == 'PNVL_exact_revised_bodies_only'


def json_links(value, pointer=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from json_links(child, pointer + '/' + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from json_links(child, pointer + '/' + str(index))
    elif isinstance(value, str) and value.startswith('https://'):
        yield pointer, value


def pnvl_catalogue_spec(fetcher):
    """Literal issuer configuration plus an already-cached public client schema.

    The cached client supplies only the read-only transport template. No new
    request is sent to its prohibited CDN origin, and no bank facts are used.
    """
    urls = ('https://ri.grupopanvel.com.br/informacoes-financeiras/avisos-comunicados-e-fatos-relevantes/',
            'https://ri.grupopanvel.com.br/governanca-corporativa/reunioes-e-assembleias/')
    sources, configurations = [], []
    for url in urls:
        response = next(r for r in fetcher.ledger('response') if r['url'] == url and r['http_status'] == 200)
        tree = html.fromstring(fetcher.body(response))
        scripts = '\n'.join(s.text for s in tree.xpath('//script[not(@src)]') if s.text)
        base = re.search(r'const BASE_URL\s*=\s*[\x22\x27]([^\x22\x27]+)', scripts)
        company = re.search(r'const COMPANIES\s*=\s*\[\s*\{\s*id:\s*[\x22\x27]([^\x22\x27]+)', scripts)
        config = re.search(r'var configPage\s*=\s*\{(.*?)\};', scripts, re.S)
        language = re.search(r'language:\s*[\x22\x27]([^\x22\x27]+)', config.group(1)) if config else None
        categories = re.findall(r'internal_name:\s*[\x22\x27]([^\x22\x27]+)', scripts)
        if not all((base, company, language)):
            raise ValueError('literal_official_catalogue_configuration_required')
        configurations.append({'base': base.group(1), 'company': company.group(1),
                               'language': language.group(1), 'categories': categories})
        sources.append(response)
    if any((c['base'], c['company'], c['language']) != (configurations[0]['base'],
            configurations[0]['company'], configurations[0]['language']) for c in configurations):
        raise ValueError('issuer_catalogue_configurations_disagree')
    reader = exact.shared.Reader(fetcher.folder)
    script_name = 'raw/primary-research/banks/bradesco-filemanager-script-d8d86b0e03f682b8797f.js'
    script, source = reader.read(script_name, 'existing_cached_public_client_transport_template_only',
        'd8d86b0e03f682b8797f951642a27c09258ba9e323c84057831f2fd9d443c6cb')
    schema = script.decode('utf-8')
    required = ['c=n.BASE_API_URL=BASE_URL',
        'getFilesByCategoriesUrl=function(e){return c+"/company/"+e+"/filter/categories/year/meta"}',
        'n={year:a,categories:w(l(t.config.categories)),language:t.config.language,published:!0}',
        '$.ajax({type:"POST",url:e,data:JSON.stringify(n)']
    if any(span not in schema for span in required):
        raise ValueError('exact_cached_public_read_only_request_template_required')
    if ('aviso_aos_acionistas' not in configurations[0]['categories']
            or 'reuniao_administracao' not in configurations[1]['categories']):
        raise ValueError('literal_notice_and_board_category_names_required')
    c = configurations[0]
    url = c['base'] + '/company/' + c['company'] + '/filter/categories/year/meta'
    if urlsplit(url).hostname != 'apicatalog.mziq.com':
        raise ValueError('literal_public_catalogue_origin_mismatch')
    return {'url': url, 'method': 'POST', 'request_json': {'year': '2024',
        'categories': ['aviso_aos_acionistas', 'reuniao_administracao'],
        'language': c['language'], 'published': True}, 'official_configurations': configurations,
        'official_page_receipts': sources, 'cached_transport_template_source': source,
        'cached_template_field_spans': required, 'no_new_prohibited_CDN_request': True,
        'selection_basis': 'exact_native_IPE_2024_notice_and_board_categories_not_amount_matching'}


def bootstrap(folder):
    """Inspect the cached HTML originals before permitting any new acquisition."""
    reader = exact.shared.Reader(folder)
    documents = {d['sequence']: d for d in exact.indexed_documents(reader)[0]}
    shells = []
    for sequence in TARGETS:
        document = documents[sequence]
        body, source = exact.document_body(reader, document)
        if body is None or body.lstrip().startswith(b'%PDF-'):
            raise ValueError('cached_shell_state_changed_review_required')
        tree = html.fromstring(body)
        links = tree.xpath('//a/@href|//iframe/@src|//frame/@src|//object/@data|//embed/@src')
        inline = [s.text for s in tree.xpath('//script[not(@src)]') if s.text and s.text.strip()]
        forms = tree.xpath('//form/@action')
        shells.append({'sequence': sequence, 'document': document, 'source': source,
            'published_links_or_frames': links, 'inline_scripts': inline,
            'script_sources': tree.xpath('//script/@src'),
            'forms': forms, 'form_only_repeats_empty_download_URL': bool(forms) and all(
                urljoin(document['url'], form) == document['url'] for form in forms),
            'classification': 'unavailable_document_body_not_authentication_or_robots_denial',
            'download_URL_not_retried': True,
            'navigation_candidate_found': bool(links or inline)})
    body, source = reader.read('raw/inventory/fca_cia_aberta_2026.zip', 'primary_CVM_FCA_public_websites',
        url='https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip')
    seeds = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        member = 'fca_cia_aberta_geral_2026.csv'
        with archive.open(member) as stream:
            rows = csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
            for line, row in enumerate(rows, 2):
                if row['Codigo_CVM'].isdigit() and int(row['Codigo_CVM']) in (9342, 20770):
                    website = row['Pagina_Web'].strip()
                    url = website if website.startswith('https://') else 'https://' + website.removeprefix('http://')
                    if not approved(url):
                        raise ValueError('FCA_website_not_allowed')
                    seeds.append({'url': url, 'cvm': int(row['Codigo_CVM']), 'issuer': row['Nome_Empresarial'],
                        'source': source, 'member': member, 'csv_line': line, 'raw_row': row,
                        'scheme_basis': 'HTTPS_transport_for_exact_FCA_published_host_and_path'})
    if {s['cvm'] for s in seeds} != {9342, 20770}:
        raise ValueError('two_exact_FCA_issuer_websites_required')
    report = {'schema_version': 1, 'cached_shell_inspection': shells, 'issuer_seeds': seeds,
        'sources': list(reader.sources.values()), 'network_requests': 0,
        'prior_acquisition_logs_preserved': True, 'earlier_revisions_not_substituted': True}
    path = save(folder, 'cached-HTML-audit', report)
    return report, path


class Fetcher:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.bootstrap, self.bootstrap_file = bootstrap(folder)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def ledger(self, prefix):
        return [json.loads(p.read_bytes()) for p in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def body(self, receipt):
        return exact.shared.Reader(self.folder).read(receipt['file'], 'public_original_response',
                                                    receipt['sha256'], receipt['url'])[0]

    def provenance(self, url):
        if not approved(url):
            raise ValueError('forbidden_or_non_public_URL_no_request')
        if url in {s['url'] for s in self.bootstrap['issuer_seeds']}:
            return {'kind': 'exact_native_FCA_published_website', 'file': self.bootstrap_file}
        responses = sorted(self.ledger('response'), key=lambda r: r['ordinal'])
        # Preserve the original canonical issuer assignment even after a later
        # RI page adds self-links. Filename hash order must not choose the proof.
        for response in responses:
            if (response.get('http_status') != 200 or response.get('access_stop')
                    or not (urlsplit(response['url']).path.endswith('.js')
                            or 'javascript' in (response.get('content_type') or ''))):
                continue
            script = self.body(response).decode('utf-8', errors='replace')
            for match in re.finditer(r'\binvestorRelationshipLink\s*=\s*([\x22\x27])(https://[^\x22\x27]+)\1', script):
                if published_https(match.group(2)) == url:
                    return {'kind': 'published_public_script_navigation', 'source': response,
                            'assignment': match.group(0)}
        for response in responses:
            if response.get('access_stop') or response.get('http_status') in (401, 403, 429):
                continue
            if response.get('location') and published_https(urljoin(response['url'], response['location'])) == url:
                return {'kind': 'published_HTTP_redirect_HTTPS_transport', 'source': response,
                        'published_location': response['location']}
            body = self.body(response)
            if body.lstrip().startswith(b'%PDF-'):
                continue
            if body.lstrip().startswith((b'{', b'[')):
                try:
                    payload = json.loads(body)
                    for pointer, target in json_links(payload):
                        if published_https(target) == url:
                            return {'kind': 'published_catalogue_JSON_link', 'source': response,
                                    'JSON_pointer': pointer, 'published_URL': target}
                except (ValueError, TypeError):
                    pass
            try:
                tree = html.fromstring(body)
            except (ValueError, html.etree.ParserError):
                continue
            base_url = document_base(tree, response['url'])
            for value in tree.xpath('//a/@href|//iframe/@src|//frame/@src|//embed/@src|//object/@data|//script/@src'):
                target = published_https(urljoin(base_url, value))
                if target == url:
                    return {'kind': 'published_public_HTML_link', 'source': response,
                            'raw_link': value, 'document_base': base_url}
        raise ValueError('no_published_navigation_provenance_no_request')

    def resolve_robots_redirect(self, url):
        sources = [r for r in self.ledger('response') if (r['url'].endswith('/robots.txt')
            or r.get('provenance', {}).get('kind') == 'published_robots_redirect_resolution')
            and r.get('http_status') in (301, 302, 303, 307, 308) and r.get('location')
            and published_https(urljoin(r['url'], r['location'])) == url]
        if not sources:
            raise ValueError('exact_published_robots_redirect_required')
        return self._get(url, {'kind': 'published_robots_redirect_resolution', 'source': sources[0]})

    def _get(self, url, provenance, *, request_json=None):
        if not approved(url):
            raise ValueError('forbidden_URL_no_request')
        requests, responses = self.ledger('request'), self.ledger('response')
        if self.ledger('stop') or any(r.get('access_stop') or r.get('http_status') in (401, 403, 429) for r in responses):
            raise ValueError('persistent_access_stop_no_request')
        phase = budget_v3(self.folder)
        limit = V3_MAX_REQUESTS if phase else MAX_REQUESTS
        validate_attempts(requests, limit)
        if len(requests) >= limit:
            raise ValueError('followon_request_budget_exhausted')
        if phase and len(requests) >= 12:
            host = urlsplit(url).hostname or ''
            pnvl_host = host == 'grupopanvel.com.br' or host.endswith('.grupopanvel.com.br')
            asset_host = host in ('api.mziq.com', 'apicatalog.mziq.com', 'filemanager-cdn.mziq.com')
            if not (pnvl_host or asset_host) or not pnvl_provenance(provenance):
                raise ValueError('v3_PNVL_only_no_EZTC_or_unselected_origin_request')
        if url in {r['url'] for r in requests}:
            raise ValueError('URL_already_attempted_no_retry')
        if request_json is not None:
            spec = pnvl_catalogue_spec(self)
            if url != spec['url'] or request_json != spec['request_json']:
                raise ValueError('only_exact_literal_read_only_PNVL_catalogue_filter_allowed')
        ordinal = len(requests) + 1
        request_record = {'url': url, 'ordinal': ordinal, 'provenance': provenance,
            'started_at': datetime.now(timezone.utc).isoformat(), 'proxy': False,
            'automatic_redirects': False, 'retries': 0}
        if phase:
            request_record.update(phase='PNVL_exact_revised_bodies_only', cumulative_limit=limit,
                                  budget_audit_file=phase['file'], budget_audit_sha256=phase['sha256'])
        if request_json is not None:
            request_record.update(method='POST', request_json=request_json,
                                  operation='public_read_only_document_catalogue')
        save(self.folder, 'request', request_record)
        body = b''
        result = {**request_record, 'status': 'unavailable', 'http_status': None}
        try:
            headers = {'User-Agent': exact.shared.USER_AGENT,
                       'Accept': 'text/html,application/pdf,application/json,text/plain', 'Accept-Encoding': 'identity'}
            data = None
            if request_json is not None:
                data = json.dumps(request_json, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            request = urllib.request.Request(url, data=data, headers=headers)
            with self.opener.open(request, timeout=25) as response:
                result.update(http_status=response.status, content_type=response.headers.get('Content-Type'),
                              location=response.headers.get('Location'))
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('response_size_limit')
            result['status'] = 'received'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, error=type(error).__name__,
                          location=error.headers.get('Location'))
            body = error.read(MAX_BYTES + 1)[:MAX_BYTES]
        except (OSError, ValueError) as error:
            result['error'] = str(error)
            body = body[:MAX_BYTES]
        challenge = any(token in body.lower() for token in (
            b'cf-chl-', b'verify you are human', b'checking your browser',
            b'access denied', b'authentication required', b'login required'))
        result['access_stop'] = result['http_status'] in (401, 403, 429) or challenge
        result.update(sha256=exact.shared._sha(body), bytes=len(body),
                      retrieved_at=datetime.now(timezone.utc).isoformat(),
                      PDF_signature=body.lstrip().startswith(b'%PDF-'))
        result['file'] = save(self.folder, 'body', body)
        if phase:
            result['phase'] = 'PNVL_exact_revised_bodies_only'
        result['receipt_file'] = save(self.folder, 'response', result)
        return result

    def acquire(self, url, *, catalogue=False):
        spec = pnvl_catalogue_spec(self) if catalogue else None
        if spec:
            if url != spec['url']:
                raise ValueError('catalogue_URL_must_equal_literal_public_template')
            spec_file = save(self.folder, 'catalogue-v3-spec', spec)
            provenance = {'kind': 'literal_official_PNVL_catalogue_configuration',
                          'source': spec['official_page_receipts'][0], 'spec_file': spec_file}
        else:
            provenance = self.provenance(url)
        parts = urlsplit(url)
        robots_url = parts.scheme + '://' + parts.netloc + '/robots.txt'
        robots = next((r for r in self.ledger('response') if r['url'] == robots_url), None)
        if robots is None:
            robots = self._get(robots_url, {'kind': 'robots_for_published_navigation',
                                         'target': url, 'target_provenance': provenance})
        # Only consume already-recorded, explicitly inspected redirect hops.
        # No request, guessed path or policy-denial fallback occurs here.
        seen = set()
        while robots.get('http_status') in (301, 302, 303, 307, 308) and robots.get('location'):
            if robots['url'] in seen:
                raise ValueError('robots_redirect_loop_no_target_request')
            seen.add(robots['url'])
            target = published_https(urljoin(robots['url'], robots['location']))
            resolved = next((r for r in self.ledger('response') if r['url'] == target
                and r.get('provenance', {}).get('kind') == 'published_robots_redirect_resolution'), None)
            if resolved is None:
                break
            robots = resolved
        if robots.get('access_stop'):
            raise ValueError('robots_access_stop_no_target_request')
        if robots['http_status'] == 200:
            parser = urllib.robotparser.RobotFileParser(robots_url)
            parser.parse(self.body(robots).decode('utf-8').splitlines())
            if not parser.can_fetch(exact.shared.USER_AGENT, url):
                save(self.folder, 'stop', {'reason': 'robots_disallow', 'url': url, 'robots': robots})
                raise ValueError('robots_disallow_no_target_request')
            if parser.crawl_delay(exact.shared.USER_AGENT) or parser.request_rate(exact.shared.USER_AGENT):
                raise ValueError('robots_requires_manual_spacing_no_target_request')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_redirect_requires_review_no_target_request')
        return self._get(url, provenance, request_json=spec['request_json'] if spec else None)


def inspect_response(folder, response):
    body = exact.shared.Reader(folder).read(response['file'], 'public_response_inspection', response['sha256'])[0]
    if body.lstrip().startswith(b'%PDF-'):
        return {'url': response['url'], 'pages': exact.pdf_pages(body)}
    try:
        tree = html.fromstring(body)
    except (ValueError, html.etree.ParserError):
        return {'url': response['url'], 'status': 'not_readable_HTML_or_PDF'}
    links = [{'label': ' '.join(node.text_content().split()),
              'url': urljoin(document_base(tree, response['url']), node.get('href'))}
             for node in tree.xpath('//a[@href]')]
    scripts = [s.text for s in tree.xpath('//script[not(@src)]') if s.text]
    navigation_urls = sorted(set(url for script in scripts for url in
        re.findall(r'https?://[^\s\x22\x27<>`]+', script)))
    return {'url': response['url'], 'title': tree.xpath('//title/text()'),
        'links': links, 'script_sources': tree.xpath('//script/@src'),
        'inline_navigation_URLs': navigation_urls,
        'forms': [{'action': f.get('action'), 'method': f.get('method'),
                   'selects': [{'name': s.get('name'), 'options': s.xpath('.//option/@value')}
                               for s in f.xpath('.//select')]} for f in tree.xpath('//form')]}


def _build_v2_report(folder):
    fetcher = Fetcher(folder)
    requests = sorted(fetcher.ledger('request'), key=lambda r: r['ordinal'])
    responses = sorted(fetcher.ledger('response'), key=lambda r: r['ordinal'])
    validate_attempts(requests, MAX_REQUESTS)
    reader = exact.shared.Reader(folder)
    for prefix in ('request', 'response'):
        for path in sorted((Path(folder) / CACHE).glob(prefix + '-*.json')):
            reader.read(path.relative_to(Path(folder)).as_posix(), 'immutable_acquisition_' + prefix)
    for response in responses:
        reader.read(response['file'], 'actual_public_HTTP_original', response['sha256'], response['url'])
        if response['url'] != requests[response['ordinal'] - 1]['url']:
            raise ValueError('request_response_identity_mismatch')
    observed = {r['url']: r for r in responses}
    panvel_url = 'https://ri.grupopanvel.com.br/'
    panvel_navigation = fetcher.provenance(panvel_url)
    robots = observed['https://ri.grupopanvel.com.br/robots.txt']
    parser = urllib.robotparser.RobotFileParser(robots['url'])
    parser.parse(fetcher.body(robots).decode('utf-8').splitlines())
    archive = observed['https://ri.eztec.com.br/arquivamentos-cvm/']
    body = fetcher.body(archive)
    tree = html.fromstring(body)
    loaders = [u for u in tree.xpath('//script/@src') if 'file-manager/' in u]
    denied_loaders = [u for u in loaders if urlsplit(u).hostname == 'cdn-sites-assets.mziq.com']
    literals = [s.text for s in tree.xpath('//script[not(@src)]') if s.text and 'const fmBase' in s.text]
    if not denied_loaders or not literals:
        raise ValueError('published_loader_state_changed_review_required')
    report = {'schema_version': 2, 'status': 'bounded_followon_complete_exact_later_bodies_not_obtained',
        'cached_HTML_audit': {'file': fetcher.bootstrap_file,
            'sha256': exact.shared._sha((Path(folder) / fetcher.bootstrap_file).read_bytes())},
        'cached_shells_are_unavailable_not_policy_denials': True,
        'empty_CVM_URLs_retried': False, 'requests': requests, 'responses': responses,
        'request_count': len(requests), 'maximum_requests': MAX_REQUESTS,
        'access_denial_encountered': any(r.get('access_stop') for r in responses),
        'results': [
            {'ticker': 'PNVL3', 'exact_missing_sequences': ['741697', '741696'],
             'status': 'blocked_by_batch_request_cap_not_by_denial',
             'canonical_public_RI': panvel_url, 'published_navigation_proof': panvel_navigation,
             'robots': robots, 'robots_can_fetch_RI_home': parser.can_fetch(exact.shared.USER_AGENT, panvel_url),
             'RI_home_requested': panvel_url in observed, 'exact_bodies_obtained': False,
             'remaining_work': 'Fetch the already-published canonical RI home and inspect its public library for the exact 28-Mar-2024 notice and board v2. This batch cannot fetch beyond twelve attempts.'},
            {'ticker': 'EZTC3', 'exact_missing_sequences': ['509579'],
             'status': 'exact_body_not_exposed_in_examined_allowed_public_HTML',
             'public_library_page': archive,
             'prohibited_published_loader_URLs_not_requested': denied_loaders,
             'published_catalog_configuration': literals,
             'unrequested_public_RI_mirror': 'https://ri-eztec2022.mz-sites.com/',
             'exact_bodies_obtained': False,
             'remaining_work': 'A static exact 25-May-2022 notice copy, a permitted published catalog request template, or the already-published issuer RI mirror may supply the final body. No API route was guessed and no prohibited script was requested.'}],
        'navigation_correction': {
            'ordinal': 10, 'issue': 'Initial script resolution omitted the HTML base element /pt/.',
            'initial_redirect_response_preserved_and_counted': True,
            'correct_published_base_resolution_ordinal': 11,
            'corrected_URI_was_explicit_in_base_plus_script_src_not_a_guessed_download': True},
        'source_receipts': list(reader.sources.values()),
        'earlier_versions_used_as_substitutes': False, 'amounts_or_dates_inferred': False,
        'production_files_changed': False, 'pointers_written': False, 'thresholds_changed': False,
        'settlement_verified': False, 'proxy': False, 'automatic_redirects': False, 'retries': 0}
    return report, save(folder, 'report-v2', report)


def select_pnvl_revised_records(payload, documents):
    """Select the later pair by issuer, category and native publication, not amount."""
    if payload.get('success') is not True:
        raise ValueError('successful_public_catalogue_required')
    records = payload['data']['document_metas']
    selected = {}
    for sequence, (category, identifier, _) in PNVL_REVISED.items():
        document = documents[sequence]
        if document['cvm'] != 9342 or document['version'] != 2:
            raise ValueError('exact_native_v2_identity_required')
        matches = [r for r in records if r.get('company_id') == PNVL_COMPANY
            and r.get('internal_name') == category and r.get('is_published') is True
            and r.get('language_code') == 'pt_BR'
            and r.get('file_date', '')[:10] == document['reference_date']
            and r.get('file_published_date', '')[:10] == document['published_at']]
        if len(matches) != 1 or matches[0]['id'] != identifier:
            raise ValueError('unique_exact_revised_issuer_catalogue_record_required')
        selected[sequence] = matches[0]
    return selected


def reviewed_pnvl_bodies(fetcher, selected, reader):
    reviewed = {}
    spans = {
        '741697': [
            (1, 'Valor por A\u00e7\u00e3o (bruto)'),
            (1, '1\u00aa Parcela 30/08/2024 R$ 0,05911502'),
            (1, '2\u00ba Parcela 31/03/2025 R$ 0,01880932'),
            (1, 'Total R$ 0,07792434'),
            (1, 'posi\u00e7\u00e3o acion\u00e1ria de 05/04/2024'),
            (1, 'valor l\u00edquido, j\u00e1 deduzido o Imposto de Renda na Fonte, R$ 0,06623569'),
            (2, 'Installments Payment start date PNVL3 (ON)'),
            (2, '1st installment August 30, 2024 BRL 0,05911502'),
            (2, '2nd installment March 31, 2025 BRL 0,01880932')],
        '741696': [
            (1, 'em 28 de mar\u00e7o de 2024, \u00e0s 17:00 horas'),
            (1, '30 de agosto de 2024, 28 de mar\u00e7o de mar\u00e7o de 2025'),
            (1, 'detentores de a\u00e7\u00f5es em 05 de abril de 2024'),
            (1, '1\u00aa Parcela do JCP 30/08/2024 R$ 0,05911502'),
            (1, '2\u00aa Parcela do JCP 31/03/2025 R$ 0,01880932'),
            (1, 'Total R$ 0,07792434'),
            (1, 'O cr\u00e9dito cont\u00e1bil ser\u00e1 efetuado em 31 de mar\u00e7o de 2024.')],
    }
    for sequence, record in selected.items():
        responses = [r for r in fetcher.ledger('response') if r['url'] == record['file_url']]
        if len(responses) != 1 or responses[0]['http_status'] != 200 or responses[0]['access_stop']:
            raise ValueError('one_successful_actual_revised_PDF_receipt_required')
        response = responses[0]
        body, source = reader.read(response['file'], 'actual_later_issuer_notice_or_board_PDF',
                                  PNVL_REVISED[sequence][2], response['url'])
        if len(body) != int(record['file_size']) or source['sha256'] != response['sha256']:
            raise ValueError('catalogue_receipt_original_bytes_disagree')
        pages = exact.pdf_pages(body)
        if len(pages) != (2 if sequence == '741697' else 1):
            raise ValueError('reviewed_PNVL_page_count_mismatch')
        for page, span in spans[sequence]:
            if exact.norm(span) not in exact.norm(pages[page - 1]):
                raise ValueError('reviewed_PNVL_original_span_missing')
        for span in ('DIMED S/A DISTRIBUIDORA DE MEDICAMENTOS', '92.665.611/0001-77',
                     '(PNVL3)', 'a\u00e7\u00f5es ordin\u00e1rias', '0,07792434', '0,06623569'):
            if exact.norm(span) not in exact.norm(pages[0]):
                raise ValueError('reviewed_PNVL_identity_or_tax_basis_missing')
        pdf_file = save(fetcher.folder, 'pnvl-revised-' + sequence, body, extension='.pdf')
        reader.read(pdf_file, 'byte_identical_readable_copy_of_acquired_original', source['sha256'], response['url'])
        reviewed[sequence] = {'catalogue_record': record, 'response': response, 'source': source,
            'readable_original_file': pdf_file, 'pages': len(pages),
            'reviewed_spans': [{'page': page, 'text': span} for page, span in spans[sequence]],
            'CVM_original_byte_identity_verified': False,
            'revision_identity_basis': 'issuer_reference_and_April_2_publication_category_identity_correspond_to_native_IPE_v2_not_CVM_byte_identity'}
    return reviewed


def build_v3_report(folder):
    folder = Path(folder).resolve()
    fetcher, reader = Fetcher(folder), exact.shared.Reader(folder)
    phase = budget_v3(folder)
    if phase is None:
        raise ValueError('explicit_v3_budget_audit_required')
    requests = sorted(fetcher.ledger('request'), key=lambda r: r['ordinal'])
    responses = sorted(fetcher.ledger('response'), key=lambda r: r['ordinal'])
    validate_attempts(requests, V3_MAX_REQUESTS)
    if [r['ordinal'] for r in responses] != [r['ordinal'] for r in requests]:
        raise ValueError('one_response_for_every_cumulative_attempt_required')
    for prefix in ('request', 'response'):
        for path in sorted((folder / CACHE).glob(prefix + '-*.json')):
            reader.read(path.relative_to(folder).as_posix(), 'immutable_acquisition_' + prefix)
    http_sources = []
    for response, request in zip(responses, requests):
        if any(response.get(key) != value for key, value in request.items()):
            raise ValueError('request_response_identity_mismatch')
        reader.read(response['file'], 'actual_public_HTTP_original', response['sha256'], response['url'])
        # Identical content can have several actual retrieval URLs. A fresh
        # reader preserves each receipt without inventing a URL for local data.
        _, source = exact.shared.Reader(folder).read(response['file'], 'actual_public_HTTP_original',
                                                    response['sha256'], response['url'])
        http_sources.append({**source, 'acquisition_ordinal': response['ordinal'],
                             'retrieved_at': response['retrieved_at']})
        if response['ordinal'] > 12 and (not pnvl_provenance(response['provenance'])
                or response.get('budget_audit_sha256') != phase['sha256']):
            raise ValueError('v3_scope_or_audit_link_mismatch')
    historical, historical_file = historical_v2_report(folder)
    if requests[:12] != historical['requests'] or responses[:12] != historical['responses']:
        raise ValueError('historical_v2_attempts_or_receipts_changed')
    for name, digest in phase['prior_files_sha256'].items():
        reader.read(name, 'immutable_v2_baseline_bytes', digest)
    reader.read(phase['file'], 'explicit_cumulative_budget_change_audit', phase['sha256'])
    reader.read(fetcher.bootstrap_file, 'cached_empty_CVM_HTML_inspection')
    # Read the cached baseline's actual inputs, retaining their real URL or None.
    for source in fetcher.bootstrap['sources']:
        reader.read(source['file'], 'cached_shell_and_native_FCA_source_chain', source['sha256'], source['url'])
    exact.pinned_audit(reader)
    documents = {d['sequence']: d for d in exact.indexed_documents(reader)[0]}
    native = exact.native(reader, documents['742120'], exact.TARGETS['PNVL3'])
    spec = pnvl_catalogue_spec(fetcher)
    spec_file = save(folder, 'catalogue-v3-spec', spec)
    reader.read(spec_file, 'derived_literal_catalogue_request_specification')
    manifest_file = 'raw/primary-research/banks/manifest-20261025T010510695952.json'
    manifest = json.loads(reader.read(manifest_file, 'existing_offline_transport_template_acquisition_manifest')[0])
    template = spec['cached_transport_template_source']
    receipt = next(r for r in manifest if r.get('sha256') == template['sha256'])
    reader.read(template['file'], 'offline_cached_transport_schema_not_bank_facts_or_new_CDN_request',
                template['sha256'], receipt['url'])
    catalogue = next(r for r in responses if r['url'] == spec['url'])
    if (catalogue['http_status'] != 200 or catalogue.get('method') != 'POST'
            or catalogue['request_json'] != spec['request_json']):
        raise ValueError('actual_catalogue_request_must_equal_exact_literal_specification')
    selected = select_pnvl_revised_records(json.loads(fetcher.body(catalogue)), documents)
    originals = reviewed_pnvl_bodies(fetcher, selected, reader)
    if (native['native_protocol'] != exact._notice_protocol(documents['741696'])
            or native['approval_date'] != '2024-03-28' or native['record_date'] != '2024-04-05'
            or Decimal(native['gross_total_decimal']) != Decimal('0.07792434')
            or [(i['payment_date'], Decimal(i['gross'])) for i in native['installments']] != [
                ('2024-08-30', Decimal('0.05911502')), ('2025-03-31', Decimal('0.01880932'))]):
        raise ValueError('native_v2_protocol_entitlement_or_complete_annex_disagrees')
    installments = [{'installment': i + 1, 'scheduled_payment_date': item['payment_date'],
        'gross_per_share_decimal': item['gross'], 'share_class': 'ON', 'isin': 'BRPNVLACNOR9',
        'payment_date_basis': 'explicit_payment_start_date_not_settlement',
        'full_settlement_on_date_verified': False,
        'production_eligible': False} for i, item in enumerate(native['installments'])]
    if sum((Decimal(i['gross_per_share_decimal']) for i in installments), Decimal(0)) != Decimal('0.07792434'):
        raise ValueError('entire_revised_installment_sum_required')
    result = {'ticker': 'PNVL3', 'status': 'later_issuer_bodies_recovered_date_conflict_blocks_patch',
        'originals': originals, 'native_documents': {s: documents[s] for s in (*PNVL_REVISED, '742120')},
        'native_form': native, 'complete_group_id': 'PNVL3:2024-04-05',
        'identity': {'cvm': 9342, 'cnpj': '92.665.611/0001-77', 'share_class': 'ON', 'isin': 'BRPNVLACNOR9',
                     'ISIN_basis': 'only_explicit_ISIN_in_native_742120v2_linked_to_board_protocol_1216970'},
        'approval_date': '2024-03-28', 'record_date': '2024-04-05', 'kind': 'JCP',
        'corporate_total_BRL_decimal': '11600000.00', 'gross_total_per_share_decimal': '0.07792434',
        'net_total_per_share_decimal_not_used_as_gross': '0.06623569',
        'amount_tax_basis': 'explicit_gross_notice_table_corroborated_by_native_gross_ISIN_row',
        'installments': installments, 'accounting_credit_date_not_cash_date': '2024-03-31',
        'conflicts': [{'source_sequence': '741696', 'page': 1,
            'board_narrative_second_payment_date': '2025-03-28',
            'board_table_notice_PT_and_EN_native_annex_second_payment_date': '2025-03-31',
            'unresolved': True, 'silent_typo_correction': False,
            'required_external_clarification': 'Issuer correction or unambiguous governing final document resolving board prose 28 March versus table 31 March 2025.'}],
        'missing_later_issuer_body': None, 'CVM_download_bodies_remain_empty_unretried': ['741697', '741696'],
        'production_eligible': False, 'patches': [], 'settlement_verified': False}
    source_receipts = list(reader.sources.values())
    source_keys = {(s['file'], s['url']) for s in source_receipts}
    source_receipts.extend(s for s in http_sources if (s['file'], s['url']) not in source_keys)
    report = {'schema_version': 3, 'report_scope': 'current_cumulative_v3_after_explicit_12_to_20_extension',
        'status': 'acquisition_frozen_20_attempts_PNVL_bodies_recovered_conflict_not_promoted',
        'historical_v2_snapshot': {'file': historical_file, 'sha256': exact.shared._sha((folder / historical_file).read_bytes()),
            'request_count': 12, 'scope': 'immutable_historical_result_at_twelve_attempts_not_current_state'},
        'budget_change_audit': phase, 'requests': requests, 'responses': responses,
        'request_count': len(requests), 'maximum_requests': 20, 'additional_request_count': len(requests) - 12,
        'prior_request_count': 12, 'counter_reset': False, 'acquisition_frozen': True,
        'access_denial_encountered': any(r.get('access_stop') for r in responses),
        'canonical_RI_script_proof': fetcher.provenance('https://ri.grupopanvel.com.br/'),
        'catalogue_spec_file': spec_file, 'catalogue_receipt': catalogue,
        'selection_before_reading_amounts': True, 'selected_original_URL_count': 2,
        'results': [result, {'ticker': 'EZTC3', 'status': 'stopped_prohibited_loader_no_alternate_CDN_or_v3_request',
                            'exact_missing_sequences': ['509579'], 'exact_bodies_obtained': False}],
        'source_receipts': source_receipts, 'actual_HTTP_reader_receipts': http_sources,
        'source_registration_identity': 'actual_file_SHA256_and_actual_URL_not_file_alone',
        'cached_shells_are_unavailable_not_policy_denials': True, 'empty_CVM_URLs_retried': False,
        'earlier_versions_used_as_substitutes': False, 'amounts_or_dates_inferred': False,
        'production_files_changed': False, 'pointers_written': False, 'thresholds_changed': False,
        'settlement_verified': False, 'proxy': False, 'automatic_redirects': False, 'retries': 0}
    return report, save(folder, 'report-v3', report)


def build_report(folder):
    return build_v3_report(folder) if budget_v3(folder) else _build_v2_report(folder)


def render_pnvl_originals(folder):
    """Offline visual review artifacts; acquired PDF bytes are never rewritten."""
    import pypdfium2
    fetcher = Fetcher(folder)
    rendered = []
    for sequence, (_, _, digest) in PNVL_REVISED.items():
        response = next(r for r in fetcher.ledger('response') if r['sha256'] == digest)
        with pypdfium2.PdfDocument(fetcher.body(response)) as pdf:
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    bitmap = page.render(scale=1.6)
                    buffer = io.BytesIO()
                    bitmap.to_pil().save(buffer, format='PNG')
                    bitmap.close()
                finally:
                    page.close()
                name = save(folder, 'render-' + sequence + '-page-' + str(index + 1),
                            buffer.getvalue(), extension='.png')
                rendered.append({'sequence': sequence, 'page': index + 1, 'file': name,
                                 'original_sha256': digest})
    return rendered


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--fetch')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--resolve-robots-redirect')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--phase-v3-budget', action='store_true')
    parser.add_argument('--catalogue-v3', action='store_true')
    args = parser.parse_args()
    fetcher = Fetcher(args.folder)
    if args.phase_v3_budget:
        audit = budget_v3(args.folder, create=True)
        print(json.dumps({'budget_audit': audit['file'], 'prior_request_count': 12,
                          'cumulative_limit': 20, 'targets': audit['targets']}))
    if args.fetch or args.resolve_robots_redirect or args.catalogue_v3:
        try:
            if args.catalogue_v3:
                spec = pnvl_catalogue_spec(fetcher)
                response = fetcher.acquire(spec['url'], catalogue=True)
            else:
                response = (fetcher.resolve_robots_redirect(args.resolve_robots_redirect)
                            if args.resolve_robots_redirect else fetcher.acquire(args.fetch))
            print(json.dumps({k: response.get(k) for k in ('url', 'status', 'http_status', 'location', 'PDF_signature',
                                                          'bytes', 'access_stop', 'receipt_file')}, ensure_ascii=True))
            if args.inspect:
                print(json.dumps(inspect_response(args.folder, response), ensure_ascii=True, indent=2))
        except ValueError as error:
            print(json.dumps({'status': 'blocked_no_request_beyond_limit_or_policy', 'reason': str(error),
                              'attempts': len(fetcher.ledger('request'))}, ensure_ascii=True))
    elif args.report:
        report, path = build_report(args.folder)
        print(json.dumps({'report': path, 'requests': report['request_count'],
            'status': report['status'], 'access_denial_encountered': report['access_denial_encountered']}, indent=2))
    else:
        phase = budget_v3(args.folder)
        print(json.dumps({'cached_HTML_audit': fetcher.bootstrap_file,
            'seeds': [s['url'] for s in fetcher.bootstrap['issuer_seeds']],
            'requests': len(fetcher.ledger('request')),
            'maximum': V3_MAX_REQUESTS if phase else MAX_REQUESTS}, indent=2))
