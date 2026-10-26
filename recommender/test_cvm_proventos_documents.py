from datetime import date
from decimal import Decimal
import csv
import hashlib
import io
import json
import urllib.request
import zipfile

from pypdf import PdfWriter
import pytest

from . import cvm_proventos_documents as module
from .cvm_proventos_documents import _OfficialRedirect, discover, document_id, gross, installment_rows, parse_pdf, parse_tables


DOC = {'cvm': 21610, 'issuer_name': 'B3 S.A. - BRASIL, BOLSA, BALCAO', 'published_at': '2026-09-22',
       'url': 'https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE&CodigoInstituicao=1&numProtocolo=1570568&numSequencia=1095274&numVersao=3',
       'sequence': '1095274', 'version': 3}


def tables(*, protocol='021610IPE2209202601', amount='0,0759467104\n0', payment='07/10/2026', isin='BRB3SAACN\nOR6'):
    return [[['Denominacao Social', 'Protocolo IPE', None], [DOC['issuer_name'], protocol, None],
             ['Data Aprovacao', 'Ultimo dia de negociacao com Direitos'], ['17/09/2026', '22/09/2026'],
             ['Protocolo Provento', 'Versao', 'Data Envio'], ['1570486', '3', '22/09/2026']],
            [['Codigo ISIN', 'Valor Bruto (R$/Unidade)', 'Periodo Base', 'Exercicio Social', 'Havera Atualizacao', 'Forma de Pagamento', 'Data Pagamento'],
             [isin, amount, '3 Trimestre', '2026', 'Nao', 'A Vista', payment]]]


def test_wrapped_decimal_and_isin_keep_every_reported_digit():
    result = parse_tables(tables(), DOC, date(2026, 10, 24))
    assert result['status'] == 'available'
    event = result['events'][0]
    assert event['isin'] == 'BRB3SAACNOR6'
    assert Decimal(event['gross_per_unit_decimal']) == Decimal('0.07594671040')
    assert event['gross_per_unit'] == .0759467104
    assert event['gross_per_share'] is None
    assert event['record_date'] == '2026-09-22'
    assert event['published_at'] == '2026-09-22'
    assert event['payment_date'] == '2026-10-07'
    assert event['status'] == 'past_scheduled'
    assert event['label'] is None
    assert event['financially_confirmed'] is event['cash_model_eligible'] is False


def test_future_schedule_is_not_a_payment_or_an_ex_date():
    event = parse_tables(tables(), DOC, date(2026, 9, 30))['events'][0]
    assert event['status'] == 'announced'
    assert event['record_date'] != event['payment_date']
    assert event['cash_model_eligible'] is False


def test_future_publication_is_retained_but_not_available_as_of():
    event = parse_tables(tables(), DOC, date(2026, 9, 20))['events'][0]
    assert event['available_as_of'] is False


@pytest.mark.parametrize('amount', ['-0,10', 'NaN', '1.05', '0,4oops', ''])
def test_invalid_gross_amounts_are_not_zero(amount):
    result = parse_tables(tables(amount=amount), DOC, date(2026, 10, 24))
    assert not result['events']
    assert 'invalid_gross_per_unit' in result['issues']


def test_exact_declared_zero_is_not_missing():
    assert gross('0,000000') == Decimal(0)


def test_other_issuer_is_rejected_even_when_name_matches():
    result = parse_tables(tables(protocol='000001IPE2026'), DOC, date(2026, 10, 24))
    assert result['status'] == 'invalid' and not result['events']


def test_missing_protocol_requires_exact_primary_index_name():
    assert parse_tables(tables(protocol='Not reported'), DOC, date(2026, 10, 24))['events']
    result = parse_tables(tables(protocol='Not reported'), {**DOC, 'issuer_name': 'Different issuer'}, date(2026, 10, 24))
    assert result['status'] == 'invalid'


def test_installments_without_a_resolved_date_stay_unresolved():
    result = parse_tables(tables(payment='Ver parcelas'), DOC, date(2026, 10, 24))
    assert result['events'][0]['payment_date'] is None
    assert result['events'][0]['cash_model_eligible'] is False
    assert 'payment_date_or_installment_mapping_unresolved' in result['issues']


def test_multiple_payment_dates_are_not_truncated_into_one_instalment():
    result = parse_tables(tables(payment='07/10/2026; 07/11/2026'), DOC, date(2026, 10, 24))
    assert result['events'][0]['payment_date'] is None
    assert result['events'][0]['issues'] == ['payment_date_or_installment_mapping_unresolved']


def test_native_annex_keeps_decimal_dots_without_guessing_a_share_class():
    rows = installment_rows([[['Dia de Pagamento das Parcelas', 'Valor Bruto (R$/Unidade)'],
        ['30/06/2027', '0.96195081900'], ['30/12/2027', '1,05814590100'], ['A definir', '1.234']]])
    assert rows[0]['gross_per_unit_decimal'] == '0.96195081900'
    assert rows[1]['gross_per_unit_decimal'] == '1.05814590100'
    assert rows[2]['gross_per_unit_decimal'] is rows[2]['payment_date'] is None
    assert all(row['isin'] is None and row['cash_model_eligible'] is False for row in rows)


