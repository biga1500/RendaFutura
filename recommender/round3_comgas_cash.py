"""Offline, exact-security Comgas cash views. No core or raw-cache writes.

The proposed hooks are apply_exact_repairs before payment enrichment and
canonical_independent_cash_rows before independent class filtering. Neither
function admits a security, changes a financial class, nor confirms settlement.
"""
from collections import Counter
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader

from . import cash_repair_proofs as proof
from . import cvm_proventos_documents as cvm
from .round2_native_prices import read, receipt_source


CUTOFF = date(2026, 9, 30)
SCOPE = 'round3-comgas'
IDENTITIES = {
    'CGAS3': {'ticker': 'CGAS3', 'cvm': 15636, 'share_class': 'ON', 'isin': 'BRCGASACNOR0'},
    'CGAS5': {'ticker': 'CGAS5', 'cvm': 15636, 'share_class': 'PN', 'isin': 'BRCGASACNPA3'},
}
CNPJ = '61.856.571/0001-17'
AUDIT_SHA = '1c13efe2e1a51a9aed2e05462d83fb3a0552347642c4416264f8e6a25623b381'
AUDIT = 'raw/primary-research/all-pending/cash/audit-' + AUDIT_SHA + '.json'
MANIFEST_SHA = 'f71c25995f4c9bd04e1e29bfef1645d222fe8c7158d859d64c7b93239ab9c98b'
DETAIL_SHA = '7f3222ffa59f17fea03e6990c31338e9dba901914591f01a2b46441077ea6f11'
WORKBOOK_SHA = 'f65b059d313a8713f06ac6885fb7d303f34058cc0a1587bcde14884f82a47932'
WORKBOOK = 'raw/primary-research/all-pending/cash/body-' + WORKBOOK_SHA + '.bin'
PAGE_SHA = '25572ad31e6b2e17bd5697c6a8715b6661ecb3c4cc03dcb6df0043021d09123d'
PAYMENT_SHAS = {
    'CGAS3': '5668a8706bee3098f005802edf45a0695e303c944dd6555bd1dc03b1889953e5',
    'CGAS5': '6b6e85ba266233576140768f28275c2e31ce0f6730d298dd25e4128e4528163c',
}
FCA_PINS = {
    2021: '65ba4b0225e9b2106901cdf348215933be8affba79925fc71361e2f6818c7999',
    2022: 'b45e655c6ffae0b40ddb0425e27cbcca7d2478867f542d78c1aa034293eeb77a',
    2023: '1df320e608f685985ad142d79f36218f26b8426e4f6a8e43ba4e836666c02dd3',
    2024: '2b9a6a7631120b78c99d1dbf52cb2f0083d03bf9e87cae869864fd7685444fab',
    2025: 'd3bf01b0f751aa001fba318adad7b8fb8f2d7cc84a4c5b03741b9e8ef9cadf37',
    2026: proof.FCA_SHA,
}
REPAIR_ID = 'Comgas_exact_class_native_cash_v1'
BINDING_ID = 'CGAS5_sole_FCA_PN_cached_PNA_exact_native_ISIN_v1'
ON_NOTICE_SEQUENCES = ('447514', '541493', '824972')
NOTICE_REVIEWS = 'raw/primary-research/comgas-cash/reviewed-notices.json'
NOTICE_REVIEWS_SHA = 'aeb6b69e8bef6556733b58b45696cc2b8f29e57e258e6a4458f83c5793dce283'
FORM_SPECS = {
    '600420': (1, '1075488', '2023-03-21', '2023-03-24', '2023-04-04', 4),
    '670033': (1, '1145142', '2023-09-21', '2023-09-26', '2023-10-06', 2),
    '739201': (1, '1213611', '2024-03-27', '2024-04-02', '2024-04-11', 2),
    '794236': (1, '1269054', '2024-08-08', '2024-08-13', '2024-08-30', 2),
    '1061747': (2, '1533179', '2026-06-10', '2026-06-15', '2026-06-25', 4),
}
DEEPER_SEQUENCES = ('516848', '550540', '612490', '485350', '497332', '556285')
DEEP_REQUEST_LIMIT = 20


def _identity(identity, cutoff):
    ticker = identity.get('ticker')
    proof.require(ticker in IDENTITIES and cutoff == CUTOFF
        and type(identity.get('cvm')) is int
        and {k: identity.get(k) for k in IDENTITIES[ticker]} == IDENTITIES[ticker],
        'comgas_exact_security_and_cutoff_required')
    return IDENTITIES[ticker]


def _source(cache, file, digest, *, url=None, authority='primary'):
    body, source = proof.read(cache, file, digest, url)
    source.update(authority=authority)
    return body, source


