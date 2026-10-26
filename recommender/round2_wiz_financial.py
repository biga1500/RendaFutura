"""Current WIZ native EPS scope review, isolated from production admission."""

import csv
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit
import zipfile

from . import inventory_financials as native
from .bulk_pending_sources import PublicSourceCollector, approved
from .collect_market_data import financial_features


ROOT = Path(__file__).resolve().parent / 'data/market'
BASE = 'raw/primary-research/all-pending/round2-wiz-financial'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
CUTOFF = date(2026, 9, 30)
SPECS = {'WIZC3': (23590, 'ON', 2)}
SIMH_SPEC = (25003, 'ON', 2)
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
DRE_SHA = '03452a4c9674e1b69ca40ba56ed08dafdafcd86d694bc7af9853e6d35264c117'
PDF_SHA = '77c3d97099d285f441cf6730df88a6666424cd7cb07d56dc24ded80fd27fc59e'
ORIGINAL_PDF = f'raw/primary-research/financials/wiz-native-155497-v1-{PDF_SHA}.pdf'
CACHED_PDF = f'{BASE}/cached-WIZC3-{PDF_SHA}.pdf'
INDEX = ('raw/primary-research/cvm-documents/index-2026-'
         'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip')
INDEX_SHA = 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c'
IPE2_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
            '&CodigoInstituicao=1&numProtocolo=1498888&numSequencia=1023594&numVersao=2')
IPE2_BODY_SHA = '6ba57d2bb0ca260f66daeb00ee779a09676c6ecb2b9521c09fbf36c775be693b'
SIMH_DRE_SHA = 'f52645a12fa61988221312627ea3a1c9f1db916ba72341ba32eab0594c9d2d5d'
SIMH_PDF_SHA = '2d0bdfc8ddde46a2a6ef5c597d6919a6634763e8b31332b270784207fb1bdcd9'
SIMH_IPE_SHA = '649c51bec9afc76eff08bcc87dbdcd6980f1c8ba87208810b1dfa9d5ed5c1f82'
SIMH_IPE_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
                '&CodigoInstituicao=1&numProtocolo=1502476&numSequencia=1027182&numVersao=3')
SOURCE_METADATA_SHA = 'b328cfd7f4e1ce9d8bd5d15e73a337dc1c68cea1acdccdbd43a0938e9deca4ae'
SIMH_METADATA_SHA = '11d59e1ecb533d655f61e64d9ffc8a323fe6660e7b7f7c2415d2eb80449eccf9'
REVIEW_PINS = {
    'native': '977bd22221424e0415826bf465bbbe6960311cf806b54e3ec9838ceb5d6444e0',
    'ipe2-incomplete': 'd03d69e28a3349ab04ec14b1f58a32ea80feef4ad783f8fc4ae822378404c757',
    'simh-native': 'a37c7cec3bd14a7da25dc1464a9cb5435cdb7061724078b79b6921d08d30fb57',
    'simh-IPE3': '8a3d0e78e97457825d805bea1f866ae35d6a61405725c86072878e55482ccea8',
}
STOPPED_ORIGINS = ('api.mziq.com', 'd169uzu5o4xu1k.cloudfront.net',
                   'components.mz-sites.com', 'ri.wiz.co')


