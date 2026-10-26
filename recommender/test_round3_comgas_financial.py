"""Offline exact-filing Comgas EPS, gross-cash capex and normal-pipeline tests."""

import builtins
from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import socket
import urllib.request
from urllib.parse import urlsplit

import pytest

from . import inventory_financials as native
from . import round3_comgas_financial as comgas


@pytest.fixture(scope='module', autouse=True)
def offline_and_originals_read_only():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('Comgas verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(comgas.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'original corpus must stay read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def annual():
    return comgas.current_tables()


@pytest.fixture(scope='module')
def evidence():
    return comgas.verify_evidence()


@pytest.fixture(scope='module')
def reading():
    return comgas.get_comgas_financial_reading()[0]


@pytest.fixture(scope='module')
def replay():
    original_research, original_parser = native._research, native._parser_tables
    proposed = comgas.prepare_comgas_financials()
    assert native._research is original_research and native._parser_tables is original_parser
    restored = native.prepare_financials(comgas.ROOT, comgas.SPECS, comgas.CUTOFF)
    return proposed, restored


def table_set(annual):
    return {'_' + name: deepcopy(rows) for name, rows in annual[1].items() if name.endswith('_ind')}


def test_exact_latest_annual_individual_only_not_arbitrary_scope_override(annual):
    master, tables = annual
    assert master['ID_DOC'] == '155069' and master['VERSAO'] == '1'
    assert master['DT_RECEB'] == '2026-03-04' and master['DT_REFER'] == '2025-12-31'
    assert not any(rows for name, rows in tables.items() if name.endswith('_con'))
    assert native._dre_fingerprint(tables['DRE_ind'], full=True) == comgas.DRE_SHA
    assert native._dre_fingerprint(tables['DFC_MI_ind'], full=True) == comgas.DFC_SHA


def test_primary_FCA_literal_names_bind_only_ON_and_PN_not_suffix_inference(evidence):
    rows = evidence['class_registry']['all_issuer_security_rows']
    assert {(r['Codigo_Negociacao'], r['Valor_Mobiliario']) for r in rows if r['Codigo_Negociacao']}
    assert next(r for r in rows if r['Codigo_Negociacao'] == 'CGAS3')['Valor_Mobiliario'] == 'A\u00e7\u00f5es Ordin\u00e1rias'
    assert next(r for r in rows if r['Codigo_Negociacao'] == 'CGAS5')['Valor_Mobiliario'] == 'A\u00e7\u00f5es Preferenciais'
    assert len([r for r in rows if r['Valor_Mobiliario'].startswith('A\u00e7\u00f5es')]) == 2


def test_native_and_final_audited_PDFs_print_actual_class_EPS_numerators_and_shares(evidence):
    note = comgas.numeric_eps_proof()
    assert note['profit_allocations_brl'] == {'ON': '1186393000', 'PN': '360085000'}
    assert sum(map(Decimal, note['profit_allocations_brl'].values())) == Decimal('1546478000')
    assert [(r['share_class'], r['value'], r['weighted_shares']) for r in note['basic_eps']] == [
        ('ON', '11.4227', '103863000'), ('PN', '12.5650', '28658000')]
    assert all(r['shares_rounding'] == '500' for r in note['basic_eps'])
    assert not evidence['reading']['EPS_recomputed_or_class_ratio_applied']


def test_existing_numeric_validator_accepts_both_reported_class_allocations(annual, reading):
    rows = table_set(annual)
    interpreted = comgas.interpret_current_tables(rows, [], 'ind', reading)
    native._checked_note_eps(interpreted['_DRE_ind'], comgas.numeric_eps_proof())


def test_cash_capex_is_gross_explicit_DFC_not_net_disposals_or_accrual_additions(reading):
    capex = reading['cash_capex_alias']
    assert capex['account'] == '6.02.03' and capex['gross_cash_outflow_brl'] == '1452748000'
    assert capex['literal_audited_DFC'] == 'Adi\u00e7\u00f5es ao intang\u00edvel e ativos de contrato'
    assert capex['sale_proceeds_separate_account'] == '6.02.02' and capex['sale_proceeds_brl'] == '1638000'
    assert not capex['net_disposals_or_accrual_rollforward_used']
    final = comgas.verify_pages(comgas.ROOT, 'annual-IPE2')
    text = ' '.join(next(p['text'] for p in final['pages'] if p['pdf_page'] == 17).split())
    assert capex['literal_audited_DFC'] + ' (1.452.748)' in text
    assert 'Caixa recebido na venda de outros ativos 1.638' in text


def test_only_three_descriptions_change_no_account_value_scale_period_or_original_mutation(annual, reading):
    tables = table_set(annual)
    original = deepcopy(tables)
    diagnostics = []
    changed = comgas.interpret_current_tables(tables, diagnostics, 'ind', reading)
    differences = []
    for name in tables:
        for before, after in zip(tables[name], changed[name], strict=True):
            assert {k: v for k, v in before.items() if k != 'DS_CONTA'} == {
                k: v for k, v in after.items() if k != 'DS_CONTA'}
            if before['DS_CONTA'] != after['DS_CONTA']:
                differences.append((name, before['CD_CONTA'], before['DS_CONTA'], after['DS_CONTA']))
    assert sorted((n, c) for n, c, _, _ in differences) == sorted([
        ('_DRE_ind', '3.99.01.01'), ('_DRE_ind', '3.99.01.02'), ('_DFC_MI_ind', '6.02.03')])
    assert tables == original and len(diagnostics) == 3


@pytest.mark.parametrize(('ticker', 'eps', 'fcf', 'account'), [
    ('CGAS3', 11.4227, 15.225224308137587, '3.99.01.01'),
    ('CGAS5', 12.5650, 16.74778672570835, '3.99.01.02')])
def test_normal_prepare_financials_and_financial_features_recover_current_only(replay, ticker, eps, fcf, account):
    proposed, restored = replay
    before = json.loads((comgas.REPORTS / 'financial-comgas-before.json').read_bytes())
    after = comgas.snapshot(*proposed[:2])
    assert before[ticker]['features'] is None
    assert comgas.snapshot(*restored[:2]) == before
    selected = after[ticker]['record']
    assert selected['filing_id'] == '155069' and selected['version'] == '1'
    assert selected['accounts_scope'] == 'individual' and not selected['annualized']
    assert selected['profit_per_share'] == eps and selected['free_cash_per_share'] == pytest.approx(fcf)
    method = selected['financial_method']
    assert method['id'] == 'industrial_cash_capex_v1'
    assert method['scope_selection'] == 'individual_only_no_consolidated_core'
    assert method['profit_account'] == '3.11' and method['basic_eps_account'] == account
    assert method['capex_accounts'] == ['6.02.03'] and after[ticker]['features'][-1] == 9
    assert after[ticker]['candidate_diagnostics'][0]['reasons'] == []


def test_all_other_year_records_unchanged_no_old_filing_fallback(replay):
    proposed, restored = replay
    historical = lambda records: [r for r in records if r['fiscal_end'] != '2025-12-31']
    assert historical(proposed[0]) == historical(restored[0])
    latest = [r for r in proposed[0] if r['fiscal_end'] == '2025-12-31']
    assert len(latest) == 2 and all(r['filing_id'] == '155069' for r in latest)


@pytest.mark.parametrize(('eps', 'capex', 'reason'), [
    (True, False, 'capex_not_identified'), (False, True, 'basic_eps_absent_ambiguous_or_zero')])
def test_each_missing_interpretation_remains_a_real_gate(eps, capex, reason):
    statements, diagnostics, _ = comgas.prepare_comgas_financials(eps=eps, capex=capex)
    result = comgas.snapshot(statements, diagnostics)
    assert all(r['features'] is None for r in result.values())
    assert all(reason in r['latest_records'][0]['unavailable_reasons'] for r in result.values())


def test_all_sources_keep_actual_or_unknown_clocks_not_file_mtime(evidence):
    native_source, final, fca = evidence['sources']
    assert native_source['exact_filing_verified'] and native_source['DFP_version'] == '1'
    assert native_source['published_at'] == '2026-03-04' and native_source['retrieved_at']
    assert final['IPE_version'] == '2' and final['DFP_version_not_changed'] == '1'
    assert final['published_at'] == '2026-03-09' and final['retrieved_at']
    assert fca['retrieved_at'] is None and 'mtime' in fca['retrieval_clock_basis']


@pytest.mark.parametrize('relative', [comgas.ANNUAL, comgas.INDEX, comgas.FCA, comgas.PDF_FILE,
    comgas.IPE_FILE, comgas.READING_FILE, comgas.BASE + '/package-comgas.json',
    comgas.BASE + '/body-' + comgas.CONTAINER_SHA + '.bin',
    comgas.BASE + '/native-155069-v1-' + comgas.XML_SHA + '.xml',
    comgas.BASE + '/pages-native.json', comgas.BASE + '/pages-annual-IPE2.json',
    comgas.BASE + '/response-' + comgas.sha(comgas.URL.encode()) + '.json',
    comgas.BASE + '/response-' + comgas.sha(comgas.IPE_URL.encode()) + '.json'])
def test_changed_original_receipt_clock_PDF_XML_or_reading_is_rejected(monkeypatch, relative):
    original = Path.read_bytes
    target = comgas.ROOT / relative

    def changed(path):
        body = original(path)
        return body + b'changed' if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='comgas_financial_source_bytes_changed'):
        comgas.get_comgas_financial_reading()


