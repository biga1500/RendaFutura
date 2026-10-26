from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import urllib.error

import pytest

from . import revised_cash_research as research


URL = ('https://www.rad.cvm.gov.br/ENET/frmDownloadDocumento.aspx?Tela=ext&descTipo=IPE'
       '&CodigoInstituicao=1&numProtocolo=123&numSequencia=123&numVersao=1')


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('tests_must_never_use_live_network')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


def chain(ticker='PINE4'):
    target = research.TARGETS[ticker]
    pine = ticker == 'PINE4'
    base = {'ticker': ticker, **target, 'amount': '0.0139242' if pine else '0.119574197',
        'payment_date': '2025-02-28' if pine else '2025-01-07',
        'approval_date': '2025-02-11' if pine else '2024-12-17',
        'published_at': '2025-02-12' if pine else '2024-12-17',
        'label': 'DIVIDENDO' if pine else 'JCP', 'identity_basis': {}}
    amendment = {'published_at': '2025-02-24' if pine else '2024-12-20',
        'sequence': '863361' if pine else '842767', 'version': 1,
        'fields': {'amount': '0.0137779' if pine else '0.120110445',
            'previous_amount': base['amount'], 'revision_language': 'explicit_from_to',
            'label': base['label'], 'share_class': target['share_class'],
            'approval_date': base['approval_date'], 'period_end': target['period_end'],
            'payment_date': base['payment_date']},
        'source': {'index_evidence': {'raw_row': {'Codigo_CVM': str(target['cvm']),
            'CNPJ_Companhia': target['cnpj']}}}}
    return base, amendment


def test_exact_source_revision_is_audited_without_admission_or_settlement():
    base, amendment = chain()
    result = research.audit_chain(base, amendment)
    assert result['gross_per_share_decimal'] == '0.0137779'
    assert result['payment_date'] == '2025-02-28'
    assert result['target_schedule_audited'] is True
    for key in ('cash_model_eligible', 'settlement_verified', 'point_in_time_archive_verified',
                'history_complete', 'gross_reconstructed_from_net', 'independent_gate_run'):
        assert result[key] is False


def test_parent_hook_rejects_wrong_security_and_leaves_other_scopes_alone(tmp_path):
    from datetime import date
    stock = {'ticker': 'PINE4', **research.TARGETS['PINE4']}
    assert not research.verified_pine_calendar(tmp_path, stock, date(2026, 8, 31))['events']
    assert not research.verified_pine_calendar(tmp_path, {'ticker': 'ALOS3'}, date(2026, 9, 30))['events']
    with pytest.raises(ValueError, match='exact_identity'):
        research.verified_pine_calendar(tmp_path, {**stock, 'share_class': 'ON'}, date(2026, 9, 30))


@pytest.mark.skipif(not (Path(__file__).parent / 'data/market' / research.CACHE / 'reviewed-notices.json').is_file(),
                    reason='immutable local research caches not distributed')
