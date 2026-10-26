from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from .issuer_bank_sources import (BANK_PAGES, _Redirect, download_sources, inspect_bank_html,
                                  inspect_bank_pdf, match_payment_declarations, parse_research_downloads,
                                  write_research_ledger)


CUTOFF = date(2026, 9, 30)
OBSERVED = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def inspect(content, issuer='bradesco'):
    source = next(row['url'] for row in BANK_PAGES if row['issuer'] == issuer)
    return inspect_bank_html(content, issuer, CUTOFF, OBSERVED, source_url=source)


def table(heading='Calendario de pagamentos', status=None, payment='15/06/2026', amount='0,10'):
    status_header = '<th>Status</th>' if status is not None else ''
    status_cell = f'<td>{status}</td>' if status is not None else ''
    return (f'<h2>{heading}</h2><table><tr><th>Tipo de Rendimento</th><th>Data-base</th>'
            f'<th>Data de Pagamento</th><th>ON bruto</th><th>PN bruto</th>{status_header}</tr>'
            f'<tr><td>JCP Mensais</td><td>01/06/2026</td><td>{payment}</td><td>{amount}</td>'
            f'<td>0,11</td>{status_cell}</tr></table>')


@pytest.mark.parametrize('heading,expected', [('Dividendos pagos', 'paid_reported'),
                                             ('Pagamentos realizados', 'paid_reported'),
                                             ('Pagos e a pagar', 'past_scheduled'),
                                             ('Nao pagos', 'past_scheduled'),
                                             ('Calendario de pagamentos', 'past_scheduled')])
def test_paid_context_is_explicit_not_merely_a_past_date(heading, expected):
    result = inspect(table(heading))
    assert len(result['events']) == 2
    assert {row['status'] for row in result['events']} == {expected}
    assert all(not row['cash_model_eligible'] and not row['financially_confirmed'] for row in result['events'])
    assert not result['history_complete']


@pytest.mark.parametrize('status,expected', [('PAGO', 'paid_reported'), ('A PAGAR', 'past_scheduled'),
                                            ('CANCELADO', 'cancelled'), ('NAO PAGO', 'past_scheduled')])
def test_explicit_row_status_overrides_general_paid_heading(status, expected):
    result = inspect(table('Dividendos pagos', status=status))
    assert result['events'][0]['status'] == expected


def test_future_payments_stay_announced_even_under_paid_heading():
    result = inspect(table('Dividendos pagos', payment='31/12/2026'))
    assert {row['status'] for row in result['events']} == {'announced'}


def test_on_pn_amounts_are_not_swapped_or_implicitly_grossed_up():
    result = inspect(table().replace('ON bruto', 'ON').replace('PN bruto', 'PN'))
    on, pn = result['events']
    assert (on['ticker'], on['share_class'], on['reported_per_share']) == ('BBDC3', 'ON', .1)
    assert (pn['ticker'], pn['share_class'], pn['reported_per_share']) == ('BBDC4', 'PN', .11)
    assert on['gross_per_share'] is pn['gross_per_share'] is None
    assert result['field_status']['gross_per_share'] == 'not_found'
    assert result['status'] == 'partial'
    assert on['published_at'] is None and on['available_as_of'] is None


def test_net_only_remains_net_with_no_tax_assumption():
    result = inspect('<p>Retencao de 17,5%.</p>' + table().replace('ON bruto', 'ON liquido').replace('PN bruto', 'PN liquido'))
    assert result['events'][0]['net_per_share'] == .1
    assert result['events'][0]['gross_per_share'] is None


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '0.10', '-0,10', '--'])
def test_bad_numbers_are_missing_not_zero(amount):
    result = inspect(table(amount=amount))
    assert result['events'][0]['gross_per_share'] is None


def test_missing_payment_date_not_record_date_or_zero():
    result = inspect(table(payment='A definir'))
    assert result['events'][0]['record_date'] == '2026-06-01'
    assert result['events'][0]['payment_date'] is None
    assert result['events'][0]['status'] == 'unresolved'


def test_itau_brazil_dates_gross_net_and_class_equality_proof():
    content = ('<p>Igualmente distribuidos para os acionistas ordinaristas e preferencialistas.</p>'
               '<table><tr><th>Tipo de Evento</th><th>Posicao Acionaria Brasil</th><th>Posicao Acionaria EUA</th>'
               '<th>Data do Pagamento Brasil</th><th>Data do Pagamento EUA</th><th>BRUTO</th><th>LIQUIDO</th></tr>'
               '<tr><td>JCP</td><td>30.12.2025</td><td>05.01.2026</td><td>02.02.2026</td>'
               '<td>09.02.2026</td><td>0,018182</td><td>0,0150</td></tr></table>')
    result = inspect(content, 'itau')
    assert {row['ticker'] for row in result['events']} == {'ITUB3', 'ITUB4'}
    assert all(row['record_date'] == '2025-12-30' and row['payment_date'] == '2026-02-02' for row in result['events'])
    assert result['events'][0]['gross_per_share'] == .018182
    assert result['events'][0]['net_per_share'] == .015
    no_proof = inspect(content[content.index('<table>'):], 'itau')
    assert len(no_proof['events']) == 1
    assert no_proof['events'][0]['ticker'] is None


