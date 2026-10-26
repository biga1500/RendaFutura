"""Exact, offline WIZC3 whole-individual-scope adapter for explicit review.

The ordinary record/feature contract supports this scope, but the default
consolidated-first policy does not select it. Nothing imports this adapter
automatically. It neither prepares a dataset nor grants model admission.
"""

from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import zipfile

from . import financial_additional_research as additional
from . import financial_followon_research as followon
from . import inventory_financials as native
from .collect_market_data import cvm_statements, financial_features


SPEC = (23590, 'ON', 2)
CUTOFF = followon.CUTOFF
SCOPE_REQUEST = {'WIZC3': 'individual'}
KEY = (23590, 'ON', '2025-12-31', '1', '155497')
METHOD_ID = 'exact_wiz_whole_individual_scope_recheck_v1'
SNAPSHOT = 'universe-20261025T193657-68d47408'
TARGETS = {'BPAC3', 'EVEN3', 'PINE4', 'SIMH3', 'WHRL4', 'WIZC3', 'WLMM3', 'WLMM4'}
PRIOR_AUDIT = ('raw/primary-research/financial-followon/'
               'audit-c6927999b02d5b47a65d92b5db70406ac2f1739bb8ccee06d5ff5937cce382e2.json')


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _key(record):
    return tuple(record[field] for field in (
        'cvm', 'share_class', 'fiscal_end', 'version', 'filing_id'))


def _is_wiz(record):
    return (record.get('cvm'), record.get('share_class')) == SPEC[:2]


def _individual_roots(content, review):
    """Cross-check actual balance cells, not just a ratio supplied by a caller."""
    selected = {'BPA': {'1': 'assets'}, 'BPP': {
        '2': 'total_passivo', '2.03': 'equity',
        '2.01': 'current_liabilities', '2.02': 'noncurrent_liabilities'}}
    roots = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for table, accounts in selected.items():
            rows = list(native._rows(archive, f'dfp_cia_aberta_{table}_ind_2025.csv'))
            for account, name in accounts.items():
                matches = [row for row in rows if row['CD_CONTA'] == account
                           and row['ORDEM_EXERC'] == '\u00daLTIMO']
                _require(len(matches) == 1, 'individual_balance_root_not_unique')
                roots[name] = format(native._brl(matches[0]).normalize(), 'f')
    _require(roots == review['individual_balance_brl'], 'individual_balance_proof_mismatch')
    numbers = {name: Decimal(value) for name, value in roots.items()}
    liabilities = numbers['total_passivo'] - numbers['equity']
    _require(numbers['assets'] == numbers['total_passivo']
             and liabilities == numbers['current_liabilities'] + numbers['noncurrent_liabilities'],
             'individual_balance_reconciliation_failed')
    return roots, float(liabilities / numbers['assets'])


