"""Pinned public daily quotes for exact native CVM/FCA security identities.

All published sessions are retained. The ordinary quote gate is invoked on
unchanged provider bytes, without selecting matching dates or fitting factors.
Acquisition is explicit; the production getter is strictly offline/read-only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import socket
from unittest.mock import patch
from urllib.parse import urlencode, urljoin, urlsplit

from lxml import html

from . import round2_b3_eqma_recovery as native
from .bulk_pending_sources import PublicSourceCollector, _save
from .collect_market_data import month_end, shift_month
from .round2_share_actions import read
from .share_basis import factor_after, reconcile_prices


ROOT, CUTOFF, START = native.ROOT, native.CUTOFF, native.START
SCOPE = 'round3-identity-prices'
CACHE = 'raw/primary-research/all-pending/' + SCOPE
PROVIDER = 'literal_ibovx_daily_close_exact_native_security'
TEMPLATE_SHA = '85d819fc0f1c17f99a938fbe8477017277b826e93647ed5d25a10dabeac9799e'
TEMPLATE_FILE = f'raw/primary-research/all-pending/round2-actions/body-{TEMPLATE_SHA}.bin'
# Body and HTTP receipt are independently pinned, including discovery/retrieval.
PINS = {
    'B3SA3': ('90797f5a43c7c6cfe702635d2490b92b0086d771393e0e66eebc8f11ddfb7c1c',
              'c9090314290d2c741dd456599733a73e5ef11260fbf5f6bd3e12dd451017d55a'),
    'EQMA3B': ('f27d32d02cc614052a58fc587e10f9872acbeab9f5c6b227ee8bf66fe5181046',
               'c1260e10f6db4ff6d3aa0a9f1c7d9e846d0bafcba5271f3bf087775ab465d52c'),
    'BAUH4': ('17846ae3dce82d78a1f55e432d7e0b97cfd3ac9f252f807d52c6001370dc3b48',
              '82c5bad2828046d70f1283b46f944b98bb5467aa1bd7874725a719994ad96ba9'),
    'BMIN3': ('b21f42b103c577e1670483bf46c3c50e1714160f7065f709ebe702244527d5ab',
              '959c9d90699a9e6b94b7ceca2d4f0a7a1773815a4cdf04742be43dfc1dd7fafc'),
    'BMIN4': ('3ae0ffa0e05eda85d7398874029dc39923ff5d1b3ffdb8fe4de1b33d73054357',
              '50f8827354f2c7db6d4445b76f5bccbeabfe0d3a152d71ca08098e17d0fae083'),
    'CEGR3': ('9ec3f0c6f23539e132debf4eaffd2a65b21b45e4ca784261a80d13fe910b4378',
              'ad0f4cce0122400745e0c131aa6292bb362279757a49b1ac1122338868df4ba2'),
    'PEAB3': ('7521678d2432e93bf355c4b36f4c835cbb7d0b505e9458737b4e979b1e2609f4',
              'b3a35cfbf5f70dd877236596454d3ef970e13b8a2d3175d52c07e5ce3e15022f'),
    'PEAB4': ('dc9fee7716e877779c85fdba6aa84d0bd3d75965c9afe56239104b2dc6297d1d',
              'ba6243590b3387e1812b28425d94e7c93273a0e1d134362ea3dde7d31f3dc90e'),
}
TARGETS = tuple(PINS)
HEADERS = ['Data', 'Varia\u00e7\u00e3o %', 'Varia\u00e7\u00e3o', 'Cota\u00e7\u00e3o',
           'Abertura', 'M\u00ednimo', 'M\u00e1ximo', 'Volume', 'N\u00ba Neg\u00f3cios']


def require(value, reason):
    if not value:
        raise ValueError('identity_prices_' + reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def json_safe(value):
    if isinstance(value, dict):
        return {k.isoformat() if isinstance(k, date) else str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def binding(folder, ticker):
    if ticker in native.TARGETS:
        return native.Corpus(folder).current_binding(ticker)
    from .round2_more_identities import PrimaryIdentityAdapter
    return PrimaryIdentityAdapter(folder).evidence(ticker, CUTOFF)


def exact_identity(folder, identity, cutoff):
    require(type(cutoff) is date and cutoff == CUTOFF, 'cutoff_outside_pinned_window')
    observed = binding(folder, identity['ticker'])
    require(type(identity.get('cvm')) is int and all(identity.get(k) == getattr(observed, k)
            for k in ('ticker', 'cvm', 'share_class', 'isin')), 'exact_native_security_mismatch')
    if identity.get('cnpj') is not None:
        require(identity['cnpj'] == observed.cnpj, 'exact_native_CNPJ_mismatch')
    return observed


def published_request(folder, ticker):
    """The public navigation publishes papel, independently of the POST form."""
    body = read(folder, TEMPLATE_FILE, TEMPLATE_SHA)
    tree = html.fromstring(body.decode('utf8'))
    nodes = tree.xpath('/html/body/form/div[2]/div/a[2]')
    require(len(nodes) == 1 and nodes[0].get('href') == 'historico-papeis-bovespa.aspx?papel=petr4',
            'published_query_template_changed')
    require(tree.xpath('//input[@id="ContentPlaceHolderCentro_txtAcoesBovespa"]'),
            'published_ticker_control_missing')
    url = urljoin('https://ibovx.com.br/', nodes[0].get('href')).split('?')[0]
    return url + '?' + urlencode({'papel': ticker}), {
        'kind': 'published_public_history_ticker_query_control', 'source_file': TEMPLATE_FILE,
        'source_sha256': TEMPLATE_SHA, 'xpath': tree.getroottree().getpath(nodes[0]),
        'literal_href': nodes[0].get('href'), 'ticker': ticker, 'exact_native_identity_verified': True}


def acquire(folder=ROOT, tickers=TARGETS):
    """Only PublicSourceCollector can perform live GETs; stopped origins persist."""
    collector = PublicSourceCollector(folder, SCOPE)
    output = []
    for ticker in tickers:
        require(ticker in TARGETS, 'acquisition_security_outside_scope')
        binding(folder, ticker)
        url, discovery = published_request(folder, ticker)
        require(urlsplit(url).hostname == 'ibovx.com.br', 'acquisition_origin_outside_scope')
        result = collector.fetch(url, discovery=discovery)
        output.append(result)
        print(json.dumps({k: result.get(k) for k in ('url', 'status', 'error', 'sha256')}), flush=True)
    return output


def source(folder, ticker):
    digest, receipt_digest = PINS[ticker]
    url, discovery = published_request(folder, ticker)
    relative = f'{CACHE}/response-{sha(url.encode())}.json'
    receipt = json.loads(read(folder, relative, receipt_digest))
    require(receipt['url'] == url and receipt['scope'] == SCOPE and receipt['retries'] == 0
            and receipt['status'] == 200 and receipt['error'] is None
            and receipt['sha256'] == digest and receipt['file'] == f'{CACHE}/body-{digest}.bin'
            and receipt['discovery'] == discovery, 'HTTP_receipt_binding_changed')
    body = read(folder, receipt['file'], digest)
    require(len(body) == receipt['bytes'] and bool(receipt['retrieved_at']), 'HTTP_body_size_or_retrieval_changed')
    ref = {k: receipt[k] for k in ('file', 'sha256', 'bytes', 'url', 'retrieved_at')}
    ref.update(receipt_file=relative, receipt_sha256=receipt_digest,
               authority='public_quote_publisher_not_CVM_or_exchange',
               observed_at_basis='actual_HTTP_receipt', point_in_time_archive_verified=False)
    return body, ref


def br_decimal(value):
    require(bool(re.fullmatch(r'\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}', value)), 'invalid_native_price')
    result = Decimal(value.replace('.', '').replace(',', '.'))
    require(result.is_finite() and result > 0, 'nonpositive_native_price')
    return result


def parse_ibovx(body, ticker):
    tree = html.fromstring(body.decode('utf8'))
    title = ' '.join(tree.xpath('//title/text()'))
    require('ibovX - ' + ticker + '.' in title, 'native_title_security_mismatch')
    inputs = tree.xpath('//input[@id="ContentPlaceHolderCentro_txtAcoesBovespa"]')
    require(len(inputs) == 1 and inputs[0].get('value') == ticker, 'native_form_security_mismatch')
    tables = [t for t in tree.xpath('//table') if t.xpath('.//tr') and
              [c.text_content().strip() for c in t.xpath('.//tr')[0].xpath('./td|./th')] == HEADERS]
    require(len(tables) == 1, 'native_daily_schema_changed')
    result, previous = [], None
    for index, row in enumerate(tables[0].xpath('.//tr')[1:], 1):
        cells = [' '.join(c.text_content().split()) for c in row.xpath('./td')]
        require(len(cells) == 9, 'native_daily_row_schema_changed')
        day = datetime.strptime(cells[0], '%d/%m/%Y').date()
        require(day.weekday() < 5 and (previous is None or day < previous), 'duplicate_or_unordered_session')
        close, opened, low, high = (br_decimal(cells[i]) for i in (3, 4, 5, 6))
        require(low <= close <= high and low <= opened <= high, 'native_OHLC_inconsistent')
        require(cells[8].replace('.', '').isdigit() and int(cells[8].replace('.', '')) > 0,
                'inactive_or_unknown_native_session')
        result.append({'date': day.isoformat(), 'close_decimal': str(close), 'literal_cells': cells,
            'native_table_row': index, 'xpath': tree.getroottree().getpath(row),
            'date_role': 'actual_reported_trading_session', 'value_role': 'Cotacao_daily_close_not_open_or_average'})
        previous = day
    require(len(result) == 134, 'pinned_native_session_count_changed')
    return result


def provider_source(folder, ticker):
    summary = json.loads((Path(folder) / 'raw/inventory/daily-summary.json').read_bytes())
    receipt = summary['instruments'][ticker]['source']
    relative = f'raw/inventory/daily-prices/{ticker}.json'
    require(receipt['file'] == relative and urlsplit(receipt['url']).hostname == 'query1.finance.yahoo.com',
            'provider_receipt_binding_changed')
    body = read(folder, relative, receipt['sha256'])
    ref = {k: receipt[k] for k in ('file', 'sha256', 'url', 'retrieved_at')}
    return body, {**ref, 'bytes': len(body), 'authority': 'unchanged_original_provider_daily_file'}


def diagnose(folder, identity, cutoff=CUTOFF, observed_actions=None,
             independent_quotes=None, comparison_content=None):
    """Return every observation/conflict, even when the actual gate rejects it."""
    ticker = identity.get('ticker')
    if ticker not in TARGETS:
        return None
    observed = exact_identity(folder, identity, cutoff)
    body, ref = source(folder, ticker)
    rows = parse_ibovx(body, ticker)
    provider_body, provider_ref = provider_source(folder, ticker)
    require(comparison_content is None or comparison_content == provider_body, 'caller_provider_bytes_changed')
    from .broad_stocks import split_price_history
    _, actions = split_price_history(provider_body, ticker, cutoff, allow_recent_actions=True)
    require(observed_actions is None or observed_actions == actions, 'caller_actions_changed')
    result = json.loads(provider_body)['chart']['result'][0]
    quotes = result['indicators']['quote'][0]
    stamps = result['timestamp']
    require(len(stamps) == len(quotes['close']), 'provider_lengths_disagree')
    days = [datetime.fromtimestamp(t, timezone.utc).date() for t in stamps]
    require(len(set(days)) == len(days), 'duplicate_provider_dates')
    daily = {d: (i, float(quotes['close'][i])) for i, d in enumerate(days) if quotes['close'][i] is not None}
    anchors = dict(independent_quotes or {})
    require(all(type(d) is date and type(p) in (int, float) and math.isfinite(p) and p > 0
                for d, p in anchors.items()), 'invalid_original_independent_quote')
    source_conflicts = []
    comparisons = []
    for row in rows:
        day, value = date.fromisoformat(row['date']), float(row['close_decimal'])
        if not START <= day <= cutoff:
            continue
        if day in anchors and anchors[day] != value:
            source_conflicts.append({'date': row['date'], 'original_independent': anchors[day],
                                     'ibovx': value, 'xpath': row['xpath']})
        else:
            anchors[day] = value
        index, provider_value = daily.get(day, (None, None))
        factor = factor_after(day, actions, cutoff)
        comparisons.append({**row, 'provider_close': provider_value, 'provider_index': index,
            'provider_timestamp': stamps[index] if index is not None else None,
            'provider_OHLCV': {k: quotes[k][index] for k in ('open', 'high', 'low', 'close', 'volume')}
                if index is not None else None,
            'factor_after': factor,
            'raw_agrees': provider_value is not None and math.isclose(provider_value, value, rel_tol=.006, abs_tol=.015),
            'split_adjusted_agrees': provider_value is not None and math.isclose(
                provider_value, value / factor, rel_tol=.006, abs_tol=.015)})
    try:
        prices, dates, mode = reconcile_prices(provider_body, ticker, actions, anchors, cutoff,
                                               evidence_provider=PROVIDER)
        gate = {'passed': True, 'mode': mode, 'all_match_dates': dates, 'returned_months': len(prices)}
    except ValueError as error:
        gate = {'passed': False, 'reason': str(error)}
    if source_conflicts:
        gate = {**gate, 'passed': False, 'reason': 'identity_prices_independent_sources_disagree',
                'ordinary_gate_result_before_source_conflict_check': dict(gate)}
    months = {}
    for day, (_, value) in sorted(daily.items()):
        if day <= cutoff:
            months[month_end(day.year, day.month)] = (day, value)
    required = [shift_month(cutoff, -i) for i in range(61)]
    regimes = defaultdict(lambda: {'model_months': [], 'independent_dates': []})
    model = []
    for key in sorted(required):
        require(key in months, 'missing_required_provider_month')
        day, value = months[key]
        factor = round(factor_after(day, actions, cutoff), 12)
        model.append({'month': key.isoformat(), 'actual_trading_date': day.isoformat(),
                      'unchanged_provider_close': value, 'factor_after': factor})
        regimes[factor]['model_months'].append(key.isoformat())
    for day in sorted(anchors):
        if START <= day <= cutoff:
            regimes[round(factor_after(day, actions, cutoff), 12)]['independent_dates'].append(day.isoformat())
    proof = {'identity': asdict(observed), 'native_ISIN_not_inferred_from_suffix': True,
        'identity_link_rule': 'literal_exact_FCA_code_and_CVM_native_form_ISIN_class_CNPJ',
        'ordinary_gate': gate, 'observed_provider_actions': actions,
        'window_actions': [a for a in actions if START < date.fromisoformat(a['date']) <= cutoff],
        'all_native_observations': rows, 'all_window_comparisons': comparisons,
        'future_observations_not_used': [r for r in rows if r['date'] > cutoff.isoformat()],
        'pre_window_observations_not_used': [r for r in rows if r['date'] < START.isoformat()],
        'conflicts': [r for r in comparisons if not r['raw_agrees'] and not r['split_adjusted_agrees']],
        'independent_source_conflicts': source_conflicts,
        'all_61_model_months': model,
        'regimes': [{'factor_after': f, **r} for f, r in sorted(regimes.items())],
        'original_independent_quotes': json_safe(independent_quotes or {}),
        'all_observations_retained': True, 'favorable_dates_selected': False,
        'provider_bytes_or_days_replaced': False, 'adjclose_used': False,
        'factors_fitted_to_quotes': False, 'gate_tolerance_modified': False,
        'independent_publication_channel': True, 'independent_ultimate_upstream_verified': False,
        'exchange_or_issuer_quote_authority_claimed': False,
        'point_in_time_archive_verified': False,
        'five_year_unchanged_identity_requirement_added': False,
        'price_gate_only_not_full_security_admission': True}
    return {'provider': PROVIDER, 'raw_quotes': anchors, 'sources': [ref, provider_ref], 'proof': proof}


def load_evidence(folder, identity, cutoff=CUTOFF, observed_actions=None,
                  independent_quotes=None, comparison_content=None):
    """Production-usable offline getter: conflicting evidence never becomes approval."""
    evidence = diagnose(folder, identity, cutoff, observed_actions, independent_quotes, comparison_content)
    if evidence is not None:
        gate = evidence['proof']['ordinary_gate']
        if not gate['passed']:
            raise ValueError(gate['reason'])
    return evidence


@dataclass(frozen=True)
class NativeIdentityView:
    """Typed boundary for _one's optional industry lookup, not a B3 payload."""
    evidence: native.CurrentSecurityBinding

    def get(self, key, default=None):
        require(key == 'industryClassification', 'unsupported_legacy_identity_lookup')
        return default


