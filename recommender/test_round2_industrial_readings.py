"""Offline exact-current native evidence and unchanged ordinary admission gates."""
from copy import deepcopy
from datetime import date
import hashlib
import json
import socket

import pytest

from . import round2_industrial_readings as industrial
from .market_inventory import load_inventory


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('industrial verification attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)


@pytest.fixture(scope='module')
def tables():
    return industrial.current_tables()


@pytest.fixture(scope='module')
def evidence():
    return industrial.assessment()


@pytest.fixture(scope='module')
def production(evidence):
    result = industrial.probe()
    body = json.dumps(result, sort_keys=True, default=str).encode()
    industrial.report(industrial.REPORTS / ('cash-industrial-test-gates-' + hashlib.sha256(body).hexdigest() + '.json'), result)
    return result


@pytest.mark.parametrize('ticker', ['WHRL4', 'EVEN3'])
def test_exact_latest_2025_master_and_nonempty_scopes(tables, ticker):
    masters, rows, _ = tables
    assert tuple(masters[ticker][k] for k in ('VERSAO', 'ID_DOC', 'DT_RECEB')) == industrial.EXPECTED[ticker]
    assert masters[ticker]['DT_REFER'] == '2025-12-31'
    assert rows[ticker]['DRE_con'] and rows[ticker]['DRE_ind']
    assert rows[ticker]['BPA_con'] and rows[ticker]['DFC_MI_con']


def test_latest_proposal_not_older_march_filing():
    plan = industrial.acquisition_plan()
    assert plan['WHRL4-latest-proposal']['sequence'] == '1028977'
    assert plan['WHRL4-latest-proposal']['Data_Entrega'] == '2026-04-13'
    assert plan['WHRL4-latest-proposal']['sequence'] != '1021051'


def test_all_new_native_original_hashes_and_publication_clocks(evidence):
    for name, source in evidence['new_primary_sources'].items():
        assert hashlib.sha256(industrial.cash.read(industrial.ROOT, source)).hexdigest() == industrial.DOCUMENTS[name][3]
        assert source['published_at'] == source['primary_index_row']['Data_Entrega']
        assert source['point_in_time_archive_verified'] is False
        assert source['original_bytes_preserved'] is True
        assert source['model_admission'] is False


def test_new_proposal_class_table_is_not_renamed_basic_eps(evidence):
    case = evidence['targets']['WHRL4']
    literal = case['new_literal_evidence']
    assert literal['ON'] == '0.4360' and literal['PN'] == '0.9461'
    assert literal['not_identified_as_basic_EPS'] is True
    assert literal['current_counts_not_transferred_as_weighted_denominators'] is True
    assert case['same_document_pooled_note']['explicit_PN_allocation'] is False
    assert case['approved_reading'] is None


def test_native_class_guard_rejects_on_eps_relabelled_pn(tables):
    rows = tables[1]['WHRL4']['DRE_con']
    reading = {'parent_profit_brl': '448526000', 'basic_eps': [{
        'share_class': 'PN', 'value': '0.2984629881', 'numerator_brl': '448526000',
        'weighted_shares': '1502786000', 'numerator_rounding_brl': '500',
        'shares_rounding': '500', 'account': '3.99.01.01'}]}
    with pytest.raises(ValueError, match='documented_native_eps_mismatch'):
        industrial.native._checked_note_eps(rows, reading)


def test_native_allocation_guard_rejects_whole_profit_for_both_classes(tables):
    reading = {'parent_profit_brl': '448526000',
        'profit_allocations_brl': {'ON': '448526000', 'PN': '448526000'},
        'basic_eps': [{'share_class': 'PN', 'value': '0.9461'}]}
    with pytest.raises(ValueError, match='documented_class_allocations_do_not_reconcile'):
        industrial.native._checked_note_eps(tables[1]['WHRL4']['DRE_con'], reading)


def test_fresh_even_release_confirms_combined_flow_not_gross_capex(evidence):
    case = evidence['targets']['EVEN3']['new_literal_evidence']
    assert case['CFO_consolidated_thousand_BRL'] == '88842'
    assert case['combined_acquisitions_disposals_consolidated_thousand_BRL'] == '12618'
    assert case['combined_acquisitions_disposals_individual_thousand_BRL'] == '-755'
    assert case['gross_consolidated_cash_acquisitions'] is None
    assert 'Acquisition (disposals)' in evidence['literal_pages']['release'][58]


def test_ordinary_parser_still_rejects_positive_acquisition_cash_leg(tables):
    rows = deepcopy(tables[1]['EVEN3']['DFC_MI_con'])
    key = industrial.native._key(rows[0])
    diagnostics = []
    parsed = industrial.native._parser_tables({'_DFC_MI_con': rows, '_DFC_MD_con': []}, diagnostics, {key})
    cash_leg = next(r for r in parsed['_DFC_MI_con'] if r['CD_CONTA'] == '6.02.01')
    assert cash_leg['VL_CONTA'] == 'NaN'
    assert any('positive_capex_requires_primary_cash_outflow_proof' in d['reasons'] for d in diagnostics)


def test_no_empty_scope_fallback_or_partial_document_admission(evidence):
    assert evidence['new_readings'] == evidence['new_admissions'] == []
    assert evidence['native_numeric_values_changed'] == []
    assert all(not c['genuinely_empty_scope'] for c in evidence['targets'].values())
    assert all(not c['model_admission'] for c in evidence['targets'].values())
    assert all(not p['partial_body_used'] for p in evidence['acquisition_limits_not_negative_document_content'])
    assert evidence['source_metadata']['scope_mixed'] is False
    assert evidence['source_metadata']['net_cash_flow_recast_as_gross_capex'] is False


@pytest.mark.parametrize('url', ['https://api.mziq.com/new', 'https://www.b3.com.br/new',
    'https://cdn-sites-assets.mziq.com/new'])
def test_denied_origins_rejected_before_collector(url, monkeypatch):
    monkeypatch.setattr(industrial, 'PublicSourceCollector', lambda *args: pytest.fail('collector constructed'))
    with pytest.raises(ValueError, match='denied_or_globally_stopped_origin'):
        industrial.acquire(url, {'kind': 'test'})


def test_previous_exact_timeout_not_retried(monkeypatch):
    url = 'https://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=155763'
    monkeypatch.setattr(industrial, 'prior_attempts', lambda root: {url: 'old_timeout'})
    monkeypatch.setattr(industrial, 'PublicSourceCollector', lambda *args: pytest.fail('retry attempted'))
    with pytest.raises(ValueError, match='previous_exact_request_not_retried'):
        industrial.acquire(url, {'kind': 'test'})


def test_stopped_api_cached_original_remains_readable():
    source = industrial.CACHED['EVEN3']
    assert source['url'].startswith('https://api.mziq.com/')
    assert industrial.cash.read(industrial.ROOT, source).startswith(b'%PDF')


def test_future_cutoff_not_implicitly_reused():
    with pytest.raises(ValueError, match='unreviewed_cutoff'):
        industrial.current_tables(cutoff=date(2026, 10, 1))


@pytest.mark.parametrize('field,value', [('cvm', 1), ('share_class', 'ON'), ('isin', 'BRWRONGCNPR1'), ('sector_code', 5)])
def test_changed_instrument_identity_rejected_before_financials(field, value, monkeypatch):
    inventory = deepcopy(load_inventory(industrial.ROOT))
    next(r for r in inventory['stocks'] if r['ticker'] == 'WHRL4')[field] = value
    monkeypatch.setattr('recommender.market_inventory.load_inventory', lambda *args: inventory)
    with pytest.raises(ValueError, match='instrument_identity_changed'):
        industrial.probe()


@pytest.mark.parametrize('ticker,reason', [('WHRL4', 'basic_eps_absent_ambiguous_or_zero'),
                                         ('EVEN3', 'invalid_numeric_accounts')])
def test_actual_production_one_pass_financial_pending_no_fallback(production, ticker, reason):
    row = next(r for r in production['targets'] if r['ticker'] == ticker)
    assert row['one_pass'] and not row['current_financial_pass']
    assert len(row['latest_records']) == 1
    current = row['latest_records'][0]
    assert current['filing_id'] == industrial.EXPECTED[ticker][1]
    assert current['accounts_scope'] == 'consolidated' and current['fiscal_end'] == '2025-12-31'
    assert reason in current['unavailable_reasons']
    assert current['profit_per_share'] is None
    assert production['financial_hooks_overridden'] == []
    assert production['network_calls'] == production['acquisitions'] == 0
