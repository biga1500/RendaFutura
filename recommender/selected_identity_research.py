"""Offline exact-code audit of selected identities, with no catalogue integration.

Every CSV member of each existing annual FCA ZIP is searched, including filings
excluded by the current catalogue parser. Only source-bound security rows can
establish a legal type. Existing exchange caches can establish an exact ISIN
mapping, but issuer-level flags and ISIN spelling cannot establish a class.
"""

import argparse
from collections import defaultdict
from copy import deepcopy
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from .dividend_scope import load_scope
from .market_inventory import market_root
from .security_registration import (FCA_URL, MASTER_FIELDS, SECURITY_FIELDS,
                                    _kind, _norm, parse_fca_registry)


POLICY = 'RF-SELECTED-IDENTITIES-OFFLINE-1'
SCOPE_ID = '0fbd068bb543bce1086ea844221265da40a51ae14818be3f27a677e764da30f3'
FOCUS = ('AURA33', 'EQPA7', 'MRSA5B', 'MRSA6B')
YEARS = tuple(range(2021, 2027))
OUTPUT = 'raw/primary-research/selected-identities'
PUBLIC_SECURITY_FIELDS = tuple(k for k in SECURITY_FIELDS if k != 'CNPJ_Companhia') + ('Composicao_BDR_Unit',)
PUBLIC_MASTER_FIELDS = tuple(k for k in MASTER_FIELDS if k != 'CNPJ_CIA')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('selected_identity_invalid_date')
    return date.fromisoformat(value)


class LocalSources:
    def __init__(self, root):
        self.root = market_root(root)
        self.receipts = {}

    def read(self, relative, expected=None):
        if (not isinstance(relative, str) or '\\' in relative
                or relative.startswith('/') or ':' in relative
                or any(p in ('', '.', '..') for p in relative.split('/'))):
            raise ValueError('selected_identity_unsafe_path')
        path = self.root / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(self.root)
                or path.stat().st_size > 20_000_000):
            raise ValueError('selected_identity_unsafe_path')
        body = path.read_bytes()
        digest = _sha(body)
        if expected is not None and (not re.fullmatch(r'[a-f0-9]{64}', expected) or digest != expected):
            raise ValueError('selected_identity_source_hash_mismatch')
        self.receipts[relative] = {'file': relative, 'sha256': digest, 'bytes': len(body),
                                   'access': 'existing_local_file_read_only'}
        return body, deepcopy(self.receipts[relative])

    def addressed_json(self, path):
        match = re.search(r'-([a-f0-9]{64})\.json$', path.name)
        if match is None:
            raise ValueError('selected_identity_manifest_hash_missing')
        body, receipt = self.read(path.relative_to(self.root).as_posix(), match[1])
        return json.loads(body), receipt


def _legal_type(row):
    reported = _norm(row['Valor_Mobiliario'])
    if reported == 'bdr' or reported.startswith('bdr ') or reported == 'brazilian depositary receipts':
        return 'bdr', None
    return _kind(row)


