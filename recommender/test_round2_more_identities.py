"""Offline tests use real pinned FCA archives and native CVM/issuer originals."""
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date
import hashlib
import socket

import pytest

from . import round2_more_identities as subject


EXPECTED = {
    'BAUH4': (1570, 'PNA', 'BRBAUHACNPR3'),
    'BMIN3': (1309, 'ON', 'BRBMINACNOR4'),
    'BMIN4': (1309, 'PN', 'BRBMINACNPR1'),
    'CEGR3': (16616, 'ON', 'BRCEGRACNOR4'),
    'GPAR3': (21393, 'ON', 'BRGPARACNOR4'),
    'PEAB3': (18708, 'ON', 'BRPEABACNOR3'),
    'PEAB4': (18708, 'PN', 'BRPEABACNPR0'),
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('offline_test_attempted_network')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)


@pytest.fixture(scope='module')
def adapter():
    return subject.PrimaryIdentityAdapter()


@pytest.fixture(scope='module')
def calendars(adapter):
    return {ticker: subject.recover_calendar(adapter, ticker) for ticker in EXPECTED}


@pytest.mark.parametrize('ticker', EXPECTED)
def test_real_primary_identity_without_B3_shape(adapter, ticker):
    evidence = adapter.evidence(ticker)
    assert is_dataclass(evidence)
    assert (evidence.cvm, evidence.share_class, evidence.isin) == EXPECTED[ticker]
    assert evidence.aliases == ()
    assert not evidence.unchanged_five_year_identity_required
    assert not evidence.model_admission
    assert not evidence.exchange_trading_verified
    assert not {'codeCVM', 'otherCodes'} & asdict(evidence).keys()
    fca = evidence.evidence[0]
    assert fca['row']['Codigo_Negociacao'] == ticker
    assert fca['master']['DT_RECEB'] <= evidence.as_of
    assert 'csv_record=' in fca['locator']
    form = evidence.evidence[1]['form']
    assert evidence.isin in [r['isin'] for r in form['rows']]
    assert form['source']['retrieved_at']
    assert form['version'] == int(form['index']['record']['Versao'])


@pytest.mark.parametrize('ticker', ['BAUH3', 'BMEB3', 'CELP3', 'PEAB', 'BMIN', 'EQTL3', 'PLPL3'])
def test_scope_has_no_suffix_or_alias_fallback(adapter, ticker):
    with pytest.raises(subject.EvidencePending, match='target_outside_scope'):
        adapter.evidence(ticker)


def test_PEAB_class_observation_is_historical_not_fabricated_current_date(adapter):
    evidence = adapter.evidence('PEAB4')
    assert evidence.native_observation_date == '2025-08-18'
    assert evidence.latest_native_ISIN_observation_date == '2026-05-18'
    join = evidence.evidence[2]
    assert join['class_form']['sequence'] == '938852'
    assert join['notice_index']['sequence'] == '937510'
    assert join['class_observation_is_dated_not_continuous_identity_claim']


@pytest.mark.parametrize('ticker', EXPECTED)
def test_future_native_identity_not_backfilled(adapter, ticker):
    with pytest.raises(subject.EvidencePending, match='not_yet_received'):
        adapter.evidence(ticker, date(2025, 12, 31))


def test_FCA_selected_missing_children_never_backfilled(adapter):
    f = deepcopy(subject.select_fca(adapter.corpus.filings, 18708, subject.CUTOFF))
    f['children'] = []
    older = deepcopy(f)
    older['master']['VERSAO'] = '1'
    older['children'] = adapter.corpus.fca('PEAB3')['reported_equities']
    with pytest.raises(subject.EvidencePending, match='children_missing_no_backfill'):
        subject.select_fca([older, f], 18708, subject.CUTOFF)


def test_FCA_late_revision_does_not_apply_early(adapter):
    # The archive retains v1's master but only v2's security child table.
    with pytest.raises(subject.EvidencePending, match='children_missing_no_backfill'):
        subject.select_fca(adapter.corpus.filings, 18708, date(2026, 5, 31))
    f = subject.select_fca(adapter.corpus.filings, 18708, date(2026, 6, 1))
    assert f['master']['VERSAO'] == '2'


def test_native_ISIN_class_join_is_independent_of_ISIN_suffix(adapter):
    c = adapter.corpus
    form = subject.read_form(c, c.document('1010630'))
    form['rows'][0]['isin'], form['rows'][1]['isin'] = form['rows'][1]['isin'], form['rows'][0]['isin']
    amounts = subject.class_amounts(c.document('1009459'), subject.ISSUERS[1309])
    bound = subject.bind_class(form, amounts, c.fca('BMIN3'))
    assert bound['ON']['row']['isin'] == 'BRBMINACNPR1'
    assert bound['PN']['row']['isin'] == 'BRBMINACNOR4'


