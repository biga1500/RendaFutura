"""Exact-current WHRL4/EVEN3 primary research, without shared-file writes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import socket
from urllib.parse import parse_qs, urlsplit
import zipfile

from . import inventory_financials as native
from . import round2_cash as cash
from .bulk_pending_sources import PublicSourceCollector, approved
from .round2_cash_eps import ANNUAL_FILE, ANNUAL_SHA, _pdf_pages


ROOT, CUTOFF, BASE = cash.ROOT, cash.CUTOFF, cash.CACHE
REPORTS = Path(__file__).resolve().parents[2] / 'work/recovery_round2_20261025'
TARGETS = {'WHRL4': (14346, 'PN', 'BRWHRLACNPR2', 4),
           'EVEN3': (20524, 'ON', 'BREVENACNOR8', 4)}
EXPECTED = {'WHRL4': ('1', '155763', '2026-03-27'), 'EVEN3': ('1', '155582', '2026-03-23')}
CACHED = {
    'WHRL4': {'file': 'raw/primary-research/financials/whrl-dfp-2025-5289c2af707ad96eff2e4b9c398a5ee2d84877328a6e37476621781b540839c2.pdf',
        'sha256': '5289c2af707ad96eff2e4b9c398a5ee2d84877328a6e37476621781b540839c2',
        'url': 'https://downloads.ctfassets.net/qfxflpv0atz9/4NgFdMkPxALBMYzeYZIkmz/07e1faf19dc2e56359d52083c0e9eddd/DFP_Whirlpool_S.A._2025__1_.pdf'},
    'EVEN3': {'file': 'raw/primary-research/financials/even-dfp-2025-fe3ba3afe073ba758c75ebec3900690481ee4157250b823f4f03b7fe5021853f.pdf',
        'sha256': 'fe3ba3afe073ba758c75ebec3900690481ee4157250b823f4f03b7fe5021853f',
        'url': 'https://api.mziq.com/mzfilemanager/v2/d/f928111c-c68e-45c9-bfc3-5d678e52e7ad/be924d29-ee97-a7dc-e757-3c691495dd13?origin=2'},
}
IPE_SOURCE = {'file': 'raw/primary-research/cvm-documents/index-2026-d2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c.zip',
    'sha256': 'd2cd1be0b0db8be3cf7a51985649dce142c34ed96b4bc38296daa9721354780c',
    'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2026.zip'}
DOCUMENTS = {
    'WHRL4-latest-proposal': ('1028977', '1', '2026-04-13',
        'a4c0b8502bb71301e506f44be8119f78a96ce6dda71b4f2a72b326a838103efe'),
    'EVEN3-annual': ('1018497', '1', '2026-03-23',
        'f4f900574d2a7551e674b084a9ab395d68b0394ad37b0ca1d93f78456520f204'),
    'EVEN3-release': ('1018508', '1', '2026-03-23',
        'a8f6074c4ff44af1ce52d9fc3e1d756473c513938d32f7bd5f03ab5821d61185'),
}


def require(condition, reason):
    if not condition:
        raise ValueError('round2_industrial_' + reason)


def report(path, value):
    require(Path(path).name.startswith('cash-industrial-'), 'report_outside_owned_prefix')
    cash.save_report(path, value)


def current_tables(root=ROOT, cutoff=CUTOFF):
    require(cutoff == CUTOFF, 'unreviewed_cutoff')
    source = {'file': ANNUAL_FILE, 'sha256': ANNUAL_SHA,
        'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_2025.zip'}
    masters, tables = {}, {}
    with zipfile.ZipFile(io.BytesIO(cash.read(root, source))) as archive:
        filings = list(native._rows(archive, 'dfp_cia_aberta_2025.csv'))
        for ticker, (cvm, _, _, _) in TARGETS.items():
            eligible = [r for r in filings if int(r['CD_CVM']) == cvm
                        and r['DT_REFER'] <= str(cutoff) and r['DT_RECEB'] <= str(cutoff)]
            require(eligible, 'current_master_absent')
            master = max(eligible, key=lambda r: (r['DT_REFER'], int(r['VERSAO']), r['DT_RECEB']))
            require(master['DT_REFER'] == '2025-12-31' and tuple(master[k] for k in (
                'VERSAO', 'ID_DOC', 'DT_RECEB')) == EXPECTED[ticker], 'latest_filing_changed_no_fallback')
            masters[ticker], tables[ticker] = master, {}
            for scope in ('con', 'ind'):
                for table in ('DRE', 'BPA', 'BPP', 'DFC_MI', 'DFC_MD'):
                    member = f'dfp_cia_aberta_{table}_{scope}_2025.csv'
                    if member in archive.namelist():
                        tables[ticker][table + '_' + scope] = [r for r in native._rows(archive, member)
                            if native._key(r) == native._key(master) and r['ORDEM_EXERC'] == '\u00daLTIMO']
    return masters, tables, source


def prior_attempts(root):
    """Do not retry stopped exact filing downloads from the previous round."""
    attempts = {}
    for path in (Path(root) / 'raw/primary-research').rglob('request-*.json'):
        if path.stat().st_size > 2_000_000:
            continue
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get('url'):
            attempts[value['url']] = str(path.relative_to(root)).replace('\\', '/')
    previous = REPORTS / 'financial-handoff.json'
    if previous.is_file():
        for value in json.loads(previous.read_bytes()).get('network', {}).get('responses', []):
            if value.get('error'):
                attempts[value['url']] = str(previous)
    return attempts


def acquire(url, discovery, root=ROOT, collector=None):
    host = (urlsplit(url).hostname or '').lower()
    require(approved(url) and host != 'api.mziq.com' and not host.endswith('.api.mziq.com'),
            'denied_or_globally_stopped_origin')
    cached = [s for s in cash.receipts(str(root)) if s['url'] == url]
    if cached:
        cash.read(root, cached[0])
        return {**cash.public_source(cached[0]), 'new_request': False}
    require(url not in prior_attempts(root), 'previous_exact_request_not_retried')
    collector = collector or PublicSourceCollector(root, 'round2-cash')
    result = collector.fetch(url, discovery=discovery)
    cash.receipts.cache_clear()
    return result


def issuer_catalogue(root, source):
    """Read published Next flight data as JSON; never execute site scripts."""
    from lxml import html
    tree = html.fromstring(cash.read(root, source))
    chunks = []
    for script in tree.xpath('//script[not(@src)]/text()'):
        match = re.fullmatch(r'self\.__next_f\.push\((.*)\);?', script.strip(), re.S)
        if match:
            record = json.loads(match[1])
            if len(record) > 1 and record[0] == 1 and isinstance(record[1], str):
                chunks.append(record[1])
    found = []
    def visit(value):
        if isinstance(value, dict):
            if 'whpDownloads' in value:
                found.append(value['whpDownloads'])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for line in ''.join(chunks).splitlines():
        payload = line.partition(':')[2]
        if payload.startswith(('{', '[')):
            try:
                visit(json.loads(payload))
            except ValueError:
                continue
    require(len(found) == 1, 'issuer_catalogue_missing_or_ambiguous')
    result = []
    for year in found[0]:
        for category in ('resultados', 'assembleias', 'informacoesAcionistas'):
            for row in year.get(category, []):
                if isinstance(row, str):
                    require(row.startswith('$'), 'unexpected_catalogue_value')
                    result.append({'year': year['ano'], 'category': category,
                        'unresolved_flight_reference': row, 'not_a_document': True})
                    continue
                file = row['file']
                result.append({'year': year['ano'], 'category': category, 'title': row['title'],
                    'url': 'https:' + file['url'] if file['url'].startswith('//') else file['url'],
                    'declared_bytes': file.get('details', {}).get('size'),
                    'page_source': cash.public_source(source)})
    return result


def final_ipe_rows(root=ROOT):
    with zipfile.ZipFile(io.BytesIO(cash.read(root, IPE_SOURCE))) as archive:
        require(len(archive.namelist()) == 1, 'ipe_index_member_ambiguous')
        member = archive.namelist()[0]
        rows = list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
    latest = {}
    for row in rows:
        if (not row['Codigo_CVM'].isdigit() or int(row['Codigo_CVM']) not in {x[0] for x in TARGETS.values()}
                or row['Data_Entrega'][:10] > str(CUTOFF)):
            continue
        sequence = parse_qs(urlsplit(row['Link_Download']).query)['numSequencia'][0]
        if sequence not in latest or int(row['Versao']) > int(latest[sequence]['Versao']):
            latest[sequence] = {**row, 'sequence': sequence,
                'index_source': IPE_SOURCE, 'index_member': member}
    return list(latest.values())


def acquisition_plan(root=ROOT):
    rows = final_ipe_rows(root)
    def select(cvm, predicate):
        matches = [r for r in rows if int(r['Codigo_CVM']) == cvm and predicate(r)]
        require(matches, 'published_ipe_document_absent')
        return max(matches, key=lambda r: (r['Data_Entrega'], int(r['Versao']), int(r['sequence'])))
    return {
        'WHRL4-latest-proposal': select(14346, lambda r: r['Data_Referencia'] == '2026-04-27'
            and cash.norm(r['Especie']) == 'proposta da administracao'),
        'WHRL4-annual': select(14346, lambda r: r['Data_Referencia'] == '2025-12-31'
            and cash.norm(r['Tipo']) == 'demonstracoes financeiras anuais completas'),
        'EVEN3-annual': select(20524, lambda r: r['Data_Referencia'] == '2025-12-31'
            and cash.norm(r['Tipo']) == 'demonstracoes financeiras anuais completas'),
        'EVEN3-release': select(20524, lambda r: r['Data_Referencia'] == '2025-12-31'
            and cash.norm(r['Tipo']) == 'press-release'),
    }


def verified_new_documents(root=ROOT):
    plan, sources = acquisition_plan(root), {}
    for name, (sequence, version, published, digest) in DOCUMENTS.items():
        row = plan[name]
        require((row['sequence'], row['Versao'], row['Data_Entrega']) == (sequence, version, published),
                'latest_primary_document_changed_no_fallback')
        receipts = [s for s in cash.receipts(str(root)) if s['url'] == row['Link_Download']
                    and s['sha256'] == digest]
        require(len(receipts) == 1, 'exact_native_transport_required')
        source, = receipts
        cash.read(root, source)
        sources[name] = {**cash.public_source(source), 'published_at': published,
            'primary_index_row': row, 'authority': 'primary', 'original_bytes_preserved': True,
            'point_in_time_archive_verified': False, 'model_admission': False}
    return sources


def assessment(root=ROOT):
    """New native evidence is not an authorization to fill absent source fields."""
    from .round2_cash_eps import _contains
    masters, tables, annual = current_tables(root)
    sources = verified_new_documents(root)
    proposal = _pdf_pages(root, sources['WHRL4-latest-proposal'], [55, 57, 59])
    release = _pdf_pages(root, sources['EVEN3-release'], [27, 58])
    _contains(proposal[55], '31 de dezembro de 2025', '448.526.788,00')
    _contains(proposal[57], '1.028.700.892', '474.085.114', 'nao existem acoes em tesouraria')
    _contains(proposal[59], 'Valor por acao do Lucro Liquido', 'Ordinarias Preferenciais',
              '2025 (interc.) 0,4360 0,9461')
    _contains(release[27], 'Controladora Consolidado', '2024 2025 2024 2025',
              'Aquisicao (baixas) de bens do ativo imobilizado e intangivel 2.412 (755) 6.249 12.618')
    _contains(release[58], 'Acquisition (disposals) of property, plant, and intangible assets',
              '2,412 (755) 6,249 12,618')
    records = {}
    for ticker in TARGETS:
        records[ticker] = {'master': masters[ticker],
            'DRE_con_sha256': native._dre_fingerprint(tables[ticker]['DRE_con'], full=True),
            'DRE_con': [r for r in tables[ticker]['DRE_con'] if r['CD_CONTA'].startswith(('3.11', '3.99'))],
            'DFC_con': [r for r in tables[ticker]['DFC_MI_con'] if r['CD_CONTA'] == '6.01'
                        or r['CD_CONTA'].startswith('6.02')],
            'consolidated_core_substantive': True, 'genuinely_empty_scope': False,
            'accounts_scope': 'consolidated', 'model_admission': False, 'approved_reading': None}
    records['WHRL4'].update(
        status='pending_native_PN_basic_EPS_basis_unresolved',
        new_literal_evidence={'document': 'WHRL4-latest-proposal', 'pdf_page': 59,
            'title': 'Valor por acao do Lucro Liquido', 'ON': '0.4360', 'PN': '0.9461',
            'not_identified_as_basic_EPS': True, 'printed_parent_profit_brl_p55': '448526788.00',
            'current_ON_shares_p57': '1028700892', 'current_PN_shares_p57': '474085114',
            'current_counts_not_transferred_as_weighted_denominators': True},
        same_document_pooled_note={'source': CACHED['WHRL4'], 'pdf_page': 79, 'note': '18.6',
            'reported_profit_thousand_BRL': '448526', 'reported_weighted_shares_thousand': '1502786',
            'reported_share_description': 'acoes ordinarias', 'reported_basic_diluted_EPS': '0.29846',
            'explicit_PN_allocation': False},
        missing_source_field='Explicit same-consolidated FY2025 basic PN EPS with reported class '
            'allocation and annual weighted PN denominator, or primary clarification of pooled EPS applicability.',
        finding='Latest April proposal is byte-identical to the issuer final proposal, not an older '
            'proposal fallback. It prints class values 0.4360/0.9461 but does not identify them as '
            'basic EPS or provide weighted class denominators and allocated numerators. The annual '
            'note still labels the pooled total-share denominator ordinary, and the native CSV '
            'has only an ON basic leaf. No PN EPS is inferred from either presentation.')
    records['EVEN3'].update(
        status='pending_gross_consolidated_cash_capex_unresolved',
        new_literal_evidence={'document': 'EVEN3-release', 'pdf_pages': [27, 58],
            'annual_document': 'EVEN3-annual', 'annual_pdf_page': 15,
            'annual_page_visually_verified': True, 'CFO_consolidated_thousand_BRL': '88842',
            'combined_acquisitions_disposals_consolidated_thousand_BRL': '12618',
            'combined_acquisitions_disposals_individual_thousand_BRL': '-755',
            'gross_consolidated_cash_acquisitions': None},
        missing_source_field='Gross consolidated FY2025 cash paid for PPE/intangibles separated '
            'from cash received on disposals, in the same final filing and scope.',
        finding='New CVM final annual statements p15 and bilingual release p27/p58 confirm the '
            'combined acquisition/disposal line 12618 thousand as a cash inflow, not a gross '
            'acquisition outflow. The individual -755 is not borrowed. The management cash '
            'generation metric and PPE carrying balances do not establish gross cash purchases.')
    return {'reference_date': str(CUTOFF), 'targets': records, 'new_primary_sources': sources,
        'annual_source': annual, 'new_readings': [], 'new_admissions': [],
        'native_numeric_values_changed': [], 'literal_pages': {'proposal': proposal, 'release': release},
        'source_metadata': {'point_in_time_archive_verified': False, 'settlement_verified': False,
            'exact_current_DFP_versions': EXPECTED, 'EPS_imputed': False,
            'scope_mixed': False, 'net_cash_flow_recast_as_gross_capex': False},
        'acquisition_limits_not_negative_document_content': [
            {'document': 'new issuer WHRL4 CVM DFP PDF', 'error': 'response_size_limit',
             'partial_body_used': False},
            {'document': 'WHRL4 annual complete CVM IPE 1020989 v1', 'error': 'response_size_limit',
             'partial_body_used': False}],
        'parent_integration': 'No reading to integrate. Retain both financial pending states. '
            'This is evidence of unresolved required source fields, not proof that no undiscovered correction exists.'}


def inspect_cached(root=ROOT):
    import pypdfium2 as pdfium
    masters, tables, annual = current_tables(root)
    output = {}
    for ticker, source in CACHED.items():
        body = cash.read(root, source)
        with pdfium.PdfDocument(body) as pdf:
            count = len(pdf)
        numbers = [1, 77, 78, 79] if ticker == 'WHRL4' else [1, 35, 60, 61, 62]
        output[ticker] = {'master': masters[ticker], 'source': source, 'annual_source': annual,
            'page_count': count, 'pages': _pdf_pages(root, source, numbers),
            'tables': tables[ticker], 'full_DRE_sha256': native._dre_fingerprint(tables[ticker]['DRE_con'], full=True),
            'exact_native_package_verified': False, 'point_in_time_archive_verified': False,
            'new_readings': [], 'model_admission': False}
    return output


def probe(root=ROOT):
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from .collect_market_data import financial_features
    from .market_inventory import load_inventory
    inventory = load_inventory(root)
    stocks = [next(r for r in inventory['stocks'] if r['ticker'] == t) for t in TARGETS]
    for stock in stocks:
        require(tuple(stock[k] for k in ('cvm', 'share_class', 'isin', 'sector_code')) == TARGETS[stock['ticker']],
                'instrument_identity_changed')
    def offline(*args, **kwargs):
        raise RuntimeError('round2_industrial_offline_network_forbidden')
    with patch.object(socket.socket, 'connect', offline), patch.object(socket.socket, 'connect_ex', offline):
        statements, diagnostics, sources = native.prepare_financials(root, stocks, CUTOFF)
        results = []
        for stock in stocks:
            checks = {}
            result = {'ticker': stock['ticker'], 'one_pass': False, 'current_financial_pass': False}
            try:
                candidate = candidates._one(stock, candidates._Cache(root, inventory), CUTOFF, checks)
                result.update(one_pass=True, ledger_rows=len(candidate['ledger']))
                current = financial_features(statements, candidate['payments'], CUTOFF,
                    (stock['cvm'], stock['share_class'], stock['sector_code']))
                result.update(current_financial=current, current_financial_pass=current is not None
                    and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
            except (ValueError, OSError, KeyError, TypeError) as error:
                result['source_blocker'] = str(error)
            result.update(checks=checks, latest_records=[r for r in statements if r['cvm'] == stock['cvm']
                and r['share_class'] == stock['share_class'] and r['fiscal_end'] == '2025-12-31'],
                diagnostics=[d for d in diagnostics if d.get('cvm') == stock['cvm'] and d.get('fiscal_end') == '2025-12-31'])
            results.append(result)
    return {'targets': results, 'sources': sources, 'network_calls': 0, 'acquisitions': 0,
            'financial_hooks_overridden': [], 'normal_gates_modified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'probe', 'acquire', 'assess'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url')
    parser.add_argument('--discovery')
    args = parser.parse_args()
    if args.command == 'inspect':
        result = inspect_cached(args.root)
    elif args.command == 'probe':
        result = probe(args.root)
    elif args.command == 'assess':
        result = assessment(args.root)
    else:
        require(args.url and args.discovery, 'acquisition_discovery_required')
        result = acquire(args.url, json.loads(args.discovery), args.root)
    report(args.output, result)
    if args.command == 'assess':
        print(json.dumps({t: r['status'] for t, r in result['targets'].items()}), flush=True)
    elif 'targets' in result:
        print(json.dumps([{k: r[k] for k in ('ticker', 'one_pass', 'current_financial_pass')}
                          for r in result['targets']]), flush=True)
    elif args.command == 'acquire':
        print(json.dumps({k: result.get(k) for k in ('status', 'error', 'file', 'sha256', 'bytes')}), flush=True)
    else:
        print(json.dumps({t: r['page_count'] for t, r in result.items()}), flush=True)


if __name__ == '__main__':
    main()