def scan_fca(content, year, tickers, as_of):
    """Search all cells; only exact Codigo_Negociacao + valid master joins bind.

    The existing strict parser validates the archive first. Its known child
    identity mismatch is recorded and independently checked per row so an
    unrelated bad join cannot suppress a complete negative search result.
    """
    when = _day(as_of)
    validation_error = None
    try:
        parse_fca_registry(content, year)
    except ValueError as error:
        if str(error) != 'security_FCA_child_identity_mismatch':
            raise
        validation_error = str(error)
    patterns = {t: re.compile(r'(?<![A-Za-z0-9_.-])' + re.escape(t) + r'(?![A-Za-z0-9_.-])') for t in tickers}
    tables, coverage = {}, []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 100:
            raise ValueError('selected_identity_duplicate_or_excess_FCA_members')
        if sum(info.file_size for info in archive.infolist()) > 80_000_000:
            raise ValueError('selected_identity_FCA_expansion_limit')
        for info in archive.infolist():
            if not info.filename.endswith('.csv') or info.file_size > 8_000_000:
                raise ValueError('selected_identity_unexpected_FCA_member')
            body = archive.read(info.filename)
            reader = csv.DictReader(io.StringIO(body.decode('latin1')), delimiter=';')
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError('selected_identity_FCA_header_invalid')
            rows = []
            counts = dict.fromkeys(tickers, 0)
            for number, row in enumerate(reader, 2):
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('selected_identity_FCA_row_misaligned')
                for ticker, pattern in patterns.items():
                    counts[ticker] += int(any(pattern.search(value) for value in row.values()))
                rows.append({**row, '_csv_record': number})
            tables[info.filename] = rows
            # No personal names, contacts, addresses or nonselected rows escape.
            coverage.append({'member': info.filename, 'sha256': _sha(body), 'bytes': len(body),
                             'csv_records': len(rows), 'exact_token_row_matches': counts})
    master_name = f'fca_cia_aberta_{year}.csv'
    security_name = f'fca_cia_aberta_valor_mobiliario_{year}.csv'
    masters = {r['ID_DOC']: r for r in tables[master_name]}
    records, issues = [], []
    joins = (('CNPJ_Companhia', 'CNPJ_CIA'), ('Data_Referencia', 'DT_REFER'), ('Versao', 'VERSAO'))
    for row in tables[security_name]:
        master = masters.get(row['ID_Documento'])
        mismatch = [child for child, parent in joins if master is None or row[child] != master[parent]]
        for field in ('Data_Inicio_Negociacao', 'Data_Fim_Negociacao', 'Data_Inicio_Listagem', 'Data_Fim_Listagem'):
            if row[field]:
                _day(row[field])
        exact = row['Codigo_Negociacao'].strip() in tickers
        if mismatch:
            issues.append({'code': 'FCA_child_identity_mismatch', 'member': security_name,
                           'csv_record': row['_csv_record'], 'filing_id': row['ID_Documento'],
                           'mismatched_fields': mismatch, 'affects_selected_exact_code': exact})
        if not exact:
            continue
        eligible = bool(master and _day(master['DT_RECEB']) <= when and _day(master['DT_REFER']) <= when)
        kind, share = _legal_type(row) if not mismatch else (None, None)
        records.append({'ticker': row['Codigo_Negociacao'].strip(), 'year': year,
            'security_member': security_name, 'csv_record': row['_csv_record'],
            'master_member': master_name, 'master_csv_record': master['_csv_record'] if master else None,
            'source_binding_valid': not mismatch, 'received_and_referenced_by_as_of': eligible,
            'filing': {k: master[k] for k in PUBLIC_MASTER_FIELDS} if master else None,
            'security': {k: row.get(k) for k in PUBLIC_SECURITY_FIELDS},
            'legal_security_type': kind, 'share_class': share,
            'cvm': int(master['CD_CVM']) if master and not mismatch else None})
    return {'year': year, 'members': coverage, 'security_records_scanned': len(tables[security_name]),
            'master_records_scanned': len(tables[master_name]), 'strict_parser_error': validation_error,
            'join_issues': issues, 'exact_security_records': records}


def _resolve(ticker, fca, cache, reports, coverage_complete):
    eligible = [r for r in fca if r['source_binding_valid'] and r['received_and_referenced_by_as_of']]
    cvms = {r['cvm'] for r in eligible} | {r['cvm'] for r in cache}
    isins = {r['isin'] for r in cache}
    types = {(r['legal_security_type'], r['share_class']) for r in eligible}
    names = {r['security']['Nome_Empresarial'] for r in eligible} | {r['issuer_name'] for r in cache if r['issuer_name']}
    conflict = len(cvms) > 1 or len(isins) > 1 or len(types) > 1
    kind, share = next(iter(types)) if len(types) == 1 and not conflict else (None, None)
    # Historical identity evidence is never promoted to current trading status.
    markets = sorted({r['security']['Mercado'] for r in eligible})
    return {'ticker': ticker,
        'status': 'conflicting_exact_evidence' if conflict else 'primary_exact_identity_evidence' if eligible else
                  'exact_cached_ISIN_mapping_only' if cache else 'unresolved_in_existing_sources',
        'issuer_name': next(iter(names)) if len(names) == 1 and not conflict else None,
        'cvm': next(iter(cvms)) if len(cvms) == 1 and not conflict else None,
        'isin': next(iter(isins)) if len(isins) == 1 and not conflict else None,
        'isin_basis': 'exact_otherCodes_mapping_in_existing_cache_not_FCA' if len(isins) == 1 and not conflict else 'not_established',
        'legal_security_type': kind, 'share_class': share,
        'type_basis': 'exact_source_bound_FCA_rows_retrospective' if kind else 'not_established_no_suffix_or_ISIN_inference',
        'reported_primary_markets': markets,
        'FCA_exact_code_found': bool(fca), 'FCA_exact_code_records': fca,
        'FCA_search_complete_for_2021_2026': coverage_complete,
        'cached_exact_code_mappings': cache, 'existing_security_report_exact_records': reports,
        'current_security_type_confirmed': False, 'current_trading_verified': False,
        'whole_security_delisting_verified': False, 'model_admission_granted': False,
        'next_step': ('Supply an offline primary security document explicitly binding ' + ticker
            + ' to issuer, legal type, share class, ISIN and dated effective terms. '
              'Do not substitute another code or infer delisting from absence.')}


