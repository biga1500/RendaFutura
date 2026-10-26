"""Bounded, offline-first research of four issuers' corporate-action records.

Only the explicitly selected public CVM documents can be acquired. Exchange
supplements are read from existing caches; no exchange request is implemented.
Research does not approve an asset, modify its history, or activate a dataset.
"""

import argparse
import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request
import zipfile

from pypdf import PdfReader


CACHE = 'raw/primary-research/corporate-actions'
MAX_BYTES = 12_000_000
IDENTITIES = {
    'BRAP3': (18724, '03847461000192', 'ON', 'BRBRAPACNOR5'),
    'BRAP4': (18724, '03847461000192', 'PN', 'BRBRAPACNPR2'),
    'ITUB3': (19348, '60872504000123', 'ON', 'BRITUBACNOR4'),
    'ITUB4': (19348, '60872504000123', 'PN', 'BRITUBACNPR1'),
}
INDEX_HASHES = {
    2021: 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7',
    2025: '22f38dff778a2b036f4b38a8f3aac2c860c2f58adf6f457784111f3684b28409',
}
DOCUMENTS = {
    '425751': (2021, 18724),  # Bradespar bonus and proposed capital return.
    '427107': (2021, 18724),  # Additional bonus terms.
    '451548': (2021, 18724),  # Capital return: record, ex and delivery dates.
    '440701': (2021, 18724),  # Cost allocation and fractional entitlement.
    '429639': (2021, 19348),  # Completed XPart / XP transaction.
    '396967': (2021, 19348),  # Constituted XPart and its one-for-one rights.
    '411233': (2021, 19348),  # Regulatory approval and continuation of rights.
    '420257': (2021, 19348),  # Meeting and Itaú's last cum-rights trading date.
    '420254': (2021, 7617),   # Itaúsa's participation in the same XPart transaction.
    '429540': (2021, 7617),   # Actual XP shares delivered in the incorporation.
    '855623': (2025, 19348),  # Ten-percent bonus and exact March record/ex dates.
    '882945': (2025, 19348),  # Actual auction of bonus fractions, not dividends.
    '983851': (2025, 19348),  # Three-percent bonus.
}
FAQ_URL = 'https://investors.xpinc.com/servicos-aos-investidores/faqs/'
SOURCE_ISSUERS = {(item[0], item[1]) for item in IDENTITIES.values()} | {(7617, '61532644000115')}
PINNED_DOCUMENTS = {
    # Hashes pin the complete primary documents whose economic terms were read.
    '396967': ('4d38844f8856a006a6b4ffe33b5111920502f0528046346a46c5ef754d517c29', '2021-05-31', '872230'),
    '411233': ('21032352d210e37ac814354d2aa2c4b851d2597e7c2c71a0192513e4a120862f', '2021-07-27', '886496'),
    '420254': ('02eb087d3c53f4d895249eb66fb016f7825ce46d5ee4ca801e39ce09b08d76d2', '2021-08-20', '895520'),
    '420257': ('5185dfd4da93ec1d767d1dc2703f9889a57df8132cd1fca5be5a3560a7130550', '2021-08-20', '895523'),
    '425751': ('7500885c4798628505c5ccd0c137029ab0f124161a16996c173a5461737db502', '2021-09-14', '901017'),
    '427107': ('bd2d56c83192adb084a1819a0c0fc12df8d30c667eafe30956fdf3b12c11832f', '2021-09-21', '902373'),
    '429540': ('2bb0cd268ea2f1400b836b9b8b59ba93e53005634f3d4bc113f403712e91ec82', '2021-10-01', '904806'),
    '429639': ('ff7d7f6d1c4650437c1dc8e8aa0e677a3e10a53046ffebdcbcbb5675d81f2989', '2021-10-02', '904905'),
    '440701': ('ed23de8901edaad1a6ce50ad9cb6186bc1e55d144764736ae8e6fb4a41a05b86', '2021-11-11', '915967'),
    '451548': ('9ac7c7f5a5ba4e77b02a5368a08bd2419277cc046a80c15f941dd65334b8c854', '2021-12-17', '926814'),
    '855623': ('a5a8e41baf026da1b1f0bcd4346dcdb2869d3792f2f8778794d78bc654b51269', '2025-02-05', '1330897'),
    '882945': ('ae1ece9adb6982b015412aea259b1801d4637c6ed734e069fe623fec66c64fbe', '2025-04-07', '1358219'),
    '983851': ('3427b8b809ceb2e119a8d6d29ac6841ebac35d74a3c9373888910b5af03f27ef', '2025-12-18', '1459139'),
}
SUPPLEMENT_HASHES = {
    18724: 'a017eeaf47b28021f1ccb6f560c8f4dca735d65cda98d74cf0f8865894666c0c',
    19348: '7abacc562d1257dc5fa829cbfc55e27dd2b9b65c5089a7b1ed700a236443fd3c',
}


