"""Bounded public BBAS research and offline, fail-closed documentary review.

No shared manifest, snapshot, or active pointer is modified by this module.
"""

import argparse
import base64
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile


RESEARCH_DIR = 'raw/primary-research/bbas-financials'
MAX_REQUESTS = 14
DENIALS = (401, 403, 429)
RESEARCH_FILE = RESEARCH_DIR + '/review-20261025-v1.json'
RESEARCH_SHA256 = '57c220c058cac82366e03f275d7a5cddf06ecfb343c97a438d966833f20a5d00'
IDENTITY = ('BBAS3', 1023, '00.000.000/0001-91', 'ON', 2, 'consolidated',
            '2025-12-31', '1', '154715', '2026-02-11')
IDENTITY_FIELDS = ('ticker', 'cvm', 'cnpj', 'share_class', 'sector_code', 'accounts_scope',
                   'fiscal_end', 'version', 'filing_id', 'published_at')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _append(path, body):
    """Exclusive creation keeps every original response immutable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == body, 'immutable_artifact_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def _json(path, value):
    _append(path, (json.dumps(value, indent=2, ensure_ascii=True) + '\n').encode())


def fetch_public(folder, url, *, discovered_from=None, request_json=None):
    """One request per invocation, no redirects/retries or authentication.

    Windows' native certificate trust is used without bypass flags. Each attempt
    is reserved on disk before transport, including timeouts and robots files.
    Only the official RI, exact native filing, and RI-discovered MZ files qualify.
    """
    folder = Path(folder)
    root = folder / RESEARCH_DIR
    root.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(url)
    _require(parsed.scheme == 'https' and not parsed.username and not parsed.password
             and parsed.port in (None, 443) and not parsed.fragment, 'public_https_url_required')
    host = parsed.hostname
    allowed = host == 'ri.bb.com.br'
    allowed |= (host == 'www.rad.cvm.gov.br' and parsed.path ==
                '/ENETCONSULTA/frmDownloadDocumento.aspx' and parsed.query ==
                'CodigoInstituicao=1&NumeroSequencialDocumento=154715')
    if host in ('api.mziq.com', 'cdn-sites-assets.mziq.com', 'apicatalog.mziq.com',
                'filemanager-cdn.mziq.com'):
        source = root / (discovered_from or '')
        _require(source.is_file() and source.parent == root, 'official_discovery_required')
        receipts = [json.loads(path.read_bytes()) for path in root.glob('response-*.json')]
        _require(any(item.get('file') == source.name and
                     urlsplit(item['url']).hostname in ('ri.bb.com.br', 'cdn-sites-assets.mziq.com',
                                                       'apicatalog.mziq.com') and
                     item.get('status') == 200 and
                     item.get('sha256') == hashlib.sha256(source.read_bytes()).hexdigest()
                     for item in receipts), 'official_discovery_required')
        text = source.read_text(encoding='utf-8', errors='replace')
        catalogue = (host == 'apicatalog.mziq.com' and parsed.path ==
                     '/filemanager/company/5760dff3-15e1-4962-9e81-322a0b3d0bbd/filter/categories/year/meta'
                     and '/filter/categories/year/meta' in text
                     and request_json == {'year': '2025', 'categories': [
                         'downloads-demonstracoes-contabeis-em-ifrs'],
                         'language': 'pt_BR', 'published': True})
        _require(url in text or url.replace('/', '\\/') in text or catalogue,
                 'exact_mz_url_not_discovered')
        allowed = (host == 'cdn-sites-assets.mziq.com' and parsed.path.startswith(
            '/wp-content/themes/mziq_banco_do_brasil_ri/js/') or
            host == 'api.mziq.com' and parsed.path.startswith('/mzfilemanager/') or
            host == 'apicatalog.mziq.com' and parsed.path.startswith('/filemanager/') or
            host == 'filemanager-cdn.mziq.com' and parsed.path.startswith('/published/'))
    _require(allowed, 'unapproved_public_origin_or_path')
    _require(request_json is None or host == 'apicatalog.mziq.com',
             'only_public_catalogue_post_allowed')
    attempts = [json.loads(path.read_bytes()) for path in root.glob('request-*.json')]
    results = [json.loads(path.read_bytes()) for path in root.glob('response-*.json')]
    origin = f'https://{host}'
    _require(not any(item.get('status') in DENIALS and
                     item.get('origin') == origin for item in results), 'origin_stopped_after_denial')
    _require(len(attempts) < MAX_REQUESTS, 'public_request_budget_exhausted')
    number = len(attempts) + 1
    request = {'number': number, 'url': url, 'origin': origin,
               'started_at': datetime.now(timezone.utc).isoformat(),
               'discovered_from': discovered_from, 'request_json': request_json}
    _json(root / f'request-{number:02d}.json', request)
    # Passing the URL through encoded data avoids shell interpolation.
    encoded_url = base64.b64encode(url.encode()).decode()
    script = r'''
$ErrorActionPreference = 'Stop'
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('URL_BASE64'))
$request = [Net.HttpWebRequest]::Create($target)
$request.AllowAutoRedirect = $false
$request.Timeout = 45000
$request.ReadWriteTimeout = 45000
$request.UserAgent = 'RendaFutura-public-financial-research/1.0'
REQUEST_BODY
$response = $null
try { $response = $request.GetResponse() }
catch [Net.WebException] {
    if ($_.Exception.Response) { $response = $_.Exception.Response }
    else { throw }
}
$buffer = New-Object IO.MemoryStream
try {
    $headers = @{}
    foreach ($key in $response.Headers.AllKeys) { $headers[$key] = $response.Headers[$key] }
    $result = @{ status = [int]$response.StatusCode; headers = $headers; body = '' }
    try {
        $response.GetResponseStream().CopyTo($buffer)
        $result.body = [Convert]::ToBase64String($buffer.ToArray())
    } catch { $result.body_error = 'response_body_incomplete' }
    $result | ConvertTo-Json -Compress
} finally {
    if ($response) { $response.Close() }
    $buffer.Dispose()
}
'''.replace('URL_BASE64', encoded_url)
    body_script = ''
    if request_json is not None:
        encoded_body = base64.b64encode(json.dumps(request_json).encode()).decode()
        body_script = f'''
$request.Method = 'POST'
$request.ContentType = 'application/json'
$payload = [Convert]::FromBase64String('{encoded_body}')
$request.ContentLength = $payload.Length
$stream = $request.GetRequestStream()
$stream.Write($payload, 0, $payload.Length)
$stream.Close()
'''
    script = script.replace('REQUEST_BODY', body_script)
    command = base64.b64encode(script.encode('utf-16le')).decode()
    result = dict(request)
    try:
        process = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                  '-EncodedCommand', command], capture_output=True,
                                 timeout=55, check=False)
        _require(process.returncode == 0, 'native_transport_failed')
        response = json.loads(process.stdout.decode('utf-8-sig'))
        body = base64.b64decode(response.pop('body'), validate=True)
        result.update(response)
        if response.get('body_error'):
            raise ValueError(response['body_error'])
        digest = hashlib.sha256(body).hexdigest()
        content_type = next((value for key, value in response['headers'].items()
                             if key.lower() == 'content-type'), '')
        suffix = '.pdf' if body.startswith(b'%PDF-') else '.zip' if body.startswith(b'PK') else '.html'
        name = 'original-' + digest + suffix
        _append(root / name, body)
        result.update(response, file=name, sha256=digest, bytes=len(body), content_type=content_type)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result.setdefault('status', None)
        result.update(error_type=type(error).__name__, detail=str(error))
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    _json(root / f'response-{number:02d}.json', result)
    return result


def extract_evidence(folder):
    """Reproduce complete page-text evidence without rewriting either PDF."""
    import fitz

    root = Path(folder) / RESEARCH_DIR
    native_name = 'native-154715-v1-4b496749c486d49c4109ce1c279c7bcba1bf73163db627f3dffc23395698628f.pdf'
    ri_name = 'original-89a146bf4bcbd220b55d084486e802be1e22b66fac50b5a9f9c6e37a0a75bd59.pdf'
    with fitz.open(root / native_name) as native_pdf, fitz.open(root / ri_name) as ri_pdf:
        pairs = [(18, 207), (20, 209), (47, 236), (109, 298), (111, 300)]
        matches = []
        for ri_page, native_page in pairs:
            left = ri_pdf[ri_page - 1].get_text()
            right = native_pdf[native_page - 1].get_text()
            _require(right.startswith(left), 'native_ifrs_page_body_mismatch')
            matches.append({'ri_pdf_page': ri_page, 'native_pdf_page': native_page,
                            'native_starts_with_complete_ri_text': True,
                            'ri_page_text_sha256': hashlib.sha256(left.encode()).hexdigest()})
        evidence = {'extraction': 'PyMuPDF ' + fitz.__version__,
                    'native_pdf': native_name, 'ri_pdf': ri_name, 'page_matches': matches,
                    'native_pages': {str(i): native_pdf[i - 1].get_text(sort=True)
                                     for i in (1, 3, 21, 207, 209, 236, 298, 300, 359)},
                    'ri_pages': {str(i): ri_pdf[i - 1].get_text(sort=True)
                                 for i in (18, 20, 47, 109, 111)}}
    _json(root / 'page-evidence-20261025-v1.json', evidence)
    return hashlib.sha256((root / 'page-evidence-20261025-v1.json').read_bytes()).hexdigest()


def read_review(folder):
    body = (Path(folder) / RESEARCH_FILE).read_bytes()
    _require(hashlib.sha256(body).hexdigest() == RESEARCH_SHA256, 'bbas_manifest_hash_mismatch')
    manifest = json.loads(body)
    _require(manifest['schema_version'] == 1 and len(manifest['readings']) == 1,
             'bbas_manifest_schema_mismatch')
    return manifest


def _local_proof(folder, proof):
    _require(Path(proof['file']).name == proof['file'], 'primary_document_must_be_local_basename')
    path = Path(folder) / RESEARCH_DIR / proof['file']
    content = path.read_bytes()
    _require(len(content) == proof['bytes'] and hashlib.sha256(content).hexdigest() == proof['sha256'],
             'bbas_primary_document_hash_mismatch')
    return content


def _source_receipts(folder, documents):
    """Keep original authority, derivation, and the three clocks distinct."""
    root = Path(folder) / RESEARCH_DIR
    receipts = []
    for key, proof in documents.items():
        derived = proof['document_type'] == 'derived_complete_page_text'
        path = root / proof['file']
        receipt = {
            'id': 'primary-financials:' + proof['sha256'], 'url': proof['url'],
            'file': RESEARCH_DIR + '/' + proof['file'], 'sha256': proof['sha256'],
            'bytes': proof['bytes'], 'provider': proof['issuer'],
            'authority': 'derived_from_verified_primary_originals' if derived else 'primary',
            'document_type': proof['document_type'], 'use': 'account_interpretation',
            'retrieved_at': None, 'retrieval_started_at': None,
            'retrieval_clock': None, 'http_date': None, 'http_last_modified': None,
            'local_file_mtime': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                                if path.is_file() else None,
            'observed_at': None, 'observed_at_basis': None,
            'clock_use': 'provenance_only_not_publication_or_point_in_time_eligibility',
        }
        if derived:
            receipt['derived_from_source_refs'] = [
                'primary-financials:' + documents[name]['sha256'] for name in ('native_pdf', 'ri_ifrs')]
            receipt['url_role'] = 'underlying_primary_original'
            receipt['observed_at'] = receipt['local_file_mtime']
            receipt['observed_at_basis'] = 'local_file_mtime_not_extraction_or_publication_time'
        else:
            transport = documents['native_download'] if key == 'native_pdf' else proof
            name = transport.get('receipt')
            if name and Path(name).name == name:
                try:
                    response = json.loads((root / name).read_bytes())
                    _require(response['status'] == 200 and response['sha256'] == transport['sha256']
                             and response['bytes'] == transport['bytes']
                             and response['url'] == transport['url'], 'transport_receipt_mismatch')
                    headers = {name.lower(): value for name, value in response['headers'].items()}
                    receipt.update(retrieved_at=response['completed_at'],
                                   retrieval_started_at=response['started_at'],
                                   retrieval_clock='client_wall_clock_from_transport_receipt',
                                   http_date=headers.get('date'),
                                   http_last_modified=headers.get('last-modified'),
                                   observed_at=response['completed_at'],
                                   observed_at_basis='collector_client_wall_clock_completion',
                                   transport_receipt_file=RESEARCH_DIR + '/' + name)
                except (OSError, ValueError, KeyError, TypeError):
                    receipt['retrieval_metadata_status'] = 'unavailable'
            if key == 'native_pdf':
                receipt['original_container_ref'] = 'primary-financials:' + transport['sha256']
        receipts.append(receipt)
    return receipts


def _validated_reading(folder, manifest, cutoff):
    from . import inventory_financials as native

    review = manifest['readings'][0]
    _require(tuple(review[field] for field in IDENTITY_FIELDS) == IDENTITY, 'unreviewed_bbas_identity')
    _require(review['standard'] == 'IFRS' and review['eps_scope'] == 'consolidated'
             and review['period_type'] == 'annual' and review['full_dre'] is True,
             'exact_annual_consolidated_ifrs_required')
    _require(review.get('profit_account', '3.11.01') == '3.11.01'
             and not any(review.get(key) for key in (
                 'correct_parent_profit_sign', 'profit_allocations_brl', 'discard_basic_eps_accounts',
                 'individual_scope_archive_sha256')), 'bbas_parent_profit_override_forbidden')
    entries = review['basic_eps']
    _require(len(entries) == 1 and entries[0]['share_class'] == 'ON'
             and entries[0]['account'] == 'note:30.f:basic_ON'
             and entries[0]['eps_kind'] == 'basic'
             and entries[0]['denominator_class'] == 'ON'
             and entries[0]['denominator_kind'] == 'weighted_average_outstanding'
             and entries[0]['numerator_scope'] == 'consolidated_parent',
             'exact_basic_on_note_required')
    _require(entries[0]['value'] == '2.40' and entries[0]['weighted_shares'] == '5708696148'
             and entries[0]['numerator_brl'] == review['parent_profit_brl'] == '13698124000'
             and entries[0]['numerator_rounding_brl'] == '500'
             and entries[0]['shares_rounding'] == '0', 'unreviewed_bbas_note_values')
    _require(native._day(review['published_at']) <= cutoff, 'filing_not_yet_published')
    documents = manifest['documents']
    bodies = {key: _local_proof(folder, proof) for key, proof in documents.items()}
    _require(bodies['native_pdf'].startswith(b'%PDF-') and bodies['ri_ifrs'].startswith(b'%PDF-'),
             'bbas_primary_pdf_required')
    with zipfile.ZipFile(io.BytesIO(bodies['native_download'])) as archive:
        _require(archive.testzip() is None, 'native_download_crc_failed')
        _require(archive.read(documents['native_pdf']['original_zip_member']) == bodies['native_pdf'],
                 'native_pdf_not_original_member')
        xml = ET.fromstring(archive.read('001023DFP31-12-2025v1.xml'))
        _require(tuple(xml.findtext(field) for field in (
            'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
            'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) ==
            ('001023', '00000000000191', '1', '31/12/2025'), 'native_xml_identity_mismatch')
    catalogue = json.loads(bodies['ri_catalogue'])
    published = [item for item in catalogue['data']['document_metas']
                 if item['file_url'] == documents['ri_ifrs']['url']]
    _require(catalogue['success'] is True and len(published) == 1
             and published[0]['company_id'] == '5760dff3-15e1-4962-9e81-322a0b3d0bbd'
             and published[0]['file_published_date'][:10] == review['published_at']
             and published[0]['file_year'] == 2025 and published[0]['file_quarter'] == 4
             and published[0]['is_published'] is True, 'ifrs_publication_proof_mismatch')
    evidence = json.loads(bodies['page_evidence'])
    _require(evidence['native_pdf'] == documents['native_pdf']['file']
             and evidence['ri_pdf'] == documents['ri_ifrs']['file']
             and all(item['native_starts_with_complete_ri_text'] for item in evidence['page_matches']),
             'native_ifrs_page_evidence_mismatch')
    source = manifest['native_archive']
    _require(source['file'] == 'raw/official-financials/dfp_cia_aberta_2025.zip',
             'unreviewed_native_archive_path')
    content = (folder / source['file']).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == source['sha256'], 'bbas_native_archive_hash_mismatch')
    masters = []
    for (form, year), path in native._caches(folder, cutoff):
        if form != 'dfp':
            continue
        with zipfile.ZipFile(path) as archive:
            masters.extend(row for row in native._rows(archive, f'dfp_cia_aberta_{year}.csv')
                           if int(row['CD_CVM']) == 1023 and native._day(row['DT_REFER']) <= cutoff
                           and native._day(row['DT_RECEB']) <= cutoff)
    _require(bool(masters), 'native_bbas_filing_absent')
    latest = max(masters, key=lambda row: (row['DT_REFER'], int(row['VERSAO'])))
    _require(sum(native._key(row) == native._key(latest) for row in masters) == 1,
             'ambiguous_latest_native_filing')
    _require((latest['DT_REFER'], latest['VERSAO'], latest['ID_DOC'], latest['DT_RECEB'][:10],
              latest['CNPJ_CIA']) == tuple(review[field] for field in (
                  'fiscal_end', 'version', 'filing_id', 'published_at', 'cnpj')),
             'review_is_not_latest_exact_native_filing')
    key = review['cvm'], review['fiscal_end'], review['version']
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        rows = [row for row in native._rows(archive, 'dfp_cia_aberta_DRE_con_2025.csv')
                if native._key(row) == key and row['ORDEM_EXERC'] == '\u00daLTIMO']
    _require(native._dre_fingerprint(rows, full=True) == review['dre_sha256'],
             'documented_original_consolidated_dre_mismatch')
    attributed = {code: [native._brl(row) for row in rows if row['CD_CONTA'] == code]
                  for code in ('3.11', '3.11.01', '3.11.02')}
    _require(all(len(values) == 1 for values in attributed.values())
             and attributed['3.11'][0] == attributed['3.11.01'][0] + attributed['3.11.02'][0]
             and attributed['3.11.01'][0] == native._decimal(review['parent_profit_brl']),
             'native_parent_nci_reconciliation_failed')
    native._checked_note_eps(rows, review)
    proof = documents['native_pdf']
    reading = {**deepcopy(review), 'manifest_file': RESEARCH_FILE, 'manifest_sha256': RESEARCH_SHA256,
               'primary_evidence': {'source_ref': 'primary-financials:' + proof['sha256'],
                                    'pdf_page': review['pdf_page'], 'note': review['note']}}
    return content, reading, _source_receipts(folder, documents)


def get_bbas_reading(folder, cutoff='2026-09-30'):
    """Return the pinned exact-filing reading and receipts, or raise on any gap."""
    from . import inventory_financials as native

    folder = Path(folder)
    _, reading, receipts = _validated_reading(folder, read_review(folder), native._day(cutoff))
    return reading, receipts


def prepare_bbas_financials(folder, candidates, cutoff, *, prepared=None):
    """Shared (statements, diagnostics, sources) contract with BBAS-only admission.

    Optional prepared is the caller's existing native result, avoiding another
    full cache scan. Invalid proof preserves its unavailable latest record.
    """
    from . import inventory_financials as native
    from .collect_market_data import cvm_statements, financial_features

    folder, cutoff = Path(folder), native._day(cutoff)
    if not isinstance(candidates, dict):
        candidates = list(candidates)
    statements, diagnostics, sources = deepcopy(prepared) if prepared is not None else \
        native.prepare_financials(folder, candidates, cutoff)
    specs = native._specs(candidates, [])
    if 'BBAS3' not in specs:
        return statements, diagnostics, sources
    try:
        _require(specs['BBAS3'] == (1023, 'ON', 2), 'exact_bbas_candidate_identity_required')
        manifest = read_review(folder)
        content, reading, receipts = _validated_reading(folder, manifest, cutoff)
        key = (1023, reading['fiscal_end'], reading['version'])
        audit = []
        filtered, contexts = native._annual_archive(content, 2025, {'BBAS3': specs['BBAS3']}, cutoff,
                                                    audit, {(*key, reading['filing_id']): reading})
        parsed = cvm_statements(filtered, 2025, specs={'BBAS3': specs['BBAS3']}, diagnostics=audit)
        current = [row for row in parsed if (row['cvm'], row['fiscal_end'], row['version']) == key]
        _require(len(current) == 1 and not contexts[key]['reasons'], 'bbas_parsed_filing_unavailable')
        record, context = current[0], contexts[key]
        _require(record['filing_id'] == reading['filing_id'] and record['share_class'] == 'ON'
                 and context['accounts_scope'] == 'consolidated'
                 and record['profit_per_share'] == 2.4
                 and record['free_cash_per_share'] is None
                 and record['financial_method']['id'] == 'financial_no_industrial_fcf_v1',
                 'bbas_financial_sector_contract_failed')
        method = record['financial_method']
        method.update(accounts_scope='consolidated', scope_selection=context['scope_selection'],
                      source_statements=context['source_statements'],
                      primary_evidence=context['primary_evidence'],
                      documentary_reading=context['documentary_reading'],
                      reported_basic_eps=context['reported_basic_eps'])
        method['basic_eps_account'] = context['account_aliases'][method['basic_eps_account']]
        source = manifest['native_archive']
        record.update(source_ref='cvm:DFP:2025:' + source['sha256'], source_file=source['file'],
                      document_type='DFP', period_type='annual', annualized=False,
                      accounts_scope='consolidated', status='included')
        _require(financial_features([record], [], cutoff, specs['BBAS3']) is not None,
                 'bbas_financial_features_unavailable')
        matching = [i for i, row in enumerate(statements) if
                    (row['cvm'], row['share_class'], row['fiscal_end'], row['version'], row['filing_id']) ==
                    (1023, 'ON', reading['fiscal_end'], reading['version'], reading['filing_id'])]
        _require(len(matching) == 1, 'bbas_latest_record_replacement_not_unique')
        statements[matching[0]] = record
        diagnostics = [row for row in diagnostics
                       if not (row.get('kind') == 'candidate' and row.get('ticker') == 'BBAS3')]
        diagnostics.extend(audit)
        diagnostics.append({'kind': 'candidate', 'ticker': 'BBAS3', 'cvm': 1023, 'share_class': 'ON',
                            'sector_code': 2, 'status': 'included', 'reasons': [],
                            'accounts_scope': 'consolidated', 'filing_id': reading['filing_id'],
                            'fiscal_end': reading['fiscal_end'], 'published_at': reading['published_at']})
        diagnostics.append({'kind': 'bbas_research', 'ticker': 'BBAS3', 'status': 'included',
                            'reasons': [], 'financial_method': deepcopy(method)})
        source_ids = {source['id'] for source in sources}
        sources.extend(receipt for receipt in receipts if receipt['id'] not in source_ids)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, csv.Error,
            zipfile.BadZipFile, ET.ParseError) as error:
        diagnostics.append({'kind': 'bbas_research', 'ticker': 'BBAS3', 'status': 'excluded',
                            'reasons': ['bbas_documentary_reading_unavailable'],
                            'error_type': type(error).__name__,
                            'detail': str(error) if isinstance(error, ValueError) else None})
    return statements, diagnostics, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--fetch')
    parser.add_argument('--discovered-from')
    parser.add_argument('--catalogue-2025', action='store_true')
    parser.add_argument('--extract-evidence', action='store_true')
    args = parser.parse_args()
    if args.extract_evidence:
        print(extract_evidence(args.folder))
    elif args.catalogue_2025:
        print(json.dumps(fetch_public(args.folder,
            'https://apicatalog.mziq.com/filemanager/company/5760dff3-15e1-4962-9e81-322a0b3d0bbd/filter/categories/year/meta',
            discovered_from='original-9ddd9949ec5a47c1bd26fd36831b0e63d30f74122bb912a8abbed4288c90c020.html',
            request_json={'year': '2025', 'categories': ['downloads-demonstracoes-contabeis-em-ifrs'],
                          'language': 'pt_BR', 'published': True}), indent=2))
    elif args.fetch:
        print(json.dumps(fetch_public(args.folder, args.fetch,
                                      discovered_from=args.discovered_from), indent=2))
    else:
        reading, receipts = get_bbas_reading(args.folder)
        print(json.dumps({'reading': reading, 'sources': receipts}, indent=2))


if __name__ == '__main__':
    main()
