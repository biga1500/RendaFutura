"""Exact-current Dexxos consolidated EPS note, not a zero-root fallback.

The native 155652 v1 package and the separately published audited annual PDF
both contain note 2.5. Printed class numerators, weighted shares, EPS and the
consolidated DRE are checked together. Acquisition never writes shared core.
"""

import argparse
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader

from . import inventory_financials as native
from . import round2_share_actions as primary
from . import round2_dexp_prices as prices
from .bulk_pending_sources import PublicSourceCollector, _save


ROOT, CUTOFF, BASE = primary.ROOT, primary.CUTOFF, primary.CACHE
ANNUAL = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
INDEX = 'raw/primary-research/cvm-documents/index-2026-d2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip'
INDEX_SHA = 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c'
DRE_SHA = '015cc9127ab178c0eaadb52d2608fd95a729479178c193f8193b8fdc33b83ae1'
REVIEW_SHA = '1dd7d2e9304728c61df243c631fca0ede69e7f5afae7c121c43d0f09eaa87291'
IDENTITIES = {
    'DEXP3': {'ticker': 'DEXP3', 'cvm': 16632, 'share_class': 'ON', 'isin': 'BRDEXPACNOR1'},
    'DEXP4': {'ticker': 'DEXP4', 'cvm': 16632, 'share_class': 'PNA', 'isin': 'BRDEXPACNPR8'},
}
PACKAGE_URL = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?'
               'CodigoInstituicao=1&NumeroSequencialDocumento=155652')
AUDIT_URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&'
             'CodigoInstituicao=1&numProtocolo=1494745&numSequencia=1019451&numVersao=1')
PINS = {
    'package': ('24996eb39cb5b6e5b116816c6530469d3038dec2894d2c3f7214b0537b2a2c76',
                '8a54bab570d20aab0faa3b39390c22a52e808811f4f0d5890b74af3a1a979063',
                'e2d672f2e67daff0635ebba834898c9a974d1bd1c1c8e6981ce9c2255c1229f6'),
    'audit': ('46de939a237f20ffb3dadb2e016fab4795737554613d22f2365b60493e71a077',
              '64d0c0bc9a8d02ad0315936a3c6c13b8572acf2e4db6147e974ba5f5c8e30721',
              'c5192255ba5070181bc5c9c71a29930eb6a7626ed90f915348f372cf755e3d46'),
}
NATIVE_MEMBERS = ('016632DFP31-12-2025v1.xml', '155652_016632_25102026220913.pdf')


def require(value, reason):
    if not value:
        raise ValueError('dexp_eps_' + reason)


