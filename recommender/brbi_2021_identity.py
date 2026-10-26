"""Isolated primary BRBI11 IPO identity research, without candidate integration.

The pre-IPO annual FCA observation is not a five-year-history determination.
The signed IPO announcement reports composition and a prospective timetable;
neither that timetable nor this report proves settlement or model eligibility.
"""

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
import zipfile

from pypdf import PdfReader

from .bulk_pending_sources import PublicSourceCollector, _safe, _save


BASE = 'raw/primary-research/all-pending/brbi'
CUTOFF = date(2026, 9, 30)
WINDOW_START = date(2021, 9, 30)
IDENTITY = {'ticker': 'BRBI11', 'cvm': 25860, 'cnpj': '10.739.356/0001-03',
            'isin': 'BRBRBICDAM10', 'components': {'ON': 1, 'PN': 2}}
IPE_SHA = 'b5ad4dcc2c0376027a23ed372469feaea11bf0dfd7ac75e2bbe99fd6b8f78ee7'
IPE_FILE = f'raw/primary-research/cvm-documents/index-2021-{IPE_SHA}.zip'
FCA_SHA = '65ba4b0225e9b2106901cdf348215933be8affba79925fc71361e2f6818c7999'
FCA_FILE = f'raw/primary-research/units/fca_cia_aberta_2021-{FCA_SHA}.zip'
PRICING_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
               '&CodigoInstituicao=1&numProtocolo=877243&numSequencia=401980&numVersao=1')
PRICING_SHA = 'a1dbefc80d09ed446a8f460ff722c574f11508a1f2851dd36dab6c1e5355f9dc'
PRICING_FILE = f'{BASE}/body-{PRICING_SHA}.bin'
PROTOCOL = '025860IPE170620210104436414-56'
ITR_URL = ('https://api.mziq.com/mzfilemanager/v2/d/f4c9c74c-60bd-49b4-9fdf-007fb8f0f351/'
           'bb89436c-bf2c-4265-a1f5-8b3efde657a3?origin=2')
ITR_SHA = '82f6ba2ce33035bf3512a371a4678c9b65c351ab420a6799646507d4bcba79f8'


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _text(value):
    ascii_text = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return ' '.join(ascii_text.lower().split())


def _local(root, relative, digest=None):
    path = root / relative
    _require(_safe(root, path) and path.stat().st_size <= 20_000_000, 'brbi_unsafe_evidence')
    content = path.read_bytes()
    _require(digest is None or _sha(content) == digest, 'brbi_evidence_changed')
    return content


def _rows(archive, member):
    with archive.open(member) as stream:
        return list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))


def pricing_index_record(root):
    content = _local(root, IPE_FILE, IPE_SHA)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        matches = [(number, row) for number, row in enumerate(_rows(archive, 'ipe_cia_aberta_2021.csv'), 2)
                   if row['Codigo_CVM'] == '25860' and row['Protocolo_Entrega'] == PROTOCOL]
    _require(len(matches) == 1, 'brbi_2021_pricing_index_ambiguous')
    number, record = matches[0]
    _require(record['CNPJ_Companhia'] == IDENTITY['cnpj']
             and record['Data_Referencia'] == record['Data_Entrega'] == '2021-06-17'
             and record['Categoria'] == 'Fato Relevante' and record['Versao'] == '1'
             and _text(record['Assunto']) == 'pricing da oferta publica inicial de units'
             and record['Link_Download'] == PRICING_URL, 'brbi_2021_pricing_index_identity_changed')
    return record, {'file': IPE_FILE, 'sha256': IPE_SHA, 'bytes': len(content),
                    'member': 'ipe_cia_aberta_2021.csv', 'csv_record': number,
                    'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2021.zip',
                    'authority': 'primary', 'retrieved_at': None,
                    'use': 'exact_issuer_protocol_and_publication_date'}


