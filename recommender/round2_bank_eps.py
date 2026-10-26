"""Exact-current native consolidated bank EPS review; no production edits."""

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
from .bulk_pending_sources import PublicSourceCollector
from .collect_market_data import financial_features


ROOT = Path(__file__).resolve().parent / 'data/market'
BASE = 'raw/primary-research/all-pending/round2-bank-eps'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
CUTOFF = date(2026, 9, 30)
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
SPECS = {'BPAC3': (22616, 'ON', 2), 'PINE4': (20567, 'PN', 2)}
EXPECTED = {'BPAC3': ('2', '155648', '2026-03-24', '30.306.294/0001-45'),
            'PINE4': ('3', '155965', '2026-03-31', '62.144.175/0001-20')}
DRE_PINS = {'BPAC3': '598ce6c4960dd21ef4730ca9663b05aa32dbad5e9ae184f36e31ff783dab0386',
            'PINE4': '2faeab62e885403a4642b3c0201121f0a587b25aaefaa1c3814c07da5d4b3f99'}
REVIEW_PINS = {
    'BPAC-support': 'e95186121c10682e3867aaea858a56af3c74258d2f49945d5af668f9a8aee336',
    'BPAC-audit-end': 'b6b51e45432cdb41e8180933e0466885f749c5cfb41363e1cd5efbfe908b74bc',
    'PINE-native': '7624b166301a29e432a743b02b752fb8ca9e17cfed389d6e3077df19892efaf0',
}
CATALOGUE_SHA = '7856bcab6ad39a74fcc7440e00f841e80875b77b148da6a905f4656b0c6700c9'
CATALOGUE_URL = ('https://api.solutions-ir.com/v2/files/78a5a309-f13a-41ed-a7d1-814e37b63259'
                 '?language=pt&categories=informacoes_financeiras&date=2025-01-02')


def require(condition, reason):
    if not condition:
        raise ValueError('bank_eps_' + reason)


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
        all_masters = list(native._rows(archive, 'dfp_cia_aberta_2025.csv'))
        masters, tables = {}, {}
        for ticker, (cvm, _, _) in SPECS.items():
            eligible = [r for r in all_masters if int(r['CD_CVM']) == cvm
                and native._day(r['DT_REFER']) <= cutoff and native._day(r['DT_RECEB']) <= cutoff]
            master = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            require(master['DT_REFER'] == '2025-12-31' and tuple(master[k] for k in (
                'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA')) == EXPECTED[ticker],
                'latest_filing_changed_no_fallback')
            masters[ticker] = master
            tables[ticker] = {}
            for suffix in ('DRE_con', 'BPA_con', 'BPP_con', 'DMPL_con'):
                tables[ticker][suffix] = [r for r in native._rows(archive,
                    f'dfp_cia_aberta_{suffix}_2025.csv')
                    if native._key(r) == native._key(master) and r['ORDEM_EXERC'] == '\u00daLTIMO']
    return masters, tables


def fetch(url, discovery, folder=ROOT):
    host = (urlsplit(url).hostname or '').lower()
    require(host != 'api.mziq.com' and not host.endswith('.api.mziq.com'), 'MZiQ_network_forbidden')
    collector = PublicSourceCollector(folder, 'round2-bank-eps')
    receipt = collector.fetch(url, discovery=discovery)
    return receipt, collector.body(receipt)


def import_pine_package(folder=ROOT):
    from .round2_financial_readings import verify_package

    masters, _ = current_tables(folder)
    source = verify_package(folder, 'PINE4', masters['PINE4'])
    original = 'raw/primary-research/all-pending/round2-financial/package-PINE4.json'
    original_body = read(folder, original)
    proof = json.loads(original_body)
    entries = []
    for member in proof['members']:
        body = read(folder, member['file'], member['sha256'])
        relative = f'{BASE}/native-PINE4-{sha(body)}{Path(member["file"]).suffix}'
        save(Path(folder) / relative, body)
        entries.append({**member, 'file': relative})
    result = {**proof, 'members': entries, 'original_proof_file': original,
        'original_proof_sha256': sha(original_body), 'new_network_request': False,
        'original_retrieval_preserved': True}
    save(Path(folder) / BASE / 'package-PINE4.json', result)
    return result, source


def import_bpac_supporting_pdf(folder=ROOT):
    relative = ('raw/primary-research/financials/bpac-ifrs-2025-'
        'ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f.pdf')
    digest = 'ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f'
    body = read(folder, relative, digest)
    target = f'{BASE}/cached-BPAC3-{digest}.pdf'
    save(Path(folder) / target, body)
    proof = {'file': target, 'sha256': digest, 'bytes': len(body), 'original_file': relative,
        'retrieved_at': None, 'published_at': None, 'exact_filing_verified': False,
        'retrieval_clock_basis': 'not_retained_never_inferred_from_mtime',
        'url': 'https://d169uzu5o4xu1k.cloudfront.net/78a5a309-f13a-41ed-a7d1-814e37b63259/2025/8fc4586b-7c7b-42a1-acc1-be8d6ba7ac76.pdf'}
    save(Path(folder) / BASE / 'cached-BPAC3.json', proof)
    return proof


