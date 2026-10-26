import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import zipfile

import openpyxl
import pytest

from .payment_sources import (SOURCE_DISCOVERIES, _fetch_primary, collect_primary_payments, inspect_cvm_proventos_index,
                              inspect_payment_page, inspect_petrobras_workbook)


AS_OF = date(2026, 9, 30)
OBSERVED = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
HEADERS = ('Tipo', 'Data com', 'Pagamento', 'Valor')
ROW = ('JSCP', '21/09/2026', '31/12/2026', '0,07130000')


def page(rows=(ROW,), headers=HEADERS, *, before='', attributes='', title='TEST4'):
    head = ''.join('<th><h3>' + item + '</h3></th>' for item in headers)
    body = ''.join('<tr>' + ''.join('<td>' + str(item) + '</td>' for item in row) + '</tr>' for row in rows)
    return f'<html><title>{title}</title>{before}<table {attributes}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></html>'


def inspect(content, ticker='TEST4', as_of=AS_OF, observed_at=OBSERVED):
    return inspect_payment_page(content, ticker, as_of, observed_at)


def codes(result):
    return {item['code'] for item in result['report']['issues']}


def test_legacy_shape_without_id_has_no_history_count_gate():
    result = inspect(page())
    assert result['status'] == 'available'
    assert result['source_format'] == 'html_tables'
    assert result['report']['counts'] == {'announced': 1, 'events': 1}
    event = result['events'][0]
    assert event['record_date'] == '2026-09-21'
    assert event['payment_date'] == '2026-12-31'
    assert event['gross_per_share'] == .0713
    assert event['label'] == 'JCP'
    assert event['available_as_of'] is None
    assert event['paid_status_as_of_verified'] is False
    assert event['payment_status_observed_at'] == OBSERVED.isoformat()
    assert not event['cash_model_eligible']
    assert not event['financially_confirmed']
    assert result['field_status']['published_at'] == 'not_found'
    assert 'payment_history_too_short' not in codes(result)
    assert not result['report']['history_complete']
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('attributes', ('', 'id="changed"', 'id="table-dividends-history"'))
def test_id_does_not_change_reading(attributes):
    assert inspect(page(attributes=attributes))['events'][0]['status'] == 'announced'


@pytest.mark.parametrize('label,expected', [('DIV', 'DIVIDENDO'), ('Dividendos', 'DIVIDENDO'),
                                           ('JCP', 'JCP'), ('Juros sobre o capital pr\u00f3prio', 'JCP')])
def test_labels(label, expected):
    assert inspect(page(((label, *ROW[1:]),)))['events'][0]['label'] == expected


def test_past_scheduled_date_is_not_paid():
    result = inspect(page((('Dividendos', '10/06/2026', '20/06/2026', '0,25'),)))
    assert result['status'] == 'available'
    assert result['events'][0]['status'] == 'past_scheduled'
    assert not result['events'][0]['cash_model_eligible']


@pytest.mark.parametrize('reported,expected', [('PAGO', 'paid_reported'), ('A PAGAR', 'past_scheduled'),
                                              ('PAGO PRESCRITO', 'paid_reported'),
                                              ('', 'past_scheduled'), ('cancelado', 'cancelled')])
def test_explicit_reported_status(reported, expected):
    headers = (*HEADERS, 'Situa\u00e7\u00e3o')
    result = inspect(page((('DIV', '01/06/2026', '15/06/2026', '0,10', reported),), headers))
    assert result['events'][0]['status'] == expected
    assert not result['events'][0]['financially_confirmed']


def test_paid_status_in_later_observation_does_not_backdate_payment():
    result = inspect(page(((*ROW, 'PAGO'),), (*HEADERS, 'Status')))
    assert result['events'][0]['status'] == 'announced'
    assert 'paid_status_after_as_of' in codes(result)


@pytest.mark.parametrize('header', ['Data ex', 'Ex-date', 'Data ex-Dividendos'])
def test_ex_date_is_never_payment_date(header):
    result = inspect(page(headers=('Tipo', 'Data com', header, 'Valor')))
    assert result['status'] == 'partial'
    assert result['events'][0]['payment_date'] is None
    assert result['events'][0]['record_date'] == '2026-09-21'
    assert result['field_status']['payment_date'] == 'not_found'


