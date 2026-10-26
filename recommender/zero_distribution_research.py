"""Isolated, fail-closed research of issuer-reported zero distributions.

The result contains aggregate reporting periods, never invented cash events.
It is not imported by the preparation/forecast pipeline and approves no asset.
All acquisition is opt-in, bounded and content addressed; no exchange requests.
"""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import zipfile

from lxml import html

from .issuer_registration import load_registration, registration_status
from .market_inventory import load_inventory, market_root
from .payment_sources import _table_rows


CACHE = 'raw/primary-research/zero-distributions'
TICKERS = ('AALR3', 'AERI3', 'AMAR3', 'AMER3', 'AMBP3', 'BIOM3', 'BLAU3', 'ARML3', 'LWSA3')
WINDOW_START, WINDOW_END = date(2021, 10, 1), date(2026, 9, 30)
MAX_BYTES, MAX_REQUESTS = 20_000_000, 16
SOURCE_SPECS = {
    'aalr-history': ('AALR3', 'https://ri.allianca.com/informacoes-financeiras/dados-historicos/'),
    'aeri-results': ('AERI3', 'https://ri.aerisenergy.com.br/pt/content/market-information/results-center'),
    'amar-history': ('AMAR3', 'https://ri.marisa.com.br/informacoes-financeiras/dividendos/'),
    'amer-faq': ('AMER3', 'https://ri.americanas.io/servicos-aos-investidores/perguntas-frequentes/'),
    'ambp-history': ('AMBP3', 'https://ri.ambipar.com/remuneracao-aos-acionistas/'),
    'biom-results': ('BIOM3', 'https://ri.biomm.com/informacoes-financeiras/central-de-resultados/'),
    'blau-history': ('BLAU3', 'https://ri.blau.com/informacoes-financeiras/dividendos-e-jcp/'),
    'arml-history': ('ARML3', 'https://ri.armac.com.br/informacoes-financeiras/dividendos-e-jcp-2/'),
    'lwsa-home': ('LWSA3', 'https://ri.lwsa.tech/'),
    'aeri-2q26': ('AERI3', 'https://d169uzu5o4xu1k.cloudfront.net/7d17322d-a2f2-4c4a-891e-8453846b7acc/2026/a46bb76e-c0f4-4188-a9bc-71069b3e521e.pdf'),
    'aeri-payment-2022': ('AERI3', 'https://api.mziq.com/mzfilemanager/v2/d/4270cd01-7524-4011-ae86-d125baa81b06/484376e8-8d92-6ee0-ee3c-eb732b1b573b?origin=1'),
    'biom-proposal-2025': ('BIOM3', 'https://api.mziq.com/mzfilemanager/v2/d/49baf2d5-181e-4692-bf91-ffeba3c6d725/26aabf68-f992-d9f2-17ec-8a0dd18831ed?origin=1'),
}
CSV_KEYS = ('CNPJ_CIA', 'DT_REFER', 'VERSAO', 'CD_CVM', 'MOEDA', 'ESCALA_MOEDA',
            'ORDEM_EXERC', 'DT_INI_EXERC', 'DT_FIM_EXERC', 'CD_CONTA', 'DS_CONTA', 'VL_CONTA')
POLICY = 'RF-ZERO-AGGREGATE-RESEARCH-1'


def _norm(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value).lower())
                           if not unicodedata.combining(c)).split())


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode('utf-8')


def _day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('invalid_ISO_date')
    return parsed


def _immutable(path, body):
    if path.is_symlink():
        raise ValueError('immutable_symlink_rejected')
    if path.exists():
        if path.read_bytes() != body:
            raise ValueError('immutable_cache_conflict')
    else:
        with path.open('xb') as stream:
            stream.write(body)


def _target(folder):
    root = Path(folder).resolve()
    target = root / CACHE
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('unsafe_research_directory')
    return target


def _local_source(folder, relative, url, *, authority='primary_CVM'):
    root = Path(folder).resolve()
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe_source_path')
    body = path.read_bytes()
    return {'file': relative, 'sha256': _digest(body), 'bytes': len(body), 'url': url,
            'authority': authority,
            'cache_file_modified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            'cache_mtime_is_not_original_acquisition_date': True,
            'settlement_verified': False, 'point_in_time_archive_verified': False}