def inspect_pdf(relative, numbers, label, folder=ROOT):
    from pypdf import PdfReader

    folder = Path(folder)
    body = read(folder, relative)
    pdf = PdfReader(io.BytesIO(body))
    executable = shutil.which('pdftoppm')
    require(executable is not None, 'Poppler_required')
    result = {'source_file': relative, 'source_sha256': sha(body),
              'total_pages': len(pdf.pages), 'pages': []}
    for number in numbers:
        require(type(number) is int and 1 <= number <= len(pdf.pages), 'invalid_page')
        prefix = folder / BASE / f'review-{label}-p{number}'
        subprocess.run([executable, '-f', str(number), '-l', str(number), '-scale-to', '1800',
            '-png', '-singlefile', str(folder / relative), str(prefix)],
            check=True, capture_output=True, timeout=45)
        png = prefix.with_suffix('.png')
        result['pages'].append({'pdf_page': number, 'text': pdf.pages[number - 1].extract_text(),
            'render': png.relative_to(folder).as_posix(), 'render_sha256': sha(png.read_bytes())})
    save(folder / BASE / f'pages-{label}.json', result)
    return result


def parser_snapshot(folder=ROOT):
    statements, diagnostics, _ = native.prepare_financials(folder, SPECS, CUTOFF)
    result = {}
    for ticker, spec in SPECS.items():
        selected = financial_features(statements, [], CUTOFF, spec)
        result[ticker] = {'features': None if selected is None else selected[0],
            'record': None if selected is None else selected[1],
            'latest_records': [r for r in statements if r['cvm'] == spec[0]
                               and r['fiscal_end'] == '2025-12-31'],
            'candidate_diagnostics': [r for r in diagnostics if r.get('ticker') == ticker
                and r.get('kind') == 'candidate' and r.get('fiscal_end') == '2025-12-31']}
    return result


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


def verify_pine(folder=ROOT):
    from .round2_financial_readings import verify_package

    masters, _ = current_tables(folder)
    source = verify_package(folder, 'PINE4', masters['PINE4'])
    proof = json.loads(read(folder, f'{BASE}/package-PINE4.json'))
    original = read(folder, proof['original_proof_file'], proof['original_proof_sha256'])
    require(json.loads(original)['original_container_sha256'] == proof['original_container_sha256']
            and proof['filing_id'] == '155965' and proof['version'] == '3'
            and proof['new_network_request'] is False and proof['original_retrieval_preserved'],
            'cached_native_identity_changed')
    with zipfile.ZipFile(io.BytesIO(read(folder, proof['original_container'],
                                       proof['original_container_sha256']))) as archive:
        for member in proof['members']:
            body = read(folder, member['file'], member['sha256'])
            require(archive.read(member['original_zip_member']) == body, 'native_member_changed')
        pdf, = [m for m in proof['members'] if m['file'].endswith('.pdf')]
    review = verify_pages(folder, 'PINE-native')
    require(review['source_file'] == pdf['file'] and review['source_sha256'] == source['sha256'],
            'PINE_visual_source_mismatch')
    return {**source, **pdf, 'use': 'native_class_EPS_conflict_review',
            'visually_inspected_pages': [21, 105, 106, 108, 117]}


