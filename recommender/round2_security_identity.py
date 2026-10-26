"""Offline primary identity sidecar for B3SA3 and EQMA3B only.

PrimarySecurityIdentityAdapter.evidence returns typed documentary observations,
not a B3 company/detail payload. Native form ISINs are joined to literal FCA
codes and share classes. No suffix, deposited-position, or related-code alias
establishes identity. require_window is deliberately stricter than recovering
an ISIN: isolated entitlement snapshots do not establish continuous validity.

The optional collector uses only exact links in the pinned public CVM index,
PublicSourceCollector scope round2-identity, and never follows redirects.
No inventory, pointer, existing source, or model is changed by this module.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
import zipfile

from .bulk_pending_sources import PublicSourceCollector, _safe, _save
from .security_registration import MASTER_FIELDS, SECURITY_FIELDS


POLICY = 'RF-ROUND2-PRIMARY-SECURITY-IDENTITY-1'
BASE = 'raw/primary-research/all-pending/round2-identity'
ROOT = Path(__file__).resolve().parent / 'data/market'
CUTOFF = date(2026, 9, 30)
WINDOW_START = date(2021, 9, 30)
TARGETS = {'B3SA3': (21610, '09.346.601/0001-25'),
           'EQMA3B': (16608, '06.272.793/0001-84')}
INDEX_MANIFEST = 'raw/primary-research/cvm-documents/index-sources.json'
INDEX_SHA = '0d5f66f63001963adf3355f103c4fae241435ebf5f22bbfeb75693f072dedc14'
FCA_SHA = 'f430e9e8475b7e48c2135140b4a7a721138ee532fc7e39c1ce234263180c1163'
FCA_MANIFEST = f'raw/primary-research/units/historical-fca-evidence-2026-10-25-{FCA_SHA}.json'
OLD_REPORT = 'raw/primary-research/cvm-documents/report.json'
OLD_REPORT_SHA = '7ec5a9d42c335541c1172761a602d73814fdee12ec22bb69f01883ab696bba86'

# These are reviewed native documents, not a catalogue of asserted identities.
REVIEWED = {
    'B3SA3': {'form': ('1095274', 'b8ecc4a439cda4fcd27807d6718fb9c09b378b2487b69428accf9a5c0972a376'),
              'notice': ('1095192', 'e517a1864c138c4ab9248477abb60e2ecd736148e32e0075149927cd2aba7bcf')},
    'EQMA3B': {'form': ('1060668', '1e810e35e39afaea566ae390ed04602ab477e74b1e688bee8689540146ce9b3a'),
               'notice': ('1034546', '0ae94be7ea9cbc56d2075d6504a2b1d8fc17c2a97424322314b0e5ef0a333ef4')},
}


class IdentityPending(ValueError):
    """Evidence is missing, contradictory, or insufficient for the requested use."""


def require(condition, reason):
    if not condition:
        raise IdentityPending('round2_identity_' + reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def norm(value):
    return ' '.join(unicodedata.normalize('NFKD', str(value or '')).encode(
        'ascii', 'ignore').decode().lower().split())


def day(value):
    if type(value) is date:
        return value
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'invalid_date')
    return date.fromisoformat(value)


def br_day(value):
    return datetime.strptime(''.join(str(value).split()), '%d/%m/%Y').date().isoformat()


def native_decimal(value):
    text = ''.join(str(value).split())
    require(bool(re.fullmatch(r'\d+,\d{1,14}', text)), 'native_decimal_ambiguous')
    return format(Decimal(text.replace(',', '.')), 'f')


@dataclass(frozen=True)
class SourceReceipt:
    file: str
    sha256: str
    bytes: int
    url: str | None
    retrieved_at: str | None
    role: str


@dataclass(frozen=True)
class NativeISINRow:
    isin: str
    gross_per_unit_decimal: str
    payment_date: str
    literal_isin: str
    literal_amount: str
    locator: str


@dataclass(frozen=True)
class NativeForm:
    cvm: int
    cnpj: str
    issuer_name: str
    sequence: str
    version: int
    received_on: str
    sent_on: str
    record_date: str
    approval_date: str
    protocol_ipe: str
    protocol_provento: str
    rows: tuple[NativeISINRow, ...]


@dataclass(frozen=True)
class IdentityObservation:
    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    isin: str
    observed_for_date: str
    available_on: str
    basis: str
    form_sequence: str
    form_version: int
    literal_proof: tuple[dict, ...]
    valid_from: str
    valid_until: str
    validity_basis: str = 'one_documented_entitlement_snapshot_only'


@dataclass(frozen=True)
class SecurityIdentityEvidence:
    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    isin: str
    as_of: str
    observations: tuple[IdentityObservation, ...]
    fca_observations: tuple[dict, ...]
    filing_selection_periods: tuple[dict, ...]
    sources: tuple[SourceReceipt, ...]
    window_blockers: tuple[str, ...]
    effective_code_change_date: str | None = None
    aliases: tuple[str, ...] = ()
    continuous_identity_window_verified: bool = False
    point_in_time_archive_verified: bool = False
    model_admission_granted: bool = False
    production_integration: bool = False

    def require_window(self, start, end, *, available_as_of=None):
        """Validate every used day, with receipt dates and no backward fill.

        available_as_of may impose an earlier knowledge cutoff, but cannot make
        a later filing usable at an earlier date within a historical window.
        """
        start, end = day(start), day(end)
        available = day(available_as_of) if available_as_of is not None else end
        require(start <= end <= day(self.as_of), 'requested_window_outside_audit')
        cursor = start
        observations = sorted(self.observations, key=lambda o: o.valid_from)
        for obs in observations:
            lo, hi, received = day(obs.valid_from), day(obs.valid_until), day(obs.available_on)
            if received > min(available, cursor) or hi < cursor or lo > cursor:
                continue
            cursor = hi + timedelta(days=1)
            if cursor > end:
                return self
        raise IdentityPending('round2_identity_whole_window_validity_not_documented')

    def to_dict(self):
        return asdict(self)


class _Reader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}

    def read(self, relative, digest=None, *, url=None, retrieved_at=None, role='primary_evidence', size=None):
        require(isinstance(relative, str) and relative.startswith('raw/')
                and '\\' not in relative and ':' not in relative
                and not set(relative.split('/')) & {'', '.', '..'}, 'unsafe_evidence_path')
        path = self.root / relative
        require(_safe(self.root, path) and path.is_file(), 'original_missing_or_unsafe')
        require(path.stat().st_size <= 100_000_000, 'original_size_limit')
        body = path.read_bytes()
        actual = sha(body)
        require(digest is None or actual == digest, 'original_hash_changed')
        require(size is None or len(body) == size, 'original_size_changed')
        if retrieved_at is not None:
            stamp = datetime.fromisoformat(retrieved_at)
            require(stamp.utcoffset() is not None and stamp <= datetime.now(timezone.utc),
                    'invalid_retrieval_timestamp')
        ref = SourceReceipt(relative, actual, len(body), url, retrieved_at, role)
        self.sources[(relative, actual, url, retrieved_at)] = ref
        return body, ref

    def source(self, source, role):
        return self.read(source['file'], source['sha256'], url=source['url'],
                         retrieved_at=source.get('retrieved_at'), role=role, size=source['bytes'])


def _index(reader):
    body, _ = reader.read(INDEX_MANIFEST, INDEX_SHA, role='pinned_CVM_IPE_manifest')
    sources = json.loads(body)
    require(sorted(s['year'] for s in sources) == list(range(2021, 2027)), 'six_IPE_indices_required')
    output = []
    for source in sources:
        year = source['year']
        require(source['url'] == f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip',
                'IPE_index_URL_changed')
        body, receipt = reader.source(source, 'primary_CVM_IPE_index')
        member = f'ipe_cia_aberta_{year}.csv'
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            require(archive.namelist().count(member) == 1
                    and archive.getinfo(member).file_size <= 100_000_000, 'IPE_member_limit')
            rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            for number, row in enumerate(rows, 2):
                if row.get('Codigo_CVM') not in ('16608', '21610'):
                    continue
                require(None not in row and all(v is not None for v in row.values()), 'IPE_row_misaligned')
                cvm = int(row['Codigo_CVM'])
                expected = next(cnpj for code, cnpj in TARGETS.values() if code == cvm)
                require(row['CNPJ_Companhia'] == expected, 'IPE_issuer_CNPJ_conflict')
                from .cvm_proventos_documents import document_id
                sequence, version = document_id(row['Link_Download'])
                require(version == int(row['Versao']), 'IPE_link_version_disagrees')
                day(row['Data_Entrega'])
                output.append({'record': row, 'sequence': sequence, 'version': version,
                    'source': asdict(receipt), 'locator': f'{member}:csv_record={number}'})
    return output


def _fca(reader):
    body, _ = reader.read(FCA_MANIFEST, FCA_SHA, role='pinned_CVM_FCA_manifest')
    sources = json.loads(body)['sources']
    filings = []
    years = []
    for source in sources:
        match = re.fullmatch(r'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_(\d{4})\.zip',
                             source['url'])
        require(match is not None, 'FCA_source_URL_changed')
        year = int(match[1])
        years.append(year)
        content, receipt = reader.source(source, 'primary_CVM_FCA_archive')
        masters, children = parse_target_fca(content, year)
        for master in masters:
            if int(master['CD_CVM']) not in (16608, 21610):
                continue
            cvm = int(master['CD_CVM'])
            require(master['CNPJ_CIA'] == next(c for v, c in TARGETS.values() if v == cvm),
                    'FCA_issuer_CNPJ_conflict')
            selected = [row for row in children if row['ID_Documento'] == master['ID_DOC']]
            filings.append({'year': year, 'master': master, 'children': selected,
                'source': asdict(receipt),
                'master_locator': f'fca_cia_aberta_{year}.csv:csv_record={master["_csv_record"]}',
                'security_member': f'fca_cia_aberta_valor_mobiliario_{year}.csv'})
    require(sorted(years) == list(range(2021, 2027)), 'six_FCA_archives_required')
    return filings


def parse_target_fca(content, year):
    """Validate the target issuers without relying on unrelated issuer rows."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        def rows(member, fields):
            require(archive.namelist().count(member) == 1
                    and archive.getinfo(member).file_size <= 20_000_000, 'FCA_member_limit')
            reader = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            require(set(fields) <= set(reader.fieldnames or ()), 'FCA_schema_changed')
            result = []
            for number, row in enumerate(reader, 2):
                require(None not in row and all(v is not None for v in row.values()), 'FCA_row_misaligned')
                result.append({**row, '_csv_record': number})
            return result
        masters = [r for r in rows(f'fca_cia_aberta_{year}.csv', MASTER_FIELDS)
                   if r['CD_CVM'].isdigit() and int(r['CD_CVM']) in (16608, 21610)]
        securities = rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv', SECURITY_FIELDS)
    by_id = {}
    for row in masters:
        require(row['VERSAO'].isdigit() and row['ID_DOC'] not in by_id, 'FCA_master_conflict')
        day(row['DT_REFER'])
        day(row['DT_RECEB'])
        by_id[row['ID_DOC']] = row
    cnpjs = {cnpj for _, cnpj in TARGETS.values()}
    children = [r for r in securities if r['CNPJ_Companhia'] in cnpjs or r['ID_Documento'] in by_id]
    for row in children:
        master = by_id.get(row['ID_Documento'])
        require(master is not None and row['CNPJ_Companhia'] == master['CNPJ_CIA']
                and row['Data_Referencia'] == master['DT_REFER'] and row['Versao'] == master['VERSAO'],
                'target_FCA_child_identity_conflict')
        for key in ('Data_Inicio_Negociacao', 'Data_Fim_Negociacao',
                    'Data_Inicio_Listagem', 'Data_Fim_Listagem'):
            if row[key]:
                day(row[key])
    return masters, children


