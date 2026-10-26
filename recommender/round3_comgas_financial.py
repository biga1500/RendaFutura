"""Exact-current Comgas financial readings, isolated for parent review only."""

from copy import deepcopy
import csv
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile

from . import inventory_financials as native
from .bulk_pending_sources import PublicSourceCollector, approved
from .collect_market_data import financial_features


ROOT = Path(__file__).resolve().parent / 'data/market'
BASE = 'raw/primary-research/all-pending/round3-comgas-financial'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
CUTOFF = date(2026, 9, 30)
SPECS = {'CGAS3': (15636, 'ON', 9), 'CGAS5': (15636, 'PN', 9)}
KEY = (15636, '2025-12-31', '1')
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
DRE_SHA = '2415a8eeec88770eef6b0225fabb79e53d1923c6efc1cfd87d5dc625cd280b5e'
DFC_SHA = '2c3439aa93b1c819da9806f1bd10ef60c1829623ace9774c57c748cec6c4ab5e'
CONTAINER_SHA = '24ae0cc23692a9a5c65b469089ab9e5efbff3d34cd0eecf2f0122bf7f323bbd0'
PDF_SHA = 'bc40e4102d6a08d8060dd6b1cede4d42a8d3dabdfb909c47ada0640be025ab13'
XML_SHA = '07d39a58af1302f00de6f7fb026a4fd08a578baac74ea44c9f7ba91d882e822d'
PDF_MEMBER = '155069_015636_25102026223510.pdf'
PDF_FILE = f'{BASE}/native-155069-v1-{PDF_SHA}.pdf'
URL = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
       '?CodigoInstituicao=1&NumeroSequencialDocumento=155069')
INDEX = ('raw/primary-research/cvm-documents/index-2026-'
         'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip')
INDEX_SHA = 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c'
IPE_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
           '&CodigoInstituicao=1&numProtocolo=1487615&numSequencia=1012321&numVersao=2')
IPE_SHA = '2e88a2fc47d50d102351d3c8f1ff468c0b12a8616aafa44c327e43de39517150'
IPE_FILE = f'{BASE}/annual-IPE2-{IPE_SHA}.pdf'
RECEIPT_PINS = {
    URL: '0e4e2751ae43143cfb9a6f231968ebca6650a1d4e885383823ce115753c6ebe3',
    IPE_URL: '5ce78ff6334e5b52694ada67a13f5e43f7f19919a91c8ef4634379d64ef1e287',
}
FCA = 'raw/inventory/fca_cia_aberta_2026.zip'
FCA_SHA = 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
HANDOFF_SHA = '76731f565d822758d34e1b3c5ca3eeb2585aed25facdd83198e550e72743f9db'
CASH_MANIFEST_SHA = 'f71c25995f4c9bd04e1e29bfef1645d222fe8c7158d859d64c7b93239ab9c98b'
READING_FILE = f'{BASE}/review-comgas-current.json'
READING_SHA = '193524c72d772126dffab699aa0427e2c1868ac31d3d9c081e807044ddd9c91e'
PACKAGE_SHA = 'c4cf2433fac8c720f69b07c086bf5f1cd7002294f28428cc565036c4953d5434'
REVIEW_PINS = {
    'native': 'ef293542332d8c64da86ea85cb06b3a242d0a6cf79ce3aae4ccb34183ad7c5b1',
    'annual-IPE2': '49272277dcd4b3972b37cd1b8afe72d6e5f5aa48330868549f7e10c525968fc8',
}
CAPEX_LITERAL = 'Adi\u00e7\u00f5es ao intang\u00edvel e ativos de contrato'
STOPPED = ('api.mziq.com', 'd169uzu5o4xu1k.cloudfront.net', 'components.mz-sites.com',
           'uol.com.br', 'ri.azzas2154.com.br', 'engie.com.br', 'allos.com.br',
           'santander.com.br', 'cdn-sites-assets.mziq.com')


