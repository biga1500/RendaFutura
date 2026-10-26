"""Offline primary action extraction, provenance and reconciliation guards."""

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import socket

import pytest
from lxml import html

from . import itausa_primary_actions as actions


BONUSES = [
    ('2021-12-13', '2021-12-20', '2021-12-21', '5', 105, 100),
    ('2022-11-07', '2022-11-10', '2022-11-11', '10', 11, 10),
    ('2023-11-22', '2023-11-27', '2023-11-28', '5', 105, 100),
    ('2024-11-11', '2024-12-02', '2024-12-03', '5', 105, 100),
    ('2025-12-15', '2025-12-18', '2025-12-19', '2', 102, 100),
]
NOTICE_IDS = [
    'f1a2dfee-3cb6-4166-ae57-1c0c9759382d',
    '04345615-2f45-4473-b447-2d5fff43cc91',
    'dee6f6ec-bf45-af3a-b09f-cf0196d45db5',
    'e9b8426a-7813-3822-8e45-59748ea8ff21',
    '333de7f9-5d66-92bf-daf5-e850ca0a8f0c',
]


def source(body, name='calendar.html'):
    return {'file': name, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}


def native_page(*, percent='5%', effect='5 novas acoes para cada 100 possuidas da mesma especie',
                species=True, extra='', record='20/12/2021'):
    identity = ('preferencialista (ITSA4) e/ou ordinaristas (ITSA3)' if species else
                'acionistas ITSA3 e ITSA4')
    bonus_headers = ''.join('<th>' + head + '</th>' for head in actions.BONUS_HEADERS)
    subscription_headers = ''.join('<th>' + head + '</th>' for head in actions.SUBSCRIPTION_HEADERS)
    return (f'<html><body><p>{identity}</p><div class="accordion__item">'
            '<div class="accordion__item__header">Bonificacao em Acoes</div>'
            f'<table><thead><tr>{bonus_headers}</tr></thead><tbody><tr>'
            f'<td>13/12/2021</td><td>{record}</td><td>R$ 18,191662</td>'
            f'<td>{percent}</td><td>{effect}</td><td><a href="{actions.NOTICE_BASE}'
            f'{NOTICE_IDS[0]}?origin=1">Documento</a></td></tr>{extra}</tbody></table></div>'
            '<div class="accordion__item"><div class="accordion__item__header">'
            'Subscricao em Acoes (chamada de capital)</div>'
            f'<table><thead><tr>{subscription_headers}</tr></thead><tbody><tr>'
            '<td>14/08/2023</td><td>17/08/2023</td><td>R$ 6,50</td><td>1,39%</td>'
            '<td>Aumento no capital social em R$ 877 milhoes e emissao de 134.923.077 novas acoes</td>'
            f'<td><a href="{actions.NOTICE_BASE}fac6b435-abe5-4eeb-8f00-eaea34902bd1?origin=1">'
            'Documento</a></td></tr></tbody></table></div></body></html>').encode()


def primary(**kwargs):
    body = native_page(**kwargs)
    return actions.extract_primary_actions(body, source(body))


def yahoo_body(ticker='ITSA3', days=('2021-12-21',), numerator=105, denominator=100):
    splits = {}
    for day in days:
        timestamp = int(datetime.fromisoformat(day + 'T13:00:00+00:00').timestamp())
        splits[str(timestamp)] = {'date': timestamp, 'numerator': numerator,
                                  'denominator': denominator,
                                  'splitRatio': f'{numerator}:{denominator}'}
    return json.dumps({'chart': {'result': [{'meta': {
        'symbol': ticker + '.SA', 'instrumentType': 'EQUITY',
        'exchangeTimezoneName': 'America/Sao_Paulo', 'gmtoffset': -10800},
        'events': {'splits': splits, 'dividends': {'unrelated': {'amount': 6.5}}}}],
        'error': None}}).encode()


def observations(**kwargs):
    return {ticker: actions.extract_yahoo_actions(
        body := yahoo_body(ticker, **kwargs), ticker, source(body, ticker + '.json'))
        for ticker in actions.TICKERS}


