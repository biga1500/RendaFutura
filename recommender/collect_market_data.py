"""Download public observations and build a reproducible, non-synthetic snapshot."""

import argparse
import calendar
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile

from .datasets import FEATURES, real_dataset
from .contracts import validate_features
from .forecast import dataset_forecast
from .credentials import brapi_token
from .dividend_scope import allowed_tickers, scope_metadata


ROOT = Path(__file__).resolve().parents[1]
TREASURY_URL = ('https://www.tesourotransparente.gov.br/ckan/dataset/'
                'df56aa42-484a-4a59-8184-7676580c81e3/resource/'
                '796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv')
STOCKS = {'PETR4': (9512, 'PN', 0), 'VALE3': (4170, 'ON', 1)}
FORECAST_TARGET = {
    'id': 'rights_by_record_date_v1',
    'event_basis': 'record_date', 'date_field': 'lastDatePrior', 'unit': 'BRL/share',
    'allowed_labels': ['DIVIDENDO', 'JCP'],
    'excluded_components': ['RENDIMENTO', 'correcao', 'atualizacao', 'complemento posterior'],
    'target': 'Direitos brutos por data-com em (as_of, label_end], horizonte de 12 meses.',
    'previous_cash': 'Nome legado: direitos brutos por data-com em (as_of menos 12 meses, as_of].',
    'regularity': 'Fracao de meses com direitos no periodo anterior; nao frequencia de pagamentos.',
    'payout': 'Direitos brutos anteriores / LPA anual disponivel; nao payout de caixa pago.',
    'availability': 'approvedOn quando informado; caso ausente, disponibilidade na propria data-com, sem imputar aprovacao. Aprovacao posterior bloqueia janelas contendo aquele direito.',
    'warning': 'NAO E CAIXA RECEBIDO, NAO E CALENDARIO DE PAGAMENTO E NAO E RENDA MENSAL.',
    'metrics_comparable_to_payment_target': False,
    'caveats': [
        'Mudanca de medida observavel definida antes das novas metricas, sem selecionar alvo pelo teste.',
        'Metricas nao sao diretamente comparaveis a versao anterior de pagamentos.',
        'Raw revisado nao e arquivo point-in-time; nao se comprova publicacao historica de todos os valores.',
        'Ajustes posteriores identificaveis sao filtrados; revisoes nao sinalizadas pelo fornecedor podem permanecer.',
        'Retornos totais ja incluem distribuicoes; direitos previstos nao devem ser adicionados novamente.',
        'Pendencias e reconciliacoes de pagamento sao somente auditoria e nao alteram este alvo.',
    ],
}


class DividendHistory(list):
    def __init__(self, cutoff, event_count, basis='payment'):
        super().__init__()
        self.cutoff = cutoff
        self.event_count = event_count
        self.future_events = 0
        self.issues = []
        self.reconciled_events = []
        self.observed_payment_availability = []
        self.basis = basis
        self.filter_counts = {}
        self.filtered_events = []
        self.cash_payment_issues = []
        self.payment_audit_counts = {}
        self.right_event_audit = []

    def blockers(self, start, end):
        # Event windows are (start, end]; uncertainty intervals include both bounds.
        return [issue for issue in self.issues
                if issue['interval_start'] <= end.isoformat() and issue['interval_end'] > start.isoformat()]


class CollectionBlocked(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__(report['reason'])


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def shift_month(value, offset):
    year, month = divmod(value.year * 12 + value.month - 1 + offset, 12)
    return month_end(year, month + 1)


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))


def fetch(url, target, *, refresh=False, token=None):
    if target.exists() and not refresh:
        return target.read_bytes()
    headers = {'User-Agent': 'RendaFutura academic data collector', 'Accept': '*/*'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
                content = response.read()
            target.write_bytes(content)
            return content
        except urllib.error.URLError as error:
            last = error
            if isinstance(error.reason, ssl.SSLCertVerificationError) and url == TREASURY_URL and os.name == 'nt':
                subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                str(ROOT / 'scripts' / 'download-public-data.ps1'), '-Uri', url,
                                '-OutFile', str(target)], check=True, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                return target.read_bytes()
            if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(1 + attempt * 2)
    raise ValueError('Falha ao consultar a fonte publica: ' + url.split('?')[0]) from last


def cached_source(url, name, raw, sources, refresh, *, cached_only=False):
    target = raw / name
    if cached_only and (refresh or not target.is_file()):
        raise ValueError('Cache raw ausente ou refresh incompativel com --cached-only: ' + name)
    content = fetch(url, target, refresh=refresh, token=brapi_token() if 'brapi.dev/' in url else None)
    sources.append({'file': name, 'url': url, 'sha256': hashlib.sha256(content).hexdigest(),
                    'bytes': len(content), 'retrieved_at': datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat()})
    return content