def verify_bpac(folder=ROOT):
    proof = json.loads(read(folder, f'{BASE}/cached-BPAC3.json'))
    body = read(folder, proof['file'], 'ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f')
    require(proof['sha256'] == sha(body) and proof['bytes'] == len(body)
            and proof['retrieved_at'] is None and proof['exact_filing_verified'] is False,
            'BPAC_cached_original_identity_changed')
    catalogue = json.loads(read(folder, f'{BASE}/body-{CATALOGUE_SHA}.bin', CATALOGUE_SHA))
    receipt = json.loads(read(folder, f'{BASE}/response-{sha(CATALOGUE_URL.encode())}.json'))
    require(receipt['url'] == CATALOGUE_URL and receipt['status'] == 200 and receipt['error'] is None
            and receipt['sha256'] == CATALOGUE_SHA, 'issuer_catalogue_receipt_changed')
    require(datetime.fromisoformat(receipt['retrieved_at']).utcoffset() is not None,
            'catalogue_retrieval_timezone_missing')
    rows = [r for r in catalogue['categories']['informacoes_financeiras'] if r['url'] == proof['url']]
    require(len(rows) == 1 and rows[0]['id'] == '4db00619-3d0a-453a-9faf-dea45fab0056'
            and rows[0]['refDate'][:10] == '2025-12-31'
            and rows[0]['deliveryDate'] == '2026-03-25T00:22:00Z', 'issuer_annual_catalogue_changed')
    for label in ('BPAC-support', 'BPAC-audit-end'):
        review = verify_pages(folder, label)
        require(review['source_file'] == proof['file'], 'BPAC_visual_source_mismatch')
    return {**proof, 'published_at': rows[0]['deliveryDate'],
        'authority': 'primary_issuer_PDF_cached_and_current_RI_catalogue',
        'publication_basis': 'literal_RI_catalogue_deliveryDate_not_CVM_version_identity',
        'catalogue_retrieved_at': receipt['retrieved_at'], 'catalogue_file': receipt['file'],
        'catalogue_metadata': rows[0], 'use': 'native_class_EPS_conflict_review',
        'point_in_time_archive_verified': False, 'bank_settlement_verified': False,
        'visually_inspected_pages': [11, 13, 66],
        'native_audit_date_literal': '2025-03-17',
        'native_audit_date_not_silently_corrected': True,
        'PDF_text_limitation': 'Pypdf and PDFium expose broken glyph mappings on the financial '
            'tables; values below are manual visual transcription from pinned complete renders.'}


def unapproved_native_candidates():
    """Printed numbers only, used to exercise existing validators, never returned as readings."""
    return {
        'BPAC3': {'profit_account': '3.09.01', 'parent_profit_brl': '16005964000',
            'accounts_scope': 'consolidated', 'basic_eps': [{'account': 'note:25:ON',
                'share_class': 'ON', 'value': '2.30', 'numerator_brl': '16663803000',
                'weighted_shares': '7244313000', 'numerator_rounding_brl': '500',
                'shares_rounding': '500'}]},
        'PINE4': {'profit_account': '3.11.01', 'parent_profit_brl': '430515000',
            'accounts_scope': 'consolidated',
            'profit_allocations_brl': {'ON': '221909000', 'PN': '210969000'},
            'basic_eps': [{'account': 'note:IFRS_DRE:PN', 'share_class': 'PN', 'value': '1.86',
                'numerator_brl': '210969000', 'weighted_shares': '113288564', 'allocation': 'PN',
                'numerator_rounding_brl': '500', 'shares_rounding': '0.5'}]},
    }


