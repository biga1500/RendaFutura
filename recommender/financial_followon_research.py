"""Bounded offline follow-up of seven pending financial inputs.

No production imports this module. The default audit admits nothing. The only
candidate is the already reviewed, explicitly requested WHOLE individual WIZ
scope, not a new consolidated admission or permission to mix statement scopes.
"""

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile

from . import financial_additional_research as additional
from . import inventory_financials as native
from .collect_market_data import financial_features, stock_observations


OUTPUT = 'raw/primary-research/financial-followon'
CUTOFF = date(2026, 9, 30)
V5_FILE = 'raw/primary-research/financials/review-20261025-v5.json'
V5_HASH = 'b070e843d2edb96288af178ea9e30c51a00f12658326b9d779506b773c177b55'
PRIOR_HASHES = {
    'review-20261025-v1.json': 'b290ff72b3b10de240c5168efdf1aaade4e32191038bd864db3590e53779efc1',
    'findings-20261025-v2.json': 'b55e488420f86694abaaa8ed6f852f700450b5129f29b8b4b5cf53f046d86bae',
    'review-20261025-v4.json': 'fa6aeb49df399661e79fc4d4df8a0629f9f82a6fe51f91f3701c6507d6f41458',
    'review-20261025-v5.json': V5_HASH,
}
SPECS = {
    'BPAC3': (22616, 'ON', 2), 'BSLI3': (14206, 'ON', 2),
    'EVEN3': (20524, 'ON', 4), 'PINE4': (20567, 'PN', 2),
    'SIMH3': (25003, 'ON', 2), 'WHRL4': (14346, 'PN', 4),
    'WIZC3': (23590, 'ON', 2),
}
# Fiscal end, revision, native document, publication, exact legal issuer.
EXPECTED = {
    'BPAC3': ('2025-12-31', '2', '155648', '2026-03-24', '30.306.294/0001-45'),
    'BSLI3': ('2024-12-31', '3', '149667', '2025-06-30', '00.000.208/0001-00'),
    'EVEN3': ('2025-12-31', '1', '155582', '2026-03-23', '43.470.988/0001-65'),
    'PINE4': ('2025-12-31', '3', '155965', '2026-03-31', '62.144.175/0001-20'),
    'SIMH3': ('2025-12-31', '1', '155854', '2026-03-30', '07.415.333/0001-20'),
    'WHRL4': ('2025-12-31', '1', '155763', '2026-03-27', '59.105.999/0001-86'),
    'WIZC3': ('2025-12-31', '1', '155497', '2026-03-19', '42.278.473/0001-03'),
}
BLOCKERS = {
    'BPAC3': 'Prior IFRS note allocates total profit including NCI; exact latest v2 '
             'parent-attributable ON basic numerator/denominator is not proved.',
    'BSLI3': 'No 2025 annual filing in cached masters; latest 2024 v3 flows are '
             'December-only. Older April PDF cannot repair June revision periods.',
    'EVEN3': 'Positive 12,618 thousand is acquisitions/disposals NET. Full PDF '
             'contains no gross cash acquisition/disposal separation. PPE balances '
             'and depreciation are not gross cash payments; FCF stays unavailable.',
    'PINE4': 'Printed ON/PN allocations sum to 432,878 thousand including NCI '
             '2,363 thousand, not parent profit 430,515 thousand. No correction '
             'or exact-v3 class allocation proof is cached.',
    'SIMH3': 'Exact native v1 note p140 is Controladora: numerator 39,741 thousand '
             'and weighted ON 426,797,758. Form parent is 39,471 thousand. '
             'Total 215,250 differs from parent+NCI 212,641 by 2,609; '
             'discontinued loss does not authorize changing roots or EPS scope.',
    'WHRL4': 'Only ON basic EPS is explicit in native DRE. Note 18.6 pools ON+PN '
             'shares and labels ordinary; PN dividend preference is distinct. '
             'No explicit PN basic EPS allocation/denominator is cached.',
    'WIZC3': 'Exact native v1 PDF p56 explicitly gives consolidated EPS/shares n/a; '
             'p102 note 24.4 is Controladora. Default consolidated-first remains '
             'blocked; only the existing explicit whole-individual alternative is available.',
}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _receipt(folder, path, *, url, authority='primary', retrieved_at=None):
    _require(not Path(path).is_symlink(), 'source_symlink_not_allowed')
    path, folder = Path(path).resolve(), Path(folder).resolve()
    _require(path.is_relative_to(folder) and not path.is_symlink(), 'source_outside_root')
    body = path.read_bytes()
    return {
        'id': 'financial-followon:' + hashlib.sha256(body).hexdigest(),
        'file': path.relative_to(folder).as_posix(), 'url': url,
        'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
        'authority': authority, 'retrieved_at': retrieved_at,
        'file_mtime_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        'mtime_is_retrieval_or_publication': False,
        'clock_use': 'local_file_observation_only' if retrieved_at is None else 'transport_receipt',
    }


