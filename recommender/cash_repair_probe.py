"""Offline inspection-hook experiment; ordinary gates, no admission or publication."""

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch

from . import inventory_candidates as bridge
from .collect_market_data import financial_features, normalize, shift_month, stock_observations
from .broad_stocks import br_day, split_price_history
from .cvm_proventos_documents import gross
from .inventory_financials import prepare_financials
from .market_inventory import load_inventory
from .share_basis import b3_evidence, factor_after


def _json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def cash_comparison(cache, identity, cutoff, parsed):
    """Audit the unchanged comparator; never substitutes its numbers into repairs."""
    detail = cache.json(f'raw/expanded/b3-detail-{identity["cvm"]}.json', 'repair_probe_comparator_identity')
    codes = {r['code']: r['isin'] for r in detail['otherCodes']}
    raw = bridge._cash_pages(identity['ticker'], codes, identity['cvm'], cache)
    expected, _ = b3_evidence(raw, identity['share_class'])
    content, _ = cache.read('raw/expanded/' + identity['ticker'] + '-prices.json', 'repair_probe_share_actions')
    _, actions = split_price_history(content, identity['ticker'], cutoff, allow_recent_actions=True)
    groups, output = defaultdict(list), []
    for row in parsed['events']:
        groups[row['record_date']].append(row)
    start = shift_month(cutoff, -60)
    for record in sorted(set(groups) | {day.isoformat() for day in expected}):
        day = date.fromisoformat(record)
        events = groups.get(record, [])
        if day < start and not any(start <= date.fromisoformat(r['payment_date']) <= cutoff for r in events):
            continue
        if day > cutoff:
            continue
        reported = sum(r['gross_per_share'] for r in events)
        declared = expected.get(day)
        factors = {1., *(factor_after(day, actions, end) for end in
            [cutoff, *[date.fromisoformat(a['date']) for a in actions]] if end >= day)}
        comparator_rows = [r for r in raw if r.get('typeStock', '').strip() == identity['share_class']
            and br_day(r['lastDatePriorEx']) == day
            and normalize(r.get('corporateAction', '')) in ('dividendo', 'jrs cap proprio')]
        decimal_amount = sum((gross(r['valueCash']) / gross(r.get('quotedPerShares') or '1') for r in comparator_rows), start=0)
        matches = declared is not None and any(math.isclose(reported, declared / factor,
            rel_tol=.002, abs_tol=.00002) for factor in factors)
        output.append({'record_date': record, 'inspected_reported_sum': reported,
            'independent_comparator_sum_decimal': str(decimal_amount), 'native_raw_comparator_rows': comparator_rows,
            'share_factors_checked': sorted(factors), 'normal_comparator_amount_match': matches,
            'inspected_rows': [{k: row.get(k) for k in ('source_row', 'label', 'payment_date',
                'gross_per_share', 'gross_per_share_decimal', 'exact_cash_repair_id')} for row in events]})
    return {'stage': 'inspected_view_before_normal_research_enrichment', 'groups': output,
        'amount_mismatches': [r for r in output if not r['normal_comparator_amount_match']],
        'comparator_numbers_modified': False, 'source_or_gate_override': False}


def probe(folder, ticker, repair, *, cutoff=date(2026, 9, 30)):
    folder = Path(folder).resolve()
    inventory = load_inventory(folder)
    identity = next(row for row in inventory['stocks'] if row['ticker'] == ticker)
    cache = bridge._Cache(folder, inventory)
    checks, views = {}, []
    inspector = bridge.inspect_payment_page

    def inspected(body, code, reference, observed_at):
        parsed = inspector(body, code, reference, observed_at)
        if code != ticker:
            return parsed
        detail = cache.json(f'raw/expanded/b3-detail-{identity["cvm"]}.json', 'exact_repair_probe_identity')
        isin = next(r['isin'] for r in detail['otherCodes'] if r['code'] == ticker)
        result = repair(parsed, {**identity, 'isin': isin}, reference, cache)
        views.append(result)
        return result

    candidate, error = None, None
    with patch.object(bridge, 'inspect_payment_page', inspected):
        try:
            candidate = bridge._one(identity, cache, cutoff, checks)
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exception:
            error = {'type': type(exception).__name__, 'reason': str(exception)}
    statements, diagnostics, sources = prepare_financials(folder, [identity], cutoff)
    financial = {'normal_prepare_financials_called': True, 'statement_count': len(statements),
        'diagnostics': diagnostics, 'sources': sources, 'features_gate': 'not_reached_without_source_candidate',
        'training_gate': 'not_reached_without_source_candidate'}
    if candidate:
        spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
        current = financial_features(statements, candidate['payments'], cutoff, spec)
        basis = date.fromisoformat(candidate['financial_basis_start'])
        valid = current is not None and date.fromisoformat(current[1]['fiscal_end']) >= basis
        financial.update(features_gate='pass' if valid else 'financial_features_unavailable',
            financial_basis_start=basis, latest_features=current[0] if current else None,
            latest_filing=current[1] if current else None)
        if valid:
            training, coverage = stock_observations(ticker, candidate['prices'], candidate['payments'],
                                                    statements, cutoff, spec=spec)
            training = [r for r in training if date.fromisoformat(r['as_of']) >= candidate['training_since']
                        and date.fromisoformat(r['fiscal_end']) >= basis]
            financial.update(training_gate='ordinary_prepare_inventory_filter_applied',
                training_observations=len(training), training_coverage=coverage,
                training_since=candidate['training_since'],
                training_rows_as_of=[r['as_of'] for r in training])
    comparison = cash_comparison(cache, identity, cutoff, views[-1]) if views else None
    return {'schema': 'exact-cash-repair-probe-v1', 'ticker': ticker, 'reference': cutoff,
        'actual_probe_at': datetime.now(timezone.utc), 'patch_scope': 'inspect_payment_page_return_only',
        'source_candidate_pass': candidate is not None, 'source_error': error, 'checks': checks,
        'parsed_views': views, 'financial': financial,
        'cash_comparison_audit': comparison,
        'all_ordinary_candidate_and_financial_gates_pass': candidate is not None
            and financial['features_gate'] == 'pass',
        'source_receipts': cache.all_receipts(), 'model_admitted': False,
        'model_fit_or_publication_attempted': False, 'source_bytes_modified': False,
        'training_interpretation': 'Ordinary prepare_inventory has no per-stock minimum; zero rows is not a training-data recovery claim.',
        'independent_comparator_or_gate_patched': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--ticker', choices=('SANB4', 'RADL3'), default='SANB4')
    args = parser.parse_args()
    if args.ticker == 'SANB4':
        from .sanb4_cash_repair import apply_exact_repairs
    else:
        from .radl_cash_repair import apply_exact_repairs
    report = probe(args.folder, args.ticker, apply_exact_repairs)
    body = (json.dumps(report, default=_json, sort_keys=True, indent=2) + '\n').encode()
    digest = hashlib.sha256(body).hexdigest()
    output = args.folder / 'raw/primary-research/all-pending/cash' / (args.ticker + '-repair-probe-' + digest + '.json')
    with output.open('xb') as stream:
        stream.write(body)
    print(json.dumps({'file': str(output), 'sha256': digest, 'source_pass': report['source_candidate_pass'],
        'source_error': report['source_error'], 'financial_features_gate': report['financial']['features_gate'],
        'training_observations': report['financial'].get('training_observations'),
        'all_ordinary_gates_pass': report['all_ordinary_candidate_and_financial_gates_pass'],
        'model_admitted': False}))


if __name__ == '__main__':
    main()
