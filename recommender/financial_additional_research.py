"""Offline v5 financial research, with explicit whole-scope preparation.

The default is the existing consolidated-first policy. An individual result is
available only when the caller explicitly requests the reviewed whole scope.
This module never changes the shared parser, its manifest list, or a snapshot.
"""

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import zipfile

from . import inventory_financials as native
from .collect_market_data import cvm_statements, financial_features


RESEARCH_FILE = 'raw/primary-research/financials/review-20261025-v5.json'
RESEARCH_SHA256 = 'b070e843d2edb96288af178ea9e30c51a00f12658326b9d779506b773c177b55'
SCOPED_IDENTITY = ('WIZC3', 23590, 'ON', 2, 'individual', '2025-12-31',
                   '1', '155497', '2026-03-19')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _identity(review):
    return tuple(review[field] for field in (
        'ticker', 'cvm', 'share_class', 'sector_code', 'accounts_scope',
        'fiscal_end', 'version', 'filing_id', 'published_at'))


def _record_key(record):
    return tuple(record[field] for field in (
        'cvm', 'share_class', 'fiscal_end', 'version', 'filing_id'))


def read_review(folder):
    """Read the pinned v5 artifact. It intentionally has no default readings."""
    body = (Path(folder) / RESEARCH_FILE).read_bytes()
    _require(hashlib.sha256(body).hexdigest() == RESEARCH_SHA256,
             'additional_manifest_hash_mismatch')
    review = json.loads(body)
    _require(review['schema_version'] == 5 and review['readings'] == [],
             'additional_manifest_schema_mismatch')
    return review


def _scope_view(archive, year, review):
    """Project one explicitly requested native scope into an in-memory ZIP.

    The full original archive is authenticated before this projection. Its real
    consolidated core is recorded in provenance, never described as a template.
    No values, periods, classes, scales, or signs change in the projection.
    """
    key = review['cvm'], review['fiscal_end'], review['version']
    suffixes = ('', '_DRE_ind', '_BPA_ind', '_BPP_ind', '_DFC_MI_ind', '_DFC_MD_ind')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as projected:
        for suffix in suffixes:
            name = f'dfp_cia_aberta{suffix}_{year}.csv'
            if name not in archive.namelist():
                continue
            rows = [row for row in native._rows(archive, name) if native._key(row) == key]
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else native.COLUMNS,
                                    delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            projected.writestr(name, stream.getvalue().encode('latin1'))
    return output.getvalue()


