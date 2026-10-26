"""Pinned PLPL3 calendar evidence with explicit native date corrections."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

from lxml import html

from . import cash_repair_proofs as proof
from . import cvm_proventos_documents as cvm
from . import pending_missing_b as widgets
from .round2_native_prices import read, receipt_source


IDENTITY = {'ticker': 'PLPL3', 'cvm': 25070, 'share_class': 'ON', 'isin': 'BRPLPLACNOR5'}
CNPJ = '24.230.275/0001-80'
PACKET_SHA = '40787917c55c7031efd81059b4e0fb4a961197f28dbe303e6d7567dd4fcd2617'
PACKET = 'raw/primary-research/all-pending/round2-plpl/evidence-' + PACKET_SHA + '.json'
DETAIL_SHA = '2da481fd4a3d3a9983ad9a00594c213fd369d009e24e393b7836a8124808be31'
PAYMENT_SHA = '8fdbcd507f25e51c43e853b274919a310d425faa42ac61eda836c3a09fe741ab'
REPAIR_ID = 'PLPL3_native_2024_entitlement_not_approval_date_v1'


def compact(value):
    return ''.join(c.lower() for c in proof.text(value) if c.isalnum())


def packet(folder):
    root = Path(folder).resolve()
    body = read(root, PACKET, PACKET_SHA)
    data = json.loads(body)
    proof.require(data['identity'] == IDENTITY and data['cutoff'] == proof.CUTOFF.isoformat(),
                  'plpl3_pinned_identity_contract_changed')
    return root, data, {'file': PACKET, 'sha256': PACKET_SHA, 'bytes': len(body), 'url': None,
        'retrieved_at': None, 'authority': 'immutable_primary_and_provider_source_bindings'}


def document(cache, entry):
    root = cache.folder
    doc, original = entry['document'], entry['source']
    evidence = doc['index_evidence']
    index_body, source = proof.read(cache, evidence['file'], evidence['sha256'], evidence['url'])
    with zipfile.ZipFile(io.BytesIO(index_body)) as archive:
        with archive.open(evidence['member']) as stream:
            rows = csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
            indexed = next((r for line, r in enumerate(rows, 2) if line == evidence['csv_line']), None)
    proof.require(indexed == evidence['raw_row'] and int(indexed['Codigo_CVM']) == IDENTITY['cvm']
        and indexed['CNPJ_Companhia'] == CNPJ
        and cvm.document_id(indexed['Link_Download']) == (doc['sequence'], doc['version'])
        and indexed['Link_Download'] == original['url'] == doc['url']
        and cvm.day(indexed['Data_Entrega']) == doc['published_at'], 'plpl3_exact_primary_index_binding_changed')
    source['source_locator'] = {'member': evidence['member'], 'csv_line': evidence['csv_line'], 'raw_row': indexed}
    body = read(root, original['file'], original['sha256'], original['bytes'])
    name = hashlib.sha256(original['url'].encode()).hexdigest()
    response_path = root / 'raw/primary-research/all-pending/round2-plpl' / ('response-' + name + '.json')
    proof.require(cache.safe_path(response_path) and response_path.stat().st_size <= 20_000,
                  'plpl3_unsafe_native_transport_receipt')
    response = json.loads(response_path.read_bytes())
    verified, receipt = receipt_source(root, response, original['sha256'])
    proof.require(verified == body and receipt == original and response['url'] == doc['url'],
                  'plpl3_native_transport_binding_changed')
    receipt.update(published_at=doc['published_at'], authority='primary_declared_schedule_not_bank_settlement')
    pages = []
    if body.startswith(b'%PDF-'):
        from pypdf import PdfReader
        pages = [proof.text(p.extract_text()) for p in PdfReader(io.BytesIO(body), strict=True).pages]
    return body, pages, [source, receipt]


def load_proof(identity, cutoff, cache):
    proof.require(cutoff == proof.CUTOFF, 'plpl3_exact_reference_required')
    sources = proof.verified_identity(cache, identity, IDENTITY, DETAIL_SHA, CNPJ)
    _, data, seal = packet(cache.folder)
    sources.append(seal)
    documents = {}
    for sequence, entry in data['documents'].items():
        body, pages, refs = document(cache, entry)
        documents[sequence] = (body, pages, entry['document'])
        sources.extend(refs)
    notice = compact(documents['708190'][1][0])
    proof.require(CNPJ in documents['708190'][1][0] and all(v in notice for v in (
        '050439835364', 'database de18dejaneirode2024'.replace(' ', ''),
        'apartirde19dejaneirode2024', 'nodia08defevereirode2024', 'acaoordinaria')),
        'plpl3_native_entitlement_approval_payment_roles_changed')
    form = cvm.parse_pdf(documents['708742'][0], documents['708742'][2], cutoff)
    proof.require(form['status'] == 'available' and len(form['events']) == 1
                  and not form['unmapped_installment_rows'], 'plpl3_single_native_ISIN_form_required')
    event = form['events'][0]
    proof.require((event['cvm'], event['isin'], event['approval_date'], event['record_date'],
        event['payment_date'], event['gross_per_unit_decimal']) ==
        (25070, IDENTITY['isin'], '2024-01-15', '2024-01-18', '2024-02-08', '0.50439835364'),
        'plpl3_native_form_identity_or_literal_cash_changed')
    initial = compact(documents['985325'][1][0])
    final = compact(documents['1062946'][1][0])
    proof.require(all(v in initial for v in ('049282986754', 'databasede29dedezembrode2025',
        'apartirde30dedezembrode2025', '1odejulhode2026')) and all(v in final for v in (
        '22dedezembrode2025', 'de1odejulhode2026para02dejulhode2026',
        'ratificadaseinalteradas')), 'plpl3_exact_named_distribution_payment_amendment_required')
    proof.require(not documents['1063185'][0].startswith(b'%PDF-'),
                  'plpl3_final_structured_form_outcome_changed_review_required')
    fact = {**IDENTITY, 'record_date': '2024-01-18', 'approval_date': '2024-01-15',
        'ex_date': '2024-01-19', 'payment_date': '2024-02-08', 'label': 'DIVIDENDO',
        'gross_per_share_decimal': event['gross_per_unit_decimal'], 'published_at': '2024-01-15',
        'source_locator_id': 'PLPL3:CVM708190:v1:clauses1-4:CVM708742:ISIN',
        'source_locator': {'notice_sequence': '708190', 'notice_page': 1, 'notice_clauses': [1, 2, 4],
            'form_sequence': '708742', 'form_table': event['source_table'], 'form_row': event['source_row']}}
    sources = proof.merge_sources(sources)
    proof.register(cache, sources)
    return {'facts': [fact], 'sources': sources, 'contract': {
        'approval_date_not_used_as_entitlement_date': True,
        '2025_distribution_amount_gross_decimal': '0.49282986754',
        '2025_distribution_record_date': '2025-12-29',
        'payment_date_originally_announced': '2026-07-01',
        'payment_date_amended_in_final_notice': '2026-07-02',
        'payment_amendment_published_at': '2026-06-25',
        'all_other_terms_explicitly_ratified_by_final_notice': True,
        'final_structured_form_v2_response': 'HTML_not_usable_native_PDF_retained_not_retried',
        'payment_priority_basis': 'final_board_notice_explicitly_amends_the_named_2025_distribution',
        'no_old_form_used_to_override_final_notice': True, 'settlement_verified': False}}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    if identity.get('ticker') != 'PLPL3':
        return parsed
    evidence = load_proof(identity, cutoff, cache)
    return proof.apply_groups(parsed, identity, cutoff, evidence, repair_id=REPAIR_ID,
        payment_sha=PAYMENT_SHA, groups={'2024-01-18': {'original_record_date': '2024-01-15',
            'expected_original': [{'label': 'DIVIDENDO', 'payment_date': '2024-02-08',
                'gross_per_share': 0.50439835, 'source_row': 'table:2:row:4'}], 'facts': evidence['facts']}})


def parse_calendar(body):
    tree = html.fromstring(body)
    proof.require('PLPL3' in tree.text_content() and 'ultimodiacom' in compact(tree.text_content()),
                  'plpl3_provider_identity_or_record_date_role_changed')
    tables = [t for t in tree.xpath('//table') if t.xpath('.//tr') and
              [compact(c.text_content()) for c in t.xpath('.//tr')[0].xpath('./th|./td')]
              == ['data', 'valor', 'tipo', 'datadepagamento', 'porquantasacoes']]
    proof.require(len(tables) == 1, 'plpl3_unique_provider_table_required')
    from datetime import datetime
    events = []
    for number, tr in enumerate(tables[0].xpath('.//tr')[1:], 1):
        fields = [' '.join(c.text_content().split()) for c in tr.xpath('./td')]
        proof.require(len(fields) == 5 and fields[2] == 'DIVIDENDO', 'plpl3_provider_cash_type_changed')
        record = datetime.strptime(fields[0], '%d/%m/%Y').date()
        payment = datetime.strptime(fields[3], '%d/%m/%Y').date()
        amount = Decimal(fields[1].replace('.', '').replace(',', '.'))
        quantity = Decimal(fields[4].replace('.', '').replace(',', '.'))
        proof.require(amount.is_finite() and quantity == 1 and amount > 0 and payment >= record,
                      'plpl3_provider_unit_or_date_changed')
        event = {'record_date': record.isoformat(), 'payment_date': payment.isoformat(),
                 'reported_amount_decimal': str(amount), 'gross_per_share_decimal': str(amount),
                 'label': fields[2], 'source_row': number, 'original_reported_fields': fields}
        if record == date(2025, 12, 29):
            proof.require(payment == date(2026, 7, 1) and amount == Decimal('0.4928'),
                          'plpl3_stale_secondary_schedule_changed_review_required')
            event.update(original_reported_payment_date='2026-07-01', payment_date='2026-07-02',
                payment_date_basis='final_CVM_board_notice_1062946_clause1',
                payment_amendment_published_at='2026-06-25')
        events.append(event)
    proof.require(len(events) == 6, 'plpl3_expected_full_provider_table_changed')
    return events


def load_evidence(folder, identity, cutoff):
    if identity.get('ticker') != 'PLPL3':
        return None
    proof.require({k: identity.get(k) for k in IDENTITY} == IDENTITY and cutoff == proof.CUTOFF,
                  'plpl3_exact_security_or_reference_required')
    from .inventory_candidates import _Cache
    from .market_inventory import load_inventory
    root, data, seal = packet(folder)
    cache = _Cache(root, load_inventory(root))
    primary = load_proof(identity, cutoff, cache)
    cash_body, cash_source = receipt_source(root, data['cash_source'], data['cash_source']['sha256'])
    quote_body, quote_source = receipt_source(root, data['quote_source'], data['quote_source']['sha256'])
    page_body, page_source = receipt_source(root, data['page_source'], data['page_source']['sha256'])
    script_body, script_source = receipt_source(root, data['script_source'], data['script_source']['sha256'])
    url, discovery = widgets.widget_request(page_body, page_source, script_body, identity)
    proof.require(url == quote_source['url'] and discovery == data['quote_discovery'],
                  'plpl3_issuer_published_native_history_contract_changed')
    monthly = widgets.parse_widget_history(quote_body, quote_source, identity)
    proof.require(monthly['has_61_consecutive_months'], 'plpl3_native_prices_do_not_cover_five_years')
    events = parse_calendar(cash_body)
    expected = defaultdict(Decimal)
    for event in events:
        expected[date.fromisoformat(event['record_date'])] += Decimal(event['gross_per_share_decimal'])
    return {'provider': 'Fundamentus_calendar_CVM_corrections_and_issuer_Enfoque',
        'pagination_basis': 'secondary_provider_cash_table_with_primary_date_corrections',
        'expected_cash': {day: float(value) for day, value in expected.items()},
        'raw_quotes': {date.fromisoformat(r['date']): r['close'] for r in monthly['monthly_prices']},
        'sources': proof.merge_sources(primary['sources'] + [seal, cash_source, quote_source, page_source, script_source]),
        'calendar': {'events': events}, 'checks': {'cash_rows': len(events), 'native_monthly_prices': 61,
            'primary_date_corrections': primary['contract'], 'secondary_schedule_conflict_retained': True,
            'provider_cash_precision': 'four_decimal_places_originals_preserved',
            'source_completeness_certified': False, 'ultimate_upstream_independence_verified': False,
            'all_provider_events_are_dividends_no_JCP_tax_inference': True,
            'gross_reconstructed_from_net': False, 'synthetic_exchange_rows_created': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False}}


def validate_primary_actions(folder, identity, cutoff, observed_actions):
    proof.require({k: identity.get(k) for k in IDENTITY} == IDENTITY and cutoff == proof.CUTOFF,
                  'plpl3_exact_security_or_reference_required')
    proof.require(all(date.fromisoformat(a['date']) < date(2021, 9, 1) for a in observed_actions),
                  'plpl3_new_primary_action_review_required')
    return {'sources': [], 'actions_observed_before_window': observed_actions,
            'primary_action_absence_certified': False,
            'model_price_basis_requires_all_61_independent_native_comparisons': True}
