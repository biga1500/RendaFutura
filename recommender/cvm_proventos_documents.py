"""Acquire public CVM proventos PDFs by content signature, never by MIME alone."""

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import uuid
import zipfile

from pypdf import PdfReader
import pdfplumber

from .collect_market_data import ROOT, write_json
from .market_inventory import load_inventory, market_root

MAX_BYTES = 8_000_000


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.hostname != urlsplit(request.full_url).hostname:
            raise ValueError('unexpected_document_redirect')
        return super().redirect_request(request, response, code, message, headers, newurl)


def _open(request, timeout):
    return urllib.request.build_opener(_OfficialRedirect()).open(request, timeout=timeout)


def norm(value):
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.sub(r'[^a-z0-9 ]', ' ', text).split())


def compact(value):
    return ''.join(str(value or '').split())


def day(value):
    text = str(value or '').strip()
    if not re.fullmatch(r'(?:\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?', text):
        return None
    for pattern in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def gross(value):
    text = compact(value).removeprefix('R$')
    if not re.fullmatch(r'\d+(?:\.\d{3})*(?:,\d+)?', text):
        raise ValueError('invalid_gross_per_unit')
    number = Decimal(text.replace('.', '').replace(',', '.'))
    if not number.is_finite() or number < 0:
        raise ValueError('invalid_gross_per_unit')
    return number


