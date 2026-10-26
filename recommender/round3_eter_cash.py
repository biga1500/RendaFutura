"""Literal ETER3 source corrections, including a cross-year payment revision."""
import hashlib
import json

from . import cash_repair_proofs as proof
from . import cvm_proventos_documents as cvm
from .round2_native_prices import receipt_source


IDENTITY = {'ticker': 'ETER3', 'cvm': 5762, 'share_class': 'ON', 'isin': 'BRETERACNOR3'}
CNPJ = '61.092.037/0001-81'
DETAIL_SHA = 'bace8ce6355febafb4112e27f4b1039182ced6ffbe8c14e013da3806f78a3f16'
PAYMENT_SHA = 'e9b7b4f477817965a62e22cca07e69284e45095837211c413ac6516e9c3efa3f'
REPAIR_ID = 'ETER3_literal_2023_entitlement_2024_gross_and_final_payment_amendment_v1'
SPECS = {
    'final2023': (2024, '734624', 2, '2023-11-07', 'relatorio proventos',
        'raw/primary-research/all-pending/round3-eter/body-844d84e6e84e30a40f899824b2d25d1ca6b1d84caaafef8bf7bf36240824d751.bin',
        '844d84e6e84e30a40f899824b2d25d1ca6b1d84caaafef8bf7bf36240824d751', 'round3-eter'),
    'original2023': (2023, '683625', 1, '2023-11-07', 'fato relevante',
        'raw/primary-research/all-pending/cash/body-d731c5b231cdc12a5fce8050f732511f8029d80d053348d5b34e684920dd87c0.bin',
        'd731c5b231cdc12a5fce8050f732511f8029d80d053348d5b34e684920dd87c0', 'cash'),
    'amendment': (2024, '734262', 2, '2024-03-19', 'aviso aos acionistas',
        'raw/primary-research/all-pending/round3-eter/body-36736ce02dcc4a09bb886dec758233f5b8df5c023623765e0f5ef5f06ec2d180.bin',
        '36736ce02dcc4a09bb886dec758233f5b8df5c023623765e0f5ef5f06ec2d180', 'round3-eter'),
    'final2024': (2024, '824462', 1, '2024-11-05', 'relatorio proventos',
        'raw/primary-research/cash-calendars/cvm824462v1-38cce43e7a4341d050ab85893f10297cb5ac5f5ef4b6dcf1af58bcdd2fe2e34b.pdf',
        '38cce43e7a4341d050ab85893f10297cb5ac5f5ef4b6dcf1af58bcdd2fe2e34b', None),
    'notice2024': (2024, '823416', 2, '2024-11-05', 'fato relevante',
        'raw/primary-research/all-pending/round3-eter/body-53acd69bb01b1f3e4c83fb652a4d796930031faabdb2e4b6c7050fa1ce747b17.bin',
        '53acd69bb01b1f3e4c83fb652a4d796930031faabdb2e4b6c7050fa1ce747b17', 'round3-eter'),
}


def compact(value):
    return ''.join(c.lower() for c in proof.text(value) if c.isalnum())


def native(cache, spec):
    year, sequence, version, reference, category, filename, digest, scope = spec
    document, index = proof.index_document(cache, year, sequence, version, 5762, CNPJ, reference, category)
    body, source, pages = proof.original(cache, document, filename, digest, transport=False)
    refs = [index, source]
    if scope is not None:
        name = hashlib.sha256(document['url'].encode()).hexdigest()
        relative = f'raw/primary-research/all-pending/{scope}/response-{name}.json'
        metadata_body, _ = cache.read(relative, 'eter_actual_native_transport')
        metadata = json.loads(metadata_body)
        verified, receipt = receipt_source(cache.folder, metadata, digest)
        proof.require(verified == body and receipt['url'] == document['url']
            and receipt['file'] == filename, 'eter_exact_native_transport_binding_required')
        source.update(retrieved_at=receipt['retrieved_at'], retrieval_basis='actual_public_GET_receipt')
        refs.append({'file': relative, 'sha256': hashlib.sha256(metadata_body).hexdigest(),
            'bytes': len(metadata_body), 'url': None, 'retrieved_at': None})
    return body, pages, document, refs


def verify_amendment(pages):
    proof.require(len(pages) == 2 and CNPJ in pages[0], 'eter_complete_native_amendment_identity_required')
    text = compact(pages[0])
    proof.require(all(value in text for value in (
        'b3eter3', 'alteracaodadatadepagamentodejurossobrecapitalproprio',
        'fatorelevante', '07112023e19122023', 'dia29032024',
        'resultadosdo3t23e4t23', 'seraoantecipadosparaodia28032024', '19demarcode2024')),
        'eter_explicit_named_distribution_payment_amendment_required')
    return {'initial_payment_date': '2024-03-29', 'final_payment_date': '2024-03-28',
        'amendment_published_at': '2024-03-19', 'date_inferred_from_holiday': False}


