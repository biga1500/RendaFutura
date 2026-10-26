"""Read-only checks of independent sources; never promotes a modelling sample."""

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path

from .collect_market_data import write_json
from .market_inventory import load_inventory, market_root


def audit(folder=None):
    root = market_root(folder)
    data = load_inventory(root)
    stocks, bonds = data['stocks'], data['treasury']
    cutoff = date.fromisoformat(data['as_of'])
    verified = {}

    def verify_source(source):
        if not source.get('sha256') or not source.get('file'):
            return
        path = root / source['file']
        if not path.exists() and not source['file'].startswith('raw/'):
            path = root / 'raw' / source['file']
        path = path.resolve()
        assert path.is_relative_to(root), 'Source file outside market cache.'
        if path not in verified:
            verified[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        assert verified[path] == source['sha256'], 'Source content digest mismatch.'

    for source in data['sources']:
        verify_source(source)
    inventory_source_files = len(verified)
    for row in stocks:
        if row['quote'].get('value') is not None:
            assert date.fromisoformat(row['quote']['as_of'][:10]) <= cutoff
        if row['sector_code'] == 2:
            assert row['fundamentals']['fields']['free_cash_flow']['value'] is None
        if row['ticker'].endswith('11'):
            assert row['fundamentals']['fields']['earnings_per_share']['value'] is None
        assert row['payments']['cash_model_eligible'] is False
        assert row['payments']['primary_documents_are_cash_observations'] is False
        for document in row['payments']['primary_documents']:
            assert date.fromisoformat(document['published_at'][:10]) <= cutoff
            assert document['cash_model_eligible'] is False
        for field in row['fundamentals']['fields'].values():
            if field.get('published_at'):
                assert date.fromisoformat(field['published_at'][:10]) <= cutoff
    assert all(row['payments']['principal_counted_as_income'] is False for row in bonds)
    assert data['catalogue_minimum_history_months'] == data['catalogue_minimum_payments'] == 0
    assert type(data['model_sample_unchanged']) is bool
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    model_path = root / pointer['snapshot'] / 'assets.json'
    model = json.loads(model_path.read_text(encoding='utf-8'))
    for source in model.get('sources', []):
        verify_source(source)
    ready = {row['ticker'] for row in stocks if row['model_ready']}
    assert ready == {row['ticker'] for row in model['assets'] if row['asset_class'] == 'acoes'}, \
        'Inventory readiness differs from the active stock model.'
    if data.get('model_preparation'):
        assert data['model_preparation']['snapshot'] == model_path.parent.name, \
            'Inventory preparation references another model snapshot.'
    independent = [row['ticker'] for row in stocks if row['quote']['status'] == 'available'
                   and row['fundamentals']['fields']['earnings_per_share']['value'] is not None
                   and not row['payments']['event_count']]
    return {
        'passed': True, 'as_of': data['as_of'], 'collected_at': data['collected_at'],
        'counts': data['counts'], 'source_files_verified': len(verified),
        'inventory_source_files_verified': inventory_source_files,
        'additional_model_source_files_verified': len(verified) - inventory_source_files,
        'quote_statuses': dict(Counter(row['quote']['status'] for row in stocks)),
        'payment_statuses': dict(Counter(row['payments']['status'] for row in stocks)),
        'independent_fields_without_payment_rows': independent,
        'treasury_offers_below_61_months_retained': sum(row['history']['observed_months'] < 61 for row in bonds),
        'treasury_coupon_statuses': dict(Counter(row['payments']['status'] for row in bonds)),
        'primary_sources': [source for source in data['sources'] if source.get('authority') == 'primary'
                            or source.get('source_id')],
        'model_snapshot_unchanged': pointer['snapshot'], 'model_sample_unchanged': data['model_sample_unchanged'],
        'model_reference': model['reference_date'],
        'model_assets': len(model['assets']), 'limitations': data['limitations'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.data_dir)
    write_json(args.output, result)
    print(json.dumps({key: result[key] for key in ('passed', 'counts', 'source_files_verified', 'model_assets')}))
