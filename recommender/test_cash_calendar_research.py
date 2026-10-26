from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from . import cash_calendar_research as research
from .collect_market_data import DividendHistory


CUTOFF = date(2026, 9, 30)


def source(content, issuer='itausa'):
    digest = hashlib.sha256(content).hexdigest()
    return {'id': issuer + ':' + digest, 'issuer': issuer,
            'url': research.SOURCES[issuer]['url'], 'sha256': digest, 'bytes': len(content),
            'file': f'{research.CACHE}/{issuer}-{digest}.html',
            'retrieved_at': datetime.now(timezone.utc).isoformat()}


def table(rows):
    return ('<table>' + ''.join('<tr>' + ''.join('<td>' + str(cell) + '</td>' for cell in row)
                               + '</tr>' for row in rows) + '</table>')


def itausa(*rows):
    return table([['TIPO', 'DELIBERADO EM', 'POSI\u00c7\u00c3O ACION\u00c1RIA', 'PAGAMENTO',
                   'BRUTO', 'L\u00cdQUIDO(1)', 'DOCUMENTO'], *rows]).encode('utf-8')


def cemig(*rows):
    native = table([['Tipo de provento', 'Data record', 'Data ex-Dividendos', 'Data de Pagamento',
                     'ON', 'PN', 'Montante Total'], *rows])
    data = {'query': {'slug': ['informacoes-financeiras', 'dividendos']},
            'props': {'pageProps': {'layout': {'page': {'content': {'htmlContent': native}}}}}}
    return ('<html><table><tr><td>Only current year rendered</td></tr></table>'
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data) + '</script></html>').encode()


def history(*rows):
    obj = DividendHistory(CUTOFF, len(rows), basis='payment')
    obj.installments, obj.semantic_events = [], []
    obj.source_issues = []
    obj.source_observed_at = datetime.now(timezone.utc).isoformat()
    for record, payment, amount in rows:
        obj.installments.append({'ticker': 'ITSA3', 'record_date': record, 'payment_date': payment,
            'gross_per_unit': amount, 'label': 'JCP', 'source_row': 'old'})
        obj.semantic_events.append({'record_date': record, 'payment_date': payment, 'label': 'JCP',
            'gross_per_share': amount, 'source_row': 'old', 'status': 'past_scheduled'})
    return obj


def test_itausa_native_utf8_gross_and_footnoted_payment_are_read_without_net_conversion():
    body = itausa(['JCP', '16/09/2024', '19/09/2024', '07/03/2025 (2)', '0,0484', '0,04114', ''])
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA4', CUTOFF, source(body))
    event, = parsed['events']
    assert event['record_date'] == '2024-09-19'
    assert event['payment_date'] == '2025-03-07'
    assert event['gross_per_share_decimal'] == '0.0484'
    assert event['status'] == 'past_scheduled'
    assert event['available_as_of'] is None
    assert event['published_at'] is None
    assert event['approval_date'] == '2024-09-16'
    assert not event['gross_reconstructed_from_net']
    assert not event['is_settlement_confirmation']
    assert not event['cash_model_eligible']


@pytest.mark.parametrize('raw', ['at\u00e9 31/12/2026', '31/12/2026 ou 01/01/2027', '-', '',
                                  '31/02/2024', '31/12/26'])
def test_deadline_multiple_date_or_invalid_date_is_not_invented(raw):
    assert research._date(raw) is None


@pytest.mark.parametrize('raw', ['NaN', '-0,3', '0', '0,00', '1.200', '1.2', 'R$ 0,12', '', True])
def test_nonpositive_ambiguous_or_non_Brazilian_amount_never_becomes_zero(raw):
    assert research._amount(raw) is None


@pytest.mark.parametrize('issuer,ticker', [('itausa', 'FESA4'), ('ferbasa', 'ITSA4'), ('unknown', 'ITSA4')])
def test_exact_issuer_ticker_binding_required(issuer, ticker):
    body = itausa()
    with pytest.raises(ValueError, match='exact_issuer_ticker'):
        research.inspect_calendar(body, issuer, ticker, CUTOFF, source(body))


@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('bytes', 1),
                                       ('url', 'https://www.b3.com.br/'), ('issuer', 'ferbasa')])
def test_parse_source_receipt_is_bound_to_original_bytes_and_exact_URL(field, value):
    body = itausa()
    receipt = source(body)
    receipt[field] = value
    with pytest.raises(ValueError, match='identity_or_hash'):
        research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, receipt)


