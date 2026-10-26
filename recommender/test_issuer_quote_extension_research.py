"""Offline cache replay and adversarial boundaries for the isolated extension."""

from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from . import issuer_quote_extension_research as q


ROOT = Path(__file__).parent / 'data/market'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('test_attempted_live_network')
    monkeypatch.setattr(q.urllib.request.OpenerDirector, 'open', forbidden)


@pytest.fixture(scope='module')
def evidence():
    return {t: (q.pinned_history(ROOT, t)[0], q.primary_actions(ROOT, t)) for t in q.TARGETS}


def receipt(ordinal):
    return next(r for r in q.records(ROOT, 'response') if r['ordinal'] == ordinal)


@pytest.mark.parametrize('ticker,first,last,anchors,conflicts', [
    ('LREN3', '37.5', '11.6', 21, 7),
    ('PSSA3', '56.54', '47.29', 19, 4),
])
def test_actual_history_identity_raw_units_and_all_conflicts(evidence, ticker, first, last, anchors, conflicts):
    rows, actions = evidence[ticker]
    assert len(rows) == len({r['date'] for r in rows}) == 1268
    assert rows[0]['date'] == '2021-09-01' and rows[-1]['date'] == '2026-09-30'
    assert rows[0]['closed'] == first and rows[-1]['closed'] == last
    identity = q.security_identity(ROOT, ticker)
    assert identity['security_row']['Codigo_Negociacao'] == ticker
    assert identity['native_share_class'] == 'ON'
    native = q.native_basis_checks(rows, actions)
    assert native['all_daily_turnover_share_OHLC_checks'] == 1268
    assert native['violations'] == []
    assert not native['adjusted_false_alone_used_as_raw_proof']
    reference = q.reference_comparison(ROOT, ticker, rows)
    assert reference['matches'] == reference['anchor_dates'] == anchors
    assert reference['mismatches'] == reference['conflicts'] == []
    daily = q.daily_comparison(ROOT, ticker, rows, actions)
    assert daily['daily_strict_matches'] == 1268 - conflicts
    assert len(daily['daily_strict_mismatches']) == conflicts
    assert len(daily['monthly_raw_anchors']) == 61
    assert not daily['missing_dates']
    assert not daily['fitted_factors'] and not daily['conflict_rows_replaced']
    assert not daily['model_admission_granted']


def test_exact_primary_bonus_ratios_and_ex_date_boundaries(evidence):
    lren = evidence['LREN3'][1]
    pssa = evidence['PSSA3'][1]
    assert [a['kind'] for a in lren + pssa] == ['share_bonus'] * 3
    assert q.factor_after('2021-11-04', lren) == Decimal('1.21')
    assert q.factor_after('2021-11-05', lren) == Decimal('1.1')
    assert q.factor_after('2024-12-11', lren) == Decimal('1.1')
    assert q.factor_after('2024-12-12', lren) == 1
    assert q.factor_after('2021-10-20', pssa) == 2
    assert q.factor_after('2021-10-21', pssa) == 1
    assert all(a['primary_ratios_verified'] for a in lren + pssa)
    assert all(not a['settlement_or_fractional_cash_proof'] for a in lren + pssa)


def test_material_month_end_conflict_is_retained(evidence):
    daily = q.daily_comparison(ROOT, 'LREN3', *evidence['LREN3'])
    assert len(daily['monthly_conflicts']) == 1
    conflict, = daily['monthly_conflicts']
    assert conflict['date'] == '2024-08-30'
    assert conflict['raw_close_BRL'] == '17.01'
    assert Decimal(conflict['cached_Yahoo_close']) * Decimal('1.1') > Decimal('17.29')
    assert len(daily['existing_point_six_percent_band_mismatches']) == 3
    pssa = q.daily_comparison(ROOT, 'PSSA3', *evidence['PSSA3'])
    assert not pssa['monthly_conflicts']
    assert not pssa['existing_point_six_percent_band_mismatches']
    assert len(pssa['daily_strict_mismatches']) == 4


def test_false_parameter_alone_cannot_prove_native_share_basis(evidence):
    rows, actions = deepcopy(evidence['LREN3'])
    for row in rows:
        factor = q.factor_after(row['date'], actions)
        for key in ('open', 'high', 'low', 'closed', 'middle'):
            row[key] = str(Decimal(row[key]) / factor)
    checks = q.native_basis_checks(rows, actions)
    assert checks['violations']
    assert checks['adjusted_false_alone_used_as_raw_proof'] is False


