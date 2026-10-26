import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import zipfile

import pytest

from . import collect_market_data as collector
from .collect_market_data import (cvm_statements, financial_features, load_reconciliations,
                                  publish_pointer, shift_month, stock_history, stock_observations,
                                  treasury_history, write_csv, write_json)
from .datasets import FEATURES, real_dataset


def test_cash_payments_are_not_future_announcements():
    cutoff = date(2026, 9, 30)
    data = {'results': [{'currency': 'BRL', 'historicalDataPrice': [
        {'date': int(datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()), 'adjustedClose': 30, 'close': 30}],
        'dividendsData': {'cashDividends': [
            {'paymentDate': '2026-08-01', 'approvedOn': '2026-07-01', 'rate': 1.2},
            {'paymentDate': '2026-12-01', 'approvedOn': '2026-08-01', 'rate': 5}]}}]}
    _, _, payments = stock_history(json.dumps(data).encode(), cutoff)
    assert payments == [(date(2026, 8, 1), date(2026, 7, 1), 1.2)]


def test_share_events_require_explicit_normalization():
    data = {'results': [{'currency': 'BRL', 'historicalDataPrice': [], 'dividendsData': {
        'stockDividends': [{'lastDatePrior': '2025-01-01', 'label': 'DESDOBRAMENTO'}]}}]}
    with pytest.raises(ValueError, match='normalizacao'):
        stock_history(json.dumps(data).encode(), date(2026, 9, 30))


def test_financial_features_respect_actual_disclosure_and_versions():
    records = [{'cvm': 9512, 'fiscal_end': '2024-12-31', 'version': '1', 'published_at': '2025-03-15',
                'profit_per_share': 4, 'free_cash_per_share': 5, 'debt_ratio': .5},
               {'cvm': 9512, 'fiscal_end': '2024-12-31', 'version': '2', 'published_at': '2025-08-01',
                'profit_per_share': 8, 'free_cash_per_share': 10, 'debt_ratio': .7}]
    payments = [(date(2025, 3, 1), date(2025, 2, 1), 2)]
    assert financial_features(records, payments, date(2025, 2, 28), (9512, 'PN', 0)) is None
    features, filing = financial_features(records, payments, date(2025, 3, 31), (9512, 'PN', 0))
    assert filing['version'] == '1'
    assert features == [2, 1 / 12, 4, 5, .5, .5, 0]
    assert financial_features(records, payments, date(2025, 8, 31), (9512, 'PN', 0))[1]['version'] == '2'


