"""Isolated CGAS3 cash research. Writes only content-addressed research evidence.

No exchange access, model integration, settlement claims or inferred payment days.
Network access requires explicit sequence selection and has a persistent 16-call cap.
"""

import argparse
import base64
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import zipfile

import pdfplumber
from pypdf import PdfReader

from .alternative_cash_research import Reader, scan_indices
from .cvm_proventos_documents import norm, parse_pdf


CACHE = 'raw/primary-research/comgas-cash'
IDENTITY = {'ticker': 'CGAS3', 'cvm': 15636, 'cnpj': '61.856.571/0001-17',
            'share_class': 'ON', 'isin': 'BRCGASACNOR0'}
START = '2021-09-30'
CUTOFF = '2026-09-30'
MAX_REQUESTS = 16
MAX_BYTES = 8_000_000


def _dump(value):
    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True,
                      allow_nan=False).encode('utf-8')


def _save(root, prefix, body, suffix):
    folder = Path(root).resolve() / CACHE
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError('unsafe_research_path')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (prefix + '-' + hashlib.sha256(body).hexdigest() + suffix)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return path.relative_to(Path(root).resolve()).as_posix()


def documents(reader):
    selected, periods, issues = scan_indices(reader, [IDENTITY])
    for doc in selected:
        row = doc['index_evidence']['raw_row']
        if row['CNPJ_Companhia'] != IDENTITY['cnpj']:
            raise ValueError('index_CNPJ_mismatch')
        if int(row['Codigo_CVM']) != IDENTITY['cvm']:
            raise ValueError('index_CVM_mismatch')
    return selected, periods, issues


def cached_document(reader, doc):
    sequence, version = doc['sequence'], doc['version']
    patterns = (
        ('raw/primary-research/comgas-cash', f'cvm{sequence}v{version}-*.pdf'),
        ('raw/primary-research/comgas-cash', f'cvm{sequence}v{version}-*.response.bin'),
        ('raw/primary-research/cash-calendars', f'cvm{sequence}v{version}-*.pdf'),
        ('raw/primary-research/cash-calendars', f'cvm{sequence}v{version}-*.response.bin'),
        ('raw/primary-research/cvm-documents', f'{sequence}-v{version}-*.pdf'),
    )
    for directory, pattern in patterns:
        paths = sorted((reader.folder / directory).glob(pattern))
        for path in paths:
            match = re.search(r'-([a-f0-9]{64})\.(?:pdf|response\.bin)$', path.name)
            if not match:
                continue
            return reader.read(path.relative_to(reader.folder).as_posix(),
                               'CVM_original_document', match[1], doc['url'])
    return None


