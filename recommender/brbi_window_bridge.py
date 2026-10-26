"""Contemporaneous IPO identity plus retrospective FCA listing corroboration."""
from datetime import date


def first_year_bridge(row, cache, cutoff, start):
    from .brbi_2021_identity import IDENTITY, audit
    from .unit_candidates import _fca, _ratios

    if (cutoff != date(2026, 9, 30) or start != date(2021, 9, 30)
            or row.get('ticker') != 'BRBI11' or row.get('cvm') != 25860
            or row.get('isin') not in (None, IDENTITY['isin'])):
        raise ValueError('brbi_window_unreviewed_exact_identity')
    proof = audit(cache.folder, cutoff)
    masters, securities, relative = _fca(cache, 2022)
    if cache.receipts[relative]['sha256'] != 'b45e655c6ffae0b40ddb0425e27cbcca7d2478867f542d78c1aa034293eeb77a':
        raise ValueError('brbi_corroborating_FCA_original_changed')
    filings = [r for r in masters if int(r['CD_CVM']) == 25860
               and r['DT_RECEB'] <= cutoff.isoformat() and r['DT_REFER'] <= cutoff.isoformat()]
    latest = max(filings, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
    if (latest['ID_DOC'], latest['CNPJ_CIA'], latest['DT_RECEB']) != (
            '115279', IDENTITY['cnpj'], '2022-05-16'):
        raise ValueError('brbi_corroborating_FCA_changed')
    members = [r for r in securities if r['ID_Documento'] == latest['ID_DOC']]
    units = [r for r in members if r['Codigo_Negociacao'] == 'BRBI11' and r['Valor_Mobiliario'] == 'Units']
    if len(units) != 1:
        raise ValueError('brbi_corroborating_unit_ambiguous')
    unit, = units
    ratios = _ratios(unit['Composicao_BDR_Unit'], members)
    if (ratios != IDENTITY['components'] or unit['CNPJ_Companhia'] != IDENTITY['cnpj']
            or unit['Data_Inicio_Negociacao'] != '2021-06-21'
            or unit['Data_Inicio_Listagem'] != '2021-05-13'
            or any(unit[field] for field in ('Data_Fim_Negociacao', 'Data_Fim_Listagem'))):
        raise ValueError('brbi_reported_listing_or_composition_changed')
    for source in proof['sources']:
        cache.register_research_evidence(source)
    receipt = cache.receipts[relative]
    return {'ratios': ratios, 'cnpj': IDENTITY['cnpj'], 'members': members,
            'evidence': [{**source, 'year': 2021, 'role': 'primary_IPO_identity_not_scheduled_execution'}
                         for source in proof['sources']] + [{
                'file': relative, 'sha256': receipt['sha256'], 'filing_id': latest['ID_DOC'],
                'published_at': latest['DT_RECEB'], 'year': 2022,
                'reported_trading_start': unit['Data_Inicio_Negociacao'],
                'role': 'retrospective_next_FCA_listing_corroboration'}],
            'scheduled_IPO_date_used_as_executed_trade': False,
            'prices_cash_and_financials_approved': False, 'point_in_time_archive_verified': False}