def _load_wiz_scope(folder, cutoff):
    """Reuse the pinned proof and shared parser on its complete native scope."""
    latest, sources = followon.latest_guard(folder, ('WIZC3',), cutoff)
    manifest = additional.read_review(folder)
    reviews = [row for row in manifest['scoped_readings']
               if additional._identity(row) == additional.SCOPED_IDENTITY]
    _require(len(reviews) == 1, 'unique_exact_scope_review_required')
    review = reviews[0]
    content, reading, receipt = additional._validated_scope(folder, manifest, review, cutoff)
    roots, debt_ratio = _individual_roots(content, review)
    diagnostics = []
    filtered, contexts = native._annual_archive(
        content, 2025, {'WIZC3': SPEC}, cutoff, diagnostics,
        {(23590, '2025-12-31', '1', '155497'): reading})
    parsed = cvm_statements(filtered, 2025, specs={'WIZC3': SPEC}, diagnostics=diagnostics)
    context = contexts[(23590, '2025-12-31', '1')]
    _require(len(parsed) == 1 and _key(parsed[0]) == KEY and not context['reasons'],
             'exact_whole_scope_parser_contract_failed')
    record = deepcopy(parsed[0])
    method = record['financial_method']
    method.update(
        accounts_scope='individual', scope_selection='explicit_reviewed_whole_individual_scope',
        native_consolidated_core_present=True, source_statements=context['source_statements'],
        primary_evidence=context['primary_evidence'],
        documentary_reading=context['documentary_reading'],
        reported_basic_eps=context['reported_basic_eps'],
        liabilities_method='total_passivo_less_individual_equity')
    method['profit_account'] = context['account_aliases'][method['profit_account']]
    archive = manifest['native_archive']
    method['documentary_reading']['native_archive_sha256'] = archive['sha256']
    method['scope_recheck'] = {
        'id': METHOD_ID, 'balance_roots_brl': roots,
        'historical_policy': 'only_exact_reviewed_whole_individual_filing',
        'model_admission_granted': False, 'default_scope_policy_changed': False}
    record.update(
        accounts_scope='individual', document_type='DFP', period_type='annual',
        annualized=False, status='included', source_file=archive['file'],
        source_ref='cvm:DFP:2025:' + archive['sha256'])
    _require(record['debt_ratio'] == debt_ratio, 'individual_debt_ratio_mismatch')
    _check_record(record)
    # Earlier helpers used mtimes as retrieval times. Preserve that qualification
    # locally; only an actual retained transport receipt could populate this field.
    source = followon._receipt(folder, folder / receipt['file'], url=receipt['url'])
    source.update(id=receipt['id'], provider=receipt['provider'],
                  document_type=receipt['document_type'], use='explicit_whole_scope_interpretation')
    sources.append(source)
    sources.append(followon._receipt(
        folder, folder / additional.RESEARCH_FILE, url=None,
        authority='local_review_not_primary_statement'))
    for source in sources:
        if source['file'] == archive['file']:
            source['id'] = record['source_ref']
            source.update(provider='CVM', document_type='DFP', year=2025)
    _require(record['filing_id'] == latest['WIZC3']['filing_id'], 'latest_scope_filing_mismatch')
    return record, sources


def _check_record(record):
    method = record['financial_method']
    required = {f'dfp_cia_aberta_{table}_ind_2025.csv'
                for table in ('DRE', 'BPA', 'BPP', 'DFC_MI')}
    _require(_key(record) == KEY and record['accounts_scope'] == 'individual'
             and record['published_at'] == '2026-03-19'
             and record['document_type'] == 'DFP' and record['period_type'] == 'annual'
             and record['annualized'] is False and record['status'] == 'included'
             and method['accounts_scope'] == 'individual'
             and method['scope_selection'] == 'explicit_reviewed_whole_individual_scope'
             and set(method['source_statements']) == required,
             'whole_individual_scope_required')
    _require(record['profit_per_share'] == 1.25745
             and record['free_cash_per_share'] is None
             and record['debt_ratio'] == float(Decimal('688049000') / Decimal('1385841000'))
             and method['id'] == 'financial_no_industrial_fcf_v1'
             and method['profit_account'] == '3.11'
             and method['basic_eps_account'] == '3.99.01.01'
             and method['liabilities_method'] == 'total_passivo_less_individual_equity'
             and method['reported_basic_eps'] == [{
                 'share_class': 'ON', 'value': '1.25745', 'numerator_brl': '201076000',
                 'numerator_rounding_brl': '500', 'weighted_shares': '159907282',
                 'shares_rounding': '0', 'account': '3.99.01.01'}],
             'exact_reported_financial_values_required')
    _require(financial_features([record], [], CUTOFF, SPEC) is not None,
             'ordinary_financial_feature_contract_failed')


