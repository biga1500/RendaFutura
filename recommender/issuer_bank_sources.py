"""Bounded public bank-RI research; no existing collector or model integration.

Only download_sources performs network/file writes, into an explicit research
folder. It preserves hash-addressed originals and records exact public URLs,
HTTP results and retrieval timestamps. No B3 endpoints, API keys or TLS bypass.
Issuer reported paid status, if present, is not independent bank settlement.

Offline APIs: inspect_bank_html/content and inspect_bank_pdf return events,
actions, field_status, coverage, source and issues. Dates are ISO, cash amounts
are BRL/share in reported/unverified share units, missing values are None.
match_payment_declarations requires exact CVM/class/ticker/label/record/payment
and a matching reported amount; it can supply gross/publication fields but
NEVER upgrades scheduled to paid. parse_research_downloads(folder, as_of)
reads only known downloaded layouts and returns (frozen_json_path, payload).
Primary calendar events and supporting declaration rows are separate lists,
not additive cash. No parser activates data, verifies settlement or fills gaps.
"""

from datetime import datetime, timezone
from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import ssl
from urllib.parse import parse_qsl, urlsplit
import urllib.error
import urllib.request
import unicodedata

from lxml import etree, html

from .payment_sources import _br_number, _table_rows


BANK_PAGES = (
    {'issuer': 'itau', 'label': 'itau-payments-page',
     'url': 'https://www.itau.com.br/relacoes-com-investidores/informacoes-ao-mercado/dividendos-e-jcp/'},
    {'issuer': 'bradesco', 'label': 'bradesco-payments-page',
     'url': 'https://www.bradescori.com.br/informacoes-ao-mercado/remuneracao-aos-acionistas/'},
    {'issuer': 'bb', 'label': 'bb-payments-page',
     'url': 'https://ri.bb.com.br/servicos-para-investidores/historico-de-remuneracao-ao-acionista/'},
)
_HOSTS = {'www.itau.com.br', 'www.bradescori.com.br', 'ri.bb.com.br', 'www.bb.com.br',
          'api.mziq.com', 'filemanager-cdn.mziq.com', 'cdn-sites-assets.mziq.com',
          'apicatalog.mziq.com', 's3.amazonaws.com'}


def _public_url(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.hostname in _HOSTS
            and not parts.username and not parts.password and parts.port in (None, 443)
            and not parts.fragment
            and not any(re.search(r'token|secret|password|auth|api.?key|signature', key, re.I)
                        for key, _ in parse_qsl(parts.query)))


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if not _public_url(newurl):
            raise ValueError('unapproved_primary_redirect')
        return super().redirect_request(request, response, code, message, headers, newurl)