@pytest.mark.parametrize('value', ['', '-', 'A definir', 'N\u00e3o informado', '31/02/2026', '0'])
def test_unknown_payment_date_is_not_zero(value):
    result = inspect(page((('DIV', '01/06/2026', value, '0,25'),)))
    assert result['status'] == 'partial'
    assert result['events'][0]['payment_date'] is None
    assert result['events'][0]['status'] == 'unresolved'


def test_publication_reference_and_approval_are_distinct_from_cash():
    headers = (*HEADERS, 'Data de divulga\u00e7\u00e3o', 'Data de refer\u00eancia', 'Data de aprova\u00e7\u00e3o')
    row = ('DIV', '01/06/2026', '20/06/2026', '0,25', '01/10/2026', '31/12/2025', '25/05/2026')
    result = inspect(page((row,), headers))
    event = result['events'][0]
    assert event['available_as_of'] is False
    assert event['published_at'] == '2026-10-01'
    assert event['reference_date'] == '2025-12-31'
    assert event['approval_date'] == '2026-05-25'
    assert event['payment_date'] == '2026-06-20'
    assert 'publication_after_as_of' in codes(result)


def test_prior_publication_can_be_available_but_is_not_confirmed_cash():
    result = inspect(page(((*ROW, '01/09/2026'),), (*HEADERS, 'Data de publica\u00e7\u00e3o')))
    assert result['events'][0]['available_as_of'] is True
    assert not result['events'][0]['cash_model_eligible']


@pytest.mark.parametrize('value,expected', [('0,25', .25), ('1.234,56', 1234.56),
                                          ('R$ 1,23456789', 1.23456789), ('1.000', 1000), ('0', 0)])
def test_strict_brazilian_numbers(value, expected):
    result = inspect(page((('DIV', *ROW[1:3], value),)))
    assert result['events'][0]['gross_per_share'] == expected
    assert result['status'] != 'zero_window_confirmed'


@pytest.mark.parametrize('value', ['1.23', '1,234.56', 'NaN', 'Infinity', '-0,01', '', 'R$ --', '1 234,56'])
def test_invalid_numbers_are_not_imputed(value):
    result = inspect(page((('DIV', *ROW[1:3], value),)))
    assert result['status'] == 'partial'
    assert result['events'][0]['gross_per_share'] is None
    assert result['field_status']['gross_per_share'] == 'invalid'


def test_explicit_quote_quantity_normalizes_but_not_total_declared_cash():
    result = inspect(page(((*ROW[:3], '250,00', '1.000'),), (*HEADERS, 'Quantidade base')))
    event = result['events'][0]
    assert event['gross_per_share'] == .25
    assert event['reported_amount'] == 250
    assert event['quoted_quantity'] == 1000
    total = inspect(page(headers=('Tipo', 'Data com', 'Pagamento', 'Montante total')))
    assert total['status'] == 'not_found'
    assert not total['events']


@pytest.mark.parametrize('quantity', ['0', '-1', '1,5', 'NaN', ''])
def test_invalid_quantity_is_not_one(quantity):
    result = inspect(page(((*ROW, quantity),), (*HEADERS, 'Qtd. cota\u00e7\u00e3o')))
    assert result['events'][0]['gross_per_share'] is None
    assert result['status'] == 'partial'


def test_explicit_per_thousand_header():
    result = inspect(page(((*ROW[:3], '250,00'),), (*HEADERS[:3], 'Valor bruto por 1.000 a\u00e7\u00f5es')))
    assert result['events'][0]['gross_per_share'] == .25


def test_ambiguous_quantity_not_automatically_per_share():
    result = inspect(page(((*ROW, '1000'),), (*HEADERS, 'Qtd')))
    assert result['status'] == 'partial'
    assert result['events'][0]['gross_per_share'] is None


def test_multirow_header_spans_and_gross_not_net():
    content = '''<table><thead>
        <tr><th rowspan="2">Tipo</th><th rowspan="2">Data com</th>
        <th rowspan="2">Pagamento</th><th colspan="2">Valor</th></tr>
        <tr><th>Bruto</th><th>Liquido</th></tr></thead><tbody>
        <tr><td>JCP</td><td>01/06/2026</td><td>10/06/2026</td><td>0,20</td><td>0,17</td></tr>
        </tbody></table>'''
    result = inspect(content)
    assert result['status'] == 'available'
    assert result['events'][0]['gross_per_share'] == .2


