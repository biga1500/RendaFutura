from datetime import date, datetime
import hashlib
from html import escape
import io
import json
from urllib.error import HTTPError
from urllib.request import Request
import xml.etree.ElementTree as ET
import zipfile

from openpyxl import Workbook
from openpyxl.utils.datetime import to_excel
import pytest

from . import issuer_operating_sources as operating


AS_OF = date(2026, 10, 24)


def source(ticker, kind='landing'):
    return {'id': f'test:{ticker}:{kind}', 'url': operating.ISSUERS[ticker][kind],
            'retrieved_at': '2026-10-24T20:00:00+00:00'}


def table(headers, rows):
    return '<table><tr>' + ''.join('<th>' + escape(str(v)) + '</th>' for v in headers) + '</tr>' + ''.join(
        '<tr>' + ''.join('<td>' + escape(str(v)) + '</td>' for v in row) + '</tr>' for row in rows) + '</table>'


def agro(*rows, english=False):
    headers = (['Type', 'Announcement Date', 'Payment Date', 'Amount R$', 'R$/share'] if english else
               ['Tipo', 'Data da Aprova\u00e7\u00e3o', 'Data Pagamento', 'Valor Total R$', 'R$/a\u00e7\u00e3o'])
    return table(headers, rows).encode('utf-8')


def workbook(rows, *, ticker='TAEE3', formula_caches=None, merges=()):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Dividendos' if ticker == 'TAEE3' else 'Alupar'
    headers = (['Remunera\u00e7\u00e3o', 'Valor distribu\u00eddo', 'Lucro L\u00edquido do Exercicio',
                '# a\u00e7\u00f5es Capital', 'DPS', 'DPU', 'Aprova\u00e7\u00e3o', 'Ex dividend', 'Pagamento']
               if ticker == 'TAEE3' else ['Ano Pagamento', 'Ato Societ\u00e1rio', 'Data de Aprova\u00e7\u00e3o',
               'Data Record1', 'Data Ex-Dividendos', 'Data de Pagamento', 'Por a\u00e7\u00e3o (R$)', 'Por Unit (R$)'])
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    for merged in merges:
        sheet.merge_cells(merged)
    for coordinate, (value, date_style) in (formula_caches or {}).items():
        if date_style:
            sheet[coordinate].number_format = 'yyyy-mm-dd'
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    if not formula_caches:
        return buffer.getvalue()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as old, zipfile.ZipFile(output, 'w') as new:
        for info in old.infolist():
            content = old.read(info)
            if info.filename == 'xl/worksheets/sheet1.xml':
                root = ET.fromstring(content)
                ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                for coordinate, (value, _) in formula_caches.items():
                    cell = root.find(f'.//s:c[@r="{coordinate}"]', ns)
                    node = cell.find('s:v', ns)
                    if node is None:
                        node = ET.SubElement(cell, '{' + ns['s'] + '}v')
                    node.text = str(value)
                content = ET.tostring(root)
            new.writestr(info, content)
    return output.getvalue()


def test_b3_gross_payment_ex_date_and_approval_never_become_record_or_publication():
    headers = ['ANO BASE', 'PROVENTO', 'EVENTO-DATA', 'MONTANTE (R$)',
               'VALOR BRUTO POR A\u00c7\u00c3O (R$)', 'DATA EX-PROVENTO', 'DATA DO PAGAMENTO']
    content = ('<div class="accordion__item__header">Hist\u00f3rico de Proventos B3</div>' + table(headers, [
        [2026, 'JCP', 'RCA DE 26/03/2026', '372.500.000,00', '0,07434', '01/04/2026', '13/04/2026']]) +
        '<div class="accordion__item__header">Hist\u00f3rico de Proventos CETIP</div>' + table(headers, [
        [2016, 'JCP', 'RCA DE 01/03/2016', '50.000', '9,99', '02/03/2016', '10/03/2016']]) +
        '<p>Considerar o desdobramento de 1 para 3 a\u00e7\u00f5es realizado em 17/05/2021.</p>').encode('utf-8')
    result = operating.parse_native_history(content, 'B3SA3', AS_OF, source('B3SA3'))
    assert len(result['events']) == 1
    event = result['events'][0]
    assert event['gross_per_share'] == .07434 and event['payment_date'] == '2026-04-13'
    assert event['ex_date'] == '2026-04-01' and event['record_date'] is None
    assert event['approval_date'] == '2026-03-26'
    assert event['published_at'] is None and event['availability_date'] is None
    assert event['status'] == 'past_scheduled' and event['cash_model_eligible'] is False
    assert result['other_security_histories'][0]['events'][0]['ticker'] is None
    action = result['corporate_actions'][0]
    assert action['ratio'] == 3 and action['effective_date'] == '2021-05-17'
    assert action['share_scale_applied'] is False and action['published_at'] is None