def review_bank_eps(folder=ROOT, cutoff=CUTOFF):
    masters, tables = current_tables(folder, cutoff)
    sources = {'BPAC3': verify_bpac(folder), 'PINE4': verify_pine(folder)}
    observations = {
        'BPAC3': {'EPS_pdf_present': True, 'genuine_PDF_EPS_absence': False,
            'printed_basic_EPS': '2.30', 'share_class': 'ON', 'pdf_page': 66, 'printed_page': 62,
            'note': '25. Lucro por acao', 'period': ['2025-01-01', '2025-12-31'],
            'table_profit_header_brl': '16663803000',
            'table_header_is_not_explicit_ON_parent_allocation': True,
            'parent_profit_brl': '16005964000', 'NCI_profit_brl': '657839000',
            'weighted_open_ON_shares': '7244313000', 'weighted_ON_treasury_separate': '27470000',
            'weighted_unit_native': 'thousand_shares', 'treasury_not_arbitrarily_subtracted': True,
            'DRE_parent_and_NCI_page': 13, 'missing_field': 'Explicit ON parent-attributable '
                'numerator/weighted-share reconciliation for printed basic 2.30 in exact 155648 v2; '
                'the note header is total consolidated profit, not parent profit.',
            'additional_native_conflict': 'Auditor signature p11 prints 17 March 2025, despite '
                'the FY2025 annual headings and March 2026 catalogue publication. Preserve literally.',
            'exact_current_CVM_package': 'Not obtained: requested native 155648 timed out. '
                'The current RI catalogue identifies the same cached IFRS PDF, but not CVM ZIP identity.'},
        'PINE4': {'EPS_pdf_present': True, 'genuine_PDF_EPS_absence': False,
            'printed_basic_EPS': '1.86', 'share_class': 'PN', 'pdf_page': 106, 'printed_page': 16,
            'note': 'Audited consolidated IFRS DRE; policy note3 at physical117/printed27',
            'period': ['2025-01-01', '2025-12-31'],
            'printed_PN_numerator_brl': '210969000', 'weighted_PN_shares': '113288564',
            'printed_ON_numerator_brl': '221909000', 'weighted_ON_shares': '119162783',
            'total_profit_brl': '432878000', 'parent_profit_brl': '430515000',
            'NCI_profit_brl': '2363000', 'DMPL_parent_NCI_reconciliation_page': 108,
            'source_policy': 'Native IFRS policy says basic EPS uses profit attributable to Pine '
                'ordinary/preferred holders. DRE class numerators sum to 432878000, while both '
                'DRE and DMPL identify 430515000 parent and 2363000 NCI.',
            'form_page': 21, 'form_roots_are_not_a_zero_duplicate_class_leaf': True,
            'missing_field': 'Same-v3 issuer clarification/correction reconciling printed '
                'PN/ON class numerators to parent profit excluding NCI. No replacement EPS calculated.',
            'exact_current_CVM_package': 'Verified original container, XML identity, embedded '
                'PDF155965 v3 and original transport/retrieval, reused without any new PINE request.'},
    }
    findings = []
    for ticker, candidate in unapproved_native_candidates().items():
        require(native._dre_fingerprint(tables[ticker]['DRE_con'], full=True) == DRE_PINS[ticker],
                'full_current_consolidated_DRE_changed')
        try:
            native._checked_note_eps(tables[ticker]['DRE_con'], candidate)
        except ValueError as error:
            validation = str(error)
        else:
            raise ValueError('bank_eps_unexpected_candidate_validation_pass')
        master = masters[ticker]
        findings.append({'ticker': ticker, 'cvm': SPECS[ticker][0], 'accounts_scope': 'consolidated',
            'fiscal_end': master['DT_REFER'], 'version': master['VERSAO'],
            'filing_id': master['ID_DOC'], 'published_at': master['DT_RECEB'],
            'full_DRE_sha256': DRE_PINS[ticker], 'status': 'native_EPS_present_but_unresolved_conflict',
            'observations': observations[ticker], 'existing_native_validator_error': validation,
            'unapproved_candidate_only': candidate, 'applied_reading': None,
            'gates_changed': False, 'EPS_computed': False, 'model_admission': False,
            'source': sources[ticker]})
    return {'approved_readings': [], 'findings': findings, 'sources': list(sources.values()),
        'native_EPS_present_in_both_PDFs': True, 'CSV_absence_is_not_PDF_absence': True,
        'bank_FCF_policy': 'Remain null; no industrial FCF or capex was introduced.',
        'correction_search_limit': 'BPAC published 2026 catalogue titles examined; no explicit EPS '
            'correction title located. Not proof that an undiscovered clarification does not exist.'}


def get_bank_eps_readings(folder=ROOT, cutoff=CUTOFF):
    review = review_bank_eps(folder, cutoff)
    return review['approved_readings'], review['sources']


def write_handoff(folder=ROOT):
    review = review_bank_eps(folder)
    before = json.loads((REPORTS / 'financial-bank-before.json').read_bytes())
    after = parser_snapshot(folder)
    require(before == after, 'unexpected_normal_parser_change')
    save(REPORTS / 'financial-bank-after.json', after)
    requests = [json.loads(p.read_bytes()) for p in sorted((Path(folder) / BASE).glob('response-*.json'))]
    report = {**review, 'cutoff': CUTOFF.isoformat(), 'before': before, 'after': after,
        'before_after_identical': True,
        'before_after_sha256': sha((REPORTS / 'financial-bank-before.json').read_bytes()),
        'normal_prepare_financials_and_financial_features_used': True,
        'cash_input_for_financial_only_test': 'Empty input is not a claim of zero distributions.',
        'scope': ['BPAC3 ON only', 'PINE4 PN only'], 'new_admissions': [],
        'requests': [{k: item.get(k) for k in ('url', 'scope', 'status', 'error', 'file', 'sha256',
            'started_at', 'retrieved_at', 'published_at')} for item in requests],
        'network_policy': {'api_mziq_requests': 0, 'B3_bvmf_bovespa_requests': 0,
            'CVM155648': 'One explicitly user-requested new-task attempt after prior timeout; '
                'no automatic retry, no denial, no downloaded native package.',
            'new_denial': 'd169uzu5o4xu1k.cloudfront.net robots.txt HTTP403; origin stopped; '
                'no PDF GET and no alternate-host substitution. Cached original only.',
            'credentials_authentication_TLS_bypass': False},
        'changed_code_files': ['recommender/round2_bank_eps.py', 'recommender/test_round2_bank_eps.py'],
        'changed_raw_scope': BASE, 'changed_reports': '../work/recovery_round2_20261025/financial-bank-*',
        'core_shared_inventory_pointer_document_edits': False,
        'parent_integration': 'No readings approved. Found native EPS must not be labeled genuinely '
            'absent, but class/parent conflicts are unresolved; do not import candidates or relax gates.'}
    save(REPORTS / 'financial-bank-handoff.json', report)
    return report
