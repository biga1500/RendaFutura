from copy import deepcopy
import csv
from datetime import date
import hashlib
import json
import urllib.request

import pytest

from . import historical_provider as provider
from .collect_market_data import DividendHistory, financial_features, shift_month, stock_observations
from .historical_import import COLUMNS, ImportValidationError, load_import_bundle
from .share_basis import factor_after


CUTOFF = date(2024, 12, 31)
SPEC = (12345, 'ON', 3)
ACTIONS = [{'date': '2022-05-16', 'ratio': 2.0}]


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('The local provider must not access the network.')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)


def registry():
    return {'TEST3': {'ticker': 'TEST3', 'name': 'Exact B3 registered name', 'cvm': 12345,
                      'share_class': 'ON', 'isin': 'BRTESTACNOR0', 'sector_code': 3,
                      'industry_classification': 'Consumo nao ciclico / Test fixture'}}


def contents(*, price_basis='raw', cash_basis='record_date', actions=ACTIONS):
    source = {'schema_version': 1, 'source_name': 'Local test source, not the B3 identity',
              'source_url': 'https://example.org/manual-history', 'retrieved_at': '2025-01-03T12:00:00Z',
              'currency': 'BRL', 'price_basis': price_basis, 'cash_share_basis': cash_basis,
              'history_start': '2015-01-01', 'history_end': CUTOFF.isoformat(),
              'tax_basis': 'gross_before_individual_taxes', 'decimal_format': 'dot', 'csv_delimiter': ',',
              'tickers': {'TEST3': {'cvm': 12345, 'share_class': 'ON', 'coverage': {
                  table: {'start': '2015-01-01', 'end': CUTOFF.isoformat(), 'complete': True} for table in COLUMNS}}}}
    if price_basis == 'split_adjusted':
        source['price_basis_date'] = CUTOFF.isoformat()
    if cash_basis == 'adjusted':
        source['cash_share_basis_date'] = CUTOFF.isoformat()
    tables = {table: [] for table in COLUMNS}
    for offset in reversed(range(61)):
        day = shift_month(CUTOFF, -offset)
        close = 10 * factor_after(day, actions, CUTOFF) if price_basis == 'raw' else 10
        tables['prices'].append({'ticker': 'TEST3', 'date': day.isoformat(), 'close': close})
    for year in range(2016, 2025):
        record = date(year, 2, 1)
        amount = factor_after(record, actions, CUTOFF) if cash_basis == 'record_date' else 1
        tables['payments'].append({'ticker': 'TEST3', 'record_date': record.isoformat(),
                                   'payment_date': date(year, 3, 15).isoformat(),
                                   'gross_per_share': amount, 'label': 'DIVIDENDO'})
    for action in actions:
        tables['actions'].append({'ticker': 'TEST3', 'effective_date': action['date'],
                                  'numerator': int(action['ratio'] * 10), 'denominator': 10,
                                  'type': 'split' if action['ratio'] > 1 else 'reverse_split'})
    for year in range(2015, 2024):
        basis = date(year, 12, 31)
        factor = factor_after(basis, actions, CUTOFF)
        tables['fundamentals'].append({'ticker': 'TEST3', 'cvm': 12345, 'share_class': 'ON',
                                       'fiscal_end': basis.isoformat(), 'published_at': date(year + 1, 3, 1).isoformat(),
                                       'share_basis_date': basis.isoformat(), 'eps': 2 * factor,
                                       'free_cash_per_share': 1.5 * factor, 'liabilities_to_assets': .5,
                                       'sector_code': 3, 'scope': 'consolidated',
                                       'source_ref': f'https://example.org/filing/{year}'})
    return source, tables


def write_bundle(folder, source, tables):
    folder.mkdir(exist_ok=True)
    (folder / 'source.json').write_text(json.dumps(source), encoding='utf8')
    for table, rows in tables.items():
        with (folder / (table + '.csv')).open('w', encoding='utf8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS[table])
            writer.writeheader()
            writer.writerows(rows)
    return folder


@pytest.fixture
def local_bundle(tmp_path):
    return write_bundle(tmp_path / 'manual', *contents())


def file_hashes(folder):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.iterdir() if path.is_file()}


