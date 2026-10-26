"""Annual exact-class CVM records composed per deposited UNIT, read-only.

The class parser remains authoritative. No issuer totals, debt ratios or stock
prices are multiplied by the deposit ratio. Missing latest component revisions
produce null UNIT records so the model cannot fall back to an older filing.
"""

from collections import defaultdict
from copy import deepcopy
import csv
from decimal import Decimal
import hashlib
import io
import json
import math
import zipfile

from .inventory_candidates import _Cache
from .inventory_financials import _annual_start, _dre_fingerprint, prepare_financials
from .share_basis import factor_after
from .unit_candidates import _day, _prices, _resolve_components, unit_composition_for_window


EPS_RESEARCH_FILE = 'raw/primary-research/units/eps-denominators-20261025-v1.json'
EPS_RESEARCH_SHA256 = '1e3ef7196576423c36d3b218ca929175f2f5f67cf418c2fa678a716215a56e31'


def _proof_require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _rounding_overlap(profit, shares, uncertainty, value, decimals):
    """Compare the disclosed rounding intervals, never a new model tolerance."""
    half = Decimal(10) ** -decimals / 2
    return (value - half <= profit / (shares - uncertainty)
            and value + half >= profit / (shares + uncertainty))


def _eps_research(cache, prepared, cutoff, diagnostics):
    if not prepared or not (cache.folder / EPS_RESEARCH_FILE).is_file():
        return {}
    reviews = {}
    requested = {candidate['cvm'] for candidate, _ in prepared}
    try:
        body, _ = cache.read(EPS_RESEARCH_FILE, 'unit_EPS_denominator_review')
        _proof_require(hashlib.sha256(body).hexdigest() == EPS_RESEARCH_SHA256,
                       'unit_EPS_review_manifest_hash_mismatch')
        manifest = json.loads(body)
        _proof_require(manifest['schema_version'] == 1, 'unit_EPS_review_schema_invalid')
        source = manifest['structured_source']
        content, _ = cache.read(source['file'], 'unit_EPS_original_CVM_cells')
        _proof_require(hashlib.sha256(content).hexdigest() == source['sha256'],
                       'unit_EPS_structured_source_hash_mismatch')
        cache.receipts[source['file']]['url'] = source['url']
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            def rows(member):
                with archive.open(member) as stream:
                    return list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
            masters = rows('dfp_cia_aberta_2025.csv')
            dre = rows('dfp_cia_aberta_DRE_con_2025.csv')
        for reading in manifest['readings']:
            if reading['cvm'] not in requested or _day(reading['published_at']) > cutoff:
                continue
            try:
                identity = reading['cvm'], reading['fiscal_end'], reading['version']
                filing = [row for row in masters if (int(row['CD_CVM']), row['DT_REFER'], row['VERSAO']) == identity]
                _proof_require(len(filing) == 1 and filing[0]['ID_DOC'] == reading['filing_id']
                    and filing[0]['DT_RECEB'] == reading['published_at'] and filing[0]['CNPJ_CIA'] == reading['cnpj'],
                    'unit_EPS_review_filing_identity_mismatch')
                cells = [row for row in dre if (int(row['CD_CVM']), row['DT_REFER'], row['VERSAO']) == identity]
                _proof_require(_dre_fingerprint(cells) == reading['dre_sha256'], 'unit_EPS_review_DRE_mismatch')
                current = [row for row in cells if row['ORDEM_EXERC'] == '\u00daLTIMO']
                _proof_require(reading['accounts_scope'] == 'consolidated'
                    and reading['denominator'] == 'one_class_share_annual_weighted_average',
                    'unit_EPS_review_denominator_unverified')
                profits = [row for row in current if row['CD_CONTA'] == '3.11.01']
                _proof_require(len(profits) == 1 and profits[0]['MOEDA'] == 'REAL'
                    and profits[0]['ESCALA_MOEDA'] in ('MIL', 'UNIDADE'), 'unit_EPS_review_profit_unavailable')
                profit = Decimal(profits[0]['VL_CONTA']) * (1000 if profits[0]['ESCALA_MOEDA'] == 'MIL' else 1)
                _proof_require(profit == Decimal(reading['profit_brl']), 'unit_EPS_review_profit_mismatch')
                counts = reading['weighted_average_shares']
                _proof_require(bool(counts) and all(type(n) is int and n > 0 for n in counts.values()),
                               'unit_EPS_review_share_counts_invalid')
                shares = Decimal(sum(counts.values()))
                uncertainty = Decimal(reading['weighted_average_quantum']) * len(counts) / 2
                _proof_require(0 < uncertainty < shares, 'unit_EPS_review_share_precision_invalid')
                note_eps = Decimal(reading['note_basic_eps'])
                _proof_require(_rounding_overlap(profit, shares, uncertainty, note_eps, -note_eps.as_tuple().exponent),
                               'unit_EPS_note_denominator_disagrees')
                _proof_require(len({Decimal(item['reported_eps']) for item in reading['classes'].values()}) == 1,
                               'unit_EPS_class_profit_allocation_not_common')
                for share, item in reading['classes'].items():
                    eps = [row for row in current if row['CD_CONTA'] == item['account']]
                    _proof_require(item['account'].startswith('3.99.01.') and len(eps) == 1
                        and eps[0]['DS_CONTA'].strip().upper() == share and eps[0]['MOEDA'] == 'REAL'
                        and Decimal(eps[0]['VL_CONTA']) == Decimal(item['reported_eps']),
                        'unit_EPS_review_exact_basic_class_mismatch')
                    _proof_require(_rounding_overlap(profit, shares, uncertainty,
                        Decimal(item['reported_eps']), item['decimal_places']),
                        'unit_EPS_class_denominator_disagrees')
                document = manifest['documents'][reading['document']]
                pdf, _ = cache.read(document['file'], 'unit_EPS_primary_DFP')
                _proof_require(pdf.startswith(b'%PDF') and hashlib.sha256(pdf).hexdigest() == document['sha256'],
                               'unit_EPS_primary_document_hash_mismatch')
                cache.receipts[document['file']]['url'] = document['url']
                catalog, _ = cache.read(document['catalog_file'], 'unit_EPS_RI_discovery')
                _proof_require(hashlib.sha256(catalog).hexdigest() == document['catalog_sha256']
                    and document['url'] in json.dumps(json.loads(catalog)), 'unit_EPS_RI_discovery_mismatch')
                cache.receipts[document['catalog_file']]['url'] = document['catalog_url']
                reviews[(*identity, reading['filing_id'])] = {**reading, 'status': 'verified',
                    'source_file': source['file'], 'source_sha256': source['sha256'],
                    'manifest_file': EPS_RESEARCH_FILE, 'manifest_sha256': EPS_RESEARCH_SHA256,
                    'document_file': document['file'], 'document_sha256': document['sha256'],
                    'point_in_time_archive_verified': False}
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                diagnostics.append({'kind': 'source', 'cvm': reading['cvm'], 'source_file': EPS_RESEARCH_FILE,
                                    'status': 'excluded', 'reasons': [str(error)]})
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, zipfile.BadZipFile) as error:
        diagnostics.append({'kind': 'source', 'source_file': EPS_RESEARCH_FILE,
                            'status': 'excluded', 'reasons': [str(error)]})
    return reviews


