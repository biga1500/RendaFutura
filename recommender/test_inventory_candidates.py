from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from .collect_market_data import DividendHistory, financial_features, shift_month, stock_observations
from .inventory_candidates import build_candidates, enrich_calendar
from .share_basis import factor_after


CUTOFF = date(2026, 9, 30)
OBSERVED = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()


def write(folder, name, data):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data), encoding='utf8')
    os.utime(path, (OBSERVED, OBSERVED))
    return path


def br_number(value):
    return f'{value:.8f}'.replace('.', ',')


def fixture(folder, *, ticker='TEST3', actions=False, adjusted=False, paid=True, published=True, history_months=60):
    cvm = 123
    isin = 'BRTESTACNOR0'
    first = shift_month(CUTOFF, -history_months).year
    records = [date(first, 9, 15), *[date(year, 8, 10) for year in range(first + 1, 2027)]]
    actions = [{'date': '2024-05-01', 'ratio': 2.}] if actions else []
    row = {'ticker': ticker, 'name': 'Synthetic cached fixture', 'cvm': cvm, 'share_class': 'ON',
           'isin': isin, 'sector_code': 1, 'identity_status': 'official_cvm_fca', 'model_ready': False}
    inventory = {'schema_version': 1, 'stocks': [row], 'sources': []}
    detail = {'codeCVM': str(cvm), 'companyName': row['name'], 'otherCodes': [
        {'code': ticker, 'isin': isin}, {'code': 'TEST4', 'isin': 'BRTESTACNPR8'}]}
    write(folder, 'raw/expanded/b3-detail-123.json', detail)
    days = sorted({*[shift_month(CUTOFF, -offset) for offset in range(history_months + 1)], *records})
    closes = [10 if adjusted else 10 * factor_after(day, actions, CUTOFF) for day in days]
    stamp = lambda day: int(datetime(day.year, day.month, day.day, 16, tzinfo=timezone.utc).timestamp())
    splits = {a['date']: {'date': stamp(date.fromisoformat(a['date'])), 'numerator': 2, 'denominator': 1}
              for a in actions}
    prices = {'chart': {'result': [{'meta': {'symbol': ticker + '.SA', 'currency': 'BRL'},
                                   'timestamp': [stamp(day) for day in days],
                                   'events': {'splits': splits},
                                   'indicators': {'quote': [{'close': closes}], 'adjclose': [{'adjclose': [99] * len(days)}]}}]}}
    write(folder, f'raw/expanded/{ticker}-prices.json', prices)
    cash, table = [], []
    for record in records:
        factor = factor_after(record, actions, CUTOFF)
        reported = 1 / factor if adjusted else 1.
        payment = record + timedelta(days=5)
        cash.append({'typeStock': 'ON', 'lastDatePriorEx': record.strftime('%d/%m/%Y'),
                     'quotedPerShares': '1', 'valueCash': br_number(1.), 'corporateAction': 'DIVIDENDO',
                     'closingPricePriorExDate': br_number(10 * factor),
                     'dateClosingPricePriorExDate': record.strftime('%d/%m/%Y')})
        values = ['DIV', record.strftime('%d/%m/%Y'), payment.strftime('%d/%m/%Y'), br_number(reported)]
        if paid:
            values.append('PAGO')
        if published:
            values.append((record - timedelta(days=1)).strftime('%d/%m/%Y'))
        table.append('<tr>' + ''.join(f'<td>{value}</td>' for value in values) + '</tr>')
    headers = ['Tipo', 'Data com', 'Pagamento', 'Valor']
    if paid:
        headers.append('Status')
    if published:
        headers.append('Data de publicacao')
    content = f'<title>{ticker}</title><table><tr>' + ''.join(f'<th>{key}</th>' for key in headers) + '</tr>' + ''.join(table) + '</table>'
    write(folder, f'raw/expanded/{ticker}-payments.html', content.encode('utf8'))
    write(folder, f'raw/expanded/{ticker}-b3-cash-1.json',
          {'page': {'pageNumber': 1, 'totalPages': 1, 'totalRecords': len(cash)}, 'results': cash})
    if actions:
        write(folder, f'raw/expanded/b3-supplement-{ticker[:4]}.json', {'stockDividends': [
            {'isinCode': isin, 'lastDatePrior': '30/04/2024', 'label': 'DESDOBRAMENTO'}]})
    return inventory