@pytest.mark.parametrize(('table', 'code', 'field', 'value'), [
    ('_DRE_ind', '3.99.01.01', 'VL_CONTA', '0'),
    ('_DRE_ind', '3.99.01.02', 'DS_CONTA', 'PNA'),
    ('_DRE_ind', '3.99.01.02', 'DT_INI_EXERC', '2025-10-01'),
    ('_DFC_MI_ind', '6.02.03', 'VL_CONTA', '1452748.0000000000'),
    ('_DFC_MI_ind', '6.02.03', 'VL_CONTA', '-1451110.0000000000'),
    ('_DFC_MI_ind', '6.02.03', 'DS_CONTA', 'Adicoes liquidas de alienacoes'),
    ('_DFC_MI_ind', '6.02.02', 'VL_CONTA', '0'),
    ('_DFC_MI_ind', '6.02.01', 'VL_CONTA', 'NaN')])
def test_in_memory_class_numeric_period_netting_or_cash_leg_mutation_rejected(
        annual, reading, table, code, field, value):
    tables = table_set(annual)
    next(r for r in tables[table] if r['CD_CONTA'] == code)[field] = value
    with pytest.raises(ValueError, match='comgas_financial_in_memory_source_cells_changed'):
        comgas.interpret_current_tables(tables, [], 'ind', reading)


