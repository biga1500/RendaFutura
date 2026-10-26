"""Immutable per-field market inventory, independent of the modelling sample."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .collect_market_data import ROOT

MAX_BYTES = 20 * 1024 * 1024


def market_root(folder=None):
    path = Path(folder or os.environ.get('RF_DATA_DIR', ROOT / 'recommender/data/market')).resolve()
    return path.parent.parent if path.parent.name == 'snapshots' else path


def _validate(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Inventario de mercado invalido.')
    for section in ('stocks', 'treasury'):
        rows = value.get(section)
        if not isinstance(rows, list) or len(rows) > 2000 or any(not isinstance(r, dict) for r in rows):
            raise ValueError('Lista de instrumentos invalida.')
    symbols = [r.get('ticker') for r in value['stocks']]
    if any(not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9.-]{1,30}', s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('Codigos ausentes ou duplicados no inventario.')
    for row in value['stocks']:
        if not all(isinstance(row.get(field), dict) for field in ('quote', 'payments', 'fundamentals', 'history')):
            raise ValueError('Componentes independentes ausentes no inventario.')
    json.dumps(value, allow_nan=False)
    return value


def publish_inventory(folder, inventory):
    inventory = _validate(inventory)
    base = market_root(folder) / 'inventory'
    identity = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    snapshot = base / 'snapshots' / identity
    snapshot.mkdir(parents=True)
    content = json.dumps(inventory, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8')
    if len(content) > MAX_BYTES:
        raise ValueError('Inventario excede o limite de tamanho.')
    (snapshot / 'market.json').write_bytes(content)
    pointer = {'schema_version': 1, 'snapshot': 'snapshots/' + identity,
               'sha256': hashlib.sha256(content).hexdigest(),
               'as_of': inventory.get('as_of'), 'collected_at': inventory.get('collected_at'),
               'counts': inventory.get('counts', {})}
    temporary = base / ('.pointer-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(pointer), encoding='utf-8')
        os.replace(temporary, base / 'current.json')
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


def load_inventory(folder=None):
    base = market_root(folder) / 'inventory'
    pointer = json.loads((base / 'current.json').read_text(encoding='utf-8'))
    relative = pointer.get('snapshot')
    if pointer.get('schema_version') != 1 or not isinstance(relative, str) or not re.fullmatch(r'snapshots/[A-Za-z0-9-]+', relative):
        raise ValueError('Ponteiro de inventario invalido.')
    path = (base / relative / 'market.json').resolve()
    if not path.is_relative_to((base / 'snapshots').resolve()) or path.stat().st_size > MAX_BYTES:
        raise ValueError('Arquivo de inventario fora do limite.')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != pointer.get('sha256'):
        raise ValueError('A integridade do inventario nao confere.')
    return _validate(json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Numero nao finito.'))))


def inventory_summary(folder=None):
    base = market_root(folder) / 'inventory'
    try:
        pointer = json.loads((base / 'current.json').read_text(encoding='utf-8'))
        if pointer.get('schema_version') != 1 or not isinstance(pointer.get('counts'), dict):
            return None
        return {'asOf': pointer.get('as_of'), 'collectedAt': pointer.get('collected_at'), 'counts': pointer['counts']}
    except (OSError, ValueError):
        return None