def _matched_eps_proof(proof, composition, records, actions):
    if not proof or proof.get('status') != 'verified':
        return False
    first = records[0]
    if any(proof.get(key) != first.get(key) for key in (
            'cvm', 'fiscal_end', 'version', 'published_at', 'filing_id', 'source_file', 'accounts_scope')):
        return False
    if first.get('source_ref') != 'cvm:DFP:' + first['fiscal_end'][:4] + ':' + proof.get('source_sha256', ''):
        return False
    if set(proof.get('classes', {})) != {part['share_class'] for part in composition['components']}:
        return False
    if {row.get('share_class') for row in records} != set(proof['classes']):
        return False
    fiscal = _day(first['fiscal_end'])
    in_period = [row for row in actions if _annual_start(fiscal) <= _day(row['date']) <= fiscal]
    if sorted(in_period, key=lambda r: r['date']) != sorted(proof['in_period_actions'], key=lambda r: r['date']):
        return False
    return all(proof['classes'][row['share_class']]['account'] == row['financial_method']['basic_eps_account']
               and float(proof['classes'][row['share_class']]['reported_eps']) == row['profit_per_share']
               for row in records)


def _compatible(records):
    fields = ('cvm', 'fiscal_end', 'version', 'published_at', 'filing_id', 'source_ref',
              'source_file', 'document_type', 'period_type', 'annualized', 'accounts_scope')
    return all(all(record.get(key) == records[0].get(key) for key in fields) for record in records)


