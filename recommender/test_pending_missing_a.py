import hashlib
import pytest

from .pending_missing_a import exact_date, native_decimal, parse_history, parse_native_history


IDENTITY = {'ticker': 'TEST3', 'cvm': 100, 'share_class': 'ON', 'isin': 'BRTESTACNOR0'}


def parse(source):
    body = source.encode('utf-8')
    receipt = {'url': 'https://issuer.example/dividendos/', 'file': 'raw/test.html',
               'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': '2026-10-25T23:00:00Z'}
    return parse_history(body, receipt, IDENTITY)


def test_native_decimals_and_exact_dates_do_not_turn_deadlines_into_payment_dates():
    assert native_decimal('R$ 0,10840223172') == '0.10840223172'
    assert native_decimal('R$ 1.234.567,89') == '1234567.89'
    assert native_decimal('R$ 50.000.000.00') is None
    assert exact_date('29/02/2026') is None
    assert exact_date('Ate 15/12/2028') is None
    assert exact_date('15/12/2025') == '2025-12-15'


def test_combined_jcp_dividends_stay_unsegregated_and_class_unresolved():
    rows = parse('''<table><tr><th>Proventos</th><th>Data EX</th>
        <th>Valor (R$/acao)</th><th>Data Pagamento</th></tr>
        <tr><td>Dividendos + JCP</td><td>18/12/2024</td><td>0,64384496</td>
        <td>07/01/2025</td></tr></table>''')
    assert len(rows) == 1
    row = rows[0]
    assert row['event_type'] == 'DIVIDENDO_E_JCP_NAO_SEGREGADOS'
    assert row['ex_date'] == '2024-12-18'
    assert row['record_date'] is None
    assert row['source_class'] is None
    assert row['exact_class_confirmed'] is False
    assert row['gross_basis'] == 'not_explicit_for_per_share_column'
    assert row['model_admission'] is False


def test_explicit_rowspans_preserve_native_table_semantics_without_filling_gaps():
    rows = parse('''<table><tr><th>Tipo</th><th>Data Base</th>
        <th>Data Pagamento</th><th>Ordinaria TEST3 (R$/acao)</th></tr>
        <tr><td rowspan="2">Dividendo</td><td>02/01/2026</td>
        <td>Ate 31/12/2028</td><td>0,35</td></tr>
        <tr><td>03/08/2026</td><td>17/08/2026</td><td>0,35</td></tr>
        <tr><td>Dividendo</td><td>03/08/2026</td><td>17/08/2026</td></tr></table>''')
    assert len(rows) == 2
    assert rows[0]['payment_date'] is None
    assert rows[0]['payment_literal'] == 'Ate 31/12/2028'
    assert rows[1]['explicit_rowspan_inherited_columns'] == [0]
    assert rows[1]['exact_class_confirmed'] is True
    assert rows[1]['amount_decimal'] == '0.35'


def test_duplicate_rows_remain_visible_for_reconciliation():
    rows = parse('''<table><tr><th>Provento</th><th>Data Pagamento</th>
        <th>Valor por acao</th></tr><tr><td>Dividendo</td><td>13/03/2026</td>
        <td>0,59</td></tr><tr><td>Dividendo</td><td>13/03/2026</td><td>0,59</td>
        </tr></table>''')
    assert len(rows) == 2
    assert [row['source_duplicate_prior_count'] for row in rows] == [0, 1]
    assert all(row['published_at'] is None for row in rows)


def test_utf8_portuguese_headers_and_amounts_are_read_without_mojibake():
    rows = parse('''<table><tr><th>APROVA\u00c7\u00c3O</th><th>DATA DO PAGAMENTO</th>
        <th>VALOR POR A\u00c7\u00c3O (BRL)</th><th>FORMA DE DISTRIBUI\u00c7\u00c3O</th></tr>
        <tr><td>20/09/2021</td><td>05/10/2021</td><td>0,105915328</td>
        <td>JCP</td></tr></table>''')
    assert len(rows) == 1
    assert rows[0]['approval_date'] == '2021-09-20'
    assert rows[0]['event_type'] == 'JCP'
    assert rows[0]['amount_decimal'] == '0.105915328'


def test_reported_net_per_share_is_never_labelled_gross():
    rows = parse('''<table><tr><th>Tipo</th><th>Data Pagamento</th>
        <th>Valor bruto por acao</th><th>Valor liquido por acao</th></tr>
        <tr><td>JCP</td><td>30/11/2026</td><td>0,0786736950</td><td>0,0649057980</td>
        </tr></table>''')
    assert [row['gross_basis'] for row in rows] == ['explicit_gross', 'explicit_net']
    assert rows[1]['amount_decimal'] == '0.0649057980'


def test_price_adapter_rejects_adjusted_or_unproven_source_before_parsing():
    body = b'untrusted non-price content'
    receipt = {'sha256': hashlib.sha256(body).hexdigest(),
               'discovery': {'ticker': 'TEST3', 'adjusted': True}}
    with pytest.raises(ValueError, match='adjustment_proof'):
        parse_native_history(body, receipt, IDENTITY)
