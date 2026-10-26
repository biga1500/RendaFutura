import base64
from collections import Counter
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import subprocess
import urllib.error

from pypdf import PdfWriter
import pytest

from . import collect_missing_primary as module


AS_OF = date(2026, 10, 24)


def doc(cvm=100, sequence=1, year=2023, version=1):
    return {'cvm': cvm, 'sequence': str(sequence), 'version': version, 'queue_year': year,
            'issuer_name': 'Issuer', 'published_at': f'{year}-01-02',
            'url': 'https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?'
                f'Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo={sequence}'
                f'&numSequencia={sequence}&numVersao={version}'}


def stock(ticker='AAAA3', cvm=100, **fields):
    return {'ticker': ticker, 'cvm': cvm, 'share_class': 'ON',
            'model_ready': False, 'payments': {'status': 'partial'}, **fields}


def target(cvm=100, ticker='AAAA3', priority=0):
    return {'cvm': cvm, 'ticker': ticker, 'priority': priority,
            'reason': 'independent_cash_events_cache_missing'}


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def saved(folder, document, body=None, **fields):
    body = pdf() if body is None else body
    digest = hashlib.sha256(body).hexdigest()
    relative = module.CACHE / f"{document['sequence']}-v{document['version']}-{digest}.pdf"
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {**document, 'file': relative.as_posix(), 'sha256': digest,
            'bytes': len(body), 'pages': 1, 'status': 'partial', 'events': [], **fields}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def test_targeting_joins_tickers_not_different_inventory_indices_and_skips_ready():
    inventory = {'stocks': [stock(), stock('BBBB3', 200, model_ready=True),
        stock('CCCC3', 300), stock('DDDD3', 400), stock('EEEE11', 500),
        stock('FFFF3', 600, payments={'status': 'not_found'}), stock('GGGG3', 700)]}
    audit = {'candidates': ['CCCC3'], 'pending': [
        {'ticker': 'AAAA3', 'inventory_index': 99, 'reason': 'independent_cash_events_cache_missing'},
        {'ticker': 'BBBB3', 'reason': 'payments_cache_missing'},
        {'ticker': 'DDDD3', 'reason': 'independent_cash_cache_missing'},
        {'ticker': 'EEEE11', 'reason': 'payments_cache_missing'},
        {'ticker': 'GGGG3', 'reason': 'financial_features_unavailable'}]}
    result = module.select_targets(inventory, audit, ['DDDD3'])
    assert [r['ticker'] for r in result] == ['AAAA3', 'FFFF3']
    assert result[0]['cvm'] == 100
    assert result[1]['reason'] == 'inventory_payments_not_found'


@pytest.mark.parametrize('ticker', ['MERC3', 'MERC4', 'NGRD3', 'SHOW3', 'TKNO3', 'TKNO4'])
def test_parent_cancelled_issuers_never_enter_download_targets(ticker):
    inventory = {'stocks': [stock(ticker, payments={'status': 'not_found'},
        issuer_registration={'registration_blocks_current_purchase': True})]}
    audit = {'candidates': [], 'pending': [
        {'ticker': ticker, 'reason': 'independent_cash_events_cache_missing'}]}
    assert module.select_targets(inventory, audit) == []


def test_research_is_not_blocked_by_a_permanent_ticker_blacklist():
    inventory = {'stocks': [stock('MERC3', payments={'status': 'not_found'},
        issuer_registration={'registration_blocks_current_purchase': False})]}
    audit = {'candidates': [], 'pending': [{'ticker': 'MERC3', 'reason': 'payments_cache_missing'}]}
    assert module.select_targets(inventory, audit)[0]['ticker'] == 'MERC3'


def test_inventory_registration_block_also_excludes_other_issuers():
    inventory = {'stocks': [stock(issuer_registration={
        'registration_blocks_current_purchase': True})]}
    audit = {'candidates': [], 'pending': [
        {'ticker': 'AAAA3', 'reason': 'payments_cache_missing'}]}
    assert module.select_targets(inventory, audit) == []


@pytest.mark.parametrize('reason', sorted(module.MISSING_REASONS))
def test_each_cash_gap_reason_is_eligible_for_research_only(reason):
    result = module.select_targets({'stocks': [stock()]}, {
        'candidates': [], 'pending': [{'ticker': 'AAAA3', 'reason': reason}]})
    assert result[0]['reason'] == reason


