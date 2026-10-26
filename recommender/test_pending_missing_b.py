import hashlib
import json
from decimal import Decimal

import pytest

from .pending_missing_b import (
    exact_date, inspect_html, native_brl, parse_supported_table,
    parse_altona_declarations, parse_widget_history, table_grid, document_tree, widget_request,
)


def identity(ticker='ARML3', cvm=26069, share_class='ON', isin='BRARMLACNOR1'):
    return {'ticker': ticker, 'cvm': cvm, 'share_class': share_class, 'isin': isin}


def receipt(url, body=b''):
    return {'url': url, 'file': 'raw/source.bin', 'sha256': hashlib.sha256(body).hexdigest(),
            'bytes': len(body), 'retrieved_at': '2026-10-26T00:00:00+00:00'}


def test_native_brl_never_scales_or_rounds():
    assert native_brl('R$ 0,06726351290') == Decimal('0.06726351290')
    assert native_brl('1.234,56789') == Decimal('1234.56789')
    for value in ('0.20422', '-', 'a definir', '0,18 bruto', '1,2,3'):
        with pytest.raises(ValueError):
            native_brl(value)


def test_no_payment_date_derived_from_month_or_ex_date():
    for value in ('Dez/26', 'A ser definida', '31/02/2026', '2026-12-31'):
        with pytest.raises(ValueError):
            exact_date(value)


def test_utf8_and_base_href_preserve_semantic_citation():
    body = '<html><head><base href="https://www.altona.com.br/ri/"></head><body><a href="br/dividendos/">Dividendo a\u00e7\u00e3o</a></body></html>'.encode()
    parsed = inspect_html(body, receipt('https://www.altona.com.br/ri/br/paginas/avisos-aos-acionistas/', body))
    assert parsed['links'][0]['url'] == 'https://www.altona.com.br/ri/br/dividendos/'
    assert parsed['links'][0]['text'] == 'Dividendo a\u00e7\u00e3o'


def test_spans_expand_only_explicit_source_cells():
    tree = document_tree(b'<table><tr><td rowspan="2">2025</td><td>JCP</td></tr><tr><td>Dividendos</td></tr><tr><td></td><td>JCP</td></tr></table>')
    rows = table_grid(tree)
    assert rows[1][0] == ['2025', 'Dividendos']
    assert rows[2][0] == ['', 'JCP']


def test_armac_keeps_unknown_payments_jcp_and_native_amount():
    body = b'<table><tr><th>Data de Pagamento</th><th>Periodo</th><th>Data Base</th><th>Data Ex</th><th>Tipo</th><th>Valor por Acao</th><th>Valor Total</th></tr><tr><td>A ser definida</td><td>4T25</td><td>08/01/2026</td><td>09/01/2026</td><td>JSCP</td><td>0,1009621821</td><td>34.955.260,85</td></tr></table>'
    r = receipt('https://ri.armac.com.br/informacoes-financeiras/dividendos-e-jcp-2/', body)
    rows = parse_supported_table(body, r, identity())
    assert len(rows) == 1
    event = rows[0]
    assert event.payment_date is None
    assert event.ex_date == '2026-01-09'
    assert event.event_type == 'interest_on_equity'
    assert event.amount_per_share == '0.1009621821'
    assert event.amount_basis == 'as_reported_gross_net_unspecified'
    assert event.published_at is None
    assert not event.model_admission
    with pytest.raises(ValueError, match='exact_manifest_identity'):
        parse_supported_table(body, r, identity(share_class='PN'))


def test_brava_does_not_import_enauta_rows():
    header = '<tr><th>Proventos</th><th>Exercicio</th><th>Data Aprovacao</th><th>Data Ex</th><th>Data Pagamento</th><th>Montante</th><th>Valor por Acao</th></tr>'
    cells = '<tr><td>Dividendos</td><td>2023</td><td>30/04/2024</td><td>02/05/2024</td><td>01/07/2024</td><td>92.565.465,09</td><td>0,385001179</td></tr>'
    body = f'<html><body><h2>Brava Energia</h2><table>{header}{cells}</table><h2>Enauta</h2><table>{header}{cells}</table></body></html>'.encode()
    r = receipt('https://ri.bravaenergia.com/informacoes-aos-acionistas/proventos/', body)
    rows = parse_supported_table(body, r, identity('BRAV3', 25291, 'ON', 'BRBRAVACNOR3'))
    assert len(rows) == 1


def history_fixture():
    observations = [{'date': f'{year}-{month:02d}-20', 'closed': 6.7}
                    for year in range(2021, 2027) for month in range(1, 13)
                    if '2021-09' <= f'{year}-{month:02d}' <= '2026-09']
    payload = {'success': True, 'data': {'tickers': [{'ticker': 'ARML3', 'history': observations}],
                                       'errors': [], 'source': 'Enfoque'}}
    return payload


