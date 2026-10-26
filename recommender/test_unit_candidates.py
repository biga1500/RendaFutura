"""Synthetic contracts plus optional offline checks of actual cached sources."""

from copy import deepcopy
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

from lxml import html
import pytest

from .collect_market_data import month_end, shift_month
from .inventory_candidates import _Cache, _cash_pages
from .share_basis import b3_evidence, factor_after
from .test_inventory_candidates import fixture, hashes, write
from .unit_candidates import _prices, build_unit_candidate


CUTOFF = date(2026, 9, 30)
MARKET = Path(__file__).parent / 'data/market'


def fca(folder, year, *, expression='1 ON + 2 PN', listing='2010-01-01', newest_missing=False):
    """All issuer identities and numbers in this helper are synthetic fixtures."""
    master = {'CNPJ_CIA': '00.000.000/0001-00', 'CD_CVM': '123', 'DT_REFER': f'{year}-01-01',
              'DT_RECEB': f'{year}-03-01', 'VERSAO': '2', 'ID_DOC': f'test-{year}-2'}
    securities = []
    for ticker, kind, composition in [('TEST11', 'Units', expression),
                                     ('TEST3', 'Acoes Ordinarias', ''), ('TEST4', 'Acoes Preferenciais', '')]:
        securities.append({'CNPJ_Companhia': master['CNPJ_CIA'], 'ID_Documento': master['ID_DOC'],
            'Codigo_Negociacao': ticker, 'Valor_Mobiliario': kind, 'Sigla_Classe_Acao_Preferencial': '',
            'Composicao_BDR_Unit': composition, 'Data_Inicio_Negociacao': listing,
            'Data_Inicio_Listagem': listing, 'Data_Fim_Negociacao': '', 'Data_Fim_Listagem': ''})
    masters = [{**master, 'VERSAO': '1', 'ID_DOC': f'test-{year}-1'}, master]
    if newest_missing:
        masters.append({**master, 'VERSAO': '3', 'ID_DOC': f'test-{year}-3'})
    body = io.BytesIO()
    with zipfile.ZipFile(body, 'w') as archive:
        for name, rows in [(f'fca_cia_aberta_{year}.csv', masters),
                           (f'fca_cia_aberta_valor_mobiliario_{year}.csv', securities)]:
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, stream.getvalue().encode('latin1'))
    content = body.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    return write(folder, f'raw/primary-research/units/fca_cia_aberta_{year}-{digest}.zip', content)


def unit_fixture(folder):
    fixture(folder)
    row = {'ticker': 'TEST11', 'name': 'Synthetic UNIT', 'cvm': 123, 'share_class': None,
           'sector_code': 1, 'identity_status': 'official_cvm_fca', 'security_type': 'Units',
           'isin': 'BRTESTCDAM10'}
    inventory = {'schema_version': 1, 'stocks': [row], 'sources': []}
    write(folder, 'raw/expanded/b3-detail-123.json', {'codeCVM': '123', 'companyName': row['name'],
        'otherCodes': [{'code': 'TEST11', 'isin': row['isin']}, {'code': 'TEST3', 'isin': 'BRTESTACNOR0'},
                       {'code': 'TEST4', 'isin': 'BRTESTACNPR8'}]})
    original = json.loads((folder / 'raw/expanded/TEST3-prices.json').read_text())
    for ticker in ('TEST4', 'TEST11'):
        payload = deepcopy(original)
        result = payload['chart']['result'][0]
        result['meta']['symbol'] = ticker + '.SA'
        if ticker == 'TEST11':
            result['indicators']['quote'][0]['close'] = [30.] * len(result['timestamp'])
        write(folder, f'raw/expanded/{ticker}-prices.json', payload)
    page = json.loads((folder / 'raw/expanded/TEST3-b3-cash-1.json').read_text())
    cash = [*page['results']]
    for share, amount, price in [('PN', '1,00000000', '12,00'), ('UNT', '3,00000000', '30,00')]:
        cash.extend({**event, 'typeStock': share, 'valueCash': amount,
                     'closingPricePriorExDate': price} for event in page['results'])
    page.update(results=cash, page={'pageNumber': 1, 'totalPages': 1, 'totalRecords': len(cash)})
    write(folder, 'raw/expanded/TEST3-b3-cash-1.json', page)
    body = (folder / 'raw/expanded/TEST3-payments.html').read_bytes().replace(b'TEST3', b'TEST4')
    write(folder, 'raw/expanded/TEST4-payments.html', body)
    for year in range(2021, 2027):
        fca(folder, year)
    return inventory