def _public_pdf(root, url, digest):
    content = _local(root, f'{BASE}/body-{digest}.bin', digest)
    receipt_file = f'{BASE}/response-{_sha(url.encode())}.json'
    receipt = json.loads(_local(root, receipt_file))
    _require(receipt['url'] == url and receipt['status'] == 200 and receipt['error'] is None
             and receipt['sha256'] == digest and receipt['bytes'] == len(content)
             and receipt['file'] == f'{BASE}/body-{digest}.bin' and receipt['scope'] == 'brbi'
             and receipt['retries'] == 0, 'brbi_original_transport_proof_changed')
    _require(content.startswith(b'%PDF-'), 'brbi_original_is_not_PDF')
    source = {key: receipt[key] for key in ('url', 'file', 'sha256', 'bytes', 'retrieved_at')}
    source.update(receipt_file=receipt_file, authority='primary', point_in_time_archive_verified=False)
    reader = PdfReader(io.BytesIO(content))
    return [page.extract_text() for page in reader.pages], source


def parse_pricing(pages, record):
    """Read printed identity and composition, never infer ratios from quantities."""
    _require(len(pages) == 10, 'brbi_unreviewed_pricing_document')
    first, timetable, signature = map(_text, (pages[0], pages[6], pages[9]))
    _require(record['Codigo_CVM'] == '25860' and record['CNPJ_Companhia'] == IDENTITY['cnpj']
             and record['Protocolo_Entrega'] == PROTOCOL and record['Versao'] == '1'
             and record['Link_Download'] == PRICING_URL,
             'brbi_pricing_metadata_identity_changed')
    _require(record['Data_Referencia'] == record['Data_Entrega'] == '2021-06-17'
             and date.fromisoformat(record['Data_Entrega']) <= WINDOW_START,
             'brbi_pricing_publication_date_changed')
    _require(first.startswith('br advisory partners participacoes s.a.')
             and IDENTITY['cnpj'] in first
             and re.search(r'codigo cvm n[o. ]*25860\b', first)
             and 'fato relevante' in first, 'brbi_pricing_issuer_changed')
    isin = re.findall(r'codigo isin das units:\s*([a-z0-9]+)', first)
    ticker = re.findall(r'codigo de negociacao das units na b3:\s*([a-z0-9]+)', first)
    _require(isin == [IDENTITY['isin'].lower()] and ticker == ['brbi11'], 'brbi_pricing_security_changed')
    composition = re.findall(
        r'cada um de (\d+) \(uma\) acao ordinaria.{0,80}? e (\d+) \(duas\) acoes preferenciais', first)
    _require(composition == [('1', '2')], 'brbi_printed_2021_composition_changed')
    _require('sao paulo, 17 de junho de 2021.' in signature, 'brbi_signed_document_date_changed')
    _require(re.search(r'passarao a ser negociadas.{0,100}?em 21 de junho de 2021', first)
             and '22 de junho de 2021' in first
             and '21/06/2021' in timetable and '22/06/2021' in timetable
             and 'datas previstas para os eventos futuros sao meramente indicativas' in timetable,
             'brbi_pricing_timetable_changed')
    return {'identity': {**IDENTITY, 'components': dict(IDENTITY['components'])},
            'document_date': '2021-06-17', 'published_at': record['Data_Entrega'],
            'publication_basis': 'exact_hashed_CVM_IPE_2021_protocol_record',
            'scheduled_first_trade': '2021-06-21', 'scheduled_settlement': '2021-06-22',
            'schedule_is_indicative': True, 'actual_first_trade_independently_verified': False,
            'bank_settlement_verified': False,
            'pdf_pages': {'identity_and_composition': 1, 'indicative_timetable': 7, 'signature': 10}}


def pre_ipo_fca(root):
    content = _local(root, FCA_FILE, FCA_SHA)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        masters = [row for row in _rows(archive, 'fca_cia_aberta_2021.csv')
                   if int(row['CD_CVM']) == 25860 and row['DT_RECEB'] <= WINDOW_START.isoformat()]
        _require(bool(masters), 'brbi_2021_FCA_master_missing')
        master = max(masters, key=lambda row: (row['DT_REFER'], int(row['VERSAO'])))
        _require((master['CNPJ_CIA'], master['DT_REFER'], master['VERSAO'], master['ID_DOC'], master['DT_RECEB'])
                 == (IDENTITY['cnpj'], '2021-01-01', '2', '104100', '2021-05-13'),
                 'brbi_pre_IPO_FCA_identity_changed')
        children = [row for row in _rows(archive, 'fca_cia_aberta_valor_mobiliario_2021.csv')
                    if row['ID_Documento'] == master['ID_DOC']]
    _require(len(children) == 1 and _text(children[0]['Valor_Mobiliario']) == 'valor mobiliario nao registrado'
             and children[0]['Codigo_Negociacao'] == '' and children[0]['Composicao_BDR_Unit'] == ''
             and children[0]['CNPJ_Companhia'] == master['CNPJ_CIA']
             and children[0]['Data_Referencia'] == master['DT_REFER']
             and children[0]['Versao'] == master['VERSAO'], 'brbi_pre_IPO_FCA_child_changed')
    return {'master': master, 'security_rows': children,
            'observation': 'annual_FCA_received_before_the_June_2021_IPO_announcement',
            'missing_annual_unit_row_proves_insufficient_five_year_history': False}, {
                'file': FCA_FILE, 'sha256': FCA_SHA, 'bytes': len(content), 'authority': 'primary',
                'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2021.zip',
                'retrieved_at': None, 'use': 'pre_IPO_annual_FCA_observation_only'}