def test_altered_pinned_bytes_rejected():
    r = receipt(13)
    body = q.read(ROOT, r)
    with pytest.raises(ValueError, match='unreviewed_history_ticker_or_hash'):
        q.parse_payload(body + b' ', 'LREN3', r['sha256'])


@pytest.mark.parametrize('mutation,reason', [
    ('ticker', 'exact_history_ticker'), ('extra_ticker', 'exact_history_ticker'),
    ('date', 'row_schema'), ('duplicate_date', 'duplicate_or_out_of_range'),
    ('future', 'duplicate_or_out_of_range'), ('bool', 'invalid_history_number'),
    ('string', 'invalid_history_number'), ('ohlc', 'invalid_OHLC'),
    ('zero_shares', 'invalid_OHLC'), ('fractional_trade', 'invalid_OHLC'),
    ('error', 'vendor_schema_or_authority'), ('authority', 'vendor_schema_or_authority'),
    ('schema', 'row_schema'), ('unsuccessful', 'history_not_successful'),
])
def test_financial_parser_rejects_semantic_corruption(mutation, reason):
    data = json.loads(q.read(ROOT, receipt(13)))
    ticker = data['data']['tickers'][0]
    row = ticker['history'][0]
    if mutation == 'ticker': ticker['ticker'] = 'LREN4'
    elif mutation == 'extra_ticker': data['data']['tickers'].append(deepcopy(ticker))
    elif mutation == 'date': row['date'] = '30/09/2026'
    elif mutation == 'duplicate_date': ticker['history'].append(deepcopy(row))
    elif mutation == 'future': row['date'] = '2026-10-01'
    elif mutation == 'bool': row['closed'] = True
    elif mutation == 'string': row['closed'] = '11.60'
    elif mutation == 'ohlc': row['closed'] = row['high'] + 1
    elif mutation == 'zero_shares': row['share'] = 0
    elif mutation == 'fractional_trade': row['trade'] = 1.5
    elif mutation == 'error': data['data']['errors'] = ['missing']
    elif mutation == 'authority': data['data']['source'] = 'Yahoo'
    elif mutation == 'schema': row['adjustedClose'] = row['closed']
    elif mutation == 'unsuccessful': data['success'] = 1
    body = json.dumps(data).encode()
    with pytest.raises(ValueError, match=reason):
        q.parse_payload(body, 'LREN3', q.sha(body))


@pytest.mark.parametrize('body,reason', [
    (b'{"success":true,"success":false}', 'duplicate_JSON_key'),
    (b'{"success":NaN}', 'nonfinite_JSON_number'),
    (b'{"success":Infinity}', 'nonfinite_JSON_number'),
])
def test_ambiguous_JSON_is_rejected(body, reason):
    with pytest.raises(ValueError, match=reason):
        q.parse_payload(body, 'LREN3', q.sha(body))


def test_conflicting_reference_quotes_are_all_retained(tmp_path, monkeypatch, evidence):
    daily, cash, detail = q.cached_inputs(ROOT, 'LREN3')
    page = json.loads(q.read(ROOT, cash))
    selected = next(r for r in page['results'] if r['dateClosingPricePriorExDate'] == '22/09/2026')
    changed = {**selected, 'closingPricePriorExDate': '99,99'}
    page['results'].append(changed)
    page['page']['totalRecords'] += 1
    body = q.dump(page)
    name = q.save(tmp_path, 'reference-fixture', body, raw=True)
    monkeypatch.setattr(q, 'cached_inputs', lambda root, ticker: (daily, {'file': name, 'sha256': q.sha(body)}, detail))
    result = q.reference_comparison(tmp_path, 'LREN3', evidence['LREN3'][0])
    conflict, = result['conflicts']
    assert conflict['date'] == '2026-09-22'
    assert {r['raw_close_BRL'] for r in conflict['all_reference_rows']} == {'11.51', '99.99'}
    assert not conflict['matches_all_reference_values']
    assert result['matches'] == 20 and len(result['mismatches']) == 1