def clone_ticker(source, tables, ticker, *, cvm=12345, share_class='PN', sector=3):
    source['tickers'][ticker] = deepcopy(source['tickers']['TEST3'])
    source['tickers'][ticker].update(cvm=cvm, share_class=share_class)
    for table in COLUMNS:
        copies = [{**row, 'ticker': ticker} for row in tables[table] if row['ticker'] == 'TEST3']
        if table == 'fundamentals':
            for row in copies:
                row.update(cvm=cvm, share_class=share_class, sector_code=sector)
                if sector == 2:
                    row['free_cash_per_share'] = ''
                else:
                    row['eps'] *= 2
        tables[table].extend(copies)


def test_candidate_and_exact_statement_contract_with_real_feature_functions(local_bundle):
    before = file_hashes(local_bundle)
    b3 = registry()
    candidates, grouped, excluded, manifest = provider.load_historical_candidates(local_bundle, CUTOFF, b3)
    assert excluded == []
    assert set(grouped) == {SPEC}
    stock = candidates[0]
    assert {key: stock[key] for key in b3['TEST3']} == b3['TEST3']
    assert stock['statement_spec'] == SPEC
    assert isinstance(stock['payments'], DividendHistory)
    assert stock['payments'].basis == 'payment'
    assert len(stock['prices']) == 61
    assert all(quote[1:] == (10, 10) for quote in stock['prices'].values())
    assert all(amount == 1 for _, _, amount in stock['payments'])
    assert all(row['profit_per_share'] == 2 for row in grouped[SPEC])
    assert all(row['free_cash_per_share'] == 1.5 for row in grouped[SPEC])
    features, filing = financial_features(grouped[SPEC], stock['payments'], CUTOFF, SPEC)
    assert features == pytest.approx([1, 1 / 12, 2, 1.5, .5, .5, 3])
    assert filing['filing_id'] == 'https://example.org/filing/2023'
    assert filing['version'] == '1'
    rows, coverage = stock_observations('TEST3', stock['prices'], stock['payments'], grouped[SPEC], CUTOFF, spec=SPEC)
    assert stock['observations'] == rows
    assert stock['observation_coverage'] == coverage
    assert len(rows) == 49
    assert all(row['label_end'] <= CUTOFF.isoformat() for row in rows)
    assert stock['independent_cash_matches'] == stock['independent_price_matches'] == []
    assert not manifest['independently_reconciled']
    assert manifest['must_not_merge_official_versions']
    assert manifest['bundle_provenance']['input_files']
    assert {row['file']: row['sha256'] for row in manifest['source_manifest']} == before
    assert file_hashes(local_bundle) == before
    assert not (local_bundle / 'current.json').exists()


@pytest.mark.parametrize('price_basis', ['raw', 'split_adjusted'])
@pytest.mark.parametrize('cash_basis', ['record_date', 'adjusted'])
def test_price_and_cash_bases_converge_without_double_adjustment(tmp_path, price_basis, cash_basis):
    folder = write_bundle(tmp_path / 'manual', *contents(price_basis=price_basis, cash_basis=cash_basis))
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    stock = stocks[0]
    assert [value[2] for value in stock['prices'].values()] == [10.] * 61
    assert [amount for _, _, amount in stock['payments']] == [1.] * 9
    assert all(row['profit_per_share'] == 2 for row in grouped[SPEC])
    assert stock['share_normalization']['price_source_basis'] == price_basis
    assert stock['share_normalization']['cash_source_basis'] == cash_basis


