"""Bounded issuer cash-calendar research; no model writes or exchange requests.

Acquisition is opt-in and content addressed. Offline adapters retain missing
dates, tax bases and share-basis qualifications. A historical schedule is never
promoted into settlement, point-in-time availability or model approval.
"""

from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from functools import lru_cache
from copy import deepcopy
import math
from pathlib import Path
import re
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import zipfile

from lxml import html

from .payment_sources import _norm, _table_rows, inspect_payment_page


SOURCES = {
    'itausa': {'url': 'https://ri.itausa.com.br/informacoes-financeiras/remuneracao-aos-acionistas/',
               'tickers': ('ITSA3', 'ITSA4')},
    'ferbasa': {'url': 'https://www.ferbasa.com.br/informacoes-aos-investidores/historico-de-pagamentos-de-proventos/',
                'tickers': ('FESA3', 'FESA4')},
    'randon': {'url': 'https://ri.randoncorp.com/governanca-corporativa/dividendos-e-jcp/',
               'tickers': ('RAPT3', 'RAPT4')},
    'romi': {'url': 'https://www.romi.com/investidores/acoes-da-romi/proventos/',
             'tickers': ('ROMI3',)},
    'banestes': {'url': 'https://banestes.com.br/ri/ri_acoes_dividendos.html',
                 'tickers': ('BEES3', 'BEES4')},
    'santander': {'url': 'https://www.santander.com.br/ri/conheca-o-santander-brasil/historico-de-dividendos/tabela-de-jcp-e-dividendos/',
                  'tickers': ('SANB3', 'SANB4', 'SANB11')},
    'copel': {'url': 'https://ri.copel.com/dados-financeiros/dividendos/', 'tickers': ('CPLE3',)},
    'tupy': {'url': 'https://ri.tupy.com.br/informacoes-financeiras/historico-de-proventos/', 'tickers': ('TUPY3',)},
    'tegma': {'url': 'https://ri.tegma.com.br/dev-historico-de-dividendos/', 'tickers': ('TGMA3',)},
    'tim': {'url': 'https://ri.tim.com.br/nossas-acoes-e-dividas/remuneracao-aos-acionistas/', 'tickers': ('TIMS3',)},
    'cury': {'url': 'https://ri.cury.net/historico-de-proventos/', 'tickers': ('CURY3',)},
    'jhsf': {'url': 'https://ri.jhsf.com.br/informacoes-financeiras/dividendos-e-jcp/', 'tickers': ('JHSF3',)},
    'cemig': {'url': 'https://ri.cemig.com.br/informacoes-financeiras/dividendos', 'tickers': ('CMIG3', 'CMIG4')},
    'metisa': {'url': 'https://www.metisa.com.br/proventos/', 'tickers': ('MTSA3', 'MTSA4')},
}
MAX_BYTES = 16_000_000
CACHE = 'raw/primary-research/cash-calendars'
IDENTITIES = {'ITSA3': (7617, 'ON'), 'ITSA4': (7617, 'PN'), 'FESA3': (3069, 'ON'),
              'FESA4': (3069, 'PN'), 'RAPT3': (14109, 'ON'), 'RAPT4': (14109, 'PNA'),
              'ROMI3': (7510, 'ON'), 'BEES3': (1155, 'ON'), 'BEES4': (1155, 'PN'),
              'SANB3': (20532, 'ON'), 'SANB4': (20532, 'PN'), 'SANB11': (20532, 'UNIT'),
              'CPLE3': (14311, 'ON'), 'TUPY3': (6343, 'ON'), 'TGMA3': (20800, 'ON'),
              'TIMS3': (24929, 'ON'), 'CURY3': (25100, 'ON'), 'JHSF3': (20605, 'ON'),
              'CMIG3': (2453, 'ON'), 'CMIG4': (2453, 'PNA'),
              'MTSA3': (8753, 'ON'), 'MTSA4': (8753, 'PN')}


def _approved(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and not parts.username and not parts.password
            and parts.port in (None, 443) and not parts.fragment
            and parts.hostname in {urlsplit(item['url']).hostname for item in SOURCES.values()}
            and parts.hostname != 'b3.com.br' and not parts.hostname.endswith('.b3.com.br'))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if not _approved(newurl) or urlsplit(newurl).hostname != urlsplit(request.full_url).hostname:
            raise ValueError('unapproved_redirect_no_request_sent')
        return super().redirect_request(request, response, code, message, headers, newurl)