def download_sources(folder, specs=BANK_PAGES, *, timeout=25, max_bytes=8_000_000):
    """Download <=12 explicitly discovered public RI URLs once; stop on 403/429.

    Additional CDN URLs must be linked by the official RI and carry a
    discovered_on URL in their spec. An allowed CDN host alone is not authority.
    Content validation/parsing is separate; HTTP 200 alone is not valid data.
    The returned manifest records failures without leaking exception bodies.
    """
    specs = list(specs)
    if len(specs) > 12 or not 0 < timeout <= 60 or not 0 < max_bytes <= 20_000_000:
        raise ValueError('invalid_research_download_limits')
    for spec in specs:
        if (not _public_url(spec['url']) or not re.fullmatch(r'[a-z][a-z0-9-]{0,70}', spec['label'])
                or spec['issuer'] not in ('itau', 'bradesco', 'bb')):
            raise ValueError('invalid_public_source_spec')
        if urlsplit(spec['url']).hostname in {'api.mziq.com', 'filemanager-cdn.mziq.com', 'cdn-sites-assets.mziq.com', 'apicatalog.mziq.com', 's3.amazonaws.com'}:
            origins = {'itau': {'www.itau.com.br'}, 'bradesco': {'www.bradescori.com.br'},
                       'bb': {'ri.bb.com.br', 'www.bb.com.br'}}
            if (not _public_url(spec.get('discovered_on', ''))
                    or urlsplit(spec['discovered_on']).hostname not in origins[spec['issuer']]):
                raise ValueError('official_discovery_reference_required')
        if 'request_json' in spec:
            if (urlsplit(spec['url']).hostname != 'apicatalog.mziq.com'
                    or not re.fullmatch(r'/filemanager/company/[a-f0-9-]{36}/filter/categories/year/meta', urlsplit(spec['url']).path)
                    or set(spec['request_json']) != {'year', 'categories', 'language', 'published'}):
                raise ValueError('only_public_read_only_catalogue_filters_allowed')
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_Redirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    manifest, stopped = [], False
    for spec in specs:
        receipt = {**spec, 'authority': 'primary_issuer_RI', 'http_status': None,
                   'retrieved_at': datetime.now(timezone.utc).isoformat(), 'status': 'not_fetched'}
        manifest.append(receipt)
        if stopped:
            receipt['status'] = 'stopped_after_access_response'
            continue
        try:
            data = json.dumps(spec['request_json']).encode('utf8') if 'request_json' in spec else None
            request = urllib.request.Request(spec['url'], data=data,
                headers={'User-Agent': 'RendaFutura-Academic/1.0', 'Accept-Encoding': 'identity',
                         **({'Content-Type': 'application/json'} if data else {})})
            with opener.open(request, timeout=timeout) as response:
                receipt.update(http_status=response.status, content_type=response.headers.get('Content-Type'),
                               final_url=response.url)
                body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError('response_size_limit')
            if not body:
                raise ValueError('empty_public_response')
            digest = hashlib.sha256(body).hexdigest()
            mime = receipt.get('content_type', '') or ''
            suffix = ('.xlsx' if body.startswith(b'PK') else '.pdf' if body.startswith(b'%PDF')
                      else '.xls' if body.startswith(bytes.fromhex('d0cf11e0'))
                      else '.js' if 'javascript' in mime else '.json' if 'json' in mime else '.html')
            filename = spec['label'] + '-' + digest[:20] + suffix
            path = root / filename
            try:
                with path.open('xb') as stream:
                    stream.write(body)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != body:
                    raise ValueError('immutable_cache_conflict') from None
            receipt.update(status='downloaded_unvalidated', file=filename, sha256=digest, bytes=len(body))
        except Exception as error:
            receipt['http_status'] = getattr(error, 'code', receipt['http_status'])
            receipt.update(status='download_failed', error_class=type(error).__name__)
            if getattr(error, 'reason', None) is not None:
                receipt['error_reason_class'] = type(error.reason).__name__
            stopped = receipt['http_status'] in (403, 429)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    with (root / ('manifest-' + stamp + '.json')).open('x', encoding='utf8') as stream:
        json.dump(manifest, stream, ensure_ascii=True, indent=2, allow_nan=False)
    return manifest


_BANKS = {'itau': (19348, {'ON': 'ITUB3', 'PN': 'ITUB4'}),
          'bradesco': (906, {'ON': 'BBDC3', 'PN': 'BBDC4'}), 'bb': (1023, {'ON': 'BBAS3'})}
_MONTHS = dict(zip(('janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho',
                   'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'), range(1, 13)))


def _fold(value):
    return ' '.join(unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower().split())


def _key(value):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', _fold(value)).split())


def _date(value):
    text = re.sub(r'(?<=\d)[o\u00ba\u00b0](?=\s*[./])', '', str(value).strip())
    text = re.sub(r'\s+', '', text).replace('.', '/')
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _number(value):
    try:
        result = float(_br_number(value))
        return result if result >= 0 and math.isfinite(result) else None
    except (ValueError, ArithmeticError):
        return None


