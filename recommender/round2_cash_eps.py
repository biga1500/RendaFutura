"""Exact-current consolidated EPS readings for the two recovered cash issuers.

No core, registry, inventory, snapshot, or pointer is written by this module.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import socket
import xml.etree.ElementTree as ET
import zipfile

from . import inventory_financials as native
from . import round2_cash as cash
from .bulk_pending_sources import _save


ROOT = cash.ROOT
CUTOFF = cash.CUTOFF
BASE = cash.CACHE
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
ANNUAL_FILE = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
TARGETS = {'ALLD3': (25330, 'ON', 'BRALLDACNOR3'), 'JSLG3': (22020, 'ON', 'BRJSLGACNOR2')}
EXPECTED = {'ALLD3': ('1', '155691', '2026-03-25'), 'JSLG3': ('1', '155651', '2026-03-24')}
DRE_PINS = {
    'ALLD3': '80217f48ca60db3f308d3e2d9a5032b9351418b65cd96bbf53e44b2ad0873edb',
    'JSLG3': 'cbcbe6fdddcfa70014c44c2127d93e38b6fdd7e1165a71c71585033861256a4a',
}
PDF_PINS = {
    'ALLD3': '667649415831e8eb486f7abf0377c7c83ed8b66aea240916d19db3daf2725b6b',
    'JSLG3': '46795a5cf37982cb51a4a626fdb8664b4093ed7e3b197969b84e420eeb3a0744',
}
JSL_PACKAGE_SHA = '3e927c8f0f2402c4d2caf559e627b327597509b65f96627c799329c0c0560890'
JSL_CATALOGUE_SHA = '82a82b3beebe6700a668e493b4581bd846e171e43002282f2150d84e15c0a8a9'
REVIEW_SHA = '9a9215c61cdc24965752026354cc5d8799ca2f91643570f9772701f646fdbb59'
REVIEW_FILE = BASE + '/cash-eps-reviewed.json'


def require(condition, reason):
    if not condition:
        raise ValueError('round2_cash_eps_' + reason)


def current_tables(root=ROOT, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'unreviewed_cutoff')
    source = {'file': ANNUAL_FILE, 'sha256': ANNUAL_SHA,
              'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip',
              'retrieved_at': None, 'published_at': None}
    archive = zipfile.ZipFile(io.BytesIO(cash.read(root, source)))
    masters, tables = {}, {}
    for ticker, (cvm, _, _) in TARGETS.items():
        filings = [r for r in native._rows(archive, 'dfp_cia_aberta_2025.csv')
                   if int(r['CD_CVM']) == cvm and r['DT_REFER'] <= str(cutoff) and r['DT_RECEB'] <= str(cutoff)]
        require(filings, 'current_filing_absent')
        master = max(filings, key=lambda r: (r['DT_REFER'], int(r['VERSAO']), r['DT_RECEB']))
        require(tuple(master[k] for k in ('VERSAO', 'ID_DOC', 'DT_RECEB')) == EXPECTED[ticker]
                and master['DT_REFER'] == '2025-12-31', 'latest_filing_changed_no_fallback')
        masters[ticker] = master
        key = native._key(master)
        tables[ticker] = {}
        for scope in ('con', 'ind'):
            for table in ('DRE', 'BPA', 'BPP', 'DFC_MI', 'DFC_MD'):
                member = f'dfp_cia_aberta_{table}_{scope}_2025.csv'
                if member in archive.namelist():
                    tables[ticker][f'{table}_{scope}'] = [r for r in native._rows(archive, member)
                        if native._key(r) == key and r['ORDEM_EXERC'] == '\u00daLTIMO']
    return masters, tables, source


def _pdf_pages(root, source, numbers):
    import pypdfium2 as pdfium
    # PDFium correctly maps the embedded font in JSL's signed RI original.
    with pdfium.PdfDocument(cash.read(root, source)) as pdf:
        result = {}
        for number in numbers:
            page = pdf[number - 1]
            text = page.get_textpage()
            try:
                result[number] = ' '.join(text.get_text_range().split())
            finally:
                text.close()
                page.close()
        return result


def _contains(text, *spans):
    require(all(cash.norm(span) in cash.norm(text) for span in spans), 'literal_native_proof_changed')


def document_readings(root=ROOT):
    """Build only the two exact, manually reviewed same-consolidated readings."""
    masters, tables, annual = current_tables(root)
    pdfs = {t: cash.source_by_hash(root, h) for t, h in PDF_PINS.items()}
    pages = {t: _pdf_pages(root, pdfs[t], [6, 9, 50] if t == 'ALLD3' else [25]) for t in TARGETS}
    _contains(pages['ALLD3'][6], 'Sao Paulo, 23 de marco de 2026')
    _contains(pages['ALLD3'][9], 'Controladora Consolidado',
              'Lucro liquido do exercicio 332.712 145.532 332.712 145.532',
              'Basico (reais por acao) 28 3,5109 1,5357 3,5109 1,5357')
    _contains(pages['ALLD3'][50], '28. Lucro por acao', 'lucro atribuivel aos acionistas da Companhia',
              'Controladora e Consolidado', 'Numero total ponderado de acoes 94.766.696 94.766.696',
              'Lucro basico por acao (em reais) 3,5109 1,5357')
    _contains(pages['JSLG3'][25], 'JSL S.A.', 'Controladora Consolidado',
              'Lucro liquido do exercicio 81.704 207.312 81.704 207.312',
              'Lucro basico por acao (em R$) 30.1 - - 0,28724 0,72865')

    package = cash.source_by_hash(root, JSL_PACKAGE_SHA)
    pdf, xml = unpack_native(root, 'JSLG3', package)
    tree = ET.fromstring(cash.read(root, xml))
    require(tuple(tree.findtext(field) for field in (
        'DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa', 'Documento/VersaoDocumento',
        'DadosDFP/DataReferencia')) == ('022020', '52548435000179', '1', '31/12/2025'),
        'native_package_identity_mismatch')
    form = _pdf_pages(root, pdf, [16, 119])
    _contains(form[16], 'DFs Consolidadas / Demonstracao do Resultado',
              '3.99.01.01 ON 0,28724', '3.99.01.02 ON 0', 'JSL S.A. Versao : 1')
    _contains(form[119], '30.1', 'Basico', 'Controladora', '284.444.946', '0,28724')
    catalogue = cash.source_by_hash(root, JSL_CATALOGUE_SHA)
    metadata = json.loads(cash.read(root, catalogue))['data']['document_metas']
    matches = [r for r in metadata if r['file_url'] == pdfs['JSLG3']['url']]
    require(len(matches) == 1, 'issuer_catalogue_binding_ambiguous')
    meta, = matches
    require(meta['company_id'] == '5cb9c9f1-1ef6-4d5f-a2fd-fcdddc308a56'
            and meta['file_year'] == 2025 and meta['file_title'] == 'DFP 4T25'
            and meta['is_published'] is True and meta['file_published_date'][:10] == EXPECTED['JSLG3'][2],
            'issuer_catalogue_current_filing_mismatch')

    readings = []
    for ticker, (cvm, share, isin) in TARGETS.items():
        rows = tables[ticker]['DRE_con']
        require(native._dre_fingerprint(rows, full=True) == DRE_PINS[ticker], 'full_dre_changed')
        cells = {r['CD_CONTA']: r for r in rows}
        eps = '3.5109' if ticker == 'ALLD3' else '0.28724'
        require(Decimal(cells['3.99.01.01']['VL_CONTA']) == Decimal(eps)
                and cells['3.99.01.01']['DS_CONTA'] == share, 'exact_class_eps_changed')
        master = masters[ticker]
        reading = {
            'ticker': ticker, 'cvm': cvm, 'share_class': share, 'isin': isin,
            'fiscal_end': master['DT_REFER'], 'version': master['VERSAO'],
            'filing_id': master['ID_DOC'], 'published_at': master['DT_RECEB'],
            'accounts_scope': 'consolidated', 'full_dre': True, 'dre_sha256': DRE_PINS[ticker],
            'manifest_file': REVIEW_FILE, 'manifest_sha256': REVIEW_SHA,
            'primary_evidence': {'source_ref': 'primary-financials:' + PDF_PINS[ticker],
                'pdf_page': 50 if ticker == 'ALLD3' else 25,
                'note': '28(a); also audited DRE p9' if ticker == 'ALLD3' else
                        'Audited consolidated DRE, not the Controladora denominator in note 30'},
            'basic_eps': [],
        }
        if ticker == 'ALLD3':
            require(native._brl(cells['3.11']) == Decimal('332712000')
                    and native._brl(cells['3.11.01']) == 0, 'reported_profit_cells_changed')
            reading.update(profit_account='3.11', parent_profit_brl='332712000', basic_eps=[{
                'account': '3.99.01.01', 'share_class': 'ON', 'value': '3.5109',
                'numerator_brl': '332712000', 'weighted_shares': '94766696',
                'numerator_rounding_brl': '500', 'shares_rounding': '0.5'}])
        else:
            require(native._brl(cells['3.11.01']) == Decimal('81704000')
                    and Decimal(cells['3.99.01.02']['VL_CONTA']) == 0
                    and cells['3.99.01.02']['DS_CONTA'] == 'ON', 'reported_duplicate_cells_changed')
            reading.update(discard_basic_eps_accounts=['3.99.01.02'],
                discard_reason='audited_same_consolidated_dre_single_basic_eps_over_zero_duplicate')
        native._checked_note_eps(rows, reading)
        readings.append(reading)
    sources = [annual, *pdfs.values(), package, pdf, xml, catalogue]
    sources = [{**cash.public_source(s), 'id': 'primary-financials:' + s['sha256'],
                'authority': 'primary', 'use': 'account_interpretation',
                'observed_after_cutoff': True, 'point_in_time_archive_verified': False} for s in sources]
    return {
        'readings': readings, 'sources': sources,
        'literal_pages': pages, 'latest_masters': masters,
        'findings': {
            'ALLD3': {'genuine_absence': False, 'consolidated_substantive': True,
                'native_basic_eps': '3.5109', 'profit_scope': 'Controladora e Consolidado',
                'resolution': 'Note 28 explicitly identifies the reported 3.11 profit as attributable to '
                    'company shareholders. Recognize that same-consolidated numerator, not zero NCI inference.',
                'filing_binding': 'Latest annual v1 only, full consolidated DRE fingerprint, native EPS '
                    'and profit agree with audited annual RI report dated 23 March 2026. '
                    'Exact CVM package timed out; the RI PDF is not asserted byte-identical to it.'},
            'JSLG3': {'genuine_absence': False, 'consolidated_substantive': True,
                'native_basic_eps': '0.28724', 'profit_scope': 'consolidated 3.11.01',
                'resolution': 'RI audited DRE p25 explicitly prints a single consolidated basic EPS '
                    '0.28724, matching native consolidated 3.99.01.01. Discard only the duplicate ON '
                    'zero 3.99.01.02. No EPS created and no profit changed.',
                'individual_note_not_used': 'Native note 30 p119 labels weighted shares Controladora. '
                    'Do not transfer that denominator into the consolidated review.',
                'filing_binding': 'Exact native 155651 v1 XML/PDF; issuer catalogue binds signed RI '
                    'DFP 4T25 to 24 March 2026. Full consolidated DRE fingerprint pinned.'}},
        'policy': {'native_gates_changed': False, 'scope_mix': False, 'eps_inferred': False,
            'older_revision_fallback': False, 'retrieval_time_is_not_publication_time': True,
            'additional_allied_results_catalogue': 'Collector refused a previously stopped origin; '
                'no request or retry made. Cached originals only after this stop.'},
    }


def freeze(root=ROOT):
    evidence = document_readings(root)
    # The manifest cannot contain its own digest.
    for reading in evidence['readings']:
        reading.pop('manifest_sha256')
    body = (json.dumps(evidence, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()
    _save(Path(root) / REVIEW_FILE, body)
    return hashlib.sha256(body).hexdigest()


def get_cash_eps_readings(root, cutoff, identities=None):
    """Production-shaped read-only evidence hook; normal financial gates still run."""
    require(cutoff == CUTOFF, 'unreviewed_cutoff')
    evidence = json.loads(cash.read(root, {'file': REVIEW_FILE, 'sha256': REVIEW_SHA}))
    masters, tables, _ = current_tables(root, cutoff)
    requested = set(TARGETS)
    if identities is not None:
        requested = {r['ticker'] for r in identities if r['ticker'] in TARGETS}
        for identity in identities:
            if identity['ticker'] in requested:
                require((identity['cvm'], identity['share_class'], identity['isin']) == TARGETS[identity['ticker']],
                        'exact_instrument_identity_required')
    for source in evidence['sources']:
        cash.read(root, source)
    readings = []
    for original in evidence['readings']:
        ticker = original['ticker']
        if ticker not in requested:
            continue
        master = masters[ticker]
        require((original['cvm'], original['share_class'], original['isin']) == TARGETS[ticker]
                and (original['version'], original['filing_id'], original['published_at']) == EXPECTED[ticker]
                and original['fiscal_end'] == master['DT_REFER']
                and original['accounts_scope'] == 'consolidated'
                and original['dre_sha256'] == native._dre_fingerprint(tables[ticker]['DRE_con'], full=True),
                'review_scope_or_latest_filing_changed')
        readings.append({**original, 'manifest_sha256': REVIEW_SHA})
    require({r['ticker'] for r in readings} == requested, 'requested_reading_absent')
    return readings, evidence['sources']


def replay(root=ROOT, *, adapter=True):
    """Compare production with only its verified EPS reader suppressed, offline."""
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from . import round2_cash_eps as reading_module
    from .collect_market_data import financial_features
    from .market_inventory import load_inventory
    root = Path(root)
    inventory = load_inventory(root)
    stocks = [next(r for r in inventory['stocks'] if r['ticker'] == t) for t in TARGETS]
    def offline(*args, **kwargs):
        raise RuntimeError('round2_cash_eps_offline_network_forbidden')

    counterfactual = None
    with patch.object(socket.socket, 'connect', offline), patch.object(socket.socket, 'connect_ex', offline):
        if adapter:
            statements, diagnostics, sources = native.prepare_financials(root, stocks, CUTOFF)
        else:
            verified, _ = reading_module.get_cash_eps_readings(root, CUTOFF, stocks)
            require({r['ticker'] for r in verified} == set(TARGETS), 'counterfactual_proof_not_validated')
            # Patch the canonical module also when this file is executed with -m.
            with patch.object(reading_module, 'get_cash_eps_readings', return_value=([], [])) as suppressed:
                statements, diagnostics, sources = native.prepare_financials(root, stocks, CUTOFF)
            require(suppressed.call_count == 1, 'production_cash_eps_hook_not_exercised')
            counterfactual = {'proof_validated_before_suppression': True,
                'suppressed_reader': 'recommender.round2_cash_eps.get_cash_eps_readings',
                'production_reader_calls': suppressed.call_count,
                'review_file': REVIEW_FILE, 'review_sha256': REVIEW_SHA}
        results = []
        for stock in stocks:
            checks = {}
            candidate = candidates._one(stock, candidates._Cache(root, inventory), CUTOFF, checks)
            current = financial_features(statements, candidate['payments'], CUTOFF,
                (stock['cvm'], stock['share_class'], stock['sector_code']))
            results.append({'ticker': stock['ticker'], 'source_gate_pass': True,
                'checks': checks, 'ledger_rows': len(candidate['ledger']),
                'current_financial_pass': current is not None and current[1]['fiscal_end'] >= candidate['financial_basis_start'],
                'current_financial': current, 'financial_basis_start': candidate['financial_basis_start'],
                'latest_records': [r for r in statements if r['cvm'] == stock['cvm']
                    and r['share_class'] == stock['share_class'] and r['fiscal_end'] == '2025-12-31'],
                'financial_diagnostics': [d for d in diagnostics if d.get('cvm') == stock['cvm']
                    and d.get('fiscal_end') == '2025-12-31']})
    return {'targets': results, 'financial_sources': sources, 'network_calls': 0,
        'normal_gates_modified': False, 'cash_hooks_modified': False, 'adapter': adapter,
        'production_research_unmodified': True, 'counterfactual': counterfactual,
        'financial_hooks_overridden': [] if adapter else ['get_cash_eps_readings']}


def acquired_source(root, url):
    sources = [s for s in cash.receipts(str(root)) if s['url'] == url]
    require(len(sources) == 1, 'exact_acquisition_receipt_required')
    source, = sources
    cash.read(root, source)
    return source


def unpack_native(root, ticker, source):
    """Preserve exact original members, never rewrite a PDF or XML."""
    body = cash.read(root, source)
    if body.startswith(b'%PDF'):
        return source, None
    require(body.startswith(b'PK'), 'native_document_neither_pdf_nor_package')
    output = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        require(archive.testzip() is None, 'invalid_native_container')
        for member in archive.namelist():
            suffix = Path(member).suffix.lower()
            cvm = TARGETS[ticker][0]
            version, filing, _ = EXPECTED[ticker]
            if not (re.fullmatch(f'{filing}_{cvm:06d}_' + r'\d+\.pdf', member)
                    or member == f'{cvm:06d}DFP31-12-2025v{version}.xml'):
                continue
            require(suffix not in output, 'ambiguous_native_member')
            data = archive.read(member)
            digest = hashlib.sha256(data).hexdigest()
            relative = BASE + '/cash-eps-native-' + ticker + '-' + digest + suffix
            _save(Path(root) / relative, data)
            output[suffix] = {**cash.public_source(source), 'file': relative, 'sha256': digest,
                'bytes': len(data), 'original_container': cash.public_source(source),
                'original_zip_member': member}
    require('.pdf' in output and '.xml' in output, 'native_pdf_or_xml_missing')
    return output['.pdf'], output['.xml']


def inspect_documents(root=ROOT):
    from pypdf import PdfReader
    sources = {
        'ALLD3': 'https://api.mziq.com/mzfilemanager/v2/d/cbfa0f13-b657-4ace-bf5e-810e7bfc1e12/2d72223d-c656-0d18-3707-06b880680e49?origin=2',
        'JSLG3': 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=155651',
    }
    masters, tables, annual = current_tables(root)
    result = {}
    for ticker, url in sources.items():
        source = acquired_source(root, url)
        pdf, xml = unpack_native(root, ticker, source)
        reader = PdfReader(io.BytesIO(cash.read(root, pdf)))
        pages = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            normal = cash.norm(text)
            if re.search(r'lucro.{0,18}por|resultado.{0,18}por|3\.99\.01|3\.11\.01|weighted|basico', normal):
                pages.append({'pdf_page': number, 'text': text})
        result[ticker] = {'master': masters[ticker], 'annual_source': annual,
                          'pdf_source': pdf, 'xml_source': xml, 'page_count': len(reader.pages),
                          'native_DRE_con': tables[ticker]['DRE_con'],
                          'native_DRE_ind': tables[ticker]['DRE_ind'], 'semantic_pages': pages}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'freeze', 'readings', 'replay', 'baseline'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'freeze':
        result = {'review_file': REVIEW_FILE, 'sha256': freeze(args.root)}
    elif args.command in ('replay', 'baseline'):
        result = replay(args.root, adapter=args.command == 'replay')
    elif args.command == 'readings':
        readings, sources = get_cash_eps_readings(args.root, CUTOFF)
        result = {'readings': readings, 'sources': sources}
    else:
        result = inspect_documents(args.root)
    cash.save_report(args.output, result)
    if 'targets' in result:
        print(json.dumps([{k: r[k] for k in ('ticker', 'source_gate_pass', 'current_financial_pass')}
                          for r in result['targets']]), flush=True)
    elif args.command == 'inspect':
        print(json.dumps({t: {'pages': r['page_count'], 'semantic_pages': [p['pdf_page'] for p in r['semantic_pages']],
                             'pdf_sha256': r['pdf_source']['sha256']} for t, r in result.items()}), flush=True)
    else:
        print(json.dumps(result if args.command == 'freeze' else
                         {'readings': [r['ticker'] for r in result['readings']]}), flush=True)


if __name__ == '__main__':
    main()
