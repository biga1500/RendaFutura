"""Offline regressions against the selected issuers' actual cached 2025 DFP.

The current evidence authorizes no new admission. CSV row numbers below are
one-based physical lines in the pinned native ZIP, including its header.
"""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
from pathlib import Path
import socket
import zipfile

import pytest

from . import inventory_financials as native
from .collect_market_data import financial_features
from .test_inventory_financials import annual, cache, proposed_pine_reading


ROOT = Path(__file__).parent / 'data/market'
ANNUAL_FILE = 'raw/official-financials/dfp_cia_aberta_2025.zip'
ANNUAL_SHA256 = '5e273568aae97d1301c852e9f5d2ed024584beab31ee82a65eff47478ffdf34f'
CUTOFF = date(2026, 9, 30)
CASES = {
    'BPAC3': ((22616, 'ON', 2), '155648', '2', '2026-03-24', '3.09.01', '2.08'),
    'EVEN3': ((20524, 'ON', 4), '155582', '1', '2026-03-23', '3.11.01', None),
    'PINE4': ((20567, 'PN', 2), '155965', '3', '2026-03-31', '3.11.01', '2.07'),
    'SIMH3': ((25003, 'ON', 2), '155854', '1', '2026-03-30', '3.11.01', '2.03'),
    'WHRL4': ((14346, 'PN', 4), '155763', '1', '2026-03-27', '3.11.01', None),
    'WIZC3': ((23590, 'ON', 2), '155497', '1', '2026-03-19', '3.11.01', '2.03'),
}
SPECS = {ticker: case[0] for ticker, case in CASES.items()}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('selected financial checks must not access the network')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


@pytest.fixture(scope='module')
def cached_native():
    content = (ROOT / ANNUAL_FILE).read_bytes()
    assert hashlib.sha256(content).hexdigest() == ANNUAL_SHA256
    requested = {spec[0] for spec in SPECS.values()}
    tables = {}
    with zipfile.ZipFile(ROOT / ANNUAL_FILE) as archive:
        for suffix in ('', *native.TABLES,
                       '_DRE_ind', '_BPA_ind', '_BPP_ind', '_DFC_MI_ind'):
            tables[suffix] = [
                (line, row) for line, row in enumerate(
                    native._rows(archive, f'dfp_cia_aberta{suffix}_2025.csv'), 2)
                if int(row['CD_CVM']) in requested]
    return content, tables


def current_rows(cached_native, ticker, suffix):
    spec, _, version, *_ = CASES[ticker]
    return [(line, row) for line, row in cached_native[1][suffix]
            if (int(row['CD_CVM']), row['DT_REFER'], row['VERSAO'])
            == (spec[0], '2025-12-31', version)
            and row.get('ORDEM_EXERC', '\u00daLTIMO') == '\u00daLTIMO']


def account(cached_native, ticker, suffix, code):
    matches = [(line, row) for line, row in current_rows(cached_native, ticker, suffix)
               if row['CD_CONTA'] == code]
    assert len(matches) == 1
    return matches[0]


def isolate_cache(folder, cached_native):
    path = folder / ANNUAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cached_native[0])
    for filename, digest in ((native.RESEARCH_FILE, native.RESEARCH_SHA256),
                             *native.ADDITIONAL_RESEARCH):
        body = (ROOT / filename).read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest
        target = folder / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


