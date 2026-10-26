"""Independent, dated fields from official CVM DFP/ITR archives.

parse_archives accepts {official_zip_filename: bytes}, specs and an as_of date.
Each instrument uses one accounting scope. Missing latest fields may use an
earlier fiscal filing, explicitly identified on the field, never an old version
of the same filing. No annualization, TTM or per-share FCF is inferred.
"""

from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import urllib.error
import urllib.request
import zipfile


BASE_URL = 'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{form}/DADOS/{name}'
FIELD_UNITS = {
    'basic_eps': 'BRL/share', 'total_assets': 'BRL', 'total_passivo': 'BRL',
    'equity': 'BRL', 'total_liabilities': 'BRL', 'liabilities_to_assets': 'ratio',
    'revenue': 'BRL', 'attributable_profit': 'BRL', 'net_income': 'BRL',
    'operating_cash_flow': 'BRL', 'capex': 'BRL', 'free_cash_flow': 'BRL',
}
SHARE_CLASSES = {'ON', 'PN', 'PNA', 'PNB', 'PNC', 'PND'}
PROFIT_NAMES = {'atribuido a socios da empresa controladora',
                'atribuido aos socios da empresa controladora'}
REVENUE_NAMES = {'receita de venda de bens e/ou servicos', 'receita de venda de bens e servicos',
                 'receitas da intermediacao financeira', 'receitas de intermediacao financeira'}


def _normalize(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value.lower())
                            if not unicodedata.combining(c)).split())


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Dates must use YYYY-MM-DD.')
    return parsed


def _specs(specs):
    result = {}
    for ticker, spec in specs.items():
        cvm, share_class, sector = spec
        if isinstance(cvm, str) and re.fullmatch(r'[0-9]+', cvm):
            cvm = int(cvm)
        if (not isinstance(ticker, str) or not ticker or isinstance(cvm, bool)
                or cvm is not None and (not isinstance(cvm, int) or cvm <= 0)
                or share_class is not None and not isinstance(share_class, str)
                or isinstance(sector, bool) or not isinstance(sector, int)):
            raise ValueError('Specs require a positive CVM integer or None, share class or None, and sector integer.')
        result[ticker] = (cvm, (share_class or 'UNKNOWN').strip().upper() or 'UNKNOWN', sector)
    return result


def _source(name, content, metadata=None):
    match = re.fullmatch(r'(dfp|itr)_cia_aberta_(\d{4})\.zip', name)
    if not match:
        raise ValueError('Use official DFP/ITR ZIP filenames as archive keys.')
    form, year = match.group(1).upper(), int(match.group(2))
    digest = hashlib.sha256(content).hexdigest()
    result = {'id': f'cvm:{form}:{year}:{digest}', 'url': BASE_URL.format(form=form, name=name),
              'file': 'official-financials/' + name, 'sha256': digest, 'bytes': len(content),
              'retrieved_at': None, 'status': 'available', 'document_type': form, 'year': year}
    if metadata:
        result['retrieved_at'] = metadata.get('retrieved_at')
    return result


def _field(name, filing, scope, *, status='missing', reason='account_not_identified',
           value=None, method=None, rows=(), flow=False):
    first = rows[0] if rows else None
    start, end, kind = None, filing['fiscal_end'] if filing else None, None
    if first:
        try:
            end = _day(first['DT_FIM_EXERC']).isoformat()
            if end != filing['fiscal_end']:
                raise ValueError('Account end differs from filing fiscal date.')
            if flow:
                start = _day(first['DT_INI_EXERC']).isoformat()
                if start > end:
                    raise ValueError('Invalid flow interval.')
                days = (_day(end) - _day(start)).days + 1
                kind = ('annual' if filing['document_type'] == 'DFP' and 330 <= days <= 380 else
                        'ytd' if filing['document_type'] == 'ITR' and start[5:] == '01-01' else
                        'quarter' if filing['document_type'] == 'ITR' and 70 <= days <= 100 else 'reported_flow')
            else:
                kind = 'instant'
        except (KeyError, TypeError, ValueError):
            value, status, reason = None, 'invalid', 'missing_or_invalid_account_period'
    return {'value': value, 'unit': FIELD_UNITS[name], 'period_start': start, 'period_end': end,
            'period_type': kind, 'annualized': False, 'method': method, 'status': status,
            'reason': None if status == 'available' else reason,
            'source_ref': filing['source_ref'] if filing else None,
            'filing_id': filing['filing_id'] if filing else None,
            'published_at': filing['published_at'] if filing else None,
            'document_type': filing['document_type'] if filing else None,
            'version': filing['version'] if filing else None, 'scope': scope,
            'accounts': [{'statement': r['_statement'], 'code': r['CD_CONTA'],
                          'description': r['DS_CONTA'], 'reported_scale': r.get('ESCALA_MOEDA')}
                         for r in rows]}