def record_http_denial(folder, url, status):
    """Persist access denial so a later command cannot silently retry it."""
    _require(status in (401, 403, 429), 'not_a_denial_status')
    receipt = {'url': url, 'http_status': status, 'retry_performed': False,
               'status': 'access_denied_no_bypass',
               'retrieved_at': datetime.now(timezone.utc).isoformat()}
    raw = _json_bytes(receipt)
    relative = f'{CACHE}/http-denial-{hashlib.sha256(raw).hexdigest()}.json'
    _immutable(_path(folder, relative), raw)
    return receipt


def _check_denials(folder, url):
    host = urlsplit(url).hostname
    for path in _path(folder, CACHE).glob('http-denial-*.json'):
        _require(not path.is_symlink() and path.stat().st_size <= 100_000, 'unsafe_denial_receipt')
        receipt = json.loads(path.read_bytes())
        if urlsplit(receipt['url']).hostname == host and receipt['http_status'] in (401, 403, 429):
            raise ValueError('primary_host_access_denied_no_retry')


def _record_failed_response(folder, document, raw, content_type, status):
    digest = hashlib.sha256(raw).hexdigest()
    relative = f'{CACHE}/response-{document["sequence"]}-{digest}.bin'
    _immutable(_path(folder, relative), raw)
    receipt = {**document, 'sha256': digest, 'file': relative, 'bytes': len(raw),
               'http_status': status, 'content_type': content_type,
               'status': 'unexpected_format_not_primary_proof', 'retry_performed': False,
               'retrieved_at': datetime.now(timezone.utc).isoformat()}
    _immutable(_path(folder, relative[:-4] + '-source.json'), _json_bytes(receipt))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _date(value):
    _require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
             'invalid_corporate_action_date')
    return date.fromisoformat(value)


def _path(folder, relative):
    root = Path(folder).resolve()
    _require(isinstance(relative, str) and not Path(relative).is_absolute()
             and '\\' not in relative and '..' not in relative.split('/'), 'unsafe_proof_path')
    target = root / relative
    _require(target.resolve().is_relative_to(root)
             and not any(parent.is_symlink() for parent in [target, *target.parents]
                         if parent.is_relative_to(root)), 'unsafe_proof_path')
    return target


def _immutable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    _require(not path.is_symlink(), 'unsafe_proof_path')
    if path.exists():
        _require(path.read_bytes() == body, 'immutable_proof_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8')


def _document_identity(url):
    parts = urlsplit(url)
    _require(parts.scheme == 'https' and parts.hostname == 'www.rad.cvm.gov.br'
             and not parts.username and not parts.password and parts.port in (None, 443)
             and not parts.fragment and parts.path == '/ENET/frmDownloadDocumento.aspx',
             'unapproved_primary_document_url')
    query = parse_qs(parts.query, strict_parsing=True)
    _require(set(query) == {'Tela', 'descTipo', 'CodigoInstituicao', 'numProtocolo',
                            'numSequencia', 'numVersao'}
             and all(len(values) == 1 for values in query.values())
             and query['Tela'] == ['ext'] and query['descTipo'] == ['IPE']
             and query['CodigoInstituicao'] == ['1']
             and all(re.fullmatch(r'[1-9]\d*', query[key][0])
                     for key in ('numProtocolo', 'numSequencia', 'numVersao')),
             'invalid_primary_document_identity')
    return query['numSequencia'][0], int(query['numVersao'][0])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('corporate_action_redirect_not_followed')