def test_future_payment_is_announced_not_paid_and_future_approval_is_blocked():
    body = itausa(['JCP', '01/10/2026', '08/10/2026', '30/10/2026', '0,10', '0,085', ''])
    event, = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))['events']
    assert event['status'] == 'announced'
    assert 'approval_after_cutoff_not_historically_available' in event['issues']
    assert not event['is_settlement_confirmation']


def test_cemig_historical_tables_are_read_from_native_embedded_JSON_not_rendered_year_only():
    body = cemig(['JSCP', '21/12/2022', '22/12/2022', '30/06/2023 30/12/2023',
                  '0,09057 0,09057', '0,09057 0,09057', '398.607'])
    parsed = research.inspect_calendar(body, 'cemig', 'CMIG3', CUTOFF, source(body, 'cemig'))
    assert len(parsed['events']) == 2
    assert [e['payment_date'] for e in parsed['events']] == ['2023-06-30', '2023-12-30']
    assert all(e['record_date'] == '2022-12-21' for e in parsed['events'])
    assert all(e['gross_per_share_decimal'] is None for e in parsed['events'])
    assert all(e['reported_per_share_decimal'] == '0.09057' for e in parsed['events'])
    assert all(e['status'] == 'past_scheduled' for e in parsed['events'])
    assert not parsed['history_complete']


def test_cemig_unaligned_installments_are_not_zipped_or_divided_by_count():
    body = cemig(['JSCP', '21/12/2022', '22/12/2022', '30/06/2023 30/12/2023',
                  '0,18114', '0,18114', '398.607'])
    parsed = research.inspect_calendar(body, 'cemig', 'CMIG3', CUTOFF, source(body, 'cemig'))
    assert not parsed['events']
    assert parsed['diagnostics'][0]['code'] == 'installment_alignment_unresolved'


def test_cemig_embedded_other_page_is_rejected():
    body = cemig().replace(b'informacoes-financeiras', b'another-page')
    with pytest.raises(ValueError, match='page_identity_mismatch'):
        research.inspect_calendar(body, 'cemig', 'CMIG3', CUTOFF, source(body, 'cemig'))


def test_primary_missing_group_does_not_overwrite_existing_conflicting_values():
    body = itausa(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '0,085', ''])
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))
    original = history(('2024-03-04', '2024-03-15', .11))
    result = research.augment_payment_history(original, parsed)
    assert result.installments == original.installments
    assert result.research_added_installments == 0


def test_missing_group_addition_preserves_dates_amounts_flags_and_original_object():
    body = itausa(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '0,085', ''])
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))
    original = history(('2023-03-04', '2023-03-15', .08))
    before = deepcopy(original.installments)
    result = research.augment_payment_history(original, parsed)
    assert original.installments == before
    assert len(result.installments) == 2
    new = result.installments[-1]
    assert new['record_date'] == '2024-03-04'
    assert new['payment_date'] == '2024-03-15'
    assert new['gross_per_unit'] == .1
    assert new['status'] == 'pending_reconciliation'
    assert new['published_at'] is None
    assert new['primary_declaration_id'] is None


def test_replacement_keeps_old_conflict_and_new_native_evidence_in_audit():
    body = itausa(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '0,085', ''])
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))
    parsed['events'][0].update(primary_declaration_id='test-native-declaration',
        source_locator={'format': 'native_CVM_PDF_table', 'row': 2})
    original = history(('2024-03-04', '2024-03-15', .11))
    corrected = research.replace_verified_primary_groups(original, parsed, {date(2024, 3, 4): .1}, [], CUTOFF)
    assert corrected.installments[0]['gross_per_unit'] == .1
    assert original.installments[0]['gross_per_unit'] == .11
    assert corrected.research_group_corrections[0]['old_calendar_rows'][0]['gross_per_unit'] == .11
    assert corrected.research_group_corrections[0]['old_source_conflict_preserved_in_audit']


def test_primary_group_that_does_not_match_independent_amount_cannot_replace():
    body = itausa(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '0,085', ''])
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))
    parsed['events'][0].update(primary_declaration_id='test-native-declaration',
        source_locator={'format': 'native_CVM_PDF_table', 'row': 2})
    original = history(('2024-03-04', '2024-03-15', .11))
    corrected = research.replace_verified_primary_groups(original, parsed, {date(2024, 3, 4): .12}, [], CUTOFF)
    assert corrected.installments == original.installments
    assert not corrected.research_group_corrections


