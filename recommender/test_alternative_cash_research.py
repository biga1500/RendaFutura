from copy import deepcopy
import csv
import hashlib
import io
import json
from pathlib import Path
import socket
import urllib.error
import urllib.request
import zipfile

import pytest

from . import alternative_cash_research as research


STOCK = {'ticker': 'ASAI3', 'cvm': 25372, 'isin': 'BRASAIACNOR0',
         'share_class': 'ON', 'cnpj': '06057223000171'}
URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
       '&CodigoInstituicao=1&numProtocolo=123&numSequencia=123&numVersao=1')
DOC = {'ticker': 'ASAI3', 'cvm': 25372, 'issuer_name': 'SENDAS DISTRIBUIDORA S.A.',
       'sequence': '123', 'version': 1, 'published_at': '2023-01-16',
       'url': URL, 'index_evidence': {'sha256': 'a' * 64}}
SOURCE = {'file': 'raw/original.pdf', 'sha256': 'b' * 64}


def tables(*, legacy=False, payment='17/02/2023', amount=None):
    return [[['Denominacao Social', 'Protocolo IPE', None],
             ['SENDAS DISTRIBUIDORA S.A.', '025372IPE231220220104491811-26', None],
             ['Data Aprovacao', 'Ultimo dia de negociacao com Direitos', None],
             ['12/23/2022 12:00:00 AM' if legacy else '23/12/2022',
              '12/28/2022 12:00:00 AM' if legacy else '28/12/2022', None],
             ['Protocolo Provento', 'Versao', 'Data Envio'],
             ['1044274', '1', '1/13/2023 8:24:22 PM' if legacy else '13/01/2023']],
            [['Codigo ISIN', 'Valor Bruto (R$/Unidade)', 'Periodo Base', 'Exercicio Social',
              'Havera Atualizacao', 'Forma de Pagamento', 'Data Pagamento'],
             ['BRASAIACN\nOR0', amount or ('0.0370599484\n9' if legacy else '0,0370599484\n9'),
              'Anual', '2022', 'Nao', 'A Vista', payment]],
            [['Dia de Pagamento das Parcelas', 'Valor Bruto (R$/Unidade)']]]


def native(**kwargs):
    return research.parse_native_tables(tables(**kwargs), DOC, SOURCE, STOCK)[0][0]


def review():
    fields = {
        'record_date': {'value': '2022-12-28', 'quote': 'posicao acionaria de 28 de dezembro de 2022', 'page': 1},
        'payment_date': {'value': '2023-02-17', 'quote': 'pagamento sera realizado em 17 de fevereiro de 2023', 'page': 1},
        'gross_per_share_decimal': {'value': '0.03705994849', 'quote': 'valor bruto de R$ 0,03705994849 por acao ordinaria', 'page': 1},
        'share_class': {'value': 'ON', 'quote': 'por acao ordinaria', 'page': 1},
        'label': {'value': 'JCP', 'quote': 'pagamento de juros sobre capital proprio', 'page': 1}}
    text = 'CNPJ 06.057.223/0001-71. ' + '. '.join(p['quote'] for p in fields.values())
    return [text], {'ticker': 'ASAI3', 'sequence': '123', 'sha256': SOURCE['sha256'], 'fields': fields}


def test_native_legacy_US_timestamps_and_exact_decimal_are_explicit():
    event = native(legacy=True)
    assert event['record_date'] == '2022-12-28'
    assert event['approval_date'] == '2022-12-23'
    assert event['gross_per_unit_decimal'] == '0.03705994849'
    assert event['gross_per_share_decimal'] is event['label'] is None
    assert event['status'] == 'past_scheduled'
    assert event['source_locator']['legacy_explicit_AM_PM_date_dialect'] is True
    assert event['cash_model_eligible'] is event['is_settlement_confirmation'] is False


def test_standard_and_legacy_AM_PM_dates_do_not_guess_locale():
    assert research._native_day('03/04/2023') == '2023-04-03'
    assert research._native_day('03/04/2023 12:00:00 AM') == '2023-03-04'
    assert research._native_day('PAGO') is None
    assert research._native_day('ate 31/12/2023') is None


