from copy import deepcopy

import pytest

from .primary_evidence import build_registry, declaration_facts, match_declaration


STOCK = {'ticker': 'TEST3', 'cvm': 123, 'isin': 'BRTESTACNOR0', 'model_ready': False}
EVENT = {'cvm': 123, 'isin': STOCK['isin'], 'record_date': '2026-07-10',
    'approval_date': '2026-07-01', 'payment_date': '2026-08-01',
    'gross_per_unit_decimal': '0.12345678901', 'source_table': 1, 'source_row': 2}
DOCUMENT = {'cvm': 123, 'pages': 1, 'published_at': '2026-07-02', 'url': 'https://official.example/doc1',
    'sha256': 'a' * 64, 'file': 'raw/doc1.pdf', 'sequence': '1', 'version': 1, 'events': [EVENT]}
RI = {'ticker': 'TEST3', 'payment_date': EVENT['payment_date'], 'record_date': EVENT['record_date'],
    'approval_date': EVENT['approval_date'], 'gross_per_share': .123456789, 'status': 'past_scheduled', 'issues': []}


def facts(*documents):
    return declaration_facts([{'results': list(documents or [DOCUMENT])}], '2026-10-24')[1]


def test_repeated_report_is_not_an_additional_event():
    documents, items = declaration_facts([{'results': [DOCUMENT]}, {'results': [DOCUMENT]}], '2026-10-24')
    assert len(documents) == len(items) == 1
    assert len(items[0]['declaration_evidence']) == 1


def test_document_versions_are_evidence_not_cash_totals():
    revision = {**DOCUMENT, 'url': 'https://official.example/doc2', 'version': 2, 'sequence': '2'}
    items = facts(DOCUMENT, revision)
    assert len(items) == 1 and len(items[0]['declaration_evidence']) == 2
    assert items[0]['gross_per_unit_decimal'] == EVENT['gross_per_unit_decimal']
    assert items[0]['cash_model_eligible'] is False


def test_changed_document_body_cannot_silently_replace_evidence():
    with pytest.raises(ValueError, match='changed_content'):
        facts(DOCUMENT, {**DOCUMENT, 'sha256': 'b' * 64})


def test_document_event_must_belong_to_exact_issuer():
    with pytest.raises(ValueError, match='identity_or_amount'):
        facts({**DOCUMENT, 'events': [{**EVENT, 'cvm': 999}]})


def test_future_or_invalid_document_is_not_available():
    assert not facts({**DOCUMENT, 'published_at': '2027-01-01'})
    assert not facts({**DOCUMENT, 'pages': 0})


def test_primary_gross_match_preserves_decimals_without_changing_status():
    before = deepcopy(RI)
    result = match_declaration(RI, STOCK, facts())
    assert len(result) == 1 and RI == before
    assert result[0]['label'] is None
    assert result[0]['is_settlement_confirmation'] is False


@pytest.mark.parametrize('change', [
    {'isin': None}, {'isin': 'BRTESTACNPR0'}, {'cvm': 999}])
def test_unresolved_or_other_share_class_never_matches(change):
    assert not match_declaration(RI, {**STOCK, **change}, facts())


@pytest.mark.parametrize('change', [
    {'record_date': '2026-07-11'}, {'approval_date': '2026-07-02'},
    {'payment_date': '2026-08-02'}, {'gross_per_share': None},
    {'gross_per_share': .12345}, {'issues': ['conflicting_primary_payment_dates']},
    {'gross_per_share': float('nan')}])
def test_missing_gross_conflicts_and_rounding_differences_do_not_match(change):
    assert not match_declaration({**RI, **change}, STOCK, facts())


def test_ambiguous_approval_dates_do_not_merge_into_one_payment():
    changed = {**DOCUMENT, 'url': DOCUMENT['url'] + 'v2',
        'events': [{**EVENT, 'approval_date': '2026-07-03'}]}
    assert not match_declaration({**RI, 'approval_date': None}, STOCK, facts(DOCUMENT, changed))


def test_registry_keeps_paid_reports_but_never_promotes_or_guesses_fields():
    result = build_registry({'stocks': [STOCK]}, [{'results': [DOCUMENT]}],
        {'instruments': {'TEST3': {'events': [{**RI, 'status': 'paid_reported'}]}}}, {}, {}, '2026-10-24')
    assert result['counts']['RI_events_matched_to_CVM_gross_declaration'] == 1
    assert result['counts']['explicit_paid_reported_observations'] == 1
    assert result['counts']['model_admissions'] == 0
    assert result['active_model_changed'] is result['active_inventory_changed'] is False
    observed = result['issuer_RI_observations'][0]
    assert observed['cash_model_eligible'] is observed['history_complete'] is False
    assert observed['settlement_inferred_from_date'] is False
    assert observed.get('published_at') is None


def test_missing_issuers_do_not_inflate_download_coverage():
    inventory = {'stocks': [STOCK, {**STOCK, 'ticker': 'NONE3', 'cvm': 999, 'isin': None}]}
    result = build_registry(inventory, [{'results': [DOCUMENT]}], {}, {}, {}, '2026-10-24')
    assert result['counts']['issuers_with_valid_CVM_documents'] == 1
    assert result['instruments'][1]['issuer_document_count'] == 0