def test_cash_scaling_uses_record_date_but_keeps_actual_later_payment_date(tmp_path):
    source, tables = contents()
    row = next(row for row in tables['payments'] if row['record_date'] == '2022-02-01')
    row.update(record_date='2022-04-01', payment_date='2022-06-15', gross_per_share=2)
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, _, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    row = next(row for row in stocks[0]['ledger'] if row['record_date'] == '2022-04-01')
    assert row['payment_date'] == '2022-06-15'
    assert row['gross_per_unit'] == 1
    assert (date(2022, 6, 15), date(2022, 6, 15), 1) in stocks[0]['payments']


def test_future_installments_retained_separately_not_paid(tmp_path):
    source, tables = contents()
    future = {'ticker': 'TEST3', 'record_date': '2024-12-01', 'payment_date': '2025-06-01',
              'gross_per_share': 999, 'label': 'JCP'}
    tables['payments'].append(future)
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    assert stocks[0]['payments'].future_events == 1
    assert len(stocks[0]['payments']) == len(stocks[0]['ledger']) == 9
    assert stocks[0]['future_payments'][0]['gross_per_share'] == 999
    assert sum(amount for day, _, amount in stocks[0]['payments'] if day.year == 2024) == 1
    assert financial_features(grouped[SPEC], stocks[0]['payments'], CUTOFF, SPEC)[0][0] == 1


def test_exact_specs_isolate_share_classes_and_financial_sector_null_fcf(tmp_path):
    source, tables = contents()
    clone_ticker(source, tables, 'TEST4')
    clone_ticker(source, tables, 'BANK3', cvm=54321, share_class='ON', sector=2)
    b3 = registry()
    b3['TEST4'] = {**b3['TEST3'], 'ticker': 'TEST4', 'name': 'B3 preferred class',
                   'share_class': 'PN', 'isin': 'BRTESTACNPR8'}
    b3['BANK3'] = {**b3['TEST3'], 'ticker': 'BANK3', 'name': 'B3 financial issuer',
                   'cvm': 54321, 'sector_code': 2, 'isin': 'BRBANKACNOR1'}
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, b3)
    assert excluded == []
    assert set(grouped) == {SPEC, (12345, 'PN', 3), (54321, 'ON', 2)}
    for stock in stocks:
        spec = stock['statement_spec']
        assert all((r['cvm'], r['share_class'], r['sector_code']) == spec for r in grouped[spec])
        features, _ = financial_features(grouped[spec], stock['payments'], CUTOFF, spec)
        assert features[2] == (4 if stock['ticker'] == 'TEST4' else 2)
        if spec[2] == 2:
            assert features[3] is None
        else:
            assert features[3] == 1.5


@pytest.mark.parametrize('changes,reason', [
    ({'ticker': 'TEST4'}, 'exact_B3_ticker_mapping_unavailable'),
    ({'cvm': None}, 'incomplete_B3_instrument_identity'),
    ({'name': ''}, 'incomplete_B3_instrument_identity'),
    ({'isin': ''}, 'incomplete_B3_instrument_identity'),
    ({'share_class': 'UNIT'}, 'unsupported_B3_share_class'),
    ({'sector_code': 10}, 'B3_sector_code_unavailable'),
    ({'industry_classification': float('nan')}, 'invalid_B3_industry_classification'),
])
def test_exact_b3_identity_is_required_not_inferred(local_bundle, changes, reason):
    b3 = registry()
    b3['TEST3'].update(changes)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(local_bundle, CUTOFF, b3)
    assert stocks == [] and grouped == {}
    assert excluded == [{'ticker': 'TEST3', 'reason': reason}]


def test_missing_b3_ticker_does_not_fall_back_to_another_class(local_bundle):
    b3 = {'TEST4': {**registry()['TEST3'], 'ticker': 'TEST4'}}
    assert provider.load_historical_candidates(local_bundle, CUTOFF, b3)[2] == [
        {'ticker': 'TEST3', 'reason': 'exact_B3_ticker_mapping_unavailable'}]


def test_b3_cvm_string_is_parsed_without_creating_an_identifier(local_bundle):
    b3 = registry()
    b3['TEST3']['cvm'] = '0012345'
    assert provider.load_historical_candidates(local_bundle, CUTOFF, b3)[0][0]['cvm'] == 12345


