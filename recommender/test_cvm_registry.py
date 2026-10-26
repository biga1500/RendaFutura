import csv
from datetime import date
import io
import zipfile

from .cvm_registry import parse_fca


def fixture(*, received='2026-01-30', end='', composition='1 ON / 2 PN', kind='Units', ticker='TEST11'):
    output = io.BytesIO()
    tables = {'': [{'CNPJ_CIA': 'company', 'DT_RECEB': received, 'DT_REFER': '2026-01-01',
                    'VERSAO': '1', 'ID_DOC': '100', 'CD_CVM': '10'}],
              '_geral': [{'ID_Documento': '100', 'Situacao_Registro_CVM': 'Ativo'}],
              '_valor_mobiliario': [{'CNPJ_Companhia': 'company', 'ID_Documento': '100',
                  'Valor_Mobiliario': kind, 'Sigla_Entidade_Administradora': 'B3', 'Mercado': 'Bolsa',
                  'Data_Fim_Negociacao': end, 'Data_Fim_Listagem': '', 'Data_Inicio_Negociacao': '2020-01-01',
                  'Data_Inicio_Listagem': '2020-01-01', 'Codigo_Negociacao': ticker,
                  'Sigla_Classe_Acao_Preferencial': '', 'Nome_Empresarial': 'Test company',
                  'Composicao_BDR_Unit': composition}]}
    with zipfile.ZipFile(output, 'w') as archive:
        for suffix, rows in tables.items():
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter=';')
            writer.writeheader();writer.writerows(rows)
            archive.writestr(f'fca_cia_aberta{suffix}_2026.csv', stream.getvalue().encode('latin1'))
    return output.getvalue()


def test_official_unit_identity_and_composition_do_not_invent_eps():
    result = parse_fca(fixture(), 2026, date(2026, 10, 24))
    unit = result['instruments']['TEST11']
    assert unit['unit_composition'] == '1 ON / 2 PN' and unit['share_class'] is None
    assert unit['cvm'] == 10 and unit['identity_status'] == 'official_cvm_fca'


def test_future_publication_and_ended_listing_are_not_current():
    assert not parse_fca(fixture(received='2026-11-01'), 2026, date(2026, 10, 24))['instruments']
    assert not parse_fca(fixture(end='2026-01-01'), 2026, date(2026, 10, 24))['instruments']


def test_exact_official_OTC_suffix_is_not_dropped_or_aliased():
    result = parse_fca(fixture(ticker='TEST3B', kind='Acoes Ordinarias', composition=''),
                       2026, date(2026, 10, 24))
    assert set(result['instruments']) == {'TEST3B'}
    assert result['instruments']['TEST3B']['share_class'] == 'ON'
    assert not result['issues']