def test_no_table_is_not_zero_and_empty_table_is_not_zero():
    for content in ('<p>Dados indisponiveis</p>', page(rows=()), '<table><tr><th>Empresa</th><th>PL</th></tr></table>'):
        result = inspect(content)
        assert result['status'] == 'not_found'
        assert result['events'] == []
        assert result['field_status']['payment_date'] == 'not_found'


def test_bounded_explicit_proventos_zero_is_only_a_source_window_assertion():
    result = inspect('<p>TEST4 n\u00e3o pagou proventos entre 01/10/2025 e 30/09/2026.</p>')
    assert result['status'] == 'zero_window_confirmed'
    assert result['events'] == []
    assert not result['report']['financially_confirmed']
    assert not result['report']['history_complete']


@pytest.mark.parametrize('claim', [
    'TEST4 n\u00e3o pagou dividendos entre 01/10/2025 e 30/09/2026.',
    'Nos \u00faltimos 12 meses a empresa n\u00e3o pagou dividendos.',
    'TEST4 n\u00e3o distribuiu proventos no \u00faltimo ano.',
    'TEST4 n\u00e3o pagou proventos entre 01/01/2026 e 30/09/2026.',
])
def test_dividend_only_or_unanchored_or_short_zero_is_partial(claim):
    result = inspect('<p>' + claim + '</p>')
    assert result['status'] == 'partial'
    assert result['report']['zero_claims']


def test_rolling_zero_requires_explicit_reference_not_retrieval_backdated():
    content = '<meta name="reference_date" content="2026-09-30"><p>TEST4 n\u00e3o pagou proventos nos \u00faltimos 12 meses.</p>'
    assert inspect(content)['status'] == 'zero_window_confirmed'
    assert inspect(content.replace('2026-09-30', '2026-10-24'))['status'] == 'partial'


def test_generic_formula_disclaimer_not_a_zero_claim():
    result = inspect(page(before='<div>Formula nao recomendada para empresas que nao pagam dividendos.</div>'))
    assert result['status'] == 'available'
    assert result['report']['zero_claims'] == []


def test_zero_without_any_ticker_identity_not_confirmed():
    result = inspect('<p>Nao pagou proventos entre 01/10/2025 e 30/09/2026.</p>')
    assert result['status'] == 'partial'


def test_zero_claim_contradicting_event_is_partial():
    content = page((('DIV', '01/06/2026', '10/06/2026', '0,25'),),
                   before='<p>TEST4 nao pagou proventos entre 01/10/2025 e 30/09/2026.</p>')
    result = inspect(content)
    assert result['status'] == 'partial'
    assert 'zero_claim_conflicts_with_event' in codes(result)


@pytest.mark.parametrize('wrapper', ['<div class="comments">{}</div>', '<section id="user-reviews">{}</section>', '<footer>{}</footer>'])
def test_comments_and_other_ticker_cannot_confirm_zero(wrapper):
    statement = '<p>TEST4 n\u00e3o pagou proventos entre 01/10/2025 e 30/09/2026.</p>'
    assert inspect(wrapper.format(statement))['status'] == 'not_found'
    assert inspect(statement.replace('TEST4', 'OTHR4'))['status'] == 'not_found'


def test_duplicate_responsive_tables_are_not_double_counted():
    result = inspect(page() + page(attributes='class="mobile"'))
    assert len(result['events']) == 1
    assert 'duplicate_event' in codes(result)


def test_conflicting_rows_preserve_both_and_issue():
    result = inspect(page((ROW, (*ROW[:3], '0,10'))))
    assert len(result['events']) == 2
    assert result['status'] == 'partial'
    assert 'conflicting_event_amount' in codes(result)


def test_invalid_order_and_non_income_are_not_silently_paid():
    result = inspect(page((('DIV', '21/09/2026', '20/09/2026', '0,10'), ('Amortiza\u00e7\u00e3o', *ROW[1:]))))
    assert len(result['events']) == 1
    assert result['status'] == 'partial'
    assert not result['events'][0]['date_order_valid']
    assert 'unsupported_or_missing_income_label' in codes(result)