def test_equal_native_amounts_cannot_disambiguate_classes(adapter):
    c = adapter.corpus
    form = subject.read_form(c, c.document('1010630'))
    amounts = subject.class_amounts(c.document('1009459'), subject.ISSUERS[1309])
    form['rows'][1]['gross_per_share_decimal'] = form['rows'][0]['gross_per_share_decimal']
    with pytest.raises(subject.EvidencePending, match='amount_join_ambiguous'):
        subject.bind_class(form, amounts, c.fca('BMIN3'))


def test_generic_PN_cannot_select_among_two_preferred_classes(adapter):
    c = adapter.corpus
    fca = deepcopy(c.fca('BAUH4'))
    other = deepcopy(fca['reported_equities'][1])
    other.update(Sigla_Classe_Acao_Preferencial='PNB', Classe_Acao_Preferencial='Preferencial Classe B')
    fca['reported_equities'].append(other)
    form = subject.read_form(c, c.document('1033267'))
    with pytest.raises(subject.EvidencePending, match='preferred_class_ambiguous'):
        subject.bind_class(form, subject.class_amounts(subject.issuer_notice(c), subject.ISSUERS[1570]), fca)


@pytest.mark.parametrize('field,value', [('Versao', '9'), ('Codigo_CVM', '1325')])
def test_native_form_metadata_must_agree_with_index(adapter, field, value):
    doc = adapter.corpus.document('1010630')
    doc['index']['record'][field] = value
    with pytest.raises(subject.EvidencePending):
        subject.read_form(adapter.corpus, doc)


def test_notice_wrong_CNPJ_rejected(adapter):
    with pytest.raises(subject.EvidencePending, match='CNPJ_missing'):
        subject.class_amounts(adapter.corpus.document('1009459'), subject.ISSUERS[1570])


def test_older_native_datetime_format_is_parsed_explicitly(adapter):
    reading = subject.read_form(adapter.corpus, adapter.corpus.document('559531'))
    assert reading['sent_on'] == '2022-11-22'
    assert reading['approval_date'] == '2022-09-09'
    assert reading['record_date'] == '2022-11-25'


@pytest.mark.parametrize('ticker,count', [('BAUH4', 4), ('BMIN3', 5), ('BMIN4', 5),
    ('CEGR3', 7), ('GPAR3', 2), ('PEAB3', 9), ('PEAB4', 8)])
def test_literal_calendar_counts_are_observations_not_full_admission(calendars, ticker, count):
    cal = calendars[ticker]
    assert len(cal['native_form_observations']) == count
    assert not cal['native_calendar_coverage_verified']
    assert not cal['model_cash_admitted']
    assert cal['unknown_years_are_not_zero_payments']
    assert all(not e['model_cash_admitted'] for e in cal['native_form_observations'])


def test_CEGR_literal_installments_and_late_payment_revision(calendars):
    rows = calendars['CEGR3']['native_form_observations']
    assert {(r['payment_date'], r['gross_per_share_decimal']) for r in rows if r['form_sequence'] == '753317'} == {
        ('2024-07-25', '0.15774423000'), ('2024-12-16', '0.73281741000'), ('2024-07-25', '0.32073972000')}
    assert next(r['payment_date'] for r in rows if r['form_sequence'] == '891083') == '2025-07-10'
    assert next(r['available_on'] for r in rows if r['form_sequence'] == '891083') == '2025-04-28'
    old = calendars['CEGR3']['superseded_forms_retained_not_added_as_cash'][0]['older_form']
    assert old['rows'][0]['payment_date'] == '2025-07-30'
    assert len(calendars['CEGR3']['future_scheduled_observations']) == 2


def test_PEAB_changed_protocols_do_not_duplicate_cash(calendars):
    sequences = [r['form_sequence'] for r in calendars['PEAB3']['native_form_observations']]
    assert '757164' in sequences and '738165' not in sequences
    assert '757163' in sequences and '754591' not in sequences
    assert '833306' in sequences and '830771' not in sequences
    assert '840632' not in [r['form_sequence'] for r in calendars['PEAB4']['native_form_observations']]


def test_PEAB_payment_year_conflict_not_silently_overwritten(calendars):
    event = next(e for e in calendars['PEAB3']['native_form_observations'] if e['form_sequence'] == '983307')
    assert event['payment_date'] == '2026-01-12'
    assert event['payment_date_conflict']['notice_literal'] == '12 de janeiro de 2025'


def test_BMIN_literal_zero_is_separate_from_unknown_years(calendars):
    row = calendars['BMIN3']['supplementary_literal_class_notices'][0]
    assert row['gross_per_share_decimal'] == '0.000000' and row['explicit_zero_declaration']
    pn = calendars['BMIN4']['supplementary_literal_class_notices'][0]
    assert pn['gross_per_share_decimal'] == '0.850155' and not pn['explicit_zero_declaration']


def test_BMIN2024_native_bruto_and_dividend_columns_not_net_or_summed(calendars):
    rows = [r for r in calendars['BMIN3']['native_form_observations'] if r['form_sequence'] == '711042']
    assert {(r['label'], r['gross_per_share_decimal']) for r in rows} == {
        ('JCP', '0.42072500000'), ('DIVIDENDO', '0.85200000000')}