def _validated_scope(folder, manifest, review, cutoff):
    _require(_identity(review) == SCOPED_IDENTITY, 'unreviewed_scope_identity')
    _require(review['full_dre'] is True and review['scope_request_required'] is True,
             'explicit_whole_scope_proof_required')
    _require(review['eps_scope'] == 'individual' and review['pdf_consolidated_eps'] == 'n/a',
             'documented_eps_scope_mismatch')
    _require(native._day(review['published_at']) <= cutoff, 'filing_not_yet_published')
    source = manifest['native_archive']
    _require(source['file'] == 'raw/official-financials/dfp_cia_aberta_2025.zip',
             'unreviewed_archive_path')
    content = (folder / source['file']).read_bytes()
    _require(hashlib.sha256(content).hexdigest() == source['sha256'],
             'additional_native_archive_hash_mismatch')
    key = review['cvm'], review['fiscal_end'], review['version']
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        masters = [row for row in native._rows(archive, 'dfp_cia_aberta_2025.csv')
                   if int(row['CD_CVM']) == review['cvm']
                   and native._day(row['DT_REFER']) <= cutoff
                   and native._day(row['DT_RECEB']) <= cutoff]
        _require(bool(masters), 'native_filing_absent')
        latest_key = max((native._key(row) for row in masters),
                         key=lambda item: (item[1], int(item[2])))
        _require(latest_key == key, 'review_is_not_latest_native_filing')
        master = [row for row in masters if native._key(row) == key]
        _require(len(master) == 1 and master[0]['ID_DOC'] == review['filing_id']
                 and master[0]['DT_RECEB'][:10] == review['published_at']
                 and master[0]['CNPJ_CIA'] == review['cnpj'], 'native_filing_identity_mismatch')
        current = {}
        for scope in ('ind', 'con'):
            rows = [row for row in native._rows(archive, f'dfp_cia_aberta_DRE_{scope}_2025.csv')
                    if native._key(row) == key and row['ORDEM_EXERC'] == '\u00daLTIMO']
            digest = review['dre_sha256'] if scope == 'ind' else review['consolidated_dre_sha256']
            _require(native._dre_fingerprint(rows, full=True) == digest,
                     'documented_original_scope_cells_mismatch')
            current[scope] = rows
        native._checked_note_eps(current['ind'], review)
        attributed = {row['CD_CONTA']: native._brl(row) for row in current['con']
                      if row['CD_CONTA'] in ('3.11', '3.11.01', '3.11.02')}
        _require(len(attributed) == 3
                 and attributed['3.11'] == attributed['3.11.01'] + attributed['3.11.02'],
                 'native_parent_nci_reconciliation_failed')
        projected = _scope_view(archive, 2025, review)
    proof = manifest['documents'][review['document']]
    _require(Path(proof['file']).name == proof['file'], 'primary_document_must_be_local_basename')
    path = folder / Path(RESEARCH_FILE).parent / proof['file']
    pdf = path.read_bytes()
    _require(len(pdf) == proof['bytes'] and hashlib.sha256(pdf).hexdigest() == proof['sha256']
             and pdf.startswith(b'%PDF-'), 'additional_primary_document_hash_mismatch')
    receipt = {
        'id': 'primary-financials:' + proof['sha256'], 'url': proof['url'],
        'file': path.relative_to(folder).as_posix(), 'sha256': proof['sha256'], 'bytes': len(pdf),
        'retrieved_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        'provider': proof['issuer'], 'authority': 'primary',
        'document_type': 'audited_annual_statements', 'use': 'explicit_whole_scope_interpretation',
    }
    evidence = {'source_ref': receipt['id'], 'pdf_page': review['pdf_page'], 'note': review['note']}
    return projected, {**review, 'manifest_file': RESEARCH_FILE, 'manifest_sha256': RESEARCH_SHA256,
                       'primary_evidence': evidence}, receipt