def test_utf8_bytes_and_td_headers():
    content = page(headers=('Tipo de provento', 'Data base da posi\u00e7\u00e3o acion\u00e1ria', 'Data do pagamento', 'Valor bruto'))
    content = '<meta charset="utf-8">' + content.replace('<th>', '<td>').replace('</th>', '</td>')
    result = inspect(content.encode('utf8'))
    assert result['status'] == 'available'


@pytest.mark.parametrize('content,expected', [('', 'invalid'), ('<p>Access denied captcha</p>', 'invalid'),
                                             (page(title='OTHR4'), 'invalid')])
def test_invalid_or_wrong_identity(content, expected):
    assert inspect(content)['status'] == expected


@pytest.mark.parametrize('ticker,as_of,observed', [('test4', AS_OF, OBSERVED), ('TEST4', AS_OF, datetime(2026, 10, 24)),
                                                  ('TEST4', '2027-01-01', OBSERVED)])
def test_invalid_context(ticker, as_of, observed):
    with pytest.raises(ValueError):
        inspect(page(), ticker, as_of, observed)


def test_official_alphanumeric_ticker_b3sa3_is_parsed_with_exact_identity():
    result = inspect(page(title='B3SA3'), ticker='B3SA3')
    assert result['status'] == 'available'
    assert result['events'][0]['ticker'] == 'B3SA3'
    assert result['report']['page_ticker_verified'] is True
    assert inspect(page(title='B3SA3'), ticker='TEST4')['status'] == 'invalid'
    zero = inspect('<p>B3SA3 nao pagou proventos entre 01/10/2025 e 30/09/2026.</p>', ticker='B3SA3')
    assert zero['status'] == 'zero_window_confirmed'


@pytest.mark.parametrize('ticker', ['b3sa3', '3BSA3', 'B3SA', 'B3SA333', 'B3-SA3', '../B3SA3', 'B3SA3F', 'B3SA3\n'])
def test_invalid_tickers_remain_rejected(ticker):
    with pytest.raises(ValueError):
        inspect(page(), ticker=ticker)


def workbook(*, formula=False, duplicate=False, label='DIV'):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['A\u00e7\u00e3o Ordin\u00e1ria (ON - PETR3)', None, None, None, None, None, None, None,
                  'A\u00e7\u00e3o Preferencial (PN - PETR4)'])
    headers = ['Tipo', 'Data do pagamento', 'Data da divulga\u00e7\u00e3o', 'Data base da posi\u00e7\u00e3o acion\u00e1ria',
               'Valor bruto declarado*', 'Valor l\u00edquido pago**', 'Situa\u00e7\u00e3o', None]
    sheet.append(headers + headers)
    row = [label, date(2026, 6, 30), date(2026, 6, 1), date(2026, 6, 10),
           '=1+1' if formula else .2, .17, 'PAGO', None]
    other = ['JCP', date(2026, 12, 31), date(2026, 9, 1), date(2026, 9, 10), .3, .255, 'A PAGAR', None]
    sheet.append(row + other)
    if duplicate:
        book.copy_worksheet(sheet)
    target = io.BytesIO()
    book.save(target)
    book.close()
    return target.getvalue()


def test_primary_xlsx_exact_block_gross_not_net_and_dates():
    body = workbook()
    before = hashlib.sha256(body).hexdigest()
    result = inspect_petrobras_workbook(body, 'PETR3', AS_OF, OBSERVED)
    assert result['status'] == 'available'
    assert result['events'][0]['gross_per_share'] == .2
    assert result['events'][0]['status'] == 'paid_reported'
    assert not result['events'][0]['cash_model_eligible']
    other = inspect_petrobras_workbook(body, 'PETR4', AS_OF, OBSERVED)
    assert other['events'][0]['gross_per_share'] == .3
    assert other['events'][0]['status'] == 'announced'
    assert before == hashlib.sha256(body).hexdigest()
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('label', ['Dividendos Extraordin\u00e1rios', 'Dividendoss Extraordin\u00e1rios'])
def test_primary_petrobras_extraordinary_label_keeps_original_gross_and_dates(label):
    body = workbook(label=label)
    parsed = inspect_petrobras_workbook(body, 'PETR3', AS_OF, OBSERVED)
    event, = parsed['events']
    assert event['label'] == 'DIVIDENDO'
    assert event['reported_income_label'] == label
    assert event['gross_per_share'] == .2
    assert event['payment_date'] == '2026-06-30'
    assert event['record_date'] == '2026-06-10'
    assert event['status'] == 'paid_reported'
    assert not event['financially_confirmed']
    assert not event['cash_model_eligible']
    assert inspect_petrobras_workbook(body, 'PETR4', AS_OF, OBSERVED)['events'][0]['label'] == 'JCP'
    assert not inspect(page(((label, '10/06/2026', '20/06/2026', '0,25'),)))['events']


