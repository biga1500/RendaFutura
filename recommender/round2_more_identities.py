"""Primary, typed identity recovery for seven disjoint pending stock codes.

No B3-shaped cache is written. Current identity and dated historical cash are
separate claims; an unchanged five-year ticker/ISIN is not an admission gate.
Only exact cached CVM index links may be collected, never B3 or api.mziq.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import zipfile

from . import round2_security_identity as primary
from .bulk_pending_sources import PublicSourceCollector, _save
from .round2_b3_eqma_recovery import decimal_cell, exact_date, norm
from .security_registration import MASTER_FIELDS, SECURITY_FIELDS


ROOT = primary.ROOT
BASE = primary.BASE
ISSUER_RECEIPTS = BASE + '/identity-more-issuer'
CUTOFF = date(2026, 9, 30)
START = date(2021, 9, 30)
TARGETS = {
    'BAUH4': (1570, '95.426.862/0001-97', 'PNA'),
    'BMIN3': (1309, '34.169.557/0001-72', 'ON'),
    'BMIN4': (1309, '34.169.557/0001-72', 'PN'),
    'CEGR3': (16616, '33.938.119/0001-69', 'ON'),
    'GPAR3': (21393, '08.560.444/0001-93', 'ON'),
    'PEAB3': (18708, '01.938.783/0001-11', 'ON'),
    'PEAB4': (18708, '01.938.783/0001-11', 'PN'),
}
ISSUERS = {cvm: cnpj for cvm, cnpj, _ in TARGETS.values()}
CLASS_ORIGINAL_PINS = {
    '1009459': '38a4a9918fce3afaa612b21f48174a9b462322b077dbddd6a25bc967b136f8eb',
    '937510': '18409556fa50526586ddb6da9688557833ea38b9eee8351b7d12ebb6b858c0aa',
    '938852': '9ae2b5c0e89f31d58b350cc2e11bb7ce9cdad367f8429ddb7c6a15694aaabdbe',
}


class EvidencePending(ValueError):
    """A missing observation is not a finding of issuer ineligibility."""


def require(value, reason):
    if not value:
        raise EvidencePending('identity_more_' + reason)


@dataclass(frozen=True)
class CurrentSecurityBinding:
    ticker: str
    cvm: int
    cnpj: str
    share_class: str
    isin: str
    as_of: str
    native_observation_date: str
    evidence: tuple[dict, ...]
    latest_native_ISIN_observation_date: str
    aliases: tuple[str, ...] = ()
    unchanged_five_year_identity_required: bool = False
    exchange_trading_verified: bool = False
    point_in_time_archive_verified: bool = False
    model_admission: bool = False


def fca_share_class(row):
    require(not row['Composicao_BDR_Unit'], 'unit_not_supported')
    if norm(row['Valor_Mobiliario']) == 'acoes ordinarias':
        require(not row['Sigla_Classe_Acao_Preferencial'] and not row['Classe_Acao_Preferencial'],
                'ordinary_with_preferred_class')
        return 'ON'
    require(norm(row['Valor_Mobiliario']) == 'acoes preferenciais', 'not_literal_equity')
    value = row['Sigla_Classe_Acao_Preferencial']
    require(value in ('', 'PNA', 'PNB', 'PNC', 'PND'), 'preferred_class_unresolved')
    require(bool(value) or not row['Classe_Acao_Preferencial'], 'preferred_class_description_unresolved')
    if value:
        require(norm(row['Classe_Acao_Preferencial']) == 'preferencial classe ' + value[-1].lower(),
                'preferred_class_description_conflict')
    return value or 'PN'


def parse_fca(content, year):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        def rows(member, fields):
            require(archive.namelist().count(member) == 1
                    and archive.getinfo(member).file_size <= 20_000_000, 'FCA_member_bounds')
            records = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            require(set(fields) <= set(records.fieldnames or ()), 'FCA_schema')
            output = []
            for number, row in enumerate(records, 2):
                require(None not in row and all(v is not None for v in row.values()), 'FCA_columns')
                output.append({**row, '_csv_record': number})
            return output
        masters = [r for r in rows(f'fca_cia_aberta_{year}.csv', MASTER_FIELDS)
                   if r['CD_CVM'].isdigit() and int(r['CD_CVM']) in ISSUERS]
        securities = rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv', SECURITY_FIELDS)
    by_id = {}
    for row in masters:
        require(row['ID_DOC'] not in by_id and row['VERSAO'].isdigit(), 'FCA_duplicate_master')
        require(row['CNPJ_CIA'] == ISSUERS[int(row['CD_CVM'])], 'FCA_CNPJ')
        primary.day(row['DT_RECEB']); primary.day(row['DT_REFER'])
        by_id[row['ID_DOC']] = row
    children = [r for r in securities if r['ID_Documento'] in by_id
                or r['CNPJ_Companhia'] in ISSUERS.values()]
    for row in children:
        m = by_id.get(row['ID_Documento'])
        require(m is not None and row['CNPJ_Companhia'] == m['CNPJ_CIA']
                and row['Versao'] == m['VERSAO'] and row['Data_Referencia'] == m['DT_REFER'],
                'FCA_child_master_conflict')
    return masters, children


def select_fca(filings, cvm, as_of):
    when = primary.day(as_of).isoformat()
    eligible = [f for f in filings if int(f['master']['CD_CVM']) == cvm
                and f['master']['DT_RECEB'] <= when and f['master']['DT_REFER'] <= when]
    require(eligible, 'received_FCA_missing')
    key = lambda f: (f['master']['DT_REFER'], int(f['master']['VERSAO']), f['master']['DT_RECEB'])
    latest = max(map(key, eligible))
    chosen = [f for f in eligible if key(f) == latest]
    require(len(chosen) == 1, 'received_FCA_ambiguous')
    require(chosen[0]['children'], 'selected_FCA_children_missing_no_backfill')
    return chosen[0]


class Corpus:
    def __init__(self, folder=ROOT):
        self.reader = primary._Reader(folder)
        self.root = self.reader.root
        self.filings, self.index, self._documents = [], [], {}
        body, _ = self.reader.read(primary.FCA_MANIFEST, primary.FCA_SHA, role='pinned_FCA_manifest')
        sources = json.loads(body)['sources']
        years = []
        for source in sources:
            m = re.fullmatch(r'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_(\d{4})\.zip', source['url'])
            require(m is not None, 'FCA_URL')
            year = int(m[1]); years.append(year)
            content, receipt = self.reader.source(source, 'primary_CVM_FCA_archive')
            masters, children = parse_fca(content, year)
            for master in masters:
                self.filings.append({'year': year, 'master': master,
                    'children': [r for r in children if r['ID_Documento'] == master['ID_DOC']],
                    'source': asdict(receipt),
                    'master_locator': f'fca_cia_aberta_{year}.csv:csv_record={master["_csv_record"]}',
                    'security_member': f'fca_cia_aberta_valor_mobiliario_{year}.csv'})
        require(sorted(years) == list(range(2021, 2027)), 'six_FCA_archives_required')
        body, _ = self.reader.read(primary.INDEX_MANIFEST, primary.INDEX_SHA, role='pinned_IPE_manifest')
        for source in json.loads(body):
            year = source['year']
            require(source['url'] == f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip', 'IPE_URL')
            content, receipt = self.reader.source(source, 'primary_CVM_IPE_index')
            member = f'ipe_cia_aberta_{year}.csv'
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                require(archive.getinfo(member).file_size <= 100_000_000, 'IPE_member_bounds')
                records = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
                for number, row in enumerate(records, 2):
                    if not row['Codigo_CVM'].isdigit() or int(row['Codigo_CVM']) not in ISSUERS:
                        continue
                    require(None not in row and all(v is not None for v in row.values()), 'IPE_columns')
                    require(row['CNPJ_Companhia'] == ISSUERS[int(row['Codigo_CVM'])], 'IPE_CNPJ')
                    from .cvm_proventos_documents import document_id
                    sequence, version = document_id(row['Link_Download'])
                    require(version == int(row['Versao']), 'IPE_version')
                    self.index.append({'record': row, 'sequence': sequence, 'version': version,
                        'source': asdict(receipt), 'locator': f'{member}:csv_record={number}'})
        body, _ = self.reader.read(primary.OLD_REPORT, primary.OLD_REPORT_SHA, role='pinned_existing_CVM_receipts')
        self.older = json.loads(body)['results']

    def fca(self, ticker, as_of=CUTOFF):
        require(ticker in TARGETS, 'target_outside_scope')
        cvm, cnpj, share = TARGETS[ticker]
        filing = select_fca(self.filings, cvm, as_of)
        exact = [r for r in filing['children'] if r['Codigo_Negociacao'] == ticker]
        require(len(exact) == 1 and exact[0]['CNPJ_Companhia'] == cnpj
                and fca_share_class(exact[0]) == share, 'exact_received_FCA_code_class_missing')
        require(not exact[0]['Data_Fim_Negociacao']
                or exact[0]['Data_Fim_Negociacao'] >= primary.day(as_of).isoformat(), 'FCA_episode_ended')
        return {'as_of': primary.day(as_of).isoformat(), 'master': filing['master'],
            'master_locator': filing['master_locator'], 'row': exact[0],
            'locator': f'{filing["security_member"]}:csv_record={exact[0]["_csv_record"]}',
            'reported_equities': [r for r in filing['children'] if norm(r['Valor_Mobiliario']) in ('acoes ordinarias', 'acoes preferenciais')],
            'source': filing['source'], 'listing_start_is_not_code_effective_date': True}

    def indexed(self, sequence):
        matches = [r for r in self.index if r['sequence'] == str(sequence)]
        require(len(matches) == 1, 'unique_IPE_sequence_missing')
        require(matches[0]['record']['Data_Entrega'] <= CUTOFF.isoformat(), 'received_after_cutoff')
        return matches[0]

    def receipt(self, indexed):
        url = indexed['record']['Link_Download']
        old = [r for r in self.older if r.get('url') == url and r.get('file')]
        if old:
            require(len(old) == 1 and old[0]['version'] == indexed['version']
                    and old[0]['published_at'] == indexed['record']['Data_Entrega'], 'old_receipt_conflict')
            return old[0]
        path = self.root / f'{BASE}/response-{hashlib.sha256(url.encode()).hexdigest()}.json'
        require(path.is_file(), 'primary_original_not_cached')
        content, _ = self.reader.read(path.relative_to(self.root).as_posix(), role='actual_HTTP_receipt')
        receipt = json.loads(content)
        require(receipt['url'] == url and receipt['status'] == 200 and receipt['error'] is None
                and receipt['scope'] == 'round2-identity' and receipt['retries'] == 0, 'HTTP_receipt_invalid')
        return receipt

    def document(self, sequence):
        sequence = str(sequence)
        if sequence in self._documents:
            return deepcopy(self._documents[sequence])
        indexed = self.indexed(sequence)
        receipt = self.receipt(indexed)
        body, source = self.reader.read(receipt['file'], receipt['sha256'], size=receipt['bytes'],
            url=receipt['url'], retrieved_at=receipt.get('retrieved_at'), role='native_CVM_document')
        require(sequence not in CLASS_ORIGINAL_PINS or source.sha256 == CLASS_ORIGINAL_PINS[sequence],
                'reviewed_class_original_hash_changed')
        require(body.startswith(b'%PDF-'), 'native_PDF_required')
        from pypdf import PdfReader
        import pdfplumber
        pdf = PdfReader(io.BytesIO(body))
        require(not pdf.is_encrypted and 1 <= len(pdf.pages) <= 200, 'native_PDF_bounds')
        document = {'sequence': sequence, 'index': indexed, 'source': asdict(source),
            'format': 'PDF', 'pages': [p.extract_text() or '' for p in pdf.pages], 'tables': []}
        with pdfplumber.open(io.BytesIO(body)) as table_pdf:
            for number, page in enumerate(table_pdf.pages, 1):
                if number > 20:
                    break
                for n, cells in enumerate(page.extract_tables()):
                    document['tables'].append({'page': number, 'table_index': n, 'cells': cells})
        self._documents[sequence] = document
        return deepcopy(document)


def cash_candidates(corpus):
    return [r for r in corpus.index if r['record']['Data_Entrega'] <= CUTOFF.isoformat()
            and (norm(r['record']['Categoria']) == 'relatorio proventos'
            or ('aviso' in norm(r['record']['Categoria']) and any(
                word in norm(r['record']['Assunto']) for word in ('dividend', 'juros', 'provento'))))]


def read_form(corpus, document):
    """Read native cells, validating the CVM protocol and the FCA issuer name."""
    from .cvm_proventos_documents import metadata_value
    indexed = document['index']['record']
    cvm = int(indexed['Codigo_CVM'])
    tables = [t['cells'] for t in document['tables']]
    issuer = metadata_value(tables, 'denominacao social')
    names = {norm(f['master']['DENOM_CIA']) for f in corpus.filings if int(f['master']['CD_CVM']) == cvm}
    require(norm(issuer) in names, 'native_form_FCA_issuer_name_conflict')
    protocols = {m for table in tables for row in table for cell in row
                 for m in re.findall(r'\b(\d{6}IPE\d+-\d+)\b', str(cell or ''))}
    require(len(protocols) == 1 and int(next(iter(protocols))[:6]) == cvm, 'native_form_CVM_protocol_conflict')
    require(str(metadata_value(tables, 'versao')).strip() == indexed['Versao'], 'native_form_version_conflict')
    def native_day(key):
        value = str(metadata_value(tables, key)).strip()
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M', value):
            return datetime.strptime(value, '%m/%d/%Y %I:%M:%S %p').date().isoformat()
        return primary.br_day(value)
    sent = native_day('data envio')
    require(sent <= indexed['Data_Entrega'], 'native_send_after_receipt')
    approval = native_day('data aprovacao')
    record = native_day('ultimo dia de negociacao com direitos')
    rows, installments = [], []
    for table in document['tables']:
        cells = table['cells']
        for header, native in enumerate(cells):
            if len(native) == 7 and 'codigo isin' in norm(native[0]) and 'valor bruto' in norm(native[1]):
                for line, values in enumerate(cells[header + 1:], header + 1):
                    require(len(values) == 7, 'native_form_row_columns')
                    isin = ''.join(str(values[0]).split())
                    require(re.fullmatch(r'BR[A-Z0-9]{9}\d', isin), 'native_ISIN_missing')
                    rows.append({'isin': isin, 'gross_per_share_decimal': decimal_cell(values[1]),
                        'payment_date': primary.br_day(values[6]) if values[6] else None,
                        'payment_form': norm(values[5]), 'update_reported': norm(values[4]),
                        'literal_cells': values,
                        'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': line}})
        if cells and len(cells[0]) == 2 and 'dia de pagamento das parcelas' in norm(cells[0][0]):
            for line, values in enumerate(cells[1:], 1):
                installments.append({'payment_date': primary.br_day(values[0]),
                    'gross_per_share_decimal': decimal_cell(values[1]), 'literal_cells': values,
                    'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': line}})
    require(rows, 'native_form_rows_missing')
    return {'sequence': document['sequence'], 'cvm': cvm, 'cnpj': ISSUERS[cvm],
        'issuer_name': issuer, 'version': int(indexed['Versao']), 'received_on': indexed['Data_Entrega'],
        'sent_on': sent, 'approval_date': approval, 'record_date': record,
        'protocol_provento': str(metadata_value(tables, 'protocolo provento')).strip(),
        'native_IPE_protocol': next(iter(protocols)), 'rows': rows, 'installments': installments,
        'source': document['source'], 'index': document['index']}


def inspect_cash_documents(corpus):
    output = []
    for indexed in cash_candidates(corpus):
        seq = indexed['sequence']
        try:
            d = corpus.document(seq)
            reading = read_form(corpus, d) if norm(indexed['record']['Categoria']) == 'relatorio proventos' else None
            output.append({'sequence': seq, 'document': d, 'form': reading})
        except (EvidencePending, ValueError) as error:
            output.append({'sequence': seq, 'index': indexed, 'error': str(error),
                           'absence_is_not_zero_payment_or_ineligibility': True})
    return output


def issuer_notice(corpus):
    """Read the acquired issuer original without fetching during adaptation."""
    url = ('https://ri.excelsior.com.br/Download.aspx?Arquivo=SI+JHugAG6xkkQw1c8w63g=='
           '&IdCanal=ooGPWuFoU1QFHnz8/nwVRw==')
    relative = f'{ISSUER_RECEIPTS}/response-{hashlib.sha256(url.encode()).hexdigest()}.json'
    body, _ = corpus.reader.read(relative, role='actual_issuer_HTTP_receipt')
    receipt = json.loads(body)
    require(receipt['url'] == url and receipt['status'] == 200 and receipt['error'] is None
            and receipt['retries'] == 0, 'issuer_notice_receipt')
    body, source = corpus.reader.source(receipt, 'native_issuer_payment_notice')
    require(source.sha256 == '5e4362bac36781d33251fa41853c6d28d2e9b089bd2e11e74d9e790f67015b9d',
            'reviewed_issuer_notice_changed')
    pages = primary._pdf_pages(body)
    return {'sequence': 'issuer-BAUH-20260424', 'pages': pages, 'tables': [], 'source': asdict(source)}


def class_amounts(document, cnpj):
    """Literal class/amount observations, never an ISIN-suffix classifier."""
    text = norm(' '.join(document['pages']))
    require(cnpj in ''.join(text.split()), 'notice_literal_CNPJ_missing')
    output = []
    for page, literal in enumerate(document['pages'], 1):
        normalized = norm(literal)
        for match in re.finditer(r'(?<![\d,])(\d+,\d{4,14})\s+por\s+(?:cada\s+)?acao\s+(ordinaria|preferencial)\b', normalized):
            output.append({'share_class': 'ON' if match[2] == 'ordinaria' else 'PN',
                'gross_decimal': str(Decimal(match[1].replace(',', '.'))),
                'literal': match[0], 'locator': f'pdf_page={page}:normalized_offset={match.start()}'})
    for table in document['tables']:
        for number, cells in enumerate(table['cells']):
            nonempty = [str(c) for c in cells if c]
            if not nonempty:
                continue
            match = re.fullmatch(r'(on|pn)\s+(bmin[34])', norm(nonempty[0]))
            amount = re.fullmatch(r'r\$\s*(\d+,\d{4,14})', norm(nonempty[1])) if len(nonempty) > 1 else None
            if match and amount:
                output.append({'share_class': match[1].upper(), 'literal_code': match[2].upper(),
                    'gross_decimal': str(Decimal(amount[1].replace(',', '.'))), 'literal_cells': cells,
                    'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': number}})
    require(output, 'literal_per_class_amounts_missing')
    return output


def bind_class(form, amounts, fca):
    """Demand a unique native amount/class bijection for the same event."""
    classes = {fca_share_class(row) for row in fca['reported_equities']}
    preferred = classes - {'ON'}
    converted = []
    for observation in amounts:
        share = observation['share_class']
        if share == 'PN' and share not in classes:
            require(len(preferred) == 1, 'generic_preferred_class_ambiguous')
            share = next(iter(preferred))
        converted.append({**observation, 'exact_FCA_share_class': share})
    result = {}
    for share in classes:
        observations = [a for a in converted if a['exact_FCA_share_class'] == share]
        require(len(observations) == 1, 'unique_notice_class_observation_missing')
        amount = Decimal(observations[0]['gross_decimal'])
        matches = [r for r in form['rows'] if Decimal(r['gross_per_share_decimal']) == amount]
        require(len(matches) == 1 and sum(Decimal(a['gross_decimal']) == amount for a in converted) == 1,
                'class_native_ISIN_amount_join_ambiguous')
        literal_code = observations[0].get('literal_code')
        if literal_code:
            require(any(r['Codigo_Negociacao'] == literal_code and fca_share_class(r) == share
                        for r in fca['reported_equities']), 'notice_code_FCA_conflict')
        result[share] = {'row': matches[0], 'notice_observation': observations[0]}
    require(len({r['row']['isin'] for r in result.values()}) == len(result), 'ISIN_class_join_not_bijective')
    return result


class PrimaryIdentityAdapter:
    """Read-only parent fallback returning typed primary identity, not B3 JSON."""
    def __init__(self, folder=ROOT, *, corpus=None):
        self.corpus = corpus or Corpus(folder)

    def evidence(self, ticker, as_of=CUTOFF):
        require(ticker in TARGETS, 'target_outside_scope')
        when = primary.day(as_of)
        require(when <= CUTOFF, 'as_of_outside_reviewed_cutoff')
        c = self.corpus
        fca = c.fca(ticker, when)
        cvm, cnpj, share = TARGETS[ticker]
        latest = {1570: '1033267', 1309: '1010630', 16616: '1035256',
                  21393: '1014915', 18708: '1048700'}[cvm]
        form = read_form(c, c.document(latest))
        require(form['received_on'] <= when.isoformat(), 'native_identity_not_yet_received')
        proof = [{'kind': 'exact_current_received_FCA_code_class', **fca},
                 {'kind': 'latest_literal_native_ISIN_form', 'form': form}]
        anchor = form
        if cvm in (16616, 21393):
            require(len(fca['reported_equities']) == 1 and len(form['rows']) == 1 and share == 'ON',
                    'sole_literal_equity_join_not_unique')
            selected = form['rows'][0]
            notice = c.document('1035240' if cvm == 16616 else '1014864')
            require(cnpj in ''.join(norm(' '.join(notice['pages'])).split()), 'sole_equity_notice_CNPJ')
            proof.append({'kind': 'same_issuer_native_declaration_not_deposited_assumption',
                          'source': notice['source'], 'index': notice['index']})
        else:
            notice = issuer_notice(c) if cvm == 1570 else c.document('1009459' if cvm == 1309 else '937510')
            if cvm == 18708:
                anchor = read_form(c, c.document('938852'))
            require(anchor['received_on'] <= when.isoformat(), 'class_binding_not_yet_received')
            if notice.get('index'):
                native_index = notice['index']['record']
                require(int(native_index['Codigo_CVM']) == cvm and native_index['CNPJ_Companhia'] == cnpj
                        and native_index['Data_Referencia'] == anchor['approval_date'], 'class_notice_event_conflict')
            anchor_fca = c.fca(ticker, anchor['record_date'])
            amounts = class_amounts(notice, cnpj)
            binding = bind_class(anchor, amounts, anchor_fca)
            selected = binding[share]['row']
            current = [r for r in form['rows'] if r['isin'] == selected['isin']]
            require(len(current) == 1, 'current_native_form_ISIN_disagrees')
            proof.append({'kind': 'same_event_literal_class_amount_native_ISIN_bijection',
                'class_form': anchor, 'notice_source': notice['source'],
                'notice_index': notice.get('index'), 'FCA_at_class_observation': anchor_fca,
                'class_bindings': binding,
                'class_observation_is_dated_not_continuous_identity_claim': True})
        return CurrentSecurityBinding(ticker, cvm, cnpj, share, selected['isin'], when.isoformat(),
            anchor['record_date'], tuple(proof), form['record_date'])

    def issuer_map(self, cvm, as_of=CUTOFF):
        require(cvm in ISSUERS, 'issuer_outside_scope')
        return {t: self.evidence(t, as_of) for t, values in TARGETS.items() if values[0] == cvm}

    def core_fallback(self, row, cache, cutoff):
        """Parent may call this in memory after the normal cache is absent."""
        binding = self.evidence(row.get('ticker'), cutoff)
        require(row.get('identity_status') == 'official_cvm_fca'
                and (row.get('cvm'), row.get('share_class')) == (binding.cvm, binding.share_class)
                and row.get('isin') in (None, binding.isin) and row.get('unit_composition') is None,
                'caller_identity_conflict')
        require(type(row.get('sector_code')) is int and 0 <= row['sector_code'] <= 10,
                'caller_sector_unavailable')
        require(not row.get('listing_start') or primary.day(row['listing_start']) <= cutoff,
                'caller_listing_started_after_cutoff')
        return binding, {t: b.isin for t, b in self.issuer_map(binding.cvm, cutoff).items()}


# Families were reviewed in the pinned IPE indices. Some resubmissions change
# Protocolo Provento or Data_Referencia; neither field alone deduplicates them.
FORM_FAMILIES = {
    1570: [('618810', ()), ('753061', ()), ('890597', ()), ('1033267', ())],
    1309: [('594793', ()), ('711042', ()), ('855558', ()), ('1010630', ())],
    16616: [('565221', ()), ('753317', ()), ('891083', ('840770',)),
            ('982717', ()), ('1035256', ())],
    21393: [('559531', ()), ('1014915', ())],
    18708: [('658899', ()), ('757164', ('738165',)), ('757163', ('754591',)),
            ('833306', ('830771',)), ('840632', ()), ('893042', ()),
            ('938852', ()), ('983307', ()), ('1048700', ())],
}


@dataclass(frozen=True)
class NativeCashObservation:
    ticker: str
    cvm: int
    cnpj: str
    current_share_class: str
    isin: str
    record_date: str
    payment_date: str
    gross_per_share_decimal: str
    available_on: str
    form_sequence: str
    evidence: tuple[dict, ...]
    label: str | None = None
    payment_date_role: str = 'native_reported_schedule_not_settlement'
    current_label_is_not_a_historical_ticker_alias: bool = True
    share_regime_verified: bool = False
    model_cash_admitted: bool = False


def recover_calendar(adapter, ticker):
    c = adapter.corpus
    binding = adapter.evidence(ticker)
    observations, superseded, missing = [], [], []
    for sequence, earlier in FORM_FAMILIES[binding.cvm]:
        try:
            form = read_form(c, c.document(sequence))
        except (ValueError, EvidencePending) as error:
            missing.append({'sequence': sequence, 'reason': str(error), 'older_version_not_substituted': True})
            continue
        require(form['cvm'] == binding.cvm and form['cnpj'] == binding.cnpj, 'calendar_issuer_conflict')
        for previous in earlier:
            try:
                old = read_form(c, c.document(previous))
                require(old['version'] < form['version'] and old['received_on'] <= form['received_on'],
                        'calendar_supersession_conflict')
                superseded.append({'selected_sequence': sequence, 'older_form': old})
            except EvidencePending as error:
                superseded.append({'selected_sequence': sequence, 'older_sequence': previous,
                    'older_original_unavailable': str(error), 'older_index': c.indexed(previous)})
        matches = [r for r in form['rows'] if r['isin'] == binding.isin]
        for row in matches:
            payments = [row]
            if row['payment_form'] == 'parcelado':
                require(len([r for r in form['rows'] if r['payment_form'] == 'parcelado']) == 1
                        and form['installments'], 'installment_attribution_ambiguous')
                require(sum(Decimal(p['gross_per_share_decimal']) for p in form['installments'])
                        == Decimal(row['gross_per_share_decimal']), 'native_installments_total_disagrees')
                payments = form['installments']
            for installment in payments:
                payment = installment['payment_date']
                require(payment is not None and form['record_date'] <= payment, 'calendar_date_roles_unresolved')
                observations.append(NativeCashObservation(ticker, binding.cvm, binding.cnpj,
                    binding.share_class, binding.isin, form['record_date'], payment,
                    installment['gross_per_share_decimal'], form['received_on'], sequence,
                    ({'native_form': form, 'native_row': row, 'native_installment': installment},)))
    events = [asdict(e) for e in observations]
    supplementary = supplementary_notices(c, binding)
    for event in events:
        annotate_cash_kind(c, binding, event)
        if ticker == 'GPAR3' and event['form_sequence'] == '1014915':
            event['holder_specific_cash_or_non_cash_election_unresolved'] = True
            event['native_condition_source'] = c.document('1014864')['source']
            event['universal_cash_payment_not_asserted'] = True
        if ticker == 'CEGR3' and event['form_sequence'] in ('982717', '1035256'):
            event['payment_subject_to_issuer_financial_and_regulatory_conditions'] = True
            event['native_condition_source'] = c.document('1035240')['source']
    return {'identity': asdict(binding), 'native_form_observations': events,
        'past_scheduled_observations': [e for e in events if e['payment_date'] <= CUTOFF.isoformat()],
        'future_scheduled_observations': [e for e in events if e['payment_date'] > CUTOFF.isoformat()],
        'superseded_forms_retained_not_added_as_cash': superseded,
        'missing_final_originals': missing, 'native_calendar_coverage_verified': False,
        'supplementary_literal_class_notices': supplementary,
        'unknown_years_are_not_zero_payments': True, 'cash_labels_unresolved': sum(e['label'] is None for e in events),
        'independent_ultimate_cash_authorship_verified': False, 'model_cash_admitted': False,
        'historical_code_aliases_created': False,
        'remaining_work': ['reconcile_native_notices_and_revisions_with_calendar',
            'verify_window_coverage_without_silent_zero_years', 'verify_cash_kind_and_holder_specific_elections',
            'independent_prices_share_regime_and_annual_financial_gates_unchanged']}


def annotate_cash_kind(corpus, binding, event):
    declarations = {'753061': ('753004', 'DIVIDENDO'), '890597': ('890639', 'DIVIDENDO'),
        '1033267': ('issuer', 'DIVIDENDO'), '594793': ('594451', 'JCP'),
        '855558': ('855010', 'DIVIDENDO'), '1010630': ('1009459', 'DIVIDENDO'),
        '658899': ('658514', 'DIVIDENDO'), '893042': ('892955', 'DIVIDENDO'),
        '938852': ('937510', 'DIVIDENDO'), '983307': ('983306', 'DIVIDENDO')}
    seq = event['form_sequence']
    if seq == '711042':
        document = corpus.document('710872')
        for table in document['tables']:
            header = norm(' '.join(str(v or '') for v in table['cells'][0]))
            for number, cells in enumerate(table['cells'][1:], 1):
                values = [str(v) for v in cells if v]
                if len(values) < 2 or binding.ticker.lower() not in norm(values[0]):
                    continue
                gross = re.fullmatch(r'r\$\s*(\d+,\d+)', norm(values[1]))
                if gross and Decimal(gross[1].replace(',', '.')) == Decimal(event['gross_per_share_decimal']):
                    event['label'] = 'JCP' if 'jcp bruto' in header else 'DIVIDENDO' if 'dividendo' in header else None
                    event['evidence'] += ({'notice_source': document['source'], 'literal_header': table['cells'][0],
                        'literal_cells': cells, 'locator': {k: table[k] for k in ('page', 'table_index')} | {'row_index': number}},)
        return
    if seq not in declarations:
        return
    notice_seq, label = declarations[seq]
    document = issuer_notice(corpus) if notice_seq == 'issuer' else corpus.document(notice_seq)
    amounts = class_amounts(document, binding.cnpj)
    share = 'PN' if binding.share_class.startswith('PN') else binding.share_class
    matches = [a for a in amounts if a['share_class'] == share
               and Decimal(a['gross_decimal']) == Decimal(event['gross_per_share_decimal'])]
    require(len(matches) == 1, 'cash_kind_notice_native_amount_disagrees')
    text = norm(' '.join(document['pages']))
    require(('juros sobre capital proprio' if label == 'JCP' else 'dividend') in text,
            'cash_kind_not_in_native_declaration')
    event['label'] = label
    event['evidence'] += ({'notice_source': document['source'], 'notice_observation': matches[0],
                           'native_cash_kind': label},)
    if seq == '983307':
        event['payment_date_conflict'] = {'notice_literal': '12 de janeiro de 2025',
            'form_literal_payment': '12/01/2026', 'retained_not_silently_corrected': True}


def supplementary_notices(corpus, binding):
    """Reviewed literal early payments, without constructing historical ISINs."""
    output = []
    if binding.cvm == 18708:
        specifications = [
            ('441427', '2021-11-16', '2021-12-06', '16 de novembro de 2021', '06 de dezembro de 2021',
             [('ON', '9.115978436', 'DIVIDENDO'), ('PN', '10.02757628', 'DIVIDENDO'),
              ('ON', '1.4822729164', 'JCP'), ('PN', '1.6305002080', 'JCP')]),
            ('498416', '2022-04-29', '2022-05-30', '29 de abril de 2022', '30 de maio de 2022',
             [('ON', '0.547352737', 'DIVIDENDO'), ('PN', '0.602088011', 'DIVIDENDO')]),
            ('556894', '2022-11-17', '2022-12-05', '17 de novembro de 2022', '5 de dezembro de 2022',
             [('ON', '0.2626576780', 'DIVIDENDO'), ('PN', '0.2889234458', 'DIVIDENDO'),
              ('ON', '0.2141461175', 'JCP'), ('PN', '0.2355607293', 'JCP')]),
        ]
        for seq, record, payment, record_literal, payment_literal, readings in specifications:
            doc = corpus.document(seq)
            text = norm(doc['pages'][0])
            require(binding.cnpj in ''.join(text.split()) and record_literal in text and payment_literal in text,
                    'reviewed_notice_date_or_issuer_changed')
            for share, amount, label in readings:
                if share != binding.share_class:
                    continue
                comma = amount.replace('.', ',')
                marker = re.search(re.escape(comma) + r'\s+por\s+(?:cada\s+)?acao\s+'
                    + ('ordinaria' if share == 'ON' else r'preferencia\s*l'), text)
                require(marker is not None, 'reviewed_notice_literal_class_amount_changed')
                output.append({'ticker': binding.ticker, 'cvm': binding.cvm, 'cnpj': binding.cnpj,
                    'share_class': share, 'record_date': record, 'payment_date': payment,
                    'gross_per_share_decimal': amount, 'label': label,
                    'available_on': doc['index']['record']['Data_Entrega'],
                    'native_notice_ISIN_printed': False, 'historical_code_alias_asserted': False,
                    'source': doc['source'], 'index': doc['index'],
                    'literal_class_amount': marker[0], 'literal_rights_date': record_literal,
                    'literal_payment_date': payment_literal,
                    'locator': f'pdf_page=1:normalized_offset={marker.start()}', 'model_cash_admitted': False})
    if binding.cvm == 1309:
        doc = corpus.document('471459')
        text = norm(doc['pages'][0])
        require(binding.cnpj in ''.join(text.split()) and '03 de marco de 2022' in text
                and '15 de marco de 2022' in text, 'BMIN_2022_notice_dates_changed')
        share_word = 'ordinarias' if binding.share_class == 'ON' else 'preferenciais'
        match = re.search('acoes ' + share_word + r': valor bruto de r\$\s*(\d+,\d+) por acao', text)
        require(match is not None, 'BMIN_2022_literal_class_gross_missing')
        amount = str(Decimal(match[1].replace(',', '.')))
        output.append({'ticker': binding.ticker, 'cvm': binding.cvm, 'cnpj': binding.cnpj,
            'share_class': binding.share_class, 'record_date': '2022-03-03', 'payment_date': '2022-03-15',
            'gross_per_share_decimal': amount, 'label': 'JCP', 'explicit_zero_declaration': Decimal(amount) == 0,
            'not_zero_by_absence': True, 'available_on': doc['index']['record']['Data_Entrega'],
            'native_notice_ISIN_printed': False, 'historical_code_alias_asserted': False,
            'source': doc['source'], 'index': doc['index'], 'literal': match[0],
            'locator': f'pdf_page=1:normalized_offset={match.start()}', 'model_cash_admitted': False})
    return output


def identity_only_source_replay(adapter):
    from unittest.mock import patch
    from . import inventory_candidates as core
    from .market_inventory import load_inventory
    c = adapter.corpus
    inventory = load_inventory(c.root)
    pointers = [c.root / 'current.json', c.root / 'inventory/current.json']
    before = {p: p.read_bytes() for p in pointers if p.is_file()}
    original = core._identity
    def hook(row, cache, cutoff):
        if row.get('ticker') in TARGETS:
            return adapter.core_fallback(row, cache, cutoff)
        return original(row, cache, cutoff)
    results = []
    with patch.object(core, '_identity', hook):
        for ticker in TARGETS:
            row = next(r for r in inventory['stocks'] if r['ticker'] == ticker)
            checks = {}
            try:
                result = core._one(row, core._Cache(c.root, inventory), CUTOFF, checks)
                results.append({'ticker': ticker, 'source_pass': True, 'checks': checks,
                                'ledger_rows': len(result['ledger'])})
            except ValueError as error:
                results.append({'ticker': ticker, 'source_pass': False, 'reason': str(error), 'checks': checks})
    require(core._identity is original, 'identity_hook_not_restored')
    return {'actual_function': 'inventory_candidates._one', 'results': results,
        'only_hook': 'typed_primary_identity_in_process', 'cash_or_price_gates_hooked': False,
        'process_hook_restored': True, 'core_files_edited': False,
        'pointer_bytes_unchanged_during_replay': all(p.read_bytes() == v for p, v in before.items()),
        'production_integration': False}


def material_notice_findings(corpus):
    cases = [
        ('BAUH4', '558126', '2013 a 2018', 'legacy_collection_reminder_not_a_new_2022_distribution'),
        ('CEGR3', '448892', '09 de abril de 2021', 'signed_day_conflicts_with_December_index_do_not_infer_hoje_rights'),
        ('GPAR3', '752890', 'prazo indeterminado', 'declared_but_no_literal_rights_or_payment_day_not_zero'),
        ('GPAR3', '892022', 'incompativeis com a', 'declared_payment_deferred_not_a_paid_dividend'),
        ('GPAR3', '1014864', 'in natura e em especie', 'mixed_cash_and_non_cash_holder_election_requires_review'),
        ('GPAR3', '1039713', 'nao apresentaram', 'later_holder_election_outcome_do_not_generalize_all_holders'),
        ('GPAR3', '1039713', 'celgpar em liquidacao', 'native_issuer_liquidation_status_requires_parent_review_not_inferred_from_missing_prices'),
        ('PEAB3/PEAB4', '983306', '12 de janeiro de 2025', 'notice_payment_year_conflicts_with_native_form_2026'),
    ]
    output = []
    for ticker, sequence, needle, finding in cases:
        d = corpus.document(sequence)
        excerpts = []
        for page, text in enumerate(d['pages'], 1):
            normalized = norm(text)
            offset = normalized.find(needle)
            if offset >= 0:
                excerpts.append({'page': page, 'normalized_offset': offset,
                    'literal_normalized_excerpt': normalized[max(0, offset-180):offset+len(needle)+300]})
        require(excerpts, 'reviewed_material_notice_clause_missing')
        output.append({'ticker': ticker, 'sequence': sequence, 'finding': finding, 'excerpts': excerpts,
            'source': d['source'], 'index': d['index'], 'automatic_ineligibility': False})
    return output


def price_evidence_audit(corpus, ticker):
    from .round2_b3_eqma_recovery import replay_prices
    from .pending_all_prices import monthly_audit
    output = {'ticker': ticker, 'minimum_independent_dated_raw_closes': 4,
        'verified_independent_raw_close_references': [], 'independent_anchors_available': 0,
        'automatic_ineligibility': False, 'prices_or_dates_created': False,
        'COTAHIST_local_files': [p.relative_to(corpus.root).as_posix()
            for p in (corpus.root / 'raw').rglob('*') if p.is_file() and 'cotahist' in p.name.lower()],
        'legacy_expanded_price_file_exists': (corpus.root / f'raw/expanded/{ticker}-prices.json').is_file()}
    try:
        output['actual_price_replay'] = replay_prices(corpus, ticker)
    except ValueError as error:
        relative = f'raw/inventory/daily-prices/{ticker}.json'
        body, source = corpus.reader.read(relative, role='original_Yahoo_daily_prices')
        output['actual_price_replay'] = {'source': asdict(source), 'failure': str(error),
                                         'literal_Yahoo_61_month_audit': monthly_audit(body, ticker)}
    quote_pointer = f'raw/current-quotes/{ticker}-current.json'
    if (corpus.root / quote_pointer).is_file():
        body, pointer = corpus.reader.read(quote_pointer, role='existing_current_quote_pointer')
        relative = json.loads(body)['source']
        body, receipt_file = corpus.reader.read(relative, role='existing_current_quote_receipt')
        receipt = json.loads(body)
        body, source = corpus.reader.source(receipt, 'actual_single_current_BRAPI_quote_not_four_anchors')
        data = json.loads(body)
        output['existing_current_quote'] = {'source': asdict(source), 'receipt': asdict(receipt_file),
            'original_results': data.get('results'), 'rejection_as_four_historical_refs':
                'single_current_quote_not_four_distinct_dated_raw_closes_independence_not_established'}
    else:
        output['existing_current_quote'] = {'file': quote_pointer, 'exists': False}
    output['cached_issuer_price_research'] = []
    for relative in ('raw/primary-research/all-pending/missing-a/issuer-' + str(TARGETS[ticker][0]) + '-inspection.json',):
        if not (corpus.root / relative).is_file():
            continue
        body, source = corpus.reader.read(relative, role='existing_issuer_site_inspection_not_price_evidence')
        inspected = json.loads(body)
        output['cached_issuer_price_research'].append({'source': asdict(source),
            'outcomes': [{k: r.get(k) for k in ('url', 'outcome', 'status', 'error')}
                         for r in inspected.get('outcomes', [])],
            'not_promoted_to_four_price_refs': True})
    output['network_constraints_preserved'] = {'B3_network': False, 'api_mziq_network': False,
        'stopped_issuer_origins_not_retried': True, 'BMEB_not_relabelled_BMIN': True}
    return output


def build_report(folder=ROOT):
    corpus = Corpus(folder)
    adapter = PrimaryIdentityAdapter(corpus=corpus)
    bindings = {ticker: asdict(adapter.evidence(ticker)) for ticker in TARGETS}
    calendars = {ticker: recover_calendar(adapter, ticker) for ticker in TARGETS}
    prices = {ticker: price_evidence_audit(corpus, ticker) for ticker in TARGETS}
    return {'scope': 'round2-more-identities', 'targets': list(TARGETS),
        'cutoff': CUTOFF.isoformat(), 'identity_bindings': bindings, 'native_calendars': calendars,
        'FCA_reception_audit_not_a_five_year_identity_gate': [
            {'year': f['year'], 'master': f['master'], 'master_locator': f['master_locator'],
             'children_in_archive': len(f['children']),
             'literal_assigned_code_rows': [r for r in f['children'] if r['Codigo_Negociacao'] in TARGETS],
             'source': f['source'], 'security_member': f['security_member']}
            for f in corpus.filings],
        'material_native_notice_findings': material_notice_findings(corpus),
        'price_audits': prices, 'identity_only_actual_source_replay': identity_only_source_replay(adapter),
        'ordinary_stock_unchanged_five_year_identity_required': False,
        'FCA_and_IPE_sources': [asdict(r) for r in corpus.reader.sources.values()],
        'cash_source_or_model_admissions': [], 'automatic_ineligibility_findings': [],
        'production_integration': False, 'core_pointer_inventory_docs_writes': False,
        'fake_B3_detail_files_created': False, 'B3_or_api_mziq_live_requests': 0,
        'receiving_dates_not_replaced_by_reference_dates': True,
        'publication_not_inferred_from_retrieval_or_mtime': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--collect', nargs='+', metavar='EXACT_CVM_INDEX_SEQUENCE')
    args = parser.parse_args()
    if args.collect:
        result = collect_documents(Corpus(args.root), args.collect)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    report = build_report(args.root)
    body = json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = args.root / BASE / f'identity-more-report-{digest}.json'
    _save(path, body)
    print(json.dumps({'file': str(path.resolve()), 'sha256': digest, 'bytes': len(body),
                      'targets': list(TARGETS), 'source_or_model_admissions': 0}, indent=2))


if __name__ == '__main__':
    main()


def collect_documents(corpus, sequences):
    collector = None
    output = []
    for sequence in dict.fromkeys(map(str, sequences)):
        indexed = corpus.indexed(sequence)
        url = indexed['record']['Link_Download']
        require(urlsplit(url).hostname == 'www.rad.cvm.gov.br'
                and urlsplit(url).scheme == 'https'
                and urlsplit(url).path == '/ENET/frmDownloadDocumento.aspx', 'not_exact_public_CVM_link')
        try:
            corpus.receipt(indexed)
            output.append({'sequence': sequence, 'existing_cached_original': True})
            continue
        except EvidencePending as error:
            if str(error) != 'identity_more_primary_original_not_cached':
                raise
        if collector is None:
            collector = PublicSourceCollector(corpus.root, 'round2-identity')
        receipt = collector.fetch(url, discovery={'kind': 'identity_more_exact_cached_CVM_IPE_link',
            'index_source': indexed['source'], 'locator': indexed['locator'], 'record': indexed['record']})
        output.append({'sequence': sequence, 'receipt': receipt})
        if receipt['error'] or receipt['status'] != 200:
            break
    return output


def collect_excelsior_published_notice(corpus):
    """One exact download link already published in the cached issuer page."""
    from lxml import html
    digest = '57a354af0e2cf641226ddca501b14d665008eb025bfbdbd771161f032273ec62'
    page_file = f'raw/primary-research/all-pending/missing-a/body-{digest}.bin'
    page_url = 'https://ri.excelsior.com.br/listgroup.aspx?idCanal=ooGPWuFoU1QFHnz8/nwVRw==&ano=2026'
    body, source = corpus.reader.read(page_file, digest, url=page_url,
        retrieved_at='2026-10-25T22:56:36.475761+00:00', role='actual_cached_issuer_notice_index')
    tree = html.fromstring(body)
    links = [a for a in tree.xpath('//a[@href]')
             if 'distribuicao de dividendos' in norm(a.text_content())]
    require(len(links) == 1, 'unique_published_issuer_notice_link_missing')
    link = links[0]
    url = urljoin(page_url, link.get('href'))
    require(urlsplit(url).hostname == 'ri.excelsior.com.br', 'issuer_download_origin_conflict')
    archived = corpus.root / ISSUER_RECEIPTS / ('response-' + hashlib.sha256(url.encode()).hexdigest() + '.json')
    if archived.is_file():
        body, _ = corpus.reader.read(archived.relative_to(corpus.root).as_posix(), role='retained_actual_issuer_HTTP_receipt')
        receipt = json.loads(body)
        require(receipt['url'] == url and receipt['scope'] == 'round2-identity' and receipt['retries'] == 0,
                'archived_actual_collection_receipt_conflict')
        corpus.reader.source(receipt, 'actual_issuer_original_no_refetch')
        return receipt
    receipt = PublicSourceCollector(corpus.root, 'round2-identity').fetch(url, discovery={
        'kind': 'identity_more_actual_issuer_published_link', 'source': asdict(source),
        'locator': tree.getroottree().getpath(link), 'literal_href': link.get('href'),
        'literal_link_text': link.text_content()})
    destination = corpus.root / ISSUER_RECEIPTS
    destination.mkdir(exist_ok=True)
    require(primary._safe(corpus.root, destination), 'unsafe_issuer_receipt_folder')
    # Retain actual receipt bytes and the original collector scope. Separating
    # issuer metadata keeps the earlier CVM-only auditor's contract unchanged.
    for endpoint in ('https://ri.excelsior.com.br/robots.txt', url):
        name = hashlib.sha256(endpoint.encode()).hexdigest()
        completed = corpus.root / BASE / ('response-' + name + '.json')
        if not completed.is_file():
            continue
        for prefix in ('request-', 'response-'):
            original = corpus.root / BASE / (prefix + name + '.json')
            target = destination / original.name
            require(primary._safe(corpus.root, original) and not target.exists(),
                    'issuer_receipt_relocation_conflict')
            original.rename(target)
    return receipt
