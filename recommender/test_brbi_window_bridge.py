"""Pinned read-only BRBI first-year proof, with isolated adversarial mutations."""

import builtins
from copy import deepcopy
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from . import brbi_2021_identity as research
from . import brbi_window_bridge as bridge
from . import unit_candidates as units
from .inventory_candidates import _Cache


ROOT = Path(__file__).resolve().parent / 'data/market'
CUTOFF = date(2026, 9, 30)
START = date(2021, 9, 30)
FCA_SHA = 'b45e655c6ffae0b40ddb0425e27cbcca7d2478867f542d78c1aa034293eeb77a'
FCA_FILE = f'raw/primary-research/units/fca_cia_aberta_2022-{FCA_SHA}.zip'
ROW = {'ticker': 'BRBI11', 'cvm': 25860, 'cnpj': '10.739.356/0001-03',
       'isin': 'BRBRBICDAM10', 'sector_code': 2, 'share_class': None,
       'identity_status': 'official_cvm_fca', 'security_type': 'Units'}


@pytest.fixture(scope='module', autouse=True)
def offline_readonly():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('BRBI bridge tests must remain offline')

        for owner, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                            (socket, 'create_connection'), (socket, 'getaddrinfo'),
                            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'real cache is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


def _cache(folder=ROOT):
    return _Cache(folder, {'stocks': [deepcopy(ROW)], 'sources': []})


@pytest.fixture(scope='module')
def verified():
    proof = research.audit(ROOT, CUTOFF)
    content = (ROOT / FCA_FILE).read_bytes()
    assert hashlib.sha256(content).hexdigest() == FCA_SHA
    cache = _cache()
    tables = units._fca(cache, 2022)
    assert tables[2] == FCA_FILE and cache.receipts[FCA_FILE]['sha256'] == FCA_SHA
    return proof, content, tables


@pytest.fixture
def isolated(monkeypatch, verified):
    """Reuse a verified audit and parsed original rows; keep the real positive unmocked."""
    cache = _cache()
    cache.read(FCA_FILE, 'unit_historical_composition')
    cache.parsed[('unit_fca_tables', 2022)] = deepcopy(verified[2])

    def pinned_audit(folder, cutoff):
        assert Path(folder).resolve() == cache.folder and cutoff == CUTOFF
        return deepcopy(verified[0])

    monkeypatch.setattr(research, 'audit', pinned_audit)
    return cache


def _unit(cache):
    _, securities, _ = cache.parsed[('unit_fca_tables', 2022)]
    return next(row for row in securities if row['ID_Documento'] == '115279'
                and row['Codigo_Negociacao'] == 'BRBI11')


def _blocked(cache, reason):
    with pytest.raises(ValueError, match='^' + reason + '$'):
        bridge.first_year_bridge(deepcopy(ROW), cache, CUTOFF, START)
    assert cache.research_evidence_receipts == {}


def test_real_pinned_bridge_requires_both_primary_IPO_and_reported_FCA_listing(verified):
    proof, content, _ = verified
    protected = {FCA_FILE: content, **{s['file']: (ROOT / s['file']).read_bytes()
                                      for s in proof['sources']}}
    cache = _cache()
    result = bridge.first_year_bridge(deepcopy(ROW), cache, CUTOFF, START)
    assert result['ratios'] == {'ON': 1, 'PN': 2}
    assert result['cnpj'] == '10.739.356/0001-03'
    unit, = [r for r in result['members'] if r['Codigo_Negociacao'] == 'BRBI11']
    assert (unit['ID_Documento'], unit['Data_Referencia'], unit['Versao']) == ('115279', '2022-01-01', '2')
    assert unit['Data_Inicio_Negociacao'] == '2021-06-21'
    assert unit['Data_Inicio_Listagem'] == '2021-05-13'
    actual, = [e for e in result['evidence'] if e['role'] == 'retrospective_next_FCA_listing_corroboration']
    assert (actual['file'], actual['sha256'], actual['filing_id'], actual['published_at']) == (
        FCA_FILE, FCA_SHA, '115279', '2022-05-16')
    assert actual['year'] == 2022 and actual['reported_trading_start'] == '2021-06-21'
    primary = [e for e in result['evidence'] if e['role'] == 'primary_IPO_identity_not_scheduled_execution']
    assert {e['sha256'] for e in primary} == {s['sha256'] for s in proof['sources']}
    assert all(e['year'] == 2021 for e in primary)
    assert len(cache.research_evidence_receipts) == len(proof['sources'])
    assert result['scheduled_IPO_date_used_as_executed_trade'] is False
    assert result['prices_cash_and_financials_approved'] is False
    assert result['point_in_time_archive_verified'] is False
    assert all((ROOT / name).read_bytes() == body for name, body in protected.items())
    json.dumps(result, allow_nan=False)


def test_real_window_uses_the_bridge_but_missing_independent_identity_still_blocks_admission():
    cache = _cache()
    result = units.unit_composition_for_window(deepcopy(ROW), cache, CUTOFF)
    assert result['status'] == 'verified'
    assert (result['window_start'], result['window_end']) == ('2021-09-30', '2026-09-30')
    assert {part['share_class']: part['shares'] for part in result['components']} == {'ON': 1, 'PN': 2}
    assert result['basis'] == 'primary_IPO_identity_and_annual_FCA_constant_composition_retrospective'
    assert {e['year'] for e in result['evidence']} == set(range(2021, 2027))
    assert result['point_in_time_archive_verified'] is False
    assert not (ROOT / 'raw/expanded/b3-detail-25860.json').exists()
    with pytest.raises(ValueError, match='^unit_exact_identity_cache_missing$'):
        units.build_unit_candidate(deepcopy(ROW), cache, CUTOFF)