def index_document(folder, sequence):
    """Select an exact issuer/document from a hash-pinned, already cached index."""
    _require(sequence in DOCUMENTS, 'document_outside_bounded_research')
    year, cvm = DOCUMENTS[sequence]
    digest = INDEX_HASHES[year]
    relative = f'raw/primary-research/cvm-documents/index-{year}-{digest}.zip'
    path = _path(folder, relative)
    _require(path.stat().st_size <= MAX_BYTES, 'oversized_primary_index')
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == digest, 'primary_index_hash_mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = f'ipe_cia_aberta_{year}.csv'
        _require(archive.getinfo(member).file_size <= 100_000_000, 'oversized_primary_index_member')
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        matches = [row for row in rows if
                   parse_qs(urlsplit(row['Link_Download']).query).get('numSequencia') == [sequence]]
    _require(len(matches) == 1, 'primary_index_document_not_unique')
    row = matches[0]
    actual, version = _document_identity(row['Link_Download'])
    cnpj = re.sub(r'\D', '', row['CNPJ_Companhia'])
    _require(actual == sequence and int(row['Codigo_CVM']) == cvm
             and (cvm, cnpj) in SOURCE_ISSUERS
             and version == int(row['Versao']), 'primary_index_issuer_mismatch')
    published = _date(row['Data_Entrega'])
    _require(str(cvm).zfill(6) + 'IPE' in row['Protocolo_Entrega'], 'primary_index_protocol_mismatch')
    return {'sequence': sequence, 'version': version, 'cvm': cvm, 'cnpj': cnpj,
            'issuer_name': row['Nome_Companhia'], 'published_at': published.isoformat(),
            'url': row['Link_Download'], 'subject': row['Assunto'],
            'index_file': relative, 'index_sha256': digest, 'index_row': row}


