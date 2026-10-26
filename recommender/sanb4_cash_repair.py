"""Literal SANB4 PN components from final native notice 492293, offline only."""

from datetime import date
from decimal import Decimal
import io

import pdfplumber

from . import cash_repair_proofs as proof
from . import cvm_proventos_documents as cvm


IDENTITY = {'ticker': 'SANB4', 'cvm': 20532, 'share_class': 'PN', 'isin': 'BRSANBACNPR5'}
CNPJ = '90.400.888/0001-42'
REPAIR_ID = 'SANB4_492293_final_PN_components_v1'
BODY_SHA = '7c092093b7b9c299f21a265e5f5a270a27c6067b8ad75250082d9ddfbba7ea1a'
BODY_FILE = 'raw/primary-research/all-pending/cash/body-' + BODY_SHA + '.bin'
PAYMENT_SHA = '9f654d6e24a4786af9b3c2616eef42e7e0348946a56ee5e8aeb653f260d3f2ca'
DETAIL_SHA = 'f806e63d1446f5205fd1c3390ad127840dfc985485f0d7a6a594c5b3be521ea4'


def parse_notice(body, pages, document):
    proof.require(len(pages) == 2 and CNPJ in pages[0]
        and 'BANCO SANTANDER (BRASIL) S.A.' in pages[0], 'sanb4_complete_native_issuer_notice_required')
    proof.require('no final do dia 20 de abril de 2022 (inclusive).' in pages[0]
        and 'a partir de 22 de abril de 2022 (inclusive)' in pages[0]
        and 'serao pagos a partir do dia 16 de maio de 2022' in pages[1]
        and 'Sao Paulo, 14 de abril de 2022.' in pages[1], 'sanb4_native_record_ex_payment_start_required')
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        tables = pdf.pages[0].extract_tables()
    proof.require(len(tables) == 1 and len(tables[0]) == 4 and all(len(r) == 4 for r in tables[0]),
                  'sanb4_whole_ON_PN_UNIT_native_table_required')
    header, on, pn, unit = tables[0]
    proof.require(cvm.norm(header[0]) == 'especie' and 'dividendos intercalares' in cvm.norm(header[1])
        and 'valor bruto' in cvm.norm(header[2]) and 'valor liquido' in cvm.norm(header[3])
        and cvm.norm(on[0]) == 'on ordinarias' and cvm.norm(pn[0]) == 'pn preferenciais'
        and cvm.norm(unit[0]) == 'unit', 'sanb4_native_class_and_gross_headers_required')
    dividend, jcp, net = (str(cvm.gross(v)) for v in pn[1:])
    proof.require((dividend, jcp, net) == ('0.09839755610', '0.14056793729', '0.11948274669'),
                  'sanb4_literal_PN_gross_net_mismatch')
    facts = []
    for column, label, gross, reported_net in ((1, 'DIVIDENDO', dividend, None), (2, 'JCP', jcp, net)):
        facts.append({**IDENTITY, 'label': label, 'record_date': '2022-04-20', 'ex_date': '2022-04-22',
            'payment_date': '2022-05-16', 'payment_start_date': '2022-05-16', 'approval_date': '2022-04-14',
            'published_at': document['published_at'], 'gross_per_share_decimal': gross,
            'net_per_share_decimal': reported_net, 'payment_date_basis': 'explicit_payment_start_date',
            'payment_claim_basis': 'native_declared_payment_start_not_bank_settlement',
            'source_locator': {'page': 1, 'table': 0, 'row': 2, 'column': column,
                'raw_native_row': pn, 'record_ex_page': 1, 'payment_start_page': 2},
            'source_locator_id': 'cash-repair:492293:v1:PN:' + label})
    return facts


def load_proof(identity, cutoff, cache):
    proof.require(cutoff == proof.CUTOFF, 'sanb4_exact_reference_required')
    sources = proof.verified_identity(cache, identity, IDENTITY, DETAIL_SHA, CNPJ)
    document, index_source = proof.index_document(cache, 2022, '492293', 1, 20532, CNPJ,
        '2022-04-14', 'aviso aos acionistas')
    body, source, pages = proof.original(cache, document, BODY_FILE, BODY_SHA)
    facts = parse_notice(body, pages, document)
    sources += [index_source, source]
    proof.register(cache, sources)
    return {'sources': sources, 'facts': facts, 'contract': {
        'native_notice': document, 'selected_native_class': 'PN', 'exact_isin': IDENTITY['isin'],
        'payment_basis': 'explicit_start_2022-05-16_not_settlement',
        'components_not_two_payments_of_total': True, 'ON_and_UNIT_columns_excluded': True,
        'gross_sum_decimal': str(sum((Decimal(r['gross_per_share_decimal']) for r in facts), Decimal(0))),
        'unknown_dates_inferred': False, 'native_final_revision_verified': True}}


def apply_exact_repairs(parsed, identity, cutoff, cache):
    """Call after inspect_payment_page and before enrich_calendar; returns a copy."""
    if identity.get('ticker') != 'SANB4':
        return parsed
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    evidence = load_proof(identity, cutoff, cache)
    old = [{'label': 'JCP', 'payment_date': '2022-05-16', 'gross_per_share': 0.140567,
            'source_row': 'table:2:row:24'},
           {'label': 'DIVIDENDO', 'payment_date': '2022-05-16', 'gross_per_share': 0.098937,
            'source_row': 'table:2:row:25'}]
    return proof.apply_groups(parsed, identity, cutoff, evidence, repair_id=REPAIR_ID,
        payment_sha=PAYMENT_SHA, groups={'2022-04-20': {'expected_original': old, 'facts': evidence['facts']}})