def load_reconciliations(folder, symbol, content):
    path = folder / 'reconciliations.json'
    if not path.exists():
        return {}
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise ValueError('Manifesto de reconciliacao invalido.')
    events = json.loads(content)['results'][0].get('dividendsData', {}).get('cashDividends', [])
    overrides = {}
    for entry in manifest.get('overrides', []):
        if entry['ticker'] != symbol:
            continue
        if entry['raw_sha256'] != hashlib.sha256(content).hexdigest():
            raise ValueError(symbol + ': raw mudou; reconciliacao exige nova verificacao.')
        index = entry['event_index']
        if not isinstance(index, int) or not 0 <= index < len(events) or index in overrides:
            raise ValueError('Indice de evento reconciliado invalido ou duplicado.')
        for field, expected in entry['match'].items():
            actual = events[index].get(field)
            if field in ('paymentDate', 'approvedOn', 'lastDatePrior', 'exDate'):
                actual = (actual or '')[:10] or None
            if actual != expected:
                raise ValueError(symbol + ': evento diverge da reconciliacao verificada.')
        evidence = entry['evidence']
        evidence_path = (folder / evidence['file']).resolve()
        if not evidence_path.is_relative_to((folder / 'evidence').resolve()):
            raise ValueError('Evidencia deve estar no diretorio evidence da base.')
        if not evidence['url'].startswith('https://') or not evidence.get('locator'):
            raise ValueError('Fonte primaria e localizador da evidencia sao obrigatorios.')
        if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('Hash da evidencia de reconciliacao diverge.')
        changes = entry.get('changes', {})
        if set(changes) - {'paymentDate', 'approvedOn'} or not changes:
            raise ValueError('Reconciliacao deve verificar explicitamente pagamento ou aprovacao.')
        for field, value in changes.items():
            if field not in evidence['verified_fields'] or date.fromisoformat(value).isoformat() != value:
                raise ValueError('Data reconciliada sem verificacao primaria explicita.')
            if events[index].get(field) and events[index][field][:10] != value:
                raise ValueError('Reconciliacao nao pode sobrescrever uma data existente divergente.')
        overrides[index] = entry
    return overrides


def record_date_history(events, cutoff, payments):
    history = DividendHistory(cutoff, len(events), basis='record_date')
    history.cash_payment_issues = payments.issues
    history.payment_audit_counts = {'accepted': len(payments), 'future': payments.future_events,
                                    'unresolved': len(payments.issues)}
    history.reconciled_events = payments.reconciled_events
    history.observed_payment_availability = payments.observed_payment_availability
    counts = defaultdict(int, {'raw_events': len(events), 'accepted_events': 0, 'excluded_label': 0,
                               'excluded_later_adjustment': 0, 'future_record_date': 0,
                               'unresolved_events': 0})
    for index, row in enumerate(events):
        label = (row.get('label') or '').strip().upper()
        if label not in FORECAST_TARGET['allowed_labels']:
            reason = 'excluded_label'
        elif any(word in normalize(' '.join(str(row.get(k) or '') for k in ('remarks', 'relatedTo')))
                 for word in ('correc', 'atualiz', 'complement', 'selic')):
            reason = 'excluded_later_adjustment'
        else:
            reason = None
        if reason:
            counts[reason] += 1
            history.filtered_events.append({'event_index': index, 'label': label, 'reason': reason})
            continue
        reasons = []
        try:
            day = date.fromisoformat(row['lastDatePrior'][:10]) if row.get('lastDatePrior') else None
        except (TypeError, ValueError):
            day = None
        if day is not None and day > cutoff:
            counts['future_record_date'] += 1
            history.future_events += 1
            continue
        try:
            known = date.fromisoformat(row['approvedOn'][:10]) if row.get('approvedOn') else None
        except (TypeError, ValueError):
            known = None
            reasons.append('invalid_approval_date')
        try:
            rate = float(row.get('rate'))
        except (TypeError, ValueError):
            rate = float('nan')
        if day is None:
            reasons.append('missing_or_invalid_record_date')
        if day is not None and known is not None and known > day:
            reasons.append('approval_after_record_date')
        if not math.isfinite(rate) or rate < 0:
            reasons.append('invalid_rate')
        if reasons:
            # With no record date, neither payment nor ex-date locates the right.
            history.issues.append({'event_index': index, 'reasons': reasons, 'label': label,
                                   'rate': rate if math.isfinite(rate) else None,
                                   'record_date': day.isoformat() if day else None,
                                   'approval_date': known.isoformat() if known else None,
                                   'interval_start': (day or date.min).isoformat(),
                                   'interval_end': (day or cutoff).isoformat(),
                                   'bound_basis': 'record_date' if day else 'unknown_record_date_through_cutoff',
                                   'resolution': 'exclude_overlapping_rights_windows'})
            counts['unresolved_events'] += 1
            for reason in reasons:
                counts[reason] += 1
            continue
        availability = known or day
        history.append((day, availability, rate))
        history.right_event_audit.append({'event_index': index, 'label': label, 'record_date': day.isoformat(),
                                          'rate': rate, 'availability_date': availability.isoformat(),
                                          'availability_basis': 'reported_approval' if known else 'observed_record_date',
                                          'approval_date': known.isoformat() if known else None,
                                          'approval_publication_verified': False})
        counts['accepted_events'] += 1
    if not events:
        history.issues.append({'event_index': None, 'reasons': ['rights_history_absent'],
                               'interval_start': date.min.isoformat(), 'interval_end': cutoff.isoformat(),
                               'resolution': 'exclude_overlapping_rights_windows'})
    history.filter_counts = dict(counts)
    return history