def _base(content, issuer, as_of, observed_at, source_url, source_format):
    if issuer not in _BANKS or not _public_url(source_url):
        raise ValueError('unknown_bank_or_nonpublic_source_url')
    host = urlsplit(source_url).hostname
    expected = {'www.itau.com.br': 'itau', 'www.bradescori.com.br': 'bradesco',
                'ri.bb.com.br': 'bb', 'www.bb.com.br': 'bb'}.get(host)
    if expected is not None and expected != issuer:
        raise ValueError('issuer_source_host_mismatch')
    cutoff = datetime.strptime(str(as_of), '%Y-%m-%d').date()
    observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00')) if isinstance(observed_at, str) else observed_at
    if (not isinstance(observed, datetime) or observed.utcoffset() is None
            or observed > datetime.now(timezone.utc) or cutoff > observed.date()):
        raise ValueError('invalid_research_observation_context')
    body = content.encode('utf8') if isinstance(content, str) else content
    if not isinstance(body, bytes) or len(body) > 20_000_000:
        raise ValueError('bounded_source_bytes_required')
    return {'schema_version': 1, 'issuer': issuer, 'cvm': _BANKS[issuer][0], 'as_of': cutoff.isoformat(),
            'status': 'not_found', 'source_format': source_format, 'events': [], 'actions': [], 'issues': [],
            'field_status': {}, 'coverage': {},
            'source': {'url': source_url, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                       'retrieved_at': observed.isoformat(), 'authority': 'primary_issuer_RI'},
            'history_complete': False, 'financially_confirmed': False, 'cash_model_eligible': False}


def _finish(result):
    events = result['events']
    for field in ('record_date', 'payment_date', 'gross_per_share', 'net_per_share', 'published_at', 'share_basis_date'):
        count = sum(row.get(field) is not None for row in events)
        result['field_status'][field] = 'available' if events and count == len(events) else 'partial' if count else 'not_found'
    payment_dates = [row['payment_date'] for row in events if row['payment_date']]
    result['coverage'] = {'first_reported_payment': min(payment_dates, default=None),
                           'last_reported_payment': max(payment_dates, default=None),
                           'events': len(events), 'actions': len(result['actions']),
                           'payment_states': dict(Counter(row['status'] for row in events)),
                           'gross_amounts_known': sum(row['gross_per_share'] is not None for row in events),
                           'publication_dates_known': sum(row['published_at'] is not None for row in events),
                           'completeness_certified': False}
    if events or result['actions']:
        result['status'] = 'partial' if result['issues'] or any(row['gross_per_share'] is None for row in events) else 'available'
    return result


def _column(value):
    key = _key(value)
    if 'eua' in key or 'u s' in key or 'united states' in key:
        return None
    if key in ('tipo de rendimento', 'tipo de evento', 'provento', 'tipo', 'evento'):
        return 'label'
    if key in ('data base', 'data com', 'posicao acionaria brasil', 'posicao acionaria', 'data de registro'):
        return 'record_date'
    if key in ('data de pagamento brasil', 'data do pagamento brasil', 'data de pagamento', 'data do pagamento', 'pagamento'):
        return 'payment_date'
    if key in ('data ex', 'ex direito', 'data ex direito'):
        return 'ex_date'
    if key in ('data de publicacao', 'data de divulgacao'):
        return 'published_at'
    if key in ('status', 'situacao'):
        return 'reported_status'
    if key in ('data age', 'data aprovacao', 'data de aprovacao'):
        return 'approval_date'
    if key == 'bonificacao':
        return 'bonus_percentage'
    if key == 'custo unitario':
        return 'cost_per_new_share'
    if key in ('on', 'pn', 'on bruto', 'pn bruto', 'on liquido', 'pn liquido'):
        return 'amount_' + key[:2].upper()
    if key in ('bruto', 'valor bruto', 'valor bruto por acao'):
        return 'gross_per_share'
    if key in ('liquido', 'valor liquido', 'valor liquido por acao'):
        return 'net_per_share'
    if key in ('valor por acao', 'valor unitario'):
        return 'reported_per_share'
    return None


def _label(value):
    text = _fold(value)
    return ('JCP' if text.startswith(('jcp', 'jscp', 'juros sobre')) else
            'DIVIDENDO' if text.startswith(('dividendo', 'dividend')) else None)


