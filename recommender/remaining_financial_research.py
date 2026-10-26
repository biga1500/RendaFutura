"""Isolated exact-native SANB/SIMH research; no shared state is written."""

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
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


RESEARCH_DIR = 'raw/primary-research/remaining-financials'
FILINGS = {'SANB3': '154968', 'SIMH3': '155854'}
DENIALS = (401, 403, 429)
RESEARCH_FILE = RESEARCH_DIR + '/review-20261025-v1.json'
RESEARCH_SHA256 = '644a7e48fd992350857cfa6b65d3ec423b2d386e99c1fc64ef669888db954ad1'
IDENTITY_FIELDS = ('cvm', 'cnpj', 'accounts_scope', 'fiscal_end', 'version',
                   'filing_id', 'published_at')
IDENTITY = (20532, '90.400.888/0001-42', 'consolidated', '2025-12-31',
            '2', '154968', '2026-02-27')
SPECS = {'SANB3': (20532, 'ON', 2), 'SANB4': (20532, 'PN', 2)}
EPS_ENTRIES = [
    {'share_class': 'ON', 'value': '1.62922', 'reported_value': '1629.22',
     'numerator_brl': '6197567000', 'weighted_shares': '3804009000'},
    {'share_class': 'PN', 'value': '1.79214', 'reported_value': '1792.14',
     'numerator_brl': '6568468000', 'weighted_shares': '3665150000'},
]


def _expected_eps():
    return [{**entry, 'account': 'note:28.a:basic_' + entry['share_class'],
             'allocation': entry['share_class'], 'numerator_rounding_brl': '500',
             'shares_rounding': '500', 'eps_kind': 'basic',
             'reported_unit': 'BRL_per_1000_shares', 'normalization_divisor': '1000',
             'denominator_class': entry['share_class'],
             'denominator_kind': 'weighted_average_outstanding',
             'numerator_scope': 'consolidated_parent_class_allocation'} for entry in EPS_ENTRIES]


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _append(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == content, 'immutable_artifact_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(content)


def _json(path, value):
    _append(path, (json.dumps(value, ensure_ascii=True, indent=2) + '\n').encode())


def fetch_native(folder, ticker):
    """One attempt per exact filing, at most two total; no redirects or retries."""
    _require(ticker in FILINGS, 'unreviewed_issuer')
    root = Path(folder) / RESEARCH_DIR
    root.mkdir(parents=True, exist_ok=True)
    _require(not (root / f'request-{ticker}.json').exists(), 'exact_filing_already_attempted')
    responses = [json.loads(path.read_bytes()) for path in root.glob('response-*.json')]
    _require(not any(row.get('status') in DENIALS or row.get('access_denied')
                     for row in responses), 'native_origin_stopped_after_denial')
    _require(len(list(root.glob('request-*.json'))) < 2, 'public_request_budget_exhausted')
    url = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
           '?CodigoInstituicao=1&NumeroSequencialDocumento=' + FILINGS[ticker])
    receipt = {'ticker': ticker, 'filing_id': FILINGS[ticker], 'url': url,
               'started_at': datetime.now(timezone.utc).isoformat(),
               'policy': 'exact public CVM filing; no redirects, retries, auth, or exchange requests'}
    _json(root / f'request-{ticker}.json', receipt)
    encoded = base64.b64encode(url.encode()).decode()
    script = r'''
$ErrorActionPreference = 'Stop'
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('URL_BASE64'))
$request = [Net.HttpWebRequest]::Create($target)
$request.AllowAutoRedirect = $false
$request.Timeout = 45000
$request.ReadWriteTimeout = 45000
$request.UserAgent = 'RendaFutura-public-financial-research/1.0'
$response = $null
try { $response = $request.GetResponse() }
catch [Net.WebException] {
    if ($_.Exception.Response) { $response = $_.Exception.Response }
    else { throw }
}
$buffer = New-Object IO.MemoryStream
try {
    $headers = @{}
    foreach ($key in @('Content-Type', 'Content-Disposition', 'Content-Length',
                       'Date', 'Last-Modified', 'ETag', 'Location')) {
        if ($response.Headers[$key]) { $headers[$key] = $response.Headers[$key] }
    }
    $response.GetResponseStream().CopyTo($buffer)
    if ($buffer.Length -gt 67108864) { throw 'public_response_too_large' }
    @{ status = [int]$response.StatusCode; headers = $headers;
       body = [Convert]::ToBase64String($buffer.ToArray()) } | ConvertTo-Json -Compress
} finally {
    if ($response) { $response.Close() }
    $buffer.Dispose()
}
'''.replace('URL_BASE64', encoded)
    command = base64.b64encode(script.encode('utf-16le')).decode()
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-EncodedCommand', command], capture_output=True,
                                timeout=55, check=False)
        _require(result.returncode == 0, 'native_transport_failed')
        response = json.loads(result.stdout.decode('utf-8-sig'))
        content = base64.b64decode(response.pop('body'), validate=True)
        receipt.update(response)
        digest = hashlib.sha256(content).hexdigest()
        suffix = '.zip' if content.startswith(b'PK') else '.pdf' if content.startswith(b'%PDF-') else '.html'
        name = 'original-' + digest + suffix
        _append(root / name, content)
        receipt.update(file=name, sha256=digest, bytes=len(content))
        if suffix == '.html':
            text = content.decode('utf-8', errors='replace').lower()
            receipt['access_denied'] = any(word in text for word in (
                'captcha', 'access denied', 'robots', 'unauthorized', 'login'))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        receipt.update(status=receipt.get('status'), error_type=type(error).__name__, detail=str(error))
    receipt['completed_at'] = datetime.now(timezone.utc).isoformat()
    _json(root / f'response-{ticker}.json', receipt)
    return receipt