def test_extracts_explicit_terms_species_tax_cost_and_executable_literal_link():
    body = native_page()
    event, = actions.extract_primary_actions(body, source(body))['events']
    assert event['declared_date'] == '2021-12-13'
    assert event['record_date'] == '2021-12-20'
    assert event['bonus_percent'] == '5'
    assert event['bonus_ratio'] == {'new_shares': 5, 'existing_shares': 100}
    assert event['share_count_factor'] == {'numerator': 105, 'denominator': 100}
    assert event['securities']['ITSA3']['share_class'] == 'ON'
    assert event['securities']['ITSA4']['share_class'] == 'PN'
    assert event['assigned_tax_cost_per_new_share']['amount'] == '18.191662'
    assert not event['assigned_tax_cost_per_new_share']['is_market_quote']
    assert event['primary_ex_date'] is None
    evidence = event['notice']['evidence']
    assert html.document_fromstring(body.decode()).xpath(evidence['xpath']) == [event['notice']['url']]
    assert evidence['sha256'] == source(body)['sha256']


@pytest.mark.parametrize(('kwargs', 'message'), [
    ({'percent': ''}, 'explicit_bonus_percentage_required'),
    ({'percent': '10%'}, 'bonus_percentage_effect_mismatch'),
    ({'effect': '5 novas acoes para cada 100 possuidas'}, 'explicit_bonus_ratio_and_same_species'),
    ({'effect': 'subscricao de 5 acoes por 100'}, 'explicit_bonus_ratio_and_same_species'),
    ({'record': '2021-12-20'}, 'explicit_DD_MM_YYYY_required'),
    ({'record': '31/02/2021'}, 'invalid_primary_calendar_date'),
    ({'record': '12/12/2021'}, 'bonus_date_order_invalid'),
])
def test_never_repairs_infers_or_reclassifies_primary_fields(kwargs, message):
    with pytest.raises(actions.EvidenceError, match=message):
        primary(**kwargs)


def test_same_species_does_not_guess_ticker_species():
    event, = primary(species=False)['events']
    assert all(item['share_class'] is None for item in event['securities'].values())
    reconciled = actions.reconcile_actions([event], observations())['events'][0]
    assert 'explicit_security_species_missing:ITSA3' in reconciled['normalization']['blockers']


def test_subscription_capital_percentage_is_not_a_bonus_or_price_adjustment():
    result = primary()
    assert len(result['events']) == 1
    subscription = result['subscription_context']
    assert subscription['declared_date'] == '2023-08-14'
    assert subscription['record_date'] == '2023-08-17'
    assert subscription['subscription_price']['amount'] == '6.50'
    assert subscription['capital_increase_percent'] == '1.39'
    assert subscription['share_count_factor'] is None
    assert subscription['price_adjustment_factor'] is None
    assert subscription['explains_price_discrepancy'] is None
    assert subscription['share_classes'] is None
    assert subscription['primary_ex_date'] is None
    assert subscription['notice']['url'].endswith('fac6b435-abe5-4eeb-8f00-eaea34902bd1?origin=1')


def test_primary_hash_and_table_section_are_required():
    body = native_page()
    with pytest.raises(actions.EvidenceError, match='primary_body_hash_mismatch'):
        actions.extract_primary_actions(body + b' ', source(body))
    body = body.replace(b'Bonificacao em Acoes', b'Subscricao em Acoes')
    with pytest.raises(actions.EvidenceError, match='action_table_section_mismatch'):
        actions.extract_primary_actions(body, source(body))


def test_missing_document_link_is_not_invented_and_duplicate_bonus_is_rejected():
    body = native_page().replace(b'href=', b'not-a-link=')
    with pytest.raises(actions.EvidenceError, match='one_literal_notice_link_required'):
        actions.extract_primary_actions(body, source(body))
    body = native_page()
    document = html.document_fromstring(body.decode())
    row = document.xpath('//table')[0].xpath('./tbody/tr')[0]
    duplicate = html.tostring(row, encoding='unicode')
    with pytest.raises(actions.EvidenceError, match='duplicate_primary_bonus'):
        primary(extra=duplicate)