def _official_url(doc):
    parts = urlsplit(doc['url'])
    query = parse_qs(parts.query, strict_parsing=True)
    if (parts.scheme != 'https' or parts.hostname != 'www.rad.cvm.gov.br'
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.fragment or parts.path != '/ENET/frmDownloadDocumento.aspx'
            or set(query) != {'Tela', 'descTipo', 'CodigoInstituicao', 'numProtocolo',
                              'numSequencia', 'numVersao'}
            or query['Tela'] != ['ext'] or query['descTipo'] != ['IPE']
            or query['CodigoInstituicao'] != ['1']
            or query['numSequencia'] != [doc['sequence']]
            or query['numVersao'] != [str(doc['version'])]
            or len(query['numProtocolo']) != 1
            or not query['numProtocolo'][0].isdigit()):
        raise ValueError('unapproved_exact_CVM_document')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def acquire(root, sequences):
    reader = Reader(root)
    docs, _, _ = documents(reader)
    if len(set(sequences)) != len(sequences) or len(sequences) > MAX_REQUESTS:
        raise ValueError('invalid_bounded_sequence_selection')
    selected = []
    for sequence in sequences:
        choices = [doc for doc in docs if doc['sequence'] == sequence]
        if len(choices) != 1:
            raise ValueError('exact_index_sequence_required:' + sequence)
        _official_url(choices[0])
        selected.append(choices[0])
    folder = reader.folder / CACHE
    attempts = [json.loads(path.read_text()) for path in folder.glob('request-*.json')]
    results = []
    for doc in selected:
        cached = cached_document(reader, doc)
        if cached:
            results.append({'sequence': doc['sequence'], 'status': 'existing_cache',
                            'source': cached[1]})
            continue
        if any(item.get('http_status') in (401, 403, 429)
               for path in folder.glob('result-*.json')
               for item in [json.loads(path.read_text())]):
            raise ValueError('prior_denial_stops_all_new_requests')
        if len(attempts) >= MAX_REQUESTS:
            raise ValueError('persistent_16_request_budget_exhausted')
        if any(item['url'] == doc['url'] for item in attempts):
            raise ValueError('no_automatic_request_retry')
        attempt = {'url': doc['url'], 'sequence': doc['sequence'],
                   'request_number': len(attempts) + 1,
                   'started_at': datetime.now(timezone.utc).isoformat(),
                   'index_evidence': doc['index_evidence']}
        _save(reader.folder, 'request', _dump(attempt), '.json')
        attempts.append(attempt)
        result = {'sequence': doc['sequence'], 'url': doc['url'],
                  'request_number': attempt['request_number']}
        request = urllib.request.Request(doc['url'], headers={
            'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf',
            'Accept-Encoding': 'identity'})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                result['http_status'] = response.status
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError('oversized_CVM_response')
            suffix = '.pdf' if body.lstrip().startswith(b'%PDF-') else '.response.bin'
            name = _save(reader.folder, f"cvm{doc['sequence']}v{doc['version']}", body, suffix)
            result.update(status='source_acquired' if suffix == '.pdf' else 'not_a_pdf',
                          file=name, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        except urllib.error.HTTPError as error:
            result.update(status='http_error', http_status=error.code)
        except (OSError, ValueError) as error:
            result.update(status='request_unusable', reason=str(error))
        _save(reader.folder, 'result', _dump(result), '.json')
        results.append(result)
        print(json.dumps({k: result[k] for k in ('sequence', 'status', 'http_status')
                          if k in result}), flush=True)
        if result.get('http_status') in (401, 403, 429):
            break
    return results


def acquire_ri(root):
    """One exact issuer PDF located by the bounded search, with no redirects."""
    url = 'https://www.comgas.com.br/media/3qydzpfs/dfp.pdf'
    root = Path(root).resolve()
    folder = root / CACHE
    existing = list(folder.glob('comgas-2022-dfp-*.pdf'))
    if existing:
        if len(existing) != 1:
            raise ValueError('ambiguous_issuer_RI_cache')
        path = existing[0]
        _, source = Reader(root).read(path.relative_to(root).as_posix(), 'issuer_RI_cache',
                                     path.stem.rsplit('-', 1)[-1], url)
        return {'status': 'existing_cache', 'source': source}
    attempts = [json.loads(p.read_text()) for p in folder.glob('request-*.json')]
    if len(attempts) >= MAX_REQUESTS or any(a['url'] == url for a in attempts):
        raise ValueError('persistent_request_budget_or_no_retry')
    if any(json.loads(p.read_text()).get('http_status') in (401, 403, 429)
           for p in folder.glob('result-*.json')):
        raise ValueError('prior_denial_stops_all_new_requests')
    attempt = {'request_number': len(attempts) + 1, 'url': url,
               'sequence': None, 'started_at': datetime.now(timezone.utc).isoformat(),
               'discovery': 'domain_restricted_search_request_15'}
    _save(root, 'request', _dump(attempt), '.json')
    result = {'url': url, 'request_number': attempt['request_number']}
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0',
                                                       'Accept-Encoding': 'identity'})
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            body = response.read(MAX_BYTES + 1)
            result['http_status'] = response.status
        if len(body) > MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
            raise ValueError('RI_response_not_bounded_pdf')
        result.update(status='source_acquired', sha256=hashlib.sha256(body).hexdigest(),
                      file=_save(root, 'comgas-2022-dfp', body, '.pdf'))
    except urllib.error.HTTPError as error:
        result.update(status='http_error', http_status=error.code)
    except (OSError, ValueError) as error:
        result.update(status='request_unusable', reason=str(error))
    _save(root, 'result', _dump(result), '.json')
    return result