def _event(result, share, label, record, payment, gross, net, reported, published, context_paid, locator):
    cvm, tickers = _BANKS[result['issuer']]
    status = ('announced' if payment and payment > result['as_of'] else
              'paid_reported' if payment and context_paid else 'past_scheduled' if payment else 'unresolved')
    return {'ticker': tickers.get(share), 'cvm': cvm, 'share_class': share, 'currency': 'BRL', 'label': label,
            'record_date': record, 'payment_date': payment, 'gross_per_share': gross, 'net_per_share': net,
            'reported_per_share': reported, 'published_at': published,
            'available_as_of': published <= result['as_of'] if published else None,
            'share_basis': 'reported_source_units_unverified', 'share_basis_date': None,
            'status': status, 'paid_evidence': context_paid, 'source_locator': locator,
            'source_ref': result['source']['sha256'], 'source_url': result['source']['url'],
            'observed_at': result['source']['retrieved_at'],
            'financially_confirmed': False, 'cash_model_eligible': False}


def inspect_bank_html(content, issuer, as_of, observed_at, *, source_url):
    """Read issuer HTML by semantic headers, not IDs or assumed net-to-gross.

    ON/PN columns are exact share-class amounts. Itaú single-value rows may be
    repeated for ITUB3/4 ONLY when the page explicitly states equal distribution
    to ordinary/preferred shareholders. US/ADR dates never replace Brazil dates.
    Missing publication/effective dates remain None. A heading must explicitly
    say pagos/realizados (not 'pagos e a pagar') to supply reported-paid context.
    A generic calendar/history heading or mere passage of time is insufficient.
    """
    result = _base(content, issuer, as_of, observed_at, source_url, 'issuer_ri_html')
    try:
        tree = html.fromstring(content.decode('utf8') if isinstance(content, bytes) else content,
                               parser=html.HTMLParser(no_network=True))
    except (ValueError, TypeError, etree.ParserError):
        result.update(status='invalid', issues=[{'code': 'invalid_html'}])
        return result
    text = _fold(tree.text_content())
    equal_classes = ('igualmente distribuidos para os acionistas ordinaristas e preferencialistas' in text
                     or 'igualmente distribuidos para acoes ordinarias e preferenciais' in text)
    seen = set()
    for table_index, table in enumerate(tree.xpath('//table'), 1):
        try:
            rows = list(_table_rows(table))
        except ValueError:
            result['issues'].append({'code': 'invalid_table_span', 'table': table_index})
            continue
        columns = None
        headers = None
        start = None
        for index, row in enumerate(rows[:6]):
            mapping = {}
            duplicate = False
            for column, value in enumerate(row):
                key = _column(value)
                if key in mapping and key is not None:
                    duplicate = True
                if key:
                    mapping[key] = column
            if not duplicate and ('bonus_percentage' in mapping or 'label' in mapping and 'payment_date' in mapping):
                columns, headers, start = mapping, row, index + 1
        if columns is None:
            continue
        headings = table.xpath('preceding::*[self::h1 or self::h2 or self::h3 or self::h4][1]')
        heading = _fold(headings[0].text_content()) if headings else ''
        paid_context = ('publisher_paid_history_heading' if re.search(r'\bpagos\b|pagamentos realizados', heading)
                        and not re.search(r'a pagar|previst|anunciad|nao pagos|nao realizados', heading) else None)
        for row_index, row in enumerate(rows[start:], start + 1):
            def get(key):
                column = columns.get(key)
                return row[column] if column is not None and column < len(row) else ''

            if not row or all(not value for value in row) or len(set(row)) == 1 and re.fullmatch(r'\d{4}', row[0]):
                continue
            locator = {'table': table_index, 'row': row_index}
            if 'bonus_percentage' in columns:
                percent = _number(str(get('bonus_percentage')).replace('%', '').strip())
                if percent is None:
                    result['issues'].append({'code': 'bonus_percentage_unresolved', **locator})
                    continue
                result['actions'].append({'type': 'bonus', 'record_date': _date(get('record_date')),
                    'approval_date': _date(get('approval_date')), 'effective_date': None, 'published_at': None,
                    'bonus_percent': percent, 'new_shares_per_old_share': percent / 100,
                    'post_to_pre_share_ratio': 1 + percent / 100,
                    'cost_per_new_share': _number(get('cost_per_new_share')), 'currency': 'BRL',
                    'share_class': None, 'class_scope': 'issuer_table_does_not_specify_classes',
                    'source_ref': result['source']['sha256'], 'source_locator': locator,
                    'cash_income': False, 'normalization_ready': False,
                    'field_status': {'effective_date': 'not_found', 'published_at': 'not_found', 'share_class': 'not_found'}})
                continue
            if _key(get('label')).startswith('total'):
                result['issues'].append({'code': 'aggregate_not_payment', **locator})
                continue
            label, record, payment = _label(get('label')), _date(get('record_date')), _date(get('payment_date'))
            if label is None:
                result['issues'].append({'code': 'income_label_unresolved', **locator})
                continue
            reported_status = _key(get('reported_status'))
            explicit_paid = reported_status in ('pago', 'paga', 'pagos', 'paid')
            context = 'explicit_row_status' if explicit_paid else None if reported_status else paid_context
            shares = [share for share in _BANKS[issuer][1] if 'amount_' + share in columns]
            if not shares:
                shares = list(_BANKS[issuer][1]) if issuer != 'itau' or equal_classes else [None]
            for share in shares:
                gross, net = _number(get('gross_per_share')), _number(get('net_per_share'))
                reported = _number(get('amount_' + share)) if share and 'amount_' + share in columns else _number(get('reported_per_share'))
                if share and 'amount_' + share in columns:
                    header = _key(headers[columns['amount_' + share]])
                    gross = reported if 'bruto' in header else None
                    net = reported if 'liquido' in header else None
                event = _event(result, share, label, record, payment, gross, net, reported,
                               _date(get('published_at')), context, locator)
                if reported_status in ('cancelado', 'cancelada', 'cancelled'):
                    event['status'] = 'cancelled'
                if record and payment and payment < record:
                    result['issues'].append({'code': 'payment_precedes_record', **locator})
                    event['status'] = 'unresolved'
                if not payment:
                    result['issues'].append({'code': 'payment_date_unresolved', **locator})
                signature = tuple(event[key] for key in ('ticker', 'label', 'record_date', 'payment_date', 'gross_per_share', 'net_per_share', 'reported_per_share'))
                if signature in seen:
                    result['issues'].append({'code': 'duplicate_payment_row', **locator})
                    continue
                seen.add(signature)
                result['events'].append(event)
    return _finish(result)