def test_does_not_derive_ex_date_from_record_date_or_require_next_calendar_day():
    events = primary()['events']
    # A later observation is just an unverified candidate, never a calendar rule.
    report = actions.reconcile_actions(events, observations(days=('2021-12-27',)))
    event, = report['events']
    assert all(row['candidate']['observed_date'] == '2021-12-27'
               for row in event['yahoo_reconciliation'].values())
    assert event['primary_ex_date'] is None
    assert event['normalization']['effective_date'] is None
    assert not event['normalization']['ready']
    assert events == primary()['events']


@pytest.mark.parametrize('kwargs', [
    {'numerator': 110},
    {'days': ('2021-12-21', '2021-12-22')},
    {'days': ('2021-12-20',)},
    {'days': ()},
])
def test_ratio_mismatch_ambiguity_record_day_and_missing_observations_stay_unmatched(kwargs):
    yahoo = observations(**kwargs)
    report = actions.reconcile_actions(primary()['events'], yahoo)
    assert all(value['candidate'] is None
               for value in report['events'][0]['yahoo_reconciliation'].values())
    assert len(report['unmatched_yahoo_events']) == sum(map(len, yahoo.values()))


def test_disagreement_between_tickers_is_preserved():
    yahoo = observations()
    yahoo['ITSA4'] = observations(days=('2021-12-22',))['ITSA4']
    event, = actions.reconcile_actions(primary()['events'], yahoo)['events']
    assert 'Yahoo_dates_disagree_between_tickers' in event['normalization']['blockers']


@pytest.mark.parametrize('mutation', ['symbol', 'key', 'ratio', 'nonfinite', 'zero', 'offset'])
def test_yahoo_metadata_timestamp_and_ratio_guards(mutation):
    value = json.loads(yahoo_body())
    result = value['chart']['result'][0]
    splits = result['events']['splits']
    raw = next(iter(splits.values()))
    if mutation == 'symbol':
        result['meta']['symbol'] = 'ITSA4.SA'
    elif mutation == 'key':
        splits['0'] = splits.pop(next(iter(splits)))
    elif mutation == 'ratio':
        raw['splitRatio'] = '110:100'
    elif mutation == 'nonfinite':
        raw['numerator'] = 'NaN'
    elif mutation == 'zero':
        raw['denominator'] = 0
    else:
        result['meta'].pop('gmtoffset')
    body = json.dumps(value).encode()
    with pytest.raises(actions.EvidenceError):
        actions.extract_yahoo_actions(body, 'ITSA3', source(body))


def test_exact_native_receipt_verifies_hash_but_does_not_certify_dates(tmp_path):
    notice = primary()['events'][0]['notice']
    name = 'raw/primary-research/notices/bonus-source.json'
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    body = b'%PDF-1.7\nfixture only, no effective date proof'
    native = source(body, 'raw/primary-research/notices/bonus.pdf')
    (tmp_path / native['file']).write_bytes(body)
    receipt = {**native, 'url': notice['url'], 'http_status': 200}
    path.write_text(json.dumps(receipt))
    catalog = actions._receipt_catalog(tmp_path)
    result = actions._cached_notice(tmp_path, notice, catalog)
    assert result['status'] == 'cached_exact_pdf_requires_clause_review'
    assert not result['primary_ex_date_verified']
    (tmp_path / native['file']).write_bytes(body + b'changed')
    with pytest.raises(actions.EvidenceError, match='evidence_hash_mismatch'):
        actions._cached_notice(tmp_path, notice, catalog)