def candidate(folder, inventory):
    return build_unit_candidate(inventory['stocks'][0], _Cache(folder, inventory), CUTOFF)


def test_unknown_unit_uses_actual_adapter_without_whitelist_or_network(tmp_path, monkeypatch):
    inventory = unit_fixture(tmp_path)
    before = hashes(tmp_path)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('offline adapter fetched'))
    cache = _Cache(tmp_path, inventory)
    result = build_unit_candidate(inventory['stocks'][0], cache, CUTOFF)
    assert result['statement_spec'] == (123, 'UNIT', 1)
    assert result['share_class'] == 'UNIT' and result['financially_validated'] is False
    assert len(result['prices']) == 61
    assert all(item[2] == 30 for item in result['prices'].values())
    assert all(item['gross_per_unit'] == 3 for item in result['ledger'])
    assert result['checks']['independent_price_class'] == 'UNT'
    assert result['checks']['calendar_basis'] == 'sum_exact_component_calendars_same_right_payment_dates'
    assert all(item['point_in_time_archive_verified'] is False for item in result['ledger'])
    assert {item['year'] for item in result['unit_composition']['evidence']} == set(range(2021, 2027))
    assert before == hashes(tmp_path)
    assert len([item for item in cache.receipts.values() if item['role'] == 'unit_historical_composition']) == 6


def test_nonunit_and_suffix_alone_do_not_infer_security_type(tmp_path):
    assert build_unit_candidate({'ticker': 'TEST3'}, _Cache(tmp_path, {'stocks': []}), CUTOFF) is None
    assert build_unit_candidate({'ticker': 'ZZZZ11'}, _Cache(tmp_path, {'stocks': []}), CUTOFF) is None


@pytest.mark.parametrize('field,value,reason', [
    ('isin', 'BRWRONCDAM10', 'unit_FCA_B3_ISIN_disagrees'),
    ('cvm', True, 'unit_exact_CVM_unavailable'),
    ('sector_code', None, 'unit_exact_sector_unavailable'),
    ('identity_status', 'guess', 'unit_exact_identity_unresolved'),
])
def test_exact_identity_cannot_be_relaxed(tmp_path, field, value, reason):
    inventory = unit_fixture(tmp_path)
    inventory['stocks'][0][field] = value
    with pytest.raises(ValueError, match=reason):
        candidate(tmp_path, inventory)