def extract_native(folder, ticker):
    """Preserve the exact PDF/XML members, plus reproducible complete page text."""
    import fitz

    root = Path(folder) / RESEARCH_DIR
    receipt = json.loads((root / f'response-{ticker}.json').read_bytes())
    _require(receipt['status'] == 200, 'successful_native_transport_required')
    content = (root / receipt['file']).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == receipt['sha256'], 'native_container_hash_mismatch')
    members = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        _require(archive.testzip() is None, 'native_container_crc_failed')
        for member in archive.namelist():
            if not member.lower().endswith(('.pdf', '.xml')):
                continue
            body = archive.read(member)
            digest = hashlib.sha256(body).hexdigest()
            suffix = Path(member).suffix.lower()
            filename = f'native-{ticker}-{digest}{suffix}'
            _append(root / filename, body)
            members.append({'member': member, 'file': filename, 'sha256': digest, 'bytes': len(body)})
            if suffix == '.pdf':
                with fitz.open(stream=body, filetype='pdf') as pdf:
                    evidence = {'source_sha256': digest, 'extraction': 'PyMuPDF ' + fitz.__version__,
                                'pages': {str(i + 1): page.get_text(sort=True)
                                          for i, page in enumerate(pdf)}}
                _json(root / f'page-text-{ticker}.json', evidence)
    _json(root / f'members-{ticker}.json', members)
    return members


def parse_sanb_basic_note(text):
    """Read the printed class table; division by 1000 is a stated unit conversion."""
    from decimal import Decimal

    text = ''.join(char for char in unicodedata.normalize('NFKD', text.lower())
                   if not unicodedata.combining(char))
    _require(all(phrase in text for phrase in (
        '28.', 'a) lucro por acao basico', 'lucro basico por 1.000 acoes',
        'lucro liquido atribuido - basico', 'media ponderada das acoes em circulacao - basico',
        'valores expressos em milhares', 'demonstracoes financeiras consolidadas',
        '2025', '2024', '2023')) and 'b) lucro por acao diluido' not in text,
        'exact_consolidated_basic_note_headings_required')
    parent = re.findall(r'lucro atribuivel a controladora[ \t]+([\d.,]+)', text)
    _require(len(parent) == 1, 'ambiguous_note_parent_profit')

    def number(value):
        return Decimal(value.replace('.', '').replace(',', '.'))

    entries = []
    for share, label in (('ON', 'ordinarias'), ('PN', 'preferenciais')):
        values = re.findall(r'acoes ' + label + r'[ \t]+([\d.,]+)', text)
        _require(len(values) == 3, 'ambiguous_basic_class_note_rows')
        eps, numerator, shares = map(number, values)
        entries.append({'share_class': share, 'reported_value': str(eps),
                        'value': str(eps / 1000), 'numerator_brl': str(numerator * 1000),
                        'weighted_shares': str(shares * 1000)})
    _require(entries == EPS_ENTRIES and str(number(parent[0]) * 1000) == '12766035000',
             'unreviewed_printed_note_values')
    return entries


def read_review(folder):
    content = (Path(folder) / RESEARCH_FILE).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == RESEARCH_SHA256, 'remaining_manifest_hash_mismatch')
    result = json.loads(content)
    _require(result['schema_version'] == 1 and len(result['readings']) == 1,
             'remaining_manifest_schema_mismatch')
    return result


def _local_proof(folder, proof):
    _require(Path(proof['file']).name == proof['file'], 'local_proof_basename_required')
    content = (Path(folder) / RESEARCH_DIR / proof['file']).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == proof['sha256'] and len(content) == proof['bytes'],
             'remaining_primary_proof_hash_mismatch')
    return content


def _latest_native_guard(folder, cutoff):
    """Inspect every matching cache, including same-year mirrors and old years."""
    from . import inventory_financials as native

    files, eligible = [], []
    for directory in (folder / 'raw/official-financials', folder / 'raw'):
        for path in sorted(directory.glob('*.zip')):
            match = re.fullmatch(r'(?:cvm-dfp-?(\d{4})|dfp_cia_aberta_(\d{4}))\.zip', path.name)
            if not match:
                continue
            year = int(match[1] or match[2])
            if year > cutoff.year:
                continue
            with zipfile.ZipFile(path) as archive:
                rows = [row for row in native._rows(archive, f'dfp_cia_aberta_{year}.csv')
                        if int(row['CD_CVM']) == 20532 and native._day(row['DT_REFER']) <= cutoff
                        and native._day(row['DT_RECEB']) <= cutoff]
            files.append(path.relative_to(folder).as_posix())
            eligible.append(rows)
    masters = [row for rows in eligible for row in rows]
    _require(bool(masters), 'native_filing_absent')
    latest = max(masters, key=lambda row: (row['DT_REFER'], int(row['VERSAO'])))
    key = native._key(latest)
    _require(all(sum(native._key(row) == key for row in rows) <= 1 for rows in eligible),
             'ambiguous_latest_filing')
    fields = ('CNPJ_CIA', 'DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB')
    identities = {tuple(row[field] for field in fields) for row in masters if native._key(row) == key}
    _require(len(identities) == 1, 'conflicting_latest_filing_across_caches')
    return latest, {
        'coverage': 'all_matching_DFP_files_in_raw_and_official-financials',
        'files_scanned': files, 'identical_mirrors_are_not_independent_filings': True,
        'cutoff': cutoff.isoformat(), 'publication_and_fiscal_dates_must_not_exceed_cutoff': True,
    }