@pytest.mark.parametrize('url', [
    'https://sistemaswebb3-listados.b3.com.br/anything',
    'https://www.bmfbovespa.com.br/anything', 'https://www.bvmf.com.br/anything',
    'https://ri.azzas2154.com.br/', 'https://allos.com.br/',
    'https://www.santander.com.br/ri/', 'https://www.engie.com.br/',
    'https://cdn-sites-assets.mziq.com/robots.txt',
    'https://brapi.dev/api/quote/LREN3?range=5y',
    'https://api.mziq.com/login', 'https://api.mziq.com/wp-signup.php',
    'https://example.invalid/?target=https%253A%252F%252Fb3.com.br',
    'https://example.invalid/proxy', 'https://user:secret@example.invalid/',
    'https://127.0.0.1/', 'https://example.invalid:444/',
])
def test_forbidden_routes_rejected_without_HTTP(url):
    assert not q.allowed(url)


def test_unpublished_ticker_or_company_ID_cannot_be_requested():
    f = q.Fetcher(ROOT)
    with pytest.raises(ValueError, match='forbidden_URL_or_ticker'):
        f.get('https://ri.lojasrenner.com.br/', 'FRAS3')
    with pytest.raises(ValueError, match='exact_FCA_seed'):
        f.get('https://api.mziq.com/mzstockinfo/invented/history?tickers=LREN3', 'LREN3')
    with pytest.raises(ValueError, match='unconfigured_ticker'):
        q.widget_request(ROOT, 'PSSA3', receipt(11), receipt(13)['discovery']['bundle'])
    with pytest.raises(ValueError, match='exact_configured_ticker'):
        q.widget_request(ROOT, 'PSSA3', receipt(8), receipt(15)['discovery']['bundle'])


def test_live_configuration_only_authorizes_exact_published_ticker():
    history = receipt(15)
    page, bundle = history['discovery']['page'], history['discovery']['bundle']
    url, proof = q.widget_request(ROOT, 'PSSA3', page, bundle, kind='live')
    assert url.endswith('/d7fe71bc-e1bb-11e8-a064-0a3f429ecfb6/live') and '?' not in url
    assert proof['parameters'] == {}
    url, proof = q.widget_request(ROOT, 'PSSA3', page, bundle, configuration=receipt(14))
    assert 'tickers=PSSA3&adjusted=false' in url
    assert proof['parameters']['tickers'] == 'PSSA3'
    q.verify_chain(ROOT, history)


@pytest.mark.parametrize('status', [401, 403, 429])
@pytest.mark.parametrize('ticker,url', [
    ('LREN3', 'https://other.invalid/alternate'),
    ('PSSA3', 'https://example.invalid/alternate'),
])
def test_denial_stops_issuer_and_origin_without_alternate_routes(tmp_path, monkeypatch, status, ticker, url):
    monkeypatch.setattr(q.Fetcher, 'baseline', lambda self: None)
    attempt = {'ordinal': 1, 'ticker': 'LREN3', 'url': 'https://example.invalid/robots.txt'}
    q.save(tmp_path, 'request', attempt)
    q.save(tmp_path, 'response', {**attempt, 'http_status': status, 'stop_reason': f'HTTP_{status}'})
    with pytest.raises(ValueError, match='denied_chain_or_origin'):
        q.Fetcher(tmp_path)._wire(url, ticker, {})
    assert len(q.records(tmp_path, 'request')) == 1


def test_robots_block_persists_across_origins(tmp_path, monkeypatch):
    monkeypatch.setattr(q.Fetcher, 'baseline', lambda self: None)
    q.save(tmp_path, 'blocked', {'ticker': 'LREN3', 'url': 'https://example.invalid/x', 'stop_reason': 'robots_disallows'})
    with pytest.raises(ValueError, match='denied_chain_or_origin'):
        q.Fetcher(tmp_path)._wire('https://other.invalid/x', 'LREN3', {})
    assert not q.records(tmp_path, 'request')


def test_budget_counts_failed_attempts_and_refuses_twenty_first(tmp_path, monkeypatch):
    monkeypatch.setattr(q.Fetcher, 'baseline', lambda self: None)
    for n in range(1, 21):
        attempt = {'ordinal': n, 'ticker': 'LREN3', 'url': f'https://example.invalid/{n}'}
        q.save(tmp_path, 'request', attempt)
        q.save(tmp_path, 'response', {**attempt, 'http_status': 404, 'stop_reason': None})
    with pytest.raises(ValueError, match='twenty_request_budget_exhausted'):
        q.Fetcher(tmp_path)._wire('https://example.invalid/new', 'LREN3', {})
    assert len(q.records(tmp_path, 'request')) == 20