def hashes(folder):
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob('*') if path.is_file()}


def pending(folder, inventory):
    candidates, excluded, sources = build_candidates(folder, inventory, CUTOFF)
    assert not candidates
    assert len(excluded) == len(inventory['stocks'])
    return excluded[0], sources


@pytest.mark.parametrize('conflict', ['identity', 'share_class', 'subscription'])
def test_positive_native_security_conflict_cannot_use_a_primary_format_fallback(tmp_path, monkeypatch, conflict):
    from . import security_registration, primary_model_candidates
    inventory = fixture(tmp_path)
    inventory['sources'] = [{'url': security_registration.FCA_URL + 'fca_cia_aberta_2026.zip',
                             'file': 'raw/fca-fixture.zip'}]
    status = dict.fromkeys(('status', 'security_type', 'type_confidence', 'cvm', 'share_class',
        'isin', 'cvm_basis', 'isin_basis', 'market', 'listing_status'))
    status.update(status='primary_security_type_confirmed_not_current_trading', security_type='ordinary_share',
        share_class='ON', issues=[], blocks_current_purchase=False, block_reasons=[],
        exchange_trading_verified=False, whole_security_delisting_verified=False,
        model_admission_granted=False, evidence=[])
    if conflict == 'identity':
        status['status'] = 'conflicting_primary_or_exact_identity_evidence'
    elif conflict == 'share_class':
        status['share_class'] = 'PN'
    else:
        status.update(security_type='subscription_warrant', share_class=None)

    class Registry:
        def __init__(self, *args, **kwargs):
            pass

        def status(self, *args):
            return deepcopy(status)

    def forbidden_fallback(*args, **kwargs):
        raise AssertionError('Positive security blockers must not try an alternative format.')

    monkeypatch.setattr(security_registration, 'SecurityRegistry', Registry)
    monkeypatch.setattr(primary_model_candidates, 'build_primary_candidate', forbidden_fallback)
    excluded, _ = pending(tmp_path, inventory)
    expected = ('subscription_warrant_outside_equity_income_scope' if conflict == 'subscription'
                else 'conflicting_primary_security_identity')
    assert excluded['reason'] == expected
    assert not excluded['checks']['security_registration']['exchange_trading_verified']


def test_cancelled_issuer_cannot_be_promoted_by_alternative_sources(tmp_path, monkeypatch):
    from .issuer_registration import SOURCE_URL
    from . import primary_model_candidates

    inventory = fixture(tmp_path)
    inventory['as_of'] = '2026-10-24'
    body = b'CD_CVM;DENOM_SOCIAL;SIT;DT_INI_SIT;DT_CANCEL;MOTIVO_CANCEL\n123;Test;CANCELADA;2026-10-01;2026-10-01;INCORPORACAO\n'
    digest = hashlib.sha256(body).hexdigest()
    relative = 'raw/primary-research/registration/cadastro-' + digest + '.csv'
    write(tmp_path, relative, body)
    write(tmp_path, 'raw/primary-research/registration/current.json', {'file': relative,
        'sha256': digest, 'url': SOURCE_URL, 'retrieved_at': '2026-10-25T01:00:00+00:00'})
    monkeypatch.setattr(primary_model_candidates, 'build_primary_candidate',
        lambda *args: pytest.fail('A source fallback cannot bypass cancelled issuer registration.'))
    row, sources = pending(tmp_path, inventory)
    assert row['reason'] == 'cancelled_issuer_registration'
    assert row['checks']['issuer_registration']['cancelled_on'] == '2026-10-01'
    assert sources[0]['sha256'] == digest