def _financial_component_contract(candidate, composition, records, methods):
    """Keep the class parser's consolidated bank roots and exact class identity."""
    components = composition['components']
    if (type(candidate.get('sector_code')) is not int or candidate['sector_code'] != 2
            or len(records) != len(components)
            or len({part['share_class'] for part in components}) != len(components)):
        return False
    fields = ('profit_account', 'assets_account', 'equity_account',
              'liabilities_method', 'liabilities_accounts')
    for component, record, method in zip(components, records, methods):
        equity, profit, eps = (method.get(key) for key in (
            'equity_account', 'profit_account', 'basic_eps_account'))
        if (record.get('cvm') != candidate['cvm']
                or record.get('share_class') != component['share_class']
                or type(component.get('shares')) is not int or component['shares'] <= 0
                or record.get('document_type') != 'DFP' or record.get('accounts_scope') != 'consolidated'
                or record.get('unavailable_reasons')
                or method.get('accounts_scope') != 'consolidated'
                or method.get('assets_account') != '1'
                or not isinstance(equity, str) or not equity.startswith('2.') or equity.count('.') != 1
                or not isinstance(profit, str) or not profit.startswith('3.') or profit.startswith('3.99')
                or not isinstance(eps, str) or not eps.startswith(('3.99.01.', 'note:'))
                or method.get('liabilities_method') != 'total_passivo_less_consolidated_equity'
                or method.get('liabilities_accounts') != ['2', equity]
                or method.get('capex_accounts') != []
                or method.get('fcf_absence_reason') != 'industrial_fcf_not_applicable_to_financial_issuer'
                or any(method.get(field) != methods[0].get(field) for field in fields)):
            return False
    return True


