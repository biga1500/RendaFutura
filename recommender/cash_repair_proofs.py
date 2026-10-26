"""Offline, literal cash-calendar repair proofs. Never edits a source or gate."""

from collections import Counter
from copy import deepcopy
import csv
from datetime import date
from decimal import Decimal
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
import zipfile

import pdfplumber
from pypdf import PdfReader

from . import cvm_proventos_documents as cvm


CUTOFF = date(2026, 9, 30)
FCA_FILE = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
INDEX_PINS = {
    2021: 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7',
    2022: '44e4a5bf88efa6696b9150f4b7fde3693cbf7ec79d511b65a62c0135ac674c5d',
    2023: 'ef1093983df55399b8ec02255eaa822d941ea797f881d00b8f61f23fb63fb1b0',
    2024: '65878657e487ba86b717e93f112a11cf08faf0356ae245ef6e0e72b0fbac80e2',
    2025: '22f38dff778a2b036f4b38a8f3aac2c860c2f58adf6f457784111f3684b28409',
    2026: 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
}
REVISION_FIELDS = ('CNPJ_Companhia', 'Categoria', 'Data_Referencia', 'Data_Entrega',
                   'Protocolo_Entrega', 'Versao', 'Link_Download')


class ProofError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ProofError(reason)


def text(value):
    return ' '.join(unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().split())


def read(cache, relative, digest, url=None, published_at=None):
    body, _ = cache.read(relative, 'exact_cash_repair_primary_evidence')
    require(hashlib.sha256(body).hexdigest() == digest, 'cash_repair_original_hash_mismatch:' + relative)
    source = {'file': relative, 'sha256': digest, 'bytes': len(body), 'url': url,
        'published_at': published_at, 'retrieved_at': None,
        'retrieval_basis': 'unknown_not_file_mtime',
        'authority': 'primary_original_reported_schedule_not_settlement',
        'settlement_verified': False, 'point_in_time_archive_verified': False}
    return body, source


