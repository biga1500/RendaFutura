from copy import deepcopy
from datetime import date
import urllib.error

import pytest

from . import targeted_cash_documents as module


CUTOFF = date(2026, 9, 30)


def document(sequence, *, cvm=123, reference='2025-04-20', published='2025-04-21'):
    return {'sequence': str(sequence), 'version': 1, 'cvm': cvm,
        'reference_date': reference, 'published_at': published,
        'url': 'https://www.rad.cvm.gov.br/document/' + str(sequence)}


def audit():
    return [{'ticker': 'TEST3', 'cvm': 123, 'mismatches': [{'record_date': '2025-04-20'}]}]


def test_targets_exact_issuer_and_nearby_reference_without_mutating_inputs():
    documents = [document(1), document(2, cvm=124), document(3, reference='2024-04-20'),
        document(4, published='2026-10-01'), document(5, reference=None), document(6)]
    original = deepcopy(documents)
    assert [item['sequence'] for item in module.select_documents(documents, audit(), CUTOFF)] == ['6', '1']
    assert documents == original


def test_deduplicates_exact_urls_not_unrelated_same_date_documents():
    assert len(module.select_documents([document(1), document(1), document(2)], audit(), CUTOFF)) == 2


@pytest.mark.parametrize('cvm', [None, 0, True, '123'])
def test_never_guesses_target_identity(cvm):
    rows = audit()
    rows[0]['cvm'] = cvm
    with pytest.raises(ValueError, match='CVM'):
        module.select_documents([], rows, CUTOFF)


def test_rejects_future_record_and_unbounded_acquisition(tmp_path):
    rows = audit()
    rows[0]['mismatches'][0]['record_date'] = '2026-10-01'
    with pytest.raises(ValueError, match='after_cutoff'):
        module.select_documents([], rows, CUTOFF)
    for bound in (0, 33, True):
        with pytest.raises(ValueError, match='bound'):
            module.acquire_targets(tmp_path, audit(), CUTOFF, max_documents=bound)
    for offset in (-1, 2001, True):
        with pytest.raises(ValueError, match='offset'):
            module.acquire_targets(tmp_path, audit(), CUTOFF, offset=offset)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_without_following_other_sources(tmp_path, monkeypatch, status):
    monkeypatch.setattr(module, 'load_inventory', lambda folder: {})
    monkeypatch.setattr(module, 'discover', lambda *args: [document(1), document(2)])
    calls = []

    def denied(folder, selected, cutoff):
        calls.append(selected['sequence'])
        raise urllib.error.HTTPError(selected['url'], status, 'Denied', {}, None)

    monkeypatch.setattr(module, 'acquire_cvm_document', denied)
    path, report = module.acquire_targets(tmp_path, audit(), CUTOFF)
    assert len(calls) == 1
    assert path.is_file()
    assert report['results'][0]['http_status'] == status
    assert not report['active_model_changed']
    assert not report['settlement_verified']
