"""Bounded alternate issuer-history research. Default execution is offline.

Writes are confined to CACHE; no exchange requests, pointers or model writes.
Issuer-hosted vendor quotes retain their vendor authority. A primary schedule
is retrospective evidence, never proof of settlement or publication timing.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from copy import deepcopy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import unquote, urljoin, urlsplit
import zipfile

from lxml import html

from . import cash_calendar_research as calendars
from .collect_market_data import month_end, shift_month


CACHE = 'raw/primary-research/alternate-issuer-history'
MAX_REQUESTS = 20
MAX_BYTES = 24_000_000
USER_AGENT = 'RendaFutura-Academic/1.0'
CUTOFF = date(2026, 9, 30)
IDENTITIES = {
    'ITSA3': (7617, 'ON', 'BRITSAACNOR0', 'itausa'),
    'ITSA4': (7617, 'PN', 'BRITSAACNPR7', 'itausa'),
    'EGIE3': (17329, 'ON', 'BREGIEACNOR9', 'engie'),
    'FESA3': (3069, 'ON', 'BRFESAACNOR8', 'ferbasa'),
    'FESA4': (3069, 'PN', 'BRFESAACNPR5', 'ferbasa'),
}
DENIED = ('b3.com.br', 'bmfbovespa.com.br', 'bovespa.com.br', 'bvmf.com.br',
          'ri.azzas2154.com.br', 'allos.com.br', 'santander.com.br')


def dump(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                      allow_nan=False).encode('utf-8')


def sha(body):
    return hashlib.sha256(body).hexdigest()


def safe_path(root, name):
    if (not isinstance(name, str) or not name.startswith('raw/') or '\\' in name
            or ':' in name or '..' in name.split('/')):
        raise ValueError('unsafe_raw_cache_reference')
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError('unsafe_raw_cache_reference')
    if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(root)):
        raise ValueError('symlink_raw_cache_reference')
    return path


def save(root, prefix, body, extension='json'):
    name = f'{CACHE}/{prefix}-{sha(body)}.{extension}'
    path = safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_research_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)
    return name


def approved(url):
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        decoded = unquote(unquote(url)).lower()
        return (parts.scheme == 'https' and not parts.username and not parts.password
                and parts.port in (None, 443) and not parts.fragment
                and '.' in host and not re.fullmatch(r'[\d.]+', host)
                and not any(host == denied or host.endswith('.' + denied) for denied in DENIED)
                and not any(denied in decoded for denied in DENIED)
                and not any(word in host for word in ('proxy', 'yahoo', 'b3.', 'b3-')))
    except ValueError:
        return False


class Reader:
    """Read original bytes and verify any supplied receipt before using evidence."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.sources = {}

    def read(self, receipt, role='research_original'):
        name = receipt['file']
        path = safe_path(self.root, name)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('original_size_limit')
        body = path.read_bytes()
        if sha(body) != receipt['sha256'] or ('bytes' in receipt and len(body) != receipt['bytes']):
            raise ValueError('original_content_hash_mismatch')
        existing = self.sources.get(name)
        if existing and existing['sha256'] != sha(body):
            raise ValueError('original_changed_during_build')
        stored = self.sources.setdefault(name, {**receipt, 'bytes': len(body), 'roles': [],
            'hash_basis': receipt.get('hash_basis', 'actual_original_bytes'),
            'original_receipt_verified': receipt.get('original_receipt_verified', True)})
        if role not in stored['roles']:
            stored['roles'].append(role)
        return body

    def receipts(self):
        # Only public research originals; never scan private reference projects.
        base = self.root / 'raw/primary-research'
        for path in sorted(base.rglob('*.json')):
            if path.stat().st_size > 2_000_000:
                continue
            try:
                obj = json.loads(path.read_bytes())
            except (ValueError, OSError):
                continue
            if (isinstance(obj, dict) and obj.get('url') and obj.get('file')
                    and obj.get('sha256') and isinstance(obj.get('bytes'), int)):
                yield obj

    def cached(self, url):
        matches = [r for r in self.receipts() if r['url'] == url]
        if not matches:
            return None
        receipt = max(matches, key=lambda r: r.get('retrieved_at', ''))
        return self.read(receipt), receipt