def native_calendar(*rows):
    body = itausa(*rows)
    parsed = research.inspect_calendar(body, 'itausa', 'ITSA3', CUTOFF, source(body))
    for index, event in enumerate(parsed['events']):
        event.update(primary_declaration_id='test-native-' + str(index),
            source_locator={'format': 'native_CVM_PDF_table', 'row': index + 2})
    return parsed


def test_unique_orphan_record_date_comes_from_native_form_not_nearby_calendar_guess():
    parsed = native_calendar(['JCP', '07/11/2023', '28/12/2023', '10/01/2024', '0,27004196449', '', ''])
    original = history(('2024-01-01', '2024-01-10', .27004196))
    corrected = research.relocate_verified_primary_groups(original, parsed,
        {date(2023, 12, 28): .27004196449}, CUTOFF)
    assert corrected.installments[0]['record_date'] == '2023-12-28'
    assert corrected.installments[0]['payment_date'] == '2024-01-10'
    assert original.installments[0]['record_date'] == '2024-01-01'
    audit, = corrected.research_group_corrections
    assert audit['original_record_date'] == '2024-01-01'
    assert audit['old_calendar_rows'] == original.installments
    assert not audit['payment_dates_changed']
    assert not audit['settlement_verified']


@pytest.mark.parametrize('case', ['different_payment', 'valid_old_record', 'ambiguous_old',
                                 'ambiguous_new', 'net_versus_gross'])