def prepare_additional_financials(folder, candidates, cutoff, *, scope_requests=None):
    """Return the shared contract; opt into a complete reviewed scope explicitly.

    Example: scope_requests={'WIZC3': 'individual'}. Without that request the
    present consolidated core remains authoritative and WIZC3 remains blocked.
    An invalid proof leaves the existing unavailable latest record in place.
    All monetary roots and EPS in the opt-in record come from the individual
    tables of the same latest DFP. Financial-sector FCF remains None.
    """
    folder, cutoff = Path(folder), native._day(cutoff)
    if not isinstance(candidates, dict):
        candidates = list(candidates)
    statements, diagnostics, sources = native.prepare_financials(folder, candidates, cutoff)
    requests = dict(scope_requests or {})
    if not requests:
        return statements, diagnostics, sources
    specs = native._specs(candidates, [])
    try:
        manifest = read_review(folder)
    except (OSError, ValueError, KeyError, TypeError) as error:
        diagnostics.append({'kind': 'source', 'source_file': RESEARCH_FILE, 'status': 'excluded',
                            'reasons': ['additional_research_unavailable'], 'error_type': type(error).__name__})
        return statements, diagnostics, sources
    for ticker, scope in requests.items():
        try:
            eligible = [review for review in manifest['scoped_readings']
                        if review['ticker'] == ticker and review['accounts_scope'] == scope]
            _require(len(eligible) == 1, 'unreviewed_scope_request')
            review = eligible[0]
            _require(specs.get(ticker) == (review['cvm'], review['share_class'], review['sector_code']),
                     'exact_candidate_identity_required')
            content, reading, receipt = _validated_scope(folder, manifest, review, cutoff)
            scoped_specs = {ticker: specs[ticker]}
            key = review['cvm'], review['fiscal_end'], review['version']
            audit = []
            filtered, contexts = native._annual_archive(
                content, 2025, scoped_specs, cutoff, audit,
                {(*key, review['filing_id']): reading})
            parsed = cvm_statements(filtered, 2025, specs=scoped_specs, diagnostics=audit)
            context = contexts[key]
            _require(not context['reasons'] and context.get('primary_evidence')
                     and len(parsed) == 1, 'whole_scope_financial_features_unavailable')
            record = deepcopy(parsed[0])
            _require(_record_key(record) == (*key[:1], review['share_class'], *key[1:], review['filing_id']),
                     'parsed_scope_identity_mismatch')
            method = record['financial_method']
            method.update(accounts_scope=scope, scope_selection='explicit_reviewed_whole_individual_scope',
                          native_consolidated_core_present=True,
                          source_statements=context['source_statements'],
                          primary_evidence=context['primary_evidence'],
                          documentary_reading=context['documentary_reading'],
                          reported_basic_eps=context['reported_basic_eps'])
            method['documentary_reading']['native_archive_sha256'] = manifest['native_archive']['sha256']
            method['profit_account'] = context['account_aliases'][method['profit_account']]
            record.update(accounts_scope=scope, document_type='DFP', period_type='annual', annualized=False,
                          status='included', source_file=manifest['native_archive']['file'],
                          source_ref='cvm:DFP:2025:' + manifest['native_archive']['sha256'])
            _require(financial_features([record], [], cutoff, scoped_specs[ticker]) is not None
                     and record['free_cash_per_share'] is None,
                     'financial_sector_contract_failed')
            matches = [i for i, existing in enumerate(statements) if _record_key(existing) == _record_key(record)]
            _require(len(matches) == 1, 'latest_record_replacement_not_unique')
            statements[matches[0]] = record
            diagnostics = [row for row in diagnostics
                           if not (row.get('kind') == 'candidate' and row.get('ticker') == ticker)]
            diagnostics.append({'kind': 'candidate', 'ticker': ticker, 'cvm': review['cvm'],
                                'share_class': review['share_class'], 'sector_code': review['sector_code'],
                                'status': 'included', 'reasons': [], 'accounts_scope': scope,
                                'filing_id': review['filing_id'], 'fiscal_end': review['fiscal_end'],
                                'published_at': review['published_at'],
                                'scope_selection': method['scope_selection']})
            diagnostics.append({'kind': 'additional_scope', 'ticker': ticker, 'status': 'included',
                                'accounts_scope': scope, 'reasons': [],
                                'financial_method': deepcopy(method)})
            if receipt['id'] not in {source['id'] for source in sources}:
                sources.append(receipt)
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, csv.Error, zipfile.BadZipFile) as error:
            diagnostics.append({'kind': 'additional_scope', 'ticker': ticker, 'status': 'excluded',
                                'accounts_scope': scope, 'reasons': ['additional_scope_not_authorized'],
                                'error_type': type(error).__name__,
                                'detail': str(error) if isinstance(error, ValueError) else None})
    return statements, diagnostics, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--cutoff', default='2026-09-30')
    parser.add_argument('--individual-wiz', action='store_true')
    args = parser.parse_args()
    manifest = read_review(args.folder)
    specs = {row['ticker']: (row['cvm'], row['share_class'], row['sector_code'])
             for row in manifest['findings']}
    requests = {'WIZC3': 'individual'} if args.individual_wiz else None
    records, diagnostics, _ = prepare_additional_financials(args.folder, specs, args.cutoff,
                                                          scope_requests=requests)
    included = {}
    for ticker, spec in specs.items():
        result = financial_features(records, [], native._day(args.cutoff), spec)
        if result:
            included[ticker] = {'features': result[0], 'filing': result[1]}
    print(json.dumps({'manifest_sha256': RESEARCH_SHA256, 'scope_requests': requests or {},
                      'available': included, 'candidates': [row for row in diagnostics
                       if row['kind'] in ('candidate', 'additional_scope')]}, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