def apply_financial_scope_recheck(folder, candidates, cutoff, prepared, *, scope_requests=None):
    """Return the usual (statements, diagnostics, sources), with explicit opt-in.

    Integration point: after prepare_financials and before per-candidate feature
    selection. No request returns the original object. A failed exact proof
    raises ValueError before changing any caller-owned object. Older WIZ records
    remain in the caller's original result, but cannot enter this scoped view.
    All ordinary cash, price, five-year, training and forecast gates still apply.
    """
    if not scope_requests:
        return prepared
    _require(scope_requests == SCOPE_REQUEST, 'only_exact_explicit_wiz_scope_request_supported')
    cutoff = native._day(cutoff)
    _require(cutoff == CUTOFF, 'unreviewed_cutoff')
    issues = []
    specs = native._specs(candidates, issues)
    _require(not issues and specs.get('WIZC3') == SPEC
             and [ticker for ticker, spec in specs.items() if spec[0] == SPEC[0]] == ['WIZC3'],
             'exact_unique_wiz_candidate_identity_required')
    original, original_diagnostics, original_sources = prepared
    existing = [row for row in original if _is_wiz(row)
                and row['published_at'] <= cutoff.isoformat()]
    newest = max(((row['fiscal_end'], int(row['version'])) for row in existing), default=None)
    latest_rows = [row for row in existing
                   if (row['fiscal_end'], int(row['version'])) == newest]
    _require(len(latest_rows) == 1 and _key(latest_rows[0]) == KEY,
        'exact_unique_latest_prepared_record_required')
    record, sources = _load_wiz_scope(Path(folder), cutoff)
    _check_record(record)
    statements = [deepcopy(row) for row in original if not _is_wiz(row)] + [deepcopy(record)]
    statements.sort(key=lambda row: (row['cvm'], row['share_class'], row['fiscal_end'], int(row['version'])))
    diagnostics = deepcopy(original_diagnostics)
    diagnostics.append({
        'kind': 'financial_scope_recheck', 'ticker': 'WIZC3', 'status': 'explicit_scope_view',
        'filing_id': '155497', 'accounts_scope': 'individual',
        'prior_candidate_diagnostics_retained_as_default_policy_audit': True,
        'excluded_history': [{field: row.get(field) for field in (
            'cvm', 'share_class', 'fiscal_end', 'version', 'filing_id', 'accounts_scope')}
            for row in original if _is_wiz(row) and _key(row) != KEY],
        'reason': 'unreviewed_history_cannot_mix_with_exact_individual_scope',
        'model_admission_granted': False, 'production_integration_enabled': False})
    # Replace only the receipts whose exact byte identities were rechecked here.
    receipts = deepcopy(original_sources)
    for source in sources:
        identity = tuple(source.get(field) for field in ('file', 'sha256', 'url'))
        indices = [i for i, old in enumerate(receipts)
                   if tuple(old.get(field) for field in ('file', 'sha256', 'url')) == identity]
        if indices:
            for index in indices:
                receipts[index] = deepcopy({**receipts[index], **source, 'id': receipts[index]['id']})
        if not any(receipts[index]['id'] == source['id'] for index in indices):
            receipts.append(deepcopy(source))
    return statements, diagnostics, receipts


def _snapshot_pending(snapshot):
    pending = {row['ticker']: row['reason'] for row in snapshot['excluded_stocks']
               if row['reason'] in ('financial_features_unavailable',
                                    'native_financial_eps_source_conflict')}
    _require(set(pending) == TARGETS, 'snapshot_financial_slice_changed')
    return pending


