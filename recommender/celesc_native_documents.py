"""Acquire the four indexed primary declarations behind Celesc date conflicts."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from pypdf import PdfReader

from .celesc_history_research import CUTOFF, ROOT, candidate_cvm_notices
from .cvm_proventos_documents import MAX_BYTES, parse_pdf
from .dividend_scope import allowed_tickers
from .selected_issuer_sources import _dump, _require, _save


CACHE = 'raw/primary-research/celesc-native-documents'
SEQUENCES = {'916501', '916855', '949424', '949491'}
ROBOTS = 'raw/primary-research/alternative-cash/response-b3001f74f6aa54c277f8d6b413d7f41b13d35df28e24be374499457e43d1f313.json'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('CVM_document_redirect_not_followed')


def acquire(folder=ROOT):
    root = Path(folder).resolve()
    selected = allowed_tickers(root)
    _require(selected is not None and bool(selected & {'CLSC3', 'CLSC4'}), 'Celesc_outside_selected_scope')
    robot = json.loads((root / ROBOTS).read_bytes())
    robot_path = root / robot['file']
    _require(robot['url'] == 'https://www.rad.cvm.gov.br/robots.txt'
             and robot['http_status'] == 404 and not robot_path.is_symlink()
             and robot_path.resolve().is_relative_to(root)
             and hashlib.sha256(robot_path.read_bytes()).hexdigest() == robot['sha256']
             and robot['retrieved_at'][:10] == datetime.now(timezone.utc).date().isoformat(),
             'fresh_CVM_robots_observation_required')
    documents, _ = candidate_cvm_notices(root)
    documents = [row for row in documents if row['sequence'] in SEQUENCES]
    _require({row['sequence'] for row in documents} == SEQUENCES
             and all(row['version'] == 1 and row['cvm'] == 2461 for row in documents),
             'exact_native_conflict_documents_required')
    base = root / CACHE
    base.mkdir(parents=True, exist_ok=True)
    _require(not base.is_symlink() and base.resolve().is_relative_to(root), 'unsafe_native_document_cache')
    requests = sorted(base.glob('request-*.json'))
    responses = sorted(base.glob('response-*.json'))
    _require(len(requests) == len(responses) and len(requests) <= 4, 'interrupted_native_request_ledger')
    prior = [json.loads(path.read_bytes()) for path in responses]
    _require(all(row['status'] == 200 and row['valid_pdf_signature'] for row in prior),
             'prior_native_acquisition_stopped')
    for row in prior:
        path = root / row['file']
        _require(not path.is_symlink() and path.resolve().is_relative_to(root)
                 and path.stat().st_size == row['bytes']
                 and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'],
                 'native_original_changed')
    attempted = {json.loads(path.read_bytes())['document']['sequence'] for path in requests}
    for document in documents:
        if document['sequence'] in attempted:
            continue
        ordinal = len(prior) + 1
        _require(ordinal <= 4, 'native_request_budget_exhausted')
        started = datetime.now(timezone.utc).isoformat()
        request_record = {'ordinal': ordinal, 'document': document, 'started_at': started,
                          'robots_evidence': ROBOTS, 'automatic_retries': 0, 'redirects': False}
        _save(base, f'request-{ordinal:03d}.json', _dump(request_record))
        status, body, error = None, b'', None
        try:
            request = urllib.request.Request(document['url'], headers={
                'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf',
                'Accept-Encoding': 'identity'})
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
                status, body = response.status, response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as failure:
            status, body, error = failure.code, failure.read(MAX_BYTES + 1), type(failure).__name__
        except (ValueError, OSError) as failure:
            error = type(failure).__name__
        digest = hashlib.sha256(body).hexdigest()
        valid = bool(body.startswith(b'%PDF-') and len(body) <= MAX_BYTES)
        filename = f"cvm{document['sequence']}v1-{digest}." + ('pdf' if valid else 'response.bin')
        _save(base, filename, body)
        result = {'ordinal': ordinal, 'document': document, 'url': document['url'],
                  'file': CACHE + '/' + filename, 'sha256': digest, 'bytes': len(body),
                  'status': status, 'valid_pdf_signature': valid, 'error': error,
                  'retrieved_at': datetime.now(timezone.utc).isoformat(),
                  'settlement_verified': False, 'point_in_time_archive_verified': False,
                  'model_admitted': False, 'automatic_retries': 0, 'redirects': False}
        _save(base, f'response-{ordinal:03d}.json', _dump(result))
        prior.append(result)
        if status != 200 or not valid or error:
            break
    return prior


def inspect(folder=ROOT):
    """Review cached originals only, retaining invalid responses and date roles."""
    root = Path(folder).resolve()
    selected = allowed_tickers(root)
    _require(selected is not None and bool(selected & {'CLSC3', 'CLSC4'}), 'Celesc_outside_selected_scope')
    candidates, _ = candidate_cvm_notices(root)
    candidates = {row['sequence']: row for row in candidates if row['sequence'] in SEQUENCES}
    output, originals = [], {}
    base = root / CACHE
    for ordinal, receipt_path in enumerate(sorted(base.glob('response-*.json')), 1):
        _require(not receipt_path.is_symlink(), 'unsafe_native_receipt')
        row = json.loads(receipt_path.read_bytes())
        sequence = row['document']['sequence']
        _require(sequence in candidates and row['document'] == candidates[sequence]
                 and row['ordinal'] == ordinal and receipt_path.name == f'response-{ordinal:03d}.json'
                 and row['url'] == row['document']['url'] and sequence not in originals,
                 'native_receipt_index_binding_changed')
        path = root / row['file']
        _require(not path.is_symlink() and path.resolve().is_relative_to(base.resolve())
                 and path.stat().st_size == row['bytes'] <= MAX_BYTES + 1,
                 'unsafe_native_original')
        body = path.read_bytes()
        _require(hashlib.sha256(body).hexdigest() == row['sha256'], 'native_original_changed')
        valid = body.startswith(b'%PDF-') and len(body) <= MAX_BYTES and row['status'] == 200
        _require(valid == row['valid_pdf_signature'], 'native_signature_receipt_changed')
        original = {'sequence': sequence, 'url': row['url'], 'file': row['file'],
                    'sha256': row['sha256'], 'bytes': row['bytes'], 'retrieved_at': row['retrieved_at'],
                    'status': row['status'], 'valid_pdf': valid, 'model_admitted': False}
        if valid:
            original['pages'] = [page.extract_text() for page in PdfReader(io.BytesIO(body)).pages]
            if row['document']['category'] == 'Relat\u00f3rio Proventos':
                doc = dict(row['document'], issuer_name=row['document']['raw_row']['Nome_Companhia'])
                original['native_form'] = parse_pdf(body, doc, CUTOFF)
        originals[sequence] = original
        output.append(original)
    review = None
    if all(sequence in originals and originals[sequence]['valid_pdf'] for sequence in ('916501', '916855')):
        notice = re.sub(r'\s+', ' ', ' '.join(originals['916501']['pages']))
        _require(all(token in notice for token in (
            '25 de junho de 2025', '26 de junho de 2025', '1,435300308 por', '1,578830339 por',
            '0,7176501540', '0,7894151695', '30/06/2026', '30/12/2026')),
            'June_native_notice_facts_not_found')
        events = originals['916855']['native_form']['events']
        _require({(event['isin'], event['record_date'], event['gross_per_unit']) for event in events} == {
            ('BRCLSCACNOR6', '2025-06-25', 1.435300308),
            ('BRCLSCACNPR3', '2025-06-25', 1.578830339)}, 'June_native_form_disagrees')
        review = {'workbook_rows': [26, 27], 'primary_record_date': '2025-06-25',
                  'primary_ex_date': '2025-06-26', 'workbook_reported_ex_date': '2025-05-26',
                  'resolution': 'worksheet_ex_date_disagrees_with_both_primary_declarations',
                  'payment_role': 'deadlines_in_notice_not_confirmation_of_settlement',
                  'installment_deadlines': ['2026-06-30', '2026-12-30'],
                  'future_installment_counted_as_past_cash': False,
                  'source_sequences': ['916501', '916855'], 'original_workbook_modified': False}
    return {'status': 'primary_document_review_not_model_admission', 'network_calls': 0,
            'documents': output, 'not_acquired_sequences': sorted(SEQUENCES - originals.keys()),
            'june_date_review': review, 'model_admissions': [], 'missing_data_imputed': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT)
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    if args.inspect:
        report = inspect(args.folder)
        content = _dump(report)
        path = _save(args.folder / CACHE, 'review-' + hashlib.sha256(content).hexdigest() + '.json', content)
        print(json.dumps({'report': str(path), 'june_date_review': report['june_date_review'],
                          'model_admissions': report['model_admissions']}, indent=2))
    else:
        print(json.dumps([{'sequence': row['document']['sequence'], 'status': row['status'],
                       'bytes': row['bytes'], 'valid_pdf': row['valid_pdf_signature']}
                      for row in acquire(args.folder)], indent=2))