@pytest.mark.parametrize('raw,expected', [('3,2386', 3.2386), ('1.3977', 1.3977), ('0.0193', .0193)])
def test_agro_reported_mixed_decimal_notation_is_not_scaled_as_thousands(raw, expected):
    result = operating.parse_native_history(agro(['Dividendos', '28/10/2015', '13/11/2015', '80.665.198,38', raw]),
                                           'AGRO3', AS_OF, source('AGRO3'))
    event = result['events'][0]
    assert event['reported_per_share'] == expected
    assert event['gross_per_share'] is None and event['amount_tax_basis'] == 'unspecified_by_table'


def test_agro_english_dates_are_explicit_us_locale_and_do_not_prove_publication():
    result = operating.parse_native_history(agro(['Dividends', '10/16/2018', '11/06/2018', '41,000,000.00', '0.7620'], english=True),
                                           'AGRO3', AS_OF, source('AGRO3', 'english'))
    event = result['events'][0]
    assert event['approval_date'] == '2018-10-16' and event['payment_date'] == '2018-11-06'
    assert event['published_at'] is None and event['available_as_of'] is None


def test_primary_language_conflict_is_retained_not_repaired_or_doubled():
    pt = operating.parse_native_history(agro(['Dividendos', '16/10/2018', '06/11/2019', '41.000.000', '0,7620']),
                                        'AGRO3', AS_OF, source('AGRO3'))
    en = operating.parse_native_history(agro(['Dividends', '10/16/2018', '11/06/2018', '41,000,000', '0.7620'], english=True),
                                        'AGRO3', AS_OF, source('AGRO3', 'english'))
    result = operating._crosscheck(pt, [en])
    assert len(result['events']) == 1 and result['events'][0]['payment_date'] == '2019-11-06'
    assert result['events'][0]['status'] == 'invalid'
    assert result['issues'][0]['code'] == 'conflicting_primary_payment_dates'
    assert result['issues'][0]['alternative_value'] == '2018-11-06'


def test_unknown_payment_deadline_is_not_a_conflicting_exact_date():
    exact = operating.parse_native_history(agro(['Dividendos', '07/05/2026', '06/07/2026', '100', '0,07']),
                                           'AGRO3', AS_OF, source('AGRO3'))
    deadline = operating.parse_native_history(agro(['Dividendos', '07/05/2026', 'Ate 60 dias', '100', '0,07']),
                                              'AGRO3', AS_OF, source('AGRO3'))
    assert not operating._crosscheck(exact, [deadline])['issues']


def test_alupar_native_payment_cell_survives_unspecified_html_deadline():
    rows = [[2026, 'RCA', datetime(2026, 5, 7), datetime(2026, 5, 14), datetime(2026, 5, 15),
             datetime(2026, 7, 6), .07, .21]]
    native = operating.parse_workbook(workbook(rows, ticker='ALUP3'), 'ALUP3', AS_OF, source('ALUP3', 'workbook'))
    headers = ['Ano Pagamento', 'Ato Societ\u00e1rio', 'Data de Aprova\u00e7\u00e3o', 'Data Record1',
               'Data Ex-Dividendos', 'Data de Pagamento', 'Por a\u00e7\u00e3o (R$)', 'Por Unit (R$)']
    content = table(headers, [[2026, 'RCA', '07/05/2026', '14/05/2026', '15/05/2026',
                               'Ate 60 dias da aprovacao', '0,07', '0,21']]).encode('utf-8')
    alternative = operating.parse_native_history(content, 'ALUP3', AS_OF, source('ALUP3'))
    result = operating._crosscheck(native, [alternative])
    event = result['events'][0]
    assert event['payment_date'] == '2026-07-06'
    assert event['date_value_basis']['payment_date'] == 'native_cell_or_explicit_merge'
    assert event['status'] == 'past_scheduled' and event['issues'] == []
    assert result['issues'] == []
    assert alternative['events'][0]['payment_date'] is None


