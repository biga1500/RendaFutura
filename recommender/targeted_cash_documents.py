"""Bounded CVM acquisition for exact dates disputed by the cash-calendar audit.

This creates immutable source receipts only. It does not change calendars,
asset admission, inventories or model snapshots.
"""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error

from .cash_calendar_research import acquire_cvm_document
from .cvm_proventos_documents import discover
from .market_inventory import load_inventory, market_root


def select_documents(documents, mismatches, cutoff, *, radius_days=14):
    if type(cutoff) is not date or type(radius_days) is not int or not 0 <= radius_days <= 31:
        raise ValueError('invalid_target_document_window')
    if not isinstance(mismatches, list) or len(mismatches) > 2000:
        raise ValueError('invalid_cash_mismatch_audit')
    targets = {}
    for row in mismatches:
        cvm = row.get('cvm')
        if type(cvm) is not int or cvm <= 0:
            raise ValueError('exact_target_CVM_required')
        days = {date.fromisoformat(item['record_date']) for item in row.get('mismatches', [])}
        if any(day > cutoff for day in days):
            raise ValueError('target_record_date_after_cutoff')
        targets.setdefault(cvm, set()).update(days)
    selected = {}
    for document in documents:
        cvm = document['cvm']
        reference = document.get('reference_date')
        if type(cvm) is not int or cvm not in targets or not reference:
            continue
        if date.fromisoformat(document['published_at']) > cutoff:
            continue
        day = date.fromisoformat(reference)
        if not any(abs((day - target).days) <= radius_days for target in targets[cvm]):
            continue
        selected[document['url']] = document
    return sorted(selected.values(), key=lambda row: (
        row['published_at'], int(row['sequence']), row['version']), reverse=True)


def acquire_targets(folder, mismatches, cutoff, *, max_documents=24, offset=0):
    if type(max_documents) is not int or not 1 <= max_documents <= 32:
        raise ValueError('target_acquisition_bound_required')
    if type(offset) is not int or not 0 <= offset <= 2000:
        raise ValueError('invalid_target_acquisition_offset')
    folder = market_root(folder)
    documents = select_documents(discover(folder, load_inventory(folder), cutoff), mismatches, cutoff)
    results = []
    for document in documents[offset:offset + max_documents]:
        try:
            parsed, source = acquire_cvm_document(folder, document, cutoff)
            result = {'sequence': document['sequence'], 'cvm': document['cvm'],
                'reference_date': document['reference_date'], 'status': 'source_acquired',
                'events': parsed['events'], 'issues': parsed['issues'], 'source': source}
        except urllib.error.HTTPError as error:
            result = {'sequence': document['sequence'], 'cvm': document['cvm'],
                'status': 'source_http_error', 'http_status': error.code, 'url': document['url']}
            results.append(result)
            if error.code in (401, 403, 429):
                break
            continue
        except (OSError, ValueError) as error:
            result = {'sequence': document['sequence'], 'cvm': document['cvm'],
                'status': 'source_unusable', 'reason': str(error)}
        results.append(result)
        print(json.dumps({key: result[key] for key in ('sequence', 'cvm', 'status')}), flush=True)
        time.sleep(.6)
    report = {'as_of': cutoff.isoformat(), 'created_at': datetime.now(timezone.utc).isoformat(),
        'matching_documents': len(documents), 'acquisition_bound': max_documents, 'offset': offset,
        'results': results, 'active_model_changed': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False}
    content = json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8')
    target = folder / 'raw/primary-research/cash-calendars' / (
        'targeted-' + hashlib.sha256(content).hexdigest() + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as stream:
        stream.write(content)
    return target, report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--cutoff', type=date.fromisoformat, required=True)
    parser.add_argument('--max-documents', type=int, default=24)
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    if args.audit.is_symlink() or args.audit.stat().st_size > 2_000_000:
        raise ValueError('unsafe_cash_audit_input')
    audit = json.loads(args.audit.read_text(encoding='utf-8'))
    path, report = acquire_targets(args.data_dir, audit, args.cutoff,
        max_documents=args.max_documents, offset=args.offset)
    print(json.dumps({'report': str(path), 'matching_documents': report['matching_documents'],
        'attempted_documents': len(report['results'])}), flush=True)
