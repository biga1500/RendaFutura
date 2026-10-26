"""Focused offline fixtures; never enumerate the live multi-agent cache tree."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import urllib.request

import pytest

from . import aalr_zero_followon as followon
from .alternative_cash_research import Reader
from .zero_distribution_research import summarize_periods


def cash(code='6.03.06', label='Dividendos pagos', value='0', **changes):
    return {'CD_CONTA': code, 'DS_CONTA': label, 'VL_CONTA': value,
            'MOEDA': 'REAL', 'ESCALA_MOEDA': 'MIL', **changes}


def both_zero():
    return [cash(), cash('6.03.09', 'Juros sobre o capital proprio pagos')]


def period(start='2026-01-01', end='2026-06-30', **changes):
    return {'scope': 'individual', 'period_start': start, 'period_end': end,
            'published_at': '2026-08-13', 'document_reference': end,
            'version': 1, 'filing_id': 'example', 'table': 'itr_cia_aberta_DFC_MI_ind_2026.csv',
            **followon.evaluate_explicit_paid(both_zero()), **changes}


def test_paid_zero_requires_both_components_and_keeps_precision():
    result = followon.evaluate_explicit_paid(both_zero())
    assert result['status'] == 'reported_zero_both_components'
    assert all(row['reporting_unit_brl'] == 1000 for row in result['rows'])
    assert result['exact_unrounded_cash_zero_verified'] is False
    assert result['individual_events_generated'] == 0
    assert followon.evaluate_explicit_paid([cash()])['status'] == 'pending'


@pytest.mark.parametrize('label', ['Dividendos', 'Dividendos declarados', 'Dividendos a pagar',
                                  'Dividendos propostos', 'Dividendo adicional proposto'])
def test_not_explicit_paid_never_becomes_zero(label):
    result = followon.evaluate_explicit_paid([cash(label=label), both_zero()[1]])
    assert result['status'] == 'pending'


def test_equity_movements_blank_or_missing_table_are_unknown():
    assert followon.evaluate_explicit_paid([])['status'] == 'pending'
    dmpl = [cash('5.04.06'), cash('5.04.07', 'Juros sobre capital proprio')]
    assert followon.evaluate_explicit_paid(dmpl)['status'] == 'pending'


def test_no_netting_opposing_or_receipts_and_outflows():
    opposing = [cash(value='-12'), cash('6.03.09', 'Juros sobre o capital proprio pagos', '12')]
    assert followon.evaluate_explicit_paid(opposing)['status'] == 'pending'
    received = cash('6.01.03.03', 'Dividendos recebidos de controladas', '12')
    result = followon.evaluate_explicit_paid([cash(value='-12'), both_zero()[1], received])
    assert result['status'] == 'known_positive_reported_outflow'
    assert Decimal(result['rows'][0]['reported_value_brl']) == -12000


def test_nci_not_assigned_to_current_class():
    nci = cash('6.03.10', 'Dividendos pagos para acionistas nao controladores', '-382')
    result = followon.evaluate_explicit_paid(both_zero() + [nci])
    assert result['status'] == 'reported_zero_both_components'
    assert nci in result['ignored_rows']
    assert len(result['rows']) == 2


@pytest.mark.parametrize('changes', [{'MOEDA': 'DOLAR'}, {'ESCALA_MOEDA': 'UNKNOWN'}, {'VL_CONTA': 'NaN'}])
def test_invalid_currency_scale_or_value_is_unknown(changes):
    rows = both_zero()
    rows[0].update(changes)
    assert followon.evaluate_explicit_paid(rows)['status'] == 'pending'


def test_duplicate_accounts_are_not_zero():
    assert followon.evaluate_explicit_paid(both_zero() + [cash()])['status'] == 'pending'


def test_specific_payable_cannot_be_hidden_by_generic_zero_or_netting():
    rows = [cash('2.01.05.02.01', 'Dividendos e JCP a Pagar'),
            cash('2.01.05.02.09', 'Dividendos a pagar', '72'),
            cash('2.01.05.02.10', 'JCP a pagar', '-72')]
    result = followon.payable_review(rows)
    assert len(result['payable_rows']) == 3
    assert [Decimal(r['reported_value_brl']) for r in result['nonzero_payable_rows']] == [72000, -72000]
    assert result['prior_payable_settlement_excluded'] is False
    assert result['holder_class_or_NCI_attribution_verified'] is False
    assert result['nonzero_liability_does_not_prove_payment'] is True


def test_zero_payables_alone_cannot_establish_settlement_history():
    result = followon.payable_review([cash('2.01.05.02.01', 'Dividendos e JCP a Pagar')])
    assert result['nonzero_payable_rows'] == []
    assert result['prior_payable_settlement_excluded'] is False


def test_no_quarter_extrapolation_or_month_cash_imputation():
    result = summarize_periods([period()], date(2026, 1, 1), date(2026, 9, 30))
    assert result['unresolved_months'] == ['2026-07', '2026-08', '2026-09']
    assert result['zero_supported_full_month_count'] == 6
    assert all(row['value_brl'] is None and not row['individual_payment_events_inferred'] for row in result['months'])


def test_consolidated_zero_never_fills_individual_gap():
    result = summarize_periods([period(scope='consolidated')], date(2026, 1, 1), date(2026, 9, 30))
    assert result['zero_supported_full_month_count'] == 0


def test_future_publication_excluded_and_not_substituted_for_cutoff():
    rows = [{'CD_CVM': '024058', 'CNPJ_CIA': followon.ISSUERS[24058][1],
             'DT_REFER': '2026-09-30', 'DT_RECEB': '2026-11-13', 'VERSAO': '1'}]
    assert followon._latest(rows) == {}
    rows[0].update(DT_REFER='2025-12-31', DT_RECEB='2026-07-30')
    assert followon._latest(rows)[24058, '2025-12-31']['DT_RECEB'] == '2026-07-30'


@pytest.fixture(scope='module')
def cloned_inputs(tmp_path_factory):
    source = Path(__file__).parent / 'data/market'
    clone = tmp_path_factory.mktemp('exact-required-inputs')
    for name, expected in followon.INPUTS.items():
        body = (source / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == expected, name
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(body)
    return clone


@pytest.fixture(scope='module')
def real_report(cloned_inputs):
    def no_network(*args, **kwargs):
        raise AssertionError('offline audit attempted network')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, 'create_connection', no_network)
        patch.setattr(urllib.request.OpenerDirector, 'open', no_network)
        return followon.audit(cloned_inputs)


def test_real_exact_gap_and_current_class(real_report):
    aalr = real_report['AALR3']
    assert aalr['cash_coverage']['zero_supported_full_month_count'] == 51
    assert aalr['missing_months'] == [f'2026-{m:02}' for m in range(1, 10)]
    assert len(aalr['cash_coverage']['months']) == 60
    identity = real_report['identity']['AALR3']
    assert identity['share_class'] == 'ON'
    assert identity['security']['Codigo_Negociacao'] == 'AALR3'
    assert identity['filing']['ID_DOC'] == '161137'
    assert identity['filing']['DT_RECEB'] == '2026-08-28'


def test_real_five_annual_individual_paid_periods_have_exact_provenance(real_report):
    periods = real_report['AALR3']['cash_coverage']['selected_individual_periods']
    assert len(periods) == 5
    for item in periods:
        assert item['scope'] == 'individual'
        assert item['published_at'] <= followon.CUTOFF
        assert item['temporal_qualification'].endswith('not_month_PIT')
        assert {row['cash_component'] for row in item['rows']} == {'dividends', 'jcp'}
        for row in item['rows']:
            assert Decimal(row['reported_value_brl']) == 0
            assert row['ESCALA_MOEDA'] == 'MIL'
            assert row['evidence']['csv_record_number_including_header'] > 1
            assert row['evidence']['archive_sha256'] == followon.INPUTS[row['evidence']['source_file']]
    assert periods[-1]['filing_id'] == '159790'


def test_real_hidden_specific_prior_payable_and_dmpl_zero_retained(real_report):
    aalr = real_report['AALR3']
    nonzero = aalr['prior_payables']['nonzero_payable_rows']
    assert len(nonzero) == 2
    assert {r['DT_FIM_EXERC'] for r in nonzero} == {'2024-12-31', '2025-12-31'}
    assert {r['CD_CONTA'] for r in nonzero} == {'2.01.05.02.09'}
    assert all(Decimal(r['reported_value_brl']) == 72000 for r in nonzero)
    generic = [r for r in aalr['prior_payables']['payable_rows'] if r['CD_CONTA'] == '2.01.05.02.01']
    assert generic and all(Decimal(r['reported_value_brl']) == 0 for r in generic)
    assert aalr['DMPL_declared_rows_not_paid_cash']
    assert all(Decimal(r['VL_CONTA']) == 0 for r in aalr['DMPL_declared_rows_not_paid_cash'])


def test_real_secondary_claim_is_not_RI_not_JCP_not_reanchored(real_report):
    claims = real_report['AALR3']['claims']
    secondary = claims['secondary']
    assert secondary['claims'] == [{'scope': ['DIVIDENDO'], 'start': '2025-10-25', 'end': '2026-10-24',
        'explicit_date_anchor': False, 'ticker_identity_verified': True, 'source_assertion_only': True}]
    assert secondary['original_as_of'] > followon.CUTOFF
    assert not secondary['reanchored_to_cutoff']
    assert not secondary['JCP_covered']
    assert not secondary['usable_primary_bridge']
    ri = claims['issuer_RI']
    assert ri['table_count'] == 1
    assert ri['nonempty_table_rows'] == 0
    assert ri['report']['zero_claims'] == []
    assert ri['empty_year_message_present']
    assert not ri['usable_primary_bridge']


def test_real_no_AALR_ITR_is_unknown_and_IPE_candidates_not_cash_proof(real_report):
    itr = real_report['financial_index_observations'][-1]
    assert itr['AALR_filings_published_by_cutoff'] == 0
    assert itr['absence_is_not_zero']
    triage = real_report['next_document_triage']
    assert len(triage['exact_candidate_rows']) == 4
    assert triage['documents_acquired_or_read_in_this_followon'] == 0
    annual = next(r for r in triage['exact_candidate_rows'] if 'numSequencia=1074213&' in r['Link_Download'])
    assert annual['Data_Referencia'] == '2025-12-31'
    assert annual['Data_Entrega'] == '2026-07-30'


def test_real_LWSA_positive_cash_rejects_full_zero_without_month_or_class_guess(real_report):
    lwsa = real_report['LWSA3']
    positives = [r for r in lwsa['cash_coverage']['selected_individual_periods']
                 if r['status'] == 'known_positive_reported_outflow']
    assert len(positives) == 5
    assert lwsa['full_window_zero_route'] == 'contradicted_by_reported_cash_outflows'
    annual = next(r for r in positives if r['period_start'] == '2023-01-01')
    assert Decimal(annual['rows'][0]['reported_value_brl']) == -30815000
    q1 = next(r for r in positives if r['period_end'] == '2026-03-31')
    assert {r['CD_CONTA'] for r in q1['rows']} == {'6.03.05', '6.03.11'}
    assert {Decimal(r['reported_value_brl']) for r in q1['rows']} == {0, -139999000}


def test_real_all_sources_actual_pinned_bytes_no_missing_late_source(real_report):
    assert {r['file'] for r in real_report['sources']} == set(followon.INPUTS)
    assert all(r['sha256'] == followon.INPUTS[r['file']] for r in real_report['sources'])


def test_real_stays_source_only_no_events_route_gate_prices_or_admission(real_report):
    assert real_report['request_decision']['new_requests'] == 0
    assert real_report['request_decision']['maximum_new_public_primary_requests'] == 8
    assert not real_report['AALR3']['complete_current_class_paid_zero_proof']
    assert not real_report['AALR3']['exact_unrounded_cash_zero_verified']
    for key in ('production_cash_gate_changed', 'typed_zero_cash_route_proposed',
                'price_independence_verified', 'model_approval_performed', 'parent_files_or_pointers_written'):
        assert real_report[key] is False
    assert real_report['individual_events_generated'] == 0


def test_real_read_only_and_content_addressed_write_isolated(cloned_inputs, real_report):
    before = {name: (cloned_inputs / name).read_bytes() for name in followon.INPUTS}
    name = followon.save_report(cloned_inputs, real_report)
    assert name.startswith(followon.CACHE + '/report-')
    body = (cloned_inputs / name).read_bytes()
    assert hashlib.sha256(body).hexdigest() in name
    assert json.loads(body) == real_report
    assert followon.save_report(cloned_inputs, real_report) == name
    for source, original in before.items():
        assert (cloned_inputs / source).read_bytes() == original
    assert not (cloned_inputs / 'current.json').exists()
    assert not (cloned_inputs / 'snapshots').exists()


def test_pinned_hash_change_fails_closed(cloned_inputs):
    with pytest.raises(ValueError, match='original_content_hash_mismatch'):
        Reader(cloned_inputs).read(followon.PAYMENTS, 'tamper_probe', '0' * 64)


def test_identity_mismatch_and_latest_revision_do_not_silently_fallback():
    row = {'CD_CVM': '024058', 'CNPJ_CIA': followon.ISSUERS[24058][1],
           'DT_REFER': '2025-12-31', 'DT_RECEB': '2026-07-30', 'VERSAO': '1'}
    newer = {**row, 'DT_RECEB': '2026-08-01', 'VERSAO': '2'}
    assert followon._latest([row, newer])[24058, '2025-12-31']['VERSAO'] == '2'
    wrong = deepcopy(row)
    wrong['CNPJ_CIA'] = '00.000.000/0001-00'
    with pytest.raises(ValueError, match='CVM_CNPJ_conflict'):
        followon._latest([wrong])