def test_BMIN2025_native_document_not_its_misleading_index_subject(calendars):
    row = next(e for e in calendars['BMIN3']['native_form_observations'] if e['form_sequence'] == '855558')
    assert row['label'] == 'DIVIDENDO'


def test_GPAR_cash_election_not_promoted_to_universal_cash(calendars):
    row = next(e for e in calendars['GPAR3']['native_form_observations'] if e['form_sequence'] == '1014915')
    assert row['holder_specific_cash_or_non_cash_election_unresolved']
    assert row['universal_cash_payment_not_asserted']


def test_all_final_forms_required_no_older_fallback(adapter, monkeypatch):
    c = adapter.corpus
    original = c.document
    def missing(sequence):
        if sequence == '891083':
            raise subject.EvidencePending('missing_final_test')
        return original(sequence)
    monkeypatch.setattr(c, 'document', missing)
    cal = subject.recover_calendar(adapter, 'CEGR3')
    assert cal['missing_final_originals'][0]['sequence'] == '891083'
    assert not any(e['form_sequence'] in ('891083', '840770') for e in cal['native_form_observations'])


@pytest.mark.parametrize('ticker', EXPECTED)
def test_real_price_replay_keeps_existing_61_month_file(adapter, ticker):
    audit = subject.price_evidence_audit(adapter.corpus, ticker)
    replay = audit['actual_price_replay']
    assert replay['literal_Yahoo_61_month_audit']['observed_required_months'] == 61
    assert replay['actual_reconcile_prices_result']['reason'] == 'insufficient_independent_price_matches'
    assert audit['independent_anchors_available'] == 0 and not audit['automatic_ineligibility']
    assert isinstance(audit['COTAHIST_local_files'], list)


def test_actual_source_replay_hooks_identity_only_and_restores_core(adapter):
    from . import inventory_candidates as core
    before = core._identity
    r = subject.identity_only_source_replay(adapter)
    assert core._identity is before and r['process_hook_restored']
    assert r['pointer_bytes_unchanged_during_replay']
    assert not r['cash_or_price_gates_hooked']
    assert {v['reason'] for v in r['results']} == {'independent_cash_cache_missing'}


def test_issuer_map_contains_only_exact_assigned_codes(adapter):
    assert set(adapter.issuer_map(1309)) == {'BMIN3', 'BMIN4'}
    assert set(adapter.issuer_map(1570)) == {'BAUH4'}


def test_core_caller_cannot_replace_class_or_ISIN(adapter):
    row = {'ticker': 'BMIN3', 'cvm': 1309, 'share_class': 'PN', 'isin': None,
           'identity_status': 'official_cvm_fca', 'unit_composition': None, 'sector_code': 10}
    with pytest.raises(subject.EvidencePending, match='caller_identity_conflict'):
        adapter.core_fallback(row, None, subject.CUTOFF)


def test_original_hash_and_safe_path_fail_closed(tmp_path):
    raw = tmp_path / 'raw'
    raw.mkdir()
    p = raw / 'original.bin'
    p.write_bytes(b'actual-test-bytes')
    reader = subject.primary._Reader(tmp_path)
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    assert reader.read('raw/original.bin', digest)[0] == b'actual-test-bytes'
    p.write_bytes(b'changed')
    with pytest.raises(subject.primary.IdentityPending, match='hash_changed'):
        reader.read('raw/original.bin', digest)
    with pytest.raises(subject.primary.IdentityPending, match='unsafe_evidence_path'):
        reader.read('raw/../original.bin')


@pytest.mark.parametrize('host', ['b3.com.br', 'www.b3.com.br', 'api.mziq.com', 'cdn-sites-assets.mziq.com'])
def test_collection_refuses_any_non_actual_CVM_link_before_collector(adapter, monkeypatch, host):
    indexed = deepcopy(adapter.corpus.indexed('1009459'))
    indexed['record']['Link_Download'] = f'https://{host}/ENET/frmDownloadDocumento.aspx'
    monkeypatch.setattr(adapter.corpus, 'indexed', lambda sequence: indexed)
    with pytest.raises(subject.EvidencePending, match='not_exact_public_CVM_link'):
        subject.collect_documents(adapter.corpus, ['1009459'])


def test_reviewed_material_findings_keep_uncertainty(adapter):
    r = subject.material_notice_findings(adapter.corpus)
    assert len(r) == 8
    assert all(not f['automatic_ineligibility'] and f['source']['retrieved_at'] for f in r)


def test_existing_issuer_acquisition_is_not_retried_or_renamed_CVM(adapter):
    receipt = subject.collect_excelsior_published_notice(adapter.corpus)
    assert receipt['scope'] == 'round2-identity'
    assert receipt['url'].startswith('https://ri.excelsior.com.br/Download.aspx?')
    assert receipt['sha256'] == '5e4362bac36781d33251fa41853c6d28d2e9b089bd2e11e74d9e790f67015b9d'
