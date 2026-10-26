"""User-selected stock universe, separate from cash histories and model inputs."""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid


RULE = 'user_supplied_snapshot_dividend_yield_strictly_positive_v1'
OUTSIDE_REASON = 'outside_user_positive_dividend_yield_scope'
MAX_BYTES = 2_000_000


def _root(folder):
    root = Path(folder).resolve()
    return root.parent.parent if root.parent.name == 'snapshots' else root


def parse_snapshot(content):
    if not isinstance(content, bytes) or not 1 <= len(content) <= MAX_BYTES:
        raise ValueError('invalid_dividend_scope_source_size')
    lines = content.decode('utf-8-sig').splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith('Papel\t')]
    if len(starts) != 1:
        raise ValueError('unique_dividend_scope_header_required')
    table = list(csv.reader(io.StringIO('\n'.join(lines[starts[0]:])), delimiter='\t'))
    headers = table[0]
    if len(headers) != len(set(headers)) or not {'Papel', 'Div.Yield'}.issubset(headers):
        raise ValueError('invalid_dividend_scope_columns')
    records, seen, selected, zero, negative = [], set(), [], [], []
    for cells in table[1:]:
        if not cells:
            continue
        if len(cells) > len(headers):
            raise ValueError('misaligned_dividend_scope_row')
        row = dict(zip(headers, cells))
        missing = [name for name in headers if name not in row or row[name] == '']
        ticker, raw = row.get('Papel'), row.get('Div.Yield')
        if (not isinstance(ticker, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}[A-Z]?', ticker)
                or ticker in seen):
            raise ValueError('invalid_or_duplicate_dividend_scope_ticker')
        if not isinstance(raw, str) or not re.fullmatch(r'-?\d+(?:\.\d{3})*,\d+%', raw):
            raise ValueError('missing_or_invalid_dividend_yield_not_zero')
        percent = Decimal(raw[:-1].replace('.', '').replace(',', '.'))
        seen.add(ticker)
        records.append({'ticker': ticker, 'dividend_yield_percent': str(percent),
                        'reported_fields': {name: row.get(name) or None for name in headers},
                        'missing_fields': missing})
        (selected if percent > 0 else zero if percent == 0 else negative).append(ticker)
    if not records or len(records) > 2000 or not selected:
        raise ValueError('positive_dividend_scope_is_empty_or_oversized')
    return {'headers': headers, 'records': records, 'selected_tickers': selected,
            'zero_yield_tickers': zero, 'negative_yield_tickers': negative,
            'source_rows': len(records), 'selected_count': len(selected)}


