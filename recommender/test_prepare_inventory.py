import csv
from datetime import date, timedelta
import hashlib
import json

import pytest

from . import inventory_candidates, inventory_financials, prepare_inventory
from .collect_income_data import PAYMENT_TARGET, payment_in_window
from .collect_market_data import DividendHistory, month_end, shift_month, write_csv, write_json
from .datasets import FEATURES, real_dataset
from .market_inventory import load_inventory, publish_inventory


CUTOFF = shift_month(month_end(date.today().year, date.today().month), -1)
SPECS = {'DROP3': (12344, 'ON', 1), 'TEST3': (12345, 'ON', 1), 'TEST4': (12346, 'PN', 1)}


def test_positive_dy_scope_restricts_preparation_without_erasing_catalog(tmp_path, monkeypatch):
    from .dividend_scope import OUTSIDE_REASON, import_scope
    baseline(tmp_path, monkeypatch)
    candidates, statements = approve(tmp_path, monkeypatch)
    supplied = tmp_path / 'positive-dy.txt'
    supplied.write_text('Papel\tDiv.Yield\nTEST3\t1,00%\nTEST4\t0,00%\n', encoding='utf-8')
    scope = import_scope(tmp_path, supplied)

    def selected_candidates(root, inventory, cutoff):
        assert [row['ticker'] for row in inventory['stocks']] == ['TEST3']
        return [row for row in candidates if row['ticker'] == 'TEST3'], [], []

    def selected_financials(root, rows, cutoff):
        assert [row['ticker'] for row in rows] == ['TEST3']
        return [row for row in statements if row['cvm'] == SPECS['TEST3'][0]], [], []

    monkeypatch.setattr(inventory_candidates, 'build_candidates', selected_candidates)
    monkeypatch.setattr(inventory_financials, 'prepare_financials', selected_financials)
    pointer_before = (tmp_path / 'current.json').read_bytes()
    snapshot, report = prepare_inventory.prepare(tmp_path)
    prepared = report['stock_universe_preparation']
    assert prepared['ready_stocks'] == 1
    assert prepared['evaluated_stocks'] == 3
    assert prepared['dividend_yield_selection']['id'] == scope['id']
    assert prepared['dividend_yield_selection']['selectedPendingStocks'] == 0
    assert all(row['reason_codes'] == [OUTSIDE_REASON] for row in prepared['stocks'] if row['ticker'] != 'TEST3')
    assert (tmp_path / 'current.json').read_bytes() == pointer_before
    supplied.write_text('Papel\tDiv.Yield\nTEST3\t0,00%\nTEST4\t1,00%\n', encoding='utf-8')
    import_scope(tmp_path, supplied)
    with pytest.raises(ValueError, match='selecao de dividend yield mudou'):
        prepare_inventory.activate_candidate(tmp_path, snapshot)
    assert (tmp_path / 'current.json').read_bytes() == pointer_before


def test_known_native_eps_conflict_cannot_be_promoted_after_cash_repair(tmp_path, monkeypatch):
    from . import wlm_financial_class_research
    baseline(tmp_path, monkeypatch)
    candidates, statements = approve(tmp_path, monkeypatch, tickers=('TEST3',))
    candidates[0]['cvm'] = 11070
    for row in statements:
        row.update(cvm=11070, filing_id='155797')
    calls = []

    def frozen_audit(folder, cutoff):
        calls.append((folder, cutoff))
        return {'sources': [], 'actual_reported_ON_EPS_reconciles_with_note': False,
                'actual_reported_PN_EPS_reconciles_with_note': False}

    monkeypatch.setattr(wlm_financial_class_research, 'audit', frozen_audit)
    _, report = prepare_inventory.prepare(tmp_path)
    result = next(row for row in report['stock_universe_preparation']['stocks'] if row['ticker'] == 'TEST3')
    assert not result['model_ready']
    assert result['reason_codes'] == ['native_financial_eps_source_conflict']
    assert result['source_checks']['financial_source_conflict']['replacement_EPS_created'] is False
    assert calls == [(tmp_path, CUTOFF)]