def read_proof(folder, receipt):
    sequence = receipt.get('sequence')
    document = index_document(folder, sequence)
    _require(all(receipt.get(key) == document[key] for key in
                 ('sequence', 'version', 'cvm', 'cnpj', 'published_at', 'url', 'index_sha256')),
             'primary_receipt_identity_mismatch')
    digest = receipt.get('sha256')
    _require(isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid_primary_hash')
    relative = f'{CACHE}/cvm-{sequence}-v{document["version"]}-{digest}.pdf'
    _require(receipt.get('file') == relative, 'invalid_primary_cache_reference')
    path = _path(folder, relative)
    _require(path.stat().st_size <= MAX_BYTES, 'oversized_primary_document')
    raw = path.read_bytes()
    _require(len(raw) == receipt.get('bytes') and hashlib.sha256(raw).hexdigest() == digest
             and raw.startswith(b'%PDF-'), 'primary_document_hash_or_signature_mismatch')
    observed = datetime.fromisoformat(receipt['retrieved_at'])
    _require(observed.tzinfo is not None and observed.date() >= _date(document['published_at']),
             'invalid_primary_retrieval_time')
    return raw


def acquire_document(folder, sequence):
    """At most one request; HTTP denial/rate limiting propagates without retry."""
    document = index_document(folder, sequence)
    directory = _path(folder, CACHE)
    for path in sorted(directory.glob(f'cvm-{sequence}-v{document["version"]}-*-source.json')):
        _require(not path.is_symlink() and path.stat().st_size <= 100_000, 'unsafe_primary_receipt')
        receipt = json.loads(path.read_bytes())
        return read_proof(folder, receipt), receipt
    _require(not list(directory.glob(f'response-{sequence}-*-source.json')),
             'previous_unexpected_response_not_retried')
    _check_denials(folder, document['url'])
    request = urllib.request.Request(document['url'], headers={
        'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
            _require(response.status == 200 and response.geturl() == document['url'],
                     'unexpected_primary_document_response')
            content_type = response.headers.get('Content-Type', '')
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429):
            record_http_denial(folder, document['url'], error.code)
        raise
    if len(raw) > MAX_BYTES or not raw.startswith(b'%PDF-'):
        _record_failed_response(folder, document, raw[:MAX_BYTES], content_type, 200)
        raise ValueError('invalid_primary_document_response')
    digest = hashlib.sha256(raw).hexdigest()
    relative = f'{CACHE}/cvm-{sequence}-v{document["version"]}-{digest}.pdf'
    receipt = {**document, 'file': relative, 'sha256': digest, 'bytes': len(raw),
               'authority': 'CVM_IPE_primary', 'retrieved_at': datetime.now(timezone.utc).isoformat(),
               'content_type': content_type, 'settlement_confirmed': False,
               'point_in_time_archive_verified': False}
    _immutable(_path(folder, relative), raw)
    _immutable(_path(folder, relative[:-4] + '-source.json'), _json_bytes(receipt))
    return raw, receipt


def acquire_xp_faq(folder):
    directory = _path(folder, CACHE)
    for path in sorted(directory.glob('xp-faq-*-source.json')):
        receipt = json.loads(path.read_bytes())
        relative = receipt.get('file', '')
        digest = receipt.get('sha256', '')
        _require(re.fullmatch(r'[0-9a-f]{64}', digest)
                 and relative == f'{CACHE}/xp-faq-{digest}.html'
                 and receipt.get('url') == FAQ_URL, 'invalid_issuer_FAQ_receipt')
        raw = _path(folder, relative).read_bytes()
        _require(len(raw) == receipt.get('bytes') and len(raw) <= MAX_BYTES
                 and hashlib.sha256(raw).hexdigest() == digest, 'issuer_FAQ_hash_mismatch')
        return raw, receipt
    _check_denials(folder, FAQ_URL)
    request = urllib.request.Request(FAQ_URL, headers={
        'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'text/html'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
            _require(response.status == 200 and response.geturl() == FAQ_URL,
                     'unexpected_issuer_FAQ_response')
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429):
            record_http_denial(folder, FAQ_URL, error.code)
        raise
    _require(len(raw) <= MAX_BYTES and b'<html' in raw.lower(), 'invalid_issuer_FAQ_response')
    digest = hashlib.sha256(raw).hexdigest()
    relative = f'{CACHE}/xp-faq-{digest}.html'
    receipt = {'file': relative, 'sha256': digest, 'bytes': len(raw), 'url': FAQ_URL,
               'authority': 'primary_issuer_RI', 'issuer': 'XP Inc.',
               'retrieved_at': datetime.now(timezone.utc).isoformat(),
               'historical_publication_date_verified': False,
               'point_in_time_archive_verified': False, 'settlement_confirmed': False}
    _immutable(_path(folder, relative), raw)
    _immutable(_path(folder, relative[:-5] + '-source.json'), _json_bytes(receipt))
    return raw, receipt


def extract_pages(raw):
    _require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES and raw.startswith(b'%PDF-'),
             'bounded_primary_PDF_required')
    reader = PdfReader(io.BytesIO(raw))
    _require(not reader.is_encrypted and 0 < len(reader.pages) <= 64, 'unsupported_primary_PDF')
    return [page.extract_text(extraction_mode='layout') for page in reader.pages]


def load_documents(folder):
    """Load only the owned proof directory, with the immutable index cross-check."""
    documents = {}
    for sequence in PINNED_DOCUMENTS:
        candidates = sorted(_path(folder, CACHE).glob(f'cvm-{sequence}-v1-*-source.json'))
        _require(len(candidates) == 1, 'pinned_primary_document_missing_or_ambiguous')
        path = candidates[0]
        _require(not path.is_symlink() and path.stat().st_size <= 100_000, 'unsafe_primary_receipt')
        receipt = json.loads(path.read_bytes())
        documents[sequence] = (read_proof(folder, receipt), receipt)
    return documents


def _proof(documents, sequence, as_of):
    """Pure verification against pinned bytes and the exact issuer/date/URL."""
    _require(sequence in PINNED_DOCUMENTS and sequence in documents, 'primary_action_proof_missing')
    raw, receipt = documents[sequence]
    digest, published, protocol = PINNED_DOCUMENTS[sequence]
    year, cvm = DOCUMENTS[sequence]
    cnpj = dict(SOURCE_ISSUERS)[cvm]
    expected_url = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
                    f'&CodigoInstituicao=1&numProtocolo={protocol}&numSequencia={sequence}&numVersao=1')
    _require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES
             and hashlib.sha256(raw).hexdigest() == digest, 'pinned_primary_action_hash_mismatch')
    _require(receipt.get('sequence') == sequence and receipt.get('version') == 1
             and type(receipt.get('cvm')) is int and receipt['cvm'] == cvm
             and receipt.get('cnpj') == cnpj and receipt.get('published_at') == published
             and receipt.get('index_sha256') == INDEX_HASHES[year]
             and receipt.get('index_file') == f'raw/primary-research/cvm-documents/index-{year}-{INDEX_HASHES[year]}.zip'
             and receipt.get('url') == expected_url and receipt.get('sha256') == digest
             and receipt.get('bytes') == len(raw)
             and receipt.get('file') == f'{CACHE}/cvm-{sequence}-v1-{digest}.pdf',
             'pinned_primary_action_identity_mismatch')
    observed = datetime.fromisoformat(receipt['retrieved_at'])
    _require(observed.tzinfo is not None and observed.date() >= _date(published),
             'invalid_primary_retrieval_time')
    _require(_date(published) <= as_of, 'primary_action_publication_after_as_of')
    return {key: receipt[key] for key in ('sequence', 'version', 'cvm', 'cnpj', 'published_at',
             'url', 'sha256', 'file', 'retrieved_at', 'index_file', 'index_sha256')}