def identities(folder, observed_on):
    """Resolve CVM/CNPJ/FCA class before looking at any financial statement."""
    observed_on = _day(observed_on) if isinstance(observed_on, str) else observed_on
    inventory = load_inventory(folder)
    records, receipt = load_registration(folder)
    observed_cache = datetime.fromisoformat(receipt['retrieved_at']).date()
    if observed_on > observed_cache:
        raise ValueError('registration_observation_older_than_research')
    source = _local_source(folder, 'raw/inventory/fca_cia_aberta_2026.zip',
                          'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_2026.zip')
    cad_reader = csv.DictReader(io.StringIO((Path(folder) / receipt['file']).read_bytes().decode('latin1')), delimiter=';')
    cad = {int(row['CD_CVM']): row for row in cad_reader}
    with zipfile.ZipFile(Path(folder) / source['file']) as archive:
        meta = list(csv.DictReader(io.TextIOWrapper(archive.open('fca_cia_aberta_2026.csv'), encoding='latin1'), delimiter=';'))
        securities = list(csv.DictReader(io.TextIOWrapper(archive.open('fca_cia_aberta_valor_mobiliario_2026.csv'), encoding='latin1'), delimiter=';'))
    result = []
    for ticker in TICKERS:
        stock = next(row for row in inventory['stocks'] if row['ticker'] == ticker)
        issuer = cad[stock['cvm']]
        eligible = [row for row in meta if int(row['CD_CVM']) == stock['cvm'] and _day(row['DT_RECEB']) <= observed_on]
        latest = max(eligible, key=lambda row: (row['DT_REFER'], int(row['VERSAO']), row['DT_RECEB']))
        matches = [row for row in securities if row['ID_Documento'] == latest['ID_DOC']
                   and row['Codigo_Negociacao'] == ticker]
        if latest['CNPJ_CIA'] != issuer['CNPJ_CIA'] or stock['share_class'] != 'ON':
            raise ValueError('issuer_security_identity_mismatch:' + ticker)
        security = matches[0] if len(matches) == 1 else None
        security_verified = (security is not None and security['CNPJ_Companhia'] == issuer['CNPJ_CIA']
                             and _norm(security['Valor_Mobiliario']) == 'acoes ordinarias')
        # A malformed ticker in a current FCA is a recorded gap, not a guessed repair.
        issuer_securities = [row for row in securities if row['ID_Documento'] == latest['ID_DOC']]
        existing = stock.get('identity_file', '')
        if not re.fullmatch(r'raw/expanded/b3-detail-\d+\.json', existing):
            raise ValueError('unexpected_existing_identity_cache')
        cached_identity = _local_source(folder, existing, 'https://sistemaswebb3-listados.b3.com.br/',
                                        authority='primary_existing_B3_cache_no_network')
        cached_identity['url_is_context_only_original_request_URL_not_recorded_here'] = True
        cache_row = json.loads((Path(folder) / existing).read_bytes())
        digits = lambda value: re.sub(r'\D', '', value)
        if (int(cache_row['codeCVM']) != stock['cvm']
                or digits(cache_row['cnpj']) != digits(issuer['CNPJ_CIA'])
                or not any(row['code'] == ticker and row['isin'] == stock['isin'] for row in cache_row['otherCodes'])):
            raise ValueError('existing_security_identity_conflict:' + ticker)
        status = registration_status(records, stock['cvm'], observed_on, observed_on=observed_cache)
        result.append({'ticker': ticker, 'cvm': stock['cvm'], 'cnpj': issuer['CNPJ_CIA'],
            'name': issuer['DENOM_SOCIAL'], 'share_class': 'ON',
            'isin_from_existing_catalog_only': stock.get('isin'),
            'isin_not_in_FCA_security_table': True, 'fca_security': security,
            'fca_issuer_security_rows': issuer_securities,
            'security_identity_status': 'exact_FCA_ticker_and_existing_B3_cache' if security_verified
                else 'current_FCA_ticker_unresolved_existing_B3_mapping_only',
            'fca_filing_id': latest['ID_DOC'], 'fca_published_at': latest['DT_RECEB'],
            'registration': status,
            'issuer_identity_verified': status['status'] == 'active_issuer_registration_not_trading_confirmation',
            'identity_verified': security_verified and status['status'] == 'active_issuer_registration_not_trading_confirmation',
            'exchange_trading_verified': False,
            'sources': [receipt, source, cached_identity]})
    return result