@pytest.mark.parametrize(('field', 'value'), [
    ('ticker', 'OTHER11'), ('cvm', 25861), ('cvm', '25860'),
    ('isin', 'BRBRBICDAM11'), ('isin', 'BRBRBIACNOR1')])
def test_wrong_exact_identity_is_rejected_before_any_evidence_read(monkeypatch, field, value):
    monkeypatch.setattr(research, 'audit', lambda *args: pytest.fail('wrong identity read proof'))
    monkeypatch.setattr(units, '_fca', lambda *args: pytest.fail('wrong identity read FCA'))
    row = {**ROW, field: value}
    with pytest.raises(ValueError, match='^brbi_window_unreviewed_exact_identity$'):
        bridge.first_year_bridge(row, _cache(), CUTOFF, START)


@pytest.mark.parametrize(('cutoff', 'start'), [
    (date(2026, 8, 31), START), (date(2026, 10, 31), START), ('2026-09-30', START),
    (CUTOFF, date(2020, 9, 30)), (CUTOFF, date(2022, 9, 30)),
    (CUTOFF, date(2021, 6, 30)), (CUTOFF, '2021-09-30')])
def test_unreviewed_cutoff_or_first_year_cannot_use_exception(monkeypatch, cutoff, start):
    monkeypatch.setattr(research, 'audit', lambda *args: pytest.fail('wrong window read proof'))
    with pytest.raises(ValueError, match='^brbi_window_unreviewed_exact_identity$'):
        bridge.first_year_bridge(deepcopy(ROW), _cache(), cutoff, start)


@pytest.mark.parametrize(('field', 'value'), [
    ('ID_DOC', '115280'), ('CNPJ_CIA', '00.000.000/0001-00'), ('DT_RECEB', '2022-05-17')])
def test_corroborating_FCA_latest_master_identity_cannot_change(isolated, field, value):
    masters, _, _ = isolated.parsed[('unit_fca_tables', 2022)]
    master = next(r for r in masters if r['ID_DOC'] == '115279')
    master[field] = value
    _blocked(isolated, 'brbi_corroborating_FCA_changed')


@pytest.mark.parametrize('field', ['Codigo_Negociacao', 'Valor_Mobiliario', 'ID_Documento'])
def test_missing_exact_unit_row_cannot_borrow_the_IPO_identity(isolated, field):
    _unit(isolated)[field] = 'OTHER'
    _blocked(isolated, 'brbi_corroborating_unit_ambiguous')


def test_duplicate_unit_is_ambiguous_even_if_both_rows_are_identical(isolated):
    _, securities, _ = isolated.parsed[('unit_fca_tables', 2022)]
    securities.append(deepcopy(_unit(isolated)))
    _blocked(isolated, 'brbi_corroborating_unit_ambiguous')


@pytest.mark.parametrize(('field', 'value'), [
    ('CNPJ_Companhia', '00.000.000/0001-00'),
    ('Composicao_BDR_Unit', '1 ON + 3 PN'), ('Composicao_BDR_Unit', '2 ON + 1 PN'),
    ('Composicao_BDR_Unit', '1 ON + 2 PNA'),
    ('Data_Inicio_Listagem', ''), ('Data_Inicio_Listagem', '2021-06-21'),
    ('Data_Fim_Negociacao', '2025-12-31'), ('Data_Fim_Listagem', '2025-12-31')])
def test_changed_2022_FCA_class_issuer_listing_or_end_dates_fail_closed(isolated, field, value):
    _unit(isolated)[field] = value
    _blocked(isolated, 'brbi_reported_listing_or_composition_changed')


@pytest.mark.parametrize('reported_start', ['', '2021-06-22', '2021-10-01', '2022-06-21'])
def test_scheduled_June21_does_not_fill_missing_or_conflicting_actual_FCA_start(isolated, verified, reported_start):
    assert verified[0]['scheduled_first_trade'] == '2021-06-21'
    assert verified[0]['actual_first_trade_independently_verified'] is False
    _unit(isolated)['Data_Inicio_Negociacao'] = reported_start
    _blocked(isolated, 'brbi_reported_listing_or_composition_changed')


@pytest.mark.parametrize('rename_to_changed_digest', [False, True])
def test_changed_2022_archive_bytes_are_blocked_even_if_renamed_to_their_own_hash(
        tmp_path, monkeypatch, verified, rename_to_changed_digest):
    proof, content, _ = verified
    changed = content + b'changed archive bytes'
    digest = hashlib.sha256(changed).hexdigest() if rename_to_changed_digest else FCA_SHA
    path = tmp_path / f'raw/primary-research/units/fca_cia_aberta_2022-{digest}.zip'
    path.parent.mkdir(parents=True)
    path.write_bytes(changed)
    monkeypatch.setattr(research, 'audit', lambda *args: deepcopy(proof))
    cache = _cache(tmp_path)
    reason = 'brbi_corroborating_FCA_original_changed' if rename_to_changed_digest else 'unit_FCA_hash_mismatch'
    _blocked(cache, reason)


def test_missing_2022_FCA_cannot_be_replaced_by_a_valid_primary_IPO_schedule(tmp_path, monkeypatch, verified):
    monkeypatch.setattr(research, 'audit', lambda *args: deepcopy(verified[0]))
    _blocked(_cache(tmp_path), 'unit_historical_FCA_2022_cache_missing')


def test_audit_failure_stops_before_FCA_and_registers_no_partial_research(monkeypatch):
    def changed_proof(*args):
        raise ValueError('brbi_evidence_changed')

    monkeypatch.setattr(research, 'audit', changed_proof)
    monkeypatch.setattr(units, '_fca', lambda *args: pytest.fail('invalid IPO proof read FCA'))
    _blocked(_cache(), 'brbi_evidence_changed')