def _validated_sanb(folder, manifest, cutoff):
    from . import inventory_financials as native

    review = manifest['readings'][0]
    _require(tuple(review[field] for field in IDENTITY_FIELDS) == IDENTITY,
             'unreviewed_sanb_filing_identity')
    _require(review['standard'] == 'IFRS' and review['eps_scope'] == 'consolidated'
             and review['period_type'] == 'annual' and review['full_dre'] is True
             and review['profit_account'] == '3.11.01'
             and review['parent_profit_brl'] == '12766035000'
             and review['profit_allocations_brl'] == {'ON': '6197567000', 'PN': '6568468000'}
             and review['basic_eps'] == _expected_eps()
             and not any(review.get(key) for key in (
                 'correct_parent_profit_sign', 'discard_basic_eps_accounts',
                 'individual_scope_archive_sha256')), 'unreviewed_sanb_note_contract')
    _require(native._day(review['published_at']) <= cutoff, 'filing_not_yet_published')
    documents = manifest['documents']
    bodies = {key: _local_proof(folder, proof) for key, proof in documents.items()}
    _require(bodies['native_pdf'].startswith(b'%PDF-'), 'native_pdf_required')
    with zipfile.ZipFile(io.BytesIO(bodies['native_download'])) as archive:
        _require(archive.testzip() is None, 'native_container_crc_failed')
        for key in ('native_pdf', 'filing_xml'):
            _require(archive.read(documents[key]['member']) == bodies[key], 'native_member_mismatch')
    xml = ET.fromstring(bodies['filing_xml'])
    _require(tuple(xml.findtext(field) for field in (
        'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
        'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) ==
        ('020532', '90400888000142', '2', '31/12/2025'), 'native_xml_identity_mismatch')
    evidence = json.loads(bodies['page_text'])
    _require(evidence['source_sha256'] == documents['native_pdf']['sha256'],
             'page_text_source_mismatch')
    parse_sanb_basic_note(evidence['pages']['192'])
    transport = json.loads(bodies['transport_receipt'])
    request = json.loads(bodies['transport_request'])
    container = documents['native_download']
    _require(transport['status'] == 200 and transport['url'] == container['url']
             and transport['sha256'] == container['sha256'] and transport['bytes'] == container['bytes']
             and request['url'] == transport['url'] and request['started_at'] == transport['started_at']
             and transport['filing_id'] == request['filing_id'] == review['filing_id'],
             'native_transport_receipt_mismatch')
    source = manifest['native_archive']
    _require(source['file'] == 'raw/official-financials/dfp_cia_aberta_2025.zip',
             'unreviewed_native_archive_path')
    content = (folder / source['file']).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == source['sha256'], 'native_archive_hash_mismatch')
    latest, latest_guard = _latest_native_guard(folder, cutoff)
    _require((latest['DT_REFER'], latest['VERSAO'], latest['ID_DOC'], latest['DT_RECEB'][:10],
              latest['CNPJ_CIA']) == tuple(review[field] for field in (
                  'fiscal_end', 'version', 'filing_id', 'published_at', 'cnpj')),
             'review_is_not_latest_exact_filing')
    key = review['cvm'], review['fiscal_end'], review['version']
    tables = {}
    _require(set(review['table_fingerprints']) == set(native.TABLES), 'whole_scope_proof_required')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for suffix in native.TABLES:
            name = f'dfp_cia_aberta{suffix}_2025.csv'
            rows = [row for row in native._rows(archive, name)
                    if native._key(row) == key and row['ORDEM_EXERC'] == '\u00daLTIMO'] \
                if name in archive.namelist() else []
            _require(native._dre_fingerprint(rows, full=True) == review['table_fingerprints'][suffix],
                     'documented_whole_scope_cells_mismatch')
            tables[suffix] = rows
    rows = tables['_DRE_con']
    _require(native._dre_fingerprint(rows, full=True) == review['dre_sha256'],
             'documented_original_dre_mismatch')
    attributed = {code: [native._brl(row) for row in rows if row['CD_CONTA'] == code]
                  for code in ('3.11', '3.11.01', '3.11.02')}
    _require(all(len(values) == 1 for values in attributed.values())
             and attributed['3.11'][0] == attributed['3.11.01'][0] + attributed['3.11.02'][0]
             and attributed['3.11.01'][0] == 12766035000 and attributed['3.11.02'][0] == 199088000,
             'native_parent_nci_reconciliation_failed')
    for suffix, code, value in (('_BPA_con', '1', 1270029465000),
                                ('_BPP_con', '2', 1270029465000),
                                ('_BPP_con', '2.07', 126553144000)):
        cells = [native._brl(row) for row in tables[suffix] if row['CD_CONTA'] == code]
        _require(cells == [value], 'consolidated_balance_root_mismatch')
    native._checked_note_eps(rows, review)
    proof = documents['native_pdf']
    reading = {**deepcopy(review), 'manifest_file': RESEARCH_FILE, 'manifest_sha256': RESEARCH_SHA256,
               'primary_evidence': {'source_ref': 'primary-financials:' + proof['sha256'],
                                    'pdf_page': 192, 'note': review['note']}}
    # The shared optional profit_account field aliases alternate roots. This
    # filing already has the canonical parent root and needs no alias operation.
    reading['validated_profit_account'] = reading.pop('profit_account')
    reading['latest_native_filing_guard'] = latest_guard
    reading['canonical_proof_scope'] = {
        'accounts_scope': 'consolidated', 'standard': 'IFRS', 'period_type': 'annual',
        'primary_evidence': 'exact_original_PDF_member_of_verified_CVM_download_ZIP',
        'xml_identity_verified': ['CD_CVM', 'CNPJ', 'DFP_version', 'fiscal_end'],
        'reported_basic_classes': ['ON', 'PN'], 'unit_authorization': False,
        'parent_profit_brl': '12766035000', 'nci_profit_brl': '199088000',
        'consolidated_total_profit_brl': '12965123000',
        'whole_consolidated_table_fingerprints_verified': True,
        'fca_exact_ticker_class_identity': 'required_from_caller_inventory_not_certified_by_financial_getter',
        'derived_text_role': 'local_extraction_of_verified_original_not_a_primary_document',
    }
    reading['forecast_use'] = {
        'inference_policy': 'pooled_cross_company_model',
        'own_training_rows': None, 'own_training_rows_status': 'not_measured_by_documentary_getter',
        'zero_own_training_rows_precludes_current_features': False,
        'issuer_specific_forecast_validation': False, 'model_fit_or_prediction_performed': False,
        'qualification': 'Documentary feature proof does not establish forecast accuracy. '
                         'Own training rows and complete-label eligibility must be measured separately.',
    }
    receipts = []
    for name in ('native_download', 'native_pdf', 'filing_xml', 'page_text'):
        proof = documents[name]
        derived = name == 'page_text'
        headers = {key.lower(): value for key, value in transport['headers'].items()}
        receipts.append({
            'id': 'primary-financials:' + proof['sha256'], 'file': RESEARCH_DIR + '/' + proof['file'],
            'url': container['url'], 'sha256': proof['sha256'], 'bytes': proof['bytes'],
            'provider': 'CVM / Banco Santander (Brasil) S.A.',
            'authority': 'derived_from_verified_primary_original' if derived else 'primary',
            'document_type': proof['document_type'], 'use': 'account_interpretation',
            'collected_at': None,
            'retrieved_at': None if derived else transport['completed_at'],
            'retrieval_started_at': None if derived else transport['started_at'],
            'retrieval_clock': None if derived else 'client_wall_clock_from_transport_receipt',
            'http_date': None if derived else headers.get('date'),
            'http_last_modified': None if derived else headers.get('last-modified'),
            'clock_use': 'provenance_only_not_publication_or_point_in_time_eligibility',
            'original_container_ref': 'primary-financials:' + container['sha256'],
            'original_zip_member': proof.get('member'),
            'retrieval_kind': ('local_text_extraction' if derived else 'exact_original_ZIP_member'
                               if name != 'native_download' else 'direct_HTTP_response'),
            'http_headers_scope': None if derived else 'original_native_download',
            'url_role': 'underlying_primary_original' if derived else 'native_download_container',
            'derived_from_source_refs': ['primary-financials:' + documents['native_pdf']['sha256']]
                                        if derived else [],
            'transport_receipt_file': RESEARCH_DIR + '/' + documents['transport_receipt']['file'],
        })
    return content, reading, receipts


def get_sanb_reading(folder, cutoff='2026-09-30'):
    """Exact-filing injection hook for the parent's documentary reading map."""
    from . import inventory_financials as native

    _, reading, receipts = _validated_sanb(Path(folder), read_review(folder), native._day(cutoff))
    return reading, receipts


def prepare_remaining_financials(folder, candidates, cutoff, *, prepared=None):
    """Offline (statements, diagnostics, sources); only SANB3/SANB4 may change."""
    from . import inventory_financials as native
    from .collect_market_data import cvm_statements, financial_features

    folder, cutoff = Path(folder), native._day(cutoff)
    if not isinstance(candidates, dict):
        candidates = list(candidates)
    result = deepcopy(prepared) if prepared is not None else native.prepare_financials(folder, candidates, cutoff)
    statements, diagnostics, sources = result
    specs = native._specs(candidates, [])
    requested = {ticker: spec for ticker, spec in specs.items() if ticker in SPECS}
    if not requested:
        return result
    try:
        _require(all(spec == SPECS[ticker] for ticker, spec in requested.items()),
                 'exact_sanb_candidate_identity_required')
        content, reading, receipts = _validated_sanb(folder, read_review(folder), cutoff)
        key = reading['cvm'], reading['fiscal_end'], reading['version']
        audit = []
        filtered, contexts = native._annual_archive(content, 2025, requested, cutoff, audit,
                                                    {(*key, reading['filing_id']): reading})
        parsed = cvm_statements(filtered, 2025, specs=requested, diagnostics=audit)
        context = contexts[key]
        _require(context['accounts_scope'] == 'consolidated' and not context['reasons']
                 and all('_con_' in name for name in context['source_statements']),
                 'whole_consolidated_scope_required')
        replacements = {}
        for ticker, spec in requested.items():
            current = [row for row in parsed if (row['cvm'], row['share_class'],
                       row['fiscal_end'], row['version'], row['filing_id']) ==
                       (20532, spec[1], '2025-12-31', '2', '154968')]
            _require(len(current) == 1, 'exact_sanb_parsed_filing_required')
            record = current[0]
            expected_eps = next(float(row['value']) for row in EPS_ENTRIES if row['share_class'] == spec[1])
            method = record['financial_method']
            _require(record['profit_per_share'] == expected_eps and record['free_cash_per_share'] is None
                     and method['id'] == 'financial_no_industrial_fcf_v1', 'financial_sector_contract_failed')
            method.update(accounts_scope='consolidated', scope_selection=context['scope_selection'],
                          source_statements=context['source_statements'],
                          primary_evidence=context['primary_evidence'],
                          documentary_reading=context['documentary_reading'],
                          reported_basic_eps=[row for row in context['reported_basic_eps']
                                              if row['share_class'] == spec[1]])
            method['basic_eps_account'] = context['account_aliases'][method['basic_eps_account']]
            source = read_review(folder)['native_archive']
            record.update(source_ref='cvm:DFP:2025:' + source['sha256'], source_file=source['file'],
                          document_type='DFP', period_type='annual', annualized=False,
                          accounts_scope='consolidated', status='included')
            _require(financial_features([record], [], cutoff, spec) is not None,
                     'sanb_financial_features_unavailable')
            matches = [i for i, row in enumerate(statements) if
                       (row['cvm'], row['share_class'], row['fiscal_end'], row['version'], row['filing_id']) ==
                       (20532, spec[1], '2025-12-31', '2', '154968')]
            _require(len(matches) == 1, 'latest_record_replacement_not_unique')
            replacements[ticker] = (matches[0], record)
        for ticker, (index, record) in replacements.items():
            statements[index] = record
            diagnostics[:] = [row for row in diagnostics if not
                              (row.get('kind') == 'candidate' and row.get('ticker') == ticker)]
            diagnostics.append({'kind': 'candidate', 'ticker': ticker, 'cvm': 20532,
                                'share_class': requested[ticker][1], 'sector_code': 2,
                                'status': 'included', 'reasons': [], 'accounts_scope': 'consolidated',
                                'filing_id': '154968', 'fiscal_end': '2025-12-31', 'published_at': '2026-02-27'})
        diagnostics.extend(audit)
        diagnostics.append({'kind': 'remaining_financial_research', 'status': 'included',
                            'tickers': sorted(requested), 'reasons': []})
        existing = {row['id'] for row in sources}
        sources.extend(row for row in receipts if row['id'] not in existing)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, csv.Error,
            zipfile.BadZipFile, ET.ParseError) as error:
        diagnostics.append({'kind': 'remaining_financial_research', 'tickers': sorted(requested),
                            'status': 'excluded', 'reasons': ['remaining_documentary_reading_unavailable'],
                            'error_type': type(error).__name__,
                            'detail': str(error) if isinstance(error, ValueError) else None})
    return statements, diagnostics, sources


