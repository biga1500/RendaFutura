"""Isolated exact-current annual research; no production or publication imports.

All acquisition uses the shared polite collector. Original CVM packages and
members are immutable. This module never manufactures EPS or changes scope.
"""

import argparse
from datetime import date, datetime, timezone
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
BASE = 'raw/primary-research/all-pending/round2-financial'
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
CUTOFF = date(2026, 9, 30)
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
SPECS = {
    'BPAC3': (22616, 'ON', 2), 'EVEN3': (20524, 'ON', 4),
    'PINE4': (20567, 'PN', 2), 'SIMH3': (25003, 'ON', 2),
    'WHRL4': (14346, 'PN', 4), 'WIZC3': (23590, 'ON', 2),
    'WLMM3': (11070, 'ON', 5), 'WLMM4': (11070, 'PNA', 5),
}
EXPECTED = {
    'BPAC3': ('2', '155648', '2026-03-24', '30.306.294/0001-45'),
    'EVEN3': ('1', '155582', '2026-03-23', '43.470.988/0001-65'),
    'PINE4': ('3', '155965', '2026-03-31', '62.144.175/0001-20'),
    'SIMH3': ('1', '155854', '2026-03-30', '07.415.333/0001-20'),
    'WHRL4': ('1', '155763', '2026-03-27', '59.105.999/0001-86'),
    'WIZC3': ('1', '155497', '2026-03-19', '42.278.473/0001-03'),
    'WLMM3': ('1', '155797', '2026-03-27', '33.228.024/0001-51'),
    'WLMM4': ('1', '155797', '2026-03-27', '33.228.024/0001-51'),
}
PACKAGE_PINS = {
    'PINE4': ('a21761cae0746c4aace81bbaab4053426bfb09f75876537e1620b59e89eb4a6f',
              '7213f0114f7c328dfaba561dc31db0a509fc1ddf39a665322c73ecb90af79a2d',
              '1ccda2fae320f4dd4871e1f8c66c114682909dc15d0432272f21fdccc3a965d2'),
    'SIMH3': ('d349502d8527288ffceb44735a55bc782e5881559a0defe91905036a1bd0189f',
              '2d0bdfc8ddde46a2a6ef5c597d6919a6634763e8b31332b270784207fb1bdcd9',
              'e0109976d18ac4fd870b8fa7499ce3b7809ffd7833366b1312fdf89298b580a1'),
    'WLMM3': ('b6bbd172e861c3fd35704648f9aefefe3eb3bc1b858758968113d6393b62db6e',
              '56c7ae71e2b71a004f895bd8c69320d6fa3ace3a83a4128ae1ffb79d8f04003d',
              '54d2bc73901f74469a2a02234f78062e05d2e80276cf21e533489e5e861c433d'),
}
PDF_PINS = {
    'BPAC3': 'ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f',
    'EVEN3': 'fe3ba3afe073ba758c75ebec3900690481ee4157250b823f4f03b7fe5021853f',
    'WHRL4': '5289c2af707ad96eff2e4b9c398a5ee2d84877328a6e37476621781b540839c2',
    'WIZC3': '77c3d97099d285f441cf6730df88a6666424cd7cb07d56dc24ded80fd27fc59e',
}
PDF_URLS = {
    'BPAC3': 'https://d169uzu5o4xu1k.cloudfront.net/78a5a309-f13a-41ed-a7d1-814e37b63259/2025/8fc4586b-7c7b-42a1-acc1-be8d6ba7ac76.pdf',
    'EVEN3': 'https://api.mziq.com/mzfilemanager/v2/d/f928111c-c68e-45c9-bfc3-5d678e52e7ad/be924d29-ee97-a7dc-e757-3c691495dd13?origin=2',
    'WHRL4': 'https://downloads.ctfassets.net/qfxflpv0atz9/4NgFdMkPxALBMYzeYZIkmz/07e1faf19dc2e56359d52083c0e9eddd/DFP_Whirlpool_S.A._2025__1_.pdf',
    'WIZC3': 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=155497',
}
ASSESSMENTS = {
    'BPAC3': {
        'status': 'missing_exact_current_parent_class_EPS_evidence', 'pages': [13, 66],
        'finding': 'Current v2 consolidated CSV has only zero basic EPS roots, no ON leaf. '
            'Prior RI IFRS PDF note 25 reports ON basic 2.30 using total profit 16663803 thousand, '
            'whereas its DRE and the current CSV report parent 16005964 and NCI 657839 thousand. '
            'The RI PDF does not certify revision v2 identity. Exact v2 native acquisition timed out.',
        'missing_source_field': 'Exact 155648 v2 printed ON basic EPS with class-allocated '
            'parent numerator and annual weighted ON denominator excluding NCI.',
    },
    'EVEN3': {
        'status': 'missing_gross_cash_capex', 'pages': [35],
        'finding': 'Current 6.02.01 says acquisition of PPE but is positive 12618 thousand. '
            'The same-year audited DFC explicitly labels the line acquisitions/(disposals) '
            'of PPE and intangibles, net. CFO is 88842 thousand. The individual -755 thousand '
            'is a different scope. Net cash inflow does not establish zero gross acquisitions.',
        'missing_source_field': 'Gross consolidated FY2025 cash paid for PPE/intangibles, '
            'separated from cash received on disposals in this same filing.',
    },
    'PINE4': {
        'status': 'unresolved_exact_native_class_numerator_conflict', 'pages': [20, 21, 106],
        'finding': 'Exact native 155965 v3 IFRS DRE p106 prints PN basic/diluted 1.86, '
            'PN numerator 210969 thousand, weighted PN shares 113288564; ON numerator '
            '221909 thousand, weighted ON 119162783. The class numerators sum to total '
            '432878 thousand, while parent profit is 430515 and NCI is 2363 thousand. '
            'Native form p21 contains generic roots 186223, not a class EPS leaf.',
        'missing_source_field': 'Same-v3 primary correction/clarification reconciling '
            'printed class allocations and basic EPS to parent profit excluding NCI.',
    },
    'SIMH3': {
        'status': 'unresolved_exact_native_profit_and_EPS_scope_conflict', 'pages': [24, 140],
        'finding': 'Exact native v1 form p24 prints ON basic 0.0931 and no second zero ON '
            'leaf, but parent 39471 thousand differs from note 32 p140 numerator 39741. '
            'The note table is explicitly Controladora. Total 215250 differs from parent '
            '39471 plus NCI 173170 by 2609 thousand. A form zero placeholder alone does '
            'not authorize changing profit or borrowing Controladora EPS.',
        'missing_source_field': 'Same-v1 primary clarification of parent 39471 versus '
            '39741, total/discontinued reconciliation, and same-scope ON basic EPS basis.',
    },
    'WHRL4': {
        'status': 'missing_explicit_PN_basic_EPS', 'pages': [77, 79],
        'finding': 'Current CSV reports only ON basic 0.2984629881. Note 18.6 p79 labels '
            'its pooled 1502786 thousand weighted shares ordinary, while note 18.1 p77 '
            'separates ON 1028701 and PN 474085 thousand. The pooled printed 0.29846 '
            'does not expressly apply to PN; dividends on p79 have different class rates.',
        'missing_source_field': 'Explicit PN basic EPS and parent allocation/weighted PN '
            'denominator, or issuer clarification of the pooled EPS applicability to PN.',
    },
    'WIZC3': {
        'status': 'consolidated_basic_EPS_explicitly_not_applicable', 'pages': [56, 102],
        'finding': 'Exact native v1 audited DRE p56 explicitly reports consolidated EPS '
            'and share counts n/a. Note 24.4 p102 explicitly labels the 1.25745 EPS table '
            'Controladora. Consolidated core is substantive, not an empty template. '
            'Whole-individual selection is not authorized by this round.',
        'missing_source_field': 'Same-filing explicit consolidated basic ON EPS basis. '
            'The individual prototype does not resolve this source field.',
    },
    'WLMM3': {
        'status': 'unresolved_exact_native_EPS_source_conflict', 'pages': [94],
        'finding': 'Exact v1 note 32 says class shares did not change in the last two '
            'years, with weighted ON 16571220 and preferred 19843450. Continued class '
            'numerators 40559687.37/48568791.45 BRL and discontinued '
            '-1204364.13/-1442183.47 BRL do not support the printed class EPS. '
            'The ordinary parser nevertheless accepts ON 2.25219. Its passing numeric '
            'route does not resolve the existing native conflict exclusion.',
        'missing_source_field': 'Issuer same-filing clarification/correction reconciling '
            'reported ON basic 2.25219 to its allocated parent numerator and denominator.',
    },
    'WLMM4': {
        'status': 'unresolved_exact_native_EPS_and_preferred_class_binding', 'pages': [94],
        'finding': 'The same note-32 conflict affects native PN 2.47741. The instrument '
            'is legally PNA; relabeling PN alone cannot resolve the inconsistent printed '
            'numerators and weighted denominator. The attempted official annual separata '
            'link returned 404 and supplies no correction.',
        'missing_source_field': 'Same-filing issuer correction reconciling PN EPS, plus '
            'complete legal preferred-class universe binding that printed EPS uniquely to PNA.',
    },
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _save(path, value):
    body = value if isinstance(value, bytes) else (
        json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _require(path.read_bytes() == body, 'immutable_round2_evidence_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def _read(folder, relative, digest=None):
    root = Path(folder).resolve()
    path = root / relative
    _require(path.resolve().is_relative_to(root) and not path.is_symlink()
             and path.is_file(), 'round2_unsafe_evidence_path')
    body = path.read_bytes()
    _require(digest is None or _sha(body) == digest, 'round2_evidence_changed')
    return body


def current_tables(folder=ROOT, cutoff=CUTOFF):
    _require(cutoff == CUTOFF, 'round2_unreviewed_cutoff')
    body = (Path(folder) / ANNUAL).read_bytes()
    _require(_sha(body) == ANNUAL_SHA, 'round2_annual_archive_changed')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        masters = list(native._rows(archive, 'dfp_cia_aberta_2025.csv'))
        latest = {}
        for ticker, (cvm, _, _) in SPECS.items():
            eligible = [row for row in masters if int(row['CD_CVM']) == cvm
                        and native._day(row['DT_REFER']) <= cutoff
                        and native._day(row['DT_RECEB']) <= cutoff]
            _require(bool(eligible), 'round2_current_master_missing')
            row = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            _require(row['DT_REFER'] == '2025-12-31' and tuple(row[k] for k in (
                'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA')) == EXPECTED[ticker],
                'round2_current_filing_changed')
            latest[ticker] = row
        cvms = {spec[0] for spec in SPECS.values()}
        tables = {}
        for suffix in (*native.TABLES, '_DMPL_con', '_DRE_ind'):
            name = f'dfp_cia_aberta{suffix}_2025.csv'
            tables[suffix] = [row for row in native._rows(archive, name)
                if int(row['CD_CVM']) in cvms and row['ORDEM_EXERC'] == '\u00daLTIMO'
                and row['DT_REFER'] == '2025-12-31'
                and any(int(row['CD_CVM']) == SPECS[t][0] and row['VERSAO'] == latest[t]['VERSAO']
                        for t in SPECS)]
    return latest, tables


def fetch_current(tickers, folder=ROOT):
    latest, _ = current_tables(folder)
    collector = PublicSourceCollector(folder, 'round2-financial')
    results = {}
    for ticker in tickers:
        _require(ticker in SPECS, 'round2_ticker_outside_scope')
        master = latest[ticker]
        url = master['LINK_DOC'].replace('http://', 'https://', 1)
        receipt = collector.fetch(url, discovery={
            'source_file': ANNUAL, 'source_sha256': ANNUAL_SHA,
            'master_url_raw': master['LINK_DOC'], 'scheme_upgrade_only': True,
            'filing_id': master['ID_DOC'], 'version': master['VERSAO'],
            'published_at': master['DT_RECEB']})
        results[ticker] = receipt
        print(json.dumps({'ticker': ticker, 'status': receipt['status'],
                          'error': receipt['error'], 'bytes': receipt['bytes']}), flush=True)
        if receipt['status'] == 200 and receipt['error'] is None:
            extract_package(folder, ticker, receipt, collector.body(receipt))
    return results


def extract_package(folder, ticker, receipt, body):
    master = current_tables(folder)[0][ticker]
    _require(receipt['url'] == master['LINK_DOC'].replace('http://', 'https://', 1)
             and receipt['status'] == 200 and receipt['error'] is None
             and receipt['sha256'] == _sha(body) and receipt['bytes'] == len(body),
             'round2_transport_proof_mismatch')
    cvm = SPECS[ticker][0]
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        _require(archive.testzip() is None, 'round2_package_CRC_failed')
        xml_member = f'{cvm:06d}DFP31-12-2025v{master["VERSAO"]}.xml'
        xml_body = archive.read(xml_member)
        xml = ET.fromstring(xml_body)
        _require(tuple(xml.findtext(field) for field in (
            'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
            'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) == (
                f'{cvm:06d}', re.sub(r'\D', '', master['CNPJ_CIA']),
                master['VERSAO'], '31/12/2025'), 'round2_package_identity_mismatch')
        pdf_members = [name for name in archive.namelist()
                       if re.fullmatch(master['ID_DOC'] + f'_{cvm:06d}_' + r'\d+\.pdf', name)]
        _require(len(pdf_members) == 1, 'round2_pdf_member_ambiguous')
        members = []
        for name in (xml_member, pdf_members[0]):
            content = archive.read(name)
            relative = f'{BASE}/native-{ticker}-{_sha(content)}{Path(name).suffix}'
            _save(Path(folder) / relative, content)
            members.append({'file': relative, 'sha256': _sha(content), 'bytes': len(content),
                            'original_zip_member': name})
    proof = {'ticker': ticker, 'cvm': cvm, 'fiscal_end': master['DT_REFER'],
             'version': master['VERSAO'], 'filing_id': master['ID_DOC'],
             'cnpj': master['CNPJ_CIA'], 'published_at': master['DT_RECEB'],
             'retrieved_at': receipt['retrieved_at'], 'url': receipt['url'],
             'original_container': receipt['file'], 'original_container_sha256': _sha(body),
             'members': members, 'point_in_time_archive_verified': False}
    _save(Path(folder) / BASE / f'package-{ticker}.json', proof)
    return proof


def parser_snapshot(folder=ROOT):
    statements, diagnostics, sources = native.prepare_financials(folder, SPECS, CUTOFF)
    result = {}
    for ticker, spec in SPECS.items():
        selected = financial_features(statements, [], CUTOFF, spec)
        result[ticker] = {
            'features': None if selected is None else selected[0],
            'record': None if selected is None else selected[1],
            'latest_records': [r for r in statements if r['cvm'] == spec[0]
                and r['share_class'] == spec[1] and r['fiscal_end'] == '2025-12-31'],
            'candidate_diagnostics': [r for r in diagnostics
                if r.get('ticker') == ticker and r.get('kind') == 'candidate'
                and r.get('fiscal_end') == '2025-12-31'],
        }
    return result


def import_cached(folder=ROOT):
    """Carry forward exact native bytes with their original retrieval clocks."""
    folder = Path(folder)
    packages = (
        ('SIMH3', 'raw/primary-research/remaining-financials/response-SIMH3.json',
         'd349502d8527288ffceb44735a55bc782e5881559a0defe91905036a1bd0189f'),
        ('WLMM3', 'raw/primary-research/wlm-financial-class/response-native-https.json',
         'b6bbd172e861c3fd35704648f9aefefe3eb3bc1b858758968113d6393b62db6e'),
    )
    for ticker, receipt_name, digest in packages:
        path = folder / receipt_name
        original = path.read_bytes()
        receipt = json.loads(original)
        body = (path.parent / receipt['file']).read_bytes()
        _require(receipt['status'] == 200 and not receipt.get('failure')
                 and receipt['sha256'] == digest == _sha(body)
                 and receipt['bytes'] == len(body), 'round2_cached_package_changed')
        normalized = {**receipt, 'error': None,
                      'retrieved_at': receipt['completed_at'],
                      'file': (path.parent / receipt['file']).relative_to(folder).as_posix()}
        proof = extract_package(folder, ticker, normalized, body)
        _save(folder / BASE / f'cached-transport-{ticker}.json', {
            'original_receipt': receipt_name, 'original_receipt_sha256': _sha(original),
            'original_receipt_body': receipt, 'proof': proof,
            'new_network_request': False})
    cached_pdfs = {
        'BPAC3': ('bpac-ifrs-2025',
            'ce5ff293c0055fc951571e59925f7b4f193d1dc39bffd902bc9a39eaab30776f'),
        'EVEN3': ('even-dfp-2025',
            'fe3ba3afe073ba758c75ebec3900690481ee4157250b823f4f03b7fe5021853f'),
        'WHRL4': ('whrl-dfp-2025',
            '5289c2af707ad96eff2e4b9c398a5ee2d84877328a6e37476621781b540839c2'),
        'WIZC3': ('wiz-native-155497-v1',
            '77c3d97099d285f441cf6730df88a6666424cd7cb07d56dc24ded80fd27fc59e'),
    }
    for ticker, (stem, digest) in cached_pdfs.items():
        prior = f'raw/primary-research/financials/{stem}-{digest}.pdf'
        body = (folder / prior).read_bytes()
        _require(_sha(body) == digest, 'round2_prior_PDF_changed')
        relative = f'{BASE}/cached-{ticker}-{digest}.pdf'
        _save(folder / relative, body)
        _save(folder / BASE / f'cached-PDF-{ticker}.json', {
            'file': relative, 'sha256': digest, 'bytes': len(body), 'original_file': prior,
            'retrieved_at': None, 'retrieval_clock_basis': 'prior_transport_not_retained',
            'use': 'negative_evidence_only_no_current_filing_override',
            'not_a_fallback_financial_record': True})


def inspect_pdf(ticker, pages, folder=ROOT):
    from pypdf import PdfReader

    folder = Path(folder)
    package_path = folder / BASE / f'package-{ticker}.json'
    if package_path.exists():
        proof = json.loads(package_path.read_bytes())
        pdf = next(m for m in proof['members'] if m['file'].endswith('.pdf'))
    else:
        pdf = json.loads((folder / BASE / f'cached-PDF-{ticker}.json').read_bytes())
    path = folder / pdf['file']
    _require(_sha(path.read_bytes()) == pdf['sha256'], 'round2_inspection_PDF_changed')
    reader = PdfReader(path)
    poppler = shutil.which('pdftoppm')
    _require(poppler is not None, 'round2_Poppler_required')
    result = {'ticker': ticker, 'source': pdf, 'total_pages': len(reader.pages), 'pages': []}
    for number in pages:
        _require(type(number) is int and 1 <= number <= len(reader.pages), 'round2_invalid_PDF_page')
        prefix = folder / BASE / f'review-{ticker}-p{number}'
        process = subprocess.run([poppler, '-f', str(number), '-l', str(number),
            '-scale-to', '1800', '-png', '-singlefile', str(path), str(prefix)],
            check=False, capture_output=True, timeout=45)
        _require(process.returncode == 0 and prefix.with_suffix('.png').is_file(),
                 'round2_page_render_failed')
        result['pages'].append({'pdf_page': number,
            'text': reader.pages[number - 1].extract_text(),
            'render': prefix.with_suffix('.png').relative_to(folder).as_posix(),
            'render_sha256': _sha(prefix.with_suffix('.png').read_bytes())})
    _save(folder / BASE / f'pages-{ticker}.json', result)
    return result


def verify_package(folder, ticker, master):
    """Validate original transport and native identity without writing or networking."""
    container_sha, pdf_sha, xml_sha = PACKAGE_PINS[ticker]
    proof = json.loads(_read(folder, f'{BASE}/package-{ticker}.json'))
    _require(tuple(proof[key] for key in (
        'ticker', 'cvm', 'fiscal_end', 'version', 'filing_id', 'cnpj', 'published_at')) == (
            ticker, SPECS[ticker][0], master['DT_REFER'], master['VERSAO'],
            master['ID_DOC'], master['CNPJ_CIA'], master['DT_RECEB']),
        'round2_package_master_mismatch')
    url = master['LINK_DOC'].replace('http://', 'https://', 1)
    _require(proof['url'] == url and proof['original_container_sha256'] == container_sha
             and proof['point_in_time_archive_verified'] is False,
             'round2_package_provenance_mismatch')
    body = _read(folder, proof['original_container'], container_sha)
    if ticker == 'PINE4':
        receipt_file = f'{BASE}/response-{_sha(url.encode())}.json'
        receipt = json.loads(_read(folder, receipt_file))
        retrieved_at = receipt['retrieved_at']
        _require(receipt.get('error') is None
                 and receipt['file'] == proof['original_container'],
                 'round2_original_transport_changed')
    else:
        copied = json.loads(_read(folder, f'{BASE}/cached-transport-{ticker}.json'))
        original = _read(folder, copied['original_receipt'], copied['original_receipt_sha256'])
        receipt = json.loads(original)
        _require(receipt == copied['original_receipt_body']
                 and copied['proof'] == proof and copied['new_network_request'] is False
                 and receipt.get('failure') is None, 'round2_original_transport_changed')
        retrieved_at = receipt['completed_at']
    _require(receipt['status'] == 200 and receipt['url'] == url
             and receipt['sha256'] == container_sha and receipt['bytes'] == len(body)
             and proof['retrieved_at'] == retrieved_at,
             'round2_original_transport_changed')
    _require(datetime.fromisoformat(retrieved_at).utcoffset() is not None,
             'round2_retrieval_timezone_missing')
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        _require(archive.testzip() is None and len(proof['members']) == 2,
                 'round2_native_members_invalid')
        for suffix, digest in (('.pdf', pdf_sha), ('.xml', xml_sha)):
            members = [member for member in proof['members'] if member['file'].endswith(suffix)]
            _require(len(members) == 1 and members[0]['sha256'] == digest,
                     'round2_native_member_digest_mismatch')
            member = members[0]
            content = _read(folder, member['file'], digest)
            _require(len(content) == member['bytes']
                     and archive.read(member['original_zip_member']) == content,
                     'round2_native_member_changed')
            if suffix == '.xml':
                xml = ET.fromstring(content)
                _require(tuple(xml.findtext(field) for field in (
                    'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
                    'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) == (
                        f'{SPECS[ticker][0]:06d}', re.sub(r'\D', '', master['CNPJ_CIA']),
                        master['VERSAO'], '31/12/2025'), 'round2_package_identity_mismatch')
            else:
                _require(re.fullmatch(master['ID_DOC'] + f'_{SPECS[ticker][0]:06d}_'
                         + r'\d+\.pdf', member['original_zip_member']) is not None,
                         'round2_pdf_filing_mismatch')
                pdf = member
    return {**pdf, 'id': 'round2-financial:' + pdf_sha, 'url': url,
            'retrieved_at': retrieved_at, 'published_at': master['DT_RECEB'],
            'original_container': proof['original_container'],
            'original_container_sha256': container_sha, 'exact_filing_verified': True,
            'point_in_time_archive_verified': False, 'authority': 'primary',
            'use': 'negative_evidence_only_no_account_override'}


def get_round2_readings(folder=ROOT, cutoff=CUTOFF):
    """Return (empty approved readings, explicit blockers, verified sources).

    No recovered interpretation meets the current consolidated contract. In
    particular, a passing WLMM3 parser record is not documentary clearance.
    """
    latest, _ = current_tables(folder, cutoff)
    sources, by_ticker = [], {}
    for ticker in PACKAGE_PINS:
        source = verify_package(folder, ticker, latest[ticker])
        sources.append(source)
        by_ticker[ticker] = source
    for ticker, digest in PDF_PINS.items():
        proof = json.loads(_read(folder, f'{BASE}/cached-PDF-{ticker}.json'))
        body = _read(folder, proof['file'], digest)
        _require(proof['sha256'] == digest and proof['bytes'] == len(body)
                 and proof['retrieved_at'] is None
                 and proof['use'] == 'negative_evidence_only_no_current_filing_override',
                 'round2_prior_PDF_proof_changed')
        source = {**proof, 'id': 'round2-financial:' + digest, 'url': PDF_URLS[ticker],
                  'authority': 'primary', 'published_at': None,
                  'exact_filing_verified': False, 'point_in_time_archive_verified': False}
        sources.append(source)
        by_ticker[ticker] = source
    by_ticker['WLMM4'] = by_ticker['WLMM3']
    diagnostics = []
    for ticker, assessment in ASSESSMENTS.items():
        master = latest[ticker]
        diagnostics.append({
            'kind': 'round2_financial_evidence', 'ticker': ticker,
            'cvm': SPECS[ticker][0], 'share_class': SPECS[ticker][1],
            'fiscal_end': master['DT_REFER'], 'version': master['VERSAO'],
            'filing_id': master['ID_DOC'], 'published_at': master['DT_RECEB'],
            'accounts_scope': 'consolidated', 'status': 'pending_unchanged',
            'evidence_status': assessment['status'],
            'missing_source_field': assessment['missing_source_field'],
            'finding': assessment['finding'], 'primary_source_ref': by_ticker[ticker]['id'],
            'pdf_pages': assessment['pages'], 'model_admission': False,
            'applied_reading': None, 'EPS_computed': False, 'numeric_values_changed': False,
        })
    return {}, diagnostics, sources


def write_handoff(folder=ROOT, reports=REPORTS):
    readings, findings, sources = get_round2_readings(folder)
    latest, tables = current_tables(folder)
    before = json.loads((Path(reports) / 'financial-before.json').read_bytes())
    after = parser_snapshot(folder)
    _require(before == after, 'round2_unexpected_production_behavior_changed')
    _save(Path(reports) / 'financial-after.json', after)
    for finding in findings:
        ticker = finding['ticker']
        row = after[ticker]
        finding['parser_before_after'] = {
            'unchanged': True, 'features_before': before[ticker]['features'],
            'features_after': row['features'], 'candidate_diagnostics': row['candidate_diagnostics']}
        finding['annual_csv_cells'] = {
            suffix: [{key: cell.get(key) for key in native.COLUMNS}
                     for cell in cells if int(cell['CD_CVM']) == SPECS[ticker][0]
                     and (cell['CD_CONTA'].startswith(('3.99', '6.02'))
                          or cell['CD_CONTA'] in ('3.09', '3.09.01', '3.09.02', '3.10',
                                                  '3.11', '3.11.01', '3.11.02', '6.01'))]
            for suffix, cells in tables.items() if suffix in ('_DRE_con', '_DFC_MI_con')}
        dre = [r for r in tables['_DRE_con'] if int(r['CD_CVM']) == SPECS[ticker][0]]
        finding['full_current_DRE_sha256'] = native._dre_fingerprint(dre, full=True)
    responses = [json.loads(path.read_bytes())
                 for path in sorted((Path(folder) / BASE).glob('response-*.json'))]
    inspected = []
    for path in sorted((Path(folder) / BASE).glob('pages-*.json')):
        proof = json.loads(path.read_bytes())
        inspected.append({'ticker': proof['ticker'], 'source_sha256': proof['source']['sha256'],
            'page_text_file': path.relative_to(folder).as_posix(),
            'visually_reviewed_pages': [number for number in ASSESSMENTS[proof['ticker']]['pages']
                                      if number in {p['pdf_page'] for p in proof['pages']}],
            'renders': [{k: page[k] for k in ('pdf_page', 'render', 'render_sha256')}
                        for page in proof['pages']]})
    report = {
        'schema_version': 1, 'scope': list(SPECS), 'reference_date': CUTOFF.isoformat(),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'approved_readings': readings, 'new_admissions': [], 'changed_financial_fields': [],
        'current108_262_recomputed': False, 'inventory_or_pointer_publication_performed': False,
        'result': 'No evidence-backed current consolidated recovery; retain all eight pending cases.',
        'WLMM3_warning': 'Normal financial_features already passes this stock. The unchanged '
            'native-financial-EPS-source-conflict exclusion must remain in the parent inventory.',
        'WIZ_scope_policy': 'No whole-individual request or consolidated substitution performed.',
        'annual_source': {'file': ANNUAL, 'sha256': ANNUAL_SHA,
            'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip',
            'retrieved_at': None, 'retrieval_clock_not_inferred_from_mtime': True},
        'current_masters': latest, 'findings': findings, 'sources': sources,
        'visual_inspections': inspected,
        'network': {'collector_scope': 'round2-financial', 'responses': [
            {key: row.get(key) for key in ('url', 'status', 'error', 'bytes', 'file',
                                         'sha256', 'started_at', 'retrieved_at', 'published_at')}
            for row in responses],
            'retries': 0, 'denied_origins_requested': [], 'stopped_origins_retried': [],
            'partial_native_WIZ_response_used_as_evidence': False,
            'limitation': 'BPAC/EVEN/WHRL exact native attempts timed out; WIZ ZIP exceeded '
                'the collector 16MB bound; WLM official separata returned 404. These are '
                'acquisition limits, not proof that an undiscovered correction cannot exist.'},
        'offline_verification': {'normal_prepare_financials_and_financial_features': True,
            'full_before_after_equal': True, 'no_new_scope_or_account_interpretation': True},
        'changed_files': ['recommender/round2_financial_readings.py',
            'recommender/test_round2_financial_readings.py',
            'recommender/data/market/' + BASE + '/',
            '../work/recovery_round2_20261025/financial-*'],
        'parent_integration': 'No new readings to import. Preserve current default scope, '
            'source conflict gates and current pointers. New exact native PINE v3 evidence '
            'is negative evidence confirming, not repairing, the class numerator conflict.',
    }
    _save(Path(reports) / 'financial-handoff.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('fetch', 'baseline', 'import-cached', 'inspect', 'report'))
    parser.add_argument('--tickers', nargs='+', choices=tuple(SPECS), default=list(SPECS))
    parser.add_argument('--pages', nargs='+', type=int)
    args = parser.parse_args()
    if args.command == 'fetch':
        fetch_current(args.tickers)
    elif args.command == 'import-cached':
        import_cached()
    elif args.command == 'inspect':
        _require(len(args.tickers) == 1 and args.pages, 'one_inspection_ticker_and_pages_required')
        result = inspect_pdf(args.tickers[0], args.pages)
        for page in result['pages']:
            print(page['pdf_page'], page['render'])
    elif args.command == 'report':
        report = write_handoff()
        print(report['result'])
        print(REPORTS / 'financial-handoff.json')
    else:
        snapshot = parser_snapshot()
        _save(REPORTS / 'financial-before.json', snapshot)
        for ticker, row in snapshot.items():
            print(ticker, 'features_available=', row['features'] is not None,
                  json.dumps(row['candidate_diagnostics']))


if __name__ == '__main__':
    main()