def audit(folder, cutoff=CUTOFF):
    """Strict offline proof; never writes, downloads, or admits a candidate."""
    root = Path(folder).resolve()
    _require(cutoff == CUTOFF, 'brbi_unreviewed_cutoff')
    record, index_source = pricing_index_record(root)
    pages, primary_source = _public_pdf(root, PRICING_URL, PRICING_SHA)
    reading = parse_pricing(pages, record)
    fca, fca_source = pre_ipo_fca(root)
    later_pages, later_source = _public_pdf(root, ITR_URL, ITR_SHA)
    _require('31 de marco de 2022' in _text(later_pages[0]), 'brbi_later_ITR_identity_changed')
    later_context = _text(later_pages[14])
    _require('em 17 de junho de 2021 foi concluida a precificacao' in later_context
             and 'em 4 de janeiro de 2022' in later_context, 'brbi_later_ITR_context_changed')
    return {'policy': 'BRBI_2021_PRIMARY_IDENTITY_RESEARCH_1', 'cutoff': cutoff.isoformat(),
            'window_start': WINDOW_START.isoformat(), 'status': 'primary_2021_unit_identity_evidence',
            **reading, 'IPE_record': record, 'pre_IPO_FCA_2021': fca,
            'user_supplied_later_document': {
                'actual_document_type': 'ITR_2022_03_31', 'context_pdf_page': 15,
                '2021_IPO_pricing_date_reported': '2021-06-17',
                'composition_paragraph_relates_to_2022_follow_on': True,
                'used_as_2021_composition_authority': False},
            'sources': [index_source, primary_source, fca_source, later_source],
            'scope_of_proof': 'contemporaneous_2021_unit_identity_composition_and_issuer_announced_timetable',
            'annual_FCA_gap_resolved_documentarily': True,
            'five_year_unchanged_composition_continuity_independently_verified': False,
            'five_year_price_and_cash_history_verified': False,
            'five_year_history_insufficient': None,
            'point_in_time_archive_verified': False, 'numerical_features_manufactured': False,
            'model_admitted': False, 'admissions': [], 'production_integration': False,
            'unit_candidates_modified': False,
            'remaining_work': ['parent_must_review_any_strict_historical_FCA_adapter_change',
                               'all_existing_price_cash_financial_and_five_year_gates_still_apply']}


def collect(folder):
    root = Path(folder).resolve()
    record, _ = pricing_index_record(root)
    collector = PublicSourceCollector(root, 'brbi')
    return [collector.fetch(record['Link_Download'], discovery={
                'kind': 'cached_CVM_IPE_2021_exact_issuer_pricing', 'cvm': 25860,
                'protocol': PROTOCOL, 'published_at': record['Data_Entrega']}),
            collector.fetch(ITR_URL, discovery={'kind': 'user_supplied_primary_issuer_PDF'})]


def write_report(folder, report):
    root = Path(folder).resolve()
    base = root / BASE
    _require(_safe(root, base), 'brbi_unsafe_report_folder')
    base.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    path = base / f'identity-report-{_sha(content)}.json'
    _save(path, content)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent / 'data/market')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--write-report', action='store_true')
    args = parser.parse_args()
    if args.fetch:
        collect(args.root)
    report = audit(args.root)
    if args.write_report:
        print(write_report(args.root, report))
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
