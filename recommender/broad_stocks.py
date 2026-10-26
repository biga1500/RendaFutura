"""Public B3 identities, observed payment dates and independently checked amounts.

Yahoo dividend dates are used only to check corporate actions, never as cash dates.
Share transformations require a complete per-share reconciliation. Missing prices,
payment dates and financial accounts are not imputed.
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import threading
import urllib.error
from urllib.parse import urlparse
import urllib.request
import uuid

from lxml import html

from .collect_market_data import DividendHistory, month_end, normalize, shift_month, write_json

B3 = 'https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/'
SECTOR_CODES = {'petroleo gas e biocombustiveis': 0, 'materiais basicos': 1, 'financeiro': 2,
                'consumo nao ciclico': 3, 'consumo ciclico': 4, 'bens industriais': 5,
                'saude': 6, 'tecnologia da informacao': 7, 'comunicacoes': 8, 'utilidade publica': 9}


class PublicSources:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.manifest = []
        self._locks = {}
        self._locks_guard = threading.Lock()

    def get(self, url, name):
        path = self.folder / name
        with self._locks_guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            if not path.is_file():
                request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0',
                                                               'Accept': '*/*'})
                # Shared issuers are fetched once, and readers never see a partial file.
                with urllib.request.urlopen(request, timeout=45) as response:
                    body = response.read()
                temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(body)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                time.sleep(.15)
            body = path.read_bytes()
        self.manifest.append({'url': url, 'file': 'expanded/' + name, 'bytes': len(body),
                              'sha256': hashlib.sha256(body).hexdigest(),
                              'retrieved_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
        return body

    def b3(self, method, params, name):
        encoded = base64.b64encode(json.dumps(params, separators=(',', ':')).encode()).decode()
        return json.loads(self.get(B3 + method + '/' + encoded, name))


def company_catalog(sources):
    result, pages = {}, None
    for page in range(1, 61):
        body = sources.b3('GetInitialCompanies', {'language': 'pt-br', 'pageNumber': page, 'pageSize': 120},
                          f'b3-companies-{page}.json')
        if pages is None:
            pages = body['page']['totalPages']
            if not isinstance(pages, int) or not 1 <= pages <= 60:
                raise ValueError('B3 company catalogue pagination unavailable.')
        for row in body['results']:
            # Market indicators also encode Novo Mercado/N1/N2. Restrict by
            # issuer type and exact instrument identity, not one market segment.
            if row.get('status') == 'A' and str(row.get('type')) == '1' and not row.get('typeBDR'):
                prefix = row.get('issuingCompany', '')
                if re.fullmatch('[A-Z]{4}', prefix):
                    result[prefix] = row
        if page == pages:
            return result
    raise ValueError('Incomplete B3 company catalogue.')


def number(value):
    result = float(str(value).strip().replace('.', '').replace(',', '.'))
    if not math.isfinite(result):
        raise ValueError('Non-finite public numeric value.')
    return result


def br_day(value):
    return datetime.strptime(str(value).strip(), '%d/%m/%Y').date()


def paid_history(content, ticker, cutoff):
    tree = html.fromstring(content)
    tables = tree.xpath('//table[@id="table-dividends-history"]')
    if len(tables) != 1:
        raise ValueError('payment_table_absent')
    history = DividendHistory(cutoff, 0)
    records, installments, seen, comparable = [], [], set(), defaultdict(float)
    oldest = None
    for index, row in enumerate(tables[0].xpath('.//tbody/tr')):
        cells = [' '.join(cell.itertext()).strip() for cell in row.xpath('./td')]
        if len(cells) != 4:
            raise ValueError('payment_row_schema_changed')
        label, record_text, payment_text, amount_text = cells
        # Monetary corrections are not principal, but remain outside this model's
        # dividends/JCP target, consistently with the existing Petrobras source.
        if normalize(label) not in ('dividendos', 'dividendo', 'jscp', 'jcp'):
            continue
        record = br_day(record_text)
        oldest = min(oldest or record, record)
        if record > cutoff:
            continue
        try:
            payment = br_day(payment_text)
            amount = number(amount_text)
        except (TypeError, ValueError):
            raise ValueError('missing_payment_date_or_amount') from None
        if payment < record or amount < 0 or not math.isfinite(amount):
            raise ValueError('invalid_payment_order_or_amount')
        key = (normalize(label), record, payment, amount)
        if key in seen:
            raise ValueError('duplicate_payment_event')
        seen.add(key)
        # Reconcile the entire declared installment amount, including dated
        # future installments, but never turn those installments into paid cash.
        comparable[record] += amount
        installment = {'ticker': ticker, 'payment_date': payment.isoformat(), 'record_date': record.isoformat(),
                       'gross_per_unit': amount, 'label': label, 'status': 'included',
                       'basis': 'reported_payment_date_investidor10_crosschecked_B3'}
        installments.append(installment)
        if payment > cutoff:
            history.future_events += 1
            continue
        history.append((payment, payment, amount))
        records.append(installment)
    history.event_count = len(records)
    if oldest is None or oldest > shift_month(cutoff, -72) or len(records) < 8:
        raise ValueError('payment_history_too_short')
    history.coverage_start = min(day for day, _, _ in history)
    history.installments = installments
    if history.coverage_start > shift_month(cutoff, -60):
        raise ValueError('payments_do_not_cover_common_return_window')
    return history, records, comparable


def split_price_history(content, ticker, cutoff, *, allow_recent_actions=False):
    payload = json.loads(content)
    if payload.get('chart', {}).get('error'):
        raise ValueError('price_provider_error')
    result = payload['chart']['result'][0]
    if result['meta'].get('symbol') != ticker + '.SA' or result['meta'].get('currency') != 'BRL':
        raise ValueError('price_identity_or_currency_mismatch')
    splits = []
    for event in result.get('events', {}).get('splits', {}).values():
        day = datetime.fromtimestamp(event['date'], timezone.utc).date()
        ratio = float(event['numerator']) / float(event['denominator'])
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError('invalid_share_action')
        splits.append({'date': day.isoformat(), 'ratio': ratio})
        if day > shift_month(cutoff, -61) and not allow_recent_actions:
            raise ValueError('recent_share_action_requires_reconciliation')
    prices = {}
    values = result['indicators']['quote'][0]['close']
    if len(values) != len(result['timestamp']):
        raise ValueError('price_length_mismatch')
    for timestamp, value in zip(result['timestamp'], values):
        day = datetime.fromtimestamp(timestamp, timezone.utc).date()
        if day > cutoff or value is None:
            continue
        if not math.isfinite(value) or value <= 0:
            raise ValueError('invalid_stock_price')
        key = month_end(day.year, day.month)
        if key not in prices or day > prices[key][0]:
            prices[key] = (day, float(value), float(value))
    required = [shift_month(cutoff, -i) for i in range(61)]
    if any(day not in prices for day in required):
        raise ValueError('less_than_61_consecutive_prices')
    if (cutoff - prices[cutoff][0]).days > 7:
        raise ValueError('stale_month_end_price')
    for previous, current in zip(sorted(prices), sorted(prices)[1:]):
        if not allow_recent_actions and current in required and not .2 < prices[current][2] / prices[previous][2] < 3:
            raise ValueError('unexplained_extreme_price_change')
    return prices, splits


def check_cash_amounts(rows, comparable, cutoff, share_class):
    expected = defaultdict(float)
    for row in rows:
        if row.get('typeStock', '').strip() != share_class:
            continue
        day = br_day(row['lastDatePriorEx'])
        if shift_month(cutoff, -60) < day <= cutoff and normalize(row.get('corporateAction', '')) in ('dividendo', 'jrs cap proprio'):
            quota = number(row.get('quotedPerShares') or '1')
            if quota <= 0:
                raise ValueError('invalid_share_quote_unit')
            expected[day] += number(row['valueCash']) / quota
    checked = []
    for day, amount in comparable.items():
        if not shift_month(cutoff, -60) < day <= cutoff:
            continue
        if day not in expected or not math.isclose(amount, expected[day], rel_tol=.002, abs_tol=.00002):
            raise ValueError('cash_amount_or_share_scale_disagrees_with_B3')
        checked.append(day.isoformat())
    # Do not silently omit B3 events with mature rights and no matching paid row.
    if any(day not in comparable for day in expected):
        raise ValueError('B3_cash_event_missing_from_payment_table')
    if len(checked) < 4:
        raise ValueError('insufficient_independent_cash_matches')
    return checked


def check_b3_prices(content, rows, cutoff, share_class):
    result = json.loads(content)['chart']['result'][0]
    daily = {datetime.fromtimestamp(t, timezone.utc).date(): value
             for t, value in zip(result['timestamp'], result['indicators']['quote'][0]['close']) if value is not None}
    checked = set()
    for row in rows:
        if row.get('typeStock', '').strip() != share_class:
            continue
        quote_date = row.get('dateClosingPricePriorExDate')
        if quote_date in (None, '', '31/12/9999'):
            continue
        day = br_day(quote_date)
        if not shift_month(cutoff, -60) < day <= cutoff:
            continue
        value = number(row.get('closingPricePriorExDate') or '0')
        if value <= 0:
            continue
        if day not in daily or not math.isclose(value, daily[day], rel_tol=.006, abs_tol=.015):
            raise ValueError('historical_quote_or_share_scale_disagrees_with_B3')
        checked.add(day.isoformat())
    if len(checked) < 4:
        raise ValueError('insufficient_independent_price_matches')
    return sorted(checked)


def fetch_one(ticker, company, sources, cutoff):
    detail = sources.b3('GetDetail', {'codeCVM': str(company['codeCVM']), 'language': 'pt-br'},
                        'b3-detail-' + company['codeCVM'] + '.json')
    codes = {row['code']: row['isin'] for row in detail.get('otherCodes', [])}
    if ticker not in codes or str(detail.get('codeCVM')) != str(company['codeCVM']):
        raise ValueError('B3_ticker_issuer_identity_unresolved')
    payment_url = f'https://investidor10.com.br/acoes/{ticker.lower()}/'
    paid, ledger, comparable = paid_history(sources.get(payment_url, ticker + '-payments.html'), ticker, cutoff)
    price_url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval=1d&events=div%2Csplits'
    price_content = sources.get(price_url, ticker + '-prices.json')
    prices, actions = split_price_history(price_content, ticker, cutoff, allow_recent_actions=True)
    # Full pagination is mandatory: the default first page truncates long histories.
    cash_rows, pages = [], None
    trading = detail['tradingName'].replace('/', '').replace('.', '').replace(' ', '')
    for page in range(1, 31):
        body = sources.b3('GetListedCashDividends', {'language': 'pt-br', 'pageNumber': page, 'pageSize': 120,
                                                   'tradingName': trading}, f'{ticker}-b3-cash-{page}.json')
        pages = body.get('page', {}).get('totalPages')
        if type(pages) is not int or not 1 <= pages <= 30:
            raise ValueError('B3_cash_pagination_unavailable')
        cash_rows.extend(body['results'])
        if page == pages:
            break
    share_class = 'ON' if ticker.endswith('3') else {'4': 'PN', '5': 'PNA', '6': 'PNB', '8': 'PND'}[ticker[-1]]
    normalization = None
    recent_actions = [row for row in actions if date.fromisoformat(row['date']) > shift_month(cutoff, -61)]
    if recent_actions:
        supplement = sources.b3('GetListedSupplementCompany', {'issuingCompany': ticker[:4], 'language': 'pt-br'},
                                'b3-supplement-' + ticker[:4] + '.json')
        supplement = supplement[0] if isinstance(supplement, list) and supplement else supplement
        for event in supplement.get('stockDividends', []):
            try:
                day = br_day(event.get('lastDatePrior'))
            except (TypeError, ValueError):
                raise ValueError('corporate_action_date_unavailable') from None
            if (event.get('isinCode') == codes[ticker] and shift_month(cutoff, -61) < day <= cutoff
                    and normalize(event.get('label', '')) not in ('desdobramento', 'grupamento', 'bonificacao')):
                raise ValueError('non_cash_corporate_action_requires_reconciliation')
        from .share_basis import b3_evidence, reconcile_payments, reconcile_prices
        expected, quotes = b3_evidence(cash_rows, share_class)
        prices, price_matches, price_mode = reconcile_prices(price_content, ticker, actions, quotes, cutoff)
        paid, ledger, adjustments = reconcile_payments(ticker, paid, expected, actions, cutoff)
        matches = [row['record_date'] for row in adjustments]
        normalization = {'share_basis_date': cutoff.isoformat(), 'price_source_basis': price_mode,
                         'price_output_basis': 'cutoff_share_units_no_dividend_reinvestment',
                         'payments': adjustments, 'actions': actions,
                         'calendar_basis': 'reported_payment_dates_not_ex_dates'}
    else:
        matches = check_cash_amounts(cash_rows, comparable, cutoff, share_class)
        price_matches = check_b3_prices(price_content, cash_rows, cutoff, share_class)
    latest_action = max((date.fromisoformat(a['date']) for a in actions), default=date(2014, 1, 1))
    # No historical EPS/share is compared across a reported transformation.
    available_since = max(shift_month(month_end(latest_action.year, latest_action.month), 24),
                          shift_month(month_end(paid.coverage_start.year, paid.coverage_start.month), 12))
    if normalization:
        available_since = max(shift_month(shift_month(cutoff, -60), 12),
                              shift_month(month_end(paid.coverage_start.year, paid.coverage_start.month), 12))
    return {'ticker': ticker, 'name': detail['companyName'], 'cvm': int(company['codeCVM']),
            'sector_code': SECTOR_CODES.get(normalize(detail.get('industryClassification', '').split('/')[0]).strip(), 10),
            'industry_classification': detail.get('industryClassification'),
            'share_class': share_class, 'isin': codes[ticker], 'prices': prices, 'payments': paid,
            'ledger': ledger, 'independent_cash_matches': matches, 'share_actions': actions,
            'independent_price_matches': price_matches,
            'payment_coverage_start': paid.coverage_start.isoformat(),
            'financial_basis_start': latest_action.isoformat(), 'share_normalization': normalization,
            'training_since': max(available_since, date(2016, 10, 31)),
            'payment_url': payment_url, 'price_url': price_url}


def harvest(folder, catalogue, cutoff, *, workers=3):
    sources = PublicSources(Path(folder) / 'raw/expanded')
    companies = company_catalog(sources)
    audit_path = Path(folder) / 'expanded-source-audit.json'
    denied = {}
    if audit_path.is_file() and datetime.fromtimestamp(audit_path.stat().st_mtime, timezone.utc).date() == datetime.now(timezone.utc).date():
        previous_audit = json.loads(audit_path.read_text(encoding='utf-8'))
        if previous_audit.get('reference_date') == cutoff.isoformat():
            denied = {r['ticker']: r for r in previous_audit.get('excluded_stocks', [])
                      if r.get('reason') == 'public_source_http_error' and r.get('http_status') in (403, 404, 429)}
    candidates, excluded = [], []
    for stock in catalogue['stocks']:
        ticker = stock['ticker']
        if ticker in ('PETR4', 'VALE3'):
            continue
        if ticker.endswith('F'):
            excluded.append({'ticker': ticker, 'reason': 'fractional_alias_not_distinct_asset'})
        elif not re.fullmatch('[A-Z]{4}[34568]', ticker):
            excluded.append({'ticker': ticker, 'reason': 'unit_or_other_share_structure_not_reconciled'})
        elif ticker[:4] not in companies:
            excluded.append({'ticker': ticker, 'reason': 'B3_active_issuer_mapping_unavailable'})
        elif ticker in denied:
            # Respect today's denials rather than repeatedly hammering a source.
            excluded.append(denied[ticker])
        else:
            candidates.append(ticker)
    included = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(fetch_one, t, companies[t[:4]], sources, cutoff): t for t in candidates}
        for count, future in enumerate(as_completed(jobs), 1):
            ticker = jobs[future]
            try:
                included.append(future.result())
                print(json.dumps({'stock': ticker, 'status': 'source_validated', 'progress': f'{count}/{len(jobs)}'}), flush=True)
            except urllib.error.HTTPError as error:
                excluded.append({'ticker': ticker, 'reason': 'public_source_http_error', 'http_status': error.code,
                                 'source_host': urlparse(error.url).hostname})
            except Exception as error:
                excluded.append({'ticker': ticker, 'reason': str(error)[:180], 'error_type': type(error).__name__})
            if count % 25 == 0:
                print(json.dumps({'processed': count, 'candidates': len(jobs), 'source_validated': len(included),
                                  'excluded': len(excluded)}), flush=True)
    write_json(audit_path, {'reference_date': cutoff.isoformat(), 'checked_at': datetime.now(timezone.utc).isoformat(),
               'source_validated': [r['ticker'] for r in included], 'excluded_stocks': excluded,
               'sources': sorted(sources.manifest, key=lambda r: r['file'])})
    return sorted(included, key=lambda r: r['ticker']), excluded, sources.manifest