def stock_history(content, cutoff, *, reconciliations=None, event_basis='payment'):
    if event_basis not in ('payment', 'record_date'):
        raise ValueError('Base temporal de proventos desconhecida.')
    result = json.loads(content)['results'][0]
    if result.get('currency') != 'BRL':
        raise ValueError('A cotacao precisa estar em reais.')
    prices = {}
    for row in result['historicalDataPrice']:
        day = datetime.fromtimestamp(row['date'], timezone.utc).date()
        adjusted, close = row.get('adjustedClose'), row.get('close')
        if day <= cutoff and adjusted is not None and close is not None and adjusted > 0 and close > 0:
            key = month_end(day.year, day.month)
            if key not in prices or day > prices[key][0]:
                prices[key] = (day, float(adjusted), float(close))
    actions = result.get('dividendsData', {}).get('stockDividends', [])
    # This initial universe has no split/bonus in the modelling window. Refuse new
    # share scales rather than silently mixing per-share units across the labels.
    if any('2010-01-01' <= (r.get('lastDatePrior') or '')[:10] <= cutoff.isoformat()
           and r.get('label') in ('DESDOBRAMENTO', 'GRUPAMENTO', 'BONIFICACAO') for r in actions):
        raise ValueError('Evento em acoes exige normalizacao explicita antes de incluir o ativo.')
    events = result.get('dividendsData', {}).get('cashDividends', [])
    dividends = DividendHistory(cutoff, len(events))
    for index, original in enumerate(events):
        reconciliation = (reconciliations or {}).get(index)
        row = {**original, **reconciliation['changes']} if reconciliation else original
        parsed, reasons = {}, []
        for field in ('paymentDate', 'approvedOn', 'exDate', 'lastDatePrior'):
            try:
                parsed[field] = date.fromisoformat(row[field][:10]) if row.get(field) else None
            except (TypeError, ValueError):
                parsed[field] = None
                reasons.append('invalid_' + field)
        day, known = parsed['paymentDate'], parsed['approvedOn']
        if day is not None and day > cutoff:
            dividends.future_events += 1
            continue
        if day is None and parsed['exDate'] is not None and parsed['exDate'] > cutoff:
            dividends.future_events += 1
            continue
        try:
            rate = float(row.get('rate'))
        except (TypeError, ValueError):
            rate = float('nan')
        if day is None:
            reasons.append('missing_or_invalid_payment_date')
        if day is not None and known is not None and known > day:
            reasons.append('approval_after_payment')
        if not math.isfinite(rate) or rate < 0:
            reasons.append('invalid_rate')
        if reasons:
            # An ex-date only bounds the earliest possible payment. It is never a payment date.
            lower = day or parsed['exDate'] or parsed['lastDatePrior'] or known or date.min
            dividends.issues.append({
                'event_index': index, 'reasons': reasons, 'label': row.get('label'),
                'rate': rate if math.isfinite(rate) else None,
                'payment_date': day.isoformat() if day else None,
                'approval_date': known.isoformat() if known else None,
                'ex_date': parsed['exDate'].isoformat() if parsed['exDate'] else None,
                'last_date_prior': parsed['lastDatePrior'].isoformat() if parsed['lastDatePrior'] else None,
                'interval_start': lower.isoformat(), 'interval_end': (day or cutoff).isoformat(),
                'bound_basis': 'reported_payment_date' if day else 'earliest_event_anchor_to_cutoff',
                'resolution': 'unresolved_exclude_overlapping_windows',
                'approval_publication_verified': False,
            })
            continue
        # Historical paid cash is observable on payment, even without an approval date.
        availability = known or day
        dividends.append((day, availability, rate))
        if known is None:
            dividends.observed_payment_availability.append({
                'event_index': index, 'payment_date': day.isoformat(),
                'availability_date': day.isoformat(), 'availability_basis': 'observed_payment',
                'approval_date': None, 'approval_publication_verified': False,
                'source': 'reported_historical_payment_brapi_not_point_in_time',
            })
        if reconciliation:
            dividends.reconciled_events.append({'event_index': index, 'changes': reconciliation['changes'],
                                                'original_payment_date': (original.get('paymentDate') or '')[:10] or None,
                                                'original_approval_date': (original.get('approvedOn') or '')[:10] or None,
                                                'evidence': reconciliation['evidence']})
    if not events:
        dividends.issues.append({'event_index': None, 'reasons': ['cash_history_absent'],
                                'interval_start': date.min.isoformat(), 'interval_end': cutoff.isoformat(),
                                'resolution': 'unresolved_exclude_overlapping_windows'})
    if not prices:
        raise ValueError('Historico de cotacoes ausente.')
    dividends.cash_payment_issues = dividends.issues
    dividends.filter_counts = {'raw_events': len(events), 'accepted_events': len(dividends),
                               'future_payment_date': dividends.future_events,
                               'unresolved_events': len(dividends.issues)}
    if event_basis == 'record_date':
        dividends = record_date_history(events, cutoff, dividends)
    return result, prices, dividends