def test_less_than_eight_events_and_less_than_72_months_can_reconcile(tmp_path, monkeypatch):
    import urllib.request
    from .broad_stocks import PublicSources

    def no_network(*args, **kwargs):
        pytest.fail('This bridge must never fetch, even on missing caches.')

    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    monkeypatch.setattr(PublicSources, 'get', no_network)
    inventory = fixture(tmp_path)
    before = hashes(tmp_path)
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded and len(candidates) == 1
    candidate = candidates[0]
    assert candidate['inventory_index'] == 0 and candidate['verdict'] == 'candidate'
    assert len(candidate['payments']) == 5 < 8
    assert isinstance(candidate['payments'], DividendHistory)
    assert len(candidate['independent_cash_matches']) == len(candidate['independent_price_matches']) == 5
    assert candidate['training_since'] == date(2022, 9, 30)
    assert candidate['financial_basis_start'] == '2014-01-01'
    assert candidate['statement_spec'] == (123, 'ON', 1)
    assert candidate['payments'].coverage_start == date(2021, 9, 30)
    assert all(value[2] == 10 for value in candidate['prices'].values())
    assert all(row['status'] == 'included' for row in candidate['ledger'])
    assert candidate['payments'].observed_payment_availability
    assert before == hashes(tmp_path)
    assert all(row['file'].startswith('raw/') and '..' not in Path(row['file']).parts for row in sources)
    assert all(row['access'] == 'existing_cache_read_only' for row in sources)
    for receipt in sources:
        assert receipt['sha256'] == before[receipt['file']]
    json.dumps(sources, allow_nan=False)


def test_long_training_history_is_independently_reconciled_not_truncated_to_scenario_window(tmp_path):
    inventory = fixture(tmp_path, history_months=120)
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    candidate = candidates[0]
    assert candidate['training_since'] == date(2017, 9, 30)
    assert candidate['checks']['extended_training_history']['status'] == 'independently_reconciled'
    assert min(day for day, _, _ in candidate['payments']) < date(2021, 9, 30)
    assert all(row['point_in_time_archive_verified'] is False for row in candidate['ledger'])


def test_declaration_group_retains_prewindow_installment_but_not_in_window_cash(tmp_path):
    from .inventory_candidates import _Cache, _payments
    from .share_basis import reconcile_payments

    inventory = fixture(tmp_path)
    html = b'''<title>TEST3</title><table><tr><th>Tipo</th><th>Data com</th><th>Pagamento</th><th>Valor</th></tr>
      <tr><td>DIV</td><td>01/04/2021</td><td>01/06/2021</td><td>0,20</td></tr>
      <tr><td>DIV</td><td>01/04/2021</td><td>01/11/2021</td><td>0,08</td></tr>
      <tr><td>DIV</td><td>01/08/2022</td><td>10/08/2022</td><td>0,10</td></tr>
      <tr><td>DIV</td><td>01/08/2023</td><td>10/08/2023</td><td>0,10</td></tr>
      <tr><td>DIV</td><td>01/08/2024</td><td>10/08/2024</td><td>0,10</td></tr>
    </table>'''
    write(tmp_path, 'raw/expanded/TEST3-payments.html', html)
    original, _ = _payments('TEST3', _Cache(tmp_path, inventory), CUTOFF)
    expected = {date(2021, 4, 1): .28, **{date(year, 8, 1): .10 for year in (2022, 2023, 2024)}}
    paid, ledger, adjustments = reconcile_payments('TEST3', original, expected, [], CUTOFF)
    assert len(original.installments) == 5
    assert adjustments[0]['reported_installment_sum'] == pytest.approx(.28)
    assert len(ledger) == len(paid) == 4
    assert ledger[0]['gross_per_unit'] == .08
    assert not any(row['payment_date'] == '2021-06-01' for row in ledger)


def test_primary_matching_is_exact_and_does_not_turn_declaration_into_payment():
    event = {'record_date': '2026-08-01', 'payment_date': '2026-08-20', 'gross_per_share': 1.,
             'published_at': None, 'status': 'past_scheduled'}
    declaration = {'kind': 'cvm_declaration', 'id': 'document:1', 'issues': [], 'blockers': [],
        'record_date': event['record_date'], 'payment_date': event['payment_date'],
        'gross_per_unit_decimal': '1.000000000', 'published_at': '2026-07-30',
        'available_as_of': True, 'evidence': [{'url': 'https://example.test/primary.pdf'}]}
    parsed = {'events': [event]}
    assert enrich_calendar(parsed, {'events': [declaration]}, verify_source=lambda ref: True) == 1
    assert event['published_at'] == '2026-07-30' and event['status'] == 'past_scheduled'
    assert enrich_calendar({'events': [{**event, 'payment_date': '2026-08-21'}]}, {'events': [declaration]}) == 0
    assert enrich_calendar({'events': [dict(event)]}, {'events': [declaration]}, verify_source=lambda ref: False) == 0