def table(path):
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def observation(ticker):
    as_of = shift_month(CUTOFF, -24)
    return {'ticker': ticker, 'as_of': as_of.isoformat(), 'published_at': as_of.isoformat(),
            'label_end': shift_month(as_of, 12).isoformat(), **dict(zip(FEATURES, [9, 1, 10, 8, .9, .4, 1])),
            'target': 99, 'fiscal_end': shift_month(as_of, -12).isoformat(), 'filing_id': 'old-fixture',
            'forecast_target_id': PAYMENT_TARGET['id'], 'event_basis': 'payment', 'event_date_field': 'paymentDate',
            'previous_window_start': shift_month(as_of, -12).isoformat(), 'features_available_at': as_of.isoformat(),
            'audit_note': 'Preserve only the non-stock observation.'}


def baseline(folder, monkeypatch):
    base = folder / 'snapshots/prior-fixture'
    base.mkdir(parents=True)
    write_json(folder / 'current.json', {'snapshot': 'snapshots/prior-fixture'})
    assets = [{'ticker': ticker, 'name': 'Prior ' + ticker, 'asset_class': kind,
               'risk_level': 2 if kind == 'acoes' else 0, 'liquidity_days': 2,
               'minimum_years': 5 if kind == 'acoes' else 0, 'price': 99,
               'features': [9, 1, 10, 8, .9, .4, 1] if kind == 'acoes' else None,
               'as_of': CUTOFF.isoformat(), 'source': 'Synthetic isolated test fixture.'}
              for ticker, kind in [('DROP3', 'acoes'), ('FIXA-A', 'renda_fixa'),
                                   ('TEST3', 'acoes'), ('CAIXA-A', 'liquidez')]]
    dates = [shift_month(CUTOFF, offset).isoformat() for offset in range(-59, 1)]
    returns = [{'date': day, 'inflation': .003, 'DROP3': .01, 'FIXA-A': .004, 'TEST3': .02,
                'CAIXA-A': .005, 'official_index': '100.000'} for day in dates]
    cash = [{'date': day, 'DROP3': .001, 'FIXA-A': .002, 'TEST3': .002,
             'CAIXA-A': 0, 'official_calendar': 'unchanged'} for day in dates]
    observations = [observation(ticker) for ticker in ('TEST3', 'FIXA-A', 'DROP3')]
    ledger = [{'ticker': ticker, 'payment_date': CUTOFF.isoformat(), 'gross_per_unit': 99,
               'status': 'included', 'source': 'prior-' + ticker} for ticker in ('DROP3', 'TEST3', 'FIXA-A')]
    write_csv(base / 'returns.csv', list(returns[0]), returns)
    write_csv(base / 'cashflows.csv', list(cash[0]), cash)
    write_csv(base / 'observations.csv', list(observations[0]), observations)
    write_json(base / 'payment-ledger.json', ledger)
    write_json(base / 'assets.json', {'assets': assets, 'source': 'Synthetic isolated test fixture.',
        'sources': [], 'reference_date': CUTOFF.isoformat(), 'returns_include_distributions': True,
        'forecast_target': PAYMENT_TARGET, 'cashflow_basis': 'verified_paid_cash',
        'cashflow_excludes_principal': True,
        'cashflow_source': {'url': 'https://example.test/official-cash', 'sha256': 'a' * 64},
        'cashflows_sha256': hashlib.sha256((base / 'cashflows.csv').read_bytes()).hexdigest(),
        'payment_ledger_sha256': hashlib.sha256((base / 'payment-ledger.json').read_bytes()).hexdigest(),
        'bonds': [{'ticker': 'FIXA-A', 'maturity': '2040-01-01'}],
        'expanded_stocks': [{'ticker': 'DROP3', 'filing_id': 'obsolete'}, {'ticker': 'TEST3', 'filing_id': 'obsolete'}]})
    write_json(base / 'collection-report.json', {'reference_date': CUTOFF.isoformat()})
    inventory = {'schema_version': 1, 'as_of': date.today().isoformat(), 'counts': {'stocks': len(SPECS)},
        'treasury': [], 'stocks': [
            {'ticker': ticker, 'name': 'Synthetic preparation fixture', 'cvm': spec[0],
             'share_class': spec[1], 'sector_code': spec[2], 'model_ready': ticker != 'TEST4',
             'quote': {'value': 10, 'status': 'available'}, 'history': {},
             'payments': {'status': 'partial'}, 'fundamentals': {'status': 'partial', 'fields': {}},
             'identity_status': 'official_cvm_fca'} for ticker, spec in SPECS.items()]}
    publish_inventory(folder, inventory)
    monkeypatch.setattr(prepare_inventory, 'dataset_forecast', lambda dataset: (None, {'fixture': True}))

    def build_candidates(root, supplied_inventory, cutoff):
        assert root == folder and cutoff == CUTOFF
        assert supplied_inventory == inventory
        return [], [{'ticker': ticker, 'reason': 'independent_cash_cache_missing'} for ticker in SPECS], []

    def financials(root, stocks, cutoff):
        assert {row['ticker'] for row in stocks} == set(SPECS)
        return [], [{'kind': 'candidate', 'ticker': row['ticker'], 'status': 'excluded'} for row in stocks], []

    monkeypatch.setattr(inventory_candidates, 'build_candidates', build_candidates)
    monkeypatch.setattr(inventory_financials, 'prepare_financials', financials)
    return base