def build_review(folder):
    """Explicit research authoring only; never invoked by offline preparation."""
    from . import inventory_financials as native

    folder = Path(folder)
    root = folder / RESEARCH_DIR
    archive_file = 'raw/official-financials/dfp_cia_aberta_2025.zip'
    content = (folder / archive_file).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        fingerprints = {}
        for suffix in native.TABLES:
            name = f'dfp_cia_aberta{suffix}_2025.csv'
            rows = [row for row in native._rows(archive, name)
                    if native._key(row) == (20532, '2025-12-31', '2')
                    and row['ORDEM_EXERC'] == '\u00daLTIMO'] if name in archive.namelist() else []
            fingerprints[suffix] = native._dre_fingerprint(rows, full=True)
    receipt = json.loads((root / 'response-SANB3.json').read_bytes())
    members = json.loads((root / 'members-SANB3.json').read_bytes())
    documents = {}

    def proof(name, filename, document_type, **extra):
        body = (root / filename).read_bytes()
        documents[name] = {'file': filename, 'sha256': hashlib.sha256(body).hexdigest(),
                           'bytes': len(body), 'document_type': document_type,
                           'url': receipt['url'], **extra}

    proof('native_download', receipt['file'], 'native_DFP_download')
    for member in members:
        if member['member'].endswith('.pdf'):
            proof('native_pdf', member['file'], 'audited_native_annual_statements', member=member['member'])
        elif member['member'] == '020532DFP31-12-2025v2.xml':
            proof('filing_xml', member['file'], 'native_filing_identity', member=member['member'])
    proof('page_text', 'page-text-SANB3.json', 'derived_complete_page_text')
    proof('transport_receipt', 'response-SANB3.json', 'public_transport_receipt')
    proof('transport_request', 'request-SANB3.json', 'public_transport_request')
    review = dict(zip(IDENTITY_FIELDS, IDENTITY))
    review.update(standard='IFRS', eps_scope='consolidated', period_type='annual', full_dre=True,
                  document='native_pdf', pdf_page=192, printed_page=72,
                  note='28.a: consolidated IFRS basic ON/PN EPS, explicitly BRL per 1,000 shares; '
                       'profit and weighted share counts in thousands. PDF p192, printed IFRS p72. '
                       'Annual 2025 column; diluted table p193 excluded. Parent/NCI IFRS reconciliation p234.',
                  dre_sha256=fingerprints['_DRE_con'], table_fingerprints=fingerprints,
                  profit_account='3.11.01', parent_profit_brl='12766035000',
                  profit_allocations_brl={'ON': '6197567000', 'PN': '6568468000'},
                  basic_eps=_expected_eps())
    simh_members = json.loads((root / 'members-SIMH3.json').read_bytes())
    simh_pdf = next(item for item in simh_members if item['member'].endswith('.pdf'))
    protected = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                 for name in [f'raw/primary-research/financials/review-20261025-v{i}.json'
                              for i in range(1, 6)]}
    manifest = {
        'schema_version': 1, 'reviewed_at': '2026-10-25', 'cutoff': '2026-09-30',
        'scope': 'Isolated SANB3/SANB4 exact-native reading; SANB11/UNIT not authorized.',
        'policy': 'No shared manifests, parser, inventory, engine, snapshot, or pointer writes. '
                  'Only printed basic EPS is admitted; unit conversion is not profit/shares EPS derivation. '
                  'All core accounts remain consolidated in the latest published annual native filing. '
                  'Financial sector free cash per share remains null.',
        'native_archive': {'file': archive_file, 'sha256': hashlib.sha256(content).hexdigest(),
                           'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip'},
        'documents': documents, 'readings': [review], 'immutable_prior_manifests': protected,
        'staged_context': {'snapshot': 'universe-20261025T094443-a67674b9',
                           'remaining_financial_only': ['BPAC3', 'BSLI3', 'EVEN3', 'PINE4',
                                                       'SANB3', 'SIMH3', 'WHRL4', 'WIZC3'],
                           'new_recoverable_financial_only': ['SANB3']},
        'negative_findings': [{
            'ticker': 'SIMH3', 'filing_id': '155854', 'version': '1', 'published_at': '2026-03-30',
            'status': 'blocked', 'native_pdf': simh_pdf,
            'pdf_pages': [24, 63, 139, 140],
            'new_exact_native_evidence': 'PDF p140 note 32.1 explicitly labels its table Controladora. '
                'Printed numerator 39741 thousand differs from form parent 39471 thousand. '
                'Weighted basic ON 426797758 and printed basic EPS 0.0931 belong to that table. '
                'Note p139 mentions retrospective share consolidation. Native DRE total 215250 thousand '
                'differs from parent 39471 + NCI 173170 = 212641 thousand by 2609, while p63 reports '
                'discontinued loss -2609 thousand. No same-scope parent correction or consolidated '
                'EPS authorization. No placeholder deletion, sign change, individual scope borrowing, '
                'or older filing fallback is authorized.',
        }],
        'integration_hook': {'getter': 'recommender.remaining_financial_research.get_sanb_reading(folder, cutoff)',
                             'map_key': '(20532, "2025-12-31", "2", "154968")',
                             'receipts': 'append returned receipts by unique id',
                             'standalone': 'prepare_remaining_financials(folder, candidates, cutoff, prepared=existing)',
                             'automatic_integration': False},
        'network': {'maximum_native_requests': 2, 'used_native_requests': 2,
                    'statuses': {'SANB3': 200, 'SIMH3': 200},
                    'exchange_requests': 0, 'redirects': 0, 'retries': 0,
                    'off_limits_hosts_contacted': [],
                    'discovery': 'Two public web discovery queries, primary CVM filing IDs already in staged audit.'},
    }
    _json(root / Path(RESEARCH_FILE).name, manifest)
    return hashlib.sha256((folder / RESEARCH_FILE).read_bytes()).hexdigest()