def test_history_exactly_61_actual_months_no_imputation():
    payload = history_fixture()
    r = receipt('https://api.mziq.com/mzstockinfo/id/history?from=2021-09-01&to=2026-09-30&tickers=ARML3&adjusted=false')
    parsed = parse_widget_history(json.dumps(payload).encode(), r, identity())
    assert parsed['has_61_consecutive_months']
    assert parsed['monthly_observations'] == 61
    assert parsed['monthly_prices'][0]['close'] == 6.7
    assert parsed['monthly_prices'][0]['date'] == '2021-09-20'
    payload['data']['tickers'][0]['history'].pop(0)
    parsed = parse_widget_history(json.dumps(payload).encode(), r, identity())
    assert not parsed['has_61_consecutive_months']
    assert parsed['missing_months'] == ['2021-09']


def test_index_error_is_retained_and_requested_ticker_error_is_fatal():
    payload = history_fixture()
    payload['data']['errors'] = [{'ticker': 'SMAL11', 'status': 'Invalid index / not configured'}]
    r = receipt('https://api.mziq.com/mzstockinfo/id/history?tickers=ARML3&adjusted=false')
    parsed = parse_widget_history(json.dumps(payload).encode(), r, identity())
    assert parsed['has_61_consecutive_months']
    assert parsed['unrelated_index_configuration_errors_retained'] == payload['data']['errors']
    payload['data']['errors'][0]['ticker'] = 'ARML3'
    with pytest.raises(ValueError, match='did_not_return_success'):
        parse_widget_history(json.dumps(payload).encode(), r, identity())


def test_request_does_not_accept_a_suffix_ticker():
    r = receipt('https://api.mziq.com/mzstockinfo/id/history?tickers=ARML3F&adjusted=false')
    with pytest.raises(ValueError, match='identity_or_adjustment'):
        parse_widget_history(json.dumps(history_fixture()).encode(), r, identity())


def test_altona_never_divides_aggregate_into_equal_installments_or_guesses_PNA():
    body = b'<html><div class="tabela"><div class="menu-tabela"><ul><li>Classe</li><li>Quantidade</li><li>Valor Proposto</li><li>Dividendos</li></ul></div><div class="itens-tabela"><ul><li>Preferenciais</li><li>12.750.000</li><li>0,2082018927444800</li><li>2.654.574,13</li></ul></div><div class="itens-tabela"><ul><li>Aprovado AGOE 29/04/2026 | Pagamento 1: 12/06/2026 | Pagamento 2: 28/08/2026 | Pagamento 3: 30/10/2026 | Ex: 30/04/2026 - As parcelas nao seguiram a proporcao de 1/3 por pagamento</li></ul></div></div></html>'
    rows = parse_altona_declarations(body, receipt('https://www.altona.com.br/ri/br/dividendos/', body),
                                     identity('EALT4', 5380, 'PNA', 'BREALTACNPR1'))
    assert len(rows) == 1
    assert rows[0]['native_proposed_value'] == '0.2082018927444800'
    assert all(row['amount_per_share'] is None for row in rows[0]['installments'])
    assert rows[0]['preferred_PNA_binding_not_inferred']
    assert not rows[0]['source_class_matches_registered_class']


@pytest.mark.parametrize('bad', [0, -1, True, None, '6.7'])
def test_history_rejects_non_native_or_unusable_prices(bad):
    payload = history_fixture()
    payload['data']['tickers'][0]['history'][0]['closed'] = bad
    r = receipt('https://api.mziq.com/mzstockinfo/id/history?tickers=ARML3&adjusted=false')
    with pytest.raises(ValueError, match='positive_native_number'):
        parse_widget_history(json.dumps(payload).encode(), r, identity())


def test_widget_requires_issuer_script_exact_id_and_literal_ticker():
    script = b'https://api.mziq.com/mzstockinfo /history?from= &to= &tickers= &adjusted= YYYY-MM-DD'
    page = b'<html><body><script src="https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js"></script><script>var stockInfoId="b971d9f6-a76f-11eb-bb74-12333d57aab9";var options={tickers:"ARML3",template:{type:"history"}};</script></body></html>'
    r = receipt('https://ri.armac.com.br/servicos-aos-investidores/cotacoes-e-graficos/', page)
    url, proof = widget_request(page, r, script, identity())
    assert 'b971d9f6-a76f-11eb-bb74-12333d57aab9/history?' in url
    assert url.endswith('tickers=ARML3&adjusted=false')
    assert proof['identity']['isin'] == 'BRARMLACNOR1'
    with pytest.raises(ValueError, match='exact_ticker_not_literal'):
        widget_request(page, r, script, identity('MTRE3', 24902))