@pytest.mark.parametrize('value', ['0.037', '1.000', '0.03705994849'])
def test_dot_amount_without_explicit_legacy_dialect_is_rejected(value):
    with pytest.raises(ValueError, match='dialect_ambiguous'):
        native(amount=value)


def test_deadline_and_paid_label_do_not_produce_payment_or_settlement():
    for text in ('ate 31/12/2023', 'PAGO', ''):
        event = native(payment=text)
        assert event['payment_date'] is None
        assert event['status'] == 'date_unresolved'
        assert event['settlement_verified'] is False
    event = research._observation(DOC, SOURCE, STOCK, 'test', {})
    assert event['gross_per_share_decimal'] is None
    assert event['payment_date'] is None
    assert event['history_complete'] is False


@pytest.mark.parametrize('mutation', ['isin', 'cvm', 'version', 'publication'])
def test_native_wrong_security_version_or_time_is_rejected(mutation):
    data, doc = tables(), deepcopy(DOC)
    if mutation == 'isin':
        data[1][1][0] = 'BRASAIACNPR9'
    elif mutation == 'cvm':
        data[0][1][1] = '025658IPE231220220104491811-26'
    elif mutation == 'version':
        data[0][-1][1] = '2'
    else:
        doc['published_at'] = '2023-01-01'
    with pytest.raises(ValueError):
        research.parse_native_tables(data, doc, SOURCE, STOCK)


def test_native_installment_rows_remain_unmapped():
    data = tables(payment='')
    data[-1].append(['17/02/2023', '0.01000000000'])
    events, installments = research.parse_native_tables(data, DOC, SOURCE, STOCK)
    assert events[0]['payment_date'] is None
    assert installments[0]['isin'] is None
    assert installments[0]['mapping_status'] == 'unresolved_no_explicit_ISIN_column'


def test_review_preserves_absent_ISIN_and_never_promotes_paid():
    pages, row = review()
    event = research.parse_reviewed_notice(pages, row, DOC, SOURCE, STOCK)
    assert event['isin'] is None
    assert event['identity_mapping_candidate']['isin'] == STOCK['isin']
    assert event['share_class'] == 'ON'
    assert event['gross_per_share_decimal'] == '0.03705994849'
    assert event['is_settlement_confirmation'] is event['cash_model_eligible'] is False


@pytest.mark.parametrize('mutation', ['hash', 'quote', 'date', 'amount', 'CNPJ', 'status'])
def test_review_mismatches_are_rejected(mutation):
    pages, row = review()
    if mutation == 'hash':
        row['sha256'] = 'c' * 64
    elif mutation == 'quote':
        row['fields']['payment_date']['quote'] = 'invented payment'
    elif mutation == 'date':
        row['fields']['payment_date']['value'] = '2023-02-18'
    elif mutation == 'amount':
        row['fields']['gross_per_share_decimal']['value'] = '0.1'
    elif mutation == 'CNPJ':
        pages[0] = pages[0].replace('06.057.223/0001-71', '21.240.146/0001-84')
    else:
        row['fields']['status'] = {'value': 'PAGO', 'quote': 'pagamento', 'page': 1}
    with pytest.raises(ValueError):
        research.parse_reviewed_notice(pages, row, DOC, SOURCE, STOCK)


@pytest.mark.parametrize('quote', [
    'ex-dividendos em 17 de fevereiro de 2023',
    'pagamento ate o dia 17 de fevereiro de 2023',
    '17 de fevereiro de 2023'])
def test_review_ex_date_or_deadline_is_not_payment(quote):
    pages, row = review()
    row['fields']['payment_date']['quote'] = quote
    pages[0] += '. ' + quote
    with pytest.raises(ValueError, match='payment_context'):
        research.parse_reviewed_notice(pages, row, DOC, SOURCE, STOCK)


def test_explicit_net_amount_cannot_become_gross():
    pages, row = review()
    quote = 'valor liquido de R$ 0,03705994849 por acao ordinaria'
    row['fields']['gross_per_share_decimal']['quote'] = quote
    pages[0] += '. ' + quote
    with pytest.raises(ValueError, match='gross_not_explicit'):
        research.parse_reviewed_notice(pages, row, DOC, SOURCE, STOCK)


def test_exact_amounts_equal_at_different_scales_do_not_conflict():
    one, two = native(), native()
    two['gross_per_unit_decimal'] += '00'
    assert research.annotate_conflicts([one, two]) == []