def research(folder, as_of='2026-10-25', *, tickers=FOCUS, expected_scope=SCOPE_ID):
    """Read existing caches only; return a detached, scope-pinned JSON audit."""
    _day(as_of)
    local = LocalSources(folder)
    scope = load_scope(local.root)
    if scope is None or scope['id'] != expected_scope:
        raise ValueError('selected_identity_scope_missing_or_changed')
    tickers = tuple(tickers)
    if (not tickers or len(set(tickers)) != len(tickers)
            or any(t not in scope['selected_tickers'] for t in tickers)):
        raise ValueError('selected_identity_outside_positive_DY_scope')
    if any(not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}[A-Z]?', t) for t in tickers):
        raise ValueError('selected_identity_invalid_exact_code')
    protected = {}
    for relative in ('stock-selection/current.json', 'inventory/current.json', 'current.json'):
        if (local.root / relative).exists():
            body, receipt = local.read(relative)
            protected[relative] = receipt['sha256']
    pointer = json.loads(local.read('inventory/current.json')[0])
    snapshot = pointer.get('snapshot')
    if not isinstance(snapshot, str) or not re.fullmatch(r'snapshots/[A-Za-z0-9-]+', snapshot):
        raise ValueError('selected_identity_inventory_pointer_invalid')
    inventory_body, inventory_receipt = local.read('inventory/' + snapshot + '/market.json', pointer['sha256'])
    inventory = json.loads(inventory_body)
    protected[inventory_receipt['file']] = inventory_receipt['sha256']
    original_sources = defaultdict(list)
    for source in inventory.get('sources', []):
        original_sources[source['file']].append(source)
    history = sorted((local.root / 'raw/primary-research/units').glob('historical-fca-evidence-*.json'))
    report_paths = sorted((local.root / 'raw/primary-research/security-registration').glob('security-registration-*.json'))
    existing_reports, anchors = [], []
    for path in [*history, *report_paths]:
        data, receipt = local.addressed_json(path)
        anchors.append(receipt)
        for source in data.get('sources', []):
            original_sources[source['file']].append(source)
        if path in report_paths:
            existing_reports.append((data, receipt))
    fca_files = set((local.root / 'raw/inventory').glob('fca_cia_aberta_*.zip'))
    fca_files.update((local.root / 'raw/primary-research/units').glob('fca_cia_aberta_*.zip'))
    for relative, receipts in original_sources.items():
        if any(re.fullmatch(re.escape(FCA_URL) + r'fca_cia_aberta_20(?:21|22|23|24|25|26)\.zip', str(r.get('url'))) for r in receipts):
            fca_files.add(local.root / relative)
    archives, by_digest, fca_records = [], {}, defaultdict(list)
    for path in sorted(fca_files):
        match = re.fullmatch(r'fca_cia_aberta_(\d{4})(?:-[a-f0-9]{64})?\.zip', path.name)
        if match is None or int(match[1]) not in YEARS:
            continue
        year = int(match[1])
        relative = path.relative_to(local.root).as_posix()
        receipts = [r for r in original_sources[relative] if r.get('url') == FCA_URL + f'fca_cia_aberta_{year}.zip']
        if not receipts or any(not re.fullmatch(r'[a-f0-9]{64}', str(r.get('sha256'))) for r in receipts):
            raise ValueError('selected_identity_official_FCA_receipt_missing')
        expected = {r['sha256'] for r in receipts}
        if len(expected) != 1:
            raise ValueError('selected_identity_conflicting_source_receipts')
        body, source = local.read(relative, next(iter(expected)))
        if any(r.get('bytes', len(body)) != len(body) for r in receipts):
            raise ValueError('selected_identity_source_size_mismatch')
        source['existing_receipts'] = deepcopy(receipts)
        source['receipt_hash_verified'] = True
        key = (year, source['sha256'])
        if key in by_digest:
            by_digest[key]['sources'].append(source)
            continue
        scan = scan_fca(body, year, tickers, as_of)
        scan['sources'] = [source]
        for record in scan.pop('exact_security_records'):
            record['source'] = source
            fca_records[record['ticker']].append(record)
        by_digest[key] = scan
        archives.append(scan)
    mappings, cache_coverage = defaultdict(list), []
    for path in sorted((local.root / 'raw/expanded').glob('b3-detail-*.json')):
        relative = path.relative_to(local.root).as_posix()
        receipts = original_sources[relative]
        expected = {r['sha256'] for r in receipts if r.get('sha256')}
        if len(expected) > 1:
            raise ValueError('selected_identity_conflicting_source_receipts')
        body, source = local.read(relative, next(iter(expected)) if expected else None)
        source.update(existing_receipts=deepcopy(receipts),
            hash_matched_prior_receipt=bool(expected), original_download_receipt_verified=any(
                r.get('original_receipt_verified') is True for r in receipts))
        data = json.loads(body)
        codes = data.get('otherCodes', []) if isinstance(data, dict) else []
        hits = [n for n, code in enumerate(codes) if code.get('code') in tickers]
        cache_coverage.append({**local.receipts[relative], 'exact_selected_matches': len(hits)})
        for n in hits:
            code = codes[n]
            cvm = data.get('codeCVM')
            if (not isinstance(cvm, str) or not cvm.isdigit() or int(cvm) <= 0
                    or path.name != f'b3-detail-{int(cvm)}.json'
                    or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d', str(code.get('isin')))):
                raise ValueError('selected_identity_cache_binding_invalid')
            mappings[code['code']].append({'ticker': code['code'], 'isin': code['isin'], 'cvm': int(cvm),
                'issuer_name': data.get('companyName'), 'locator': f'otherCodes[{n}]', 'source': source,
                'source_reported_lastDate': data.get('lastDate'),
                'source_reported_lastDate_is_publication_date': False,
                'legal_type_or_class_established': False, 'issuer_flags_not_security_status': True})
    report_records, report_coverage = defaultdict(list), []
    for data, source in existing_reports:
        records = data.get('records', [])
        selected = [r for r in records if r.get('ticker') in tickers]
        report_coverage.append({'source': source, 'as_of': data.get('as_of'),
            'records_scanned': len(records), 'exact_selected_records': len(selected)})
        # Prior conclusions are not independent evidence; do not emit their PII.
        for row in selected:
            report_records[row['ticker']].append({'source': source, 'ticker': row['ticker'],
                'reported_status': row.get('status'), 'independent_primary_binding_established': False})
    covered_years = sorted({a['year'] for a in archives})
    complete = covered_years == list(YEARS)
    result = {'schema_version': 1, 'policy': POLICY, 'as_of': as_of,
        'scope': {'id': scope['id'], 'selected_count': scope['selected_count'],
                  'source_sha256': scope['source_sha256'], 'researched_exact_tickers': list(tickers)},
        'network_requests': 0, 'b3_network_requests': 0, 'shared_state_written': False,
        'code_sha256': _sha(Path(__file__).read_bytes()),
        'test_code_sha256': _sha(Path(__file__).with_name('test_selected_identity_research.py').read_bytes()),
        'inventory_source': inventory_receipt, 'inventory_as_of': inventory.get('as_of'),
        'source_manifest_anchors': anchors, 'FCA_archives': archives,
        'FCA_coverage': {'expected_years': list(YEARS), 'covered_years': covered_years,
            'complete': complete, 'distinct_archives': len(archives),
            'csv_members_scanned': sum(len(a['members']) for a in archives),
            'security_rows_scanned': sum(a['security_records_scanned'] for a in archives),
            'missing_years': sorted(set(YEARS) - set(covered_years))},
        'existing_exchange_cache_coverage': cache_coverage,
        'existing_security_report_coverage': report_coverage,
        'records': [_resolve(t, fca_records[t], mappings[t], report_records[t], complete) for t in tickers],
        'protected_inputs': protected,
        'limitations': [
            'Full existing annual archives are retrospective revisions, not verified point-in-time availability.',
            'Missing exact codes do not establish delisting, an alias, a legal type, a class, or a market.',
            'Cached exact ISIN mappings do not establish legal type from ISIN characters or issuer-level flags.',
            'FCA receipt retrieval dates, reference dates, delivery dates and versions are distinct and preserved.',
            'Nonsecurity CSV cells are searched only for coverage; their personal and contact data are not emitted.',
            'No prices, payments, financial indicators, model admissions or shared catalogue changes are produced.']}
    for relative, digest in protected.items():
        local.read(relative, digest)
    return deepcopy(result)


def write_audit(folder, report):
    local = LocalSources(folder)
    _day(report['as_of'])
    target = local.root / OUTPUT
    if target.is_symlink() or not target.resolve().is_relative_to(local.root):
        raise ValueError('selected_identity_unsafe_output')
    target.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    path = target / f'selected-identities-{report["as_of"]}-{_sha(body)}.json'
    try:
        with path.open('xb') as stream:
            stream.write(body)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != body:
            raise ValueError('selected_identity_immutable_output_conflict')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--as-of', default='2026-10-25')
    parser.add_argument('--write-audit', action='store_true')
    args = parser.parse_args()
    report = research(args.data_dir, args.as_of)
    summary = {'FCA_coverage': report['FCA_coverage'], 'records': [{k: r[k] for k in
        ('ticker', 'status', 'issuer_name', 'cvm', 'isin', 'legal_security_type', 'share_class')}
        for r in report['records']], 'network_requests': 0}
    if args.write_audit:
        summary['audit'] = str(write_audit(args.data_dir, report))
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == '__main__':
    main()