def test_paid_context_not_date_age_is_required_and_future_is_never_paid():
    rows = [['JCP 2026', 100, None, 1000, .1, .3, 'RCA 01/03/2026', datetime(2026, 3, 2), datetime(2026, 4, 13)],
            ['JCP 2026', 200, None, 1000, .2, .6, 'RCA 01/08/2026', datetime(2026, 8, 3), datetime(2026, 11, 26)],
            [None, 'Ano', 'Proventos Pagos']]
    result = operating.parse_workbook(workbook(rows), 'TAEE3', AS_OF, source('TAEE3', 'workbook'))
    first, future = result['events']
    assert first['status'] == 'paid_reported' and first['reported_per_share'] == .1
    assert first['reported_per_unit'] == .3 and first['gross_per_share'] is None
    assert first['financially_confirmed'] is False and first['paid_status_as_of_verified'] is False
    assert future['status'] == 'announced' and result['history_complete'] is False
    no_context = operating.parse_workbook(workbook(rows[:1]), 'TAEE3', AS_OF, source('TAEE3', 'workbook'))
    assert no_context['events'][0]['status'] == 'past_scheduled'


def test_formula_value_and_payment_date_use_native_stored_caches_without_calculation():
    rows = [['Dividendos 2026', 100, None, 1000, '=B2/D2', '=E2*3', 'RCA 01/03/2026', datetime(2026, 3, 2), '=H2+30']]
    content = workbook(rows, formula_caches={'E2': (.1234, False), 'F2': (.3702, False),
                                             'I2': (to_excel(datetime(2026, 4, 13)), True)})
    event = operating.parse_workbook(content, 'TAEE3', AS_OF, source('TAEE3', 'workbook'))['events'][0]
    assert event['reported_per_share'] == .1234  # Deliberately not recalculated as 100 / 1000.
    assert event['payment_date'] == '2026-04-13'
    assert event['amount_value_basis'] == 'stored_formula_cache'
    assert event['date_value_basis']['payment_date'] == 'stored_formula_cache'
    assert event['raw_fields']['payment_date'] == '=H2+30'


def test_formula_without_numeric_cache_is_unavailable_not_zero_or_computed():
    rows = [['JCP', 100, None, 1000, '=B2/D2', .3, 'RCA 01/03/2026', datetime(2026, 3, 2), datetime(2026, 4, 13)]]
    result = operating.parse_workbook(workbook(rows), 'TAEE3', AS_OF, source('TAEE3', 'workbook'))
    event = result['events'][0]
    assert event['reported_per_share'] is None and 'formula_cache_unavailable' in event['issues']
    assert event['status'] == 'invalid'


def test_alupar_only_explicit_merged_cells_supply_shared_dates():
    rows = [[2026, 'RCA', datetime(2026, 5, 7), datetime(2026, 5, 14), datetime(2026, 5, 15), datetime(2026, 7, 6), .07, .21],
            [None, None, None, None, None, datetime(2026, 8, 6), .08, .24]]
    merged = workbook(rows, ticker='ALUP3', merges=['C2:C3', 'D2:D3', 'E2:E3'])
    result = operating.parse_workbook(merged, 'ALUP3', AS_OF, source('ALUP3', 'workbook'))
    assert len(result['events']) == 2
    assert all(e['approval_date'] == '2026-05-07' for e in result['events'])
    assert result['events'][1]['reported_per_share'] == .08
    unmerged = operating.parse_workbook(workbook(rows, ticker='ALUP3'), 'ALUP3', AS_OF, source('ALUP3', 'workbook'))
    assert unmerged['events'][1]['approval_date'] is None