def test_interrupted_request_is_consumed(tmp_path, monkeypatch):
    monkeypatch.setattr(q.Fetcher, 'baseline', lambda self: None)
    q.save(tmp_path, 'request', {'ordinal': 1, 'ticker': 'LREN3', 'url': 'https://example.invalid/x'})
    with pytest.raises(ValueError, match='interrupted_request_consumed'):
        q.Fetcher(tmp_path)._wire('https://example.invalid/y', 'LREN3', {})


def test_real_budget_is_sealed_and_has_no_retries():
    attempts = q.records(ROOT, 'request')
    assert len(attempts) == 20
    assert len({r['url'] for r in attempts}) == 20
    assert all(r['retries'] == 0 and not r['proxy'] and not r['automatic_redirects'] for r in attempts)
    with pytest.raises(ValueError, match='acquisition_sealed'):
        q.Fetcher(ROOT)._wire('https://example.invalid/no-more', 'LREN3', {})


def test_captcha_review_keeps_receipts_and_never_suppresses_real_denials():
    for n in (8, 11):
        r = receipt(n)
        assert r['stop_reason'] == 'captcha_scope_unresolved_stop'
        assert q.effective_stop(r) is None
        assert q.classify_body(q.read(ROOT, r)) == 'reviewed_library_only_no_public_content_guard'
        assert q.effective_stop({**r, 'http_status': 403, 'stop_reason': 'HTTP_403'}) == 'HTTP_403'
    assert q.classify_body(b'<title>Just a moment</title>') == 'public_content_access_challenge'
    assert q.classify_body(b'<div class="g-recaptcha">Prove access</div>') == 'captcha_scope_unresolved_stop'
    assert q.classify_body(q.read(ROOT, receipt(10))) == 'optional_form_captcha_public_content_present_not_solved'


def test_empty_HTTP_200_download_is_not_primary_action_proof():
    empty = q.read(ROOT, receipt(17))
    assert not empty.startswith(b'%PDF-')
    assert q.classify_body(empty) == 'no_challenge_detected'
    assert q.html.fromstring(empty).text_content().strip() == 'ENET Download de Documento'


def test_report_is_offline_and_never_admits_or_modifies_production():
    report = q.build_report(ROOT)
    assert report['acquisition_requests'] == report['maximum_requests'] == 20
    assert report['unused_budget'] == 0 and report['network_sealed']
    assert report['B3_network_requests'] == report['BRAPI_network_requests'] == report['Yahoo_network_requests'] == 0
    assert report['network_requests_during_report'] == report['model_admissions'] == 0
    assert not report['production_parser_installed']
    for proof in report['raw_proofs'].values():
        assert proof['raw_history_source_gap_recovered']
        assert not proof['full_existing_price_history_reproduced']
        assert proof['remaining_blockers'] == ['unresolved_Enfoque_vs_cached_Yahoo_daily_close_disagreements']
        assert not proof['model_admission_granted'] and not proof['production_writes']
        assert proof['cash_events_created'] == 0


@pytest.fixture(scope='module')
def pssa_inputs():
    from .broad_stocks import split_price_history
    from .share_basis import b3_evidence

    daily, cash, _ = q.cached_inputs(ROOT, 'PSSA3')
    content = q.read(ROOT, daily)
    cutoff = date(2026, 9, 30)
    _, actions = split_price_history(content, 'PSSA3', cutoff, allow_recent_actions=True)
    _, quotes = b3_evidence(json.loads(q.read(ROOT, cash))['results'], 'ON')
    return {'root': ROOT, 'identity': dict(q.PSSA_IDENTITY), 'cutoff': cutoff,
            'observed_actions': actions, 'existing_quotes': quotes, 'price_content': content}


@pytest.fixture(scope='module')
def pssa_supplement(pssa_inputs):
    return q.get_pssa_independent_raw_quotes(**pssa_inputs)


