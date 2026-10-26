import json

import pytest

from .market_inventory import load_inventory, publish_inventory


def record():
    return {'schema_version': 1, 'as_of': '2026-10-24', 'stocks': [
        {'ticker': 'TEST3', 'quote': {'value': 10, 'status': 'available'},
         'payments': {'status': 'not_found', 'events': []},
         'fundamentals': {'fields': {'earnings_per_share': {'value': 2}}},
         'history': {'months': 2}, 'model_ready': False}], 'treasury': []}


def test_partial_components_are_preserved_without_zero_imputation(tmp_path):
    original = record()
    publish_inventory(tmp_path, original)
    loaded = load_inventory(tmp_path)
    assert loaded == original
    assert loaded['stocks'][0]['payments']['status'] == 'not_found'
    assert loaded['stocks'][0]['quote']['value'] == 10


def test_inventory_never_changes_modelling_pointer(tmp_path):
    model = tmp_path / 'current.json'
    model.write_text('{"model":"unchanged"}', encoding='utf-8')
    publish_inventory(tmp_path, record())
    assert json.loads(model.read_text()) == {'model': 'unchanged'}


def test_inventory_loads_alongside_an_explicit_model_snapshot(tmp_path):
    publish_inventory(tmp_path, record())
    assert load_inventory(tmp_path / 'snapshots/model-one') == record()


def test_pointer_traversal_and_modified_snapshot_are_rejected(tmp_path):
    snapshot = publish_inventory(tmp_path, record())
    (snapshot / 'market.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='integridade'):
        load_inventory(tmp_path)
    pointer = tmp_path / 'inventory/current.json'
    pointer.write_text('{"schema_version":1,"snapshot":"../outside"}', encoding='utf-8')
    with pytest.raises(ValueError, match='Ponteiro'):
        load_inventory(tmp_path)


def test_duplicate_symbols_and_nonfinite_values_are_rejected(tmp_path):
    value = record()
    value['stocks'].append(value['stocks'][0].copy())
    with pytest.raises(ValueError):
        publish_inventory(tmp_path, value)
    value = record()
    value['stocks'][0]['quote']['value'] = float('nan')
    with pytest.raises(ValueError):
        publish_inventory(tmp_path, value)
