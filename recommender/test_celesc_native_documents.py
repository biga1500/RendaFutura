from datetime import datetime, timezone
import hashlib
import json
import shutil
import socket

import pytest

from . import celesc_native_documents as native


@pytest.fixture(scope='module')
def report():
    return native.inspect()


def test_primary_documents_resolve_june_ex_date_without_turning_deadlines_into_cash(report):
    review = report['june_date_review']
    assert review['primary_record_date'] == '2025-06-25'
    assert review['primary_ex_date'] == '2025-06-26'
    assert review['workbook_reported_ex_date'] == '2025-05-26'
    assert review['payment_role'] == 'deadlines_in_notice_not_confirmation_of_settlement'
    assert review['installment_deadlines'] == ['2026-06-30', '2026-12-30']
    assert not review['original_workbook_modified']
    assert not review['future_installment_counted_as_past_cash']
    assert not report['model_admissions'] and not report['missing_data_imputed']
    assert not report['settlement_verified'] and not report['point_in_time_archive_verified']


def test_invalid_200_response_is_preserved_and_not_parsed_as_pdf(report):
    rows = {row['sequence']: row for row in report['documents']}
    assert set(rows) == {'916501', '916855', '949424'}
    assert rows['949424']['status'] == 200 and not rows['949424']['valid_pdf']
    assert 'pages' not in rows['949424'] and 'native_form' not in rows['949424']
    assert report['not_acquired_sequences'] == ['949491']
    assert all(hashlib.sha256((native.ROOT / row['file']).read_bytes()).hexdigest() == row['sha256']
               for row in rows.values())


def test_inspection_is_offline_and_does_not_change_live_publication(monkeypatch, report):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    paths = [native.ROOT / 'current.json', native.ROOT / 'inventory/current.json',
             native.ROOT / 'stock-selection/current.json']
    before = {path: path.read_bytes() for path in paths}
    assert native.inspect() == report and report['network_calls'] == 0
    assert all(path.read_bytes() == before[path] for path in paths)


@pytest.mark.parametrize('operation', [native.acquire, native.inspect])
def test_no_celesc_research_outside_positive_yield_selection(monkeypatch, tmp_path, operation):
    monkeypatch.setattr(native, 'allowed_tickers', lambda folder: frozenset({'ITUB4'}))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    with pytest.raises(ValueError, match='Celesc_outside_selected_scope'):
        operation(tmp_path)
    assert not list(tmp_path.iterdir())


def test_acquisition_does_not_retry_failed_original_or_fetch_next_document(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(native, 'datetime', Frozen)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    base = native.ROOT / native.CACHE
    before = {path: path.read_bytes() for path in base.glob('*.json')}
    with pytest.raises(ValueError, match='prior_native_acquisition_stopped'):
        native.acquire()
    assert {path: path.read_bytes() for path in base.glob('*.json')} == before


@pytest.mark.parametrize('change', ['url', 'document', 'bytes', 'body', 'signature', 'order'])
def test_tampered_native_proof_fails_closed(tmp_path, monkeypatch, change):
    candidates, _ = native.candidate_cvm_notices(native.ROOT)
    monkeypatch.setattr(native, 'candidate_cvm_notices', lambda folder: (candidates, []))
    monkeypatch.setattr(native, 'allowed_tickers', lambda folder: frozenset({'CLSC4'}))
    base = tmp_path / native.CACHE
    base.mkdir(parents=True)
    original = native.ROOT / native.CACHE / 'response-001.json'
    row = json.loads(original.read_bytes())
    target = tmp_path / row['file']
    shutil.copyfile(native.ROOT / row['file'], target)
    if change == 'url':
        row['url'] += '&other=1'
    elif change == 'document':
        row['document']['cvm'] = 1
    elif change == 'bytes':
        row['bytes'] += 1
    elif change == 'body':
        target.write_bytes(target.read_bytes().replace(b'%PDF-', b'%PDE-', 1))
    elif change == 'signature':
        row['valid_pdf_signature'] = False
    else:
        row['ordinal'] = 2
    (base / 'response-001.json').write_text(json.dumps(row), encoding='utf-8')
    with pytest.raises(ValueError):
        native.inspect(tmp_path)