def cvm_statements(content, year, *, diagnostics=None, specs=None):
    requested = defaultdict(set)
    financial_classes = set()
    for cvm, share_class, sector in (STOCKS if specs is None else specs).values():
        requested[cvm].add(share_class)
        if sector == 2:
            financial_classes.add((cvm, share_class))
    archive = zipfile.ZipFile(io.BytesIO(content))

    def rows(suffix):
        name = f'dfp_cia_aberta{suffix}_{year}.csv'
        with archive.open(name) as stream:
            for row in csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'):
                try:
                    cvm = int(row.get('CD_CVM') or 0)
                except (TypeError, ValueError):
                    continue
                if cvm in requested:
                    yield row

    filings = {(int(r['CD_CVM']), r['DT_REFER'], r['VERSAO']): r for r in rows('')}
    accounts = defaultdict(dict)
    capex = defaultdict(list)
    for statement in ('_DRE_con', '_DFC_MI_con', '_BPA_con', '_BPP_con'):
        for r in rows(statement):
            if r['ORDEM_EXERC'] != '\u00daLTIMO' or r['MOEDA'] != 'REAL':
                continue
            key = (int(r['CD_CVM']), r['DT_REFER'], r['VERSAO'])
            if key not in filings:
                continue
            try:
                value = float(r['VL_CONTA'])
            except (TypeError, ValueError, OverflowError):
                value = float('nan')
            # EPS is explicitly Reais/Acao, even when the statement is in MIL.
            if not r['CD_CONTA'].startswith('3.99'):
                if r['ESCALA_MOEDA'] not in ('MIL', 'UNIDADE'):
                    raise ValueError('Escala monetaria CVM nao reconhecida.')
                value *= 1000 if r['ESCALA_MOEDA'] == 'MIL' else 1
            # Invalid values remain unavailable, including individual EPS/capex legs.
            value = value if math.isfinite(value) else None
            accounts[key][r['CD_CONTA']] = (value, r['DS_CONTA'])
            desc = normalize(r['DS_CONTA'])
            if (statement == '_DFC_MI_con' and r['CD_CONTA'].startswith('6.02.')
                and ('imobiliz' in desc or 'intangiv' in desc)
                and any(word in desc for word in ('aquis', 'adico', 'compra', 'investimento'))
                and (value is None or value <= 0)):
                capex[key].append((r['CD_CONTA'], value))
    output = []
    for key in filings:
        a = accounts[key]
        cvm, fiscal, version = key
        descriptions = {code: normalize(desc).strip() for code, (_, desc) in a.items()}
        industrial_layout = (descriptions.get('2.01', 'passivo circulante') == 'passivo circulante'
                             and descriptions.get('2.02', 'passivo nao circulante') == 'passivo nao circulante')
        consolidated_equity = [code for code, desc in descriptions.items()
                               if code.startswith('2.') and code.count('.') == 1
                               and desc == 'patrimonio liquido consolidado']
        for share_class in sorted(requested[cvm]):
            eps_accounts = {code: value for code, (value, desc) in a.items()
                            if code.startswith('3.99.01.') and desc.strip().upper() == share_class}
            eps = list(eps_accounts.values())
            reasons = []
            financial = ((cvm, share_class) in financial_classes
                         and ('2' in a or consolidated_equity or not industrial_layout))
            profit_code, equity_code = '3.11.01', None
            if financial:
                profit_codes = [code for code, desc in descriptions.items()
                                if code.startswith('3.') and desc in (
                                    'atribuido a socios da empresa controladora',
                                    'atribuido aos socios da empresa controladora')]
                profit_code = profit_codes[0] if len(profit_codes) == 1 else None
                equity_code = consolidated_equity[0] if len(consolidated_equity) == 1 else None
                if profit_code is None:
                    reasons.append('attributable_profit_absent_or_ambiguous')
                if equity_code is None:
                    reasons.append('consolidated_equity_absent_or_ambiguous')
                required = ('1', '2', *[code for code in (profit_code, equity_code) if code])
            else:
                required = ('3.11.01', '6.01', '1', '2.01', '2.02')
            selected = set() if financial else {code for code, _ in capex[key]}
            if not a:
                reasons.append('accounts_unavailable_for_version')
            valid_eps = len(eps) == 1 and eps[0] is not None and eps[0] != 0
            if not valid_eps:
                reasons.append('basic_eps_absent_ambiguous_or_zero')
            missing = [code for code in required if code not in a or a[code][0] is None]
            if missing:
                reasons.append('required_accounts_missing')
            invalid = sorted({code for code in set(required) | set(eps_accounts)
                              if code in a and a[code][0] is None}
                             | {code for code, value in capex[key] if value is None and not financial})
            if invalid:
                reasons.append('invalid_numeric_accounts')
            if not financial and not capex[key]:
                reasons.append('capex_not_identified')
            shares = a[profit_code][0] / eps[0] if not missing and valid_eps and profit_code else None
            if shares is not None and (not math.isfinite(shares) or shares <= 0 or a['1'][0] <= 0):
                reasons.append('invalid_inferred_shares_or_assets')
            if not reasons:
                if financial:
                    free_cash = None
                    liabilities = a['2'][0] - a[equity_code][0]
                    debt_ratio = liabilities / a['1'][0]
                    if (descriptions['2'] != 'passivo total' or descriptions['1'] != 'ativo total'
                            or a['2'][0] < 0 or liabilities < 0
                            or not math.isclose(a['2'][0], a['1'][0], rel_tol=1e-6, abs_tol=1)):
                        reasons.append('invalid_consolidated_balance_sheet')
                else:
                    # Keep only leaves when both an acquisition aggregate and its children exist.
                    spending = sum(value for code, value in capex[key]
                                   if not any(other.startswith(code + '.') for other, _ in capex[key]))
                    free_cash = (a['6.01'][0] + spending) / shares
                    debt_ratio = (a['2.01'][0] + a['2.02'][0]) / a['1'][0]
                if (free_cash is not None and not math.isfinite(free_cash)) or not math.isfinite(debt_ratio):
                    reasons.append('invalid_derived_financial_values')
            method = {
                'id': 'financial_no_industrial_fcf_v1' if financial else 'industrial_cash_capex_v1',
                'accounts_scope': 'consolidated', 'profit_account': profit_code,
                'basic_eps_account': next(iter(eps_accounts)) if len(eps_accounts) == 1 else None,
                'assets_account': '1', 'equity_account': equity_code,
                'liabilities_method': 'total_passivo_less_consolidated_equity' if financial else 'current_plus_noncurrent',
                'liabilities_accounts': ['2', equity_code] if equity_code else ([] if financial else ['2.01', '2.02']),
                'capex_accounts': sorted(selected),
                'fcf_absence_reason': 'industrial_fcf_not_applicable_to_financial_issuer' if financial else None,
            }
            if diagnostics is not None:
                diagnostics.append({
                    'cvm': cvm, 'share_class': share_class, 'fiscal_end': fiscal, 'version': version,
                    'filing_id': filings[key]['ID_DOC'], 'published_at': filings[key]['DT_RECEB'],
                    'status': 'excluded' if reasons else 'included', 'reasons': reasons,
                    'missing_accounts': missing, 'invalid_numeric_accounts': invalid,
                    'financial_method': method,
                    'capex_accounts': sorted(selected),
                    'unselected_investing_outflows': [
                        {'code': code, 'description': desc, 'value_brl': value}
                        for code, (value, desc) in a.items()
                        if code.startswith('6.02.') and value is not None and value < 0 and code not in selected],
                })
            if reasons:
                continue
            output.append({'cvm': cvm, 'share_class': share_class, 'fiscal_end': fiscal, 'version': version,
                           'published_at': filings[key]['DT_RECEB'], 'filing_id': filings[key]['ID_DOC'],
                           'profit_per_share': eps[0], 'free_cash_per_share': free_cash,
                           'debt_ratio': debt_ratio, 'financial_method': method})
    return output