def test_queue_balances_issuers_and_years_and_is_input_order_independent():
    documents = [doc(cvm, cvm * 100 + year, year)
                 for cvm in range(1, 11) for year in range(2021, 2027)]
    targets = [target(cvm, f'T{cvm:03}3') for cvm in range(1, 11)]
    queue = module.balanced_queue(documents, targets, set(), [], 60)
    reverse = module.balanced_queue(list(reversed(documents)), list(reversed(targets)), set(), [], 60)
    assert queue == reverse
    assert len(queue) == 60
    assert len({r['cvm'] for r in queue[:10]}) == 10
    assert Counter(r['queue_year'] for r in queue) == {year: 10 for year in range(2021, 2027)}
    assert Counter(r['cvm'] for r in queue) == {cvm: 6 for cvm in range(1, 11)}


def test_queue_skips_cached_keeps_versions_and_shared_classes_request_once():
    documents = [doc(sequence=1, version=1), doc(sequence=1, version=2),
                 doc(sequence=2), doc(sequence=2), doc(cvm=999, sequence=3)]
    queue = module.balanced_queue(documents, [target(), target(ticker='AAAA4')], {('1', 1)}, [], 60)
    assert [(d['sequence'], d['version']) for d in queue] == [('1', 2), ('2', 1)]
    assert all(d['target_tickers'] == ['AAAA3', 'AAAA4'] for d in queue)


def test_lower_coverage_and_explicit_gap_priority_are_deterministic():
    documents = [doc(cvm, cvm) for cvm in (1, 2, 3)]
    targets = [target(1, 'AAAA3'), target(2, 'BBBB3'), target(3, 'CCCC3', priority=1)]
    queue = module.balanced_queue(documents, targets, set(), [doc(1, 999)], 3)
    assert [d['cvm'] for d in queue] == [2, 1, 3]


@pytest.mark.parametrize('limit', [0, -1, 61, 1000, True])
def test_hard_cap_cannot_be_overridden(limit):
    with pytest.raises(ValueError, match='limit'):
        module.balanced_queue([], [], set(), [], limit)


def test_archived_rows_and_revisions_are_retained_and_actual_pdf_hashes_verified(tmp_path):
    first = saved(tmp_path, doc(version=1))
    revision = saved(tmp_path, doc(version=2))
    write_json(tmp_path / module.CACHE / 'documents-old.json', {'results': [first, revision]})
    cached, retained, verified = module.existing_evidence(tmp_path, {'results': [first]})
    assert cached == {('1', 1), ('1', 2)}
    assert retained == [first, revision]
    assert len(verified) == 2
    (tmp_path / first['file']).write_bytes(b'%PDF-modified')
    with pytest.raises(ValueError, match='hash_or_signature'):
        module.existing_evidence(tmp_path, {'results': [first]})


def test_a_report_cannot_assert_an_unverified_pdf(tmp_path):
    first = saved(tmp_path, doc())
    first['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='not_verified'):
        module.existing_evidence(tmp_path, {'results': [first]})


class Response:
    status = 200
    headers = {'Content-Type': 'text/html'}

    def __init__(self, body, url=None):
        self.body, self.url = body, url or doc()['url']

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body[:size]


def test_fetch_hashes_real_pdf_bytes_even_when_mime_is_html(monkeypatch):
    body = pdf()
    calls = []

    def open_response(request, timeout):
        calls.append((request.full_url, timeout))
        return Response(body)

    monkeypatch.setattr(module.primary, '_open', open_response)
    result = module._fetch_parse(doc(), AS_OF.isoformat(), 29)
    assert base64.b64decode(result['body_base64']) == body
    assert result['sha256'] == hashlib.sha256(body).hexdigest()
    assert result['pages'] == 1
    assert result['events'] == []
    assert calls == [(doc()['url'], 29)]


@pytest.mark.parametrize('body', [b'<html>blocked</html>', b'%PDF-' + b'0' * module.primary.MAX_BYTES],
                         ids=['html', 'oversized'])
def test_html_and_oversized_responses_never_become_documents(body, monkeypatch):
    monkeypatch.setattr(module.primary, '_open', lambda *args, **kwargs: Response(body))
    result = module._fetch_parse(doc(), AS_OF.isoformat(), 30)
    assert result['status'] == 'source_error'
    assert result['error_code'] == 'response_is_not_a_pdf'
    assert 'body_base64' not in result and not result['retryable']


def test_no_request_to_non_cvm_or_redirected_exchange_host(monkeypatch):
    calls = []
    monkeypatch.setattr(module.primary, '_open', lambda *args, **kwargs: calls.append(args))
    result = module._fetch_parse({**doc(), 'url': doc()['url'].replace('www.rad.cvm.gov.br', 'b3.com.br')},
                                 AS_OF.isoformat(), 30)
    assert result['status'] == 'source_error' and not calls
    monkeypatch.setattr(module.primary, '_open', lambda *args, **kwargs: Response(pdf(), 'https://b3.com.br/file'))
    assert module._fetch_parse(doc(), AS_OF.isoformat(), 30)['status'] == 'source_error'


@pytest.mark.parametrize('code', [401, 403, 429])
def test_access_denial_stops_entire_queue_without_retry_or_fallback(code, monkeypatch):
    calls = []

    def attempt(document, as_of, remaining):
        calls.append(document['sequence'])
        return {'status': 'source_error', 'events': [], 'http_status': code, 'retryable': True}

    monkeypatch.setattr(module, '_bounded_attempt', attempt)
    rows, requests, stopped = module.acquire([doc(sequence=1), doc(sequence=2)], AS_OF)
    assert (requests, stopped, len(rows)) == (1, code, 1)
    assert calls == ['1']


def test_transient_failure_retries_once_and_waits_between_every_request(monkeypatch):
    now = [0.0]
    times = []
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))

    def attempt(document, as_of, remaining):
        times.append(now[0])
        return {'status': 'source_error', 'events': [], 'http_status': 503, 'retryable': True}

    monkeypatch.setattr(module, '_bounded_attempt', attempt)
    rows, requests, stopped = module.acquire([doc(sequence=1), doc(sequence=2)], AS_OF)
    assert requests == 4 and stopped is None
    assert [len(r['attempts']) for r in rows] == [2, 2]
    assert times == [0, .5, 1, 1.5]