def test_PSSA_anchor_supplement_uses_normal_reconciler_with_unchanged_Yahoo(pssa_inputs, pssa_supplement):
    from .share_basis import reconcile_prices

    content, actions, exchange = (pssa_inputs[k] for k in ('price_content', 'observed_actions', 'existing_quotes'))
    cutoff = pssa_inputs['cutoff']
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_prices(content, 'PSSA3', actions, exchange, cutoff)
    prices, matches, mode = reconcile_prices(content, 'PSSA3', actions, pssa_supplement['quotes'], cutoff)
    assert mode == 'split_adjusted' and len(matches) == 1248
    assert len(prices) == 120 and prices[cutoff][2] == 47.290000915527344
    assert all(type(day) is date and type(value) is float for day, value in pssa_supplement['quotes'].items())
    assert len(pssa_supplement['quotes']) == 1312
    assert len(exchange) == 63
    assert all(pssa_supplement['quotes'][day] == value for day, value in exchange.items())
    assert pssa_supplement['quotes'][date(2021, 9, 1)] == 56.54
    assert pssa_supplement['quotes'][date(2021, 10, 20)] == 47.36
    assert pssa_supplement['quotes'][date(2021, 10, 21)] == 23.92
    assert q.sha(content) == '21cd9fc6858ba5c2b404441f1e7a9167ac9091f0ca757fa63f83f3daa20fe540'
    proof = pssa_supplement['proof']
    assert proof['source_label'] == 'independent_issuer_Enfoque_plus_cached_exchange_quotes'
    assert proof['native_Enfoque_quote_dates'] == 1268
    assert len(proof['shared_exchange_Enfoque_dates']) == 19
    assert len(proof['documented_Yahoo_differences']) == 4
    assert all(r['within_existing_point_six_percent_band'] for r in proof['documented_Yahoo_differences'])
    assert proof['price_band'] == {'relative': .006, 'absolute_BRL': .015}
    assert proof['normal_minimum_four_and_every_regime_checks_still_required']
    assert not proof['primary_Yahoo_bytes_replaced'] and not proof['synthetic_provider_JSON_created']
    assert not proof['model_admission_granted'] and not proof['LREN_adapter_enabled']
    action, = pssa_supplement['primary_actions']
    assert (action['date'], action['ratio_numerator'], action['ratio_denominator']) == ('2021-10-21', 2, 1)
    assert action['kind'] == 'share_bonus'
    assert len(pssa_supplement['sources']) == 11
    assert all(source['url'] and q.sha(q.read(ROOT, source)) == source['sha256']
               for source in pssa_supplement['sources'])


def test_PSSA_minimum_four_anchor_gate_is_unchanged(pssa_inputs, pssa_supplement):
    from .share_basis import reconcile_prices

    days = [date(2021, 9, 30), date(2021, 10, 21), date(2026, 9, 30)]
    only_three = {day: pssa_supplement['quotes'][day] for day in days}
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        reconcile_prices(pssa_inputs['price_content'], 'PSSA3', pssa_inputs['observed_actions'],
                         only_three, pssa_inputs['cutoff'])


def test_PSSA_every_regime_gate_is_unchanged(pssa_inputs, pssa_supplement):
    from .share_basis import reconcile_prices

    post_bonus_only = {day: value for day, value in pssa_supplement['quotes'].items()
                       if day >= date(2021, 10, 21)}
    assert len(post_bonus_only) > 1000
    with pytest.raises(ValueError, match='unverified_share_price_regime_requires_raw_history'):
        reconcile_prices(pssa_inputs['price_content'], 'PSSA3', pssa_inputs['observed_actions'],
                         post_bonus_only, pssa_inputs['cutoff'])


@pytest.mark.parametrize('field,value', [
    ('ticker', 'LREN3'), ('ticker', 'PSSA3F'), ('cvm', 8133), ('cvm', '16659'),
    ('cvm', True), ('share_class', 'PN'), ('isin', 'BRLRENACNOR1'),
    ('unit_composition', ['PSSA3']), ('cnpj', '92754738000162'),
])
def test_PSSA_getter_rejects_identity_substitution(pssa_inputs, field, value):
    inputs = {**pssa_inputs, 'identity': {**pssa_inputs['identity'], field: value}}
    with pytest.raises(ValueError, match='PSSA_exact_'):
        q.get_pssa_independent_raw_quotes(**inputs)