def test_share_class_is_supplied_by_registry_not_guessed_from_suffix(tmp_path):
    source, tables = contents()
    source['tickers']['TEST7'] = source['tickers'].pop('TEST3')
    source['tickers']['TEST7']['share_class'] = 'PNC'
    for rows in tables.values():
        for row in rows:
            row['ticker'] = 'TEST7'
            if 'share_class' in row:
                row['share_class'] = 'PNC'
    b3 = {'TEST7': {**registry()['TEST3'], 'ticker': 'TEST7', 'share_class': 'PNC'}}
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, b3)
    assert excluded == []
    assert stocks[0]['share_class'] == 'PNC'
    assert set(grouped) == {(12345, 'PNC', 3)}


def test_manual_identity_mismatch_is_excluded(local_bundle):
    b3 = registry()
    b3['TEST3']['cvm'] = 99999
    assert provider.load_historical_candidates(local_bundle, CUTOFF, b3)[2][0]['reason'] == 'declared_issuer_or_share_class_disagrees_with_B3'


def test_fundamental_sector_must_match_exact_b3_spec(tmp_path):
    source, tables = contents()
    for row in tables['fundamentals']:
        row['sector_code'] = 4
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'financial_statement_spec_disagrees_with_B3'


def test_duplicate_instrument_or_exact_spec_does_not_double_count(tmp_path):
    source, tables = contents()
    clone_ticker(source, tables, 'OTHR3', share_class='ON')
    b3 = registry()
    b3['OTHR3'] = {**b3['TEST3'], 'ticker': 'OTHR3'}
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, b3)
    assert stocks == [] and grouped == {}
    assert len(excluded) == 2
    assert {r['reason'] for r in excluded} == {'ambiguous_B3_instrument_identity'}


@pytest.mark.parametrize('table', list(COLUMNS))
def test_missing_tables_are_exclusions_not_zero_series(local_bundle, table):
    (local_bundle / (table + '.csv')).unlink()
    stocks, grouped, excluded, _ = provider.load_historical_candidates(local_bundle, CUTOFF, registry())
    assert stocks == [] and grouped == {}
    assert excluded == [{'ticker': 'TEST3', 'reason': 'missing_' + table + '_table'}]


@pytest.mark.parametrize('table', list(COLUMNS))
def test_incomplete_coverage_rejected_even_with_all_observed_rows(local_bundle, table):
    source = json.loads((local_bundle / 'source.json').read_text(encoding='utf8'))
    source['tickers']['TEST3']['coverage'][table]['complete'] = False
    (local_bundle / 'source.json').write_text(json.dumps(source), encoding='utf8')
    assert provider.load_historical_candidates(local_bundle, CUTOFF, registry())[2][0]['reason'] == 'incomplete_' + table + '_coverage'


@pytest.mark.parametrize('table,anchor', [('prices', 'date'), ('payments', 'payment_date'),
                                       ('actions', 'effective_date'), ('fundamentals', 'published_at')])
def test_coverage_start_must_precede_60_month_window(tmp_path, table, anchor):
    source, tables = contents()
    source['tickers']['TEST3']['coverage'][table]['start'] = '2020-01-01'
    tables[table] = [row for row in tables[table] if row[anchor] >= '2020-01-01']
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'insufficient_' + table + '_coverage_window'


def test_missing_interior_month_cannot_be_filled(tmp_path):
    source, tables = contents()
    del tables['prices'][25]
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'less_than_61_consecutive_prices'


def test_latest_observed_quote_selected_without_fabricated_month_end_date(tmp_path):
    source, tables = contents()
    tables['prices'][-1]['date'] = '2024-12-30'
    tables['prices'].append({'ticker': 'TEST3', 'date': '2024-12-02', 'close': 999})
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[0][0]['prices'][CUTOFF] == (date(2024, 12, 30), 10, 10)


