"""Download the six literal primary notices needed for Itausa share-basis review."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

from .dividend_scope import allowed_tickers
from .itausa_primary_actions import build_report
from .published_issuer_quotes import ROOT, MAX_BYTES, _NoRedirect
from .selected_issuer_sources import _dump, _require, _save, source_body


CACHE = 'raw/primary-research/itausa-action-documents'


def acquire(root=ROOT):
    root = Path(root).resolve()
    _require(bool((allowed_tickers(root) or set()) & {'ITSA3', 'ITSA4'}), 'Itausa_outside_selected_scope')
    report = build_report(root)
    documents = sorted(report['missing_documents'] + report['pending_document_reviews'], key=lambda row: row['event_id'])
    _require(len(documents) == len({row['url'] for row in documents}) == 6, 'exact_six_primary_action_notices_required')
    robot = json.loads((root / 'raw/primary-research/selected-issuer-sources/response-006.json').read_bytes())
    _require(robot['url'] == 'https://api.mziq.com/robots.txt' and robot['status'] == 404
             and robot['completed_at'][:10] == datetime.now(timezone.utc).date().isoformat(),
             'fresh_provider_robots_observation_required')
    source_body(root, robot)
    base = root / CACHE
    base.mkdir(parents=True, exist_ok=True)
    _require(not base.is_symlink() and base.resolve().is_relative_to(root), 'unsafe_action_document_cache')
    requests = [json.loads(path.read_bytes()) for path in sorted(base.glob('request-*.json'))]
    responses = [json.loads(path.read_bytes()) for path in sorted(base.glob('response-*.json'))]
    by_url = {row['url']: row for row in documents}
    _require(len(requests) == len(responses) <= 6
             and [row['ordinal'] for row in requests] == list(range(1, len(requests) + 1))
             and [row['ordinal'] for row in responses] == list(range(1, len(responses) + 1))
             and len({row['url'] for row in requests}) == len(requests)
             and all(request['url'] == response['url'] and request['document'] == response['document']
                     and request['document'] == by_url.get(request['url'])
                     for request, response in zip(requests, responses))
             and all(row['http_status'] == 200 and row['valid_pdf_signature'] and not row['error'] for row in responses),
             'prior_or_interrupted_action_document_acquisition_stopped')
    for row in responses:
        path = root / row['file']
        _require(not path.is_symlink() and path.resolve().is_relative_to(base)
                 and path.stat().st_size == row['bytes'] <= MAX_BYTES
                 and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], 'action_original_changed')
    for document in documents:
        if document['url'] in {row['url'] for row in requests}:
            continue
        ordinal = len(responses) + 1
        request_record = {'ordinal': ordinal, 'url': document['url'], 'document': document,
                          'started_at': datetime.now(timezone.utc).isoformat(), 'robots_observation': robot,
                          'automatic_retries': 0, 'redirects': False, 'proxy': False, 'authentication': 'none'}
        _save(base, f'request-{ordinal:03d}.json', _dump(request_record))
        status, body, error, headers = None, b'', None, {}
        try:
            request = urllib.request.Request(document['url'], headers={
                'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf', 'Accept-Encoding': 'identity'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=30) as response:
                status, headers, body = response.status, response.headers, response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as failure:
            status, headers, body, error = failure.code, failure.headers, failure.read(MAX_BYTES + 1), type(failure).__name__
        except (ValueError, OSError) as failure:
            error = type(failure).__name__
        digest = hashlib.sha256(body).hexdigest()
        valid = len(body) <= MAX_BYTES and body.startswith(b'%PDF-')
        filename = f'original-{digest}.' + ('pdf' if valid else 'response.bin')
        _save(base, filename, body)
        result = {**request_record, 'http_status': status, 'valid_pdf_signature': valid, 'error': error,
                  'file': CACHE + '/' + filename, 'sha256': digest, 'bytes': len(body),
                  'http_date': headers.get('Date'), 'retrieved_at': datetime.now(timezone.utc).isoformat(),
                  'model_admitted': False, 'settlement_verified': False, 'point_in_time_archive_verified': False}
        _save(base, f'response-{ordinal:03d}.json', _dump(result))
        responses.append(result)
        if status != 200 or not valid or error:
            break
    return responses


if __name__ == '__main__':
    print(json.dumps([{'event': row['document']['event_id'], 'status': row['http_status'],
                       'bytes': row['bytes'], 'pdf': row['valid_pdf_signature']} for row in acquire()], indent=2))