def cash_component(code, description):
    """Accept explicit financing cash-out labels, not income or distributions due."""
    label = _norm(description)
    if not re.fullmatch(r'6\.03(?:\.\d+)+', code):
        return None
    if any(word in label for word in ('recebid', 'recebimento', 'nao controlador',
            'minoritari', 'a pagar', 'propost', 'declarad', 'provision', 'revers', 'compens', 'capitalizacao')):
        return None
    label = re.sub(r'[^a-z0-9 ]', ' ', label)
    label = ' '.join(label.split())
    dividends = re.fullmatch(r'(?:(?:pagamento|pagamentos|distribuicao) de )?dividendos(?: pagos)?', label)
    interest = re.fullmatch(r'(?:pagamento de )?(?:juros sobre (?:o )?capital proprio|jcp|jscp)(?: pagos)?', label)
    both = re.fullmatch(r'(?:(?:pagamento|pagamentos|distribuicao) de )?dividendos e (?:juros sobre (?:o )?capital(?: proprio)?|jcp|jscp)(?: pagos)?', label)
    return 'both' if both else 'dividends' if dividends else 'jcp' if interest else None


def evaluate_period(rows):
    """Zero requires both explicit cash components. Never net opposing entries."""
    recognized, ignored, unresolved_cash_labels = [], [], []
    for row in rows:
        component = cash_component(row['CD_CONTA'], row['DS_CONTA'])
        if component is None:
            label = _norm(row['DS_CONTA'])
            if any(word in label for word in ('dividend', 'capital proprio', 'jcp', 'jscp')):
                ignored.append(row)
                if (row['CD_CONTA'].startswith('6.03.') or 'pag' in label or 'distribui' in label) and not any(
                        word in label for word in ('recebid', 'recebimento', 'nao controlador', 'minoritari')):
                    unresolved_cash_labels.append(row)
            continue
        try:
            value = Decimal(row['VL_CONTA'])
            if not value.is_finite() or row['MOEDA'] != 'REAL' or row['ESCALA_MOEDA'] not in ('MIL', 'UNIDADE'):
                raise ValueError('unsupported_cash_value_or_units')
        except (InvalidOperation, ValueError):
            return {'status': 'pending', 'reason': 'invalid_explicit_cash_value_or_units',
                    'rows': recognized + [row], 'ignored_rows': ignored}
        recognized.append({**row, 'cash_component': component,
                           'reported_value_brl': str(value * (1000 if row['ESCALA_MOEDA'] == 'MIL' else 1)),
                           'reporting_unit_brl': 1000 if row['ESCALA_MOEDA'] == 'MIL' else 1})
    codes = [row['CD_CONTA'] for row in recognized]
    if len(codes) != len(set(codes)):
        status, reason = 'pending', 'duplicate_distribution_account'
    elif any(Decimal(row['reported_value_brl']) > 0 for row in recognized):
        status, reason = 'pending', 'positive_sign_cash_out_requires_documentary_review'
    elif any(Decimal(row['reported_value_brl']) < 0 for row in recognized):
        status, reason = 'known_positive_reported_outflow', 'explicit_distribution_cash_outflow_not_individual_settlement'
    elif unresolved_cash_labels:
        status, reason = 'pending', 'unresolved_additional_distribution_cashflow_label'
    elif any(row['cash_component'] == 'both' for row in recognized) or {'dividends', 'jcp'}.issubset({row['cash_component'] for row in recognized}):
        status, reason = 'reported_zero_both_components', 'explicit_zero_dividends_and_JCP_at_reported_precision'
    elif recognized:
        status, reason = 'pending', 'only_one_distribution_component_explicitly_reported'
    else:
        status, reason = 'pending', 'no_explicit_distributions_paid_accounts_not_zero'
    return {'status': status, 'reason': reason, 'rows': recognized, 'ignored_rows': ignored,
            'individual_events_generated': 0, 'settlement_verified': False,
            'exact_unrounded_cash_zero_verified': False}


