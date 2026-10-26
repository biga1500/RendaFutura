"""Read listed share identities and unit compositions from official FCA files."""

import csv
from datetime import date
import io
import re
import zipfile

from .collect_market_data import normalize


# Exact FCA categories; holdings qualify only through an explicit financial sector.
FINANCIAL_SECTORS = frozenset({
    'arrendamento mercantil', 'bancos', 'bolsas de valores/mercadorias e futuros',
    'credito imobiliario', 'intermediacao financeira', 'securitizacao de recebiveis',
    'seguradoras e corretoras',
})


def parse_fca(content, year, as_of):
    archive = zipfile.ZipFile(io.BytesIO(content))

    def rows(suffix):
        name = f'fca_cia_aberta{suffix}_{year}.csv'
        with archive.open(name) as stream:
            yield from csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')

    latest = {}
    for row in rows(''):
        if date.fromisoformat(row['DT_RECEB'][:10]) > as_of or date.fromisoformat(row['DT_REFER']) > as_of:
            continue
        key = row['CNPJ_CIA']
        if key not in latest or (row['DT_REFER'], int(row['VERSAO'])) > (latest[key]['DT_REFER'], int(latest[key]['VERSAO'])):
            latest[key] = row
    general = {r['ID_Documento']: r for r in rows('_geral')}
    instruments, issues = {}, []
    for row in rows('_valor_mobiliario'):
        filing = latest.get(row['CNPJ_Companhia'])
        if not filing or row['ID_Documento'] != filing['ID_DOC']:
            continue
        issuer = general.get(row['ID_Documento'], {})
        if normalize(issuer.get('Situacao_Registro_CVM', '')) != 'ativo':
            continue
        kind = normalize(row['Valor_Mobiliario'])
        if kind not in ('acoes ordinarias', 'acoes preferenciais', 'units') or row['Sigla_Entidade_Administradora'] != 'B3':
            continue
        if normalize(row['Mercado']) not in ('bolsa', 'balcao organizado'):
            continue
        if any(row.get(key) and date.fromisoformat(row[key]) <= as_of for key in ('Data_Fim_Negociacao', 'Data_Fim_Listagem')):
            continue
        if any(row.get(key) and date.fromisoformat(row[key]) > as_of for key in ('Data_Inicio_Negociacao', 'Data_Inicio_Listagem')):
            continue
        ticker = row['Codigo_Negociacao'].strip().upper()
        if not re.fullmatch(r'[A-Z][A-Z0-9]{3}[0-9]{1,2}B?', ticker):
            issues.append({'code': 'unresolved_official_trading_code', 'filing_id': filing['ID_DOC']})
            continue
        share = 'ON' if kind == 'acoes ordinarias' else (row['Sigla_Classe_Acao_Preferencial'].strip().upper() or 'PN') if kind == 'acoes preferenciais' else None
        issuer_sector = issuer.get('Setor_Atividade', '').strip() or None
        record = {'ticker': ticker, 'name': row['Nome_Empresarial'], 'cvm': int(filing['CD_CVM']),
                  'share_class': share, 'security_type': row['Valor_Mobiliario'],
                  'issuer_sector': issuer_sector,
                  'unit_composition': row['Composicao_BDR_Unit'] or None, 'market': row['Mercado'],
                  'filing_id': filing['ID_DOC'], 'published_at': filing['DT_RECEB'],
                  'fca_reference': filing['DT_REFER'], 'listing_start': row['Data_Inicio_Listagem'] or None,
                  'identity_status': 'official_cvm_fca'}
        sector = normalize(issuer_sector or '').removeprefix('emp. adm. part. - ')
        if sector in FINANCIAL_SECTORS:
            record['sector_code'] = 2
        if ticker in instruments and instruments[ticker] != record:
            previous = instruments[ticker]
            if all(previous[key] == record[key] for key in ('cvm', 'share_class', 'unit_composition')):
                previous.setdefault('additional_listings', []).append({'market': record['market'], 'listing_start': record['listing_start']})
            else:
                issues.append({'code': 'conflicting_official_identity', 'ticker': ticker, 'filing_id': filing['ID_DOC']})
                previous.update(identity_status='conflicting_official_identity', cvm=None, share_class=None)
            continue
        instruments[ticker] = record
    return {'instruments': instruments, 'issues': issues,
            'basis': 'latest_received_FCA_declared_B3_listing_not_intraday_exchange_membership'}