def inspect_bank_pdf(content, issuer, as_of, observed_at, *, source_url, pages=(1,)):
    """Sniff %PDF irrespective of HTTP MIME; inspect <=6 explicitly selected pages.

    Supported proven layouts: Bradesco 2026 monthly notice (declared gross/net,
    exact calendar and signature publication); BB annual-letter split footnote.
    No entire unknown-document crawl, OCR guess, formula evaluation, or paid
    inference. BB page 38 is explicit at call time, not an assumed global layout.
    """
    import pdfplumber

    result = _base(content, issuer, as_of, observed_at, source_url, 'issuer_ri_pdf')
    if (not content.startswith(b'%PDF') or not isinstance(pages, (tuple, list)) or not 1 <= len(pages) <= 6
            or any(type(page) is not int or page <= 0 for page in pages)):
        raise ValueError('invalid_bounded_PDF_selection')
    with pdfplumber.open(io.BytesIO(content)) as document:
        if any(page > len(document.pages) for page in pages):
            raise ValueError('PDF_page_not_present')
        texts = [(number, document.pages[number - 1].extract_text() or '') for number in pages]
    result['pages_inspected'] = list(pages)
    for number, text in texts:
        folded = _fold(text)
        if issuer == 'bb':
            compact = folded.replace(' ', '')
            if 'bancodobrasil' not in compact:
                continue
            for match in re.finditer(r'em(\d{2}\.\d{2}\.\d{4}),ocorreuodesdobramentodasacoesnaproporcaode(\d+):(\d+)', compact):
                old, new = int(match[2]), int(match[3])
                if old <= 0 or new <= 0:
                    continue
                result['actions'].append({'type': 'split', 'ticker': 'BBAS3', 'share_class': 'ON',
                    'effective_date': _date(match[1]), 'record_date': None, 'published_at': None,
                    'old_shares': old, 'new_shares': new, 'post_to_pre_share_ratio': new / old,
                    'cash_income': False, 'source_ref': result['source']['sha256'],
                    'source_locator': {'page': number}, 'normalization_ready': False,
                    'field_status': {'effective_date': 'available', 'published_at': 'not_found'}})
        if issuer != 'bradesco' or 'banco bradesco' not in folded or 'novos valores previstos a serem pagos' not in folded:
            continue
        boundary = folded.find('que, liquidos do imposto de renda')
        if boundary < 0:
            result['issues'].append({'code': 'gross_net_declaration_not_identified', 'page': number})
            continue
        pairs = {}
        for share, word in (('ON', 'ordinaria'), ('PN', 'preferencial')):
            pattern = r'r\$\s*([\d.,]+)\s+por acao\s+' + word
            gross = re.search(pattern, folded[:boundary])
            net = re.search(pattern, folded[boundary:])
            pairs[share] = (_number(gross[1]) if gross else None, _number(net[1]) if net else None)
        dates = [(int(day), _MONTHS.get(month), int(year)) for day, month, year in
                 re.findall(r'\b(\d{1,2}) de ([a-z]+) de (20\d{2})\b', folded)]
        publication = None
        if len(set(dates)) == 1:
            day, month, year = dates[0]
            if month:
                publication = _date(f'{day}/{month}/{year}')
        date_pattern = r'\d{1,2}(?:[o\u00ba\u00b0])?\s*[./]\s*\d{1,2}\s*[./]\s*\d{4}'
        for line_index, line in enumerate(text.splitlines(), 1):
            normalized = _fold(line)
            if not any(normalized.startswith(month + ' ') for month in _MONTHS):
                continue
            row_dates = re.findall(date_pattern, normalized)
            if len(row_dates) != 3:
                continue
            record, ex_date, payment = map(_date, row_dates)
            for share, (gross, net) in pairs.items():
                event = _event(result, share, 'JCP', record, payment, gross, net, gross,
                               publication, None, {'page': number, 'line': line_index})
                event.update(ex_date=ex_date, amount_evidence='declared_amount_before_explicit_withholding',
                             declaration_only=True)
                result['events'].append(event)
    return _finish(result)