@pytest.fixture(scope='module')
def prepared(cached_native, tmp_path_factory):
    folder = tmp_path_factory.mktemp('selected-financial-cache')
    isolate_cache(folder, cached_native)
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
        result = native.prepare_financials(folder, SPECS, CUTOFF)
    assert before == {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    return result


@pytest.mark.parametrize('ticker', CASES)
def test_latest_exact_class_and_scope_stay_unavailable(prepared, cached_native, ticker):
    statements, diagnostics, _ = prepared
    spec, filing_id, version, published, profit, equity = CASES[ticker]
    master = current_rows(cached_native, ticker, '')
    assert len(master) == 1
    assert (master[0][1]['ID_DOC'], master[0][1]['DT_RECEB']) == (filing_id, published)
    record = max((row for row in statements if (row['cvm'], row['share_class']) == spec[:2]),
                 key=lambda row: (row['fiscal_end'], int(row['version'])))
    assert (record['filing_id'], record['version'], record['published_at']) == (
        filing_id, version, published)
    assert record['fiscal_end'] == '2025-12-31'
    assert record['period_type'] == 'annual' and record['annualized'] is False
    assert record['accounts_scope'] == 'consolidated'
    assert record['profit_per_share'] is None and record['free_cash_per_share'] is None
    assert financial_features(statements, [], CUTOFF, spec) is None
    method = record['financial_method']
    assert (method['profit_account'], method['equity_account']) == (profit, equity)
    assert method['scope_selection'] == 'consolidated_core_present'
    assert all('_con_' in name for name in method['source_statements'])
    assert method['id'] == ('financial_no_industrial_fcf_v1' if spec[2] == 2
                            else 'industrial_cash_capex_v1')
    filing = next(row for row in diagnostics if row.get('kind') == 'filing'
                  and (row['cvm'], row['share_class'], row['filing_id'])
                  == (spec[0], spec[1], filing_id))
    assert filing['missing_accounts'] == []
    assert filing['invalid_numeric_accounts'] == (['6.02.01'] if ticker == 'EVEN3' else [])
    assert filing['reasons'] == (['invalid_numeric_accounts'] if ticker == 'EVEN3'
                                 else ['basic_eps_absent_ambiguous_or_zero'])


@pytest.mark.parametrize('ticker', CASES)
def test_native_current_flows_are_annual_not_a_period_mapping_fix(cached_native, ticker):
    for suffix in native.TABLES[:3]:
        for _, row in current_rows(cached_native, ticker, suffix):
            assert row['MOEDA'] == 'REAL'
            assert (row['DT_INI_EXERC'], row['DT_FIM_EXERC']) == ('2025-01-01', '2025-12-31')


def test_even_positive_net_disposal_is_not_gross_acquisition_or_individual_cash(prepared, cached_native):
    # Audited DFC PDF p35: acquisition/disposal net; no gross con outflow supplied.
    line, row = account(cached_native, 'EVEN3', '_DFC_MI_con', '6.02.01')
    assert (line, row['VL_CONTA']) == (34173, '12618.0000000000')
    _, individual = account(cached_native, 'EVEN3', '_DFC_MI_ind', '6.02.01')
    assert individual['VL_CONTA'] == '-755.0000000000'
    assert account(cached_native, 'EVEN3', '_DRE_con', '3.99.01.01')[1]['VL_CONTA'] == '1.4500000000'
    excluded = [row for row in prepared[1] if row.get('cvm') == SPECS['EVEN3'][0]
                and 'positive_capex_requires_primary_cash_outflow_proof' in row['reasons']]
    assert len(excluded) == 1 and excluded[0]['account'] == '6.02.01'
    assert excluded[0]['reported_value'] == '12618.0000000000'


@pytest.mark.parametrize('ticker,line,value', [
    ('BPAC3', 17171, '0.0000000000'), ('PINE4', 26193, '186223.0000000000'),
    ('WIZC3', 20701, '0.0000000000')])
def test_generic_eps_roots_are_not_class_eps(cached_native, ticker, line, value):
    found_line, root = account(cached_native, ticker, '_DRE_con', '3.99.01')
    assert (found_line, root['VL_CONTA']) == (line, value)
    assert not any(row['CD_CONTA'].startswith('3.99.01.')
                   for _, row in current_rows(cached_native, ticker, '_DRE_con'))


@pytest.mark.parametrize('ticker,parent,numerator,shares,eps,profit_account', [
    ('BPAC3', '16005964000', '16663803000', '7244313000', '2.30', '3.09.01'),
    ('SIMH3', '39471000', '39741000', '426797758', '0.0931', '3.11.01')])
def test_printed_note_cannot_replace_a_different_parent_numerator(
        cached_native, ticker, parent, numerator, shares, eps, profit_account):
    # BPAC note 25 p66 includes NCI; SIMH note 32.1 p140 is Controladora.
    rows = [row for _, row in current_rows(cached_native, ticker, '_DRE_con')]
    before = deepcopy(rows)
    review = {'parent_profit_brl': parent, 'profit_account': profit_account,
              'basic_eps': [{'share_class': 'ON', 'value': eps, 'numerator_brl': numerator,
                             'numerator_rounding_brl': '500', 'weighted_shares': shares,
                             'shares_rounding': '0', 'account': 'note:basic_eps:ON'}]}
    with pytest.raises(ValueError, match='documented_eps_numerator_mismatch'):
        native._checked_note_eps(rows, review)
    assert rows == before


def test_pine_printed_pn_allocations_include_nci(cached_native):
    rows = [row for _, row in current_rows(cached_native, 'PINE4', '_DRE_con')]
    with pytest.raises(ValueError, match='documented_class_allocations_do_not_reconcile'):
        native._checked_note_eps(rows, proposed_pine_reading())
    assert Decimal('221909000') + Decimal('210969000') - Decimal('430515000') == Decimal('2363000')


def test_simh_duplicate_on_and_total_discontinued_difference_are_preserved(cached_native):
    first = account(cached_native, 'SIMH3', '_DRE_con', '3.99.01.01')
    second = account(cached_native, 'SIMH3', '_DRE_con', '3.99.01.02')
    assert (first[0], first[1]['DS_CONTA'], first[1]['VL_CONTA']) == (6785, 'ON', '0.0931000000')
    assert (second[0], second[1]['DS_CONTA'], second[1]['VL_CONTA']) == (6787, 'ON', '0.0000000000')
    amounts = {code: native._brl(account(cached_native, 'SIMH3', '_DRE_con', code)[1])
               for code in ('3.11', '3.11.01', '3.11.02')}
    assert amounts['3.11'] - amounts['3.11.01'] - amounts['3.11.02'] == 2609000


def test_whrl_reported_on_never_supplies_pn(cached_native):
    line, row = account(cached_native, 'WHRL4', '_DRE_con', '3.99.01.01')
    assert (line, row['DS_CONTA'], row['VL_CONTA']) == (23503, 'ON', '0.2984629881')
    assert not any(row['DS_CONTA'] == 'PN' and row['CD_CONTA'].startswith('3.99.01.')
                   for _, row in current_rows(cached_native, 'WHRL4', '_DRE_con'))


def test_wiz_individual_eps_is_present_but_is_not_consolidated(cached_native):
    _, eps = account(cached_native, 'WIZC3', '_DRE_ind', '3.99.01.01')
    assert (eps['DS_CONTA'], eps['VL_CONTA']) == ('ON', '1.2574500000')
    assert native._brl(account(cached_native, 'WIZC3', '_BPA_ind', '1')[1]) == 1385841000
    assert native._brl(account(cached_native, 'WIZC3', '_BPA_con', '1')[1]) == 2434061000


def test_unavailable_latest_cannot_fall_back_to_an_older_usable_year(cached_native, tmp_path):
    isolate_cache(tmp_path, cached_native)
    older = [annual(year=2024, cvm=spec[0], published='2025-03-01')
             for spec in SPECS.values()]
    cache(tmp_path, *older, year=2024)
    statements, _, _ = native.prepare_financials(tmp_path, SPECS, CUTOFF)
    for ticker, spec in SPECS.items():
        earlier = financial_features(statements, [], date(2025, 12, 31), spec)
        assert earlier is not None and earlier[1]['fiscal_end'] == '2024-12-31'
        assert financial_features(statements, [], CUTOFF, spec) is None
