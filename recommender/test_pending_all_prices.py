import json
from datetime import datetime, timezone

import pytest

from .pending_all_prices import (
    compare_vendor, digest, full_daily_comparison, inspect_html, monthly_audit, published_history_request,
    read_source, vendor_rows, verified_native_history,
)


def daily(closes=(10.25, 11.5), days=('2021-09-30', '2026-09-30'), ticker='ABCB4'):
    return json.dumps({'chart': {'result': [{'meta': {'symbol': ticker + '.SA',
        'currency': 'BRL', 'dataGranularity': '1d'},
        'timestamp': [int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()) for d in days],
        'indicators': {'quote': [{'close': closes}], 'adjclose': [{'adjclose': [1, 2]}]}}]}}).encode()


def payload(rows, ticker='ABCB4'):
    return json.dumps({'success': True, 'data': {'tickers': [{'ticker': ticker,
        'history': rows}], 'source': 'Enfoque', 'errors': [], 'indexes': [],
        'link': 'https://www.enfoque.com.br'}}).encode()


def quote(day='2021-09-30', value=10.25):
    return {'date': day, 'closed': value, 'high': value + 1, 'low': value - 1,
            'open': value, 'middle': value, 'share': 100, 'trade': 10, 'vol': 1025}


def test_full_61_month_audit_never_fills_missing_or_uses_adjusted():
    audit = monthly_audit(daily(), 'ABCB4')
    assert len(audit['anchors']) == 61
    assert audit['observed_required_months'] == 2
    assert len(audit['missing_months']) == 59
    assert audit['anchors'][0]['native_close_BRL'] == 10.25
    assert audit['anchors'][1]['native_close_BRL'] is None
    assert audit['prices_transformed'] is False


def test_wrong_share_code_or_currency_cannot_supply_prices():
    with pytest.raises(ValueError, match='exact_daily_identity'):
        monthly_audit(daily(ticker='ABCB3'), 'ABCB4')


def test_duplicate_dates_are_rejected_instead_of_last_row_winning():
    with pytest.raises(ValueError, match='duplicate_daily_date'):
        monthly_audit(daily(days=('2021-09-30', '2021-09-30')), 'ABCB4')
    with pytest.raises(ValueError, match='duplicate_vendor_quote_date'):
        vendor_rows(payload([quote(), quote()]), 'ABCB4')


def test_vendor_native_decimal_and_class_identity():
    rows, inactive = vendor_rows(payload([quote(value=10.123456789)]), 'ABCB4')
    assert rows[0]['native_close_decimal'] == '10.123456789'
    assert inactive == []
    with pytest.raises(ValueError, match='vendor_exact_ticker'):
        vendor_rows(payload([quote()], ticker='ABCB3'), 'ABCB4')


def test_zero_trade_row_is_retained_separately_not_a_price():
    original = quote()
    original['trade'] = 0
    rows, inactive = vendor_rows(payload([original]), 'ABCB4')
    assert rows == []
    assert inactive[0]['native_fields']['trade'] == 0


def test_no_nearest_session_or_fitted_price_substitution():
    audit = monthly_audit(daily(), 'ABCB4')
    comparison = compare_vendor(audit, [{'date': '2021-09-29', 'native_close_decimal': '10.25'},
                                       {'date': '2026-09-30', 'native_close_decimal': '23'}])
    assert comparison['same_date_monthly_observations'] == 1
    assert comparison['required_61_months'][0]['same_date_observed'] is False
    assert len(comparison['native_month_end_disagreements']) == 1


def widget():
    bundle = b'API_STOCKINFO_URL:"https://api.mziq.com/mzstockinfo";t.API_URL+"/"+t.STOCK_ID+"/history?from=";t.ADJUSTED=!1;"&tickers="+t.TICKER;"&adjusted="+t.ADJUSTED;noProvents:"No"'
    page = b'<html><script src="https://cdn.mziq.com/stockinfo-viewer/main.js"></script><script>stockInfoId:"729b4498-02b4-11e9-934c-0a3f429ecfb6";tickers:"ABCB4"</script><p>Data de publicacao desconhecida</p></html>'
    return page, bundle


