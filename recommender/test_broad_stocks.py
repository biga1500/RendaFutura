from datetime import date, datetime, timezone
import json

import pytest

from .broad_stocks import check_b3_prices, check_cash_amounts, company_catalog, paid_history, split_price_history
from .collect_market_data import shift_month


def test_company_catalog_keeps_novo_mercado_and_level_segments():
    class Source:
        def b3(self, *args):
            return {'page': {'totalPages': 1}, 'results': [
                {'status': 'A', 'type': '1', 'typeBDR': '', 'marketIndicator': segment,
                 'issuingCompany': prefix}
                for segment, prefix in [('1', 'ABEV'), ('18', 'BBAS'), ('16', 'ITUB'), ('17', 'PETR')]]
                    + [{'status': 'A', 'type': '7', 'typeBDR': '', 'issuingCompany': 'TEST'}]}
    assert set(company_catalog(Source())) == {'ABEV', 'BBAS', 'ITUB', 'PETR'}


def test_payment_dates_are_not_record_dates_and_unknown_payment_blocks():
    rows = ''.join(f'<tr><td>Dividendos</td><td>01/01/{year}</td><td>15/03/{year}</td><td>0,50</td></tr>'
                   for year in range(2015, 2027))
    body = f'<table id="table-dividends-history"><tbody>{rows}</tbody></table>'.encode()
    history, ledger, comparable = paid_history(body, 'TEST3', date(2026, 9, 30))
    assert history[-1][0] == date(2026, 3, 15)
    assert ledger[-1]['record_date'] == '2026-01-01'
    assert ledger[-1]['payment_date'] == '2026-03-15'
    assert comparable[date(2026, 1, 1)] == .5
    with pytest.raises(ValueError, match='missing_payment'):
        paid_history(body.replace(b'15/03/2025', b'-'), 'TEST3', date(2026, 9, 30))


def test_b3_class_and_quote_unit_must_match_mature_payments():
    rows = [{'typeStock': share, 'lastDatePriorEx': f'01/01/{year}', 'corporateAction': 'DIVIDENDO',
             'valueCash': amount, 'quotedPerShares': '1000'}
            for year in range(2022, 2026) for share, amount in [('ON', '500,00'), ('PN', '600,00')]]
    comparable = {date(y, 1, 1): .5 for y in range(2022, 2026)}
    assert len(check_cash_amounts(rows, comparable, date(2026, 9, 30), 'ON')) == 4
    with pytest.raises(ValueError, match='disagrees'):
        check_cash_amounts(rows, comparable, date(2026, 9, 30), 'PN')
    with pytest.raises(ValueError, match='missing_from_payment'):
        check_cash_amounts(rows, {d: a for d, a in comparable.items() if d.year != 2023}, date(2026, 9, 30), 'ON')


def test_latest_year_is_reconciled_too_not_only_mature_rights():
    rows = [{'typeStock': 'ON', 'lastDatePriorEx': f'01/01/{year}', 'corporateAction': 'DIVIDENDO',
             'valueCash': '0,50', 'quotedPerShares': '1'} for year in range(2022, 2027)]
    comparable = {date(y, 1, 1): .5 for y in range(2022, 2027)}
    assert len(check_cash_amounts(rows, comparable, date(2026, 9, 30), 'ON')) == 5
    with pytest.raises(ValueError, match='disagrees'):
        check_cash_amounts(rows, {**comparable, date(2026, 1, 1): 5000}, date(2026, 9, 30), 'ON')
    with pytest.raises(ValueError, match='missing_from_payment'):
        check_cash_amounts(rows, {d: a for d, a in comparable.items() if d.year != 2026}, date(2026, 9, 30), 'ON')