def select_fca(filings, ticker, as_of):
    """Select the received master FIRST; never substitute a later child table."""
    when = day(as_of).isoformat()
    cvm, _ = TARGETS[ticker]
    eligible = [f for f in filings if int(f['master']['CD_CVM']) == cvm
                and f['master']['DT_RECEB'] <= when and f['master']['DT_REFER'] <= when]
    require(bool(eligible), 'no_received_FCA_filing')
    key = lambda f: (f['master']['DT_REFER'], int(f['master']['VERSAO']), f['master']['DT_RECEB'])
    newest = max(key(f) for f in eligible)
    selected = [f for f in eligible if key(f) == newest]
    require(len(selected) == 1, 'ambiguous_received_FCA_master')
    filing = selected[0]
    exact = [r for r in filing['children'] if r['Codigo_Negociacao'] == ticker]
    return {'as_of': when, 'filing': filing, 'exact': exact,
            'status': ('selected_filing_child_missing' if not filing['children'] else
                       'exact_code_not_reported' if not exact else 'exact_code_reported')}


def _ordinary(row):
    return (norm(row['Valor_Mobiliario']) == 'acoes ordinarias'
            and not row['Sigla_Classe_Acao_Preferencial']
            and not row['Classe_Acao_Preferencial'] and not row['Composicao_BDR_Unit'])