def _value(row, *, eps=False):
    if row.get('MOEDA') != 'REAL':
        raise ValueError('unsupported_currency')
    if not eps and row.get('ESCALA_MOEDA') not in ('MIL', 'UNIDADE'):
        raise ValueError('unsupported_currency_scale')
    try:
        value = float(row['VL_CONTA'])
        if not eps and row['ESCALA_MOEDA'] == 'MIL':
            value *= 1000
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid_numeric_value') from None
    if not math.isfinite(value):
        raise ValueError('invalid_numeric_value')
    return value


def _period(rows, filing, flow):
    if not rows:
        return []
    matching = [r for r in rows if r.get('DT_FIM_EXERC') == filing['fiscal_end']]
    if not matching:
        return rows  # Retain invalid evidence; _field rejects mismatched periods.
    if flow:
        starts = []
        for row in matching:
            try:
                start = _day(row.get('DT_INI_EXERC')).isoformat()
                if start <= filing['fiscal_end']:
                    starts.append(start)
            except (TypeError, ValueError):
                pass
        if starts:
            matching = [r for r in matching if r.get('DT_INI_EXERC') == min(starts)]
    return matching


def _reported(name, rows, filing, scope, predicate, method, *, flow=False, nonnegative=False):
    selected = _period([r for r in rows if predicate(r)], filing, flow)
    if not selected:
        return _field(name, filing, scope, method=method)
    field = _field(name, filing, scope, rows=selected, method=method, flow=flow)
    if field['status'] == 'invalid':
        return field
    if len(selected) != 1:
        return {**field, 'status': 'ambiguous', 'reason': 'multiple_matching_accounts'}
    try:
        value = _value(selected[0], eps=name == 'basic_eps')
        if nonnegative and value < 0:
            raise ValueError('negative_balance')
    except ValueError as error:
        return {**field, 'status': 'invalid', 'reason': str(error)}
    return {**field, 'value': value, 'status': 'available', 'reason': None}


def _capex(rows, filing, scope):
    selected = _period([r for r in rows if r['CD_CONTA'].startswith('6.02.')
                       and any(word in _normalize(r['DS_CONTA']) for word in ('imobiliz', 'intangiv'))
                       and any(word in _normalize(r['DS_CONTA']) for word in ('aquis', 'adico', 'compra', 'investimento'))
                       and not any(word in _normalize(r['DS_CONTA']) for word in ('alien', 'venda', 'resgate'))], filing, True)
    if not selected:
        return _field('capex', filing, scope, method='identified_nonpositive_ppe_intangible_acquisitions')
    leaves = [r for r in selected if not any(other['CD_CONTA'].startswith(r['CD_CONTA'] + '.') for other in selected)]
    field = _field('capex', filing, scope, rows=leaves, flow=True,
                   method='sum_identified_nonpositive_ppe_intangible_acquisition_leaves')
    if field['status'] == 'invalid':
        return field
    if len({r['CD_CONTA'] for r in leaves}) != len(leaves):
        return {**field, 'status': 'ambiguous', 'reason': 'duplicate_capex_accounts'}
    try:
        values = [_value(row) for row in leaves]
        if any(value > 0 for value in values):
            raise ValueError('capex_is_not_a_nonpositive_outflow')
        total = sum(values)
        if not math.isfinite(total):
            raise ValueError('invalid_numeric_value')
    except ValueError as error:
        return {**field, 'status': 'invalid', 'reason': str(error)}
    return {**field, 'value': total, 'status': 'available', 'reason': None}


