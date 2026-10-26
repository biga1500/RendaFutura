"""Offline primary-evidence checks and explicitly synthetic EPS contract fixtures."""

from copy import deepcopy
import csv
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
import hashlib
import html
import io
import json
from pathlib import Path
import urllib.request
import zipfile

import pytest

from . import unit_composition as units


AS_OF = '2026-10-25'
MARKET = Path(__file__).parent / 'data/market'
RESEARCH = MARKET / units.EVIDENCE_ROOT
MANIFEST = MARKET / units.EVIDENCE_MANIFEST
EXPECTED = {
    'ALUP11': (21490, {'ON': 1, 'PN': 2}, '161432', '1 ON E 2 PN'),
    'BPAC11': (22616, {'ON': 1, 'PNA': 2}, '158545', '1 ON E 2 PNA'),
    'BRBI11': (25860, {'ON': 1, 'PN': 2}, '157515', '2 a\u00e7\u00f5es preferenciais e 1 a\u00e7\u00e3o ordin\u00e1ria'),
    'ENGI11': (15253, {'ON': 1, 'PN': 4}, '158184', '1 a\u00e7\u00e3o ordin\u00e1ria e 4 a\u00e7\u00f5es preferenciais'),
    'IGTI11': (8672, {'ON': 1, 'PN': 2}, '158558', '1 ON e 2 PN'),
    'KLBN11': (12653, {'ON': 1, 'PN': 4}, '155729', '1 KLBN3 + 4 KLBN4'),
    'SANB11': (20532, {'ON': 1, 'PN': 1}, '156191', '1 ON + 1 PN'),
    'SAPR11': (18627, {'ON': 1, 'PN': 4}, '159163', '1 ON e 4 PN'),
    'TAEE11': (20257, {'ON': 1, 'PN': 2}, '158852', '1 ON / 2 PN'),
}


def csv_rows(archive, member):
    with archive.open(member) as stream:
        return list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))


def synthetic_classes(ticker='ALUP11'):
    """Numbers and proof references below are test fixtures, not researched EPS."""
    record = units.UNIT_COMPOSITIONS[ticker]
    result = {}
    for index, component in enumerate(record.components):
        share_class = component.share_class
        field = {
            'value': 2.0 + index, 'unit': 'BRL/share', 'status': 'available',
            'period_start': '2026-01-01', 'period_end': '2026-06-30', 'period_type': 'ytd',
            'annualized': False, 'scope': 'consolidated', 'document_type': 'ITR',
            'filing_id': 'SYNTHETIC-FILING', 'source_ref': 'SYNTHETIC-ITR',
            'published_at': '2026-08-10', 'version': 1,
        }
        result[share_class] = {
            'cvm': record.cvm, 'share_class': share_class, 'fields': {'basic_eps': field},
            'eps_basis': {
                'status': 'verified', 'denominator': 'one_class_share', 'share_class': share_class,
                'share_basis_date': AS_OF, 'method': 'explicitly_reconciled_class_per_share',
                'field_source_ref': field['source_ref'], 'filing_id': field['filing_id'],
                'evidence': {
                    'authority': 'CVM_DFP_ITR',
                    'url': 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_2026.zip',
                    'file': 'SYNTHETIC-TEST-ONLY.zip', 'sha256': 'a' * 64,
                    'locator': 'SYNTHETIC fixture: exact class EPS basis', 'published_at': '2026-08-10',
                },
            },
        }
    return result


def eps_result(classes=None, ticker='ALUP11', **options):
    return units.map_unit_fundamentals(ticker, synthetic_classes(ticker) if classes is None else classes,
                                       AS_OF, **options)['fields']['basic_eps']