def _latest_rows(rows, ticker, cutoff):
    """Inspect ALL mirror masters; never prefer a readable older revision."""
    _require(ticker in SPECS, 'unreviewed_ticker')
    eligible = [row for row in rows if int(row['CD_CVM']) == SPECS[ticker][0]
                and native._day(row['DT_REFER']) <= cutoff
                and native._day(row['DT_RECEB']) <= cutoff]
    _require(bool(eligible), 'latest_native_filing_absent')
    newest = max((row['DT_REFER'], int(row['VERSAO'])) for row in eligible)
    matches = [row for row in eligible if (row['DT_REFER'], int(row['VERSAO'])) == newest]
    identities = {tuple(row[field][:10] if field == 'DT_RECEB' else row[field]
                        for field in ('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'))
                  for row in matches}
    _require(len(identities) == 1, 'conflicting_latest_native_mirrors')
    identity = next(iter(identities))
    _require(identity == EXPECTED[ticker], 'review_is_not_latest_exact_native_filing')
    return dict(zip(('fiscal_end', 'version', 'filing_id', 'published_at', 'cnpj'), identity))


def latest_guard(folder, tickers=tuple(SPECS), cutoff=CUTOFF):
    folder, cutoff = Path(folder), native._day(cutoff)
    _require(cutoff == CUTOFF, 'unreviewed_cutoff')
    rows, receipts = [], []
    requested = {SPECS[ticker][0] for ticker in tickers}
    for directory in (folder / 'raw/official-financials', folder / 'raw'):
        for path in sorted(directory.glob('*.zip')):
            match = re.fullmatch(r'(?:cvm-dfp-?(\d{4})|dfp_cia_aberta_(\d{4}))\.zip', path.name)
            if not match:
                continue
            year = int(match[1] or match[2])
            if year > cutoff.year:
                continue
            with zipfile.ZipFile(path) as archive:
                rows.extend(row for row in native._rows(archive, f'dfp_cia_aberta_{year}.csv')
                            if int(row['CD_CVM']) in requested)
            receipts.append(_receipt(folder, path, url=(
                'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/'
                f'dfp_cia_aberta_{year}.zip')))
    return {ticker: _latest_rows(rows, ticker, cutoff) for ticker in tickers}, receipts


def _review(folder):
    body = (Path(folder) / V5_FILE).read_bytes()
    _require(hashlib.sha256(body).hexdigest() == V5_HASH, 'prior_review_hash_mismatch')
    review = json.loads(body)
    _require(review['readings'] == [] and review['schema_version'] == 5,
             'prior_review_scope_changed')
    return review