def current_tables(folder=ROOT, cutoff=CUTOFF):
    require(type(cutoff) is date and cutoff == CUTOFF, 'exact_cutoff_required')
    body = primary.read(folder, ANNUAL, ANNUAL_SHA)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        masters = list(native._rows(archive, 'dfp_cia_aberta_2025.csv'))
        rows = [r for r in masters if int(r['CD_CVM']) == 16632
                and r['DT_REFER'] <= cutoff.isoformat() and r['DT_RECEB'] <= cutoff.isoformat()]
        require(rows, 'current_filing_missing')
        master = max(rows, key=lambda r: (r['DT_REFER'], int(r['VERSAO']), r['DT_RECEB']))
        require(tuple(master[k] for k in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA')) ==
                ('2025-12-31', '1', '155652', '2026-03-24', '02.193.750/0001-52'),
                'latest_filing_changed_no_older_fallback')
        require(master['LINK_DOC'].replace('http://', 'https://', 1) == PACKAGE_URL,
                'master_download_link_changed')
        key = native._key(master)
        member = 'dfp_cia_aberta_DRE_con_2025.csv'
        dre = [r for r in native._rows(archive, member)
               if native._key(r) == key and r['ORDEM_EXERC'] == '\u00daLTIMO']
    require(native._dre_fingerprint(dre, full=True) == DRE_SHA, 'full_consolidated_DRE_changed')
    annual = {'file': ANNUAL, 'sha256': ANNUAL_SHA, 'bytes': len(body),
        'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip',
        'retrieved_at': None, 'retrieval_not_inferred_from_mtime': True}
    return master, dre, annual


def transport(folder, kind):
    digest, request, receipt_digest = PINS[kind]
    relative = f'{BASE}/response-{request}.json'
    receipt = json.loads(primary.read(folder, relative, receipt_digest))
    expected_url = PACKAGE_URL if kind == 'package' else AUDIT_URL
    require(receipt['url'] == expected_url and receipt['sha256'] == digest,
            'pinned_transport_identity_changed')
    ref = {k: receipt[k] for k in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}
    ref.update(receipt_file=relative, receipt_sha256=receipt_digest,
               authority='primary_CVM', published_at='2026-03-24')
    return primary.checked_source(folder, ref), ref


def package_members(folder=ROOT, *, extract=False):
    """Preserve the exact original members, not a rewritten PDF/XML."""
    body, container = transport(folder, 'package')
    result = {}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        require(archive.testzip() is None, 'native_package_CRC_failure')
        require(all(name in archive.namelist() for name in NATIVE_MEMBERS), 'native_package_member_missing')
        require(sum(i.file_size for i in archive.infolist()) < 25_000_000,
                'native_package_uncompressed_size_limit')
        for name in NATIVE_MEMBERS:
            content = archive.read(name)
            digest, suffix = primary.sha(content), Path(name).suffix
            relative = f'{BASE}/dexp-eps-native-{digest}{suffix}'
            if extract:
                _save(Path(folder) / relative, content)
            require(primary.read(folder, relative, digest) == content, 'native_member_differs_from_container')
            result[suffix[1:]] = {'file': relative, 'sha256': digest, 'bytes': len(content),
                'url': container['url'], 'retrieved_at': container['retrieved_at'],
                'published_at': '2026-03-24', 'original_container': container,
                'original_zip_member': name, 'authority': 'primary_CVM_native_package_member'}
    xml = ET.fromstring(primary.read(folder, result['xml']['file'], result['xml']['sha256']))
    require(tuple(xml.findtext(field) for field in ('DadosEmpresa/CodigoCvm', 'DadosEmpresa/CnpjEmpresa',
            'Documento/VersaoDocumento', 'DadosDFP/DataReferencia')) ==
            ('016632', '02193750000152', '1', '31/12/2025'), 'native_XML_exact_filing_identity_changed')
    return result, container


def index_binding(folder):
    body = primary.read(folder, INDEX, INDEX_SHA)
    member = 'ipe_cia_aberta_2026.csv'
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        rows = list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
    matches = [(line, r) for line, r in enumerate(rows, 2) if r['Link_Download'] == AUDIT_URL]
    require(len(matches) == 1, 'audited_annual_index_row_missing_or_ambiguous')
    line, row = matches[0]
    require((row['Codigo_CVM'], row['CNPJ_Companhia'], row['Data_Referencia'], row['Data_Entrega'],
             row['Versao'], row['Tipo']) == ('16632', '02.193.750/0001-52', '2025-12-31',
                                            '2026-03-24', '1', 'Demonstra\u00e7\u00f5es Financeiras Anuais Completas'),
            'audited_annual_index_identity_changed')
    keys = ('CNPJ_Companhia', 'Data_Referencia', 'Categoria', 'Tipo', 'Especie', 'Assunto')
    require(max(int(r['Versao']) for r in rows if all(r[k] == row[k] for k in keys)) == 1,
            'audited_annual_not_final_published_index_version')
    return {'source': {'file': INDEX, 'sha256': INDEX_SHA, 'bytes': len(body)},
            'member': member, 'csv_line': line, 'native_row': row}


def page_readings(folder, reference, fragments_by_page):
    reader = PdfReader(io.BytesIO(primary.read(folder, reference['file'], reference['sha256'])))
    readings = []
    for number, fragments in fragments_by_page.items():
        text = reader.pages[number - 1].extract_text()
        for fragment in fragments:
            readings.append(primary.clause(text, fragment, reference, number))
    return readings


NOTE_TABLE = (
    'Numerador basico e diluido - lucro atribuivel aos acionistas atribuido igualmente entre as classes de acoes',
    'Ordinarias 136.449 136.564', 'Preferenciais 7.204 7.632', '143.653 144.196',
    'Denominador basico e diluido - media ponderada da quantidade de acoes',
    'Ordinarias 102.602.353 96.781.473', 'Preferenciais 5.416.933 5.408.795',
    '108.019.286 102.190.268', 'Lucro basico e diluido por acao (R$ por acao)',
    'Ordinarias 1,3299 1,4111', 'Preferenciais 1,3299 1,4111',
)


def build_proof(folder=ROOT):
    root = Path(folder).resolve()
    master, dre, annual = current_tables(root)
    members, container = package_members(root)
    audit_body, audit = transport(root, 'audit')
    require(audit_body.startswith(b'%PDF'), 'audited_annual_native_PDF_required')
    index = index_binding(root)
    native_spans = page_readings(root, members['pdf'], {
        18: ('DFs Consolidadas / Demonstracao do Resultado',
             '3.11.01 Atribuido a Socios da Empresa Controladora 143.653 144.196 180.791',
             '3.11.02 Atribuido a Socios Nao Controladores 0 9.003 30.495'),
        63: ('2.5 Lucro por acao basico e diluido',
             'lucro do exercicio atribuido aos acionistas da Companhia pela media ponderada',
             'Os lucros apurados, basico e diluido, apresentam o mesmo valor por acao'),
        64: NOTE_TABLE,
    })
    audit_spans = page_readings(root, audit, {
        30: ('Controladora Consolidado',
             'Acionistas controladores 143.653 144.196 143.653 144.196',
             'Lucro basico e diluido do exercicio por acao - R$ 2.5 1,3299 1,4111 1,3299 1,4111',
             'Quantidade media de acoes ao final do exercicio (media ponderada) 108.019.286 102.190.268'),
        60: ('Notas explicativas as demonstracoes contabeis individuais e consolidadas',
             'Em milhares de reais', '2.5 Lucro por acao basico e diluido'),
        61: ('Notas explicativas as demonstracoes contabeis individuais e consolidadas',
             'Em milhares de reais', *NOTE_TABLE),
    })
    from .preferred_class_binding import class_binding
    binding = class_binding(root, IDENTITIES['DEXP4'], CUTOFF)
    data = primary.packet(root)
    identities = {ticker: primary.exact_identity(root, identity, data) for ticker, identity in IDENTITIES.items()}
    cells = {r['CD_CONTA']: r for r in dre}
    require(native._brl(cells['3.11.01']) == Decimal('143653000')
            and all(Decimal(cells[c]['VL_CONTA']) == 0 for c in ('3.99', '3.99.01', '3.99.02')),
            'reported_consolidated_profit_or_placeholder_changed')
    reading = {'cvm': 16632, 'tickers': list(IDENTITIES), 'fiscal_end': '2025-12-31',
        'version': '1', 'filing_id': '155652', 'published_at': '2026-03-24',
        'accounts_scope': 'consolidated', 'full_dre': True, 'dre_sha256': DRE_SHA,
        'parent_profit_brl': '143653000',
        'profit_allocations_brl': {'ON': '136449000', 'PN': '7204000'},
        'primary_evidence': {'source_ref': 'primary-financials:' + audit['sha256'], 'pdf_page': 61,
            'note': '2.5, audited individual AND consolidated notes; audited DRE p30 explicitly prints consolidated EPS; exact native 155652 v1 note pp63-64'},
        'basic_eps': [
            {'account': 'note:2.5_basic_ON', 'share_class': 'ON', 'value': '1.3299',
             'allocation': 'ON', 'numerator_brl': '136449000', 'weighted_shares': '102602353',
             'numerator_rounding_brl': '500', 'shares_rounding': '0.5'},
            {'account': 'note:2.5_basic_PN', 'share_class': 'PN', 'value': '1.3299',
             'allocation': 'PN', 'numerator_brl': '7204000', 'weighted_shares': '5416933',
             'numerator_rounding_brl': '500', 'shares_rounding': '0.5'},
        ]}
    native._checked_note_eps(dre, reading)
    sources = [annual, container, *members.values(), audit, index['source'],
               data['targets']['DEXP3']['detail'], *data['fca'], *binding['sources']]
    unique = {}
    for ref in sources:
        primary.read(root, ref['file'], ref['sha256'])
        unique[ref['file']] = {**ref, 'id': 'primary-financials:' + ref['sha256'],
            'use': 'exact_current_account_interpretation', 'authority': 'primary',
            'point_in_time_archive_verified': False}
    return {'schema_version': 1, 'cutoff': CUTOFF.isoformat(), 'identities': IDENTITIES,
        'latest_master': master, 'native_package': container, 'native_members': members,
        'audited_annual_index': index, 'readings': [reading], 'sources': list(unique.values()),
        'native_literal_readings': native_spans, 'audited_literal_readings': audit_spans,
        'exact_security_primary_history': identities, 'preferred_class_binding': binding,
        'reported_EPS': {'ON': '1.3299', 'PN': '1.3299'},
        'printed_note_weighted_share_total': '108019286',
        'weighted_share_source': 'note_2.5_individual_AND_consolidated_scope_header_audited_page61_native_page64',
        'DRE_controladora_only_weighted_share_row_used_for_consolidated_denominator': False,
        'numerator_class_allocations_sum_to_consolidated_parent_profit': True,
        'DRE_scope_selected': 'consolidated', 'individual_DRE_used_as_consolidated': False,
        'CSV_zero_roots_are_not_reported_note_EPS': True,
        'reported_EPS_copied_not_computed': True, 'weights_copied_not_reconstructed': True,
        'price_factors_or_market_prices_used_to_derive_EPS': False,
        'old_filing_or_revision_fallback': False, 'gates_relaxed': False,
        'publication_date_basis': 'actual_CVM_current_master_and_final_IPE_index_not_retrieval_time',
        'PIT_status': 'experimental_retrospective_evidence_not_certified',
        'point_in_time_archive_verified': False, 'model_admission': False}


def review_file():
    require(len(REVIEW_SHA) == 64, 'immutable_review_not_frozen')
    return f'{BASE}/dexp-eps-reviewed-{REVIEW_SHA}.json'


def freeze(folder=ROOT):
    proof = build_proof(folder)
    body = primary.encoded(proof)
    digest = primary.sha(body)
    relative = f'{BASE}/dexp-eps-reviewed-{digest}.json'
    _save(Path(folder) / relative, body)
    return relative, digest


def get_dexp_eps_readings(folder, cutoff, identities=None):
    """Read-only production evidence hook: one current issuer review, ON + PN.

    Core's existing primary PN-to-PNA bridge remains necessary for DEXP4.
    Returning one issuer review prevents same-filing overwrite by class.
    """
    require(type(cutoff) is date and cutoff == CUTOFF, 'exact_cutoff_required')
    requested = list(IDENTITIES.values()) if identities is None else [r for r in identities
                                                                   if r.get('ticker') in IDENTITIES]
    if not requested:
        return [], []
    require(len({r['ticker'] for r in requested}) == len(requested), 'duplicate_requested_security')
    for r in requested:
        require(type(r.get('cvm')) is int and all(r.get(k) == v for k, v in IDENTITIES[r['ticker']].items()),
                'exact_CVM_class_ISIN_required')
    saved = json.loads(primary.read(folder, review_file(), REVIEW_SHA))
    proof = build_proof(folder)
    require(saved == json.loads(primary.encoded(proof)), 'frozen_proof_differs_from_current_native_reading')
    readings = [{**r, 'manifest_file': review_file(), 'manifest_sha256': REVIEW_SHA}
                for r in proof['readings']]
    return readings, proof['sources']


def acquire(folder=ROOT):
    master, _, _ = current_tables(folder)
    index = index_binding(folder)
    collector = PublicSourceCollector(folder, 'round2-actions')
    cached = primary.receipt_index(folder)
    requests = [(PACKAGE_URL, {'kind': 'actual_current_DFP_master_LINK_DOC', 'native_master': master,
                              'annual_file': ANNUAL, 'annual_sha256': ANNUAL_SHA}),
                (AUDIT_URL, {'kind': 'actual_final_CVM_audited_annual_index', **index})]
    for url, discovery in requests:
        prior = primary.existing(folder, url, cached)
        receipt = prior or collector.fetch(url, discovery=discovery)
        require(receipt.get('status', receipt.get('http_status')) == 200 and not receipt.get('error'),
                'public_source_unavailable_no_retry')
        print(json.dumps({k: receipt.get(k) for k in ('url', 'status', 'error', 'sha256')}), flush=True)
    return package_members(folder, extract=True)


def replay(folder=ROOT, *, adapter=True):
    """Run production; the counterfactual suppresses only its verified EPS reader."""
    from . import inventory_candidates as candidates
    from . import round2_dexp_eps as reading_module
    from .market_inventory import load_inventory
    from .collect_market_data import financial_features, stock_observations
    root = Path(folder).resolve()
    inventory = load_inventory(root)
    stocks = [r for r in inventory['stocks'] if r['ticker'] in IDENTITIES]
    require({r['ticker'] for r in stocks} == set(IDENTITIES), 'inventory_security_missing')
    before = prices.fingerprints(root)
    original_prices, original_research, original_one = candidates.reconcile_prices, native._research, candidates._one
    original_getter, original_quote_loader = reading_module.get_dexp_eps_readings, prices.load_evidence
    candidate_rows, result, counterfactual = {}, {}, None

    def offline(*args, **kwargs):
        raise AssertionError('dexp_eps_replay_network_forbidden')

    with patch.object(socket.socket, 'connect', offline), patch.object(socket.socket, 'connect_ex', offline), \
         patch.object(socket, 'create_connection', offline), \
         patch.object(socket, 'getaddrinfo', offline):
        if adapter:
            statements, diagnostics, sources = native.prepare_financials(root, stocks, CUTOFF)
        else:
            verified, _ = reading_module.get_dexp_eps_readings(root, CUTOFF, stocks)
            require(len(verified) == 1 and verified[0]['manifest_sha256'] == REVIEW_SHA
                    and set(verified[0]['tickers']) == set(IDENTITIES), 'counterfactual_proof_not_validated')
            # The canonical module is also the core import target under python -m.
            with patch.object(reading_module, 'get_dexp_eps_readings', return_value=([], [])) as suppressed:
                statements, diagnostics, sources = native.prepare_financials(root, stocks, CUTOFF)
            require(suppressed.call_count == 1, 'production_dexp_eps_hook_not_exercised')
            counterfactual = {'proof_validated_before_suppression': True,
                'suppressed_reader': 'recommender.round2_dexp_eps.get_dexp_eps_readings',
                'production_reader_calls': suppressed.call_count,
                'review_file': review_file(), 'review_sha256': REVIEW_SHA}
        caches = {r['ticker']: candidates._Cache(root, inventory) for r in stocks}
        for row in stocks:
            ticker, checks = row['ticker'], {}
            try:
                candidate = candidates._one(row, caches[ticker], CUTOFF, checks)
                candidate_rows[ticker] = candidate
                result[ticker] = {'source_candidate': True, 'cash_matches': len(candidate['independent_cash_matches']),
                    'ledger_rows': len(candidate['ledger']), 'price_history_months': len(candidate['prices']),
                    'price_matches': len(candidate['independent_price_matches']),
                    'financial_basis_start': candidate['financial_basis_start'],
                    'training_since': candidate['training_since'], 'pending_reason': None}
            except (ValueError, KeyError, TypeError, IndexError) as error:
                result[ticker] = {'source_candidate': False, 'pending_reason': str(error)}
            result[ticker]['source_checks'] = checks
            result[ticker]['source_receipts'] = caches[ticker].all_receipts()
        for stock in stocks:
            ticker = stock['ticker']
            candidate = candidate_rows.get(ticker)
            spec = stock['cvm'], stock['share_class'], stock['sector_code']
            current = (financial_features(statements, candidate['payments'], CUTOFF, spec)
                       if candidate is not None else None)
            result[ticker]['same_current_financial_features'] = current
            result[ticker]['latest_2025_records'] = [r for r in statements
                if r['cvm'] == 16632 and r['share_class'] == stock['share_class'] and r['fiscal_end'] == '2025-12-31']
            result[ticker]['financial_statement_current_pass'] = any(
                r['status'] == 'included' and r.get('profit_per_share') == 1.3299
                and r.get('accounts_scope') == 'consolidated' and r.get('filing_id') == '155652'
                for r in result[ticker]['latest_2025_records'])
            result[ticker]['financial_current_pass'] = (
                current is not None and current[1]['fiscal_end'] >= candidate['financial_basis_start']
                if candidate is not None else None)
            result[ticker]['financial_features_called'] = candidate is not None
            result[ticker]['current_financial_call_used_candidate_cash'] = candidate is not None
            if candidate:
                observations, coverage = stock_observations(ticker, candidate['prices'], candidate['payments'],
                                                            statements, CUTOFF, spec=spec)
                admitted = [r for r in observations if date.fromisoformat(r['as_of']) >= candidate['training_since']
                            and r['fiscal_end'] >= candidate['financial_basis_start']]
                result[ticker]['training_rows_after_normal_gates'] = len(admitted)
                result[ticker]['historical_training_limit'] = (
                    'Current 2025 note only; publication 2026-03-24 is after latest usable 12-month-label '
                    'as-of 2025-09-30. No historical EPS reconstructed and no forward label fabricated.'
                    if not admitted else None)
                result[ticker]['coverage'] = coverage
                result[ticker]['source_and_current_financial_pass'] = result[ticker]['financial_current_pass']
                if not result[ticker]['financial_current_pass']:
                    result[ticker]['pending_reason'] = 'ordinary_current_financial_unavailable'
            else:
                result[ticker]['financial_reading_only_not_candidate_admission'] = True
                result[ticker]['source_and_current_financial_pass'] = False
    require(candidates._one is original_one and candidates.reconcile_prices is original_prices
            and native._research is original_research and prices.load_evidence is original_quote_loader
            and reading_module.get_dexp_eps_readings is original_getter, 'production_functions_changed')
    require(prices.fingerprints(root) == before, 'original_source_or_pointer_changed')
    return primary.json_safe({'records': result, 'adapter': adapter, 'counterfactual': counterfactual,
        'financial_diagnostics_2025': [d for d in diagnostics if d.get('cvm') == 16632
                                     and d.get('fiscal_end') == '2025-12-31'],
        'financial_sources': sources,
        'normal_one_called': True, 'normal_prepare_financials_called': True,
        'normal_financial_features_called': True, 'cash_functions_patched': False,
        'quote_dispatch': 'actual_production_core_no_quote_or_price_gate_mocks',
        'financial_dispatch': 'actual_production_research_no_research_mock',
        'production_research_unmodified': True, 'production_price_hooks_unmodified': True,
        'financial_hooks_overridden': [] if adapter else ['get_dexp_eps_readings'],
        'gates_relaxed': False, 'EPS_imputed': False, 'network_requests': 0,
        'unknown_candidate_cash_replaced_with_zero': False,
        'protected_fingerprints_before': before, 'protected_fingerprints_after': prices.fingerprints(root),
        'core_files_edited': False, 'original_caches_modified': False,
        'point_in_time_archive_verified': False, 'model_publication_performed': False})


def ledger_comparison(before, after):
    """Keep documentary changes explicit and separate from every ledger input."""
    def inputs(row):
        return {k: v for k, v in row.items() if k != 'primary_evidence'}

    order = lambda row: primary.encoded(inputs(row))
    old, new = sorted(before, key=order), sorted(after, key=order)
    equal_inputs = [inputs(r) for r in old] == [inputs(r) for r in new]
    changes = []
    if equal_inputs:
        for left, right in zip(old, new):
            if left != right:
                changes.append({'ticker': left['ticker'], 'record_date': left['record_date'],
                    'payment_date': left['payment_date'], 'source_row': left['source_row'],
                    'changed_fields': ['primary_evidence'], 'all_other_ledger_fields_equal': True,
                    'before_primary_evidence': left.get('primary_evidence'),
                    'after_primary_evidence': right.get('primary_evidence')})
    return {'ledger_inputs_equal': equal_inputs,
        'ledger_equal': sorted(before, key=primary.encoded) == sorted(after, key=primary.encoded),
        'ledger_before_sha256': primary.sha(primary.encoded(sorted(before, key=primary.encoded))),
        'ledger_after_sha256': primary.sha(primary.encoded(sorted(after, key=primary.encoded))),
        'ledger_evidence_only_changes': changes}


def production_parity(folder=ROOT, snapshot=None):
    """Rebuild the published 112 numerical inputs in memory, never a snapshot."""
    from .inventory_candidates import build_candidates
    from .market_inventory import load_inventory
    from .collect_income_data import PAYMENT_TARGET, payment_in_window
    from .collect_market_data import financial_features, shift_month, stock_observations
    from .current_quotes import stock_quote
    from .current_quote_sources import load_snapshot
    from .unit_financials import prepare_unit_financials
    root = Path(folder).resolve()
    base = (root / 'snapshots/universe-20261026T010202-74f8d227' if snapshot is None
            else Path(snapshot).resolve())
    require(base.is_relative_to(root / 'snapshots') and base.is_dir(), 'published_parity_snapshot_required')
    files = {name: primary.sha((base / name).read_bytes()) for name in (
        'assets.json', 'returns.csv', 'cashflows.csv', 'observations.csv', 'payment-ledger.json')}
    metadata = json.loads((base / 'assets.json').read_bytes())
    assets = {r['ticker']: r for r in metadata['assets'] if r['asset_class'] == 'acoes'}
    require(len(assets) == 112 and not set(assets).intersection(IDENTITIES)
            and metadata['reference_date'] == CUTOFF.isoformat(), 'exact_published_112_baseline_required')
    inventory = load_inventory(root)
    stocks = [r for r in inventory['stocks'] if r['ticker'] in assets]
    require(len(stocks) == len(assets), 'published_parity_inventory_identity_missing')
    tables = {}
    for name in ('returns.csv', 'cashflows.csv', 'observations.csv'):
        with (base / name).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            tables[name] = (reader.fieldnames, list(reader))
    old_ledger = json.loads((base / 'payment-ledger.json').read_bytes())
    require(len(tables['returns.csv'][1]) == len(tables['cashflows.csv'][1]) == 60,
            'all_60_return_cash_months_required')
    before = prices.fingerprints(root)

    def offline(*args, **kwargs):
        raise AssertionError('dexp_eps_parity_network_forbidden')

    results = []
    with patch.object(socket.socket, 'connect', offline), patch.object(socket.socket, 'connect_ex', offline), \
         patch.object(socket, 'create_connection', offline), patch.object(socket, 'getaddrinfo', offline):
        candidates, pending, _ = build_candidates(root, {**inventory, 'stocks': stocks}, CUTOFF)
        require(not pending and len(candidates) == 112, 'published_112_source_regression')
        statements, _, _ = native.prepare_financials(root, stocks, CUTOFF)
        units = [r for r in candidates if r['share_class'] == 'UNIT']
        if units:
            unit_statements, _, _ = prepare_unit_financials(root, units, CUTOFF)
            statements.extend(unit_statements)
        for candidate in sorted(candidates, key=lambda r: r['ticker']):
            ticker = candidate['ticker']
            current = financial_features(statements, candidate['payments'], CUTOFF,
                (candidate['cvm'], candidate['share_class'], candidate['sector_code']))
            basis = date.fromisoformat(candidate['financial_basis_start'])
            require(current is not None and date.fromisoformat(current[1]['fiscal_end']) >= basis,
                    'published_112_financial_regression_' + ticker)
            training, _ = stock_observations(ticker, candidate['prices'], candidate['payments'], statements,
                CUTOFF, spec=(candidate['cvm'], candidate['share_class'], candidate['sector_code']))
            training = [r | {'forecast_target_id': PAYMENT_TARGET['id']} for r in training
                        if date.fromisoformat(r['as_of']) >= candidate['training_since']
                        and date.fromisoformat(r['fiscal_end']) >= basis]
            fields, old_observations = tables['observations.csv']
            old_training = [r for r in old_observations if r['ticker'] == ticker]
            new_training = [{k: '' if r.get(k) is None else str(r[k]) for k in fields} for r in training]
            monthly = defaultdict(float)
            for day, _, value in candidate['payments']:
                monthly[day.isoformat()[:7]] += value
            returns_equal = cash_equal = True
            for total, cash in zip(tables['returns.csv'][1], tables['cashflows.csv'][1]):
                require(total['date'] == cash['date'], 'baseline_return_cash_dates_disagree')
                day = date.fromisoformat(total['date'])
                start, end = candidate['prices'][shift_month(day, -1)][2], candidate['prices'][day][2]
                paid = monthly[day.isoformat()[:7]] / start
                returns_equal &= float(total[ticker]) == end / start - 1 + paid
                cash_equal &= float(cash[ticker]) == paid
            first = date.fromisoformat(tables['returns.csv'][1][0]['date'])
            ledger = [r for r in candidate['ledger']
                      if payment_in_window(date.fromisoformat(r['payment_date']), first, CUTOFF)]
            content = (root / candidate.get('current_quote_source_file',
                candidate.get('price_source_file', 'raw/expanded/' + ticker + '-prices.json'))).read_bytes()
            quote = stock_quote(content, ticker, CUTOFF)
            if not quote['purchase_available']:
                fallback, _ = load_snapshot(root, ticker, CUTOFF, content)
                if fallback and fallback['purchase_available']:
                    quote = fallback
            order = lambda r: primary.encoded(r)
            ledger_audit = ledger_comparison([r for r in old_ledger if r['ticker'] == ticker], ledger)
            checks = {'price_equal': assets[ticker]['price'] == candidate['prices'][CUTOFF][2],
                'features_equal': assets[ticker]['features'] == current[0],
                'current_quote_equal': assets[ticker]['current_quote'] == quote,
                'monthly_returns_equal': returns_equal, 'monthly_cash_equal': cash_equal,
                'training_equal': sorted(new_training, key=order) == sorted(old_training, key=order),
                'ledger_inputs_equal': ledger_audit['ledger_inputs_equal']}
            results.append({'ticker': ticker, **checks, **ledger_audit, 'training_rows': len(training),
                'ledger_rows': len(ledger), 'numerical_parity': all(checks.values()),
                'training_before_sha256': primary.sha(primary.encoded(sorted(old_training, key=order))),
                'training_after_sha256': primary.sha(primary.encoded(sorted(new_training, key=order)))})
    after_files = {name: primary.sha((base / name).read_bytes()) for name in files}
    require(files == after_files and before == prices.fingerprints(root), 'published_parity_original_mutated')
    return {'baseline_snapshot': str(base), 'baseline_stock_count': 112, 'results': results,
        'all_112_numerical_inputs_equal': all(r['numerical_parity'] for r in results),
        'all_112_ledgers_exact_equal': all(r['ledger_equal'] for r in results),
        'ledger_comparison_policy': 'All ledger fields compared; primary_evidence differences retained separately, not model inputs.',
        'baseline_files_before': files, 'baseline_files_after': after_files,
        'protected_fingerprints_before': before, 'protected_fingerprints_after': prices.fingerprints(root),
        'snapshot_written': False, 'pointer_written': False, 'network_requests': 0,
        'production_price_hooks_unmodified': True, 'production_research_unmodified': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--replay', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--parity', action='store_true')
    args = parser.parse_args(argv)
    if args.acquire:
        print(primary.encoded(primary.json_safe(acquire())).decode())
    if args.freeze:
        print(json.dumps({'frozen': freeze()}))
    if args.report:
        require(args.report.name.startswith('actions-dexp-eps-') and args.report.suffix == '.json',
                'report_outside_assigned_prefix')
        proof = build_proof()
        readings, sources = get_dexp_eps_readings(ROOT, CUTOFF)
        result = {'proof': proof, 'loader_readings': readings, 'loader_sources': sources,
            'baseline': replay(adapter=False) if args.baseline else None,
            'replay': replay() if args.replay else None,
            'published_112_parity': production_parity() if args.parity else None,
            'producer_sha256': primary.sha(Path(__file__).read_bytes()), 'model_admission': False}
        args.report.write_bytes(primary.encoded(result))
        print(json.dumps({'report': str(args.report), 'EPS': proof['reported_EPS'],
            'replay': {t:{k:r.get(k) for k in ('source_candidate', 'financial_current_pass', 'pending_reason',
                                              'training_rows_after_normal_gates')}
                       for t,r in result['replay']['records'].items()} if result['replay'] else None}))


if __name__ == '__main__':
    main()
