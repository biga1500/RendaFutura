"""Focused payment-import receipt contracts, offline and isolated from snapshots."""

import builtins
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import urllib.request

import pytest

from .inventory_candidates import _Cache, _payments


MARKET = Path(__file__).resolve().parent / 'data/market'
PAYMENT = 'raw/expanded/CEEB5-payments.html'
SIDECAR = PAYMENT + '.source.json'
URL = 'https://investidor10.com.br/acoes/ceeb5/'
BODY = b'<html><title>CEEB5</title><p>synthetic receipt fixture</p></html>'
RETRIEVED = '2025-10-24T21:31:09.123456+00:00'
MTIME = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def offline_readonly_real_cache(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('payment receipt tests must never access the network')

    for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                        (socket, 'create_connection'), (socket, 'getaddrinfo'),
                        (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
        monkeypatch.setattr(owner, name, forbidden)

    def guard(original):
        def read_only(file, mode='r', *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve().is_relative_to(MARKET.resolve()):
                assert not any(flag in mode for flag in 'wax+'), 'real market cache is read-only'
            return original(file, mode, *args, **kwargs)
        return read_only

    monkeypatch.setattr(builtins, 'open', guard(builtins.open))
    monkeypatch.setattr(io, 'open', guard(io.open))


def sha(body):
    return hashlib.sha256(body).hexdigest()


def put(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def put_receipt(root, receipt):
    return put(root, SIDECAR, json.dumps(receipt, ensure_ascii=True).encode('ascii'))


def reader(root, sources=()):
    return _Cache(root, {'stocks': [], 'sources': list(sources)})


@pytest.fixture
def imported(tmp_path):
    original = f'raw/primary-research/pending-recheck/body-{sha(BODY)}.bin'
    put(tmp_path, original, BODY)
    payment = put(tmp_path, PAYMENT, BODY)
    os.utime(payment, (MTIME, MTIME))
    receipt = {'file': PAYMENT, 'sha256': sha(BODY), 'bytes': len(BODY), 'status': 200,
        'error': None, 'url': URL, 'retrieved_at': RETRIEVED, 'published_at': None,
        'source_original': original, 'bank_settlement_verified': False}
    put_receipt(tmp_path, receipt)
    return receipt


def assert_not_cached(cache):
    assert PAYMENT not in cache.bodies and PAYMENT not in cache.receipts


def test_valid_receipt_binds_body_URL_hash_and_original_without_using_mtime(tmp_path, imported):
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    cache = reader(tmp_path)
    body, observed = cache.read(PAYMENT, 'payments')
    assert body == BODY and sha(body) == imported['sha256']
    assert observed == datetime.fromisoformat(RETRIEVED)
    assert observed != datetime.fromtimestamp(MTIME, timezone.utc)
    source = cache.receipts[PAYMENT]
    assert source['file'] == PAYMENT and source['bytes'] == len(BODY)
    assert source['url'] == URL and source['retrieved_at'] == RETRIEVED
    assert source['sha256'] == sha(body)
    assert source['source_original'] == imported['source_original']
    assert source['receipt_basis'] == 'retained_public_HTTP_response'
    assert source['publication_inferred'] is False and source.get('published_at') is None
    assert source['observed_at'] == datetime.fromtimestamp(MTIME, timezone.utc).isoformat()
    assert source['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
    assert cache.read(PAYMENT, 'payments') == (body, observed)
    assert cache.all_receipts() == [source]
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_receipt_supports_explicit_non_UTC_timezone(tmp_path, imported):
    imported['retrieved_at'] = '2025-10-24T18:31:09.123456-03:00'
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    _, observed = cache.read(PAYMENT, 'payments')
    assert observed == datetime.fromisoformat(RETRIEVED)
    assert cache.receipts[PAYMENT]['retrieved_at'] == imported['retrieved_at']


def test_changing_file_mtime_cannot_change_proven_retrieval(tmp_path, imported):
    _, first = reader(tmp_path).read(PAYMENT, 'payments')
    changed = datetime(2035, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / PAYMENT, (changed, changed))
    cache = reader(tmp_path)
    _, second = cache.read(PAYMENT, 'payments')
    assert first == second == datetime.fromisoformat(imported['retrieved_at'])
    assert cache.receipts[PAYMENT]['retrieved_at'] == RETRIEVED
    assert cache.receipts[PAYMENT]['observed_at'] == datetime.fromtimestamp(changed, timezone.utc).isoformat()


def test_missing_receipt_keeps_retrieval_unknown_instead_of_promoting_mtime(tmp_path):
    path = put(tmp_path, PAYMENT, BODY)
    os.utime(path, (MTIME, MTIME))
    cache = reader(tmp_path)
    body, observed = cache.read(PAYMENT, 'payments')
    assert body == BODY and observed == datetime.fromtimestamp(MTIME, timezone.utc)
    assert cache.receipts[PAYMENT]['retrieved_at'] is None
    assert cache.receipts[PAYMENT]['url'] is None
    assert cache.receipts[PAYMENT]['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
    assert 'receipt_basis' not in cache.receipts[PAYMENT]


def test_recent_mtime_cannot_make_an_old_import_cover_the_payment_cutoff(tmp_path, imported):
    changed = datetime(2035, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / PAYMENT, (changed, changed))
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='payment_source_observation_precedes_cutoff'):
        _payments('CEEB5', cache, date(2026, 9, 30))
    assert cache.receipts[PAYMENT]['retrieved_at'] == RETRIEVED


@pytest.mark.parametrize('url', ['http://investidor10.com.br/acoes/ceeb5/',
    'https://investidor10.com.br/acoes/ceeb3/', 'https://example.invalid/acoes/ceeb5/',
    'https://investidor10.com.br.example.invalid/acoes/ceeb5/',
    URL + '?ticker=ceeb3', URL + '#calendar'])
def test_receipt_with_wrong_URL_is_rejected(tmp_path, imported, url):
    imported['url'] = url
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='payment_import_receipt_disagrees'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


@pytest.mark.parametrize('stamp', ['2025-10-24T21:31:09', '2025-10-24'])
def test_receipt_without_retrieval_timezone_is_rejected(tmp_path, imported, stamp):
    imported['retrieved_at'] = stamp
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='invalid_payment_import_retrieval'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


def test_future_retrieval_is_rejected_even_when_mtime_is_old(tmp_path, imported):
    imported['retrieved_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='invalid_payment_import_retrieval'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('bytes', len(BODY) + 1),
    ('bytes', str(len(BODY))), ('file', 'raw/expanded/CEEB3-payments.html'),
    ('file', '../CEEB5-payments.html'), ('status', 401), ('status', 403),
    ('status', 429), ('error', 'HTTPError')])
def test_receipt_hash_size_file_or_HTTP_status_disagreement_is_rejected(tmp_path, imported, field, value):
    imported[field] = value
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='payment_import_receipt_disagrees'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


@pytest.mark.parametrize('which,changed', [('payment', b'X' * len(BODY)),
    ('original', b'X' * len(BODY)), ('original', BODY + b'changed size')])
def test_changed_imported_or_original_body_is_rejected(tmp_path, imported, which, changed):
    path = tmp_path / (PAYMENT if which == 'payment' else imported['source_original'])
    path.write_bytes(changed)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='payment_import_receipt_disagrees' if which == 'payment'
                       else 'payment_import_original_changed'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


def test_missing_original_is_rejected_without_caching_unverified_payment(tmp_path, imported):
    (tmp_path / imported['source_original']).unlink()
    cache = reader(tmp_path)
    with pytest.raises((FileNotFoundError, ValueError)):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


def test_malformed_sidecar_does_not_fall_back_to_mtime(tmp_path, imported):
    (tmp_path / SIDECAR).write_bytes(b'{invalid JSON')
    cache = reader(tmp_path)
    with pytest.raises(ValueError):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


@pytest.mark.parametrize('original', ['../body.bin', 'raw/primary-research/../body.bin',
    'raw/primary-research/pending-recheck/../../expanded/CEEB5-payments.html',
    'C:/outside/body.bin', '/outside/body.bin',
    'raw\\primary-research\\pending-recheck\\body.bin'])
def test_original_path_traversal_and_absolute_paths_are_rejected(tmp_path, imported, original):
    imported['source_original'] = original
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match='unsafe_payment_import_original'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


def test_read_cannot_escape_cache_root_even_for_an_existing_file(tmp_path):
    root = tmp_path / 'market'
    root.mkdir()
    outside = put(tmp_path, 'outside-payments.html', BODY)
    for relative in ('../outside-payments.html', str(outside.resolve())):
        cache = reader(root)
        with pytest.raises(ValueError, match='unsafe_cache_path'):
            cache.read(relative, 'payments')
        assert not cache.bodies and not cache.receipts


@pytest.mark.parametrize('which,reason', [('payment', 'unsafe_cache_path'),
    ('original', 'payment_import_original_changed')])
def test_resolved_path_outside_root_is_rejected_before_reading_it(tmp_path, imported, monkeypatch, which, reason):
    selected = tmp_path / (PAYMENT if which == 'payment' else imported['source_original'])
    outside = tmp_path.parent / (tmp_path.name + '-outside') / selected.name
    original_resolve, original_read = Path.resolve, Path.read_bytes

    def resolve(path, *args, **kwargs):
        return outside if path == selected else original_resolve(path, *args, **kwargs)

    def read_bytes(path):
        assert path != selected, 'unsafe path must be rejected before its body is read'
        return original_read(path)

    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    cache = reader(tmp_path)
    with pytest.raises(ValueError, match=reason):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)


def test_oversized_receipt_is_rejected_before_JSON_parsing(tmp_path, imported):
    (tmp_path / SIDECAR).write_bytes(b' ' * 16_001)
    with pytest.raises(ValueError, match='unsafe_payment_import_receipt'):
        reader(tmp_path).read(PAYMENT, 'payments')


@pytest.mark.parametrize('which,reason', [('payment', 'unsafe_cache_path'),
    ('receipt', 'unsafe_payment_import_receipt'), ('original', 'payment_import_original_changed')])
def test_symlink_rejection_branches_do_not_depend_on_OS_privileges(tmp_path, imported, monkeypatch, which, reason):
    selected = tmp_path / {'payment': PAYMENT, 'receipt': SIDECAR,
                           'original': imported['source_original']}[which]
    original = Path.is_symlink

    def is_symlink(path):
        return path == selected or original(path)

    monkeypatch.setattr(Path, 'is_symlink', is_symlink)
    with pytest.raises(ValueError, match=reason):
        reader(tmp_path).read(PAYMENT, 'payments')


@pytest.mark.parametrize('which', ['payment', 'original'])
def test_parent_symlink_metadata_cannot_be_ignored(tmp_path, imported, monkeypatch, which):
    selected = tmp_path / (PAYMENT if which == 'payment' else imported['source_original'])
    parent = selected.parent
    original = Path.is_symlink

    def is_symlink(path):
        return path == parent or original(path)

    monkeypatch.setattr(Path, 'is_symlink', is_symlink)
    with pytest.raises(ValueError, match='unsafe_cache_path|unsafe_payment_import_original|payment_import_original_changed'):
        reader(tmp_path).read(PAYMENT, 'payments')


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            pytest.skip('Windows does not grant symlink creation; rejection branches are covered separately')
        raise


@pytest.mark.parametrize('which,reason', [('payment', 'unsafe_cache_path'),
    ('receipt', 'unsafe_payment_import_receipt'), ('original', 'payment_import_original_changed')])
def test_real_file_symlinks_are_rejected(tmp_path, imported, which, reason):
    selected = tmp_path / {'payment': PAYMENT, 'receipt': SIDECAR,
                           'original': imported['source_original']}[which]
    target = put(tmp_path, 'detached-' + selected.name, selected.read_bytes())
    selected.unlink()
    symlink(selected, target)
    with pytest.raises(ValueError, match=reason):
        reader(tmp_path).read(PAYMENT, 'payments')


@pytest.mark.parametrize('which', ['payment', 'original'])
def test_symlinked_parent_directory_is_rejected_even_within_cache_root(tmp_path, imported, which):
    selected = tmp_path / (PAYMENT if which == 'payment' else imported['source_original'])
    parent = selected.parent
    detached = tmp_path / ('detached-' + which)
    parent.rename(detached)
    symlink(parent, detached, directory=True)
    with pytest.raises(ValueError, match='unsafe_cache_path|unsafe_payment_import_original|payment_import_original_changed'):
        reader(tmp_path).read(PAYMENT, 'payments')


def test_inventory_hash_remains_authoritative_over_unneeded_sidecar(tmp_path, imported):
    known = {**deepcopy(imported), 'retrieved_at': None}
    imported['url'] = 'https://example.invalid/wrong'
    put_receipt(tmp_path, imported)
    cache = reader(tmp_path, [known])
    with pytest.raises(ValueError, match='payment_import_receipt_disagrees'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)
    bad = {**known, 'sha256': '0' * 64}
    with pytest.raises(ValueError, match='inventory_source_hash_mismatch'):
        reader(tmp_path, [bad]).read(PAYMENT, 'payments')


def test_activated_sources_keep_sidecar_retrieval_clock_and_receipt_metadata(tmp_path, imported):
    initial = reader(tmp_path)
    original_body, original_clock = initial.read(PAYMENT, 'payments')
    sources = deepcopy(initial.all_receipts())
    assert sources[0]['sha256'] == imported['sha256']
    changed = datetime(2035, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(tmp_path / PAYMENT, (changed, changed))
    activated = reader(tmp_path, sources)
    assert activated.known[PAYMENT]['sha256'] == imported['sha256']
    body, clock = activated.read(PAYMENT, 'payments')
    assert body == original_body == BODY
    assert clock == original_clock == datetime.fromisoformat(imported['retrieved_at'])
    assert clock != datetime.fromtimestamp(changed, timezone.utc)
    receipt = activated.receipts[PAYMENT]
    for key in ('file', 'sha256', 'bytes', 'url', 'retrieved_at', 'receipt_basis',
                'source_original', 'publication_inferred'):
        assert receipt[key] == sources[0][key]
    assert receipt['observed_at'] == datetime.fromtimestamp(changed, timezone.utc).isoformat()
    assert receipt['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
    assert activated.all_receipts() == [receipt]
    assert receipt.get('published_at') is None and receipt['publication_inferred'] is False


def test_valid_sidecar_cannot_override_conflicting_inventory_hash(tmp_path, imported, monkeypatch):
    assert json.loads((tmp_path / SIDECAR).read_bytes()) == imported
    known = {**deepcopy(imported), 'sha256': '0' * 64}
    cache = reader(tmp_path, [known])
    original_read = Path.read_bytes

    def read_bytes(path):
        assert path != tmp_path / SIDECAR, 'inventory hash must be checked before reading the sidecar'
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    with pytest.raises(ValueError, match='inventory_source_hash_mismatch'):
        cache.read(PAYMENT, 'payments')
    assert_not_cached(cache)
    assert cache.known[PAYMENT]['sha256'] == '0' * 64


def test_real_CEEB5_receipt_hash_body_and_read_in_isolated_copy(tmp_path):
    raw_receipt = (MARKET / SIDECAR).read_bytes()
    receipt = json.loads(raw_receipt)
    original = receipt['source_original']
    assert re.fullmatch(r'raw/primary-research/pending-recheck/body-[a-f0-9]{64}\.bin', original)
    raw_payment, raw_original = (MARKET / PAYMENT).read_bytes(), (MARKET / original).read_bytes()
    assert raw_payment == raw_original
    assert sha(raw_payment) == receipt['sha256'] and len(raw_payment) == receipt['bytes']
    assert receipt['file'] == PAYMENT and receipt['url'] == URL
    assert receipt['published_at'] is None and receipt['bank_settlement_verified'] is False
    put(tmp_path, PAYMENT, raw_payment)
    put(tmp_path, original, raw_original)
    put(tmp_path, SIDECAR, raw_receipt)
    os.utime(tmp_path / PAYMENT, (MTIME, MTIME))
    cache = reader(tmp_path)
    body, observed = cache.read(PAYMENT, 'payments')
    assert body == raw_original
    assert observed == datetime.fromisoformat(receipt['retrieved_at'])
    assert observed != datetime.fromtimestamp(MTIME, timezone.utc)
    assert cache.receipts[PAYMENT]['retrieved_at'] == receipt['retrieved_at']
    assert cache.receipts[PAYMENT]['receipt_basis'] == 'retained_public_HTTP_response'
    assert (MARKET / SIDECAR).read_bytes() == raw_receipt
    assert (MARKET / PAYMENT).read_bytes() == raw_payment
    assert (MARKET / original).read_bytes() == raw_original