def _text(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return ' '.join(value.split())


def _pages(body):
    if len(body) > MAX_BYTES or not body.lstrip().startswith(b'%PDF-'):
        raise ValueError('response_is_not_bounded_pdf')
    pdf = PdfReader(io.BytesIO(body), strict=True)
    if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 100:
        raise ValueError('unsupported_pdf_structure')
    return [page.extract_text() for page in pdf.pages]


def _anchor(pages, quote):
    needle = _text(quote)
    normalized = [_text(page) for page in pages]
    joined = ' '.join(normalized)
    if not needle or joined.count(needle) != 1:
        raise ValueError('literal_review_anchor_missing_or_ambiguous:' + needle)
    start = joined.index(needle)
    end = start + len(needle)
    offset, selected = 0, []
    for number, page in enumerate(normalized, 1):
        if offset < end and offset + len(page) > start:
            selected.append(number)
        offset += len(page) + 1
    return {'normalized_quote': needle, 'pages_1_based': selected}


MONTHS = ('janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho',
          'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro')


def _quote_day(quote, role):
    text = _text(quote)
    if role == 'record' and not re.search(r'(?:posicao|composicao) acionaria', text):
        raise ValueError('explicit_record_date_role_required')
    if role == 'payment' and not re.search(r'pagamento.*(?:ocorrera|realizado|pagos|pago)', text):
        raise ValueError('explicit_payment_date_role_required')
    matches = re.findall(r'\b(\d{1,2}) de (' + '|'.join(MONTHS)
                         + r') (?:de )?(20\s?\d\s?\d)\b', text)
    if len(matches) != 1:
        raise ValueError('one_explicit_date_required')
    day, month, year = matches[0]
    return date(int(year.replace(' ', '')), MONTHS.index(month) + 1, int(day)).isoformat()


def _amount(value):
    if not re.fullmatch(r'\d+,\d{1,15}', value):
        raise ValueError('explicit_positive_decimal_amount_required')
    amount = Decimal(value.replace(',', '.'))
    if amount <= 0:
        raise ValueError('explicit_positive_decimal_amount_required')
    return amount


def parse_reviewed_notice(pages, review, doc, source):
    if (review['sequence'] != doc['sequence'] or review['sha256'] != source['sha256']
            or source['url'] != doc['url']):
        raise ValueError('review_original_identity_or_hash_mismatch')
    joined = ' '.join(_text(page) for page in pages)
    cnpjs = set(re.findall(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b', joined))
    if cnpjs != {IDENTITY['cnpj']} or 'comgas' not in joined:
        raise ValueError('custom_pdf_exact_CNPJ_required')
    row = doc['index_evidence']['raw_row']
    if row['CNPJ_Companhia'] != IDENTITY['cnpj'] or int(row['Codigo_CVM']) != IDENTITY['cvm']:
        raise ValueError('native_index_issuer_mismatch')
    record_proof = _anchor(pages, review['record_quote'])
    payment_proof = _anchor(pages, review['payment_quote'])
    record = _quote_day(review['record_quote'], 'record')
    payment = _quote_day(review['payment_quote'], 'payment')
    if payment < record or doc['published_at'] > CUTOFF:
        raise ValueError('invalid_review_date_order')
    events = []
    for item in review['events']:
        quote = _text(item['amount_quote'])
        amount = _amount(item['gross_ptbr'])
        if ('acoes ordinarias' not in quote or 'preferenc' in quote
                or not re.search(r'r\$\s*' + re.escape(item['gross_ptbr']) + r' por acao\b', quote)):
            raise ValueError('explicit_ON_gross_per_share_required')
        kind = _text(item['kind_quote']).replace('p roprio', 'proprio')
        if not ((item['label'] == 'JCP' and 'juros sobre capital proprio' in kind)
                or (item['label'] == 'DIVIDENDO' and 'dividendos' in kind)):
            raise ValueError('explicit_distribution_type_required')
        events.append({**IDENTITY, 'record_date': record, 'payment_date': payment,
            'scheduled_payment_date': payment, 'gross_per_share_decimal': str(amount),
            'amount_unit': 'BRL/share', 'amount_basis': 'gross', 'label': item['label'],
            'published_at': doc['published_at'], 'source': source,
            'index_evidence': doc['index_evidence'],
            'document_sequence': doc['sequence'], 'document_version': doc['version'],
            'class_basis': 'literal_acoes_ordinarias_in_per_share_clause',
            'evidence': {'amount_and_class': _anchor(pages, item['amount_quote']),
                         'record': record_proof, 'payment': payment_proof,
                         'kind': _anchor(pages, item['kind_quote'])},
            'notes': review.get('notes', []), 'issues': [],
            'settlement_verified': False, 'point_in_time_archive_verified': False,
            'cash_model_eligible': False, 'financially_confirmed': False})
    return events


def identity_evidence(reader):
    choices = list((reader.folder / 'raw/primary-research/units').glob('fca_cia_aberta_2026-*.zip'))
    if len(choices) != 1:
        raise ValueError('exact_native_FCA_required')
    path = choices[0]
    digest = path.stem.rsplit('-', 1)[-1]
    body, source = reader.read(path.relative_to(reader.folder).as_posix(), 'native_FCA_identity',
                               digest, 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip')
    evidence = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        for member in ('fca_cia_aberta_geral_2026.csv', 'fca_cia_aberta_valor_mobiliario_2026.csv'):
            with archive.open(member) as stream:
                for line, row in enumerate(csv.DictReader(io.TextIOWrapper(
                        stream, encoding='latin1'), delimiter=';'), 2):
                    if row.get('CNPJ_Companhia') != IDENTITY['cnpj']:
                        continue
                    if ('Codigo_CVM' in row and int(row['Codigo_CVM']) == IDENTITY['cvm']) or (
                            row.get('Codigo_Negociacao') == 'CGAS3'
                            and norm(row.get('Valor_Mobiliario')) == 'acoes ordinarias'):
                        evidence.append({'source': source, 'member': member,
                                         'csv_line': line, 'raw_row': row})
    if len(evidence) != 2 or len({item['raw_row']['ID_Documento'] for item in evidence}) != 1:
        raise ValueError('FCA_CNPJ_CVM_ON_link_unconfirmed')
    return evidence


def cached_entitlements(reader):
    audit, _ = reader.json('expanded-source-audit.json', 'original_exchange_source_receipts')

    def original(name):
        matches = [s for s in audit['sources'] if s.get('file') == name]
        identities = {(s['sha256'], s['url']) for s in matches}
        if len(identities) != 1:
            raise ValueError('exact_original_exchange_cache_receipt_required')
        digest, url = identities.pop()
        body, receipt = reader.read('raw/' + name, 'existing_exchange_cache_only', digest, url)
        return json.loads(body), receipt

    detail, detail_source = original('expanded/b3-detail-15636.json')
    if (detail['cnpj'] != '61856571000117' or int(detail['codeCVM']) != IDENTITY['cvm']
            or {'code': 'CGAS3', 'isin': IDENTITY['isin']} not in detail['otherCodes']):
        raise ValueError('cached_exchange_exact_security_identity_required')
    rows, seen = [], set()
    for number in (1, 2):
        payload, source = original(f'expanded/CGAS3-b3-cash-{number}.json')
        parameters = json.loads(base64.b64decode(urlsplit(source['url']).path.rsplit('/', 1)[-1]))
        if parameters.get('tradingName') != detail['tradingName'] or parameters.get('pageNumber') != number:
            raise ValueError('cached_exchange_issuer_or_page_mismatch')
        for index, row in enumerate(payload['results']):
            if row.get('typeStock') != 'ON':
                continue
            record = datetime.strptime(row['lastDatePriorEx'], '%d/%m/%Y').date().isoformat()
            if not START <= record <= CUTOFF:
                continue
            if row['ratio'] != '1' or row['quotedPerShares'] != '1':
                raise ValueError('unresolved_exchange_quote_basis')
            amount = str(_amount(row['valueCash']))
            label = {'DIVIDENDO': 'DIVIDENDO', 'JRS CAP PROPRIO': 'JCP'}.get(row['corporateAction'])
            if not label:
                raise ValueError('unresolved_cash_kind')
            key = (record, label, amount)
            if key in seen:
                continue
            seen.add(key)
            rows.append({**IDENTITY, 'record_date': record, 'label': label,
                'approval_date': datetime.strptime(row['dateApproval'], '%d/%m/%Y').date().isoformat(),
                'gross_per_share_decimal': amount, 'source': source,
                'source_row_0_based': index, 'raw_row': row, 'identity_source': detail_source,
                'record_basis': 'lastDatePriorEx_last_session_WITH_rights_not_ex_date',
                'payment_date': None})
    return sorted(rows, key=lambda row: (row['record_date'], row['label']))


def parse_native(body, doc, source, entitlements):
    parsed = parse_pdf(body, doc, date.fromisoformat(CUTOFF))
    output = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        positions = [(page_number, table) for page_number, page in enumerate(pdf.pages, 1)
                     for table in page.extract_tables()]
    for row in parsed.get('events', []):
        if row['isin'] != IDENTITY['isin']:
            continue
        if not row['record_date'] or not row['payment_date'] or row['issues']:
            continue
        matches = [e for e in entitlements if e['record_date'] == row['record_date']
                   and e['approval_date'] == row['approval_date']
                   and Decimal(e['gross_per_share_decimal']) == Decimal(row['gross_per_unit_decimal'])]
        labels = {e['label'] for e in matches}
        table_number = row['source_table']
        if len(labels) != 1 or table_number >= len(positions):
            continue
        page, table = positions[table_number]
        output.append({**IDENTITY, 'record_date': row['record_date'], 'payment_date': row['payment_date'],
            'scheduled_payment_date': row['payment_date'],
            'gross_per_share_decimal': row['gross_per_unit_decimal'], 'amount_basis': 'gross',
            'amount_unit': 'BRL/share', 'label': labels.pop(),
            'published_at': doc['published_at'], 'approval_date': row['approval_date'],
            'document_sequence': doc['sequence'], 'document_version': doc['version'],
            'source': source, 'index_evidence': doc['index_evidence'],
            'class_basis': 'exact_ISIN_BRCGASACNOR0_only',
            'evidence': {'page_1_based': page, 'table_0_based': table_number,
                'row_0_based': row['source_row'], 'raw_row': table[row['source_row']],
                'record_metadata_tables': [{'page_1_based': p, 'table_0_based': i, 'cells': t}
                    for i, (p, t) in enumerate(positions)
                    if any('ultimo dia de negociacao com direitos' in norm(cell)
                           for cells in t for cell in cells)],
                'type_basis': 'exact_cached_ON_record_approval_amount_match',
                'type_source': matches[0]}, 'issues': [],
            'settlement_verified': False, 'point_in_time_archive_verified': False,
            'cash_model_eligible': False, 'financially_confirmed': False})
    return output, parsed.get('issues', [])


def dfp_evidence(reader):
    path = reader.folder / CACHE / 'comgas-2022-dfp-b03b7438a48e5ee98bca633717ad2c8f365dd16be57ab78a5845ba689894345f.pdf'
    body, source = reader.read(path.relative_to(reader.folder).as_posix(), 'issuer_DFP_payment_evidence',
        path.stem.rsplit('-', 1)[-1], 'https://www.comgas.com.br/media/3qydzpfs/dfp.pdf')
    pages = _pages(body)
    quotes = {
        'march_jcp': ('2022-03-21', 'JCP', 58, 'Em 21 de marco 2022 , o Conselho de Administracao aprovou o pagamento de juros sobre capital proprio, referente ao periodo compreendido em 1o de janeiro de 2022 e 21 de marco de 2022, no valor de R$ 15.891, antes dos tributos, pagos em 04 de abril de 2022.'),
        'march_dividend': ('2022-03-21', 'DIVIDENDO', 59, 'Em 21 de marco 2022, o Conselho de Administracao aprovou o pagamento de dividendos no montante de R$ 684.109, sendo R$ 580.830 com base no saldo da reserva de lucros gerados no exercicio anterior e R$ 103.279 com base no resultado do periodo apurado, pago no dia 04 de abril de 2022.'),
        'november_dividend': ('2022-10-28', 'DIVIDENDO', 59, 'Em 28 de outubro de 2022, foi deliberado e aprovado o pagamento de dividendos intercalares no valor de R$ 1.150.000 com base nas demonstracoes financeiras intermediarias da Companhia de 30 de setembro de 2022, pago em 11 de novembro de 2022.')}
    observations = []
    for key, (approval, kind, page, quote) in quotes.items():
        # DFP facts cover all shareholders; they never supply an ON per-share amount.
        anchor = _anchor([pages[page - 1]], quote)
        anchor['pages_1_based'] = [page]
        payment = re.search(r'\b(?:pagos em|pago no dia|pago em) (\d{1,2} de .+?)\.', _text(quote))
        if payment is None:
            raise ValueError('DFP_explicit_payment_date_required')
        observations.append({'id': key, 'approval_date': approval, 'label': kind,
            'reported_payment_date': _quote_day('pagamento pago em ' + payment[1], 'payment'),
            'scope': 'issuer_aggregate_not_class_per_share', 'evidence': anchor,
            'source': source, 'cash_model_eligible': False, 'settlement_verified': False})
    return observations


def build_report(root, audit_path=None):
    reader = Reader(root)
    if audit_path is not None:
        audit_path = Path(audit_path)
        if audit_path.is_symlink() or audit_path.stat().st_size > 2_000_000:
            raise ValueError('unsafe_mismatch_audit')
        _save(reader.folder, 'input-audit', audit_path.read_bytes(), '.json')
    docs, periods, index_issues = documents(reader)
    identity = identity_evidence(reader)
    entitlements = cached_entitlements(reader)
    manifest, review_source = reader.json(CACHE + '/reviewed-notices.json', 'literal_review_manifest')
    reviews = {row['sequence']: row for row in manifest['reviews']}
    events, document_audit = [], []
    latest = {}
    for doc in docs:
        if doc['category'] == 'relatorio proventos':
            key = doc['reference_date']
            latest[key] = max(latest.get(key, 0), doc['version'])
    for doc in docs:
        cached = cached_document(reader, doc)
        audit = {'document': doc, 'status': 'not_acquired', 'event_count': 0}
        if not cached:
            document_audit.append(audit)
            continue
        body, source = cached
        audit['source'] = source
        if doc['category'] == 'relatorio proventos' and doc['version'] < latest[doc['reference_date']]:
            audit['status'] = 'superseded_native_version'
            document_audit.append(audit)
            continue
        try:
            pages = _pages(body)
            if doc['sequence'] in reviews:
                found = parse_reviewed_notice(pages, reviews[doc['sequence']], doc, source)
                audit['status'] = 'reviewed_explicit_ON_schedule'
            elif doc['category'] == 'relatorio proventos':
                found, issues = parse_native(body, doc, source, entitlements)
                audit.update(status='native_exact_ISIN_schedule' if found else 'no_ON_event', issues=issues)
                if doc['sequence'] == '566184':
                    audit['reason'] = 'debt_ISINs_BRCGASDBS051_BRCGASDBS069_BRCGASDBS077_not_ON'
                if doc['sequence'] == '568565':
                    audit['reason'] = 'index_only_single_page_no_cash_payload_or_issuer_identity'
            else:
                found = []
                audit['status'] = 'original_preserved_not_used_duplicate_or_unreviewed'
            found = [event for event in found if START <= event['record_date'] <= CUTOFF]
            audit['event_count'] = len(found)
            events.extend(found)
        except ValueError as error:
            audit.update(status='unusable', reason=str(error))
        document_audit.append(audit)
    dfp = dfp_evidence(reader)
    conflicts = []
    for event in events:
        if event['record_date'] == '2022-11-03':
            other = next(row for row in dfp if row['id'] == 'november_dividend')
            if event['payment_date'] != other['reported_payment_date']:
                event['issues'].append('primary_payment_date_conflict')
                event['payment_date'] = None
                conflicts.append({'record_date': event['record_date'], 'label': event['label'],
                    'notice_scheduled_payment_date': event['scheduled_payment_date'],
                    'DFP_reported_payment_date': other['reported_payment_date'], 'DFP_evidence': other,
                    'resolution': 'unresolved_no_date_selected'})
    cross_source = []
    missing = []
    for expected in entitlements:
        matches = [event for event in events if event['record_date'] == expected['record_date']
                   and event['label'] == expected['label']
                   and abs(Decimal(event['gross_per_share_decimal'])
                           - Decimal(expected['gross_per_share_decimal'])) <= Decimal('0.00000000001')]
        if not matches:
            evidence = [item for item in dfp if item['approval_date'] == expected['approval_date']
                        and item['label'] == expected['label']]
            candidate = {**expected, 'payment_evidence': evidence,
                'payment_date': evidence[0]['reported_payment_date'] if len(evidence) == 1 else None,
                'status': 'cross_source_candidate_requires_review', 'cash_model_eligible': False,
                'settlement_verified': False, 'primary_single_document_schedule_verified': False}
            cross_source.append(candidate)
            missing.append({'record_date': expected['record_date'], 'label': expected['label'],
                'gross_per_share_decimal': expected['gross_per_share_decimal'],
                'payment_date_candidate': candidate['payment_date'],
                'missing': 'issuer_class_explicit_notice_or_native_ISIN_schedule',
                'attempted_sequences': ['481170', '481162']})
    secondary, secondary_source = reader.json('raw/inventory/payments/CGAS3.json', 'secondary_calendar_diagnosis_only')
    secondary_rows = secondary.get('selected_cash_source', {}).get('events', [])
    comparisons = []
    for event in events:
        matches = [row for row in secondary_rows if row.get('record_date') == event['record_date']
                   and row.get('label') == event['label']]
        comparisons.append({'record_date': event['record_date'], 'label': event['label'],
            'primary_ON_gross': event['gross_per_share_decimal'],
            'secondary_rows': matches, 'secondary_source': secondary_source})
    request_rows = [json.loads(path.read_text()) for path in (reader.folder / CACHE).glob('request-*.json')]
    if len(request_rows) > MAX_REQUESTS:
        raise ValueError('recorded_request_budget_exceeded')
    mismatch_evidence = []
    for path in sorted((reader.folder / CACHE).glob('input-audit-*.json')):
        body, source = reader.read(path.relative_to(reader.folder).as_posix(), 'original_mismatch_audit',
                                   path.stem.rsplit('-', 1)[-1])
        rows = json.loads(body)
        targets = [row for row in rows if row.get('ticker') == 'CGAS3' and row.get('cvm') == 15636]
        if len(targets) != 1:
            raise ValueError('exact_CGAS3_mismatch_audit_required')
        for disputed in targets[0]['mismatches']:
            record = disputed['record_date']
            observations = [e for e in events if e['record_date'] == record]
            candidates = [e for e in cross_source if e['record_date'] == record]
            mismatch_evidence.append({'input': disputed, 'audit_source': source,
                'issuer_schedule_events': observations, 'cross_source_candidates': candidates,
                'status': 'primary_schedule_with_payment_conflict' if any(e['issues'] for e in observations)
                    else 'primary_exact_ON_schedule_observed' if observations
                    else 'class_explicit_issuer_schedule_still_missing' if candidates
                    else 'secondary_record_date_not_corroborated_do_not_infer_absence',
                'primary_ON_sum_decimal': str(sum((Decimal(e['gross_per_share_decimal'])
                    for e in observations), Decimal(0))) if observations else None})
    result_rows = []
    for path in sorted((reader.folder / CACHE).glob('result-*.json')):
        result, _ = reader.json(path.relative_to(reader.folder).as_posix(), 'network_acquisition_result')
        result_rows.append(result)
    search_discovery = None
    if (reader.folder / CACHE / 'search-discovery.json').exists():
        search_discovery, _ = reader.json(CACHE + '/search-discovery.json', 'RI_URL_discovery_only')
    report = {'schema_version': 1, 'identity': IDENTITY,
        'parser_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'window': {'start': START, 'end': CUTOFF,
        'basis': 'record_date_inclusive'}, 'active_model_changed': False,
        'cash_model_eligible': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False, 'calendar_complete': False,
        'identity_evidence': identity, 'IPE_periods': periods, 'index_issues': index_issues,
        'review_manifest_source': review_source, 'events': sorted(events, key=lambda e: (e['record_date'], e['label'])),
        'cross_source_candidates': cross_source, 'exact_missing': missing, 'payment_conflicts': conflicts,
        'cached_ON_entitlements': entitlements, 'documents': document_audit,
        'DFP_observations': dfp, 'secondary_comparison': comparisons,
        'original_mismatch_review': mismatch_evidence,
        'coverage': {'cached_ON_entitlement_events': len(entitlements),
            'class_explicit_issuer_schedule_observations': len(events),
            'unconflicted_class_explicit_issuer_schedules': sum(not event['issues'] for event in events),
            'cross_source_candidates': len(cross_source), 'unresolved_payment_conflicts': len(conflicts),
            'unknown_absence_is_not_zero': True,
            'history_completeness': 'not_proven_by_index_or_cached_entitlement_coverage'},
        'network': {'new_requests': len(request_rows), 'cap': MAX_REQUESTS,
            'exchange_requests': 0, 'automated_exchange_access': False,
            'results': sorted(result_rows, key=lambda row: row['request_number']),
            'search_discovery': search_discovery,
            'request_log': sorted(request_rows, key=lambda row: row['request_number'])},
        'sources': list(reader.sources.values()),
        'integration': 'research_sidecar_only_parent_must_review_no_pointer_or_inventory_writes'}
    target = _save(reader.folder, 'report', _dump(report), '.json')
    return target, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--fetch-sequences', nargs='+')
    parser.add_argument('--fetch-ri', action='store_true')
    parser.add_argument('--build-report', action='store_true')
    parser.add_argument('--audit', type=Path)
    args = parser.parse_args()
    if args.fetch_sequences:
        acquire(args.data_dir, args.fetch_sequences)
    if args.fetch_ri:
        print(json.dumps(acquire_ri(args.data_dir)))
    if args.build_report:
        target, report = build_report(args.data_dir, args.audit)
        print(json.dumps({'report': target, 'coverage': report['coverage'],
                          'network': report['network']['new_requests']}))


if __name__ == '__main__':
    main()