def approve(folder, monkeypatch, tickers=('TEST3', 'TEST4')):
    candidates, statements = [], []
    for ticker in tickers:
        cvm, share, sector = SPECS[ticker]
        prices = {shift_month(CUTOFF, offset): (shift_month(CUTOFF, offset), 20 + offset / 10, 20 + offset / 10)
                  for offset in range(-60, 1)}
        days = [shift_month(CUTOFF, offset).replace(day=1) for offset in range(-72, 2)]
        payments = DividendHistory(CUTOFF, len(days), basis='payment')
        payments.extend((day, day, .2) for day in days)
        ledger = [{'ticker': ticker, 'payment_date': day.isoformat(), 'gross_per_unit': .2,
                   'status': 'included', 'published_at': None,
                   'availability_basis': 'payment_date_retrospective_observation_not_publication',
                   'point_in_time_archive_verified': False} for day in days]
        candidates.append({'ticker': ticker, 'name': 'Rebuilt ' + ticker, 'cvm': cvm, 'share_class': share,
            'sector_code': sector, 'prices': prices, 'payments': payments, 'ledger': ledger,
            'financial_basis_start': shift_month(CUTOFF, -72).isoformat(),
            'training_since': shift_month(CUTOFF, -48), 'price_url': 'https://example.test/' + ticker,
            'checks': {'prices': '61_consecutive_monthly_anchors'}})
        statements.extend({'cvm': cvm, 'share_class': share, 'version': 1,
            'fiscal_end': date(year, 12, 31).isoformat(), 'published_at': date(year + 1, 3, 31).isoformat(),
            'filing_id': f'new-{ticker}-{year}', 'profit_per_share': 5, 'free_cash_per_share': 4,
            'debt_ratio': .4} for year in range(CUTOFF.year - 7, CUTOFF.year))

    def build_candidates(root, supplied_inventory, cutoff):
        assert root == folder and cutoff == CUTOFF
        assert {row['ticker'] for row in supplied_inventory['stocks']} == set(SPECS)
        return candidates, [{'ticker': ticker, 'reason': 'independent_cash_cache_missing'}
                            for ticker in SPECS if ticker not in tickers], []

    monkeypatch.setattr(inventory_candidates, 'build_candidates', build_candidates)
    monkeypatch.setattr(inventory_financials, 'prepare_financials', lambda *args: (statements, [], []))
    return candidates, statements


