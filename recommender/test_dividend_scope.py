from copy import deepcopy
import hashlib
import json

import pytest

from . import dividend_scope as scope


def source():
    return ('Resultado da busca\nPapel\tCotacao\tDiv.Yield\tOutro\n'
            'TEST3\t12,00\t0,01%\t10\nZERO3\t2,00\t0,00%\t20\n'
            'NEGA3\t3,00\t-0,01%\t30\nHIGH3\t4,00\t121,25%\t\n').encode()


def activate(tmp_path):
    path = tmp_path / 'supplied.txt'
    path.write_bytes(source())
    return scope.import_scope(tmp_path / 'market', path)


def test_strict_boundary_and_preserved_missing_non_yield_field():
    parsed = scope.parse_snapshot(source())
    assert parsed['selected_tickers'] == ['TEST3', 'HIGH3']
    assert parsed['zero_yield_tickers'] == ['ZERO3']
    assert parsed['negative_yield_tickers'] == ['NEGA3']
    assert parsed['records'][-1]['reported_fields']['Outro'] is None
    assert parsed['records'][-1]['missing_fields'] == ['Outro']


@pytest.mark.parametrize('replacement', ['-', '', 'NaN%', '0.1%', '0,00', 'Infinity%'])
def test_unknown_or_malformed_yield_is_not_zero_or_positive(replacement):
    with pytest.raises(ValueError, match='missing_or_invalid_dividend_yield'):
        scope.parse_snapshot(source().replace(b'0,01%', replacement.encode()))


def test_duplicate_or_missing_identity_rejected():
    with pytest.raises(ValueError, match='invalid_or_duplicate'):
        scope.parse_snapshot(source().replace(b'HIGH3', b'TEST3'))


def test_no_scope_legacy_and_exact_ticker_filter_no_issuer_or_suffix_guess(tmp_path):
    assert scope.allowed_tickers(tmp_path / 'market') is None
    records = [{'ticker': 'TEST3'}, {'ticker': 'TEST4'}, {'ticker': 'ZERO3'}]
    assert scope.filter_records(tmp_path / 'market', records) == records
    manifest = activate(tmp_path)
    assert manifest['source_sha256'] == hashlib.sha256(source()).hexdigest()
    assert scope.filter_records(tmp_path / 'market', records) == [{'ticker': 'TEST3'}]
    snapshot = tmp_path / 'market' / 'snapshots' / 'example'
    assert scope.allowed_tickers(snapshot) == frozenset(['TEST3', 'HIGH3'])
    metadata = scope.scope_metadata(snapshot)
    assert metadata['sourceDate'] is None and metadata['treasuryUnchanged']
    assert metadata['dividendYieldIsPaymentHistory'] is False
    receipts = scope.scope_evidence(snapshot)
    assert len(receipts) == 2
    for receipt in receipts:
        actual = tmp_path / 'market' / receipt['file']
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == receipt['sha256']
        assert actual.stat().st_size == receipt['bytes']
        assert receipt['retrieved_at'] is None and receipt['source_date'] is None


@pytest.mark.parametrize('target', ['manifest', 'source'])
def test_corrupt_manifest_or_raw_never_reverts_to_unrestricted(tmp_path, target):
    manifest = activate(tmp_path)
    base = tmp_path / 'market' / 'stock-selection'
    pointer = json.loads((base / 'current.json').read_bytes())
    path = base / (pointer['manifest'] if target == 'manifest' else manifest['source_file'])
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='hash_mismatch'):
        scope.allowed_tickers(tmp_path / 'market')


def test_hashed_but_forged_selection_still_rejected(tmp_path):
    activate(tmp_path)
    base = tmp_path / 'market' / 'stock-selection'
    pointer = json.loads((base / 'current.json').read_bytes())
    original = json.loads((base / pointer['manifest']).read_bytes())
    changed = deepcopy(original)
    changed['selected_tickers'].append('ZERO3')
    body = scope._json(changed)
    digest = hashlib.sha256(body).hexdigest()
    pointer.update(manifest='manifests/' + digest + '.json', sha256=digest)
    (base / pointer['manifest']).write_bytes(body)
    (base / 'current.json').write_bytes(scope._json(pointer))
    with pytest.raises(ValueError, match='does_not_match_source'):
        scope.allowed_tickers(tmp_path / 'market')


def test_import_does_not_rewrite_market_or_inventory_pointer(tmp_path):
    market = tmp_path / 'market'
    market.mkdir()
    (market / 'current.json').write_text('untouched')
    activate(tmp_path)
    assert (market / 'current.json').read_text() == 'untouched'


def test_shared_document_preserves_unknown_and_known_retrieval_proofs(tmp_path):
    from .inventory_candidates import _Cache
    from .cash_calendar_research import _native_evidence_receipts
    body = b'Exact shared document bytes'
    path = tmp_path / 'source.bin'
    path.write_bytes(body)
    original = {'file': 'source.bin', 'sha256': hashlib.sha256(body).hexdigest(),
                'url': 'https://example.test/source', 'retrieved_at': None, 'bytes': len(body)}
    later = {**original, 'retrieved_at': '2026-10-25T01:00:00Z'}
    cache = _Cache(tmp_path, {'stocks': []})
    cache.register_research_evidence(original)
    cache.register_research_evidence(later)
    receipts = cache.all_receipts()
    assert len(receipts) == 2
    assert _native_evidence_receipts([{'evidence_sources': [original]}], receipts)[0]['retrieved_at'] is None
    assert _native_evidence_receipts([{'evidence_sources': [later]}], receipts)[0]['retrieved_at'] == later['retrieved_at']