def match_payment_declarations(history, declarations):
    """Exact class/label/record/payment match supplies fields, NEVER paid status.

    Missing or conflicting amounts remain unresolved. This can also accept the
    parent's normalized CVM declarations when they expose the same event keys.
    Publication here is declaration availability, not payment-status availability.
    """
    result = deepcopy(history)
    result['declaration_sources'] = [declarations['source']]
    groups = {}
    for item in declarations['events']:
        key = tuple(item.get(field) for field in ('ticker', 'cvm', 'share_class', 'label', 'record_date', 'payment_date'))
        groups.setdefault(key, []).append(item)
    for event in result['events']:
        key = tuple(event.get(field) for field in ('ticker', 'cvm', 'share_class', 'label', 'record_date', 'payment_date'))
        matches = groups.get(key, [])
        if len(matches) != 1 or any(value is None for value in key):
            continue
        declared = matches[0]
        gross, reported = declared.get('gross_per_share'), event.get('reported_per_share')
        if gross is None or reported is None:
            continue
        if not math.isclose(gross, reported, rel_tol=1e-9, abs_tol=1e-10):
            result['issues'].append({'code': 'declaration_amount_disagrees_with_RI', 'ticker': event['ticker'],
                                      'record_date': event['record_date'], 'payment_date': event['payment_date']})
            continue
        event.update(gross_per_share=gross, net_per_share=declared.get('net_per_share'),
                     declaration_source_ref=declared['source_ref'])
        if event['published_at'] is None:
            event['published_at'] = declared.get('published_at')
            event['available_as_of'] = event['published_at'] <= result['as_of'] if event['published_at'] else None
        event['cash_model_eligible'] = False
    return _finish(result)