@pytest.mark.parametrize('recognized_capex', [True, False])
def test_cvm_eps_is_not_scaled_as_aggregate_currency(recognized_capex):
    fields = ['CD_CVM', 'DT_REFER', 'VERSAO', 'DT_RECEB', 'ID_DOC', 'ORDEM_EXERC', 'MOEDA',
              'ESCALA_MOEDA', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA']
    common = dict(CD_CVM='009512', DT_REFER='2025-12-31', VERSAO='1', DT_RECEB='2026-03-05',
                  ID_DOC='155122', ORDEM_EXERC='\u00daLTIMO', MOEDA='REAL', ESCALA_MOEDA='MIL')
    statements = {'': [('', '', 0)], '_DRE_con': [('3.99.01.02', 'PN', 8.54), ('3.11.01', 'Controladora', 110129000)],
                  '_DFC_MI_con': [('6.01', 'Operacoes', 200333000),
                                  ('6.02.01', 'Aquisicoes de ativos imobilizados e intangiveis'
                                   if recognized_capex else 'Investimentos em Exploracao e Producao', -108714000)],
                  '_BPA_con': [('1', 'Ativo total', 1223389000)],
                  '_BPP_con': [('2.01', 'Passivo circulante', 198368000), ('2.02', 'Passivo nao circulante', 607434000)]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for suffix, entries in statements.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=';')
            writer.writeheader()
            for code, description, value in entries:
                writer.writerow(common | dict(CD_CONTA=code, DS_CONTA=description, VL_CONTA=value))
            archive.writestr(f'dfp_cia_aberta{suffix}_2025.csv', stream.getvalue().encode('latin1'))
    diagnostics = []
    rows = cvm_statements(buffer.getvalue(), 2025, diagnostics=diagnostics)
    if not recognized_capex:
        assert rows == []
        assert diagnostics[0]['status'] == 'excluded'
        assert 'capex_not_identified' in diagnostics[0]['reasons']
        assert diagnostics[0]['unselected_investing_outflows'][0]['code'] == '6.02.01'
        return
    row = rows[0]
    assert diagnostics[0]['status'] == 'included'
    assert row['profit_per_share'] == 8.54
    assert row['published_at'] == '2026-03-05'
    assert row['free_cash_per_share'] == pytest.approx((200333000 - 108714000) * 8.54 / 110129000)


def test_treasury_never_fills_missing_history_or_invents_coupons():
    cutoff = date(2026, 9, 30)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(['Tipo Titulo', 'Data Vencimento', 'Data Base', 'PU Venda Manha', 'Taxa Compra Manha'])
    for kind, maturity in [('Tesouro Selic', '01/03/2027'), ('Tesouro IPCA+', '15/05/2035')]:
        for offset in range(61):
            day = shift_month(cutoff, -offset)
            writer.writerow([kind, maturity, day.strftime('%d/%m/%Y'), '1000,00', '12,00'])
    writer.writerow(['Tesouro Prefixado', '01/01/2029', '30/09/2026', '900,00', '12,00'])
    selected = treasury_history(stream.getvalue().encode(), cutoff)
    assert [key[0] for key, _ in selected] == ['Tesouro Selic', 'Tesouro IPCA+']
    with pytest.raises(ValueError, match='61 precos'):
        treasury_history(stream.getvalue().replace('Tesouro Selic', 'Outro').encode(), cutoff)


def cash_payload(events, cutoff=date(2026, 9, 30)):
    return json.dumps({'results': [{'currency': 'BRL', 'longName': 'Synthetic test issuer',
                                   'historicalDataPrice': [{'date': int(datetime.combine(cutoff, datetime.min.time(),
                                                                                       timezone.utc).timestamp()),
                                                             'adjustedClose': 30, 'close': 30}],
                                   'dividendsData': {'cashDividends': events}}]}).encode()


def financial_record():
    return {'cvm': 9512, 'fiscal_end': '2025-12-31', 'version': '1', 'published_at': '2026-03-01',
            'profit_per_share': 4, 'free_cash_per_share': 5, 'debt_ratio': .5, 'filing_id': 'test'}


def test_paid_history_without_approval_uses_observed_payment_availability():
    payload = cash_payload([{'paymentDate': '2026-08-01', 'approvedOn': None, 'rate': 1.2}])
    _, _, history = stock_history(payload, date(2026, 9, 30))
    assert history == [(date(2026, 8, 1), date(2026, 8, 1), 1.2)]
    assert not history.issues
    audit = history.observed_payment_availability[0]
    assert audit['availability_basis'] == 'observed_payment'
    assert audit['approval_date'] is None and audit['approval_publication_verified'] is False
    assert financial_features([financial_record()], history, date(2026, 7, 31), (9512, 'PN', 0))[0][0] == 0
    assert financial_features([financial_record()], history, date(2026, 8, 31), (9512, 'PN', 0))[0][0] == 1.2


def test_unknown_payment_blocks_overlaps_through_cutoff_without_imputing_date():
    payload = cash_payload([{'paymentDate': None, 'approvedOn': '2020-01-01',
                             'exDate': '2020-02-01', 'rate': 1.2}])
    _, _, history = stock_history(payload, date(2026, 9, 30))
    assert history == []
    issue = history.issues[0]
    assert issue['payment_date'] is None
    assert issue['interval_start'] == '2020-02-01' and issue['interval_end'] == '2026-09-30'
    assert not history.blockers(date(2018, 1, 31), date(2020, 1, 31))
    assert history.blockers(date(2020, 1, 31), date(2020, 2, 1))
    assert financial_features([financial_record()], history, date(2026, 9, 30), (9512, 'PN', 0)) is None


def test_unknown_payment_excludes_both_target_and_previous_cash_windows():
    _, _, history = stock_history(cash_payload([{'paymentDate': None, 'approvedOn': '2025-06-01',
                                                'exDate': '2025-06-15', 'rate': 1}]), date(2026, 9, 30))
    prices = {date(2024, 6, 30): (), date(2025, 6, 30): ()}
    records = [financial_record() | {'fiscal_end': '2023-12-31', 'published_at': '2024-03-01'}]
    rows, coverage = stock_observations('PETR4', prices, history, records, date(2026, 9, 30))
    assert rows == []
    assert 'target_cash_incomplete' in coverage['excluded_windows'][0]['reasons']
    assert 'previous_cash_incomplete' in coverage['excluded_windows'][1]['reasons']
    assert coverage['current_cash_window']['complete'] is False


@pytest.mark.parametrize('event', [
    {'paymentDate': None, 'approvedOn': None, 'rate': 1},
    {'paymentDate': '2026-08-01', 'approvedOn': '2026-09-01', 'rate': 1},
    {'paymentDate': 'invalid', 'approvedOn': None, 'rate': 1},
    {'paymentDate': '2026-08-01', 'approvedOn': None, 'rate': None},
])
def test_invalid_or_undated_cash_is_never_silently_zero(event):
    _, _, history = stock_history(cash_payload([event]), date(2026, 9, 30))
    assert not history and history.issues
    assert history.blockers(date(2025, 9, 30), date(2026, 9, 30))


def test_absent_cash_history_is_incomplete_not_a_zero_distribution():
    _, _, history = stock_history(cash_payload([]), date(2026, 9, 30))
    assert history.blockers(date(2025, 9, 30), date(2026, 9, 30))


def reconciliation_fixture(folder):
    content = cash_payload([{'paymentDate': None, 'approvedOn': '2019-12-19',
                             'lastDatePrior': '2019-12-26', 'exDate': '2019-12-27',
                             'label': 'JCP', 'rate': 1.414364369}])
    evidence = folder / 'evidence' / 'test-primary.txt'
    evidence.parent.mkdir()
    evidence.write_bytes(b'Synthetic primary evidence for tests only: payment 2020-08-07.')
    entry = {'ticker': 'VALE3', 'raw_sha256': hashlib.sha256(content).hexdigest(), 'event_index': 0,
             'match': {'approvedOn': '2019-12-19', 'label': 'JCP', 'rate': 1.414364369},
             'changes': {'paymentDate': '2020-08-07'},
             'evidence': {'file': 'evidence/test-primary.txt', 'url': 'https://example.com/test-only',
                          'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                          'verified_fields': ['paymentDate'], 'locator': 'Synthetic test evidence.'}}
    write_json(folder / 'reconciliations.json', {'schema_version': 1, 'overrides': [entry]})
    return content, entry


def test_primary_reconciliation_preserves_raw_and_original_approval(tmp_path):
    content, entry = reconciliation_fixture(tmp_path)
    original = hashlib.sha256(content).hexdigest()
    overrides = load_reconciliations(tmp_path, 'VALE3', content)
    _, _, history = stock_history(content, date(2026, 9, 30), reconciliations=overrides)
    assert history == [(date(2020, 8, 7), date(2019, 12, 19), 1.414364369)]
    assert history.issues == [] and len(history.reconciled_events) == 1
    assert hashlib.sha256(content).hexdigest() == original
    assert history.reconciled_events[0]['original_approval_date'] == '2019-12-19'


@pytest.mark.parametrize('change', ['evidence_hash', 'raw_hash', 'event_match', 'unverified_field'])
def test_reconciliation_rejects_changed_or_unverified_evidence(tmp_path, change):
    content, entry = reconciliation_fixture(tmp_path)
    if change == 'evidence_hash':
        entry['evidence']['sha256'] = '0' * 64
    elif change == 'raw_hash':
        entry['raw_sha256'] = '0' * 64
    elif change == 'event_match':
        entry['match']['rate'] = 999
    else:
        entry['evidence']['verified_fields'] = []
    write_json(tmp_path / 'reconciliations.json', {'schema_version': 1, 'overrides': [entry]})
    with pytest.raises(ValueError):
        load_reconciliations(tmp_path, 'VALE3', content)


def dataset_files(folder, *, price=30, ticker=True):
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = shift_month(date.today(), -1)
    assets = [{'ticker': t, 'name': 'Synthetic test asset', 'asset_class': 'liquidez', 'risk_level': 0,
               'liquidity_days': 1, 'minimum_years': 0, 'price': price, 'annual_cash_yield': 0,
               'features': None, 'as_of': cutoff.isoformat(), 'source': 'Synthetic test data.'}
              for t in ['TEST-A', 'TEST-B']]
    write_json(folder / 'assets.json', {'assets': assets, 'returns_include_distributions': True,
                                      'source': 'Synthetic test data.'})
    write_csv(folder / 'returns.csv', ['date', 'inflation', 'TEST-A', 'TEST-B'],
              [{'date': shift_month(cutoff, i).isoformat(), 'inflation': 0, 'TEST-A': price / 1000, 'TEST-B': .02}
               for i in range(-60, 1)])
    end = shift_month(cutoff, -1)
    row = {'as_of': shift_month(end, -12).isoformat(), 'published_at': shift_month(end, -13).isoformat(),
           'label_end': end.isoformat(), **dict(zip(FEATURES, [1, .5, 2, 2, .5, .5, 0])), 'target': 1}
    if ticker:
        row['ticker'] = 'TEST-A'
    write_csv(folder / 'observations.csv', list(row), [row])
    report = {'status': 'ready', 'reference_date': cutoff.isoformat()}
    write_json(folder / 'collection-report.json', report)
    return report


@pytest.mark.parametrize('ticker', [True, False])
def test_legacy_dataset_preserves_optional_ticker(tmp_path, ticker):
    dataset_files(tmp_path, ticker=ticker)
    loaded = real_dataset(tmp_path)
    assert ('ticker' in loaded.observations[0]) is ticker
    if ticker:
        assert loaded.observations[0]['ticker'] == 'TEST-A'


def test_pointer_uses_whole_version_and_is_resolved_once(tmp_path, monkeypatch):
    dataset_files(tmp_path, price=10)
    first = tmp_path / 'snapshots' / 'first'
    report = dataset_files(first, price=20)
    second = tmp_path / 'snapshots' / 'second'
    dataset_files(second, price=40)
    publish_pointer(tmp_path, first, report)
    from . import datasets
    read = datasets.read_csv

    def switch_after_metadata(path, required):
        publish_pointer(tmp_path, second, report)
        return read(path, required)

    monkeypatch.setattr(datasets, 'read_csv', switch_after_metadata)
    loaded = real_dataset(tmp_path)
    assert loaded.assets[0].price == 20
    assert loaded.returns[0, 0] == .02
    assert json.loads((tmp_path / 'current.json').read_text())['snapshot'] == 'snapshots/second'


def test_failed_pointer_switch_keeps_old_snapshot(tmp_path, monkeypatch):
    first = tmp_path / 'snapshots' / 'first'
    report = dataset_files(first, price=20)
    second = tmp_path / 'snapshots' / 'second'
    dataset_files(second, price=40)
    publish_pointer(tmp_path, first, report)
    previous = (tmp_path / 'current.json').read_bytes()

    def fail(*args):
        raise OSError('Synthetic pointer-switch failure')

    monkeypatch.setattr(collector.os, 'replace', fail)
    with pytest.raises(OSError):
        publish_pointer(tmp_path, second, report)
    assert (tmp_path / 'current.json').read_bytes() == previous
    assert real_dataset(tmp_path).assets[0].price == 20


@pytest.mark.parametrize('pointer', [
    {'schema_version': 1, 'status': 'blocked', 'reason': 'uncertain current payment'},
    {'schema_version': 1, 'status': 'ready', 'snapshot': '../outside'},
    {'schema_version': 1, 'status': 'ready', 'snapshot': 'snapshots/missing'},
    {'schema_version': 1, 'status': 'unexpected'},
    [],
])
def test_bad_pointer_never_falls_back_to_legacy_files(tmp_path, pointer):
    dataset_files(tmp_path)
    write_json(tmp_path / 'current.json', pointer)
    with pytest.raises(ValueError):
        real_dataset(tmp_path)


def collect_inputs(folder, monkeypatch, *, unknown_payment=False, missing_record=False, short=False):
    today = date.today()
    cutoff = shift_month(today, -1)
    raw = folder / 'raw'
    raw.mkdir()
    prices, inflation = [], []
    day = date(2010, 1, 31)
    while day <= cutoff:
        prices.append({'date': int(datetime.combine(day, datetime.min.time(), timezone.utc).timestamp()),
                       'adjustedClose': 30, 'close': 30})
        inflation.append({'data': day.strftime('%d/%m/%Y'), 'valor': '0.3'})
        day = shift_month(day, 1)
    for ticker in collector.STOCKS:
        events = [{'paymentDate': f'{year}-01-20', 'lastDatePrior': f'{year}-01-10',
                   'approvedOn': None, 'label': 'JCP', 'rate': 1.0}
                  for year in range(2010, today.year + 1) if date(year, 1, 20) <= cutoff]
        if unknown_payment and ticker == 'PETR4':
            events.append({'paymentDate': None, 'approvedOn': '2020-01-01', 'label': 'JCP',
                           'lastDatePrior': '2020-01-02', 'exDate': '2020-01-03', 'rate': 1.0})
        if missing_record and ticker == 'PETR4':
            events.append({'paymentDate': '2020-02-01', 'approvedOn': '2020-01-01',
                           'lastDatePrior': None, 'exDate': '2020-01-03', 'label': 'JCP', 'rate': 1.0})
        write_json(raw / f'brapi-{ticker}.json', {'results': [{'currency': 'BRL', 'longName': 'Synthetic test issuer',
                    'historicalDataPrice': prices, 'dividendsData': {'cashDividends': events}}]})
    for year in range(2010, today.year):
        (raw / f'cvm-dfp-{year}.zip').write_bytes(b'Synthetic fixture; cvm parser independently tested.')
    write_json(raw / 'bcb-ipca.json', inflation)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=';')
    writer.writerow(['Tipo Titulo', 'Data Vencimento', 'Data Base', 'PU Venda Manha', 'Taxa Compra Manha'])
    for kind, maturity in [('Tesouro Selic', f'01/03/{today.year+2}'),
                           ('Tesouro IPCA+', f'15/05/{today.year+10}')]:
        for i in range(70):
            writer.writerow([kind, maturity, shift_month(cutoff, -i).strftime('%d/%m/%Y'), '1000,00', '12,00'])
    (raw / 'tesouro-precos.csv').write_bytes(stream.getvalue().encode())

    def statements(content, year, *, diagnostics=None):
        if short and year != today.year - 2:
            return []
        return [financial_record() | {'cvm': spec[0], 'fiscal_end': f'{year}-12-31',
                                      'published_at': f'{year+1}-04-01'}
                for spec in collector.STOCKS.values()]

    def forbid_network(*args, **kwargs):
        raise AssertionError('cached-only collector must not access the network')

    monkeypatch.setattr(collector, 'cvm_statements', statements)
    monkeypatch.setattr(collector.urllib.request, 'urlopen', forbid_network)
    return today


def test_collect_cached_snapshot_is_atomic_and_keeps_legacy_files(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch)
    legacy = tmp_path / 'assets.json'
    legacy.write_bytes(b'Previous legacy snapshot remains byte-identical.')
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (tmp_path / 'raw').iterdir()}
    report = collector.collect(tmp_path, today=today, cached_only=True)
    assert report['status'] == 'ready' and report['observation_dates'] >= 60
    assert report['model']['trainRows'] > 0
    assert legacy.read_bytes() == b'Previous legacy snapshot remains byte-identical.'
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (tmp_path / 'raw').iterdir()} == before
    assert real_dataset(tmp_path).metadata['snapshot_id'] == report['snapshot_id']
    assert {r['ticker'] for r in real_dataset(tmp_path).observations} == {'PETR4', 'VALE3'}