def test_other_document_redirect_shell_or_denial_is_not_the_exact_notice(tmp_path):
    notice = primary()['events'][0]['notice']
    assert actions._cached_notice(tmp_path, notice, [])['status'] == 'missing_exact_native_document'
    body = b'<html>download shell</html>'
    native = source(body, 'body.html')
    (tmp_path / 'body.html').write_bytes(body)
    receipt = {**native, 'url': notice['url'], 'http_status': 403}
    (tmp_path / 'receipt.json').write_text(json.dumps(receipt))
    result = actions._cached_notice(tmp_path, notice, [('receipt.json', receipt)])
    assert result['status'] == 'missing_exact_native_document'
    assert result['matches'][0]['status'] == 'cached_response_is_not_verified_native_pdf'
    receipt['url'] += '&invented=1'
    assert not actions._cached_notice(tmp_path, notice, [('receipt.json', receipt)])['matches']


@pytest.mark.parametrize('name', ['../current.json', 'C:/private.json', '/private.json',
                                'raw/../../current.json', 'raw\\private.json'])
def test_receipt_paths_cannot_escape_data_root(tmp_path, name):
    with pytest.raises(actions.EvidenceError, match='unsafe_evidence_path'):
        actions._path(tmp_path, name)


def test_real_cache_offline_replay_five_bonuses_six_native_documents_and_locators(monkeypatch):
    root = actions.DEFAULT_ROOT
    if not (root / actions.CALENDAR_FILE).exists():
        pytest.skip('primary research originals not installed')

    def forbidden(*args, **kwargs):
        raise AssertionError('offline read-only verifier attempted network or a write')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    original_open = Path.open

    def read_only(path, mode='r', *args, **kwargs):
        assert not any(flag in mode for flag in 'wax+')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', read_only)
    monkeypatch.setattr(Path, 'write_bytes', forbidden)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    monkeypatch.setattr(Path, 'mkdir', forbidden)
    report = actions.build_report(root)
    assert len(report['events']) == 5
    assert not report['unmatched_yahoo_events']
    assert not report['price_series_compared']
    assert not report['fitted_factors']
    assert not report['all_ready_for_normalization']
    document = html.document_fromstring((root / actions.CALENDAR_FILE).read_bytes().decode())
    for event, expected, notice_id in zip(report['events'], BONUSES, NOTICE_IDS):
        declared, record, observed, percent, numerator, denominator = expected
        assert (event['declared_date'], event['record_date'], event['bonus_percent']) == (
            declared, record, percent)
        assert event['table_index'] == 12
        assert event['notice']['url'] == actions.NOTICE_BASE + notice_id + (
            '?origin=2' if declared.startswith('2025') else '?origin=1')
        assert event['primary_ex_date'] is None
        assert event['normalization']['effective_date'] is None
        assert not event['normalization']['ready']
        assert document.xpath(event['notice']['evidence']['xpath']) == [event['notice']['url']]
        for ticker in actions.TICKERS:
            value = event['yahoo_reconciliation'][ticker]
            assert value['status'] == 'ratio_and_order_match_ex_date_unverified'
            candidate = value['candidate']
            assert candidate['observed_date'] == observed
            assert candidate['raw']['numerator'] == numerator
            assert candidate['raw']['denominator'] == denominator
            assert not value['primary_ex_date_verified']
    subscription = report['subscription_context']
    assert subscription['table_index'] == 13
    assert subscription['capital_increase_percent'] == '1.39'
    assert subscription['subscription_price']['amount'] == '6.50'
    assert subscription['share_count_factor'] is None
    assert subscription['price_adjustment_factor'] is None
    assert subscription['explains_price_discrepancy'] is None
    assert document.xpath(subscription['notice']['evidence']['xpath']) == [subscription['notice']['url']]
    assert not report['missing_documents']
    assert len(report['pending_document_reviews']) == 6
    for event in [*report['events'], subscription]:
        assert event['notice']['cache_verification']['status'] == 'cached_exact_pdf_requires_clause_review'
        assert len(event['notice']['cache_verification']['matches']) == 1
    for ticker in actions.TICKERS:
        selected = report['price_sources'][ticker]
        requested = f'raw/expanded/{ticker}-prices.json'
        assert selected['requested_file'] == requested
        if (root / requested).exists():
            assert selected['status'] == 'requested_cache'
        else:
            assert requested in report['missing_requested_price_files']
            assert selected['status'] == 'explicit_inventory_cache_substitution'
    # The public result is a JSON-compatible input for future normalization.
    assert json.loads(json.dumps(report, allow_nan=False)) == report