@pytest.mark.parametrize('adjusted', [False, True])
def test_share_basis_normalization_and_actual_payment_dates(tmp_path, adjusted):
    inventory = fixture(tmp_path, actions=True, adjusted=adjusted)
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    candidate = candidates[0]
    assert all(value[2] == 10 for value in candidate['prices'].values())
    assert candidate['share_normalization']['price_source_basis'] == ('split_adjusted' if adjusted else 'raw')
    assert candidate['financial_basis_start'] == '2024-05-01'
    assert candidate['training_since'] == date(2022, 9, 30)
    for row in candidate['ledger']:
        assert row['gross_per_unit'] == .5 if row['record_date'] < '2024-05-01' else row['gross_per_unit'] == 1.
        assert (date.fromisoformat(row['payment_date']) - date.fromisoformat(row['record_date'])).days == 5
        assert row['share_basis_date'] == '2026-09-30'


def test_legacy_four_column_calendar_uses_same_reconciliation_not_paid_inference(tmp_path):
    inventory = fixture(tmp_path, paid=False, published=False)
    inventory['stocks'][0]['model_ready'] = True
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    candidate = candidates[0]
    assert candidate['checks']['independent_cash_matches'] == 5
    assert candidate['checks']['independent_price_matches'] == 5
    assert candidate['checks']['payment_states'] == {'past_scheduled': 5}
    assert all(row['reported_payment_status'] == 'past_scheduled' for row in candidate['ledger'])
    assert all(row['published_at'] is None and row['publication_verified'] is False for row in candidate['ledger'])
    assert all(row['point_in_time_archive_verified'] is False for row in candidate['ledger'])
    assert all(row['availability_basis'] == 'payment_date_retrospective_observation_not_publication'
               for row in candidate['ledger'])


def test_paid_report_without_publication_keeps_publication_unknown(tmp_path):
    inventory = fixture(tmp_path, published=False)
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    assert all(row['published_at'] is None and row['publication_verified'] is False for row in candidates[0]['ledger'])