def _compose(candidate, composition, records, cutoff, *, denominator_proof=None):
    first = records[0]
    reasons = []
    if not _compatible(records) or first.get('period_type') != 'annual' or first.get('annualized') is not False:
        reasons.append('unit_incompatible_component_filings_or_periods')
    if any(record.get('status') != 'included' for record in records):
        reasons.append('unit_component_annual_accounts_unavailable')
    methods = [record['financial_method'] if isinstance(record.get('financial_method'), dict) else {}
               for record in records]
    method_ids = {method.get('id') if isinstance(method.get('id'), str) else None for method in methods}
    if len(method_ids) != 1 or None in method_ids:
        reasons.append('unit_component_financial_methods_disagree')
    actions = candidate.get('share_actions')
    basis = candidate.get('share_normalization', {}).get('share_basis_date')
    if not isinstance(actions, list) or basis != cutoff.isoformat():
        reasons.append('unit_component_denominator_normalization_unverified')
        actions = []
    fiscal = _day(first['fiscal_end'])
    # The same annual weighted-average class basis used by the class parser
    # needs documentary verification when the fiscal period contains a bonus.
    verified_basis = _matched_eps_proof(denominator_proof, composition, records, actions)
    if any(_annual_start(fiscal) <= _day(action['date']) <= fiscal for action in actions) and not verified_basis:
        reasons.append('unit_in_period_weighted_share_denominator_unverified')
    if any(_day(action['date']) > cutoff for action in actions):
        reasons.append('unit_future_share_action_unreconciled')
    factor = factor_after(fiscal, actions, cutoff)
    industrial = method_ids == {'industrial_cash_capex_v1'}
    bank_methods = method_ids == {'financial_no_industrial_fcf_v1'}
    financial = bank_methods and type(candidate.get('sector_code')) is int and candidate['sector_code'] == 2
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    if any(not finite(record.get('profit_per_share'))
           or bank_methods and record['profit_per_share'] == 0 for record in records):
        reasons.append('unit_component_basic_eps_unavailable')
    if bank_methods:
        if not _financial_component_contract(candidate, composition, records, methods):
            reasons.append('unit_financial_component_contract_unverified')
        if any('free_cash_per_share' not in record or record['free_cash_per_share'] is not None
               for record in records):
            reasons.append('unit_financial_FCF_must_be_not_applicable')
    elif not industrial or any(not finite(record.get('free_cash_per_share')) for record in records):
        reasons.append('unit_required_comparable_FCF_unavailable')
    if candidate.get('sector_code') == 2 and not bank_methods:
        reasons.append('unit_financial_sector_requires_financial_component_method')
    debts = [record.get('debt_ratio') for record in records]
    if any(not finite(value) for value in debts) or any(value != debts[0] for value in debts):
        reasons.append('unit_issuer_debt_ratio_disagrees')
    result = {key: deepcopy(first.get(key)) for key in (
        'cvm', 'fiscal_end', 'version', 'published_at', 'filing_id', 'source_ref', 'source_file',
        'document_type', 'period_type', 'annualized', 'accounts_scope')}
    result.update(ticker=candidate['ticker'], share_class='UNIT', sector_code=candidate['sector_code'],
        profit_per_share=None, free_cash_per_share=None, debt_ratio=None,
        status='excluded' if reasons else 'included', unavailable_reasons=list(dict.fromkeys(reasons)),
        financial_method={'id': 'unit_exact_class_annual_v1', 'accounts_scope': first.get('accounts_scope'),
            'method': 'sum_deposited_shares_times_same_filing_class_per_share',
            'share_basis_date': cutoff.isoformat(), 'post_period_share_factor': factor,
            'denominator': 'reported_basic_class_share_normalized_for_post_period_actions',
            'FCF_method': 'existing_class_parser_profit_over_basic_EPS_denominator',
            'debt_ratio_method': 'same_issuer_ratio_unchanged',
            'composition_evidence': composition['evidence'],
            'components': [{'share_class': component['share_class'], 'shares': component['shares'],
                'profit_per_share': record.get('profit_per_share'),
                'free_cash_per_share': record.get('free_cash_per_share'),
                'financial_method': deepcopy(record.get('financial_method'))}
                for component, record in zip(composition['components'], records)]})
    if financial:
        result['financial_method'].update(
            FCF_method='not_applicable_to_financial_issuer',
            fcf_absence_reason='industrial_fcf_not_applicable_to_financial_issuer',
            basic_EPS_source='derived_sum_of_reported_class_basic_EPS_not_reported_UNIT_EPS')
    if verified_basis:
        result['financial_method'].update(
            denominator='reported_basic_class_share_common_annual_weighted_average',
            share_basis_date_role='post_period_action_normalization_not_year_end_outstanding_denominator',
            basic_EPS_denominator_evidence=deepcopy(denominator_proof))
    if not reasons:
        profit = sum(part['shares'] * record['profit_per_share'] / factor
                     for part, record in zip(composition['components'], records))
        cash = (None if financial else
                sum(part['shares'] * record['free_cash_per_share'] / factor
                    for part, record in zip(composition['components'], records)))
        if math.isfinite(profit) and (financial or math.isfinite(cash)) and profit != 0:
            result.update(profit_per_share=profit, free_cash_per_share=cash, debt_ratio=debts[0])
        else:
            reason = 'unit_basic_EPS_zero_model_features_unavailable' if profit == 0 else 'unit_weighted_financial_overflow'
            result.update(status='excluded', unavailable_reasons=[reason])
    return result


