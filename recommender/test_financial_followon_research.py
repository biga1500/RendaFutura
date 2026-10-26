from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import socket
import zipfile

import pytest

from . import financial_followon_research as followon
from .collect_market_data import financial_features
from . import inventory_financials as native


ROOT = Path(__file__).parent / 'data/market'


def _master(ticker='WIZC3'):
    values = followon.EXPECTED[ticker]
    return dict(zip(('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'), values),
                CD_CVM=str(followon.SPECS[ticker][0]))


@pytest.mark.parametrize('ticker', followon.SPECS)
def test_exact_latest_and_identical_mirror_are_accepted(ticker):
    row = _master(ticker)
    result = followon._latest_rows([row, deepcopy(row)], ticker, followon.CUTOFF)
    assert result['filing_id'] == followon.EXPECTED[ticker][2]


@pytest.mark.parametrize('field,value', [
    ('VERSAO', '2'), ('ID_DOC', '155498'), ('CNPJ_CIA', '00.000.000/0001-00'),
    ('DT_RECEB', '2026-03-20'), ('DT_REFER', '2026-06-30'),
])
def test_newer_or_conflicting_filing_never_borrows_old_note(field, value):
    old, changed = _master(), _master()
    changed[field] = value
    with pytest.raises(ValueError, match='latest'):
        followon._latest_rows([old, changed], 'WIZC3', followon.CUTOFF)


def test_future_filing_is_not_used_before_publication():
    old, future = _master(), _master()
    future.update(VERSAO='2', DT_RECEB='2026-10-01')
    assert followon._latest_rows([old, future], 'WIZC3', followon.CUTOFF)['version'] == '1'


def test_absent_filing_and_wrong_ticker_fail_closed():
    with pytest.raises(ValueError, match='absent'):
        followon._latest_rows([], 'WIZC3', followon.CUTOFF)
    with pytest.raises(ValueError, match='unreviewed_ticker'):
        followon._latest_rows([], 'WIZC4', followon.CUTOFF)


@pytest.mark.parametrize('spec', [(23590, 'PN', 2), (23590, 'ON', 4), (1023, 'ON', 2),
                                (23590.0, 'ON', 2), (23590, 'ON', 2.0), [23590, 'ON', 2]])
def test_class_sector_and_issuer_cannot_borrow_candidate(spec):
    with pytest.raises(ValueError, match='exact_wiz_identity_required'):
        followon.prepare_wiz_candidate(ROOT, spec=spec, scope_request='individual')


@pytest.mark.parametrize('scope', [None, '', 'consolidated', 'auto', True])
def test_individual_scope_must_be_explicit(scope):
    with pytest.raises(ValueError, match='explicit_whole_individual_scope_required'):
        followon.prepare_wiz_candidate(ROOT, spec=followon.SPECS['WIZC3'], scope_request=scope)


def test_wrong_cutoff_rejected_before_file_reads(tmp_path):
    with pytest.raises(ValueError, match='unreviewed_cutoff'):
        followon.latest_guard(tmp_path, cutoff=date(2026, 10, 1))


def test_receipt_never_promotes_mtime_to_retrieval(tmp_path):
    path = tmp_path / 'original.pdf'
    path.write_bytes(b'%PDF-fixture')
    source = followon._receipt(tmp_path, path, url='https://issuer.example/original.pdf')
    assert source['retrieved_at'] is None
    assert source['file_mtime_utc'] and not source['mtime_is_retrieval_or_publication']
    assert source['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_cannot_escape_root(tmp_path):
    with pytest.raises(ValueError, match='source_outside_root'):
        followon._receipt(tmp_path / 'child', tmp_path / 'outside', url='https://issuer.example')


def test_live_cached_candidate_is_conditional_whole_scope_and_read_only(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    protected = [ROOT / followon.V5_FILE,
                 ROOT / 'raw/official-financials/dfp_cia_aberta_2025.zip']
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    records, diagnostics, sources = followon.prepare_wiz_candidate(
        ROOT, spec=followon.SPECS['WIZC3'], scope_request='individual')
    values, record = financial_features(records, [], followon.CUTOFF, followon.SPECS['WIZC3'])
    assert values == pytest.approx([0, 0, 1.25745, None, 0, 688049 / 1385841, 2])
    assert record['accounts_scope'] == 'individual' and record['free_cash_per_share'] is None
    assert record['filing_id'] == '155497' and record['version'] == '1'
    assert record['financial_method']['reported_basic_eps'][0]['weighted_shares'] == '159907282'
    assert any(row['kind'] == 'conditional_handoff' and row['status'] == 'not_admitted'
               and row['automatic_integration'] is False for row in diagnostics)
    assert all(source['retrieved_at'] is None for source in sources)
    assert len({(source['file'], source['sha256'], source['url']) for source in sources}) == len(sources)
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    json.dumps((records, diagnostics, sources), allow_nan=False)


def test_current_audit_admits_nothing_and_flags_preexisting_alternative(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    report = followon.audit(ROOT)
    assert report['new_default_admissions'] == [] and report['network_requests'] == 0
    assert {row['ticker'] for row in report['findings']} == set(followon.SPECS)
    assert all(row['default_status'] == 'blocked' for row in report['findings'])
    candidate = report['conditional_candidate']
    assert not candidate['is_new_recovery'] and not candidate['automatic_integration']
    assert candidate['balance_scope'] == 'individual'
    assert candidate['requires_explicit_whole_individual_scope']
    known = [source for source in report['sources'] if source['retrieved_at'] is not None]
    assert len(known) == 3 and all('155854' in source['url'] for source in known)
    replay = candidate['offline_normal_source_and_financial_replay']
    assert replay['ordinary_one_source_route_passed']
    assert replay['normal_financial_features_and_share_basis_passed']
    assert replay['automatic_admission'] is False
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('share,eps,shares', [('ON', '2.30', '7244313000'),
                                          ('PNA', '5.82', '2864824000'),
                                          ('PNB', '11.92', '1397425000')])
def test_bpac_printed_class_eps_cannot_import_nci_into_parent_numerator(share, eps, shares):
    with zipfile.ZipFile(ROOT / 'raw/official-financials/dfp_cia_aberta_2025.zip') as archive:
        rows = [row for row in native._rows(archive, 'dfp_cia_aberta_DRE_con_2025.csv')
                if native._key(row) == (22616, '2025-12-31', '2')
                and row['ORDEM_EXERC'] == '\u00daLTIMO']
    assert native._dre_fingerprint(rows, full=True) == \
        '598ce6c4960dd21ef4730ca9663b05aa32dbad5e9ae184f36e31ff783dab0386'
    reading = {'profit_account': '3.09.01', 'parent_profit_brl': '16005964000',
               'basic_eps': [{'share_class': share, 'value': eps,
                              'numerator_brl': '16663803000', 'weighted_shares': shares,
                              'numerator_rounding_brl': '500', 'shares_rounding': '500',
                              'account': 'note:25:basic_' + share}]}
    with pytest.raises(ValueError, match='documented_eps_numerator_mismatch'):
        native._checked_note_eps(rows, reading)