def test_rejected_legacy_stocks_are_removed_everywhere_without_publication(tmp_path, monkeypatch):
    base = baseline(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    snapshot, report = prepare_inventory.prepare(tmp_path)
    data = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    preparation = data['stock_universe_preparation']
    assert preparation['evaluated_stocks'] == 3 and preparation['ready_stocks'] == 0
    assert preparation['pending_stocks'] == 3 and preparation['added_stocks'] == []
    assert preparation['removed_stocks'] == ['DROP3', 'TEST3'] and preparation['revalidated_stocks'] == []
    assert all(not row['model_ready'] and row['preparation_basis'] == 'pending' for row in preparation['stocks'])
    assert report['excluded_stocks'] == [{'ticker': ticker, 'reason': 'independent_cash_cache_missing'} for ticker in SPECS]
    assert [asset['ticker'] for asset in data['assets']] == ['FIXA-A', 'CAIXA-A']
    assert data['expanded_stocks'] == []
    for name in ('returns.csv', 'cashflows.csv'):
        fields, _ = table(snapshot / name)
        assert not {'DROP3', 'TEST3'} & set(fields)
    assert {row['ticker'] for row in table(snapshot / 'observations.csv')[1]} == {'FIXA-A'}
    assert {row['ticker'] for row in json.loads((snapshot / 'payment-ledger.json').read_text())} == {'FIXA-A'}
    assert all(path.read_bytes() == body for path, body in before.items())
    assert report['base_snapshot'] == base.name


def test_approved_legacy_and_new_stocks_are_rebuilt_and_non_stocks_preserved(tmp_path, monkeypatch):
    base = baseline(tmp_path, monkeypatch)
    candidates, _ = approve(tmp_path, monkeypatch)
    candidates.reverse()
    snapshot, report = prepare_inventory.prepare(tmp_path)
    data = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    prior = json.loads((base / 'assets.json').read_text(encoding='utf-8'))
    preparation = data['stock_universe_preparation']
    for record in (preparation, report):
        assert record['added_stocks'] == ['TEST4']
        assert record['removed_stocks'] == ['DROP3']
        assert record['revalidated_stocks'] == ['TEST3']
    assert preparation['ready_stocks'] == 2 and preparation['pending_stocks'] == 1
    assert data['assets'][:2] == [asset for asset in prior['assets'] if asset['asset_class'] != 'acoes']
    assert data['bonds'] == prior['bonds']
    for asset in data['assets'][2:]:
        assert asset['name'] == 'Rebuilt ' + asset['ticker'] and asset['price'] == 20
        assert asset['features'][0] == pytest.approx(2.4) and asset['features'][2] == 5
        assert asset['current_quote']['purchase_available'] is False
    assert {row['ticker'] for row in data['expanded_stocks']} == {'TEST3', 'TEST4'}
    assert all(row['filing_id'].startswith('new-') for row in data['expanded_stocks'])
    for name in ('returns.csv', 'cashflows.csv'):
        old_fields, old_rows = table(base / name)
        fields, rows = table(snapshot / name)
        preserved = [field for field in old_fields if field not in ('DROP3', 'TEST3')]
        assert fields == [*preserved, 'TEST3', 'TEST4']
        assert [{field: row[field] for field in preserved} for row in rows] == [
            {field: row[field] for field in preserved} for row in old_rows]
        assert [row['date'] for row in rows] == sorted(row['date'] for row in rows)
        for row in rows:
            day = date.fromisoformat(row['date'])
            prices = candidates[0]['prices']
            start, end = prices[shift_month(day, -1)][2], prices[day][2]
            expected = .2 / start + (end / start - 1 if name == 'returns.csv' else 0)
            assert float(row['TEST3']) == pytest.approx(expected)
            assert row['TEST3'] == row['TEST4']
    fields, observations = table(snapshot / 'observations.csv')
    assert fields == table(base / 'observations.csv')[0]
    assert [row for row in observations if row['ticker'] == 'FIXA-A'] == [
        row for row in table(base / 'observations.csv')[1] if row['ticker'] == 'FIXA-A']
    assert [(row['as_of'], row['ticker']) for row in observations] == sorted(
        (row['as_of'], row['ticker']) for row in observations)
    training = [row for row in observations if row['ticker'] != 'FIXA-A']
    assert {row['ticker'] for row in training} == {'TEST3', 'TEST4'}
    assert all(row['filing_id'].startswith('new-') and float(row['target']) == pytest.approx(2.4) for row in training)
    assert all(row['forecast_target_id'] == PAYMENT_TARGET['id'] and row['event_basis'] == 'payment'
               and row['event_date_field'] == 'paymentDate' for row in training)
    ledger = json.loads((snapshot / 'payment-ledger.json').read_text())
    assert [row for row in ledger if row['ticker'] == 'FIXA-A'] == [
        row for row in json.loads((base / 'payment-ledger.json').read_text()) if row['ticker'] == 'FIXA-A']
    first = date.fromisoformat(table(base / 'returns.csv')[1][0]['date'])
    for candidate in candidates:
        expected = [row for row in candidate['ledger'] if payment_in_window(date.fromisoformat(row['payment_date']), first, CUTOFF)]
        assert [row for row in ledger if row['ticker'] == candidate['ticker']] == expected
        assert expected[0]['payment_date'] == first.replace(day=1).isoformat()
        assert all(row['published_at'] is None for row in expected)
    assert report['payment_events'] == 121
    assert data['forecast_target'] == report['forecast_target'] == PAYMENT_TARGET
    for record in (data, preparation):
        assert record['cashflow_basis'] == 'reported_historical_cash_reconciled'
        assert record['data_quality_policy'] == record['source_policy_version'] == 'RF-CALENDAR-RECONCILED-1'
        assert record['availability_basis'] == 'payment_date_retrospective_observation_not_publication'
        assert record['point_in_time_archive_verified'] is False and record['cash_settlement_verified'] is False
    assert all(row['preparation_basis'] == 'rebuilt_from_reconciled_sources'
               for row in preparation['stocks'] if row['model_ready'])
    for name, key in [('cashflows.csv', 'cashflows_sha256'), ('payment-ledger.json', 'payment_ledger_sha256')]:
        assert data[key] == hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
    assert len(real_dataset(snapshot).assets) == 4


@pytest.mark.parametrize('ticker', ['TEST3', 'TEST4'])
@pytest.mark.parametrize('failure', ['missing', 'zero_earnings', 'unpublished', 'stale', 'share_basis'])
def test_financial_gates_apply_identically_to_legacy_and_new_stocks(tmp_path, monkeypatch, ticker, failure):
    baseline(tmp_path, monkeypatch)
    candidates, statements = approve(tmp_path, monkeypatch, (ticker,))
    if failure == 'missing':
        statements.clear()
    elif failure == 'zero_earnings':
        for row in statements:
            row['profit_per_share'] = 0
    elif failure == 'unpublished':
        for row in statements:
            row['published_at'] = (CUTOFF + timedelta(days=1)).isoformat()
    elif failure == 'stale':
        statements[:] = statements[:1]
    else:
        candidates[0]['financial_basis_start'] = CUTOFF.isoformat()
    snapshot, report = prepare_inventory.prepare(tmp_path)
    result = next(row for row in report['stock_universe_preparation']['stocks'] if row['ticker'] == ticker)
    assert not result['model_ready'] and result['reason_codes'] == ['financial_features_unavailable']
    assert result['source_checks'] == candidates[0]['checks']
    assert ticker not in table(snapshot / 'returns.csv')[0]
    assert ticker not in {row['ticker'] for row in table(snapshot / 'observations.csv')[1]}


def test_rebuilt_training_keeps_date_and_financial_share_basis_windows(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    candidates, _ = approve(tmp_path, monkeypatch)
    for candidate in candidates:
        candidate['training_since'] = shift_month(CUTOFF, -36)
        candidate['financial_basis_start'] = date(CUTOFF.year - 3, 12, 31).isoformat()
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    for candidate in candidates:
        rows = [row for row in table(snapshot / 'observations.csv')[1] if row['ticker'] == candidate['ticker']]
        assert rows
        assert all(date.fromisoformat(row['as_of']) >= candidate['training_since']
                   and row['fiscal_end'] >= candidate['financial_basis_start']
                   and date.fromisoformat(row['label_end']) == shift_month(date.fromisoformat(row['as_of']), 12)
                   and date.fromisoformat(row['label_end']) <= CUTOFF for row in rows)


@pytest.mark.parametrize('name', ['cashflows.csv', 'payment-ledger.json'])
def test_rebuilt_payment_hash_validation_cannot_be_bypassed(tmp_path, monkeypatch, name):
    baseline(tmp_path, monkeypatch)
    approve(tmp_path, monkeypatch)
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    path = snapshot / name
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='Integridade dos pagamentos divergente'):
        real_dataset(snapshot)


def test_failed_candidate_validation_never_changes_active_snapshot(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    before = (tmp_path / 'current.json').read_bytes()
    calls = []

    def check(path):
        calls.append(path)
        if len(calls) > 1:
            raise ValueError('Rejected synthetic candidate')
        return real_dataset(path)

    monkeypatch.setattr(prepare_inventory, 'real_dataset', check)
    with pytest.raises(ValueError, match='Rejected synthetic'):
        prepare_inventory.prepare(tmp_path, activate=True)
    assert (tmp_path / 'current.json').read_bytes() == before


def test_activation_cannot_overwrite_a_model_published_by_another_collection(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    write_json(tmp_path / 'current.json', {'snapshot': 'snapshots/another-collection'})
    with pytest.raises(ValueError, match='base ativa mudou'):
        prepare_inventory.activate_candidate(tmp_path, snapshot)
    assert json.loads((tmp_path / 'current.json').read_text())['snapshot'] == 'snapshots/another-collection'


def test_sync_inventory_reports_removal_as_a_changed_model_sample(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    _, report = prepare_inventory.prepare(tmp_path)
    inventory = load_inventory(tmp_path)
    published = []
    monkeypatch.setattr(prepare_inventory, 'publish_inventory', lambda folder, data: published.append(data))
    prepare_inventory.sync_inventory(tmp_path, inventory, report['stock_universe_preparation'], 'synthetic-candidate')
    assert published == [inventory] and inventory['model_sample_unchanged'] is False
    assert inventory['counts']['model_ready_stocks'] == 0
    assert all(not row['model_ready'] and row['model_pending_reason'] for row in inventory['stocks'])


def test_current_quote_source_is_separate_from_raw_historical_provider(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    candidates, _ = approve(tmp_path, monkeypatch)
    path = tmp_path / 'raw/expanded/TEST3-current.json'
    path.parent.mkdir(parents=True)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).timestamp()
    write_json(path, {'chart': {'result': [{'meta': {'symbol': 'TEST3.SA', 'currency': 'BRL',
        'regularMarketPrice': 42.5, 'regularMarketTime': stamp}, 'events': {}}]}})
    candidates[0]['current_quote_source_file'] = path.relative_to(tmp_path).as_posix()
    candidates[0]['price_source_file'] = 'raw/different-historical-schema.json'
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    metadata = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    asset = next(row for row in metadata['assets'] if row['ticker'] == 'TEST3')
    assert asset['current_quote']['purchase_available'] is True
    assert asset['current_quote']['price'] == 42.5


def test_declared_payment_start_is_preserved_without_becoming_settlement_confirmation(tmp_path, monkeypatch):
    baseline(tmp_path, monkeypatch)
    candidates, _ = approve(tmp_path, monkeypatch)
    row = candidates[0]['ledger'][0]
    row.update(payment_date=CUTOFF.isoformat(),
        payment_date_basis='explicit_payment_start_date_not_settlement',
        full_settlement_on_date_verified=False)
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    metadata = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    qualification, = metadata['cash_calendar_date_qualifications']
    assert qualification['ticker'] == candidates[0]['ticker']
    assert qualification['date_basis'] == 'explicit_payment_start_date_not_settlement'
    assert qualification['full_settlement_on_date_verified'] is False
    assert metadata['cash_settlement_verified'] is False
    saved = json.loads((snapshot / 'payment-ledger.json').read_text(encoding='utf-8'))
    assert any(item.get('payment_date_basis') == qualification['date_basis'] for item in saved)


def test_one_download_url_keeps_distinct_original_and_derived_artifact_proofs(tmp_path, monkeypatch):
    base = baseline(tmp_path, monkeypatch)
    metadata = json.loads((base / 'assets.json').read_text(encoding='utf-8'))
    url = 'https://example.test/exact-native-download'
    proofs = [{'file': 'raw/' + name, 'sha256': marker * 64, 'url': url}
              for name, marker in [('original.zip', 'a'), ('original-member.pdf', 'b'),
                                   ('derived-page-text.json', 'c')]]
    metadata['sources'] = proofs
    write_json(base / 'assets.json', metadata)
    snapshot, _ = prepare_inventory.prepare(tmp_path)
    saved = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    assert saved['sources'] == proofs