def render_review_pages(folder):
    import fitz

    root = Path(folder) / RESEARCH_DIR
    output = []
    for ticker, pages in (('SANB3', (192, 234)), ('SIMH3', (140,))):
        members = json.loads((root / f'members-{ticker}.json').read_bytes())
        proof = next(item for item in members if item['file'].endswith('.pdf'))
        with fitz.open(root / proof['file']) as pdf:
            for page in pages:
                name = f'review-{ticker}-page-{page}.png'
                body = pdf[page - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes('png')
                _append(root / name, body)
                output.append(name)
    return output


def verify_staged_integration(folder):
    """Read-only native/candidate/training dry run; write a receipt here only."""
    from . import inventory_financials as native
    from .collect_market_data import financial_features, stock_observations
    from .inventory_candidates import build_candidates
    from .market_inventory import _validate as validate_inventory

    folder = Path(folder)
    cutoff = native._day('2026-09-30')
    staged = folder / 'snapshots/universe-20261025T094443-a67674b9'
    protected = [folder / 'current.json', folder / 'inventory/current.json',
                 Path(__file__).with_name('inventory_financials.py'),
                 Path(__file__).with_name('engine.py'),
                 *staged.glob('*'),
                 *(folder / filename for filename in read_review(folder)['immutable_prior_manifests'])]

    def fingerprints():
        return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in protected if path.is_file()}

    before = fingerprints()
    preparation = json.loads((staged / 'stock-universe-preparation.json').read_bytes())
    audit = json.loads((staged / 'financial-account-audit.json').read_bytes())
    staged_sanb = next(row for row in preparation['stocks'] if row['ticker'] == 'SANB3')
    _require(staged_sanb['reason_codes'] == ['financial_features_unavailable'],
             'unexpected_staged_sanb_blockers')
    inventory_matches = []
    for path in (folder / 'inventory/snapshots').glob('*/market.json'):
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() == preparation['inventory_sha256']:
            inventory_matches.append((path, body))
    _require(len(inventory_matches) == 1, 'exact_staged_inventory_not_unique')
    inventory_path, inventory_body = inventory_matches[0]
    inventory = validate_inventory(json.loads(inventory_body))
    subset = {**inventory, 'stocks': [row for row in inventory['stocks'] if row['ticker'] == 'SANB3']}
    candidates, pending, candidate_receipts = build_candidates(folder, subset, cutoff)
    _require(len(candidates) == 1 and not pending, 'cached_sanb_candidate_did_not_reconcile')
    candidate = candidates[0]
    spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
    _require(spec == SPECS['SANB3'], 'candidate_identity_mismatch')
    baseline = native.prepare_financials(folder, {'SANB3': spec}, cutoff)
    _require(financial_features(baseline[0], candidate['payments'], cutoff, spec) is None,
             'expected_unavailable_native_baseline')
    result = prepare_remaining_financials(folder, {'SANB3': spec}, cutoff, prepared=baseline)
    current = financial_features(result[0], candidate['payments'], cutoff, spec)
    _require(current is not None, 'real_candidate_financial_features_unavailable')
    features, record = current
    _require(native._day(record['fiscal_end']) >= native._day(candidate['financial_basis_start']),
             'candidate_financial_basis_gate_failed')
    training, coverage = stock_observations('SANB3', candidate['prices'], candidate['payments'],
                                           result[0], cutoff, spec=spec)
    training = [row for row in training if native._day(row['as_of']) >= candidate['training_since']
                and native._day(row['fiscal_end']) >= native._day(candidate['financial_basis_start'])]
    _require(fingerprints() == before, 'protected_inputs_changed_during_verification')
    report = {
        'schema_version': 1, 'snapshot_context': staged.name, 'reference_date': cutoff.isoformat(),
        'inventory_file': inventory_path.relative_to(folder).as_posix(),
        'inventory_sha256': preparation['inventory_sha256'],
        'mode': 'offline_dry_run_no_snapshot_or_pointer_publication',
        'ticker': 'SANB3', 'native_before': 'financial_features_unavailable',
        'isolated_after': 'available', 'features': features,
        'record': record, 'candidate_financial_basis_start': candidate['financial_basis_start'],
        'training_observations': len(training),
        'training_windows_included_before_basis_filter': coverage['included_windows'],
        'candidate_checks': candidate.get('checks', {}),
        'candidate_source_receipts': len(candidate_receipts),
        'primary_financial_receipts_added': len(result[2]) - len(baseline[2]),
        'staged_candidate_audit': [row for row in audit if row.get('kind') == 'candidate'
                                   and row.get('ticker') == 'SANB3'],
        'protected_inputs_sha256_before_and_after': before,
        'all_protected_inputs_unchanged': True,
        'point_in_time_archive_verified': False,
        'qualification': 'Dry run proves the financial/candidate gates only. '
                         'No new quote, exchange data, payment settlement, or publication is claimed.',
    }
    _json(folder / RESEARCH_DIR / 'integration-verification-20261025-v1.json', report)
    return {key: report[key] for key in ('ticker', 'native_before', 'isolated_after', 'features',
                                        'training_observations', 'primary_financial_receipts_added',
                                        'all_protected_inputs_unchanged')}