def _simh_original_sources(folder):
    root = folder / 'raw/primary-research/remaining-financials'
    transport = json.loads((root / 'response-SIMH3.json').read_bytes())
    url = ('https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx'
           '?CodigoInstituicao=1&NumeroSequencialDocumento=155854')
    digest = 'd349502d8527288ffceb44735a55bc782e5881559a0defe91905036a1bd0189f'
    _require(transport['status'] == 200 and transport['url'] == url
             and transport['sha256'] == digest and transport['bytes'] == 12885669
             and transport['filing_id'] == '155854', 'simh_transport_proof_mismatch')
    container = root / ('original-' + digest + '.zip')
    source = _receipt(folder, container, url=url, retrieved_at=transport['completed_at'])
    _require(source['sha256'] == digest and source['bytes'] == 12885669,
             'simh_native_container_mismatch')
    result = [source]
    names = {
        '155854_025003_25102026065109.pdf':
            ('native-SIMH3-2d0bdfc8ddde46a2a6ef5c597d6919a6634763e8b31332b270784207fb1bdcd9.pdf',
             '2d0bdfc8ddde46a2a6ef5c597d6919a6634763e8b31332b270784207fb1bdcd9'),
        '025003DFP31-12-2025v1.xml':
            ('native-SIMH3-e0109976d18ac4fd870b8fa7499ce3b7809ffd7833366b1312fdf89298b580a1.xml',
             'e0109976d18ac4fd870b8fa7499ce3b7809ffd7833366b1312fdf89298b580a1'),
    }
    with zipfile.ZipFile(container) as archive:
        for member, (filename, expected_hash) in names.items():
            path = root / filename
            _require(path.read_bytes() == archive.read(member), 'simh_original_member_mismatch')
            receipt = _receipt(folder, path, url=url, retrieved_at=transport['completed_at'])
            _require(receipt['sha256'] == expected_hash, 'simh_original_hash_mismatch')
            receipt.update(original_zip_member=member, original_container_ref=source['id'],
                           use='negative_evidence_only_no_EPS_override',
                           http_headers_scope='original_native_download',
                           http_date=transport['headers'].get('Date'))
            result.append(receipt)
    receipt = _receipt(folder, root / 'response-SIMH3.json', url=url,
                       authority='local_transport_receipt_not_primary_statement')
    result.append(receipt)
    return result


def prepare_wiz_candidate(folder, *, spec, scope_request, cutoff=CUTOFF):
    """Conditional handoff only; NEVER called automatically by the engine.

    Parent may use this tuple only after deciding on whole individual scope.
    Running _one and all ordinary cash/price/features/observations gates remains
    mandatory. This function does not admit an asset or publish a snapshot.
    """
    _require(isinstance(spec, tuple) and len(spec) == 3
             and type(spec[0]) is int and type(spec[2]) is int
             and spec == SPECS['WIZC3'], 'exact_wiz_identity_required')
    _require(scope_request == 'individual', 'explicit_whole_individual_scope_required')
    folder = Path(folder)
    _review(folder)
    latest, guard_sources = latest_guard(folder, ('WIZC3',), cutoff)
    result = additional.prepare_additional_financials(
        folder, {'WIZC3': spec}, cutoff, scope_requests={'WIZC3': scope_request})
    records, diagnostics, sources = deepcopy(result)
    features = financial_features(records, [], native._day(cutoff), spec)
    _require(features is not None, 'conditional_wiz_candidate_unavailable')
    _, record = features
    method = record['financial_method']
    _require(record['filing_id'] == latest['WIZC3']['filing_id']
             and record['version'] == '1' and record['accounts_scope'] == 'individual'
             and record['profit_per_share'] == 1.25745
             and record['free_cash_per_share'] is None
             and method['id'] == 'financial_no_industrial_fcf_v1'
             and method['source_statements']
             and all('_ind_' in name for name in method['source_statements'])
             and method['scope_selection'] == 'explicit_reviewed_whole_individual_scope',
             'whole_individual_contract_failed')
    # Legacy receipt dates came from file mtimes, not retained transport receipts.
    # Keep their original value for audit, explicitly NOT as a retrieval date.
    for source in sources:
        path = folder / source['file']
        proof = _receipt(folder, path, url=source['url'], authority=source['authority'])
        _require(proof['sha256'] == source['sha256'], 'source_receipt_hash_mismatch')
        original_id = source['id']
        source.update(proof, previous_unverified_clock_value=source.get('retrieved_at'))
        source['id'] = original_id
    existing = {(source['file'], source['sha256'], source['url']) for source in sources}
    sources.extend(source for source in guard_sources
                   if (source['file'], source['sha256'], source['url']) not in existing)
    diagnostics.append({'kind': 'conditional_handoff', 'ticker': 'WIZC3',
                        'status': 'not_admitted', 'automatic_integration': False,
                        'scope_decision_required': True, 'latest_native_guard': latest['WIZC3']})
    return records, diagnostics, sources