def financial_features(statements, dividends, as_of, spec):
    if isinstance(dividends, DividendHistory) and dividends.blockers(shift_month(as_of, -12), as_of):
        return None
    eligible = [r for r in statements if r['cvm'] == spec[0]
                and ('share_class' not in r or r['share_class'] == spec[1])
                and r['published_at'] <= as_of.isoformat()]
    if not eligible:
        return None
    record = max(eligible, key=lambda r: (r['fiscal_end'], int(r['version'])))
    if (as_of - date.fromisoformat(record['fiscal_end'])).days > 730:
        return None
    if record['profit_per_share'] is None or record['profit_per_share'] == 0:
        return None
    previous = [(d, value) for d, known, value in dividends if shift_month(as_of, -12) < d <= as_of and known <= as_of]
    cash = sum(value for _, value in previous)
    features = [cash, len({(d.year, d.month) for d, _ in previous}) / 12,
                record['profit_per_share'], record['free_cash_per_share'],
                cash / record['profit_per_share'], record['debt_ratio'], float(spec[2])]
    try:
        validate_features(features)
    except ValueError:
        return None
    return features, record


def stock_observations(symbol, prices, dividends, statements, cutoff, spec=None):
    spec = STOCKS[symbol] if spec is None else spec
    observations, excluded = [], []
    for as_of in sorted(prices):
        end = shift_month(as_of, 12)
        if as_of < month_end(2012, 1) or end > cutoff:
            continue
        past_issues = dividends.blockers(shift_month(as_of, -12), as_of)
        target_issues = dividends.blockers(as_of, end)
        features = financial_features(statements, dividends, as_of, spec)
        reasons = []
        if past_issues:
            reasons.append('previous_cash_incomplete')
        if target_issues:
            reasons.append('target_cash_incomplete')
        if features is None and not past_issues:
            reasons.append('financial_features_unavailable')
        if reasons:
            excluded.append({'as_of': as_of.isoformat(), 'label_end': end.isoformat(), 'reasons': reasons,
                             'previous_cash_issue_ids': [r['event_index'] for r in past_issues],
                             'target_cash_issue_ids': [r['event_index'] for r in target_issues]})
            continue
        f, filing = features
        target = sum(value for d, _, value in dividends if as_of < d <= end)
        availability = max([filing['published_at'], *[known.isoformat() for d, known, _ in dividends
                           if shift_month(as_of, -12) < d <= as_of and known <= as_of]])
        observations.append({'ticker': symbol, 'as_of': as_of.isoformat(), 'published_at': filing['published_at'],
                             'label_end': end.isoformat(), **dict(zip(FEATURES, f)), 'target': target,
                             'fiscal_end': filing['fiscal_end'], 'filing_id': filing['filing_id'],
                             'forecast_target_id': FORECAST_TARGET['id'] if dividends.basis == 'record_date' else 'cash_payments_12m_v1',
                             'event_basis': dividends.basis,
                             'event_date_field': 'lastDatePrior' if dividends.basis == 'record_date' else 'paymentDate',
                             'previous_window_start': shift_month(as_of, -12).isoformat(),
                             'features_available_at': availability})
    current_issues = dividends.blockers(shift_month(cutoff, -12), cutoff)
    coverage = {
        'ticker': symbol, 'basis': dividends.basis, 'filter_counts': dividends.filter_counts,
        'filtered_events': dividends.filtered_events, 'raw_cash_events': dividends.event_count,
        'accepted_cash_events': dividends.payment_audit_counts.get('accepted', len(dividends)),
        'future_cash_events': dividends.payment_audit_counts.get('future', dividends.future_events),
        'unresolved_cash_events': dividends.payment_audit_counts.get('unresolved', len(dividends.issues)),
        'cash_payment_issues': dividends.cash_payment_issues,
        'payment_audit_affects_target': dividends.basis == 'payment',
        'accepted_right_events': len(dividends) if dividends.basis == 'record_date' else None,
        'unresolved_right_events': len(dividends.issues) if dividends.basis == 'record_date' else None,
        'right_event_audit': dividends.right_event_audit,
        'reconciled_events': dividends.reconciled_events,
        'observed_payment_availability': dividends.observed_payment_availability,
        'event_issues': dividends.issues, 'candidate_windows': len(observations) + len(excluded),
        'included_windows': len(observations), 'excluded_windows_count': len(excluded),
        'excluded_by_reason': {reason: sum(reason in row['reasons'] for row in excluded)
                               for reason in ('previous_cash_incomplete', 'target_cash_incomplete',
                                              'financial_features_unavailable')},
        'excluded_windows': excluded,
        'current_cash_window': {'start_exclusive': shift_month(cutoff, -12).isoformat(),
                                'end_inclusive': cutoff.isoformat(), 'complete': not current_issues,
                                'basis': dividends.basis,
                                'issue_ids': [r['event_index'] for r in current_issues]},
    }
    if dividends.basis == 'record_date':
        coverage['current_rights_window'] = coverage['current_cash_window']
        coverage['legacy_alias_note'] = 'current_cash_window/previous_cash/target_cash identificam direitos, nao pagamentos; contagens cash_events sao somente auditoria de pagamentos.'
    return observations, coverage


