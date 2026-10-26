"""Independent public quote feed using an issuer-published provider identifier.

The template is independently published by another issuer using the same public
provider. It is not claimed to be the unavailable Itausa JavaScript bundle.
No denied CDN, exchange endpoint, authentication or proxy is requested.
"""

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request

from .celesc_history_research import ROOT
from .dividend_scope import allowed_tickers
from .selected_issuer_sources import _dump, _require, _save, source_body


CACHE = 'raw/primary-research/published-issuer-quotes'
PAGE = 'raw/primary-research/alternate-issuer-history/body-4a3e4cfeb18c22eb524eaab0453246467d80942b9d3b6e3d3686f3fa13af7f5a.bin'
PAGE_SHA = '4a3e4cfeb18c22eb524eaab0453246467d80942b9d3b6e3d3686f3fa13af7f5a'
PAGE_URL = 'https://ri.itausa.com.br/servicos-aos-investidores/graficos-e-cotacoes-itsa4/'
PROVIDER_ID = '16c81f44-190e-11ef-9292-127822a4adef'
MAX_BYTES = 8_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('provider_redirect_not_followed')


def request_proof(root, ticker, *, require_fresh_robots=True):
    root = Path(root).resolve()
    _require(ticker in ('ITSA3', 'ITSA4') and ticker in (allowed_tickers(root) or ()),
             'issuer_quote_outside_selected_scope')
    body = (root / PAGE).read_bytes()
    _require(hashlib.sha256(body).hexdigest() == PAGE_SHA, 'published_issuer_identifier_source_changed')
    page = body.decode('utf-8')
    _require(set(re.findall(r'stockinfoId:\s*"([a-f0-9-]{36})"', page)) == {PROVIDER_ID}
             and all(re.search(r'id_stockinfo:\s*"' + PROVIDER_ID + r'",\s*name:\s*"' + code + '"', page)
                     for code in ('ITSA3', 'ITSA4'))
             and "currencySymbol: 'R$'" in page, 'unverified_provider_security_configuration')
    prior = root / 'raw/primary-research/selected-issuer-sources'
    bundle = json.loads((prior / 'response-009.json').read_bytes())
    script = source_body(root, bundle).decode('utf-8')
    _require(bundle['sha256'] == '19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0'
             and bundle['url'] == 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js'
             and bundle['status'] == 200 and not bundle['stop'] and all(token in script for token in (
                 'API_STOCKINFO_URL:"https://api.mziq.com/mzstockinfo"',
                 't.API_URL+"/"+t.STOCK_ID+"/history?from="', 't.ADJUSTED=!1',
                 '"&tickers="+t.TICKER', '"&adjusted="+t.ADJUSTED')),
             'unverified_independent_public_provider_template')
    robot = json.loads((prior / 'response-006.json').read_bytes())
    _require(robot['url'] == 'https://api.mziq.com/robots.txt' and robot['status'] == 404
             and (not require_fresh_robots
                  or robot['completed_at'][:10] == datetime.now(timezone.utc).date().isoformat()),
             'fresh_provider_robots_observation_required')
    source_body(root, robot)
    url = f'https://api.mziq.com/mzstockinfo/{PROVIDER_ID}/history?from=2021-09-01&to=2026-09-30&tickers={ticker}&adjusted=false'
    return {'url': url, 'ticker': ticker, 'issuer_identifier': PROVIDER_ID,
            'issuer_configuration': {'file': PAGE, 'sha256': PAGE_SHA, 'url': PAGE_URL},
            'public_provider_template': bundle, 'robots_observation': robot,
            'issuer_bundle_version_verified': False, 'requested_adjusted': False,
            'authority': 'independent_provider_from_public_issuer_configuration_not_exchange_proxy'}