def test_verified_pine_revision_passes_source_gates_without_granting_financial_admission():
    from datetime import date
    from .market_inventory import load_inventory
    from .inventory_candidates import build_candidates
    root = Path(__file__).parent / 'data/market'
    before = research.protected_state(root)
    inventory = load_inventory(root)
    stock = next(row for row in inventory['stocks'] if row['ticker'] == 'PINE4')
    calendar = research.verified_pine_calendar(root, stock, date(2026, 9, 30))
    assert len(calendar['events']) == 1
    event = calendar['events'][0]
    assert event['reported_per_share_decimal'] == '0.0137779'
    assert event['payment_date'] == '2025-02-28'
    assert event['source_locator']['format'] == 'CVM_explicit_revision_chain'
    assert not calendar['settlement_verified'] and not calendar['history_complete']
    candidates, pending, sources = build_candidates(root, {**inventory, 'stocks': [stock]}, date(2026, 9, 30))
    assert not pending, pending
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert len(candidate['independent_cash_matches']) >= 4
    assert len(candidate['independent_price_matches']) >= 4
    for sequence in ('858761', '863361', '863360'):
        assert any('numSequencia=' + sequence + '&' in (source.get('url') or '') for source in sources)
    corrected = [row for row in candidate['ledger'] if row['record_date'] == '2025-02-19']
    assert len(corrected) == 1 and corrected[0]['gross_per_unit'] == .0137779
    assert corrected[0]['payment_date'] == '2025-02-28'
    assert not corrected[0]['point_in_time_archive_verified']
    assert candidate['financially_validated'] is False
    assert candidate['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    assert research.protected_state(root) == before


def test_wrong_year_in_latest_notice_stays_conflicted_not_repaired():
    base, amendment = chain('ALOS3')
    amendment['fields']['payment_date'] = '2024-01-07'
    result = research.audit_chain(base, amendment)
    assert result['amount_revision_verified'] is True
    assert result['payment_date'] is None
    assert result['base_schedule_payment_date'] == '2025-01-07'
    assert result['target_schedule_audited'] is False
    assert result['remaining_conflicts'][0]['amendment_payment_before_record'] is True


@pytest.mark.parametrize(('field', 'wrong'), [
    ('cvm', 22357), ('isin', 'BRPINEACNOR1'), ('share_class', 'ON'),
    ('record_date', '2025-02-20'), ('period_end', '2023-12-31')])
def test_revision_cannot_cross_CVM_class_ISIN_record_or_period(field, wrong):
    base, amendment = chain()
    amendment['fields'][field] = wrong
    with pytest.raises(ValueError, match='identity_or_period_mismatch'):
        research.audit_chain(base, amendment)


@pytest.mark.parametrize(('field', 'wrong'), [
    ('isin', 'BRPINEACNOR1'), ('share_class', 'ON'), ('period_end', '2023-12-31')])
def test_base_must_match_exact_target_identity_and_proved_period(field, wrong):
    base, amendment = chain()
    base[field] = wrong
    with pytest.raises(ValueError, match='base_exact_security'):
        research.audit_chain(base, amendment)


@pytest.mark.parametrize('mutation', ['no_revision', 'no_link', 'old_amount', 'wrong_CNPJ', 'earlier_delivery'])
def test_numeric_agreement_alone_never_proves_a_revision(mutation):
    base, amendment = chain()
    if mutation == 'no_revision':
        amendment['fields'].pop('revision_language')
    elif mutation == 'no_link':
        amendment['fields'].pop('approval_date')
    elif mutation == 'old_amount':
        amendment['fields']['previous_amount'] = '0.123'
    elif mutation == 'wrong_CNPJ':
        amendment['source']['index_evidence']['raw_row']['CNPJ_Companhia'] = '05878397000132'
    else:
        amendment['published_at'] = '2025-02-01'
    with pytest.raises(ValueError):
        research.audit_chain(base, amendment)


def test_amount_scale_is_not_a_revision_or_rounding_tolerance():
    base, amendment = chain()
    amendment['fields']['previous_amount'] = '0.01392420000'
    assert research.audit_chain(base, amendment)['amount_revision_verified'] is True
    amendment['fields']['previous_amount'] = '0.01392420001'
    with pytest.raises(ValueError, match='previous_amount_mismatch'):
        research.audit_chain(base, amendment)


def test_neither_document_version_nor_market_amount_selects_source():
    base, amendment = chain()
    result = research.audit_chain(base, amendment)
    for market_value in ('0.0139242', '0.0137779', '999.99'):
        amendment['independent_amount'] = market_value
        amendment['version'] = 999
        other = research.audit_chain(base, amendment)
        assert other['gross_per_share_decimal'] == result['gross_per_share_decimal']
        assert other['payment_date'] == result['payment_date']


def test_bad_signature_date_requires_separate_dated_board():
    base, amendment = chain()
    amendment['fields']['document_date'] = '2025-01-24'
    result = research.audit_chain(base, amendment)
    assert result['target_schedule_audited'] is False
    assert result['source_anomalies'][0]['raw_document_date'] == '2025-01-24'
    board = deepcopy(amendment)
    board['fields']['document_date'] = '2025-02-24'
    result = research.audit_chain(base, amendment, corroboration=board)
    assert result['target_schedule_audited'] is True
    assert result['source_anomalies'][0]['no_body_date_repair'] is True


def review_fixture():
    stock = {'ticker': 'PINE4', **research.TARGETS['PINE4']}
    doc = {'cvm': stock['cvm'], 'sequence': '123', 'version': 1,
        'published_at': '2025-02-24', 'url': URL,
        'index_evidence': {'raw_row': {'Codigo_CVM': str(stock['cvm']), 'CNPJ_Companhia': stock['cnpj']}}}
    source = {'sha256': 'a' * 64, 'file': 'raw/original.pdf'}
    review = {'sequence': '123', 'version': 1, 'sha256': source['sha256'], 'fields': {
        'payment_date': {'page': 1, 'quote': 'O credito dos dividendos ocorrera em 28 de fevereiro de 2025',
                         'value': '2025-02-28'}}}
    return [review['fields']['payment_date']['quote']], review, doc, source, stock


@pytest.mark.parametrize('quote', [
    'ex-dividendos em 28 de fevereiro de 2025', 'pagamento ate o dia 28 de fevereiro de 2025',
    '28 de fevereiro de 2025'])
def test_ex_date_deadline_and_bare_date_are_not_payment(quote):
    pages, review, doc, source, stock = review_fixture()
    pages[0] = review['fields']['payment_date']['quote'] = quote
    with pytest.raises(ValueError, match='payment_context'):
        research.reviewed_facts(pages, review, doc, source, stock)


@pytest.mark.parametrize('mutation', ['hash', 'version', 'CVM', 'CNPJ', 'quote', 'day'])
def test_field_review_requires_exact_original_and_index_identity(mutation):
    pages, review, doc, source, stock = review_fixture()
    if mutation == 'hash':
        review['sha256'] = 'b' * 64
    elif mutation == 'version':
        review['version'] = 2
    elif mutation == 'CVM':
        doc['cvm'] = 22357
    elif mutation == 'CNPJ':
        doc['index_evidence']['raw_row']['CNPJ_Companhia'] = '05878397000132'
    elif mutation == 'quote':
        review['fields']['payment_date']['quote'] = 'invented'
    else:
        review['fields']['payment_date']['value'] = '2025-02-27'
    with pytest.raises(ValueError):
        research.reviewed_facts(pages, review, doc, source, stock)


def test_English_cash_table_cannot_swap_ex_and_payment_columns():
    pages, review, doc, source, stock = review_fixture()
    quote = 'Event Cut-off Date Ex Date Payment Date IOE 12/20/2024 12/23/2024 01/07/2025'
    pages[0] = quote
    proof = review['fields']['payment_date']
    proof.update(quote=quote, locale='en-US', value='2024-12-23')
    with pytest.raises(ValueError, match='date_column_mismatch'):
        research.reviewed_facts(pages, review, doc, source, stock)
    proof['value'] = '2025-01-07'
    assert research.reviewed_facts(pages, review, doc, source, stock)['fields']['payment_date'] == '2025-01-07'


@pytest.mark.parametrize('url', [
    'https://www.b3.com.br/robots.txt', 'https://sistemaswebb3-listados.b3.com.br/data',
    'https://brapi.dev/api/quote/PINE4', 'https://proxy.example/?url=' + URL,
    URL + '&proxy=https://www.b3.com.br', URL.replace('https://', 'http://'),
    URL.replace('www.rad.cvm.gov.br', 'www.rad.cvm.gov.br.evil.example')])
def test_acquisition_rejects_exchange_BRAPI_proxy_and_lookalike_URLs(tmp_path, url):
    with pytest.raises(ValueError, match='unapproved_URL'):
        research.Fetcher(tmp_path)._get(url)
    assert research.Fetcher(tmp_path).ledger('request') == []


def test_known_863440_non_PDF_never_retried(tmp_path):
    with pytest.raises(ValueError, match='known_non_PDF'):
        research.Fetcher(tmp_path)._get(URL.replace('numSequencia=123', 'numSequencia=863440'))
    assert research.Fetcher(tmp_path).ledger('request') == []


def test_persistent_twenty_request_budget_cannot_be_reset(tmp_path):
    for ordinal in range(1, 21):
        research.save_body(tmp_path, f'request-{ordinal:02d}.json', research.shared._dump({'url': URL + str(ordinal)}))
    with pytest.raises(ValueError, match='budget_exhausted'):
        research.Fetcher(tmp_path)._get(URL)


@pytest.mark.parametrize('status', [401, 403, 429])
def test_denial_stops_future_requests_after_restarting_fetcher(tmp_path, status):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, 'denied', {}, io.BytesIO(b'denied'))
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = Opener()
    _, receipt = fetcher._get(URL)
    assert receipt['http_status'] == status
    with pytest.raises(ValueError, match='access_stop'):
        research.Fetcher(tmp_path)._get(URL.replace('numSequencia=123', 'numSequencia=124'))
    assert len(fetcher.ledger('request')) == 1