def replay_wiz_source_gates(folder, statements):
    """Run the ordinary read-only _one route, without publishing the candidate."""
    from .inventory_candidates import _Cache, _one
    from .market_inventory import load_inventory

    inventory = load_inventory(folder)
    matches = [row for row in inventory['stocks'] if row['ticker'] == 'WIZC3']
    _require(len(matches) == 1, 'unique_wiz_inventory_identity_required')
    cache = _Cache(folder, inventory)
    checks = {}
    candidate = _one(matches[0], cache, CUTOFF, checks)
    spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
    _require(spec == SPECS['WIZC3'], 'exact_wiz_inventory_identity_required')
    current = financial_features(statements, candidate['payments'], CUTOFF, spec)
    _require(current is not None, 'normal_financial_features_failed')
    basis = date.fromisoformat(candidate['financial_basis_start'])
    _require(date.fromisoformat(current[1]['fiscal_end']) >= basis,
             'annual_financial_share_basis_failed')
    training, _ = stock_observations('WIZC3', candidate['prices'], candidate['payments'],
                                     statements, CUTOFF, spec=spec)
    training = [row for row in training
                if date.fromisoformat(row['as_of']) >= candidate['training_since']
                and date.fromisoformat(row['fiscal_end']) >= basis]
    return {
        'ordinary_one_source_route_passed': True,
        'normal_financial_features_and_share_basis_passed': True,
        'training_rows_after_unchanged_parent_filters': len(training),
        'financial_basis_start': candidate['financial_basis_start'],
        'training_since': candidate['training_since'].isoformat(),
        'monthly_price_anchors': len(candidate['prices']),
        'checks': checks, 'automatic_admission': False,
        'current_purchase_quote_and_engine_forecast_not_tested': True,
        'point_in_time_archive_verified': False, 'bank_settlement_verified': False,
    }