def test_collect_no_publish_leaves_active_pointer_unchanged(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch)
    original = {'schema_version': 1, 'status': 'blocked', 'reason': 'Existing test state'}
    write_json(tmp_path / 'current.json', original)
    report = collector.collect(tmp_path, today=today, cached_only=True, activate=False)
    assert report['status'] == 'ready'
    assert json.loads((tmp_path / 'current.json').read_text()) == original


def test_collect_blocks_uncertain_stock_but_preserves_safe_assets(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch, missing_record=True)
    report = collector.collect(tmp_path, today=today, cached_only=True)
    assert report['status'] == 'partial' and report['model']['status'] == 'not_executed'
    assert report['blocked_assets'] == [{'ticker': 'PETR4', 'reason': 'current_rights_incomplete'}]
    loaded = real_dataset(tmp_path)
    assert loaded.assets[0].features is None
    assert loaded.assets[1].features is not None
    assert all(a.price > 0 for a in loaded.assets)
    assert json.loads((tmp_path / 'current.json').read_text())['status'] == 'partial'


def test_collect_never_lowers_60_date_requirement_or_executes_insufficient_model(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch, short=True)

    def forbidden_forecast(dataset):
        raise AssertionError('Insufficient history must not execute the forecast')

    monkeypatch.setattr(collector, 'dataset_forecast', forbidden_forecast)
    report = collector.collect(tmp_path, today=today, cached_only=True)
    assert report['model']['status'] == 'not_executed'
    assert report['model']['available_dates'] < 60 and report['model']['required_dates'] == 60