def _archive_periods(folder, relative, kind, year, identity_rows, observed_on, window_start, window_end):
    """Retain latest filing version and its reported comparative, including gaps."""
    source = _local_source(folder, relative,
        f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/{kind}/DADOS/{kind.lower()}_cia_aberta_{year}.zip')
    identities_by_cvm = {row['cvm']: row for row in identity_rows}
    diagnostics, periods = [], []
    with zipfile.ZipFile(Path(folder) / relative) as archive:
        prefix = f'{kind.lower()}_cia_aberta_'
        metadata = defaultdict(list)
        for row in csv.DictReader(io.TextIOWrapper(archive.open(f'{prefix}{year}.csv'), encoding='latin1'), delimiter=';'):
            cvm = int(row['CD_CVM'])
            if cvm in identities_by_cvm and _day(row['DT_RECEB']) <= observed_on:
                if row['CNPJ_CIA'] != identities_by_cvm[cvm]['cnpj']:
                    raise ValueError('financial_issuer_CNPJ_mismatch')
                metadata[cvm, row['DT_REFER']].append(row)
        latest = {key: max(values, key=lambda r: (int(r['VERSAO']), r['DT_RECEB'])) for key, values in metadata.items()}
        grouped = defaultdict(list)
        for member in archive.namelist():
            if not re.fullmatch(prefix + r'DFC_(?:MI|MD)_(?:ind|con)_' + str(year) + r'\.csv', member):
                continue
            scope = 'individual' if '_ind_' in member else 'consolidated'
            reader = csv.DictReader(io.TextIOWrapper(archive.open(member), encoding='latin1'), delimiter=';')
            for number, row in enumerate(reader, 2):
                cvm = int(row['CD_CVM'])
                filing = latest.get((cvm, row['DT_REFER']))
                if filing is None or int(row['VERSAO']) != int(filing['VERSAO']):
                    continue
                start, end = _day(row['DT_INI_EXERC']), _day(row['DT_FIM_EXERC'])
                if start > end or end > _day(row['DT_REFER']):
                    raise ValueError('invalid_reported_cash_period')
                if start > window_end or end < window_start:
                    continue
                if row['CNPJ_CIA'] != identities_by_cvm[cvm]['cnpj']:
                    raise ValueError('financial_row_identity_mismatch')
                cells = {key: row[key] for key in CSV_KEYS}
                evidence = {**cells, 'source_file': relative, 'archive_sha256': source['sha256'],
                    'member': member, 'csv_record_number_including_header': number,
                    'csv_physical_line_end': reader.line_num, 'cells_sha256': _digest(_json(cells))}
                grouped[cvm, row['DT_REFER'], start, end, scope, member].append(evidence)
        for (cvm, ref), filing in latest.items():
            if _day(ref) < window_start or _day(ref) > window_end:
                continue
            matches = [key for key in grouped if key[0] == cvm and key[1] == ref]
            if not any(key[4] == 'individual' for key in matches):
                diagnostics.append({'ticker': identities_by_cvm[cvm]['ticker'], 'reference_date': ref,
                    'filing_id': filing['ID_DOC'], 'version': int(filing['VERSAO']),
                    'reason': 'latest_filing_has_no_individual_DFC_rows_no_older_fallback',
                    'source_file': relative, 'archive_sha256': source['sha256']})
                # A newer consolidated context with a missing individual table
                # cannot silently leave an older individual zero in force.
                for start, end in {(key[2], key[3]) for key in matches}:
                    periods.append({'ticker': identities_by_cvm[cvm]['ticker'], 'cvm': cvm,
                        'period_start': start.isoformat(), 'period_end': end.isoformat(),
                        'document_reference': ref, 'document_type': kind, 'scope': 'individual',
                        'table': f'{prefix}DFC_MI_ind_{year}.csv', 'published_at': filing['DT_RECEB'],
                        'filing_id': filing['ID_DOC'], 'version': int(filing['VERSAO']),
                        'source': source, 'status': 'pending', 'rows': [], 'ignored_rows': [],
                        'reason': 'latest_individual_DFC_missing_no_older_fallback',
                        'individual_events_generated': 0, 'settlement_verified': False,
                        'exact_unrounded_cash_zero_verified': False})
        for key, rows in grouped.items():
            cvm, ref, start, end, scope, member = key
            filing = latest[cvm, ref]
            periods.append({'ticker': identities_by_cvm[cvm]['ticker'], 'cvm': cvm,
                'period_start': start.isoformat(), 'period_end': end.isoformat(),
                'document_reference': ref, 'document_type': kind,
                'scope': scope, 'table': member, 'published_at': filing['DT_RECEB'],
                'filing_id': filing['ID_DOC'], 'version': int(filing['VERSAO']),
                'reported_exercise': sorted({row['ORDEM_EXERC'] for row in rows}),
                'source': source, **evaluate_period(rows)})
    return periods, diagnostics, source


def summarize_periods(periods, window_start=WINDOW_START, window_end=WINDOW_END):
    """Count supported full calendar months without turning aggregates into events."""
    if not isinstance(window_start, date) or not isinstance(window_end, date) or window_start > window_end:
        raise ValueError('invalid_research_window')
    individual = [row for row in periods if row['scope'] == 'individual']
    by_period = defaultdict(list)
    for row in individual:
        by_period[row['period_start'], row['period_end']].append(row)
    latest = []
    for values in by_period.values():
        rank = max((row['published_at'], row['document_reference'], row['version']) for row in values)
        selected = [row for row in values if (row['published_at'], row['document_reference'], row['version']) == rank]
        statuses = {row['status'] for row in selected}
        if len(statuses) != 1 or len({row['table'].split('_DFC_')[1].split('_')[0] for row in selected}) > 1:
            latest.append({**selected[0], 'status': 'pending', 'reason': 'ambiguous_latest_cashflow_statement'})
        else:
            latest.append(selected[0])
    zeros = [row for row in latest if row['status'] == 'reported_zero_both_components']
    positives = [row for row in latest if row['status'] == 'known_positive_reported_outflow']

    def fully_covered_by_zeros(positive):
        needed, end = _day(positive['period_start']), _day(positive['period_end'])
        for first, last in sorted((_day(row['period_start']), _day(row['period_end'])) for row in zeros):
            if last < needed:
                continue
            if first > needed:
                return False
            needed = last + timedelta(days=1)
            if needed > end:
                return True
        return False

    contradictory = [row for row in positives if fully_covered_by_zeros(row)]
    months, cursor = [], window_start.replace(day=1)
    while cursor <= window_end:
        following = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = following - timedelta(days=1)
        supporting = [row for row in zeros if _day(row['period_start']) <= cursor and _day(row['period_end']) >= end]
        conflicts = [row for row in contradictory if any(_day(row['period_start']) <= _day(zero['period_end'])
                     and _day(row['period_end']) >= _day(zero['period_start']) for zero in supporting)]
        full = cursor >= window_start and end <= window_end
        status = ('reported_zero_period_support' if supporting and not conflicts and full else 'pending')
        months.append({'month': cursor.strftime('%Y-%m'), 'status': status,
            'supporting_filing_ids': sorted({row['filing_id'] for row in supporting}),
            'conflicting_filing_ids': sorted({row['filing_id'] for row in conflicts}),
            'value_brl': None, 'individual_payment_events_inferred': False})
        cursor = following
    positive_in_window = [row for row in positives if _day(row['period_start']) >= window_start
                          and _day(row['period_end']) <= window_end]
    return {'status': 'known_positive' if positive_in_window else 'pending',
        'selected_individual_periods': latest,
        'reported_zero_both_period_count': len(zeros), 'known_positive_period_count': len(positive_in_window),
        'zero_supported_full_month_count': sum(row['status'] == 'reported_zero_period_support' for row in months),
        'months': months, 'unresolved_months': [row['month'] for row in months if row['status'] == 'pending'],
        'all_requested_months_reported_zero': all(row['status'] == 'reported_zero_period_support' for row in months),
        'settlement_verified': False, 'point_in_time_archive_verified': False,
        'individual_events_generated': 0, 'model_approval_performed': False,
        'limitation': 'Reported aggregate zero at statement precision, not exact unrounded cash or individual settlement. Positive aggregates do not locate a payment month.'}


def _approved_url(url):
    parsed = urlsplit(url)
    return (url in {value[1] for value in SOURCE_SPECS.values()}
            and parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and parsed.hostname != 'b3.com.br'
            and not parsed.hostname.endswith('.b3.com.br'))


class _NoUnapprovedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        if not _approved_url(newurl):
            raise ValueError('unapproved_redirect_no_request_sent')
        return super().redirect_request(request, response, code, message, headers, newurl)


def acquire_sources(folder, source_ids=None):
    """At most 16 single-attempt exact public sources; persist failures, no bypass."""
    target = _target(folder)
    selected = list(SOURCE_SPECS) if source_ids is None else list(source_ids)
    if len(selected) > MAX_REQUESTS or len(set(selected)) != len(selected):
        raise ValueError('research_request_budget_exceeded_or_duplicate')
    if not set(selected).issubset(SOURCE_SPECS):
        raise ValueError('unknown_primary_source_no_request_sent')
    receipts, blocked_hosts = [], set()
    for path in target.glob('*-receipt.json'):
        previous = _load_receipt(path)
        if previous.get('http_status') in (401, 403, 429):
            blocked_hosts.add(urlsplit(previous['url']).hostname)
    for name in selected:
        ticker, url = SOURCE_SPECS[name]
        if not _approved_url(url):
            raise ValueError('unapproved_primary_source')
        cached = sorted(target.glob(name + '-*-receipt.json'))
        if cached:
            receipt = _load_receipt(cached[-1])
            if receipt['url'] != url:
                raise ValueError('cached_source_URL_mismatch')
            if receipt['status'] == 'acquired':
                read_acquired_source(folder, receipt)
            if receipt.get('http_status') in (401, 403, 429):
                blocked_hosts.add(urlsplit(url).hostname)
            receipts.append(receipt)
            continue
        host = urlsplit(url).hostname
        receipt = {'source_id': name, 'ticker': ticker, 'url': url,
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'authority': 'primary_issuer_RI',
            'settlement_verified': False, 'point_in_time_archive_verified': False}
        if host in blocked_hosts:
            receipt.update(status='not_requested', reason='host_previously_returned_401_403_429')
        else:
            try:
                request = urllib.request.Request(url, headers={'User-Agent': 'RendaFutura-Academic/1.0',
                    'Accept': 'text/html,application/pdf'})
                with urllib.request.build_opener(_NoUnapprovedRedirect()).open(request, timeout=25) as response:
                    body = response.read(MAX_BYTES + 1)
                    final = response.geturl()
                    if len(body) > MAX_BYTES or not _approved_url(final):
                        raise ValueError('source_size_or_URL_rejected')
                    suffix = 'pdf' if body.startswith(b'%PDF-') else 'html'
                    path = target / f'{name}-{_digest(body)}.{suffix}'
                    _immutable(path, body)
                    receipt.update(status='acquired', file=path.relative_to(Path(folder)).as_posix(),
                        sha256=_digest(body), bytes=len(body), final_url=final,
                        content_type=response.headers.get('Content-Type', ''), http_status=response.status)
            except urllib.error.HTTPError as error:
                receipt.update(status='http_error', http_status=error.code, reason='no_retry_or_bypass')
                if error.code in (401, 403, 429):
                    blocked_hosts.add(host)
            except (OSError, ValueError) as error:
                receipt.update(status='unavailable', reason=type(error).__name__, detail=str(error)[:200])
        body = _json(receipt)
        _immutable(target / f'{name}-{_digest(body)}-receipt.json', body)
        receipts.append(receipt)
    return receipts


def _load_receipt(path):
    if path.is_symlink() or path.stat().st_size > 32_000:
        raise ValueError('unsafe_source_receipt')
    body = path.read_bytes()
    receipt = json.loads(body)
    name = receipt['source_id']
    if (name not in SOURCE_SPECS or receipt['url'] != SOURCE_SPECS[name][1]
            or path.name != name + '-' + _digest(body) + '-receipt.json'):
        raise ValueError('source_receipt_integrity_mismatch')
    return receipt


def read_acquired_source(folder, receipt):
    relative = receipt.get('file', '')
    if not isinstance(relative, str) or not re.fullmatch(CACHE + r'/[a-z0-9-]+-[0-9a-f]{64}\.(?:html|pdf)', relative):
        raise ValueError('invalid_research_source_reference')
    root = Path(folder).resolve()
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_BYTES:
        raise ValueError('unsafe_research_source_reference')
    body = path.read_bytes()
    if _digest(body) != receipt.get('sha256') or len(body) != receipt.get('bytes'):
        raise ValueError('research_source_integrity_mismatch')
    return body


def _calendar_disclosures(tables, receipt):
    layouts = {
        'ambp-history': {'kind': 0, 'amount': 3, 'payment': 4, 'record': None,
                         'header': ['provento', 'evento data', 'montante', 'valor por acao r$',
                                    'data do pagamento', 'payout lucro liquido']},
        'arml-history': {'kind': 4, 'amount': 5, 'payment': 0, 'record': 2,
                         'header': ['data de pagamento', 'periodo de referencia', 'data base',
                                    'data ex', 'tipo de provento', 'valor por acao r$', 'valor total r$']},
        'blau-history': {'kind': 7, 'amount': 6, 'payment': 4, 'record': 3,
                         'header': ['exercicio social', 'trimestre', 'aprovacao', 'data base',
                                    'data do pagamento', 'valor bruto brl', 'valor por acao brl', 'forma de distribuicao']},
    }
    layout = layouts.get(receipt['source_id'])
    if layout is None:
        return []
    clean = lambda value: ' '.join(re.sub(r'[^a-z0-9$ ]', ' ', _norm(value)).split())
    results = []
    for table in tables:
        if not table['rows'] or [clean(cell) for cell in table['rows'][0]] != layout['header']:
            continue
        for index, cells in enumerate(table['rows'][1:], 1):
            if len(cells) != len(layout['header']):
                continue
            kind = clean(cells[layout['kind']])
            amount = cells[layout['amount']].strip()
            if kind not in ('dividendo', 'dividendos', 'jcp', 'jscp') or not re.fullmatch(r'\d+,\d+', amount):
                continue
            value = Decimal(amount.replace(',', '.'))
            if value <= 0:
                continue

            def explicit_day(cell):
                if not re.fullmatch(r'\d{2}/\d{2}/\d{4}', cell.strip()):
                    return None
                try:
                    return datetime.strptime(cell.strip(), '%d/%m/%Y').date().isoformat()
                except ValueError:
                    return None

            day = explicit_day(cells[layout['payment']])
            results.append({'ticker': receipt['ticker'], 'kind': kind, 'amount_per_share_as_reported': str(value),
                'amount_basis': 'unverified_no_net_to_gross_conversion',
                'calendar_payment_date': day, 'payment_field_exact': cells[layout['payment']],
                'record_date': explicit_day(cells[layout['record']]) if layout['record'] is not None else None,
                'reported_in_requested_window': day is not None and WINDOW_START <= _day(day) <= WINDOW_END,
                'publication_date': None, 'source_sha256': receipt['sha256'],
                'source_url': receipt['url'], 'table': table['table'], 'table_row': index,
                'exact_cells': cells, 'payment_status': 'issuer_calendar_not_settlement',
                'settlement_verified': False, 'point_in_time_archive_verified': False,
                'share_basis_verified': False, 'cash_model_eligible': False})
    return results


def _documentary_readings(pages, receipt):
    """Hash-bound readings distinguish an explicit dividend statement from JCP."""
    if (receipt['source_id'] == 'biom-proposal-2025'
            and receipt['sha256'] == '0f64b9496eafe38f0e3772e293a7c2bca3be594352bb4fbd08823730b0ea5aa9'):
        page = next((row for row in pages if row['pdf_page'] == 53), None)
        anchor = 'nos exercicios de 31 de dezembro de 2024 e 2023, a companhia nao apurou lucro e, portanto, nao realizou a distribuicao de dividendos.'
        if page is not None and anchor in _norm(page['text']):
            return [{'kind': 'explicit_no_dividend_distribution_statement', 'pdf_page': 53,
                'source_sha256': receipt['sha256'], 'page_text_sha256': page['page_text_sha256'],
                'periods': [{'start': f'{year}-01-01', 'end': f'{year}-12-31'} for year in (2023, 2024)],
                'components_explicit': ['dividends'], 'jcp_explicit': False,
                'reason_not_combined_zero': 'Dividend distribution statement does not explicitly cover JCP or cash paid on prior declarations.',
                'qualified_zero_periods': [], 'settlement_verified': False}]
    return []


def inspect_issuer_source(body, receipt):
    """Expose primary tables/text for review. An empty history is never zero."""
    base = {'source_id': receipt['source_id'], 'ticker': receipt['ticker'], 'source': receipt,
            'status': 'pending_documentary_review', 'qualified_zero_periods': [],
            'empty_or_missing_history_is_not_zero': True, 'settlement_verified': False}
    if body.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(body))
        matches = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            if 'dividend' in _norm(text) or 'capital proprio' in _norm(text):
                matches.append({'pdf_page': number, 'text': text, 'page_text_sha256': _digest(text.encode('utf-8'))})
        return {**base, 'pages_with_distribution_mentions': matches, 'page_count': len(reader.pages),
                'documentary_readings': _documentary_readings(matches, receipt)}
    try:
        content = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = body
    tree = html.fromstring(content, parser=html.HTMLParser(no_network=True))
    tables = [{'table': index, 'rows': list(_table_rows(table))}
              for index, table in enumerate(tree.xpath('//table'))]
    texts = [' '.join(node.itertext()).strip() for node in tree.xpath('//p|//h1|//h2|//h3')]
    return {**base, 'tables': tables, 'positive_calendar_disclosures': _calendar_disclosures(tables, receipt),
        'distribution_paragraphs': [text for text in texts if any(token in _norm(text)
                                   for token in ('dividend', 'capital proprio', 'ultima atualizacao'))]}