def test_future_installment_reconciles_the_right_but_never_becomes_paid_cash(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    body = path.read_bytes()
    original = (b'<tr><td>DIV</td><td>10/08/2026</td><td>15/08/2026</td>'
                b'<td>1,00000000</td><td>PAGO</td><td>09/08/2026</td></tr>')
    assert original in body
    split = original.replace(b'1,00000000', b'0,50000000')
    future = split.replace(b'15/08/2026', b'01/12/2026').replace(b'<td>PAGO</td>', b'<td>A PAGAR</td>')
    path.write_bytes(body.replace(original, split + future))
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    history = candidates[0]['payments']
    assert history.future_events == 1
    assert sum(value for _, _, value in history) == 4.5
    assert all(row['payment_date'] <= '2026-09-30' for row in candidates[0]['ledger'])
    assert any(row['payment_date'] == '2026-12-01' for row in history.installments)


def test_after_cutoff_publication_is_not_historical_payment_availability(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    path.write_bytes(path.read_bytes().replace(b'09/08/2026', b'01/10/2026'))
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'payment_publication_after_cutoff'


def test_same_issuer_other_class_cash_cache_can_be_reused(tmp_path):
    inventory = fixture(tmp_path)
    old = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    alternate = old.with_name('TEST4-b3-cash-1.json')
    old.rename(alternate)
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert len(candidates) == 1 and not excluded
    assert any(item['file'] == 'raw/expanded/TEST4-b3-cash-1.json' for item in sources)


def test_same_prefix_without_exact_cached_issuer_mapping_not_reused(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    path.rename(path.with_name('TEST5-b3-cash-1.json'))
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'independent_cash_cache_missing'


@pytest.mark.parametrize('other_cvm,accepted', [(123, True), (456, False)])
def test_reuse_can_follow_explicit_FCA_CVM_mapping_not_prefix(tmp_path, other_cvm, accepted):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    path.rename(path.with_name('NEWW4-b3-cash-1.json'))
    inventory['stocks'].append({'ticker': 'NEWW4', 'cvm': other_cvm, 'share_class': 'PN',
                                'identity_status': 'official_cvm_fca'})
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert len(candidates) == int(accepted)
    assert len(candidates) + len(excluded) == 2
    if accepted:
        assert candidates[0]['ticker'] == 'TEST3'
    else:
        assert excluded[0]['reason'] == 'independent_cash_cache_missing'


@pytest.mark.parametrize('fault,reason', [
    ('identity', 'independent_corporate_evidence_cache_missing'), ('prices', 'daily_prices_cache_missing'),
    ('cash', 'independent_cash_cache_missing'), ('payment', 'payments_cache_missing')])
def test_missing_cache_is_pending_without_network(tmp_path, fault, reason):
    inventory = fixture(tmp_path, actions=fault == 'supplement')
    filename = {'identity': 'b3-detail-123.json', 'prices': 'TEST3-prices.json', 'cash': 'TEST3-b3-cash-1.json',
                'payment': 'TEST3-payments.html', 'supplement': 'b3-supplement-TEST.json'}[fault]
    (tmp_path / 'raw/expanded' / filename).unlink()
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == reason
    if fault == 'identity':
        assert excluded['checks']['identity'] == 'primary_FCA_exact_CVM_class'
        assert excluded['checks']['prices'] == '61_consecutive_monthly_anchors'


def test_missing_supplement_still_requires_independent_price_and_cash_regime_matches(tmp_path):
    inventory = fixture(tmp_path, actions=True)
    (tmp_path / 'raw/expanded/b3-supplement-TEST.json').unlink()
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    assert candidates[0]['checks']['corporate_action_validation'] == 'independent_cash_and_raw_price_regimes'
    assert all(row['gross_per_unit'] == .5 for row in candidates[0]['ledger'] if row['record_date'] < '2024-05-01')


def test_full_cash_pagination_required_even_when_first_page_has_matching_events(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    value = json.loads(path.read_text())
    value['page'].update(totalPages=2, totalRecords=7)
    write(tmp_path, path.relative_to(tmp_path).as_posix(), value)
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'independent_cash_cache_missing'


def test_documents_and_explicit_zero_cannot_supply_missing_cash_calendar(tmp_path):
    inventory = fixture(tmp_path)
    inventory['stocks'][0]['payments'] = {'status': 'zero_window_confirmed', 'cash_model_eligible': True,
                                         'documents': [{'status': 'document_reference_only'}], 'events': []}
    write(tmp_path, 'raw/expanded/TEST3-payments.html',
          b'<p>TEST3 nao pagou proventos entre 01/10/2025 e 30/09/2026.</p>')
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'payment_calendar_not_found_no_zero_inferred'


@pytest.mark.parametrize('change,reason', [
    ({'isin': 'BROTROACNOR1'}, 'FCA_and_cached_B3_ISIN_disagree'),
    ({'share_class': None, 'unit_composition': [{'ticker': 'TEST3'}]}, 'unit_historical_FCA_2021_cache_missing'),
    ({'identity_status': 'conflicting_official_identity'}, 'exact_issuer_identity_unresolved'),
    ({'share_class': 'ONA'}, 'unsupported_exact_share_class'),
    ({'cvm': 999}, 'independent_corporate_evidence_cache_missing'),
])
def test_exact_identity_class_and_unsupported_units_not_guessed(tmp_path, change, reason):
    inventory = fixture(tmp_path)
    inventory['stocks'][0].update(change)
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == reason


def test_every_input_record_gets_one_verdict_including_duplicates(tmp_path):
    inventory = fixture(tmp_path)
    inventory['stocks'].extend([deepcopy(inventory['stocks'][0]), {'ticker': 'UNIT11', 'share_class': None}, {}])
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not candidates
    assert sorted(row['inventory_index'] for row in excluded) == list(range(4))
    assert [row['reason'] for row in excluded[:2]] == ['duplicate_inventory_ticker'] * 2


def test_hash_mismatch_and_parent_pointer_are_not_modified(tmp_path):
    inventory = fixture(tmp_path)
    inventory['sources'] = [{'file': 'raw/expanded/TEST3-prices.json', 'sha256': '0' * 64,
                             'url': 'https://example.org/synthetic-fixture'}]
    pointer = tmp_path / 'current.json'
    pointer.write_text('{"existing_model":"unchanged"}', encoding='utf8')
    before = hashes(tmp_path)
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'inventory_source_hash_mismatch'
    assert hashes(tmp_path) == before


def test_legacy_source_manifest_preserves_real_download_url_and_hash(tmp_path):
    inventory = fixture(tmp_path)
    relative = 'raw/expanded/TEST3-prices.json'
    source = {'file': 'expanded/TEST3-prices.json',
        'sha256': hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest(),
        'url': 'https://example.test/original-raw-request'}
    write(tmp_path, 'snapshots/prior-source/assets.json', {'sources': [source]})
    write(tmp_path, 'current.json', {'snapshot': 'snapshots/prior-source'})
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded and candidates[0]['price_url'] == source['url']
    receipt = next(row for row in sources if row['file'] == relative)
    assert receipt['sha256'] == source['sha256']


def test_mismatched_cash_or_price_is_not_overridden_by_source_flags(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    value = json.loads(path.read_text())
    value['results'][-1]['valueCash'] = '10,00'
    write(tmp_path, path.relative_to(tmp_path).as_posix(), value)
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'cash_amount_or_share_scale_disagrees_with_B3'


@pytest.mark.parametrize('second,accepted', [('0,60000000', True), ('0,70000000', False)])
def test_same_date_different_amounts_require_independent_total_reconciliation(tmp_path, second, accepted):
    inventory = fixture(tmp_path, paid=False, published=False)
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    original = b'<tr><td>DIV</td><td>10/08/2026</td><td>15/08/2026</td><td>1,00000000</td></tr>'
    replacement = original.replace(b'1,00000000', b'0,40000000') + original.replace(b'1,00000000', second.encode())
    assert original in path.read_bytes()
    path.write_bytes(path.read_bytes().replace(original, replacement))
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert bool(candidates) is accepted
    if accepted:
        assert sum(row['gross_per_unit'] for row in candidates[0]['ledger'] if row['record_date'] == '2026-08-10') == 1
        assert candidates[0]['checks']['same_date_amount_ambiguities_resolved'] == 1
    else:
        assert excluded[0]['reason'] == 'cash_amount_or_share_scale_disagrees_with_B3'


def test_missing_monthly_anchor_cannot_use_current_quote_or_adjusted_close(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-prices.json'
    value = json.loads(path.read_text())
    prices = value['chart']['result'][0]
    keep = [index for index, stamp in enumerate(prices['timestamp'])
            if datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m') != '2021-11']
    prices['timestamp'] = [prices['timestamp'][index] for index in keep]
    prices['indicators']['quote'][0]['close'] = [prices['indicators']['quote'][0]['close'][index] for index in keep]
    write(tmp_path, path.relative_to(tmp_path).as_posix(), value)
    inventory['stocks'][0]['quote'] = {'status': 'available', 'value': 10}
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'less_than_61_consecutive_prices'


def test_unknown_historical_payment_date_is_not_assumed_outside_window(tmp_path):
    inventory = fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    path.write_bytes(path.read_bytes().replace(b'20/09/2021', b'A definir'))
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'payment_date_unresolved_may_overlap_window'


def test_b3sa3_exact_alphanumeric_identity_and_candidate_contract(tmp_path):
    inventory = fixture(tmp_path, ticker='B3SA3')
    candidates, excluded, _ = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded and candidates[0]['ticker'] == 'B3SA3'
    candidate = candidates[0]
    statements = [{'cvm': 123, 'share_class': 'ON', 'published_at': '2026-03-01', 'fiscal_end': '2025-12-31',
                   'version': 1, 'filing_id': 'synthetic-test-statement', 'profit_per_share': 2.,
                   'free_cash_per_share': 1., 'debt_ratio': .3}]
    assert financial_features(statements, candidate['payments'], CUTOFF, candidate['statement_spec']) is not None
    observations, coverage = stock_observations(candidate['ticker'], candidate['prices'], candidate['payments'],
                                                 statements, CUTOFF, spec=candidate['statement_spec'])
    assert coverage['basis'] == 'payment'
    assert observations == []  # Publication is too late for a completed forward year.


def test_new_daily_cache_fallback_preserves_recorded_url_file_and_hash(tmp_path):
    inventory = fixture(tmp_path)
    old = tmp_path / 'raw/expanded/TEST3-prices.json'
    new = tmp_path / 'raw/inventory/daily-prices/TEST3.json'
    new.parent.mkdir(parents=True)
    old.rename(new)
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/TEST3.SA?range=10y&interval=1d&events=div%2Csplits'
    source = {'url': url, 'file': new.relative_to(tmp_path).as_posix(),
              'sha256': hashlib.sha256(new.read_bytes()).hexdigest(), 'authority': 'secondary'}
    write(tmp_path, 'raw/inventory/daily-summary.json', {'instruments': {'TEST3': {'source': source}}})
    before = hashes(tmp_path)
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded and candidates[0]['price_url'] == url
    assert candidates[0]['price_source_file'] == source['file']
    receipt = next(row for row in sources if row['file'] == source['file'])
    assert receipt['sha256'] == source['sha256'] and receipt['url'] == source['url']
    assert 'url_basis' not in receipt
    assert hashes(tmp_path) == before


def test_expanded_price_cache_has_priority_over_new_daily_data(tmp_path):
    inventory = fixture(tmp_path)
    write(tmp_path, 'raw/inventory/daily-prices/TEST3.json', {'must_not_be_selected': True})
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    assert candidates[0]['price_source_file'] == 'raw/expanded/TEST3-prices.json'
    assert all(row['file'] != 'raw/inventory/daily-prices/TEST3.json' for row in sources)


def test_incomplete_daily_summary_does_not_erase_hashable_daily_observations(tmp_path):
    inventory = fixture(tmp_path)
    old = tmp_path / 'raw/expanded/TEST3-prices.json'
    new = tmp_path / 'raw/inventory/daily-prices/TEST3.json'
    new.parent.mkdir(parents=True)
    old.rename(new)
    write(tmp_path, 'raw/inventory/daily-summary.json', b'{')
    candidates, excluded, sources = build_candidates(tmp_path, inventory, CUTOFF)
    assert not excluded
    assert 'interval=1d' in candidates[0]['price_url']
    receipt = next(row for row in sources if row['file'].endswith('daily-prices/TEST3.json'))
    assert receipt['url_basis'] == 'documented_collect_inventory_daily_request_template'


def test_nonprimary_registry_still_needs_cached_identity_detail(tmp_path):
    inventory = fixture(tmp_path)
    inventory['stocks'][0]['identity_status'] = 'verified_cached_registry'
    (tmp_path / 'raw/expanded/b3-detail-123.json').unlink()
    excluded, _ = pending(tmp_path, inventory)
    assert excluded['reason'] == 'identity_detail_cache_missing'


@pytest.mark.parametrize('cutoff', ['2026-09-29', '2099-09-30'])
def test_cutoff_must_be_valid_month_end(tmp_path, cutoff):
    with pytest.raises(ValueError):
        build_candidates(tmp_path, {'schema_version': 1, 'stocks': []}, cutoff)


def test_native_petrobras_ON_extraordinary_dividends_pass_same_independent_gates():
    from .market_inventory import load_inventory
    folder = Path(__file__).parent / 'data/market'
    original = folder / 'raw/income/petr-history.xlsx'
    if not original.is_file():
        pytest.skip('Native issuer workbook not installed in this checkout.')
    inventory = load_inventory(folder)
    inventory = {**inventory, 'stocks': [row for row in inventory['stocks'] if row['ticker'] == 'PETR3']}
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    candidates, excluded, _ = build_candidates(folder, inventory, CUTOFF)
    assert not excluded
    candidate, = candidates
    groups = {record: sum(row['gross_per_unit'] for row in candidate['ledger']
                         if row['record_date'] == record)
              for record in ('2024-05-02', '2024-12-11')}
    assert groups == pytest.approx({'2024-05-02': 1.69925676, '2024-12-11': 1.55174293})
    corrected = [row for row in candidate['ledger'] if row['record_date'] in groups]
    assert corrected and all(row['reported_income_label'] == 'Dividendoss Extraordin\u00e1rios'
                             for row in corrected)
    assert candidate['checks']['independent_cash_matches'] >= 4
    assert candidate['checks']['independent_price_matches'] >= 4
    assert not candidate['financially_validated']
    assert not candidate['checks']['point_in_time_archive_verified']
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
