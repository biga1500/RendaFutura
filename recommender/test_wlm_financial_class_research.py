from copy import deepcopy
from datetime import date
import hashlib
import json
import socket

import pytest

from . import wlm_financial_class_research as research


def _master():
    return dict(zip(('DT_REFER', 'VERSAO', 'ID_DOC', 'DT_RECEB', 'CNPJ_CIA'), research.EXPECTED),
                CD_CVM='011070', LINK_DOC='http://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento=155797')


@pytest.fixture(scope='module')
def audited():
    return research.audit()


def test_real_cached_sources_show_interoperability_candidate_not_authorization(audited):
    assert audited['reported_source_basic_EPS']['value'] == '2.4774100000'
    assert audited['reported_source_basic_EPS']['label'] == 'PN'
    assert audited['identity']['share_class'] == 'PNA'
    assert audited['reported_source_basic_EPS']['weighted_preferred_denominator_reported'] == '19843450'
    assert audited['admissions'] == [] and audited['network_requests'] == 2
    assert audited['offline_audit_network_requests'] == 0
    assert audited['native_annual_PDF_XML_found_or_verified_in_this_task']
    assert audited['actual_reported_PN_EPS_reconciles_with_note'] is False
    assert audited['actual_reported_ON_EPS_reconciles_with_note'] is False
    assert audited['consolidated_profit_brl'] == {'total': '86892000', 'parent': '86482000', 'NCI': '410000'}
    assert all(source['mtime_is_retrieval_or_publication'] is False for source in audited['sources'])
    assert len([source for source in audited['sources'] if source['retrieved_at'] is not None]) == 3
    for source in audited['sources']:
        assert hashlib.sha256((research.ROOT / source['file']).read_bytes()).hexdigest() == source['sha256']
    json.dumps(audited, allow_nan=False)


@pytest.mark.parametrize('year', [2025, 2026])
def test_period_specific_FCA_preserves_legal_PNA_but_not_full_capital_proof(audited, year):
    proof = audited['FCA_periods'][str(year)]
    assert proof['listed_preferred_classes'] == ['PNA'] and proof['legal_class_retained'] == 'PNA'
    assert proof['full_legal_capital_universe_proved'] is False
    assert proof['within_year_capital_changes_proved_absent'] is False
    assert research.listed_fca_universe([proof['master']], proof['all_reported_security_rows'], year) == proof


@pytest.mark.parametrize('field,value', [('Sigla_Classe_Acao_Preferencial', 'PNB'),
    ('Sigla_Classe_Acao_Preferencial', 'PN'), ('Codigo_Negociacao', 'OTHER4'),
    ('Data_Referencia', '2024-01-01'), ('Versao', '2'), ('CNPJ_Companhia', '00.000.000/0001-00')])
def test_wrong_class_instrument_period_or_issuer_cannot_borrow_FCA(audited, field, value):
    proof = deepcopy(audited['FCA_periods']['2025'])
    proof['all_reported_security_rows'][1][field] = value
    with pytest.raises(ValueError):
        research.listed_fca_universe([proof['master']], proof['all_reported_security_rows'], 2025)


def test_second_preferred_class_is_ambiguous_even_with_same_cash_rights(audited):
    proof = deepcopy(audited['FCA_periods']['2025'])
    second = deepcopy(proof['all_reported_security_rows'][1])
    second.update(Sigla_Classe_Acao_Preferencial='PNB', Classe_Acao_Preferencial='Preferencial Classe B',
                  Codigo_Negociacao='WLMM6')
    with pytest.raises(ValueError, match='preferred_class_universe_not_unique_exact_PNA'):
        research.listed_fca_universe([proof['master']], proof['all_reported_security_rows'] + [second], 2025)


def test_unpublished_FCA_cannot_be_used(audited):
    proof = deepcopy(audited['FCA_periods']['2026'])
    proof['master']['DT_RECEB'] = '2026-10-01'
    with pytest.raises(ValueError, match='published_FCA_missing'):
        research.listed_fca_universe([proof['master']], proof['all_reported_security_rows'], 2026)