def _derived(name, inputs, filing, scope, method, calculate, *, same_statement=False):
    field = _field(name, filing, scope, method=method)
    if any(item['status'] != 'available' for item in inputs):
        return {**field, 'reason': 'required_fields_unavailable'}
    periods = {(item['period_start'], item['period_end']) for item in inputs}
    statements = {account['statement'] for item in inputs for account in item['accounts']}
    if len(periods) != 1 or same_statement and len(statements) != 1:
        return {**field, 'reason': 'incompatible_periods_or_statements'}
    try:
        value = calculate(*[item['value'] for item in inputs])
        if not math.isfinite(value) or name in ('total_liabilities', 'liabilities_to_assets') and value < 0:
            raise ValueError('invalid_derived_value')
    except (ValueError, ZeroDivisionError, OverflowError):
        return {**field, 'status': 'invalid', 'reason': 'invalid_derived_value'}
    return {**field, 'value': value, 'status': 'available', 'reason': None,
            'period_start': inputs[0]['period_start'], 'period_end': inputs[0]['period_end'],
            'period_type': inputs[0]['period_type'],
            'accounts': [account for item in inputs for account in item['accounts']]}


def _fields(filing, scope, tables, ticker, share_class, sector):
    def pick(name, table, predicate, method, **options):
        return _reported(name, tables.get(table, []), filing, scope, predicate, method, **options)

    fields = {}
    if share_class not in SHARE_CLASSES or ticker.endswith('11'):
        fields['basic_eps'] = _field('basic_eps', filing, scope, status='not_applicable',
                                     reason='unit_composition_or_exact_share_class_not_supported',
                                     method='no_generic_or_unit_eps_inference')
    else:
        fields['basic_eps'] = pick('basic_eps', 'DRE',
                                   lambda r: r['CD_CONTA'].startswith('3.99.01.') and r['DS_CONTA'].strip().upper() == share_class,
                                   'reported_basic_eps_exact_share_class_unscaled', flow=True)
    fields['total_assets'] = pick('total_assets', 'BPA',
                                 lambda r: r['CD_CONTA'] == '1' and _normalize(r['DS_CONTA']) == 'ativo total',
                                 'reported_total_assets', nonnegative=True)
    fields['total_passivo'] = pick('total_passivo', 'BPP',
                                  lambda r: r['CD_CONTA'] == '2' and _normalize(r['DS_CONTA']) == 'passivo total',
                                  'reported_total_passivo_including_equity', nonnegative=True)
    equity_names = {'patrimonio liquido consolidado'} if scope == 'consolidated' else {'patrimonio liquido', 'patrimonio liquido individual'}
    fields['equity'] = pick('equity', 'BPP',
                           lambda r: r['CD_CONTA'].startswith('2.') and r['CD_CONTA'].count('.') == 1 and _normalize(r['DS_CONTA']) in equity_names,
                           'reported_root_equity_exact_description')
    fields['total_liabilities'] = _derived('total_liabilities', [fields['total_passivo'], fields['equity']],
                                          filing, scope, 'total_passivo_less_root_equity', lambda total, equity: total - equity)
    if sector != 2 and fields['total_liabilities']['status'] == 'missing':
        current = pick('total_passivo', 'BPP', lambda r: r['CD_CONTA'] == '2.01' and _normalize(r['DS_CONTA']) == 'passivo circulante', 'reported_current_liabilities')
        noncurrent = pick('total_passivo', 'BPP', lambda r: r['CD_CONTA'] == '2.02' and _normalize(r['DS_CONTA']) == 'passivo nao circulante', 'reported_noncurrent_liabilities')
        fields['total_liabilities'] = _derived('total_liabilities', [current, noncurrent], filing, scope,
                                              'explicit_industrial_current_plus_noncurrent_liabilities', lambda a, b: a + b)
    fields['liabilities_to_assets'] = _derived('liabilities_to_assets', [fields['total_liabilities'], fields['total_assets']],
                                              filing, scope, 'same_filing_liabilities_divided_by_assets', lambda debt, assets: debt / assets)
    fields['revenue'] = pick('revenue', 'DRE', lambda r: r['CD_CONTA'] == '3.01' and _normalize(r['DS_CONTA']) in REVENUE_NAMES,
                             'reported_revenue_exact_description', flow=True)
    fields['attributable_profit'] = pick('attributable_profit', 'DRE', lambda r: r['CD_CONTA'].startswith('3.') and _normalize(r['DS_CONTA']) in PROFIT_NAMES,
                                         'reported_profit_attributable_to_parent', flow=True)
    net_names = {'lucro/prejuizo consolidado do periodo'} if scope == 'consolidated' else {'lucro/prejuizo do periodo'}
    fields['net_income'] = pick('net_income', 'DRE', lambda r: r['CD_CONTA'].startswith('3.') and _normalize(r['DS_CONTA']) in net_names,
                                'reported_net_income_exact_description', flow=True)
    for table in ('DFC_MI', 'DFC_MD'):
        cfo = pick('operating_cash_flow', table,
                   lambda r: r['CD_CONTA'] == '6.01' and _normalize(r['DS_CONTA']) in (
                       'caixa liquido atividades operacionais', 'caixa liquido das atividades operacionais'),
                   'reported_operating_cash_flow_' + table.lower(), flow=True)
        capex = _capex(tables.get(table, []), filing, scope)
        for name, field in (('operating_cash_flow', cfo), ('capex', capex)):
            if name not in fields or fields[name]['status'] == 'missing':
                fields[name] = field
    fields['free_cash_flow'] = (_field('free_cash_flow', filing, scope, status='not_applicable',
                                      reason='industrial_fcf_not_applicable_to_financial_sector', method='no_financial_sector_industrial_fcf')
                                if sector == 2 else
                                _derived('free_cash_flow', [fields['operating_cash_flow'], fields['capex']], filing, scope,
                                         'operating_cash_plus_identified_signed_capex', lambda cfo, capex: cfo + capex, same_statement=True))
    return fields