def _br_date(value):
    _require(isinstance(value, str) and re.fullmatch(r'\d{2}/\d{2}/\d{4}', value),
             'invalid_cached_action_date')
    return datetime.strptime(value, '%d/%m/%Y').date()


def canonicalize_supplement(identity, supplement_raw, documents, as_of, *, window_start):
    """Pure action classification; never repairs prices, dividends or admission.

    Returns standard ``date``/``ratio`` entries only for independently proven
    same-security bonuses. In-kind distributions remain separately blocked.
    Inputs are never mutated. All original in-window exchange rows are retained.
    """
    as_of = _date(as_of) if isinstance(as_of, str) else as_of
    start = _date(window_start) if isinstance(window_start, str) else window_start
    _require(type(as_of) is date and type(start) is date and start < as_of,
             'invalid_corporate_action_window')
    ticker = identity.get('ticker')
    _require(ticker in IDENTITIES, 'issuer_outside_bounded_action_research')
    cvm, cnpj, share, isin = IDENTITIES[ticker]
    _require(type(identity.get('cvm')) is int and identity['cvm'] == cvm
             and identity.get('share_class') == share and identity.get('isin') == isin,
             'corporate_action_instrument_identity_mismatch')
    if identity.get('cnpj') is not None:
        _require(isinstance(identity['cnpj'], str)
                 and re.sub(r'\D', '', identity['cnpj']) == cnpj, 'corporate_action_CNPJ_mismatch')
    _require(isinstance(supplement_raw, bytes) and len(supplement_raw) <= MAX_BYTES
             and hashlib.sha256(supplement_raw).hexdigest() == SUPPLEMENT_HASHES[cvm],
             'unreviewed_corporate_action_supplement_hash')
    supplement = json.loads(supplement_raw)
    _require(isinstance(supplement, list) and len(supplement) == 1
             and supplement[0].get('codeCVM') == str(cvm)
             and supplement[0].get('code') == ticker[:4], 'corporate_action_supplement_issuer_mismatch')
    raw_actions = [row for row in supplement[0]['stockDividends']
                   if row.get('isinCode') == isin and start < _br_date(row['lastDatePrior']) <= as_of]
    actions, non_income, issues, used = [], [], [], {}

    def sources(*sequences):
        values = [_proof(documents, sequence, as_of) for sequence in sequences]
        used.update({item['sequence']: item for item in values})
        return values

    if cvm == 18724:
        if start < date(2021, 9, 21) <= as_of:
            evidence = sources('425751', '427107')
            actions.append({'date': '2021-09-21', 'ratio': 1.1295, 'kind': 'same_class_bonus',
                'isin': isin, 'record_date': '2021-09-20', 'credit_date': '2021-09-23',
                'tradable_from': '2021-09-24', 'ratio_basis': 'primary_stated_1.295_new_per_10',
                'primary_stated_ratio_decimal': '1.1295', 'evidence': evidence,
                'automatic_price_factor_application_allowed': False})
            issues.append({'code': 'bonus_ratio_precision_difference_requires_explicit_basis',
                'primary_stated_factor': '1.1295', 'cached_exchange_factor': '1.1294774625',
                'total_issued_shares': 45062565, 'total_pre_bonus_shares': 348034045,
                'total_count_implied_percentage': str(Decimal(45062565) / Decimal(348034045) * 100),
                'explanation': 'Primary headline ratio is rounded; cached percentage matches total counts to 8 decimals. '
                    'Do not silently replace either source or treat fractional-share proceeds as dividends.'})
        if start < date(2021, 12, 17) <= as_of:
            evidence = sources('440701', '451548')
            non_income.append({'kind': 'in_kind_capital_return', 'isin': isin,
                'holder_share_factor': '1', 'record_date': '2021-12-16', 'ex_date': '2021-12-17',
                'announced_delivery_date': '2021-12-20', 'received_isin': 'BRVALEACNOR0',
                'received_security': 'VALE3', 'received_units_per_origin_unit': '0.332373453',
                'quantity_rounding': 'floor_whole_shares', 'fraction_policy': 'needs_separate_verified_notice',
                'cash_dividend_or_JCP': False, 'income_amount': '0',
                'principal_in_kind_not_income': True, 'is_own_share_split': False,
                'settlement_confirmed': False, 'status': 'announced_historical_delivery_not_account_settlement',
                'evidence': evidence})
    else:
        if start < date(2021, 10, 4) <= as_of:
            evidence = sources('396967', '420257', '429639', '420254', '429540')
            xpart = 'BRXPARACNOR9' if share == 'ON' else 'BRXPARACNPR6'
            non_income.append({'kind': 'spin_off_and_subsequent_merger', 'isin': isin,
                'holder_share_factor': '1', 'record_date': '2021-10-01', 'ex_date': '2021-10-04',
                'xpart_isin': xpart, 'xpart_units_per_origin_unit': '1',
                'merger_approved_at': '2021-10-01', 'final_received_security': 'XP_Class_A_backed_BDR',
                'final_received_isin': None, 'final_received_isin_status': 'not_verified_in_pinned_notices',
                'xpart_units_per_final_received_unit': '43.3128323',
                'final_received_units_per_origin_unit': str(Decimal(1) / Decimal('43.3128323')),
                'announced_delivery_date': None, 'delivery_date_status': 'not_verified_in_pinned_notices',
                'cash_dividend_or_JCP': False, 'income_amount': '0',
                'principal_in_kind_not_income': True, 'is_own_share_split': False,
                'fraction_policy': 'needs_verified_depositary_notice_and_fraction_auction',
                'settlement_confirmed': False, 'evidence': evidence,
                'announced_reference_opening_BRL': '22.6454999' if share == 'ON' else '24.4654999',
                'reference_opening_is_executed_quote': False})
        for record, ex_day, credit, ratio, sequence in [
                ('2025-03-17', '2025-03-18', '2025-03-20', '1.10', '855623'),
                ('2025-12-23', '2025-12-26', '2025-12-30', '1.03', '983851')]:
            if start < _date(ex_day) <= as_of:
                evidence = sources(sequence)
                actions.append({'date': ex_day, 'ratio': float(Decimal(ratio)), 'kind': 'same_class_bonus',
                    'isin': isin, 'record_date': record, 'credit_date': credit,
                    'ratio_decimal': ratio, 'ratio_basis': 'primary_explicit_same_class_bonus',
                    'same_security_ratio_primary_verified': True,
                    'automatic_price_factor_application_allowed': False,
                    'unchanged_independent_price_regime_checks_still_required': True, 'evidence': evidence,
                    'present_in_cached_supplement': any(row['label'] == 'BONIFICACAO'
                        and _br_date(row['lastDatePrior']).isoformat() == record for row in raw_actions)})
    for row in raw_actions:
        label, record = row['label'], _br_date(row['lastDatePrior']).isoformat()
        if label == 'BONIFICACAO':
            _require(any(item['record_date'] == record and item['isin'] == row['assetIssued']
                         for item in actions), 'unverified_same_security_bonus')
        elif label == 'REST CAP ACOES':
            _require(cvm == 18724 and record == '2021-12-16' and row['assetIssued'] == 'BRVALEACNOR0'
                     and Decimal(row['factor'].replace(',', '.')) == Decimal('33.2373453'),
                     'unverified_in_kind_capital_return')
        elif label == 'CIS RED CAP':
            expected_xpart = 'BRXPARACNOR9' if share == 'ON' else 'BRXPARACNPR6'
            _require(cvm == 19348 and record == '2021-10-01'
                     and row['assetIssued'] in (isin, expected_xpart)
                     and Decimal(row['factor'].replace(',', '.')) == 100,
                     'unverified_spin_off_leg')
        else:
            raise ValueError('corporate_action_outside_pinned_proof_scope')
    blockers = []
    if non_income:
        blockers.append('in_kind_capital_return_needs_multi_asset_history' if cvm == 18724
                        else 'spin_off_needs_multi_asset_history')
    blockers.extend(item['code'] for item in issues)
    return {'ticker': ticker, 'cvm': cvm, 'cnpj': cnpj, 'share_class': share, 'isin': isin,
            'as_of': as_of.isoformat(), 'window_start': start.isoformat(),
            'canonical_share_actions': actions, 'non_income_events': non_income,
            'source_issues': issues, 'blockers': blockers, 'raw_in_window_actions': raw_actions,
            'sources': list(used.values()), 'supplement_sha256': SUPPLEMENT_HASHES[cvm],
            'classification_basis': 'hash_pinned_primary_terms_and_exact_cached_ISIN',
            'model_admission_granted': False, 'prices_or_cash_modified': False,
            'settlement_confirmed': False, 'point_in_time_archive_verified': False}