def _json(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def _immutable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError('immutable_dividend_scope_source_changed')
    else:
        with path.open('xb') as stream:
            stream.write(content)


def import_scope(folder, source_path):
    root = _root(folder)
    content = Path(source_path).read_bytes()
    parsed = parse_snapshot(content)
    source_hash = hashlib.sha256(content).hexdigest()
    source_file = 'sources/' + source_hash + '.tsv'
    manifest = {'schema_version': 1, 'rule': RULE, **parsed,
                'source_sha256': source_hash, 'source_file': source_file,
                'source_date': None, 'source_url': None,
                'provenance': 'user_supplied_table_original_bytes_preserved',
                'source_date_is_import_date': False,
                'current_universe_selection_not_point_in_time_backtest': True,
                'dividend_yield_is_not_a_payment_history': True,
                'treasury_scope_unchanged': True}
    encoded = _json(manifest)
    digest = hashlib.sha256(encoded).hexdigest()
    base = root / 'stock-selection'
    _immutable(base / source_file, content)
    relative = 'manifests/' + digest + '.json'
    _immutable(base / relative, encoded)
    pointer = _json({'schema_version': 1, 'manifest': relative, 'sha256': digest,
                     'imported_at': datetime.now(timezone.utc).isoformat()})
    temporary = base / ('.pointer-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(pointer)
        os.replace(temporary, base / 'current.json')
    finally:
        temporary.unlink(missing_ok=True)
    return load_scope(root)


def _read(base, relative, expected, pattern):
    if not isinstance(relative, str) or not re.fullmatch(pattern, relative):
        raise ValueError('unsafe_dividend_scope_path')
    path = base / relative
    if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()) or path.stat().st_size > MAX_BYTES:
        raise ValueError('unsafe_dividend_scope_file')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError('dividend_scope_hash_mismatch')
    return content


def load_scope(folder):
    base = _root(folder) / 'stock-selection'
    pointer_path = base / 'current.json'
    if not pointer_path.exists():
        return None
    if pointer_path.is_symlink() or pointer_path.stat().st_size > 10_000:
        raise ValueError('unsafe_dividend_scope_pointer')
    try:
        pointer = json.loads(pointer_path.read_bytes())
        if pointer.get('schema_version') != 1 or not re.fullmatch(r'[0-9a-f]{64}', str(pointer.get('sha256'))):
            raise ValueError('invalid_dividend_scope_pointer')
        content = _read(base, pointer.get('manifest'), pointer['sha256'], r'manifests/[0-9a-f]{64}\.json')
        manifest = json.loads(content)
        if (manifest.get('schema_version') != 1 or manifest.get('rule') != RULE
                or manifest.get('treasury_scope_unchanged') is not True):
            raise ValueError('invalid_dividend_scope_manifest')
        raw = _read(base, manifest.get('source_file'), manifest.get('source_sha256'), r'sources/[0-9a-f]{64}\.tsv')
        reparsed = parse_snapshot(raw)
        if any(manifest.get(key) != value for key, value in reparsed.items()):
            raise ValueError('dividend_scope_selection_does_not_match_source')
        if manifest.get('source_date') is not None or manifest.get('source_date_is_import_date') is not False:
            raise ValueError('unverified_dividend_scope_source_date')
        return {**manifest, 'id': pointer['sha256']}
    except (OSError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError('dividend_scope_unavailable_no_unrestricted_fallback') from error


def allowed_tickers(folder):
    scope = load_scope(folder)
    return frozenset(scope['selected_tickers']) if scope else None


def filter_records(folder, records):
    allowed = allowed_tickers(folder)
    return list(records) if allowed is None else [row for row in records if row['ticker'] in allowed]


def scope_metadata(folder):
    scope = load_scope(folder)
    if scope is None:
        return None
    return {'id': scope['id'], 'rule': RULE, 'sourceSHA256': scope['source_sha256'],
            'sourceDate': None, 'sourceRows': scope['source_rows'],
            'selectedTickersCount': scope['selected_count'],
            'zeroYieldTickersCount': len(scope['zero_yield_tickers']),
            'negativeYieldTickersCount': len(scope['negative_yield_tickers']),
            'selectedTickers': scope['selected_tickers'], 'treasuryUnchanged': True,
            'dividendYieldIsPaymentHistory': False,
            'selectionIsPointInTimeBacktest': False}


def scope_evidence(folder):
    scope = load_scope(folder)
    if scope is None:
        return []
    base = _root(folder) / 'stock-selection'
    return [{'kind': kind, 'id': digest, 'policy': RULE,
             'sourceDate': None, 'sourceSHA256': scope['source_sha256'],
             'file': 'stock-selection/' + relative, 'sha256': digest,
             'bytes': (base / relative).stat().st_size, 'url': None,
             'retrieved_at': None, 'source_date': None,
             'authority': 'user_supplied_not_independently_verified',
             'provider': 'Arquivo fornecido pelo usuario'}
            for kind, relative, digest in (
                ('stock_selection', 'manifests/' + scope['id'] + '.json', scope['id']),
                ('stock_selection_source', scope['source_file'], scope['source_sha256']))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    scope = import_scope(args.data_dir, args.input)
    print(json.dumps({'source_rows': scope['source_rows'], 'positive_yield': scope['selected_count'],
                      'zero_yield': len(scope['zero_yield_tickers']), 'id': scope['id']}))


if __name__ == '__main__':
    main()