def build_report(folder, *, test_results):
    """Recheck cached proofs and return a report, without preparing any dataset."""
    import xml.etree.ElementTree as ET
    from . import wlm_financial_class_research as wlm

    folder = Path(folder)
    snapshot = folder / 'snapshots' / SNAPSHOT
    collection = json.loads((snapshot / 'collection-report.json').read_bytes())
    pending = _snapshot_pending(collection)
    diagnostics = json.loads((snapshot / 'financial-account-audit.json').read_bytes())
    previous_bytes = (folder / PRIOR_AUDIT).read_bytes()
    _require(hashlib.sha256(previous_bytes).hexdigest() == Path(PRIOR_AUDIT).stem[6:],
             'prior_audit_hash_mismatch')
    previous = json.loads(previous_bytes)
    specs = {ticker: spec for ticker, spec in followon.SPECS.items() if ticker in TARGETS}
    latest, sources = followon.latest_guard(folder, tuple(specs))
    record, wiz_sources = _load_wiz_scope(folder, CUTOFF)
    sources.extend(wiz_sources)
    wlm_report = wlm.audit(folder, CUTOFF)
    _require(wlm_report['actual_reported_ON_EPS_reconciles_with_note'] is False
             and wlm_report['actual_reported_PN_EPS_reconciles_with_note'] is False,
             'wlm_conflict_changed_reinspect')
    sources.extend(wlm_report['sources'])
    sources.extend(followon._simh_original_sources(folder))
    for source in previous['sources']:
        if (source['file'].startswith('raw/primary-research/financials/')
                and source['file'].endswith('.pdf') and '/bsli-' not in source['file']):
            proof = followon._receipt(folder, folder / source['file'], url=source['url'])
            _require(proof['sha256'] == source['sha256'], 'prior_primary_evidence_changed')
            proof['use'] = 'previously_reviewed_supporting_or_negative_evidence'
            sources.append(proof)
    for relative in (PRIOR_AUDIT,
                     'raw/primary-research/financial-followon/README.txt',
                     'raw/primary-research/financial-followon/btg-class-eps-negative-followup-v1.json',
                     *(str(Path('raw/primary-research/financials') / name).replace('\\', '/')
                       for name in followon.PRIOR_HASHES)):
        proof = followon._receipt(folder, folder / relative, url=None,
                                  authority='local_review_not_primary_statement')
        if Path(relative).name in followon.PRIOR_HASHES:
            _require(proof['sha256'] == followon.PRIOR_HASHES[Path(relative).name],
                     'prior_manifest_changed')
        sources.append(proof)
    for filename in ('collection-report.json', 'financial-account-audit.json'):
        sources.append(followon._receipt(folder, snapshot / filename, url=None,
                                         authority='local_snapshot_audit'))
    next_proof = {
        'BPAC3': 'Exact 155648 v2 note reconciling printed basic ON EPS and weighted '
                 'class denominators to parent profit 16005964000, excluding NCI 657839000.',
        'EVEN3': 'Same consolidated annual period: gross cash acquisition outflow separated '
                 'from disposals. Positive net 12618000 cannot become zero or negative capex.',
        'PINE4': 'Exact 155965 v3 correction or clarification reconciling ON/PN allocated '
                 'numerators to parent 430515000 rather than total 432878000.',
        'SIMH3': 'Exact 155854 v1 clarification of parent 39471000 versus note 39741000 '
                 'and total/discontinued reconciliation; no scope or numerator substitution.',
        'WHRL4': 'Explicit PN basic EPS, allocated parent numerator and weighted PN denominator; '
                 'pooled ordinary labels and dividend rights do not establish PN EPS.',
    }
    findings = []
    document_prefix = {
        'BPAC3': 'bpac-', 'EVEN3': 'even-', 'PINE4': 'pine-', 'SIMH3': 'native-SIMH3-',
        'WHRL4': 'whrl-', 'WIZC3': 'wiz-native-',
        'WLMM3': 'native-' + wlm.PDF_HASH, 'WLMM4': 'native-' + wlm.PDF_HASH}
    for ticker in sorted(TARGETS):
        default_diagnostics = [row for row in diagnostics
                               if row.get('kind') == 'candidate' and row.get('ticker') == ticker]
        item = {'ticker': ticker, 'snapshot_pending_reason': pending[ticker],
                'snapshot_parser_diagnostics': default_diagnostics,
                'active_status': 'pending_unchanged', 'replacement_eps_created': False,
                'annual_archive_ref': record['source_ref'],
                'primary_evidence_refs': sorted({source['id'] for source in sources
                    if Path(source['file']).name.startswith(document_prefix[ticker])
                    and source['file'].endswith(('.pdf', '.xml'))})}
        if ticker in specs:
            prior = next(row for row in previous['findings'] if row['ticker'] == ticker)
            item.update(spec=list(specs[ticker]), latest=latest[ticker],
                        evidence_assessment=prior['reason'])
        if ticker in next_proof:
            item.update(action_taken='Rechecked latest cached master and hashed prior primary evidence; '
                                     'retained the blocker without overriding any financial field.',
                        required_next_evidence=next_proof[ticker])
        elif ticker == 'WIZC3':
            item.update(action_taken='Implemented an exact explicit whole-individual-scope adapter; '
                                     'corrected the individual liabilities metadata and isolated unreviewed history.',
                        contract_supported=True, default_consolidated_policy_selects_this=False,
                        production_integration_enabled=False, candidate_record=record,
                        source_gate_replay=followon.replay_wiz_source_gates(folder, [record]))
        else:
            share = 'ON' if ticker == 'WLMM3' else 'PNA'
            item.update(spec=[11070, share, 5],
                        latest={key: wlm_report['latest_annual'][field] for key, field in (
                            ('fiscal_end', 'DT_REFER'), ('version', 'VERSAO'),
                            ('filing_id', 'ID_DOC'), ('published_at', 'DT_RECEB'))},
                        action_taken='Reparsed the exact cached native note 32 and verified both '
                                     'published EPS arithmetic conflicts; retained the existing blocker.',
                        reported_basic_eps='2.25219' if share == 'ON' else '2.47741',
                        reported_eps_reconciles_with_note=False,
                        native_note_32=wlm_report['native_note_32'],
                        label_binding_alone_sufficient=False,
                        required_next_evidence=wlm_report['required_before_crosswalk'])
        findings.append(item)
    tests = ET.parse(test_results).getroot()
    suites = [tests] if tests.tag == 'testsuite' else list(tests.findall('testsuite'))
    counts = {field: sum(int(suite.get(field, '0')) for suite in suites)
              for field in ('tests', 'failures', 'errors', 'skipped')}
    _require(counts['tests'] > 0 and counts['failures'] == counts['errors'] == 0,
             'passing_focused_test_receipt_required')
    sources = list({(s['id'], s['file'], s['sha256'], s['url']): s for s in sources}.values())
    source_ids = {source['id'] for source in sources}
    _require(record['source_ref'] in source_ids
             and all(proof['source_ref'] in source_ids
                     for proof in record['financial_method']['primary_evidence'])
             and all(item['primary_evidence_refs']
                     and set(item['primary_evidence_refs']).issubset(source_ids) for item in findings),
             'report_source_reference_unresolved')
    return {
        'schema_version': 1, 'reviewed_on': '2026-10-25', 'reference_date': CUTOFF.isoformat(),
        'snapshot_id': SNAPSHOT, 'network_requests': 0, 'new_admissions': [],
        'dataset_preparation_or_publication_performed': False,
        'original_evidence_or_current_pointer_modified': False,
        'scope': sorted(TARGETS),
        'scope_note': 'BSLI3 is outside the active DY-positive selection; the seventh unavailable '
                      'case is WLMM4. ABCB4/CMIG4/DEXP4/ISAE4/RSUL4/TASA4 and MGLU3 belong to the parent.',
        'findings': findings,
        'integration_proposal': {
            'hook': 'apply_financial_scope_recheck(folder, candidates, cutoff, prepared, '
                    "scope_requests={'WIZC3': 'individual'})",
            'location': 'After existing prepare_financials returns; before per-candidate financial_features.',
            'no_request_behavior': 'Returns the exact original prepared object without source reads.',
            'policy_assessment': 'The general record/feature contract accepts the complete individual '
                'filing. The default consolidated-first selector does not. This is an explicit alternative '
                'methodology, not a repair of consolidated EPS or a new discovery.',
            'prior_production_decision': 'The previous financial-followon README declined production '
                'integration. This recheck implements a reviewable adapter and does not change that decision.',
            'historical_scope': 'Only the exact reviewed individual filing enters the WIZ view. '
                'No older consolidated or unreviewed individual rows enter its observations.',
            'remaining_gates': 'Unchanged five-year cash/price reconciliation, financial share basis, '
                'training filters, pooled-model validation, current quote and forecast validation.',
            'limitations': 'The replay has zero own complete training rows. No purchase quote, '
                'forecast, portfolio, settlement or point-in-time archive validation is claimed.'},
        'tests': {**counts, 'junit_file': str(Path(test_results).resolve()),
                  'junit_sha256': hashlib.sha256(Path(test_results).read_bytes()).hexdigest()},
        'sources': sources,
        'clock_policy': 'Publication dates come from native masters. Retrieval remains null without '
                        'retained transport receipts. File mtime is only a local observation.'}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--test-results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    work = Path(__file__).resolve().parent.parent / 'work/pending_recheck_20261025'
    _require(args.output.resolve().is_relative_to(work.resolve()), 'report_output_outside_recheck_work')
    report = build_report(args.folder, test_results=args.test_results)
    content = (json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as stream:
        stream.write(content)
    print(json.dumps({'report': str(args.output.resolve()), 'network_requests': 0,
                      'new_admissions': [], 'tests': report['tests']}))


if __name__ == '__main__':
    main()
