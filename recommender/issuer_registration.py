"""Current issuer registration evidence; never an exchange trading-status feed."""

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request

from .collect_market_data import ROOT, normalize, write_json
from .market_inventory import load_inventory, market_root, publish_inventory

SOURCE_URL = 'https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv'
DOCUMENTATION_URL = 'https://dados.cvm.gov.br/dataset/cia_aberta-cad'
MAX_BYTES = 8_000_000
FIELDS = ('CD_CVM', 'DENOM_SOCIAL', 'SIT', 'DT_INI_SIT', 'DT_CANCEL', 'MOTIVO_CANCEL')


def _date(value):
    if not value:
        return None
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('invalid_registration_date')
    return parsed


def parse_registration(content):
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ValueError('registration_size_limit')
    reader = csv.DictReader(io.StringIO(content.decode('latin1')), delimiter=';')
    if not set(FIELDS).issubset(reader.fieldnames or []):
        raise ValueError('registration_schema_changed')
    records = {}
    for row in reader:
        if None in row or any(row.get(key) is None for key in FIELDS):
            raise ValueError('registration_row_misaligned')
        if not re.fullmatch(r'\d{1,9}', row['CD_CVM']):
            raise ValueError('invalid_registration_CVM_code')
        cvm = int(row['CD_CVM'])
        if cvm <= 0:
            raise ValueError('invalid_registration_CVM_code')
        _date(row['DT_INI_SIT'])
        _date(row['DT_CANCEL'])
        # Only issuer fields enter the app. Contact/address columns are not used.
        record = {key: row[key] for key in FIELDS}
        if cvm in records and records[cvm] != record:
            raise ValueError('conflicting_registration_rows')
        records[cvm] = record
    if not records:
        raise ValueError('empty_registration')
    return records


def registration_status(records, cvm, as_of, *, observed_on):
    """Interpret a dated current register, not a point-in-time archived register."""
    as_of, observed_on = _date(as_of) if isinstance(as_of, str) else as_of, _date(observed_on) if isinstance(observed_on, str) else observed_on
    if type(as_of) is not date or type(observed_on) is not date or as_of > observed_on:
        raise ValueError('invalid_registration_reference')
    row = records.get(cvm) if type(cvm) is int else None
    result = {'status': 'unresolved', 'issuer_cvm': cvm, 'as_of': as_of.isoformat(),
        'observed_on': observed_on.isoformat(), 'registration_status': None,
        'status_since': None, 'cancelled_on': None, 'cancellation_reason': None,
        'registration_blocks_current_purchase': False, 'exchange_trading_verified': False,
        'source_url': SOURCE_URL, 'documentation_url': DOCUMENTATION_URL,
        'basis': 'current_CVM_issuer_register_not_exchange_membership'}
    if row is None:
        return result
    since, cancelled = _date(row['DT_INI_SIT']), _date(row['DT_CANCEL'])
    result.update(registration_status=row['SIT'], status_since=row['DT_INI_SIT'] or None,
        cancelled_on=row['DT_CANCEL'] or None, cancellation_reason=row['MOTIVO_CANCEL'] or None)
    normalized = normalize(row['SIT']).strip()
    if cancelled is not None and cancelled <= as_of and normalized == 'cancelada':
        result.update(status='cancelled_issuer_registration', registration_blocks_current_purchase=True)
    elif since is None or since > as_of:
        result['status'] = 'current_status_not_established_at_reference'
    elif normalized == 'ativo' and cancelled is None:
        result['status'] = 'active_issuer_registration_not_trading_confirmation'
    elif normalized.startswith('suspens'):
        result.update(status='suspended_issuer_registration', registration_blocks_current_purchase=True)
    return result


def load_registration(folder):
    folder = market_root(folder)
    manifest = folder / 'raw/primary-research/registration/current.json'
    receipt = json.loads(manifest.read_text(encoding='utf-8'))
    name = receipt.get('file', '')
    if not re.fullmatch(r'raw/primary-research/registration/cadastro-[0-9a-f]{64}\.csv', name):
        raise ValueError('unsafe_registration_cache')
    path = folder / name
    if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > MAX_BYTES:
        raise ValueError('unsafe_registration_cache')
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if receipt.get('url') != SOURCE_URL or receipt.get('sha256') != digest or path.stem != 'cadastro-' + digest:
        raise ValueError('registration_integrity_mismatch')
    return parse_registration(body), receipt


def attach_registration(inventory, records, receipt):
    observed = datetime.fromisoformat(receipt['retrieved_at']).date()
    stocks = [{**row, 'issuer_registration': registration_status(records, row.get('cvm'),
        inventory['as_of'], observed_on=observed)} for row in inventory['stocks']]
    sources = [row for row in inventory.get('sources', []) if row.get('role') != receipt['role']]
    return {**inventory, 'stocks': stocks, 'sources': [*sources, receipt]}


def collect(folder, *, activate_inventory=False):
    folder = market_root(folder)
    request = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'RendaFutura-Academic/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.url != SOURCE_URL:
            raise ValueError('unexpected_registration_redirect')
        body = response.read(MAX_BYTES + 1)
    records = parse_registration(body)
    digest = hashlib.sha256(body).hexdigest()
    target = folder / 'raw/primary-research/registration'
    target.mkdir(parents=True, exist_ok=True)
    path = target / ('cadastro-' + digest + '.csv')
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(body)
    elif path.read_bytes() != body:
        raise ValueError('registration_cache_conflict')
    now = datetime.now(timezone.utc)
    receipt = {'file': path.relative_to(folder).as_posix(), 'sha256': digest, 'bytes': len(body),
        'url': SOURCE_URL, 'retrieved_at': now.isoformat(), 'authority': 'primary', 'provider': 'CVM CAD',
        'role': 'current_issuer_registration_not_exchange_trading'}
    write_json(target / 'current.json', receipt)
    inventory = load_inventory(folder)
    rows = [{'ticker': row['ticker'], **registration_status(records, row.get('cvm'), now.date(), observed_on=now.date())}
            for row in inventory['stocks']]
    report = {'schema_version': 1, 'observed_at': now.isoformat(), 'source': receipt, 'stocks': rows,
        'active_model_changed': False, 'missing_or_active_register_is_not_trading_confirmation': True}
    write_json(target / ('audit-' + now.strftime('%Y%m%dT%H%M%S') + '.json'), report)
    if activate_inventory:
        publish_inventory(folder, attach_registration(inventory, records, receipt))
    print(json.dumps({'issuers': len(records), 'evaluated_stocks': len(rows),
        'cancelled_stocks': [row['ticker'] for row in rows if row['status'] == 'cancelled_issuer_registration'],
        'suspended_stocks': [row['ticker'] for row in rows if row['status'] == 'suspended_issuer_registration']}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--publish-inventory', action='store_true')
    args = parser.parse_args()
    collect(args.data_dir, activate_inventory=args.publish_inventory)
