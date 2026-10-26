"""Exact-code fixtures and offline replay of the complete real catalogue."""

from copy import deepcopy
import csv
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

import pytest

from . import security_registration as module
from .issuer_registration import FIELDS, SOURCE_URL
from .security_registration import (FCA_URL, MASTER_FIELDS, SECURITY_FIELDS, SecurityRegistry,
                                    evidence_report, parse_fca_registry, security_status, write_evidence)


MARKET = Path(__file__).parent / 'data/market'
WHEN = '2026-10-25'


def filing(**changes):
    return {'CNPJ_CIA': '00.000.000/0001-00', 'DT_REFER': '2026-01-01', 'VERSAO': '1',
            'CD_CVM': '123', 'ID_DOC': '1000', 'DT_RECEB': '2026-03-01', **changes}


def security(**changes):
    return {**dict.fromkeys(SECURITY_FIELDS, ''),
            'CNPJ_Companhia': '00.000.000/0001-00', 'Data_Referencia': '2026-01-01',
            'Versao': '1', 'ID_Documento': '1000', 'Nome_Empresarial': 'Synthetic issuer',
            'Valor_Mobiliario': 'Acoes Ordinarias', 'Codigo_Negociacao': 'TEST3',
            'Mercado': 'Bolsa', 'Sigla_Entidade_Administradora': 'B3',
            'Segmento': 'Basico', 'Data_Inicio_Negociacao': '2020-01-01',
            'Data_Inicio_Listagem': '2020-01-01', **changes}


def archive(masters=None, securities=None, *, year=2026):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as zipped:
        for name, rows, fields in [(f'fca_cia_aberta_{year}.csv', masters or [filing()], MASTER_FIELDS),
                (f'fca_cia_aberta_valor_mobiliario_{year}.csv', securities or [security()], SECURITY_FIELDS)]:
            text = io.StringIO()
            writer = csv.DictWriter(text, fieldnames=fields, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            zipped.writestr(name, text.getvalue().encode('latin1'))
    return stream.getvalue()


def registry(tmp_path, *, masters=None, securities=None, rows=None, details=None, cad=None):
    path = tmp_path / 'raw/inventory/fca_cia_aberta_2026.zip'
    path.parent.mkdir(parents=True, exist_ok=True)
    body = archive(masters, securities)
    path.write_bytes(body)
    inventory = {'as_of': WHEN, 'stocks': rows or [{'ticker': 'TEST3', 'cvm': 123}],
                 'sources': [{'file': path.relative_to(tmp_path).as_posix(),
                              'url': FCA_URL + path.name, 'sha256': hashlib.sha256(body).hexdigest()}]}
    for cvm, codes in (details or {}).items():
        target = tmp_path / f'raw/expanded/b3-detail-{cvm}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'codeCVM': str(cvm), 'status': 'A', 'otherCodes': codes}), encoding='utf-8')
    if cad:
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=FIELDS, delimiter=';')
        writer.writeheader()
        writer.writerows(cad)
        body = text.getvalue().encode('latin1')
        digest = hashlib.sha256(body).hexdigest()
        target = tmp_path / 'raw/primary-research/registration'
        target.mkdir(parents=True)
        file = target / f'cadastro-{digest}.csv'
        file.write_bytes(body)
        (target / 'current.json').write_text(json.dumps({'file': file.relative_to(tmp_path).as_posix(),
            'sha256': digest, 'url': SOURCE_URL, 'retrieved_at': WHEN + 'T12:00:00+00:00'}), encoding='utf-8')
    return SecurityRegistry(tmp_path, inventory=inventory)


def test_exact_FCA_code_not_suffix_defines_type_and_class(tmp_path):
    reader = registry(tmp_path, securities=[security(Codigo_Negociacao='TEST99')])
    result = security_status({'ticker': 'TEST99', 'share_class': 'PN'}, reader, WHEN)
    assert result['security_type'] == 'ordinary_share' and result['share_class'] == 'ON'
    assert result['cvm'] == 123 and result['cvm_basis'] == 'primary_exact_code'
    assert result['isin'] is None and result['isin_basis'] == 'not_established'
    assert not result['exchange_trading_verified'] and not result['model_admission_granted']
    assert result['evidence'][0]['filing_id'] == '1000'
    assert result['evidence'][0]['csv_record'] == 2
    assert result['evidence'][0]['source']['original_receipt_verified']