def test_cached_only_rejects_missing_cache_without_network(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        raise AssertionError('Network is forbidden')

    monkeypatch.setattr(collector.urllib.request, 'urlopen', forbid_network)
    with pytest.raises(ValueError, match='Cache raw ausente'):
        collector.cached_source('https://example.com', 'missing', tmp_path, [], False, cached_only=True)


def test_record_basis_includes_past_entitlement_with_future_or_unknown_payment():
    payload = cash_payload([
        {'lastDatePrior': '2026-08-12', 'approvedOn': '2026-08-01', 'paymentDate': '2027-01-01',
         'label': 'DIVIDENDO', 'rate': 1.2},
        {'lastDatePrior': '2026-09-10', 'approvedOn': None, 'paymentDate': None, 'label': 'JCP', 'rate': 2},
        {'lastDatePrior': '2026-10-10', 'approvedOn': None, 'paymentDate': '2026-09-01',
         'label': 'JCP', 'rate': 3},
    ])
    _, _, rights = stock_history(payload, date(2026, 9, 30), event_basis='record_date')
    assert rights.basis == 'record_date'
    assert rights == [(date(2026, 8, 12), date(2026, 8, 1), 1.2),
                      (date(2026, 9, 10), date(2026, 9, 10), 2)]
    assert rights.issues == [] and rights.cash_payment_issues
    assert rights.filter_counts['future_record_date'] == 1
    assert rights.right_event_audit[1]['availability_basis'] == 'observed_record_date'
    assert rights.right_event_audit[1]['approval_date'] is None
    assert rights.right_event_audit[1]['approval_publication_verified'] is False


def test_record_basis_excludes_rendimento_corrections_and_later_complements():
    common = {'lastDatePrior': '2026-08-12', 'approvedOn': '2026-08-01', 'paymentDate': None}
    events = [common | {'label': 'DIVIDENDO', 'rate': 1},
              common | {'label': 'JCP', 'rate': 2},
              common | {'label': 'RENDIMENTO', 'rate': 1000},
              common | {'label': 'DIVIDENDO', 'rate': 2000, 'remarks': 'Complemento posterior'},
              common | {'label': 'JCP', 'rate': 3000, 'relatedTo': 'Atualizacao pela Selic'}]
    _, _, history = stock_history(cash_payload(events), date(2026, 9, 30), event_basis='record_date')
    assert sum(rate for _, _, rate in history) == 3
    assert history.filter_counts['excluded_label'] == 1
    assert history.filter_counts['excluded_later_adjustment'] == 2
    assert not history.issues


def test_record_basis_never_substitutes_ex_date_or_payment_for_missing_record_date():
    event = {'lastDatePrior': None, 'exDate': '2026-08-02', 'paymentDate': '2026-08-20',
             'approvedOn': '2026-08-01', 'label': 'JCP', 'rate': 1}
    _, _, history = stock_history(cash_payload([event]), date(2026, 9, 30), event_basis='record_date')
    assert history == [] and len(history.issues) == 1
    assert history.issues[0]['record_date'] is None
    assert history.issues[0]['interval_start'] == date.min.isoformat()
    assert history.issues[0]['interval_end'] == '2026-09-30'
    assert history.cash_payment_issues == []


def test_approval_after_record_date_blocks_only_windows_containing_that_entitlement():
    event = {'lastDatePrior': '2025-08-15', 'approvedOn': '2025-09-01',
             'paymentDate': '2025-10-01', 'label': 'JCP', 'rate': 1}
    _, _, history = stock_history(cash_payload([event]), date(2026, 9, 30), event_basis='record_date')
    assert history == []
    issue = history.issues[0]
    assert issue['reasons'] == ['approval_after_record_date']
    assert issue['interval_start'] == issue['interval_end'] == '2025-08-15'
    assert history.blockers(date(2025, 8, 14), date(2025, 8, 15))
    assert not history.blockers(date(2025, 8, 15), date(2026, 9, 30))
    assert not history.blockers(date(2024, 8, 14), date(2025, 8, 14))


def test_payment_reconciliation_does_not_change_rights_target(tmp_path):
    content, entry = reconciliation_fixture(tmp_path)
    overrides = load_reconciliations(tmp_path, 'VALE3', content)
    _, _, before = stock_history(content, date(2026, 9, 30), event_basis='record_date')
    _, _, after = stock_history(content, date(2026, 9, 30), event_basis='record_date', reconciliations=overrides)
    assert before == after == [(date(2019, 12, 26), date(2019, 12, 19), 1.414364369)]
    assert len(before.cash_payment_issues) == 1 and not after.cash_payment_issues
    assert len(after.reconciled_events) == 1


def test_record_availability_never_anticipates_future_entitlement_or_month_boundary():
    events = [{'lastDatePrior': '2025-09-30', 'exDate': '2025-10-01', 'approvedOn': None,
               'paymentDate': '2027-01-01', 'label': 'DIVIDENDO', 'rate': 2},
              {'lastDatePrior': '2026-09-30', 'exDate': '2026-10-01', 'approvedOn': '2026-09-01',
               'paymentDate': '2027-01-01', 'label': 'JCP', 'rate': 3}]
    _, _, history = stock_history(cash_payload(events), date(2026, 9, 30), event_basis='record_date')
    record = financial_record() | {'fiscal_end': '2024-12-31', 'published_at': '2025-03-01'}
    assert financial_features([record], history, date(2025, 8, 31), (9512, 'PN', 0))[0][0] == 0
    assert financial_features([record], history, date(2025, 9, 30), (9512, 'PN', 0))[0][0] == 2
    assert financial_features([record], history, date(2026, 9, 30), (9512, 'PN', 0))[0][0] == 3
    rows, coverage = stock_observations('PETR4', {date(2025, 9, 30): ()}, history, [record], date(2026, 9, 30))
    assert rows[0]['target'] == 3 and rows[0]['previous_cash'] == 2
    assert rows[0]['features_available_at'] == '2025-09-30'
    assert rows[0]['features_available_at'] <= rows[0]['as_of']
    assert rows[0]['forecast_target_id'] == 'rights_by_record_date_v1'
    assert rows[0]['event_date_field'] == 'lastDatePrior'
    assert coverage['current_rights_window']['complete'] is True


def test_collect_rights_with_unknown_payment_still_executes_valid_model(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch, unknown_payment=True)
    report = collector.collect(tmp_path, today=today, cached_only=True)
    assert report['status'] == 'ready' and not report['blocked_assets']
    assert report['basis_validated'] is True
    assert report['model']['forecastTargetId'] == 'rights_by_record_date_v1'
    assert report['model']['comparableToPaymentTarget'] is False
    coverage = report['cash_coverage'][0]
    assert coverage['unresolved_cash_events'] == 1 and coverage['unresolved_right_events'] == 0
    assert coverage['payment_audit_affects_target'] is False
    snapshot = tmp_path / 'snapshots' / report['snapshot_id']
    with (snapshot / 'observations.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert {r['event_basis'] for r in rows} == {'record_date'}
    assert {r['event_date_field'] for r in rows} == {'lastDatePrior'}
    assert all(r['features_available_at'] <= r['as_of'] for r in rows)
    assert real_dataset(tmp_path).metadata['forecast_target']['warning'].startswith('NAO E CAIXA')


def test_collect_invalid_basis_does_not_execute_model(tmp_path, monkeypatch):
    today = collect_inputs(tmp_path, monkeypatch)
    parse = collector.stock_history

    def wrong_basis(*args, **kwargs):
        return parse(*args, **(kwargs | {'event_basis': 'payment'}))

    def forbidden_forecast(dataset):
        raise AssertionError('Invalid target basis must not execute the model')

    monkeypatch.setattr(collector, 'stock_history', wrong_basis)
    monkeypatch.setattr(collector, 'dataset_forecast', forbidden_forecast)
    with pytest.raises(ValueError, match='rights_by_record_date_v1'):
        collector.collect(tmp_path, today=today, cached_only=True)
    assert not (tmp_path / 'current.json').exists()


@pytest.mark.parametrize('change', [None, ('forecast_target_id', 'cash_payments_12m_v1'),
    ('forecast_target_id', ''), ('forecast_target_id', '0'), ('forecast_target_id', 'NaN'),
    ('event_basis', 'payment'), ('event_basis', ''), ('event_date_field', 'exDate'),
    ('features_available_at', ''), ('features_available_at', '0'), ('features_available_at', 'NaN'),
    ('features_available_at', '2099-01-01')])
def test_rights_loader_rejects_mixed_basis_or_missing_availability(tmp_path, change):
    dataset_files(tmp_path)
    path = tmp_path / 'assets.json'
    metadata = json.loads(path.read_text())
    metadata['forecast_target'] = collector.FORECAST_TARGET
    write_json(path, metadata)
    with (tmp_path / 'observations.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    rows[0].update(forecast_target_id='rights_by_record_date_v1', event_basis='record_date',
                   event_date_field='lastDatePrior', features_available_at=rows[0]['as_of'])
    if change:
        rows[0][change[0]] = change[1]
    write_csv(tmp_path / 'observations.csv', list(rows[0]), rows)
    if change:
        with pytest.raises(ValueError):
            real_dataset(tmp_path)
    else:
        assert real_dataset(tmp_path).observations[0]['ticker'] == 'TEST-A'