def test_stale_cutoff_month_quote_excluded(tmp_path):
    source, tables = contents()
    tables['prices'][-1]['date'] = '2024-12-01'
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'stale_month_end_price'


@pytest.mark.parametrize('table', ['payments', 'fundamentals'])
def test_empty_cash_or_accounts_do_not_become_zero(tmp_path, table):
    source, tables = contents()
    tables[table] = []
    folder = write_bundle(tmp_path / 'manual', source, tables)
    expected = 'paid_payment_history_unavailable' if table == 'payments' else 'financial_statements_unavailable'
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == expected


def test_declared_complete_empty_actions_is_explicit_no_action_history(tmp_path):
    folder = write_bundle(tmp_path / 'manual', *contents(actions=[]))
    stocks, _, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    assert stocks[0]['share_actions'] == []


def test_reverse_split_and_bonus_use_existing_factor_contract(tmp_path):
    actions = [{'date': '2021-04-01', 'ratio': .1}, {'date': '2023-05-01', 'ratio': 1.2}]
    source, tables = contents(actions=actions)
    tables['actions'][-1]['type'] = 'bonus'
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    assert [row[2] for row in stocks[0]['prices'].values()] == pytest.approx([10] * 61)
    assert [row[2] for row in stocks[0]['payments']] == pytest.approx([1] * 9)
    assert [row['profit_per_share'] for row in grouped[SPEC]] == pytest.approx([2] * 9)


def test_action_chain_must_cover_actual_financial_basis_dates(tmp_path):
    source, tables = contents()
    source['tickers']['TEST3']['coverage']['actions']['start'] = '2019-12-31'
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'share_action_coverage_does_not_cover_basis'


def test_already_normalized_financial_values_not_adjusted_twice(tmp_path):
    source, tables = contents()
    for row in tables['fundamentals']:
        row.update(share_basis_date=CUTOFF.isoformat(), eps=2, free_cash_per_share=1.5)
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert {r['profit_per_share'] for r in provider.load_historical_candidates(folder, CUTOFF, registry())[1][SPEC]} == {2}


def test_publication_order_versions_respect_historical_availability(tmp_path):
    source, tables = contents()
    row = next(r for r in tables['fundamentals'] if r['fiscal_end'] == '2023-12-31')
    tables['fundamentals'].append({**row, 'published_at': '2024-09-01', 'eps': 4,
                                   'source_ref': 'https://example.org/filing/2023-revised'})
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, grouped, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    before = financial_features(grouped[SPEC], stocks[0]['payments'], date(2024, 6, 30), SPEC)
    after = financial_features(grouped[SPEC], stocks[0]['payments'], CUTOFF, SPEC)
    assert (before[0][2], before[1]['version']) == (2, '1')
    assert (after[0][2], after[1]['version']) == (4, '2')
    assert after[1]['financial_method']['version_basis'] == 'imported_publication_order_not_official_CVM_version'


def test_future_financial_publication_rejected_even_if_adapter_called_directly(local_bundle):
    bundle = load_import_bundle(local_bundle, CUTOFF)
    bundle['fundamentals'][-1]['published_at'] = '2025-01-01'
    assert provider.adapt_import_bundle(bundle, registry())[2][0]['reason'] == 'future_or_invalid_financial_publication'


def test_missing_payment_date_blocked_by_loader_not_inferred(tmp_path):
    source, tables = contents()
    tables['payments'][-1]['payment_date'] = ''
    folder = write_bundle(tmp_path / 'manual', source, tables)
    with pytest.raises(ImportValidationError, match='expected_ISO_date'):
        provider.load_historical_candidates(folder, CUTOFF, registry())


def test_dividend_adjusted_close_basis_cannot_be_reinterpreted(local_bundle):
    bundle = load_import_bundle(local_bundle, CUTOFF)
    bundle['provenance']['source']['price_basis'] = 'adjusted_close'
    with pytest.raises(provider.HistoricalProviderError, match='unsupported_or_total_return_share_basis'):
        provider.adapt_import_bundle(bundle, registry())


