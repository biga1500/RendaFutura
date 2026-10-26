"""Native cash recovery, isolated from published inventories and pointers."""
from __future__ import annotations

import argparse
from copy import deepcopy
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urljoin, urlsplit
import zipfile

from lxml import html

from .bulk_pending_sources import PublicSourceCollector, approved
from .bulk_pending_sources import MAX_BYTES, USER_AGENT, _save, _stopped
from .pending_missing_a import expanded_rows, native_decimal, norm

CUTOFF = date(2026, 9, 30)
CACHE = 'raw/primary-research/all-pending/round2-cash'
ROOT = Path(__file__).parent / 'data/market'
BUNDLE_SHA = 'c2fe947b3298ceda50fec4af9e408ab3ac4f989d074bf59ca36a392c4e5cefc3'
TARGETS = {
    'ALLD3': (25330, 'ON', 'BRALLDACNOR3'),
    'FESA3': (3069, 'ON', 'BRFESAACNOR8'),
    'FESA4': (3069, 'PN', 'BRFESAACNPR5'),
    'JSLG3': (22020, 'ON', 'BRJSLGACNOR2'),
    'HBOR3': (20877, 'ON', 'BRHBORACNOR3'),
}


def require(value, reason):
    if not value:
        raise ValueError('round2_cash_' + reason)


def read(root, source):
    root = Path(root).resolve()
    path = root / source['file']
    require(path.resolve().is_relative_to(root) and path.is_file()
            and all(not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)()
                    for p in (path, *path.parents) if p.is_relative_to(root)), 'unsafe_original')
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == source['sha256'], 'original_hash_changed')
    if 'bytes' in source:
        require(len(body) == source['bytes'], 'original_length_changed')
    return body


@lru_cache(maxsize=4)
def receipts(root):
    root = Path(root)
    result = []
    for path in (root / 'raw/primary-research/all-pending').glob('*/response-*.json'):
        source = json.loads(path.read_bytes())
        if source.get('status') == 200 and not source.get('error'):
            result.append(source)
    return result


@lru_cache(maxsize=4)
def index_rows(root):
    root = Path(root)
    sources = json.loads((root / 'raw/primary-research/cvm-documents/index-sources.json').read_bytes())
    output = []
    for source in sources:
        body = read(root, source)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            member = archive.namelist()[0]
            rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            for line, row in enumerate(rows, 2):
                if row['Codigo_CVM'].isdigit() and int(row['Codigo_CVM']) in {v[0] for v in TARGETS.values()}:
                    output.append({**row, 'index_source': source, 'index_member': member, 'index_line': line})
    return output


def final_rows(root, cvm):
    rows = [r for r in index_rows(root) if int(r['Codigo_CVM']) == cvm
            and r['Data_Entrega'][:10] <= CUTOFF.isoformat()]
    families = defaultdict(list)
    for row in rows:
        protocol = row['Protocolo_Entrega']
        match = re.fullmatch(r'(\d{6}IPE\d{8})\d{2}(\d+-\d+)', protocol)
        family = (match[1], match[2]) if match else parse_qs(urlsplit(row['Link_Download']).query).get('numProtocolo', [protocol])[0]
        families[(row['Categoria'], str(family))].append(row)
    return sorted((max(group, key=lambda r: (int(r['Versao']), r['Data_Entrega']))
                   for group in families.values()), key=lambda r: r['Data_Referencia'])


def pdf_pages(body):
    from pypdf import PdfReader
    require(body.lstrip().startswith(b'%PDF'), 'final_document_not_native_pdf')
    return [p.extract_text() or '' for p in PdfReader(io.BytesIO(body)).pages]