@pytest.mark.parametrize('cutoff', [date(2026, 9, 29), date(2026, 10, 31), '2026-09-30', datetime(2026, 9, 30)])
def test_PSSA_getter_requires_exact_date_cutoff(pssa_inputs, cutoff):
    with pytest.raises(ValueError, match='PSSA_exact_reviewed_cutoff_required'):
        q.get_pssa_independent_raw_quotes(**{**pssa_inputs, 'cutoff': cutoff})


@pytest.mark.parametrize('actions', [
    [], [{'date': '2021-10-20', 'ratio': 2.}], [{'date': '2021-10-25', 'ratio': 2.}],
    [{'date': '2021-10-21', 'ratio': 1.}], [{'date': '2021-10-21', 'ratio': 1.999999999}],
    [{'date': '2021-10-21', 'ratio': True}], [{'date': '2021-10-21', 'ratio': float('nan')}],
    [{'date': '2021-10-21', 'ratio': 2.}, {'date': '2027-01-01', 'ratio': 2.}],
])
def test_PSSA_observed_actions_must_match_exact_primary_ratio_and_ex_date(pssa_inputs, evidence, actions):
    with pytest.raises(ValueError, match='PSSA_observed_primary_action_chain_mismatch'):
        q._pssa_observed_action_chain(actions, evidence['PSSA3'][1])


def test_PSSA_getter_cannot_ignore_observed_action_mismatch(pssa_inputs):
    with pytest.raises(ValueError, match='PSSA_observed_primary_action_chain_mismatch'):
        q.get_pssa_independent_raw_quotes(**{**pssa_inputs,
            'observed_actions': [{'date': '2021-10-21', 'ratio': 1.9999}]})


def test_PSSA_getter_rejects_any_changed_primary_price_bytes(pssa_inputs):
    with pytest.raises(ValueError, match='PSSA_primary_Yahoo_bytes_must_remain_unchanged'):
        q.get_pssa_independent_raw_quotes(**{**pssa_inputs, 'price_content': pssa_inputs['price_content'] + b' '})


@pytest.mark.parametrize('alteration', ['conflict', 'missing_old_exchange_anchor', 'string_date'])
def test_PSSA_existing_exchange_quotes_are_preserved_without_conflict_selection(pssa_inputs, alteration):
    quotes = dict(pssa_inputs['existing_quotes'])
    if alteration == 'conflict':
        quotes[date(2021, 10, 29)] += .001
        reason = 'PSSA_shared_exchange_Enfoque_anchor_disagreement'
    elif alteration == 'missing_old_exchange_anchor':
        quotes.pop(min(quotes))
        reason = 'PSSA_existing_cached_exchange_quotes_must_be_preserved'
    else:
        quotes['2021-10-29'] = 23.34
        reason = 'PSSA_existing_exchange_quotes_must_be_typed'
    before = dict(quotes)
    with pytest.raises(ValueError, match=reason):
        q.get_pssa_independent_raw_quotes(**{**pssa_inputs, 'existing_quotes': quotes})
    assert quotes == before


@pytest.mark.parametrize('ordinal', [8, 14, 15, 19])
def test_PSSA_getter_rejects_corrupt_publisher_config_history_or_ratio_bytes(pssa_inputs, tmp_path, monkeypatch, ordinal):
    source = receipt(ordinal)
    changed = tmp_path / 'changed-source.bin'
    changed.write_bytes(q.read(ROOT, source) + b' ')
    actual_safe_path = q.safe_path

    def redirected(root, name):
        return changed if name == source['file'] else actual_safe_path(root, name)
    monkeypatch.setattr(q, 'safe_path', redirected)
    with pytest.raises(ValueError, match='source_hash_or_size_mismatch'):
        q.get_pssa_independent_raw_quotes(**pssa_inputs)


def test_PSSA_getter_rejects_changed_receipt_metadata(pssa_inputs, monkeypatch):
    actual_records = q.records

    def changed_records(root, prefix):
        rows = actual_records(root, prefix)
        if prefix == 'response':
            rows = deepcopy(rows)
            next(row for row in rows if row['ordinal'] == 14)['url'] += '?tickers=LREN3'
        return rows
    monkeypatch.setattr(q, 'records', changed_records)
    with pytest.raises(ValueError, match='PSSA_publisher_config_or_ratio_receipt_changed'):
        q.get_pssa_independent_raw_quotes(**pssa_inputs)