def test_changed_latest_FCA_version_is_not_an_older_readable_fallback(audited):
    proof = deepcopy(audited['FCA_periods']['2025'])
    newer = dict(proof['master'], VERSAO='2', ID_DOC='148174')
    with pytest.raises(ValueError, match='unreviewed_exact_FCA_identity'):
        research.listed_fca_universe([proof['master'], newer], proof['all_reported_security_rows'], 2025)


@pytest.mark.parametrize('field,value', [('VERSAO', '2'), ('ID_DOC', '155798'),
    ('CNPJ_CIA', '00.000.000/0001-00'), ('DT_RECEB', '2026-03-28'), ('DT_REFER', '2026-06-30')])
def test_new_or_conflicting_latest_annual_invalidates_reading(field, value):
    current, changed = _master(), _master()
    changed[field] = value
    with pytest.raises(ValueError):
        research.latest_filing([current, changed])


def test_identical_annual_mirror_accepted_future_filing_excluded():
    current = _master()
    assert research.latest_filing([current, deepcopy(current)]) == current
    assert research.latest_filing([current, dict(current, VERSAO='2', DT_RECEB='2026-10-01')]) == current


def test_before_publication_and_wrong_cutoff_fail_closed():
    with pytest.raises(ValueError, match='latest_annual_missing'):
        research.latest_filing([dict(_master(), DT_RECEB='2026-10-01')])
    with pytest.raises(ValueError, match='unreviewed_cutoff'):
        research.latest_filing([_master()], cutoff=date(2026, 3, 26))


def test_no_silent_getter_or_financial_class_conversion(monkeypatch, audited):
    monkeypatch.setattr(research, 'audit', lambda *a, **k: deepcopy(audited))
    with pytest.raises(ValueError, match='reported_class_EPS_does_not_reconcile_with_native_note_numerator_and_denominator'):
        research.get_wlm_financial_reading()


def test_original_parser_keeps_PNA_unavailable_and_ON_unchanged(audited):
    records = {row['share_class']: row for row in audited['unchanged_parser_latest_records']}
    assert records['ON']['profit_per_share'] == 2.25219
    assert 'PNA' not in records
    diagnostic = audited['unchanged_parser_PNA_diagnostic']
    assert diagnostic['status'] == 'excluded'
    assert diagnostic['reasons'] == ['basic_eps_absent_ambiguous_or_zero']
    assert records['ON']['financial_method']['accounts_scope'] == 'consolidated'


def test_audit_has_no_network_calls_and_leaves_sources_unchanged(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))
    protected = research.ROOT / research.ANNUAL_FILE
    before = hashlib.sha256(protected.read_bytes()).hexdigest()
    report = research.audit()
    assert report['production_writes'] is False and report['admissions'] == []
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('eps,numerator,shares', [
    ('2.47741', '47126607.98', '19843450'), ('2.25219', '39355323.24', '16571220'),
])
def test_actual_class_EPS_conflict_far_exceeds_printed_rounding(eps, numerator, shares):
    assert research.eps_reconciles(eps, numerator, shares) is False


@pytest.mark.parametrize('old,new', [('19.843.450', '19.843.451'),
                                  ('48.568.791,45', '49.160.395,16'),
                                  ('Controladora e consolidado', 'Controladora somente')])
def test_changed_denominator_invented_allocation_or_scope_cannot_authorize_reading(old, new):
    from pypdf import PdfReader

    path = research.ROOT / research.OUTPUT / ('native-' + research.PDF_HASH + '.pdf')
    text = PdfReader(path).pages[93].extract_text()
    assert old in text
    with pytest.raises(ValueError):
        research.parse_basic_note(text.replace(old, new, 1))


@pytest.mark.parametrize('https', [False, True])
def test_authorized_budget_exhausted_no_third_request_even_if_getter_requested(monkeypatch, https):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('third request forbidden'))
    with pytest.raises(ValueError, match='native_request_already_attempted'):
        research.fetch_native_once(https_transport_phase=https)
