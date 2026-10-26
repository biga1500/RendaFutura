"""Offline, exact-identity issuer evidence for MTRE3, ARML3 and RDOR3.

No core imports call this provider yet. The CLI replays the real candidate and
financial functions with only the two issuer-provider entry points patched in
its own process. All 61 monthly model anchors must agree in date and price;
other daily conflicts remain in the audit. Nothing is published.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit
import zipfile

from . import pending_missing_b as research
from .collect_market_data import month_end, shift_month


CUTOFF = date(2026, 9, 30)
CACHE = 'raw/primary-research/all-pending/missing-b'
PROVIDER = 'official_issuer_calendar_and_Enfoque_native_unadjusted'
SCRIPT_URL = 'https://cdn.mziq.com/stockinfo-viewer/main/latest/js/main.js'
SCRIPT = ('19ed56d6b05a5f1f722cb304cbadf890ce05fb437426822bcbe08db62d3d62a0',
          '450cba0fb9c1e63599413f982045a773ace2be971e63dacd7156bddd33f65516')
FCA_PINS = {
    2021: '65ba4b0225e9b2106901cdf348215933be8affba79925fc71361e2f6818c7999',
    2022: 'b45e655c6ffae0b40ddb0425e27cbcca7d2478867f542d78c1aa034293eeb77a',
    2023: '1df320e608f685985ad142d79f36218f26b8426e4f6a8e43ba4e836666c02dd3',
    2024: '2b9a6a7631120b78c99d1dbf52cb2f0083d03bf9e87cae869864fd7685444fab',
    2025: 'd3bf01b0f751aa001fba318adad7b8fb8f2d7cc84a4c5b03741b9e8ef9cadf37',
    2026: 'a9ad50bf6a2f0b20fde81eeef2cee7c6108e4a7571d177922b42803a0642483b',
}
TARGETS = {
    'MTRE3': {
        'identity': (24902, 'ON', 'BRMTREACNOR6'), 'domain': 'mitrerealty.com.br',
        'marker': 'mitre', 'stockinfo_id': '183c879a-4756-11ea-97b3-0aed8756bb2a',
        'identity_sha': 'fe53519f58259513a03b9c20f1684625bf469c8b6dd138e9413032f58d03dd2c',
        'calendar_url': 'https://ri.mitrerealty.com.br/informacoes-aos-investidores/dividendos-2/',
        'calendar': ('50ca57c96e09919dde6fbffe37da6a49e1558bd3ada712df0d17f5c77fda940e',
                     '02a38a784f0bb2632e01d095b4011d71418f0ced70f41ff659bd217f78d840b9'),
        'configuration_url': 'https://ri.mitrerealty.com.br/informacoes-aos-investidores/cotacoes-e-graficos/',
        'configuration': ('a68b06fedf5db59c5fd2e27dfdc5d319c40e748614d96c9156e942b04e601f22',
                          'db683a66a2b180f857b3136d9c4c65b8bc67282cf2badcf58ff4dbed88d88a1c'),
        'quotes': ('db83a3511bb17f915560c7295f2840c4019a49f323836803ef697bc48bf181e7',
                   '00cfdd91f22e736a0957f272f266722ba742f6e42d0b242d6a484092b6527621'),
        'cash_rows': 40, 'record_column': 2, 'record_header': 'base de pagamento',
    },
    'ARML3': {
        'identity': (26069, 'ON', 'BRARMLACNOR1'), 'domain': 'armac.com.br',
        'marker': 'armac', 'stockinfo_id': 'b971d9f6-a76f-11eb-bb74-12333d57aab9',
        'identity_sha': 'e8bf3ff62c37bfb3b2d64ff57f511378fca7767d844e3dbc0fad96e885e12e97',
        'calendar_url': 'https://ri.armac.com.br/informacoes-financeiras/dividendos-e-jcp-2/',
        'calendar': ('0c5bbcdb6124da3dbfc2c45f3cbf52479a9c7a7d16e1c298272ca52dd6dc7f81',
                     '32e8a78d1ff62840794b1e550ab0ebb21ed2225706d7b04d075c62081e532ca5'),
        'configuration_url': 'https://ri.armac.com.br/servicos-aos-investidores/cotacoes-e-graficos/',
        'configuration': ('99f6f8c528a94d03da02fa8fb0a496a86caf5c63520a9220756e59dac0ed2fc7',
                          'cf53c730ad1e9f32df1f1f56875ad8f067081315de835fb07aa8880a58f6aecb'),
        'quotes': ('63d1f34512ba87c915570b1aa6629e17b7eb6fc7e26988de88dcfbe114a4f20f',
                   '25571916e37973d3c80f7afaf5e6c4c7626c4e4c4143821e5a7dcbb88476e904'),
        'cash_rows': 22, 'record_column': 2, 'record_header': 'data base',
    },
    'RDOR3': {
        'identity': (24821, 'ON', 'BRRDORACNOR8'), 'domain': 'rededorsaoluiz.com.br',
        'marker': 'rede', 'stockinfo_id': '9c683364-2dda-11eb-a235-1247d5119027',
        'identity_sha': '18cd1bddb7fccb8c39d424f37216854834f52af31bdc70e277da583402df92be',
        'calendar_url': 'https://ri.rededorsaoluiz.com.br/informacoes-financeiras/devivendo_old/',
        'calendar': ('a02a3d6f1322df0127ac692996b8a577b807342f7178469aef8605705617120e',
                     'e239cda795fd31344da3ae4dea8894d18a36babbfd57cacea72df44a9c46f227'),
        'configuration_url': 'https://ri.rededorsaoluiz.com.br/informacoes-financeiras/cotacoes-e-graficos/',
        'configuration': ('43580316f8a8e955b6e53b1d0f101cd58da64d1cefa8d4f1a32bb565981c3aad',
                          '5ed827e768e51c28fe7b5560396b077172865883e968bb63d795f921f7e99347'),
        'quotes': ('753bd83702bb9e9c695e00a89e1d44ccc73fa241e33f1fe630c4f426a740e74e',
                   '5855e5c8f65ec44185b3bbd0ca1e8f27ef6a21c80b0a11043c82a85bc4483d75'),
        'cash_rows': 25, 'record_column': 5, 'record_header': 'data de corte',
    },
}


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _read(root, relative, digest):
    path = root / relative
    _require(path.resolve().is_relative_to(root) and path.is_file()
             and all(not node.is_symlink() and not getattr(node, 'is_junction', lambda: False)()
                     for node in (path, *path.parents) if node.is_relative_to(root)),
             'pending_b_unsafe_or_missing_original')
    _require(path.stat().st_size <= 16_000_000, 'pending_b_original_size_limit')
    body = path.read_bytes()
    _require(hashlib.sha256(body).hexdigest() == digest, 'pending_b_original_hash_changed')
    return body


def _source(root, url, pins):
    relative = CACHE + '/response-' + hashlib.sha256(url.encode()).hexdigest() + '.json'
    receipt = json.loads(_read(root, relative, pins[1]))
    _require(receipt.get('url') == url and receipt.get('sha256') == pins[0]
             and receipt.get('status') == 200 and receipt.get('error') is None
             and receipt.get('published_at') is None, 'pending_b_source_receipt_disagrees')
    _require(receipt.get('file') == CACHE + '/body-' + pins[0] + '.bin', 'pending_b_source_path_disagrees')
    body = _read(root, receipt['file'], pins[0])
    _require(receipt['bytes'] == len(body), 'pending_b_original_length_changed')
    retrieved = datetime.fromisoformat(receipt['retrieved_at'])
    _require(retrieved.utcoffset() is not None and retrieved <= datetime.now(timezone.utc),
             'pending_b_retrieval_date_invalid')
    return receipt, body


def _identity(root, identity):
    ticker = identity['ticker']
    spec = TARGETS[ticker]
    _require(tuple(identity.get(k) for k in ('cvm', 'share_class', 'isin')) == spec['identity'],
             'pending_b_exact_CVM_class_ISIN_mismatch')
    from .dividend_scope import allowed_tickers
    _require(ticker in (allowed_tickers(root) or ()), 'pending_b_outside_selected_scope')
    detail_file = f'raw/expanded/b3-detail-{identity["cvm"]}.json'
    original = _read(root, detail_file, spec['identity_sha'])
    detail = json.loads(original)
    _require(int(detail['codeCVM']) == identity['cvm']
             and [(r['code'], r['isin']) for r in detail['otherCodes']] == [(ticker, identity['isin'])],
             'pending_b_cached_exact_ISIN_binding_disagrees')
    sources = [{'file': detail_file, 'sha256': spec['identity_sha'], 'bytes': len(original),
                'url': None, 'retrieved_at': None, 'published_at': None,
                'authority': 'existing_exact_exchange_identity_cache_read_only',
                'not_independent_cash_or_quote_evidence': True}]
    proof = []
    for year, digest in FCA_PINS.items():
        filename = (f'raw/inventory/fca_cia_aberta_{year}.zip' if year == 2026 else
                    f'raw/primary-research/units/fca_cia_aberta_{year}-{digest}.zip')
        body = _read(root, filename, digest)
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            def rows(member):
                return list(csv.DictReader(io.StringIO(archive.read(member).decode('latin1')), delimiter=';'))
            masters = [r for r in rows(f'fca_cia_aberta_{year}.csv')
                       if int(r['CD_CVM']) == identity['cvm'] and r['DT_RECEB'] <= CUTOFF.isoformat()]
            _require(masters, 'pending_b_primary_FCA_identity_missing')
            master = max(masters, key=lambda r: (r['DT_REFER'], int(r['VERSAO'])))
            member = f'fca_cia_aberta_valor_mobiliario_{year}.csv'
            equity = [(index, r) for index, r in enumerate(rows(member), 2)
                      if r['ID_Documento'] == master['ID_DOC']
                      and research.normalized(r['Valor_Mobiliario']).startswith('acoes ')]
            _require(len(equity) == 1 and equity[0][1]['Codigo_Negociacao'] == ticker
                     and research.normalized(equity[0][1]['Valor_Mobiliario']) == 'acoes ordinarias'
                     and not equity[0][1]['Sigla_Classe_Acao_Preferencial'],
                     'pending_b_primary_FCA_single_ON_class_disagrees')
            general = [r for r in rows(f'fca_cia_aberta_geral_{year}.csv')
                       if r['ID_Documento'] == master['ID_DOC']]
            _require(len(general) == 1 and int(general[0]['Codigo_CVM']) == identity['cvm']
                     and general[0]['CNPJ_Companhia'] == equity[0][1]['CNPJ_Companhia']
                     and ''.join(c for c in general[0]['CNPJ_Companhia'] if c.isdigit()) == detail['cnpj'],
                     'pending_b_primary_issuer_CNPJ_binding_disagrees')
            if year == 2026:
                _require(spec['domain'] in general[0]['Pagina_Web'], 'pending_b_FCA_issuer_website_disagrees')
            proof.append({'year': year, 'filing_id': master['ID_DOC'], 'received_at': master['DT_RECEB'],
                          'member': member, 'csv_line': equity[0][0], 'native_row': equity[0][1]})
        sources.append({'file': filename, 'sha256': digest, 'bytes': len(body), 'retrieved_at': None,
            'published_at': None, 'authority': 'primary_CVM_FCA',
            'url': f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{year}.zip'})
    return proof, sources


def _cash(body, source, identity):
    spec = TARGETS[identity['ticker']]
    parsed = research.inspect_html(body, source)
    _require(spec['marker'] in research.normalized(parsed['title']), 'pending_b_calendar_issuer_title_disagrees')
    header = ' '.join(' '.join(t['rows'][0]) for t in parsed['tables'] if t['rows'])
    _require(spec['record_header'] in research.normalized(header), 'pending_b_record_date_header_disagrees')
    facts = research.parse_supported_table(body, source, identity)
    _require(len(facts) == spec['cash_rows'], 'pending_b_native_calendar_row_count_changed')
    events, seen = [], set()
    for fact in facts:
        record = research.schedule_date(fact.native_cells[spec['record_column']])
        label = 'DIVIDENDO' if fact.event_type == 'dividend' else 'JCP'
        basis = ('dividend_per_share' if label == 'DIVIDENDO' else
                 'explicit_gross' if fact.amount_basis == 'gross' else 'unresolved')
        key = (label, record, fact.payment_date, fact.amount_per_share)
        _require(key not in seen, 'pending_b_duplicate_native_cash_installment')
        seen.add(key)
        events.append({**{k: identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')},
            'record_date': record, 'record_date_role': 'entitlement_record',
            'record_date_source_header': spec['record_header'],
            'payment_date': fact.payment_date, 'payment_date_role': 'payment',
            'approval_date': fact.declaration_date, 'label': label,
            'amount_tax_basis': basis, 'gross_per_share_decimal': fact.amount_per_share,
            'gross_reconstructed_from_net': False, 'native_cells': list(fact.native_cells),
            'source_locator': fact.locator, 'source_file': fact.source_file,
            'published_at': None, 'issues': []})
    return events


def _validate_calendar(identity, events, cutoff):
    """Use the same semantic requirements for these exact supported issuers."""
    spec = TARGETS.get(identity.get('ticker'))
    _require(spec and tuple(identity.get(k) for k in ('cvm', 'share_class', 'isin')) == spec['identity'],
             'pending_b_cash_exact_identity_disagrees')
    def exact_day(value):
        _require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value),
                 'pending_b_exact_readable_date_required')
        return date.fromisoformat(value)
    start = shift_month(cutoff, -60)
    previous, current, future, seen = [], [], [], set()
    for event in events:
        _require(all(event[k] == identity[k] for k in ('ticker', 'cvm', 'share_class', 'isin')),
                 'pending_b_cash_exact_identity_disagrees')
        _require(event['payment_date_role'] == 'payment' and event['record_date_role'] == 'entitlement_record',
                 'pending_b_cash_date_role_disagrees')
        payment = exact_day(event['payment_date'])
        record = exact_day(event['record_date'])
        _require(payment >= record, 'pending_b_payment_precedes_record_date')
        _require(not event['approval_date'] or exact_day(event['approval_date']) <= payment,
                 'pending_b_payment_precedes_approval')
        _require(event['label'] in ('DIVIDENDO', 'JCP')
                 and (event['amount_tax_basis'] == 'explicit_gross'
                      or (event['label'] == 'DIVIDENDO' and event['amount_tax_basis'] == 'dividend_per_share')),
                 'pending_b_cash_gross_tax_basis_unresolved')
        native = event['gross_per_share_decimal']
        _require(isinstance(native, str), 'pending_b_native_decimal_string_required')
        try:
            value = Decimal(native)
        except InvalidOperation:
            raise ValueError('pending_b_native_cash_value_unusable') from None
        _require(value.is_finite() and value > 0 and not event['gross_reconstructed_from_net']
                 and not event['issues'], 'pending_b_native_cash_value_unusable')
        key = event['label'], event['record_date'], event['payment_date'], native
        _require(key not in seen, 'pending_b_duplicate_native_cash_installment')
        seen.add(key)
        (previous if payment < start else future if payment > cutoff else current).append(event)
    _require(previous and current, 'pending_b_pre_window_or_in_window_cash_missing')
    return {'pre_window_cash_rows': len(previous), 'window_cash_rows': len(current),
            'future_cash_rows': len(future)}


def _price_source(root, ticker):
    legacy = f'raw/expanded/{ticker}-prices.json'
    relative = legacy if (root / legacy).is_file() else f'raw/inventory/daily-prices/{ticker}.json'
    path = root / relative
    _require(path.resolve().is_relative_to(root) and path.is_file() and not path.is_symlink(),
             'pending_b_Yahoo_cache_missing')
    content = path.read_bytes()
    return content, {'file': relative, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content),
        'url': f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval=1d&events=div%2Csplits',
        'retrieved_at': None, 'published_at': None, 'authority': 'existing_comparison_price_cache',
        'original_acquisition_receipt_not_reconstructed': True}


def _monthly_quotes(quotes, cutoff):
    """Select the last native observation in every required calendar month."""
    required = sorted(shift_month(cutoff, -offset) for offset in range(61))
    monthly = {}
    for day, value in sorted(quotes.items()):
        month = month_end(day.year, day.month)
        if month in required and day <= cutoff:
            monthly[month] = (day, value)
    _require(set(monthly) == set(required), 'pending_b_independent_quotes_less_than_61_months')
    return {monthly[month][0]: monthly[month][1] for month in required}


def _raw_window(root, identity, quotes, cutoff, class_proof):
    from .broad_stocks import split_price_history
    content, source = _price_source(root, identity['ticker'])
    model_prices, actions = split_price_history(content, identity['ticker'], cutoff, allow_recent_actions=True)
    payload = json.loads(content)['chart']['result'][0]
    start = shift_month(cutoff, -60)
    audit_start = start.replace(day=1)
    daily = {datetime.fromtimestamp(stamp, timezone.utc).date(): value
             for stamp, value in zip(payload['timestamp'], payload['indicators']['quote'][0]['close'])
             if value is not None and audit_start <= datetime.fromtimestamp(stamp, timezone.utc).date() <= cutoff}
    independent = {month_end(day.year, day.month): (day, value)
                   for day, value in _monthly_quotes(quotes, cutoff).items()}
    monthly_checks = []
    for month in sorted(shift_month(cutoff, -offset) for offset in range(61)):
        observed, value = model_prices[month][:2]
        other_day, other = independent[month]
        monthly_checks.append({'month': month.isoformat(), 'comparison_date': observed.isoformat(),
            'issuer_native_date': other_day.isoformat(), 'comparison_close': value, 'issuer_native_close': other,
            'date_matches': observed == other_day,
            'price_matches': math.isclose(value, other, rel_tol=.006, abs_tol=.015)})
    monthly_failures = [row for row in monthly_checks if not row['date_matches'] or not row['price_matches']]
    failures = []
    for day, value in sorted(daily.items()):
        other = quotes.get(day)
        if other is None or not math.isclose(value, other, rel_tol=.006, abs_tol=.015):
            failures.append({'date': day.isoformat(), 'comparison_close': value, 'issuer_native_close': other,
                             'difference': value - other if other is not None else None})
    unmatched = sorted(day.isoformat() for day in quotes if audit_start <= day <= cutoff and day not in daily)
    worst = sorted((r for r in failures if r['difference'] is not None), key=lambda r: abs(r['difference']), reverse=True)
    anchor_days = {row['comparison_date'] for row in monthly_checks}
    anchor_days.update(row['issuer_native_date'] for row in monthly_checks)
    non_model_disagreements = [row for row in failures if row['date'] not in anchor_days]
    non_model_disagreements.extend({'date': day, 'comparison_close': None,
        'issuer_native_close': quotes[date.fromisoformat(day)], 'difference': None}
        for day in unmatched if day not in anchor_days)
    _require([row['year'] for row in class_proof] == list(range(2021, 2027)),
             'pending_b_primary_FCA_class_window_incomplete')
    return {'actions': actions, 'sources': [source], 'blocked': bool(actions or monthly_failures),
        'reason': ('primary_action_documents_required' if actions else
                   'model_monthly_inputs_disagree' if monthly_failures else None),
        'validation_scope': 'all_61_actual_monthly_model_inputs_dates_and_native_raw_prices',
        'monthly_selection_rule': 'last_observed_trading_day_of_each_calendar_month_independently_per_source',
        'model_monthly_inputs_agree61': len(monthly_checks) == 61 and not monthly_failures,
        'model_monthly_input_count': len(monthly_checks), 'model_monthly_inputs': monthly_checks,
        'model_monthly_mismatches': monthly_failures,
        'other_daily_disagreements_retained_not_used': non_model_disagreements,
        'daily_disagreements': failures,
        'raw_full_window_agrees': not failures and not unmatched and bool(daily),
        'compared_days': len(daily), 'mismatch_days': len(failures), 'unmatched_independent_days': unmatched,
        'first_mismatches': failures[:5], 'worst_mismatches': worst[:5],
        'observed_action_events': actions,
        'action_observation_basis': 'split_events_in_original_comparison_cache_no_events_added_or_removed',
        'FCA_exact_security_class_years_verified': [row['year'] for row in class_proof],
        'FCA_snapshot_not_claimed_to_be_exhaustive_action_history': True,
        'core_tolerances_unchanged': {'relative': .006, 'absolute_BRL': .015},
        'price_or_action_values_modified': False, 'empty_actions_inferred_from_price_adjustment': False}


def inspect_evidence(folder, identity, cutoff):
    """Reparse pinned originals; retain incomplete evidence as diagnostics."""
    if identity.get('ticker') not in TARGETS or cutoff != CUTOFF:
        return None
    root, ticker = Path(folder).resolve(), identity['ticker']
    spec = TARGETS[ticker]
    class_proof, sources = _identity(root, identity)
    calendar_source, calendar_body = _source(root, spec['calendar_url'], spec['calendar'])
    config_source, config_body = _source(root, spec['configuration_url'], spec['configuration'])
    script_source, script_body = _source(root, SCRIPT_URL, SCRIPT)
    _require(spec['marker'] in research.normalized(research.inspect_html(config_body, config_source)['title']),
             'pending_b_widget_issuer_title_disagrees')
    url, proof = research.widget_request(config_body, config_source, script_body, identity)
    _require(urlsplit(url).path == f'/mzstockinfo/{spec["stockinfo_id"]}/history',
             'pending_b_published_stockinfo_identifier_disagrees')
    quote_source, quote_body = _source(root, url, spec['quotes'])
    _require(all(quote_source['discovery'].get(k) == v for k, v in proof.items()),
             'pending_b_quote_discovery_proof_disagrees')
    monthly = research.parse_widget_history(quote_body, quote_source, identity)
    _require(monthly['has_61_consecutive_months'], 'pending_b_independent_quotes_less_than_61_months')
    rows = json.loads(quote_body)['data']['tickers'][0]['history']
    quotes = {date.fromisoformat(row['date']): row['closed'] for row in rows}
    _require(len(quotes) == len(rows), 'pending_b_duplicate_native_quote_date')
    events = _cash(calendar_body, calendar_source, identity)
    sources.extend([calendar_source, config_source, script_source, quote_source])
    expected = defaultdict(Decimal)
    for event in events:
        expected[date.fromisoformat(event['record_date'])] += Decimal(event['gross_per_share_decimal'])
    start = shift_month(cutoff, -60)
    blockers = []
    if min(expected) > start:
        blockers.append('pending_b_cash_does_not_cover_window_start')
    if any(event['payment_date'] is None for event in events):
        blockers.append('pending_b_payment_date_unresolved')
    if any(event['amount_tax_basis'] == 'unresolved' for event in events):
        blockers.append('pending_b_cash_gross_tax_basis_unresolved')
    calendar_checks = _validate_calendar(identity, events, cutoff) if not blockers else {}
    monthly_quotes = _monthly_quotes(quotes, cutoff)
    actions = _raw_window(root, identity, quotes, cutoff, class_proof)
    return {'provider': PROVIDER, 'expected_cash': {day: float(value) for day, value in expected.items()},
        'raw_quotes': monthly_quotes, 'sources': sources, 'calendar': {'events': events},
        'primary_actions': actions, 'blockers': blockers, 'checks': {
            'provider': PROVIDER, 'cash_rows': len(events), **calendar_checks,
            'independent_daily_prices': len(quotes),
            'independent_monthly_prices': monthly['monthly_observations'],
            'model_monthly_raw_quotes': len(monthly_quotes),
            'adjusted_parameter': False, 'published_widget_config_and_template_verified': True,
            'exact_ISIN_binding': 'pinned_existing_exact_code_exchange_cache_not_suffix_inference',
            'issuer_CVM_CNPJ_and_ON_class_history': class_proof,
            'record_dates_derived_from_ex_dates': False, 'payment_dates_inferred': False,
            'zero_cash_events_created': False, 'net_to_gross_inference': False,
            'synthetic_exchange_rows_created': False, 'settlement_verified': False,
            'point_in_time_archive_verified': False, 'calendar_blockers': blockers,
            'raw_price_full_window': actions,
            'normal_cash_price_share_and_financial_gates_required': True}}


def load_evidence(folder, identity, cutoff):
    """Production-shaped contract, with unavailable native calendars blocked."""
    data = inspect_evidence(folder, identity, cutoff)
    if data is None:
        return None
    if data['blockers']:
        raise ValueError(data['blockers'][0])
    return data


def validate_primary_actions(folder, identity, cutoff, observed_actions):
    """Require all monthly inputs and the observed exact-security action list."""
    data = inspect_evidence(folder, identity, cutoff)
    _require(data is not None, 'pending_b_primary_action_identity_unsupported')
    proof = data['primary_actions']
    _require(observed_actions == proof['actions'], 'pending_b_observed_action_list_disagrees')
    _require(not observed_actions, 'pending_b_primary_action_documents_required')
    _require(proof['model_monthly_inputs_agree61'] and proof['model_monthly_input_count'] == 61,
             'pending_b_model_monthly_inputs_disagree')
    _require(not proof['blocked'], 'pending_b_primary_action_proof_blocked')
    return proof


def replay(folder):
    """Call the actual _one and current annual gate offline in this process."""
    from unittest.mock import patch
    from . import issuer_independent_evidence as issuer
    from .collect_market_data import financial_features
    from .inventory_candidates import _Cache, _one, _payments
    from .inventory_financials import prepare_financials
    from .market_inventory import load_inventory
    root = Path(folder).resolve()
    protected = [root / 'current.json', root / 'inventory/current.json']
    before = {str(p): p.read_bytes() for p in protected}
    inventory = load_inventory(root)
    stocks = [next(r for r in inventory['stocks'] if r['ticker'] == ticker) for ticker in TARGETS]
    reports = []
    def no_network(*args, **kwargs):
        raise RuntimeError('pending_b_replay_network_forbidden')
    old_load, old_actions = issuer.load_evidence, issuer.validate_primary_actions
    with patch.object(socket.socket, 'connect', no_network), patch.object(socket.socket, 'connect_ex', no_network):
        statements, financial_audit, financial_sources = prepare_financials(root, stocks, CUTOFF)
        for identity in stocks:
            ticker = identity['ticker']
            print(json.dumps({'stage': 'isolated_replay', 'ticker': ticker}), flush=True)
            cache = _Cache(root, inventory)
            checks, candidate, evidence = {}, None, None
            item = {'ticker': ticker, 'identity': {k: identity[k] for k in ('cvm', 'share_class', 'isin')},
                    'sources_pass': False, 'financial_current_pass': False}
            try:
                evidence = inspect_evidence(root, identity, CUTOFF)
                item['evidence'] = {'checks': evidence['checks'], 'blockers': evidence['blockers'],
                    'primary_actions': evidence['primary_actions'], 'sources': evidence['sources']}
                with patch.object(issuer, 'load_evidence', load_evidence), patch.object(issuer, 'validate_primary_actions', validate_primary_actions):
                    candidate = _one(identity, cache, CUTOFF, checks)
                item.update(sources_pass=True, source_result='candidate',
                            reconciled_payment_rows=len(candidate['ledger']))
            except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                item['source_result'] = str(error)
            item['one_checks'] = checks
            native_statements = [r for r in statements if r['cvm'] == identity['cvm']
                                 and r.get('share_class') == identity['share_class'] and r['published_at'] <= CUTOFF.isoformat()]
            latest = max(native_statements, key=lambda r: (r['fiscal_end'], int(r['version'])), default=None)
            item['latest_annual_statement'] = latest
            item['financial_diagnostics'] = [r for r in financial_audit if r.get('cvm') == identity['cvm']]
            item['financial_sources'] = financial_sources
            if candidate is not None:
                current = financial_features(statements, candidate['payments'], CUTOFF,
                    (identity['cvm'], identity['share_class'], identity['sector_code']))
                item['financial_current_pass'] = current is not None and current[1]['fiscal_end'] >= candidate['financial_basis_start']
                item['financial_current'] = current
            else:
                # This diagnostic uses actual native RI payments; it never
                # becomes a reconciled candidate or a replacement calendar.
                events = evidence['calendar']['events'] if evidence else []
                usable = events and all(r['payment_date'] and r['amount_tax_basis'] != 'unresolved' for r in events)
                if usable:
                    observed = [(date.fromisoformat(r['payment_date']), date.fromisoformat(r['payment_date']),
                                 float(Decimal(r['gross_per_share_decimal']))) for r in events
                                if r['payment_date'] <= CUTOFF.isoformat()]
                    diagnostic = financial_features(statements, observed, CUTOFF,
                        (identity['cvm'], identity['share_class'], identity['sector_code']))
                    item['financial_current_on_unreconciled_native_calendar'] = diagnostic
                    item['financial_inputs_available_separately'] = diagnostic is not None
                else:
                    item['financial_inputs_available_separately'] = bool(latest and latest['profit_per_share'])
                item['financial_current_not_approved_without_reconciled_candidate'] = True
            if evidence and not evidence['blockers']:
                try:
                    payment, _ = _payments(ticker, cache, CUTOFF, identity=identity)
                    from .share_basis import reconcile_payments
                    _, ledger, _ = reconcile_payments(ticker, payment, evidence['expected_cash'], [], CUTOFF,
                                                      evidence_provider=PROVIDER)
                    item['cash_reconciliation_diagnostic'] = {'passes': True, 'ledger_rows': len(ledger)}
                except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
                    item['cash_reconciliation_diagnostic'] = {'passes': False, 'reason': str(error)}
            item['source_and_financial_current_pass'] = item['sources_pass'] and item['financial_current_pass']
            reports.append(item)
    _require(issuer.load_evidence is old_load and issuer.validate_primary_actions is old_actions,
             'pending_b_replay_patch_not_restored')
    _require(all(Path(p).read_bytes() == body for p, body in before.items()), 'pending_b_replay_pointer_changed')
    return {'provider': PROVIDER, 'cutoff': CUTOFF.isoformat(), 'targets': reports,
            'sources_and_current_financial_pass_count': sum(r['source_and_financial_current_pass'] for r in reports),
            'isolated_process_monkeypatch_only': True, 'core_modified': False, 'network_calls': 0,
            'pointers_unchanged': True, 'snapshots_or_model_data_written': False,
            'production_integration_required_by_parent': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).parent / 'data/market')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.root)
    research.immutable_json(args.output, report)
    print(json.dumps({'report_path': str(args.output.resolve()), 'results': [
        {k: r.get(k) for k in ('ticker', 'sources_pass', 'source_result', 'financial_current_pass',
          'financial_inputs_available_separately', 'cash_reconciliation_diagnostic')} for r in report['targets']]}), flush=True)


if __name__ == '__main__':
    main()