@pytest.mark.parametrize('reported,expected', [('', 'PN'), ('A', 'PNA'), ('PNA', 'PNA'),
    ('B', 'PNB'), ('PNB', 'PNB'), ('C', 'PNC'), ('D', 'PND'), ('ONA', None), ('UNKNOWN', None)])
def test_preferred_class_is_never_replaced_with_suffix_guess(tmp_path, reported, expected):
    reader = registry(tmp_path, securities=[security(Valor_Mobiliario='Acoes Preferenciais',
                                                    Sigla_Classe_Acao_Preferencial=reported)])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['security_type'] == 'preferred_share' and result['share_class'] == expected
    assert ('reported_preferred_class_not_recognized_no_substitution' in result['issues']) == (expected is None)


@pytest.mark.parametrize('kind,expected,blocked', [('Units', 'unit', False),
    ('Bonus de Subscricao', 'subscription_warrant', True),
    ('Direitos de Subscricao', 'subscription_right', True),
    ('Debentures', 'other_reported_security', False)])
def test_non_share_types_need_explicit_primary_rows(tmp_path, kind, expected, blocked):
    reader = registry(tmp_path, securities=[security(Valor_Mobiliario=kind)])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['security_type'] == expected and result['blocks_current_purchase'] == blocked
    assert result['catalogue_member_preserved']


@pytest.mark.parametrize('market,expected', [('Bolsa', 'exchange'), ('Balcao Organizado', 'organized_otc'),
    ('Balcao Nao-Organizado', 'unorganized_otc'), ('', 'other_reported_market')])
def test_market_is_reported_not_inferred_from_trailing_B(tmp_path, market, expected):
    reader = registry(tmp_path, securities=[security(Codigo_Negociacao='TEST3B', Mercado=market)])
    result = reader.status({'ticker': 'TEST3B'}, WHEN)
    assert result['market'] == expected and result['security_type'] == 'ordinary_share'


def test_issuer_and_related_code_do_not_establish_missing_alias(tmp_path):
    reader = registry(tmp_path)
    result = reader.status({'ticker': 'TEST3B', 'name': 'Synthetic issuer', 'cvm': 123,
                            'share_class': 'ON', 'isin': 'BRTESTACNOR0'}, WHEN)
    assert result['status'] == 'unresolved' and result['cvm'] is None and result['isin'] is None
    assert result['security_type'] is None and not result['blocks_current_purchase']
    assert result['related_issuer_research_leads_not_code_identity'][0]['reported_code'] == 'TEST3'
    assert not result['whole_security_delisting_verified']


def test_same_code_conflicting_classes_are_not_chosen_by_row_order(tmp_path):
    first, second = security(), security(Valor_Mobiliario='Acoes Preferenciais',
                                        Sigla_Classe_Acao_Preferencial='ONA')
    for records in ([first, second], [second, first]):
        reader = registry(tmp_path, securities=records)
        result = reader.status({'ticker': 'TEST3'}, WHEN)
        assert result['security_type'] is None and result['share_class'] is None
        assert result['type_confidence'] == 'unresolved'
        assert 'conflicting_exact_code_primary_security_types' in result['issues']
        assert len(result['evidence']) == 2


def test_segment_end_and_later_open_episode_are_not_delisting(tmp_path):
    reader = registry(tmp_path, securities=[security(Data_Fim_Negociacao='2021-05-07',
        Data_Fim_Listagem='2021-05-07'), security(Segmento='Novo Mercado',
        Data_Inicio_Negociacao='2021-05-10', Data_Inicio_Listagem='2021-05-10')])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['listing_status'] == 'declared_open_episode_not_current_trading_confirmation'
    assert result['episode_dates'][0]['Data_Fim_Listagem'] == '2021-05-07'
    assert not result['blocks_current_purchase'] and not result['whole_security_delisting_verified']


def test_closed_episode_without_reopening_still_not_legal_delisting_confirmation(tmp_path):
    reader = registry(tmp_path, securities=[security(Data_Fim_Listagem='2024-01-01')])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['listing_status'] == 'declared_closed_episodes_current_status_unresolved'
    assert not result['whole_security_delisting_verified'] and not result['blocks_current_purchase']
    assert result['episode_dates'][0]['Data_Fim_Listagem'] == '2024-01-01'