def research(folder, *, observed_on='2026-10-25', fetch=False):
    """Offline by default; write only a new immutable research report/receipts."""
    folder, observed_on = market_root(folder), _day(observed_on)
    verified = identities(folder, observed_on)
    periods, diagnostics, sources = [], [], []
    for year in range(2021, 2027):
        kind = 'ITR' if year == 2026 else 'DFP'
        relative = f'raw/official-financials/itr_cia_aberta_{year}.zip' if kind == 'ITR' else f'raw/cvm-dfp-{year}.zip'
        if not (folder / relative).is_file():
            diagnostics.append({'source_file': relative, 'reason': 'cached_primary_archive_missing_not_zero'})
            continue
        rows, audit, source = _archive_periods(folder, relative, kind, year, verified, observed_on, WINDOW_START, WINDOW_END)
        periods.extend(rows)
        diagnostics.extend(audit)
        sources.append(source)
    receipts = acquire_sources(folder) if fetch else [_load_receipt(path)
        for path in sorted((folder / CACHE).glob('*-receipt.json'))]
    ri = [inspect_issuer_source(read_acquired_source(folder, receipt), receipt)
          for receipt in receipts if receipt['status'] == 'acquired']
    companies = []
    for identity in verified:
        rows = [row for row in periods if row['ticker'] == identity['ticker']]
        summary = summarize_periods(rows)
        disclosed = [entry for item in ri if item['ticker'] == identity['ticker']
                     for entry in item.get('positive_calendar_disclosures', []) if entry['reported_in_requested_window']]
        status = summary['status']
        if status == 'pending' and disclosed:
            status = 'known_positive_distribution_disclosure_not_settlement'
        gap = []
        if not identity['identity_verified']:
            gap.append('Current FCA ticker field unresolved; existing cached B3 mapping is not a repair or current trading verification.')
        if identity['ticker'] == 'AALR3':
            gap.append('No 2026 issuer filing in the cached ITR index; January through September 2026 remain unknown.')
        if identity['ticker'] in ('AERI3', 'AMER3'):
            gap.append('Explicit dividend-only zeros do not establish zero JCP. A nonzero annual outflow is reported during the window.')
        if identity['ticker'] in ('AMAR3', 'BIOM3', 'AMBP3'):
            gap.append('No qualifying explicit combined dividends/JCP paid zero in individual cashflow statements. Received dividends are excluded.')
        gap.append('No reviewed primary statement explicitly covers both cash components for July through September 2026; no extrapolation from Q2.')
        companies.append({'identity': identity, **summary, 'cashflow_status': summary['status'], 'status': status,
            'positive_primary_calendar_disclosures': disclosed, 'gaps': gap, 'all_cashflow_readings': rows})
    report = {'schema_version': 1, 'policy': POLICY, 'observed_on': observed_on.isoformat(),
        'window_start': WINDOW_START.isoformat(), 'window_end': WINDOW_END.isoformat(),
        'module_sha256': _digest(Path(__file__).read_bytes()), 'sources': sources,
        'issuer_sources': receipts, 'issuer_source_inspections': ri,
        'companies': companies, 'diagnostics': diagnostics,
        'counts': {'issuers': len(companies), 'cashflow_period_readings': len(periods),
            'explicit_zero_both_selected_individual_periods': sum(row['reported_zero_both_period_count'] for row in companies),
            'zero_supported_issuer_months': sum(row['zero_supported_full_month_count'] for row in companies),
            'known_positive_cashflow_issuers': sum(row['cashflow_status'] == 'known_positive' for row in companies),
            'known_positive_disclosure_only_issuers': sum(row['status'] == 'known_positive_distribution_disclosure_not_settlement' for row in companies),
            'known_positive_issuers': sum(row['status'].startswith('known_positive') for row in companies),
            'exact_current_FCA_security_identities': sum(row['identity']['identity_verified'] for row in companies),
            'positive_calendar_disclosures_in_window': sum(len(row['positive_primary_calendar_disclosures']) for row in companies),
            'fully_reported_zero_issuers': sum(row['all_requested_months_reported_zero'] for row in companies),
            'primary_RI_sources_acquired': len(ri)},
        'active_model_changed': False, 'individual_events_generated': 0,
        'model_approval_performed': False, 'settlement_verified': False,
        'point_in_time_archive_verified': False,
        'interpretation': 'A reported zero component is not zero dividends plus JCP. Missing tables, received dividends, policies, financing totals and periods after the latest report remain unknown. RI schedules are not cash settlement. Amounts are not converted from net to gross.'}
    target = _target(folder)
    body = _json(report)
    path = target / ('research-' + _digest(body) + '.json')
    _immutable(path, body)
    return report, path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--observed-on', default='2026-10-25')
    parser.add_argument('--fetch-primary-RI', action='store_true')
    args = parser.parse_args()
    report, path = research(args.data_dir, observed_on=args.observed_on, fetch=args.fetch_primary_RI)
    print(json.dumps({'report': str(path), 'counts': report['counts'],
        'companies': [{'ticker': row['identity']['ticker'], 'status': row['status'],
            'zero_supported_months': row['zero_supported_full_month_count'],
            'unresolved_months': row['unresolved_months']} for row in report['companies']]}, ensure_ascii=True))