def parse_ferbasa_notice(body, source):
    """Parse paired native ON/PN gross/net paragraphs, retaining exact decimals."""
    text = norm(' '.join(pdf_pages(body))).replace('ac ao', 'acao').replace('a cao', 'acao')
    require('15.141.799/0001-03' in text and 'ferbasa' in text and 'aviso aos acionistas' in text,
            'ferbasa_native_identity_missing')
    mentions = list(re.finditer(r'r\$\s*([01],\s*[\d ]+)\s*(?:para cada|por 01 \(uma\))\s*acao\s*'
                               r'(ordin\s*ar\s*i\s*a|preferencial)', text))
    amounts = []
    for match in mentions:
        amounts.append({'class': 'ON' if match[2].startswith('ordin') else 'PN',
                        'decimal': native_decimal(match[1]), 'literal': match[0], 'offset': match.start()})
    require(amounts and all(a['decimal'] for a in amounts), 'ferbasa_class_amounts_missing')
    require([a['class'] for a in amounts] == ['ON', 'PN'] * (len(amounts) // 2),
            'ferbasa_class_pair_order_ambiguous')
    record = re.search(r'posic(?:oes|ao) acionaria.{0,200}?(\d{2}/\d{2}/20\d{2})', text)
    ex = re.search(r'(?:negociadas|negociados).{0,60}?ex.{0,70}?(\d{2}/\d{2}/20\d{2})', text)
    require(record and ex, 'ferbasa_record_or_ex_role_missing')
    def iso(value):
        return datetime.strptime(value, '%d/%m/%Y').date().isoformat()
    record_date, ex_date = iso(record[1]), iso(ex[1])
    payment = re.search(r'programado[s]? para creditamento.{0,100}?(\d{2}/\d{2}/20\d{2})', text)
    if not payment:
        payment = re.search(r'pagamento.{0,85}?efetuado a partir do dia (\d{2}/\d{2}/20\d{2})', text)
    components = []
    if source['sha256'] == '92725eb6176828929137edff1f86244482a8985ca088fbda0c7400f41c65966c':
        require(len(amounts) == 4 and '04 de maio de 2022' in text and '12 de maio de 2022' in text,
                'ferbasa_revised_may_payment_text_changed')
        components = [('DIVIDENDO', '2022-05-04', amounts[:2], []),
                      ('DIVIDENDO', '2022-05-12', amounts[2:4], [])]
    elif source['sha256'] == '3244b633856ddb7006ffb69408559439b529c18d985f0dab5fb4056485b17870':
        require(len(amounts) == 8 and 'primeira a partir de 05/12/2025' in text
                and 'segunda a partir de 12/06/2026' in text, 'ferbasa_unequal_installments_changed')
        components = [('JCP', '2025-12-05', amounts[:2], amounts[2:4]),
                      ('JCP', '2026-06-12', amounts[4:6], amounts[6:8])]
    else:
        require(payment and len(amounts) in (4, 6) and 'valores brutos' in text,
                'ferbasa_unsupported_component_or_payment_template')
        components = [('JCP', iso(payment[1]), amounts[:2], amounts[2:4])]
        if len(amounts) == 6:
            require('dividendos complementares' in text, 'ferbasa_unlabelled_second_component')
            components.append(('DIVIDENDO', iso(payment[1]), amounts[4:6], []))
    return {'record_date': record_date, 'ex_date': ex_date,
            'record_literal': record[0], 'ex_literal': ex[0], 'components': [
                {'label': label, 'payment_date': day, 'payment_date_role': 'reported_payment_start',
                 'gross': {a['class']: a['decimal'] for a in gross},
                 'net': {a['class']: a['decimal'] for a in net},
                 'native_amount_mentions': gross + net} for label, day, gross, net in components]}


def ferbasa_parse_report(root):
    result = []
    for source in receipts(str(root)):
        meta = source.get('discovery', {}).get('native_metadata', {})
        if (meta.get('company_id') != 'e71f73d4-4d0a-41fb-8e5b-8d7f3660b6ad'
                or not norm(meta.get('file_title', '')).startswith('aviso')):
            continue
        item = {'source': {k: source.get(k) for k in ('file', 'sha256', 'url', 'bytes', 'retrieved_at')},
                'metadata': meta}
        try:
            item['parsed'] = parse_ferbasa_notice(read(root, source), source)
        except ValueError as error:
            item['blocker'] = str(error)
        result.append(item)
    return result


def exact_identity(root, identity):
    from .pending_b_independent import FCA_PINS
    from .security_registration import _kind
    ticker = identity.get('ticker')
    require(ticker in TARGETS and tuple(identity.get(k) for k in ('cvm', 'share_class', 'isin')) == TARGETS[ticker],
            'exact_CVM_class_ISIN_mismatch')
    from .dividend_scope import allowed_tickers
    require(ticker in (allowed_tickers(root) or ()), 'outside_selected_DY_scope')
    cvm, share, isin = TARGETS[ticker]
    path = Path(root) / f'raw/expanded/b3-detail-{cvm}.json'
    body = path.read_bytes()
    detail = json.loads(body)
    require(int(detail['codeCVM']) == cvm and [r['isin'] for r in detail['otherCodes'] if r['code'] == ticker] == [isin],
            'cached_exact_ISIN_disagrees')
    sources = [{'file': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(body).hexdigest(),
                'bytes': len(body), 'url': None, 'retrieved_at': None,
                'authority': 'read_only_exact_exchange_ISIN_cache_not_cash_or_quote_evidence'}]
    proof = []
    for year, digest in FCA_PINS.items():
        filename = (f'raw/inventory/fca_cia_aberta_{year}.zip' if year == 2026 else
                    f'raw/primary-research/units/fca_cia_aberta_{year}-{digest}.zip')
        source = {'file': filename, 'sha256': digest,
                  'url': f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip',
                  'retrieved_at': None, 'published_at': None}
        with zipfile.ZipFile(io.BytesIO(read(root, source))) as archive:
            def rows(member):
                return list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
            masters = [r for r in rows(f'fca_cia_aberta_{year}.csv')
                       if int(r['CD_CVM']) == cvm and r['DT_RECEB'] <= str(CUTOFF) and r['DT_REFER'] <= str(CUTOFF)]
            require(masters, 'FCA_master_missing')
            master = max(masters, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            securities = [(line, r) for line, r in enumerate(rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv'), 2)
                          if r['ID_Documento'] == master['ID_DOC'] and r['Codigo_Negociacao'].strip() == ticker]
            if not securities:
                proof.append({'year': year, 'master': master, 'security': None,
                    'status': 'latest_FCA_master_has_no_exact_equity_child_no_older_version_substituted',
                    'source_file': filename, 'source_sha256': digest})
                sources.append(source)
                continue
            require(len(securities) == 1, 'FCA_exact_security_missing_or_ambiguous')
            line, row = securities[0]
            require(_kind(row)[1] == share and row['CNPJ_Companhia'] == master['CNPJ_CIA']
                    and row['Data_Referencia'] == master['DT_REFER'] and row['Versao'] == master['VERSAO']
                    and re.sub(r'\D', '', row['CNPJ_Companhia']) == detail['cnpj'], 'FCA_class_or_master_join_conflict')
            proof.append({'year': year, 'master': master, 'security': row, 'line': line,
                          'source_file': filename, 'source_sha256': digest})
        sources.append(source)
    require(any(p.get('security') for p in proof), 'primary_exact_class_unavailable')
    return proof, sources


def notice_binding(root, cvm, published, *, reference=None):
    eligible = [r for r in final_rows(str(root), cvm) if norm(r['Categoria']) == 'aviso aos acionistas'
                and r['Data_Entrega'][:10] == published and (not reference or r['Data_Referencia'][:10] == reference)]
    require(len(eligible) == 1, 'final_native_notice_index_ambiguous')
    row = eligible[0]
    # An RI notice is corroborated by the final index revision, not relabelled as
    # a downloaded CVM body. Publication and retrieval remain separate.
    return {'index_source': row['index_source'], 'member': row['index_member'], 'line': row['index_line'],
            'native_index_row': {k: v for k, v in row.items() if not k.startswith('index_')},
            'RI_body_not_claimed_byte_identical_to_CVM_download': True}


def _event(identity, label, record, payment, gross, source, *, net=None, approval=None,
           ex=None, published=None, locator=None, literals=None, role='reported_payment_start'):
    return {**{k: identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')}, 'label': label,
            'record_date': record, 'record_date_role': 'entitlement_record', 'payment_date': payment,
            'payment_date_role': role, 'approval_date': approval, 'ex_date': ex,
            'gross_per_share_decimal': gross, 'net_per_share_decimal': net,
            'amount_tax_basis': 'explicit_gross' if label == 'JCP' else 'native_dividend_per_share',
            'gross_reconstructed_from_net': False, 'published_at': published,
            'source': source, 'source_locator': locator, 'native_literals': literals,
            'settlement_verified': False, 'point_in_time_archive_verified': False}


def ferbasa_calendar(root, identity):
    require(identity['ticker'] in ('FESA3', 'FESA4'), 'ferbasa_identity_unsupported')
    events, sources, bindings = [], [], []
    reports = ferbasa_parse_report(root)
    require(len(reports) == 20 and all('parsed' in r for r in reports), 'ferbasa_notice_set_incomplete')
    for report in reports:
        source, parsed, meta = report['source'], report['parsed'], report['metadata']
        published = meta['file_published_date'][:10]
        binding = notice_binding(root, identity['cvm'], published)
        sources.extend([source, binding['index_source']])
        bindings.append(binding)
        for index, component in enumerate(parsed['components'], 1):
            events.append(_event(identity, component['label'], parsed['record_date'], component['payment_date'],
                component['gross'][identity['share_class']], source,
                net=component['net'].get(identity['share_class']), ex=parsed['ex_date'], published=published,
                locator=f'PDF page 1 native class component {index}',
                literals={'record': parsed['record_literal'], 'ex': parsed['ex_literal'],
                          'amounts': component['native_amount_mentions']}))
    return {'events': sorted(events, key=lambda e: (e['record_date'], e['payment_date'], e['label'])),
            'sources': sources, 'final_notice_bindings': bindings, 'blockers': [],
            'original_history_not_used_as_native_precision_or_record_dates': True}


def _cached_form(root, sequence, version):
    from .cvm_proventos_documents import parse_pdf
    row, = [r for r in index_rows(str(root)) if parse_qs(urlsplit(r['Link_Download']).query).get('numSequencia') == [sequence]
            and int(r['Versao']) == version]
    require(row in final_rows(str(root), int(row['Codigo_CVM'])), 'older_final_revision_fallback_forbidden')
    same_reference = [r for r in final_rows(str(root), int(row['Codigo_CVM']))
                      if r['Categoria'] == row['Categoria'] and r['Data_Referencia'] == row['Data_Referencia']]
    require(not any(int(r['Versao']) > version for r in same_reference), 'higher_final_revision_unavailable_no_fallback')
    paths = list((Path(root) / 'raw/primary-research/cvm-documents').glob(f'{sequence}-v{version}-*.pdf'))
    require(len(paths) == 1, 'final_form_original_missing')
    path, = paths
    source = {'file': path.relative_to(root).as_posix(), 'sha256': path.stem.split('-')[-1],
              'url': row['Link_Download'], 'retrieved_at': None, 'published_at': row['Data_Entrega'][:10]}
    document = {'cvm': int(row['Codigo_CVM']), 'issuer_name': row['Nome_Companhia'], 'url': row['Link_Download'],
                'sequence': sequence, 'version': version, 'published_at': row['Data_Entrega'][:10]}
    parsed = parse_pdf(read(root, source), document, CUTOFF)
    return parsed, source, row['index_source']


def jsl_calendar(root, identity):
    source = next(s for s in receipts(str(root)) if s['url'] == 'https://ri.jsl.com.br/governanca-corporativa/dividendos/')
    tree = html.fromstring(read(root, source).decode('utf-8'))
    table, = [t for t in tree.xpath('//table') if 'composicao acionaria' in norm(t.text_content())]
    rows = expanded_rows(table)
    require(len(rows) == 11 and 'valor bruto' in norm(' '.join(rows[0]['cells'])), 'jsl_native_table_changed')
    events = []
    def day(text):
        return datetime.strptime(text, '%d/%m/%Y').date().isoformat()
    for row in rows[1:]:
        c = row['cells']
        label = 'JCP' if c[0] == 'JCP' else 'DIVIDENDO'
        value = native_decimal(c[6])
        require(value is not None, 'jsl_native_amount_unreadable')
        events.append(_event(identity, label, day(c[3]), day(c[4]), value, source,
            approval=day(c[2]), locator=row['locator'], literals=c))
    changes, sources = [], [source]
    for seq, version, label in [('990999', 4, 'JCP'), ('1097540', 4, 'DIVIDENDO')]:
        parsed, form_source, index_source = _cached_form(root, seq, version)
        native, = [e for e in parsed['events'] if e['isin'] == identity['isin']]
        require(not native['issues'], 'jsl_final_form_unresolved')
        original, = [e for e in events if e['record_date'] == native['record_date'] and e['label'] == label]
        changes.append({'original': dict(original), 'native_final': native, 'source': form_source})
        original.update(gross_per_share_decimal=native['gross_per_unit_decimal'],
                        payment_date=native['payment_date'], published_at=native['published_at'],
                        source=form_source, source_locator='native CVM gross/ISIN table row 2')
        sources.extend([form_source, index_source])
    # The final 2022 correction supersedes the amount still on the RI table.
    correction = source_by_hash(root, '46199c2f550eaef4caf71f54611afab7416ccbd7eacc181ff9a81b81dbcb34d8')
    require('0,216129949' in norm(' '.join(pdf_pages(read(root, correction)))), 'jsl_final_2022_amount_missing')
    event, = [e for e in events if e['record_date'] == '2022-05-05']
    changes.append({'original': dict(event), 'source': public_source(correction), 'final_amount': '0.216129949'})
    event.update(gross_per_share_decimal='0.216129949', source=public_source(correction),
                 published_at='2022-05-13', source_locator='native final notice sole adjustment paragraph')
    sources.append(public_source(correction))
    final_notice = source_by_hash(root, '5bad1af312041b6d2b664e12d3b588645ce0603612083e8f5ee099c12d7efb1e')
    text = norm(' '.join(pdf_pages(read(root, final_notice))))
    require('0,740150001' in text and '02 de outubro de 2026' in text and '28 de dezembro de 2026' in text,
            'jsl_final_future_installment_notice_changed')
    aggregate, = [e for e in events if e['record_date'] == '2025-12-29' and e['label'] == 'DIVIDENDO']
    events.remove(aggregate)
    for day in ('2026-10-02', '2026-12-28'):
        events.append(_event(identity, 'DIVIDENDO', '2025-12-29', day, '0.740150001', public_source(final_notice),
            published='2026-09-28', locator='sole revised two-installment paragraph', literals=text, role='payment'))
    sources.append(public_source(final_notice))
    return {'events': events, 'sources': sources, 'native_final_updates': changes, 'blockers': [],
            'unknown_table_publication_retained_for_other_rows': True}


def source_by_hash(root, digest):
    matches = [s for s in receipts(str(root)) if s['sha256'] == digest]
    require(matches, 'reviewed_native_receipt_missing')
    source = matches[0]
    read(root, source)
    return source


def allied_calendar(root, identity):
    # These are reviewed native values, not values calculated from comparator
    # discrepancies. Every value is checked again in the preserved PDF text.
    reviews = [
        ('fcd43014899b58a28381347296e8f1b891dbdec8601b830d85b8891ee8792213', '2021-09-15', '2021-09-30', 'DIVIDENDO', '0.028555113'),
        ('fcd43014899b58a28381347296e8f1b891dbdec8601b830d85b8891ee8792213', '2021-09-15', '2021-09-30', 'DIVIDENDO', '0.097948098'),
        ('fcd43014899b58a28381347296e8f1b891dbdec8601b830d85b8891ee8792213', '2021-09-15', '2021-09-30', 'JCP', '0.337139337'),
        ('77e0af4a49bdda45e872be7834fd720c781a7f6945a3f96ecceb2ebfb2bb3d3d', '2021-12-16', '2021-12-30', 'JCP', '0.297310057'),
        ('498f2568fa45b77c83858ac47b769c6bddefffb93171203ded27975b1402a315', '2022-04-29', '2022-07-05', 'DIVIDENDO', '0.570983231'),
        ('1f64f92183b004baa2d27bb4270a8c4be5fc54bdf13b9d5eda8a7bd05f506ba2', '2022-12-27', '2023-06-16', 'JCP', '0.35399907716'),
        ('7542e06384283d18f5a0289ec9179119931e4adfef66d6ec0b980e6cc1d0182e', '2023-12-19', '2024-03-25', 'JCP', '0.965452029'),
        ('b55e937baed9a7e135fba389625fe0900ae83414bda937fdc303cc518b3dcc16', '2024-04-15', '2024-07-31', 'JCP', '1.06976308021'),
        ('58ac2f79951c1ccce9110260e2bf405ca3d1d240d1e7b889160965a1ae39eef2', '2024-12-06', '2025-04-15', 'JCP', '0.64852930776'),
        ('58ac2f79951c1ccce9110260e2bf405ca3d1d240d1e7b889160965a1ae39eef2', '2024-12-06', '2025-08-29', 'JCP', '0.64852930776'),
        ('10c83888d9ab5e7895574fecfef9a52db04e1f28628511ce671c7767ad70a61f', '2025-08-15', '2025-08-29', 'JCP', '0.73765058864'),
        ('ebf8194187ee8910fc50511233a18f63cbf63191d3666e97066fb2e4ebd5db15', '2026-01-30', '2026-03-31', 'JCP', '0.42082241071'),
    ]
    events, sources = [], []
    for digest, record, payment, label, amount in reviews:
        source = source_by_hash(root, digest)
        text = norm(' '.join(pdf_pages(read(root, source))))
        compact = re.sub(r'\s+', '', text)
        require('alliedtecnologia' in compact and amount.replace('.', ',') in compact,
                'allied_reviewed_value_not_in_native_notice')
        meta = source.get('discovery', {}).get('native_metadata', {})
        published = meta.get('file_published_date', '')[:10] or None
        require(published is not None and published <= str(CUTOFF), 'allied_publication_unavailable')
        event = _event(identity, label, record, payment, amount, public_source(source),
                       published=published, locator='reviewed Portuguese native notice paragraphs',
                       literals={'amount_decimal_literal': amount.replace('.', ','), 'native_notice_text': text})
        if record == '2024-12-06' and payment == '2025-08-29':
            changed = source_by_hash(root, '10c83888d9ab5e7895574fecfef9a52db04e1f28628511ce671c7767ad70a61f')
            event['supporting_sources'] = [public_source(changed)]
            event['published_at'] = changed['discovery']['native_metadata']['file_published_date'][:10]
            event['original_payment_date_before_update'] = '2025-10-15'
            sources.append(public_source(changed))
        events.append(event)
        sources.append(public_source(source))
    parsed, form, index = _cached_form(root, '989703', 2)
    native, = [e for e in parsed['events'] if e['isin'] == identity['isin']]
    require(native['gross_per_unit_decimal'] == events[-1]['gross_per_share_decimal']
            and native['record_date'] == events[-1]['record_date']
            and native['payment_date'] == events[-1]['payment_date'], 'allied_latest_form_disagrees')
    sources.extend([form, index])
    return {'events': events, 'sources': sources, 'blockers': [],
            'excluded_older_installment_form': '698853-v1 superseded by the 2024-03-11 native notice',
            'after_cutoff_record_declaration_not_included': '2026-10-05; later October forms not backdated',
            'original_table_duplicate_rows_and_obsolete_values_not_used': True}


def helbor_calendar(root, identity):
    reviews = [
        ('bbcbce30cb6b0d54146cf15061d68515751ccdf546622b714546d48bce0b73d3', '2021-04-30', '2021-05-31', '0.0474631991'),
        ('67c858f20266981cf8972ac4824e1bea0445036ad7ce0873a52d332b4de2ce92', '2022-04-29', '2022-05-31', '0.1807182983'),
        ('f8014a4e777db8a36161b5b2b053b22b00a1f84312067c8c3862bb941cc40322', None, '2023-05-31', '0.0913792941'),
        ('e772b66557c028c78d21878966a7472bf1c4ff3735f8a537a1707379b90da924', '2024-04-30', '2024-05-31', '0.09101291930'),
        ('ad7f95da8c0a21d88448dadd2fb56e8caf69005770d47aaeb1d0b74b9d2d0aaf', '2025-04-23', '2025-05-30', '0.10113954780'),
        ('a8c13c665a8e35addba1c3efdb35e0e84f7ec50df29ce27998f815b0ac1607a7', '2026-01-05', '2026-05-29', '0.01884039452'),
        ('a8c13c665a8e35addba1c3efdb35e0e84f7ec50df29ce27998f815b0ac1607a7', '2026-04-24', '2026-05-29', '0.00131386204'),
        ('93e05f1297e23189fed2dac9484abc5fba95562dccb5a5fa5936cf83e84c9b5a', '2026-01-05', None, '0.1130423671'),
    ]
    events, sources = [], []
    for digest, record, payment, amount in reviews:
        source = source_by_hash(root, digest)
        text = norm(' '.join(pdf_pages(read(root, source))))
        require(amount.replace('.', ',') in re.sub(r'\s+', '', text), 'helbor_native_amount_missing')
        meta = source['discovery']['native_metadata']
        events.append(_event(identity, 'DIVIDENDO', record, payment, amount, public_source(source),
            published=meta['file_published_date'][:10], locator='Portuguese notice entitlement/payment paragraph',
            literals=text, role='payment' if payment else 'deadline_not_promoted_to_payment'))
        sources.append(public_source(source))
    final_shell = source_by_hash(root, '68568773bfce65eb1cbe3f15e330f23272c0a330c168bf5f890e9fd8479bc225')
    sources.append(public_source(final_shell))
    return {'events': events, 'sources': sources, 'blockers': [
        'helbor_2023_final_v2_native_body_unavailable_record_date_unresolved',
        'helbor_2026_remaining_0_1130423671_payment_date_unresolved'],
        'source_conflicts': [
            {'field': '2025 payment_date', 'history_table': '2025-04-30', 'native_notice': '2025-05-30'},
            {'field': '2023 entitlement', 'notice_date': '2023-05-15', 'notice_ex_date': '2023-05-02',
             'record_date': None, 'older_form_v1_not_used': True}],
        'final_v2_shell_not_retried': True, 'unknown_remainder_not_zeroed': True}


def quote_audit(root, identity):
    from .broad_stocks import split_price_history
    from .collect_market_data import month_end, shift_month
    from .share_basis import factor_after
    ticker = identity['ticker']
    relative = f'raw/expanded/{ticker}-prices.json'
    if not (Path(root) / relative).is_file():
        relative = f'raw/inventory/daily-prices/{ticker}.json'
    body = (Path(root) / relative).read_bytes()
    model, actions = split_price_history(body, ticker, CUTOFF, allow_recent_actions=True)
    matches = [s for s in receipts(str(root)) if '/history?' in s['url']
               and parse_qs(urlsplit(s['url']).query).get('tickers') == [ticker]
               and parse_qs(urlsplit(s['url']).query).get('adjusted') == ['false']]
    if not matches:
        return {'blockers': ['exact_FESA3_widget_ticker_not_published' if ticker == 'FESA3' else
                             'native_widget_script_on_denied_CDN_not_available_in_cache'],
                'monthly_comparisons': [], 'actions': actions, 'raw_quotes': {}, 'sources': [],
                'all61_compared': False, 'monthly_inputs_agree61': False}
    source = matches[0]
    native = json.loads(read(root, source))
    require(native.get('success') is True and native['data']['source'] == 'Enfoque', 'native_quote_provider_disagrees')
    series_rows = [s for s in native['data']['tickers'] if s['ticker'] == ticker]
    require(series_rows and all(s == series_rows[0] for s in series_rows), 'conflicting_duplicate_native_quote_series')
    series = series_rows[0]
    values, seen = {}, set()
    for row in series['history']:
        day, close = date.fromisoformat(row['date']), row['closed']
        require(day not in seen and type(close) in (int, float) and math.isfinite(close) and close > 0,
                'invalid_native_quote_date_or_amount')
        seen.add(day)
        month = month_end(day.year, day.month)
        if date(2021, 9, 1) <= day <= CUTOFF and (month not in values or day > values[month][0]):
            values[month] = day, close
    required = sorted(shift_month(CUTOFF, -i) for i in range(61))
    require(set(required).issubset(values), 'native_quote_61_months_missing')
    page_script = None
    for page in receipts(str(root)):
        if not re.search('cotac|grafico', page['url']):
            continue
        page_body = read(root, page)
        tree = html.fromstring(page_body.decode('utf-8'))
        urls = {urljoin(page['url'], s.get('src')) for s in tree.xpath('//script[@src]')}
        for script in receipts(str(root)):
            if script['url'] not in urls or 'stockinfo-viewer' not in script['url']:
                continue
            try:
                url, proof = widget_spec(page_body, page, read(root, script), script, identity)
            except ValueError:
                continue
            if url == source['url']:
                page_script = [public_source(page), public_source(script)]
                break
        if page_script:
            break
    require(page_script, 'native_quote_exact_issuer_config_or_script_unbound')
    checks = []
    for month in required:
        model_day, model_value = model[month][:2]
        day, close = values[month]
        adjusted = close / factor_after(day, actions, CUTOFF)
        checks.append({'month': month.isoformat(), 'model_date': model_day.isoformat(),
            'native_date': day.isoformat(), 'model_close': model_value, 'native_raw_close': close,
            'date_matches': model_day == day,
            'raw_price_matches': math.isclose(model_value, close, rel_tol=.006, abs_tol=.015),
            'split_adjusted_price_matches': math.isclose(model_value, adjusted, rel_tol=.006, abs_tol=.015)})
    agreement = (all(r['date_matches'] for r in checks) and
                 (all(r['raw_price_matches'] for r in checks) or all(r['split_adjusted_price_matches'] for r in checks)))
    comparison_source = {'file': relative, 'sha256': hashlib.sha256(body).hexdigest(),
                         'bytes': len(body), 'url': None, 'retrieved_at': None}
    return {'blockers': [], 'monthly_comparisons': checks, 'all61_compared': len(checks) == 61,
            'monthly_inputs_agree61': agreement, 'actions': actions,
            'byte_equal_repeated_native_series_count': len(series_rows),
            'raw_quotes': {values[m][0].isoformat(): values[m][1] for m in required},
            'sources': [public_source(source), *page_script, comparison_source],
            'selection_rule': 'independent_last_actual_trade_in_all_61_calendar_months',
            'price_values_changed': False, 'factors_fitted': False, 'tolerances': {'relative': .006, 'absolute': .015}}


def inspect_evidence(root, identity, cutoff=CUTOFF):
    if identity.get('ticker') not in TARGETS or cutoff != CUTOFF:
        return None
    require(tuple(identity.get(k) for k in ('cvm', 'share_class', 'isin')) == TARGETS[identity['ticker']],
            'exact_CVM_class_ISIN_mismatch')
    from .dividend_scope import allowed_tickers
    require(identity['ticker'] in (allowed_tickers(root) or ()), 'outside_selected_DY_scope')
    if BUNDLE_SHA:
        bundle_source = {'file': CACHE + '/cash-evidence-' + BUNDLE_SHA + '.json', 'sha256': BUNDLE_SHA}
        bundle = json.loads(read(root, bundle_source))
        require(bundle['cutoff'] == str(cutoff), 'frozen_evidence_cutoff_mismatch')
        data = deepcopy(bundle['targets'][identity['ticker']])
        for source in data['sources']:
            read(root, source)
        return data
    proof, sources = exact_identity(Path(root), identity)
    ticker = identity['ticker']
    calendar = (ferbasa_calendar(root, identity) if ticker.startswith('FESA') else
                jsl_calendar(root, identity) if ticker == 'JSLG3' else
                allied_calendar(root, identity) if ticker == 'ALLD3' else helbor_calendar(root, identity))
    quotes = quote_audit(root, identity)
    sources.extend(calendar['sources'] + quotes['sources'])
    expected = defaultdict(Decimal)
    for event in calendar['events']:
        if event['record_date']:
            expected[event['record_date']] += Decimal(event['gross_per_share_decimal'])
    return {'provider': 'round2_native_issuer_cash_and_Enfoque',
            'identity': {k: identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')},
            'expected_cash': {day: str(value) for day, value in expected.items()},
            'raw_quotes': quotes['raw_quotes'], 'calendar': calendar, 'quote_audit': quotes,
            'sources': list({(s['file'], s['sha256']): public_source(s) for s in sources}.values()),
            'blockers': calendar['blockers'] + quotes['blockers'],
            'checks': {'FCA_exact_class_history': proof, 'independent_monthly_prices': len(quotes['raw_quotes']),
                       'calendar_events': len(calendar['events']), 'monthly_inputs_agree61': quotes['monthly_inputs_agree61'],
                       'settlement_verified': False, 'point_in_time_archive_verified': False,
                       'synthetic_exchange_responses': False}}


def freeze_evidence(root):
    from .market_inventory import load_inventory
    require(BUNDLE_SHA is None, 'evidence_already_frozen')
    inventory = load_inventory(root)
    evidence = {}
    for ticker in TARGETS:
        identity = next(r for r in inventory['stocks'] if r['ticker'] == ticker)
        evidence[ticker] = inspect_evidence(root, identity)
    bundle = {'cutoff': str(CUTOFF), 'targets': evidence,
              'policy': 'native_literal_cash_values_all61_months_no_fitted_factors_no_zero_substitution',
              'shared_artifacts_changed': False}
    body = json.dumps(bundle, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = Path(root) / CACHE / ('cash-evidence-' + digest + '.json')
    _save(path, body)
    return {'file': path.relative_to(root).as_posix(), 'sha256': digest, 'bytes': len(body),
            'events': {t: len(d['calendar']['events']) for t, d in evidence.items()},
            'blockers': {t: d['blockers'] for t, d in evidence.items()}}


def load_evidence(root, identity, cutoff):
    data = inspect_evidence(root, identity, cutoff)
    if data is None:
        return None
    require(not data['blockers'], data['blockers'][0] if data['blockers'] else 'calendar_unresolved')
    return {**data, 'expected_cash': {date.fromisoformat(day): float(Decimal(value)) for day, value in data['expected_cash'].items()},
            'raw_quotes': {date.fromisoformat(day): value for day, value in data['raw_quotes'].items()}}


ALLD_PAYMENT_SHA = '0df2b64d26302dac0dca418ff7c453a34b2986550c229fd3d91cc095072f0399'
REPAIR_ID = 'round2_allied_accelerated_2023_JCP'


def apply_exact_repairs(parsed, identity, cutoff, cache):
    """Hook immediately after inspect_payment_page, before primary enrichment."""
    if identity.get('ticker') != 'ALLD3':
        return parsed
    from .cash_repair_proofs import apply_groups
    require(cutoff == CUTOFF, 'repair_cutoff_mismatch')
    require(tuple(identity.get(k) for k in ('cvm', 'share_class', 'isin')) == TARGETS['ALLD3'],
            'repair_exact_identity_mismatch')
    source = source_by_hash(cache.folder, '7542e06384283d18f5a0289ec9179119931e4adfef66d6ec0b980e6cc1d0182e')
    text = norm(' '.join(pdf_pages(read(cache.folder, source))))
    require('0,965452029' in text and 'parcela unica' in text and '25 de marco de 2024' in text
            and '19 de dezembro de 2023' in text, 'allied_final_acceleration_notice_changed')
    binding = notice_binding(cache.folder, 25330, '2024-03-11')
    require(binding['native_index_row']['Versao'] == '2', 'allied_final_notice_version_changed')
    sources = [public_source(source), public_source(binding['index_source'])]
    for item in sources:
        cache.register_research_evidence(item)
    fact = _event(identity, 'JCP', '2023-12-19', '2024-03-25', '0.965452029', public_source(source),
                  published='2024-03-11', locator='Portuguese notice first and second paragraphs', literals=text)
    fact['source_locator_id'] = 'round2-cash:ALLD3:730957-v2:2023-12-19:2024-03-25'
    proof = {'sources': sources, 'contract': {'final_notice_binding': binding,
        'single_native_full_payment_not_equal_installment_inference': True,
        'payment_and_record_dates_explicit_in_notice': True}}
    return apply_groups(parsed, identity, cutoff, proof, repair_id=REPAIR_ID, payment_sha=ALLD_PAYMENT_SHA,
        groups={'2023-12-19': {'expected_original': [{'label': 'JCP', 'payment_date': '2024-03-25',
                 'gross_per_share': 0.482726, 'source_row': 'table:2:row:9'}], 'facts': [fact]}})


def validate_primary_actions(root, identity, cutoff, observed_actions):
    data = inspect_evidence(root, identity, cutoff)
    require(data is not None, 'unsupported_action_identity')
    proof = data['quote_audit']
    require(observed_actions == proof['actions'], 'observed_action_list_disagrees')
    require(not observed_actions, 'primary_share_action_document_required')
    require(proof['all61_compared'] and proof['monthly_inputs_agree61'], 'all_61_monthly_inputs_disagree')
    return {'actions': observed_actions, 'sources': data['sources'],
            'actual_empty_comparison_action_list_and_61_monthly_prices_verified': True,
            'legal_absence_of_all_corporate_actions_not_certified': True, 'factors_fitted': False}


def cached_documents(root, ticker):
    root = Path(root)
    result = []
    for row in final_rows(str(root), TARGETS[ticker][0]):
        seq = parse_qs(urlsplit(row['Link_Download']).query).get('numSequencia', [''])[0]
        paths = list((root / 'raw/primary-research/cvm-documents').glob(f'{seq}-v{row["Versao"]}-*.pdf'))
        for path in paths:
            digest = path.stem.split('-')[-1]
            source = {'file': path.relative_to(root).as_posix(), 'sha256': digest,
                      'url': row['Link_Download'], 'published_at': row['Data_Entrega'][:10],
                      'retrieved_at': None, 'bytes': path.stat().st_size}
            result.append({'index': row, 'source': source, 'pages': pdf_pages(read(root, source))})
    return result


def acquire_pages(root):
    collector = PublicSourceCollector(root, 'round2-cash')
    pages = [
        ('https://ri.helbor.com.br/informacoes-financeiras/avisos-comunicados-e-fatos-relevantes/',
         'https://ri.helbor.com.br/informacoes-financeiras/dividendos/'),
        ('https://www.ferbasa.com.br/divulgacoes-e-resultados/comunicados-e-fatos-relevantes/',
         'https://www.ferbasa.com.br/informacoes-aos-investidores/historico-de-pagamentos-de-proventos/'),
    ]
    result = []
    for url, parent in pages:
        try:
            source = collector.fetch(url, discovery={'kind': 'issuer_published_link', 'source_url': parent})
            result.append({k: source[k] for k in ('url', 'status', 'error', 'file', 'sha256', 'bytes')})
            print(json.dumps(result[-1]), flush=True)
        except ValueError as error:
            result.append({'url': url, 'error': str(error)})
            print(json.dumps(result[-1]), flush=True)
    return result


def widget_spec(page_body, page, script_body, script, identity):
    tree = html.fromstring(page_body.decode('utf-8'))
    urls = [urljoin(page['url'], n.get('src')) for n in tree.xpath('//script[@src]')]
    require(script['url'] in urls, 'widget_script_not_published_by_issuer')
    text = page_body.decode('utf-8')
    ids = set(re.findall(r'(?:stockInfoId\s*=|stockinfoId\s*:)\s*[\x22\x27]([a-f0-9-]{36})', text))
    ticker_lists = re.findall(r'tickers\s*:\s*[\x22\x27]([^\x22\x27]+)', text)
    require(len(ids) == 1 and identity['ticker'] in {t.strip() for ts in ticker_lists for t in ts.split(',')},
            'exact_ticker_not_published_in_widget')
    code = script_body.decode('utf-8')
    spans = ['https://api.mziq.com/mzstockinfo', '/history?from=', '&to=', '&tickers=', '&adjusted=', 'YYYY-MM-DD']
    require(all(s in code for s in spans), 'published_widget_template_not_demonstrated')
    url = (f'https://api.mziq.com/mzstockinfo/{next(iter(ids))}/history'
           f'?from=2021-09-01&to=2026-09-30&tickers={identity["ticker"]}&adjusted=false')
    return url, {'kind': 'exact_issuer_published_widget_template',
                 'page_source': public_source(page), 'script_source': public_source(script),
                 'stockinfo_id': next(iter(ids)), 'ticker': identity['ticker'], 'adjusted': False,
                 'template_literal_spans': spans}


def public_source(source):
    result = {k: source.get(k) for k in ('file', 'sha256', 'url', 'retrieved_at', 'published_at')}
    if source.get('bytes') is not None:
        result['bytes'] = source['bytes']
    return result


def acquire_widget(root, ticker):
    matches = []
    sources = receipts(str(root))
    cvm, share, isin = TARGETS[ticker]
    identity = dict(ticker=ticker, cvm=cvm, share_class=share, isin=isin)
    for source in sources:
        if not re.search('cotac|grafico', source['url']):
            continue
        body = read(root, source)
        tree = html.fromstring(body.decode('utf-8'))
        text = body.decode('utf-8')
        if ticker not in re.findall(r'tickers\s*:\s*[\x22\x27]([^\x22\x27]+)', text):
            continue
        script_urls = [urljoin(source['url'], n.get('src')) for n in tree.xpath('//script[@src]')
                       if 'cdn.mziq.com/stockinfo-viewer/' in n.get('src')]
        for script_url in script_urls:
            script = next((s for s in sources if s['url'] == script_url), None)
            if script is None:
                script = PublicSourceCollector(root, 'round2-cash').fetch(script_url,
                    discovery={'kind': 'issuer_published_script', 'page_source': public_source(source)})
            require(script['status'] == 200 and not script['error'], 'published_script_unavailable')
            try:
                url, discovery = widget_spec(body, source, read(root, script), script, identity)
            except (ValueError, KeyError):
                continue
            matches.append((url, discovery))
    require(matches, 'published_quote_config_missing')
    url, discovery = matches[0]
    # A stopped or already acquired full-window URL is never requested again.
    existing = [s for s in sources if s['url'] == url]
    if existing:
        source = existing[0]
    else:
        attempted = [json.loads(p.read_bytes()).get('url') for p in
                     (Path(root) / 'raw/primary-research/all-pending').glob('*/request-*.json')]
        require(url not in attempted, 'previous_full_window_attempt_not_retried')
        source = PublicSourceCollector(root, 'round2-cash').fetch(url, discovery=discovery)
    return {k: source[k] for k in ('url', 'status', 'error', 'file', 'sha256', 'bytes')}


class CashCollector(PublicSourceCollector):
    """The same acquisition policy for issuer-published read-only POST filters."""

    def catalogue(self, url, payload, discovery):
        from protego import Protego
        require(approved(url) and urlsplit(url).hostname in ('api.mziq.com', 'apicatalog.mziq.com'),
                'unapproved_catalogue')
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode()
        key = hashlib.sha256(b'POST\n' + url.encode() + b'\n' + body).hexdigest()
        receipt_path = self.base / ('response-' + key + '.json')
        if receipt_path.exists():
            source = json.loads(receipt_path.read_bytes())
            self.body(source)
            return source
        require(not (self.base / ('request-' + key + '.json')).exists(), 'interrupted_catalogue_no_retry')
        host = urlsplit(url).hostname
        require(host not in self.prior_stops, 'previously_stopped_origin_no_retry')
        robots = self.fetch('https://' + host + '/robots.txt', {'kind': 'robots_policy'})
        require(not robots['error'] and robots['status'] in (200, 404), 'catalogue_robots_unavailable')
        policy = Protego.parse(self.body(robots).decode('utf-8', errors='replace'))
        require(robots['status'] == 404 or policy.can_fetch(url, USER_AGENT), 'catalogue_robots_disallow')
        delay = max(1, policy.crawl_delay(USER_AGENT) or 0)
        rate = policy.request_rate(USER_AGENT)
        if rate:
            delay = max(delay, rate.seconds / rate.requests)
        require(delay <= 60, 'catalogue_requires_later_run')
        require(len(list(self.base.glob('request-*.json'))) < 200, 'request_budget_exhausted')
        time.sleep(delay)
        request = dict(url=url, method='POST', request_json=payload, scope='round2-cash',
                       discovery=discovery, started_at=datetime.now(timezone.utc).isoformat(), retries=0)
        _save(self.base / ('request-' + key + '.json'), request)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        content, status, error, response = b'', None, None, None
        try:
            try:
                response = urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(
                    url, data=body, method='POST', headers={'User-Agent': USER_AGENT,
                    'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}), timeout=25)
            except urllib.error.HTTPError as failure:
                response = failure
            status = response.status
            content = response.read(MAX_BYTES + 1)
            if status in (401, 403, 429):
                error = 'access_stop_no_retry'
            elif status != 200:
                error = 'non_success_no_retry'
            elif len(content) > MAX_BYTES:
                error = 'response_size_limit'
            elif re.search(rb'(?is)<title[^>]*>[^<]*(?:captcha|access denied|sign in|just a moment)', content):
                error = 'public_content_challenge_stop'
        except Exception as failure:
            error = type(failure).__name__
        finally:
            if response is not None:
                response.close()
        digest = hashlib.sha256(content).hexdigest()
        file = self.base / ('body-' + digest + '.bin')
        _save(file, content)
        source = {**request, 'status': status, 'error': error, 'bytes': len(content),
                  'file': file.relative_to(self.root).as_posix(), 'sha256': digest,
                  'retrieved_at': datetime.now(timezone.utc).isoformat(), 'published_at': None}
        _save(receipt_path, source)
        if _stopped(source):
            self.prior_stops.add(host)
        return source


def acquire_catalogue(root, ticker, year, collector=None):
    root = Path(root)
    domain = {25330: 'alliedbrasil', 3069: 'ferbasa', 22020: 'jsl.com', 20877: 'helbor'}[TARGETS[ticker][0]]
    pages = [s for s in receipts(str(root)) if domain in s['url']
             and re.search('avisos|comunicados|fatos-relevantes|documentos|publicacoes-cvm', s['url'])]
    require(pages, 'issuer_catalogue_page_missing')
    source = pages[0]
    text = read(root, source).decode('utf-8')
    fm_id = re.search(r'fmId\s*=\s*[\x22\x27]([^\x22\x27]+)', text)
    fm_base = re.search(r'fmBase\s*=\s*[\x22\x27]([^\x22\x27]+)', text)
    categories = re.findall(r'internal_name:\s*[\x22\x27]([^\x22\x27]+)', text)
    array = re.search(r'var categories\s*=\s*(\[\{.*?\}\]);', text, re.S)
    if array:
        categories = [r['internal_name'] for r in json.loads(array[1])]
    language = re.search(r'language:\s*[\x22\x27]([^\x22\x27]+)', text)
    require(fm_id and fm_base and categories and language, 'literal_catalogue_configuration_missing')
    script = root / 'raw/primary-research/banks/bradesco-filemanager-script-d8d86b0e03f682b8797f.js'
    digest = 'd8d86b0e03f682b8797f951642a27c09258ba9e323c84057831f2fd9d443c6cb'
    script_text = read(root, {'file': script.relative_to(root).as_posix(), 'sha256': digest}).decode('utf-8')
    spans = ['return c+"/company/"+e+"/filter/categories/year/meta"',
             'n={year:a,categories:w(l(t.config.categories)),language:t.config.language,published:!0}',
             '$.ajax({type:"POST",url:e,data:JSON.stringify(n)']
    require(all(span in script_text for span in spans), 'catalogue_transport_template_changed')
    url = fm_base[1] + '/company/' + fm_id[1] + '/filter/categories/year/meta'
    payload = {'year': str(year), 'categories': categories, 'language': language[1], 'published': True}
    collector = collector or CashCollector(root, 'round2-cash')
    result = collector.catalogue(url, payload, {'kind': 'issuer_published_read_only_catalogue_filter',
        'source_url': source['url'], 'source_file': source['file'], 'source_sha256': source['sha256'],
        'cached_transport_file': script.relative_to(root).as_posix(), 'cached_transport_sha256': digest,
        'cached_transport_spans': spans, 'issuer_fmId': fm_id[1]})
    return {k: result[k] for k in ('url', 'status', 'error', 'file', 'sha256', 'bytes')}


def recover_documents(root, ticker):
    root = Path(root)
    collector = CashCollector(root, 'round2-cash')
    domain = {25330: 'alliedbrasil', 3069: 'ferbasa', 22020: 'jsl.com', 20877: 'helbor'}[TARGETS[ticker][0]]
    sources = receipts(str(root))
    pages = [s for s in sources if domain in s['url']
             and re.search('avisos|comunicados|fatos-relevantes|documentos|publicacoes-cvm', s['url'])]
    if not pages:
        parent = next(s for s in sources if domain in s['url'] and re.search('dividend|provento', s['url']))
        tree = html.fromstring(read(root, parent).decode('utf-8'))
        links = [(urljoin(parent['url'], a.get('href')), norm(a.text_content())) for a in tree.xpath('//a[@href]')]
        url = next(u for u, label in links if domain in u and re.search('aviso|comunicado|documentos|publicacoes cvm', label))
        page = collector.fetch(url, discovery={'kind': 'issuer_published_link', 'source_url': parent['url'],
                                               'source_file': parent['file'], 'source_sha256': parent['sha256']})
        require(page['status'] == 200 and not page['error'], 'catalogue_page_unavailable')
        receipts.cache_clear()
    output = []
    prior = {s['url']: s for s in receipts(str(root))}
    seen = set()
    for year in range(2021, 2027):
        try:
            source = acquire_catalogue(root, ticker, year, collector)
            print(json.dumps({'stage': 'catalogue', 'ticker': ticker, 'year': year,
                              'status': source['status'], 'error': source['error']}), flush=True)
            if source['status'] != 200 or source['error']:
                output.append(source)
                continue
            payload = json.loads(read(root, source))
            metas = payload.get('data', {}).get('document_metas', [])
            for index, row in enumerate(metas):
                title = norm(row.get('file_title', ''))
                if (not re.search('dividend|juros|provento|pagamento|jcp|jscp', title)
                        or re.search('debentur|debenture|nota comercial|cra |cri ', title)
                        or row.get('file_published_date', '')[:10] > CUTOFF.isoformat()
                        or row.get('is_published') is not True):
                    continue
                url = row.get('file_url')
                if url in seen or not approved(url):
                    continue
                seen.add(url)
                discovery = {'kind': 'literal_issuer_catalogue_document', 'catalogue_source': source,
                             'source_locator': f'$.data.document_metas[{index}]', 'native_metadata': row}
                try:
                    result = prior.get(url) or collector.fetch(url, discovery=discovery)
                    item = {'ticker': ticker, 'year': year, 'native_metadata': row,
                            'source': {k: result.get(k) for k in ('url', 'status', 'error', 'file', 'sha256', 'bytes', 'retrieved_at')},
                            'catalogue_source': source, 'catalogue_locator': discovery['source_locator']}
                    if result['status'] == 200 and not result['error']:
                        item['pages'] = pdf_pages(read(root, result))
                    output.append(item)
                    print(json.dumps({'stage': 'native_document', 'ticker': ticker,
                        'year': year, 'title': row['file_title'], 'status': result['status'],
                        'sha256': result['sha256']}), flush=True)
                except ValueError as error:
                    output.append({'url': url, 'error': str(error), 'native_metadata': row})
        except ValueError as error:
            output.append({'year': year, 'ticker': ticker, 'error': str(error)})
            print(json.dumps(output[-1]), flush=True)
    return output


def replay(root, *, adapter=False):
    """Run ordinary source and financial functions, with sockets disabled."""
    from unittest.mock import patch
    from . import inventory_candidates as candidates
    from . import issuer_independent_evidence as issuer
    from .collect_market_data import financial_features
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(root).resolve()
    inventory = load_inventory(root)
    stocks = [next(r for r in inventory['stocks'] if r['ticker'] == ticker) for ticker in TARGETS]
    protected = [root / 'current.json', root / 'inventory/current.json']
    before = {str(p): p.read_bytes() for p in protected}
    def offline(*args, **kwargs):
        raise RuntimeError('round2_cash_offline_network_forbidden')
    reports = []
    ordinary_inspector = candidates.inspect_payment_page
    with patch.object(socket.socket, 'connect', offline), patch.object(socket.socket, 'connect_ex', offline):
        statements, diagnostics, sources = prepare_financials(root, stocks, CUTOFF)
        for identity in stocks:
            ticker = identity['ticker']
            cache = candidates._Cache(root, inventory)
            checks, candidate = {}, None
            result = {'ticker': ticker, 'identity': {k: identity.get(k) for k in ('cvm', 'share_class', 'isin')},
                      'source_gate_pass': False, 'current_financial_pass': False}
            try:
                if adapter:
                    result['native_evidence'] = inspect_evidence(root, identity, CUTOFF)
                    def inspected(body, ticker, cutoff, observed):
                        parsed = ordinary_inspector(body, ticker, cutoff, observed)
                        return apply_exact_repairs(parsed, identity, cutoff, cache)
                    with patch.object(issuer, 'load_evidence', load_evidence), \
                            patch.object(issuer, 'validate_primary_actions', validate_primary_actions), \
                            patch.object(candidates, 'inspect_payment_page', inspected):
                        candidate = candidates._one(identity, cache, CUTOFF, checks)
                else:
                    candidate = candidates._one(identity, cache, CUTOFF, checks)
                current = financial_features(statements, candidate['payments'], CUTOFF,
                    (identity['cvm'], identity['share_class'], identity['sector_code']))
                result.update(source_gate_pass=True, source_result='candidate',
                    current_financial=current, ledger_rows=len(candidate['ledger']),
                    current_financial_pass=current is not None and current[1]['fiscal_end'] >= candidate['financial_basis_start'])
            except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                result['source_result'] = str(error)
            result['checks'] = checks
            data = result.get('native_evidence')
            if data and not data['calendar']['blockers']:
                from .share_basis import reconcile_payments
                try:
                    with patch.object(candidates, 'inspect_payment_page', inspected):
                        original, _ = candidates._payments(ticker, cache, CUTOFF, identity=identity)
                    expected = {date.fromisoformat(d): float(Decimal(v)) for d, v in data['expected_cash'].items()}
                    _, ledger, _ = reconcile_payments(ticker, original, expected, data['quote_audit']['actions'],
                        CUTOFF, evidence_provider=data['provider'])
                    result['cash_only_probe'] = {'ordinary_cash_gate_pass': True, 'ledger_rows': len(ledger),
                        'share_actions_not_independently_approved_by_this_probe': True,
                        'not_a_source_candidate_or_financial_admission': True}
                except (ValueError, KeyError, OSError, TypeError) as error:
                    result['cash_only_probe'] = {'ordinary_cash_gate_pass': False, 'reason': str(error)}
            result['financial_diagnostics'] = [d for d in diagnostics if d.get('cvm') == identity['cvm']]
            result['native_statements'] = [s for s in statements if s['cvm'] == identity['cvm']
                                           and s.get('share_class') == identity['share_class']]
            reports.append(result)
            print(json.dumps({k: result[k] for k in ('ticker', 'source_gate_pass', 'source_result', 'current_financial_pass')}), flush=True)
    require(all(Path(p).read_bytes() == body for p, body in before.items()), 'shared_pointer_changed')
    return {'targets': reports, 'financial_sources': sources, 'cutoff': str(CUTOFF),
            'adapter_enabled_only_in_isolated_process': adapter, 'normal_gates_modified': False,
            'network_calls': 0, 'pointers_unchanged': True}


def scan(root, ticker):
    root = Path(root)
    cvm = TARGETS[ticker][0]
    domains = {25330: 'alliedbrasil', 3069: 'ferbasa', 22020: 'jsl.com', 20877: 'helbor'}
    calendars = []
    for source in receipts(str(root)):
        if domains[cvm] not in source['url'] or not re.search('dividend|provento', source['url']):
            continue
        body = read(root, source)
        tree = html.fromstring(body)
        calendars.append({'source': source, 'tables': [expanded_rows(t) for t in tree.xpath('//table')],
            'links': [{'url': urljoin(source['url'], a.get('href')), 'text': ' '.join(a.text_content().split())}
                      for a in tree.xpath('//a[@href]') if re.search('aviso|acionista|comunicado|provento|pdf|resultado', norm(a.text_content()) + a.get('href'))]})
    documents = []
    for row in final_rows(str(root), cvm):
        if not re.search('aviso aos acionistas|relatorio proventos', norm(row['Categoria'])):
            continue
        query = parse_qs(urlsplit(row['Link_Download']).query)
        seq = query.get('numSequencia', [''])[0]
        cached = list((root / 'raw/primary-research/cvm-documents').glob(f'{seq}-v{row["Versao"]}-*.pdf'))
        documents.append({k: row[k] for k in ('Data_Referencia', 'Data_Entrega', 'Categoria', 'Tipo', 'Especie', 'Assunto', 'Versao', 'Link_Download')} | {
            'sequence': seq, 'cached': [str(p.relative_to(root)).replace('\\', '/') for p in cached]})
    return {'ticker': ticker, 'calendars': calendars, 'final_documents': documents}


def save_report(path, value):
    path = Path(path)
    require(path.name.startswith('cash-'), 'report_outside_owned_prefix')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=True, indent=2, default=str, allow_nan=False).encode()
    if path.exists():
        require(path.read_bytes() == data, 'immutable_report_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(data)


def handoff(root, replay_file):
    root = Path(root).resolve()
    path = Path(replay_file).resolve()
    owned_reports = root.parents[3] / 'work/recovery_round2_20261025'
    require(path.parent == owned_reports and path.name.startswith('cash-normal-gates-'),
            'handoff_requires_owned_normal_gate_report')
    body = path.read_bytes()
    require(path.stem.endswith(hashlib.sha256(body).hexdigest()), 'normal_gate_report_hash_changed')
    report = json.loads(body)
    results = []
    for result in report['targets']:
        data = result['native_evidence']
        statements = result['native_statements']
        latest = max(statements, key=lambda r: (r['fiscal_end'], int(r['version'])), default=None)
        item = {'ticker': result['ticker'], 'identity': result['identity'],
                'native_cash_events': len(data['calendar']['events']),
                'ordinary_one_pass': result['source_gate_pass'],
                'ordinary_cash_only_probe': result.get('cash_only_probe'),
                'all61_quote_comparisons': data['quote_audit']['all61_compared'],
                'all61_quote_inputs_agree': data['quote_audit']['monthly_inputs_agree61'],
                'current_financial_features_pass': result['current_financial_pass'],
                'native_blockers': data['blockers'],
                'latest_financial': {k: latest.get(k) for k in
                    ('fiscal_end', 'version', 'published_at', 'profit_per_share', 'source_file')} if latest else None}
        if result['source_gate_pass'] and not result['current_financial_pass']:
            item['remaining_financial_blocker'] = 'latest_2025_DFP_reported_EPS_unavailable_no_older_statement_fallback'
        if result['ticker'].startswith('FESA'):
            item['subsequent_primary_action_requirement'] = {
                'date': '2024-01-24', 'observed_ratio': 4.0,
                'basis': 'unchanged_Yahoo_action_observation_not_a_primary_action_document',
                'primary_document_still_required_before_admission': True}
        results.append(item)
    base = root / CACHE
    requests = [json.loads(p.read_bytes()) for p in base.glob('request-*.json')]
    responses = [json.loads(p.read_bytes()) for p in base.glob('response-*.json')]
    require(all(approved(r['url']) for r in requests), 'denied_network_request_in_owned_ledger')
    require(report['pointers_unchanged'] and report['network_calls'] == 0, 'normal_gate_replay_not_offline')
    raw_files = [{'file': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size,
                  'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in sorted(base.iterdir()) if p.is_file()]
    return {'status': 'cash_recovery_completed_with_explicit_remaining_blockers',
            'cutoff': str(CUTOFF), 'normal_gate_report': str(path),
            'normal_gate_report_sha256': hashlib.sha256(body).hexdigest(), 'targets': results,
            'ordinary_source_pass_count': sum(r['ordinary_one_pass'] for r in results),
            'source_and_current_financial_pass_count': sum(r['ordinary_one_pass'] and r['current_financial_features_pass'] for r in results),
            'new_source_files': ['recommender/round2_cash.py', 'recommender/test_round2_cash.py'],
            'owned_raw_files': raw_files, 'raw_cache_scope': CACHE, 'frozen_evidence_sha256': BUNDLE_SHA,
            'normal_gate_tests': {'passed': 23, 'command': '.recommender-venv/Scripts/python.exe -m pytest recommender/test_round2_cash.py -q',
                                  'junit': str(owned_reports / 'cash-tests.xml')},
            'integration_contract': {
                'load_evidence': '(market_root, exact_identity, cutoff) -> typed native expected_cash/raw_quotes/sources/checks; blocks incomplete calendars',
                'validate_primary_actions': '(market_root, exact_identity, cutoff, observed_actions) -> proof; no inferred or fitted action factors',
                'apply_exact_repairs': '(parsed_payment_page, exact_identity, cutoff, cache) -> immutable repaired view; ALLD3 only',
                'repair_hook_location': 'after inspect_payment_page, before enrich_calendar in ordinary _payments',
                'provider_hook_location': 'issuer_independent_evidence.load_evidence / validate_primary_actions for the five exact targets',
                'current_production_integration': 'parent_required; only this replay process installs the three provider/inspection hooks',
                'all_arithmetic_and_financial_gates': 'ordinary inventory_candidates._one, share_basis, prepare_financials and current financial_features unchanged'},
            'important_native_findings': [
                'ALLD3 final notice 730957 v2 accelerates full JCP 0.965452029 to 2024-03-25; original comparator page has only 0.482726.',
                'FESA3/FESA4: 26 exact native components each; explicit gross/net, record/ex/payment roles and unequal 2025/2026 installments.',
                'JSLG3: final 2022 dividend 0.216129949; two future installments 0.740150001 on 2026-10-02 and 2026-12-28; aggregate form retained in audit.',
                'HBOR3: May 2026 cash separates January-record 0.01884039452 and April-record 0.00131386204; January remainder 0.1130423671 has no exact payment date.',
                'HBOR3 2023 final 627717 v2 yielded HTTP 200 HTML, not PDF; not retried, older 618119 v1 rejected.',
                'Latest FCA masters omit exact equity children for ALLD3 2021 and JSLG3 2026; gaps retained explicitly, no older master substituted. Ordinary cached exact identity gate remains unchanged.'],
            'network': {'requests_including_robots': len(requests), 'response_count': len(responses),
                        'denied_origin_requests': 0, 'automatic_redirects': False,
                        'retry_count': sum(r.get('retries', 0) for r in requests),
                        'access_stop_responses': sum(bool(_stopped(r)) for r in responses)},
            'no_snapshot_inventory_pointer_core_or_document_edits': True,
            'model_admission_or_publication_attempted': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['scan', 'fetch', 'pdf', 'pages', 'widget', 'documents', 'catalogue', 'recover', 'baseline', 'replay', 'ferbasa', 'freeze', 'handoff'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--ticker', choices=list(TARGETS), default='HBOR3')
    parser.add_argument('--url')
    parser.add_argument('--source-url')
    parser.add_argument('--file')
    parser.add_argument('--output')
    parser.add_argument('--year', type=int, default=2026)
    args = parser.parse_args()
    if args.command == 'scan':
        result = scan(args.root, args.ticker)
    elif args.command == 'pages':
        result = acquire_pages(args.root)
    elif args.command == 'widget':
        result = acquire_widget(args.root, args.ticker)
    elif args.command == 'documents':
        result = cached_documents(args.root, args.ticker)
    elif args.command == 'catalogue':
        result = acquire_catalogue(args.root, args.ticker, args.year)
    elif args.command == 'recover':
        result = recover_documents(args.root, args.ticker)
    elif args.command in ('baseline', 'replay'):
        result = replay(args.root, adapter=args.command == 'replay')
    elif args.command == 'ferbasa':
        result = ferbasa_parse_report(args.root)
    elif args.command == 'freeze':
        result = freeze_evidence(args.root)
    elif args.command == 'handoff':
        result = handoff(args.root, args.file)
    elif args.command == 'fetch':
        collector = PublicSourceCollector(args.root, 'round2-cash')
        source = collector.fetch(args.url, discovery={'kind': 'published_link', 'source_url': args.source_url})
        result = {k: source[k] for k in ('url', 'status', 'error', 'file', 'sha256', 'bytes')}
    else:
        result = pdf_pages((args.root / args.file).read_bytes())
    if args.output:
        save_report(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