def test_published_widget_requires_exact_configured_code_and_literal_script():
    page, bundle = widget()
    p = {'url': 'https://ri.abcbrasil.com.br/quotes/'}
    b = {'url': 'https://cdn.mziq.com/stockinfo-viewer/main.js'}
    url, proof = published_history_request(page, p, bundle, b, 'ABCB4')
    assert url.endswith('tickers=ABCB4&adjusted=false')
    assert proof['adjusted'] is False
    with pytest.raises(ValueError, match='exact_ticker_not'):
        published_history_request(page, p, bundle, b, 'ABCB3')
    with pytest.raises(ValueError, match='bundle_not_published'):
        published_history_request(page, p, bundle, {'url': 'https://cdn.mziq.com/other.js'}, 'ABCB4')


def test_raw_receipt_hash_mismatch_and_path_escape_rejected(tmp_path):
    file = tmp_path / 'native.bin'
    file.write_bytes(b'native')
    assert read_source(tmp_path, {'file': 'native.bin', 'sha256': digest(b'native')}) == b'native'
    with pytest.raises(ValueError, match='hash_mismatch'):
        read_source(tmp_path, {'file': 'native.bin', 'sha256': digest(b'changed')})
    with pytest.raises(ValueError, match='unsafe_price_evidence_path'):
        read_source(tmp_path, {'file': '../native.bin', 'sha256': digest(b'native')})


def test_adapter_rejects_wrong_CVM_class_ISIN_without_touching_files(tmp_path):
    expected = {'ticker': 'ABCB4', 'cvm': 20958, 'share_class': 'PNA', 'isin': 'BRABCBACNPR4'}
    for key, value in [('cvm', 1), ('share_class', 'ON'), ('isin', 'BRABCBACNOR1')]:
        with pytest.raises(ValueError, match='exact_CVM_class_ISIN'):
            verified_native_history(tmp_path, {**expected, key: value}, expected, {}, {}, {})


def test_page_keeps_gross_bonus_rows_and_unknown_publication_separate():
    page = b'<html><head><title>Quotes</title></head><table><tr><td>12/06/2023</td><td>Bonificacao 1:20</td><td>6,52429353319</td></tr></table><script>DIVIDENDO FAKE SCRIPT</script></html>'
    inspected = inspect_html(page, 'https://issuer.example/quotes/', ['FLRY3'])
    assert inspected['publication_date'] is None
    assert inspected['table_rows'] == [['12/06/2023', 'Bonificacao 1:20', '6,52429353319']]
    assert all('FAKE SCRIPT' not in e for e in inspected['semantic_excerpts'])


def test_full_gate_cannot_select_four_favorable_dates_and_drop_conflict():
    days = ('2021-10-01', '2021-10-04', '2021-10-05', '2021-10-06', '2021-10-07')
    body = daily(closes=(10, 10, 10, 10, 10), days=days)
    audit = monthly_audit(body, 'ABCB4')
    rows = [{'date': day, 'native_close_decimal': '20' if i == 4 else '10'}
            for i, day in enumerate(days)]
    comparison = full_daily_comparison(body, 'ABCB4', rows, audit)
    assert comparison['independent_exact_dates'] == 5
    assert comparison['favorable_subset_selected'] is False
    assert len(comparison['raw_disagreements']) == 1
    assert comparison['unchanged_price_gate_trial_using_every_native_date']['reason'] == (
        'historical_quote_or_share_scale_disagrees_with_independent_source')


def test_adapter_returns_native_prices_without_admitting_or_transforming(tmp_path):
    page_body, bundle_body = widget()
    def receipt(name, body, url):
        (tmp_path / name).write_bytes(body)
        return {'file': name, 'sha256': digest(body), 'bytes': len(body), 'url': url, 'status': 200}
    page = receipt('page.bin', page_body, 'https://ri.abcbrasil.com.br/quotes/')
    bundle = receipt('bundle.bin', bundle_body, 'https://cdn.mziq.com/stockinfo-viewer/main.js')
    url, _ = published_history_request(page_body, page, bundle_body, bundle, 'ABCB4')
    history = receipt('history.bin', payload([quote()]), url)
    identity = {'ticker': 'ABCB4', 'cvm': 20958, 'share_class': 'PNA', 'isin': 'BRABCBACNPR4'}
    result = verified_native_history(tmp_path, identity, identity, history, page, bundle)
    assert result['rows'][0]['native_close_decimal'] == '10.25'
    assert result['model_admission'] is False
    assert result['prices_transformed'] is False
    assert result['corporate_actions_applied'] is False


def test_a_failed_vendor_response_is_not_history_evidence():
    body = json.loads(payload([quote()]))
    body['success'] = False
    with pytest.raises(ValueError, match='vendor_response_not_successful'):
        vendor_rows(json.dumps(body).encode(), 'ABCB4')