@pytest.mark.parametrize('label', ['Restitui\u00e7\u00e3o de capital', 'Amortiza\u00e7\u00e3o',
                                  'Dividendos e restitui\u00e7\u00e3o de capital', 'Dividendoss'])
def test_primary_petrobras_extraordinary_fix_never_turns_principal_or_unknown_label_into_income(label):
    assert not inspect_petrobras_workbook(workbook(label=label), 'PETR3', AS_OF, OBSERVED)['events']


def test_formula_not_cached_gross_and_ambiguous_block_invalid():
    result = inspect_petrobras_workbook(workbook(formula=True), 'PETR3', AS_OF, OBSERVED)
    assert result['status'] == 'partial'
    assert result['events'][0]['gross_per_share'] is None
    assert inspect_petrobras_workbook(workbook(duplicate=True), 'PETR3', AS_OF, OBSERVED)['status'] == 'invalid'
    assert inspect_petrobras_workbook(workbook(), 'OTHR4', AS_OF, OBSERVED)['status'] == 'invalid'


CVM_URL = SOURCE_DISCOVERIES[2]['url']


def cvm_csv(rows=None):
    headers = ['Codigo_CVM', 'Data_Referencia', 'Categoria', 'Data_Entrega', 'Protocolo_Entrega', 'Versao', 'Link_Download']
    if rows is None:
        rows = [['001023', '2026-02-11', 'Relat\u00f3rio Proventos', '2026-02-12', '', '1', CVM_URL],
                ['1023', '2026-10-01', 'Relat\u00f3rio Proventos', '2026-10-02', 'ABC123', '2', CVM_URL],
                ['9999', '2026-02-11', 'Relat\u00f3rio Proventos', '2026-02-12', '', '1', CVM_URL]]
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode('latin1')


def test_primary_cvm_index_is_discovery_not_cash_or_zero():
    result = inspect_cvm_proventos_index(cvm_csv(), 1023, AS_OF, OBSERVED)
    assert result['status'] == 'partial'
    assert result['events'] == []
    assert len(result['documents']) == 2
    first, second = result['documents']
    assert first['protocol'] is None
    assert first['published_at'] == '2026-02-12'
    assert first['reference_date'] == '2026-02-11'
    assert first['available_as_of'] is True
    assert second['available_as_of'] is False
    assert second['protocol'] == 'ABC123'
    assert second['version'] == 2
    assert not first['cash_model_eligible']
    assert inspect_cvm_proventos_index(cvm_csv(), 111, AS_OF, OBSERVED)['status'] == 'not_found'
    json.dumps(result, allow_nan=False)


def test_collector_accepts_exact_alphanumeric_b3sa3_spec(tmp_path):
    calls = []

    def fetch(url, **limits):
        calls.append(url)
        return 200, cvm_csv()

    result = collect_primary_payments(tmp_path, [{'ticker': 'B3SA3', 'cvm': 21610}], AS_OF,
                                      observed_at=OBSERVED, fetch=fetch)
    assert result['requests_made'] == len(calls) == 1
    assert result['by_ticker']['B3SA3']['status'] == 'partial'
    assert result['by_ticker']['B3SA3']['events'] == []


def test_primary_cvm_zip_and_traversal_never_extract(tmp_path):
    for member, expected in [('ipe_cia_aberta_2026.csv', 'partial'), ('../ipe_cia_aberta_2026.csv', 'invalid')]:
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr(member, cvm_csv())
        assert inspect_cvm_proventos_index(data.getvalue(), 1023, AS_OF, OBSERVED)['status'] == expected
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url', ['https://name:secret@www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?token=secret',
                                CVM_URL + '&token=secret', 'http://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx',
                                'https://example.org/file', CVM_URL + '&numVersao=2'])
