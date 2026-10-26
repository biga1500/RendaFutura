import json
import hashlib

import pytest

from . import audit_inventory


def fixture(tmp_path):
    snapshot = tmp_path / 'snapshots/model-one'
    snapshot.mkdir(parents=True)
    (snapshot / 'assets.json').write_text(json.dumps({'reference_date': '2026-09-30',
        'assets': [{'ticker': 'TEST3', 'asset_class': 'acoes'}]}), encoding='utf-8')
    (tmp_path / 'current.json').write_text(json.dumps({'snapshot': 'snapshots/model-one'}), encoding='utf-8')
    return {'as_of': '2026-10-25', 'collected_at': '2026-10-25T12:00:00Z', 'sources': [],
        'stocks': [{'ticker': 'TEST3', 'sector_code': 1, 'model_ready': True,
            'quote': {'status': 'available', 'value': 20, 'as_of': '2026-10-23'},
            'fundamentals': {'fields': {'earnings_per_share': {'value': 1}}},
            'payments': {'status': 'available', 'cash_model_eligible': False,
                'primary_documents_are_cash_observations': False, 'primary_documents': [], 'event_count': 1}}],
        'treasury': [], 'counts': {}, 'limitations': [], 'catalogue_minimum_payments': 0,
        'catalogue_minimum_history_months': 0, 'model_sample_unchanged': False,
        'model_preparation': {'snapshot': 'model-one'}}


def test_expanded_sample_can_pass_source_audit_when_exactly_synchronized(tmp_path, monkeypatch):
    data = fixture(tmp_path)
    monkeypatch.setattr(audit_inventory, 'load_inventory', lambda folder: data)
    result = audit_inventory.audit(tmp_path)
    assert result['passed']
    assert result['model_sample_unchanged'] is False


def test_model_only_primary_evidence_is_also_hash_checked(tmp_path, monkeypatch):
    data = fixture(tmp_path)
    raw = tmp_path / 'raw'
    raw.mkdir()
    proof = raw / 'model-proof.pdf'
    proof.write_bytes(b'primary-original')
    path = tmp_path / 'snapshots/model-one/assets.json'
    model = json.loads(path.read_text(encoding='utf-8'))
    model['sources'] = [{'file': 'model-proof.pdf', 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest()}]
    path.write_text(json.dumps(model), encoding='utf-8')
    monkeypatch.setattr(audit_inventory, 'load_inventory', lambda folder: data)
    result = audit_inventory.audit(tmp_path)
    assert result['source_files_verified'] == result['additional_model_source_files_verified'] == 1
    proof.write_bytes(b'changed-primary-original')
    with pytest.raises(AssertionError, match='digest mismatch'):
        audit_inventory.audit(tmp_path)


@pytest.mark.parametrize('problem', ['readiness', 'snapshot'])
def test_expansion_flag_does_not_allow_a_stale_inventory(tmp_path, monkeypatch, problem):
    data = fixture(tmp_path)
    if problem == 'readiness':
        data['stocks'][0]['model_ready'] = False
    else:
        data['model_preparation']['snapshot'] = 'another-model'
    monkeypatch.setattr(audit_inventory, 'load_inventory', lambda folder: data)
    with pytest.raises(AssertionError):
        audit_inventory.audit(tmp_path)