def test_record_relocation_rejects_ambiguous_or_economically_different_event(case):
    rows = [['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '', '']]
    original = history(('2024-03-05', '2024-03-15', .1))
    expected = {date(2024, 3, 4): .1}
    if case == 'different_payment':
        rows[0][3] = '18/03/2024'
    elif case == 'valid_old_record':
        expected[date(2024, 3, 5)] = .1
    elif case == 'ambiguous_old':
        original = history(('2024-03-05', '2024-03-15', .1), ('2024-03-06', '2024-03-15', .1))
    elif case == 'ambiguous_new':
        rows.append(['JCP', '01/03/2024', '07/03/2024', '15/03/2024', '0,10', '', ''])
        expected[date(2024, 3, 7)] = .1
    else:
        original.installments[0]['gross_per_unit'] = .085
    corrected = research.relocate_verified_primary_groups(original, native_calendar(*rows), expected, CUTOFF)
    assert corrected.installments == original.installments
    assert not corrected.research_group_corrections


def test_repeated_reconciliation_retains_original_conflict_audit_and_source_receipts():
    parsed = native_calendar(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '', ''])
    original = history(('2024-03-04', '2024-03-15', .11))
    expected = {date(2024, 3, 4): .1}
    first = research.replace_verified_primary_groups(original, parsed, expected, [], CUTOFF)
    second = research.replace_verified_primary_groups(first, parsed, expected, [], CUTOFF)
    third = research.augment_payment_history(second, parsed)
    assert first.installments == second.installments == third.installments
    assert first.research_group_corrections == second.research_group_corrections == third.research_group_corrections
    assert first.research_sources == second.research_sources == third.research_sources
    assert first.event_count == second.event_count == third.event_count


@pytest.mark.parametrize('variant,expected_count', [('identical', 1), ('different_field', 2),
                                                    ('two_native_declarations', 2)])
def test_independent_duplicates_need_every_field_equal_and_one_exact_native_row(monkeypatch, tmp_path,
                                                                              variant, expected_count):
    stock = {'ticker': 'TEST3', 'cvm': 1, 'share_class': 'ON', 'isin': 'BRTESTACNOR1'}
    row = {'typeStock': 'ON', 'dateApproval': '01/03/2024', 'lastDatePriorEx': '04/03/2024',
           'valueCash': '0,10', 'quotedPerShares': '1', 'closingPricePriorExDate': '20,00'}
    rows = [row, deepcopy(row)]
    if variant == 'different_field':
        rows[1]['closingPricePriorExDate'] = '20,01'
    event = {'record_date': '2024-03-04', 'approval_date': '2024-03-01', 'isin': stock['isin'],
             'reported_per_share_decimal': '0.10', 'source_ref': 'native'}
    calendar = {'events': [event], 'sources': [{'id': 'native', 'file': 'original.pdf',
                                              'url': 'https://www.rad.cvm.gov.br/', 'authority': 'primary'}]}
    if variant == 'two_native_declarations':
        calendar['events'].append(deepcopy(event))
    monkeypatch.setattr(research, 'load_cvm_calendar', lambda *args: calendar)

    class Cache:
        folder = tmp_path
        receipts = {}
        def read(self, name, role):
            self.receipts[name] = {'role': role}

    result, audit = research.deduplicate_independent_rows(stock, Cache(), CUTOFF, rows)
    assert len(result) == expected_count
    assert len(rows) == 2
    assert bool(audit) == (variant == 'identical')


@pytest.mark.parametrize('url', ['https://www.b3.com.br/', 'https://sistemaswebb3-listados.b3.com.br/',
                                 'https://ri.b3.com.br/', 'http://ri.itausa.com.br/',
                                 'https://ri.itausa.com.br:444/', 'https://user@ri.itausa.com.br/',
                                 'https://ri.itausa.com.br.evil.example/'])
def test_no_exchange_endpoint_credentials_plain_HTTP_or_unapproved_host(url):
    assert not research._approved(url)


def test_cross_host_redirect_is_rejected_before_following():
    request = urllib.request.Request(research.SOURCES['itausa']['url'])
    with pytest.raises(ValueError, match='no_request_sent'):
        research._Redirect().redirect_request(request, None, 302, '', {}, research.SOURCES['romi']['url'])


def test_acquisition_creates_hash_bound_immutable_receipt_then_reuses_offline(tmp_path, monkeypatch):
    body = itausa(['JCP', '01/03/2024', '04/03/2024', '15/03/2024', '0,10', '0,085', ''])
    calls = []

    class Response:
        headers = {'Content-Type': 'text/html; charset=UTF-8'}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def geturl(self): return research.SOURCES['itausa']['url']
        def read(self, size):
            assert size == research.MAX_BYTES + 1
            return body

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return Response()

    monkeypatch.setattr(research.urllib.request, 'build_opener', lambda *_: Opener())
    actual, receipt = research.acquire(tmp_path, 'itausa')
    assert actual == body and len(calls) == 1
    stored = (tmp_path / receipt['file']).read_bytes()
    again, repeated = research.acquire(tmp_path, 'itausa')
    assert again == stored and repeated == receipt and len(calls) == 1
    (tmp_path / receipt['file']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash_mismatch'):
        research.acquire(tmp_path, 'itausa')


@pytest.mark.parametrize('status', [401, 403, 429])
def test_access_error_stops_with_no_retry_or_alternate_endpoint(tmp_path, monkeypatch, status):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'stop', {}, None)

    monkeypatch.setattr(research.urllib.request, 'build_opener', lambda *_: Opener())
    with pytest.raises(urllib.error.HTTPError):
        research.acquire(tmp_path, 'itausa')
    assert calls == [research.SOURCES['itausa']['url']]
    assert not list((tmp_path / research.CACHE).glob('*source.json'))


@pytest.mark.parametrize('name', ['../outside.html', 'raw/primary-research/cash-calendars/../../secret',
                                 'C:/secrets', research.CACHE + '/itausa-' + 'g' * 64 + '.html'])
def test_read_only_cache_rejects_unbounded_paths(tmp_path, name):
    with pytest.raises(ValueError, match='reference'):
        research.read_receipt(tmp_path, {'file': name})


REAL_FOLDER = Path(__file__).parent / 'data/market'
REAL_RECOVERED = ('BNBR3', 'BRSR3', 'BRSR6', 'BSLI3', 'CMIG3', 'HYPE3', 'MDIA3', 'MTSA4',
                  'TGMA3', 'TUPY3', 'MILS3', 'PFRM3', 'CPLE3', 'RENT3', 'VIVA3', 'ENMT3', 'ENMT4')


@pytest.fixture(scope='module')
def real_statements():
    from .market_inventory import load_inventory
    from .inventory_financials import prepare_financials
    inventory = load_inventory(REAL_FOLDER)
    stocks = [row for row in inventory['stocks'] if row['ticker'] in REAL_RECOVERED]
    statements, _, _ = prepare_financials(REAL_FOLDER, stocks, CUTOFF)
    return statements


@pytest.mark.parametrize('ticker', REAL_RECOVERED)
@pytest.mark.skipif(not (REAL_FOLDER / research.CACHE).is_dir(), reason='immutable local research caches not distributed')
def test_real_recovered_sources_pass_the_entire_unchanged_candidate_pipeline(ticker, monkeypatch, real_statements):
    from . import inventory_candidates as candidates
    from .market_inventory import load_inventory
    from .broad_stocks import split_price_history
    inventory = load_inventory(REAL_FOLDER)
    stock = next(row for row in inventory['stocks'] if row['ticker'] == ticker)
    original_reader = candidates._payments
    original_cash_reader = candidates._cash_pages

    def cash_reader(code, codes, cvm, cache):
        rows = original_cash_reader(code, codes, cvm, cache)
        return research.deduplicate_independent_rows(stock, cache, CUTOFF, rows)[0]

    def reader(code, cache, cutoff, primary=None, *, coverage_start=None, identity=None):
        original, url = original_reader(code, cache, cutoff, primary,
                                        coverage_start=coverage_start, identity=identity)
        cvm = stock['cvm']
        detail = cache.json(f'raw/expanded/b3-detail-{cvm}.json', 'identity')
        codes = {item['code']: item['isin'] for item in detail['otherCodes']}
        rows = candidates._cash_pages(code, codes, cvm, cache)
        relative = f'raw/expanded/{code}-prices.json'
        if not (cache.folder / relative).is_file():
            relative = f'raw/inventory/daily-prices/{code}.json'
        _, actions = split_price_history(cache.read(relative, 'prices')[0], code, cutoff, allow_recent_actions=True)
        return research.apply_research_calendar(stock, cache, cutoff, original, rows, actions), url

    monkeypatch.setattr(candidates, '_payments', reader)
    monkeypatch.setattr(candidates, '_cash_pages', cash_reader)
    prepared, pending, receipts = candidates.build_candidates(
        REAL_FOLDER, {**inventory, 'stocks': [stock]}, CUTOFF)
    assert not pending, pending
    assert len(prepared) == 1
    result = prepared[0]
    assert result['ticker'] == ticker
    assert len(result['independent_cash_matches']) >= 4
    assert len(result['independent_price_matches']) >= 4
    assert not result['checks']['point_in_time_archive_verified']
    assert not result['financially_validated']
    assert all(row['financial_confirmation'] == 'reported_calendar_independently_amount_reconciled_not_settlement_confirmed'
               for row in result['ledger'])
    if ticker in ('ENMT3', 'ENMT4'):
        start, = [row for row in result['ledger'] if row['record_date'] == '2022-03-22']
        assert start['payment_date_basis'] == 'explicit_payment_start_date_not_settlement'
        assert start['full_settlement_on_date_verified'] is False
    assert any(row['role'] == 'primary_cash_calendar' for row in receipts)
    from .collect_market_data import financial_features
    current = financial_features(real_statements, result['payments'], CUTOFF,
                                 (result['cvm'], result['share_class'], result['sector_code']))
    if ticker not in ('BSLI3', 'TGMA3'):
        assert current is not None
    # Source reconciliation alone never grants financial/model approval.
    # A later filing-bound financial recovery may legitimately resolve these
    # separate pending cases without changing this cash-calendar contract.
    assert result['readiness_scope'] == 'reconciled_sources_only_financial_checks_required'
    if current is not None:
        assert date.fromisoformat(current[1]['fiscal_end']) >= date.fromisoformat(result['financial_basis_start'])


@pytest.mark.skipif(not (REAL_FOLDER / research.CACHE).is_dir(), reason='immutable research caches not distributed')
def test_real_luxm_unresolved_native_version_is_not_replaced_with_an_older_readable_notice():
    from .market_inventory import load_inventory
    from .inventory_candidates import _Cache, _cash_pages
    inventory = load_inventory(REAL_FOLDER)
    stock = next(row for row in inventory['stocks'] if row['ticker'] == 'LUXM4')
    cache = _Cache(REAL_FOLDER, inventory)
    cvm = stock['cvm']
    detail = cache.json(f'raw/expanded/b3-detail-{cvm}.json', 'identity')
    rows = _cash_pages('LUXM4', {item['code']: item['isin'] for item in detail['otherCodes']}, cvm, cache)
    calendar = research.load_cvm_calendar(REAL_FOLDER, stock, CUTOFF, rows)
    assert not calendar['events']
    assert calendar['diagnostics']


def older_complete_calendar():
    event = {'ticker': 'TEST3', 'cvm': 1, 'share_class': 'ON', 'record_date': '2024-03-04',
        'payment_date': '2024-03-15', 'label': 'JCP', 'primary_declaration_id': 'reviewed:row',
        'complete_group_id': 'group:1', 'source_ref': 'source:1', 'issues': [],
        'gross_per_share_decimal': '0.10', 'reported_per_share_decimal': '0.10',
        'gross_reconstructed_from_net': False, 'amount_tax_basis': 'explicit_gross_per_share',
        'source_locator': {'format': 'CVM_reviewed_old_notice_complete_group'}}
    return {'ticker': 'TEST3', 'cvm': 1, 'share_class': 'ON', 'events': [event],
        'sources': [{'id': 'source:1'}], 'complete_groups': [
            {'id': 'group:1', 'record_date': '2024-03-04', 'events': [event],
             'target_group_complete': True, 'installment_count': 1, 'gross_total_decimal': '0.10'}]}


def test_verified_older_notice_requires_complete_gross_group_not_only_a_format_label():
    calendar = older_complete_calendar()
    assert research._verified_primary_group_event(calendar['events'][0], calendar)


def test_exact_native_group_requires_security_identity_for_every_component():
    from copy import deepcopy
    calendar = older_complete_calendar()
    calendar['isin'] = 'BRTESTACNOR0'
    first = calendar['events'][0]
    first['isin'] = calendar['isin']
    first['source_locator']['format'] = 'exact_reviewed_native_entitlement_group'
    first['amount_tax_basis'] = 'dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows'
    second = deepcopy(first)
    second['payment_date'] = '2024-04-15'
    calendar['events'].append(second)
    group = calendar['complete_groups'][0]
    group.update(events=calendar['events'], installment_count=2, gross_total_decimal='0.20')
    assert research._verified_primary_group_event(first, calendar)
    second['isin'] = 'BRWRONGCNO00'
    assert not research._verified_primary_group_event(first, calendar)


@pytest.mark.parametrize('fault', ['partial', 'net', 'wrong_total', 'missing_source', 'missing_identity'])
def test_exact_native_format_alone_cannot_authorize_a_partial_or_unverified_group(fault):
    calendar = older_complete_calendar()
    calendar['isin'] = 'BRTESTACNOR0'
    event, group = calendar['events'][0], calendar['complete_groups'][0]
    event.update(isin=calendar['isin'],
                 amount_tax_basis='dividend_per_ordinary_share_corroborated_by_native_gross_ISIN_rows')
    event['source_locator']['format'] = 'exact_reviewed_native_entitlement_group'
    if fault == 'partial': group['target_group_complete'] = False
    elif fault == 'net': event['amount_tax_basis'] = 'explicit_net'
    elif fault == 'wrong_total': group['gross_total_decimal'] = '0.20'
    elif fault == 'missing_source': calendar['sources'] = []
    elif fault == 'missing_identity': del calendar['isin']
    assert not research._verified_primary_group_event(event, calendar)


@pytest.mark.parametrize('fault', ['partial', 'wrong_class', 'net', 'gross_up', 'missing_source',
                                  'wrong_total', 'wrong_record', 'wrong_installments', 'numeric_amount',
                                  'infinite_amount', 'missing_group'])
def test_partial_wrong_class_or_net_older_group_cannot_repair_parent_cash(fault):
    calendar = older_complete_calendar()
    event, group = calendar['events'][0], calendar['complete_groups'][0]
    if fault == 'partial': group['target_group_complete'] = False
    elif fault == 'wrong_class': event['share_class'] = 'PN'
    elif fault == 'net': event['amount_tax_basis'] = 'explicit_net'
    elif fault == 'gross_up': event['gross_reconstructed_from_net'] = True
    elif fault == 'missing_source': calendar['sources'] = []
    elif fault == 'wrong_total': group['gross_total_decimal'] = '0.20'
    elif fault == 'wrong_record': event['record_date'] = '2024-03-05'
    elif fault == 'wrong_installments': group['installment_count'] = 2
    elif fault == 'numeric_amount':
        event['gross_per_share_decimal'] = event['reported_per_share_decimal'] = .1
    elif fault == 'infinite_amount':
        event['gross_per_share_decimal'] = event['reported_per_share_decimal'] = 'Infinity'
    elif fault == 'missing_group': calendar['complete_groups'] = []
    assert not research._verified_primary_group_event(event, calendar)
