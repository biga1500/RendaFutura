"""Explain exact cash discrepancies without altering or repairing source values."""

import argparse
from collections import defaultdict
from datetime import date
import json
import math
from pathlib import Path

from .collect_market_data import ROOT, shift_month, write_json
from .inventory_candidates import _Cache, _cash_pages, _identity, _payments
from .market_inventory import load_inventory, market_root
from .share_basis import b3_evidence, factor_after


def discrepancies(original, expected, actions, cutoff):
    start = shift_month(cutoff, -60)
    groups = defaultdict(list)
    for row in original.installments:
        groups[date.fromisoformat(row['record_date'])].append(row)
    required = {day for day in expected if start <= day <= cutoff}
    required.update(day for day, rows in groups.items()
        if any(start <= date.fromisoformat(row['payment_date']) <= cutoff for row in rows))
    output = []
    for day in sorted(required):
        rows = groups.get(day, [])
        reported = math.fsum(row['gross_per_unit'] for row in rows)
        declared = expected.get(day)
        factors = {1., factor_after(day, actions, cutoff)}
        factors.update(factor_after(day, actions, date.fromisoformat(action['date']))
            for action in actions if day <= date.fromisoformat(action['date']) <= cutoff)
        matches = [factor for factor in sorted(factors) if declared is not None
            and math.isclose(reported, declared / factor, rel_tol=.002, abs_tol=.00002)]
        if not rows or declared is None or not matches:
            output.append({'record_date': day.isoformat(), 'reported_installment_sum': reported if rows else None,
                'independent_declared_amount': declared,
                'reported_to_declared_ratio': reported / declared if declared else None,
                'verified_share_factors': sorted(factors),
                'reason': 'calendar_group_missing' if not rows else 'independent_group_missing' if declared is None
                    else 'amount_not_explained_by_documented_share_factors',
                'installments': [{key: row.get(key) for key in ('payment_date', 'gross_per_unit', 'label',
                    'source_row', 'reported_status', 'published_at')} for row in rows]})
    return output


def audit(folder):
    folder = market_root(folder)
    inventory = load_inventory(folder)
    pointer = json.loads((folder / 'current.json').read_text(encoding='utf-8'))
    metadata = json.loads((folder / pointer['snapshot'] / 'assets.json').read_text(encoding='utf-8'))
    preparation = metadata['stock_universe_preparation']
    tickers = {row['ticker'] for row in preparation['stocks']
        if any(code in row['reason_codes'] for code in ('cash_amount_or_share_scale_disagrees_with_B3',
                                                       'B3_cash_event_missing_from_payment_table'))}
    cutoff = date.fromisoformat(metadata['reference_date'])
    cache, results = _Cache(folder, inventory), []
    for row in inventory['stocks']:
        ticker = row['ticker']
        if ticker not in tickers:
            continue
        try:
            _, codes = _identity(row, cache, cutoff)
            expected, _ = b3_evidence(_cash_pages(ticker, codes, row['cvm'], cache), row['share_class'])
            original, _ = _payments(ticker, cache, cutoff)
            legacy = folder / f'raw/expanded/{ticker}-prices.json'
            price_path = legacy if legacy.is_file() else folder / f'raw/inventory/daily-prices/{ticker}.json'
            events = json.loads(price_path.read_text(encoding='utf-8'))['chart']['result'][0].get('events', {})
            from datetime import datetime, timezone
            actions = [{'date': datetime.fromtimestamp(event['date'], timezone.utc).date().isoformat(),
                        'ratio': event['numerator'] / event['denominator']}
                       for event in events.get('splits', {}).values()]
            results.append({'ticker': ticker, 'discrepancies': discrepancies(original, expected, actions, cutoff)})
        except (ValueError, OSError, KeyError, TypeError, IndexError):
            results.append({'ticker': ticker, 'status': 'source_checks_incomplete'})
    return {'reference': cutoff.isoformat(), 'stocks': results, 'source_values_modified': False,
            'net_to_gross_conversion_applied': False, 'active_model_changed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.data_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.output, result)
    print(json.dumps({'stocks': len(result['stocks']), 'discrepancies': sum(len(row.get('discrepancies', []))
        for row in result['stocks']), 'output': str(args.output)}), flush=True)
