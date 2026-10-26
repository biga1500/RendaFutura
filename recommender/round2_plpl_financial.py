"""Isolated PLPL3 exact-current capex investigation, never a core override."""

from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from . import inventory_financials as native
from .bulk_pending_sources import PublicSourceCollector
from .collect_market_data import financial_features


ROOT = Path(__file__).resolve().parent / 'data/market'
BASE = 'raw/primary-research/all-pending/round2-plpl-financial'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
CUTOFF = date(2026, 9, 30)
SPECS = {'PLPL3': (25070, 'ON', 4)}
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
PUBLICATION_URL = 'https://vipfiles.valor.com.br/BDEmpresas/e892fc89-d380-4eb0-b0a2-c661ffc6ced5.pdf'
PUBLICATION_SHA = 'eaa85ccbd409fbf0fb04214e2af0d0f056ada1b11d37eb07acb0f323188da383'
PUBLICATION = f'{BASE}/body-{PUBLICATION_SHA}.bin'


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def save(path, value):
    body = value if isinstance(value, bytes) else (
        json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'plpl_immutable_evidence_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def read(folder, relative, digest=None):
    root = Path(folder).resolve()
    path = root / relative
    require(path.resolve().is_relative_to(root) and not path.is_symlink(), 'plpl_unsafe_path')
    body = path.read_bytes()
    require(digest is None or sha(body) == digest, 'plpl_evidence_changed')
    return body


def current_tables(folder=ROOT, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'plpl_unreviewed_cutoff')
    body = read(folder, ANNUAL, ANNUAL_SHA)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        masters = [r for r in native._rows(archive, 'dfp_cia_aberta_2025.csv')
                   if int(r['CD_CVM']) == 25070 and native._day(r['DT_RECEB']) <= cutoff]
        master = max(masters, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
        require(tuple(master[k] for k in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
                == ('2025-12-31', '2', '155293', '2026-03-12', '24.230.275/0001-80'),
                'plpl_current_filing_changed')
        tables = {}
        for suffix in native.TABLES:
            name = f'dfp_cia_aberta{suffix}_2025.csv'
            tables[suffix] = [r for r in native._rows(archive, name)
                if native._key(r) == (25070, '2025-12-31', '2')
                and r['ORDEM_EXERC'] == '\u00daLTIMO']
    return master, tables


def acquire(url, discovery, folder=ROOT):
    collector = PublicSourceCollector(folder, 'round2-plpl-financial')
    receipt = collector.fetch(url, discovery=discovery)
    return receipt, collector.body(receipt)


def extract_package(receipt, body, folder=ROOT):
    master, _ = current_tables(folder)
    require(receipt['status'] == 200 and receipt['error'] is None
            and receipt['sha256'] == sha(body) and receipt['bytes'] == len(body)
            and receipt['url'] == master['LINK_DOC'].replace('http://', 'https://', 1),
            'plpl_exact_transport_mismatch')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        require(archive.testzip() is None, 'plpl_native_CRC_failure')
        name = '025070DFP31-12-2025v2.xml'
        xml = ET.fromstring(archive.read(name))
        require(tuple(xml.findtext(k) for k in ('DadosEmpresa/CodigoCvm',
            'DadosEmpresa/CnpjEmpresa', 'Documento/VersaoDocumento', 'DadosDFP/DataReferencia'))
            == ('025070', '24230275000180', '2', '31/12/2025'), 'plpl_native_identity_mismatch')
        pdfs = [n for n in archive.namelist() if re.fullmatch(r'155293_025070_\d+\.pdf', n)]
        require(len(pdfs) == 1, 'plpl_native_pdf_ambiguous')
        members = []
        for name in (name, pdfs[0]):
            content = archive.read(name)
            relative = f'{BASE}/native-{sha(content)}{Path(name).suffix}'
            save(Path(folder) / relative, content)
            members.append({'file': relative, 'sha256': sha(content), 'bytes': len(content),
                            'original_zip_member': name})
    proof = {'master': master, 'transport': receipt, 'members': members,
             'point_in_time_archive_verified': False, 'bank_settlement_verified': False}
    save(Path(folder) / BASE / 'package-PLPL3.json', proof)
    return proof


def inspect_pdf(relative, pages, label, folder=ROOT):
    from pypdf import PdfReader

    folder = Path(folder)
    pdf = folder / relative
    body = read(folder, relative)
    reader = PdfReader(io.BytesIO(body))
    executable = shutil.which('pdftoppm')
    require(executable is not None, 'plpl_Poppler_required')
    result = {'file': relative, 'sha256': sha(body), 'total_pages': len(reader.pages), 'pages': []}
    for number in pages:
        require(type(number) is int and 1 <= number <= len(reader.pages), 'plpl_bad_page')
        prefix = folder / BASE / f'review-{label}-p{number}'
        subprocess.run([executable, '-f', str(number), '-l', str(number), '-scale-to', '1800',
                        '-png', '-singlefile', str(pdf), str(prefix)],
                       check=True, capture_output=True, timeout=45)
        image = prefix.with_suffix('.png')
        result['pages'].append({'pdf_page': number, 'text': reader.pages[number - 1].extract_text(),
            'render': image.relative_to(folder).as_posix(), 'render_sha256': sha(image.read_bytes())})
    save(folder / BASE / f'pages-{label}.json', result)
    return result


def parser_snapshot(folder=ROOT):
    records, diagnostics, _ = native.prepare_financials(folder, SPECS, CUTOFF)
    selected = financial_features(records, [], CUTOFF, SPECS['PLPL3'])
    return {'ticker': 'PLPL3', 'features': None if selected is None else selected[0],
        'record': None if selected is None else selected[1],
        'latest_records': [r for r in records if r['cvm'] == 25070
                           and r['fiscal_end'] == '2025-12-31'],
        'candidate_diagnostics': [r for r in diagnostics if r.get('ticker') == 'PLPL3'
            and r.get('kind') == 'candidate' and r.get('fiscal_end') == '2025-12-31'],
        'cash_input': 'empty_for_financial_only_test_not_a_zero_cash_claim'}


def verify_publication(folder=ROOT):
    """Native audited author document, not certified as the exact CVM revision."""
    from pypdf import PdfReader

    receipt_name = f'{BASE}/response-{sha(PUBLICATION_URL.encode())}.json'
    receipt = json.loads(read(folder, receipt_name))
    body = read(folder, PUBLICATION, PUBLICATION_SHA)
    require(receipt['url'] == PUBLICATION_URL and receipt['file'] == PUBLICATION
            and receipt['sha256'] == PUBLICATION_SHA and receipt['bytes'] == len(body)
            and receipt['status'] == 200 and receipt['error'] is None
            and receipt['scope'] == 'round2-plpl-financial'
            and receipt['published_at'] is None and not receipt['bank_settlement_verified'],
            'plpl_publication_transport_changed')
    require(datetime.fromisoformat(receipt['retrieved_at']).utcoffset() is not None,
            'plpl_retrieval_timezone_missing')
    review = json.loads(read(folder, f'{BASE}/pages-publication.json'))
    require(review['file'] == PUBLICATION and review['sha256'] == PUBLICATION_SHA
            and review['total_pages'] == 141, 'plpl_review_source_changed')
    reader = PdfReader(io.BytesIO(body))
    require(len(reader.pages) == 141, 'plpl_publication_page_count_changed')
    for page in review['pages']:
        require(reader.pages[page['pdf_page'] - 1].extract_text() == page['text'],
                'plpl_review_text_changed')
        read(folder, page['render'], page['render_sha256'])
    require({p['pdf_page'] for p in review['pages']} == {1, 47, 53, 104, 105, 133},
            'plpl_review_pages_changed')
    return {'url': PUBLICATION_URL, 'file': PUBLICATION, 'sha256': PUBLICATION_SHA,
        'bytes': len(body), 'retrieved_at': receipt['retrieved_at'], 'published_at': None,
        'http_last_modified': receipt['headers'].get('Last-Modified'),
        'http_last_modified_is_not_publication_proof': True,
        'audit_report_date': '2026-03-11', 'fiscal_end': '2025-12-31',
        'authority': 'issuer_authored_audited_document_publication_host',
        'exact_filing_verified': False, 'point_in_time_archive_verified': False,
        'bank_settlement_verified': False, 'use': 'negative_evidence_no_override',
        'visually_inspected_pages': [53, 104, 105, 133]}


def get_plpl_reading(folder=ROOT, cutoff=CUTOFF):
    """Return no approved override, with exact current unresolved source fields."""
    master, tables = current_tables(folder, cutoff)
    row, = [r for r in tables['_DFC_MI_con'] if r['CD_CONTA'] == '6.02.03']
    require(row['DS_CONTA'] == 'Acr\u00e9scimo/baixa do Imobilizado'
            and row['VL_CONTA'] == '-26810.0000000000'
            and row['MOEDA'] == 'REAL' and row['ESCALA_MOEDA'] == 'MIL'
            and (row['DT_INI_EXERC'], row['DT_FIM_EXERC']) == ('2025-01-01', '2025-12-31'),
            'plpl_current_capex_cell_changed')
    source = verify_publication(folder)
    finding = {'ticker': 'PLPL3', 'cvm': 25070, 'share_class': 'ON',
        'fiscal_end': master['DT_REFER'], 'version': master['VERSAO'],
        'filing_id': master['ID_DOC'], 'published_at': master['DT_RECEB'],
        'accounts_scope': 'consolidated', 'status': 'pending_unchanged',
        'reason': 'gross_cash_capex_not_separately_identified_in_available_native_evidence',
        'original_account': row, 'applied_reading': None, 'model_admission': False,
        'numeric_values_changed': False, 'EPS_computed': False,
        'missing_source_fields': [
            'Exact 155293 v2 native DFP/notes identity; direct official download timed out.',
            'FY2025 consolidated cash paid for PPE and intangibles, separately from any '
            'disposal cash receipts and noncash additions, in that same filing.'],
        'native_observations_thousand_BRL': {
            'DFC_combined_increase_disposal_PPE_intangibles': {'page': 53, 'value': -26810},
            'PPE_rollforward_acquisitions_not_certified_cash_paid': {'page': 104, 'value': 17997},
            'PPE_rollforward_disposals_not_certified_cash_received': {'page': 104, 'value': -2},
            'intangible_rollforward_additions_not_certified_cash_paid': {'page': 105, 'value': 8815}},
        'arithmetic_check_only': '17997 + 8815 - 2 = 26810; not a gross cash capex derivation',
        'rejected_interpretations': [
            'Relabel negative mixed increase/disposal DFC as gross acquisition cash.',
            'Add 2 thousand to the DFC or use 26812 thousand accrual additions as cash capex.',
            'Infer zero disposal proceeds or zero noncash additions from absent disclosures.',
            'Borrow individual values, a previous filing, or 2024 comparative values.',
            'Certify exact CVM revision from matching fiscal year, values, or HTTP Last-Modified.'],
        'note_34_page': 133,
        'note_34_limit': 'Noncash table does not explicitly reconcile gross PPE/intangible '
            'cash paid or identify disposal cash proceeds; absence is not a declared zero.',
        'source': source}
    return None, finding, [source]


def historical_candidates(folder=ROOT):
    """Offline discovery only; comparative figures never become training readings."""
    findings = []
    caches = {year: path for (form, year), path in native._caches(Path(folder), CUTOFF)
              if form == 'dfp'}
    for year in range(2020, 2025):
        path = caches.get(year)
        if path is None:
            findings.append({'year': year, 'status': 'annual_archive_not_cached'})
            continue
        relative = path.relative_to(folder).as_posix()
        body = read(folder, relative)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            masters = [r for r in native._rows(archive, f'dfp_cia_aberta_{year}.csv')
                if int(r['CD_CVM']) == 25070 and native._day(r['DT_RECEB']) <= CUTOFF]
            if not masters:
                continue
            master = max(masters, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            rows = [r for r in native._rows(archive, f'dfp_cia_aberta_DFC_MI_con_{year}.csv')
                if native._key(r) == native._key(master) and r['ORDEM_EXERC'] == '\u00daLTIMO'
                and r['CD_CONTA'].startswith('6.02.')
                and any(s in native.normalize(r['DS_CONTA']) for s in ('imobiliz', 'intangiv'))]
        findings.append({'year': year, 'master': master, 'source_file': relative,
            'source_sha256': sha(body), 'investing_asset_rows': rows,
            'status': 'native_same_filing_review_pending_no_training_override', 'approved': False})
    return findings


def handoff(folder=ROOT):
    reading, finding, sources = get_plpl_reading(folder)
    before = json.loads((REPORTS / 'financial-plpl-before.json').read_bytes())
    after = parser_snapshot(folder)
    require(before == after, 'plpl_unexpected_parser_change')
    save(REPORTS / 'financial-plpl-after.json', after)
    attempts = [json.loads(p.read_bytes()) for p in sorted((Path(folder) / BASE).glob('response-*.json'))]
    result = {'scope': 'PLPL3 exact-current 2025 only; no core or shared edits',
        'supersedes_report': 'financial-plpl-handoff.json; historical cache alias discovery corrected',
        'approved_reading': reading, 'finding': finding, 'sources': sources,
        'before': {'filing_id': '155293', 'reason': ['capex_not_identified'], 'features': None},
        'after': {'filing_id': '155293', 'reason': ['capex_not_identified'], 'features': None},
        'before_after_identical': True, 'baseline_sha256': sha((REPORTS / 'financial-plpl-before.json').read_bytes()),
        'historical_2020_2024_discovery_only': historical_candidates(folder),
        'requests': [{k: r.get(k) for k in ('url', 'status', 'error', 'scope', 'file', 'sha256',
            'started_at', 'retrieved_at', 'published_at')} for r in attempts],
        'network_stops': ['CVM 155293 TimeoutError: no retry.',
            'api.mziq.com public catalogue HTTP 401: origin stopped; no PDF requests there.'],
        'changed_code_files': ['recommender/round2_plpl_financial.py',
                               'recommender/test_round2_plpl_financial.py'],
        'changed_evidence_scope': BASE, 'changed_reports': 'financial-plpl-*',
        'DESK3_BMOB3_evidence': 'preserved unchanged; full zero period proof still unapproved',
        'integration': 'No capex alias, numeric replacement, new EPS, scope switch, or stale fallback authorized.'}
    save(REPORTS / 'financial-plpl-handoff-v2.json', result)
    return result