def test_future_end_or_start_is_interpreted_only_at_reference(tmp_path):
    reader = registry(tmp_path, securities=[security(Data_Fim_Negociacao='2026-11-01')])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['listing_status'].startswith('declared_open')
    reader = registry(tmp_path, securities=[security(Data_Inicio_Negociacao='2026-11-01')])
    assert reader.status({'ticker': 'TEST3'}, WHEN)['listing_status'] == 'listing_start_not_established_at_reference'


def test_future_filing_is_not_available_for_historical_reference(tmp_path):
    reader = registry(tmp_path, masters=[filing(DT_RECEB='2026-10-26')])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['security_type'] is None and not result['evidence']


def test_latest_missing_child_table_does_not_backfill_old_type(tmp_path):
    reader = registry(tmp_path, masters=[filing(), filing(VERSAO='2', ID_DOC='1001', DT_RECEB='2026-04-01')])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['security_type'] is None
    assert 'latest_FCA_security_table_missing_or_exact_code_not_reported' in result['issues']
    assert not result['evidence'][0]['latest_child_table_available']


def test_latest_annual_archive_is_not_backfilled_by_previous_year(tmp_path):
    reader = registry(tmp_path, securities=[security(Codigo_Negociacao='NEWC3')])
    previous = archive([filing(DT_REFER='2025-01-01', DT_RECEB='2025-03-01', ID_DOC='900')],
        [security(Data_Referencia='2025-01-01', ID_Documento='900')], year=2025)
    path = tmp_path / 'raw/inventory/fca_cia_aberta_2025.zip'
    path.write_bytes(previous)
    inventory = deepcopy(reader.inventory)
    inventory['sources'].append({'file': path.relative_to(tmp_path).as_posix(),
        'sha256': hashlib.sha256(previous).hexdigest(), 'url': FCA_URL + path.name})
    for sources in (inventory['sources'], list(reversed(inventory['sources']))):
        actual = SecurityRegistry(tmp_path, inventory={**inventory, 'sources': sources})
        result = actual.status({'ticker': 'TEST3'}, WHEN)
        assert result['security_type'] is None and len(result['evidence']) == 1
        assert not result['evidence'][0]['is_latest_received_filing']
        historical = actual.status({'ticker': 'TEST3'}, '2025-10-25')
        assert historical['security_type'] == 'ordinary_share'


def test_exact_cache_ISIN_and_CVM_do_not_define_security_type(tmp_path):
    reader = registry(tmp_path, details={999: [{'code': 'TEST97', 'isin': 'BRTESTCDAXX7'}]})
    result = reader.status({'ticker': 'TEST97'}, WHEN)
    assert result['cvm'] == 999 and result['isin'] == 'BRTESTCDAXX7'
    assert result['security_type'] is None and result['share_class'] is None
    assert result['isin_basis'].endswith('not_FCA_reported_ISIN')
    assert not result['cached_exchange_identity'][0]['source']['original_receipt_verified']


@pytest.mark.parametrize('row', [{'ticker': 'TEST3', 'cvm': 456},
                              {'ticker': 'TEST3', 'isin': 'BRTESTCDAXX7'}])
def test_identity_conflicts_remain_unresolved(tmp_path, row):
    reader = registry(tmp_path, details={123: [{'code': 'TEST3', 'isin': 'BRTESTACNOR0'}]})
    result = reader.status(row, WHEN)
    assert result['type_confidence'] == 'unresolved' and result['security_type'] is None
    assert not result['blocks_current_purchase']


@pytest.mark.parametrize('state,blocked', [('ATIVO', False), ('CANCELADA', True)])
def test_CAD_applies_only_to_verified_issuer_not_security_trading(tmp_path, state, blocked):
    cad = dict(zip(FIELDS, ('123', 'Synthetic issuer', state, '2024-01-01',
                          '2024-01-01' if blocked else '', 'INCORPORACAO' if blocked else '')))
    reader = registry(tmp_path, cad=[cad])
    result = reader.status({'ticker': 'TEST3'}, WHEN)
    assert result['blocks_current_purchase'] == blocked
    assert not result['whole_security_delisting_verified'] and not result['exchange_trading_verified']
    unknown = reader.status({'ticker': 'MISS3', 'cvm': 123}, WHEN)
    assert unknown['cvm'] is None and not unknown['blocks_current_purchase']