@pytest.mark.parametrize('specs', [{'CGAS3': (15636, 'PN', 9)}, {'CGAS5': (15636, 'ON', 9)},
    {'CGAS5': (15636, 'PNA', 9)}, {'CGAS5': (15636, 'PN', 2)},
    {'CGAS3': (15636, 'ON', 9.0)}, {'OTHER3': (15636, 'ON', 9)}])
def test_exact_class_sector_ticker_contract_not_weakened(specs):
    with pytest.raises(ValueError, match='comgas_financial_exact_codes_classes_sector_required'):
        comgas.prepare_comgas_financials(candidates=specs)


def test_wrong_cutoff_cannot_borrow_older_or_future_publication():
    with pytest.raises(ValueError, match='comgas_financial_unreviewed_cutoff'):
        comgas.get_comgas_financial_reading(cutoff=date(2025, 9, 30))


@pytest.mark.parametrize('host', ['api.mziq.com', 'b3.com.br', 'bvmf.com.br', 'bovespa.com.br',
    'ri.azzas2154.com.br', 'engie.com.br', 'allos.com.br', 'santander.com.br',
    'cdn-sites-assets.mziq.com', 'www.uol.com.br'])
def test_global_stop_before_any_collector_construction(monkeypatch, host):
    def forbidden(*args, **kwargs):
        pytest.fail('collector must not be constructed for stopped origin')

    monkeypatch.setattr(comgas, 'PublicSourceCollector', forbidden)
    with pytest.raises(ValueError, match='comgas_financial_stopped_or_unapproved_origin'):
        comgas.fetch('https://' + host + '/public-file', {})


def test_actual_network_receipts_only_public_CVM_links_no_retry_or_credentials():
    requests = [json.loads(p.read_bytes()) for p in (comgas.ROOT / comgas.BASE).glob('request-*.json')]
    assert {r['url'] for r in requests} == {
        'https://www.rad.cvm.gov.br/robots.txt', comgas.URL, comgas.IPE_URL}
    assert all(r['retries'] == 0 and urlsplit(r['url']).hostname == 'www.rad.cvm.gov.br' for r in requests)
