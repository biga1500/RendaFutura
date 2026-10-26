from datetime import datetime, timezone
import hashlib
import io
import json
import socket

from pypdf import PdfReader
import pytest

from . import itausa_action_documents as documents


def receipts():
    return [json.loads(path.read_bytes()) for path in sorted((documents.ROOT / documents.CACHE).glob('response-*.json'))]


def test_all_six_literal_notices_are_preserved_as_primary_pdf_originals():
    rows = receipts()
    assert len(rows) == len({row['url'] for row in rows}) == 6
    assert sum(row['document']['kind'] == 'stock_bonus' for row in rows) == 5
    for row in rows:
        body = (documents.ROOT / row['file']).read_bytes()
        assert row['http_status'] == 200 and row['valid_pdf_signature']
        assert hashlib.sha256(body).hexdigest() == row['sha256'] and len(body) == row['bytes']
        assert body.startswith(b'%PDF-') and PdfReader(io.BytesIO(body)).pages
        assert row['document']['url'] == row['url'] == row['document']['discovery']['literal']
        assert not row['automatic_retries'] and not row['redirects'] and not row['proxy']
        assert not row['model_admitted'] and not row['settlement_verified']


def test_native_subscription_clause_proves_ex_rights_date_but_does_not_supply_price_adjustment_factor():
    row = next(row for row in receipts() if row['document']['kind'] == 'subscription')
    assert row['sha256'] == 'bbe33b1e48eeb385efba4ddeca000c96609983d13a8de43e9fd33948cc7f7ec9'
    body = (documents.ROOT / row['file']).read_bytes()
    page = PdfReader(io.BytesIO(body)).pages[7].extract_text()
    assert '1,390757436%' in page and '18.08.2023' in page and '17.08.2023' in page
    assert 'ex-direitos' in page and 'subscri' in page
    assert not row['model_admitted']


def test_repeated_acquisition_reads_existing_originals_without_another_network_request(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 14, tzinfo=timezone.utc)

    monkeypatch.setattr(documents, 'datetime', Frozen)
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('Network forbidden'))
    base = documents.ROOT / documents.CACHE
    before = {path: path.read_bytes() for path in base.glob('*') if path.is_file()}
    assert documents.acquire() == receipts()
    assert {path: path.read_bytes() for path in base.glob('*') if path.is_file()} == before


def test_no_notice_research_when_issuer_is_outside_user_positive_yield_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(documents, 'allowed_tickers', lambda root: frozenset({'PETR4'}))
    with pytest.raises(ValueError, match='Itausa_outside_selected_scope'):
        documents.acquire(tmp_path)
    assert not list(tmp_path.iterdir())