def protected(folder):
    root = Path(folder)
    names = ['current.json', 'inventory/current.json', 'stock-selection/current.json']
    names += [f'raw/inventory/daily-prices/{s}.json' for s in TARGETS]
    names += [f'raw/expanded/{s}-{kind}' for s in TARGETS
              for kind in ('prices.json', 'payments.html', 'b3-cash-1.json')]
    result = {n: sha((root / n).read_bytes()) for n in names if (root / n).is_file()}
    for name in ('inventory_candidates.py', 'share_basis.py', 'issuer_independent_evidence.py', 'market_inventory.py'):
        result['code/' + name] = sha((Path(__file__).parent / name).read_bytes())
    return result


def isolated_replay(folder=ROOT):
    """Dispatch already recovered typed identity/cash and new quotes into real _one.

    No action validator, cash arithmetic, price arithmetic or financial gate is
    patched. Thus a newly exposed unsupported action dispatcher stays pending.
    """
    from . import inventory_candidates as core
    from . import issuer_independent_evidence as independent
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    corpus, inventory = native.Corpus(root), load_inventory(root)
    calendars = {'B3SA3': native.recover_b3_calendar(corpus), 'EQMA3B': native.recover_eqma_calendar(corpus)}
    before = protected(root)
    old_identity, old_payments, old_evidence = core._identity, core._payments, independent.load_evidence
    old_gate, old_one = core.reconcile_prices, core._one
    output = []

    def identity_hook(row, cache, cutoff):
        original = old_identity(row, cache, cutoff)
        if row['ticker'] not in native.TARGETS or original[0] is not None:
            return original
        observed = corpus.current_binding(row['ticker'])
        exact_identity(root, {**row, 'isin': row.get('isin') or observed.isin}, cutoff)
        for ref in observed.sources:
            cache.register_research_evidence(ref)
        return NativeIdentityView(observed), {observed.ticker: observed.isin}

    def payment_hook(ticker, cache, cutoff, primary=None, *, coverage_start=None, identity=None):
        if ticker not in native.TARGETS:
            return old_payments(ticker, cache, cutoff, primary, coverage_start=coverage_start, identity=identity)
        require(cutoff == CUTOFF and (coverage_start is None or coverage_start >= START),
                'earlier_cash_not_verified')
        return native.payment_history(calendars[ticker], coverage_start=coverage_start or START), None

    def evidence_hook(folder, identity, cutoff):
        if identity['ticker'] not in native.TARGETS:
            return old_evidence(folder, identity, cutoff)
        exact_identity(folder, identity, cutoff)
        return {'provider': 'native_CVM_calendar_with_typed_quote_sidecar',
            'expected_cash': native.expected_cash(calendars[identity['ticker']]), 'raw_quotes': {},
            'sources': native.receipts_in(calendars[identity['ticker']]['events']),
            'checks': {'documentary_cash_recovery_dispatch_only': True,
                       'independent_ultimate_cash_authors_verified': False}}

    def forbidden(*args, **kwargs):
        raise AssertionError('identity_prices_replay_network_forbidden')

    with patch.object(socket.socket, 'connect', forbidden), patch.object(socket.socket, 'connect_ex', forbidden), \
         patch.object(socket, 'create_connection', forbidden), patch.object(socket, 'getaddrinfo', forbidden), \
         patch.object(core, '_identity', identity_hook), patch.object(core, '_payments', payment_hook), \
         patch.object(independent, 'load_evidence', evidence_hook):
        for ticker in native.TARGETS:
            row = next(r for r in inventory['stocks'] if r['ticker'] == ticker)
            observed = corpus.current_binding(ticker)
            identity = {**row, 'isin': observed.isin}
            checks, calls = {}, []
            cache = core._Cache(root, inventory)

            def gate_hook(content, symbol, actions, original_quotes, cutoff, **kwargs):
                if symbol != ticker or kwargs.get('coverage_start') is not None:
                    return old_gate(content, symbol, actions, original_quotes, cutoff, **kwargs)
                evidence = diagnose(root, identity, cutoff, actions, original_quotes, content)
                checks['identity_prices'] = evidence['proof']
                for ref in evidence['sources']:
                    cache.register_research_evidence(ref)
                calls.append({'all_anchors': len(evidence['raw_quotes']),
                    'provider_sha256': sha(content), 'gate': evidence['proof']['ordinary_gate']})
                # Preserve both raw-source conflicts and the original arithmetic.
                require(not evidence['proof']['independent_source_conflicts'], 'independent_sources_disagree')
                return old_gate(content, symbol, actions, evidence['raw_quotes'], cutoff,
                                evidence_provider=PROVIDER, **{k: v for k, v in kwargs.items() if k != 'evidence_provider'})

            try:
                with patch.object(core, 'reconcile_prices', gate_hook):
                    candidate = core._one(row, cache, CUTOFF, checks)
                outcome = {'source_candidate': True, 'ledger_rows': len(candidate['ledger']),
                    'price_matches': candidate['independent_price_matches'],
                    'readiness_scope': candidate['readiness_scope']}
            except (ValueError, KeyError, TypeError, IndexError) as error:
                outcome = {'source_candidate': False, 'pending_reason': str(error)}
            output.append({'ticker': ticker, **outcome, 'checks': checks, 'quote_gate_calls': calls,
                           'source_receipts': cache.all_receipts()})
    require(core._identity is old_identity and core._payments is old_payments and core._one is old_one
            and independent.load_evidence is old_evidence and core.reconcile_prices is old_gate, 'hooks_not_restored')
    require(protected(root) == before, 'protected_files_changed')
    gate_text, gate_line = inspect.getsourcelines(reconcile_prices)
    return {'results': output, 'normal_one_called': True, 'cash_and_price_arithmetic_unchanged': True,
        'primary_action_validator_patched': False, 'financial_gate_patched': False,
        'production_integration': False, 'model_admissions': [], 'network_calls': 0,
        'protected_before': before, 'protected_after': protected(root),
        'original_reconcile_prices': {'file': inspect.getsourcefile(reconcile_prices),
            'first_line': gate_line, 'literal': ''.join(gate_text)},
        'process_local_dispatch_only': True, 'no_fake_exchange_payload': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--acquire', action='store_true')
    parser.add_argument('--replay', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if args.acquire:
        acquire(args.root)
    if args.report:
        require(args.report.name.startswith('identity-prices-') and args.report.suffix == '.json', 'report_prefix_required')
        def offline(*args, **kwargs):
            raise AssertionError('identity_prices_report_network_forbidden')
        with patch.object(socket.socket, 'connect', offline), patch.object(socket, 'getaddrinfo', offline):
            records = []
            for ticker in TARGETS:
                observed = binding(args.root, ticker)
                records.append(diagnose(args.root, {k: getattr(observed, k)
                    for k in ('ticker', 'cvm', 'share_class', 'isin')}))
            report = {'schema_version': 1, 'cutoff': CUTOFF, 'coverage_start': START,
                'producer_sha256': sha(Path(__file__).read_bytes()), 'records': records,
                'isolated_replay': isolated_replay(args.root) if args.replay else None,
                'model_admissions': [], 'core_edited': False, 'pointers_edited': False}
        _save(args.report, (json.dumps(json_safe(report), ensure_ascii=True, indent=2, allow_nan=False) + '\n').encode())
        print(args.report)


if __name__ == '__main__':
    main()