def test_HTTP_200_HTML_is_cached_as_non_PDF_and_not_retried(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'application/pdf'}

    class Opener:
        calls = 0

        def open(self, request, timeout):
            self.calls += 1
            if request.full_url == research.ROBOT_URL:
                raise urllib.error.HTTPError(request.full_url, 404, 'not found', {}, io.BytesIO(b''))
            return Response(b'<html>not a PDF despite MIME</html>')

    doc = {'url': URL, 'sequence': '123', 'version': 1}
    monkeypatch.setattr(research, 'indexed_documents', lambda reader: ([doc], []))
    fetcher = research.Fetcher(tmp_path)
    fetcher.opener = opener = Opener()
    receipt = fetcher.acquire(doc)
    assert receipt['pdf_signature_verified'] is False
    with pytest.raises(ValueError, match='already_cached'):
        fetcher.acquire(doc)
    assert opener.calls == len(fetcher.ledger('request')) == 2
    with pytest.raises(ValueError, match='not_PDF'):
        research.pdf_pages(b'<html>not a PDF</html>')


def test_immutable_scope_and_original_hash_verification(tmp_path):
    with pytest.raises(ValueError, match='unsafe_cache_reference'):
        research.save_body(tmp_path, '../../outside.json', b'{}')
    research.save_body(tmp_path, 'original.bin', b'original')
    with pytest.raises(ValueError, match='immutable_research_conflict'):
        research.save_body(tmp_path, 'original.bin', b'changed')
    with pytest.raises(ValueError, match='content_hash_mismatch'):
        research.shared.Reader(tmp_path).read(research.CACHE + '/original.bin', 'original', '0' * 64)


