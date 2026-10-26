"""Research invariants, exact dates and cached selected-universe regressions."""

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from . import selected_price_research as research
from .dividend_scope import allowed_tickers


ROOT = Path(__file__).parent / 'data/market'


def chart(points, actions=()):
    stamps = [int(datetime.combine(d, datetime.min.time(), timezone.utc).timestamp()) for d, _ in points]
    splits = {str(i): {'date': int(datetime.combine(d, datetime.min.time(), timezone.utc).timestamp()),
                       'numerator': n, 'denominator': den}
              for i, (d, n, den) in enumerate(actions)}
    return json.dumps({'chart': {'result': [{
        'meta': {'symbol': 'BAZA3.SA', 'currency': 'BRL', 'dataGranularity': '1d'},
        'timestamp': stamps, 'indicators': {'quote': [{'close': [v for _, v in points]}]},
        'events': {'splits': splits}}]}}).encode()


def row(day, quote='20,00', record=None):
    return {'typeStock': 'ON', 'corporateAction': 'DIVIDENDO', 'valueCash': '0,10',
            'quotedPerShares': '1', 'dateClosingPricePriorExDate': day,
            'lastDatePriorEx': record or day, 'closingPricePriorExDate': quote}


def monthly_points():
    return [(research.shift_month(research.END, -i), 10.) for i in reversed(range(61))]


@pytest.fixture(scope='module')
def report():
    with research.offline():
        return research.build_report(ROOT)


def test_scope_is_mandatory_and_filters_before_research():
    allowed = allowed_tickers(ROOT)
    assert len(allowed) == 262
    audit = {'pending': [{'ticker': 'BAZA3', 'reason': next(iter(research.REASONS))},
                         {'ticker': 'CSAN3', 'reason': next(iter(research.REASONS))}]}
    assert [r['ticker'] for r in research.selected_pending(audit, allowed)] == ['BAZA3']
    with pytest.raises(ValueError, match='selected262'):
        research.selected_pending(audit, None)


def test_network_guard_denies_http_socket_and_dns():
    with research.offline():
        with pytest.raises(AssertionError, match='network_forbidden'):
            urllib.request.urlopen('https://example.invalid')
        with socket.socket() as sock:
            with pytest.raises(AssertionError, match='network_forbidden'):
                sock.connect(('127.0.0.1', 1))
        with pytest.raises(AssertionError, match='network_forbidden'):
            socket.getaddrinfo('example.invalid', 443)


def test_missing_exact_session_never_uses_adjacent_price():
    points = monthly_points() + [(date(2022, 4, 28), 20.)]
    diagnostic = research.diagnose(chart(points), 'BAZA3', [row('29/04/2022')], 'ON')
    anchor, = diagnostic['explicit_anchors']
    assert anchor['date'] == '2022-04-29'
    assert anchor['daily_close_BRL'] is None
    assert anchor['all_raw_values_match'] is False
    assert anchor['all_split_adjusted_values_match'] is False


def test_missing_date_is_not_promoted_from_record_date():
    diagnostic = research.diagnose(chart(monthly_points()), 'BAZA3',
                                  [row(None, record='30/09/2021')], 'ON')
    assert diagnostic['explicit_anchor_count'] == 0
    unknown, = diagnostic['unknown_quote_date_rows']
    assert unknown['explicit_quote_date'] is None
    assert unknown['parent_fallback_date'] == '2021-09-30'
    assert unknown['record_date_substituted_by_sidecar'] is False
    assert unknown['parent_fallback_comparison_not_accepted_evidence']['daily_close_BRL'] == 10


def test_sentinel_conflict_is_retained_without_a_fabricated_quote_date():
    rows = [row('31/12/9999', '30,89', '17/12/2007'),
            row('31/12/9999', '143,11', '07/03/2006')]
    diagnostic = research.diagnose(chart(monthly_points()), 'BAZA3', rows, 'ON')
    assert diagnostic['baseline_replay']['reason'] == 'conflicting_B3_reference_quotes'
    assert diagnostic['explicit_anchors'] == []
    assert diagnostic['conflicting_explicit_quote_dates'] == []
    assert diagnostic['parent_reference_conflicts_including_sentinels'][0]['is_unavailable_date_sentinel']
    assert diagnostic['old_sentinel_only_trial']['reason'] == 'insufficient_independent_price_matches'
    assert len(diagnostic['old_sentinel_corrections_applied_to_trial_only']) == 2
    assert diagnostic['source_candidate_closed'] is False
    assert rows[0]['closingPricePriorExDate'] == '30,89'


def test_four_late_anchors_do_not_establish_early_share_regime():
    points = monthly_points()
    days = ('31/01/2022', '28/02/2022', '31/03/2022', '30/04/2022')
    diagnostic = research.diagnose(chart(points, [(date(2021, 12, 3), 2, 1)]),
                                  'BAZA3', [row(d, '10,00') for d in days], 'ON')
    assert diagnostic['baseline_replay']['reason'] == 'unverified_share_price_regime_requires_raw_history'
    regime, = diagnostic['missing_explicitly_observed_regimes']
    assert regime['reported_factor'] == 2
    assert regime['monthly_dates'] == ['2021-09-30', '2021-10-31', '2021-11-30']
    assert len(diagnostic['required_61_monthly_anchors']) == 61