def write_research_ledger(folder, results, *, discoveries=(), declarations=(), sources=()):
    """Write a hash-addressed JSON research ledger, never an active-model file."""
    payload = {'schema_version': 1, 'scope': 'primary_bank_research_not_model_approval',
               'results': list(results), 'discoveries': list(discoveries),
               'declaration_documents': list(declarations), 'sources': list(sources),
               'cash_model_eligible': False, 'financially_confirmed': False}
    events = [row for result in payload['results'] for row in result['events']]
    actions = [row for result in payload['results'] for row in result['actions']]
    payload['summary'] = {'calendar_events': len(events), 'actions': len(actions),
        'gross_amounts_known': sum(row['gross_per_share'] is not None for row in events),
        'publication_dates_known': sum(row['published_at'] is not None for row in events),
        'payment_states': dict(Counter(row['status'] for row in events)),
        'supporting_declaration_rows_not_additional_cash': sum(len(item['events']) for item in payload['declaration_documents'])}
    content = json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode('utf8')
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ('ledger-' + hashlib.sha256(content).hexdigest()[:20] + '.json')
    try:
        with path.open('xb') as stream:
            stream.write(content)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError('immutable_research_ledger_conflict') from None
    return path


def parse_research_downloads(folder, as_of):
    """Validate downloaded hashes, parse known layouts and freeze one JSON.

    folder is the explicit raw/primary-research/banks directory. Source filenames
    in the returned payload are relative to the market-data folder. No network.
    Only downloaded Bradesco HTML/notice and BB annual-letter page 38 are parsed;
    failed RI acquisition never becomes zero-payment coverage. Supporting
    declarations stay in declaration_documents, not a duplicate events list.
    """
    root = Path(folder).resolve()
    receipts = []
    for manifest in sorted(root.glob('manifest-*.json')):
        if manifest.is_symlink():
            raise ValueError('unsafe_manifest_path')
        rows = json.loads(manifest.read_text(encoding='utf8'))
        if not isinstance(rows, list):
            raise ValueError('invalid_download_manifest')
        receipts.extend(rows)
    selected = {}
    sources = []
    for receipt in receipts:
        item = dict(receipt)
        if item.get('file'):
            name = item['file']
            if not re.fullmatch(r'[a-z0-9-]+\.(?:html|pdf|js|json|xlsx|xls)', name):
                raise ValueError('unsafe_download_filename')
            path = root / name
            if path.is_symlink() or path.resolve().parent != root or path.stat().st_size > 20_000_000:
                raise ValueError('unsafe_download_file')
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != item['sha256']:
                raise ValueError('research_source_hash_mismatch')
            item['file'] = 'raw/primary-research/banks/' + name
            if item['label'] in ('bradesco-payments-page', 'bradesco-monthly-jcp-notice', 'bb-annual-letter-2025'):
                selected[item['label']] = item, body
        sources.append(item)
    parsed = {}
    for label, (source, content) in selected.items():
        args = content, source['issuer'], as_of, source['retrieved_at']
        if label == 'bradesco-payments-page':
            result = inspect_bank_html(*args, source_url=source['url'])
        else:
            result = inspect_bank_pdf(*args, source_url=source['url'], pages=(38,) if source['issuer'] == 'bb' else (1,))
        result['source']['file'] = source['file']
        parsed[label] = result
    history = parsed.get('bradesco-payments-page')
    notice = parsed.get('bradesco-monthly-jcp-notice')
    if history is not None and notice is not None:
        history = match_payment_declarations(history, notice)
    results = [result for result in (history, parsed.get('bb-annual-letter-2025')) if result is not None]
    declarations = [notice] if notice is not None else []
    failures = [{'issuer': item['issuer'], 'url': item['url'], 'http_status': item['http_status'],
                 'status': item['status'], 'error_class': item.get('error_class'),
                 'error_reason_class': item.get('error_reason_class'), 'retrieved_at': item['retrieved_at']}
                for item in sources if item['status'] == 'download_failed']
    output = write_research_ledger(root, results, declarations=declarations, sources=sources,
                                  discoveries=[{'kind': 'acquisition_failures_not_zero_income', 'attempts': failures}])
    return output, json.loads(output.read_text(encoding='utf8'))