def audit(folder):
    """Pure read-only report; a missing proof is not a fabricated usable input."""
    folder = Path(folder)
    previous = _review(folder)
    latest, sources = latest_guard(folder)
    records, diagnostics, _ = native.prepare_financials(folder, SPECS, CUTOFF)
    result = []
    for ticker, spec in SPECS.items():
        _require(financial_features(records, [], CUTOFF, spec) is None,
                 'default_financial_state_changed_reinspect_before_reporting')
        candidate = next((row for row in previous['findings'] if row['ticker'] == ticker), None)
        if ticker == 'PINE4':
            body = (folder / 'raw/primary-research/financials/review-20261025-v4.json').read_bytes()
            _require(hashlib.sha256(body).hexdigest() ==
                     'fa6aeb49df399661e79fc4d4df8a0629f9f82a6fe51f91f3701c6507d6f41458',
                     'prior_pine_review_hash_mismatch')
            candidate = json.loads(body)['findings'][0]
        _require(candidate is not None, 'prior_exact_ticker_finding_required')
        result.append({'ticker': ticker, 'spec': list(spec), 'latest': latest[ticker],
                       'default_status': 'blocked', 'reason': BLOCKERS[ticker],
                       'prior_reason': candidate['reason'],
                       'current_parser_diagnostics': [row for row in diagnostics
                           if row.get('kind') == 'candidate' and row.get('ticker') == ticker]})
    conditional = prepare_wiz_candidate(folder, spec=SPECS['WIZC3'], scope_request='individual')
    values, wiz = financial_features(conditional[0], [], CUTOFF, SPECS['WIZC3'])
    replay = replay_wiz_source_gates(folder, conditional[0])
    sources.extend(conditional[2])
    sources.extend(_simh_original_sources(folder))
    # Primary reviewed PDFs retain their actual download URLs and byte identities.
    manifests = []
    for name, digest in PRIOR_HASHES.items():
        path = folder / 'raw/primary-research/financials' / name
        body = path.read_bytes()
        _require(hashlib.sha256(body).hexdigest() == digest, 'prior_source_manifest_changed')
        manifests.append(json.loads(body))
        sources.append(_receipt(folder, path, url=None, authority='local_review_not_primary_original'))
    wanted = {'even', 'bpac', 'whrl', 'bsli_unusable', 'pine_ifrs_2025', 'wiz_native_v1'}
    for manifest in manifests:
        for name, document in manifest['documents'].items():
            if name not in wanted:
                continue
            path = folder / 'raw/primary-research/financials' / document['file']
            receipt = _receipt(folder, path, url=document['url'])
            _require(receipt['sha256'] == document['sha256'], 'primary_reviewed_pdf_changed')
            receipt.update(use='supporting_or_negative_evidence_not_automatic_filing_authorization',
                           native_container_retained_and_verified=False)
            sources.append(receipt)
    sources = list({(s['file'], s['sha256'], s['url']): s for s in sources}.values())
    return {'schema_version': 1, 'cutoff': CUTOFF.isoformat(), 'network_requests': 0,
            'new_default_admissions': [], 'production_or_pointer_writes': False,
            'conclusion': 'No NEW missing detail resolves these seven under unchanged default gates. '
                          'WIZ individual handoff repeats an existing reviewed alternative, '
                          'not a newly discovered or automatic admission.',
            'findings': result, 'sources': sources,
            'conditional_candidate': {
                'ticker': 'WIZC3', 'is_new_recovery': False, 'automatic_integration': False,
                'requires_explicit_whole_individual_scope': True,
                'normal_parent_cash_price_training_validation_still_required': True,
                'offline_normal_source_and_financial_replay': replay,
                'filing': {key: wiz[key] for key in ('cvm', 'share_class', 'fiscal_end',
                             'filing_id', 'version', 'published_at', 'accounts_scope')},
                'feature_vector_without_price_or_cash_inputs': values,
                'balance_scope': 'individual', 'parent_profit_brl': '201076000',
                'basic_eps_reported': '1.25745', 'weighted_basic_ON_shares': '159907282',
                'assets_brl': '1385841000', 'equity_brl': '697792000',
                'liabilities_brl': '688049000', 'free_cash_per_share': None,
                'hook': 'prepare_wiz_candidate(folder, spec=(23590, "ON", 2), scope_request="individual")',
                'source_clock_qualification': 'No retained WIZ HTTP receipt: retrieved_at stays null; '
                    'file mtime remains a separate local observation. Publication comes from native master.'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    args = parser.parse_args()
    report = audit(args.folder)
    encoded = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    root = args.folder / OUTPUT
    root.mkdir(parents=True, exist_ok=True)
    target = root / ('audit-' + hashlib.sha256(encoded).hexdigest() + '.json')
    if target.exists():
        _require(target.read_bytes() == encoded, 'immutable_audit_conflict')
    else:
        with target.open('xb') as stream:
            stream.write(encoded)
    print(json.dumps({'report': str(target.resolve()), 'default_admissions': [],
                      'conditional_candidate': 'WIZC3 (existing explicit individual scope only)',
                      'network_requests': 0}, indent=2))


if __name__ == '__main__':
    main()