def require(condition, reason):
    if not condition:
        raise ValueError('comgas_financial_' + reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def read(folder, relative, digest=None):
    root = Path(folder).resolve()
    path = root / relative
    require(path.resolve().is_relative_to(root) and not path.is_symlink(), 'unsafe_source_path')
    body = path.read_bytes()
    require(digest is None or sha(body) == digest, 'source_bytes_changed')
    return body


def save(path, value):
    path = Path(path)
    body = value if isinstance(value, bytes) else (
        json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_evidence_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def current_tables(folder=ROOT, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'unreviewed_cutoff')
    with zipfile.ZipFile(io.BytesIO(read(folder, ANNUAL, ANNUAL_SHA))) as archive:
        eligible = [r for r in native._rows(archive, 'dfp_cia_aberta_2025.csv')
            if int(r['CD_CVM']) == KEY[0] and native._day(r['DT_REFER']) <= cutoff
            and native._day(r['DT_RECEB']) <= cutoff]
        master = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
        require(tuple(master[k] for k in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
            == ('2025-12-31', '1', '155069', '2026-03-04', '61.856.571/0001-17')
            and master['LINK_DOC'].replace('http://', 'https://', 1) == URL,
            'latest_filing_changed_no_fallback')
        tables = {}
        for name in archive.namelist():
            if re.fullmatch(r'dfp_cia_aberta_(DRE|BPA|BPP|DFC_MI|DFC_MD|DRA|DVA|DMPL)_(con|ind)_2025.csv', name):
                tables[name.removeprefix('dfp_cia_aberta_').removesuffix('_2025.csv')] = [
                    r for r in native._rows(archive, name) if native._key(r) == KEY
                    and r.get('ORDEM_EXERC') == '\u00daLTIMO']
    require(not any(rows for name, rows in tables.items() if name.endswith('_con')),
            'consolidated_core_now_present_no_scope_override')
    require(native._dre_fingerprint(tables['DRE_ind'], full=True) == DRE_SHA
        and native._dre_fingerprint(tables['DFC_MI_ind'], full=True) == DFC_SHA,
        'current_individual_DRE_or_DFC_changed')
    return master, tables


def ipe_rows(folder=ROOT):
    with zipfile.ZipFile(io.BytesIO(read(folder, INDEX, INDEX_SHA))) as archive:
        with archive.open('ipe_cia_aberta_2026.csv') as stream:
            rows = [r for r in csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
                if r['Codigo_CVM'] == '15636' and r['CNPJ_Companhia'] == '61.856.571/0001-17'
                and r['Data_Referencia'] == '2025-12-31'
                and r['Tipo'] == 'Demonstra\u00e7\u00f5es Financeiras Anuais Completas']
    require([(r['Versao'], r['Data_Entrega']) for r in rows]
        == [('1', '2026-03-04'), ('2', '2026-03-09')]
        and rows[-1]['Link_Download'] == IPE_URL, 'latest_annual_IPE_changed')
    return rows


def transport(folder, url, digest, size):
    receipt_file = f'{BASE}/response-{sha(url.encode())}.json'
    receipt = json.loads(read(folder, receipt_file, RECEIPT_PINS[url]))
    body = read(folder, receipt['file'], digest)
    require(receipt['url'] == url and receipt['status'] == 200 and receipt['error'] is None
        and receipt['sha256'] == digest and receipt['bytes'] == len(body) == size
        and receipt['retries'] == 0 and receipt['scope'] == 'round3-comgas-financial'
        and datetime.fromisoformat(receipt['retrieved_at']).utcoffset() is not None,
        'actual_complete_transport_receipt_changed')
    return receipt, body, receipt_file


def extract_originals(folder=ROOT):
    master, _ = current_tables(folder)
    receipt, body, receipt_file = transport(folder, URL, CONTAINER_SHA, 4_782_511)
    require(receipt['discovery']['master'] == master, 'DFP_discovery_master_changed')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        require(archive.testzip() is None, 'native_container_CRC_failed')
        xml = archive.read('015636DFP31-12-2025v1.xml')
        root = ET.fromstring(xml)
        require(sha(xml) == XML_SHA and tuple(root.findtext(field) for field in (
            'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa', 'Documento/VersaoDocumento',
            'DadosDFP/DataReferencia')) == ('015636', '61856571000117', '1', '31/12/2025'),
            'native_XML_identity_changed')
        pdf = archive.read(PDF_MEMBER)
        require(sha(pdf) == PDF_SHA and pdf.startswith(b'%PDF-'), 'native_PDF_changed')
        save(Path(folder) / PDF_FILE, pdf)
        xml_file = f'{BASE}/native-155069-v1-{XML_SHA}.xml'
        save(Path(folder) / xml_file, xml)
    ipe, complete, ipe_receipt = transport(folder, IPE_URL, IPE_SHA, 2_942_957)
    require(ipe['discovery']['index_row'] == ipe_rows(folder)[-1]
        and complete.startswith(b'%PDF-'), 'final_annual_IPE_discovery_changed')
    save(Path(folder) / IPE_FILE, complete)
    proof = {'native': {'file': PDF_FILE, 'sha256': PDF_SHA, 'bytes': len(pdf), 'url': URL,
        'retrieved_at': receipt['retrieved_at'], 'published_at': master['DT_RECEB'],
        'publication_basis': 'CVM_DFP_master_DT_RECEB', 'receipt_file': receipt_file,
        'original_container': receipt['file'], 'original_container_sha256': CONTAINER_SHA,
        'original_zip_member': PDF_MEMBER, 'XML_file': xml_file, 'XML_sha256': XML_SHA,
        'exact_filing_verified': True, 'DFP_version': '1'},
        'annual_IPE': {'file': IPE_FILE, 'sha256': IPE_SHA, 'bytes': len(complete), 'url': IPE_URL,
            'original_body': ipe['file'], 'receipt_file': ipe_receipt,
            'retrieved_at': ipe['retrieved_at'], 'published_at': ipe_rows(folder)[-1]['Data_Entrega'],
            'publication_basis': 'CVM_IPE_Data_Entrega', 'IPE_version': '2',
            'DFP_version_not_changed': '1', 'same_FY': '2025-12-31',
            'not_claimed_byte_identical_to_embedded_DFP_PDF': True},
        'bytes_unchanged_no_PDF_resave_or_padding_removal': True,
        'point_in_time_archive_verified': False, 'bank_settlement_verified': False}
    save(Path(folder) / BASE / 'package-comgas.json', proof)
    return proof


def inspect_pdf(relative, numbers, label, folder=ROOT):
    from pypdf import PdfReader

    folder = Path(folder)
    body = read(folder, relative)
    reader = PdfReader(io.BytesIO(body))
    executable = shutil.which('pdftoppm')
    require(executable is not None, 'Poppler_required')
    result = {'source_file': relative, 'source_sha256': sha(body),
              'total_pages': len(reader.pages), 'pages': []}
    for number in numbers:
        require(type(number) is int and 1 <= number <= len(reader.pages), 'invalid_page')
        prefix = folder / BASE / f'review-{label}-p{number}'
        subprocess.run([executable, '-f', str(number), '-l', str(number), '-scale-to', '1800',
            '-png', '-singlefile', str(folder / relative), str(prefix)],
            check=True, capture_output=True, timeout=45)
        png = prefix.with_suffix('.png')
        result['pages'].append({'pdf_page': number, 'text': reader.pages[number - 1].extract_text(),
            'render': png.relative_to(folder).as_posix(), 'render_sha256': sha(png.read_bytes())})
    save(folder / BASE / f'pages-{label}.json', result)
    return result


def fetch(url, discovery, folder=ROOT):
    host = (urlsplit(url).hostname or '').lower()
    require(approved(url) and not any(host == d or host.endswith('.' + d) for d in STOPPED),
            'stopped_or_unapproved_origin')
    collector = PublicSourceCollector(folder, 'round3-comgas-financial')
    receipt = collector.fetch(url, discovery=discovery)
    return receipt, collector.body(receipt)


def snapshot(statements, diagnostics):
    result = {}
    for ticker, spec in SPECS.items():
        selected = financial_features(statements, [], CUTOFF, spec)
        result[ticker] = {'features': None if selected is None else selected[0],
            'record': None if selected is None else selected[1],
            'latest_records': [r for r in statements if r['cvm'] == spec[0]
                and r['share_class'] == spec[1] and r['fiscal_end'] == '2025-12-31'],
            'candidate_diagnostics': [r for r in diagnostics if r.get('ticker') == ticker
                and r.get('kind') == 'candidate' and r.get('fiscal_end') == '2025-12-31']}
    return result


def normal_snapshot(folder=ROOT):
    statements, diagnostics, _ = native.prepare_financials(folder, SPECS, CUTOFF)
    return snapshot(statements, diagnostics)


def verify_pages(folder, label):
    from pypdf import PdfReader

    review = json.loads(read(folder, f'{BASE}/pages-{label}.json', REVIEW_PINS[label]))
    body = read(folder, review['source_file'], review['source_sha256'])
    reader = PdfReader(io.BytesIO(body))
    require(len(reader.pages) == review['total_pages'], 'PDF_page_count_changed')
    for page in review['pages']:
        require(reader.pages[page['pdf_page'] - 1].extract_text() == page['text'],
                'extracted_page_text_changed')
        read(folder, page['render'], page['render_sha256'])
    return review


def class_registry(folder=ROOT):
    from .round3_comgas_cash import _registry_year

    registry = _registry_year(read(folder, FCA, FCA_SHA), 2026)
    equity = [r for r in registry['all_issuer_security_rows'] if r['Valor_Mobiliario'].startswith('A\u00e7\u00f5es')]
    require([(r['Codigo_Negociacao'], r['Valor_Mobiliario']) for r in equity] == [
        ('CGAS3', 'A\u00e7\u00f5es Ordin\u00e1rias'), ('CGAS5', 'A\u00e7\u00f5es Preferenciais')],
        'literal_class_code_registry_changed')
    return registry


def cash_handoff_reference(folder=ROOT):
    handoff_file = REPORTS / 'round3-comgas-handoff-final.json'
    body = handoff_file.read_bytes()
    require(sha(body) == HANDOFF_SHA, 'upstream_cash_handoff_changed')
    handoff = json.loads(body)
    read(folder, handoff['source_manifest']['file'], CASH_MANIFEST_SHA)
    require(handoff['source_manifest']['sha256'] == CASH_MANIFEST_SHA
        and handoff['actual_annual_gate']['filing_id'] == '155069', 'cash_manifest_reference_changed')
    return {'file': str(handoff_file), 'sha256': HANDOFF_SHA,
        'source_manifest': handoff['source_manifest'],
        'use': 'upstream_task_context_not_primary_financial_proof',
        'cash_and_one_findings_not_repaired_by_financial_reading': True}


def numeric_eps_proof():
    return {'profit_account': '3.11', 'parent_profit_brl': '1546478000',
        'profit_allocations_brl': {'ON': '1186393000', 'PN': '360085000'},
        'basic_eps': [
            {'account': '3.99.01.01', 'share_class': 'ON', 'value': '11.4227',
             'numerator_brl': '1186393000', 'weighted_shares': '103863000', 'allocation': 'ON',
             'numerator_rounding_brl': '500', 'shares_rounding': '500'},
            {'account': '3.99.01.02', 'share_class': 'PN', 'value': '12.5650',
             'numerator_brl': '360085000', 'weighted_shares': '28658000', 'allocation': 'PN',
             'numerator_rounding_brl': '500', 'shares_rounding': '500'}]}


def reading_definition():
    return {'cvm': 15636, 'fiscal_end': '2025-12-31', 'version': '1', 'filing_id': '155069',
        'published_at': '2026-03-04', 'accounts_scope': 'individual',
        'full_dre': True, 'dre_sha256': DRE_SHA, 'dfc_sha256': DFC_SHA,
        'native_archive_sha256': ANNUAL_SHA, 'profit_account': '3.11',
        'parent_profit_brl': '1546478000', 'basic_eps': [],
        'printed_EPS_crosscheck': numeric_eps_proof(),
        'eps_label_aliases': {'3.99.01.01': {'original': 'Ordin\u00e1rias', 'class': 'ON'},
                              '3.99.01.02': {'original': 'Preferenciais', 'class': 'PN'}},
        'cash_capex_alias': {'account': '6.02.03', 'original': 'Adi\u00e7\u00f5es ao ativo e ativos de contrato',
            'literal_audited_DFC': CAPEX_LITERAL, 'reported_value': '-1452748.0000000000',
            'reported_scale': 'MIL', 'gross_cash_outflow_brl': '1452748000',
            'sale_proceeds_separate_account': '6.02.02', 'sale_proceeds_brl': '1638000',
            'net_disposals_or_accrual_rollforward_used': False},
        'source_package_sha256': PACKAGE_SHA, 'visual_review_sha256': REVIEW_PINS,
        'primary_evidence': {'source_ref': 'primary-financials:' + IPE_SHA, 'pdf_page': 17,
            'note': 'Audited FY2025 DFC p17 prints gross additions to intangible/contract assets '
                'separately from sale receipts. Native155069 v1 p6 and note28 p66 print exact '
                'ordinary/preferred basic EPS; final annual IPE2 p13/p72 confirms. No numeric edit.'},
        'EPS_recomputed_or_class_ratio_applied': False, 'scope_override': False}


def verify_evidence(folder=ROOT, cutoff=CUTOFF):
    master, tables = current_tables(folder, cutoff)
    proof = json.loads(read(folder, f'{BASE}/package-comgas.json', PACKAGE_SHA))
    receipt, container, _ = transport(folder, URL, CONTAINER_SHA, 4_782_511)
    require(receipt['discovery']['master'] == master, 'actual_DFP_master_changed')
    with zipfile.ZipFile(io.BytesIO(container)) as archive:
        require(archive.testzip() is None and archive.read(PDF_MEMBER) == read(folder, PDF_FILE, PDF_SHA),
                'native_PDF_member_changed')
        xml = read(folder, proof['native']['XML_file'], XML_SHA)
        require(archive.read('015636DFP31-12-2025v1.xml') == xml, 'native_XML_member_changed')
        tree = ET.fromstring(xml)
        require(tuple(tree.findtext(field) for field in ('DadosEmpresa/CodigoCvm',
            'DadosEmpresa/CnpjEmpresa', 'Documento/VersaoDocumento', 'DadosDFP/DataReferencia'))
            == ('015636', '61856571000117', '1', '31/12/2025'), 'native_identity_changed')
    ipe, complete, _ = transport(folder, IPE_URL, IPE_SHA, 2_942_957)
    require(ipe['discovery']['index_row'] == ipe_rows(folder)[-1]
        and complete == read(folder, IPE_FILE, IPE_SHA), 'same_year_final_annual_changed')
    reviews = {label: verify_pages(folder, label) for label in REVIEW_PINS}
    native_pages = {p['pdf_page']: ' '.join(p['text'].split()) for p in reviews['native']['pages']}
    final_pages = {p['pdf_page']: ' '.join(p['text'].split()) for p in reviews['annual-IPE2']['pages']}
    require('Ordin\u00e1rias 11,4227' in native_pages[6]
        and 'Preferenciais 12,565' in native_pages[6]
        and all(v in native_pages[66] for v in ('1.186.393', '360.085', '103.863', '28.658', '12,5650'))
        and all(v in final_pages[72] for v in ('1.186.393', '360.085', '103.863', '28.658', '11,4227')),
        'printed_class_EPS_basis_changed')
    require(CAPEX_LITERAL + ' (1.452.748)' in final_pages[17]
        and 'Caixa recebido na venda de outros ativos 1.638' in final_pages[17]
        and '3.514.036' in final_pages[17] and 'obras em andamento' in final_pages[39]
        and 'S\u00e3o Paulo, 4 de mar\u00e7o de 2026' in final_pages[10],
        'gross_cash_capex_or_annual_audit_changed')
    reading = reading_definition()
    labeled = deepcopy(tables['DRE_ind'])
    for row in labeled:
        alias = reading['eps_label_aliases'].get(row['CD_CONTA'])
        if alias:
            require(row['DS_CONTA'] == alias['original'], 'native_class_label_changed')
            row['DS_CONTA'] = alias['class']
    native._checked_note_eps(labeled, numeric_eps_proof())
    registry = class_registry(folder)
    sources = []
    for label in ('native', 'annual_IPE'):
        s = proof[label]
        sources.append({**s, 'id': 'primary-financials:' + s['sha256'], 'authority': 'primary',
            'use': 'exact_current_account_interpretation', 'point_in_time_archive_verified': False})
    sources.append({'id': 'primary-financials:' + FCA_SHA, 'file': FCA, 'sha256': FCA_SHA,
        'bytes': len(read(folder, FCA, FCA_SHA)), 'retrieved_at': None,
        'retrieval_clock_basis': 'not_reconstructed_from_mtime', 'authority': 'primary',
        'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip',
        'use': 'literal_current_class_registry', 'published_at': registry['master']['DT_RECEB']})
    return {'reading': reading, 'sources': sources, 'class_registry': registry,
        'cash_handoff_context': cash_handoff_reference(folder), 'master': master}


def freeze_reading(folder=ROOT):
    evidence = verify_evidence(folder)
    save(Path(folder) / READING_FILE, evidence['reading'])
    return sha(read(folder, READING_FILE))


def get_comgas_financial_reading(folder=ROOT, cutoff=CUTOFF):
    evidence = verify_evidence(folder, cutoff)
    require(READING_SHA is not None, 'reading_not_frozen')
    reading = json.loads(read(folder, READING_FILE, READING_SHA))
    require(reading == evidence['reading'], 'unreviewed_reading')
    return {**reading, 'manifest_file': READING_FILE, 'manifest_sha256': READING_SHA}, evidence['sources']


def interpret_current_tables(tables, diagnostics, scope, reading, *, eps=True, capex=True):
    require(reading_definition() == {k: v for k, v in reading.items()
        if k not in ('manifest_file', 'manifest_sha256')}, 'unreviewed_table_interpretation')
    if scope != 'ind':
        return tables
    dre = [r for r in tables['_DRE_ind'] if native._key(r) == KEY]
    if not dre:
        return tables
    dfc = [r for r in tables['_DFC_MI_ind'] if native._key(r) == KEY]
    require(native._dre_fingerprint(dre, full=True) == DRE_SHA
        and native._dre_fingerprint(dfc, full=True) == DFC_SHA, 'in_memory_source_cells_changed')
    result = deepcopy(tables)
    for suffix, rows in result.items():
        for row in rows:
            if native._key(row) != KEY:
                continue
            alias = reading['eps_label_aliases'].get(row['CD_CONTA'])
            replacement = None
            if eps and suffix == '_DRE_ind' and alias:
                replacement = alias['class']
            if capex and suffix == '_DFC_MI_ind' and row['CD_CONTA'] == '6.02.03':
                replacement = reading['cash_capex_alias']['literal_audited_DFC']
            if replacement:
                diagnostics.append({**native._account_diagnostic(KEY, suffix, row, status='recognized',
                    reasons=['documented_same_filing_literal_account_label']),
                    'literal_parser_label': replacement, 'primary_evidence': reading['primary_evidence']})
                row['DS_CONTA'] = replacement
    return result


def prepare_comgas_financials(folder=ROOT, candidates=None, cutoff=CUTOFF, *, eps=True, capex=True):
    candidates = SPECS if candidates is None else candidates
    require(isinstance(candidates, dict) and candidates
        and all(t in SPECS and isinstance(s, (tuple, list)) and len(s) == 3
            and type(s[0]) is int and type(s[2]) is int and tuple(s) == SPECS[t]
            for t, s in candidates.items()),
        'exact_codes_classes_sector_required')
    reading, receipts = get_comgas_financial_reading(folder, cutoff)
    original_research, original_parser = native._research, native._parser_tables

    def research(root, specs, day, diagnostics, sources):
        reviews = original_research(root, specs, day, diagnostics, sources)
        key = (*KEY, '155069')
        require(key not in reviews or reviews[key] == reading, 'conflicting_documentary_reader')
        reviews[key] = reading
        sources.extend(deepcopy(receipts))
        return reviews

    def parser(tables, diagnostics, indirect_keys, scope='con'):
        interpreted = interpret_current_tables(tables, diagnostics, scope, reading, eps=eps, capex=capex)
        return original_parser(interpreted, diagnostics, indirect_keys, scope)

    # Hooks exist only in this isolated call, with normal scope, period and numeric gates intact.
    with patch.object(native, '_research', research), patch.object(native, '_parser_tables', parser):
        return native.prepare_financials(folder, candidates, cutoff)


def write_handoff(folder=ROOT):
    evidence = verify_evidence(folder)
    reading, sources = get_comgas_financial_reading(folder)
    before = json.loads((REPORTS / 'financial-comgas-before.json').read_bytes())
    statements, diagnostics, _ = prepare_comgas_financials(folder)
    proposed = snapshot(statements, diagnostics)
    restored = normal_snapshot(folder)
    require(restored == before, 'unpatched_parser_changed_after_isolated_replay')
    require(all(r['features'] is not None and r['record']['filing_id'] == '155069'
        and r['record']['accounts_scope'] == 'individual' for r in proposed.values()),
        'proposed_current_financials_not_recovered')
    save(REPORTS / 'financial-comgas-proposed-after.json', proposed)
    save(REPORTS / 'financial-comgas-unpatched-after.json', restored)
    responses = [json.loads(p.read_bytes()) for p in sorted((Path(folder) / BASE).glob('response-*.json'))]
    report = {'schema': 'round3-comgas-current-financial-handoff-v1',
        'status': 'isolated_literal_reading_proposal_ready_for_parent_review_not_integrated',
        'cutoff': CUTOFF.isoformat(), 'current_annual': evidence['master'],
        'reading_file': READING_FILE, 'reading_sha256': READING_SHA, 'reading': reading,
        'sources': sources, 'class_registry': evidence['class_registry'],
        'upstream_cash_handoff': evidence['cash_handoff_context'],
        'before': before, 'proposed_after': proposed, 'unpatched_after': restored,
        'unpatched_before_after_identical': True,
        'before_sha256': sha((REPORTS / 'financial-comgas-before.json').read_bytes()),
        'proposed_after_sha256': sha((REPORTS / 'financial-comgas-proposed-after.json').read_bytes()),
        'exact_literal_fields': {'ordinary_basic_EPS_brl': '11.4227', 'preferred_basic_EPS_brl': '12.5650',
            'class_profit_allocations_brl': {'ON': '1186393000', 'PN': '360085000'},
            'reported_weighted_shares': {'ON': '103863000', 'PN': '28658000'},
            'weighted_share_reporting_unit': 'thousand_shares_with_500_share_rounding_bound',
            'gross_cash_capex_brl': '1452748000', 'operating_cash_brl': '3514036000',
            'sale_receipts_brl_separate_not_netted': '1638000',
            'portfolio_securities_outflow_brl_excluded_from_capex': '260118000'},
        'native_locators': {'DFP': {'class_EPS': 6, 'cash_flow': 8, 'EPS_policy_and_basis': [65, 66],
            'contract_asset_policy': 33, 'audit_opinion_date': [69, 71]},
            'final_annual_IPE2': {'audited_DRE': 13, 'gross_cash_capex_DFC': 17,
                'EPS_policy_and_basis': [71, 72], 'contract_asset_policy': 39,
                'audit_opinion_signature': [5, 10]}},
        'interpretation': 'Native155069 v1 contains real reported class EPS under Portuguese '
            'class names. Sole current FCA equity rows map those literal names to CGAS3 ON and '
            'CGAS5 PN. Same audited FY2025 IPE2 DFC restores the missing word intangible in '
            'the native DFC6.02.03 label. Its cash amount is unchanged and asset sale cash is separate.',
        'not_used': ['10_percent_preferred_ratio_to_manufacture_EPS', 'profit_divided_into_invented_class_EPS',
            'capital_or_year_end_shares_substituted_for_weighted_note_counts',
            'net_asset_sales_as_gross_capex', 'contract_asset_accrual_additions_or_capitalized_interest_as_cash',
            'older_DFP_or_annualized_ITR', 'consolidated_or_financial_sector_substitution'],
        'legacy_method_explicit': 'industrial_cash_capex_v1 remains unchanged. Its existing '
            'FCF-per-share calculation uses total issuer profit/native class EPS as its effective '
            'share basis; it was not replaced with a new denominator or class allocation formula. '
            'Printed weighted counts are used only to validate printed EPS against printed class numerators.',
        'cash_input_for_financial_only_test': 'Empty input is not a claim of zero distributions.',
        'cash_and_model_admission': 'No cash/quote repairs applied, no _one result overwritten. '
            'Prior CGAS3 cash conflicts remain; CGAS5 prior cash pass does not alone imply admission.',
        'proposed_parent_hooks': ['get_comgas_financial_reading: register the verified documentary '
            'review in the existing _research path only for the exact issuer/classes/sector/cutoff.',
            'interpret_current_tables: apply immediately before _parser_tables, after documentary '
            'validation, for exact individual2025v1 rows. Preserve every numeric field and account code.',
            'prepare_comgas_financials: isolated wrapper demonstrates both hooks with ordinary '
            'prepare_financials and financial_features; no source/core/pointer write.'],
        'new_network_receipts': [{k: r.get(k) for k in ('url', 'status', 'error', 'bytes', 'file',
            'sha256', 'started_at', 'retrieved_at', 'published_at', 'scope')} for r in responses],
        'network_policy': {'only_actual_public_CVM_links': True, 'new_B3_bvmf_bovespa_MZiQ_UOL_requests': 0,
            'credentials_authentication_TLS_bypass_retries': False},
        'changed_code_files': ['recommender/round3_comgas_financial.py',
                               'recommender/test_round3_comgas_financial.py'],
        'changed_raw_scope': BASE, 'changed_reports': '../work/recovery_round2_20261025/financial-comgas-*',
        'core_other_agent_inventory_pointer_document_edits': False,
        'dataset_model_forecast_training_publication_performed': False, 'new_active_model_admissions': 0}
    save(REPORTS / 'financial-comgas-handoff-final.json', report)
    return report