def treasury_history(content, cutoff):
    text = content.decode('utf-8-sig')
    histories = defaultdict(dict)
    for r in csv.DictReader(io.StringIO(text), delimiter=';'):
        kind = r['Tipo Titulo']
        if kind not in ('Tesouro Selic', 'Tesouro Prefixado', 'Tesouro IPCA+'):
            continue
        maturity = datetime.strptime(r['Data Vencimento'], '%d/%m/%Y').date()
        day = datetime.strptime(r['Data Base'], '%d/%m/%Y').date()
        price = float(r['PU Venda Manha'].replace('.', '').replace(',', '.'))
        rate = float(r['Taxa Compra Manha'].replace(',', '.')) / 100
        if day > cutoff or maturity <= cutoff or price <= 0:
            continue
        month = month_end(day.year, day.month)
        key = (kind, maturity)
        if month not in histories[key] or day > histories[key][month][0]:
            histories[key][month] = (day, price, rate)
    selected = []
    for kind in ('Tesouro Selic', 'Tesouro Prefixado', 'Tesouro IPCA+'):
        eligible = [(key, values) for key, values in histories.items() if key[0] == kind and cutoff in values
                    and all(shift_month(cutoff, -i) in values for i in range(61))]
        if not eligible and kind == 'Tesouro Prefixado':
            continue
        if not eligible:
            raise ValueError(f'{kind}: nenhum titulo com 61 precos mensais consecutivos e referencia atual.')
        selected.append(min(eligible, key=lambda item: item[0][1]))
    return selected


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path, value):
    with path.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def publish_pointer(folder, snapshot, report):
    relative = snapshot.relative_to(folder).as_posix()
    pointer = {'schema_version': 1, 'status': report['status'], 'snapshot_id': snapshot.name,
               'reference_date': report['reference_date'], 'report': relative + '/collection-report.json'}
    if report['status'] in ('ready', 'partial'):
        pointer['snapshot'] = relative
    else:
        pointer['reason'] = report['reason']
    legacy_files = ('assets.json', 'returns.csv', 'observations.csv', 'collection-report.json')
    if not (folder / 'current.json').exists() and all((folder / name).is_file() for name in legacy_files):
        backup = folder / 'snapshots' / ('legacy-backup-' + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        for name in legacy_files:
            shutil.copy2(folder / name, backup / name)
        pointer['legacy_backup'] = backup.relative_to(folder).as_posix()
    temporary = folder / ('.current-' + uuid.uuid4().hex + '.tmp')
    try:
        write_json(temporary, pointer)
        for attempt in range(5):
            try:
                os.replace(temporary, folder / 'current.json')
                break
            except PermissionError:
                # Windows sync/antivirus can briefly lock a just-read pointer.
                # Retry the atomic replace; never delete the active pointer.
                if attempt == 4:
                    raise
                time.sleep(.05 * 2**attempt)
    finally:
        temporary.unlink(missing_ok=True)


def collect(folder, *, refresh=False, today=None, cached_only=False, activate=True):
    from .market_inventory import market_root

    folder = market_root(folder)
    selected = allowed_tickers(folder)
    specs = {ticker: spec for ticker, spec in STOCKS.items() if selected is None or ticker in selected}
    scope = scope_metadata(folder)
    if cached_only and refresh:
        raise ValueError('--cached-only nao pode ser combinado com --refresh.')
    today = today or date.today()
    cutoff = shift_month(month_end(today.year, today.month), -1)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / 'raw'
    raw.mkdir(exist_ok=True)
    sources, statements, stocks, financial_diagnostics = [], [], {}, []
    for symbol, spec in specs.items():
        print(f'Consultando precos e proventos: {symbol}', flush=True)
        url = f'https://brapi.dev/api/quote/{symbol}?range=max&interval=1d&dividends=true'
        content = cached_source(url, f'brapi-{symbol}.json', raw, sources, refresh, cached_only=cached_only)
        reconciliations = load_reconciliations(folder, symbol, content)
        stocks[symbol] = stock_history(content, cutoff, reconciliations=reconciliations, event_basis='record_date')
        for entry in reconciliations.values():
            evidence = entry['evidence']
            if any(source['file'] == evidence['file'] for source in sources):
                continue
            evidence_path = folder / evidence['file']
            sources.append({'file': evidence['file'], 'url': evidence['url'], 'sha256': evidence['sha256'],
                            'bytes': evidence_path.stat().st_size,
                            'retrieved_at': datetime.fromtimestamp(evidence_path.stat().st_mtime, timezone.utc).isoformat(),
                            'role': 'primary_reconciliation_evidence_payment_audit_only'})
    for year in range(2010, today.year):
        print(f'Consultando demonstracoes CVM: {year}', flush=True)
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip'
        content = cached_source(url, f'cvm-dfp-{year}.zip', raw, sources, refresh, cached_only=cached_only)
        statements.extend(cvm_statements(content, year, diagnostics=financial_diagnostics,
                                         **({'specs': specs} if selected is not None else {})))
    print('Consultando IPCA e precos do Tesouro Direto', flush=True)
    url = ('https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json'
           f'&dataInicial=01/01/2010&dataFinal={cutoff:%d/%m/%Y}')
    inflation = {month_end(int(r['data'][6:]), int(r['data'][3:5])): float(r['valor']) / 100
                 for r in json.loads(cached_source(url, 'bcb-ipca.json', raw, sources, refresh, cached_only=cached_only))}
    treasury = treasury_history(cached_source(TREASURY_URL, 'tesouro-precos.csv', raw, sources, refresh,
                                            cached_only=cached_only), cutoff)
    months = []
    first = month_end(2010, 2)
    current = cutoff
    while current >= first:
        previous = shift_month(current, -1)
        if (current not in inflation or any(current not in p or previous not in p for _, p, _ in stocks.values())
            or any(current not in p or previous not in p for _, p in treasury)):
            break
        months.append(current)
        current = previous
    months.reverse()
    if len(months) < 60:
        raise ValueError('Nao ha 60 meses consecutivos comuns entre todas as fontes.')
    assets, returns, observations, coverage, blocked_assets = [], [], [], [], []
    for symbol, (result, prices, dividends) in stocks.items():
        stock_rows, stock_coverage = stock_observations(symbol, prices, dividends, statements, cutoff, spec=specs[symbol])
        observations.extend(stock_rows)
        coverage.append(stock_coverage)
        current = financial_features(statements, dividends, cutoff, specs[symbol])
        if current is None:
            blocked_assets.append({'ticker': symbol, 'reason': 'current_rights_incomplete'
                                   if not stock_coverage['current_cash_window']['complete'] else
                                       'current_financial_features_unavailable'})
        assets.append({'ticker': symbol, 'name': result['longName'], 'asset_class': 'acoes', 'risk_level': 2,
                       'liquidity_days': 2, 'minimum_years': 5, 'price': prices[cutoff][2],
                       'annual_cash_yield': None, 'features': current[0] if current else None,
                       'as_of': cutoff.isoformat(),
                       'source': (f'brapi: direitos brutos por data-com (lastDatePrior), nao pagamentos; CVM DFP documento {current[1]["filing_id"]}.'
                                  if current else 'Ativo bloqueado: direitos por data-com ou fundamentos incompletos.')})
    bond_details = []
    for (kind, maturity), history in treasury:
        ticker = {'Tesouro Selic': 'TD-SELIC', 'Tesouro Prefixado': 'TD-PRE', 'Tesouro IPCA+': 'TD-IPCA'}[kind] + '-' + str(maturity.year)
        risk = {'Tesouro Selic': 0, 'Tesouro Prefixado': 1, 'Tesouro IPCA+': 2}[kind]
        assets.append({'ticker': ticker, 'name': f'{kind} {maturity:%d/%m/%Y}',
                       'asset_class': 'liquidez' if risk == 0 else 'renda_fixa', 'risk_level': risk,
                       'liquidity_days': 1, 'minimum_years': 0 if risk == 0 else 2 if risk == 1 else 5,
                       'price': history[cutoff][1], 'annual_cash_yield': 0, 'features': None,
                       'as_of': cutoff.isoformat(), 'source': 'Tesouro Transparente: PU de venda mensal; titulo sem cupom. Taxa de compra nao e dividendo.'})
        bond_details.append({'ticker': ticker, 'kind': kind, 'maturity': maturity.isoformat(),
                             'quote_date': history[cutoff][0].isoformat(), 'buy_rate': history[cutoff][2],
                             'cash_coupon': 0, 'return_method': 'PU Venda Manha mes / mes anterior - 1; sem cupom',
                             'holding_rule': 'Prazo minimo e nivel de risco sao parametros academicos, nao recomendacao do emissor.'})
    for day in months:
        row = {'date': day.isoformat(), 'inflation': inflation[day]}
        for symbol, (_, prices, _) in stocks.items():
            row[symbol] = prices[day][1] / prices[shift_month(day, -1)][1] - 1
        for asset, (_, history) in zip(assets[len(stocks):], treasury):
            row[asset['ticker']] = history[day][1] / history[shift_month(day, -1)][1] - 1
        returns.append(row)
    observations.sort(key=lambda r: (r['as_of'], r['ticker']))
    limitations = [
        f'Universo restrito a {len(stocks)} acoes brasileiras e {len(treasury)} titulos; sujeito a vies de selecao e sobrevivencia.',
        'Historicos brapi podem conter ajustes ou correcoes posteriores. Nao constituem arquivo point-in-time integral.',
        FORECAST_TARGET['warning'],
        'Alvo rights_by_record_date_v1: somente DIVIDENDO/JCP pela data-com informada em lastDatePrior. Nunca substitui por exDate ou paymentDate.',
        'previous_cash e nome legado de direitos brutos nos 12 meses anteriores; nao representa pagamentos, caixa mensal ou calendario de recebimento.',
        'RENDIMENTO e ajustes/complementos posteriores identificaveis sao excluidos. Data-com ausente bloqueia janelas; nao se inferem dias uteis.',
        'Aprovacao ausente usa disponibilidade observada na data-com, sem imputar aprovacao. Aprovacao posterior bloqueia janelas daquele direito, sem retroagir valor.',
        'Pendencias e reconciliacoes de pagamento permanecem na auditoria separada e nao afetam o alvo por data-com.',
        *FORECAST_TARGET['caveats'],
        'Fundamentos anuais CVM sao usados somente apos DT_RECEB da versao efetivamente baixada; nao se inventam datas de publicacao.',
        'Lucro por acao CVM e em reais por acao, sem multiplicar pela escala MIL das contas agregadas.',
        'Fluxo de caixa livre e proxy: caixa operacional menos aquisicoes de imobilizado/intangivel identificadas na DFC; acoes medias inferidas de lucro atribuivel/LPA.',
        'Payout e a razao assinada entre direitos brutos nos 12 meses anteriores e LPA anual disponivel; pode ser negativo em prejuizos.',
        'Direitos brutos por acao nao modelam imposto de JCP, reinvestimento individual nem recebimento efetivo.',
        'Titulos selecionados nao pagam cupom: fluxo em dinheiro igual a zero, valorizacao no retorno total. Nao equivalem a renda mensal contratada.',
        'Historico de titulos termina na data-base. Cenarios longos reamostram retornos; nao modelam vencimento, resgate de principal ou substituicao do titulo.',
        'Liquidez de resgate de titulos pode ser suspensa. Prazos e risco sao premissas academicas.',
        'Dados reais nao constituem validacao financeira, recomendacao profissional ou promessa de retorno.'
    ]
    metadata = {'assets': assets, 'returns_include_distributions': True,
                'source': 'Dados reais: brapi (PETR4/VALE3, direitos brutos por data-com, nao caixa mensal), CVM DFP, Tesouro Transparente e BCB/SGS IPCA 433.',
                'forecast_target': FORECAST_TARGET,
                'observation_schema': {'published_at': 'DT_RECEB do fundamento CVM; nao data de publicacao de todos os direitos.',
                                       'features_available_at': 'Maior disponibilidade reportada/observada dos atributos, sem garantia point-in-time.',
                                       'previous_window_start': 'Limite inferior exclusivo; limite superior e as_of.',
                                       'label_end': 'Limite superior inclusivo; limite inferior do alvo e as_of.'},
                'collected_at': datetime.now(timezone.utc).isoformat(), 'reference_date': cutoff.isoformat(),
                'observed_from': months[0].isoformat(), 'sources': sources, 'bonds': bond_details,
                'limitations': limitations, 'financially_validated': False,
                'refresh_command': 'python -m recommender.collect_market_data --refresh'}
    if scope is not None:
        metadata['dividend_scope'] = scope
    snapshot_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    snapshot = folder / 'snapshots' / snapshot_id
    snapshot.mkdir(parents=True)
    available_dates = len({row['as_of'] for row in observations})
    metadata['snapshot_id'] = snapshot_id
    report = {'reference_date': cutoff.isoformat(), 'assets': [a['ticker'] for a in assets], 'months': len(months),
              'first_month': months[0].isoformat(), 'observations': len(observations),
              'snapshot_id': snapshot_id, 'status': 'ready', 'blocked_assets': blocked_assets,
              'observation_dates': available_dates, 'cash_coverage': coverage,
              'forecast_target': FORECAST_TARGET,
              'basis_validated': all(history.basis == 'record_date' for _, _, history in stocks.values()),
              'financial_statements': {'included': sum(r['status'] == 'included' for r in financial_diagnostics),
                                       'excluded': sum(r['status'] == 'excluded' for r in financial_diagnostics),
                                       'diagnostics': financial_diagnostics},
              'excluded_classes': [] if any(key[0] == 'Tesouro Prefixado' for key, _ in treasury) else
                  [{'asset_class': 'Tesouro Prefixado', 'reason': 'Sem 61 precos mensais consecutivos no titulo atual; sem preenchimento artificial.'}],
              'sources': len(sources), 'source_manifest': sources, 'cached_only': cached_only,
              'financially_validated': False}
    if scope is not None:
        report['dividend_scope'] = scope
    if blocked_assets:
        report.update(status='partial', reason='Acoes com cobertura atual incompleta bloqueadas; titulos preservados.')
    if available_dates < 60:
        report.update(status='partial', reason='Dados insuficientes: menos de 60 datas seguras; modelo nao executado.'
                      + (' Cobertura atual tambem incompleta.' if blocked_assets else ''))
    if not report['basis_validated']:
        report.update(status='partial', reason='Base temporal do alvo nao validada; modelo nao executado.')
    write_csv(snapshot / 'observations.csv', ['ticker', 'as_of', 'published_at', 'label_end', *FEATURES,
                                             'target', 'fiscal_end', 'filing_id', 'forecast_target_id', 'event_basis',
                                             'event_date_field', 'previous_window_start', 'features_available_at'], observations)
    metadata['data_availability'] = report['status']
    metadata['blocked_assets'] = blocked_assets
    metadata['cash_coverage'] = coverage
    metadata['limitations'].extend(a['ticker'] + ': ativo bloqueado por ' + a['reason'] + '.' for a in blocked_assets)
    write_json(snapshot / 'assets.json', metadata)
    write_csv(snapshot / 'returns.csv', ['date', 'inflation', *[a['ticker'] for a in assets]], returns)
    dataset = real_dataset(snapshot)
    if report['status'] == 'ready':
        try:
            _, report['model'] = dataset_forecast(dataset)
        except ValueError as error:
            report.update(status='partial', reason='Dados insuficientes para o modelo: ' + str(error))
    if report['status'] != 'ready':
        report['model'] = {'status': 'not_executed', 'reason': report['reason'],
                           'available_dates': available_dates, 'required_dates': 60}
    report['model']['forecastTargetId'] = FORECAST_TARGET['id']
    report['model']['comparableToPaymentTarget'] = False
    metadata['data_availability'] = report['status']
    write_json(snapshot / 'assets.json', metadata)
    write_json(snapshot / 'collection-report.json', report)
    if activate:
        publish_pointer(folder, snapshot, report)
    print(json.dumps({'status': report['status'], 'report': str(snapshot / 'collection-report.json'),
                      'observations': len(observations), 'observation_dates': available_dates,
                      'model': report['model'], 'pointer_updated': activate}, ensure_ascii=True, indent=2), flush=True)
    if report['status'] == 'blocked':
        raise CollectionBlocked(report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'recommender' / 'data' / 'market')
    parser.add_argument('--refresh', action='store_true', help='Reconsultar as fontes, mantendo verificacao TLS.')
    parser.add_argument('--cached-only', action='store_true', help='Usar somente raw existente, sem rede ou refresh.')
    parser.add_argument('--no-publish', action='store_true', help='Validar uma nova versao sem alterar current.json.')
    args = parser.parse_args()
    try:
        collect(args.output.resolve(), refresh=args.refresh, cached_only=args.cached_only, activate=not args.no_publish)
    except CollectionBlocked as error:
        parser.exit(2, str(error) + '\n')