def read_original(root, receipt):
    root = Path(root).resolve()
    relative = receipt.get('file', '')
    _require(re.fullmatch(CACHE + r'/original-[a-f0-9]{64}\.bin', relative), 'unsafe_provider_quote_reference')
    path = root / relative
    _require(not path.is_symlink() and path.resolve().is_relative_to(root)
             and path.stat().st_size == receipt['bytes'] <= MAX_BYTES, 'unsafe_provider_quote_original')
    body = path.read_bytes()
    _require(hashlib.sha256(body).hexdigest() == receipt['sha256'], 'provider_original_changed')
    return body


def acquire(root=ROOT):
    root = Path(root).resolve()
    base = root / CACHE
    base.mkdir(parents=True, exist_ok=True)
    _require(not base.is_symlink() and base.resolve().is_relative_to(root), 'unsafe_provider_cache')
    requests = [json.loads(path.read_bytes()) for path in sorted(base.glob('request-*.json'))]
    responses = [json.loads(path.read_bytes()) for path in sorted(base.glob('response-*.json'))]
    _require(len(requests) == len(responses) <= 2
             and [row['ordinal'] for row in requests] == list(range(1, len(requests) + 1))
             and all(row['status'] == 200 and not row['error'] for row in responses),
             'prior_or_interrupted_provider_acquisition_stopped')
    for row in responses:
        read_original(root, row)
    for ticker in ('ITSA3', 'ITSA4'):
        if ticker in {row['ticker'] for row in requests}:
            continue
        proof = request_proof(root, ticker)
        ordinal = len(responses) + 1
        request_record = {'ordinal': ordinal, **proof, 'started_at': datetime.now(timezone.utc).isoformat(),
                          'automatic_retries': 0, 'redirects': False, 'authentication': 'none', 'proxy': False}
        _save(base, f'request-{ordinal:03d}.json', _dump(request_record))
        body, status, error, headers = b'', None, None, {}
        try:
            request = urllib.request.Request(proof['url'], headers={
                'User-Agent': 'RendaFutura-Academic/1.0', 'Accept': 'application/json',
                'Accept-Encoding': 'identity'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=30) as response:
                status, headers, body = response.status, response.headers, response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as failure:
            status, headers, body, error = failure.code, failure.headers, failure.read(MAX_BYTES + 1), type(failure).__name__
        except (ValueError, OSError) as failure:
            error = type(failure).__name__
        digest = hashlib.sha256(body).hexdigest()
        _save(base, f'original-{digest}.bin', body)
        result = {**request_record, 'status': status, 'error': error, 'sha256': digest, 'bytes': len(body),
                  'file': CACHE + f'/original-{digest}.bin', 'http_date': headers.get('Date'),
                  'retrieved_at': datetime.now(timezone.utc).isoformat(), 'model_admitted': False}
        _save(base, f'response-{ordinal:03d}.json', _dump(result))
        responses.append(result)
        if status != 200 or error or len(body) > MAX_BYTES:
            break
    return responses


def quote_rows(body, ticker):
    payload = json.loads(body)
    data = payload.get('data', {})
    _require(payload.get('success') is True and data.get('errors') == []
             and data.get('source') == 'Enfoque' and data.get('link') == 'https://www.enfoque.com.br'
             and [row.get('ticker') for row in data.get('tickers', [])] == [ticker],
             'provider_quote_identity_or_source_mismatch')
    quotes, months = {}, set()
    for row in data['tickers'][0]['history']:
        day, value = row.get('date'), row.get('closed')
        _require(isinstance(day, str) and date.fromisoformat(day).isoformat() == day
                 and '2021-09-01' <= day <= '2026-09-30' and day not in quotes
                 and type(value) in (int, float) and math.isfinite(value) and value > 0,
                 'invalid_or_duplicate_provider_quote')
        quotes[day] = value
        months.add(day[:7])
    expected = {f'{year:04d}-{month:02d}' for year in range(2021, 2027) for month in range(1, 13)
                if '2021-09' <= f'{year:04d}-{month:02d}' <= '2026-09'}
    _require(months == expected and len(months) == 61, 'provider_quote_window_missing_61_anchors')
    return {date.fromisoformat(day): value for day, value in sorted(quotes.items())}


def inspect_alignment(root, ticker):
    """Retain every disagreement; do not fit additional price adjustment factors."""
    from .broad_stocks import split_price_history
    from .share_basis import factor_after, reconcile_prices

    root = Path(root).resolve()
    _require(ticker in ('ITSA3', 'ITSA4') and ticker in (allowed_tickers(root) or ()),
             'issuer_quote_outside_selected_scope')
    receipts = [json.loads(path.read_bytes()) for path in sorted((root / CACHE).glob('response-*.json'))]
    matches = [row for row in receipts if row['ticker'] == ticker]
    _require(len(matches) == 1, 'unique_provider_response_required')
    receipt, = matches
    _require(receipt['status'] == 200 and not receipt['error'], 'unusable_provider_response')
    independent = quote_rows(read_original(root, receipt), ticker)
    relative = 'raw/expanded/' + ticker + '-prices.json'
    if not (root / relative).is_file():
        relative = 'raw/inventory/daily-prices/' + ticker + '.json'
    path = root / relative
    _require(not path.is_symlink() and path.resolve().is_relative_to(root)
             and path.stat().st_size <= MAX_BYTES, 'unsafe_secondary_original')
    body = path.read_bytes()
    cutoff = date(2026, 9, 30)
    _, actions = split_price_history(body, ticker, cutoff, allow_recent_actions=True)
    chart = json.loads(body)['chart']['result'][0]
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): value
             for stamp, value in zip(chart['timestamp'], chart['indicators']['quote'][0]['close'])
             if value is not None}
    conflicts = []
    for day, value in independent.items():
        factor = factor_after(day, actions, cutoff)
        expected = value / factor
        if day not in daily or not math.isclose(daily[day], expected, rel_tol=.006, abs_tol=.015):
            conflicts.append({'date': day.isoformat(), 'independent_reported_close': value,
                              'secondary_reported_close': daily.get(day), 'reported_bonus_factor': factor,
                              'expected_split_adjusted_close': expected})
    try:
        _, anchors, mode = reconcile_prices(body, ticker, actions, independent, cutoff)
        price_status = {'status': 'price_gate_passed_not_model_admission', 'mode': mode, 'anchors': len(anchors)}
    except ValueError as error:
        price_status = {'status': 'blocked', 'reason': str(error)}
    return {'ticker': ticker, 'cutoff': cutoff.isoformat(), 'independent_daily_rows': len(independent),
            'independent_monthly_anchors': len({day.strftime('%Y-%m') for day in independent}),
            'normal_price_gate': price_status, 'conflicts': conflicts,
            'extra_factors_fitted': False, 'missing_prices_created': False, 'model_admitted': False,
            'corporate_action_type_inferred_from_price_ratio': False,
            'sources': [{'file': receipt['file'], 'sha256': receipt['sha256'], 'bytes': receipt['bytes'],
                         'url': receipt['url'], 'retrieved_at': receipt['retrieved_at']},
                        {'file': relative, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
                         'url': None, 'retrieved_at': None, 'basis': 'existing_secondary_original'}]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, default=ROOT)
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    if args.inspect:
        reports = [inspect_alignment(args.folder, ticker) for ticker in ('ITSA3', 'ITSA4')]
        content = _dump(reports)
        path = _save(args.folder / CACHE, 'alignment-' + hashlib.sha256(content).hexdigest() + '.json', content)
        print(json.dumps({'file': str(path), 'results': [
            {'ticker': row['ticker'], 'daily': row['independent_daily_rows'],
             'conflicts': len(row['conflicts']), 'gate': row['normal_price_gate']} for row in reports]}, indent=2))
    else:
        print(json.dumps([{'ticker': row['ticker'], 'status': row['status'], 'bytes': row['bytes'],
                           'error': row['error']} for row in acquire(args.folder)], indent=2))