def test_cvm_unsafe_urls_not_returned_or_logged(url):
    body = cvm_csv([['1023', '2026-02-11', 'Relat\u00f3rio Proventos', '2026-02-12', '', '1', url]])
    result = inspect_cvm_proventos_index(body, 1023, AS_OF, OBSERVED)
    assert result['status'] == 'invalid'
    assert result['documents'] == []
    assert 'secret' not in json.dumps(result)


def test_manifest_primary_sources_and_explicit_restrictions():
    assert all(source['primary'] for source in SOURCE_DISCOVERIES)
    assert SOURCE_DISCOVERIES[0]['proventos_rows'] == 546
    assert 'incomplete transfer' in SOURCE_DISCOVERIES[2]['limitation']
    assert SOURCE_DISCOVERIES[-1]['tickers'] == ['PETR3', 'PETR4']


def test_collector_shared_issuer_request_once_and_immutable_cache(tmp_path):
    body, calls = workbook(), []

    def fetch(url, **limits):
        calls.append((url, limits))
        return 200, body

    specs = [{'ticker': 'PETR3', 'cvm': 9512}, {'ticker': 'PETR4', 'cvm': '09512'}]
    result = collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED, fetch=fetch)
    assert len(calls) == result['requests_made'] == 1
    assert result['status'] == 'available'
    assert result['by_ticker']['PETR3']['events'][0]['gross_per_share'] == .2
    assert result['by_ticker']['PETR4']['events'][0]['gross_per_share'] == .3
    assert result['manifest'][0]['http_status'] == 200
    assert result['manifest'][0]['status'] == 'downloaded_valid'
    files = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert len(files) == 1
    cached = collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED, fetch=fetch)
    assert cached['requests_made'] == 0
    assert cached['manifest'][0]['status'] == 'cached'
    assert len(calls) == 1
    assert files == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('failure', [403, 429, 500, 200])
def test_refresh_failure_preserves_prior_valid_file(tmp_path, failure):
    specs = [{'ticker': 'PETR3', 'cvm': 9512}]
    collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED, fetch=lambda *a, **k: (200, workbook()))
    previous = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    result = collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED, refresh=True,
                                      fetch=lambda *a, **k: (failure, b'<html>Login instead of workbook</html>'))
    assert result['by_ticker']['PETR3']['events'][0]['gross_per_share'] == .2
    assert result['manifest'][0]['http_status'] == failure
    assert result['manifest'][0]['status'] == 'refresh_failed_cached_prior_retained'
    assert result['by_ticker']['PETR3']['status'] == 'partial'
    assert previous == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


@pytest.mark.parametrize('status', [403, 429])
def test_access_denial_stops_further_requests_without_bypass(tmp_path, status):
    calls = []

    def fetch(url, **limits):
        calls.append(url)
        return status, b''

    specs = [{'ticker': 'PETR3', 'cvm': 9512}, {'ticker': 'OTHR3', 'cvm': 1023}]
    result = collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED, fetch=fetch)
    assert len(calls) == 1
    assert result['status'] == 'partial'
    assert not result['by_ticker']['PETR3']['events']
    assert result['by_ticker']['OTHR3']['status'] == 'partial'
    assert result['manifest'][1]['status'] == 'request_budget_or_access_stop'
    assert not list(tmp_path.iterdir())


def test_cvm_discovery_bounded_publications_no_pdf_crawl(tmp_path):
    body = cvm_csv()
    calls = []

    def fetch(url, **limits):
        calls.append(url)
        return 200, body

    specs = [{'ticker': 'TEST4', 'cvm': 1023}, {'ticker': 'OTHR4', 'cvm': 1023}]
    result = collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED,
                                      max_documents=1, fetch=fetch)
    assert result['requests_made'] == 1
    assert all(url.startswith('https://dados.cvm.gov.br/') and url.endswith('.zip') for url in calls)
    bundle = result['by_ticker']['TEST4']
    assert len(bundle['documents']) == len(bundle['future_documents']) == 1
    assert bundle['documents'][0]['published_at'] <= AS_OF.isoformat()
    assert bundle['future_documents'][0]['published_at'] > AS_OF.isoformat()
    assert bundle['status'] == 'partial'
    assert not bundle['events']