def document_id(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.rad.cvm.gov.br' or parsed.path != '/ENET/frmDownloadDocumento.aspx':
        raise ValueError('unsupported_official_document_url')
    query = parse_qs(parsed.query, strict_parsing=True)
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('ambiguous_document_url')
    if set(query) != {'Tela', 'descTipo', 'CodigoInstituicao', 'numProtocolo', 'numSequencia', 'numVersao'}:
        raise ValueError('unsupported_document_parameters')
    if query['Tela'] != ['ext'] or query['descTipo'] != ['IPE'] or query['CodigoInstituicao'] != ['1']:
        raise ValueError('unsupported_document_kind')
    if any(not re.fullmatch(r'\d+', query[key][0]) for key in ('numProtocolo', 'numSequencia', 'numVersao')):
        raise ValueError('invalid_document_identity')
    return query['numSequencia'][0], int(query['numVersao'][0])


def metadata_value(tables, needle):
    for table in tables:
        for index, row in enumerate(table[:-1]):
            for column, cell in enumerate(row):
                if needle in norm(cell) and column < len(table[index + 1]):
                    return table[index + 1][column]
    return None


def installment_rows(tables):
    rows = []
    for table_index, table in enumerate(tables):
        if not table or len(table[0]) != 2 or 'dia de pagamento das parcelas' not in norm(table[0][0]):
            continue
        for row_index, row in enumerate(table[1:], 1):
            if len(row) != 2:
                continue
            text = compact(row[1])
            # The native annex uses decimal dots; ambiguous three-digit groups stay unresolved.
            if re.fullmatch(r'\d+\.\d{4,}', text):
                amount = Decimal(text)
            else:
                try:
                    amount = gross(text) if ',' in text or text.isdigit() else None
                except (ValueError, InvalidOperation):
                    amount = None
            rows.append({'payment_date': day(row[0]),
                'gross_per_unit_decimal': str(amount) if amount is not None else None,
                'raw_payment_date': str(row[0] or ''), 'raw_amount': str(row[1] or ''),
                'source_table': table_index, 'source_row': row_index, 'isin': None,
                'mapping_status': 'unresolved_no_explicit_ISIN_column',
                'cash_model_eligible': False})
    return rows


def parse_tables(tables, document, as_of):
    cutoff = as_of.isoformat()
    published = document['published_at']
    issuer = metadata_value(tables, 'denominacao social')
    identities = {int(match) for table in tables for row in table for cell in row
                  for match in re.findall(r'\b(\d{6})IPE', str(cell or ''))}
    exact_name = bool(issuer and document.get('issuer_name') and norm(issuer) == norm(document['issuer_name']))
    if (identities and identities != {document['cvm']}) or (not identities and not exact_name):
        return {'status': 'invalid', 'events': [], 'issues': ['document_issuer_not_confirmed']}
    record_date = day(metadata_value(tables, 'ultimo dia de negociacao com direitos'))
    approval = day(metadata_value(tables, 'data aprovacao'))
    sent = day(metadata_value(tables, 'data envio'))
    issues, events = [], []
    if sent and sent > published:
        issues.append('document_send_after_index_publication')
    if not record_date:
        issues.append('record_date_not_resolved')
    for table_number, table in enumerate(tables):
        header = next((i for i, row in enumerate(table) if len(row) >= 7 and 'isin' in norm(row[0])
                       and 'valor bruto' in norm(row[1]) and 'data pagamento' in norm(row[6])), None)
        if header is None:
            continue
        for row_number, row in enumerate(table[header + 1:], header + 1):
            if len(row) < 7 or not compact(row[0]):
                continue
            isin = compact(row[0]).upper()
            if not re.fullmatch(r'BR[A-Z0-9]{9}\d', isin):
                issues.append('invalid_isin_row')
                continue
            try:
                amount = gross(row[1])
            except (ValueError, InvalidOperation):
                issues.append('invalid_gross_per_unit')
                continue
            payment = day(row[6])
            form = norm(row[5])
            row_issues = []
            if not payment:
                row_issues.append('payment_date_or_installment_mapping_unresolved')
            if record_date and payment and payment < record_date:
                row_issues.append('payment_before_record_date')
            issues.extend(row_issues)
            events.append({'cvm': document['cvm'], 'issuer': issuer, 'isin': isin,
                'record_date': record_date, 'approval_date': approval, 'published_at': published,
                'scheduled_payment_date': payment, 'payment_date': payment,
                'gross_per_unit': float(amount), 'gross_per_unit_decimal': str(amount),
                'amount_unit': 'BRL/ISIN-unit', 'gross_per_share': None,
                'label': None, 'event_type_status': 'not_specified_in_form', 'payment_form': form,
                'reference_period': str(row[2] or '').replace('\n', ' '), 'fiscal_year': compact(row[3]),
                'monetary_update': norm(row[4]), 'status': 'announced' if payment and payment > cutoff else 'past_scheduled' if payment else 'date_unresolved',
                'available_as_of': published <= cutoff, 'source_url': document['url'],
                'source_table': table_number, 'source_row': row_number,
                'document_sequence': document.get('sequence'), 'document_version': document['version'],
                'issues': row_issues,
                'cash_model_eligible': False, 'financially_confirmed': False})
    if not events:
        issues.append('structured_proventos_table_not_found')
    return {'status': 'available' if events and not issues else 'partial', 'events': events,
            'unmapped_installment_rows': installment_rows(tables),
            'issues': sorted(set(issues)), 'issuer': issuer, 'cvm': document['cvm'],
            'interpretation': 'Primary declaration/payment schedule, not proof of settled payment or complete history.'}


def parse_pdf(body, document, as_of):
    if not isinstance(body, bytes) or len(body) > MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
        raise ValueError('response_is_not_a_pdf')
    reader = PdfReader(io.BytesIO(body), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 12:
        raise ValueError('unsupported_pdf_structure')
    tables = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page in pdf.pages:
            if len(page.chars) > 150_000:
                raise ValueError('pdf_text_size_limit')
            tables.extend(page.extract_tables())
    result = parse_tables(tables, document, as_of)
    result.update(pages=len(reader.pages), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
    return result


def discover(folder, inventory, as_of):
    codes = {row['cvm'] for row in inventory['stocks'] if row.get('cvm')}
    documents = {}
    extra_path = folder / 'raw/primary-research/cvm-documents/index-sources.json'
    extra = json.loads(extra_path.read_text(encoding='utf-8')) if extra_path.is_file() else []
    for source in inventory['sources'] + extra:
        if source.get('provider') != 'CVM IPE' or not source.get('file') or not source.get('sha256'):
            continue
        path = (folder / source['file']).resolve()
        if not path.is_relative_to(folder.resolve()):
            raise ValueError('unsafe_index_path')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != source['sha256']:
            raise ValueError('index_content_hash_mismatch')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = [name for name in archive.namelist() if re.fullmatch(r'ipe_cia_aberta_\d{4}\.csv', name)]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 100_000_000:
                raise ValueError('invalid_index_archive')
            with archive.open(names[0]) as stream:
                for row in csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'):
                    if norm(row['Categoria']) != 'relatorio proventos' or not row['Codigo_CVM'].isdigit():
                        continue
                    cvm, published = int(row['Codigo_CVM']), day(row['Data_Entrega'])
                    if cvm not in codes or not published or published > as_of.isoformat():
                        continue
                    try:
                        sequence, version = document_id(row['Link_Download'])
                    except ValueError:
                        continue
                    documents[row['Link_Download']] = {'cvm': cvm, 'issuer_name': row['Nome_Companhia'], 'url': row['Link_Download'], 'sequence': sequence,
                        'version': version, 'published_at': published, 'reference_date': day(row['Data_Referencia']),
                        'index_sha256': source['sha256'], 'index_url': source['url']}
    return sorted(documents.values(), key=lambda item: (item['published_at'], item['sequence']), reverse=True)


def acquire_indices(folder, as_of, since):
    if not 2003 <= since <= as_of.year <= date.today().year:
        raise ValueError('invalid_index_years')
    folder = market_root(folder)
    cache = folder / 'raw/primary-research/cvm-documents'
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / 'index-sources.json'
    old = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else []
    by_url = {source['url']: source for source in old}
    stopped = False
    for year in range(since, as_of.year + 1):
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip'
        previous = by_url.get(url)
        if previous and previous.get('sha256'):
            path = (folder / previous['file']).resolve()
            if path.is_relative_to(cache.resolve()) and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == previous['sha256']:
                continue
        if stopped:
            break
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/zip'})
            with _open(request, timeout=60) as response:
                body = response.read(12_000_001)
            if len(body) > 12_000_000:
                raise ValueError('index_size_limit')
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                if archive.namelist() != [f'ipe_cia_aberta_{year}.csv']:
                    raise ValueError('index_schema_mismatch')
            digest = hashlib.sha256(body).hexdigest()
            path = cache / f'index-{year}-{digest}.zip'
            if not path.exists():
                with path.open('xb') as stream:
                    stream.write(body)
            by_url[url] = {'url': url, 'provider': 'CVM IPE', 'authority': 'primary', 'year': year,
                'file': path.relative_to(folder).as_posix(), 'sha256': digest, 'bytes': len(body),
                'retrieved_at': datetime.now(timezone.utc).isoformat(), 'status': 'downloaded_valid_zip'}
            print(json.dumps({'component': 'cvm_index', 'year': year, 'bytes': len(body)}), flush=True)
        except Exception as error:
            code = getattr(error, 'code', None)
            stopped = code in (401, 403, 429)
            by_url[url] = {'url': url, 'provider': 'CVM IPE', 'year': year, 'status': 'source_error',
                          'http_status': code, 'error_class': type(error).__name__}
        write_json(manifest, list(by_url.values()))
    return list(by_url.values())


def collect(folder, as_of, *, max_documents=32, issuer_cvms=None, published_since=None, cached_only=False):
    if not 1 <= max_documents <= 1000 or as_of > date.today():
        raise ValueError('invalid_collection_limits')
    folder = market_root(folder)
    inventory = load_inventory(folder)
    documents = discover(folder, inventory, as_of)
    if published_since is not None:
        if published_since > as_of:
            raise ValueError('invalid_publication_window')
        documents = [document for document in documents if document['published_at'] >= published_since.isoformat()]
    if issuer_cvms is not None:
        if not issuer_cvms or any(type(code) is not int or code <= 0 for code in issuer_cvms):
            raise ValueError('invalid_issuer_filter')
        documents = [document for document in documents if document['cvm'] in issuer_cvms]
    cache = folder / 'raw/primary-research/cvm-documents'
    cache.mkdir(parents=True, exist_ok=True)
    if cached_only:
        documents = [document for document in documents
            if any(cache.glob(f"{document['sequence']}-v{document['version']}-*.pdf"))]
    results, requests, stopped = [], 0, False
    focus = {21610, 19348, 1023, 906, 20257, 20036}
    documents.sort(key=lambda item: (item['cvm'] not in focus, -date.fromisoformat(item['published_at']).toordinal(), item['sequence']))
    for document in documents[:max_documents]:
        prefix = f"{document['sequence']}-v{document['version']}-"
        try:
            paths = sorted(cache.glob(prefix + '*.pdf'))
            if paths:
                path = paths[-1]
                if path.is_symlink() or path.resolve().parent != cache.resolve() or path.stat().st_size > MAX_BYTES:
                    raise ValueError('unsafe_or_oversized_cache')
                body = path.read_bytes()
                if path.name != prefix + hashlib.sha256(body).hexdigest() + '.pdf':
                    raise ValueError('cache_hash_mismatch')
                parsed = parse_pdf(body, document, as_of)
                receipt = {'status': 'cached', 'http_status': None}
            else:
                if stopped:
                    results.append({**document, 'status': 'access_stopped', 'events': []})
                    continue
                request = urllib.request.Request(document['url'], headers={'User-Agent': 'RendaFutura-Academic/1.0',
                    'Accept-Encoding': 'identity', 'Accept': 'application/pdf,application/octet-stream;q=0.9,*/*;q=0.5'})
                requests += 1
                with _open(request, timeout=30) as response:
                    if urlsplit(response.url).hostname != 'www.rad.cvm.gov.br':
                        raise ValueError('unexpected_document_redirect')
                    body = response.read(MAX_BYTES + 1)
                    headers = {key: response.headers.get(key) for key in ('Content-Type', 'Content-Disposition', 'Content-Length')}
                    code = response.status
                parsed = parse_pdf(body, document, as_of)
                path = cache / (prefix + hashlib.sha256(body).hexdigest() + '.pdf')
                try:
                    with path.open('xb') as stream:
                        stream.write(body)
                except FileExistsError:
                    if path.read_bytes() != body:
                        raise ValueError('immutable_cache_conflict') from None
                receipt = {'status': 'downloaded_valid_pdf', 'http_status': code, 'headers': headers}
                time.sleep(.4)
            results.append({**document, **parsed, 'acquisition': receipt, 'file': path.relative_to(folder).as_posix(),
                            'retrieved_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        except Exception as error:
            code = getattr(error, 'code', None)
            if code in (401, 403, 429):
                stopped = True
            reason = str(error) if type(error) is ValueError and re.fullmatch(r'[a-z_]+', str(error)) else None
            results.append({**document, 'status': 'source_error', 'http_status': code,
                            'error_class': type(error).__name__, 'error_code': reason, 'events': []})
        if len(results) % 10 == 0:
            print(json.dumps({'component': 'cvm_document_content', 'processed': len(results),
                'valid_documents': sum(result.get('pages', 0) > 0 for result in results),
                'event_rows': sum(len(result['events']) for result in results)}), flush=True)
    report = {'schema_version': 1, 'parser_version': 'cvm_native_pdf_v2',
              'collection_mode': 'verified_cache_only' if cached_only else 'bounded_public_download',
              'as_of': as_of.isoformat(), 'collected_at': datetime.now(timezone.utc).isoformat(),
              'discovered_documents': len(documents), 'attempted_documents': len(results),
              'valid_documents': sum(result.get('pages', 0) > 0 for result in results),
              'structured_documents': sum(bool(result['events']) for result in results),
              'event_rows': sum(len(result['events']) for result in results), 'network_requests': requests,
              'results': results, 'events_are_declarations_not_settled_cash': True,
              'type_not_guessed': True, 'active_model_changed': False}
    report['report_file'] = (cache / ('documents-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        + '-' + uuid.uuid4().hex[:8] + '.json')).relative_to(folder).as_posix()
    write_json(folder / report['report_file'], report)
    write_json(cache / 'report.json', report)
    print(json.dumps({key: report[key] for key in ('discovered_documents', 'attempted_documents', 'valid_documents', 'event_rows')}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    parser.add_argument('--max-documents', type=int, default=32)
    parser.add_argument('--index-since', type=int)
    parser.add_argument('--indexes-only', action='store_true')
    parser.add_argument('--issuer-cvm', type=int, action='append')
    parser.add_argument('--published-since', type=date.fromisoformat)
    parser.add_argument('--cached-only', action='store_true')
    args = parser.parse_args()
    if args.index_since is not None:
        acquire_indices(args.data_dir, args.as_of, args.index_since)
    if not args.indexes_only:
        collect(args.data_dir, args.as_of, max_documents=args.max_documents, issuer_cvms=args.issuer_cvm,
                published_since=args.published_since, cached_only=args.cached_only)