def require(condition, reason):
    if not condition:
        raise ValueError('wiz_financial_' + reason)


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
            if int(r['CD_CVM']) == 23590 and native._day(r['DT_REFER']) <= cutoff
            and native._day(r['DT_RECEB']) <= cutoff]
        master = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
        require(tuple(master[k] for k in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
            == ('2025-12-31', '1', '155497', '2026-03-19', '42.278.473/0001-03'),
            'latest_filing_changed_no_fallback')
        tables = {}
        for suffix in ('DRE_con', 'DRE_ind', 'BPA_con', 'BPP_con', 'DFC_MI_con'):
            tables[suffix] = [r for r in native._rows(archive, f'dfp_cia_aberta_{suffix}_2025.csv')
                if native._key(r) == native._key(master) and r['ORDEM_EXERC'] == '\u00daLTIMO']
    require(native._dre_fingerprint(tables['DRE_con'], full=True) == DRE_SHA,
            'current_consolidated_cells_changed')
    return master, tables


def annual_ipe_rows(folder=ROOT):
    with zipfile.ZipFile(io.BytesIO(read(folder, INDEX, INDEX_SHA))) as archive:
        with archive.open('ipe_cia_aberta_2026.csv') as stream:
            rows = [r for r in csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
                if r['Codigo_CVM'] == '23590' and r['Data_Referencia'] == '2025-12-31'
                and r['Tipo'] == 'Demonstra\u00e7\u00f5es Financeiras Anuais Completas']
    require([(r['Versao'], r['Data_Entrega']) for r in rows]
            == [('1', '2026-03-19'), ('2', '2026-03-31')], 'annual_IPE_index_changed')
    require(rows[-1]['Link_Download'] == IPE2_URL, 'actual_IPE_download_link_changed')
    return rows


def fetch(url, discovery, folder=ROOT):
    host = (urlsplit(url).hostname or '').lower()
    require(approved(url) and not any(host == d or host.endswith('.' + d)
            for d in STOPPED_ORIGINS), 'stopped_or_unapproved_origin')
    collector = PublicSourceCollector(folder, 'round2-wiz-financial')
    receipt = collector.fetch(url, discovery=discovery)
    return receipt, collector.body(receipt)


def import_cached_pdf(folder=ROOT):
    current_tables(folder)
    body = read(folder, ORIGINAL_PDF, PDF_SHA)
    require(len(body) == 16_777_216 and body.startswith(b'%PDF-'), 'cached_original_changed')
    save(Path(folder) / CACHED_PDF, body)
    source = {'file': CACHED_PDF, 'sha256': PDF_SHA, 'bytes': len(body),
        'original_file': ORIGINAL_PDF, 'new_request': False, 'retrieved_at': None,
        'retrieval_clock_basis': 'not_retained_never_inferred_from_mtime_or_member_name',
        'published_at': '2026-03-19', 'publication_basis': 'current_CVM_DFP_master_DT_RECEB',
        'filing_id': '155497', 'version': '1', 'fiscal_end': '2025-12-31',
        'url': 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
               '?CodigoInstituicao=1&NumeroSequencialDocumento=155497',
        'binding': 'Native PDF has exact issuer/year/version form headers; original complete '
            'container and its acquisition clock are not retained. Not re-certified as transport proof.',
        'original_container_verified': False, 'scope': 'joint_individual_and_consolidated',
        'bytes_unchanged_including_trailing_padding': True, 'model_admission': False}
    save(Path(folder) / BASE / 'cached-WIZC3.json', source)
    return source


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


def parser_snapshot(folder=ROOT, ticker='WIZC3'):
    require(ticker in ('WIZC3', 'SIMH3'), 'ticker_outside_disjoint_scope')
    specs = {ticker: SPECS['WIZC3'] if ticker == 'WIZC3' else SIMH_SPEC}
    statements, diagnostics, _ = native.prepare_financials(folder, specs, CUTOFF)
    selected = financial_features(statements, [], CUTOFF, specs[ticker])
    return {'features': None if selected is None else selected[0],
        'record': None if selected is None else selected[1],
        'records': statements,
        'latest_candidate_diagnostics': [r for r in diagnostics if r.get('ticker') == ticker
            and r.get('kind') == 'candidate' and r.get('fiscal_end') == '2025-12-31'],
        'cutoff': CUTOFF.isoformat(), 'cash_input': 'empty_not_a_claim_of_zero_distributions'}


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


def verify_incomplete_ipe2(folder=ROOT):
    rows = annual_ipe_rows(folder)
    receipt_file = f'{BASE}/response-{sha(IPE2_URL.encode())}.json'
    receipt = json.loads(read(folder, receipt_file))
    require(receipt['url'] == rows[-1]['Link_Download'] and receipt['status'] == 200
        and receipt['error'] == 'response_size_limit' and receipt['bytes'] == 16_000_001
        and receipt['sha256'] == IPE2_BODY_SHA and receipt['retries'] == 0
        and receipt['scope'] == 'round2-wiz-financial', 'incomplete_transport_identity_changed')
    require(receipt['discovery']['index_row'] == rows[-1], 'IPE_discovery_mismatch')
    require(datetime.fromisoformat(receipt['retrieved_at']).utcoffset() is not None,
            'retrieval_timezone_missing')
    body = read(folder, receipt['file'], IPE2_BODY_SHA)
    require(len(body) == receipt['bytes'] and body.startswith(b'%PDF-'), 'incomplete_body_changed')
    review = verify_pages(folder, 'ipe2-incomplete')
    require(review['source_file'] == receipt['file'], 'incomplete_visual_source_mismatch')
    return {'receipt_file': receipt_file, 'file': receipt['file'], 'sha256': IPE2_BODY_SHA,
        'url': IPE2_URL, 'retrieved_at': receipt['retrieved_at'], 'bytes': len(body),
        'published_at': rows[-1]['Data_Entrega'], 'publication_basis': 'CVM_IPE_Data_Entrega',
        'IPE_version': '2', 'embedded_DFP_header_version': '1',
        'complete_delivery': False, 'eligible_for_reading': False, 'model_admission': False,
        'status': 'response_size_limit_not_a_verified_complete_source',
        'visual_pages_for_research_only': [56, 102],
        'observed_page_content': 'Received pages still label EPS Controladora and consolidated '
            'EPS n/a. This partial body is not promoted to a complete same-filing source.',
        'DFP_version_not_replaced_by_IPE_version': True}


def individual_numeric_crosscheck_only(rows):
    # Deliberately demonstrate that arithmetic validation cannot establish documentary scope.
    native._checked_note_eps(rows, {'parent_profit_brl': '201076000',
        'profit_account': '3.11.01', 'basic_eps': [{'account': 'note:24.4:ON',
            'share_class': 'ON', 'value': '1.25745', 'numerator_brl': '201076000',
            'weighted_shares': '159907282', 'numerator_rounding_brl': '500',
            'shares_rounding': '0.5'}]})
    return {'numeric_crosscheck_passes': True, 'source_table_scope': 'Controladora',
        'consolidated_scope_proved_by_numeric_check': False, 'eligible_for_reading': False}


def review_wiz_financial(folder=ROOT, cutoff=CUTOFF):
    master, tables = current_tables(folder, cutoff)
    source = json.loads(read(folder, f'{BASE}/cached-WIZC3.json', SOURCE_METADATA_SHA))
    read(folder, source['file'], PDF_SHA)
    require(read(folder, ORIGINAL_PDF, PDF_SHA) == read(folder, source['file'], PDF_SHA),
            'cached_original_not_byte_identical')
    review = verify_pages(folder, 'native')
    require(review['source_file'] == CACHED_PDF and review['source_sha256'] == PDF_SHA
        and [p['pdf_page'] for p in review['pages']] == [19, 56, 57, 62, 64, 65, 101, 102, 121, 124],
        'native_visual_source_mismatch')
    pages = {p['pdf_page']: ' '.join(p['text'].split()) for p in review['pages']}
    require('DFs Consolidadas / Demonstra\u00e7\u00e3o do Resultado' in pages[19]
        and 'Vers\u00e3o : 1' in pages[19] and '31/12/2025' in pages[19], 'native_header_changed')
    require('159.907.282 159.907.282 n/a n/a' in pages[56]
        and '1,25745 1,00127 n/a n/a' in pages[56]
        and 'Controladora' in pages[102] and 'A\u00e7\u00f5es Ordin\u00e1rias' in pages[102],
        'native_EPS_scope_changed')
    require('18 de mar\u00e7o de 2026' in pages[124], 'native_audit_date_changed')
    incomplete = verify_incomplete_ipe2(folder)
    return {'ticker': 'WIZC3', 'cvm': 23590, 'share_class': 'ON',
        'fiscal_end': master['DT_REFER'], 'filing_id': master['ID_DOC'], 'version': master['VERSAO'],
        'published_at': master['DT_RECEB'], 'full_consolidated_DRE_sha256': DRE_SHA,
        'accounts_scope_requested_and_preserved': 'consolidated', 'approved_readings': [],
        'status': 'consolidated_EPS_explicit_na_in_inspected_native_DFP',
        'CSV_absence_is_not_used_as_PDF_absence_proof': True,
        'genuine_absence_in_all_possible_primary_publications_claimed': False,
        'native_observations': {
            'audited_DRE': {'pdf_page': 56, 'printed_note_page': 11, 'DFP_form_page': 54,
                'columns': ['Controladora 2025', 'Controladora 2024',
                            'Consolidado 2025', 'Consolidado 2024'],
                'consolidated_basic_EPS_literal': 'n/a', 'consolidated_share_count_literal': 'n/a',
                'consolidated_parent_profit_brl': '201076000', 'NCI_profit_brl': '166634000',
                'consolidated_total_profit_brl': '367710000'},
            'audited_DRA': {'pdf_page': 57, 'consolidated_EPS_literal': 'n/a'},
            'EPS_note': {'pdf_page': 102, 'printed_note_page': 57, 'DFP_form_page': 100,
                'note': '24.4', 'table_scope_literal': 'Controladora', 'basic_and_diluted': '1.25745',
                'numerator_brl': '201076000', 'ordinary_weighted_shares': '159907282',
                'definition': 'Profit attributable to company shareholders divided by weighted '
                    'ordinary shares; no issues/events changing the count. Table remains Controladora.'},
            'share_class': {'pdf_page': 101, 'note': '24.1', 'class': 'ON',
                'issued_registered_ordinary_shares': '159907282'},
            'joint_presentation': {'pdf_pages': [64, 65], 'note': '3.1-3.3',
                'literal_claim': 'Parent-attributable consolidated equity/result equal individual '
                    'equity/result, so individual and consolidated accounts are presented side by side.',
                'interpretation': 'This does not expressly relabel the Controladora EPS table '
                    'as consolidated or override the explicit consolidated n/a cells.'},
            'audit': {'pdf_pages': [121, 124], 'signature_date_literal': '2026-03-18',
                'auditor': 'BDO RCS Auditores Independentes SS Ltda.'},
            'consolidated_form': {'pdf_page': 19, 'printed_form_page': 17,
                'EPS_class_leaves': [], 'generic_zero_roots': ['3.99', '3.99.01', '3.99.02'],
                'duplicate_zero_class_leaf_proved': False}},
        'numeric_check_only': individual_numeric_crosscheck_only(tables['DRE_con']),
        'missing_field': 'Explicit same-year same-filing consolidated ON basic EPS disclosure '
            'or issuer clarification applying the printed note24.4 numerator/weighted shares/EPS '
            'to consolidated parent holders despite the native n/a cells. No numeric reconstruction.',
        'applied_reading': None, 'EPS_computed': False, 'scope_switched': False, 'gates_changed': False,
        'model_admission': False, 'sources': [{**source, 'visually_inspected_pages':
            [19, 56, 57, 62, 64, 65, 101, 102, 121, 124]}],
        'annual_IPE_rows': annual_ipe_rows(folder), 'new_IPE2_acquisition': incomplete}


def get_wiz_financial_readings(folder=ROOT, cutoff=CUTOFF):
    review = review_wiz_financial(folder, cutoff)
    return review['approved_readings'], review['sources']


def write_handoff(folder=ROOT):
    review = review_wiz_financial(folder)
    before = json.loads((REPORTS / 'financial-wiz-before.json').read_bytes())
    after = parser_snapshot(folder)
    require(before == after, 'unexpected_normal_parser_change')
    save(REPORTS / 'financial-wiz-after.json', after)
    requests = [r for p in sorted((Path(folder) / BASE).glob('response-*.json'))
                if (r := json.loads(p.read_bytes()))['url'] != SIMH_IPE_URL]
    report = {**review, 'before': before, 'after': after, 'before_after_identical': True,
        'before_after_sha256': sha((REPORTS / 'financial-wiz-before.json').read_bytes()),
        'normal_prepare_financials_and_financial_features_used': True,
        'normal_parser_result': {'before': None, 'after': None,
            'reason': 'basic_eps_absent_ambiguous_or_zero', 'latest_filing': '155497 v1',
            'scope': 'consolidated', 'industrial_FCF_policy': 'remain_null_financial_issuer'},
        'five_year_history_cash_price_and_one_gates': 'Not edited or bypassed; this is financial-only.',
        'network_receipts': [{k: r.get(k) for k in ('url', 'scope', 'status', 'error', 'file',
            'sha256', 'bytes', 'started_at', 'retrieved_at', 'published_at')} for r in requests],
        'network_policy': {'api_MZiQ_B3_and_other_prohibited_requests': 0,
            'RI_robots': 'URLError; policy unavailable; page not requested; no retry or TLS bypass.',
            'CVM_IPE2': 'One actual-index-link request, 200 response_size_limit; bytes preserved '
                'as incomplete. No retry, range assembly, stripping padding or size-limit override.',
            'credentials_authentication_bank_settlement_claim': False},
        'changed_code_files': ['recommender/round2_wiz_financial.py',
                               'recommender/test_round2_wiz_financial.py'],
        'changed_raw_scope': BASE, 'changed_reports': '../work/recovery_round2_20261025/financial-wiz-*',
        'core_shared_inventory_pointer_document_edits': False,
        'parent_integration': 'No readings approved. Do not import individual scope prototype or '
            'the numeric-check-only data as consolidated evidence. Current normal result unchanged.',
        'SIMH3': 'Not advanced in this WIZ-only handoff; no shared or other-agent files touched.'}
    save(REPORTS / 'financial-wiz-handoff.json', report)
    return report


def current_simh_tables(folder=ROOT, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'unreviewed_cutoff')
    with zipfile.ZipFile(io.BytesIO(read(folder, ANNUAL, ANNUAL_SHA))) as archive:
        eligible = [r for r in native._rows(archive, 'dfp_cia_aberta_2025.csv')
            if int(r['CD_CVM']) == SIMH_SPEC[0] and native._day(r['DT_REFER']) <= cutoff
            and native._day(r['DT_RECEB']) <= cutoff]
        master = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
        require(tuple(master[k] for k in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
            == ('2025-12-31', '1', '155854', '2026-03-30', '07.415.333/0001-20'),
            'latest_SIMH_filing_changed_no_fallback')
        rows = [r for r in native._rows(archive, 'dfp_cia_aberta_DRE_con_2025.csv')
            if native._key(r) == native._key(master) and r['ORDEM_EXERC'] == '\u00daLTIMO']
    require(native._dre_fingerprint(rows, full=True) == SIMH_DRE_SHA, 'SIMH_current_DRE_changed')
    return master, rows


def import_simh_cached(folder=ROOT):
    from .round2_financial_readings import verify_package

    master, _ = current_simh_tables(folder)
    source = verify_package(folder, 'SIMH3', master)
    body = read(folder, source['file'], SIMH_PDF_SHA)
    target = f'{BASE}/cached-SIMH3-{SIMH_PDF_SHA}.pdf'
    save(Path(folder) / target, body)
    proof = {**source, 'file': target, 'original_file': source['file'],
        'new_request': False, 'original_retrieval_preserved': True}
    save(Path(folder) / BASE / 'cached-SIMH3.json', proof)
    return proof


def simh_ipe_rows(folder=ROOT):
    with zipfile.ZipFile(io.BytesIO(read(folder, INDEX, INDEX_SHA))) as archive:
        with archive.open('ipe_cia_aberta_2026.csv') as stream:
            rows = [r for r in csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
                if r['Codigo_CVM'] == '25003' and r['Data_Referencia'] == '2025-12-31'
                and r['Tipo'] == 'Demonstra\u00e7\u00f5es Financeiras Anuais Completas']
    require([(r['Versao'], r['Data_Entrega']) for r in rows] == [
        ('1', '2026-03-30'), ('2', '2026-04-08'), ('3', '2026-04-08')]
        and rows[-1]['Link_Download'] == SIMH_IPE_URL, 'SIMH_final_IPE_index_changed')
    return rows


def verify_simh_sources(folder=ROOT):
    from .round2_financial_readings import verify_package

    master, _ = current_simh_tables(folder)
    original = verify_package(folder, 'SIMH3', master)
    copied = json.loads(read(folder, f'{BASE}/cached-SIMH3.json', SIMH_METADATA_SHA))
    require(read(folder, copied['file'], SIMH_PDF_SHA) == read(folder, original['file'], SIMH_PDF_SHA)
        and copied['retrieved_at'] == original['retrieved_at'], 'SIMH_original_identity_changed')
    cached = verify_pages(folder, 'simh-native')
    require(cached['source_file'] == copied['file'], 'SIMH_cached_visual_source_mismatch')
    receipt_file = f'{BASE}/response-{sha(SIMH_IPE_URL.encode())}.json'
    receipt = json.loads(read(folder, receipt_file))
    row = simh_ipe_rows(folder)[-1]
    require(receipt['url'] == row['Link_Download'] and receipt['status'] == 200
        and receipt['error'] is None and receipt['sha256'] == SIMH_IPE_SHA
        and receipt['bytes'] == 8_239_462 and receipt['retries'] == 0
        and receipt['discovery']['index_row'] == row, 'SIMH_final_IPE_transport_changed')
    body = read(folder, receipt['file'], SIMH_IPE_SHA)
    require(len(body) == receipt['bytes'] and body.startswith(b'%PDF-')
        and datetime.fromisoformat(receipt['retrieved_at']).utcoffset() is not None,
        'SIMH_final_PDF_or_clock_changed')
    final = verify_pages(folder, 'simh-IPE3')
    require(final['source_file'] == receipt['file'] and final['total_pages'] == 136,
            'SIMH_final_visual_source_mismatch')
    published = {'file': receipt['file'], 'sha256': SIMH_IPE_SHA, 'url': SIMH_IPE_URL,
        'bytes': len(body), 'receipt_file': receipt_file, 'retrieved_at': receipt['retrieved_at'],
        'published_at': row['Data_Entrega'], 'publication_basis': 'CVM_IPE_Data_Entrega',
        'complete_delivery': True, 'IPE_version': '3', 'fiscal_end': row['Data_Referencia'],
        'DFP_version_not_changed': '1', 'exact_DFP_container_identity_claimed': False,
        'binding': 'Official same-issuer FY2025 final complete annual IPE, not a DFP v3 package.',
        'visually_inspected_pages': [33, 35, 115, 116, 122, 129], 'model_admission': False}
    return copied, published, cached, final


def simh_unapproved_note_candidate():
    return {'profit_account': '3.11.01', 'parent_profit_brl': '39471000',
        'basic_eps': [{'account': 'note:32.1:ON', 'share_class': 'ON', 'value': '0.0931',
            'numerator_brl': '39741000', 'weighted_shares': '426797758',
            'numerator_rounding_brl': '500', 'shares_rounding': '0.5'}]}


def review_simh_after_wiz(folder=ROOT, cutoff=CUTOFF):
    master, rows = current_simh_tables(folder, cutoff)
    cached, final, cached_pages, final_pages = verify_simh_sources(folder)
    pages = {p['pdf_page']: ' '.join(p['text'].split()) for p in final_pages['pages']}
    require(all(value in pages[33] for value in ('Consolidado', '39.471', '173.170',
        '212.641', '215.250', '(2.609)', '0,0931'))
        and all(value in pages[116] for value in ('Controladora', '39.741', '426.797.758', '0,0931'))
        and '30 de mar\u00e7o de 2026' in pages[129], 'SIMH_native_values_changed')
    try:
        native._checked_note_eps(rows, simh_unapproved_note_candidate())
    except ValueError as error:
        validation = str(error)
    else:
        raise ValueError('wiz_financial_SIMH_conflicting_note_unexpectedly_accepted')
    return {'ticker': 'SIMH3', 'share_class': 'ON', 'cvm': SIMH_SPEC[0],
        'fiscal_end': master['DT_REFER'], 'filing_id': master['ID_DOC'], 'version': master['VERSAO'],
        'published_at': master['DT_RECEB'], 'accounts_scope': 'consolidated',
        'full_consolidated_DRE_sha256': SIMH_DRE_SHA, 'approved_readings': [],
        'status': 'native_consolidated_EPS_present_but_basis_conflict_not_repaired',
        'genuine_PDF_EPS_absence': False, 'printed_consolidated_basic_EPS': '0.0931',
        'new_final_native_DRE': {'pdf_page': 33, 'printed_page': 31,
            'parent_profit_brl': '39471000', 'NCI_profit_brl': '173170000',
            'total_profit_brl': '212641000', 'continuing_profit_brl': '215250000',
            'discontinued_result_brl': '-2609000',
            'DMPL_independent_reconciliation_pdf_page': 35,
            'prior_apparent_total_difference_explained': 'Audited DRE separates continuing '
                '215250 from discontinued -2609 and total 212641, in R$ thousands; '
                'native DFP form 3.11 still prints 215250. No account was silently corrected.'},
        'native_zero_placeholder_proof': {'exact_DFP_pdf_page': 24,
            'single_reported_basic_ON': '3.99.01.01 = 0.0931',
            'CSV_extra_basic_ON': '3.99.01.02 = 0', 'second_zero_ON_printed_in_PDF': False,
            'same_period_audited_consolidated_DRE_corroborates_nonzero_EPS': True,
            'discard_zero_alone_approved': False},
        'remaining_native_note_conflict': {'exact_DFP_pdf_pages': [139, 140],
            'final_IPE_pdf_pages': [115, 116], 'note': '32.1', 'table_scope': 'Controladora',
            'printed_numerator_brl': '39741000', 'printed_weighted_ON_shares': '426797758',
            'DRE_parent_profit_brl': '39471000', 'same_conflict_survives_latest_IPE3': True,
            'quantity_profit_or_printed_EPS_adjusted': False},
        'existing_numeric_validator_error': validation,
        'missing_field': 'Native same-current-filing issuer correction/reconciliation of '
            'the printed consolidated EPS basis versus 39471 DRE parent and 39741 '
            'Controladora note numerator. Do not alter weighted shares, profit, or reported EPS.',
        'sources': [cached, final], 'annual_IPE_rows': simh_ipe_rows(folder),
        'applied_reading': None, 'EPS_computed': False, 'gates_changed': False,
        'scope_switched': False, 'model_admission': False}


def write_simh_handoff(folder=ROOT):
    review = review_simh_after_wiz(folder)
    before = json.loads((REPORTS / 'financial-wiz-simh-before.json').read_bytes())
    after = parser_snapshot(folder, ticker='SIMH3')
    require(before == after, 'unexpected_SIMH_normal_parser_change')
    save(REPORTS / 'financial-wiz-simh-after.json', after)
    report = {**review, 'before': before, 'after': after, 'before_after_identical': True,
        'before_after_sha256': sha((REPORTS / 'financial-wiz-simh-before.json').read_bytes()),
        'normal_prepare_financials_and_financial_features_used': True,
        'normal_financial_features': {'before': None, 'after': None},
        'scope_of_work': 'Authorized secondary SIMH review after WIZ had no approved reading.',
        'changed_code_files': ['recommender/round2_wiz_financial.py',
                               'recommender/test_round2_wiz_financial.py'],
        'changed_raw_scope': BASE, 'parent_integration': 'No reading approved. Keep original '
            'form cells. This is additional actual audited consolidated PDF evidence, not '
            'a claim of absent native EPS or permission to repair source errata.',
        'core_shared_inventory_pointer_document_edits': False,
        'network_policy': 'One trusted actual CVM IPE3 link; 200 complete 8239462 bytes; '
            'original clock/bytes retained. No prohibited host, retry, credentials or bypass.'}
    save(REPORTS / 'financial-wiz-simh-handoff.json', report)
    return report