def test_PSSA_unmodified_parent_build_candidates_registers_proofs_and_passes_financial_gates(pssa_inputs, pssa_supplement):
    from . import inventory_candidates as parent
    from .collect_market_data import financial_features, stock_observations
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory

    original_reconcile = parent.reconcile_prices
    yahoo_path = ROOT / 'raw/expanded/PSSA3-prices.json'
    yahoo_hash = '21cd9fc6858ba5c2b404441f1e7a9167ac9091f0ca757fa63f83f3daa20fe540'
    before_yahoo = yahoo_path.read_bytes()
    assert q.sha(before_yahoo) == yahoo_hash
    pointer_hashes = {name: q.sha((ROOT / name).read_bytes()) for name in q.PROTECTED}
    inventory = load_inventory(ROOT)
    row, = [r for r in inventory['stocks'] if r['ticker'] == 'PSSA3']
    scoped = {**inventory, 'stocks': [row]}
    cutoff = date(2026, 9, 30)
    candidates, pending, sources = parent.build_candidates(ROOT, scoped, cutoff)
    assert parent.reconcile_prices is original_reconcile
    assert pending == []
    candidate, = candidates
    assert {key: candidate[key] for key in q.PSSA_IDENTITY} == q.PSSA_IDENTITY
    assert candidate['checks']['independent_cash_matches'] == 20
    assert candidate['checks']['independent_price_matches'] == 1248
    assert candidate['checks']['share_regime'] == 'split_adjusted'
    assert candidate['price_source_file'] == 'raw/expanded/PSSA3-prices.json'
    assert candidate['price_url'] == next(s['url'] for s in sources if s['file'] == candidate['price_source_file'])
    assert not candidate['financially_validated']
    assert candidate['checks']['extended_training_history'] == {
        'status': 'not_used', 'reason': 'B3_cash_event_missing_from_payment_table'}
    proof = candidate['checks']['independent_raw_quote_evidence']
    assert proof == pssa_supplement['proof']
    assert candidate['checks']['primary_share_action_evidence'] == pssa_supplement['primary_actions']
    assert proof['observed_vendor'] == 'Enfoque'
    assert not proof['licensing_contract_verified'] and not proof['ultimate_upstream_independence_verified']
    assert not proof['LREN_adapter_enabled'] and not proof['primary_Yahoo_bytes_replaced']
    assert len(proof['documented_Yahoo_differences']) == 4
    registered = {source['file']: source for source in sources}
    for expected in pssa_supplement['sources']:
        actual = registered[expected['file']]
        assert actual['sha256'] == expected['sha256']
        assert actual['url'] == expected['url']
        assert actual['bytes'] == expected['bytes']
        assert actual['retrieved_at'] == expected.get('retrieved_at')
        assert q.sha(q.read(ROOT, actual)) == expected['sha256']
        assert actual['observed_at'] is not None
        if not expected.get('retrieved_at'):
            assert actual['observed_at_basis'] == 'local_file_mtime_not_network_retrieval'
    statements, financial_audit, _ = prepare_financials(ROOT, [row], cutoff)
    annual, = [r for r in financial_audit if r.get('kind') == 'candidate' and r.get('ticker') == 'PSSA3']
    assert annual['status'] == 'included' and annual['reasons'] == []
    spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
    current = financial_features(statements, candidate['payments'], cutoff, spec)
    assert current is not None
    _, filing = current
    assert filing['filing_id'] == '154597' and filing['fiscal_end'] == '2025-12-31'
    basis = date.fromisoformat(candidate['financial_basis_start'])
    assert date.fromisoformat(filing['fiscal_end']) >= basis
    training, _ = stock_observations('PSSA3', candidate['prices'], candidate['payments'], statements, cutoff, spec=spec)
    training = [r for r in training if date.fromisoformat(r['as_of']) >= candidate['training_since']
                and date.fromisoformat(r['fiscal_end']) >= basis]
    assert len(training) == 37
    assert training[0]['as_of'] == '2022-09-30' and training[-1]['as_of'] == '2025-09-30'
    assert yahoo_path.read_bytes() == before_yahoo
    assert q.sha(yahoo_path.read_bytes()) == yahoo_hash
    assert {name: q.sha((ROOT / name).read_bytes()) for name in q.PROTECTED} == pointer_hashes
    assert len(q.records(ROOT, 'request')) == len(q.records(ROOT, 'response')) == 20
    assert q.records(ROOT, 'seal')[0]['network_closed']