def test_cli_prints_json_without_output_file_option(monkeypatch, capsys):
    monkeypatch.setattr(actions, 'build_report', lambda root: {'events': [1, 2, 3, 4, 5]})
    actions.main(['--data-root', 'local-cache'])
    assert json.loads(capsys.readouterr().out) == {'events': [1, 2, 3, 4, 5]}


@pytest.mark.parametrize(('ticker', 'species'), [('ITSA3', 'ON'), ('ITSA4', 'PN')])
def test_integration_api_returns_five_actions_with_exact_ratios_sources_and_blocks(ticker, species):
    if not (actions.DEFAULT_ROOT / actions.CALENDAR_FILE).exists():
        pytest.skip('primary research originals not installed')
    identity = {'ticker': ticker, 'share_class': species, 'caller_field': 'preserved'}
    before = deepcopy(identity)
    result = actions.primary_actions(actions.DEFAULT_ROOT, identity, date(2026, 9, 30))
    assert identity == before
    assert result['identity'] == identity
    assert result['blocked']
    assert result['blockers'] == ['primary_ex_dates_unverified']
    assert len(result['actions']) == 5
    assert [row['ratio'] for row in result['actions']] == [1.05, 1.1, 1.05, 1.05, 1.02]
    assert [row['ratio_decimal'] for row in result['actions']] == ['1.05', '1.1', '1.05', '1.05', '1.02']
    assert all(row['date'] is None and row['blocked'] and row['source']['sha256'] == actions.CALENDAR_SHA
               and row['share_class'] == species for row in result['actions'])
    assert not result['missing_documents']
    assert len(result['pending_document_reviews']) == 6
    assert len(result['sources']) == 15
    assert sum(source['file'].endswith('.pdf') for source in result['sources']) == 6
    assert json.loads(json.dumps(result, allow_nan=False)) == result


def test_integration_does_not_hide_primary_rows_when_Yahoo_is_invalid(monkeypatch):
    if not (actions.DEFAULT_ROOT / actions.CALENDAR_FILE).exists():
        pytest.skip('primary research originals not installed')

    def invalid(*args, **kwargs):
        raise actions.EvidenceError('fixture_bad_comparison')

    monkeypatch.setattr(actions, 'extract_yahoo_actions', invalid)
    result = actions.primary_actions(actions.DEFAULT_ROOT, {'ticker': 'ITSA3'}, '2026-09-30')
    assert len(result['actions']) == 5
    assert result['blocked']
    assert result['price_source_selection']['status'] == 'invalid_cached_Yahoo_source'


def test_integration_rejects_other_issuers_without_reading_cache(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected cache read')

    monkeypatch.setattr(actions, 'build_report', forbidden)
    result = actions.primary_actions('unused', {'ticker': 'OTHER'}, '2026-09-30')
    assert result['blocked']
    assert result['actions'] == []
    assert result['blockers'] == ['identity_outside_ITSA3_ITSA4']


def test_integration_cutoff_and_species_conflicts_never_fabricate_dates():
    if not (actions.DEFAULT_ROOT / actions.CALENDAR_FILE).exists():
        pytest.skip('primary research originals not installed')
    result = actions.primary_actions(actions.DEFAULT_ROOT,
                                     {'ticker': 'ITSA3', 'share_class': 'PN'}, '2022-11-10')
    assert len(result['actions']) == 2
    assert all(row['date'] is None for row in result['actions'])
    assert any(code.startswith('identity_share_class_conflicts_with_primary:')
               for code in result['blockers'])
    assert result['actions'][-1]['yahoo_reconciliation']['candidate'] is None
    assert not result['missing_documents']
    assert len(result['pending_document_reviews']) == 2