def build_manifest(folder):
    """Extract a small derived catalogue, never a counterfeit primary file."""
    root = Path(folder).resolve()
    path = root / AUDIT
    proof.require(path.resolve().is_relative_to(root) and not path.is_symlink()
        and path.stat().st_size < 100_000_000, 'comgas_unsafe_original_research_catalogue')
    body = path.read_bytes()
    proof.require(hashlib.sha256(body).hexdigest() == AUDIT_SHA, 'comgas_original_research_catalogue_changed')
    audit = json.loads(body)
    selected = set(FORM_SPECS) | set(ON_NOTICE_SEQUENCES) | {'568344', '550546', '516861'}
    packet = {'authority': 'derived_catalogue_extracted_from_hash_pinned_research_not_primary',
        'origin': {'file': AUDIT, 'sha256': AUDIT_SHA, 'bytes': len(body)},
        'index_sources': [s for s in audit['index_sources'] if s['file'].endswith('.zip')],
        'inspected_originals': [{k: x[k] for k in ('document', 'source')}
            for x in audit['inspected_originals'] if x.get('document', {}).get('sequence') in selected],
        'new_public_sources': [s['source'] for s in audit['new_public_sources']
                              if s.get('source', {}).get('sha256') in (WORKBOOK_SHA, PAGE_SHA)],
        'acquisition_scope': SCOPE, 'new_HTTP_requests': 0, 'original_bytes_not_copied_or_modified': True}
    encoded = json.dumps(packet, sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    target = root / f'raw/primary-research/all-pending/{SCOPE}/round3-comgas-manifest-{digest}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        proof.require(target.read_bytes() == encoded, 'comgas_immutable_manifest_conflict')
    else:
        with target.open('xb') as stream:
            stream.write(encoded)
    return target, digest


def acquire_deeper_documents(folder, sequences=DEEPER_SEQUENCES):
    """Bounded new exact-index originals; never retry a previously seen filing."""
    from unittest.mock import patch
    from .bulk_pending_sources import PublicSourceCollector
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    proof.require(sequences and len(sequences) <= len(DEEPER_SEQUENCES)
        and len(set(sequences)) == len(sequences) and set(sequences) <= set(DEEPER_SEQUENCES),
        'comgas_bounded_native_discovery_only')
    cache = _Cache(root, load_inventory(root))
    relative = f'raw/primary-research/all-pending/{SCOPE}/round3-comgas-manifest-{MANIFEST_SHA}.json'
    audit = json.loads(read(root, relative, MANIFEST_SHA))
    indexes, _ = _indexes(cache, audit)
    collector = PublicSourceCollector(root, SCOPE)
    existing = []
    for path in (root / 'raw/primary-research').rglob('*.json'):
        if path.stat().st_size > 30_000 or not cache.safe_path(path):
            continue
        encoded = path.read_bytes()
        try:
            value = json.loads(encoded)
        except (ValueError, UnicodeError):
            proof.require(not any(('numSequencia=' + seq).encode() in encoded for seq in sequences),
                          'comgas_unknown_prior_filing_outcome_no_retry')
            continue
        if isinstance(value, dict) and isinstance(value.get('url'), str):
            try:
                filing = cvm.document_id(value['url'])
            except ValueError:
                continue
            if filing[0] in sequences and 'status' in value and 'sha256' in value and 'file' in value:
                existing.append(value)
    results = []
    for sequence in sequences:
        proof.require(len(list(collector.base.glob('request-*.json'))) < DEEP_REQUEST_LIMIT,
                      'comgas_persisted_deeper_budget_exhausted')
        selected = [(year, line, row) for year, rows in indexes.items() for line, row in enumerate(rows, 2)
            if int(row['Codigo_CVM']) == 15636 and cvm.document_id(row['Link_Download'])[0] == sequence]
        proof.require(len(selected) == 1, 'comgas_unique_deeper_IPE_original_required')
        year, line, row = selected[0]
        doc = {'sequence': sequence, 'version': int(row['Versao']), 'cvm': 15636,
            'issuer_name': row['Nome_Companhia'], 'published_at': cvm.day(row['Data_Entrega']),
            'reference_date': cvm.day(row['Data_Referencia']), 'url': row['Link_Download'],
            'category': row['Categoria'], 'type': row['Tipo'], 'subject': row['Assunto'],
            'index_evidence': {'file': next(s['file'] for s in audit['index_sources']
                if f'/index-{year}-' in s['file']), 'csv_line': line, 'raw_row': row}}
        _latest_index(doc, indexes)
        prior = [r for r in existing if cvm.document_id(r['url'])[0] == sequence]
        if prior:
            proof.require(len({(r['url'], r['sha256']) for r in prior}) == 1,
                          'comgas_prior_original_outcome_ambiguous_no_retry')
            receipt = prior[0]
            collector.body(receipt)
        else:
            with patch('urllib.request.getproxies', return_value={}):
                receipt = collector.fetch(doc['url'], discovery={'kind': 'exact_primary_CVM_IPE_CNPJ_row',
                    'index_file': doc['index_evidence']['file'], 'csv_line': line,
                    'CNPJ': CNPJ, 'CVM': 15636, 'sequence': sequence, 'version': doc['version']})
        results.append({'document': doc, 'source': receipt})
        if receipt['status'] in (401, 403, 429) or receipt['error']:
            break
    packet = {'schema': 'round3-comgas-deeper-native-acquisition-v1', 'scope': SCOPE,
        'base_manifest_sha256': MANIFEST_SHA, 'inspected_originals': results,
        'new_B3_requests': 0, 'api_mziq_requested': False,
        'discovery_not_cash_admission': True}
    body = json.dumps(packet, sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    target = root / f'raw/primary-research/all-pending/{SCOPE}/round3-comgas-deeper-{digest}.json'
    if target.exists():
        proof.require(target.read_bytes() == body, 'comgas_immutable_deeper_packet_conflict')
    else:
        with target.open('xb') as stream:
            stream.write(body)
    return target, digest, packet


def catalogue_spec(folder):
    """The issuer's literal current catalogue, not an alias for a stopped API."""
    from lxml import html
    root = Path(folder).resolve()
    sources, configs = [], []
    for suffix in ('avisos', 'reunioes-da-administracao'):
        url = f'https://ri.comgas.com.br/divulgacoes-e-documentos/{suffix}/'
        relative = f'raw/primary-research/all-pending/{SCOPE}/response-' + hashlib.sha256(url.encode()).hexdigest() + '.json'
        source = json.loads((root / relative).read_bytes())
        body, receipt = receipt_source(root, source, source['sha256'])
        scripts = '\n'.join(s.text or '' for s in html.fromstring(body).xpath('//script[not(@src)]'))
        def literal(pattern):
            matches = re.findall(pattern, scripts)
            proof.require(len(matches) == 1, 'comgas_unique_issuer_catalogue_literal_required')
            return matches[0]
        base = literal(r"var fmBase\s*=\s*'([^']+)'")
        company = literal(r"var fmId\s*=\s*'([^']+)'")
        category = literal(r'categories\.push\(\{[^}]*internal_name:\s*"([^"]+)"')
        language = re.findall(r"language:\s*'([^']+)'", scripts)
        proof.require(language and set(language) == {'pt_BR'}, 'comgas_catalogue_language_changed')
        configs.append((base, company, category))
        sources.append(receipt)
    proof.require({(base, company) for base, company, _ in configs}
        == {('https://apicatalog.mziq.com/filemanager', '14abe1c8-5911-4e94-a991-82f500120a89')}
        and {category for _, _, category in configs} == {'avisos', 'reunioes_da_administracao'},
        'comgas_literal_independent_published_catalogue_changed')
    file = 'raw/primary-research/banks/bradesco-filemanager-script-d8d86b0e03f682b8797f.js'
    digest = 'd8d86b0e03f682b8797f951642a27c09258ba9e323c84057831f2fd9d443c6cb'
    script = read(root, file, digest).decode('utf-8')
    spans = ['return c+"/company/"+e+"/filter/categories/year/meta"',
        'n={year:a,categories:w(l(t.config.categories)),language:t.config.language,published:!0}',
        '$.ajax({type:"POST",url:e,data:JSON.stringify(n)']
    proof.require(all(span in script for span in spans), 'comgas_cached_public_transport_template_changed')
    return {'url': configs[0][0] + '/company/' + configs[0][1] + '/filter/categories/year/meta',
        'method': 'POST', 'request_json': {'year': '2022',
            'categories': [category for _, _, category in configs], 'language': 'pt_BR', 'published': True},
        'discovery': {'kind': 'literal_issuer_published_read_only_catalogue_filter',
            'issuer_configuration_sources': sources, 'generic_transport_only_not_bank_evidence': {
                'file': file, 'sha256': digest, 'literal_spans': spans},
            'no_rewrite_or_request_to_stopped_api_mziq_origin': True}}


def acquire_catalogue(folder):
    """PublicSourceCollector policy extended only to the proven read-only POST."""
    from unittest.mock import patch
    from urllib.parse import urlsplit
    import time
    import urllib.error
    import urllib.request
    from protego import Protego
    from .bulk_pending_sources import PublicSourceCollector, USER_AGENT, MAX_BYTES, _save, _stopped
    root = Path(folder).resolve()
    spec = catalogue_spec(root)
    collector = PublicSourceCollector(root, SCOPE)
    url, payload = spec['url'], spec['request_json']
    body = json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode()
    key = hashlib.sha256(b'POST\n' + url.encode() + b'\n' + body).hexdigest()
    response_file = collector.base / ('response-' + key + '.json')
    if response_file.exists():
        receipt = json.loads(response_file.read_bytes())
        collector.body(receipt)
        return receipt
    proof.require(not (collector.base / ('request-' + key + '.json')).exists(), 'comgas_interrupted_catalogue_no_retry')
    host = urlsplit(url).hostname
    proof.require(host == 'apicatalog.mziq.com' and host not in collector.prior_stops,
                  'comgas_stopped_or_unpublished_catalogue_origin')
    with patch('urllib.request.getproxies', return_value={}):
        robots = collector.fetch('https://' + host + '/robots.txt', {'kind': 'robots_policy'})
    proof.require(not robots['error'] and robots['status'] in (200, 404), 'comgas_catalogue_robots_unavailable')
    policy = Protego.parse(collector.body(robots).decode('utf-8', errors='replace'))
    allowed = robots['status'] == 404 or policy.can_fetch(url, USER_AGENT)
    proof.require(allowed, 'comgas_catalogue_robots_disallow')
    delay = max(1, policy.crawl_delay(USER_AGENT) or 0)
    rate = policy.request_rate(USER_AGENT)
    if rate:
        delay = max(delay, rate.seconds / rate.requests)
    proof.require(delay <= 60 and len(list(collector.base.glob('request-*.json'))) < DEEP_REQUEST_LIMIT,
                  'comgas_catalogue_rate_or_persisted_budget_exhausted')
    time.sleep(delay)
    request = {'url': url, 'method': 'POST', 'request_json': payload, 'scope': SCOPE,
        'discovery': spec['discovery'], 'started_at': datetime.now(timezone.utc).isoformat(),
        'retries': 0, 'maximum_bytes': MAX_BYTES}
    _save(collector.base / ('request-' + key + '.json'), request)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    content, status, error, response, headers = b'', None, None, None, {}
    try:
        with patch('urllib.request.getproxies', return_value={}):
            try:
                response = urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(
                    url, data=body, method='POST', headers={'User-Agent': USER_AGENT,
                    'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}), timeout=25)
            except urllib.error.HTTPError as failure:
                response = failure
            status, headers = response.status, dict(response.headers.items())
            content = response.read(MAX_BYTES + 1)
        if status in (401, 403, 429):
            error = 'access_stop_no_retry'
        elif status != 200:
            error = 'non_success_no_retry'
        elif len(content) > MAX_BYTES:
            error = 'response_size_limit'
        elif re.search(rb'(?is)<title[^>]*>[^<]*(?:captcha|access denied|sign in|just a moment)', content):
            error = 'public_content_challenge_stop'
    except Exception as failure:
        error = type(failure).__name__
    finally:
        if response is not None:
            response.close()
    sha = hashlib.sha256(content).hexdigest()
    file = collector.base / ('body-' + sha + '.bin')
    _save(file, content)
    receipt = {**request, 'status': status, 'error': error, 'bytes': len(content),
        'file': file.relative_to(root).as_posix(), 'sha256': sha, 'headers': headers,
        'retrieved_at': datetime.now(timezone.utc).isoformat(), 'published_at': None,
        'bank_settlement_verified': False, 'model_admission': False,
        'location': headers.get('Location') or headers.get('location'), 'robots_allowed': allowed}
    _save(response_file, receipt)
    if _stopped(receipt):
        collector.prior_stops.add(host)
    return receipt


def _table(body, member):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        proof.require(archive.namelist().count(member) == 1, 'comgas_unique_ZIP_member_required')
        return list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))


def _registry_year(body, year):
    """Inspect every issuer security, not just the selected ticker row."""
    from .security_registration import _kind
    masters = [r for r in _table(body, f'fca_cia_aberta_{year}.csv') if int(r['CD_CVM']) == 15636]
    proof.require(masters and all(m['CNPJ_CIA'] == CNPJ
        and m['DT_RECEB'] <= CUTOFF.isoformat() and m['DT_REFER'] <= CUTOFF.isoformat()
        for m in masters), 'comgas_FCA_master_not_unique_or_late')
    latest = max((m['DT_REFER'], int(m['VERSAO']), m['DT_RECEB']) for m in masters)
    current = [m for m in masters if (m['DT_REFER'], int(m['VERSAO']), m['DT_RECEB']) == latest]
    proof.require(len(current) == 1, 'comgas_FCA_latest_master_ambiguous')
    master = current[0]
    rows = [r for r in _table(body, f'fca_cia_aberta_valor_mobiliario_{year}.csv')
            if r['ID_Documento'] == master['ID_DOC'] or r['CNPJ_Companhia'] == CNPJ]
    proof.require(rows and all(r['ID_Documento'] == master['ID_DOC']
        and r['CNPJ_Companhia'] == CNPJ and r['Versao'] == master['VERSAO']
        and r['Data_Referencia'] == master['DT_REFER'] for r in rows), 'comgas_FCA_child_binding_conflict')
    equity = [r for r in rows if _kind(r)[0] in ('ordinary_share', 'preferred_share')]
    proof.require(Counter((_kind(r)[1], r['Codigo_Negociacao'].strip()) for r in equity)
        == Counter({('ON', 'CGAS3'): 1, ('PN', 'CGAS5'): 1}),
        'comgas_all_equity_classes_not_unique_ON_and_PN')
    return {'year': year, 'master': master, 'all_issuer_master_versions': masters,
            'all_issuer_security_rows': rows,
            'only_preferred_class': 'PN', 'only_preferred_code': 'CGAS5'}


def _indexes(cache, audit):
    output, sources = {}, []
    for year in range(2021, 2027):
        matches = [s for s in audit['index_sources'] if s['file'].endswith('.zip')
                   and f'/index-{year}-' in s['file']]
        proof.require(len(matches) == 1, 'comgas_all_six_IPE_indexes_required')
        source = matches[0]
        body, receipt = _source(cache, source['file'], source['sha256'], url=source['url'])
        receipt['retrieved_at'] = source.get('retrieved_at')
        output[year] = _table(body, f'ipe_cia_aberta_{year}.csv')
        sources.append(receipt)
    return output, sources


def _latest_index(doc, indexes):
    evidence = doc['index_evidence']
    year = int(re.search(r'index-(20\d\d)-', evidence['file'])[1])
    selected = indexes[year][evidence['csv_line'] - 2]
    proof.require(selected == evidence['raw_row'] and selected['CNPJ_Companhia'] == CNPJ
        and int(selected['Codigo_CVM']) == 15636 and selected['Link_Download'] == doc['url']
        and cvm.document_id(doc['url']) == (doc['sequence'], doc['version'])
        and int(selected['Versao']) == doc['version']
        and cvm.day(selected['Data_Entrega']) == doc['published_at'] <= CUTOFF.isoformat(),
        'comgas_exact_IPE_binding_changed')
    family = [r for rows in indexes.values() for r in rows
        if r['CNPJ_Companhia'] == CNPJ and int(r['Codigo_CVM']) == 15636
        and cvm.day(r['Data_Entrega']) and cvm.day(r['Data_Entrega']) <= CUTOFF.isoformat()
        and ((selected['Protocolo_Entrega'] and r['Protocolo_Entrega'] == selected['Protocolo_Entrega'])
             or (r['Categoria'] == selected['Categoria'] and r['Data_Referencia'] == selected['Data_Referencia']
                 and (not selected['Protocolo_Entrega'] or r['Tipo'] == selected['Tipo'])))]
    proof.require(family and max(int(r['Versao']) for r in family) == doc['version'],
                  'comgas_superseded_native_document')
    return selected


def _document(cache, audit, indexes, sequence):
    matches = [x for x in audit['inspected_originals'] if x.get('document', {}).get('sequence') == sequence]
    proof.require(len(matches) == 1, 'comgas_original_document_not_unique')
    item = matches[0]
    doc, source = item['document'], item['source']
    indexed = _latest_index(doc, indexes)
    body, receipt = _source(cache, source['file'], source['sha256'], url=doc['url'])
    proof.require(len(body) == source['bytes'] and body.startswith(b'%PDF-'),
                  'comgas_original_PDF_required_no_text_fallback')
    pages = [proof.text(p.extract_text()) for p in PdfReader(io.BytesIO(body), strict=True).pages]
    receipt.update(published_at=doc['published_at'], source_locator={'raw_IPE_row': indexed},
        authority='primary_declared_schedule_not_settlement')
    return body, pages, doc, receipt


def _workbook(cache, audit):
    entries = [s for s in audit['new_public_sources'] if s.get('sha256') in (WORKBOOK_SHA, PAGE_SHA)]
    proof.require(len(entries) == 2, 'comgas_actual_RI_workbook_receipts_required')
    sources = []
    for entry in entries:
        relative = ('raw/primary-research/all-pending/cash/response-'
                    + hashlib.sha256(entry['url'].encode()).hexdigest() + '.json')
        transport, _ = cache.read(relative, 'comgas_original_public_HTTP_receipt')
        proof.require(json.loads(transport) == entry, 'comgas_original_HTTP_receipt_changed')
        sources.append({'file': relative, 'sha256': hashlib.sha256(transport).hexdigest(),
            'bytes': len(transport), 'url': None, 'retrieved_at': None,
            'authority': 'actual_acquisition_metadata_not_primary_content'})
        body, receipt = receipt_source(cache.folder, entry, entry['sha256'])
        sources.append({**receipt, 'published_at': None, 'authority': 'primary_issuer_published_bytes'})
        if entry['sha256'] == WORKBOOK_SHA:
            workbook_body, workbook_source = body, receipt
        else:
            from lxml import html
            urls = html.fromstring(body).xpath('//a/@href')
    proof.require(workbook_source['url'] in urls, 'comgas_workbook_not_literally_issuer_linked')
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(io.BytesIO(workbook_body)) as archive:
        strings = [''.join(s.itertext()) for s in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si', ns)]
        rows = {}
        for row in ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).findall('.//m:row', ns):
            cells = {}
            for cell in row.findall('m:c', ns):
                value = cell.find('m:v', ns)
                if value is not None:
                    cells[re.sub(r'\d', '', cell.attrib['r'])] = {
                        'literal': strings[int(value.text)] if cell.attrib.get('t') == 's' else value.text,
                        'formula': cell.find('m:f', ns) is not None, 'cell': cell.attrib['r']}
            rows[int(row.attrib['r'])] = cells
    proof.require(proof.text(rows[7]['H']['literal']) == 'Valor por acao ON (R$)'
        and proof.text(rows[7]['J']['literal']) == 'Valor por acao PNA (R$)'
        and proof.text(rows[7]['D']['literal']) == 'Data da deliberacao'
        and proof.text(rows[7]['E']['literal']) == 'Data do Pagamento', 'comgas_workbook_column_roles_changed')
    return rows, sources


def _form_facts(body, pages, doc, identity, workbook):
    version, protocol, approval, record, payment, count = FORM_SPECS[doc['sequence']]
    parsed = cvm.parse_pdf(body, doc, CUTOFF)
    proof.require(doc['version'] == version and parsed['status'] == 'available'
        and not parsed['issues'] and not parsed['unmapped_installment_rows']
        and len(parsed['events']) == count, 'comgas_complete_latest_native_form_required')
    proof.require(all(e['cvm'] == identity['cvm'] for e in parsed['events']),
                  'comgas_native_form_issuer_changed')
    compact = ''.join(' '.join(pages).split())
    proof.require('ProtocoloProventoVersaoDataEnvio' + protocol + str(version) in compact,
                  'comgas_native_internal_protocol_version_changed')
    proof.require({e['isin'] for e in parsed['events']} == {x['isin'] for x in IDENTITIES.values()}
        and Counter(e['isin'] for e in parsed['events'])
            == Counter({i['isin']: count // 2 for i in IDENTITIES.values()})
        and len({(e['isin'], e['gross_per_unit_decimal']) for e in parsed['events']}) == count
        and all((e['approval_date'], e['record_date'], e['payment_date']) == (approval, record, payment)
                for e in parsed['events']), 'comgas_form_whole_equity_scope_or_dates_changed')
    facts = []
    for event in parsed['events']:
        if event['isin'] != identity['isin']:
            continue
        column = 'H' if identity['share_class'] == 'ON' else 'J'
        amount = Decimal(event['gross_per_unit_decimal'])
        matches = []
        for number, row in workbook.items():
            if number < 8 or not all(c in row for c in ('C', 'D', 'E', column)):
                continue
            if not row['D']['literal'].isdigit() or not row['E']['literal'].isdigit():
                continue
            if ((date(1899, 12, 30) + timedelta(days=int(row['D']['literal']))).isoformat(),
                (date(1899, 12, 30) + timedelta(days=int(row['E']['literal']))).isoformat()) != (approval, payment):
                continue
            proof.require(not any(row[c]['formula'] for c in ('C', 'D', 'E', column)),
                          'comgas_no_recomputed_workbook_amounts')
            if abs(Decimal(row[column]['literal']) - amount) <= Decimal('0.00000000001'):
                matches.append((number, row))
        proof.require(len(matches) == 1, 'comgas_unique_primary_type_crosscheck_required')
        number, row = matches[0]
        label = {'Dividendos': 'DIVIDENDO', 'JCP': 'JCP'}.get(row['C']['literal'])
        proof.require(label is not None, 'comgas_explicit_cash_type_required')
        facts.append({**identity, 'record_date': record, 'approval_date': approval, 'ex_date': None,
            'payment_date': payment, 'label': label, 'gross_per_share_decimal': str(amount),
            'published_at': doc['published_at'],
            'source_locator_id': f"Comgas:CVM{doc['sequence']}:v{version}:row{event['source_row']}",
            'source_locator': {'form_table': event['source_table'], 'form_row': event['source_row'],
                'ISIN_literal': event['isin'], 'primary_type_crosscheck': {
                    'file': WORKBOOK, 'sheet': 'Remuneracao aos Acionistas', 'row': number,
                    'cells': {k: row[k] for k in ('C', 'D', 'E', column)},
                    'amount_authority': 'native_CVM_explicit_gross_not_workbook_tax_assumption'}}})
    proof.require(len({f['label'] for f in facts}) == len(facts),
                  'comgas_duplicate_primary_cash_types_in_selected_form')
    return facts


def _unresolved_conflicts(cache, audit, indexes, workbook):
    """Preserve source disagreements; gate tolerance is not their resolution."""
    _, pages, doc, notice_source = _document(cache, audit, indexes, '550546')
    joined = ' '.join(pages)
    proof.require('posicao acionaria da Companhia verificada em 03 de novembro de 2022' in joined
        and 'pagamento aos acionistas ocorrera em 14 de novembro de 2022' in joined,
        'comgas_original_Nov2022_schedule_conflict_changed')
    digest = 'b03b7438a48e5ee98bca633717ad2c8f365dd16be57ab78a5845ba689894345f'
    relative = f'raw/primary-research/comgas-cash/comgas-2022-dfp-{digest}.pdf'
    body, dfp_source = _source(cache, relative, digest, url='https://www.comgas.com.br/media/3qydzpfs/dfp.pdf')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    p58, p59 = (proof.text(pdf.pages[n].extract_text()) for n in (57, 58))
    proof.require('28.658 acoes preferenciais de classe A' in p58
        and 'pago em 11 de novembro de 2022.' in p59, 'comgas_original_DFP_conflict_or_class_span_changed')
    literal = workbook[25]['J']
    proof.require(literal['literal'] == '0.12811561006' and not literal['formula'],
                  'comgas_unresolved_March2022_workbook_literal_changed')
    raw_rows, cached_sources = [], []
    for page, sha in enumerate((
        '2227b5cb9b37f13ddd93b08175a7a87af1fd1313fddab326fb27cb6623da32d6',
        '58a7b1e3312e19b523e5dfcfe77d29ca34983186dc60f11f43901acbe59385ac'), 1):
        raw, source = _source(cache, f'raw/expanded/CGAS5-b3-cash-{page}.json', sha,
                             authority='existing_exchange_comparator_cache_not_primary')
        raw_rows.extend(json.loads(raw)['results'])
        cached_sources.append(source)
    march = [r for r in raw_rows if r['typeStock'] == 'PNA' and r['lastDatePriorEx'] == '24/03/2022'
             and r['corporateAction'] == 'JRS CAP PROPRIO']
    proof.require(len(march) == 1 and march[0]['valueCash'] == '0,12911561'
        and march[0]['dateApproval'] == '21/03/2022' and march[0]['ratio'] == march[0]['quotedPerShares'] == '1',
        'comgas_original_March2022_comparator_conflict_changed')
    _, june_pages, _, june_source = _document(cache, audit, indexes, '516861')
    june = ' '.join(june_pages)
    proof.require('posicao acionaria da Companhia verificada em 24 de junho de 2022' in june
        and 'a partir de 27 de marco de 2022' in june,
        'comgas_original_June2022_record_ex_conflict_changed')
    return ([
        {'code': 'unresolved_primary_schedule_vs_later_DFP_reported_payment',
         'record_date': '2022-11-03', 'notice_sequence': doc['sequence'],
         'notice_schedule': '2022-11-14', 'DFP_reported_payment': '2022-11-11', 'DFP_page': 59,
         'selection_or_settlement_inferred': False, 'notice_source': notice_source, 'DFP_source': dfp_source},
        {'code': 'unresolved_March2022_PNA_JCP_workbook_vs_cached_comparator',
         'workbook_locator': {'file': WORKBOOK, 'cell': 'J25', 'literal': literal['literal']},
         'workbook_approval_date': '2022-03-21', 'workbook_payment_date': '2022-04-04',
         'cached_comparator_JCP': '0.12911561', 'comparator_record_date': '2022-03-24',
         'no_gross_tax_or_record_date_reconstruction': True,
         'native_original_481170_and_481162_missing_usable_PDF': True,
         'existing_group_tolerance_is_not_primary_conflict_resolution': True},
        {'code': 'unresolved_June2022_native_record_ex_date_condition',
         'notice_sequence': '516861', 'literal_record_date': '2022-06-24',
         'literal_ex_date': '2022-03-27', 'not_called_typo_or_calendar_adjusted': True,
         'notice_not_used_for_new_calendar_replacement': True, 'source': june_source}],
        [notice_source, dfp_source, june_source, *cached_sources])


def load_proof(identity, cutoff, cache):
    identity = _identity(identity, cutoff)
    sources = proof.verified_identity(cache, identity, identity, DETAIL_SHA, CNPJ)
    proof.require(MANIFEST_SHA is not None, 'comgas_manifest_not_pinned')
    manifest = f'raw/primary-research/all-pending/{SCOPE}/round3-comgas-manifest-{MANIFEST_SHA}.json'
    audit_body, audit_source = _source(cache, manifest, MANIFEST_SHA, authority='derived_source_catalogue_not_primary')
    audit = json.loads(audit_body)
    sources.append(audit_source)
    history = []
    for year, digest in FCA_PINS.items():
        file = (proof.FCA_FILE if year == 2026 else
                f'raw/primary-research/units/fca_cia_aberta_{year}-{digest}.zip')
        body, source = _source(cache, file, digest,
            url=f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip')
        history.append(_registry_year(body, year))
        sources.append(source)
    indexes, refs = _indexes(cache, audit)
    sources.extend(refs)
    workbook, refs = _workbook(cache, audit)
    sources.extend(refs)
    conflicts, refs = _unresolved_conflicts(cache, audit, indexes, workbook)
    sources.extend(refs)
    facts, selected = [], []
    for sequence in FORM_SPECS:
        body, pages, doc, source = _document(cache, audit, indexes, sequence)
        facts.extend(_form_facts(body, pages, doc, identity, workbook))
        sources.append(source)
        selected.append(doc)
    _, pages, doc, source = _document(cache, audit, indexes, '568344')
    joined = ' '.join(pages)
    for span in (CNPJ, 'montante total bruto de R$ 15.733.265,62',
        'pagos as acoes ordinarias, no valor de R$ 0,116210115892 por acao',
        'pagos as acoes preferenciais, no valor de R$ 0,127831127481 por acao',
        'posicao acionaria verificada em 26 de dezembro de 2022',
        'a partir de 27 de dezembro de 2022', 'pagamento aos acionistas ocorrera em 05 de janeiro de 2023'):
        proof.require(span in joined, 'comgas_native_Dec2022_whole_declaration_span_changed')
    amount = '0.116210115892' if identity['share_class'] == 'ON' else '0.127831127481'
    facts.append({**identity, 'record_date': '2022-12-26', 'approval_date': '2022-12-21',
        'ex_date': '2022-12-27', 'payment_date': '2023-01-05', 'label': 'JCP',
        'gross_per_share_decimal': amount, 'published_at': doc['published_at'],
        'source_locator_id': 'Comgas:CVM568344:v1:clauses4-5:pages1-2',
        'source_locator': {'pages': [1, 2], 'clause': 5, 'class_basis': 'literal_acoes_ordinarias_or_preferenciais',
            'gross_basis': 'explicit_montante_total_bruto_followed_by_class_per_share_allocations'}})
    sources.append(source)
    selected.append(doc)
    if identity['ticker'] == 'CGAS3':
        from .comgas_cash_research import parse_reviewed_notice
        reviews_body, reviews_source = _source(cache, NOTICE_REVIEWS, NOTICE_REVIEWS_SHA,
            authority='derived_literal_span_locations_not_primary')
        sources.append(reviews_source)
        reviews = json.loads(reviews_body)['reviews']
        for sequence in ON_NOTICE_SEQUENCES:
            _, pages, doc, source = _document(cache, audit, indexes, sequence)
            reviewed = [r for r in reviews if r['sequence'] == sequence]
            proof.require(len(reviewed) == 1, 'comgas_unique_ON_literal_review_required')
            native_facts = parse_reviewed_notice(pages, reviewed[0], doc, source)
            for native_fact in native_facts:
                facts.append({**identity, **{k: native_fact[k] for k in (
                    'record_date', 'payment_date', 'gross_per_share_decimal', 'label', 'published_at')},
                    'approval_date': doc['reference_date'], 'ex_date': None,
                    'source_locator_id': f"Comgas:CVM{sequence}:v{doc['version']}:{native_fact['label']}:ON",
                    'source_locator': {'format': 'original_native_PDF_literal_ON_clauses',
                        'evidence': native_fact['evidence'], 'notice_sequence': sequence}})
            sources.append(source)
            selected.append(doc)
    sources = proof.merge_sources(sources)
    proof.register(cache, sources)
    return {'facts': facts, 'sources': sources, 'selected_filings': selected,
        'unresolved_other_source_conflicts': conflicts,
        'historical_registry': history, 'contract': {
            'model_basis': 'CVM_reported_class_specific_gross_payment_schedule_not_settlement',
            'annual_financial_class_unchanged': identity['share_class'],
            'workbook_used_only_for_literal_type_crosscheck_not_record_dates_or_gross_reconstruction': True,
            '2022_March_missing_native_notice_not_replaced_by_workbook_or_tax_inference': True,
            'warnings': conflicts,
            'source_gate_pass_is_not_admission_or_resolution_of_other_primary_conflicts': True,
            '2022_November_notice_schedule_14_vs_DFP_paid_11_conflict_retained_not_resolved': True,
            '2023_March_CGAS3_two_original_dates_belong_to_one_native_entitlement_group': True,
            'no_zero_claims_no_imputation_no_quarter_annualization': True,
            'forecast_inference_performed': False, 'own_training_rows_created': 0,
            'point_in_time_archive_verified': False, 'settlement_verified': False, 'model_admitted': False}}


def canonical_independent_cash_rows(folder, identity, cutoff, rows):
    """Strengthen the existing CGAS5-only view, retaining all original bytes."""
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    from .price_basis_research import reviewed_inputs
    identity = _identity(identity, cutoff)
    proof.require(identity['ticker'] == 'CGAS5', 'comgas_preferred_view_CGAS5_only')
    evidence = load_proof(identity, cutoff, _Cache(folder, load_inventory(folder)))
    view = reviewed_inputs(folder, identity, cutoff)
    proof.require(rows == view['original_cash_rows'] or rows == view['cash_rows'],
                  'comgas_exact_complete_cached_cash_rows_required')
    original = view['original_cash_rows']
    proof.require({r['typeStock'] for r in original} == {'ON', 'PNA', 'PN'}
        and all(r['lastDatePriorEx'] in ('26/04/2001', '27/04/1998')
                and r['ratio'] == r['quotedPerShares'] == '1000'
                for r in original if r['typeStock'] == 'PN')
        and not any(r.get(k) and r[k] != identity['isin'] for r in original if r['typeStock'] == 'PNA'
                    for k in ('isinCode', 'isin')), 'comgas_ambiguous_preferred_source_or_ISIN')
    for old, new in zip(original, view['cash_rows']):
        proof.require(all(old.get(k) == new.get(k) for k in set(old) | set(new)
            if k not in ('typeStock', 'closingPricePriorExDate')), 'comgas_cash_numeric_or_date_mutation')
    return {'binding_id': BINDING_ID, 'identity': identity, 'cash_rows': deepcopy(view['cash_rows']),
        'original_cash_rows': deepcopy(original), 'cash_files': view['cash_files'],
        'sources': proof.merge_sources(evidence['sources'] + view['receipts']),
        'class_crosswalk': {'source_class': 'PNA', 'registered_class': 'PN',
            'only_preferred_class_in_each_FCA_2021_2026': True,
            'exact_otherCodes_ISIN_and_native_CVM_gross_rows': True,
            'two_original_PN_rows_1998_2001_unchanged_not_a_second_current_class': True,
            'historical_FCA_coverage_starts': '2021-01-01',
            'suffix_inference': False, 'financial_class_changed': False},
        'original_files_modified': False, 'synthetic_exchange_rows_created': False}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    if identity.get('ticker') not in IDENTITIES:
        return parsed
    identity = _identity(identity, cutoff)
    evidence = load_proof(identity, cutoff, cache)
    from .payment_sources import inspect_payment_page
    ticker = identity['ticker']
    body, _ = cache.read(f'raw/expanded/{ticker}-payments.html', 'comgas_immutable_original_calendar')
    proof.require(hashlib.sha256(body).hexdigest() == PAYMENT_SHAS[ticker], 'comgas_original_calendar_changed')
    original = inspect_payment_page(body, ticker, cutoff, datetime(2026, 10, 25, tzinfo=timezone.utc))
    groups = {}
    for fact in evidence['facts']:
        record = fact['record_date']
        groups.setdefault(record, {'facts': [], 'expected_original': []})['facts'].append(fact)
    copied = deepcopy(parsed)
    # This is a whole native group relocation, never an invented zero event.
    if ticker == 'CGAS3':
        record, orphan = '2023-03-24', '2023-03-31'
        proof.require(len([r for r in original['events'] if r.get('record_date') == orphan]) == 1,
                      'comgas_expected_single_misdated_JCP_required')
        for row in copied['events']:
            if row.get('record_date') == orphan:
                row['reported_original_record_date'] = orphan
                row['record_date'] = record
    keys = ('label', 'payment_date', 'gross_per_share', 'source_row')
    for record, spec in groups.items():
        old = [r for r in original['events'] if r.get('record_date') == record
               or (ticker == 'CGAS3' and record == '2023-03-24' and r.get('record_date') == '2023-03-31')]
        spec['expected_original'] = [{k: r.get(k) for k in keys} for r in old]
    result = proof.apply_groups(copied, identity, cutoff, evidence, repair_id=REPAIR_ID,
                                payment_sha=PAYMENT_SHAS[ticker], groups=groups)
    audit = result['exact_cash_repairs'][REPAIR_ID]
    audit['original_calendar_events_before_any_view'] = deepcopy(original['events'])
    audit['source_contract']['unrepaired_original_source_conflicts_remain_for_normal_gates'] = True
    return result


def _cash_diagnostics(stock, cache):
    """Describe remaining disagreements, without changing a gate or input."""
    from . import inventory_candidates as native
    from .cash_calendar_research import apply_research_calendar
    from .share_basis import b3_evidence
    codes = {t: i['isin'] for t, i in IDENTITIES.items()}
    rows = native._cash_pages(stock['ticker'], codes, 15636, cache)
    if stock['ticker'] == 'CGAS5':
        rows = canonical_independent_cash_rows(cache.folder, stock, CUTOFF, rows)['cash_rows']
    original, _ = native._payments(stock['ticker'], cache, CUTOFF, {}, identity=stock)
    body, _ = cache.read(f"raw/expanded/{stock['ticker']}-prices.json", 'comgas_existing_daily_prices')
    _, actions = native.split_price_history(body, stock['ticker'], CUTOFF, allow_recent_actions=True)
    original = apply_research_calendar(stock, cache, CUTOFF, original, rows, actions)
    expected, _ = b3_evidence(rows, stock['share_class'])
    groups = {}
    for event in original.installments:
        groups.setdefault(event['record_date'], []).append(event)
    output = []
    for day, amount in sorted(expected.items()):
        if not date(2021, 9, 30) <= day <= CUTOFF:
            continue
        members = groups.get(day.isoformat(), [])
        total = sum(r['gross_per_unit'] for r in members) if members else None
        if total is None or not math.isclose(total, amount, rel_tol=.002, abs_tol=.00002):
            output.append({'record_date': day.isoformat(), 'calendar_group_sum': total,
                'independent_group_sum': amount, 'original_members': deepcopy(members),
                'diagnostic_only_not_admission_or_threshold_override': True})
    return output


def replay(folder):
    """Real candidate and annual gates, with only the proposed source hooks."""
    from unittest.mock import patch
    from . import inventory_candidates as native
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    inventory = load_inventory(root)
    stocks = [s for s in inventory['stocks'] if s['ticker'] in IDENTITIES]
    inspector, reviewer = native.inspect_payment_page, native._reviewed_cash_view
    candidate_results, candidates = [], {}

    def inspect(body, ticker, cutoff, observed):
        parsed = inspector(body, ticker, cutoff, observed)
        stock = next((s for s in stocks if s['ticker'] == ticker), None)
        return apply_exact_repairs(parsed, stock, cutoff, active_cache) if stock else parsed

    def review(identity, cache, cutoff, rows, checks):
        if identity['ticker'] != 'CGAS5':
            return reviewer(identity, cache, cutoff, rows, checks)
        view = canonical_independent_cash_rows(root, identity, cutoff, rows)
        offset = 0
        for file in view['cash_files']:
            payload = cache.json(file, 'independent_cash')
            count = len(payload['results'])
            cache.parsed[file] = {**payload, 'results': deepcopy(view['cash_rows'][offset:offset + count])}
            offset += count
        proof.register(cache, view['sources'])
        checks['comgas_exact_class_source_view'] = {k: v for k, v in view.items() if k not in ('cash_rows', 'original_cash_rows')}
        return view['cash_rows']

    with patch('socket.socket.connect', side_effect=AssertionError('comgas_replay_must_be_offline')):
        statements, financial_audit, financial_sources = prepare_financials(root, stocks, CUTOFF)
        for stock in stocks:
            checks = {}
            active_cache = native._Cache(root, inventory)
            row = {'identity': IDENTITIES[stock['ticker']], 'one_pass': False, 'financial_current_pass': False,
                   'training_rows': 0, 'forecast_inference': False}
            annual = [r for r in financial_audit if r.get('kind') == 'filing'
                and (r.get('cvm'), r.get('share_class')) == (stock['cvm'], stock['share_class'])]
            row['latest_annual_audit'] = max(annual, key=lambda r: (r['fiscal_end'], int(r['version']))) if annual else None
            with patch.object(native, 'inspect_payment_page', inspect), patch.object(native, '_reviewed_cash_view', review):
                try:
                    candidate = native._one(stock, active_cache, CUTOFF, checks)
                    candidates[stock['ticker']] = candidate
                    row.update(one_pass=True, prices=len(candidate['prices']), payments=len(candidate['payments']),
                        financial_basis_start=candidate['financial_basis_start'], training_since=candidate['training_since'])
                    spec = (stock['cvm'], stock['share_class'], stock['sector_code'])
                    current = financial_features(statements, candidate['payments'], CUTOFF, spec)
                    row['financial_current'] = current
                    row['financial_current_pass'] = bool(current and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
                    training, _ = stock_observations(stock['ticker'], candidate['prices'], candidate['payments'], statements, CUTOFF, spec=spec)
                    since = candidate['training_since']
                    since = since.isoformat() if isinstance(since, date) else since
                    row['training_rows'] = len([o for o in training if o['as_of'] >= since
                        and o['fiscal_end'] >= candidate['financial_basis_start']])
                except ValueError as error:
                    row['reason'] = str(error)
                    if str(error) == 'cash_amount_or_share_scale_disagrees_with_B3':
                        row['remaining_common_window_cash_disagreements'] = _cash_diagnostics(stock, active_cache)
            row['checks'] = checks
            candidate_results.append(row)
    return {'schema': 'round3-comgas-offline-source-proposal-v1', 'reference': CUTOFF.isoformat(),
        'collection_scope': SCOPE, 'network_requests': 0, 'B3_network_requests': 0,
        'api_mziq_global_401_stop_respected': True, 'results': candidate_results,
        'financial_audit': financial_audit, 'financial_sources': financial_sources,
        'no_admission_or_publication': True, 'original_source_files_modified': False,
        'synthetic_B3_payload_written': False, 'core_or_pointers_modified': False}


def write_report(folder, output):
    """Write only an immutable report inside the expressly owned work folder."""
    root = Path(folder).resolve()
    work = root.parents[3] / 'work/recovery_round2_20261025'
    output = Path(output).resolve()
    proof.require(output == work.resolve() and not output.is_symlink(), 'comgas_owned_work_folder_only')
    report = replay(root)
    report['manifest_sha256'] = MANIFEST_SHA
    report['owned_helper_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    cache = _Cache(root, load_inventory(root))
    report['proofs'] = {t: load_proof(i, CUTOFF, cache) for t, i in IDENTITIES.items()}
    body = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2, default=str).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = output / ('round3-comgas-report-' + digest + '.json')
    output.mkdir(parents=True, exist_ok=True)
    if path.exists():
        proof.require(path.read_bytes() == body, 'comgas_immutable_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path, digest, report