def test_native_revision_does_not_hide_a_conflicting_issuer_notice():
    old, latest = native(payment='31/12/2023'), native(payment='17/02/2023')
    latest['document_version'] = 2
    latest['id'] = 'latest'
    pages, row = review()
    notice = research.parse_reviewed_notice(pages, row, DOC, SOURCE, STOCK)
    notice['payment_date'], notice['id'] = '2023-02-21', 'notice'
    conflicts = research.annotate_conflicts([old, latest, notice])
    assert old['superseded_native_version'] is True
    assert latest['superseded_native_version'] is False
    conflict = next(row for row in conflicts if row['code'] == 'primary_payment_date_conflict')
    assert conflict['values'] == ['2023-02-17', '2023-02-21']
    assert old['id'] not in conflict['event_ids']


@pytest.mark.parametrize('url', [
    'https://www.b3.com.br/robots.txt', 'https://sistemaswebb3-listados.b3.com.br/data',
    'https://api.brapi.dev/quote/ASAI3', 'https://proxy.example/?url=https://www.b3.com.br',
    URL.replace('www.rad.cvm.gov.br', 'www.rad.cvm.gov.br.evil.example'),
    URL.replace('https://', 'https://user@'), URL.replace('https://', 'http://'),
    URL + '&proxy=https://www.b3.com.br'])
def test_only_exact_direct_CVM_urls_are_approved(url):
    assert research._approved(url) is False


def test_network_budget_includes_existing_attempts_and_cannot_reset(tmp_path):
    for index in range(20):
        research._save_json(tmp_path, 'request', {'url': URL + str(index), 'ordinal': index + 1})
    fetcher = research.BoundedCVMFetcher(tmp_path)
    with pytest.raises(ValueError, match='budget_exhausted'):
        fetcher._get(URL)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_denial_stops_all_later_CVM_requests_and_no_retry(tmp_path, status):
    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(request.full_url, status, 'denied', {}, io.BytesIO(b'denied'))

    fetcher = research.BoundedCVMFetcher(tmp_path)
    fetcher.opener = opener = Opener()
    _, receipt = fetcher._get(URL)
    assert receipt['http_status'] == status
    assert len(fetcher.requests) == 1
    with pytest.raises(ValueError, match='access_stop'):
        fetcher._get(URL.replace('numSequencia=123', 'numSequencia=124'))
    assert opener.calls == 1
    assert research.BoundedCVMFetcher(tmp_path).responses[0]['http_status'] == status


def test_robots_disallow_prevents_document_request(tmp_path, monkeypatch):
    fetcher = research.BoundedCVMFetcher(tmp_path)
    calls = []

    def get(url):
        calls.append(url)
        return b'User-agent: *\nDisallow: /ENET/\n', {'http_status': 200, 'status': 'received'}

    monkeypatch.setattr(fetcher, '_get', get)
    with pytest.raises(ValueError, match='robots_disallow'):
        fetcher.acquire(DOC)
    assert calls == [research.ROBOT_URL]


def test_acquisition_refuses_other_issuer_or_future_publication_before_network(tmp_path):
    fetcher = research.BoundedCVMFetcher(tmp_path)
    for update in ({'ticker': 'OTHER3', 'cvm': 1}, {'cvm': 25658},
                   {'published_at': '2026-10-01'}, {'sequence': '124'}):
        with pytest.raises(ValueError, match='scope_no_request_sent'):
            fetcher.acquire({**DOC, **update})
    assert fetcher.requests == []


def test_redirect_is_never_followed():
    with pytest.raises(ValueError, match='redirect_not_followed'):
        research._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://www.b3.com.br/')


def test_actual_byte_hash_and_path_are_checked(tmp_path):
    research._save(tmp_path, research.CACHE + '/source.bin', b'original')
    reader = research.Reader(tmp_path)
    name = research.CACHE + '/source.bin'
    reader.read(name, 'test', hashlib.sha256(b'original').hexdigest())
    with pytest.raises(ValueError, match='hash_mismatch'):
        reader.read(name, 'test', '0' * 64)
    with pytest.raises(ValueError, match='unsafe_cache'):
        reader.read('../secret', 'test')
    with pytest.raises(ValueError, match='write_scope'):
        research._save(tmp_path, 'inventory/current.json', b'new pointer')