def one_form(body, document, expected):
    parsed = cvm.parse_pdf(body, document, proof.CUTOFF)
    proof.require(parsed['status'] == 'available' and len(parsed['events']) == 1
        and not parsed['unmapped_installment_rows'], 'eter_single_final_native_ISIN_form_required')
    event, = parsed['events']
    proof.require((event['cvm'], event['isin'], event['approval_date'], event['record_date'],
        event['payment_date'], event['gross_per_unit_decimal']) == expected,
        'eter_final_native_ISIN_amount_or_date_role_changed')
    return event


def load_proof(identity, cutoff, cache):
    proof.require(cutoff == proof.CUTOFF, 'eter_exact_reference_required')
    sources = proof.verified_identity(cache, identity, IDENTITY, DETAIL_SHA, CNPJ)
    docs = {name: native(cache, spec) for name, spec in SPECS.items()}
    for _, _, _, refs in docs.values():
        sources.extend(refs)
    amendment = verify_amendment(docs['amendment'][1])
    first = one_form(docs['final2023'][0], docs['final2023'][2],
        (5762, IDENTITY['isin'], '2023-11-07', '2023-11-10', '2024-03-28', '0.07661590000'))
    second = one_form(docs['final2024'][0], docs['final2024'][2],
        (5762, IDENTITY['isin'], '2024-11-05', '2024-11-08', '2025-03-31', '0.05202290000'))
    for name, amount, approval, ex_date, payment in (
        ('original2023', '00766159', '07112023', '13112023', '29032024'),
        ('notice2024', '00520229', '05112024', '11112024', '31032025')):
        pages = docs[name][1]
        proof.require(CNPJ in pages[0] and all(value in compact(pages[0]) for value in
            ('eter3', 'jurossobreocapitalproprio', 'valorbruto', amount, approval, ex_date, payment)),
            'eter_literal_JCP_gross_notice_dates_required')
    facts = []
    for name, event, ex_date in (('final2023', first, '2023-11-13'), ('final2024', second, '2024-11-11')):
        fact = {**IDENTITY, 'label': 'JCP', 'approval_date': event['approval_date'],
            'record_date': event['record_date'], 'ex_date': ex_date, 'payment_date': event['payment_date'],
            'gross_per_share_decimal': event['gross_per_unit_decimal'],
            'published_at': docs[name][2]['published_at'], 'gross_reconstructed_from_net': False,
            'source_locator_id': f'ETER3:CVM:{event["document_sequence"]}:v{event["document_version"]}:ISIN',
            'source_locator': {'form_sequence': event['document_sequence'], 'form_version': event['document_version'],
                'form_table': event['source_table'], 'form_row': event['source_row'],
                'notice_sequence': docs['original2023' if name == 'final2023' else 'notice2024'][2]['sequence']}}
        if name == 'final2023':
            fact.update(initial_declared_payment_date='2024-03-29',
                payment_amendment_published_at='2024-03-19', payment_amendment_sequence='734262',
                payment_date_basis='explicit_final_v2_form_and_named_primary_amendment')
        facts.append(fact)
    sources = proof.merge_sources(sources)
    proof.register(cache, sources)
    return {'facts': facts, 'sources': sources, 'contract': {
        'registered_identity': IDENTITY, 'latest_revision_search_includes_later_calendar_years': True,
        'initial_2023_form_v1_not_used_as_final': True, 'payment_amendment': amendment,
        'record_date_not_derived_from_ex_or_business_calendar': True,
        'native_gross_not_rounded_or_inferred_from_net': True,
        'original_source_bytes_modified': False, 'settlement_verified': False}}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    if identity.get('ticker') != 'ETER3':
        return parsed
    evidence = load_proof(identity, cutoff, cache)
    groups = {
        '2023-11-10': {'original_record_date': '2023-11-12', 'expected_original': [
            {'label': 'JCP', 'payment_date': '2024-03-28', 'gross_per_share': .076615,
             'source_row': 'table:2:row:9'}], 'facts': [evidence['facts'][0]]},
        '2024-11-08': {'expected_original': [
            {'label': 'JCP', 'payment_date': '2025-03-31', 'gross_per_share': .05,
             'source_row': 'table:2:row:5'}], 'facts': [evidence['facts'][1]]},
    }
    return proof.apply_groups(parsed, identity, cutoff, evidence, repair_id=REPAIR_ID,
        payment_sha=PAYMENT_SHA, groups=groups)