def _fca_proof(selection):
    filing = selection['filing']
    return {'as_of': selection['as_of'], 'status': selection['status'],
        'master': filing['master'], 'master_locator': filing['master_locator'],
        'source': filing['source'], 'reported_securities': [
            {'row': r, 'locator': f'{filing["security_member"]}:csv_record={r["_csv_record"]}'}
            for r in filing['children']],
        'listing_start_is_not_exact_code_effective_date': True}


def parse_native_form(body, indexed):
    """Read actual PDF cells; do not reconstruct ISINs from class-like endings."""
    import pdfplumber
    from .cvm_proventos_documents import metadata_value
    require(body.startswith(b'%PDF-') and len(body) <= 8_000_000, 'native_form_not_PDF')
    row = indexed['record']
    tables, locations = [], []
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        require(1 <= len(pdf.pages) <= 12, 'native_form_page_limit')
        for page_number, page in enumerate(pdf.pages, 1):
            require(len(page.chars) <= 150_000, 'native_form_text_limit')
            for table_number, table in enumerate(page.extract_tables()):
                tables.append(table)
                locations.append((page_number, table_number))
    issuer = metadata_value(tables, 'denominacao social')
    require(norm(issuer) == norm(row['Nome_Companhia']), 'native_form_issuer_changed')
    native_protocols = {m for table in tables for cells in table for cell in cells
                        for m in re.findall(r'\b(\d{6}IPE\d+-\d+)\b', str(cell or ''))}
    require(len(native_protocols) == 1 and int(next(iter(native_protocols))[:6]) == int(row['Codigo_CVM']),
            'native_form_CVM_protocol_conflict')
    version = metadata_value(tables, 'versao')
    require(str(version).strip() == row['Versao'], 'native_form_version_conflict')
    sent = br_day(metadata_value(tables, 'data envio'))
    require(sent <= row['Data_Entrega'], 'native_send_after_index_receipt')
    record = br_day(metadata_value(tables, 'ultimo dia de negociacao com direitos'))
    approval = br_day(metadata_value(tables, 'data aprovacao'))
    output = []
    for table_index, table in enumerate(tables):
        headers = [i for i, cells in enumerate(table) if len(cells) == 7
                   and 'codigo isin' in norm(cells[0]) and 'valor bruto' in norm(cells[1])
                   and 'data pagamento' in norm(cells[6])]
        for header in headers:
            for number, cells in enumerate(table[header + 1:], header + 1):
                require(len(cells) == 7, 'native_form_row_misaligned')
                isin = ''.join(str(cells[0]).split())
                require(bool(re.fullmatch(r'BR[A-Z0-9]{9}\d', isin)), 'native_ISIN_cell_invalid')
                page_number, table_number = locations[table_index]
                output.append(NativeISINRow(isin, native_decimal(cells[1]), br_day(cells[6]),
                    cells[0], cells[1], f'pdf_page={page_number}:table_index={table_number}:row_index={number}'))
    require(bool(output), 'native_ISIN_rows_missing')
    return NativeForm(int(row['Codigo_CVM']), row['CNPJ_Companhia'], issuer,
        indexed['sequence'], int(row['Versao']), row['Data_Entrega'], sent, record, approval,
        next(iter(native_protocols)), str(metadata_value(tables, 'protocolo provento')).strip(), tuple(output))


