"""Exact cached-price replay, financial boundaries and acquisition failures."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import urllib.error

import pytest

from . import noncash_price_research as module


MARKET = Path(__file__).parent / 'data/market'


@pytest.fixture(scope='module')
def loaded():
    documents = module.corporate.load_documents(MARKET)
    return {ticker: module._load(MARKET, module.identity(ticker), documents)
            for ticker in module.corporate.IDENTITIES}


@pytest.fixture(scope='module')
def report():
    return module.build_report(MARKET)


def audit(item, **overrides):
    values = {'daily': item['daily'], 'anchors': item['anchors'],
              'actions': item['corporate']['canonical_share_actions'],
              'event_day': module._day(item['corporate']['non_income_events'][0]['ex_date']),
              'origin_isin': item['identity']['isin']}
    return module.audit_origin_basis(**(values | overrides))


@pytest.mark.parametrize('ticker', ['ITUB3', 'ITUB4'])
def test_real_origin_basis_has_all_80_anchors_and_four_event_share_regimes(loaded, ticker):
    result = audit(loaded[ticker])
    assert result['bonus_only_basis_verified']
    assert result['anchor_count'] == 80
    assert len(result['used_regimes']) == 4
    assert result['monthly_price_count'] == 61
    assert result['joint_return_periods_required'] == 60
    assert result['joint_economic_returns_recovered'] == 0
    assert all(row['matches_bonus_only_basis'] for row in result['anchors'])
    assert result['thresholds'] == {'relative': .006, 'absolute': .015}


@pytest.mark.parametrize('ticker,raw', [('BRAP3', '54.00'), ('BRAP4', '55.78')])
def test_real_brap_origin_is_not_relabelled_as_raw_or_recipient_split(loaded, ticker, raw):
    result = audit(loaded[ticker])
    assert not result['bonus_only_basis_verified']
    assert result['anchor_count'] == 12 and len(result['used_regimes']) == 2
    row = next(a for a in result['anchors'] if a['date'] == '2021-12-16')
    assert Decimal(row['raw_reference_close']) == Decimal(raw)
    assert row['same_security_factor'] == '1'
    assert Decimal(row['vendor_close']) < Decimal(raw) * Decimal('.55')
    with pytest.raises(ValueError, match='original_price_basis_not_reconstructible'):
        module.recover_origin_prices(MARKET, module.identity(ticker))


@pytest.mark.parametrize('ticker', ['ITUB3', 'ITUB4'])
def test_adapter_returns_raw_scale_view_not_a_total_return_or_pit_series(ticker):
    result = module.recover_origin_prices(MARKET, module.identity(ticker))
    assert len(result['cutoff_share_monthly_prices']) == 61
    assert not result['model_admission_granted'] and not result['income_modified']
    assert not result['economic_total_returns_available']
    assert not result['noncash_principal_included_in_origin_price']
    assert not result['point_in_time_archive_verified']
    assert not result['cent_rounding_applied']
    raw = result['raw_daily_prices'][date(2021, 9, 30)]
    assert float(raw) == pytest.approx(27 if ticker == 'ITUB3' else 28.93, abs=.00001)
    assert result['raw_daily_prices'][date(2026, 9, 30)] > 0


@pytest.mark.parametrize('ticker', ['ITUB3', 'ITUB4'])
def test_recovered_monthly_view_agrees_with_unchanged_parent_price_gate(loaded, ticker):
    from .share_basis import reconcile_prices

    item = loaded[ticker]
    actions = item['corporate']['canonical_share_actions']
    original, matches, mode = reconcile_prices(item['raw'], ticker, actions,
        {day: float(row['raw_close']) for day, row in item['anchors'].items()}, module.CUTOFF)
    result = module.recover_origin_prices(MARKET, module.identity(ticker))
    assert mode == 'split_adjusted' and len(matches) == 80
    for month, entry in result['cutoff_share_monthly_prices'].items():
        assert entry == original[month]


@pytest.mark.parametrize('ticker', module.corporate.IDENTITIES)
def test_principal_financial_receipt_keeps_zero_income_and_independent_financial_gates(loaded, ticker):
    result = module.noncash_principal_receipt(loaded[ticker]['corporate'])
    assert result['cash_income_brl_per_origin_share'] == '0'
    assert result['origin_units_after_per_origin_unit'] == '1'
    assert result['income_ledger_entries_created'] == 0
    assert not result['DFC_cash_outflow_inferred'] and not result['profit_or_EPS_inferred']
    assert not result['NCI_attributed_to_origin_shareholders']
    assert not result['cost_basis_is_FMV'] and not result['settlement_confirmed']
    assert result['financial_and_cash_history_gates_still_required']
    assert result['corporate_proof_sources'] == loaded[ticker]['corporate']['non_income_events'][0]['evidence']


@pytest.mark.parametrize('field,value', [('holder_share_factor', '1.332373453'),
    ('holder_share_factor', '2'), ('income_amount', '26.736'), ('is_own_share_split', True),
    ('cash_dividend_or_JCP', True), ('isin', 'BRVALEACNOR0')])
def test_principal_cannot_be_promoted_to_origin_shares_or_income(loaded, field, value):
    canonical = deepcopy(loaded['BRAP3']['corporate'])
    canonical['non_income_events'][0][field] = value
    with pytest.raises(ValueError, match='principal_or_share_basis_conflict'):
        module.noncash_principal_receipt(canonical)


@pytest.mark.parametrize('field,value', [('cvm', True), ('cvm', 18724),
    ('isin', 'BRITUBACNPR1'), ('share_class', 'PN'), ('cnpj', '00000000000000')])
def test_exact_class_cnpj_and_cvm_required_before_any_source_read(field, value):
    instrument = module.identity('ITUB3') | {field: value}
    with pytest.raises(ValueError, match='exact_instrument_identity_mismatch'):
        module.recover_origin_prices(MARKET, instrument)


def test_received_vale_or_xpart_cannot_be_added_as_same_security_action(loaded):
    for ticker, isin, ratio in [('BRAP3', 'BRVALEACNOR0', '.332373453'),
                                ('ITUB3', 'BRXPARACNOR9', '2')]:
        action = {'date': loaded[ticker]['corporate']['non_income_events'][0]['ex_date'],
                  'kind': 'same_class_bonus', 'isin': isin, 'ratio': ratio,
                  'ratio_decimal': ratio, 'same_security_ratio_primary_verified': True}
        with pytest.raises(ValueError, match='received_stock_cannot_be_origin_share_factor'):
            audit(loaded[ticker], actions=[action])


def test_wrong_class_bonus_conflicting_ratio_duplicate_and_post_cutoff_fail(loaded):
    original = loaded['ITUB3']['corporate']['canonical_share_actions']
    cases = [(original + original, 'duplicate_same_security_action'),
             ([original[0] | {'isin': module.identity('ITUB4')['isin']}], 'origin_share_factor'),
             ([original[0] | {'ratio_decimal': '1.11'}], 'conflicting_same_security_ratio'),
             ([original[0] | {'date': '2026-10-01'}], 'post_cutoff_share_action')]
    for actions, reason in cases:
        with pytest.raises(ValueError, match=reason):
            audit(loaded['ITUB3'], actions=actions)


@pytest.mark.parametrize('regime', ['cum_noncash', 'middle_bonus', 'last_bonus'])
def test_all_used_regimes_must_have_own_independent_anchor(loaded, regime):
    item = loaded['ITUB3']
    bounds = {'cum_noncash': ('2021-09-30', '2021-10-03'),
              'middle_bonus': ('2025-03-18', '2025-12-25'),
              'last_bonus': ('2025-12-26', '2026-09-30')}
    start, end = map(date.fromisoformat, bounds[regime])
    anchors = {day: value for day, value in item['anchors'].items() if not start <= day <= end}
    with pytest.raises(ValueError, match='unverified_share_or_noncash_price_regime'):
        audit(item, anchors=anchors)


def test_missing_quote_is_not_matched_to_neighboring_date(loaded):
    daily = dict(loaded['ITUB3']['daily'])
    del daily[date(2021, 11, 19)]
    result = audit(loaded['ITUB3'], daily=daily)
    assert not result['bonus_only_basis_verified']
    failed = next(row for row in result['anchors'] if row['date'] == '2021-11-19')
    assert failed['vendor_close'] is None


def test_one_disagreeing_anchor_invalidates_the_entire_recovery(loaded):
    daily = dict(loaded['ITUB3']['daily'])
    daily[date(2021, 11, 19)] *= 2
    assert not audit(loaded['ITUB3'], daily=daily)['bonus_only_basis_verified']


def test_no_short_history_missing_month_or_stale_end_workaround(loaded):
    daily = loaded['ITUB3']['daily']
    for subset in [{d: v for d, v in daily.items() if d >= date(2021, 10, 1)},
                   {d: v for d, v in daily.items() if (d.year, d.month) != (2023, 7)}]:
        with pytest.raises(ValueError, match='less_than_61_consecutive_prices'):
            audit(loaded['ITUB3'], daily=subset)
    with pytest.raises(ValueError, match='stale_month_end_price'):
        audit(loaded['ITUB3'], daily={d: v for d, v in daily.items() if d <= date(2026, 9, 1)})


def test_adjusted_close_is_never_consumed_even_if_replaced_with_arbitrary_values():
    raw = (MARKET / 'raw/expanded/ITUB3-prices.json').read_bytes()
    original, _ = module._daily(raw, 'ITUB3')
    payload = json.loads(raw)
    payload['chart']['result'][0]['indicators']['adjclose'] = [{'adjclose': ['not a price']}]
    assert module._daily(json.dumps(payload).encode(), 'ITUB3')[0] == original


@pytest.mark.parametrize('mutation,reason', [
    ('duplicate', 'duplicate_daily_price_dates'), ('length', 'daily_array_length_mismatch'),
    ('currency', 'price_instrument_currency_or_interval_mismatch'),
    ('symbol', 'price_instrument_currency_or_interval_mismatch'), ('price', 'invalid_economic_number')])
def test_daily_source_integrity(mutation, reason):
    data = json.loads((MARKET / 'raw/expanded/ITUB3-prices.json').read_bytes())
    result = data['chart']['result'][0]
    if mutation == 'duplicate':
        result['timestamp'][1] = result['timestamp'][0]
    elif mutation == 'length':
        result['timestamp'].pop()
    elif mutation == 'price':
        result['indicators']['quote'][0]['close'][10] = -1
    else:
        result['meta'][mutation] = 'USD' if mutation == 'currency' else 'ITUB4.SA'
    with pytest.raises(ValueError, match=reason):
        module._daily(json.dumps(data).encode(), 'ITUB3')


def bridge(loaded):
    canonical = loaded['BRAP3']['corporate']
    instrument = module.identity('BRAP3')
    receipt = {'origin_identity': instrument, 'received_isin': 'BRVALEACNOR0',
        'valuation_date': '2021-12-17', 'origin_price_date': '2021-12-17',
        'received_price_date': '2021-12-17', 'currency': 'BRL',
        'origin_price_basis': 'raw_origin_share', 'received_price_basis': 'raw_received_security_unit',
        'origin_share_basis': '2021-12-16', 'entitlement_share_basis': '2021-12-16',
        'principal_already_in_origin_price': False, 'valuation_kind': 'market_value',
        'NCI_value_included': '0', 'received_units_per_origin_unit': '0.332373453'}
    # Synthetic algebra only: no claimed market-data provenance or settlement.
    args = {'valuation_day': '2021-12-17', 'origin_raw_price': '23', 'received_raw_price': '79',
            'origin_units': '10', 'received_units': '3', 'receivable_units': '.323734530',
            'cash_income': '0', 'claimed_economic_value': str(Decimal(230) + Decimal('3.323734530') * 79)}
    return instrument, canonical, receipt, args


def test_bridge_conserves_principal_between_receivable_and_delivered_units(loaded):
    instrument, canonical, receipt, args = bridge(loaded)
    result = module.validate_economic_bridge(instrument, canonical, receipt, **args)
    assert result['cash_income_brl'] == '0' and not result['claim_is_observed_total_return']
    assert not result['model_admission_granted']
    args.update(received_units='0', receivable_units='3.323734530')
    assert module.validate_economic_bridge(instrument, canonical, receipt, **args) == result


@pytest.mark.parametrize('changes,reason', [
    ({'received_units': '3.323734530'}, 'double_count_or_missing_principal_units'),
    ({'receivable_units': '0'}, 'double_count_or_missing_principal_units'),
    ({'cash_income': '.01'}, 'noncash_principal_is_not_cash_income'),
    ({'claimed_economic_value': '100000'}, 'economic_value_does_not_reconcile'),
    ({'valuation_day': '2021-12-20'}, 'economic_valuation_date_conflict')])
def test_bridge_rejects_double_count_missing_principal_fake_income_or_value(loaded, changes, reason):
    instrument, canonical, receipt, args = bridge(loaded)
    with pytest.raises(ValueError, match=reason):
        module.validate_economic_bridge(instrument, canonical, receipt, **(args | changes))


@pytest.mark.parametrize('changes,reason', [
    ({'origin_identity': module.identity('BRAP4')}, 'economic_instrument_or_class_conflict'),
    ({'received_isin': 'BRXPARACNOR9'}, 'economic_instrument_or_class_conflict'),
    ({'received_price_date': '2021-12-16'}, 'economic_valuation_date_conflict'),
    ({'currency': 'USD'}, 'economic_price_or_share_basis_conflict'),
    ({'origin_price_basis': 'adjusted_close'}, 'economic_price_or_share_basis_conflict'),
    ({'principal_already_in_origin_price': True}, 'economic_price_or_share_basis_conflict'),
    ({'origin_share_basis': '2026-09-30'}, 'economic_price_or_share_basis_conflict'),
    ({'NCI_value_included': '1'}, 'NCI_or_nonmarket_value_conflict'),
    ({'valuation_kind': 'book_cost'}, 'NCI_or_nonmarket_value_conflict'),
    ({'received_units_per_origin_unit': '.5'}, 'noncash_entitlement_ratio_conflict')])
def test_bridge_rejects_class_share_basis_nci_and_nonmarket_value_conflicts(loaded, changes, reason):
    instrument, canonical, receipt, args = bridge(loaded)
    with pytest.raises(ValueError, match=reason):
        module.validate_economic_bridge(instrument, canonical, receipt | changes, **args)


def test_unverified_xp_received_identity_cannot_be_manufactured_from_parent_or_ratio(loaded):
    instrument, _, receipt, args = bridge(loaded)
    instrument = module.identity('ITUB3')
    receipt['origin_identity'] = instrument
    with pytest.raises(ValueError, match='received_security_identity_not_verified'):
        module.validate_economic_bridge(instrument, loaded['ITUB3']['corporate'], receipt, **args)


def test_report_distinguishes_book_cost_fmv_origin_recovery_and_total_returns(report):
    rows = {r['identity']['ticker']: r for r in report['records']}
    for ticker in ['BRAP3', 'BRAP4']:
        result = rows[ticker]['noncash_reversal_diagnostic']
        assert not result['original_price_basis_reconstruction_available']
        assert not result['candidate_applied_to_any_price']
        assert not result['independent_distribution_FMV_verified']
        assert result['book_cost_cannot_replace_distribution_FMV']
        assert result['book_cost_reference_date'] == '2021-09-30'
        assert rows[ticker]['separate_cash_distribution']['payment_date'] == '2021-12-29'
        assert not rows[ticker]['separate_cash_distribution']['principal_included']
    for ticker in ['ITUB3', 'ITUB4']:
        assert rows[ticker]['original_price_basis_reconstruction_available']
        assert not rows[ticker]['issuer_theoretical_opening_reference']['is_executed_quote']
    for row in rows.values():
        assert not row['economic_total_returns_available'] and not row['model_admission_granted']
        assert row['cash_income_from_principal'] == '0'
        assert row['joint_economic_returns_produced'] == 0 and not row['normal_gates_waived']
    assert report['new_admissions'] == 0 and not report['dataset_activated']
    assert not report['normal_financial_history_gates_waived']
    assert not report['missing_prices_imputed']
    assert report['economic_return_method_proposal']['status'].startswith('hypothetical')


def test_brap_factor_is_class_specific_and_never_estimated_as_an_own_split(report):
    results = [r['noncash_reversal_diagnostic'] for r in report['records'] if r['identity']['ticker'].startswith('BRAP')]
    assert results[0]['candidate_proportional_price_factor'] != results[1]['candidate_proportional_price_factor']
    assert all(r['received_units_per_origin_unit'] == '0.332373453' for r in results)
    assert all(not r['provider_documented_noncash_close_adjustment'] for r in results)
    assert all(len(r['candidate_all_anchor_fit_diagnostics']) == 12 for r in results)
    assert all(all(a['fits_numeric_candidate_only'] for a in r['candidate_all_anchor_fit_diagnostics'])
               for r in results)


def test_cached_recipient_raw_quotes_are_acknowledged_without_inventing_independence(report):
    evidence = report['recipient_quote_evidence']
    assert not evidence['adjustedClose_consumed']
    assert evidence['quote_field_used'] == 'rawClose'
    assert all(r['providers_agree'] for r in evidence['observations'])
    assert [r['brapi_rawClose'] for r in evidence['observations']] == ['80.44', '79.17', '78.28', '77.96']
    assert not evidence['two_provider_agreement_is_primary_independent_proof']
    assert not evidence['normal_recipient_price_and_action_regime_gate_completed']


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_persists_and_stops_all_subsequent_acquisition(tmp_path, monkeypatch, status):
    document = module.select_document(MARKET, '441220')
    monkeypatch.setattr(module, 'select_document', lambda folder, sequence: document | {'sequence': sequence})
    calls = []

    class Denied:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'denied', None, None)

    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *_: Denied())
    with pytest.raises(urllib.error.HTTPError):
        module.acquire_document(tmp_path, '441220')
    with pytest.raises(ValueError, match='access_denied_no_retry'):
        module.acquire_document(tmp_path, '439046')
    assert calls == [document['url']]
    assert len(module._attempts(tmp_path)) == 1


def test_timeout_consumes_request_and_is_not_retried(tmp_path, monkeypatch):
    document = module.select_document(MARKET, '441220')
    monkeypatch.setattr(module, 'select_document', lambda *_: document)

    class Timeout:
        def open(self, *args, **kwargs):
            raise TimeoutError('test')

    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *_: Timeout())
    with pytest.raises(TimeoutError):
        module.acquire_document(tmp_path, '441220')
    with pytest.raises(ValueError, match='previous_request_not_retried'):
        module.acquire_document(tmp_path, '441220')
    assert len(module._attempts(tmp_path)) == 1


def test_request_budget_survives_restarts(tmp_path, monkeypatch):
    for index in range(1, module.MAX_REQUESTS + 1):
        module._write(tmp_path, f'{module.CACHE}/request-{index:02d}.json',
                      module._json({'sequence': f'previous-{index}', 'ordinal': index}))
    monkeypatch.setattr(module, 'select_document', lambda *_: {'url': 'https://www.rad.cvm.gov.br/unused'})
    with pytest.raises(ValueError, match='primary_request_budget_exhausted'):
        module.acquire_document(tmp_path, '441220')


@pytest.mark.parametrize('target', ['https://www.b3.com.br/anything', 'https://www.engie.com.br/anything',
    'https://ri.azzas2154.com.br/anything', 'https://allos.com.br/anything',
    'https://investors.xpinc.com/servicos-aos-investidores/faqs/', 'https://example.com/proxy'])
def test_arbitrary_origins_cannot_be_selected_or_reached_by_redirect(target):
    with pytest.raises(ValueError, match='outside_bounded_primary_selection'):
        module.select_document(MARKET, target)
    with pytest.raises(ValueError, match='redirect_not_followed'):
        module._NoRedirect().redirect_request(None, None, 302, '', {}, target)


@pytest.mark.parametrize('relative', ['../escape', '/tmp/escape', 'raw/../escape', r'raw\escape',
    'C:/escape', '//server/escape'])
def test_proof_path_cannot_escape(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe_noncash_proof_path'):
        module._path(tmp_path, relative)


def test_writes_limited_to_owned_cache_and_immutable(tmp_path):
    with pytest.raises(ValueError, match='write_outside_owned_noncash_cache'):
        module._write(tmp_path, 'current.json', b'{}')
    relative = module.CACHE + '/test.json'
    module._write(tmp_path, relative, b'original')
    module._write(tmp_path, relative, b'original')
    with pytest.raises(ValueError, match='immutable_proof_conflict'):
        module._write(tmp_path, relative, b'changed')


def test_unreviewed_bytes_rejected_even_when_receipt_would_be_self_consistent(tmp_path):
    relative = module.CACHE + '/changed.json'
    module._write(tmp_path, relative, b'changed')
    with pytest.raises(ValueError, match='noncash_source_hash_mismatch'):
        module._read(tmp_path, relative, hashlib.sha256(b'original').hexdigest())


def test_report_and_adapter_are_offline_and_leave_originals_and_parent_pointers_unchanged(monkeypatch):
    protected = [MARKET / 'current.json', Path(module.corporate.__file__),
                 MARKET / 'raw/expanded/BRAP3-prices.json', MARKET / 'raw/expanded/ITUB3-prices.json']
    before = {p: p.read_bytes() for p in protected}
    monkeypatch.setattr(socket, 'create_connection', lambda *_a, **_kw: pytest.fail('offline replay'))
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *_: pytest.fail('offline replay'))
    path, result = module.write_report(MARKET)
    module.recover_origin_prices(MARKET, module.identity('ITUB3'))
    assert {p: p.read_bytes() for p in protected} == before
    assert path.is_relative_to(MARKET / module.CACHE)
    assert path.read_bytes() == module._json(result)
    assert result['acquisition']['B3_network_requests'] == 0
    assert result['acquisition']['report_network_requests'] == 0