def test_bonus_record_date_not_effective_date_and_tax_cost_not_cash():
    result = inspect('<table><tr><th>Data AGE</th><th>Data-base</th><th>Bonificacao</th><th>Custo Unitario</th></tr>'
                     '<tr><td>10/03/2022</td><td>18/04/2022</td><td>10%</td><td>R$4,128165265</td></tr></table>')
    assert result['events'] == []
    action = result['actions'][0]
    assert action['approval_date'] == '2022-03-10'
    assert action['record_date'] == '2022-04-18'
    assert action['effective_date'] is None and action['share_class'] is None
    assert action['post_to_pre_share_ratio'] == 1.1
    assert action['cost_per_new_share'] == 4.128165265
    assert not action['cash_income'] and not action['normalization_ready']


def pdf_text(monkeypatch, text):
    class Document:
        pages = [SimpleNamespace(extract_text=lambda: text)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    import pdfplumber
    monkeypatch.setattr(pdfplumber, 'open', lambda *args, **kwargs: Document())


def notice(monkeypatch):
    pdf_text(monkeypatch, '''Banco Bradesco S.A.
Os novos valores previstos a serem pagos serao de R$0,017249826 por acao ordinaria
e R$0,018974809 por acao preferencial, que, liquidos do imposto de renda na fonte de 17,5%,
correspondem a R$0,014231106 por acao ordinaria e R$0,015654217 por acao preferencial.
Janeiro 2.1.2026 5.1.2026 2.2.2026
Junho 1o.6.2026 2.6.2026 1o .7.2026
Dezembro 1o.12.2026 2.12.2026 4.1.2027
Cidade de Deus, Osasco, SP, 23 de janeiro de 2026''')
    return inspect_bank_pdf(b'%PDF unit-test fixture', 'bradesco', CUTOFF, OBSERVED,
                            source_url=BANK_PAGES[1]['url'])


def test_notice_extracts_declared_gross_net_publication_but_does_not_confirm_payment(monkeypatch):
    result = notice(monkeypatch)
    assert len(result['events']) == 6
    assert result['events'][0]['gross_per_share'] == .017249826
    assert result['events'][0]['net_per_share'] == .014231106
    assert result['events'][0]['published_at'] == '2026-01-23'
    assert result['events'][0]['payment_date'] == '2026-02-02'
    assert result['events'][2]['payment_date'] == '2026-07-01'
    assert result['events'][0]['status'] == 'past_scheduled'
    assert result['events'][-1]['status'] == 'announced'
    assert all(row['declaration_only'] for row in result['events'])


def test_bb_actual_split_date_is_not_a_dividend_or_bonus(monkeypatch):
    pdf_text(monkeypatch, 'Banco do Brasil. Em15.04.2024,ocorreuodesdobramentodasacoesnaproporcaode1:2.')
    result = inspect_bank_pdf(b'%PDF unit-test fixture', 'bb', CUTOFF, OBSERVED,
                             source_url='https://www.bb.com.br/docs/portal/bbbi/cartaanual25.pdf')
    assert result['events'] == []
    assert result['actions'][0]['effective_date'] == '2024-04-15'
    assert result['actions'][0]['post_to_pre_share_ratio'] == 2
    assert result['actions'][0]['published_at'] is None


def test_exact_declaration_join_adds_fields_not_paid_state(monkeypatch):
    declarations = notice(monkeypatch)
    history = inspect(table(payment='02/02/2026', amount='0,017249826')
                      .replace('01/06/2026', '02/01/2026').replace('ON bruto', 'ON').replace('PN bruto', 'PN'))
    original = deepcopy(history)
    result = match_payment_declarations(history, declarations)
    assert history == original
    assert result['events'][0]['gross_per_share'] == .017249826
    assert result['events'][0]['published_at'] == '2026-01-23'
    assert result['events'][0]['status'] == 'past_scheduled'
    assert not result['events'][0]['cash_model_eligible']
    assert result['events'][1]['gross_per_share'] is None  # PN value conflicts.
    assert any(row['code'] == 'declaration_amount_disagrees_with_RI' for row in result['issues'])
    assert result['declaration_sources'][0]['sha256'] == declarations['source']['sha256']


@pytest.mark.parametrize('field,new_value', [('cvm', 999), ('share_class', 'PN'), ('ticker', 'ITUB3'),
                                           ('record_date', '2026-01-03'), ('payment_date', None)])
def test_declaration_join_requires_exact_identity_and_dates(monkeypatch, field, new_value):
    declarations = notice(monkeypatch)
    history = inspect(table(payment='02/02/2026', amount='0,017249826')
                      .replace('01/06/2026', '02/01/2026').replace('ON bruto', 'ON').replace('PN bruto', 'PN'))
    declarations['events'][0][field] = new_value
    assert match_payment_declarations(history, declarations)['events'][0]['gross_per_share'] is None


def test_empty_missing_and_wrong_source_do_not_establish_zero():
    assert inspect('<p>Sem dados disponiveis.</p>')['status'] == 'not_found'
    assert inspect('')['status'] == 'invalid'
    with pytest.raises(ValueError, match='issuer_source_host_mismatch'):
        inspect_bank_html(table(), 'itau', CUTOFF, OBSERVED, source_url=BANK_PAGES[1]['url'])


def test_hash_addressed_research_json_preserves_prior_files(tmp_path):
    original = tmp_path / 'original-source.html'
    original.write_bytes(b'unchanged')
    result = inspect(table())
    path = write_research_ledger(tmp_path, [result])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert write_research_ledger(tmp_path, [result]) == path
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert json.loads(path.read_text())['scope'] == 'primary_bank_research_not_model_approval'
    assert not (tmp_path / 'current.json').exists()


def test_offline_download_parser_hashes_and_no_declaration_double_count(tmp_path):
    body = table().encode('utf8')
    name = 'bradesco-payments-page-test.html'
    (tmp_path / name).write_bytes(body)
    receipt = {**BANK_PAGES[1], 'file': name, 'sha256': hashlib.sha256(body).hexdigest(),
               'http_status': 200, 'retrieved_at': OBSERVED.isoformat(), 'status': 'downloaded_unvalidated'}
    (tmp_path / 'manifest-test.json').write_text(json.dumps([receipt]), encoding='utf8')
    output, result = parse_research_downloads(tmp_path, CUTOFF)
    assert output.exists()
    assert result['summary']['calendar_events'] == 2
    assert result['summary']['supporting_declaration_rows_not_additional_cash'] == 0
    assert result['results'][0]['source']['file'] == 'raw/primary-research/banks/' + name
    assert result['sources'][0]['sha256'] == receipt['sha256']
    (tmp_path / name).write_bytes(b'changed source')
    with pytest.raises(ValueError, match='research_source_hash_mismatch'):
        parse_research_downloads(tmp_path, CUTOFF)


def test_supporting_declarations_not_counted_twice_in_research_summary(tmp_path, monkeypatch):
    declarations = notice(monkeypatch)
    history = inspect(table())
    path = write_research_ledger(tmp_path, [history], declarations=[declarations])
    payload = json.loads(path.read_text())
    assert payload['summary']['calendar_events'] == 2
    assert payload['summary']['supporting_declaration_rows_not_additional_cash'] == 6


@pytest.mark.parametrize('url', ['https://www.b3.com.br/file', 'http://www.itau.com.br/file',
                                'https://u:secret@www.itau.com.br/file', 'https://www.itau.com.br/file?token=secret'])
def test_downloader_rejects_unapproved_or_sensitive_urls_without_fetching(tmp_path, url):
    with pytest.raises(ValueError):
        download_sources(tmp_path, [{'issuer': 'itau', 'label': 'test', 'url': url}])
    assert not list(tmp_path.iterdir())


def test_downloader_stops_after_access_denial_and_keeps_prior_files(tmp_path, monkeypatch):
    previous = tmp_path / 'prior.html'
    previous.write_bytes(b'prior source')
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 403, 'private-error-do-not-log', {}, None)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    result = download_sources(tmp_path)
    assert len(calls) == 1
    assert result[0]['http_status'] == 403
    assert result[1]['status'] == 'stopped_after_access_response'
    assert 'private-error' not in json.dumps(result)
    assert previous.read_bytes() == b'prior source'


def test_download_sniffs_pdf_despite_html_mime_and_uses_verified_tls(tmp_path, monkeypatch):
    import ssl

    contexts = []

    class Response:
        status = 200
        url = BANK_PAGES[1]['url']
        headers = {'Content-Type': 'text/html'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'%PDF only a transport fixture'

    class Opener:
        def open(self, request, timeout):
            assert request.get_header('Accept-encoding') == 'identity'
            return Response()

    def build(*handlers):
        contexts.extend(handlers)
        return Opener()

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    result = download_sources(tmp_path, [BANK_PAGES[1]])
    assert result[0]['file'].endswith('.pdf')
    assert result[0]['sha256'] == hashlib.sha256(b'%PDF only a transport fixture').hexdigest()
    assert contexts[1]._context.verify_mode == ssl.CERT_REQUIRED
    assert contexts[1]._context.check_hostname
    with pytest.raises(ValueError, match='unapproved_primary_redirect'):
        _Redirect().redirect_request(urllib.request.Request(BANK_PAGES[1]['url']), None, 302, '', {}, 'https://www.b3.com.br/file')