def test_secondary_INDEX_and_EQUITY_labels_are_context_only(tmp_path):
    reader = registry(tmp_path)
    for ticker, kind in [('BRAX', 'INDEX'), ('TEST97', 'EQUITY')]:
        path = tmp_path / f'raw/inventory/quotes/{ticker}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'chart': {'result': [{'meta': {'symbol': ticker + '.SA',
                                                                'instrumentType': kind}}]}}), encoding='utf-8')
        result = reader.status({'ticker': ticker}, WHEN)
        assert result['security_type'] is None and not result['blocks_current_purchase']
        assert result['secondary_provider_context']['reported_instrument_type'] == kind


def test_read_only_calls_no_network_mutation_or_writes(tmp_path, monkeypatch):
    reader = registry(tmp_path)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('network forbidden'))
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    row = {'ticker': 'TEST3', 'cvm': 123, 'quote': {'value': 9}, 'model_ready': False}
    original = deepcopy(row)
    result = security_status(row, reader, date(2026, 10, 25))
    result['evidence'][0]['security']['Codigo_Negociacao'] = 'MUTATED'
    assert reader.status(row, WHEN)['evidence'][0]['security']['Codigo_Negociacao'] == 'TEST3'
    monkeypatch.setattr(module, 'load_inventory', lambda folder: reader.inventory)
    assert security_status(row, tmp_path, WHEN)['security_type'] == 'ordinary_share'
    assert row == original
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('when', ['2026-1-1', '2026-02-30', datetime(2026, 10, 25), None])
def test_ambiguous_or_invalid_dates_rejected(tmp_path, when):
    reader = registry(tmp_path)
    with pytest.raises(ValueError):
        reader.status({'ticker': 'TEST3'}, when)


def test_missing_or_changed_primary_receipt_fails_closed(tmp_path):
    reader = registry(tmp_path)
    path = tmp_path / reader.inventory['sources'][0]['file']
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='hash_mismatch'):
        SecurityRegistry(tmp_path, inventory=reader.inventory)
    with pytest.raises(ValueError, match='receipt_missing'):
        SecurityRegistry(tmp_path, inventory={'stocks': [], 'sources': []})


@pytest.mark.parametrize('relative', ['../outside.csv', 'raw/../../outside.csv', 'raw\\..\\outside.csv',
                                     'C:/outside.csv', '/outside.csv'])
def test_unsafe_sources_rejected(tmp_path, relative):
    reader = registry(tmp_path)
    with pytest.raises(ValueError, match='unsafe'):
        reader.read(relative, authority='test')


@pytest.mark.parametrize('mutation', ['bad_cvm', 'bad_date', 'child_identity', 'child_version'])
def test_malformed_primary_tables_rejected(mutation):
    master, child = filing(), security()
    if mutation == 'bad_cvm': master['CD_CVM'] = 'UNKNOWN'
    if mutation == 'bad_date': child['Data_Fim_Listagem'] = '2024-1-1'
    if mutation == 'child_identity': child['CNPJ_Companhia'] = 'wrong'
    if mutation == 'child_version': child['Versao'] = '2'
    with pytest.raises(ValueError):
        parse_fca_registry(archive([master], [child]), 2026)


def test_content_addressed_report_is_only_explicit_new_write(tmp_path):
    report = {'as_of': WHEN, 'records': [{'ticker': 'TEST3'}], 'counts': {'catalogue_codes': 1}}
    path = write_evidence(tmp_path, report)
    original = path.read_bytes()
    assert hashlib.sha256(original).hexdigest() in path.name
    assert write_evidence(tmp_path, report) == path and path.read_bytes() == original
    assert not (path.parent / 'current.json').exists()


@pytest.fixture(scope='module')
def actual_report():
    if not (MARKET / 'inventory/current.json').is_file():
        pytest.skip('real offline cache is not installed')
    return evidence_report(MARKET, WHEN)


