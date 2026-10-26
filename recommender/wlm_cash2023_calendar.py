"""Bounded WLM follow-on: add the explicit missing August 2023 JCP group.

The frozen 2022/2024 repair remains unchanged. This module reads exactly two
new primary originals offline, retains every receipt, and never admits assets.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
from pathlib import Path
import re
import unicodedata

from . import wlm_native_calendar as frozen


CACHE = 'raw/primary-research/wlm-cash2023-calendar'
CUTOFF = frozen.CUTOFF
ProofError = frozen.ProofError
require = frozen.require
PINS = {
    'notice': ('642682', 2, '53e546b771be9fb3d09c76da5eb71deee2d8e1fbbd9a9db3d90da801d68dcfa6',
        '315beecdb6f67bc927ffa8293b3732a4548a34a25c80b45e11f1986fb0ea6f8a',
        'fd0902d479be8c50f7a7ec4c56aa20418d6eab4b7ee6b84887b6e40f1dfbe36f'),
    'form': ('644048', 1, '25a08eb8b90a1bcc8f24a8892b2ec343c2ae0c2254c9d2d4ee347df0c959891c',
        '89ced3c57415dca5b45729d83ee78224d77ce011890d4340a89efa65f24d7af0',
        '80bb65a0ffc026027c36ce3e695379c3cbd0fc76048f84dc1fc411626a742bd1'),
}


def select_documents(documents):
    require(all(frozen.cvm.document_id(d['url']) == (d['sequence'],d['version'])
                and d['index_evidence']['raw_row']['Link_Download'] == d['url'] for d in documents),
            'cash2023_index_document_identity_inconsistent')
    selected = {}
    for role, reference, category, sequence, version, publication in (
        ('notice', '2023-06-29', 'aviso aos acionistas', '642682', 2, '2023-06-29'),
        ('form', '2023-06-28', 'relatorio proventos', '644048', 1, '2023-07-04'),
        ('approval_index', '2023-06-28', 'reuniao da administracao', '642675', 2, '2023-06-29')):
        def family(doc):
            match = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)',
                doc['index_evidence']['raw_row']['Protocolo_Entrega'])
            return (match[1], match[2]) if match else None
        seeds = [d for d in documents if d['category'] == category and d['reference_date'] == reference]
        families = {family(d) for d in seeds if family(d)}
        # Native forms have no IPE family in the public index. A changed
        # reference on the same explicit download protocol still cannot hide a
        # later revision; do not group unrelated approvals by nearby dates.
        def download_protocol(doc):
            return tuple(frozen.parse_qs(frozen.urlsplit(doc['url']).query).get('numProtocolo', []))
        protocols = {download_protocol(d) for d in seeds if download_protocol(d)}
        rows = [d for d in documents if d['category'] == category
                and (d['reference_date'] == reference or family(d) in families
                     or download_protocol(d) in protocols)]
        require(rows, 'cash2023_exact_primary_document_missing:' + role)
        latest = [d for d in rows if d['version'] == max(r['version'] for r in rows)
                  and d['published_at'] == max(r['published_at'] for r in rows)]
        require(len(latest) == 1 and latest[0]['sequence'] == sequence and latest[0]['version'] == version
                and latest[0]['published_at'] == publication,
                'cash2023_latest_revision_unreviewed_no_fallback:' + role)
        selected[role] = latest[0]
    return selected


def read_originals(reader, documents):
    originals = {}
    for role, (sequence, version, digest, receipt_sha, request_sha) in PINS.items():
        document = documents[role]
        stem = 'cash2023-' + sequence + '-v' + str(version)
        request, _ = reader.json(frozen.CACHE + '/request-' + stem + '.json',
            'cash2023_actual_single_attempt_request', request_sha)
        receipt, _ = reader.json(frozen.CACHE + '/response-' + stem + '-' + receipt_sha + '.json',
            'cash2023_actual_transport_receipt', receipt_sha)
        require(request['document'] == receipt['document'] == document
                and request['url'] == receipt['url'] == receipt['response_url'] == document['url']
                and receipt['http_status'] == 200 and receipt['sha256'] == digest
                and receipt['network_requests'] == 1 and receipt['retries_performed'] == 0
                and receipt['redirects'] is False and receipt['access_stop'] is False
                and request['phase_max_requests'] == receipt['phase_max_requests'] == 2,
                'cash2023_exact_index_protocol_transport_binding_required')
        name = frozen.CACHE + '/' + stem + '-' + digest + '.pdf'
        require(receipt['file'] == name, 'cash2023_receipt_filename_mismatch')
        body, source = reader.read(name, 'cash2023_original_primary_' + role, digest,
            document['url'], receipt['retrieved_at'], document['published_at'])
        require(body.lstrip().startswith(b'%PDF-') and len(body) == receipt['bytes'],
                'cash2023_actual_whole_original_PDF_required')
        source['authority'] = 'primary_CVM_original_reported_schedule_not_settlement'
        originals[role] = {'body': body, 'source': source}
    return originals


def parse_notice(pages, document):
    require(len(pages) == 1, 'cash2023_whole_one_page_notice_required')
    text = ' '.join(unicodedata.normalize('NFKD', pages[0]).encode('ascii', 'ignore').decode().split())
    require('CNPJ: 33.228.024/0001-51' in text and '33.229.024' not in text
            and text.count('CNPJ:') == 1
            and 'WLM PARTICIPACOES E COMERCIO DE MAQUINAS' in text
            and 'DISTRIBUICAO DE JUROS SOBRE CAPITAL PROPRIO' in text
            and document['sequence'] == '642682' and document['version'] == 2
            and document['reference_date'] == document['published_at'] == '2023-06-29'
            and document['index_evidence']['raw_row']['Protocolo_Entrega'] == '011070IPE290620230204510611-18'
            and re.sub(r'\D', '', document['index_evidence']['raw_row']['CNPJ_Companhia']) == frozen.CNPJ,
            'cash2023_explicit_issuer_kind_latest_notice_identity_required')
    require('em reuniao de 28/06/2023' in text and 'montante bruto de R$8.000.000,14' in text
            and '15% (quinze por cento)' in text
            and 'comprovar esta condicao ate o dia 14 de julho de 2023' in text
            and 'posicao final de acoes da Companhia em 08 de agosto de 2023.' in text
            and 'O pagamento sera efetuado na data de 25 de agosto de 2023.' in text
            and 'exercicio social do ano de 2023' in text
            and text.endswith('Rio de Janeiro, 29 de junho de 2023. Leandro Cardoso Massa Diretor de Relacoes com Investidores'),
            'cash2023_full_explicit_approval_record_payment_tax_roles_and_signature_required')
    rows = re.findall(r'Valor bruto por acao (ON|PN) \((Ordinarias|preferenciais)\): R\$(0,\d+)', text)
    require(len(rows) == 2 and [(r[0], r[1]) for r in rows] == [('ON','Ordinarias'),('PN','preferenciais')]
            and text.count('Valor bruto por acao') == 2 and 'Valor liquido por acao' not in text,
            'cash2023_whole_two_class_explicit_gross_required')
    gross = {share: str(frozen.decimal(amount)) for share, _, amount in rows}
    return {'label': 'JCP', 'approval_date': '2023-06-28', 'record_date': '2023-08-08',
        'payment_date': '2023-08-25', 'tax_document_deadline': '2023-07-14',
        'published_at': document['published_at'], 'gross_per_share_by_source_class': gross,
        'whole_body_reviewed': True, 'page_count': 1, 'gross_derived_from_net_or_class_ratio': False,
        'payment_date_is_explicit_not_deadline': True, 'settlement_verified': False,
        'source_locator': {'page': 1, 'paragraph': 'explicit holding/payment/gross class passage'}}


def parse_form(tables, document, notice, approval_index):
    require(len(tables) == 3 and len(tables[1]) == 4 and len(tables[1][1]) == 7
            and len(tables[2]) == 1 and len(tables[2][0]) == 2,
            'cash2023_complete_native_two_class_form_required')
    metadata = frozen.metadata
    require(frozen.cvm.norm(metadata(tables, 'denominacao social')) == frozen.cvm.norm(document['issuer_name'])
            and frozen.cvm.norm(metadata(tables, 'ato societario')) == 'rca'
            and document['sequence'] == '644048' and document['version'] == 1
            and frozen.cvm.compact(metadata(tables, 'versao')) == '1',
            'cash2023_native_exact_issuer_form_version_required')
    protocol = frozen.cvm.compact(metadata(tables, 'protocolo provento'))
    ipe = re.findall(r'\b\d{6}IPE\d{8}\d{2}\d+-\d+\b', metadata(tables, 'protocolo ipe'))
    require(protocol == '1117946' and ipe == ['011070IPE280620230204510611-53']
            and approval_index['sequence'] == '642675' and approval_index['version'] == 2
            and approval_index['index_evidence']['raw_row']['Protocolo_Entrega'] == ipe[0]
            and frozen.parse_qs(frozen.urlsplit(approval_index['url']).query).get('numProtocolo') == [protocol],
            'cash2023_native_latest_indexed_approval_protocol_binding_required')
    require(frozen.day(metadata(tables, 'data aprovacao')) == notice['approval_date'] == document['reference_date']
            and frozen.day(metadata(tables, 'ultimo dia de negociacao com direitos')) == notice['record_date']
            and frozen.day(metadata(tables, 'data envio')) == '2023-06-29'
            and document['published_at'] == '2023-07-04',
            'cash2023_native_notice_dates_or_actual_index_publication_conflict')
    require(frozen.cvm.norm(tables[1][1][0]) == 'codigo isin'
            and frozen.cvm.norm(tables[1][1][1]) == 'valor bruto r unidade'
            and frozen.cvm.norm(tables[1][1][6]) == 'data pagamento'
            and frozen.cvm.norm(tables[2][0][0]) == 'dia de pagamento das parcelas'
            and frozen.cvm.norm(tables[2][0][1]) == 'valor bruto r unidade',
            'cash2023_native_explicit_gross_and_empty_installment_headers_required')
    components = []
    for index, row in enumerate(tables[1][2:], 2):
        require(len(row) == 7, 'cash2023_native_malformed_row')
        isin = frozen.cvm.compact(row[0])
        shares = [share for share, (_, expected) in frozen.CLASSES.items() if isin == expected]
        require(len(shares) == 1, 'cash2023_native_exact_class_ISIN_required')
        share = shares[0]
        gross = frozen.decimal(frozen.cvm.compact(row[1]))
        require(frozen.cvm.norm(row[2]) == 'anual' and row[3] == '2023'
                and frozen.cvm.norm(row[4]) == 'nao' and frozen.cvm.norm(row[5]) == 'a vista'
                and frozen.day(row[6]) == notice['payment_date']
                and gross == Decimal(notice['gross_per_share_by_source_class'][share]),
                'cash2023_native_notice_gross_class_schedule_conflict')
        components.append({'share_class': share, 'isin': isin, 'gross_per_share_decimal': str(gross),
            'label': notice['label'], 'approval_date': notice['approval_date'], 'record_date': notice['record_date'],
            'payment_date': notice['payment_date'], 'source_locator': {'page':1,'table':1,'row':index}})
    require(Counter(c['share_class'] for c in components) == Counter({'ON':1,'PN':1}),
            'cash2023_all_classes_exactly_once_required')
    return {'components': components, 'native_cash_kind_not_reported': True,
        'cash_kind_source': 'explicit_latest_issuer_notice_not_amount_or_status', 'native_protocol': protocol,
        'native_IPE': ipe[0], 'native_sent_date': '2023-06-29', 'actual_index_publication': '2023-07-04',
        'latest_approval_index_bound_but_body_not_downloaded_or_claimed_reviewed': True}


def verify_independent_group(notice, form, detail, page):
    require(detail['cnpj'] == frozen.CNPJ and detail['codeCVM'] == '11070'
            and {(r['code'],r['isin']) for r in detail['otherCodes']} == set(frozen.CLASSES.values()),
            'cash2023_exact_independent_issuer_security_identity_required')
    require(page['page']['totalPages'] == page['page']['pageNumber'] == 1
            and page['page']['totalRecords'] == len(page['results']), 'cash2023_full_independent_page_required')
    rows = [r for r in page['results'] if frozen.cvm.day(r['lastDatePriorEx']) == notice['record_date']]
    require(len(rows) == 2 and all(r['corporateAction'] == 'JRS CAP PROPRIO'
            and frozen.cvm.day(r['dateApproval']) == notice['approval_date']
            and r['quotedPerShares'] == r['ratio'] == '1' for r in rows)
            and Counter((r['typeStock'],frozen.decimal(r['valueCash'])) for r in rows) == Counter(
                (r['share_class'],Decimal(r['gross_per_share_decimal'])) for r in form['components']),
            'cash2023_whole_independent_both_class_kind_gross_rights_conflict')
    return deepcopy(rows)


def merge_sources(*groups):
    merged = {}
    for source in (s for group in groups for s in group):
        key = source['file'],source['sha256'],source.get('url')
        if key not in merged:
            merged[key] = deepcopy(source)
        else:
            merged[key]['roles'] = sorted(set(merged[key].get('roles',[])) | set(source.get('roles',[])))
    return list(merged.values())


def verified_cash2023_group(folder, stock, cutoff=CUTOFF):
    native_class = 'PN' if stock.get('ticker') == 'WLMM4' and stock.get('share_class') == 'PNA' else stock.get('share_class')
    require(cutoff == CUTOFF and native_class in frozen.CLASSES and type(stock.get('cvm')) is int
            and stock['cvm'] == 11070 and (stock.get('ticker'),stock.get('isin')) == frozen.CLASSES[native_class]
            and (native_class != 'PN' or stock.get('share_class') == 'PNA'), 'cash2023_exact_verified_security_required')
    reader = frozen.EvidenceReader(folder)
    indexed, coverage = frozen.indexed_documents(reader, cutoff)
    documents = select_documents(indexed)
    originals = read_originals(reader, documents)
    pages, _ = frozen.pdf_pages(originals['notice']['body'], 1)
    notice = parse_notice(pages, documents['notice'])
    _, tables = frozen.pdf_pages(originals['form']['body'], 1)
    form = parse_form(tables, documents['form'], notice, documents['approval_index'])
    payloads, _ = frozen.cached_sources(reader)
    independent = verify_independent_group(notice, form, payloads['expanded/b3-detail-11070.json'],
                                           payloads['expanded/WLMM4-b3-cash-1.json'])
    if native_class == 'PN':
        crosswalk = frozen.read_wlmm4_class_crosswalk(reader)
    else:
        crosswalk = None
    return {'stock': {k:stock[k] for k in frozen.STOCK}, 'native_share_class': native_class,
        'as_of': cutoff.isoformat(), 'notice': notice, 'native_form': form, 'documents': documents,
        'independent_whole_class_group': independent, 'class_crosswalk': crosswalk,
        'sources': list(reader.sources.values()), 'IPE_coverage': coverage,
        'whole_both_class_group_verified': True, 'network_requests_in_verification': 0,
        'separate_followon_acquisition_requests': 2, 'settlement_verified': False,
        'point_in_time_archive_verified': False, 'admission_performed': False}


def augment_missing_group(parsed, proof):
    """Add one complete new primary group, never clone/shift a nearby vendor row."""
    stock, notice = proof['stock'], proof['notice']
    require(not any(e.get('record_date') == notice['record_date'] for e in parsed['events']),
            'cash2023_missing_group_already_present_no_partial_replacement')
    native, = [r for r in proof['native_form']['components'] if r['share_class'] == proof['native_share_class']]
    originals = [s for s in proof['sources'] if s['sha256'] in (PINS['notice'][2],PINS['form'][2])]
    require(len(originals) == 2 and all(s['published_at'] <= proof['as_of'] for s in originals),
            'cash2023_both_actual_primary_schedules_required')
    amount = float(Decimal(native['gross_per_share_decimal']))
    event = {'ticker':stock['ticker'], 'label':'JCP', 'isin':stock['isin'], 'record_date':notice['record_date'],
        'approval_date':notice['approval_date'], 'payment_date':notice['payment_date'],
        'published_at':max(s['published_at'] for s in originals),
        'publication_basis':'latest_required_primary_index_delivery_not_native_sent_date_or_PIT_archive',
        'available_as_of':True, 'gross_per_share':amount, 'reported_amount':amount, 'quoted_quantity':1,
        'primary_exact_gross_per_share_decimal':native['gross_per_share_decimal'], 'date_order_valid':True,
        'reported_status':None, 'reference_date':None, 'status':'past_scheduled', 'share_basis':'reported_unverified',
        'source_row':'primary:cvm644048v1:'+stock['isin'], 'source_locator':deepcopy(native['source_locator']),
        'primary_evidence':deepcopy(originals), 'primary_declaration_id':stock['ticker']+':2023-08-08:cvm642682v2',
        'payment_date_basis':'explicit_latest_notice_v2_and_latest_native_form_v1',
        'cash_model_eligible':False, 'financially_confirmed':False, 'paid_status_as_of_verified':False,
        'payment_status_observed_at':None, 'reported_expired':False, 'full_settlement_on_date_verified':False}
    out = deepcopy(parsed)
    before_issues = deepcopy(out['report']['issues'])
    out['events'].append(event)
    frozen.inspector._finish(out)
    require(out['report']['issues'] == before_issues, 'cash2023_unexpected_issue_change_no_existing_blocker_removed')
    audit = out['wlm_full_calendar_repair']
    audit['cash2023_missing_group_augmentation'] = {'original_target_group':[], 'added_primary_group':[deepcopy(event)],
        'proof':deepcopy(proof), 'closest_vendor_rows_used_as_replacement':False,
        'existing_dates_amounts_or_labels_changed':False, 'existing_issues_removed':False,
        'partial_group_or_zero_imputation':False, 'gross_from_class_ratio_or_tax_rescaling':False,
        'reported_schedule_not_settlement':True, 'normal_cash_price_share_and_annual_gates_required':True}
    audit['evidence_sources'] = merge_sources(audit['evidence_sources'], proof['sources'])
    return out


def repair_full_inspected_calendar(folder, stock, cutoff, parsed):
    repaired = frozen.repair_full_inspected_calendar(folder, stock, cutoff, parsed)
    proof = verified_cash2023_group(folder, stock, cutoff)
    return augment_missing_group(repaired, proof)


def canonical_independent_cash_rows(folder, stock, cutoff, rows):
    view = frozen.canonical_independent_cash_rows(folder, stock, cutoff, rows)
    proof = verified_cash2023_group(folder, stock, cutoff)
    view['evidence_sources'] = merge_sources(view['evidence_sources'], proof['sources'])
    view['cash2023_primary_group_proof'] = proof
    return view


def save_report(folder, report):
    body = frozen.shared._dump(report)
    name = CACHE + '/report-' + hashlib.sha256(body).hexdigest() + '.json'
    path = frozen.shared._safe(Path(folder).resolve(), name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, 'immutable_cash2023_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name