def discover(body, receipt):
    """Return literal links only, keeping the exact discovery bytes and locator."""
    if sha(body) != receipt['sha256'] or len(body) != receipt['bytes']:
        raise ValueError('discovery_source_hash_mismatch')
    text = body.decode('utf-8-sig', errors='replace')
    tree = html.fromstring(text, parser=html.HTMLParser(no_network=True))
    links = []
    for tag, attribute in (('a', 'href'), ('iframe', 'src'), ('script', 'src'), ('form', 'action')):
        for index, node in enumerate(tree.xpath(f'//{tag}[@{attribute}]'), 1):
            raw = node.get(attribute)
            if not raw or raw.startswith(('#', 'javascript:', 'mailto:')):
                continue
            url = urljoin(receipt['url'], raw)
            links.append({'url': url, 'label': ' '.join(node.text_content().split())[:180],
                'locator': f'//{tag}[@{attribute}][{index}]/@{attribute}',
                'discovered_in': receipt['file'], 'discovery_sha256': receipt['sha256'],
                'network_permitted': approved(url)})
    # JS endpoints must be literal URLs, not guessed paths, tokens or API keys.
    for index, match in enumerate(re.finditer(r'''https?://[^\s<>"'`\\]+''', text), 1):
        url = match.group().replace('&amp;', '&')
        links.append({'url': url, 'label': 'literal_embedded_URL',
            'locator': f'UTF8_text_literal_URL[{index}]', 'discovered_in': receipt['file'],
            'discovery_sha256': receipt['sha256'], 'network_permitted': approved(url)})
    unique = {}
    for link in links:
        unique.setdefault(link['url'], link)
    return list(unique.values())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class Fetcher:
    """Persistent twenty-attempt budget. Every wire request has a prior receipt."""

    def __init__(self, root):
        self.reader = Reader(root)
        self.root = self.reader.root
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def history(self, prefix):
        result = []
        for path in (self.root / CACHE).glob(prefix + '-*.json'):
            body = path.read_bytes()
            if path.stem != prefix + '-' + sha(body):
                raise ValueError('request_ledger_hash_mismatch')
            result.append(json.loads(body))
        return result

    def _request(self, url, discovery):
        if not approved(url):
            raise ValueError('forbidden_origin_no_request_sent')
        if any(r.get('http_status') in (401, 403, 429) for r in self.history('response')):
            raise ValueError('access_stop_no_alternate_route_or_request')
        prior = self.history('request')
        if url in {r['url'] for r in prior}:
            raise ValueError('already_attempted_no_retry')
        if len(prior) >= MAX_REQUESTS:
            raise ValueError('twenty_public_request_budget_exhausted')
        request = {'ordinal': len(prior) + 1, 'url': url, 'discovery': discovery,
            'started_at': datetime.now(timezone.utc).isoformat(), 'proxy': False,
            'redirects': False, 'automatic_retries': 0}
        save(self.root, 'request', dump(request))
        body, status, content_type, error, location = b'', None, None, None, None
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                'Accept': '*/*', 'Accept-Encoding': 'identity'})
            with self.opener.open(req, timeout=25) as response:
                status = response.status
                content_type = response.headers.get('Content-Type')
                location = response.headers.get('Location')
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status, error = exc.code, 'HTTPError'
            content_type = exc.headers.get('Content-Type')
            location = exc.headers.get('Location')
            body = exc.read(MAX_BYTES + 1)
        except (OSError, ValueError) as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        receipt = {**request, 'http_status': status, 'content_type': content_type,
            'error': error, 'location': location, 'bytes': len(body), 'sha256': sha(body),
            'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'file': save(self.root, 'body', body, 'bin')}
        save(self.root, 'response', dump(receipt))
        if status in (401, 403, 429):
            raise ValueError(f'access_stop_HTTP_{status}_no_alternate_route')
        if len(body) > MAX_BYTES:
            raise ValueError('response_size_limit')
        return body, receipt

    def get(self, url, discovered_in):
        if not approved(url):
            raise ValueError('forbidden_origin_no_request_sent')
        original = json.loads(safe_path(self.root, discovered_in).read_bytes())
        if url not in {link['url'] for link in discover(self.reader.read(original), original)}:
            raise ValueError('literal_public_link_discovery_required')
        return self._get_verified(url, {'file': original['file'], 'sha256': original['sha256'],
                                       'receipt': discovered_in})

    def _get_verified(self, url, discovery):
        if not approved(url):
            raise ValueError('forbidden_origin_no_request_sent')
        cached = self.reader.cached(url)
        if cached:
            if cached[1].get('http_status') in (401, 403, 429):
                raise ValueError('cached_access_denial_no_alternate_route')
            return cached
        parts = urlsplit(url)
        robots_url = f'{parts.scheme}://{parts.netloc}/robots.txt'
        robots = self.reader.cached(robots_url)
        if robots is None:
            robots = self._request(robots_url, {'role': 'robots_for_discovered_origin',
                                              'discovered_url': url})
        robots_body, robots_receipt = robots
        status = robots_receipt.get('http_status', 200)
        if status == 200:
            policy = urllib.robotparser.RobotFileParser()
            policy.parse(robots_body.decode('utf-8', errors='replace').splitlines())
            if not policy.can_fetch(USER_AGENT, url):
                raise ValueError('robots_disallows_no_request_sent')
        elif status not in (404, 410):
            raise ValueError('robots_unavailable_or_denied_no_request_sent')
        return self._request(url, discovery)

    def get_official_site(self, cvm):
        sites = fca_sites(self.reader)
        matches = [row for row in sites if row['cvm'] == cvm]
        if len(matches) != 1:
            raise ValueError('exact_official_FCA_website_required')
        return self._get_verified(matches[0]['url'], matches[0])

    def get_ipe(self, url):
        documents, _ = ipe_documents(self.reader)
        matches = [row for row in documents if row['url'] == url]
        if len(matches) != 1:
            raise ValueError('exact_native_IPE_link_required')
        return self._get_verified(url, matches[0])


def fca_sites(reader):
    from .market_inventory import load_inventory
    inventory = load_inventory(reader.root)
    sources = [s for s in inventory['sources'] if s.get('url') ==
        'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip']
    if len(sources) != 1:
        raise ValueError('official_FCA_source_missing_or_ambiguous')
    source, = sources
    body = reader.read(source, 'official_FCA_issuer_website_discovery')
    member = 'fca_cia_aberta_geral_2026.csv'
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if archive.getinfo(member).file_size > 8_000_000:
            raise ValueError('FCA_member_size_limit')
        rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
        sites = []
        for line, row in enumerate(rows, 2):
            if int(row['Codigo_CVM']) not in {i[0] for i in IDENTITIES.values()}:
                continue
            page = row['Pagina_Web'].strip()
            url = page if page.startswith('https://') else 'https://' + page if '://' not in page else None
            if url and approved(url):
                sites.append({'cvm': int(row['Codigo_CVM']), 'url': url,
                    'reported_webpage': page, 'scheme_policy': 'HTTPS_only_no_http_fallback',
                    'file': source['file'], 'sha256': source['sha256'], 'bytes': source['bytes'],
                    'member': member, 'csv_line': line, 'filing_id': row['ID_Documento']})
    return sites


def native_cash(reader, ticker, cutoff=CUTOFF):
    issuer = IDENTITIES[ticker][3]
    if issuer not in calendars.SOURCES:
        return None
    matches = [r for r in reader.receipts() if r.get('issuer') == issuer
               and r['url'] == calendars.SOURCES[issuer]['url']]
    if not matches:
        return None
    source = max(matches, key=lambda r: r.get('retrieved_at', ''))
    body = reader.read(source, 'native_issuer_cash_calendar')
    result = calendars.inspect_calendar(body, issuer, ticker, cutoff, source)
    return {**result, 'source': source}


def exact_identity(stock):
    ticker = stock.get('ticker')
    if ticker not in IDENTITIES:
        raise ValueError('unsupported_exact_security')
    if tuple(stock.get(key) for key in ('cvm', 'share_class', 'isin')) != IDENTITIES[ticker][:3]:
        raise ValueError('exact_CVM_class_ISIN_mismatch')
    return {key: stock[key] for key in ('ticker', 'cvm', 'share_class', 'isin')}


def positive(value):
    if isinstance(value, bool):
        raise ValueError('invalid_positive_decimal')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid_positive_decimal') from None
    if not number.is_finite() or number <= 0:
        raise ValueError('invalid_positive_decimal')
    return number


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('exact_readable_date_required')
    return date.fromisoformat(value)


def validate_cash_events(stock, events, cutoff=CUTOFF):
    """Validate semantics before any payment-date observations are constructed.

    The full native table must be supplied. This function never claims calendar
    completeness from count, recent payments, or the first distribution date.
    """
    identity = exact_identity(stock)
    start = shift_month(cutoff, -60)
    selected, future, previous, keys = [], [], [], set()
    for original in events:
        event = deepcopy(original)
        if any(event.get(k) != identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')):
            raise ValueError('cash_exact_CVM_class_ISIN_mismatch')
        if event.get('payment_date_role') != 'payment' or event.get('record_date_role') != 'entitlement_record':
            raise ValueError('cash_date_role_mismatch')
        payment, record = iso_day(event.get('payment_date')), iso_day(event.get('record_date'))
        if payment < record:
            raise ValueError('payment_precedes_entitlement_record')
        if event.get('approval_date') and iso_day(event['approval_date']) > payment:
            raise ValueError('payment_precedes_approval')
        if event.get('label') not in ('JCP', 'DIVIDENDO'):
            raise ValueError('cash_kind_unspecified')
        basis = event.get('amount_tax_basis')
        if (basis != 'explicit_gross' and not
                (event['label'] == 'DIVIDENDO' and basis == 'dividend_per_share')):
            raise ValueError('cash_gross_tax_basis_unresolved')
        if event.get('gross_reconstructed_from_net'):
            raise ValueError('cash_net_to_gross_inference_forbidden')
        positive(event.get('gross_per_share_decimal'))
        if event.get('issues'):
            raise ValueError('unresolved_primary_cash_row')
        key = (event['label'], event['record_date'], event['payment_date'], event['gross_per_share_decimal'])
        if key in keys:
            raise ValueError('duplicate_primary_cash_installment')
        keys.add(key)
        if payment > cutoff:
            future.append(event)
        elif payment < start:
            previous.append(event)
        else:
            selected.append(event)
    if not selected or not previous:
        raise ValueError('pre_window_primary_cash_evidence_missing')
    return {'events': selected, 'pre_window_events': previous, 'future_events': future,
        'window_start': start.isoformat(), 'window_end': cutoff.isoformat(),
        'cash_history_complete': False, 'share_basis_verified': False,
        'point_in_time_archive_verified': False, 'settlement_verified': False,
        'observation_basis': 'retrospective_primary_payment_schedule'}


def yahoo_daily(body, ticker, cutoff=CUTOFF):
    """Retain reported close and every split, without guessing its price basis."""
    payload = json.loads(body)
    chart = payload['chart']
    if chart.get('error') or len(chart.get('result', [])) != 1:
        raise ValueError('ambiguous_Yahoo_series')
    result = chart['result'][0]
    if result['meta'].get('symbol') != ticker + '.SA' or result['meta'].get('currency') != 'BRL':
        raise ValueError('Yahoo_exact_security_or_currency_mismatch')
    stamps, values = result['timestamp'], result['indicators']['quote'][0]['close']
    if len(stamps) != len(values):
        raise ValueError('Yahoo_series_length_mismatch')
    daily, seen = {}, set()
    for stamp, value in zip(stamps, values):
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError('invalid_Yahoo_timestamp')
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        if day in seen:
            raise ValueError('duplicate_Yahoo_trading_day')
        seen.add(day)
        if value is not None and day <= cutoff:
            daily[day] = positive(value)
    actions = []
    for key, action in result.get('events', {}).get('splits', {}).items():
        if int(key) != action['date']:
            raise ValueError('Yahoo_split_timestamp_mismatch')
        day = datetime.fromtimestamp(action['date'], timezone.utc).date()
        ratio = positive(action['numerator']) / positive(action['denominator'])
        if day > cutoff:
            raise ValueError('post_cutoff_split_requires_separate_reconciliation')
        actions.append({'date': day.isoformat(), 'ratio_decimal': str(ratio),
            'source_locator': 'chart.result[0].events.splits.' + key,
            'primary_action_verified': False})
    if len({a['date'] for a in actions}) != len(actions):
        raise ValueError('duplicate_share_action_day')
    monthly = {}
    for day, value in sorted(daily.items()):
        monthly[month_end(day.year, day.month)] = (day, value)
    months = sorted(shift_month(cutoff, -offset) for offset in range(61))
    if any(month not in monthly for month in months):
        raise ValueError('less_than_61_consecutive_monthly_anchors')
    if any((month - monthly[month][0]).days > 7 for month in months):
        raise ValueError('stale_month_end_anchor')
    return daily, {month: monthly[month] for month in months}, sorted(actions, key=lambda a: a['date'])


def factor_after(day, actions, cutoff):
    factor = Decimal(1)
    for action in actions:
        if day < iso_day(action['date']) <= cutoff:
            factor *= positive(action['ratio_decimal'])
    return positive(factor)


def inspect_price_window(body, ticker, cutoff=CUTOFF):
    _, monthly, actions = yahoo_daily(body, ticker, cutoff)
    regimes = {}
    for month, (day, value) in monthly.items():
        factor = format(factor_after(day, actions, cutoff).normalize(), 'f')
        regimes.setdefault(factor, []).append(day.isoformat())
    return {'monthly_anchors': len(monthly), 'first_month': min(monthly).isoformat(),
        'last_month': max(monthly).isoformat(), 'reported_share_actions': actions,
        'required_regimes': [{'factor_after': factor, 'first_used_trade': min(days),
            'last_used_trade': max(days)} for factor, days in regimes.items()],
        'minimum_independent_raw_anchors': max(4, len(regimes)),
        'raw_price_basis_verified': False, 'candidate_eligible': False}


def _literal_documentation(reader, evidence):
    if not approved(evidence['source'].get('url', '')):
        raise ValueError('provider_documentation_origin_forbidden')
    body = reader.read(evidence['source'], 'quote_provider_documentation')
    literal = evidence.get('literal')
    if not isinstance(literal, str) or not literal.strip():
        raise ValueError('literal_provider_documentation_required')
    text = body.decode('utf-8-sig')
    if 'html' in evidence['source'].get('content_type', ''):
        text = html.fromstring(text, parser=html.HTMLParser(no_network=True)).text_content()
    if literal not in text:
        raise ValueError('provider_documentation_literal_not_found')
    return literal


def parse_raw_quote_csv(reader, stock, source, contract):
    """Offline, explicit schema adapter for a publisher's original CSV export.

    The contract must be reviewed against actual publisher documentation; it is
    not discovered from a convenient column name. The export is never labelled
    exchange-certified. No live provider contract is asserted by this module.
    """
    identity = exact_identity(stock)
    if not approved(source.get('url', '')):
        raise ValueError('independent_quote_origin_forbidden')
    provider = contract.get('provider')
    origin = _literal_documentation(reader, contract['origin_documentation'])
    basis = _literal_documentation(reader, contract['basis_documentation'])
    if (not isinstance(provider, str) or not provider.strip() or provider not in origin
            or any(word in origin.lower() for word in ('yahoo', 'yfinance'))):
        raise ValueError('independent_upstream_origin_not_documented')
    if not any(phrase in calendars._norm(basis) for phrase in
               ('sem ajuste', 'nao ajustad', 'unadjusted', 'not adjusted')):
        raise ValueError('raw_unadjusted_price_basis_not_documented')
    if contract.get('currency') != 'BRL' or contract.get('date_role') != 'trade':
        raise ValueError('quote_currency_or_date_role_mismatch')
    if contract.get('date_format') not in ('%d/%m/%Y', '%Y-%m-%d'):
        raise ValueError('explicit_quote_date_format_required')
    if contract.get('decimal') not in ('.', ',') or contract.get('delimiter') not in (',', ';', '\t'):
        raise ValueError('explicit_quote_numeric_format_required')
    columns = contract['columns']
    if not {'ticker', 'date', 'raw_close'}.issubset(columns):
        raise ValueError('exact_quote_column_mapping_required')
    if columns['raw_close'] not in basis or 'BRL' not in basis:
        raise ValueError('raw_quote_column_and_BRL_basis_not_documented')
    body = reader.read(source, 'original_independent_raw_quote_CSV')
    rows = csv.DictReader(io.StringIO(body.decode(contract.get('encoding', 'utf-8-sig'))),
                          delimiter=contract['delimiter'])
    if len(set(columns.values())) != len(columns) or not set(columns.values()).issubset(rows.fieldnames or []):
        raise ValueError('quote_export_schema_mismatch')
    quotes, seen = [], set()
    for line, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError('misaligned_quote_CSV_row')
        if row[columns['ticker']] != identity['ticker']:
            continue
        for key in ('isin', 'share_class', 'cvm'):
            if key in columns and row[columns[key]] != str(identity[key]):
                raise ValueError('quote_exact_CVM_class_ISIN_mismatch')
        raw_day = row[columns['date']]
        day = datetime.strptime(raw_day, contract['date_format']).date()
        if day.strftime(contract['date_format']) != raw_day:
            raise ValueError('ambiguous_quote_trade_date')
        if day in seen:
            raise ValueError('duplicate_independent_quote_date')
        seen.add(day)
        raw_value = row[columns['raw_close']]
        separator = re.escape(contract['decimal'])
        if not re.fullmatch(r'\d+(?:' + separator + r'\d+)?', raw_value):
            raise ValueError('ambiguous_independent_raw_price')
        value = positive(raw_value.replace(contract['decimal'], '.'))
        quotes.append({'date': day.isoformat(), 'raw_close_decimal': str(value),
                       'csv_line': line, 'raw_row': row})
    if not quotes:
        raise ValueError('no_quotes_for_exact_security')
    return {'identity': identity, 'provider': provider, 'source': source,
        'contract': deepcopy(contract), 'quotes': quotes, 'currency': 'BRL',
        'price_basis': 'publisher_documented_unadjusted',
        'authority': 'public_provider_not_exchange_certified', 'independent_of_Yahoo': True,
        'documentation_contract_requires_review': True}


def reconcile_raw_quote_export(reader, stock, yahoo_source, csv_source, contract, cutoff=CUTOFF):
    """Reparse hashed originals and apply four-anchor/every-regime checks."""
    identity = exact_identity(stock)
    quote_set = parse_raw_quote_csv(reader, stock, csv_source, contract)
    daily, monthly, actions = yahoo_daily(reader.read(yahoo_source, 'comparison_Yahoo_daily_close'),
                                          identity['ticker'], cutoff)
    start = shift_month(cutoff, -60)
    anchors = {iso_day(q['date']): positive(q['raw_close_decimal'])
               for q in quote_set['quotes'] if start <= iso_day(q['date']) <= cutoff}
    if len(anchors) < 4:
        raise ValueError('insufficient_independent_raw_price_anchors')
    observed = {factor_after(day, actions, cutoff) for day in anchors}
    needed = {factor_after(day, actions, cutoff) for day, value in monthly.values()}
    if not needed.issubset(observed):
        raise ValueError('every_used_share_regime_requires_independent_raw_anchor')
    modes = []
    for mode in ('split_adjusted', 'raw'):
        if all(day in daily and math.isclose(float(daily[day]),
                float(value / factor_after(day, actions, cutoff) if mode == 'split_adjusted' else value),
                rel_tol=.006, abs_tol=.015) for day, value in anchors.items()):
            modes.append(mode)
    if not modes:
        raise ValueError('independent_historical_price_or_share_scale_mismatch')
    mode = modes[0]
    prices = []
    for month, (day, value) in monthly.items():
        factor = factor_after(day, actions, cutoff) if mode == 'raw' else Decimal(1)
        prices.append({'month': month.isoformat(), 'trade_date': day.isoformat(),
            'normalized_close_decimal': str(value / factor), 'reported_close_decimal': str(value)})
    for previous, current in zip(prices, prices[1:]):
        if not Decimal('.2') < positive(current['normalized_close_decimal']) / positive(previous['normalized_close_decimal']) < 3:
            raise ValueError('unexplained_extreme_monthly_price_change')
    return {'identity': identity, 'prices': prices, 'mode': mode,
        'independent_raw_anchors': len(anchors), 'anchors': quote_set['quotes'],
        'provider': quote_set['provider'], 'authority': quote_set['authority'],
        'reported_actions': actions, 'primary_action_proof_still_required': True,
        'sources': [yahoo_source, csv_source], 'documentation_contract': contract,
        'cash_calendar_reconciliation_still_required': True, 'financial_gates': 'unchanged',
        'model_approved': False}


def ipe_documents(reader):
    """Scan entire cached original annual indexes, retaining old declarations."""
    path = reader.root / 'raw/primary-research/cvm-documents/index-sources.json'
    manifests = json.loads(path.read_bytes())
    documents, periods = [], []
    for source in manifests:
        year = source['year']
        if year not in range(2021, CUTOFF.year + 1):
            continue
        if source['url'] != f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_{year}.zip':
            raise ValueError('wrong_native_IPE_origin')
        body = reader.read(source, 'native_CVM_IPE_index')
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            member = f'ipe_cia_aberta_{year}.csv'
            if archive.namelist() != [member] or archive.getinfo(member).file_size > 100_000_000:
                raise ValueError('invalid_native_IPE_archive')
            rows = csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';')
            count = 0
            for line, row in enumerate(rows, 2):
                count += 1
                if not row['Codigo_CVM'].isdigit() or int(row['Codigo_CVM']) not in {7617, 17329, 3069}:
                    continue
                published = row['Data_Entrega'][:10]
                if published > CUTOFF.isoformat():
                    continue
                text = calendars._norm(' '.join(row.get(k, '') for k in ('Categoria', 'Tipo', 'Assunto')))
                if not any(word in text for word in ('provento', 'dividendo', 'capital proprio', 'jcp', 'bonifica', 'desdobr')):
                    continue
                documents.append({'cvm': int(row['Codigo_CVM']), 'url': row['Link_Download'],
                    'published_at': published, 'raw_row': row, 'index_file': source['file'],
                    'index_sha256': source['sha256'], 'member': member, 'csv_line': line})
        periods.append({'year': year, 'native_rows_scanned': count, 'file': source['file'],
                        'sha256': source['sha256'], 'cash_history_complete': False})
    return documents, periods


def cached_declarations(reader, documents):
    from . import cvm_proventos_documents as cvm
    report = json.loads((reader.root / 'raw/primary-research/cvm-documents/report.json').read_bytes())
    indexed = {d['url']: d for d in documents}
    results = []
    for source in report['results']:
        if source.get('cvm') not in {i[0] for i in IDENTITIES.values()} or not source.get('file'):
            continue
        row = indexed.get(source['url'])
        if not row or row['cvm'] != source['cvm'] or row['published_at'] != source['published_at']:
            raise ValueError('cached_document_native_index_identity_mismatch')
        sequence, version = cvm.document_id(row['url'])
        document = {'cvm': row['cvm'], 'issuer_name': row['raw_row']['Nome_Companhia'],
            'published_at': row['published_at'], 'url': row['url'],
            'sequence': sequence, 'version': version}
        # Discard stored event assertions and reparse the verified original PDF.
        parsed = cvm.parse_pdf(reader.read(source, 'reparsed_native_CVM_cash_form'), document, CUTOFF)
        results.append({**parsed, 'source': {k: source[k] for k in
            ('file', 'sha256', 'bytes', 'url', 'published_at')}, 'native_index': row,
            'calendar_completeness_established': False})
    return results


def declaration_matches(stock, cash, declarations):
    """Resolve a table row only against one exact-ISIN primary form installment."""
    exact_identity(stock)
    matches = []
    for event in (cash or {}).get('events', []):
        if not event.get('payment_date') or not event.get('approval_date') or not event.get('reported_per_share_decimal'):
            continue
        facts = []
        for doc in declarations:
            for fact in doc.get('events', []):
                if (fact.get('cvm'), fact.get('isin'), fact.get('payment_date'), fact.get('approval_date')) != (
                        stock['cvm'], stock['isin'], event['payment_date'], event['approval_date']):
                    continue
                if fact.get('issues') or not fact.get('record_date') or not fact.get('gross_per_unit_decimal'):
                    continue
                if event.get('record_date') and event['record_date'] != fact['record_date']:
                    continue
                amount = positive(fact['gross_per_unit_decimal'])
                if abs(amount - positive(event['reported_per_share_decimal'])) > Decimal('.00000001'):
                    continue
                facts.append((fact, doc['source']))
        if len(facts) == 1:
            fact, source = facts[0]
            matches.append({'ticker': stock['ticker'], 'isin': stock['isin'],
                'calendar_event': event, 'native_form_fact': fact, 'native_form_source': source,
                'resolution': 'exact_ISIN_approval_payment_gross_native_form_match',
                'record_date': fact['record_date'], 'gross_per_share_decimal': fact['gross_per_unit_decimal'],
                'native_form_tax_basis': 'explicit_gross', 'is_settlement_confirmation': False,
                'cash_model_eligible': False, 'share_basis_still_requires_reconciliation': True})
    return matches


def source_inputs(reader, stock, cash):
    """Concrete original-table inputs for parent integration; no cash history claim."""
    identity = exact_identity(stock)
    if cash is None:
        return {'identity': identity, 'status': 'primary_calendar_missing', 'events': []}
    events = [{**deepcopy(event), 'isin': identity['isin'], 'payment_date_role': 'payment',
               'record_date_role': 'entitlement_record'} for event in cash['events']]
    try:
        validated = validate_cash_events(stock, events)
        status, error = 'calendar_semantics_validated_reconciliation_pending', None
    except ValueError as exc:
        validated, status, error = None, 'primary_calendar_semantics_unresolved', str(exc)
    return {'identity': identity, 'status': status, 'events': events, 'validation': validated,
        'pending_reason': error, 'source': cash['source'], 'all_native_rows_retained': True,
        'identity_binding': 'caller_FCA_identity_and_exact_cached_ISIN_mapping',
        'cash_history_complete': False, 'model_approved': False}


def build_report(root):
    from .market_inventory import load_inventory
    from .security_registration import parse_fca_registry, _kind
    reader = Reader(root)
    inventory = load_inventory(root)
    stocks = {stock['ticker']: stock for stock in inventory['stocks']}
    known = {s['file']: s for s in inventory['sources']}
    official_sites = fca_sites(reader)
    fca_source = next(s for s in inventory['sources'] if s['file'] == 'raw/inventory/fca_cia_aberta_2026.zip')
    masters, securities = parse_fca_registry(reader.read(fca_source, 'native_FCA_exact_class_identity'), 2026)
    documents, periods = ipe_documents(reader)
    declarations = cached_declarations(reader, documents)
    results = []
    for ticker, (cvm, share_class, isin, issuer) in IDENTITIES.items():
        stock = stocks[ticker]
        exact_identity(stock)
        exact_fca = [s for s in securities if s['Codigo_Negociacao'] == ticker
            and s['ID_Documento'] == stock['filing_id'] and _kind(s)[1] == share_class]
        if not exact_fca:
            raise ValueError('native_FCA_exact_security_class_not_found')
        identity_name = stock['identity_file']
        identity_path = safe_path(reader.root, identity_name)
        identity_body = identity_path.read_bytes()
        identity_receipt = {'file': identity_name, 'sha256': sha(identity_body), 'bytes': len(identity_body),
            'authority': 'previously_cached_exchange_identity_no_network',
            'hash_basis': 'actual_cache_bytes_not_prior_acquisition_receipt',
            'original_receipt_verified': False, 'url': None}
        registry = json.loads(reader.read(identity_receipt, 'cached_exact_ISIN_binding'))
        if int(registry['codeCVM']) != cvm or not any(
                row.get('code') == ticker and row.get('isin') == isin for row in registry['otherCodes']):
            raise ValueError('cached_ISIN_mapping_conflicts_with_exact_security')
        cash = native_cash(reader, ticker)
        events = cash.get('events', []) if cash else []
        price_source = known[stock['history']['source_file']]
        price_window = inspect_price_window(reader.read(price_source, 'existing_Yahoo_daily_history'), ticker)
        input_payload = source_inputs(reader, stock, cash)
        matches = declaration_matches(stock, cash, declarations)
        pending = ['independent_non_Yahoo_raw_price_export_missing',
                   'full_share_regime_reconciliation_required',
                   'primary_corporate_actions_for_every_used_regime_required',
                   'full_cash_calendar_and_share_basis_reconciliation_required']
        if cash is None:
            pending.append('native_primary_cash_calendar_missing')
        if input_payload.get('pending_reason'):
            pending.append(input_payload['pending_reason'])
        results.append({'ticker': ticker, 'expected_identity': {'cvm': cvm,
            'share_class': share_class, 'isin': isin}, 'issuer': issuer,
            'native_cash': cash, 'cash_rows': len(events),
            'cash_issues': dict(Counter(issue for event in events for issue in event['issues'])),
            'source_inputs': input_payload, 'price_window': price_window,
            'matched_primary_form_installments': matches,
            'fca_identity_rows': exact_fca, 'fca_source': fca_source,
            'ISIN_mapping_source': identity_receipt,
            'candidate': None, 'model_approved': False, 'pending': pending})
    discovery = []
    for receipt in list(reader.sources.values()):
        if receipt.get('content_type', '').startswith('text/html'):
            discovery.extend(discover(reader.read(receipt), receipt))
    own = list((Path(root) / CACHE).glob('response-*.json'))
    for path in own:
        receipt = json.loads(path.read_bytes())
        body = reader.read(receipt, 'HTTP_response_original_including_errors')
        if receipt.get('http_status') == 200:
            if 'html' in receipt.get('content_type', '') or 'javascript' in receipt.get('content_type', ''):
                discovery.extend(discover(body, receipt))
    requests = sorted(Fetcher(root).history('response'), key=lambda r: r['ordinal'])
    request_attempts = Fetcher(root).history('request')
    blocked = [{'code': 'access_stop_no_further_network', 'url': r['url'],
                'http_status': r['http_status'], 'response_original': r['file'],
                'sha256': r['sha256']} for r in requests if r['http_status'] in (401, 403, 429)]
    blocked.extend({'code': 'robots_redirect_not_followed_CDN_scripts_not_fetched',
                    'url': r['url'], 'http_status': r['http_status'],
                    'response_original': r['file'], 'sha256': r['sha256'],
                    'location': r.get('location'), 'location_not_recorded_on_initial_attempt': 'location' not in r}
                   for r in requests if '/robots.txt' in r['url'] and r['http_status'] in (301, 302, 307, 308))
    return {'schema_version': 1, 'cutoff': CUTOFF.isoformat(), 'results': results,
        'documents': documents, 'index_periods': periods, 'discoveries': discovery,
        'official_FCA_sites': official_sites, 'cached_primary_forms_reparsed': declarations,
        'sources': list(reader.sources.values()), 'requests': requests,
        'public_request_attempts': len(request_attempts), 'maximum_public_requests': MAX_REQUESTS,
        'blocked': blocked, 'network_stopped': bool(blocked),
        'model_admissions': 0, 'active_pointers_changed': False,
        'financial_gates': 'unchanged_parent_must_apply', 'published': False,
        'integration': 'Consume source_inputs after matching exact FCA CVM/class and cached ISIN. '
            'Reparse original CSV via parse_raw_quote_csv with reviewed literal provider-origin and '
            'unadjusted-basis documentation; reconcile_raw_quote_export requires 61 months, '
            'four independent raw anchors and every used share regime. Then reconcile complete '
            'primary corporate actions and cash basis, and apply unchanged parent annual financial '
            'features and stock observations. Never synthesize B3 payloads. No production provider '
            'CSV contract has been verified or installed; all five instruments remain pending.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', default='recommender/data/market')
    parser.add_argument('--fetch')
    parser.add_argument('--discovered-in')
    args = parser.parse_args()
    if args.fetch:
        if not args.discovered_in:
            parser.error('--fetch requires --discovered-in exact original receipt')
        _, receipt = Fetcher(args.folder).get(args.fetch, args.discovered_in)
        print(json.dumps(receipt, sort_keys=True))
    else:
        report = build_report(args.folder)
        output = save(args.folder, 'report', dump(report))
        print(json.dumps({'file': output, 'requests': len(report['requests']),
            'results': [{k: r[k] for k in ('ticker', 'cash_rows', 'cash_issues', 'pending')}
                        for r in report['results']], 'documents': len(report['documents'])}))


if __name__ == '__main__':
    main()