def test_actual_all_468_are_preserved_without_model_admission(actual_report):
    report = actual_report
    assert report['counts']['catalogue_codes'] == report['counts']['preserved_codes'] == 468
    assert report['network_requests'] == 0 and not report['active_model_changed']
    assert not report['active_inventory_changed']
    assert all(r['catalogue_member_preserved'] and not r['model_admission_granted'] for r in report['records'])
    assert sum(report['counts']['type_confidence'].values()) == 468


@pytest.mark.parametrize('ticker', ['ABCB10', 'AZUL97', 'AZUL98', 'AZUL99', 'BRAX', 'MRSA3B'])
def test_actual_no_primary_exact_type_is_invented_for_unsupported_codes(actual_report, ticker):
    row = actual_report['focus'][ticker]
    assert row['security_type'] is None and row['type_confidence'] == 'unresolved'
    assert not row['blocks_current_purchase'] and not row['whole_security_delisting_verified']
    if ticker.startswith('AZUL'):
        assert row['cvm'] == 24112 and row['isin'].startswith('BRAZUL')
        assert row['isin_basis'].endswith('not_FCA_reported_ISIN')


def test_actual_CRTE3_conflict_and_exact_EQMA3B_OTC_proof(actual_report):
    conflict = actual_report['focus']['CRTE3']
    assert conflict['cvm'] == 19208 and conflict['security_type'] is None
    assert 'conflicting_exact_code_primary_security_types' in conflict['issues']
    otc = actual_report['focus']['EQMA3B']
    assert (otc['cvm'], otc['share_class'], otc['market']) == (16608, 'ON', 'organized_otc')
    assert otc['evidence'][0]['filing_id'] == '158240'


def test_actual_missing_daily_histories_are_not_inferred_to_be_delisted(actual_report):
    from collections import Counter
    from .market_inventory import load_inventory
    inventory = load_inventory(MARKET)
    expected = {row['ticker'] for row in inventory['stocks']
                if row.get('model_pending_reason') == 'daily_prices_cache_missing'}
    assert set(actual_report['daily_prices_cache_missing_codes']) == expected
    summary = actual_report['daily_prices_cache_missing_summary']
    rows = [row for row in actual_report['records'] if row['ticker'] in expected]
    assert summary['codes'] == len(expected) and summary['closed_episode_codes'] == []
    assert all(row['security_type'] in ('ordinary_share', 'preferred_share') for row in rows)
    assert summary['security_type'] == dict(Counter(row['security_type'] for row in rows))
    assert summary['market'] == dict(Counter(row['market'] for row in rows))
    assert all(not row['whole_security_delisting_verified'] for row in rows)


def test_actual_cancelled_issuers_and_ONCO_warrant_remain_blocked(actual_report):
    rows = {r['ticker']: r for r in actual_report['records']}
    cancelled = [t for t, r in rows.items() if 'cancelled_issuer_registration' in r['block_reasons']]
    assert sorted(cancelled) == ['MERC3', 'MERC4', 'NGRD3', 'SHOW3', 'TKNO3', 'TKNO4']
    assert rows['ONCO11']['security_type'] == 'subscription_warrant' and rows['ONCO11']['blocks_current_purchase']
    assert rows['ONCO11']['reviewed_registry']['expires_on'] == '2027-11-19'


@pytest.mark.parametrize('ticker', ['PDTC3', 'NEXP3', 'GSHP3', 'NGRD3', 'PRNR3', 'RVEE3'])
def test_actual_closed_segments_have_later_open_episodes(actual_report, ticker):
    row = next(r for r in actual_report['records'] if r['ticker'] == ticker)
    assert any(e['ended_by_reference'] for e in row['episode_dates'])
    assert row['listing_status'] == 'declared_open_episode_not_current_trading_confirmation'
    assert not row['whole_security_delisting_verified']


@pytest.mark.parametrize('ticker', ['CEAB3', 'FIEI3', 'RAIZ4'])
def test_actual_closed_only_FCA_rows_do_not_force_delisting(actual_report, ticker):
    row = next(r for r in actual_report['records'] if r['ticker'] == ticker)
    assert row['listing_status'] == 'declared_closed_episodes_current_status_unresolved'
    assert not row['blocks_current_purchase'] and not row['whole_security_delisting_verified']