def _pdf_pages(body):
    from pypdf import PdfReader
    require(body.startswith(b'%PDF-') and len(body) <= 8_000_000, 'native_notice_not_PDF')
    reader = PdfReader(io.BytesIO(body))
    require(not reader.is_encrypted and 1 <= len(reader.pages) <= 12, 'native_notice_page_limit')
    return [page.extract_text() or '' for page in reader.pages]


def bind_eqma_class(form, notice_pages, notice_index):
    """Unique native amount/class join for the same issuer and record date.

    Equal ON/PNA amounts (2024 and 2025) are expressly non-identifying. Even a
    successful 2026 join establishes no earlier code or ISIN validity interval.
    """
    record = notice_index['record']
    require(form.cvm == int(record['Codigo_CVM']) == 16608
            and form.cnpj == record['CNPJ_Companhia'] == TARGETS['EQMA3B'][1], 'notice_issuer_conflict')
    text = norm(' '.join(notice_pages))
    require(form.cnpj in ''.join(text.split()) and 'aviso aos acionistas' in text,
            'notice_native_CNPJ_missing')
    require(record['Data_Referencia'] == form.record_date == form.approval_date
            and 'data-base de 28 de abril de 2026' in text
            and form.record_date == '2026-04-28', 'notice_record_date_conflict')
    amounts = []
    for page_number, page in enumerate(notice_pages, 1):
        normalized = norm(page)
        for m in re.finditer(r'r\$\s*(\d+,\d+)\s+por acao (on|pna|pnb)\b', normalized):
            amounts.append({'share_class': m[2].upper(), 'decimal': native_decimal(m[1]),
                            'literal': m[0], 'locator': f'pdf_page={page_number}:normalized_offset={m.start()}'})
    require(len(amounts) == 3 and {a['share_class'] for a in amounts} == {'ON', 'PNA', 'PNB'},
            'native_class_amounts_incomplete')
    ordinary = next(a for a in amounts if a['share_class'] == 'ON')
    matches = [r for r in form.rows if Decimal(r.gross_per_unit_decimal) == Decimal(ordinary['decimal'])]
    same_classes = [a for a in amounts if Decimal(a['decimal']) == Decimal(ordinary['decimal'])]
    require(len(matches) == len(same_classes) == 1, 'ordinary_ISIN_amount_join_ambiguous')
    require(len(form.rows) == 3 and len({r.isin for r in form.rows}) == 3
            and all(sum(Decimal(r.gross_per_unit_decimal) == Decimal(a['decimal'])
                        for r in form.rows) == 1 for a in amounts), 'full_class_ISIN_join_not_bijective')
    return matches[0], ordinary, amounts


