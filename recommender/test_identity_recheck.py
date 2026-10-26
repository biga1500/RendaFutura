"""Focused regression for a dropped OTC suffix and false missing-class conflict."""

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from .cvm_registry import parse_fca
from .identity_recheck import recheck_identity
from .security_registration import FCA_URL
from .test_selected_identity_research import archive, master, security


MARKET = Path(__file__).parent / 'data/market'
PINNED = 'inventory/snapshots/20261025T134516-47399429/market.json'
PINNED_SHA = 'fe442d91f3f9240ba648222a370291f7558777b06de476c55000513a5bdcb08c'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('identity_recheck_network_forbidden')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)


def fixture_source(tmp_path, *, changes=None, masters=None, securities=None):
    sec = security(**{'Codigo_Negociacao': 'EQMA3B', 'Sigla_Entidade_Administradora': 'B3',
        'Data_Inicio_Negociacao': '1997-11-13', 'Data_Inicio_Listagem': '1997-11-13', **(changes or {})})
    body = archive(masters=masters, securities=securities if securities is not None else [sec])
    path = tmp_path / 'fca.zip'
    path.write_bytes(body)
    return {'file': 'fca.zip', 'sha256': hashlib.sha256(body).hexdigest(),
            'url': FCA_URL + 'fca_cia_aberta_2026.zip', 'retrieved_at': None}


def test_missing_fields_are_repaired_without_mutation_or_isin_inference(tmp_path):
    source = fixture_source(tmp_path)
    row = {'ticker': 'EQMA3B', 'cvm': None, 'share_class': None}
    before = deepcopy((row, source))
    result = recheck_identity(tmp_path, row, source, '2026-10-24')
    assert (row, source) == before
    assert result['proposed_fields'] == {'cvm': 123, 'share_class': 'ON',
        'identity_status': 'official_cvm_fca', 'listing_start': '1997-11-13'}
    assert result['source']['retrieved_at'] is None
    assert result['source']['published_at'] is None and not result['source']['mtime_used']
    assert result['isin'] is None and not result['alias_verified']
    assert not result['integration_performed'] and not result['model_admission_granted']


@pytest.mark.parametrize('fields', [{'cvm': 999}, {'share_class': 'PNA'}])
def test_real_declared_disagreement_remains_a_conflict(tmp_path, fields):
    result = recheck_identity(tmp_path, {'ticker': 'EQMA3B', **fields},
                              fixture_source(tmp_path), '2026-10-24')
    assert result['status'] == 'conflicting_declared_identity' and not result['proposed_fields']
    assert result['conflicts'] == list(fields)


@pytest.mark.parametrize('ticker', ['EQMA3', 'ENMA3B', 'AURA33', 'MRSA5B', 'MRSA6B', 'EQPA7'])
def test_no_suffix_removal_alias_or_same_issuer_substitution(tmp_path, ticker):
    result = recheck_identity(tmp_path, {'ticker': ticker}, fixture_source(tmp_path), '2026-10-24')
    assert not result['records'] and not result['proposed_fields']


@pytest.mark.parametrize('changes', [
    {'Data_Fim_Listagem': '2026-09-30'}, {'Data_Fim_Negociacao': '2026-09-30'},
    {'Data_Inicio_Listagem': ''}, {'Data_Inicio_Negociacao': '2026-10-25'}])
def test_episode_dates_cannot_be_extended_or_invented(tmp_path, changes):
    result = recheck_identity(tmp_path, {'ticker': 'EQMA3B'},
        fixture_source(tmp_path, changes=changes), '2026-10-24')
    assert result['status'] == 'exact_identity_without_open_dated_episode'
    assert not result['proposed_fields'] and not result['whole_security_delisting_verified']


def test_later_filing_without_exact_code_does_not_reuse_earlier_identity(tmp_path):
    source = fixture_source(tmp_path, masters=[master(), master(ID_DOC='101', VERSAO='2')])
    result = recheck_identity(tmp_path, {'ticker': 'EQMA3B'}, source, '2026-10-24')
    assert not result['records'] and not result['proposed_fields']


def test_future_received_filing_does_not_prove_available_identity(tmp_path):
    source = fixture_source(tmp_path, masters=[master(DT_RECEB='2026-10-25')])
    assert not recheck_identity(tmp_path, {'ticker': 'EQMA3B'}, source, '2026-10-24')['records']


def test_unrecognized_preferred_class_is_never_guessed_from_code(tmp_path):
    source = fixture_source(tmp_path, changes={'Valor_Mobiliario': 'Acoes Preferenciais',
                                              'Sigla_Classe_Acao_Preferencial': 'ONA'})
    result = recheck_identity(tmp_path, {'ticker': 'EQMA3B'}, source, '2026-10-24')
    assert result['status'] == 'exact_share_class_not_established' and not result['proposed_fields']


def test_source_hash_and_child_master_join_fail_closed(tmp_path):
    source = fixture_source(tmp_path)
    source['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        recheck_identity(tmp_path, {'ticker': 'EQMA3B'}, source, '2026-10-24')
    source = fixture_source(tmp_path, changes={'Versao': '2'})
    with pytest.raises(ValueError, match='child_identity_mismatch'):
        recheck_identity(tmp_path, {'ticker': 'EQMA3B'}, source, '2026-10-24')


@pytest.mark.skipif(not (MARKET / PINNED).is_file(), reason='pinned local inventory unavailable')
def test_real_previously_missing_identity_is_now_read_without_cache_writes():
    body = (MARKET / PINNED).read_bytes()
    assert hashlib.sha256(body).hexdigest() == PINNED_SHA
    inventory = json.loads(body)
    row = next(r for r in inventory['stocks'] if r['ticker'] == 'EQMA3B')
    source = next(s for s in inventory['sources'] if s.get('url') == FCA_URL + 'fca_cia_aberta_2026.zip')
    raw = (MARKET / source['file']).read_bytes()
    protected = [MARKET / 'current.json', MARKET / 'inventory/current.json', MARKET / PINNED,
                 MARKET / source['file']]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    assert row['cvm'] is None and row['share_class'] is None
    parsed = parse_fca(raw, 2026, date(2026, 10, 24))['instruments']['EQMA3B']
    assert parsed['cvm'] == 16608 and parsed['share_class'] == 'ON'
    result = recheck_identity(MARKET, row, source, '2026-10-24')
    assert result['status'] == 'exact_FCA_identity_detached_view'
    assert result['proposed_fields']['cvm'] == 16608 and result['proposed_fields']['share_class'] == 'ON'
    assert result['records'][0]['csv_record'] == 208
    assert result['records'][0]['filing']['ID_DOC'] == '158240'
    assert result['records'][0]['filing']['VERSAO'] == '2'
    assert result['records'][0]['filing']['DT_RECEB'] == '2026-05-29'
    assert result['source']['sha256'] == 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b'
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