def _immutable(path, content):
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError('immutable_cache_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(content)


def acquire(folder, issuer, *, refresh=False):
    """Acquire one exact public RI URL; no retry, token, widget or paywall bypass."""
    source = SOURCES[issuer]
    url = source['url']
    if not _approved(url):
        raise ValueError('unapproved_issuer_source')
    root = Path(folder).resolve()
    target = root / CACHE
    target.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise ValueError('unsafe_cache_path')
    if not refresh:
        for path in sorted(target.glob(issuer + '-*-source.json'), reverse=True):
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if receipt.get('url') != url:
                continue
            content = read_receipt(root, receipt)
            return content, receipt
    request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0',
                                                  'Accept': 'text/html,application/xhtml+xml'})
    with urllib.request.build_opener(_Redirect()).open(request, timeout=30) as response:
        content = response.read(MAX_BYTES + 1)
        final_url, content_type = response.geturl(), response.headers.get('Content-Type', '')
    if len(content) > MAX_BYTES or not _approved(final_url):
        raise ValueError('invalid_or_oversized_response')
    digest = hashlib.sha256(content).hexdigest()
    relative = f'{CACHE}/{issuer}-{digest}.html'
    receipt = {'id': f'issuer-calendar:{issuer}:{digest}', 'issuer': issuer,
               'tickers': list(source['tickers']), 'url': url, 'final_url': final_url,
               'authority': 'primary_issuer_RI', 'sha256': digest, 'bytes': len(content),
               'retrieved_at': datetime.now(timezone.utc).isoformat(), 'file': relative,
               'content_type': content_type, 'settlement_verified': False,
               'point_in_time_archive_verified': False}
    _immutable(root / relative, content)
    _immutable(target / f'{issuer}-{digest}-source.json',
               json.dumps(receipt, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8'))
    return content, receipt


def read_receipt(folder, receipt):
    root = Path(folder).resolve()
    name = receipt.get('file', '')
    if not isinstance(name, str) or not re.fullmatch(CACHE + r'/[a-z0-9]+-[a-f0-9]{64}\.(?:html|pdf)', name):
        raise ValueError('invalid_calendar_cache_reference')
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_BYTES:
        raise ValueError('unsafe_or_oversized_calendar_cache')
    content = path.read_bytes()
    if (hashlib.sha256(content).hexdigest() != receipt.get('sha256')
            or path.stem.rsplit('-', 1)[-1] != receipt.get('sha256')
            or len(content) != receipt.get('bytes')):
        raise ValueError('calendar_source_hash_mismatch')
    return content


def _tree(content):
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise ValueError('bounded_HTML_required')
    try:
        content = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        pass
    return html.fromstring(content, parser=html.HTMLParser(no_network=True))


def inspect_tables(content):
    """Research-only structured rows, retaining rowspan/colspan source layout."""
    return [{'table': index, 'rows': list(_table_rows(table))}
            for index, table in enumerate(_tree(content).xpath('//table'))]


def _date(text):
    # A footnote marker is not a second date. A deadline is not a payment date.
    text = re.sub(r'\s*\(\d+\)\s*$', '', str(text or '').strip())
    if not re.fullmatch(r'\d{2}/\d{2}/\d{4}', text):
        return None
    try:
        return datetime.strptime(text, '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None


def _amount(text):
    text = str(text or '').strip()
    if not re.fullmatch(r'\d+(?:,\d+)?', text):
        return None
    value = Decimal(text.replace(',', '.'))
    return format(value, 'f') if value.is_finite() and value > 0 else None


def _label(text):
    norm = _norm(text)
    if re.fullmatch(r'div(?:idendos?)?(?: .*|)', norm):
        return 'DIVIDENDO'
    if norm.startswith(('jcp', 'jscp', 'juros sobre capital proprio', 'juros sobre o capital proprio')):
        return 'JCP'
    return None


def _event(ticker, cutoff, source, locator, *, label, payment, amount, record=None,
           ex=None, approval=None, gross=False, paid=False, adjusted=False):
    day, right, ex_day, approved = map(_date, (payment, record, ex, approval))
    value, kind = _amount(amount), _label(label)
    if kind is None:
        return None
    issues = []
    if not day:
        issues.append('payment_date_unresolved_deadlines_are_not_dates')
    if not right:
        issues.append('record_date_not_reported_in_primary')
    if value is None:
        issues.append('per_share_amount_unresolved')
    if day and (right and day < right or approved and day < approved):
        issues.append('payment_before_record_or_approval')
    if approved and approved > cutoff.isoformat():
        issues.append('approval_after_cutoff_not_historically_available')
    if adjusted:
        issues.append('publisher_adjusted_share_basis_requires_reconciliation')
    if not gross and kind == 'JCP':
        issues.append('tax_basis_unspecified_by_table')
    past = day is not None and day <= cutoff.isoformat()
    return {'ticker': ticker, 'cvm': IDENTITIES[ticker][0], 'share_class': IDENTITIES[ticker][1],
            'label': kind, 'record_date': right, 'ex_date': ex_day, 'payment_date': day,
            'approval_date': approved, 'published_at': None, 'available_as_of': None,
            'reported_per_share_decimal': value,
            'gross_per_share_decimal': value if gross or kind == 'DIVIDENDO' else None,
            'amount_tax_basis': 'explicit_gross' if gross else 'dividend_per_share' if kind == 'DIVIDENDO'
                else 'unspecified_by_table', 'gross_reconstructed_from_net': False,
            'status': 'paid_reported' if paid and past else 'past_scheduled' if past
                else 'announced' if day else 'date_unresolved',
            'payment_claim_basis': 'explicit_issuer_table_paid_label' if paid else 'issuer_historical_schedule',
            'share_basis': 'publisher_adjusted_unverified' if adjusted else 'reported_unverified',
            'source_ref': source['id'], 'source_locator': locator, 'issues': issues,
            'is_settlement_confirmation': False, 'cash_model_eligible': False,
            'point_in_time_archive_verified': False}


def _cemig_tree(tree):
    scripts = [node for node in tree.xpath('//script') if node.get('id') == '__NEXT_DATA__']
    if len(scripts) != 1:
        raise ValueError('cemig_exact_embedded_page_data_required')
    data = json.loads(scripts[0].text)
    if data.get('query', {}).get('slug') != ['informacoes-financeiras', 'dividendos']:
        raise ValueError('cemig_embedded_page_identity_mismatch')
    content = data['props']['pageProps']['layout']['page']['content']['htmlContent']
    if not isinstance(content, str) or len(content) > MAX_BYTES:
        raise ValueError('invalid_cemig_embedded_calendar')
    return html.fromstring(content, parser=html.HTMLParser(no_network=True))


def inspect_calendar(content, issuer, ticker, cutoff, source):
    """Parse source semantics only; identity, share factors and model gates stay upstream."""
    if issuer not in SOURCES or ticker not in SOURCES[issuer]['tickers'] or ticker not in IDENTITIES:
        raise ValueError('exact_issuer_ticker_required')
    if (source.get('url') != SOURCES[issuer]['url'] or source.get('issuer') != issuer
            or source.get('sha256') != hashlib.sha256(content).hexdigest()
            or source.get('bytes') != len(content)):
        raise ValueError('calendar_source_identity_or_hash_mismatch')
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    observed_text = source.get('retrieved_at') or source.get('observed_at')
    if not isinstance(observed_text, str):
        raise ValueError('calendar_source_observation_time_unavailable')
    observed = datetime.fromisoformat(observed_text)
    if (type(cutoff) is not date or observed.utcoffset() is None or observed.date() < cutoff
            or observed > datetime.now(timezone.utc)):
        raise ValueError('invalid_observation_or_cutoff')
    tree = _tree(content)
    if issuer == 'cemig':
        tree = _cemig_tree(tree)
    events, diagnostics = [], []

    def append(locator, **values):
        event = _event(ticker, cutoff, source, locator, **values)
        if event is not None:
            events.append(event)

    for table_index, table in enumerate(tree.xpath('//table')):
        rows = list(_table_rows(table))
        if not rows:
            continue
        heads = [_norm(x) for x in rows[0]]
        for row_index, row in enumerate(rows[1:], 1):
            loc = {'format': 'embedded_next_data_HTML' if issuer == 'cemig' else 'native_HTML_table',
                   'table': table_index, 'row': row_index}
            if issuer == 'itausa' and heads[:2] == ['tipo', 'deliberado em'] and len(row) == 7:
                if _label(row[0]):
                    append(loc, label=row[0], approval=row[1], record=row[2], payment=row[3],
                           amount=row[4], gross=heads[4] == 'bruto')
            elif issuer == 'romi' and heads[:2] == ['tipo de provento', 'data rca/ago'] and len(row) == 6:
                append(loc, label=row[0], approval=row[1], record=row[2], payment=row[3],
                       amount=row[4], gross='valor bruto' in heads[4], adjusted=True)
            elif issuer == 'jhsf' and heads[:2] == ['ticker', 'tipo'] and len(row) == 9:
                if row[0] == ticker:
                    append(loc, label=row[1], approval=row[2], record=row[6], ex=row[7], payment=row[8],
                           amount=row[5], gross='bruto' in heads[5])
            elif issuer == 'ferbasa' and len(row) >= 7 and _label(row[1]):
                # The source contains explicit rowspans; no manual carry-forward.
                append(loc, label=row[1], approval=row[2], payment=row[3],
                       amount=row[5 if ticker == 'FESA3' else 6])
            elif issuer == 'cemig' and len(row) == 7 and _label(row[0]):
                payments, amounts = row[3].split(), row[4 if ticker == 'CMIG3' else 5].split()
                if len(payments) != len(amounts):
                    diagnostics.append({'code': 'installment_alignment_unresolved', **loc})
                    continue
                for installment, (payment, amount) in enumerate(zip(payments, amounts)):
                    append({**loc, 'installment': installment}, label=row[0], record=row[1], ex=row[2],
                           payment=payment, amount=amount)
            elif issuer == 'tegma' and heads[0] == 'proventos' and len(row) == 7:
                label = ('JCP' if _norm(row[0]) == 'juros' and 'jcp' in _norm(tree.text_content()) else row[0])
                append(loc, label=label, record=row[1], amount=row[2], payment=row[4])
            elif issuer == 'tim' and heads[0] == 'provento' and len(row) == 8:
                append(loc, label=row[0], approval=row[1], ex=row[2], payment=row[7], amount=row[4])
                if events and events[-1]['source_locator'] == loc:
                    events[-1]['issues'].append('per_share_versus_thousand_share_header_conflict')
            elif issuer == 'cury' and heads[0] == 'data de pagamento' and len(row) == 5:
                label = 'DIVIDENDO' if 'dividendo' in _norm(row[1]) else row[1]
                append(loc, label=label, payment=row[0], amount=row[2], paid='pagos' in _norm(row[1]))
            elif issuer == 'banestes' and len(row) == 6 and heads[1] == 'data base de direito':
                # The monthly schedule is explicitly identified as JCP in the page.
                if any(label in _norm(tree.text_content()) for label in
                       ('juros sobre capital proprio', 'juros sobre o capital proprio', 'jcp mensais')):
                    append(loc, label='JCP', record=row[1], ex=row[2], payment=row[3], amount=row[5], gross=True)
            elif issuer == 'copel' and len(row) == 8 and heads[:2] == ['exercicio', 'tipo de provento']:
                append(loc, label=row[1], payment=row[6], amount=row[3])
    if issuer == 'metisa':
        grids = [node for node in tree.xpath('//div') if 'grid-cols-5' in node.get('class', '').split()]
        if len(grids) != 1:
            raise ValueError('metisa_exact_column_grid_required')
        columns = [[p.text_content().strip() for p in node.xpath('./p')]
                   for node in grids[0].xpath('./div')]
        if len(columns) != 5 or len({len(column) for column in columns}) != 1:
            raise ValueError('metisa_column_lengths_disagree')
        expected = ['tipo de provento', 'data da deliberacao rca/ago', 'data pagamento',
                    'r por acao ordinaria', 'r por acao preferencial']
        if [_norm(column[0]) for column in columns] != expected:
            raise ValueError('metisa_column_headers_disagree')
        for index in range(1, len(columns[0])):
            row = [column[index] for column in columns]
            append({'format': 'native_HTML_aligned_column_grid', 'row': index}, label=row[0],
                   approval=row[1], payment=row[2], amount=row[3 if ticker == 'MTSA3' else 4],
                   gross='valor bruto' in _norm(tree.text_content()))
    unique, duplicates = {}, 0
    for event in events:
        key = tuple(event[field] for field in ('label', 'record_date', 'payment_date', 'reported_per_share_decimal'))
        if key in unique:
            duplicates += 1
        else:
            unique[key] = event
    events = list(unique.values())
    return {'ticker': ticker, 'issuer': issuer, 'cvm': IDENTITIES[ticker][0],
            'share_class': IDENTITIES[ticker][1], 'events': events, 'sources': [source],
            'status': 'parsed_not_model_approved' if events else 'calendar_not_found_no_zero_inferred',
            'as_of': cutoff.isoformat(), 'diagnostics': diagnostics, 'duplicate_rows': duplicates,
            'counts': dict(Counter(event['status'] for event in events)),
            'settlement_verified': False, 'point_in_time_archive_verified': False,
            'active_model_changed': False, 'history_complete': False}


def load_calendar(folder, ticker, cutoff):
    """Offline only; immutable original bytes are verified before each parse."""
    matches = [name for name, spec in SOURCES.items() if ticker in spec['tickers']]
    if len(matches) != 1:
        raise ValueError('issuer_calendar_not_supported')
    issuer = matches[0]
    root = Path(folder).resolve()
    receipts = []
    for path in (root / CACHE).glob(issuer + '-*-source.json'):
        if path.is_symlink() or path.stat().st_size > 100_000:
            raise ValueError('unsafe_source_receipt')
        source = json.loads(path.read_text(encoding='utf-8'))
        receipts.append(source)
    if not receipts:
        raise ValueError('issuer_calendar_cache_missing')
    source = max(receipts, key=lambda item: item.get('retrieved_at') or item.get('observed_at') or '')
    return inspect_calendar(read_receipt(root, source), issuer, ticker, cutoff, source)


def missing_primary_installments(original, calendar):
    """Return only documented new entitlement groups, never overwrite a conflict.

    The parent must run the unchanged independent-amount/share-factor gates.
    Unknown primary record dates, deadlines and future approvals cannot enter.
    No net-to-gross conversion or historical publication inference occurs.
    """
    records = {item['record_date'] for item in original.installments}
    result = []
    groups = defaultdict(list)
    for event in calendar['events']:
        groups[event['record_date']].append(event)
    for record, events in groups.items():
        if record is None or record in records:
            continue
        if any(not event['payment_date'] or not event['reported_per_share_decimal']
               or any(issue not in ('tax_basis_unspecified_by_table',
                                     'publisher_adjusted_share_basis_requires_reconciliation')
                      for issue in event['issues']) for event in events):
            continue
        for event in events:
            result.append(deepcopy(event))
    return result


def augment_payment_history(original, calendar):
    """Return an in-memory alternative for the parent's UNCHANGED reconciliation.

    Reading this result does not establish gross amounts: the parent still has
    to reconcile every entire entitlement group to an independent gross source
    with the known split/bonus chain. Dates and amounts are source reported.
    Existing groups and unresolved primary groups are not silently repaired.
    """
    result = deepcopy(original)
    events = missing_primary_installments(original, calendar)
    sources = {source['id']: source for source in calendar['sources']}
    for event in events:
        source = sources[event['source_ref']]
        locator = event['source_locator']
        row_id = 'primary:' + event['source_ref'] + ':' + json.dumps(locator, sort_keys=True)
        evidence = {**source, 'locator': locator, 'hash_basis': 'original_bytes_verified',
                    'reported_tax_basis': event['amount_tax_basis']}
        result.installments.append({'ticker': calendar['ticker'], 'record_date': event['record_date'],
            'payment_date': event['payment_date'], 'gross_per_unit': float(event['reported_per_share_decimal']),
            'label': event['label'], 'status': 'pending_reconciliation', 'source_row': row_id,
            'reported_status': None, 'published_at': event.get('published_at'),
            'observed_at': source.get('retrieved_at') or source['observed_at'],
            'primary_evidence': [evidence], 'primary_declaration_id': event.get('primary_declaration_id'),
            'reported_amount_tax_basis': event['amount_tax_basis'],
            'gross_basis_requires_independent_reconciliation': event['gross_per_share_decimal'] is None})
        if event.get('payment_date_basis'):
            result.installments[-1].update(payment_date_basis=event['payment_date_basis'],
                full_settlement_on_date_verified=False)
        result.semantic_events.append({**event, 'gross_per_share': float(event['reported_per_share_decimal']),
            'source_row': row_id, 'reported_status': None, 'date_order_valid': True,
            'primary_evidence': [evidence], 'primary_declaration_id': event.get('primary_declaration_id')})
    result.event_count += len(events)
    result.research_added_installments = getattr(original, 'research_added_installments', 0) + len(events)
    used = {event['source_ref'] for event in events}
    retained = {source['id']: source for source in getattr(original, 'research_sources', [])}
    retained.update({key: value for key, value in sources.items() if key in used})
    result.research_sources = list(retained.values())
    return result


def acquire_cvm_document(folder, document, cutoff):
    """Acquire a document selected from the hashed IPE index, without editing its report."""
    from . import cvm_proventos_documents as cvm
    sequence, version = cvm.document_id(document['url'])
    if str(document.get('sequence')) != sequence or document.get('version') != version:
        raise ValueError('document_index_identity_mismatch')
    root = Path(folder).resolve()
    _verified_delivery(root, document)
    cache = root / CACHE
    cache.mkdir(parents=True, exist_ok=True)
    key = f'cvm{sequence}v{version}'
    receipts = sorted(cache.glob(key + '-*-source.json'))
    if receipts:
        source = json.loads(receipts[-1].read_text(encoding='utf-8'))
        if source['url'] != document['url'] or source['document'] != document:
            raise ValueError('cached_CVM_index_identity_mismatch')
        body = read_receipt(root, source)
    else:
        legacy = root / 'raw/primary-research/cvm-documents'
        paths = sorted(legacy.glob(f'{sequence}-v{version}-*.pdf'))
        if paths:
            path = paths[-1]
            if path.is_symlink() or path.stat().st_size > cvm.MAX_BYTES:
                raise ValueError('unsafe_primary_document_cache')
            body = path.read_bytes()
            if path.name != f'{sequence}-v{version}-' + hashlib.sha256(body).hexdigest() + '.pdf':
                raise ValueError('CVM_document_cache_hash_mismatch')
            observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        else:
            request = urllib.request.Request(document['url'], headers={
                'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/pdf',
                'Accept-Encoding': 'identity'})
            with cvm._open(request, timeout=30) as response:
                body = response.read(cvm.MAX_BYTES + 1)
            observed = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(body).hexdigest()
        try:
            parsed = cvm.parse_pdf(body, document, cutoff)
        except ValueError as error:
            # Preserve a failed original response for diagnosis, never as a
            # successful PDF receipt or an absence-of-dividends observation.
            relative = f'{CACHE}/{key}-{digest}.response.bin'
            _immutable(root / relative, body)
            failure = {'document': document, 'url': document['url'], 'file': relative,
                       'sha256': digest, 'bytes': len(body), 'retrieved_at': observed,
                       'signature_hex': body[:16].hex(), 'status': 'unusable_original_response',
                       'error': str(error), 'cash_model_eligible': False}
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            _immutable(cache / f'{key}-{digest}-{stamp}-failure.json',
                       json.dumps(failure, ensure_ascii=True, indent=2).encode('utf-8'))
            raise
        relative = f'{CACHE}/{key}-{digest}.pdf'
        source = {'id': key + ':' + digest, 'issuer': 'cvm', 'url': document['url'],
                  'sha256': digest, 'bytes': len(body), 'retrieved_at': observed, 'file': relative,
                  'authority': 'primary_CVM_declaration_not_settlement', 'document': document,
                  'settlement_verified': False, 'point_in_time_archive_verified': False}
        _immutable(root / relative, body)
        _immutable(cache / f'{key}-{digest}-source.json',
                   json.dumps(source, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8'))
        return parsed, source
    return cvm.parse_pdf(body, document, cutoff), source


@lru_cache(maxsize=4)
def _index_records(body):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = archive.namelist()
        if len(names) != 1 or not re.fullmatch(r'ipe_cia_aberta_\d{4}\.csv', names[0]) \
                or archive.getinfo(names[0]).file_size > 100_000_000:
            raise ValueError('invalid_IPE_index_archive')
        with archive.open(names[0]) as stream:
            return {row['Link_Download']: row for row in csv.DictReader(
                io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')
                if _norm(row['Categoria']) == 'relatorio proventos'}


def _verified_delivery(folder, document):
    """Recover the reported IPE delivery day/timestamp from the hashed index."""
    manifest = folder / 'raw/primary-research/cvm-documents/index-sources.json'
    if manifest.is_symlink() or manifest.stat().st_size > 100_000:
        raise ValueError('invalid_IPE_index_manifest')
    sources = json.loads(manifest.read_text(encoding='utf-8'))
    matches = [source for source in sources if source.get('sha256') == document.get('index_sha256')
               and source.get('url') == document.get('index_url')]
    if len(matches) != 1:
        raise ValueError('exact_IPE_index_source_required')
    source = matches[0]
    relative = source.get('file', '')
    if not re.fullmatch(r'raw/primary-research/cvm-documents/index-\d{4}-[a-f0-9]{64}\.zip', relative):
        raise ValueError('invalid_IPE_index_cache_path')
    path = folder / relative
    if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > MAX_BYTES:
        raise ValueError('unsafe_IPE_index_cache')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != source['sha256']:
        raise ValueError('IPE_index_hash_mismatch')
    row = _index_records(body).get(document['url'])
    if not row or int(row['Codigo_CVM']) != document['cvm']:
        raise ValueError('document_not_in_verified_IPE_index')
    stamp = datetime.fromisoformat(row['Data_Entrega'])
    if stamp.date().isoformat() != document['published_at']:
        raise ValueError('IPE_document_publication_disagrees')
    return {'delivered_at': row['Data_Entrega'], 'file': relative, 'sha256': source['sha256'],
            'url': source['url'], 'basis': 'reported_IPE_delivery_not_point_in_time_archive'}


def load_cvm_calendar(folder, stock, cutoff, independent_rows):
    """Native CVM dates/amounts, type corroborated by exact cached entitlement row.

    The CVM form does NOT name DIV/JCP. It is never guessed from ticker, tax rate
    or PDF title. Only a unique matching cached independent class/record/amount
    supplies the event type. Neither schedule claims payment actually settled.
    """
    from . import cvm_proventos_documents as cvm
    from .share_basis import br_number
    from .collect_market_data import normalize
    root = Path(folder).resolve()
    cutoff = date.fromisoformat(cutoff) if isinstance(cutoff, str) else cutoff
    if stock.get('share_class') == 'PNU':
        from .schulz_share_class import class_binding
        class_binding(root, stock, cutoff)
    if (type(stock.get('cvm')) is not int or stock.get('share_class') not in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND', 'PNU')
            or not re.fullmatch(r'BR[A-Z0-9]{9}\d', stock.get('isin') or '')):
        raise ValueError('exact_CVM_class_ISIN_required')
    events, sources, diagnostics = [], [], []
    documents_by_record = defaultdict(list)
    for path in sorted((root / CACHE).glob('cvm*-source.json')):
        if path.is_symlink() or path.stat().st_size > 100_000:
            raise ValueError('unsafe_source_receipt')
        source = json.loads(path.read_text(encoding='utf-8'))
        document = source.get('document', {})
        if document.get('cvm') != stock['cvm'] or document.get('published_at', '') > cutoff.isoformat():
            continue
        sequence, version = cvm.document_id(source['url'])
        if (source['url'] != document.get('url') or str(document.get('sequence')) != sequence
                or document.get('version') != version or source.get('issuer') != 'cvm'):
            raise ValueError('CVM_receipt_document_identity_mismatch')
        source['index_evidence'] = _verified_delivery(root, document)
        parsed = cvm.parse_pdf(read_receipt(root, source), document, cutoff)
        sources.append(source)
        for item in parsed['events']:
            if item['isin'] != stock['isin'] or item['cvm'] != stock['cvm'] or not item.get('record_date'):
                continue
            documents_by_record[item['record_date']].append((item, source, parsed.get('issues', [])))
    for record, variants in documents_by_record.items():
        latest = max(source['index_evidence']['delivered_at'] for _, source, _ in variants)
        current = [(item, source, issues) for item, source, issues in variants
                   if source['index_evidence']['delivered_at'] == latest]
        if any(issues or item.get('issues') or not item.get('payment_date') for item, _, issues in current):
            diagnostics.append({'record_date': record, 'code': 'latest_CVM_native_form_unresolved_no_older_fallback'})
            continue
        if len({source['document']['sequence'] for _, source, _ in current}) != 1:
            # Separate notices on the same date may be revisions or additive;
            # the index day alone does not prove either interpretation.
            diagnostics.append({'record_date': record, 'code': 'same_day_CVM_documents_require_version_reconciliation'})
            continue
        for item, source, _ in current:
            document = source['document']
            kinds = set()
            for row in independent_rows:
                if row.get('typeStock', '').strip() != stock['share_class']:
                    continue
                if _date(row.get('lastDatePriorEx')) != item['record_date']:
                    continue
                quantity = br_number(row.get('quotedPerShares') or '1')
                value = br_number(row.get('valueCash', ''))
                if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(value) or value < 0:
                    raise ValueError('invalid_independent_share_quantity')
                amount = Decimal(str(value / quantity))
                reported = Decimal(item['gross_per_unit_decimal'])
                if abs(amount - reported) > max(Decimal('0.00002'), reported * Decimal('0.002')):
                    continue
                kind = normalize(row.get('corporateAction', ''))
                if kind in ('dividendo', 'jrs cap proprio'):
                    kinds.add('DIVIDENDO' if kind == 'dividendo' else 'JCP')
            if len(kinds) != 1:
                diagnostics.append({'source_ref': source['id'], 'code': 'cash_type_not_independently_resolved',
                                    'record_date': item['record_date']})
                continue
            event = {'ticker': stock['ticker'], 'cvm': stock['cvm'], 'share_class': stock['share_class'],
                     'isin': stock['isin'], 'label': next(iter(kinds)),
                     'label_basis': 'exact_independent_class_record_and_gross_amount_match',
                     'record_date': item['record_date'], 'payment_date': item['payment_date'],
                     'approval_date': item['approval_date'], 'ex_date': None,
                     'published_at': document['published_at'], 'available_as_of': True,
                     'reported_per_share_decimal': item['gross_per_unit_decimal'],
                     'gross_per_share_decimal': item['gross_per_unit_decimal'],
                     'amount_tax_basis': 'explicit_gross_exact_ISIN_unit',
                     'gross_reconstructed_from_net': False, 'share_basis': 'reported_unverified',
                     'status': item['status'], 'source_ref': source['id'],
                     'source_locator': {'format': 'native_CVM_PDF_table',
                         'table': item['source_table'], 'row': item['source_row']},
                     'primary_declaration_id': source['id'] + ':' + str(item['source_row']),
                     'payment_claim_basis': 'CVM_declared_schedule_not_settled_cash', 'issues': [],
                     'is_settlement_confirmation': False, 'cash_model_eligible': False,
                     'point_in_time_archive_verified': False}
            events.append(event)
    return {'ticker': stock['ticker'], 'cvm': stock['cvm'], 'share_class': stock['share_class'],
            'events': events, 'sources': sources, 'diagnostics': diagnostics,
            'as_of': cutoff.isoformat(), 'active_model_changed': False,
            'settlement_verified': False, 'point_in_time_archive_verified': False, 'history_complete': False}


def _verified_primary_group_event(event, calendar):
    format_name = event.get('source_locator', {}).get('format')
    if format_name == 'native_CVM_PDF_table':
        return True
    if format_name == 'CVM_explicit_revision_chain':
        return event.get('revision_chain_verified') is True
    if format_name not in ('CVM_reviewed_old_notice_complete_group',
                           'exact_reviewed_native_entitlement_group'):
        return False
    if format_name == 'exact_reviewed_native_entitlement_group' and (
            not calendar.get('isin') or event.get('isin') != calendar['isin']):
        return False
    groups = [group for group in calendar.get('complete_groups', [])
              if group.get('id') == event.get('complete_group_id')]
    if len(groups) != 1:
        return False
    group = groups[0]
    events = group.get('events', [])
    if (group.get('target_group_complete') is not True or not events or event not in events
            or group.get('installment_count') != len(events)):
        return False
    for item in events:
        if format_name == 'exact_reviewed_native_entitlement_group' and item.get('isin') != calendar['isin']:
            return False
        if (item.get('complete_group_id') != group['id']
                or item.get('ticker') != calendar['ticker']
                or item.get('cvm') != calendar['cvm']
                or item.get('share_class') != calendar['share_class']
                or item.get('record_date') != group.get('record_date')
                or not item.get('primary_declaration_id') or item.get('issues')
                or not item.get('payment_date') or not item.get('gross_per_share_decimal')
                or type(item['gross_per_share_decimal']) is not str
                or item.get('gross_reconstructed_from_net') is not False
                or item.get('reported_per_share_decimal') != item['gross_per_share_decimal']
                or item.get('source_ref') not in {source['id'] for source in calendar['sources']}):
            return False
        if (item.get('label') not in ('DIVIDENDO', 'JCP')
                or item.get('amount_tax_basis') not in ('explicit_gross_exact_ISIN_unit',
                    'explicit_gross_per_share', 'dividend_per_share',
                    'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows')):
            return False
    try:
        if type(group['gross_total_decimal']) is not str:
            return False
        amounts = [Decimal(item['gross_per_share_decimal']) for item in events]
        total = Decimal(group['gross_total_decimal'])
        return (all(amount.is_finite() and amount > 0 for amount in amounts)
                and total.is_finite() and sum(amounts, Decimal(0)) == total)
    except (InvalidOperation, KeyError, TypeError):
        return False


def replace_verified_primary_groups(original, calendar, expected, actions, cutoff):
    """Replace a disputed complete group only with byte-verified primary rows.

    Every replacement is audited, including old values/dates and source refs.
    The caller must still run ALL normal cash, price and financial gates. This
    is an explicit source correction, not permission to accept an unmatched row.
    """
    import math
    from .share_basis import factor_after
    result = deepcopy(original)
    result.research_group_corrections = deepcopy(getattr(original, 'research_group_corrections', []))
    groups = defaultdict(list)
    for event in calendar['events']:
        if event.get('primary_declaration_id') and _verified_primary_group_event(event, calendar):
            groups[event['record_date']].append(event)
    bases = [date.fromisoformat(item['date']) for item in actions]
    for record, events in groups.items():
        day = date.fromisoformat(record)
        if day > cutoff or day not in expected or any(event.get('issues') for event in events):
            continue
        total = sum(float(event['reported_per_share_decimal']) for event in events)
        factors = {1., *(factor_after(day, actions, basis) for basis in [*bases, cutoff] if basis >= day)}
        if total <= 0 or not any(math.isclose(total, expected[day] / factor, rel_tol=.002, abs_tol=.00002)
                                 for factor in factors):
            continue
        old = [item for item in result.installments if item['record_date'] == record]
        old_view = sorted((item['payment_date'], item['label'], item['gross_per_unit']) for item in old)
        new_view = sorted((event['payment_date'], event['label'], float(event['reported_per_share_decimal']))
                          for event in events)
        if old_view == new_view:
            continue
        result.installments = [item for item in result.installments if item['record_date'] != record]
        result.semantic_events = [item for item in result.semantic_events if item['record_date'] != record]
        result.event_count -= len(old)
        alternative = augment_payment_history(result, {**calendar, 'events': events})
        alternative.research_group_corrections = result.research_group_corrections + [{
            'record_date': record, 'old_calendar_rows': old, 'new_primary_rows': deepcopy(events),
            'reason': 'native_CVM_full_entitlement_group_matches_independent_gross_amount',
            'settlement_verified': False, 'old_source_conflict_preserved_in_audit': True}]
        result = alternative
    return result


def relocate_verified_primary_groups(original, calendar, expected, cutoff):
    """Correct an orphan record date using one exact native cash-group match.

    Neither proximity nor a holiday calendar supplies a new date. The native
    CVM form supplies it explicitly. Both directions of the match must be
    unique, payment dates/types/totals must agree, and the old record date must
    have no independent entitlement. All normal reconciliation still follows.
    """
    groups, old_groups = defaultdict(list), defaultdict(list)
    for event in calendar['events']:
        if (event.get('primary_declaration_id') and not event.get('issues')
                and _verified_primary_group_event(event, calendar)
                and event.get('record_date') and event.get('payment_date')):
            day = date.fromisoformat(event['record_date'])
            if day in expected and day <= cutoff:
                groups[event['record_date']].append(event)
    for row in original.installments:
        if date.fromisoformat(row['record_date']) not in expected:
            old_groups[row['record_date']].append(row)

    def cash_view(rows, field):
        totals = defaultdict(float)
        for row in rows:
            totals[row['label'], row['payment_date']] += float(row[field])
        return totals

    matches = defaultdict(list)
    for record, events in groups.items():
        native = cash_view(events, 'reported_per_share_decimal')
        # The native group must already match the independent gross amount;
        # relocation cannot turn a net amount into a gross one.
        if not math.isclose(sum(native.values()), expected[date.fromisoformat(record)],
                            rel_tol=.002, abs_tol=.00002):
            continue
        for old_record, rows in old_groups.items():
            old = cash_view(rows, 'gross_per_unit')
            if old.keys() == native.keys() and all(math.isclose(old[key], native[key],
                    rel_tol=.002, abs_tol=.00002) for key in native):
                matches[record].append(old_record)
    reverse = Counter(old_record for records in matches.values() for old_record in records)
    result = deepcopy(original)
    result.research_group_corrections = deepcopy(getattr(original, 'research_group_corrections', []))
    for record, records in matches.items():
        if len(records) != 1 or reverse[records[0]] != 1:
            continue
        old_record = records[0]
        # A target already represented in the calendar needs a full conflict
        # review, not a second event pasted over its existing group.
        if any(row['record_date'] == record for row in result.installments):
            continue
        removed = [row for row in result.installments if row['record_date'] == old_record]
        result.installments = [row for row in result.installments if row['record_date'] != old_record]
        result.semantic_events = [row for row in result.semantic_events if row['record_date'] != old_record]
        result.event_count -= len(removed)
        alternative = augment_payment_history(result, {**calendar, 'events': groups[record]})
        alternative.research_group_corrections = result.research_group_corrections + [{
            'record_date': record, 'original_record_date': old_record,
            'old_calendar_rows': deepcopy(removed), 'new_primary_rows': deepcopy(groups[record]),
            'reason': 'unique_native_CVM_cash_group_with_explicit_record_date',
            'payment_dates_changed': False, 'settlement_verified': False,
            'old_source_conflict_preserved_in_audit': True}]
        result = alternative
    return result


def deduplicate_independent_rows(stock, cache, cutoff, rows):
    """Deduplicate byte-equivalent cached records only with one native declaration.

    Equal values alone never imply duplicates. Every original field must agree,
    and one exact native CVM class/ISIN/record/approval/amount row must corroborate
    the event. Different rows or multiple declarations remain unresolved.
    """
    keys = [json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False) for row in rows]
    counts = Counter(keys)
    if not any(count > 1 for count in counts.values()):
        return rows, []
    calendar = load_cvm_calendar(cache.folder, stock, cutoff, rows)
    from .share_basis import br_number
    result, seen, corrections = [], set(), []
    for row, key in zip(rows, keys):
        if counts[key] == 1 or row.get('typeStock', '').strip() != stock['share_class']:
            result.append(row)
            continue
        record, approved = _date(row.get('lastDatePriorEx')), _date(row.get('dateApproval'))
        matches = [event for event in calendar['events'] if event['record_date'] == record
                   and event.get('approval_date') == approved and event.get('isin') == stock['isin']]
        amount = br_number(row.get('valueCash', '')) / br_number(row.get('quotedPerShares') or '1')
        if len(matches) != 1 or not math.isclose(amount, float(matches[0]['reported_per_share_decimal']),
                                                rel_tol=1e-9, abs_tol=1e-11):
            result.append(row)
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
        source = next(source for source in calendar['sources'] if source['id'] == matches[0]['source_ref'])
        cache.read(source['file'], 'primary_cash_calendar')
        cache.receipts[source['file']].update(url=source['url'], authority=source['authority'])
        corrections.append({'record_date': record, 'copies_in_original_cache': counts[key],
            'original_row': deepcopy(row), 'native_declaration': matches[0],
            'basis': 'all_original_fields_equal_and_one_exact_native_CVM_declaration',
            'settlement_verified': False})
    return result, corrections


def diagnose_cash_mismatches(folder, inventory, tickers, cutoff):
    """Read-only dates/amounts diagnostic using the same parent tolerance/factors."""
    import math
    from .inventory_candidates import _Cache, _payments, _cash_pages
    from .share_basis import b3_evidence, factor_after
    from .broad_stocks import split_price_history
    from .collect_market_data import shift_month
    cache = _Cache(folder, inventory)
    start = shift_month(cutoff, -60)
    results = []
    for stock in inventory['stocks']:
        ticker = stock['ticker']
        if ticker not in tickers:
            continue
        try:
            cvm = stock['cvm']
            detail = cache.json(f'raw/expanded/b3-detail-{cvm}.json', 'identity')
            codes = {item['code']: item['isin'] for item in detail['otherCodes']}
            rows = _cash_pages(ticker, codes, cvm, cache)
            expected, _ = b3_evidence(rows, stock['share_class'])
            original, _ = _payments(ticker, cache, cutoff)
            prices = f'raw/expanded/{ticker}-prices.json'
            if not (Path(folder) / prices).is_file():
                prices = f'raw/inventory/daily-prices/{ticker}.json'
            _, actions = split_price_history(cache.read(prices, 'prices')[0], ticker, cutoff,
                                             allow_recent_actions=True)
            groups = defaultdict(float)
            for row in original.installments:
                groups[date.fromisoformat(row['record_date'])] += row['gross_per_unit']
            relevant = {day for day in expected if start <= day <= cutoff}
            relevant.update(date.fromisoformat(row['record_date']) for row in original.installments
                if start <= date.fromisoformat(row['payment_date']) <= cutoff)
            bases = [date.fromisoformat(row['date']) for row in actions]
            mismatches = []
            for record in sorted(relevant):
                actual, target = groups.get(record), expected.get(record)
                factors = {1., *(factor_after(record, actions, basis) for basis in [*bases, cutoff]
                                  if basis >= record)}
                if (actual is not None and target is not None and actual > 0
                        and any(math.isclose(actual, target / factor, rel_tol=.002, abs_tol=.00002)
                                for factor in factors)):
                    continue
                mismatches.append({'record_date': record.isoformat(), 'reported_calendar_sum': actual,
                                   'independent_entitlement_sum': target,
                                   'known_share_factors': sorted(factors)})
            results.append({'ticker': ticker, 'cvm': cvm, 'mismatches': mismatches})
        except (ValueError, OSError, KeyError, IndexError, TypeError) as error:
            results.append({'ticker': ticker, 'error': str(error)})
    return results


def apply_research_calendar(stock, cache, cutoff, original, independent_rows, actions):
    """Parent integration hook, called before its normal amount/price checks.

    cache is the parent's read-only _Cache. This hook reads immutable sources,
    adds receipts to that same audit, and returns an alternative DividendHistory.
    It does not alter readiness, thresholds, snapshots, or any pointer.
    """
    from .share_basis import b3_evidence
    if stock.get('share_class') not in ('ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND', 'PNU'):
        return original
    expected, _ = b3_evidence(independent_rows, stock['share_class'])
    result = original
    primary = load_cvm_calendar(cache.folder, stock, cutoff, independent_rows)
    if primary['events']:
        result = relocate_verified_primary_groups(result, primary, expected, cutoff)
        result = replace_verified_primary_groups(result, primary, expected, actions, cutoff)
    if stock['ticker'] in ('ENMT3', 'ENMT4', 'BEES3', 'BEES4'):
        from .older_cash_notice_research import (
            CACHE as OLDER_CACHE, load_older_cash_evidence, verified_older_cash_calendar)
        if (cache.folder / OLDER_CACHE / 'reviewed-notices.json').is_file():
            if stock['ticker'] not in cache.older_calendars:
                if cache.older_cash_evidence is None:
                    cache.older_cash_evidence = load_older_cash_evidence(cache.folder)
                cache.older_calendars[stock['ticker']] = verified_older_cash_calendar(
                    cache.folder, stock, cutoff, evidence=cache.older_cash_evidence)
            older = cache.older_calendars[stock['ticker']]
            result = relocate_verified_primary_groups(result, older, expected, cutoff)
            result = replace_verified_primary_groups(result, older, expected, actions, cutoff)
            retained = {source['id']: source for source in getattr(result, 'research_sources', [])}
            retained.update({source['id']: source for source in older['sources']})
            result.research_sources = list(retained.values())
    if stock['ticker'] == 'AFLT3':
        from .primary_cash_exact_research import CACHE as EXACT_CACHE, verified_calendar
        if (cache.folder / EXACT_CACHE / 'reviewed-notices.json').is_file():
            if stock['ticker'] not in cache.exact_cash_calendars:
                cache.exact_cash_calendars[stock['ticker']] = verified_calendar(cache.folder, stock, cutoff)
            calendar = cache.exact_cash_calendars[stock['ticker']]
            result = replace_verified_primary_groups(result, calendar, expected, actions, cutoff)
            retained = {source['id']: source for source in getattr(result, 'research_sources', [])}
            for source in calendar['sources']:
                retained[source['id']] = {**source, 'retrieved_at': source.get('retrieved_at')}
            for source in calendar.get('evidence_sources', []):
                body, _ = cache.read(source['file'], 'primary_cash_calendar_proof')
                if hashlib.sha256(body).hexdigest() != source['sha256']:
                    raise ValueError('exact_cash_evidence_hash_mismatch')
                if source.get('url'):
                    key = 'primary-cash-exact-source:' + source['sha256']
                    retained[key] = {**source, 'id': key, 'authority': 'primary_original_read_receipt',
                                     'retrieved_at': source.get('retrieved_at')}
            result.research_sources = list(retained.values())
    if stock['ticker'] == 'PNVL3':
        from .pnvl_native_calendar import metadata_warnings, verified_calendar
        if stock['ticker'] not in cache.pnvl_native_calendars:
            cache.pnvl_native_calendars[stock['ticker']] = verified_calendar(cache.folder, stock, cutoff)
        calendar = cache.pnvl_native_calendars[stock['ticker']]
        warnings = metadata_warnings(calendar)
        proof = {'native_calendar_contract': calendar, 'source_conflicts': calendar['source_conflicts'],
                 'modelling_priority_rule': calendar['modelling_priority_rule'],
                 'source_conflict_warnings': warnings}
        validate_native_calendar_checks(proof)
        for source in calendar['evidence_sources']:
            cache.register_research_evidence(source)
        result = replace_verified_primary_groups(result, calendar, expected, actions, cutoff)
        retained = {source['id']: source for source in getattr(result, 'research_sources', [])}
        retained.update({source['id']: source for source in calendar['sources']})
        result.research_sources = list(retained.values())
        result.native_calendar_contract = deepcopy(calendar)
        result.source_conflict_warnings = deepcopy(warnings)
    if stock['ticker'] == 'PINE4':
        from .revised_cash_research import CACHE as REVISION_CACHE, verified_pine_calendar
        if (cache.folder / REVISION_CACHE / 'reviewed-notices.json').is_file():
            if 'PINE4' not in cache.revised_calendars:
                cache.revised_calendars['PINE4'] = verified_pine_calendar(cache.folder, stock, cutoff)
            revision = cache.revised_calendars['PINE4']
            result = replace_verified_primary_groups(result, revision, expected, actions, cutoff)
            retained = {source['id']: source for source in getattr(result, 'research_sources', [])}
            retained.update({source['id']: source for source in revision['sources']})
            result.research_sources = list(retained.values())
    if stock['ticker'] in ('CMIG3', 'CMIG4'):
        calendar = load_calendar(cache.folder, stock['ticker'], cutoff)
        result = augment_payment_history(result, calendar)
    for source in getattr(result, 'research_sources', []):
        cache.read(source['file'], 'primary_cash_calendar')
        cache.receipts[source['file']].update(url=source['url'], authority=source['authority'])
        for key in ('retrieved_at', 'observed_at', 'observed_at_basis',
                    'settlement_verified', 'point_in_time_archive_verified'):
            if key in source:
                cache.receipts[source['file']][key] = source[key]
        index = source.get('index_evidence')
        if index:
            cache.read(index['file'], 'CVM_calendar_publication_index')
            cache.receipts[index['file']]['url'] = index['url']
    return result


def validate_native_calendar_checks(research):
    from .pnvl_native_calendar import explicit_kind, validate_warning_propagation
    calendar = research.get('native_calendar_contract')
    if not calendar:
        raise ValueError('pnvl_native_calendar_metadata_required')
    if (research.get('source_conflicts') != calendar['source_conflicts']
            or research.get('modelling_priority_rule') != calendar['modelling_priority_rule']):
        raise ValueError('pnvl_native_conflict_or_priority_metadata_missing')
    groups = calendar.get('complete_groups', [])
    if (len(groups) != 1 or not groups[0].get('cash_type_proof', {}).get('located_cached_rows')):
        raise ValueError('pnvl_native_explicit_type_proof_missing')
    sources = calendar.get('evidence_sources', [])
    if len(sources) != 115 or len({(s['file'], s['sha256'], s.get('url')) for s in sources}) != 115:
        raise ValueError('pnvl_native_complete_evidence_receipts_required')
    try:
        validate_warning_propagation(calendar, research.get('source_conflict_warnings', []))
        proof = calendar['complete_groups'][0]['cash_type_proof']
        rows = proof['located_cached_rows']
        if (len(rows) != len(calendar['events'])
                or {explicit_kind(row['raw']['corporateAction']) for row in rows} != {proof['label']}
                or any(event['label'] != proof['label'] for event in calendar['events'])):
            raise ValueError('pnvl_native_explicit_type_proof_mismatch')
    except ValueError as error:
        raise ValueError(str(error)) from error
    return calendar


def _native_calendar_fields(calendars):
    from .pnvl_native_calendar import metadata_warnings
    return {
        'cash_calendar_native_contracts': deepcopy(calendars),
        'cash_calendar_source_conflicts': [
            {'ticker': calendar['ticker'], 'complete_group_id': calendar['complete_groups'][0]['id'], **deepcopy(conflict)}
            for calendar in calendars for conflict in calendar['source_conflicts']],
        'cash_calendar_modelling_priority_rules': [
            {'ticker': calendar['ticker'], 'rule': deepcopy(calendar['modelling_priority_rule'])} for calendar in calendars],
        'cash_calendar_source_warnings': [warning for calendar in calendars for warning in metadata_warnings(calendar)]}


def _native_evidence_receipts(calendars, receipts):
    actual = {(source['file'], source['sha256'], source.get('url'), source.get('retrieved_at')): source for source in receipts}
    retained = {}
    for calendar in calendars:
        for source in calendar['evidence_sources']:
            key = (source['file'], source['sha256'], source.get('url'), source.get('retrieved_at'))
            receipt = actual.get(key)
            if receipt is None or receipt.get('retrieved_at') != source.get('retrieved_at'):
                raise ValueError('pnvl_native_evidence_receipt_missing_or_changed')
            retained[key] = deepcopy(receipt)
    return list(retained.values())


def native_calendar_metadata(candidates, ready, receipts):
    calendars = []
    for ticker in sorted(set(ready) & {'PNVL3'}):
        matches = [row for row in candidates if row['ticker'] == ticker]
        if len(matches) != 1:
            raise ValueError('pnvl_native_ready_candidate_ambiguous')
        calendars.append(validate_native_calendar_checks(matches[0].get('checks', {}).get('cash_calendar_research', {})))
    return {**_native_calendar_fields(calendars),
            'cash_calendar_evidence_receipts': _native_evidence_receipts(calendars, receipts)}


def calendar_conflict_engine_warnings(metadata, tickers):
    calendars = metadata.get('cash_calendar_native_contracts', [])
    expected = set(tickers) & {'PNVL3'}
    if {calendar.get('ticker') for calendar in calendars} != expected or len(calendars) != len(expected):
        raise ValueError('pnvl_native_calendar_metadata_required')
    fields = _native_calendar_fields(calendars)
    if any(metadata.get(key, []) != value for key, value in fields.items()):
        raise ValueError('pnvl_native_flat_conflict_or_priority_metadata_missing')
    if not calendars:
        return []
    for calendar in calendars:
        validate_native_calendar_checks({'native_calendar_contract': calendar,
            'source_conflicts': calendar['source_conflicts'], 'modelling_priority_rule': calendar['modelling_priority_rule'],
            'source_conflict_warnings': metadata['cash_calendar_source_warnings']})
    _native_evidence_receipts(calendars, metadata.get('cash_calendar_evidence_receipts', []))
    _native_evidence_receipts(calendars, metadata.get('sources', []))
    return ['PNVL3: o modelo usa as datas programadas do formulario nativo exato da CVM '
        '(31/03/2025 na segunda parcela). O texto de RI do emissor diverge entre 28/03/2025 e 31/03/2025; '
        'o conflito permanece sem resolucao e a versao da copia de RI e desconhecida. '
        'Isto nao comprova liquidacao bancaria nem disponibilidade historica point-in-time.']


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--issuers', nargs='+', choices=sorted(SOURCES), default=sorted(SOURCES))
    args = parser.parse_args()
    for name in args.issuers:
        try:
            body, source = acquire(args.folder, name)
            tables = inspect_tables(body)
            print(json.dumps({'issuer': name, 'source': source, 'table_sizes': [len(t['rows']) for t in tables]},
                             ensure_ascii=True), flush=True)
        except Exception as error:
            print(json.dumps({'issuer': name, 'status': 'unavailable', 'error': str(error)}, ensure_ascii=True), flush=True)