def test_alupar_bad_source_year_and_deadline_are_not_silently_corrected():
    rows = [[2026, 'RCA', datetime(2025, 12, 18), datetime(2025, 12, 26), datetime(2025, 12, 29), datetime(2025, 2, 16), .11, .33],
            [2026, 'RCA', datetime(2026, 8, 6), datetime(2026, 8, 13), datetime(2026, 8, 14), 'Ate 60 dias da aprovacao', .07, .21]]
    first, deadline = operating.parse_workbook(workbook(rows, ticker='ALUP3'), 'ALUP3', AS_OF, source('ALUP3', 'workbook'))['events']
    assert first['payment_date'] == '2025-02-16' and first['status'] == 'invalid'
    assert 'payment_before_approval_or_record' in first['issues']
    assert deadline['payment_date'] is None and deadline['status'] == 'invalid'


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '-1', '1e999', 'not a number'])
def test_invalid_numbers_remain_null_and_output_is_strict_json(bad):
    result = operating.parse_native_history(agro(['Dividendos', '01/03/2026', '13/04/2026', '100', bad]),
                                           'AGRO3', AS_OF, source('AGRO3'))
    assert result['events'][0]['reported_per_share'] is None
    json.dumps(result, allow_nan=False)


def test_acquisition_is_bounded_content_addressed_and_cache_reuse_is_offline(tmp_path, monkeypatch):
    body = agro(['Dividendos', '01/03/2026', '13/04/2026', '100', '0,1'])
    calls = []

    class Response:
        headers = {'Content-Type': 'text/html; charset=UTF-8'}

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def geturl(self): return operating.ISSUERS['AGRO3']['landing']
        def read(self, size):
            assert size == operating.MAX_BYTES + 1
            return body

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            assert timeout == 25
            return Response()

    monkeypatch.setattr(operating.urllib.request, 'build_opener', lambda *args: Opener())
    content, metadata = operating.acquire_source(tmp_path, 'AGRO3', 'landing')
    assert content == body and metadata['sha256'] == hashlib.sha256(body).hexdigest()
    assert metadata['file'].startswith('raw/primary-research/operating/')
    assert operating.acquire_source(tmp_path, 'AGRO3', 'landing') == (content, metadata)
    assert len(calls) == 1 and not (tmp_path / 'current.json').exists()


def test_exchange_endpoints_and_cross_host_redirects_are_never_allowed():
    for url in ['https://www.b3.com.br/', 'https://sistemaswebb3-listados.b3.com.br/',
                'http://ri.b3.com.br/pt-br/proventos/', 'https://ri.b3.com.br:444/']:
        assert not operating._approved(url)
    request = Request(operating.ISSUERS['B3SA3']['landing'])
    with pytest.raises(ValueError, match='unapproved_redirect'):
        operating._IssuerRedirect().redirect_request(request, None, 302, '', {}, 'https://sistemaswebb3-listados.b3.com.br/')


def test_blocked_landing_does_not_try_attachment_or_any_access_bypass(tmp_path, monkeypatch):
    calls = []

    def blocked(folder, ticker, kind, **kwargs):
        calls.append((ticker, kind))
        raise HTTPError(operating.ISSUERS[ticker][kind], 403, 'Forbidden', {}, None)

    monkeypatch.setattr(operating, 'acquire_source', blocked)
    result = operating.collect_operating_sources(tmp_path, AS_OF, tickers=['TAEE3'])
    assert calls == [('TAEE3', 'landing')]
    assert result['instruments']['TAEE3']['status'] == 'unavailable'
    assert all(a['status'] == 'unavailable' for a in result['attempts'])
    assert (tmp_path / result['report_file']).is_file()


def test_empty_table_is_not_confirmed_zero_or_complete_history():
    result = operating.parse_native_history(agro(), 'AGRO3', AS_OF, source('AGRO3'))
    assert result['status'] == 'not_found' and result['events'] == []
    assert result['coverage']['first_observed_payment'] is None
    assert result['history_complete'] is False and result['cash_model_eligible'] is False