def test_missing_native_periods_are_not_zero_cash(tmp_path):
    manifest = tmp_path / 'raw/primary-research/cvm-documents/index-sources.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('[]')
    documents, periods, diagnostics = research.scan_indices(research.Reader(tmp_path), [STOCK])
    assert documents == periods == []
    assert len(diagnostics) == 6
    assert all(row['code'] == 'native_IPE_period_missing_or_ambiguous' for row in diagnostics)


def test_invalid_later_index_row_does_not_leak_partially_valid_period(tmp_path):
    columns = ['Codigo_CVM', 'Data_Entrega', 'Categoria', 'Tipo', 'Assunto', 'Versao',
               'Nome_Companhia', 'Data_Referencia', 'Link_Download']
    row = {'Codigo_CVM': '25372', 'Data_Entrega': '2021-10-01', 'Categoria': 'Relatorio Proventos',
           'Tipo': '', 'Assunto': '', 'Versao': '1', 'Nome_Companhia': 'SENDAS DISTRIBUIDORA S.A.',
           'Data_Referencia': '2021-09-30', 'Link_Download': URL}
    text = io.StringIO()
    writer = csv.DictWriter(text, columns, delimiter=';')
    writer.writeheader()
    writer.writerows([row, {**row, 'Versao': '2'}])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('ipe_cia_aberta_2021.csv', text.getvalue().encode('latin1'))
    directory = tmp_path / 'raw/primary-research/cvm-documents'
    directory.mkdir(parents=True)
    body = buffer.getvalue()
    (directory / 'index.zip').write_bytes(body)
    source = {'year': 2021, 'file': 'raw/primary-research/cvm-documents/index.zip',
              'sha256': hashlib.sha256(body).hexdigest(),
              'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/ipe_cia_aberta_2021.zip'}
    (directory / 'index-sources.json').write_text(json.dumps([source]))
    documents, periods, diagnostics = research.scan_indices(research.Reader(tmp_path), [STOCK])
    assert documents == periods == []
    assert diagnostics[0]['error'] == 'native_IPE_version_mismatch'


def test_real_sidecar_is_offline_read_only_and_keeps_conflicts(monkeypatch):
    folder = Path(__file__).parent / 'data/market'
    if not (folder / research.CACHE / 'reviewed-notices.json').exists():
        pytest.skip('local research originals are not installed')

    def forbidden(*args, **kwargs):
        raise AssertionError('offline sidecar attempted network or write')

    original_open = Path.open

    def read_only_open(self, mode='r', *args, **kwargs):
        if any(flag in mode for flag in ('w', 'a', 'x', '+')):
            forbidden()
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', read_only_open)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    monkeypatch.setattr(research.BoundedCVMFetcher, '__init__', forbidden)
    result = research.build_report(folder)
    assert result['tickers'] == ['ASAI3', 'AGXY3', 'ALPK3']
    assert {row['year'] for row in result['native_CVM_IPE_periods']} == set(range(2021, 2027))
    assert result['network_audit']['new_HTTP_attempts'] == 18
    assert result['network_audit']['direct_CVM_only'] is True
    assert result['active_model_changed'] is result['cash_model_eligible'] is False
    by_ticker = {row['identity']['ticker']: row for row in result['results']}
    assert {c['record_date'] for c in by_ticker['ASAI3']['conflicts']} == {'2022-12-28', '2025-01-06'}
    assert by_ticker['AGXY3']['conflicts'][0]['values'] == ['2023-12-28', '2023-12-29']
    assert len(by_ticker['ALPK3']['events']) == 4
    for issuer in result['results']:
        assert issuer['identity_exact_cached_mapping'] is True
        assert issuer['history_complete'] is issuer['alternative_proof_complete'] is False
        assert issuer['exact_missing']
        assert all(not event['settlement_verified'] and not event['cash_model_eligible'] for event in issuer['events'])
        assert all(row.get('error') != 'review_quote_not_in_original_page' for row in issuer['diagnostics'])
    for receipt in result['sources']:
        assert hashlib.sha256((folder / receipt['file']).read_bytes()).hexdigest() == receipt['sha256']
    json.dumps(result, allow_nan=False)