class PrimarySecurityIdentityAdapter:
    """Frozen-source offline adapter. No B3-shaped return value or writes."""
    def __init__(self, folder):
        self.reader = _Reader(folder)
        self.index = _index(self.reader)
        self.filings = _fca(self.reader)

    def _document(self, ticker, kind, cutoff, *, reviewed=None):
        sequence, digest = reviewed or REVIEWED[ticker][kind]
        matches = [r for r in self.index if r['sequence'] == sequence]
        require(len(matches) == 1, 'reviewed_document_index_ambiguous')
        indexed = matches[0]
        record = indexed['record']
        require(int(record['Codigo_CVM']) == TARGETS[ticker][0]
                and record['Data_Entrega'] <= cutoff.isoformat(), 'reviewed_document_not_received_by_cutoff')
        url = record['Link_Download']
        receipt_file = f'{BASE}/response-{sha(url.encode())}.json'
        if (self.reader.root / receipt_file).is_file():
            body, _ = self.reader.read(receipt_file, role='public_HTTP_receipt')
            source = json.loads(body)
            require(source['scope'] == 'round2-identity' and source['retries'] == 0
                    and source['status'] == 200 and source['error'] is None
                    and source['url'] == url and source['sha256'] == digest
                    and source['file'] == f'{BASE}/body-{digest}.bin', 'public_HTTP_receipt_conflict')
            content, ref = self.reader.source(source, 'native_CVM_' + kind)
        else:
            body, _ = self.reader.read(OLD_REPORT, OLD_REPORT_SHA, role='pinned_existing_document_receipts')
            found = [r for r in json.loads(body)['results'] if r.get('url') == url and r.get('sha256') == digest]
            require(len(found) == 1, 'existing_native_document_receipt_missing')
            source = found[0]
            require(source['sequence'] == sequence and source['version'] == indexed['version']
                    and source['published_at'] == record['Data_Entrega'], 'existing_native_receipt_disagrees')
            content, ref = self.reader.read(source['file'], digest, url=url,
                retrieved_at=source.get('retrieved_at'), role='native_CVM_' + kind, size=source['bytes'])
        return content, indexed, ref

    def evidence(self, ticker, cutoff=CUTOFF, *, window_start=WINDOW_START, identity=None):
        require(ticker in TARGETS, 'outside_disjoint_target_scope')
        cutoff, window_start = day(cutoff), day(window_start)
        require(WINDOW_START <= window_start <= cutoff <= CUTOFF, 'unreviewed_audit_window')
        cvm, cnpj = TARGETS[ticker]
        body, indexed, form_ref = self._document(ticker, 'form', cutoff)
        form = parse_native_form(body, indexed)
        notice_body, notice_index, notice_ref = self._document(ticker, 'notice', cutoff)
        pages = _pdf_pages(notice_body)
        require(cnpj in ''.join(' '.join(pages).split()), 'notice_CNPJ_disagrees')
        # Select the FCA actually received at the entitlement date, not a later
        # annual revision whose child table happens to survive in today's ZIP.
        selected = select_fca(self.filings, ticker, form.record_date)
        exact = selected['exact']
        require(len(exact) == 1 and _ordinary(exact[0]), 'exact_received_FCA_ordinary_row_missing')
        current = select_fca(self.filings, ticker, cutoff)
        require(len(current['exact']) == 1 and _ordinary(current['exact'][0]),
                'latest_received_FCA_ordinary_identity_changed')
        fca_row = exact[0]
        require(not fca_row['Data_Fim_Negociacao'] or fca_row['Data_Fim_Negociacao'] >= form.record_date,
                'FCA_reported_trading_episode_ended')
        proof = [{'source': asdict(form_ref), 'index_source': indexed['source'],
                  'index_locator': indexed['locator'], 'index_record': indexed['record'],
                  'native_form': asdict(form)},
                 {'source': selected['filing']['source'], 'FCA': _fca_proof(selected)}]
        if ticker == 'EQMA3B':
            isin_row, literal, class_amounts = bind_eqma_class(form, pages, notice_index)
            isin = isin_row.isin
            basis = 'unique_same_issuer_record_date_decimal_ON_to_native_ISIN_join'
            proof.append({'source': asdict(notice_ref), 'index_locator': notice_index['locator'],
                          'index_record': notice_index['record'], 'ordinary_class_literal': literal,
                          'all_native_class_amounts': class_amounts, 'ISIN_row': asdict(isin_row)})
        else:
            equities = [r for r in selected['filing']['children']
                        if norm(r['Valor_Mobiliario']) in ('acoes ordinarias', 'acoes preferenciais', 'units')]
            require(len(equities) == 1 and equities[0] == fca_row
                    and len({r.isin for r in form.rows}) == 1, 'sole_FCA_equity_native_ISIN_join_ambiguous')
            require(notice_index['record']['Protocolo_Entrega'] == form.protocol_ipe
                    and parse_qs(urlsplit(notice_index['record']['Link_Download']).query)['numProtocolo']
                    == [form.protocol_provento], 'native_notice_form_protocol_disagrees')
            text = norm(pages[0])
            require('posicao acionaria de 22 de setembro de 2026' in text
                    and all(r.gross_per_unit_decimal.replace('.', ',') in text for r in form.rows),
                    'native_notice_form_amounts_or_record_date_disagree')
            isin = form.rows[0].isin
            basis = 'sole_literal_FCA_equity_and_native_ISIN_same_protocol_per_share_notice'
            proof.append({'source': asdict(notice_ref), 'index_record': notice_index['record'],
                          'index_locator': notice_index['locator'], 'locator': 'pdf_page=1',
                          'literal_notice': pages[0], 'join_is_documentary_inference': True})
        if identity is not None:
            require(identity.get('ticker') == ticker and type(identity.get('cvm')) is int
                    and identity['cvm'] == cvm and identity.get('share_class') == 'ON'
                    and identity.get('cnpj', cnpj) == cnpj and identity.get('isin') in (None, isin),
                    'caller_exact_identity_disagrees')
        available = max(form.received_on, notice_index['record']['Data_Entrega'],
                        selected['filing']['master']['DT_RECEB'])
        observation = IdentityObservation(ticker, cvm, cnpj, 'ON', isin, form.record_date, available,
            basis, form.sequence, form.version, tuple(proof), form.record_date, form.record_date)
        annual = tuple(_fca_proof(select_fca(self.filings, ticker, min(date(y, 12, 31), cutoff)))
                       for y in range(window_start.year, cutoff.year + 1))
        boundaries = {window_start, cutoff + timedelta(days=1)}
        for filing in self.filings:
            if int(filing['master']['CD_CVM']) == cvm:
                for key in ('DT_RECEB', 'DT_REFER'):
                    when = day(filing['master'][key])
                    if window_start < when <= cutoff:
                        boundaries.add(when)
        periods, blockers = [], ['native_ISIN_continuous_validity_interval_not_documented']
        points = sorted(boundaries)
        for lo, following in zip(points, points[1:]):
            chosen = select_fca(self.filings, ticker, lo)
            if chosen['status'] != 'exact_code_reported':
                blockers.append('FCA_' + chosen['status'])
            periods.append({'filing_selection_from': lo.isoformat(),
                'filing_selection_until': (following - timedelta(days=1)).isoformat(),
                'status': chosen['status'], 'master': chosen['filing']['master'],
                'reported_codes': [r['Codigo_Negociacao'] for r in chosen['filing']['children']],
                'not_a_legal_code_validity_interval': True})
        if ticker == 'EQMA3B':
            blockers.append('ENMA3B_to_EQMA3B_exact_code_change_date_and_ISIN_continuity_unproven')
        return deepcopy(SecurityIdentityEvidence(ticker, cvm, cnpj, 'ON', isin, cutoff.isoformat(),
            (observation,), annual, tuple(periods), tuple(self.reader.sources.values()), tuple(sorted(set(blockers)))))