def test_retry_shares_document_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    calls = []

    def attempt(document, as_of, remaining):
        calls.append(remaining)
        now[0] += 29
        return {'status': 'source_error', 'events': [], 'retryable': True}

    monkeypatch.setattr(module, '_bounded_attempt', attempt)
    rows, requests, _ = module.acquire([doc()], AS_OF)
    assert calls == [30, .5] and requests == 2
    assert len(rows[0]['attempts']) == 2


def test_child_timeout_is_hard_and_not_retried(monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs['timeout'] == 7.5
        assert kwargs['stderr'] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, 7.5)

    monkeypatch.setattr(module.subprocess, 'run', timeout)
    result = module._bounded_attempt(doc(), AS_OF.isoformat(), 7.5)
    assert result['error_code'] == 'document_total_timeout'
    assert result['retryable'] is False


@pytest.mark.parametrize('code', [401, 403, 429, 404, 503])
def test_fetch_http_errors_are_sanitized_and_only_transient_errors_retry(code, monkeypatch):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(doc()['url'], code, 'opaque server message', None, None)

    monkeypatch.setattr(module.primary, '_open', denied)
    result = module._fetch_parse(doc(), AS_OF.isoformat(), 30)
    assert result['http_status'] == code
    assert result['retryable'] is (code == 503)
    assert 'opaque server message' not in json.dumps(result)


def test_fact_counts_do_not_treat_decimal_scale_as_another_declaration():
    event = {'cvm': 100, 'isin': 'EXACT', 'payment_date': '2023-01-31',
             'gross_per_unit_decimal': '0.10000000000'}
    assert module._fact_key(event) == module._fact_key({**event, 'gross_per_unit_decimal': '0.1'})
    assert module._fact_key(event) != module._fact_key({**event, 'payment_date': '2023-02-01'})


def test_merge_preserves_all_615_rows_errors_unknown_fields_and_revisions():
    original = [{**doc(sequence=n), 'sha256': str(n), 'events': [{'raw': n}],
                 'old_extra': {'preserve': n}} for n in range(1, 616)]
    previous = {'results': original, 'unrelated_metadata': {'preserve': True}}
    snapshot = deepcopy(previous)
    revision = {**original[0], **doc(sequence=1, version=2), 'sha256': 'revision'}
    error = {**doc(sequence=700), 'status': 'source_error', 'events': []}
    run = {'as_of': AS_OF.isoformat(), 'collected_at': 'now', 'discovered_documents': 3090,
           'network_requests': 2, 'counts': {}, 'baseline': {}}
    merged = module.merge_report(previous, original, [original[0], revision, error], run, 'new.json')
    assert previous == snapshot
    assert merged['results'][:615] == original
    assert merged['results'][615:] == [revision, error]
    assert merged['unrelated_metadata'] == previous['unrelated_metadata']
    assert merged['event_rows'] == 616
    assert merged['active_model_changed'] is merged['point_in_time_archive_verified'] is False