def test_already_adapted_bundle_cannot_be_normalized_again(local_bundle):
    bundle = load_import_bundle(local_bundle, CUTOFF)
    bundle['provenance']['share_adjustments_applied'] = True
    with pytest.raises(provider.HistoricalProviderError, match='unmodified_validated'):
        provider.adapt_import_bundle(bundle, registry())


def test_stock_coupons_are_not_silently_treated_as_dividends(tmp_path):
    source, tables = contents()
    tables['payments'][-1]['label'] = 'CUPOM'
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'unsupported_equity_payment_label'


def test_zero_eps_is_not_replaced_by_previous_positive_statement(tmp_path):
    source, tables = contents()
    tables['fundamentals'][-1]['eps'] = 0
    folder = write_bundle(tmp_path / 'manual', source, tables)
    assert provider.load_historical_candidates(folder, CUTOFF, registry())[2][0]['reason'] == 'current_financial_features_unavailable'


def test_coverage_blockers_prevent_unobserved_early_zero_cash(local_bundle):
    stocks, grouped, _, _ = provider.load_historical_candidates(local_bundle, CUTOFF, registry())
    history = stocks[0]['payments']
    assert history.blockers(date(2014, 12, 31), date(2015, 12, 31))
    assert financial_features(grouped[SPEC], history, date(2015, 12, 31), SPEC) is None
    assert not history.blockers(date(2023, 12, 31), CUTOFF)


def test_exact_60_month_coverage_limits_training_windows(tmp_path):
    source, tables = contents()
    start = shift_month(CUTOFF, -60).isoformat()
    for coverage in source['tickers']['TEST3']['coverage'].values():
        coverage['start'] = start
    tables['payments'] = [r for r in tables['payments'] if r['payment_date'] >= start]
    tables['fundamentals'] = [r for r in tables['fundamentals'] if r['published_at'] >= start]
    folder = write_bundle(tmp_path / 'manual', source, tables)
    stocks, _, excluded, _ = provider.load_historical_candidates(folder, CUTOFF, registry())
    assert excluded == []
    assert stocks[0]['training_since'] == date(2020, 12, 31)
    assert len(stocks[0]['observations']) == 37
    assert all(row['as_of'] >= '2020-12-31' for row in stocks[0]['observations'])


@pytest.mark.parametrize('table,field,reason', [
    ('prices', 'date', 'future_training_price'),
    ('actions', 'effective_date', 'future_share_action'),
    ('fundamentals', 'share_basis_date', 'future_financial_share_basis'),
])
def test_future_dates_cannot_enter_adapted_history(local_bundle, table, field, reason):
    bundle = load_import_bundle(local_bundle, CUTOFF)
    bundle[table][-1][field] = '2025-01-01'
    assert provider.adapt_import_bundle(bundle, registry())[2][0]['reason'] == reason


def test_adapter_does_not_mutate_bundle_or_registry_or_log_rows(local_bundle, capsys):
    bundle = load_import_bundle(local_bundle, CUTOFF)
    b3 = registry()
    bundle_before, b3_before = deepcopy(bundle), deepcopy(b3)
    provider.adapt_import_bundle(bundle, b3)
    assert bundle == bundle_before
    assert b3 == b3_before
    logged = capsys.readouterr()
    assert logged.out == logged.err == ''


def test_none_folder_is_explicit_optional_noop(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No bundle was configured.')
    monkeypatch.setattr(provider, 'load_import_bundle', forbidden)
    stocks, grouped, excluded, manifest = provider.load_historical_candidates(None, CUTOFF, {})
    assert stocks == excluded == [] and grouped == {}
    assert manifest['status'] == 'not_configured'


def test_cutoff_must_be_month_end(local_bundle):
    with pytest.raises(provider.HistoricalProviderError, match='cutoff_must_be_ISO_month_end'):
        provider.load_historical_candidates(local_bundle, '2024-12-30', registry())