def inspect_raw_files(folder, ticker, cutoff=CUTOFF, window_start=WINDOW_START):
    """File/content audit, never a declaration of zero cash or raw-price approval."""
    require(ticker in TARGETS, 'outside_disjoint_target_scope')
    reader = _Reader(folder)
    root = reader.root
    daily_name = f'raw/inventory/daily-prices/{ticker}.json'
    payment_name = f'raw/inventory/payments/{ticker}.json'
    result = {'ticker': ticker, 'independent_cash_pages': [], 'independent_price_anchors': [],
              'payment_calendar_html_exists': (root / f'raw/expanded/{ticker}-payments.html').is_file(),
              'no_absence_interpreted_as_zero': True, 'identity_alone_grants_admission': False}
    for path in (root / 'raw/expanded').glob(f'{ticker}-b3-cash-*.json'):
        body, receipt = reader.read(path.relative_to(root).as_posix(), role='existing_independent_cash')
        data = json.loads(body)
        result['independent_cash_pages'].append({'source': asdict(receipt), 'page': data.get('page')})
        result['independent_price_anchors'].extend([
            {'date_literal': r['dateClosingPricePriorExDate'],
             'price_literal': r['closingPricePriorExDate'], 'source': asdict(receipt)}
            for r in data.get('results', []) if r.get('typeStock', '').strip() == 'ON'
            and r.get('closingPricePriorExDate') not in (None, '', '0', '0,00')
            and r.get('dateClosingPricePriorExDate') not in (None, '', '31/12/9999')])
    if (root / daily_name).is_file():
        summary_name = 'raw/inventory/daily-summary.json'
        source = {}
        if (root / summary_name).is_file():
            summary, _ = reader.read(summary_name, role='existing_daily_price_receipts')
            source = json.loads(summary).get('instruments', {}).get(ticker, {}).get('source', {})
            require(not source or source['file'] == daily_name, 'daily_receipt_path_conflict')
        content, receipt = reader.read(daily_name, source.get('sha256'), url=source.get('url'),
            retrieved_at=source.get('retrieved_at'), role='existing_daily_price_response')
        chart = json.loads(content)['chart']
        require(not chart.get('error') and len(chart['result']) == 1, 'daily_price_response_invalid')
        data = chart['result'][0]
        require(data['meta']['symbol'] == ticker + '.SA' and data['meta']['currency'] == 'BRL',
                'daily_price_symbol_or_currency_conflict')
        stamps = data['timestamp']
        closes = data['indicators']['quote'][0]['close']
        volumes = data['indicators']['quote'][0]['volume']
        require(len(stamps) == len(closes) == len(volumes), 'daily_price_alignment_invalid')
        observed = [(datetime.fromtimestamp(stamp, timezone.utc).date(), close)
                    for stamp, close in zip(stamps, closes)
                    if type(close) in (float, int) and math.isfinite(close) and close > 0]
        require(len({d for d, _ in observed}) == len(observed), 'duplicate_daily_dates')
        in_window = [(d, v) for d, v in observed if day(window_start).replace(day=1) <= d <= day(cutoff)]
        months = sorted({d.strftime('%Y-%m') for d, _ in in_window})
        from .broad_stocks import split_price_history
        try:
            prices, actions = split_price_history(content, ticker, day(cutoff), allow_recent_actions=True)
            existing_gate = {'status': 'passed_existing_61_month_anchor_parser',
                             'parsed_months': len(prices), 'provider_reported_actions': actions}
        except ValueError as error:
            existing_gate = {'status': 'blocked', 'reason': str(error)}
        result['daily_prices'] = {'source': asdict(receipt), 'provider_symbol': data['meta']['symbol'],
            'retrieval_time_unknown': receipt.retrieved_at is None, 'positive_close_count': len(observed),
            'zero_volume_rows_in_provider_file': sum(v == 0 for v in volumes),
            'provider_rows_are_not_all_verified_executed_trades': True,
            'first_observed_day': min(d for d, _ in observed).isoformat(),
            'last_observed_day': max(d for d, _ in observed).isoformat(),
            'model_window_observed_months': months, 'model_window_observed_month_count': len(months),
            'existing_price_parser': existing_gate, 'native_unadjusted_regime_verified': False,
            'adjusted_close_used_as_raw': False}
    else:
        result['daily_prices'] = {'status': 'missing'}
    if (root / payment_name).is_file():
        content, receipt = reader.read(payment_name, role='existing_payment_bundle')
        payload = json.loads(content)
        primary = payload.get('primary_sources') or {}
        result['payment_bundle'] = {'source': asdict(receipt),
            'selected_cash_source': payload.get('selected_cash_source'),
            'primary_status': primary.get('status'), 'cached_event_row_count': len(primary.get('events', [])),
            'actual_distribution_count': None,
            'document_reference_count': len(primary.get('documents', [])),
            'history_complete': primary.get('report', {}).get('history_complete'),
            'document_references_are_not_paid_cash': True}
    else:
        result['payment_bundle'] = {'status': 'missing'}
    result['expected_next_current_bridge_blocker_after_parent_identity_integration'] = (
        'independent_cash_cache_missing' if not result['independent_cash_pages'] else
        'cash_pagination_calendar_price_and_share_action_reconciliation_still_required')
    result['further_required'] = ['whole_window_identity_validity', 'exact_class_paid_cash_calendar',
        'independent_cash_and_raw_price_anchors', 'share_action_reconciliation', 'annual_financial_gates']
    return result