def inspect_cached_price_regimes(folder, *, as_of='2026-09-30'):
    """Replay the unmodified price gate only; never skip it or repair its input."""
    from .broad_stocks import split_price_history
    from .inventory_candidates import _Cache, _cash_pages, _identity
    from .market_inventory import load_inventory
    from .share_basis import b3_evidence, reconcile_prices

    cutoff = _date(as_of)
    inventory = load_inventory(folder)
    cache = _Cache(folder, inventory)
    results = []
    for ticker in IDENTITIES:
        row = next(item for item in inventory['stocks'] if item['ticker'] == ticker)
        _, codes = _identity(row, cache, cutoff)
        _, quotes = b3_evidence(_cash_pages(ticker, codes, row['cvm'], cache), row['share_class'])
        relative = f'raw/expanded/{ticker}-prices.json'
        raw, _ = cache.read(relative, 'corporate_action_price_regime_research')
        _, actions = split_price_history(raw, ticker, cutoff, allow_recent_actions=True)
        result = json.loads(raw)['chart']['result'][0]
        daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): price for stamp, price in
                 zip(result['timestamp'], result['indicators']['quote'][0]['close'])}
        event_day = date(2021, 12, 16) if ticker.startswith('BRAP') else date(2021, 9, 30)
        anchors = [{'date': day.isoformat(), 'independent_raw_quote': value,
                    'cached_vendor_quote': daily.get(day)} for day, value in sorted(quotes.items())
                   if event_day <= day <= date(2022, 5, 31)]
        diagnostic = {'ticker': ticker, 'prices_file': relative,
            'prices_sha256': hashlib.sha256(raw).hexdigest(), 'anchors': anchors,
            'share_actions_from_existing_price_cache': actions, 'reconciliation_thresholds_changed': False,
            'price_gate_only_not_complete_candidate_or_financial_approval': True}
        try:
            _, matches, mode = reconcile_prices(raw, ticker, actions, quotes, cutoff)
            diagnostic.update(price_gate_pass=True, price_regime=mode, independent_matches=len(matches))
        except ValueError as error:
            diagnostic.update(price_gate_pass=False, reason=str(error))
        results.append(diagnostic)
    return {'records': results, 'receipts': list(cache.receipts.values()),
            'network_requests': 0, 'prices_or_cash_modified': False}