def test_future_installments_reconcile_rights_but_do_not_become_paid_cash():
    rows = ''.join(f'<tr><td>Dividendos</td><td>01/01/{year}</td><td>15/03/{year}</td><td>0,50</td></tr>'
                   for year in range(2015, 2027))
    rows += '<tr><td>Dividendos</td><td>01/01/2026</td><td>15/12/2026</td><td>0,25</td></tr>'
    body = f'<table id="table-dividends-history"><tbody>{rows}</tbody></table>'.encode()
    history, ledger, comparable = paid_history(body, 'TEST3', date(2026, 9, 30))
    assert comparable[date(2026, 1, 1)] == .75
    assert sum(amount for day, _, amount in history if day.year == 2026) == .5
    assert history.future_events == 1
    assert all(r['payment_date'] <= '2026-09-30' for r in ledger)


def test_payment_coverage_has_explicit_start_not_assumed_preceding_zeroes():
    rows = ''.join(f'<tr><td>Dividendos</td><td>01/01/{year}</td><td>15/03/{year}</td><td>0,50</td></tr>'
                   for year in range(2019, 2027))
    history, _, _ = paid_history(f'<table id="table-dividends-history"><tbody>{rows}</tbody></table>'.encode(),
                                  'TEST3', date(2026, 9, 30))
    assert history.coverage_start == date(2019, 3, 15)
    # The previous twelve-month feature window cannot predate this boundary.
    assert shift_month(history.coverage_start, 12).year == 2020


def price_payload(cutoff, *, split=None, gap=False):
    days = [shift_month(cutoff, -i) for i in reversed(range(61))]
    if gap:
        days.pop(20)
    result = {'meta': {'symbol': 'TEST3.SA', 'currency': 'BRL'},
              'timestamp': [int(datetime(d.year, d.month, d.day, 16, tzinfo=timezone.utc).timestamp()) for d in days],
              'indicators': {'quote': [{'close': [10.] * len(days)}]},
              'events': {'splits': {'1': {'date': int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()),
                                         'numerator': 2, 'denominator': 1}} if split else {}}}
    return json.dumps({'chart': {'result': [result], 'error': None}}).encode()


def test_prices_require_full_common_history_and_reject_recent_splits():
    cutoff = date(2026, 9, 30)
    prices, actions = split_price_history(price_payload(cutoff), 'TEST3', cutoff)
    assert len(prices) == 61 and not actions
    with pytest.raises(ValueError, match='share_action'):
        split_price_history(price_payload(cutoff, split=True), 'TEST3', cutoff)
    with pytest.raises(ValueError, match='61_consecutive'):
        split_price_history(price_payload(cutoff, gap=True), 'TEST3', cutoff)


def test_independent_price_scale_check_rejects_dividend_adjusted_prices():
    cutoff = date(2026, 9, 30)
    rows = [{'typeStock': 'ON', 'lastDatePriorEx': shift_month(cutoff, -m).strftime('%d/%m/%Y'),
             'dateClosingPricePriorExDate': shift_month(cutoff, -m).strftime('%d/%m/%Y'),
             'closingPricePriorExDate': '10,00'} for m in range(1, 8)]
    assert len(check_b3_prices(price_payload(cutoff), rows, cutoff, 'ON')) == 7
    rows[0]['closingPricePriorExDate'] = '20,00'
    with pytest.raises(ValueError, match='quote_or_share_scale'):
        check_b3_prices(price_payload(cutoff), rows, cutoff, 'ON')


@pytest.mark.parametrize('unknown_date', [None, '', '31/12/9999'])
def test_price_reference_without_quote_date_cannot_borrow_cash_right_date(unknown_date):
    cutoff = date(2026, 9, 30)
    rows = [{'typeStock': 'ON', 'lastDatePriorEx': shift_month(cutoff, -m).strftime('%d/%m/%Y'),
             'dateClosingPricePriorExDate': unknown_date,
             'closingPricePriorExDate': '10,00'} for m in range(1, 8)]
    with pytest.raises(ValueError, match='insufficient_independent_price_matches'):
        check_b3_prices(price_payload(cutoff), rows, cutoff, 'ON')