def setup_collection(tmp_path, monkeypatch):
    inventory = {'stocks': [stock()], 'sources': []}
    pointer = {'snapshot': 'snapshots/inventory-original'}
    write_json(tmp_path / 'inventory/current.json', pointer)
    write_json(tmp_path / 'inventory' / pointer['snapshot'] / 'market.json', inventory)
    write_json(tmp_path / 'current.json', {'snapshot': 'snapshots/model-original'})
    write_json(tmp_path / 'snapshots/model-original/assets.json', {'assets': []})
    audit_path = tmp_path / 'snapshots/staged/candidate-source-audit.json'
    write_json(audit_path, {'candidates': [], 'pending': [
        {'ticker': 'AAAA3', 'reason': 'independent_cash_events_cache_missing'}]})
    previous = {'schema_version': 1, 'results': [saved(tmp_path, doc(sequence=1))]}
    write_json(tmp_path / module.CACHE / 'report.json', previous)
    monkeypatch.setattr(module, 'load_inventory', lambda folder: inventory)
    monkeypatch.setattr(module.primary, 'discover', lambda *args: [doc(sequence=1), doc(sequence=2)])
    return audit_path, previous


def test_collection_writes_only_sidecar_artifacts_and_preserves_protected_inputs(tmp_path, monkeypatch):
    audit_path, previous = setup_collection(tmp_path, monkeypatch)
    protected = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    body = pdf()
    event = {'cvm': 100, 'isin': 'BREXACT00001', 'record_date': '2023-01-03',
             'payment_date': '2023-01-31', 'gross_per_unit_decimal': '0.12345678900',
             'gross_per_share': None, 'status': 'past_scheduled', 'cash_model_eligible': True}

    def acquire(queue, as_of):
        assert len(queue) == 1 and queue[0]['sequence'] == '2'
        return [{**queue[0], 'status': 'available', 'events': [event], 'pages': 1,
                 'body_base64': base64.b64encode(body).decode('ascii'),
                 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}], 1, None

    monkeypatch.setattr(module, 'acquire', acquire)
    run = module.collect(tmp_path, audit_path, AS_OF)
    merged = json.loads((tmp_path / module.CACHE / 'report.json').read_bytes())
    assert merged['results'][:1] == previous['results']
    assert run['counts']['new_downloaded_documents'] == 1
    assert run['counts']['distinct_new_declaration_facts'] == 1
    assert run['counts']['preserved_baseline_rows'] == 1
    result_event = merged['results'][1]['events'][0]
    assert result_event['gross_per_unit_decimal'] == '0.12345678900'
    assert result_event['cash_model_eligible'] is result_event['financially_confirmed'] is False
    assert result_event['is_settlement_confirmation'] is result_event['point_in_time_archive_verified'] is False
    assert result_event['status'] == 'past_scheduled'
    for path, original in protected.items():
        if path.name != 'report.json':
            assert path.read_bytes() == original
    for path in tmp_path.rglob('*'):
        if path.is_file() and path not in protected:
            assert path.is_relative_to(tmp_path / module.CACHE)
    baseline = tmp_path / run['baseline']['file']
    assert baseline.read_bytes() == protected[tmp_path / module.CACHE / 'report.json']
    assert run['counts']['model_admissions'] == 0


def test_stopped_collection_merges_error_without_dropping_prior_rows(tmp_path, monkeypatch):
    audit_path, previous = setup_collection(tmp_path, monkeypatch)
    monkeypatch.setattr(module, '_bounded_attempt', lambda *args: {
        'status': 'source_error', 'http_status': 403, 'events': [], 'retryable': False})
    run = module.collect(tmp_path, audit_path, AS_OF)
    merged = json.loads((tmp_path / module.CACHE / 'report.json').read_bytes())
    assert merged['results'][0] == previous['results'][0]
    assert len(merged['results']) == 2
    assert run['counts']['new_downloaded_documents'] == 0
    assert run['stopped_http_status'] == 403
    assert run['network_requests'] == 1


def test_exclusive_artifact_creation_never_overwrites(tmp_path):
    path = tmp_path / 'immutable.pdf'
    module._immutable(path, b'original')
    with pytest.raises(FileExistsError):
        module._immutable(path, b'changed')
    assert path.read_bytes() == b'original'