def verified_identity(cache, identity, expected, detail_sha, cnpj):
    keys = ('ticker', 'cvm', 'share_class', 'isin')
    require({k: identity.get(k) for k in keys} == expected, 'cash_repair_exact_CVM_class_ISIN_required')
    fca, source = read(cache, FCA_FILE, FCA_SHA,
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip')
    with zipfile.ZipFile(io.BytesIO(fca)) as archive:
        members = {}
        for name in ('fca_cia_aberta_geral_2026.csv', 'fca_cia_aberta_valor_mobiliario_2026.csv'):
            with archive.open(name) as stream:
                members[name] = [(line, row) for line, row in enumerate(
                    csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'), 2)
                    if row['CNPJ_Companhia'] == cnpj]
        general = [(line, row) for line, row in members['fca_cia_aberta_geral_2026.csv']
                   if int(row['Codigo_CVM']) == identity['cvm']]
        stocks = [(line, row) for line, row in members['fca_cia_aberta_valor_mobiliario_2026.csv']
                  if row['Codigo_Negociacao'] == identity['ticker']]
        require(len(general) == len(stocks) == 1, 'cash_repair_primary_registered_identity_ambiguous')
        gline, grow = general[0]
        sline, srow = stocks[0]
        label = 'Acoes Ordinarias' if identity['share_class'] == 'ON' else 'Acoes Preferenciais'
        require(text(srow['Valor_Mobiliario']) == label and not srow['Sigla_Classe_Acao_Preferencial']
                and grow['ID_Documento'] == srow['ID_Documento'], 'cash_repair_primary_class_conflict')
    source['source_locator'] = {'members': [
        {'member': 'fca_cia_aberta_geral_2026.csv', 'line': gline, 'row': grow},
        {'member': 'fca_cia_aberta_valor_mobiliario_2026.csv', 'line': sline, 'row': srow}]}
    detail_body, detail_source = read(cache, f'raw/expanded/b3-detail-{identity["cvm"]}.json', detail_sha)
    detail = json.loads(detail_body)
    require(int(detail['codeCVM']) == identity['cvm'] and detail['cnpj'] == re.sub(r'\D', '', cnpj)
            and [r['isin'] for r in detail['otherCodes'] if r['code'] == identity['ticker']] == [identity['isin']],
            'cash_repair_cached_exact_ISIN_conflict')
    detail_source.update(authority='existing_exact_security_identity_cache_no_network',
        source_locator={'field': 'otherCodes', 'ticker': identity['ticker'], 'isin': identity['isin']})
    return [source, detail_source]


@lru_cache(maxsize=8)
def _revision_rows(body, member):
    """Cache only immutable bytes and compact rows, never mutable source views."""
    result = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        with archive.open(member) as stream:
            reader = csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
            for line, row in enumerate(reader, 2):
                if row['Codigo_CVM'].isdigit():
                    result.setdefault(int(row['Codigo_CVM']), []).append(
                        (line, tuple(row[field] for field in REVISION_FIELDS)))
    return {code: tuple(rows) for code, rows in result.items()}


def _revision_family(protocol):
    matched = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', protocol)
    require(not protocol or matched, 'cash_repair_unrecognized_revision_family')
    return (matched[1], matched[2]) if matched else None


@lru_cache(maxsize=32)
def _structured_revision_fields(body):
    require(body.lstrip().startswith(b'%PDF-') and len(body) <= cvm.MAX_BYTES,
            'cash_repair_structured_revision_native_PDF_required')
    reader = PdfReader(io.BytesIO(body), strict=True)
    require(not reader.is_encrypted and 1 <= len(reader.pages) <= 12,
            'cash_repair_structured_revision_native_PDF_required')
    tables = []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page in pdf.pages:
            require(len(page.chars) <= 150_000, 'cash_repair_structured_revision_text_limit')
            tables.extend(page.extract_tables())

    def field(label):
        values = [str(table[i + 1][column] or '') for table in tables
                  for i, row in enumerate(table[:-1]) for column, cell in enumerate(row)
                  if cvm.norm(cell) == label and column < len(table[i + 1])]
        require(len(values) == 1, 'cash_repair_structured_revision_link_ambiguous:' + label)
        return values[0]

    provento, version = cvm.compact(field('protocolo provento')), cvm.compact(field('versao'))
    ipe = re.findall(r'\b\d{6}IPE\d{8}\d{2}\d+-\d+\b', field('protocolo ipe'))
    sent = cvm.day(field('data envio'))
    isins = tuple(sorted({cvm.compact(cell) for table in tables for row in table for cell in row
                          if re.fullmatch(r'BR[A-Z0-9]{9}\d', cvm.compact(cell))}))
    require(provento.isdigit() and int(provento) > 0 and version.isdigit()
            and len(ipe) == 1 and sent and isins, 'cash_repair_structured_revision_link_ambiguous')
    return provento, ipe[0], int(version), sent, isins


def _structured_revision_link(cache, row, cvm_code):
    """Resolve only existing, URL-bound native originals; never acquire or infer a link."""
    require(hasattr(cache, 'folder'), 'cash_repair_structured_revision_link_ambiguous:no_native_cache')
    root = cache.folder.resolve()
    sequence, version = cvm.document_id(row['Link_Download'])
    research = root / 'raw/primary-research'
    rid = hashlib.sha256(row['Link_Download'].encode()).hexdigest()
    records = sorted((research / 'cash-calendars').glob(f'cvm{sequence}v{version}-*-source.json'))
    records += sorted((research / 'all-pending').glob(f'*/response-{rid}.json'))
    sources, originals = [], {}
    for path in records:
        require(path.resolve().is_relative_to(research.resolve()), 'cash_repair_native_link_path_conflict')
        relative = path.relative_to(root).as_posix()
        metadata_body, _ = cache.read(relative, 'cash_repair_native_revision_source_record')
        metadata = json.loads(metadata_body)
        require(isinstance(metadata, dict) and metadata.get('url') == row['Link_Download'] and not metadata.get('error')
                and metadata.get('status', 200) == 200,
                'cash_repair_structured_revision_link_ambiguous:source_record')
        digest, filename = metadata.get('sha256', ''), metadata.get('file', '')
        require(isinstance(digest, str) and isinstance(filename, str),
                'cash_repair_native_link_path_or_hash_conflict')
        target = (root / filename).resolve()
        require(re.fullmatch(r'[0-9a-f]{64}', digest) and target.is_relative_to(research.resolve())
                and target.relative_to(root).as_posix() == filename and digest in target.name,
                'cash_repair_native_link_path_or_hash_conflict')
        body, source = read(cache, filename, digest, row['Link_Download'], cvm.day(row['Data_Entrega']))
        require(len(body) == metadata.get('bytes'), 'cash_repair_native_link_byte_count_conflict')
        originals[digest] = _structured_revision_fields(body)
        sources.extend([source, {'file': relative, 'sha256': hashlib.sha256(metadata_body).hexdigest(),
            'bytes': len(metadata_body), 'url': None, 'settlement_verified': False,
            'point_in_time_archive_verified': False}])
    require(len(originals) == 1, 'cash_repair_structured_revision_link_ambiguous:missing_or_conflicting_originals')
    provento, ipe, native_version, sent, isins = next(iter(originals.values()))
    require(int(ipe[:6]) == cvm_code and native_version == version == int(row['Versao'])
            and sent <= cvm.day(row['Data_Entrega']), 'cash_repair_structured_revision_native_identity_conflict')
    sources = merge_sources(sources)
    register(cache, sources)
    return {'protocolo_provento': provento, 'protocolo_ipe': ipe,
            'isin_scope': list(isins), 'sources': sources}


def _revision_candidates(cache, year, cvm_code, reference, category, family, protocol, selected):
    candidates, sources, rows, parent_protocols = [], [], [], set()
    first_year = min(year, date.fromisoformat(reference).year)
    for current_year, digest in sorted(INDEX_PINS.items()):
        if not first_year <= current_year <= CUTOFF.year:
            continue
        member = f'ipe_cia_aberta_{current_year}.csv'
        name = f'raw/primary-research/cvm-documents/index-{current_year}-{digest}.zip'
        body, source = read(cache, name, digest,
            f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{current_year}.zip')
        sources.append(source)
        for line, fields in _revision_rows(body, member).get(cvm_code, ()):
            item = dict(zip(REVISION_FIELDS, fields))
            if cvm.day(item['Data_Entrega']) > CUTOFF.isoformat():
                continue
            if item['Protocolo_Entrega']:
                parent_protocols.add(item['Protocolo_Entrega'])
            if cvm.norm(item['Categoria']) == category:
                rows.append({**item, 'index_file': name, 'index_sha256': digest,
                             'index_member': member, 'index_line': line})
    selected_link = None
    for item in rows:
        current = _revision_family(item['Protocolo_Entrega'])
        same_family = family is not None and family == current
        same_protocol = parse_qs(urlsplit(item['Link_Download']).query)['numProtocolo'] == protocol
        if family and current and family != current:
            require(not same_protocol, 'cash_repair_conflicting_explicit_revision_families')
            continue
        if same_family or same_protocol:
            candidates.append(item)
            continue
        if category != 'relatorio proventos' or cvm.day(item['Data_Referencia']) != reference:
            continue
        # A reference date is a search hint, not a distribution/revision identifier.
        selected_link = selected_link or _structured_revision_link(cache, selected, cvm_code)
        other = _structured_revision_link(cache, item, cvm_code)
        require(selected_link['protocolo_ipe'] in parent_protocols and other['protocolo_ipe'] in parent_protocols,
                'cash_repair_structured_revision_link_ambiguous:parent_IPE_not_indexed')
        if selected_link['protocolo_provento'] != other['protocolo_provento']:
            continue
        require(_revision_family(selected_link['protocolo_ipe']) == _revision_family(other['protocolo_ipe'])
                and selected_link['isin_scope'] == other['isin_scope'],
                'cash_repair_structured_revision_link_ambiguous:parent_or_security_conflict')
        item['native_revision_link'] = {'selected': selected_link, 'candidate': other}
        candidates.append(item)
    register(cache, sources)
    return candidates, sources


def index_document(cache, year, sequence, version, cvm_code, cnpj, reference, category):
    digest = INDEX_PINS[year]
    name = f'raw/primary-research/cvm-documents/index-{year}-{digest}.zip'
    body, source = read(cache, name, digest,
        f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip')
    member = f'ipe_cia_aberta_{year}.csv'
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        with archive.open(member) as stream:
            rows = list(enumerate(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'), 2))
    found = [(line, row) for line, row in rows
        if row['Codigo_CVM'].isdigit() and int(row['Codigo_CVM']) == cvm_code
        and cvm.document_id(row['Link_Download']) == (sequence, version)]
    require(len(found) == 1, 'cash_repair_exact_index_document_required')
    line, row = found[0]
    require(int(row['Codigo_CVM']) == cvm_code and row['CNPJ_Companhia'] == cnpj
            and cvm.day(row['Data_Referencia']) == reference and cvm.norm(row['Categoria']) == category
            and int(row['Versao']) == version, 'cash_repair_index_identity_date_conflict')
    family = _revision_family(row['Protocolo_Entrega'])
    protocol = parse_qs(urlsplit(row['Link_Download']).query)['numProtocolo']
    candidates, revision_sources = _revision_candidates(cache, year, cvm_code, reference, category, family, protocol, row)
    require(all(item['CNPJ_Companhia'] == cnpj for item in candidates),
            'cash_repair_revision_issuer_identity_conflict')
    require(candidates and max(int(r['Versao']) for r in candidates) == version
            and max(cvm.day(r['Data_Entrega']) for r in candidates) == cvm.day(row['Data_Entrega']),
            'cash_repair_latest_native_revision_required_no_fallback')
    require(len({cvm.document_id(r['Link_Download']) for r in candidates
                 if int(r['Versao']) == version and cvm.day(r['Data_Entrega']) == cvm.day(row['Data_Entrega'])}) == 1,
            'cash_repair_latest_native_revision_ambiguous')
    source['source_locator'] = {'member': member, 'line': line, 'raw_row': row}
    source['revision_search'] = {'through': CUTOFF.isoformat(), 'index_sources': revision_sources,
        'same_family_or_reference_candidates': candidates,
        'later_calendar_year_revisions_checked': True}
    return {'sequence': sequence, 'version': version, 'cvm': cvm_code,
        'issuer_name': row['Nome_Companhia'], 'published_at': cvm.day(row['Data_Entrega']),
        'reference_date': reference, 'url': row['Link_Download'], 'index_evidence': source}, source


def original_blob(cache, document, file, digest, transport=True):
    body, source = read(cache, file, digest, document['url'], document['published_at'])
    if transport:
        rid = hashlib.sha256(document['url'].encode()).hexdigest()
        relative = 'raw/primary-research/all-pending/cash/response-' + rid + '.json'
        metadata_body, _ = cache.read(relative, 'exact_cash_repair_actual_acquisition_receipt')
        metadata = json.loads(metadata_body)
        require(metadata['url'] == document['url'] and metadata['sha256'] == digest
                and metadata['file'] == file and metadata['bytes'] == len(body)
                and metadata['status'] == 200 and not metadata['error'],
                'cash_repair_actual_transport_binding_required')
        source.update(retrieved_at=metadata['retrieved_at'], retrieval_basis='actual_public_GET_receipt')
    return body, source


def original(cache, document, file, digest, transport=True):
    body, source = original_blob(cache, document, file, digest, transport=transport)
    require(body.lstrip().startswith(b'%PDF-'), 'cash_repair_native_PDF_required_no_fallback')
    pages = [text(p.extract_text()) for p in PdfReader(io.BytesIO(body)).pages]
    return body, source, pages


def merge_sources(sources):
    """One byte receipt can have several distinct native document-index citations."""
    merged = {}
    for source in sources:
        key = source['file'], source['sha256']
        if key not in merged:
            merged[key] = deepcopy(source)
            merged[key]['source_locators'] = []
        locator = source.get('source_locator')
        if locator is not None and locator not in merged[key]['source_locators']:
            merged[key]['source_locators'].append(deepcopy(locator))
    return list(merged.values())


def register(cache, sources):
    for source in sources:
        cache.register_research_evidence(source)


def apply_groups(parsed, identity, cutoff, proof, *, repair_id, payment_sha, groups):
    require(cutoff == CUTOFF, 'cash_repair_exact_reference_required')
    require(parsed.get('status') != 'invalid' and parsed['report']['sha256'] == payment_sha,
            'cash_repair_exact_original_payment_page_required')
    prior = parsed.get('exact_cash_repairs', {}).get(repair_id)
    if prior:
        actual = [r for r in parsed['events'] if r.get('exact_cash_repair_id') == repair_id]
        require(actual == prior['new_events'], 'cash_repair_modified_view_cannot_be_reapplied')
        for group in prior['groups']:
            record = group['record_date']
            complete = [r for r in parsed['events'] if r.get('record_date') == record]
            expected = [r for r in prior['new_events'] if r.get('record_date') == record]
            require(complete == expected, 'cash_repair_modified_view_complete_group_conflict')
            original_record = group.get('original_record_date', record)
            require(original_record == record or not any(
                r.get('record_date') == original_record for r in parsed['events']),
                'cash_repair_modified_view_relocated_origin_reintroduced')
        return deepcopy(parsed)
    output, audits = deepcopy(parsed), []
    for record, specs in groups.items():
        original_record = specs.get('original_record_date', record)
        if original_record != record:
            require(not any(r.get('record_date') == record for r in output['events']),
                    'cash_repair_relocation_target_not_empty:' + record)
        current = [r for r in output['events'] if r.get('record_date') == original_record]
        expected = specs['expected_original']
        keys = ('label', 'payment_date', 'gross_per_share', 'source_row')
        actual = [tuple(r.get(k) for k in keys) for r in current]
        require(Counter(actual) == Counter(tuple(r.get(k) for k in keys) for r in expected),
                'cash_repair_original_group_changed:' + record)
        replacement = []
        for fact in specs['facts']:
            require(fact['record_date'] == record and fact['isin'] == identity['isin']
                    and fact['share_class'] == identity['share_class']
                    and fact['payment_date'] >= record, 'cash_repair_literal_fact_identity_or_date_conflict')
            amount = Decimal(fact['gross_per_share_decimal'])
            require(amount.is_finite() and amount > 0, 'cash_repair_positive_native_gross_required')
            matched = [r for r in current if r['label'] == fact['label']]
            template = deepcopy(matched[0]) if len(matched) == 1 else {}
            event = {**template, **fact, 'ticker': identity['ticker'], 'cvm': identity['cvm'],
                'gross_per_share': float(amount), 'reported_amount': float(amount), 'quoted_quantity': 1,
                'share_basis': 'native_per_record_share_unadjusted',
                'amount_tax_basis': 'native_explicit_gross_no_net_reconstruction',
                'reported_original_fields': deepcopy(matched if matched else current),
                'source_row': fact['source_locator_id'], 'exact_cash_repair_id': repair_id,
                'primary_declaration_id': fact['source_locator_id'],
                'primary_evidence': deepcopy(proof['sources']),
                'date_order_valid': True, 'available_as_of': fact['published_at'] <= cutoff.isoformat(),
                'status': 'announced' if fact['payment_date'] > cutoff.isoformat() else 'past_scheduled',
                'reported_status': None, 'paid_status_as_of_verified': False,
                'settlement_verified': False, 'is_settlement_confirmation': False,
                'cash_model_eligible': False, 'financially_confirmed': False,
                'gross_reconstructed_from_net': False, 'model_admitted': False}
            replacement.append(event)
        output['events'] = [r for r in output['events'] if r.get('record_date') != original_record] + replacement
        audits.append({'record_date': record, 'reported_original_fields': deepcopy(current),
            **({'original_record_date': original_record} if original_record != record else {}),
            'replacement': deepcopy(replacement), 'original_issues': [i for i in output['report']['issues']
                if i.get('row') in {r['source_row'] for r in current}],
            'original_sum_decimal': str(sum((Decimal(str(r['gross_per_share'])) for r in current), Decimal(0))),
            'native_sum_decimal': str(sum((Decimal(r['gross_per_share_decimal']) for r in replacement), Decimal(0)))})
    new = [r for r in output['events'] if r.get('exact_cash_repair_id') == repair_id]
    output.setdefault('exact_cash_repairs', {})[repair_id] = {
        'identity': {k: identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')},
        'groups': audits, 'new_events': deepcopy(new), 'sources': deepcopy(proof['sources']),
        'source_contract': proof['contract'], 'original_report_counts': deepcopy(parsed['report']['counts']),
        'original_calendar_sha256': payment_sha,
        'source_bytes_modified': False, 'independent_comparator_changed': False,
        'settlement_verified': False, 'model_admitted': False}
    return output
