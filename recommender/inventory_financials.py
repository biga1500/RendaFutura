"""Offline annual CVM inputs for the existing seven-feature training contract.

prepare_financials returns (statements, diagnostics, sources). Statements can be
passed unchanged to financial_features and stock_observations. Unavailable
filings retain null EPS to prevent fallback to an older, usable revision.
"""

from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from .collect_market_data import cvm_statements, normalize


TABLES = ('_DRE_con', '_DFC_MI_con', '_DFC_MD_con', '_BPA_con', '_BPP_con')
PRESENCE_ONLY = ('_DRA_con', '_DVA_con', '_DMPL_con')
COLUMNS = ('CD_CVM', 'DT_REFER', 'VERSAO', 'ORDEM_EXERC', 'MOEDA', 'ESCALA_MOEDA',
           'DT_INI_EXERC', 'DT_FIM_EXERC', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA')
RESEARCH_FILE = 'raw/primary-research/financials/review-20261025-v1.json'
RESEARCH_SHA256 = 'b290ff72b3b10de240c5168efdf1aaade4e32191038bd864db3590e53779efc1'
ADDITIONAL_RESEARCH = (('raw/primary-research/financials/review-20261025-v2.json',
                        '5aad347cb18b3e06d71bd20b93939176cb922b7b1ff73bbda8566e52e27e84a1'),
                       ('raw/primary-research/financials/review-20261025-v3.json',
                        'bf8bdf5cff86d0bb2ee444625b4b3a30076f8846c51ec6fbdf1b698bf6b0bd68'),
                       ('raw/primary-research/financials/review-20261025-v4.json',
                        'fa6aeb49df399661e79fc4d4df8a0629f9f82a6fe51f91f3701c6507d6f41458'),
                       ('raw/primary-research/financials/review-20261025-v5.json',
                        'b070e843d2edb96288af178ea9e30c51a00f12658326b9d779506b773c177b55'))


def _dre_fingerprint(rows, full=False):
    columns = ('CD_CONTA', 'DS_CONTA', 'VL_CONTA', 'MOEDA', 'ESCALA_MOEDA',
               'DT_INI_EXERC', 'DT_FIM_EXERC', 'ORDEM_EXERC')
    cells = sorted([str(row.get(column, '')) for column in columns] for row in rows
                   if full or row['CD_CONTA'].startswith(('3.11', '3.99')))
    return hashlib.sha256(json.dumps(cells, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _research(folder, specs, cutoff, diagnostics, sources):
    reviews = {}
    for filename, digest in ((RESEARCH_FILE, RESEARCH_SHA256), *ADDITIONAL_RESEARCH):
        reviews.update(_research_manifest(folder, specs, cutoff, diagnostics, sources, filename, digest))
    if specs.get('BBAS3') == (1023, 'ON', 2):
        from .bbas_financial_research import RESEARCH_FILE as BBAS_FILE, get_bbas_reading
        if (folder / BBAS_FILE).is_file():
            try:
                reading, receipts = get_bbas_reading(folder, cutoff)
                key = (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'])
                reviews[key] = reading
                sources.extend(receipts)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    csv.Error, zipfile.BadZipFile) as error:
                diagnostics.append({'kind': 'source', 'source_file': BBAS_FILE, 'status': 'excluded',
                    'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
    sanb_names = {'SANB3': (20532, 'ON', 2), 'SANB4': (20532, 'PN', 2)}
    sanb_specs = set(sanb_names.values())
    requested = [spec for spec in specs.values() if spec[0] == 20532]
    named_classes_match = all(name not in sanb_names or spec == sanb_names[name]
                              for name, spec in specs.items())
    if requested and named_classes_match and all(spec in sanb_specs for spec in requested):
        from .remaining_financial_research import RESEARCH_FILE as SANB_FILE, get_sanb_reading
        if (folder / SANB_FILE).is_file():
            try:
                reading, receipts = get_sanb_reading(folder, cutoff)
                key = (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'])
                reviews[key] = reading
                sources.extend(receipts)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    csv.Error, zipfile.BadZipFile) as error:
                diagnostics.append({'kind': 'source', 'source_file': SANB_FILE, 'status': 'excluded',
                    'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
    if any(cvm == 13439 and sector == 5 and share in ('ON', 'PN', 'PNA')
           for cvm, share, sector in specs.values()):
        from .rsul_financial_reading import BASE as RSUL_BASE, get_rsul_reading
        if (folder / RSUL_BASE / 'rsul-profit-root-review.json').is_file():
            try:
                reading, receipts = get_rsul_reading(folder, cutoff)
                key = (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'])
                reviews[key] = reading
                sources.extend(receipts)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    csv.Error, zipfile.BadZipFile) as error:
                diagnostics.append({'kind': 'source', 'source_file': RSUL_BASE, 'status': 'excluded',
                    'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
    cash_specs = {'ALLD3': (25330, 'ON', 4, 'BRALLDACNOR3'),
                  'JSLG3': (22020, 'ON', 5, 'BRJSLGACNOR2')}
    requested_cash = [{'ticker': ticker, 'cvm': cvm, 'share_class': share, 'isin': isin}
        for ticker, (cvm, share, sector, isin) in cash_specs.items()
        if specs.get(ticker) == (cvm, share, sector)]
    if requested_cash:
        from .round2_cash_eps import REVIEW_FILE as CASH_FILE, get_cash_eps_readings
        if (folder / CASH_FILE).is_file():
            try:
                readings, receipts = get_cash_eps_readings(folder, cutoff, requested_cash)
                cash_reviews = {}
                for reading in readings:
                    key = (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'])
                    if key in reviews and reviews[key] != reading:
                        raise ValueError('conflicting_exact_current_cash_issuer_reading')
                    cash_reviews[key] = reading
                reviews.update(cash_reviews)
                sources.extend(receipts)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    csv.Error, zipfile.BadZipFile) as error:
                diagnostics.append({'kind': 'source', 'source_file': CASH_FILE, 'status': 'excluded',
                    'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
    dexp_specs = {'DEXP3': (16632, 'ON', 1, 'BRDEXPACNOR1'),
                  'DEXP4': (16632, 'PNA', 1, 'BRDEXPACNPR8')}
    requested_dexp = [{'ticker': ticker, 'cvm': cvm, 'share_class': share, 'isin': isin}
        for ticker, (cvm, share, sector, isin) in dexp_specs.items()
        if specs.get(ticker) == (cvm, share, sector)]
    if requested_dexp:
        from .round2_dexp_eps import get_dexp_eps_readings, review_file
        dexp_file = review_file()
        if (folder / dexp_file).is_file():
            try:
                readings, receipts = get_dexp_eps_readings(folder, cutoff, requested_dexp)
                dexp_reviews = {}
                for reading in readings:
                    key = (reading['cvm'], reading['fiscal_end'], reading['version'], reading['filing_id'])
                    if key in reviews and reviews[key] != reading:
                        raise ValueError('conflicting_exact_current_dexp_reading')
                    dexp_reviews[key] = reading
                reviews.update(dexp_reviews)
                sources.extend(receipts)
            except (OSError, ValueError, KeyError, TypeError, ArithmeticError,
                    csv.Error, zipfile.BadZipFile) as error:
                diagnostics.append({'kind': 'source', 'source_file': dexp_file, 'status': 'excluded',
                    'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
    return reviews


def _research_manifest(folder, specs, cutoff, diagnostics, sources, filename, digest):
    """Only the reviewed, immutable manifest can authorize a documentary reading."""
    path = folder / filename
    if not path.is_file():
        return {}
    try:
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError('research_manifest_hash_mismatch')
        manifest = json.loads(body)
        requested = {spec[0] for spec in specs.values()}
        reviews, receipts = {}, {}
        for review in manifest['readings']:
            if review['cvm'] not in requested or _day(review['published_at']) > cutoff:
                continue
            proof = manifest['documents'][review['document']]
            if Path(proof['file']).name != proof['file']:
                raise ValueError('primary_document_must_be_local_basename')
            relative = path.parent.relative_to(folder) / proof['file']
            try:
                content = (folder / relative).read_bytes()
                if hashlib.sha256(content).hexdigest() != proof['sha256']:
                    raise ValueError('primary_document_hash_mismatch')
            except (OSError, ValueError):
                diagnostics.append({'kind': 'source', 'source_file': relative.as_posix(),
                                    'cvm': review['cvm'], 'status': 'excluded',
                                    'reasons': ['documentary_readings_unavailable']})
                continue
            source_id = 'primary-financials:' + proof['sha256']
            receipts[source_id] = {
                'id': source_id, 'url': proof['url'], 'file': relative.as_posix(),
                'sha256': proof['sha256'], 'bytes': len(content),
                'retrieved_at': None,
                'observed_at': datetime.fromtimestamp((folder / relative).stat().st_mtime, timezone.utc).isoformat(),
                'observed_at_basis': 'local_file_mtime_not_network_retrieval',
                'provider': proof['issuer'], 'authority': 'primary',
                'document_type': 'audited_annual_statements', 'use': 'account_interpretation'}
            key = review['cvm'], review['fiscal_end'], review['version'], review['filing_id']
            reviews[key] = {**review, 'manifest_file': filename, 'manifest_sha256': digest,
                            'primary_evidence': {
                'source_ref': source_id, 'pdf_page': review['pdf_page'], 'note': review['note']}}
        sources.extend(receipts.values())
        return reviews
    except (OSError, ValueError, KeyError, TypeError) as error:
        diagnostics.append({'kind': 'source', 'source_file': filename, 'status': 'excluded',
                            'reasons': ['documentary_readings_unavailable'], 'error_type': type(error).__name__})
        return {}


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('nonfinite_documented_value')
    return number


def _brl(row):
    if row['MOEDA'] != 'REAL' or row['ESCALA_MOEDA'] not in ('MIL', 'UNIDADE'):
        raise ValueError('invalid_documented_profit_units')
    return _decimal(row['VL_CONTA']) * (1000 if row['ESCALA_MOEDA'] == 'MIL' else 1)


def _checked_note_eps(rows, review):
    """Validate printed EPS against printed weighted shares, never manufacture EPS."""
    entries = review.get('basic_eps', [])
    if not entries:
        return
    by_code = defaultdict(list)
    for row in rows:
        by_code[row['CD_CONTA']].append(row)

    def account(code):
        matches = by_code[code]
        if len(matches) != 1:
            raise ValueError('ambiguous_documented_account')
        return matches[0]

    profit = _decimal(review['parent_profit_brl'])
    original = _brl(account(review.get('profit_account', '3.11.01')))
    if review.get('correct_parent_profit_sign'):
        # A same-filing consolidated reconciliation must independently support the sign.
        if (original != -profit or original <= 0 or profit >= 0
                or _brl(account('3.11')) != profit + _brl(account('3.11.02'))):
            raise ValueError('documented_parent_loss_does_not_reconcile')
    elif original != profit:
        raise ValueError('documented_parent_profit_mismatch')
    allocations = {key: _decimal(value) for key, value in review.get('profit_allocations_brl', {}).items()}
    if allocations and sum(allocations.values()) != profit:
        raise ValueError('documented_class_allocations_do_not_reconcile')
    classes = set()
    for entry in entries:
        share = entry['share_class']
        if share not in ('ON', 'PN', 'PNA', 'PNB') or share in classes:
            raise ValueError('ambiguous_documented_share_class')
        classes.add(share)
        eps, numerator, shares = map(_decimal, (entry['value'], entry['numerator_brl'], entry['weighted_shares']))
        num_rounding = _decimal(entry['numerator_rounding_brl'])
        shares_rounding = _decimal(entry['shares_rounding'])
        if (not eps or not profit or shares <= 0 or min(num_rounding, shares_rounding) < 0
                or shares <= shares_rounding or numerator * profit <= 0):
            raise ValueError('invalid_documented_eps_denominator')
        if numerator != (allocations[entry['allocation']] if allocations else profit):
            raise ValueError('documented_eps_numerator_mismatch')
        components = entry.get('pool_components')
        if components and (share not in components or sum(map(_decimal, components.values())) != shares):
            raise ValueError('documented_preferred_pool_does_not_reconcile')
        ratios = [(numerator + sign * num_rounding) / (shares + other * shares_rounding)
                  for sign in (-1, 1) for other in (-1, 1)]
        half_unit = Decimal(10) ** eps.as_tuple().exponent / 2
        if max(ratios) < eps - half_unit or min(ratios) > eps + half_unit:
            raise ValueError('reported_basic_eps_fails_weighted_share_crosscheck')
        if not entry['account'].startswith('note:'):
            cell = account(entry['account'])
            if (not cell['CD_CONTA'].startswith('3.99.01.') or cell['DS_CONTA'].strip().upper() != share
                    or _decimal(cell['VL_CONTA']) != eps):
                raise ValueError('documented_native_eps_mismatch')


def _documented_accounts(tables, contexts, filings, reviews, diagnostics, scope='con'):
    suffix = f'_DRE_{scope}'
    accounts_scope = 'consolidated' if scope == 'con' else 'individual'
    grouped = defaultdict(list)
    for row in tables[suffix]:
        grouped[_key(row)].append(row)
    for key, rows in grouped.items():
        review = reviews.get((*key, filings[key]['ID_DOC']))
        if (review is None or filings[key]['DT_RECEB'] != review['published_at']
                or review.get('accounts_scope', 'consolidated') != accounts_scope):
            continue
        if _dre_fingerprint(rows, full=review.get('full_dre', False)) != review['dre_sha256']:
            diagnostics.append({'kind': 'scope', 'cvm': key[0], 'fiscal_end': key[1], 'version': key[2],
                                'status': 'excluded', 'accounts_scope': accounts_scope,
                                'reasons': ['documented_reading_original_cells_mismatch']})
            continue
        try:
            _checked_note_eps(rows, review)
        except (ValueError, KeyError, TypeError, InvalidOperation, ZeroDivisionError):
            contexts[key]['reasons'].append('documented_basic_eps_crosscheck_failed')
            continue
        contexts[key]['primary_evidence'] = [review['primary_evidence']]
        contexts[key]['documentary_reading'] = {
            'manifest_file': review['manifest_file'], 'manifest_sha256': review['manifest_sha256'],
            'original_dre_sha256': review['dre_sha256']}
        gate = 'documented_individual_scope_reading_not_validated'
        if gate in contexts[key]['reasons']:
            contexts[key]['reasons'].remove(gate)
            contexts[key]['documentary_reading']['individual_scope_archive_sha256'] = review['individual_scope_archive_sha256']
        discarded = set(review.get('discard_basic_eps_accounts', []))
        for row in rows:
            if row['CD_CONTA'] in discarded:
                diagnostic = _account_diagnostic(key, suffix, row, status='excluded',
                                                 reasons=[review.get('discard_reason',
                                                          'documented_duplicate_basic_eps_placeholder')])
                diagnostics.append({**diagnostic, 'primary_evidence': review['primary_evidence']})
                tables[suffix].remove(row)
        if review.get('profit_account') and scope == 'con':
            # The note identifies this reported root as the EPS numerator. Never infer it from zero NCI.
            root = next(row for row in rows if row['CD_CONTA'] == review['profit_account'])
            diagnostic = _account_diagnostic(key, '_DRE_con', root, status='recognized',
                                             reasons=['documented_same_scope_parent_profit_root'])
            diagnostics.append({**diagnostic, 'primary_evidence': review['primary_evidence']})
            tables['_DRE_con'][:] = [row for row in tables['_DRE_con']
                                    if not (_key(row) == key and row['CD_CONTA'] == '3.11.01')]
            contexts[key]['account_aliases']['3.11.01'] = root['CD_CONTA']
            root.update(CD_CONTA='3.11.01', DS_CONTA='Atribuido a Socios da Empresa Controladora')
        if review.get('correct_parent_profit_sign'):
            root = next(row for row in rows if row['CD_CONTA'] == '3.11.01')
            diagnostic = _account_diagnostic(key, '_DRE_con', root, status='recognized',
                                             reasons=['documented_parent_loss_sign_reconciled_with_consolidated_total_and_nci'])
            diagnostics.append({**diagnostic, 'primary_evidence': review['primary_evidence']})
            root['VL_CONTA'] = str(-_decimal(root['VL_CONTA']))
        contexts[key]['reported_basic_eps'] = review.get('basic_eps', [])
        for entry in review.get('basic_eps', []):
            if not entry['account'].startswith('note:'):
                continue
            share = entry['share_class']
            for row in tables[suffix]:
                if (_key(row) == key and row['CD_CONTA'].startswith('3.99.01.')
                        and row['DS_CONTA'].strip().upper() == share):
                    diagnostic = _account_diagnostic(key, suffix, row, status='excluded', reasons=[
                        'documented_note_total_basic_eps_supersedes_form_placeholder_or_component'])
                    diagnostics.append({**diagnostic, 'primary_evidence': review['primary_evidence']})
            tables[suffix][:] = [row for row in tables[suffix]
                                    if not (_key(row) == key and row['CD_CONTA'].startswith('3.99.01.')
                                            and row['DS_CONTA'].strip().upper() == share)]
            adapter_code = '3.99.01.note_' + share
            cell = {**rows[0], 'CD_CONTA': adapter_code, 'DS_CONTA': share,
                    'VL_CONTA': entry['value'], 'ESCALA_MOEDA': 'UNIDADE'}
            tables[suffix].append(cell)
            contexts[key]['account_aliases'][adapter_code] = entry['account']
            diagnostic = _account_diagnostic(key, suffix, cell, status='recognized',
                                             reasons=['reported_note_basic_eps_exact_share_class'])
            diagnostics.append({**diagnostic, 'account': entry['account'], 'statement': f'notes_{scope}',
                                'primary_evidence': review['primary_evidence']})


def _day(value):
    return date.fromisoformat(value[:10]) if isinstance(value, str) else value


def _annual_start(end):
    following = end + timedelta(days=1)
    try:
        return following.replace(year=following.year - 1)
    except ValueError:
        return date(following.year - 1, 3, 1)


def _capex_reading(description):
    desc = normalize(description).strip()
    if any(word in desc for word in ('venda', 'alien', 'resgate', 'arrend', 'recebimento')):
        return False
    # Public DFC investment rows sometimes name the acquired asset without a verb.
    return (desc in ('imobilizado', 'intangivel', 'no imobilizado', 'no intangivel')
            or (any(word in desc for word in ('imobiliz', 'intangiv'))
                and desc.startswith(('acrescimo de ', 'aplicacoes no ', 'aplicacao no '))))


def _account_diagnostic(key, suffix, row, *, status, reasons):
    return {'kind': 'account', 'cvm': key[0], 'fiscal_end': key[1], 'version': key[2],
            'account': row['CD_CONTA'], 'statement': suffix[1:], 'status': status,
            'accounts_scope': 'individual' if suffix.endswith('_ind') else 'consolidated',
            'reasons': reasons, 'description': row['DS_CONTA'],
            'reported_value': row['VL_CONTA'], 'reported_scale': row.get('ESCALA_MOEDA'),
            'period_start': row.get('DT_INI_EXERC'), 'period_end': row.get('DT_FIM_EXERC')}


def _parser_tables(tables, diagnostics, indirect_keys, scope='con'):
    indirect_suffix, direct_suffix = f'_DFC_MI_{scope}', f'_DFC_MD_{scope}'
    direct = tables.pop(direct_suffix)
    for row in direct:
        key = _key(row)
        if key not in indirect_keys:
            tables[indirect_suffix].append(row)
            diagnostics.append(_account_diagnostic(
                key, direct_suffix, row, status='recognized',
                reasons=['direct_cash_flow_same_filing_without_indirect_statement']))
    for suffix, rows in tables.items():
        if not suffix:
            continue
        grouped = defaultdict(list)
        for row in rows:
            grouped[_key(row), row['CD_CONTA']].append(row)
        canonical = []
        for (key, _), duplicates in grouped.items():
            source_suffix = (direct_suffix if suffix == indirect_suffix and key not in indirect_keys
                             else suffix)
            cells = {tuple(row.get(column) for column in (
                'MOEDA', 'ESCALA_MOEDA', 'DT_INI_EXERC', 'DT_FIM_EXERC', 'DS_CONTA', 'VL_CONTA'))
                     for row in duplicates}
            if len(cells) > 1:
                for row in duplicates:
                    diagnostics.append(_account_diagnostic(
                        key, source_suffix, row, status='excluded', reasons=['conflicting_duplicate_account']))
                    row['VL_CONTA'] = 'NaN'
                canonical.extend(duplicates)
            else:
                canonical.append(duplicates[0])
                for row in duplicates[1:]:
                    diagnostics.append(_account_diagnostic(
                        key, source_suffix, row, status='recognized',
                        reasons=['identical_duplicate_account_counted_once']))
        tables[suffix] = rows = canonical
        if suffix != indirect_suffix:
            continue
        for row in rows:
            if not row['CD_CONTA'].startswith('6.02.'):
                continue
            alias = _capex_reading(row['DS_CONTA'])
            desc = normalize(row['DS_CONTA'])
            acquisition = (any(word in desc for word in ('imobiliz', 'intangiv'))
                           and (any(word in desc for word in ('aquis', 'adico', 'compra'))
                                or 'investimento' in desc and not any(word in desc for word in (
                                    'venda', 'alien', 'resgat', 'receb', 'arrend', 'transf', 'incorp'))))
            source_suffix = indirect_suffix if _key(row) in indirect_keys else direct_suffix
            try:
                positive = float(row['VL_CONTA']) > 0
            except (TypeError, ValueError, OverflowError):
                positive = False
            if positive and (alias or acquisition):
                diagnostics.append(_account_diagnostic(
                    _key(row), source_suffix, row, status='excluded',
                    reasons=['positive_capex_requires_primary_cash_outflow_proof']))
                # Preserve the leg as invalid so another negative leg cannot supply a partial FCF.
                row['VL_CONTA'] = 'NaN'
            if alias and not positive:
                diagnostics.append(_account_diagnostic(
                    _key(row), source_suffix,
                    row, status='recognized',
                    reasons=['explicit_ppe_intangible_investing_account']))
            if alias:
                row['DS_CONTA'] = 'Aquisicao: ' + row['DS_CONTA']
    return tables


def _individual_accounts(tables, contexts, specs, diagnostics):
    financial = {cvm for cvm, _, sector in specs.values() if sector == 2}
    for key, context in contexts.items():
        if context['accounts_scope'] == 'individual' and key[0] not in financial:
            context['account_aliases']['3.11.01'] = '3.11'
    profits = defaultdict(list)
    for row in tables['_DRE_ind']:
        profits[_key(row)].append(row)
    for key, rows in profits.items():
        roots = [row for row in rows if row['CD_CONTA'].count('.') == 1
                 and normalize(row['DS_CONTA']).strip() in (
                     'lucro/prejuizo do periodo', 'lucro/prejuizo liquido do periodo',
                     'lucro ou prejuizo liquido do periodo', 'lucro liquido do periodo')
                 and (key[0] in financial or row['CD_CONTA'] == '3.11')]
        if len(roots) != 1 or any(row['CD_CONTA'] == '3.11.01' for row in rows):
            contexts[key]['reasons'].append('individual_profit_root_absent_or_ambiguous')
            continue
        row = roots[0]
        diagnostics.append(_account_diagnostic(
            key, '_DRE_ind', row, status='recognized', reasons=['individual_net_profit_root_for_legacy_parser']))
        contexts[key]['account_aliases']['3.11.01'] = row['CD_CONTA']
        row.update(CD_CONTA='3.11.01', DS_CONTA='Atribuido a Socios da Empresa Controladora')
    for row in tables['_BPP_ind']:
        if (int(row['CD_CVM']) in financial and row['CD_CONTA'].count('.') == 1
                and normalize(row['DS_CONTA']).strip() in ('patrimonio liquido', 'patrimonio liquido individual')):
            diagnostics.append(_account_diagnostic(
                _key(row), '_BPP_ind', row, status='recognized',
                reasons=['individual_equity_root_for_legacy_parser']))
            row['DS_CONTA'] = 'Patrimonio Liquido Consolidado'


def _specs(candidates, diagnostics):
    if isinstance(candidates, dict):
        candidates = [({'ticker': ticker, **value} if isinstance(value, dict) else
                       dict(zip(('ticker', 'cvm', 'share_class', 'sector_code'), (ticker, *value))))
                      for ticker, value in candidates.items()]
    specs = {}
    for candidate in candidates:
        ticker = candidate['ticker']
        cvm, share, sector = (candidate.get(key) for key in ('cvm', 'share_class', 'sector_code'))
        if isinstance(cvm, str) and cvm.isascii() and cvm.isdigit():
            cvm = int(cvm)
        share = share.strip().upper() if isinstance(share, str) else None
        reasons = []
        if isinstance(cvm, bool) or not isinstance(cvm, int) or cvm <= 0:
            reasons.append('unresolved_cvm')
        if (not share or share in ('UNKNOWN', 'UNIT', 'UNITS', 'UNT') or ticker.endswith('11')
                or normalize(candidate.get('security_type') or '') == 'units'):
            reasons.append('unit_or_unknown_exact_share_class')
        if isinstance(sector, bool) or not isinstance(sector, int):
            reasons.append('unresolved_sector_code')
        if reasons:
            diagnostics.append({'kind': 'candidate', 'ticker': ticker, 'status': 'excluded', 'reasons': reasons})
        else:
            specs[ticker] = (cvm, share, sector)
    return specs


def _caches(folder, cutoff):
    found = {}
    for directory in (folder / 'raw/official-financials', folder / 'raw'):
        for path in sorted(directory.glob('*.zip')):
            match = re.fullmatch(r'(?:cvm-(dfp|itr)-?(\d{4})|(dfp|itr)_cia_aberta_(\d{4}))\.zip', path.name)
            if not match:
                continue
            form, year = match[1] or match[3], int(match[2] or match[4])
            if 2015 <= year <= cutoff.year:
                found.setdefault((form, year), path)
    return sorted(found.items())


def _rows(archive, name):
    with archive.open(name) as stream:
        yield from csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')


def _key(row):
    return int(row['CD_CVM']), row['DT_REFER'], row['VERSAO']


def _annual_archive(content, year, specs, cutoff, diagnostics, reviews=None):
    requested = {spec[0] for spec in specs.values()}
    filings = {}
    contexts = {}
    with zipfile.ZipFile(io.BytesIO(content)) as original:
        for row in _rows(original, f'dfp_cia_aberta_{year}.csv'):
            try:
                key = _key(row)
                if key[0] not in requested:
                    continue
                fiscal, published = _day(row['DT_REFER']), _day(row['DT_RECEB'])
                if fiscal > cutoff or published > cutoff or int(row['VERSAO']) < 1 or not row['ID_DOC']:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            if key not in filings or row['DT_RECEB'] > filings[key]['DT_RECEB']:
                filings[key] = {**row, 'DT_RECEB': published.isoformat()}
        if not filings:
            return None, contexts
        # A reviewed filing can identify an empty consolidated form as a template.
        # Bind the whole native archive as well as its identity; zeros alone never authorize fallback.
        digest = hashlib.sha256(content).hexdigest()
        documented_individual = set()
        for key, filing in filings.items():
            review = (reviews or {}).get((*key, filing['ID_DOC']))
            if (review and review.get('accounts_scope') == 'individual'
                    and review.get('individual_scope_archive_sha256') == digest
                    and review['published_at'] == filing['DT_RECEB']):
                documented_individual.add(key)
        tables = {'': list(filings.values())}
        consolidated = set()
        for scope in ('con', 'ind'):
            scoped = {suffix.removesuffix('con') + scope: [] for suffix in TABLES}
            indirect_keys = set()
            for suffix in (*scoped, *(PRESENCE_ONLY if scope == 'con' else ())):
                name = f'dfp_cia_aberta{suffix}_{year}.csv'
                if name not in original.namelist():
                    continue
                for original_row in _rows(original, name):
                    try:
                        key = _key(original_row)
                    except (ValueError, KeyError, TypeError):
                        continue
                    if key not in filings or scope == 'ind' and key in consolidated:
                        continue
                    if scope == 'con' and key in documented_individual:
                        continue
                    # Presence is decided from the full core, before validating any cell.
                    if scope == 'con':
                        consolidated.add(key)
                    context = contexts.setdefault(key, {
                        'accounts_scope': 'consolidated' if scope == 'con' else 'individual',
                        'scope_selection': ('consolidated_core_present' if scope == 'con' else
                                            'documented_individual_only_filing_zero_consolidated_templates'
                                            if key in documented_individual else
                                            'individual_only_no_consolidated_core'),
                        'source_statements': [], 'account_aliases': {},
                        'reasons': (['documented_individual_scope_reading_not_validated']
                                    if key in documented_individual else [])})
                    if name not in context['source_statements']:
                        context['source_statements'].append(name)
                    if suffix not in scoped:
                        continue
                    if original_row.get('ORDEM_EXERC') != '\u00daLTIMO':
                        continue
                    if suffix == f'_DFC_MI_{scope}':
                        indirect_keys.add(key)
                    if original_row.get('MOEDA') != 'REAL':
                        continue
                    row = dict(original_row)
                    fiscal = _day(row['DT_REFER'])
                    annual_flow = suffix.startswith(('_DRE_', '_DFC_'))
                    invalid_period = (row.get('DT_FIM_EXERC') != fiscal.isoformat()
                                      or annual_flow and row.get('DT_INI_EXERC') != _annual_start(fiscal).isoformat())
                    invalid_scale = not row['CD_CONTA'].startswith('3.99') and row.get('ESCALA_MOEDA') not in ('MIL', 'UNIDADE')
                    if invalid_period or invalid_scale:
                        diagnostics.append(_account_diagnostic(
                            key, suffix, row, status='excluded', reasons=[
                                reason for reason, invalid in (
                                    ('nonannual_or_mismatched_period', invalid_period),
                                    ('unrecognized_monetary_scale', invalid_scale)) if invalid]))
                        # Keep invalid legs visible so the parser cannot use a partial flow.
                        row.update(VL_CONTA='NaN', ESCALA_MOEDA='UNIDADE')
                    scoped[suffix].append(row)
            if reviews:
                _documented_accounts(scoped, contexts, filings, reviews, diagnostics, scope)
            scoped = _parser_tables(scoped, diagnostics, indirect_keys, scope)
            if scope == 'ind':
                _individual_accounts(scoped, contexts, specs, diagnostics)
            for suffix, rows in scoped.items():
                canonical = suffix.removesuffix(scope) + 'con'
                tables.setdefault(canonical, []).extend(rows)
        for key, context in contexts.items():
            diagnostics.append({'kind': 'scope', 'cvm': key[0], 'fiscal_end': key[1], 'version': key[2],
                                'accounts_scope': context['accounts_scope'], 'status': 'selected',
                                'reasons': [context['scope_selection']],
                                'source_statements': context['source_statements']})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as filtered:
        for suffix, rows in tables.items():
            stream = io.StringIO()
            columns = list(rows[0]) if rows else list(COLUMNS)
            writer = csv.DictWriter(stream, fieldnames=columns, delimiter=';')
            writer.writeheader()
            writer.writerows(rows)
            filtered.writestr(f'dfp_cia_aberta{suffix}_{year}.csv', stream.getvalue().encode('latin1'))
    return output.getvalue(), contexts


def prepare_financials(folder, candidates, cutoff):
    """Read cached DFPs only; never download, publish, or modify a cache.

    candidates is a sequence of identity records or a ticker -> (CVM, class,
    sector) mapping. Annual consolidated figures take precedence; individual
    figures require no consolidated core, or an exact-archive documentary scope
    reading identifying the consolidated form as an unused template.
    Cached ITR is listed as audit-only, never treated as annual/TTM earnings.
    """
    folder, cutoff = Path(folder), _day(cutoff)
    diagnostics, statements, sources = [], [], []
    specs = _specs(candidates, diagnostics)
    parser_specs = dict(specs)
    class_proofs = []
    if specs.get('SHUL4') == (14664, 'PNU', 5):
        from .schulz_share_class import IDENTITY, class_binding
        if isinstance(candidates, dict):
            requested = candidates['SHUL4']
            identity = {**IDENTITY, **requested} if isinstance(requested, dict) else IDENTITY
        else:
            identity = next(row for row in candidates if row['ticker'] == 'SHUL4')
        try:
            class_proof = class_binding(folder, identity, cutoff)
            parser_specs['SHUL4'] = (14664, 'PN', 5)
            sources.extend(class_proof['sources'])
            class_proofs.append(class_proof)
        except (OSError, ValueError, KeyError, TypeError) as error:
            diagnostics.append({'kind': 'class_evidence', 'ticker': 'SHUL4', 'status': 'excluded',
                'reasons': ['exact_share_class_binding_unavailable'], 'error': str(error)})
    from .preferred_class_binding import IDENTITIES as PREFERRED_IDENTITIES, class_binding as preferred_binding
    for ticker, expected in PREFERRED_IDENTITIES.items():
        if ticker not in specs or specs[ticker][:2] != (expected['cvm'], expected['share_class']):
            continue
        if isinstance(candidates, dict):
            requested = candidates[ticker]
            identity = {'ticker': ticker, **expected, **(requested if isinstance(requested, dict) else {})}
        else:
            identity = next(row for row in candidates if row['ticker'] == ticker)
        try:
            proof = preferred_binding(folder, identity, cutoff)
            parser_specs[ticker] = (expected['cvm'], proof['financial_source_class'], specs[ticker][2])
            class_proofs.append(proof)
            sources.extend(proof['sources'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            diagnostics.append({'kind': 'class_evidence', 'ticker': ticker, 'status': 'excluded',
                'reasons': ['exact_share_class_binding_unavailable'], 'error': str(error)})
    reviews = _research(folder, specs, cutoff, diagnostics, sources)
    financial = {(cvm, share) for cvm, share, sector in specs.values() if sector == 2}
    for (form, year), path in _caches(folder, cutoff):
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        source = {'id': f'cvm:{form.upper()}:{year}:{digest}',
                  'url': f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{form.upper()}/DADOS/{form}_cia_aberta_{year}.zip',
                  'file': path.relative_to(folder).as_posix(), 'sha256': digest, 'bytes': len(content),
                  'retrieved_at': None,
                  'observed_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                  'observed_at_basis': 'local_file_mtime_not_network_retrieval',
                  'provider': 'CVM', 'authority': 'primary', 'document_type': form.upper(), 'year': year,
                  'use': 'annual_model_features' if form == 'dfp' else 'audit_only_not_annual_or_ttm'}
        sources.append(source)
        if form == 'itr':
            diagnostics.append({'kind': 'source', 'source_ref': source['id'], 'status': 'excluded',
                                'reasons': ['interim_flows_not_substituted_for_annual_model_features']})
            continue
        if not specs:
            continue
        audit = []
        try:
            filtered, contexts = _annual_archive(content, year, specs, cutoff, audit, reviews)
            if filtered and class_proofs:
                # A source-label bridge must not select PN over another native
                # preferred EPS line in the same exact annual filing.
                with zipfile.ZipFile(io.BytesIO(filtered)) as archive:
                    eps_classes = defaultdict(set)
                    for cell in _rows(archive, f'dfp_cia_aberta_DRE_con_{year}.csv'):
                        if cell['CD_CONTA'].startswith('3.99.01.'):
                            eps_classes[_key(cell)].add(cell['DS_CONTA'].strip().upper())
                for proof in class_proofs:
                    labels = {proof['financial_source_class'], proof['registered_class']}
                    for key, classes in eps_classes.items():
                        if key[0] == proof['identity']['cvm'] and labels <= classes:
                            contexts[key]['reasons'].append('ambiguous_native_and_bound_preferred_EPS_classes')
            parsed = cvm_statements(filtered, year, specs=parser_specs, diagnostics=audit) if filtered else []
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, csv.Error, zipfile.BadZipFile) as error:
            diagnostics.append({'kind': 'source', 'source_ref': source['id'], 'status': 'excluded',
                                'reasons': ['invalid_cached_archive'], 'error_type': type(error).__name__})
            continue
        for class_proof in class_proofs:
            binding_cvm = class_proof['identity']['cvm']
            source_class = class_proof['financial_source_class']
            registered_class = class_proof['registered_class']
            parsed = [record for record in parsed if (record['cvm'], record['share_class']) != (binding_cvm, source_class)
                      or record['fiscal_end'] >= class_proof['financial_period_from']]
            for row in [*parsed, *audit]:
                if row.get('cvm') != binding_cvm or row.get('share_class') != source_class:
                    continue
                row['share_class'] = registered_class
                method = row.get('financial_method')
                if method is not None:
                    method['source_share_class'] = source_class
                    method['registered_share_class'] = registered_class
                    method['share_class_binding'] = {
                        'id': class_proof['id'], 'financial_period_from': class_proof['financial_period_from'],
                        'sources': [{key: source.get(key) for key in ('file', 'sha256', 'url')}
                                    for source in class_proof['sources']]}
                if row.get('fiscal_end', '') < class_proof['financial_period_from']:
                    row.update(status='excluded', reasons=[*row.get('reasons', []),
                               'exact_class_binding_does_not_cover_financial_period'])
        by_key = {(r['cvm'], r['share_class'], r['fiscal_end'], r['version']): r for r in parsed}
        readings = defaultdict(list)
        for row in audit:
            if row.get('kind') == 'account' and row.get('status') == 'recognized':
                readings[row['cvm'], row['fiscal_end'], row['version']].append({
                    key: row[key] for key in ('account', 'statement', 'description', 'reported_value',
                                             'reported_scale', 'period_start', 'period_end', 'reasons',
                                             'accounts_scope')})
        for row in audit:
            row.update(source_ref=source['id'], source_file=source['file'])
            if 'filing_id' not in row:
                diagnostics.append(row)
                continue
            row['kind'] = 'filing'
            context = contexts.get((row['cvm'], row['fiscal_end'], row['version']), {})
            scope = context.get('accounts_scope')
            method = row['financial_method']
            method.update(accounts_scope=scope, source_statements=context.get('source_statements', []),
                          scope_selection=context.get('scope_selection', 'no_core_accounts_for_version'))
            if context.get('primary_evidence'):
                method['primary_evidence'] = context['primary_evidence']
                method['documentary_reading'] = context['documentary_reading']
                method['reported_basic_eps'] = [entry for entry in context.get('reported_basic_eps', [])
                                                if entry['share_class'] == method.get('source_share_class', row['share_class'])]
            aliases = context.get('account_aliases', {})
            for field in ('profit_account', 'basic_eps_account'):
                method[field] = aliases.get(method[field], method[field])
            for field in ('missing_accounts', 'invalid_numeric_accounts'):
                row[field] = [aliases.get(code, code) for code in row[field]]
            if scope == 'individual':
                method['liabilities_method'] = method['liabilities_method'].replace('consolidated', 'individual')
                row['reasons'] = [reason.replace('consolidated', 'individual') for reason in row['reasons']]
            row.update(accounts_scope=scope, reasons=[*row['reasons'], *context.get('reasons', [])])
            if context.get('reasons'):
                row['status'] = 'excluded'
            evidence = readings[row['cvm'], row['fiscal_end'], row['version']]
            if evidence:
                row['financial_method']['account_readings'] = evidence
            key = row['cvm'], row['share_class'], row['fiscal_end'], row['version']
            record = by_key.get(key)
            if context.get('reasons'):
                record = None
            if record and (row['cvm'], row['share_class']) in financial and record['financial_method']['id'] != 'financial_no_industrial_fcf_v1':
                row.update(status='excluded', reasons=[*row['reasons'], 'financial_root_balance_accounts_required'])
                record = None
            if record is None:
                record = {key: row[key] for key in ('cvm', 'share_class', 'fiscal_end', 'version', 'published_at', 'filing_id', 'financial_method')}
                record.update(profit_per_share=None, free_cash_per_share=None, debt_ratio=None,
                              unavailable_reasons=row['reasons'])
            record = {**record, 'source_ref': source['id'], 'source_file': source['file'],
                      'document_type': 'DFP', 'period_type': 'annual', 'annualized': False,
                      'accounts_scope': scope,
                      'status': row['status']}
            statements.append(record)
            diagnostics.append(row)
    by_issuer = defaultdict(list)
    for record in statements:
        by_issuer[record['cvm'], record['share_class']].append(record)
    for ticker, (cvm, share, sector) in specs.items():
        eligible = by_issuer[cvm, share]
        latest = max(eligible, key=lambda r: (r['fiscal_end'], int(r['version']))) if eligible else None
        reasons = (['annual_accounts_unavailable'] if latest is None else
                   ['annual_accounts_older_than_730_days'] if (cutoff - _day(latest['fiscal_end'])).days > 730 else
                   latest.get('unavailable_reasons', []))
        diagnostics.append({'kind': 'candidate', 'ticker': ticker, 'cvm': cvm, 'share_class': share,
                            'sector_code': sector, 'status': 'excluded' if reasons else 'included', 'reasons': reasons,
                            'accounts_scope': latest['accounts_scope'] if latest else None,
                            'filing_id': latest['filing_id'] if latest else None,
                            'fiscal_end': latest['fiscal_end'] if latest else None,
                            'published_at': latest['published_at'] if latest else None})
    statements.sort(key=lambda r: (r['cvm'], r['share_class'], r['fiscal_end'], int(r['version'])))
    return statements, diagnostics, sources