def write_report(folder, *, as_of='2026-09-30', window_start='2021-09-30'):
    """Write one content-addressed, reproducible four-code report only."""
    documents = load_documents(folder)
    records = []
    for ticker, (cvm, cnpj, share, isin) in IDENTITIES.items():
        relative = f'raw/expanded/b3-supplement-{ticker[:4]}.json'
        result = canonicalize_supplement({'ticker': ticker, 'cvm': cvm, 'cnpj': cnpj,
                'share_class': share, 'isin': isin}, _path(folder, relative).read_bytes(), documents,
                as_of, window_start=window_start)
        result['supplement_file'] = relative
        records.append(result)
    report = {'schema_version': 1, 'as_of': as_of, 'window_start': window_start,
        'scope': 'BRAP3_BRAP4_ITUB3_ITUB4_corporate_action_source_research_only',
        'records': records, 'source_receipts': [receipt for _, receipt in documents.values()],
        'cached_price_regime_replay': inspect_cached_price_regimes(folder, as_of=as_of),
        'newly_model_ready_stocks': 0, 'parent_modules_edited': False, 'dataset_activated': False,
        'B3_network_requests': 0, 'missing_data_imputed': False,
        'conclusions': [
            'The parent non-cash action block is economically justified, not merely a label/HTML parsing bug.',
            'stockDividends is an exchange array for multiple action types, not a promise of cash dividends.',
            'REST CAP ACOES transfers Vale shares as principal; Bradespar share quantity is unchanged.',
            'CIS RED CAP includes XPart entitlement and retained Itaú shares; factor 100 must not double Itaú shares.',
            'Two bona-fide Itaú bonuses are separately canonicalized; the March 2025 bonus is absent from this supplement.',
            'CVM returned PDF bytes with text/html MIME; bytes/signature, not MIME, establish the parsed format.',
            'The unchanged price gate additionally rejects BRAP3 and BRAP4 before capital return; never add returned value to an already adjusted vendor series without reconstructing its exact regime.',
            'Issuer XP FAQ returned HTTP 403; no retry or access bypass. The exchange ratio is independently pinned in CVM Itaúsa filing 420254.',
        ],
        'required_before_admission': [
            'Verify origin and received-security raw price regimes across every event; share factors alone do not reconcile a spin-off.',
            'Model the in-kind receivable from ex date through actual announced/verified delivery; do not book a dividend.',
            'Preserve the retained origin quantity and add the received security/receivable with its own ISIN and historical value.',
            'Specify and validate hold-versus-sale treatment, transaction costs, fractional-share proceeds, and capital-cost allocation.',
            'For Itaú, obtain exact final XP BDR ISIN, delivery and fractional-sale dates from a primary depositary/issuer notice.',
            'Check any existing vendor capital-return adjustment before adding the received value to avoid double counting.',
            'Re-run unchanged cash, price, fiscal, issuer-concentration and out-of-sample financial validation after ledger integration.',
        ]}
    raw = _json_bytes(report)
    relative = f'{CACHE}/report-{as_of}-{hashlib.sha256(raw).hexdigest()}.json'
    _immutable(_path(folder, relative), raw)
    return _path(folder, relative), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.acquire:
        for sequence in DOCUMENTS:
            try:
                raw, receipt = acquire_document(args.folder, sequence)
                print(json.dumps({'sequence': sequence, 'sha256': receipt['sha256'],
                                  'pages': extract_pages(raw)}, ensure_ascii=True), flush=True)
            except urllib.error.HTTPError as error:
                print(json.dumps({'sequence': sequence, 'http_status': error.code,
                                  'stopped_without_retry': True}), flush=True)
                break
    if args.report:
        path, report = write_report(args.folder)
        print(json.dumps({'report': str(path), 'stocks': len(report['records']),
                          'newly_model_ready_stocks': report['newly_model_ready_stocks']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