def supplemental_forms(adapter):
    """Earlier native observations, explicitly not retroactive class bindings."""
    reviews = [
        ('B3SA3', '589310', '0e7b88cf17f6583663efe099896d009ae6c9ccb15673c006fdcf846d9b42d733',
         'native_ISIN_observed_in_2023_not_proof_of_2021_validity'),
        ('EQMA3B', '781130', 'd67eb9a90a72ffc3cfbce103c9d088149fbf5c8ca2e45f51565040c1a45ae24c',
         'all_three_ISIN_amounts_equal_no_unique_ordinary_class_join'),
        ('EQMA3B', '962948', '82201c9b50f09dce12497dab7aea4bd75dee555992cb9fb52e7c4768ec8845ba',
         'ON_and_PNA_notice_amounts_equal_no_unique_ordinary_class_join'),
    ]
    result = []
    for ticker, sequence, digest, qualification in reviews:
        body, indexed, source = adapter._document(ticker, 'form', CUTOFF, reviewed=(sequence, digest))
        item = {'ticker': ticker, 'native_form': asdict(parse_native_form(body, indexed)),
            'index_record': indexed['record'], 'index_locator': indexed['locator'],
            'index_source': indexed['source'], 'source': asdict(source), 'qualification': qualification,
            'used_to_fill_identity_window': False, 'native_form_states_share_class': False}
        notices = {
            '781130': ('780822', 'bcd8b8c97285a0089bfa8253d3a52b39cc67cfc76c713949d8ddaf499df6ce3e'),
            '962948': ('962899', '3f9b45a8c158f88c4450b3a624460170031d47d40d4743ab4ff19691fb988017'),
        }
        if sequence in notices:
            notice, notice_index, notice_source = adapter._document(
                ticker, 'notice', CUTOFF, reviewed=notices[sequence])
            item['class_notice'] = {'source': asdict(notice_source),
                'index_record': notice_index['record'], 'index_locator': notice_index['locator'],
                'literal_pages': _pdf_pages(notice)}
        result.append(item)
    return result


