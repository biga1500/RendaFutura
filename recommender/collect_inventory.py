"""Collect each market component independently; missing dividends never erase a quote."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from .broad_stocks import SECTOR_CODES
from .collect_market_data import ROOT, cached_source, normalize, write_json
from .cvm_registry import parse_fca
from .dividend_scope import OUTSIDE_REASON, allowed_tickers, filter_records, scope_metadata
from .market_inventory import market_root, publish_inventory


def identities(folder):
    catalog = json.loads((folder / 'catalog.json').read_text(encoding='utf-8-sig'))
    details = {}
    for path in sorted((folder / 'raw/expanded').glob('b3-detail-*.json')):
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(record, dict):
            continue
        for code in record.get('otherCodes', []):
            details[code['code']] = (record, code['isin'], path)
    prepared, reference, reasons = set(), None, {}
    if (folder / 'current.json').is_file():
        pointer = json.loads((folder / 'current.json').read_text(encoding='utf-8'))
        relative = pointer.get('snapshot', '')
        if not re.fullmatch(r'snapshots/[A-Za-z0-9-]+', relative):
            raise ValueError('Ponteiro de modelo invalido.')
        snapshot = folder / relative
        data = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
        report = json.loads((snapshot / 'collection-report.json').read_text(encoding='utf-8'))
        prepared = {a['ticker'] for a in data['assets']}
        reference = data['reference_date']
        reasons = {r['ticker']: r['reason'] for r in report.get('excluded_stocks', [])}
    aliases, records = {}, []
    for row in catalog['stocks']:
        if row['ticker'].endswith('F'):
            aliases.setdefault(row['ticker'][:-1], []).append(row['ticker'])
    for row in catalog['stocks']:
        ticker = row['ticker']
        if ticker.endswith('F'):
            continue
        if not re.fullmatch(r'[A-Z0-9.-]{1,30}', ticker):
            raise ValueError('Codigo invalido no catalogo.')
        found = details.get(ticker)
        detail, isin, identity_path = found if found else ({}, None, None)
        share = {'3': 'ON', '4': 'PN', '5': 'PNA', '6': 'PNB', '8': 'PND'}.get(ticker[-1]) if re.fullmatch(r'[A-Z]{4}[34568]', ticker) else None
        records.append({'ticker': ticker, 'name': detail.get('companyName') or row.get('name') or ticker,
                        'cvm': int(detail['codeCVM']) if detail.get('codeCVM') else None,
                        'isin': isin, 'share_class': share, 'aliases': aliases.get(ticker, []),
                        'sector_code': SECTOR_CODES.get(normalize(detail.get('industryClassification', '').split('/')[0]).strip(), 10),
                        'identity_status': 'verified_cached_registry' if found else 'not_resolved',
                        'identity_file': identity_path.relative_to(folder).as_posix() if identity_path else None,
                        'model_ready': ticker in prepared, 'model_reference_date': reference,
                        'model_pending_reason': reasons.get(ticker) if ticker not in prepared else None})
    return records


def collect_identities(folder, as_of):
    folder = market_root(folder)
    allowed_tickers(folder)
    records = {row['ticker']: row for row in identities(folder)}
    sources = []
    name = f'fca_cia_aberta_{as_of.year}.zip'
    url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/{name}'
    raw = folder / 'raw/inventory'
    content = cached_source(url, name, raw, sources, False)
    official = parse_fca(content, as_of.year, as_of)
    for ticker, identity in official['instruments'].items():
        old = records.get(ticker, {'ticker': ticker, 'aliases': [], 'isin': None,
                                  'sector_code': 10, 'model_ready': False,
                                  'model_reference_date': None, 'model_pending_reason': 'not_in_prepared_sample'})
        if old.get('cvm') is not None and identity['cvm'] is not None and old['cvm'] != identity['cvm']:
            old['isin'] = None
        records[ticker] = {**old, **identity}
    for source in sources:
        source.update(file='raw/inventory/' + name, authority='primary', provider='CVM FCA')
    return sorted(records.values(), key=lambda row: row['ticker']), sources, official['issues']


def parse_quote(content, ticker, as_of):
    result = json.loads(content)['chart']['result'][0]
    meta = result['meta']
    if meta.get('symbol') != ticker + '.SA' or meta.get('currency') != 'BRL':
        raise ValueError('quote_identity_or_currency_mismatch')
    value, stamp = meta.get('regularMarketPrice'), meta.get('regularMarketTime')
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('quote_price_missing')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
        raise ValueError('quote_timestamp_missing')
    quote_day = datetime.fromtimestamp(stamp, timezone.utc).date()
    if quote_day > as_of:
        raise ValueError('quote_after_reference_date')
    timestamps = result.get('timestamp') or []
    closes = ((result.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    if len(timestamps) != len(closes):
        raise ValueError('quote_history_misaligned')
    months = set()
    for timestamp, close in zip(timestamps, closes):
        day = datetime.fromtimestamp(timestamp, timezone.utc).date()
        if day <= as_of and isinstance(close, (float, int)) and not isinstance(close, bool) and math.isfinite(close) and close > 0:
            months.add(day.isoformat()[:7])
    return {'quote': {'value': float(value), 'unit': 'BRL', 'as_of': quote_day.isoformat(),
                      'status': 'available' if (as_of - quote_day).days <= 7 else 'stale',
                      'basis': 'last_observed_trade_not_executable_offer'},
            'history': {'status': 'available' if months else 'not_found', 'observed_months': len(months),
                        'first_month': min(months) if months else None, 'last_month': max(months) if months else None,
                        'basis': 'provider_close_not_reconciled_for_simulation', 'paid_cash_inferred_from_ex_dates': False}}


def collect_quotes(folder, records, as_of, *, workers=2, interval='1mo', reuse_existing=False):
    folder = market_root(folder)
    records = filter_records(folder, records)
    if interval not in ('1mo', '1d'):
        raise ValueError('Unsupported quote interval.')
    target = folder / ('raw/inventory/daily-prices' if interval == '1d' else 'raw/inventory/quotes')
    target.mkdir(parents=True, exist_ok=True)
    limited = threading.Event()
    network_lock = threading.Lock()
    next_request = [0.]

    def one(row):
        ticker = row['ticker']
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval={interval}&events=div%2Csplits'
        old = folder / 'raw/expanded' / (ticker + '-prices.json')
        current = target / (ticker + '.json')
        path = current if current.is_file() else old
        cached = path.is_file() and (reuse_existing or
            datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date() >= as_of)
        try:
            if not cached:
                if limited.is_set():
                    return ticker, {'quote': {'value': None, 'status': 'source_rate_limited'}, 'history': {'status': 'not_fetched'}}
                with network_lock:
                    time.sleep(max(0, next_request[0] - time.monotonic()))
                    next_request[0] = time.monotonic() + .6
                request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/json'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    content = response.read(4 * 1024 * 1024 + 1)
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError('quote_response_too_large')
                parse_quote(content, ticker, as_of)
                temporary = target / ('.' + ticker + '-' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(content)
                os.replace(temporary, current)
                path = current
            content = path.read_bytes()
            parsed = parse_quote(content, ticker, as_of)
            # Cached daily and new monthly downloads are both retained with their actual URL.
            source_url = url if path == current else url.replace('interval=1mo', 'interval=1d')
            source = {'url': source_url, 'provider': 'Yahoo Finance', 'authority': 'secondary',
                      'file': path.relative_to(folder).as_posix(), 'sha256': hashlib.sha256(content).hexdigest(),
                      'retrieved_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
            parsed['source'] = source
            parsed['quote']['source_url'] = source_url
            return ticker, parsed
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 429):
                limited.set()
            return ticker, {'quote': {'value': None, 'status': 'source_http_error', 'http_status': error.code, 'source_url': url},
                            'history': {'status': 'not_fetched'}}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return ticker, {'quote': {'value': None, 'status': 'source_unavailable', 'source_url': url},
                            'history': {'status': 'not_fetched'}}

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for count, future in enumerate(as_completed([pool.submit(one, row) for row in records]), 1):
            ticker, result = future.result()
            results[ticker] = result
            if count % 25 == 0:
                print(json.dumps({'component': 'quotes', 'processed': count, 'total': len(records),
                                  'statuses': dict(Counter(r['quote']['status'] for r in results.values()))}), flush=True)
    summary = 'daily-summary.json' if interval == '1d' else 'quote-summary.json'
    write_json(folder / 'raw/inventory' / summary, {'as_of': as_of.isoformat(), 'instruments': results})
    return results


def collect_daily_histories(folder, inventory, as_of, *, refresh=False, workers=2):
    """Collect selected codes independently from current-quote freshness."""
    folder = market_root(folder)
    records = filter_records(folder, inventory['stocks'])
    return collect_quotes(folder, records, as_of, workers=workers, interval='1d',
                          reuse_existing=not refresh)


def sync_daily_results(folder, inventory, results, as_of):
    """Expose verified collection receipts without approving any model asset."""
    folder = market_root(folder)
    selected = allowed_tickers(folder)
    updated = deepcopy(inventory)
    sources = {source['file']: source for source in updated.get('sources', []) if source.get('file')}
    for row in updated['stocks']:
        ticker = row['ticker']
        if selected is not None and ticker not in selected:
            row['daily_history_collection'] = {
                'status': 'outside_selected_scope', 'quote_status': 'outside_selected_scope',
                'reason': OUTSIDE_REASON, 'http_status': None,
                'absence_is_not_delisting_or_zero_distribution': True}
            continue
        result = results.get(ticker, {})
        source = result.get('source')
        row['daily_history_collection'] = {
            'status': result.get('history', {}).get('status', 'not_fetched'),
            'quote_status': result.get('quote', {}).get('status', 'not_fetched'),
            'http_status': result.get('quote', {}).get('http_status'),
            'absence_is_not_delisting_or_zero_distribution': True}
        if not source:
            continue
        relative = source.get('file')
        allowed = (f'raw/inventory/daily-prices/{ticker}.json', f'raw/expanded/{ticker}-prices.json')
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval=1d&events=div%2Csplits'
        if relative not in allowed or source.get('url') != url:
            raise ValueError('daily_history_receipt_identity_mismatch')
        path = folder / relative
        if path.is_symlink() or not path.resolve().is_relative_to(folder) or path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('unsafe_daily_history_source')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source.get('sha256'):
            raise ValueError('daily_history_source_hash_mismatch')
        previous = sources.get(relative)
        if previous and previous.get('sha256') != source['sha256']:
            raise ValueError('existing_daily_history_original_changed')
        # Reparse originals rather than accepting summary-provided prices/counts.
        parsed = parse_quote(content, ticker, as_of)
        if parsed['history']['status'] == 'available':
            row['history'] = {**parsed['history'], 'source_url': url, 'source_file': relative}
        quote = {**parsed['quote'], 'source_url': url}
        old = row['quote']
        if not old.get('value') or quote['as_of'] > old.get('as_of', ''):
            row['quote'] = quote
        elif (quote['as_of'] == old.get('as_of') and old.get('value') is not None
                and not math.isclose(quote['value'], old['value'], rel_tol=.001, abs_tol=.015)):
            row['daily_history_collection']['current_quote_conflict'] = True
        sources[relative] = source
    updated['sources'] = list(sources.values())
    updated['counts']['history_available'] = sum(row['history'].get('status') == 'available'
                                                for row in updated['stocks'])
    updated['counts']['daily_histories_available'] = sum(row['daily_history_collection']['status'] == 'available'
                                                       for row in updated['stocks'])
    updated['counts']['quotes_available'] = sum(row['quote'].get('status') == 'available'
                                               for row in updated['stocks'])
    updated['history_collection_updated_at'] = datetime.now(timezone.utc).isoformat()
    return updated


def collect_inventory(folder, as_of, *, refresh=False, activate=True):
    from .cvm_fundamentals import collect_fundamentals
    from .official_treasury_bundle import collect_treasury_bundle
    from .payment_sources import collect_primary_payments
    folder = market_root(folder)
    scope = scope_metadata(folder)
    (folder / 'raw/inventory').mkdir(parents=True, exist_ok=True)
    records, registry_sources, issues = collect_identities(folder, as_of)
    selected_records = filter_records(folder, records)
    selected = {row['ticker'] for row in selected_records}
    quote_path = folder / 'raw/inventory/quote-summary.json'
    saved = json.loads(quote_path.read_text(encoding='utf-8')) if quote_path.is_file() else {}
    quotes = saved.get('instruments', {})
    if selected_records and (saved.get('as_of') != as_of.isoformat()
                             or any(row['ticker'] not in quotes for row in selected_records)):
        quotes = collect_quotes(folder, selected_records, as_of)
    specs = {row['ticker']: (row.get('cvm'), row.get('share_class'), row['sector_code']) for row in selected_records}
    print(json.dumps({'component': 'official_financials', 'instruments': len(specs)}), flush=True)
    financials = collect_fundamentals(folder, specs, as_of, refresh=refresh)
    write_json(folder / 'raw/inventory/financial-fields.json', financials)
    print(json.dumps({'component': 'official_treasury'}), flush=True)
    treasury = collect_treasury_bundle(folder, as_of, refresh=refresh)
    financial_sources = [{**source, 'file': 'raw/' + source['file'],
                          'authority': 'primary', 'provider': 'CVM ' + source['document_type']}
                         for source in financials['sources']]
    sources = registry_sources + financial_sources + treasury['sources']
    print(json.dumps({'component': 'primary_payment_sources'}), flush=True)
    primary = collect_primary_payments(folder / 'raw/primary-payments',
        [{'ticker': row['ticker'], 'cvm': row['cvm']} for row in selected_records if row.get('cvm')],
        as_of, refresh=refresh)
    acquisitions = []
    for entry in primary['manifest']:
        source = {**entry, 'authority': 'primary',
                  'provider': 'CVM IPE' if 'dados.cvm.gov.br' in entry['url'] else 'Petrobras RI'}
        if source.get('file'):
            source['file'] = 'raw/primary-payments/' + source['file']
        acquisitions.append(source)
    sources.extend(acquisitions)
    write_json(folder / 'raw/inventory/primary-payment-acquisitions.json', primary['manifest'])
    source_urls = {r['id']: r['url'] for r in financials['sources']}
    aliases = {'basic_eps': 'earnings_per_share', 'total_assets': 'assets',
               'equity': 'equity', 'total_liabilities': 'liabilities', 'attributable_profit': 'profit'}
    stocks = []
    for number, row in enumerate(records, 1):
        ticker = row['ticker']
        if ticker not in selected:
            row.update(quote={'value': None, 'status': 'outside_selected_scope', 'reason': OUTSIDE_REASON},
                       history={'status': 'outside_selected_scope', 'reason': OUTSIDE_REASON,
                                'observed_months': None},
                       fundamentals={'status': 'outside_selected_scope', 'reason': OUTSIDE_REASON, 'fields': {}},
                       payments={'status': 'outside_selected_scope', 'reason': OUTSIDE_REASON, 'event_count': None,
                                 'events': [], 'history_complete': False, 'cash_model_eligible': False})
            stocks.append(row)
            continue
        row.update(quotes[ticker])
        if row.get('source'):
            sources.append(row.pop('source'))
        finances = financials['instruments'][ticker]
        fields = {aliases.get(key, key): {**value, 'source_url': source_urls.get(value['source_ref'])}
                  for key, value in finances['fields'].items()}
        row['fundamentals'] = {**finances, 'fields': fields}
        row['payments'], payment_sources = payment_component(folder, ticker, as_of,
            primary['by_ticker'].get(ticker), acquisitions)
        sources.extend(payment_sources)
        stocks.append(row)
        if number % 50 == 0:
            print(json.dumps({'component': 'independent_payments', 'processed': number, 'total': len(records)}), flush=True)
    model = json.loads((folder / json.loads((folder / 'current.json').read_text(encoding='utf-8'))['snapshot'] / 'assets.json').read_text(encoding='utf-8'))
    prepared_bonds = {(bond['kind'], bond['maturity']) for bond in model.get('bonds', [])}
    bonds = []
    for item in treasury['instruments']:
        no_coupon = item['kind'] in ('Tesouro Selic', 'Tesouro Prefixado', 'Tesouro IPCA+')
        bonds.append({'name': item['name'], 'kind': item['kind'], 'maturity': item['maturity'],
                      'quote': {'value': item['buy_price'], 'unit': 'BRL', 'rate_text': item.get('buy_rate_text'),
                                'as_of': item['as_of'], 'market_status': item['market_status'],
                                'status': 'available' if item['buy_price'] is not None else 'not_found', 'source_url': item['source_url']},
                      'history': {'observed_months': item['observed_history_month_count'], 'status': item['history_status']},
                      'payments': {'status': 'no_periodic_coupon' if no_coupon else item['paid_coupon_status'],
                                   'event_count': item['paid_coupon_count'], 'events': item['actual_coupon_events'][-10:],
                                   'coverage_complete': item['coupon_coverage_complete'], 'principal_counted_as_income': False},
                      'contractual_model_status': item['contractual_model_status'],
                      'model_ready': (item['kind'], item['maturity']) in prepared_bonds})
    inventory = {'schema_version': 1, 'as_of': as_of.isoformat(), 'collected_at': datetime.now(timezone.utc).isoformat(),
                 'stocks': stocks, 'treasury': bonds, 'sources': sources, 'issues': issues + treasury['issues'],
                 'counts': {'stocks': len(stocks), 'treasury': len(bonds),
                            'quotes_available': sum(row['quote']['status'] == 'available' for row in stocks),
                            'fundamentals_available': sum(any(f['value'] is not None for f in row['fundamentals']['fields'].values()) for row in stocks),
                            'payments_read': sum(bool(row['payments'].get('event_count')) for row in stocks),
                            'primary_document_references': sum(bool(row['payments'].get('primary_documents')) for row in stocks),
                            'model_ready_stocks': sum(row['model_ready'] for row in stocks)},
                 'model_reference_date': model['reference_date'], 'model_sample_unchanged': True,
                 'catalogue_minimum_payments': 0, 'catalogue_minimum_history_months': 0,
                 'limitations': ['Cadastro declarado no FCA e catalogo consultado nao certificam negociacao ativa neste instante.',
                                'Cotacoes Yahoo sao fonte secundaria identificada, nao dados oficiais da B3.',
                                'Documentos e pagamentos ausentes nao sao zero; cada campo preserva fonte e periodo.',
                                'A amostra de simulacao permanece separada; nao foram inventados historicos ou pagamentos.']}
    if scope is not None:
        inventory['dividend_scope'] = scope
    if activate:
        snapshot = publish_inventory(folder, inventory)
        print(json.dumps({'inventory_snapshot': str(snapshot), 'counts': inventory['counts']}), flush=True)
    return inventory


def payment_component(folder, ticker, as_of, primary, acquisitions):
    from .payment_sources import inspect_payment_page

    folder = market_root(folder)
    selected = allowed_tickers(folder)
    if selected is not None and ticker not in selected:
        return {'status': 'outside_selected_scope', 'reason': OUTSIDE_REASON, 'event_count': None, 'events': [],
                'history_complete': False, 'cash_model_eligible': False}, []
    selected, selected_source, sources = None, None, []
    # Primary document references are not cash observations. Prefer issuer cash
    # rows when present; otherwise keep readable secondary rows independently.
    if primary and primary['events']:
        selected = primary
        digest = next(source for source in primary['source_results'] if source['events'])['report']['sha256']
        selected_source = next((source for source in acquisitions if source.get('sha256') == digest), None)
    payment_path = folder / 'raw/expanded' / (ticker + '-payments.html')
    if selected is None and payment_path.is_file():
        observed = datetime.fromtimestamp(payment_path.stat().st_mtime, timezone.utc)
        body = payment_path.read_bytes()
        selected = inspect_payment_page(body, ticker, min(as_of, observed.date()), observed)
        selected_source = {'url': f'https://investidor10.com.br/acoes/{ticker.lower()}/',
                           'authority': 'secondary', 'provider': 'Investidor10',
                           'file': payment_path.relative_to(folder).as_posix(),
                           'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': observed.isoformat()}
        sources.append(selected_source)
    documents = primary.get('documents', []) if primary else []
    component = {'status': 'partial' if documents else 'not_found', 'event_count': None, 'events': [],
                 'history_complete': False, 'cash_model_eligible': False,
                 'primary_documents': documents, 'primary_document_count': len(documents),
                 'primary_documents_are_cash_observations': False}
    if selected is not None:
        window = selected['report']['requested_window']
        component.update(status=selected['status'], event_count=len(selected['events']),
            events=selected['events'][-10:], source_url=selected_source['url'] if selected_source else None,
            authority=selected_source['authority'] if selected_source else None,
            window_start=window['start'], window_end=window['end'], field_status=selected['field_status'],
            issues=selected['report']['issues'][:20], history_complete=selected['report']['history_complete'])
    report = folder / 'raw/inventory/payments' / (ticker + '.json')
    report.parent.mkdir(parents=True, exist_ok=True)
    write_json(report, {'selected_cash_source': selected, 'primary_sources': primary})
    component['full_report'] = report.relative_to(folder).as_posix()
    return component, sources


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    parser.add_argument('--complete', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--daily-histories', action='store_true')
    args = parser.parse_args()
    folder = market_root(args.data_dir)
    if args.daily_histories:
        from .market_inventory import load_inventory
        inventory = load_inventory(folder)
        # A stale current quote does not erase a usable historical price book.
        # Preserve existing originals unless refresh was explicitly requested.
        results = collect_daily_histories(folder, inventory, args.as_of, refresh=args.refresh)
        synced = sync_daily_results(folder, inventory, results, args.as_of)
        publish_inventory(folder, synced)
    elif args.complete:
        collect_inventory(folder, args.as_of, refresh=args.refresh)
    else:
        rows, _, _ = collect_identities(folder, args.as_of)
        collect_quotes(folder, rows, args.as_of)