def parse_archives(archives, specs, as_of, *, source_metadata=None):
    """Parse official ZIP bytes without I/O; all output values are JSON-safe.

    An available field can refer to an earlier fiscal filing, marked by
    is_latest_filing=False. Its own filing/period/scope always travels with it.
    A newer invalid/ambiguous field is not replaced by an older valid value.
    """
    as_of, specs = _day(as_of), _specs(specs)
    requested = {spec[0] for spec in specs.values() if spec[0] is not None}
    filings, account_rows, sources = {}, defaultdict(lambda: defaultdict(list)), []
    for name, content in archives.items():
        source = _source(name, content, (source_metadata or {}).get(name))
        sources.append(source)
        prefix = source['document_type'].lower() + '_cia_aberta'
        year = source['year']
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                def rows(member):
                    if member not in archive.namelist():
                        return
                    with archive.open(member) as stream:
                        yield from csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';')

                local = {}
                for row in rows(f'{prefix}_{year}.csv'):
                    try:
                        cvm, version = int(row['CD_CVM']), int(row['VERSAO'])
                        fiscal, published = _day(row['DT_REFER']), _day(row['DT_RECEB'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if cvm not in requested or not fiscal <= published <= as_of or version < 1:
                        continue
                    key = (cvm, source['document_type'], fiscal.isoformat(), version)
                    local[key] = {'cvm': cvm, 'document_type': source['document_type'],
                                  'fiscal_end': fiscal.isoformat(), 'version': version,
                                  'published_at': published.isoformat(), 'filing_id': row['ID_DOC'],
                                  'source_ref': source['id']}
                local_rows = defaultdict(lambda: defaultdict(list))
                for scope, suffix in (('consolidated', 'con'), ('individual', 'ind')):
                    for table in ('BPA', 'BPP', 'DRE', 'DFC_MI', 'DFC_MD'):
                        member = f'{prefix}_{table}_{suffix}_{year}.csv'
                        for row in rows(member):
                            try:
                                key = (int(row['CD_CVM']), source['document_type'], row['DT_REFER'], int(row['VERSAO']))
                            except (KeyError, TypeError, ValueError):
                                continue
                            if key in local and row.get('ORDEM_EXERC') == '\u00daLTIMO':
                                local_rows[(key, scope)][table].append({**row, '_statement': table})
                filings.update(local)
                account_rows.update(local_rows)
        except (zipfile.BadZipFile, KeyError, UnicodeError, csv.Error, OSError):
            source.update(status='invalid', reason='invalid_official_archive')
    latest_versions = {}
    for key in filings:
        identity = key[:3]
        if identity not in latest_versions or key[3] > latest_versions[identity][3]:
            latest_versions[identity] = key
    instruments = {}
    for ticker, (cvm, share_class, sector) in specs.items():
        keys = sorted((key for key in latest_versions.values() if key[0] == cvm),
                      key=lambda key: (key[2], filings[key]['published_at'], key[3]), reverse=True)
        scope = next((scope for scope in ('consolidated', 'individual')
                      if any(account_rows.get((key, scope)) for key in keys)), None)
        latest = filings[keys[0]] if keys else None
        observations = [(filings[key], _fields(filings[key], scope, account_rows.get((key, scope), {}), ticker, share_class, sector))
                        for key in keys] if scope else []
        fields = {}
        for name in FIELD_UNITS:
            candidates = [values[name] for _, values in observations if values[name]['status'] != 'missing']
            field = candidates[0] if candidates else (observations[0][1][name] if observations else
                                                      _field(name, latest, scope, reason='unresolved_cvm' if cvm is None else 'no_eligible_accounts'))
            if not observations and name == 'free_cash_flow' and sector == 2:
                field.update(status='not_applicable', reason='industrial_fcf_not_applicable_to_financial_sector',
                             method='no_financial_sector_industrial_fcf')
            if not observations and name == 'basic_eps' and (share_class not in SHARE_CLASSES or ticker.endswith('11')):
                field.update(status='not_applicable', reason='unit_composition_or_exact_share_class_not_supported',
                             method='no_generic_or_unit_eps_inference')
            field['is_latest_filing'] = bool(latest and field['filing_id'] == latest['filing_id']
                                            and field['source_ref'] == latest['source_ref'])
            fields[name] = field
        statuses = [field['status'] for field in fields.values()]
        instruments[ticker] = {'ticker': ticker, 'cvm': cvm, 'share_class': share_class, 'sector_code': sector,
                               'identity_status': 'resolved' if cvm is not None else 'unresolved_cvm',
                               'as_of': as_of.isoformat(), 'scope': scope,
                               'scope_selection': 'consolidated_preferred' if scope == 'consolidated' else
                                   'individual_only_no_consolidated_accounts' if scope else 'no_eligible_accounts',
                               'filing': latest, 'fields': fields,
                               'status': ('available' if all(s in ('available', 'not_applicable') for s in statuses) else
                                          'partial' if 'available' in statuses else 'unavailable')}
    return {'schema_version': 1, 'as_of': as_of.isoformat(), 'instruments': instruments, 'sources': sources,
            'limitations': ['Official archives are revised weekly, not complete point-in-time archives.',
                            'Flow periods are reported, never annualized or converted into TTM/P-E.',
                            'Identified capex is a strict proxy, not certified completeness of all capital spending.']}


def _download(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura academic official financials collector', 'Accept': 'application/zip'})
    with urllib.request.urlopen(request, timeout=90) as response:
        content = response.read()
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError('Official response is not a ZIP archive.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return content


def collect_fundamentals(folder, specs, as_of, refresh=False):
    """Read/cache previous-year DFP and current-year ITR in raw/official-financials.

    For an as_of in 2026 these are DFP 2025 and ITR 2026. Download failures are
    explicit source diagnostics and do not discard fields from the other ZIP.
    No snapshot, active pointer, or external collector file is written.
    """
    as_of = _day(as_of)
    if as_of > date.today():
        raise ValueError('as_of cannot be in the future.')
    specs = _specs(specs)
    if not any(spec[0] is not None for spec in specs.values()):
        return parse_archives({}, specs, as_of)
    cache = Path(folder) / 'raw' / 'official-financials'
    cache.mkdir(parents=True, exist_ok=True)
    archives, metadata, errors = {}, {}, []
    for form, year in (('DFP', as_of.year - 1), ('ITR', as_of.year)):
        name = f'{form.lower()}_cia_aberta_{year}.zip'
        path = cache / name
        url = BASE_URL.format(form=form, name=name)
        try:
            content = _download(url, path) if refresh or not path.is_file() else path.read_bytes()
            archives[name] = content
            metadata[name] = {'retrieved_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
        except (OSError, ValueError, urllib.error.URLError) as error:
            errors.append({'id': f'cvm:{form}:{year}:unavailable', 'url': url,
                           'file': 'official-financials/' + name, 'sha256': None, 'bytes': None,
                           'retrieved_at': None, 'status': 'unavailable', 'document_type': form, 'year': year,
                           'reason': 'official_source_unavailable', 'error_type': type(error).__name__})
    result = parse_archives(archives, specs, as_of, source_metadata=metadata)
    result['sources'].extend(errors)
    return result