def prepare_unit_financials(folder, candidates, cutoff):
    """Return (statements, diagnostics, sources) for exact share_class='UNIT'."""
    cutoff = _day(cutoff)
    if isinstance(candidates, dict):
        candidates = [{'ticker': ticker, **value} if isinstance(value, dict) else
                      dict(zip(('ticker', 'cvm', 'share_class', 'sector_code'), (ticker, *value)))
                      for ticker, value in candidates.items()]
    candidates = [row for row in candidates if row.get('share_class') == 'UNIT']
    diagnostics, prepared, class_specs = [], [], {}
    cache = _Cache(folder, {'stocks': candidates})
    duplicates = defaultdict(list)
    for candidate in candidates:
        duplicates[candidate['ticker']].append(candidate)
    for candidate in candidates:
        try:
            if len(duplicates[candidate['ticker']]) != 1:
                raise ValueError('unit_duplicate_candidate_identity')
            composition = unit_composition_for_window(candidate, cache, cutoff)
            _, actions, _ = _prices(candidate['ticker'], cache, cutoff)
            if candidate.get('share_actions') != actions:
                raise ValueError('unit_financial_actions_disagree_with_cached_unit_history')
            detail = cache.json(f"raw/expanded/b3-detail-{candidate['cvm']}.json", 'unit_component_identity')
            if not str(detail.get('codeCVM', '')).isdigit() or int(detail['codeCVM']) != candidate['cvm']:
                raise ValueError('unit_cached_B3_issuer_mismatch')
            codes = {item['code']: item['isin'] for item in detail.get('otherCodes', [])}
            if codes.get(candidate['ticker']) != candidate.get('isin') or candidate.get('isin') is None:
                raise ValueError('unit_exact_ISIN_unavailable')
            _resolve_components(composition, codes)
            for component in composition['components']:
                _, component_actions, _ = _prices(component['ticker'], cache, cutoff)
                if sorted(component_actions, key=lambda a: a['date']) != sorted(actions, key=lambda a: a['date']):
                    raise ValueError('unit_component_share_actions_disagree')
            prepared.append((candidate, composition))
            for index, component in enumerate(composition['components']):
                key = f"{candidate['ticker']}:component:{index}"
                class_specs[key] = (candidate['cvm'], component['share_class'], candidate['sector_code'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            diagnostics.append({'kind': 'candidate', 'ticker': candidate['ticker'], 'share_class': 'UNIT',
                                'status': 'excluded', 'reasons': [str(error)]})
    class_rows, class_diagnostics, sources = prepare_financials(folder, class_specs, cutoff)
    diagnostics.extend({**row, 'unit_component_diagnostic': True} for row in class_diagnostics)
    eps_reviews = _eps_research(cache, prepared, cutoff, diagnostics)
    sources.extend(sorted(cache.receipts.values(), key=lambda item: item['file']))
    by_class = defaultdict(dict)
    for row in class_rows:
        by_class[row['cvm'], row['share_class']][row['fiscal_end'], row['version']] = row
    statements = []
    for candidate, composition in prepared:
        classes = [by_class[candidate['cvm'], part['share_class']] for part in composition['components']]
        keys = set().union(*(set(rows) for rows in classes))
        current = []
        for fiscal, version in sorted(keys, key=lambda key: (key[0], int(key[1]))):
            records = [rows.get((fiscal, version)) for rows in classes]
            if any(record is None for record in records):
                # Retain a blocker for the latest filing even if one class was
                # entirely absent, instead of selecting an older complete pair.
                first = next(record for record in records if record is not None)
                records = [record if record is not None else {**first, 'status': 'excluded',
                           'profit_per_share': None, 'free_cash_per_share': None} for record in records]
            proof = eps_reviews.get((candidate['cvm'], fiscal, version, records[0]['filing_id']))
            record = _compose(candidate, composition, records, cutoff, denominator_proof=proof)
            statements.append(record)
            current.append(record)
            diagnostics.append({'kind': 'filing', 'ticker': candidate['ticker'], 'cvm': candidate['cvm'],
                'share_class': 'UNIT', 'sector_code': candidate['sector_code'], 'fiscal_end': fiscal,
                'version': version, 'filing_id': record['filing_id'], 'status': record['status'],
                'reasons': record['unavailable_reasons']})
        latest = max(current, key=lambda row: (row['fiscal_end'], int(row['version']))) if current else None
        reasons = (['unit_annual_accounts_unavailable'] if latest is None else
                   ['annual_accounts_older_than_730_days'] if (cutoff - _day(latest['fiscal_end'])).days > 730 else
                   latest['unavailable_reasons'])
        diagnostics.append({'kind': 'candidate', 'ticker': candidate['ticker'], 'cvm': candidate['cvm'],
            'share_class': 'UNIT', 'sector_code': candidate['sector_code'],
            'status': 'excluded' if reasons else 'included', 'reasons': reasons,
            'fiscal_end': latest['fiscal_end'] if latest else None,
            'filing_id': latest['filing_id'] if latest else None})
    statements.sort(key=lambda row: (row['cvm'], row['share_class'], row['fiscal_end'], int(row['version'])))
    return statements, diagnostics, sources