def test_collector_budget_zero_and_no_global_data_is_partial_not_zero(tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError('No requests should run.')

    result = collect_primary_payments(tmp_path, [{'ticker': 'TEST4', 'cvm': 1023}], AS_OF,
                                      observed_at=OBSERVED, max_requests=0, fetch=fail_if_called)
    assert result['requests_made'] == 0
    assert result['status'] == 'partial'
    assert result['by_ticker']['TEST4']['field_status']['payment_date'] == 'not_found'
    assert result['by_ticker']['TEST4']['events'] == []


def test_collector_cvm_reference_count_cap(tmp_path):
    rows = [['1023', f'2026-06-{day:02d}', 'Relat\u00f3rio Proventos', f'2026-06-{day:02d}', '', '1', CVM_URL]
            for day in range(1, 8)]
    result = collect_primary_payments(tmp_path, [{'ticker': 'TEST4', 'cvm': 1023}], AS_OF,
                                      observed_at=OBSERVED, max_documents=2,
                                      fetch=lambda *a, **k: (200, cvm_csv(rows)))
    bundle = result['by_ticker']['TEST4']
    assert len(bundle['documents']) == 2
    assert bundle['documents'][0]['published_at'] == '2026-06-07'
    assert 'document_reference_limit' in codes(bundle)


def test_collector_exception_text_does_not_leak_secrets(tmp_path):
    def fetch(*args, **kwargs):
        raise OSError('token=secret password=private')

    result = collect_primary_payments(tmp_path, [{'ticker': 'TEST4', 'cvm': 1023}], AS_OF,
                                      observed_at=OBSERVED, fetch=fetch)
    assert result['status'] == 'partial'
    assert 'secret' not in json.dumps(result)
    assert result['manifest'][0]['error_class'] == 'OSError'


@pytest.mark.parametrize('specs,limits', [
    ([{'ticker': '../TEST4', 'cvm': 1023}], {}),
    ([{'ticker': 'PETR3', 'cvm': 1023}], {}),
    ([{'ticker': 'TEST4', 'cvm': 1023, 'sources': ['https://b3.com.br']}], {}),
    ([{'ticker': 'TEST4', 'cvm': 1023, 'years': [2027]}], {}),
    ([{'ticker': 'TEST4', 'cvm': 1023, 'years': [2026, 2026]}], {}),
    ([{'ticker': 'TEST4', 'cvm': 1023}], {'max_requests': 11}),
    ([{'ticker': 'TEST4', 'cvm': 1023}], {'max_requests': True}),
])
def test_collector_rejects_unbounded_or_unsafe_specs_before_network(tmp_path, specs, limits):
    calls = []
    with pytest.raises(ValueError):
        collect_primary_payments(tmp_path, specs, AS_OF, observed_at=OBSERVED,
                                 fetch=lambda *a, **k: calls.append(a), **limits)
    assert not calls
    assert not list(tmp_path.iterdir())


def test_default_transport_validates_tls_and_rejects_cross_host_redirect(monkeypatch):
    import ssl
    import urllib.request

    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 101
            return b'payload'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == SOURCE_DISCOVERIES[-1]['url']
            assert timeout == 10
            return Response()

    def build(*handlers):
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    assert _fetch_primary(SOURCE_DISCOVERIES[-1]['url'], timeout=10, max_bytes=100) == (200, b'payload')
    assert captured[1]._context.verify_mode == ssl.CERT_REQUIRED
    assert captured[1]._context.check_hostname
    request = urllib.request.Request(SOURCE_DISCOVERIES[-1]['url'])
    with pytest.raises(Exception, match='unapproved_redirect'):
        captured[0].redirect_request(request, None, 302, 'Found', {}, 'https://www.b3.com.br/anything')


def test_incomplete_transfer_keeps_exact_http_status_in_manifest(tmp_path, monkeypatch):
    import http.client
    import urllib.request

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            raise http.client.IncompleteRead(b'partial secret response')

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: Opener())
    result = collect_primary_payments(tmp_path, [{'ticker': 'TEST4', 'cvm': 1023}], AS_OF, observed_at=OBSERVED)
    assert result['manifest'][0]['http_status'] == 200
    assert result['manifest'][0]['status'] == 'refresh_failed'
    assert result['status'] == 'partial'
    assert 'secret' not in json.dumps(result)
    assert not list(tmp_path.iterdir())