def audit_getter_receipts(folder):
    """Append an offline receipt audit; preserve the original manifest/dry run."""
    from .security_registration import parse_fca_registry, _kind

    folder = Path(folder)
    reading, receipts = get_sanb_reading(folder)
    root = folder / RESEARCH_DIR
    previous_body = (root / 'integration-verification-20261025-v1.json').read_bytes()
    previous = json.loads(previous_body)
    inventory_file = previous['inventory_file']
    inventory_body = (folder / inventory_file).read_bytes()
    _require(hashlib.sha256(inventory_body).hexdigest() == previous['inventory_sha256'],
             'staged_inventory_hash_mismatch')
    inventory = json.loads(inventory_body)
    fca_sources = [source for source in inventory['sources'] if source.get('url') ==
                   'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip']
    _require(len(fca_sources) == 1, 'exact_fca_source_required')
    source = fca_sources[0]
    _require(source['file'] == 'raw/inventory/fca_cia_aberta_2026.zip', 'unreviewed_fca_path')
    body = (folder / source['file']).read_bytes()
    _require(hashlib.sha256(body).hexdigest() == source['sha256'], 'fca_source_hash_mismatch')
    masters, securities = parse_fca_registry(body, 2026)
    eligible = [row for row in masters if int(row['CD_CVM']) == 20532
                and row['DT_REFER'] <= inventory['as_of'] and row['DT_RECEB'] <= inventory['as_of']]
    _require(bool(eligible), 'fca_issuer_absent')
    master = max(eligible, key=lambda row: (row['DT_REFER'], int(row['VERSAO']), row['DT_RECEB']))
    _require(master['CNPJ_CIA'] == IDENTITY[1], 'fca_dfp_cnpj_mismatch')
    fca_classes = {}
    for ticker, spec in SPECS.items():
        rows = [row for row in securities if row['ID_Documento'] == master['ID_DOC']
                and row['Codigo_Negociacao'] == ticker]
        _require(bool(rows) and all(_kind(row)[1] == spec[1]
                 and row['CNPJ_Companhia'] == IDENTITY[1] for row in rows), 'fca_exact_class_mismatch')
        fca_classes[ticker] = {'cvm': spec[0], 'share_class': spec[1], 'cnpj': IDENTITY[1],
                               'fca_filing_id': master['ID_DOC'],
                               'fca_reference_date': master['DT_REFER'],
                               'fca_published_at': master['DT_RECEB'],
                               'csv_records': [row['_csv_record'] for row in rows]}
    report = {
        'schema_version': 1, 'mode': 'offline_getter_receipt_audit_no_prepare_or_publication',
        'manifest_file': RESEARCH_FILE, 'manifest_sha256': RESEARCH_SHA256,
        'selected_identity': {field: reading[field] for field in IDENTITY_FIELDS},
        'canonical_proof_scope': reading['canonical_proof_scope'],
        'latest_native_filing_guard': reading['latest_native_filing_guard'],
        'original_zip_pdf_xml_hashes_and_members_verified': True,
        'source_receipts': receipts,
        'source_receipt_counts': {
            'total': len(receipts), 'primary': sum(row['authority'] == 'primary' for row in receipts),
            'derived_text': sum(row['document_type'] == 'derived_complete_page_text' for row in receipts),
            'legacy_report_count_qualification': 'The preserved dry-run field '
                'primary_financial_receipts_added=4 counts all four receipts, including the one derived '
                'text extraction. Per-receipt authority, not that legacy counter name, determines primacy.',
        },
        'fca_exact_code_class_evidence': {
            'file': source['file'], 'sha256': source['sha256'], 'bytes': len(body),
            'url': source['url'], 'authority': 'primary', 'classes': fca_classes,
            'identity_reference_date': inventory['as_of'],
            'scope': 'cached_FCA_identity_not_exchange_trading_or_point_in_time_financial_eligibility',
        },
        'forecast_use_from_getter': reading['forecast_use'],
        'staged_forecast_inference': {
            'inference_policy': 'pooled_cross_company_model',
            'own_training_rows': previous['training_observations'],
            'source_report': RESEARCH_DIR + '/integration-verification-20261025-v1.json',
            'source_report_sha256': hashlib.sha256(previous_body).hexdigest(),
            'current_features_eligible': previous['isolated_after'] == 'available',
            'issuer_specific_forecast_validation': False,
            'model_fit_or_prediction_performed': False,
            'qualification': 'Zero own rows permit current-feature use in the existing pooled model; '
                'they do not establish issuer-specific forecast accuracy or create historical labels. '
                'The getter does not count training rows; zero was measured by the separately preserved dry run.',
        },
        'all_writes_restricted_to_owned_research_folder': True,
    }
    _json(root / 'getter-receipt-audit-20261025-v2.json', report)
    return {'manifest_sha256': RESEARCH_SHA256, 'selected_filing': reading['filing_id'],
            'DFP_cache_files_scanned': len(reading['latest_native_filing_guard']['files_scanned']),
            'FCA_classes': fca_classes, 'receipt_authorities': [row['authority'] for row in receipts],
            'staged_own_training_rows': previous['training_observations']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--fetch', choices=FILINGS)
    parser.add_argument('--extract', choices=FILINGS)
    parser.add_argument('--build-review', action='store_true')
    parser.add_argument('--render-pages', action='store_true')
    parser.add_argument('--validate', action='store_true')
    parser.add_argument('--verify-staged', action='store_true')
    parser.add_argument('--audit-receipts', action='store_true')
    args = parser.parse_args()
    if args.fetch:
        print(json.dumps(fetch_native(args.folder, args.fetch), indent=2))
    elif args.extract:
        print(json.dumps(extract_native(args.folder, args.extract), indent=2))
    elif args.build_review:
        print(build_review(args.folder))
    elif args.render_pages:
        print(json.dumps(render_review_pages(args.folder)))
    elif args.validate:
        reading, receipts = get_sanb_reading(args.folder)
        print(json.dumps({'filing_id': reading['filing_id'], 'basic_eps': reading['basic_eps'],
                          'source_receipts': len(receipts)}, indent=2))
    elif args.verify_staged:
        from unittest.mock import patch

        def offline_only(*args, **kwargs):
            raise ValueError('network_or_subprocess_forbidden_during_verification')

        with patch('urllib.request.urlopen', offline_only), patch('subprocess.run', offline_only):
            print(json.dumps(verify_staged_integration(args.folder), indent=2))
    elif args.audit_receipts:
        from unittest.mock import patch

        def offline_only(*args, **kwargs):
            raise ValueError('network_or_subprocess_forbidden_during_receipt_audit')

        with patch('urllib.request.urlopen', offline_only), patch('subprocess.run', offline_only):
            print(json.dumps(audit_getter_receipts(args.folder), indent=2))


if __name__ == '__main__':
    main()