def test_cached_sources_reproduce_records_periods_and_remaining_conflict(monkeypatch):
    folder = Path(__file__).parent / 'data/market'
    if not (folder / research.CACHE / 'reviewed-notices.json').is_file():
        pytest.skip('local primary originals are not distributed with the code')
    monkeypatch.setattr(research.Fetcher, '_get', lambda *args: pytest.fail('offline build attempted network'))
    captured = {}

    def capture(folder, prefix, payload):
        captured['report'] = payload
        return 'report-not-written-in-test.json'

    monkeypatch.setattr(research, 'save', capture)
    _, report = research.build_report(folder)
    pine, allos = report['events']
    assert pine['gross_per_share_decimal'] == '0.0137779'
    assert pine['target_schedule_audited'] is True
    assert allos['gross_per_share_decimal'] == '0.120110445'
    assert allos['record_date'] == '2024-12-20'
    assert allos['payment_date'] is None
    assert allos['base']['linked_notice']['fields']['portuguese_record_date'] == '2025-12-20'
    forms = {e['document_sequence']: e for e in report['native_observations']}
    assert forms['851744']['record_date'] == '2025-01-23'
    assert forms['851744']['fiscal_year'] == '2023'
    for sequence in ('841736', '842850'):
        assert forms[sequence]['record_date'] == '2025-02-20'
        assert forms[sequence]['record_matches_target'] is False
    assert len(report['remaining_conflicts']) == 1
    assert 0 < report['requests']['new_HTTP_attempts'] <= research.MAX_REQUESTS
    assert report['active_model_changed'] is report['cash_model_eligible'] is False
    blocked = next(d for d in report['document_diagnostics'] if d['sequence'] == '863440')
    assert blocked['previous_signature_is_PDF'] is False
