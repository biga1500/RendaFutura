"""Isolated JHSF3 primary cash research, offline unless acquisition is explicit.

Only this module, its test, and raw/primary-research/jhsf-cash are owned here.
No parent integrations, pointers, source replacements, or model approvals.
Issuer notices and their CVM copies share one author lineage. A historical
calendar is not settlement confirmation or a point-in-time archive.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import parse_qs, urljoin, urlsplit
import zipfile

from lxml import html

from .alternative_cash_research import Reader, _safe
from . import cvm_proventos_documents as cvm


CACHE = 'raw/primary-research/jhsf-cash'
IDENTITY = {'ticker': 'JHSF3', 'cvm': 20605, 'cnpj': '08.294.224/0001-65',
            'share_class': 'ON', 'isin': 'BRJHSFACNOR2'}
START, CUTOFF = '2021-09-30', '2026-09-30'
MAX_REQUESTS, MAX_BYTES = 16, 12_000_000
ROBOT_URL = 'https://www.rad.cvm.gov.br/robots.txt'
USER_AGENT = 'RendaFutura-Academic/1.0'
RI_CALENDAR = ('raw/primary-research/cash-calendars/'
    'jhsf-90fa35e53e150236070d98c7778a849b350b384280f888c0a8d3d4c543b61952.html')
RI_CALENDAR_URL = 'https://ri.jhsf.com.br/informacoes-financeiras/dividendos-e-jcp/'
RI_NOTICES_URL = 'https://ri.jhsf.com.br/informacoes-financeiras/avisos-comunicados-e-fatos-relevantes/'
SEQUENCES = ('565444', '565462', '565690', '572739', '573827',
             '611114', '611118', '611700', '611704', '611834', '611835')
ORIGINALS = {
    '565444': ('2f4cf23713ce828fdbe2ae2c04c820bd15daefac8ba4eace3968804434162852', 'pdf'),
    '565462': ('8cf163da66b6a3a1231c62b415788d6b1f1a86aae5b3a1bb308387636886d2e6', 'pdf'),
    '565690': ('9f2fba05423cdbabca994a968125787611e7d3304b2d4d6fe522815d25f8af6e', 'pdf'),
    '572739': ('02b4c58a511b68c3187154ef5b3cb8c5bbb70a569d9d23ea907de6a90f89cdb9', 'bin'),
    '573827': ('1f64f84195a1f658e6639d722e0ac8e456fcbb4c84be48b2de47abd585037a65', 'pdf'),
    '611114': ('08853c25043b67bb7264b275fd3143b1839cdf00f54257f245e060b6cbcd8d4c', 'bin'),
    '611118': ('aaddc4bd7ecd84427c3fc667cce7e320eb0075528b858d1054e7d5eececa4223', 'pdf'),
    '611700': ('c9310897f2c0515afe547c574e680e892de766553d7e92bd554ea23f85440d4b', 'pdf'),
    '611704': ('725c2d96ed4197717756d2037a4eab7382ab6e41a28370dfd6d2a1d285908cb9', 'bin'),
    '611834': ('a31185ed315514c4bd9b3dbf996a85afad211e156292f843a3b30df643d14863', 'pdf'),
    '611835': ('4140f6762f26e90ccca10adb5bf62e2e887ac6abd12d3eada49853953cc4bad5', 'pdf'),
}
EXTERNAL_ORIGINALS = {
    '611834': 'raw/primary-research/cash-calendars/cvm611834v2-' + ORIGINALS['611834'][0] + '.pdf',
    '611835': 'raw/primary-research/cash-calendars/cvm611835v1-' + ORIGINALS['611835'][0] + '.pdf',
}


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                      allow_nan=False).encode('utf-8')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def save_body(folder, name, body):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise ValueError('research_filename_required')
    path = _safe(Path(folder).resolve(), CACHE + '/' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_JHSF_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return CACHE + '/' + name


def save(folder, prefix, payload):
    body = dump(payload)
    return save_body(folder, f'{prefix}-{sha(body)}.json', body)


def indexed_documents(reader):
    """Scan every issuer category, including blank-subject revisions and boards."""
    manifest, _ = reader.json('raw/primary-research/cvm-documents/index-sources.json',
                              'native_IPE_manifest')
    documents, periods = [], []
    for year in range(2021, 2027):
        choices = [source for source in manifest if source.get('year') == year]
        if len(choices) != 1:
            raise ValueError('six_exact_native_IPE_indices_required')
        source = choices[0]
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        if source.get('url') != url:
            raise ValueError('native_IPE_origin_mismatch')
        body, receipt = reader.read(source['file'], 'native_IPE_ALL_categories', source['sha256'], url)
        member = f'ipe_cia_aberta_{year}.csv'
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if (archive.namelist() != [member]
                    or archive.getinfo(member).file_size > 100_000_000):
                raise ValueError('invalid_native_IPE_archive')
            with archive.open(member) as stream:
                rows = list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
        count = 0
        for line, row in enumerate(rows, 2):
            if row.get('Codigo_CVM') != str(IDENTITY['cvm']):
                continue
            if row.get('CNPJ_Companhia') != IDENTITY['cnpj']:
                raise ValueError('JHSF_index_CNPJ_mismatch')
            published = cvm.day(row.get('Data_Entrega'))
            if not published or not published.startswith(str(year)):
                raise ValueError('native_IPE_publication_period_mismatch')
            if published > CUTOFF:
                continue
            sequence, version = cvm.document_id(row['Link_Download'])
            if int(row['Versao']) != version:
                raise ValueError('native_IPE_version_mismatch')
            document = {'cvm': IDENTITY['cvm'], 'sequence': sequence, 'version': version,
                'url': row['Link_Download'], 'published_at': published,
                'reference_date': cvm.day(row['Data_Referencia']),
                'category': row['Categoria'], 'subject': row['Assunto'], 'type': row['Tipo'],
                'index_evidence': {'file': receipt['file'], 'sha256': receipt['sha256'],
                    'member': member, 'csv_line': line, 'raw_row': row}}
            official_url(document)
            documents.append(document)
            count += 1
        periods.append({'year': year, 'source': receipt, 'member': member,
            'native_rows_scanned': len(rows), 'issuer_rows_before_cutoff': count,
            'all_categories': True, 'index_scan_complete': True,
            'cash_history_complete': False})
    return documents, periods


def official_url(document):
    parts = urlsplit(document['url'])
    query = parse_qs(parts.query, strict_parsing=True)
    if (parts.scheme != 'https' or parts.hostname != 'www.rad.cvm.gov.br'
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.fragment or parts.path != '/ENET/frmDownloadDocumento.aspx'
            or set(query) != {'Tela', 'descTipo', 'CodigoInstituicao', 'numProtocolo',
                              'numSequencia', 'numVersao'}
            or query['Tela'] != ['ext'] or query['descTipo'] != ['IPE']
            or query['CodigoInstituicao'] != ['1']
            or query['numSequencia'] != [document['sequence']]
            or query['numVersao'] != [str(document['version'])]
            or len(query['numProtocolo']) != 1 or not query['numProtocolo'][0].isdigit()
            or document.get('cvm') != IDENTITY['cvm']):
        raise ValueError('exact_indexed_JHSF_CVM_URL_required')
    return document['url']


def cached_document(reader, document):
    """Exact sequence/version reads only; a changed original is not auto-selected."""
    seq, version = document['sequence'], document['version']
    choices = []
    for directory, pattern in (
            (CACHE, f'cvm{seq}v{version}-*.pdf'),
            ('raw/primary-research/cash-calendars', f'cvm{seq}v{version}-*.pdf'),
            ('raw/primary-research/cvm-documents', f'{seq}-v{version}-*.pdf')):
        for path in sorted((reader.folder / directory).glob(pattern)):
            match = re.search(r'-([a-f0-9]{64})\.pdf$', path.name)
            if match:
                choices.append(reader.read(path.relative_to(reader.folder).as_posix(),
                    'JHSF_CVM_original', match[1], document['url']))
    if len({sha(body) for body, _ in choices}) > 1:
        raise ValueError('same_sequence_version_conflicting_originals')
    return choices[0] if choices else None


def public_links(body, source):
    if sha(body) != source['sha256'] or source['url'] not in (RI_CALENDAR_URL, RI_NOTICES_URL):
        raise ValueError('literal_issuer_discovery_source_required')
    tree = html.fromstring(body, parser=html.HTMLParser(no_network=True))
    return [{'url': urljoin(source['url'], node.get('href')),
             'text': ' '.join(node.itertext()).strip(),
             'locator': f'//a[@href][{index}]', 'source': source}
            for index, node in enumerate(tree.xpath('//a[@href]'), 1)]


def validate_public_link(folder, discovery):
    source, url = discovery['source'], discovery['url']
    body, _ = Reader(folder).read(source['file'], 'literal_issuer_link', source['sha256'], source['url'])
    if discovery not in public_links(body, source):
        raise ValueError('exact_literal_issuer_link_required')
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.username or parts.password
            or parts.port not in (None, 443) or parts.fragment or parts.query
            or not (url == RI_NOTICES_URL or parts.hostname == 'api.mziq.com'
                    and re.fullmatch(r'/mzfilemanager/v2/d/[A-Za-z0-9-]+', parts.path))):
        raise ValueError('unapproved_public_issuer_document')
    return url


def robots_url(url):
    return urlsplit(url)._replace(path='/robots.txt', query='', fragment='').geturl()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class Fetcher:
    """Persistent cap counts robots, failures and every reserved HTTP attempt."""

    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def ledger(self, prefix):
        reader = Reader(self.folder)
        return [reader.json(path.relative_to(self.folder).as_posix(), 'JHSF_network_' + prefix)[0]
                for path in sorted((self.folder / CACHE).glob(prefix + '-*.json'))]

    def _get(self, url, document=None, discovery=None, robots_for=None):
        public = discovery is not None and validate_public_link(self.folder, discovery) == url
        public_robots = (robots_for is not None
            and robots_url(validate_public_link(self.folder, robots_for)) == url)
        if (url != ROBOT_URL and not public and not public_robots
                and (document is None or official_url(document) != url)):
            raise ValueError('unapproved_URL_no_request_sent')
        attempts, responses = self.ledger('request'), self.ledger('response')
        if any(r.get('http_status') in (401, 403, 429) or r.get('access_stop') for r in responses):
            raise ValueError('prior_access_stop_no_request_sent')
        if any(r['url'] == url for r in attempts):
            raise ValueError('already_attempted_no_retry')
        if len(attempts) >= MAX_REQUESTS:
            raise ValueError('persistent_JHSF_request_budget_exhausted')
        if len(attempts) != len(responses):
            raise ValueError('unfinished_prior_request_no_retry')
        attempt = {'url': url, 'ordinal': len(attempts) + 1,
            'started_at': datetime.now(timezone.utc).isoformat(), 'proxy': False,
            'automatic_redirects': False, 'automatic_retries': 0}
        save_body(self.folder, f"request-{attempt['ordinal']:02d}.json", dump(attempt))
        body = b''
        result = {**attempt, 'status': 'failed', 'http_status': None}
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                'Accept': 'application/pdf,text/plain', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                result.update(http_status=response.status, content_type=response.headers.get('Content-Type'))
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('response_size_limit')
            result['status'] = 'received'
        except urllib.error.HTTPError as error:
            result.update(http_status=error.code, location=error.headers.get('Location'),
                          content_type=error.headers.get('Content-Type'), error=type(error).__name__)
            body = error.read(MAX_BYTES + 1)
        except (OSError, ValueError) as error:
            result['error'] = str(error)
        if len(body) > MAX_BYTES:
            body = body[:MAX_BYTES]
            result.update(status='failed', error='response_size_limit', access_stop=True)
        result['access_stop'] = (result.get('access_stop', False)
            or result['http_status'] in (401, 403, 429)
            or result['http_status'] is not None and 300 <= result['http_status'] < 400)
        suffix = 'pdf' if body.lstrip().startswith(b'%PDF-') else 'bin'
        if suffix == 'bin' and re.search(rb'captcha|access\s+denied|login\s+required|not\s+authorized', body, re.I):
            result.update(access_stop=True, error='access_challenge_not_followed')
        prefix = (f"cvm{document['sequence']}v{document['version']}" if document else
                  'issuer' if public else 'robots')
        result.update(sha256=sha(body), bytes=len(body),
                      retrieved_at=datetime.now(timezone.utc).isoformat())
        result['file'] = save_body(self.folder, f'{prefix}-{sha(body)}.{suffix}', body)
        if document:
            result['document'] = document
            result['pdf_signature_verified'] = suffix == 'pdf'
        if discovery:
            result['discovery'] = discovery
        save(self.folder, 'response', result)
        return body, result

    def acquire(self, document):
        documents, _ = indexed_documents(Reader(self.folder))
        if document not in documents:
            raise ValueError('exact_native_JHSF_index_document_required')
        existing = cached_document(Reader(self.folder), document)
        if existing:
            return {**existing[1], 'status': 'existing_verified_cache', 'document': document}
        robots = next((r for r in self.ledger('response') if r['url'] == ROBOT_URL), None)
        if robots is None:
            body, robots = self._get(ROBOT_URL)
        else:
            body, _ = Reader(self.folder).read(robots['file'], 'robots_original', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(ROBOT_URL)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(USER_AGENT, document['url']):
                raise ValueError('robots_disallow_no_document_request')
            if parser.crawl_delay(USER_AGENT) or parser.request_rate(USER_AGENT):
                raise ValueError('robots_rate_requires_spacing_no_request')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_denied_no_document_request')
        _, receipt = self._get(official_url(document), document)
        return receipt

    def acquire_public(self, discovery):
        url = validate_public_link(self.folder, discovery)
        robot_url = robots_url(url)
        robots = next((r for r in self.ledger('response') if r['url'] == robot_url), None)
        if robots is None:
            body, robots = self._get(robot_url, robots_for=discovery)
        else:
            body, _ = Reader(self.folder).read(robots['file'], 'issuer_robots', robots['sha256'])
        if robots['http_status'] == 200 and robots['status'] == 'received':
            parser = urllib.robotparser.RobotFileParser(robot_url)
            parser.parse(body.decode('utf-8').splitlines())
            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError('robots_disallow_no_document_request')
            if parser.crawl_delay(USER_AGENT) or parser.request_rate(USER_AGENT):
                raise ValueError('robots_rate_requires_spacing_no_request')
        elif robots['http_status'] != 404:
            raise ValueError('robots_unavailable_or_denied_no_document_request')
        _, receipt = self._get(url, discovery=discovery)
        return receipt


def pdf_pages(body):
    from pypdf import PdfReader
    if not body.lstrip().startswith(b'%PDF-') or len(body) > MAX_BYTES:
        raise ValueError('actual_primary_PDF_required')
    reader = PdfReader(io.BytesIO(body), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 30:
        raise ValueError('unsupported_primary_PDF')
    return [page.extract_text() or '' for page in reader.pages]


def inspect_documents(folder):
    reader = Reader(folder)
    documents, periods = indexed_documents(reader)
    output = []
    for seq in SEQUENCES:
        choices = [d for d in documents if d['sequence'] == seq]
        if len(choices) != 1:
            raise ValueError('exact_research_sequence_required:' + seq)
        doc = choices[0]
        cached = cached_document(reader, doc)
        if not cached:
            output.append({'document': doc, 'status': 'primary_original_missing'})
            continue
        body, receipt = cached
        pages = pdf_pages(body)
        native = cvm.parse_pdf(body, doc, date.fromisoformat(CUTOFF))
        output.append({'document': doc, 'source': receipt, 'status': 'original_verified',
            'pages': [{'pdf_page': i, 'text': text, 'text_sha256': sha(text.encode('utf-8'))}
                      for i, text in enumerate(pages, 1)],
            'native_events': native.get('events', []),
            'unmapped_installments': native.get('unmapped_installment_rows', [])})
    report = {'schema_version': 1, 'identity': IDENTITY, 'cutoff': CUTOFF,
        'index_periods': periods, 'documents': output,
        'sources': list(reader.sources.values()), 'history_complete': False,
        'cash_model_eligible': False, 'parent_files_changed': False}
    return report


def text(value):
    folded = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'\s*-\s*', '-', folded).split())


def _date_in(value):
    value = re.sub(r'(?<=\d)\s+(?=\d)', '', text(value))
    months = ('janeiro fevereiro marco abril maio junho julho agosto setembro outubro novembro dezembro').split()
    pattern = r'\b(\d{1,2}) de (' + '|'.join(months) + r') de (\d{4})\b'
    matches = list(re.finditer(pattern, value))
    if not matches:
        raise ValueError('explicit_Portuguese_date_required')
    match = matches[0]
    return date(int(match[3]), months.index(match[2]) + 1, int(match[1])).isoformat()


def _amount(value):
    if not re.fullmatch(r'\d+(?:\.\d{3})*(?:,\d+)?', value):
        raise ValueError('literal_positive_BRL_amount_required')
    amount = Decimal(value.replace('.', '').replace(',', '.'))
    if not amount.is_finite() or amount <= 0:
        raise ValueError('literal_positive_BRL_amount_required')
    return amount


def _document_identity(pages, document, source):
    raw = document['index_evidence']['raw_row']
    if (document['cvm'] != IDENTITY['cvm'] or raw['CNPJ_Companhia'] != IDENTITY['cnpj']
            or raw['Codigo_CVM'] != str(IDENTITY['cvm']) or raw['Link_Download'] != document['url']
            or int(raw['Versao']) != document['version']
            or cvm.day(raw['Data_Entrega']) != document['published_at']
            or document['published_at'] > CUTOFF or source.get('url') != document['url']):
        raise ValueError('original_exact_JHSF_index_identity_required')
    joined = text(' '.join(pages))
    if IDENTITY['cnpj'] not in joined or 'jhsf participacoes s.a.' not in joined:
        raise ValueError('original_JHSF_CNPJ_and_name_required')
    return joined


def parse_board(body, document, source):
    """Read actual board installment clauses, not amounts supplied by a comparator."""
    if sha(body) != source['sha256']:
        raise ValueError('board_original_hash_mismatch')
    pages = pdf_pages(body)
    joined = _document_identity(pages, document, source)
    if (cvm.norm(document['category']) != 'reuniao da administracao'
            or cvm.norm(document['type']) != 'conselho de administracao'
            or 'por acao ordinaria em circulacao' not in joined
            or 'dividendos intermediarios' not in joined):
        raise ValueError('gross_ON_dividend_board_required')
    approval = _date_in(joined.split('realizada em', 1)[1])
    if approval != document['reference_date']:
        raise ValueError('board_approval_and_reference_mismatch')
    total = re.search(r'valor bruto total de r\$\s*([\d.,]+)', joined)
    per_share = re.search(r'correspondente ao valor bruto de r\$\s*([\d.,]+)', joined)
    count = re.search(r'pagos em (\d+) \((?:quatro|tres)\) parcelas', joined)
    if not total or not per_share or not count:
        raise ValueError('board_gross_total_and_installment_count_required')
    chunks = re.split(r'\((i|ii|iii|iv)\)', joined)
    rows = []
    for number, chunk in zip(chunks[1::2], chunks[2::2]):
        amount = re.search(r'r\$\s*([\d.,]+)', chunk)
        payment = re.search(r'\bem (\d[\d ]* de [a-z]+ de \d[\d ]*)', chunk)
        record = re.search(r'base acionaria da companhia existente em (\d[\d ]* de [a-z]+ de \d[\d ]*)', chunk)
        ex = re.search(r'a partir (.{0,200})', chunk)
        if not all((amount, payment, record, ex)):
            raise ValueError('complete_role_specific_board_installment_required')
        payment_day, record_day, ex_day = map(_date_in, (payment[0], record[0], ex[0]))
        if not approval <= record_day < ex_day <= payment_day:
            raise ValueError('board_installment_chronology_invalid')
        rows.append({'installment': number, 'approval_date': approval,
            'record_date': record_day, 'ex_date': ex_day, 'payment_date': payment_day,
            'currency_total_decimal': str(_amount(amount[1])),
            'source_clause': chunk.split('fica a diretoria', 1)[0].strip()})
    if (len(rows) != int(count[1]) or len({r['record_date'] for r in rows}) != len(rows)
            or sum(Decimal(r['currency_total_decimal']) for r in rows) != _amount(total[1])):
        raise ValueError('complete_board_currency_total_mismatch')
    return {'document': document, 'source': source, 'approval_date': approval,
        'label': 'DIVIDENDO', 'share_class': 'ON', 'installments': rows,
        'currency_total_decimal': str(_amount(total[1])),
        'rounded_declared_total_per_share_decimal': str(_amount(per_share[1])),
        'installment_per_share_amounts_derived': False, 'complete_board_schedule': True,
        'author_lineage': 'JHSF_issuer_CVM_filing_not_two_independent_authors'}


def parse_original_notice(body, document, source):
    if sha(body) != source['sha256']:
        raise ValueError('notice_original_hash_mismatch')
    pages = pdf_pages(body)
    joined = _document_identity(pages, document, source)
    page = pages[0]
    for anchor in ('pagamento de dividendos', 'r$/acao', 'base acionaria', 'ex-dividendos', 'pagamento'):
        if anchor not in text(page):
            raise ValueError('exact_notice_schedule_headers_required')
    if not re.search(r'pode sofrer altera\s*c\s*ao', joined):
        raise ValueError('original_notice_revision_qualification_required')
    pattern = (r'(?m)^\s*([1-4])[^\d\n]*\s+([\d.]+)\s+(\d+,\d+)\s+'
               r'(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s*$')
    rows = [{'installment': int(m[1]), 'currency_total_decimal': str(_amount(m[2])),
             'gross_per_share_decimal': str(_amount(m[3])), 'record_date': cvm.day(m[4]),
             'ex_date': cvm.day(m[5]), 'payment_date': cvm.day(m[6]),
             'source_locator': {'pdf_page': 1, 'literal_row': m[0].strip()}}
            for m in re.finditer(pattern, page)]
    if [r['installment'] for r in rows] != [1, 2, 3, 4]:
        raise ValueError('all_four_original_notice_rows_required')
    if any(not r['record_date'] < r['ex_date'] <= r['payment_date'] for r in rows):
        raise ValueError('original_notice_date_roles_invalid')
    if sum(Decimal(r['currency_total_decimal']) for r in rows) != Decimal('130000000'):
        raise ValueError('original_notice_currency_total_mismatch')
    return {'document': document, 'source': source, 'installments': rows,
        'status': 'original_schedule_subject_to_reported_revision',
        'gross_amount_selected_as_final': False}


def board_revision(prior, current):
    old, new = prior['document'], current['document']
    if (any(old[k] != new[k] for k in ('cvm', 'reference_date', 'category', 'type'))
            or old['index_evidence']['raw_row']['CNPJ_Companhia'] != new['index_evidence']['raw_row']['CNPJ_Companhia']
            or new['version'] != old['version'] + 1 or new['published_at'] <= old['published_at']
            or prior['currency_total_decimal'] != current['currency_total_decimal']
            or len(prior['installments']) != len(current['installments'])):
        raise ValueError('exact_same_board_version_chain_required')
    changes = []
    for before, after in zip(prior['installments'], current['installments']):
        if before['installment'] != after['installment']:
            raise ValueError('revision_installment_identity_mismatch')
        for field in ('currency_total_decimal', 'payment_date', 'record_date', 'ex_date'):
            if before[field] != after[field]:
                changes.append({'installment': before['installment'], 'field': field,
                                'prior': before[field], 'current': after[field]})
    if changes != [{'installment': 'i', 'field': 'record_date', 'prior': '2023-04-18', 'current': '2023-04-19'},
                   {'installment': 'i', 'field': 'ex_date', 'prior': '2023-04-19', 'current': '2023-04-20'}]:
        raise ValueError('unexpected_board_revision_preserve_conflict')
    return {'prior': old, 'current': new, 'changes': changes,
        'basis': 'same_CVM_board_reference_category_type_incremented_version_and_literal_fields',
        'independent_authors': False, 'historical_versions_retained': True}


def _pinned_original(reader, document, receipts):
    seq = document['sequence']
    expected, suffix = ORIGINALS[seq]
    name = EXTERNAL_ORIGINALS.get(seq, f"{CACHE}/cvm{seq}v{document['version']}-{expected}.{suffix}")
    body, source = reader.read(name, 'pinned_actual_CVM_original', expected, document['url'])
    matching = [r for r in receipts if r.get('url') == document['url']]
    if matching and (len(matching) != 1 or matching[0].get('document') != document
                     or matching[0].get('sha256') != expected or matching[0].get('file') != name):
        raise ValueError('original_acquisition_receipt_index_or_hash_mismatch')
    if not matching and seq in EXTERNAL_ORIGINALS:
        receipt_name = name[:-4] + '-source.json'
        if (reader.folder / receipt_name).is_file():
            prior, _ = reader.json(receipt_name, 'existing_original_acquisition_receipt')
            if prior.get('url') != document['url'] or prior.get('sha256') != expected or prior.get('file') != name:
                raise ValueError('existing_original_acquisition_receipt_mismatch')
            matching = [prior]
    return body, {**source, 'id': 'jhsf-primary:' + source['sha256'],
        'authority': 'primary_issuer_CVM_copy', 'author_lineage': 'JHSF_issuer',
        'retrieved_at': matching[0].get('retrieved_at') if matching else None,
        'acquisition_receipt_verified': bool(matching),
        'retrieval_basis': 'actual_acquisition_receipt' if matching else 'network_observation_unknown_no_mtime_substitution'}


def acquisition_receipts(reader):
    receipts = []
    for path in sorted((reader.folder / CACHE).glob('response-*.json')):
        receipt, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'JHSF_acquisition_receipt')
        body, _ = reader.read(receipt['file'], 'actual_acquired_response', receipt['sha256'], receipt['url'])
        if len(body) != receipt['bytes']:
            raise ValueError('acquired_response_length_mismatch')
        receipts.append(receipt)
    return receipts


def load_jhsf_evidence(folder):
    """Frozen, read-only documentary result; no directory-wide cache inventory."""
    reader = Reader(folder)
    documents, periods = indexed_documents(reader)
    receipts = acquisition_receipts(reader)
    docs = {}
    for seq in SEQUENCES:
        choices = [d for d in documents if d['sequence'] == seq]
        if len(choices) != 1:
            raise ValueError('one_exact_pinned_JHSF_index_document_required')
        docs[seq] = choices[0]
    originals, bodies, source_map = [], {}, {}
    for seq in SEQUENCES:
        doc = docs[seq]
        body, source = _pinned_original(reader, doc, receipts)
        bodies[seq], source_map[seq] = body, source
        native = cvm.parse_pdf(body, doc, date.fromisoformat(CUTOFF)) if body.startswith(b'%PDF-') else None
        status = ('native_form_index_only_no_cash_fields' if seq == '573827' and not native['events']
                  else 'empty_ENET_HTML_not_original_notice' if native is None else 'original_verified')
        originals.append({'document': doc, 'source': source, 'status': status,
            'native_events': native['events'] if native else [],
            'unmapped_installments': native.get('unmapped_installment_rows', []) if native else []})
    boards = {seq: parse_board(bodies[seq], docs[seq], source_map[seq])
              for seq in ('565444', '611118', '611700')}
    notice = parse_original_notice(bodies['565462'], docs['565462'], source_map['565462'])
    original_dates = [(r['record_date'], r['ex_date'], r['payment_date']) for r in notice['installments']]
    if original_dates != [(r['record_date'], r['ex_date'], r['payment_date'])
                          for r in boards['565444']['installments']]:
        raise ValueError('December_board_and_original_notice_disagree')
    revision = board_revision(boards['611118'], boards['611700'])
    april_document = next(o for o in originals if o['document']['sequence'] == '611834')
    if april_document['unmapped_installments']:
        raise ValueError('April_unmapped_installments_block_complete_group')
    april = april_document['native_events']
    april = [e for e in april if e.get('isin') == IDENTITY['isin']]
    final = boards['611700']['installments'][0]
    if (len(april) != 1 or april[0]['issues'] or april[0]['approval_date'] != '2023-04-13'
            or april[0]['record_date'] != final['record_date']
            or april[0]['payment_date'] != final['payment_date']
            or april[0]['payment_form'] != 'a vista'):
        raise ValueError('April_exact_ISIN_gross_and_complete_payment_group_required')
    value = april[0]['gross_per_unit_decimal']
    if Decimal(value) <= 0:
        raise ValueError('positive_native_gross_required')
    group_id = 'JHSF3:2023-04-19:DIVIDENDO'
    event = {**IDENTITY, 'label': 'DIVIDENDO', 'approval_date': '2023-04-13',
        'record_date': final['record_date'], 'ex_date': final['ex_date'], 'payment_date': final['payment_date'],
        'gross_per_share_decimal': value, 'reported_per_share_decimal': value,
        'amount_tax_basis': 'explicit_gross_exact_ISIN_unit', 'amount_unit': 'BRL/share',
        'identity_basis': 'exact_ON_ISIN_form_and_ordinary_share_board',
        'status': 'past_scheduled', 'published_at': docs['611834']['published_at'],
        'available_as_of': True, 'gross_reconstructed_from_net': False,
        'source_ref': source_map['611834']['id'], 'complete_group_id': group_id,
        'source_locator': {'format': 'JHSF_CVM_board_revision_exact_ISIN_gross',
            'native_table': april[0].get('source_table'), 'native_row': april[0].get('source_row')},
        'primary_evidence': [source_map['611118'], source_map['611700'], source_map['611834']],
        'issues': [], 'target_group_complete': True, 'cash_model_eligible': False,
        'settlement_verified': False, 'point_in_time_archive_verified': False}
    group = {'id': group_id, 'record_date': event['record_date'], 'events': [event],
        'installment_count': 1, 'gross_total_decimal': value, 'target_group_complete': True,
        'completeness_basis': 'exact_ISIN_a_vista_form_no_unmapped_installments_and_revised_board',
        'scope': 'one_entitlement_group_not_entire_April_board_declaration',
        'entire_issuer_history_complete': False}
    pending = [{'record_date': row['record_date'], 'payment_date': row['payment_date'],
        'original_notice_gross_per_share_decimal': row['gross_per_share_decimal'],
        'selected_final_gross_per_share_decimal': None, 'target_group_complete': False,
        'blockers': ['January_revised_notice_572739_actual_content_unavailable',
                     'revised_native_form_573827_contains_index_only'],
        'original_notice_source': source_map['565462'], 'known_revision': docs['572739']}
        for row in notice['installments'][1:]]
    return {'schema_version': 1, 'identity': IDENTITY, 'window_start': START, 'cutoff': CUTOFF,
        'index_periods': periods, 'originals': originals, 'boards': boards,
        'original_December_notice': notice, 'board_revision': revision,
        'events': [event], 'complete_groups': [group], 'pending_amount_groups': pending,
        'April_board_all_native_gross_installments_verified': False,
        'other_April_board_entitlements_not_promoted': boards['611700']['installments'][1:],
        'record_corrections_for_parent_review': [{'from': '2023-04-18', 'to': '2023-04-19',
            'payment_date': '2023-04-28', 'complete_group_id': group_id,
            'target_group_complete': True, 'applied_to_parent': False}],
        'sources': [{**r, 'id': 'jhsf-primary:' + r['sha256'],
                     'retrieved_at': next((s.get('retrieved_at') for s in source_map.values()
                                           if s['file'] == r['file']), None)}
                    for r in reader.sources.values()],
        'source_independence': {'author_lineages': ['JHSF_issuer'],
            'CVM_and_RI_are_not_independent_authors': True, 'price_independence_not_added': True},
        'history_complete': False, 'cash_model_eligible': False, 'model_admissions': 0,
        'settlement_verified': False, 'point_in_time_archive_verified': False,
        'financial_gates_waived': False, 'missing_as_zero': False,
        'parent_files_changed': False, 'parent_pointers_changed': False}


def verified_jhsf_calendar(folder, stock, cutoff):
    if any(stock.get(k) != IDENTITY[k] for k in ('ticker', 'cvm', 'share_class', 'isin')):
        raise ValueError('exact_JHSF3_CVM_ON_ISIN_required')
    cutoff = cutoff.isoformat() if type(cutoff) is date else cutoff
    if cutoff != CUTOFF:
        raise ValueError('reviewed_JHSF_cutoff_required')
    evidence = load_jhsf_evidence(folder)
    return {**IDENTITY, 'as_of': cutoff,
        **{k: evidence[k] for k in ('events', 'complete_groups', 'pending_amount_groups',
            'record_corrections_for_parent_review', 'sources', 'history_complete',
            'cash_model_eligible', 'settlement_verified', 'point_in_time_archive_verified')}}


def cached_comparison(folder, evidence):
    """Diagnostics after primary decisions; never a source of selected amounts."""
    from .cash_calendar_research import inspect_calendar
    reader = Reader(folder)
    receipt, _ = reader.json(RI_CALENDAR[:-5] + '-source.json', 'JHSF_RI_calendar_receipt')
    body, _ = reader.read(RI_CALENDAR, 'JHSF_RI_calendar_original', receipt['sha256'], RI_CALENDAR_URL)
    calendar = inspect_calendar(body, 'jhsf', 'JHSF3', CUTOFF, receipt)
    payload, source = reader.json('raw/expanded/JHSF3-b3-cash-1.json', 'existing_exchange_diagnostic_only')
    meta = payload.get('page', {})
    if (meta.get('pageNumber') != 1 or meta.get('totalPages') != 1
            or meta.get('totalRecords') != len(payload.get('results', []))):
        raise ValueError('complete_existing_comparator_page_required')
    exchange = {}
    for row in payload['results']:
        if row.get('typeStock', '').strip() == 'ON' and cvm.norm(row.get('corporateAction')) == 'dividendo':
            record = cvm.day(row['lastDatePriorEx'])
            quota = _amount(row.get('quotedPerShares') or '1')
            exchange[record] = exchange.get(record, Decimal(0)) + _amount(row['valueCash']) / quota
    rows = []
    for pending in evidence['pending_amount_groups']:
        record = pending['record_date']
        primary = [e for e in calendar['events'] if e['record_date'] == record and e['label'] == 'DIVIDENDO']
        rows.append({'record_date': record, 'payment_date': pending['payment_date'],
            'original_CVM_notice_gross': pending['original_notice_gross_per_share_decimal'],
            'current_issuer_calendar_gross': [e['gross_per_share_decimal'] for e in primary],
            'cached_exchange_entitlement_gross': str(exchange[record]) if record in exchange else None,
            'selected_final_amount': None, 'unresolved': True})
    rows.append({'original_RI_record_date': '2023-04-18',
        'native_and_revised_board_record_date': '2023-04-19',
        'native_gross': evidence['events'][0]['gross_per_share_decimal'],
        'cached_exchange_entitlement_gross': str(exchange.get('2023-04-19')),
        'group_complete': True, 'parent_override_applied': False})
    return {'rows': rows, 'sources': list(reader.sources.values()),
        'selection_completed_before_market_comparison': True, 'B3_network_requests': 0,
        'exchange_cache_used_as_payment_calendar': False}


def build_report(folder):
    report = load_jhsf_evidence(folder)
    report['cached_comparison'] = cached_comparison(folder, report)
    report['sources'].extend(report['cached_comparison']['sources'])
    fetcher = Fetcher(folder)
    report['network'] = {'requests': fetcher.ledger('request'), 'responses': fetcher.ledger('response'),
        'maximum_requests_including_robots': MAX_REQUESTS,
        'B3_network_requests': 0, 'denied_CDN_requested': False,
        'remaining_gap': 'issuer_listing_document_loader_uses_denied_cdn_sites_assets_origin_not_requested'}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', nargs='+')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.acquire:
        if len(set(args.acquire)) != len(args.acquire) or any(s not in SEQUENCES for s in args.acquire):
            raise ValueError('explicit_bounded_JHSF_sequence_selection_required')
        documents, _ = indexed_documents(Reader(args.folder))
        fetcher = Fetcher(args.folder)
        for seq in args.acquire:
            choices = [d for d in documents if d['sequence'] == seq]
            if len(choices) != 1:
                raise ValueError('exact_research_sequence_required:' + seq)
            result = fetcher.acquire(choices[0])
            print(json.dumps({'sequence': seq, 'status': result['status'],
                              'http_status': result.get('http_status')}), flush=True)
            if result.get('access_stop'):
                break
    if args.inspect:
        report = inspect_documents(args.folder)
        path = save(args.folder, 'inspection', report)
        print(json.dumps({'inspection': path, 'documents': len(report['documents'])}))
    if args.report:
        report = build_report(args.folder)
        path = save(args.folder, 'report', report)
        print(json.dumps({'report': path, 'complete_groups': len(report['complete_groups']),
                          'pending_amount_groups': len(report['pending_amount_groups']),
                          'model_admissions': report['model_admissions']}))


if __name__ == '__main__':
    main()