def test_pdf_content_is_validated_independently_from_http_mime():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    result = parse_pdf(buffer.getvalue(), DOC, date(2026, 10, 24))
    assert result['pages'] == 1
    assert result['status'] == 'invalid' and not result['events']
    with pytest.raises(ValueError, match='not_a_pdf'):
        parse_pdf(b'<html>Not a PDF even if the header claims application/pdf</html>', DOC, date(2026, 10, 24))


@pytest.mark.parametrize('url', [DOC['url'].replace('www.rad.cvm.gov.br', 'untrusted.example'),
    DOC['url'] + '&numVersao=4', DOC['url'].replace('numSequencia=1095274', 'numSequencia=../secret'),
    DOC['url'].replace('descTipo=IPE', 'descTipo=FRE')])
def test_untrusted_or_ambiguous_document_urls_are_rejected(url):
    with pytest.raises(ValueError):
        document_id(url)


def test_public_document_identity():
    assert document_id(DOC['url']) == ('1095274', 3)


def index_fixture(tmp_path, rows):
    text = io.StringIO()
    columns = ['Categoria', 'Codigo_CVM', 'Nome_Companhia', 'Data_Entrega', 'Link_Download', 'Data_Referencia']
    writer = csv.DictWriter(text, columns, delimiter=';')
    writer.writeheader()
    writer.writerows(rows)
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('ipe_cia_aberta_2026.csv', text.getvalue().encode('latin1'))
    path = tmp_path / 'index.zip'
    path.write_bytes(content.getvalue())
    return {'provider': 'CVM IPE', 'file': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'url': 'https://dados.cvm.gov.br/index.zip'}


def test_discovery_uses_issuer_publication_and_exact_document_identity(tmp_path):
    row = {'Categoria': 'Relatorio Proventos', 'Codigo_CVM': '21610',
        'Nome_Companhia': DOC['issuer_name'], 'Data_Entrega': '2026-09-22',
        'Link_Download': DOC['url'], 'Data_Referencia': '2026-09-17'}
    source = index_fixture(tmp_path, [row, row, {**row, 'Codigo_CVM': '999'},
        {**row, 'Data_Entrega': '2027-01-01'}, {**row, 'Categoria': 'Aviso aos Acionistas'}])
    result = discover(tmp_path, {'stocks': [{'cvm': 21610}], 'sources': [source]}, date(2026, 10, 24))
    assert len(result) == 1 and result[0]['published_at'] == '2026-09-22'
    assert result[0]['reference_date'] == '2026-09-17'
    (tmp_path / source['file']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash_mismatch'):
        discover(tmp_path, {'stocks': [{'cvm': 21610}], 'sources': [source]}, date(2026, 10, 24))


def test_official_redirect_handler_rejects_cross_host_before_sending_request():
    request = urllib.request.Request(DOC['url'])
    with pytest.raises(ValueError, match='unexpected_document_redirect'):
        _OfficialRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://other.example/file.pdf')


def test_verified_cached_pdf_reparses_without_any_network_request(tmp_path, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    content = io.BytesIO()
    writer.write(content)
    cache = tmp_path / 'raw/primary-research/cvm-documents'
    cache.mkdir(parents=True)
    filename = '1095274-v3-' + hashlib.sha256(content.getvalue()).hexdigest() + '.pdf'
    (cache / filename).write_bytes(content.getvalue())
    monkeypatch.setattr(module, 'load_inventory', lambda folder: {})
    monkeypatch.setattr(module, 'discover', lambda *args: [DOC])
    monkeypatch.setattr(module, '_open', lambda *args, **kwargs: pytest.fail('cache must not use network'))
    result = module.collect(tmp_path, date(2026, 10, 24))
    assert result['network_requests'] == 0
    assert result['valid_documents'] == 1
    assert result['results'][0]['acquisition']['status'] == 'cached'
    assert (tmp_path / result['report_file']).is_file()
    assert result['active_model_changed'] is False


def test_cache_hash_mismatch_is_not_silently_replaced_by_network(tmp_path, monkeypatch):
    cache = tmp_path / 'raw/primary-research/cvm-documents'
    cache.mkdir(parents=True)
    (cache / ('1095274-v3-' + 'a' * 64 + '.pdf')).write_bytes(b'changed')
    monkeypatch.setattr(module, 'load_inventory', lambda folder: {})
    monkeypatch.setattr(module, 'discover', lambda *args: [DOC])
    monkeypatch.setattr(module, '_open', lambda *args, **kwargs: pytest.fail('tampering must fail closed'))
    result = module.collect(tmp_path, date(2026, 10, 24))
    assert result['network_requests'] == 0
    assert result['results'][0]['error_code'] == 'cache_hash_mismatch'