def test_duplicate_dates_and_wrong_exact_identity_fail():
    with pytest.raises(ValueError, match='duplicate_daily_price_dates'):
        research.daily_input(chart([(date(2022, 1, 1), 10), (date(2022, 1, 1), 11)]), 'BAZA3')
    with pytest.raises(ValueError, match='exact_daily_identity'):
        research.daily_input(chart(monthly_points()), 'BGIP3')


def test_latest_document_identity_required():
    base = {'cvm': 8133, 'cnpj': '92754738000162', 'after_cutoff': False,
            'index_row': {'Data_Referencia': '2024-12-11', 'Categoria': 'Aviso', 'Tipo': '', 'Especie': ''}}
    rows = {'v1': {**base, 'version': 1}, 'v2': {**base, 'version': 2}}
    with pytest.raises(ValueError, match='superseded_primary'):
        research.verify_action_index({'url': 'v1'}, rows, 8133, '92754738000162')
    with pytest.raises(ValueError, match='identity_or_cutoff'):
        research.verify_action_index({'url': 'v2'}, rows, 1325, '17184037000110')
    assert research.verify_action_index({'url': 'v2'}, rows, 8133, '92754738000162')['version'] == 2


def test_receipts_and_report_writes_are_hash_checked_and_confined(tmp_path):
    path = research.write_report(tmp_path, {'source_candidates_closed': [], 'goal_complete': False})
    assert path.parent == tmp_path / research.CACHE
    assert research.write_report(tmp_path, {'source_candidates_closed': [], 'goal_complete': False}) == path
    evidence = research.Evidence(tmp_path)
    relative = path.relative_to(tmp_path).as_posix()
    evidence.read(relative, hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match='hash_mismatch'):
        evidence.read(relative, '0' * 64)
    for unsafe in ('../current.json', 'C:/current.json', '/current.json', 'raw\\current.json'):
        with pytest.raises(ValueError, match='unsafe_selected_price_path'):
            evidence.read(unsafe)
    assert not (tmp_path / 'current.json').exists()


def test_cached_audit_has_all_and_only_selected_pending(report):
    assert report['pending_reason_counts'] == {
        'historical_quote_or_share_scale_disagrees_with_B3': 13,
        'unverified_share_price_regime_requires_raw_history': 9,
        'insufficient_independent_price_matches': 4, 'conflicting_B3_reference_quotes': 2}
    allowed = allowed_tickers(ROOT)
    assert len(report['records']) == 28
    assert all(r['ticker'] in allowed for r in report['records'])
    assert not {'PSSA3', 'CSAN3', 'CSNA3', 'TUPY3'} & {r['ticker'] for r in report['records']}
    assert report['baseline_model_ready_stocks'] == 100
    assert report['baseline_actual_treasury_records'] == 37
    assert report['protected_pointers_unchanged']
    assert report['input_files_changed_during_report'] == []
    assert report['network_requests'] == report['prices_imputed'] == report['model_admissions'] == 0
    assert not report['financial_validation_performed']
    assert not report['goal_complete']
    sources = {s['file']: s for s in report['sources']}
    for record in report['records']:
        assert len(record['diagnostic']['required_61_monthly_anchors']) == 61
        assert record['diagnostic']['missing_months'] == []
        assert record['source_files'] and all(name in sources for name in record['source_files'])
        assert record['next_safe_source_work']
        assert record['diagnostic']['baseline_replay']['reason'] == record['baseline_reason']


def test_cached_focus_remains_blocked_on_observed_evidence(report):
    focus = {r['ticker']: r for r in report['focused_candidate_evidence_closures']}
    assert set(focus) == {'LREN3', 'BMEB4', 'DEXP3'}
    assert all(not r['source_candidate_closed'] and not r['financially_validated'] for r in focus.values())
    assert report['source_candidates_closed'] == []
    lren = focus['LREN3']
    assert lren['primary_action_chain_verified']
    assert {r['date'] for r in lren['material_daily_source_conflicts']} == {
        '2022-05-16', '2023-12-21', '2024-08-30'}
    assert [r['date'] for r in lren['material_daily_source_conflicts'] if r['used_as_required_month_end']] == ['2024-08-30']
    assert lren['unchanged_gate_trial_using_all_native_dates']['reason'] == 'historical_quote_or_share_scale_disagrees_with_B3'
    assert not lren['favorable_date_subset_selected']
    assert len(lren['all_daily_source_differences']) == 7
    assert focus['BMEB4']['literal_exact_code_links_discovered_in_original_body_not_requested'] == [
        'https://ri.bancomercantil.com.br/informacoes-aos-investidores/graficos-e-cotacoes-bmeb4/']
    assert not focus['BMEB4']['native_action_ratio_or_ex_date_verified']
    assert not focus['DEXP3']['native_action_ratio_or_ex_date_verified']
