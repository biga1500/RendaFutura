import csv
from datetime import date
import io
import zipfile

import pytest

from . import collect_inventory as collector
from .cvm_fundamentals import parse_archives
from .cvm_registry import parse_fca
from .test_cvm_fundamentals import archive, filing


AS_OF = date(2026, 10, 24)


def fca_archive(*, ticker='TEST3', sector='Bancos', filings=None, kind='Acoes Ordinarias'):
    filings = filings or [('100', '1', '2026-03-01')]
    tables = {'': [], '_geral': [], '_valor_mobiliario': []}
    for identifier, version, received in filings:
        tables[''].append({'CNPJ_CIA': 'company', 'DT_REFER': '2026-01-01',
                          'DT_RECEB': received, 'VERSAO': version,
                          'ID_DOC': identifier, 'CD_CVM': '12345'})
        tables['_geral'].append({'ID_Documento': identifier,
                                 'Situacao_Registro_CVM': 'Ativo', 'Setor_Atividade': sector})
        tables['_valor_mobiliario'].append({
            'CNPJ_Companhia': 'company', 'ID_Documento': identifier,
            'Valor_Mobiliario': kind, 'Sigla_Entidade_Administradora': 'B3', 'Mercado': 'Bolsa',
            'Data_Fim_Negociacao': '', 'Data_Fim_Listagem': '',
            'Data_Inicio_Negociacao': '2020-01-01', 'Data_Inicio_Listagem': '2020-01-01',
            'Codigo_Negociacao': ticker, 'Sigla_Classe_Acao_Preferencial': '',
            'Nome_Empresarial': 'Registered company', 'Composicao_BDR_Unit': '',
        })
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for suffix, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(f'fca_cia_aberta{suffix}_2026.csv', stream.getvalue().encode('latin1'))
    return output.getvalue()


def test_digit_inside_official_issuer_code_does_not_erase_cvm_identity():
    result = parse_fca(fca_archive(ticker='B3SA3'), 2026, AS_OF)
    assert result['instruments']['B3SA3']['cvm'] == 12345
    assert result['instruments']['B3SA3']['share_class'] == 'ON'


@pytest.mark.parametrize('ticker', ['B3SA3', 'B1003', 'TEST3', 'TEST11'])
def test_official_code_accepts_four_alphanumeric_prefix_characters(ticker):
    result = parse_fca(fca_archive(ticker=ticker), 2026, AS_OF)
    assert result['instruments'][ticker]['cvm'] == 12345


@pytest.mark.parametrize('ticker', ['13SA3', 'B-SA3', 'B3SA3F', 'B3S3', 'B3SAA3', 'B3SA333'])
def test_official_code_still_rejects_invalid_prefix_or_suffix(ticker):
    result = parse_fca(fca_archive(ticker=ticker), 2026, AS_OF)
    assert not result['instruments']
    assert result['issues'][0]['code'] == 'unresolved_official_trading_code'


def test_new_official_bank_does_not_receive_nonfinancial_default_sector(tmp_path, monkeypatch):
    content = fca_archive(ticker='BPAR3')
    monkeypatch.setattr(collector, 'identities', lambda folder: [])
    monkeypatch.setattr(collector, 'cached_source', lambda *args: content)
    records, _, _ = collector.collect_identities(tmp_path, AS_OF)
    assert records[0]['sector_code'] == 2, 'A bank must not enter industrial FCF handling as sector 10.'


@pytest.mark.parametrize('sector', [
    'Bancos', 'Intermedia\u00e7\u00e3o Financeira', 'Seguradoras e Corretoras',
    'Arrendamento Mercantil', 'Securitiza\u00e7\u00e3o de Receb\u00edveis',
    'Bolsas de Valores/Mercadorias e Futuros', 'Cr\u00e9dito Imobili\u00e1rio',
    'Emp. Adm. Part. - Bancos', 'Emp. Adm. Part. - Intermedia\u00e7\u00e3o Financeira',
    'Emp. Adm. Part. - Seguradoras e Corretoras', 'Emp. Adm. Part. - Cr\u00e9dito Imobili\u00e1rio',
    '  BANCOS  ',
])
def test_explicit_official_financial_category_is_carried_and_classified(sector):
    record = parse_fca(fca_archive(sector=sector), 2026, AS_OF)['instruments']['TEST3']
    assert record['issuer_sector'] == sector.strip()
    assert record['sector_code'] == 2


@pytest.mark.parametrize('sector', [
    'Energia El\u00e9trica', 'Emp. Adm. Part. - Energia El\u00e9trica',
    'Emp. Adm. Part. - Sem Setor Principal', 'Emp. Adm. Part. - Alimentos',
    'Servi\u00e7os m\u00e9dicos', 'Seguradoras e Corretoras - desconhecido', '',
])
def test_nonfinancial_or_unclassified_fca_sector_preserves_existing_b3_sector(tmp_path, monkeypatch, sector):
    content = fca_archive(sector=sector)
    identity = parse_fca(content, 2026, AS_OF)['instruments']['TEST3']
    assert identity['issuer_sector'] == (sector or None)
    assert 'sector_code' not in identity
    monkeypatch.setattr(collector, 'identities', lambda folder: [
        {'ticker': 'TEST3', 'cvm': 12345, 'sector_code': 9},
    ])
    monkeypatch.setattr(collector, 'cached_source', lambda *args: content)
    records, _, _ = collector.collect_identities(tmp_path, AS_OF)
    assert records[0]['sector_code'] == 9


@pytest.mark.parametrize('ticker,kind,sector', [
    ('BPAR3', 'Acoes Ordinarias', 'Bancos'),
    ('BRBI11', 'Units', 'Emp. Adm. Part. - Bancos'),
])
def test_financial_registry_spec_keeps_real_cash_but_no_industrial_fcf(ticker, kind, sector):
    record = parse_fca(fca_archive(ticker=ticker, kind=kind, sector=sector), 2026, AS_OF)['instruments'][ticker]
    specs = {ticker: (record['cvm'], record['share_class'], record['sector_code'])}
    fields = parse_archives(archive(filing()), specs, AS_OF)['instruments'][ticker]['fields']
    assert fields['total_assets']['value'] == 1000000
    assert fields['operating_cash_flow']['value'] == 80000
    assert fields['capex']['value'] == -20000
    assert fields['free_cash_flow']['value'] is None
    assert fields['free_cash_flow']['status'] == 'not_applicable'
    assert fields['basic_eps']['value'] == (None if kind == 'Units' else 2)


def test_only_latest_already_published_fca_version_is_selected():
    content = fca_archive(filings=[('102', '3', '2026-11-01'),
                                   ('100', '1', '2026-03-01'),
                                   ('101', '2', '2026-08-01')])
    record = parse_fca(content, 2026, AS_OF)['instruments']['TEST3']
    assert record['filing_id'] == '101'
    assert record['published_at'] == '2026-08-01'


def test_unknown_unit_class_still_has_official_cvm_identity():
    record = parse_fca(fca_archive(ticker='TEST11', kind='Units'), 2026, AS_OF)['instruments']['TEST11']
    assert record['cvm'] == 12345
    assert record['share_class'] is None
