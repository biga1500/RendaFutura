"""Detached exact-code FCA identity repair; no inventory or model integration.

Missing inventory fields are not conflicting primary evidence. In particular,
the catalogue FCA parser drops an exact code ending in B before reading its
class. This reader preserves the reported code, filing and listing episode.
It never supplies an ISIN, aliases, corporate actions or current trading status.
"""

from copy import deepcopy
from datetime import date
import re

from .security_registration import FCA_URL, _kind, _norm, parse_fca_registry
from .selected_identity_research import LocalSources


def recheck_identity(folder, row, source, as_of):
    """Return a proposed field patch backed by a hash-verified, exact FCA row.

    Callers must provide the receipt and the inventory row they are auditing.
    No current pointer is consulted and neither argument nor any file is edited.
    Unknown fields may be filled; disagreements, stale filings, closed episodes
    and missing dates never produce a patch.
    """
    if not isinstance(as_of, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', as_of):
        raise ValueError('identity_recheck_invalid_date')
    when = date.fromisoformat(as_of)
    ticker = row.get('ticker')
    if not isinstance(ticker, str) or not re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}[A-Z]?', ticker):
        raise ValueError('identity_recheck_invalid_exact_code')
    url = source.get('url')
    match = re.fullmatch(re.escape(FCA_URL) + r'fca_cia_aberta_(\d{4})\.zip', url or '')
    digest = source.get('sha256')
    if not match or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('identity_recheck_primary_receipt_missing')
    year = int(match[1])
    content, receipt = LocalSources(folder).read(source['file'], digest)
    receipt.update(url=url, retrieved_at=source.get('retrieved_at'),
                   published_at=None, mtime_used=False)
    masters, securities = parse_fca_registry(content, year)
    available, latest = {}, {}
    for filing in masters:
        if date.fromisoformat(filing['DT_RECEB']) > when or date.fromisoformat(filing['DT_REFER']) > when:
            continue
        available[filing['ID_DOC']] = filing
        cvm = int(filing['CD_CVM'])
        rank = (filing['DT_REFER'], int(filing['VERSAO']), filing['DT_RECEB'])
        if cvm not in latest or rank > latest[cvm][0]:
            latest[cvm] = rank, {filing['ID_DOC']}
        elif rank == latest[cvm][0]:
            latest[cvm][1].add(filing['ID_DOC'])
    records = []
    for security in securities:
        filing = available.get(security['ID_Documento'])
        if security['Codigo_Negociacao'].strip() != ticker or filing is None:
            continue
        cvm = int(filing['CD_CVM'])
        if filing['ID_DOC'] not in latest[cvm][1]:
            continue
        kind, share = _kind(security)
        records.append({'cvm': cvm, 'security_type': kind, 'share_class': share,
            'filing': {k: filing[k] for k in ('CD_CVM', 'ID_DOC', 'DT_REFER', 'VERSAO', 'DT_RECEB')},
            'security': {k: v for k, v in security.items() if k not in ('CNPJ_Companhia', '_csv_record')},
            'csv_record': security['_csv_record'],
            'locator': f'fca_cia_aberta_valor_mobiliario_{year}.csv:csv_record={security["_csv_record"]}'})
    result = {'ticker': ticker, 'as_of': as_of, 'status': 'exact_identity_not_established',
        'records': records, 'source': receipt, 'proposed_fields': {}, 'conflicts': [],
        'isin': None, 'isin_verified': False, 'alias_verified': False,
        'code_effective_from': None, 'code_effective_until': None,
        'current_trading_verified': False, 'whole_security_delisting_verified': False,
        'corporate_action_coverage_verified': False, 'model_admission_granted': False,
        'integration_performed': False, 'network_requests': 0}
    identities = {(r['cvm'], r['security_type'], r['share_class']) for r in records}
    if len(identities) > 1 or any(len(latest[r['cvm']][1]) != 1 for r in records):
        result.update(status='conflicting_primary_identity', conflicts=['multiple_latest_exact_identities'])
        return result
    if not records:
        return result
    cvm, kind, share = next(iter(identities))
    result['conflicts'] = [key for key, expected in (('cvm', cvm), ('share_class', share))
                           if row.get(key) is not None and row[key] != expected]
    if result['conflicts']:
        result['status'] = 'conflicting_declared_identity'
        return result
    if kind not in ('ordinary_share', 'preferred_share') or share is None:
        result['status'] = 'exact_share_class_not_established'
        return result
    open_records = []
    for record in records:
        security = record['security']
        if (security['Sigla_Entidade_Administradora'] != 'B3'
                or _norm(security['Mercado']) not in ('bolsa', 'balcao organizado')):
            continue
        starts = [security[k] for k in ('Data_Inicio_Negociacao', 'Data_Inicio_Listagem')]
        ends = [security[k] for k in ('Data_Fim_Negociacao', 'Data_Fim_Listagem')]
        if all(v and date.fromisoformat(v) <= when for v in starts) and not any(
                v and date.fromisoformat(v) <= when for v in ends):
            open_records.append(record)
    if not open_records:
        result['status'] = 'exact_identity_without_open_dated_episode'
        return result
    # The date belongs to the reported episode, never to an inferred code change.
    starts = {r['security']['Data_Inicio_Listagem'] for r in open_records}
    fields = {'cvm': cvm, 'share_class': share, 'identity_status': 'official_cvm_fca'}
    if len(starts) == 1:
        fields['listing_start'] = next(iter(starts))
    result.update(status='exact_FCA_identity_detached_view', proposed_fields=fields)
    return deepcopy(result)
