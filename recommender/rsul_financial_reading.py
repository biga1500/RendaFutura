"""Exact same-scope profit-root reading from Riosulense's native 2025 DFP.

The note explicitly attributes the consolidated result to the shareholders.
Native EPS is retained, not computed from reported ordinary/preferred counts.
"""
import hashlib
import io
import json
from pathlib import Path
import zipfile


BASE = 'raw/primary-research/all-pending/parent'
CONTAINER_SHA = '4932b8831f81f6a3673e3f59ba9ca3184dc74ecb46d3bc3a8ab7b861ac7d54f3'
PDF_SHA = 'abadd5f0c796525351893ecbaaf4a7960071b4a8b08b00e0340e81e160773134'
ARCHIVE_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
DRE_SHA = 'a86470a0b08a5baa09853ddf29cfda2f97ece3f09aca7b291a51e8b277bbdffa'
URL = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
       '?CodigoInstituicao=1&NumeroSequencialDocumento=155874')
PDF_MEMBER = '155874_013439_25102026195251.pdf'
REVIEW = {'cvm': 13439, 'fiscal_end': '2025-12-31', 'version': '1', 'filing_id': '155874',
          'published_at': '2026-03-31', 'accounts_scope': 'consolidated',
          'profit_account': '3.11', 'parent_profit_brl': '64448000',
          'full_dre': True, 'dre_sha256': DRE_SHA, 'pdf_page': 59,
          'note': 'Note 26 explicitly reports consolidated result attributable to company shareholders '
                  'of BRL 64,448,000 for 2025. Native consolidated DRE PDF page 20 reports root 3.11 '
                  'and ON/PN EPS; the annual CSV attribution children are zero form placeholders. '
                  'Use the reported same-scope root only for this exact filing; keep native PN EPS '
                  '11.67921 unchanged. No EPS, payment, share count or older filing is substituted.'}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def get_rsul_reading(folder, cutoff):
    from .inventory_financials import _day, _dre_fingerprint, _rows

    folder = Path(folder).resolve()
    _require(_day(cutoff).isoformat() == '2026-09-30', 'rsul_unreviewed_cutoff')

    def read(name, digest):
        path = folder / name
        _require(path.resolve().is_relative_to(folder) and not path.is_symlink()
                 and path.stat().st_size <= 20_000_000, 'rsul_unsafe_evidence_path')
        content = path.read_bytes()
        _require(hashlib.sha256(content).hexdigest() == digest, 'rsul_evidence_changed')
        return content

    container_file = f'{BASE}/body-{CONTAINER_SHA}.bin'
    original = read(container_file, CONTAINER_SHA)
    receipt_file = f'{BASE}/response-' + hashlib.sha256(URL.encode()).hexdigest() + '.json'
    receipt = json.loads((folder / receipt_file).read_bytes())
    _require(receipt['url'] == URL and receipt['file'] == container_file
             and receipt['status'] == 200 and receipt['error'] is None
             and receipt['sha256'] == CONTAINER_SHA and receipt['bytes'] == len(original),
             'rsul_original_transport_proof_changed')
    pdf_file = f'{BASE}/native-RSUL4-{PDF_SHA}.pdf'
    pdf = read(pdf_file, PDF_SHA)
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        _require(archive.read(PDF_MEMBER) == pdf and '013439DFP31-12-2025v1.xml' in archive.namelist(),
                 'rsul_native_member_identity_mismatch')
    native_file = 'raw/official-financials/dfp_cia_aberta_2025.zip'
    native = read(native_file, ARCHIVE_SHA)
    with zipfile.ZipFile(io.BytesIO(native)) as archive:
        master = [row for row in _rows(archive, 'dfp_cia_aberta_2025.csv')
                  if int(row['CD_CVM']) == 13439 and row['DT_REFER'] == '2025-12-31']
        _require(len(master) == 1 and master[0]['VERSAO'] == '1'
                 and master[0]['ID_DOC'] == '155874' and master[0]['DT_RECEB'][:10] == '2026-03-31'
                 and master[0]['CNPJ_CIA'] == '85.778.074/0001-06', 'rsul_latest_filing_changed')
        rows = [row for row in _rows(archive, 'dfp_cia_aberta_DRE_con_2025.csv')
                if int(row['CD_CVM']) == 13439 and row['DT_REFER'] == '2025-12-31'
                and row['VERSAO'] == '1' and row['ORDEM_EXERC'] == '\u00daLTIMO' and row['MOEDA'] == 'REAL']
    _require(_dre_fingerprint(rows, full=True) == DRE_SHA, 'rsul_native_DRE_changed')
    expected = {'3.11': '64448.0000000000', '3.11.01': '0.0000000000',
                '3.11.02': '0.0000000000', '3.99.01.02': '11.6792100000'}
    _require(all([row['VL_CONTA'] for row in rows if row['CD_CONTA'] == key] == [value]
                 for key, value in expected.items()), 'rsul_native_cells_changed')
    review_file = f'{BASE}/rsul-profit-root-review.json'
    reviewed = (folder / review_file).read_bytes()
    _require(json.loads(reviewed) == REVIEW, 'rsul_local_reading_changed')
    source_id = 'primary-financials:' + PDF_SHA
    source = {'id': source_id, 'url': URL, 'file': pdf_file, 'sha256': PDF_SHA, 'bytes': len(pdf),
              'retrieved_at': receipt['retrieved_at'], 'published_at': REVIEW['published_at'],
              'original_container': container_file, 'original_container_sha256': CONTAINER_SHA,
              'original_zip_member': PDF_MEMBER, 'provider': 'CVM native issuer annual filing',
              'authority': 'primary', 'document_type': 'audited_annual_statements',
              'use': 'exact_same_scope_profit_root_interpretation', 'point_in_time_archive_verified': False}
    reading = {**REVIEW, 'manifest_file': review_file,
               'manifest_sha256': hashlib.sha256(reviewed).hexdigest(),
               'primary_evidence': {'source_ref': source_id, 'pdf_page': 59, 'note': REVIEW['note']}}
    return reading, [source]