def test_registry_is_immutable_complete_and_independent_of_network(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail('Unit registry must be pure and offline.')

    monkeypatch.setattr(urllib.request, 'urlopen', no_network)
    assert units.validate_registry() == {}
    assert set(units.UNIT_COMPOSITIONS) == set(EXPECTED) | {'ONCO11'}
    with pytest.raises(TypeError):
        units.UNIT_COMPOSITIONS['TEST11'] = units.UNIT_COMPOSITIONS['ALUP11']
    with pytest.raises(FrozenInstanceError):
        units.UNIT_COMPOSITIONS['ALUP11'].components[0].shares = 100
    result = units.get_unit_composition('ALUP11', AS_OF)
    result['components'][0]['shares'] = 100
    assert units.get_unit_composition('ALUP11', AS_OF)['components'][0]['shares'] == 1


@pytest.mark.parametrize('ticker', EXPECTED)
def test_exact_ratios_issuer_and_live_fca_identity_are_supported_by_original_bytes(ticker):
    cvm, ratios, filing_id, original = EXPECTED[ticker]
    record = units.get_unit_composition(ticker, AS_OF)
    assert record['status'] == 'verified'
    assert record['cvm'] == cvm
    assert {item['share_class']: item['shares'] for item in record['components']} == ratios
    with zipfile.ZipFile(MARKET / record['evidence'][0]['file']) as archive:
        masters = csv_rows(archive, 'fca_cia_aberta_2026.csv')
        master = next(row for row in masters if row['ID_DOC'] == filing_id)
        eligible = [row for row in masters if int(row['CD_CVM']) == cvm
                    and row['DT_REFER'] <= AS_OF and row['DT_RECEB'] <= AS_OF]
        assert max(eligible, key=lambda row: (row['DT_REFER'], int(row['VERSAO']))) == master
        assert master['CNPJ_CIA'] == record['cnpj']
        assert master['DENOM_CIA'].upper() == record['issuer'].upper()
        assert master['DT_RECEB'] == record['evidence'][0]['published_at']
        rows = [row for row in csv_rows(archive, 'fca_cia_aberta_valor_mobiliario_2026.csv')
                if row['ID_Documento'] == filing_id]
        unit = next(row for row in rows if row['Valor_Mobiliario'] == 'Units')
        assert unit['Composicao_BDR_Unit'] == original
        assert unit['Codigo_Negociacao'] == ('000000' if ticker == 'BPAC11' else ticker)
        assert not unit['Data_Fim_Listagem'] and not unit['Data_Fim_Negociacao']
        for component in record['components']:
            if component['ticker'] is not None and ticker != 'KLBN11':
                raw = next(row for row in rows if row['Codigo_Negociacao'] == component['ticker'])
                assert raw['Valor_Mobiliario'] == ('A\u00e7\u00f5es Ordin\u00e1rias'
                                                   if component['share_class'] == 'ON'
                                                   else 'A\u00e7\u00f5es Preferenciais')


def test_original_evidence_hashes_and_manifest_are_consistent():
    manifest_bytes = MANIFEST.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == units.MANIFEST_SHA256
    manifest = json.loads(manifest_bytes)
    assert manifest['as_of'] == AS_OF
    assert manifest['new_document_downloads'] == 3
    assert manifest['b3_network_requests'] == 0
    source_files = {source['file']: source for source in manifest['sources']}
    for source in manifest['sources']:
        content = (MARKET / source['file']).read_bytes()
        assert len(content) == source['bytes']
        assert hashlib.sha256(content).hexdigest() == source['sha256']
        assert source['sha256'] in source['file']
    for record in units.UNIT_COMPOSITIONS.values():
        for evidence in record.evidence:
            assert source_files[evidence.file]['sha256'] == evidence.sha256


def test_btg_unresolved_fca_ticker_is_resolved_only_by_primary_issuer_text():
    record = units.UNIT_COMPOSITIONS['BPAC11']
    page = html.unescape((MARKET / record.evidence[1].file).read_text(encoding='utf-8'))
    assert 'As Units BPAC11 s\u00e3o atualmente compostas' in page
    assert 'duas a\u00e7\u00f5es preferenciais Classe A' in page
    assert 'uma a\u00e7\u00e3o ordin\u00e1ria do Banco BTG Pactual' in page
    assert [component.share_class for component in record.components] == ['ON', 'PNA']


def test_klabin_class_tickers_are_explicit_in_issuer_faq():
    record = units.UNIT_COMPOSITIONS['KLBN11']
    page = html.unescape((MARKET / record.evidence[1].file).read_text(encoding='utf-8'))
    assert 'Preferenciais: KLBN4' in page
    assert 'Ordin\u00e1rias: KLBN3' in page
    assert '1 a\u00e7\u00e3o ordin\u00e1ria e 4 a\u00e7\u00f5es preferenciais' in page


def test_onco11_is_a_warrant_and_fre_future_reference_label_is_not_a_publication_date():
    record = units.get_unit_composition('ONCO11', AS_OF)
    assert record['cvm'] == 26123
    assert record['status'] == 'not_a_unit'
    assert record['components'] == []
    assert record['instrument_type'] == 'subscription_warrant'
    assert record['effective_from'] == '2025-11-19'
    assert record['instrument_expires_on'] == '2027-11-19'
    with zipfile.ZipFile(MARKET / record['evidence'][1]['file']) as archive:
        masters = csv_rows(archive, 'fre_cia_aberta_2026.csv')
        latest = max((row for row in masters if int(row['CD_CVM']) == 26123
                      and row['DT_RECEB'] <= AS_OF), key=lambda row: int(row['VERSAO']))
        assert (latest['ID_DOC'], latest['DT_RECEB'], latest['DT_REFER']) == (
            '161691', '2026-10-14', '2026-12-31')
        warrant = next(row for row in csv_rows(archive, 'fre_cia_aberta_outro_valor_mobiliario_2026.csv')
                       if row['ID_Documento'] == '161691'
                       and row['Valor_Mobiliario'] == 'B\u00f4nus de Subscri\u00e7\u00e3o'
                       and row['Data_Emissao'] == '2025-11-19')
        assert warrant['Data_Emissao'] == record['effective_from']
        assert warrant['Data_Vencimento'] == record['instrument_expires_on']
    result = eps_result({}, 'ONCO11')
    assert result['status'] == 'not_applicable' and result['value'] is None


@pytest.mark.parametrize('ticker', EXPECTED)
def test_formal_ratio_without_eps_basis_never_supplies_eps(ticker):
    assert eps_result({}, ticker)['reason'] == 'component_fundamentals_missing'
    classes = synthetic_classes(ticker)
    for row in classes.values():
        row.pop('eps_basis')
    field = eps_result(classes, ticker)
    assert (field['status'], field['reason'], field['value']) == ('missing', 'eps_basis_unverified', None)


@pytest.mark.parametrize('ticker', EXPECTED)
def test_verified_compatible_class_eps_is_summed_per_unit_not_averaged(ticker):
    classes = synthetic_classes(ticker)
    before = deepcopy(classes)
    field = eps_result(classes, ticker)
    ratio = EXPECTED[ticker][1]
    assert field['status'] == 'available'
    assert field['value'] == sum(classes[name]['fields']['basic_eps']['value'] * count
                                 for name, count in ratio.items())
    assert field['unit'] == 'BRL/unit' and field['annualized'] is False
    assert field['period_type'] == 'ytd'
    assert field['share_basis_date'] == AS_OF
    assert classes == before
    assert len(field['components']) == 2
    json.dumps(field, allow_nan=False)


def test_btg_generic_pn_cannot_substitute_for_pna():
    classes = synthetic_classes('BPAC11')
    classes['PN'] = classes.pop('PNA')
    field = eps_result(classes, 'BPAC11')
    assert field['status'] == 'missing' and field['missing_or_invalid_class'] == 'PNA'
    classes['PNA'] = classes['PN']
    classes['PNA']['share_class'] = 'PN'
    assert eps_result(classes, 'BPAC11')['reason'] == 'component_share_class_mismatch'


@pytest.mark.parametrize('value', [True, None, '1.2', float('nan'), float('inf'), 10 ** 1000])
def test_invalid_eps_values_are_rejected(value):
    classes = synthetic_classes()
    classes['ON']['fields']['basic_eps']['value'] = value
    result = eps_result(classes)
    assert result['status'] == 'invalid' and result['value'] is None


def test_zero_and_negative_eps_are_preserved_without_creating_valuation_ratios():
    classes = synthetic_classes()
    classes['ON']['fields']['basic_eps']['value'] = 0
    classes['PN']['fields']['basic_eps']['value'] = -2
    classes['ON']['fields'].update(total_assets={'value': 100}, price_earnings={'value': 20})
    result = units.map_unit_fundamentals('ALUP11', classes, AS_OF)
    assert result['fields']['basic_eps']['value'] == -4
    assert set(result['fields']) == {'basic_eps'}


@pytest.mark.parametrize('key,value,reason', [
    ('unit', 'BRL/unit', 'unsupported_component_eps_unit_or_annualization'),
    ('annualized', True, 'unsupported_component_eps_unit_or_annualization'),
    ('period_start', '2026-07-01', 'invalid_or_future_eps_period'),
    ('period_end', '2026-12-31', 'invalid_or_future_eps_period'),
    ('published_at', '2026-10-26', 'invalid_or_future_eps_period'),
    ('period_type', 'ttm', 'incomplete_component_filing_identity'),
    ('version', True, 'incomplete_component_filing_identity'),
    ('filing_id', None, 'incomplete_component_filing_identity'),
    ('scope', 'unknown', 'incomplete_component_filing_identity'),
])
def test_eps_contract_rejects_unsafe_fields(key, value, reason):
    classes = synthetic_classes()
    classes['ON']['fields']['basic_eps'][key] = value
    result = eps_result(classes)
    assert result['value'] is None and result['reason'] == reason


@pytest.mark.parametrize('key,value', [
    ('period_start', '2026-04-01'), ('period_type', 'quarter'),
    ('scope', 'individual'), ('document_type', 'DFP'), ('version', 2),
    ('filing_id', 'OTHER'), ('source_ref', 'OTHER'),
])
def test_classes_from_different_periods_scopes_or_filings_are_not_combined(key, value):
    classes = synthetic_classes()
    classes['PN']['fields']['basic_eps'][key] = value
    classes['PN']['eps_basis'].update(field_source_ref=classes['PN']['fields']['basic_eps']['source_ref'],
                                     filing_id=classes['PN']['fields']['basic_eps']['filing_id'])
    field = eps_result(classes)
    assert field['reason'] == 'incompatible_component_filings_or_periods' and field['value'] is None


@pytest.mark.parametrize('key,value,reason', [
    ('status', 'unverified', 'eps_basis_unverified'),
    ('denominator', 'one_unit', 'eps_basis_unverified'),
    ('share_class', 'PN', 'eps_basis_unverified'),
    ('method', 'ticker_suffix', 'eps_basis_unverified'),
    ('field_source_ref', 'OTHER', 'eps_basis_does_not_identify_field'),
    ('filing_id', 'OTHER', 'eps_basis_does_not_identify_field'),
    ('share_basis_date', '2026-06-30', 'incompatible_eps_share_basis'),
    ('share_basis_date', '2026-10-26', 'incompatible_eps_share_basis'),
    ('share_basis_date', None, 'eps_share_basis_date_missing'),
    ('evidence', None, 'eps_basis_primary_evidence_missing'),
])
def test_eps_basis_requires_exact_field_class_and_dated_primary_proof(key, value, reason):
    classes = synthetic_classes()
    classes['ON']['eps_basis'][key] = value
    field = eps_result(classes)
    assert field['value'] is None and field['reason'] == reason


@pytest.mark.parametrize('key,value', [
    ('authority', 'aggregator'), ('url', 'https://example.com/not-primary'),
    ('sha256', ''), ('file', ''), ('locator', ''), ('published_at', '2026-10-26'),
    ('published_at', None),
])
def test_unverifiable_or_future_proof_is_missing(key, value):
    classes = synthetic_classes()
    classes['ON']['eps_basis']['evidence'][key] = value
    assert eps_result(classes)['reason'] == 'eps_basis_primary_evidence_missing'


def test_eps_basis_proof_cannot_predate_the_identified_filing():
    classes = synthetic_classes()
    classes['ON']['eps_basis']['evidence']['published_at'] = '2025-01-01'
    assert eps_result(classes)['reason'] == 'eps_basis_evidence_predates_field'


@pytest.mark.parametrize('day,status,reason', [
    ('2026-10-24', 'missing', 'composition_not_verified_for_date'),
    ('2026-10-26', 'stale', 'composition_evidence_expired'),
])
def test_snapshot_does_not_imply_historical_or_future_composition_validity(day, status, reason):
    result = units.get_unit_composition('ALUP11', day)
    assert result['status'] == status and result['reason'] == reason
    mapped = units.map_unit_fundamentals('ALUP11', synthetic_classes(), day)
    assert mapped['fields']['basic_eps']['value'] is None


@pytest.mark.parametrize('ticker', ['TEST11', 'ALUP3', 'alup11', 'ONCO3', 'ALUP12'])
def test_unknown_symbols_and_suffixes_are_never_inferred(ticker):
    result = units.get_unit_composition(ticker, AS_OF)
    assert result['status'] == 'missing' and result['components'] == []


@pytest.mark.parametrize('changes,error', [
    ({'cvm': True}, 'invalid_issuer_identity'),
    ({'valid_until': '2026-10-24'}, 'invalid_composition_dates'),
    ({'effective_from': '2026-10-26'}, 'invalid_composition_dates'),
    ({'evidence': ()}, 'missing_primary_evidence'),
    ({'status': 'guessed'}, 'unverified_composition'),
    ({'components': (units.Component('PN', 2), units.Component('PN', 1))}, 'duplicate_component_class'),
    ({'components': (units.Component('ON', True),)}, 'invalid_class_or_ratio'),
    ({'components': (units.Component('PN', 0),)}, 'invalid_class_or_ratio'),
    ({'components': (units.Component('ON', 1.5),)}, 'invalid_class_or_ratio'),
])
def test_invalid_registry_entries_cannot_supply_fundamentals(changes, error):
    record = replace(units.UNIT_COMPOSITIONS['ALUP11'], **changes)
    assert error in units.validate_composition(record)
    field = eps_result(registry={'ALUP11': record})
    assert field['status'] == 'invalid' and field['value'] is None


def test_registry_key_must_match_identity_and_issuer_must_match_eps():
    registry = {'BRBI11': units.UNIT_COMPOSITIONS['ALUP11']}
    assert 'registry_ticker_mismatch' in units.validate_registry(registry)['BRBI11']
    assert units.get_unit_composition('BRBI11', AS_OF, registry=registry)['status'] == 'invalid'
    classes = synthetic_classes()
    classes['PN']['cvm'] = 25860
    assert eps_result(classes)['reason'] == 'component_issuer_mismatch'


def test_evidence_expiry_unknown_effective_date_and_missing_isin_are_explicit():
    result = units.get_unit_composition('ALUP11', date(2026, 10, 25))
    assert result['valid_until'] == AS_OF
    assert result['effective_from'] is None and result['effective_from_status'] == 'not_established'
    assert result['isin'] is None and result['isin_status'] == 'missing_in_cited_sources'
    assert all(component['isin_status'] == 'missing_in_cited_sources' for component in result['components'])
    with pytest.raises(ValueError):
        units.get_unit_composition('ALUP11', datetime(2026, 10, 25))


def test_weighted_overflow_leaves_no_partial_value_or_provenance():
    classes = synthetic_classes()
    classes['PN']['fields']['basic_eps']['value'] = 1e308
    result = eps_result(classes)
    assert result['status'] == 'invalid' and result['value'] is None
    assert result['components'] == []