def collect_documents(folder, sequences):
    """Only existing exact CVM-published links; no endpoint/alias construction."""
    reader = _Reader(folder)
    index = _index(reader)
    selected = []
    for sequence in sequences:
        rows = [r for r in index if r['sequence'] == sequence]
        require(len(rows) == 1, 'collection_sequence_not_unique_in_native_index')
        selected.append(rows[0])
    collector = PublicSourceCollector(folder, 'round2-identity')
    return [collector.fetch(r['record']['Link_Download'], discovery={
        'kind': 'cached_CVM_IPE_exact_issuer_identity', 'index': r['source'],
        'locator': r['locator'], 'native_record': r['record']}) for r in selected]


def audit(folder=ROOT):
    adapter = PrimarySecurityIdentityAdapter(folder)
    evidence = {ticker: adapter.evidence(ticker) for ticker in TARGETS}
    receipts = []
    for path in (Path(folder) / BASE).glob('response-*.json'):
        payload = json.loads(path.read_bytes())
        require(urlsplit(payload['url']).hostname in ('www.rad.cvm.gov.br', 'api.mziq.com')
                and payload['scope'] == 'round2-identity' and payload['retries'] == 0,
                'owned_collection_scope_disagrees')
        body, _ = adapter.reader.source(payload, 'owned_HTTP_original')
        kind = ('robots_policy' if urlsplit(payload['url']).path == '/robots.txt' else
                'native_PDF' if body.startswith(b'%PDF-') else 'non_PDF_not_accepted_as_native_form')
        receipts.append({**{k: payload.get(k) for k in ('url', 'status', 'error', 'file', 'sha256',
                                                     'bytes', 'retrieved_at', 'scope', 'retries')},
                         'content_kind': kind})
    return {'policy': POLICY, 'cutoff': CUTOFF.isoformat(), 'window_start': WINDOW_START.isoformat(),
        'identities': {ticker: item.to_dict() for ticker, item in evidence.items()},
        'earlier_native_form_observations': supplemental_forms(adapter),
        'raw_file_audit': {ticker: inspect_raw_files(folder, ticker) for ticker in TARGETS},
        'HTTP_receipts_in_owned_scope': receipts,
        'native_ISIN_recovery_is_not_continuous_validity_or_model_admission': True,
        'annual_FCA_reference_and_listing_dates_are_not_code_change_dates': True,
        'retrieval_is_not_publication_or_point_in_time_archive': True,
        'missing_superseded_FCA_child_tables_are_not_backfilled': True,
        'integration_contract': {
            'adapter': 'recommender.round2_security_identity.PrimarySecurityIdentityAdapter',
            'entry_point': 'evidence(ticker, cutoff, window_start=..., identity=...)',
            'return_type': 'SecurityIdentityEvidence',
            'use': 'documentary_identity_and_native_ISIN_inspection_only_until_window_validity_proven',
            'required_before_historical_identity_use': 'evidence.require_window(start, end)',
            'corporate_actions_or_cash_or_prices_supplied': False,
            'parent_must_not_recast_as_B3_detail_or_bypass_independent_evidence_gates': True},
        'B3_network_requests': 0, 'denied_origin_requests': 0,
        'integration_performed': False, 'model_admissions': [], 'pointers_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--fetch-sequences', nargs='*', default=[])
    parser.add_argument('--write-report', action='store_true')
    args = parser.parse_args()
    if args.fetch_sequences:
        collect_documents(args.root, args.fetch_sequences)
    report = audit(args.root)
    if args.write_report:
        body = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
        base = args.root.resolve() / BASE
        require(_safe(args.root.resolve(), base), 'unsafe_report_destination')
        base.mkdir(parents=True, exist_ok=True)
        path = base / f'identity-report-{sha(body)}.json'
        _save(path, body)
        print(path)
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
