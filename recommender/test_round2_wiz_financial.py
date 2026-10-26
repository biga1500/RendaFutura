"""Offline current WIZ native EPS scope and ordinary-parser regression tests."""

import builtins
from datetime import date
import io
import json
from pathlib import Path
import socket
import urllib.request
from urllib.parse import urlsplit

import pytest

from . import inventory_financials as native
from . import round2_wiz_financial as wiz


@pytest.fixture(scope='module', autouse=True)
def offline():
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('WIZ verification must remain offline')

        for owner, name in ((socket, 'getaddrinfo'), (socket, 'create_connection'),
            (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
            (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            patch.setattr(owner, name, forbidden)

        def guarded(original):
            def read_only(file, mode='r', *args, **kwargs):
                if not isinstance(file, int) and Path(file).resolve().is_relative_to(wiz.ROOT):
                    assert not any(flag in mode for flag in 'wax+'), 'original corpus is read-only'
                return original(file, mode, *args, **kwargs)
            return read_only

        patch.setattr(builtins, 'open', guarded(builtins.open))
        patch.setattr(io, 'open', guarded(io.open))
        yield


@pytest.fixture(scope='module')
def annual():
    return wiz.current_tables()


@pytest.fixture(scope='module')
def review():
    return wiz.review_wiz_financial()


@pytest.fixture(scope='module')
def parser():
    return wiz.parser_snapshot()


def test_exact_latest_2025_filing_not_an_individual_scope_selection(annual):
    master, tables = annual
    assert master['ID_DOC'] == '155497' and master['VERSAO'] == '1'
    assert master['DT_REFER'] == '2025-12-31' and master['DT_RECEB'] == '2026-03-19'
    assert native._dre_fingerprint(tables['DRE_con'], full=True) == wiz.DRE_SHA
    assert all((r['DT_INI_EXERC'], r['DT_FIM_EXERC']) == ('2025-01-01', '2025-12-31')
               for r in tables['DRE_con'])


def test_consolidated_EPS_class_is_not_hidden_behind_duplicate_zero(annual):
    cells = [r for r in annual[1]['DRE_con'] if r['CD_CONTA'].startswith('3.99')]
    assert [r['CD_CONTA'] for r in cells] == ['3.99', '3.99.01', '3.99.02']
    assert not any(r['CD_CONTA'].startswith('3.99.01.') for r in cells)


def test_audited_DRE_DRA_explicit_consolidated_na_not_CSV_absence_inference(review):
    obs = review['native_observations']
    assert obs['audited_DRE']['consolidated_basic_EPS_literal'] == 'n/a'
    assert obs['audited_DRE']['consolidated_share_count_literal'] == 'n/a'
    assert obs['audited_DRA']['consolidated_EPS_literal'] == 'n/a'
    assert review['CSV_absence_is_not_used_as_PDF_absence_proof']
    assert not review['genuine_absence_in_all_possible_primary_publications_claimed']


def test_native_note_has_ON_numerator_weighted_shares_but_individual_heading(review):
    note = review['native_observations']['EPS_note']
    assert note['table_scope_literal'] == 'Controladora'
    assert note['basic_and_diluted'] == '1.25745'
    assert note['numerator_brl'] == '201076000' and note['ordinary_weighted_shares'] == '159907282'
    assert review['native_observations']['share_class']['class'] == 'ON'


def test_numeric_validator_pass_is_not_consolidated_documentary_proof(annual, review):
    result = wiz.individual_numeric_crosscheck_only(annual[1]['DRE_con'])
    assert result['numeric_crosscheck_passes']
    assert not result['consolidated_scope_proved_by_numeric_check']
    assert not result['eligible_for_reading'] and review['approved_readings'] == []


def test_joint_presentation_note_does_not_silently_relabel_EPS(review):
    note = review['native_observations']['joint_presentation']
    assert note['pdf_pages'] == [64, 65] and note['note'] == '3.1-3.3'
    assert 'does not expressly relabel' in note['interpretation']
    assert review['accounts_scope_requested_and_preserved'] == 'consolidated'
    assert not review['scope_switched']


def test_original_bytes_padding_publication_and_missing_clock_are_preserved(review):
    source, = review['sources']
    assert source['sha256'] == wiz.PDF_SHA and source['bytes'] == 16_777_216
    assert source['retrieved_at'] is None and not source['original_container_verified']
    assert source['published_at'] == '2026-03-19'
    assert source['bytes_unchanged_including_trailing_padding'] and not source['new_request']
    assert wiz.read(wiz.ROOT, source['file']) == wiz.read(wiz.ROOT, wiz.ORIGINAL_PDF)


def test_annual_IPE2_restatement_is_not_DFP_version2_or_verified_full_source(review):
    rows = wiz.annual_ipe_rows()
    assert rows[-1]['Data_Entrega'] == '2026-03-31' and rows[-1]['Versao'] == '2'
    result = review['new_IPE2_acquisition']
    assert result['url'] == rows[-1]['Link_Download'] == wiz.IPE2_URL
    assert result['IPE_version'] == '2' and result['embedded_DFP_header_version'] == '1'
    assert not result['complete_delivery'] and not result['eligible_for_reading']
    assert result['status'] == 'response_size_limit_not_a_verified_complete_source'
    assert result['bytes'] == 16_000_001 and result['retrieved_at']


def test_normal_prepare_financials_and_financial_features_unchanged(parser):
    before = json.loads((wiz.REPORTS / 'financial-wiz-before.json').read_bytes())
    assert parser == before and parser['features'] is None and parser['record'] is None
    latest, = [r for r in parser['records'] if r['fiscal_end'] == '2025-12-31']
    assert latest['filing_id'] == '155497' and latest['version'] == '1'
    assert latest['accounts_scope'] == 'consolidated' and latest['profit_per_share'] is None
    assert latest['free_cash_per_share'] is None
    assert latest['financial_method']['id'] == 'financial_no_industrial_fcf_v1'
    assert latest['unavailable_reasons'] == ['basic_eps_absent_ambiguous_or_zero']


def test_no_reading_no_gate_change_no_EPS_computation(review, monkeypatch):
    monkeypatch.setattr(wiz, 'review_wiz_financial', lambda *args: review)
    readings, sources = wiz.get_wiz_financial_readings()
    assert readings == [] and len(sources) == 1
    assert not review['EPS_computed'] and not review['model_admission'] and not review['gates_changed']
    assert review['applied_reading'] is None
    assert all(not source['model_admission'] for source in sources)


@pytest.mark.parametrize('relative', [wiz.ANNUAL, wiz.CACHED_PDF, wiz.INDEX,
    wiz.BASE + '/pages-native.json', wiz.BASE + '/pages-ipe2-incomplete.json',
    wiz.BASE + '/cached-WIZC3.json', wiz.BASE + '/body-' + wiz.IPE2_BODY_SHA + '.bin'])
def test_tampered_source_or_scope_review_is_rejected(monkeypatch, relative):
    original = Path.read_bytes
    target = wiz.ROOT / relative

    def changed(path):
        body = original(path)
        return body + b'changed' if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='wiz_financial_source_bytes_changed'):
        wiz.review_wiz_financial()


def test_unreviewed_cutoff_never_uses_stale_annual_fallback():
    with pytest.raises(ValueError, match='wiz_financial_unreviewed_cutoff'):
        wiz.current_tables(cutoff=date(2025, 9, 30))


@pytest.mark.parametrize('host', ['api.mziq.com', 'b3.com.br', 'ri.azzas2154.com.br',
    'allos.com.br', 'santander.com.br', 'www.engie.com.br', 'cdn-sites-assets.mziq.com',
    'd169uzu5o4xu1k.cloudfront.net', 'components.mz-sites.com', 'ri.wiz.co'])
def test_stopped_origin_is_rejected_before_collector_construction(monkeypatch, host):
    def forbidden(*args, **kwargs):
        pytest.fail('stopped origin must never reach collector')

    monkeypatch.setattr(wiz, 'PublicSourceCollector', forbidden)
    with pytest.raises(ValueError, match='wiz_financial_stopped_or_unapproved_origin'):
        wiz.fetch('https://' + host + '/public-document', {})


def test_receipts_are_actual_links_with_no_forbidden_network_or_retry():
    requests = [json.loads(p.read_bytes()) for p in (wiz.ROOT / wiz.BASE).glob('request-*.json')]
    assert {urlsplit(r['url']).hostname for r in requests} == {'ri.wiz.co', 'www.rad.cvm.gov.br'}
    assert all(r['retries'] == 0 for r in requests)
    assert [r['url'] for r in requests if urlsplit(r['url']).hostname == 'ri.wiz.co']
    assert sum(r['url'] == wiz.IPE2_URL for r in requests) == 1
    assert all(r['url'].endswith('/robots.txt') or r['url'] in (wiz.IPE2_URL, wiz.SIMH_IPE_URL)
               for r in requests)


@pytest.fixture(scope='module')
def simh():
    return wiz.review_simh_after_wiz()


def test_secondary_SIMH_latest_annual_and_complete_final_IPE_are_separate_versions(simh):
    assert simh['filing_id'] == '155854' and simh['version'] == '1'
    assert simh['published_at'] == '2026-03-30' and simh['accounts_scope'] == 'consolidated'
    final = simh['sources'][1]
    assert final['IPE_version'] == '3' and final['DFP_version_not_changed'] == '1'
    assert final['published_at'] == '2026-04-08' and final['complete_delivery']
    assert final['bytes'] == 8_239_462 and final['retrieved_at']
    assert final['url'] == wiz.simh_ipe_rows()[-1]['Link_Download']


def test_SIMH_consolidated_EPS_and_placeholder_proof_are_present_not_labeled_absent(simh):
    assert simh['printed_consolidated_basic_EPS'] == '0.0931' and not simh['genuine_PDF_EPS_absence']
    proof = simh['native_zero_placeholder_proof']
    assert proof['CSV_extra_basic_ON'] == '3.99.01.02 = 0'
    assert not proof['second_zero_ON_printed_in_PDF']
    assert proof['same_period_audited_consolidated_DRE_corroborates_nonzero_EPS']
    assert not proof['discard_zero_alone_approved']


def test_SIMH_continuing_and_discontinued_native_reconciliation_not_silent_root_correction(simh):
    dre = simh['new_final_native_DRE']
    assert dre['continuing_profit_brl'] == '215250000'
    assert dre['discontinued_result_brl'] == '-2609000' and dre['total_profit_brl'] == '212641000'
    assert dre['parent_profit_brl'] == '39471000' and dre['NCI_profit_brl'] == '173170000'
    assert 'No account was silently corrected' in dre['prior_apparent_total_difference_explained']
    _, rows = wiz.current_simh_tables()
    assert next(r for r in rows if r['CD_CONTA'] == '3.11')['VL_CONTA'] == '215250.0000000000'


def test_SIMH_note_conflict_survives_latest_full_primary_IPE_no_share_adjustment(simh):
    note = simh['remaining_native_note_conflict']
    assert note['printed_numerator_brl'] == '39741000' and note['DRE_parent_profit_brl'] == '39471000'
    assert note['printed_weighted_ON_shares'] == '426797758' and note['table_scope'] == 'Controladora'
    assert note['same_conflict_survives_latest_IPE3']
    assert not note['quantity_profit_or_printed_EPS_adjusted']
    assert simh['approved_readings'] == [] and simh['applied_reading'] is None


@pytest.mark.parametrize(('replace_numerator', 'error'), [
    (False, 'documented_eps_numerator_mismatch'),
    (True, 'reported_basic_eps_fails_weighted_share_crosscheck')])
def test_SIMH_existing_validator_rejects_printed_note_and_unauthorized_numerator_relabel(
        replace_numerator, error):
    _, rows = wiz.current_simh_tables()
    candidate = wiz.simh_unapproved_note_candidate()
    if replace_numerator:
        candidate['basic_eps'][0]['numerator_brl'] = '39471000'
    with pytest.raises(ValueError, match='^' + error + '$'):
        native._checked_note_eps(rows, candidate)


def test_SIMH_normal_prepare_financials_and_financial_features_still_unchanged():
    result = wiz.parser_snapshot(ticker='SIMH3')
    assert result == json.loads((wiz.REPORTS / 'financial-wiz-simh-before.json').read_bytes())
    assert result['features'] is None and result['record'] is None
    latest, = [r for r in result['records'] if r['fiscal_end'] == '2025-12-31']
    assert latest['filing_id'] == '155854' and latest['accounts_scope'] == 'consolidated'
    assert latest['profit_per_share'] is None
    assert latest['unavailable_reasons'] == ['basic_eps_absent_ambiguous_or_zero']


@pytest.mark.parametrize('relative', [wiz.BASE + '/body-' + wiz.SIMH_IPE_SHA + '.bin',
    wiz.BASE + '/cached-SIMH3-' + wiz.SIMH_PDF_SHA + '.pdf',
    wiz.BASE + '/pages-simh-IPE3.json', wiz.BASE + '/cached-SIMH3.json'])
def test_SIMH_final_native_or_cached_transport_evidence_tampering_fails_closed(monkeypatch, relative):
    original = Path.read_bytes
    target = wiz.ROOT / relative

    def changed(path):
        body = original(path)
        return body + b'changed' if path == target else body

    monkeypatch.setattr(Path, 'read_bytes', changed)
    with pytest.raises(ValueError, match='wiz_financial_source_bytes_changed'):
        wiz.review_simh_after_wiz()
