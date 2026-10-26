"""Final native RADL3 cash repairs, never deadline-to-payment inference."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

from . import cash_repair_proofs as proof
from . import cvm_proventos_documents as cvm


IDENTITY = {'ticker': 'RADL3', 'cvm': 5258, 'share_class': 'ON', 'isin': 'BRRADLACNOR0'}
CNPJ = '61.585.865/0001-51'
DETAIL_SHA = 'fe23e898772fa3b8da685962320b3506bd4cf1ac1925fdc5dd81af8022e6ec23'
PAYMENT_SHA = 'dfe182c2792a787990295e9bc1ce2d96daf94344ec5cafba6583cb4ff635e710'
REPAIR_ID = 'RADL3_final_2023_missing_JCP_with_AGM_payment_confirmation_v1'
ROOT = 'raw/primary-research/all-pending/cash/'
MINUTES_SHA = 'f86420a321e820a4e3d979d352c6875df601170f7f3693907bcc55d6e1b5c4fb'
UNRESOLVED_SHA = '0acda963ac4d9fafa02d2acbda53e987c88dcd27caecbc01fbccd99045c6ea48'
SPECS = (
    {'approval': '2023-03-30', 'record': '2023-04-04', 'ex': '2023-04-05',
     'notice': '605153', 'notice_sha': '1170a35d066c3c4c89311c9458edd20b2b73c5f07561c57f09a8cf7ddf461b2e',
     'form': '605590', 'form_sha': '16db674ec7f35b9de1281e88e0905b0e81286d99a6bcb6f91d47310ff432b5c3',
     'native_gross': '0.04853059700', 'notice_gross': '0,048530597', 'total_gross': '80.000.000,00',
     'minutes_section': '(ii.1.)', 'minutes_approval': '30 de marco de 202 3',
     'minutes_next_section': '(ii.2.)'},
    {'approval': '2023-06-30', 'record': '2023-07-05', 'ex': '2023-07-06',
     'notice': '643133', 'notice_sha': 'de84c517dcb315872aa636a95cca0455665213ae3cb53ba911b2aec6a9533d0a',
     'form': '643266', 'form_sha': 'f8bc255d83028340f87a12ca6f37b298ca3d0e87510ee031a7925ed264ab5909',
     'native_gross': '0.05949664580', 'notice_gross': '0,0594966458', 'total_gross': '102.000.000,00',
     'minutes_section': '(ii.2.)', 'minutes_approval': '30 de junho de 2023',
     'minutes_next_section': '(ii.3)'},
)


def confirm_historical_payment(pages, spec):
    """The declaration alone has a deadline. Only final AGM text supplies this date."""
    proof.require(len(pages) == 15 and CNPJ in pages[0]
        and 'RAIA DROGASIL S.A.' in pages[0], 'radl3_complete_final_AGM_issuer_required')
    section = pages[1].split(spec['minutes_section'])
    proof.require(len(section) == 2, 'radl3_unique_final_cash_ratification_section_required')
    passage = section[1].split(spec['minutes_next_section'])[0]
    proof.require(spec['minutes_approval'] in passage and spec['total_gross'] in passage
        and spec['notice_gross'] in passage
        and 'pago s aos acionistas em 01 de dezembro de 2023' in passage,
        'radl3_final_exact_payment_not_deadline_required')
    return passage


def parse_final_group(form_body, form_doc, notice_pages, minutes_pages, minutes_doc, spec):
    parsed = cvm.parse_pdf(form_body, form_doc, proof.CUTOFF)
    proof.require(parsed['status'] == 'available' and len(parsed['events']) == 1
        and not parsed['unmapped_installment_rows'], 'radl3_exact_single_ISIN_form_required')
    event = parsed['events'][0]
    proof.require((event['cvm'], event['isin'], event['record_date'], event['approval_date'],
        event['payment_date'], event['gross_per_unit_decimal']) ==
        (5258, IDENTITY['isin'], spec['record'], spec['approval'], '2023-12-01', spec['native_gross']),
        'radl3_native_ISIN_record_approval_amount_date_conflict')
    notice = notice_pages[0]
    approval = date.fromisoformat(spec['approval']).strftime('%d/%m/%Y')
    record = date.fromisoformat(spec['record']).strftime('%d/%m/%Y')
    ex = date.fromisoformat(spec['ex']).strftime('%d/%m/%Y')
    proof.require(len(notice_pages) == 2 and CNPJ in notice and 'RAIA DROGASIL S.A.' in notice
        and 'realizada no dia ' + approval in notice
        and 'distribuicao de Juros sobre Capital Proprio' in notice
        and spec['total_gross'] in notice and spec['notice_gross'] in notice
        and 'para pagamento ate o dia 01/12/2023, em data a ser oportunamente fixada' in notice
        and 'posicao acionaria do dia ' + record in notice
        and 'a partir de ' + ex in notice, 'radl3_native_JCP_and_explicit_record_ex_roles_required')
    passage = confirm_historical_payment(minutes_pages, spec)
    return {**IDENTITY, 'label': 'JCP', 'record_date': spec['record'], 'ex_date': spec['ex'],
        'approval_date': spec['approval'], 'payment_date': '2023-12-01',
        'gross_per_share_decimal': event['gross_per_unit_decimal'], 'net_per_share_decimal': None,
        'published_at': minutes_doc['published_at'],
        'initial_declaration_published_at': form_doc['published_at'],
        'payment_date_first_confirmed_in_used_primary_evidence': minutes_doc['published_at'],
        'payment_date_basis': 'explicit_historical_payment_date_final_AGM_minutes',
        'payment_claim_basis': 'issuer_reported_paid_in_final_AGM_not_bank_settlement',
        'native_reported_payment_status': 'issuer_reports_paid_not_bank_verified',
        'native_initial_payment_deadline': '2023-12-01',
        'deadline_was_not_promoted_to_payment_date': True,
        'source_locator_id': 'cash-repair:' + spec['form'] + ':v1:BRRADLACNOR0:JCP',
        'source_locator': {'form_sequence': spec['form'], 'form_version': 1,
            'form_table': event['source_table'], 'form_row': event['source_row'],
            'notice_sequence': spec['notice'], 'notice_version': 1, 'notice_page': 1,
            'payment_confirmation_sequence': '747640', 'payment_confirmation_version': 1,
            'payment_confirmation_page': 2, 'payment_confirmation_section': spec['minutes_section'],
            'native_payment_confirmation_passage': passage}}


def unresolved_2021(cache):
    """Preserve the observed final-revision failure; never consume the older v2."""
    doc, index_source = proof.index_document(cache, 2021, '450732', 3, 5258, CNPJ,
        '2021-12-03', 'aviso aos acionistas')
    body, source = proof.original_blob(cache, doc, ROOT + 'body-' + UNRESOLVED_SHA + '.bin', UNRESOLVED_SHA)
    source['authority'] = 'native_final_revision_download_HTML_not_usable_cash_evidence'
    proof.require(not body.lstrip().startswith(b'%PDF-'), 'radl3_unresolved_revision_outcome_changed_review_required')
    return {'record_date': '2021-12-08', 'native_final_document': doc,
        'status': 'final_v3_cached_HTML_not_native_PDF_no_older_revision_fallback',
        'earlier_revision_447470_v2_not_applied': True, 'native_final_revision_retried': False,
        'calendar_rows_or_amounts_changed': False,
        'sources': [index_source, source]}


def load_proof(identity, cutoff, cache):
    proof.require(cutoff == proof.CUTOFF, 'radl3_exact_reference_required')
    sources = proof.verified_identity(cache, identity, IDENTITY, DETAIL_SHA, CNPJ)
    minutes_doc, index_source = proof.index_document(cache, 2024, '747640', 1, 5258, CNPJ,
        '2024-04-17', 'assembleia')
    _, minutes_source, minutes = proof.original(cache, minutes_doc, ROOT + 'body-' + MINUTES_SHA + '.bin', MINUTES_SHA)
    minutes_source['authority'] = 'issuer_reported_historical_payment_final_AGM_not_bank_settlement'
    sources += [index_source, minutes_source]
    facts = []
    for spec in SPECS:
        form_doc, form_index = proof.index_document(cache, 2023, spec['form'], 1, 5258, CNPJ,
            spec['approval'], 'relatorio proventos')
        form_file = 'raw/primary-research/cash-calendars/cvm' + spec['form'] + 'v1-' + spec['form_sha'] + '.pdf'
        form_body, form_source, _ = proof.original(cache, form_doc, form_file, spec['form_sha'], transport=False)
        notice_doc, notice_index = proof.index_document(cache, 2023, spec['notice'], 1, 5258, CNPJ,
            spec['approval'], 'aviso aos acionistas')
        _, notice_source, notice = proof.original(cache, notice_doc, ROOT + 'body-' + spec['notice_sha'] + '.bin',
                                                 spec['notice_sha'])
        fact = parse_final_group(form_body, form_doc, notice, minutes, minutes_doc, spec)
        fact['initial_declaration_published_at'] = notice_doc['published_at']
        fact['native_form_published_at'] = form_doc['published_at']
        facts.append(fact)
        sources += [form_index, form_source, notice_index, notice_source]
    unresolved = unresolved_2021(cache)
    sources += unresolved['sources']
    sources = proof.merge_sources(sources)
    proof.register(cache, sources)
    return {'sources': sources, 'facts': facts, 'unresolved': unresolved, 'contract': {
        'exact_registered_CVM_class_ISIN': IDENTITY, 'native_final_revisions_verified': True,
        'payment_confirmation': minutes_doc, 'original_deadlines_retained': True,
        'applied_groups_only_not_unresolved_2021_revision': True,
        'two_JCP_declarations_not_duplicate_installments': True,
        'gross_sum_decimal': str(sum((Decimal(f['gross_per_share_decimal']) for f in facts), Decimal(0))),
        'unknown_dates_inferred': False, 'net_reconstructed': False,
        'historical_cash_evidence_only_not_annual_financial_feature_fallback': True}}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    """Copy the inspected calendar, adding only two fully cross-checked native rows."""
    if identity.get('ticker') != 'RADL3':
        return parsed
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    evidence = load_proof(identity, cutoff, cache)
    groups = {fact['record_date']: {'expected_original': [], 'facts': [fact]} for fact in evidence['facts']}
    output = proof.apply_groups(parsed, identity, cutoff, evidence, repair_id=REPAIR_ID,
                                payment_sha=PAYMENT_SHA, groups=groups)
    unresolved = deepcopy(evidence['unresolved'])
    unresolved['reported_original_fields'] = [deepcopy(r) for r in parsed['events']
                                             if r.get('record_date') == '2021-12-08']
    output['exact_cash_repairs'][REPAIR_ID]['unresolved_conflicts'] = [unresolved]
    return output