def test_component_prices_cannot_be_promoted_to_independent_unit_quotes(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-b3-cash-1.json'
    page = json.loads(path.read_text())
    page['results'] = [event for event in page['results'] if event['typeStock'] != 'UNT']
    page['page']['totalRecords'] = len(page['results'])
    write(tmp_path, path.relative_to(tmp_path).as_posix(), page)
    with pytest.raises(ValueError, match='unit_independent_cash_window_start_unverified'):
        candidate(tmp_path, inventory)


def test_mismatched_component_payment_dates_stay_pending(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST4-payments.html'
    write(tmp_path, path.relative_to(tmp_path).as_posix(), path.read_bytes().replace(b'15/08/2025', b'16/08/2025'))
    with pytest.raises(ValueError, match='unit_component_right_payment_dates_or_labels_disagree'):
        candidate(tmp_path, inventory)


def test_unknown_publication_on_one_class_cannot_hide_known_future_publication(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST3-payments.html'
    tree = html.fromstring(path.read_bytes())
    for row in tree.xpath('//tr'):
        row.remove(row[-1])
    write(tmp_path, path.relative_to(tmp_path).as_posix(), html.tostring(tree))
    path = tmp_path / 'raw/expanded/TEST4-payments.html'
    write(tmp_path, path.relative_to(tmp_path).as_posix(), path.read_bytes().replace(b'09/08/2025', b'01/10/2026'))
    with pytest.raises(ValueError, match='payment_publication_after_cutoff'):
        candidate(tmp_path, inventory)


def test_cancelled_component_payment_is_not_relabelled_as_a_past_schedule(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = tmp_path / 'raw/expanded/TEST4-payments.html'
    write(tmp_path, path.relative_to(tmp_path).as_posix(), path.read_bytes().replace(b'PAGO', b'CANCELADO'))
    with pytest.raises(ValueError, match='payment_status_unusable'):
        candidate(tmp_path, inventory)


def test_cancelled_current_issuer_registration_blocks_unit_purchase(tmp_path):
    from .issuer_registration import SOURCE_URL

    inventory = unit_fixture(tmp_path)
    inventory['as_of'] = '2026-10-24'
    body = (b'CD_CVM;DENOM_SOCIAL;SIT;DT_INI_SIT;DT_CANCEL;MOTIVO_CANCEL\n'
            b'123;Synthetic;CANCELADA;2026-10-01;2026-10-01;INCORPORACAO\n')
    digest = hashlib.sha256(body).hexdigest()
    relative = 'raw/primary-research/registration/cadastro-' + digest + '.csv'
    write(tmp_path, relative, body)
    write(tmp_path, 'raw/primary-research/registration/current.json', {'file': relative,
        'sha256': digest, 'url': SOURCE_URL, 'retrieved_at': '2026-10-25T01:00:00+00:00'})
    with pytest.raises(ValueError, match='cancelled_issuer_registration'):
        candidate(tmp_path, inventory)


def test_changed_historical_composition_and_missing_latest_table_stay_pending(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = next((tmp_path / 'raw/primary-research/units').glob('*2023-*.zip'))
    path.unlink()
    fca(tmp_path, 2023, expression='1 ON + 4 PN')
    with pytest.raises(ValueError, match='unit_composition_changed_within_window'):
        candidate(tmp_path, inventory)
    path = next((tmp_path / 'raw/primary-research/units').glob('*2023-*.zip'))
    path.unlink()
    fca(tmp_path, 2023, newest_missing=True)
    with pytest.raises(ValueError, match='unit_historical_FCA_2023_latest_security_table_missing'):
        candidate(tmp_path, inventory)


def test_missing_historical_archive_and_tampered_bytes_stay_pending(tmp_path):
    inventory = unit_fixture(tmp_path)
    path = next((tmp_path / 'raw/primary-research/units').glob('*2022-*.zip'))
    path.unlink()
    with pytest.raises(ValueError, match='unit_historical_FCA_2022_cache_missing'):
        candidate(tmp_path, inventory)
    path = fca(tmp_path, 2022)
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='unit_FCA_hash_mismatch'):
        candidate(tmp_path, inventory)


def cached_inventory():
    pointer = MARKET / 'inventory/current.json'
    if not pointer.is_file():
        pytest.skip('optional actual market cache is absent')
    snapshot = json.loads(pointer.read_text(encoding='utf-8'))['snapshot']
    return json.loads((MARKET / 'inventory' / snapshot / 'market.json').read_text(encoding='utf-8'))


def test_historical_acquisition_manifest_authenticates_original_bytes():
    paths = list((MARKET / 'raw/primary-research/units').glob('historical-fca-evidence-2026-10-25-*.json'))
    if not paths:
        pytest.skip('optional immutable historical FCA acquisition is absent')
    assert len(paths) == 1
    content = paths[0].read_bytes()
    assert hashlib.sha256(content).hexdigest() in paths[0].name
    manifest = json.loads(content)
    assert manifest['new_document_downloads'] == 5
    assert manifest['b3_network_requests'] == manifest['private_network_requests'] == 0
    assert len(manifest['sources']) == 6
    for source in manifest['sources']:
        content = (MARKET / source['file']).read_bytes()
        assert hashlib.sha256(content).hexdigest() == source['sha256']
        assert len(content) == source['bytes']
        assert source['url'].startswith('https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/')


def test_actual_cached_candidates_and_exact_pending_reasons(monkeypatch):
    inventory = cached_inventory()
    if not (MARKET / 'raw/primary-research/units').is_dir():
        pytest.skip('optional primary UNIT evidence is absent')
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('cache audit fetched'))
    cache = _Cache(MARKET, inventory)
    included, pending, nonunits = {}, {}, []
    for row in inventory['stocks']:
        if row['ticker'] not in ('ALUP11', 'BPAC11', 'BRBI11', 'ENGI11', 'IGTI11',
                                 'KLBN11', 'ONCO11', 'SANB11', 'SAPR11', 'TAEE11'):
            continue
        try:
            result = build_unit_candidate(row, cache, CUTOFF)
            if result is None:
                nonunits.append(row['ticker'])
            else:
                included[row['ticker']] = result
        except ValueError as error:
            pending[row['ticker']] = str(error)
    assert set(included) == {'ALUP11', 'ENGI11', 'SAPR11', 'TAEE11'}
    assert pending == {
        'BPAC11': 'historical_quote_or_share_scale_disagrees_with_B3',
        'BRBI11': 'unit_exact_identity_cache_missing',
        'IGTI11': 'unit_listing_does_not_cover_return_window',
        'KLBN11': 'unverified_share_price_regime_requires_raw_history',
        'SANB11': 'historical_quote_or_share_scale_disagrees_with_B3'}
    assert nonunits == ['ONCO11']
    assert {key: len(value['ledger']) for key, value in included.items()} == {
        'ALUP11': 19, 'ENGI11': 10, 'SAPR11': 11, 'TAEE11': 29}


@pytest.mark.parametrize('ticker,cvm,expected_conflicts', [
    ('BPAC11', 22616, {'2021-12-20': (21.84, 21.690000534057617),
                      '2022-12-16': (20.75, 20.959999084472656)}),
    ('SANB11', 20532, {'2022-01-07': (31.33, 31.139999389648438)}),
])
def test_actual_unit_quote_conflicts_are_not_discarded_to_admit_banks(ticker, cvm, expected_conflicts):
    inventory = cached_inventory()
    cache = _Cache(MARKET, inventory)
    detail = cache.json(f'raw/expanded/b3-detail-{cvm}.json', 'unit_identity')
    codes = {row['code']: row['isin'] for row in detail['otherCodes']}
    _, anchors = b3_evidence(_cash_pages(ticker, codes, cvm, cache), 'UNT')
    content, actions, _ = _prices(ticker, cache, CUTOFF)
    result = json.loads(content)['chart']['result'][0]
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): value
             for stamp, value in zip(result['timestamp'], result['indicators']['quote'][0]['close'])}
    conflicts = {}
    for day, raw in anchors.items():
        if shift_month(CUTOFF, -60) <= day <= CUTOFF:
            # Neither bank has a share action inside the actual return window.
            assert factor_after(day, actions, CUTOFF) == 1
            if abs(daily[day] - raw) > max(.015, .006 * max(abs(daily[day]), abs(raw))):
                conflicts[day.isoformat()] = raw, daily[day]
    assert conflicts == expected_conflicts
    row = next(row for row in inventory['stocks'] if row['ticker'] == ticker)
    with pytest.raises(ValueError, match='historical_quote_or_share_scale_disagrees_with_B3'):
        build_unit_candidate(row, cache, CUTOFF)


def test_actual_klabin_needs_an_independent_unit_quote_after_last_bonus():
    inventory = cached_inventory()
    cache = _Cache(MARKET, inventory)
    detail = cache.json('raw/expanded/b3-detail-12653.json', 'unit_identity')
    codes = {row['code']: row['isin'] for row in detail['otherCodes']}
    _, anchors = b3_evidence(_cash_pages('KLBN11', codes, 12653, cache), 'UNT')
    content, actions, _ = _prices('KLBN11', cache, CUTOFF)
    result = json.loads(content)['chart']['result'][0]
    monthly = {}
    for stamp in result['timestamp']:
        day = datetime.fromtimestamp(stamp, timezone.utc).date()
        if day <= CUTOFF:
            month = month_end(day.year, day.month)
            monthly[month] = max(monthly.get(month, day), day)
    observed = {round(factor_after(day, actions, CUTOFF), 12) for day in anchors
                if shift_month(CUTOFF, -60) <= day <= CUTOFF}
    required = {round(factor_after(monthly[shift_month(CUTOFF, -offset)], actions, CUTOFF), 12)
                for offset in range(61)}
    assert observed == {1.01, 1.111}
    assert required - observed == {1.0}
    assert max(day for day in anchors if day <= CUTOFF) == date(2025, 12, 15)
    assert actions[-1] == {'date': '2025-12-18', 'ratio': 1.01}
