"""Offline UNIT source adapter. Composition is evidence, never model admission.

Uses dated FCA bytes, exact cached B3 identities and UNT quotes, and either a
UNIT calendar or matching component calendars. Does not download or publish.
Raises ValueError with an actionable pending reason for recognized UNITs;
returns None for other securities. Sources are registered in the caller's cache.
"""

from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import re
import zipfile

from .broad_stocks import br_day, split_price_history
from .collect_market_data import DividendHistory, month_end, normalize, shift_month
from .inventory_candidates import _cash_pages, _payments, _qualify_history
from .share_basis import b3_evidence, reconcile_payments, reconcile_prices
from .unit_composition import EVIDENCE_ROOT, UNIT_COMPOSITIONS


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _day(value):
    value = date.fromisoformat(value) if isinstance(value, str) else value
    _require(type(value) is date, 'unit_invalid_cutoff')
    return value


def _class(security):
    kind = normalize(security.get('Valor_Mobiliario', ''))
    if kind == 'acoes ordinarias':
        return 'ON'
    if kind == 'acoes preferenciais':
        code = security.get('Sigla_Classe_Acao_Preferencial', '').strip().upper()
        return ('PN' + code if code in ('A', 'B', 'C', 'D') else code) if code else 'PN'
    return None


def _ratios(text, securities, reported_components=()):
    text = normalize(text).upper()
    for item in securities:
        share = _class(item)
        ticker = item.get('Codigo_Negociacao', '')
        if share and re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', ticker):
            text = re.sub(r'\b' + re.escape(ticker) + r'\b', share, text)
    for component in reported_components:
        if component.ticker:
            text = re.sub(r'\b' + re.escape(component.ticker) + r'\b', component.share_class, text)
    text = re.sub(r'AC(?:AO|OES)\s+ORDINARIAS?', 'ON', text)
    text = re.sub(r'AC(?:AO|OES)\s+PREFERENCIA(?:L|IS)\s+CLASSE\s+([ABCD])', r'PN\1', text)
    text = re.sub(r'AC(?:AO|OES)\s+PREFERENCIA(?:L|IS)', 'PN', text)
    matches = list(re.finditer(r'\b([1-9]\d*)\s*(ON|PNA|PNB|PNC|PND|PN)\b', text))
    residue = text
    for match in reversed(matches):
        residue = residue[:match.start()] + residue[match.end():]
    _require(matches and not re.search(r'[0-9A-Z]', re.sub(r'\bE\b', '', residue)),
             'unit_composition_expression_unresolved')
    result = {match[2]: int(match[1]) for match in matches}
    _require(len(result) == len(matches), 'unit_duplicate_component_class')
    return result


def _fca(cache, year):
    key = ('unit_fca_tables', year)
    if key in cache.parsed:
        return cache.parsed[key]
    paths = sorted((cache.folder / EVIDENCE_ROOT).glob(f'fca_cia_aberta_{year}-*.zip'))
    _require(bool(paths), f'unit_historical_FCA_{year}_cache_missing')
    _require(len(paths) == 1, f'unit_historical_FCA_{year}_ambiguous')
    relative = paths[0].relative_to(cache.folder).as_posix()
    content, _ = cache.read(relative, 'unit_historical_composition')
    digest = hashlib.sha256(content).hexdigest()
    _require(paths[0].name == f'fca_cia_aberta_{year}-{digest}.zip', 'unit_FCA_hash_mismatch')
    cache.receipts[relative]['url'] = ('https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS/'
                                       f'fca_cia_aberta_{year}.zip')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        def rows(name):
            with archive.open(name) as stream:
                return list(csv.DictReader(io.TextIOWrapper(stream, encoding='latin1'), delimiter=';'))
        master = rows(f'fca_cia_aberta_{year}.csv')
        securities = rows(f'fca_cia_aberta_valor_mobiliario_{year}.csv')
    cache.parsed[key] = master, securities, relative
    return cache.parsed[key]


def _registry_evidence(record, cache, cutoff):
    for evidence in record.evidence:
        if evidence.published_at and _day(evidence.published_at) > cutoff:
            continue
        body, _ = cache.read(evidence.file, 'unit_primary_identity')
        _require(hashlib.sha256(body).hexdigest() == evidence.sha256, 'unit_primary_evidence_hash_mismatch')
        cache.receipts[evidence.file]['url'] = evidence.url


def unit_composition_for_window(row, cache, cutoff, start=None):
    """Verify the available FCA security tables for every year of the window.

    This is retrospective documentary continuity, not a point-in-time archive
    or a claim that annual observations establish a legal effective interval.
    New tickers with parseable primary FCA ratios use exactly the same gates.
    """
    cutoff = _day(cutoff)
    start = _day(start) if start is not None else shift_month(cutoff, -60)
    ticker, cvm = row.get('ticker'), row.get('cvm')
    _require(isinstance(ticker, str) and re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', ticker),
             'unit_invalid_ticker')
    _require(type(cvm) is int and cvm > 0, 'unit_exact_CVM_unavailable')
    _require(type(row.get('sector_code')) is int and 0 <= row['sector_code'] <= 10,
             'unit_exact_sector_unavailable')
    _require(row.get('identity_status') in ('official_cvm_fca', 'verified_cached_registry'),
             'unit_exact_identity_unresolved')
    record = UNIT_COMPOSITIONS.get(ticker)
    if record and record.status == 'not_a_unit':
        _registry_evidence(record, cache, cutoff)
        raise ValueError('subscription_warrant_not_unit')
    if record:
        _registry_evidence(record, cache, cutoff)
    ratios, cnpj, evidence, latest_securities = None, None, [], []
    ipo_bridge_used = False
    for year in range(start.year, cutoff.year + 1):
        if ticker == 'BRBI11' and year == 2021:
            from .brbi_window_bridge import first_year_bridge
            bridge = first_year_bridge(row, cache, cutoff, start)
            ratios, cnpj, latest_securities = bridge['ratios'], bridge['cnpj'], bridge['members']
            evidence.extend(bridge['evidence'])
            ipo_bridge_used = True
            continue
        masters, securities, relative = _fca(cache, year)
        filings = [item for item in masters if int(item['CD_CVM']) == cvm
                   and item['DT_REFER'] <= cutoff.isoformat() and item['DT_RECEB'] <= cutoff.isoformat()]
        _require(bool(filings), f'unit_historical_FCA_{year}_issuer_missing')
        latest_filing = max(filings, key=lambda item: (item['DT_REFER'], int(item['VERSAO'])))
        _require(any(item['ID_Documento'] == latest_filing['ID_DOC'] for item in securities),
                 f'unit_historical_FCA_{year}_latest_security_table_missing')
        seen = []
        for filing in filings:
            members = [item for item in securities if item['ID_Documento'] == filing['ID_DOC']]
            # CVM's annual child tables omit superseded versions. They cannot
            # be used as proof that a security disappeared in that revision.
            if not members:
                continue
            units = [item for item in members if item['Valor_Mobiliario'] == 'Units'
                     and item['Codigo_Negociacao'] == ticker]
            if not units and record and record.cvm == cvm:
                units = [item for item in members if item['Valor_Mobiliario'] == 'Units'
                         and item['Codigo_Negociacao'] == '000000']
                if units:
                    _registry_evidence(record, cache, cutoff)
            _require(len(units) == 1, f'unit_historical_FCA_{year}_exact_security_missing')
            unit = units[0]
            current = _ratios(unit['Composicao_BDR_Unit'], members, record.components if record else ())
            _require(ratios is None or current == ratios, 'unit_composition_changed_within_window')
            _require(cnpj is None or cnpj == filing['CNPJ_CIA'], 'unit_historical_issuer_changed')
            _require(unit.get('CNPJ_Companhia') == filing['CNPJ_CIA'], 'unit_FCA_issuer_mismatch')
            if record:
                _require(record.cvm == cvm and record.cnpj == filing['CNPJ_CIA'], 'unit_registry_issuer_mismatch')
                _require(current == {part.share_class: part.shares for part in record.components},
                         'unit_primary_composition_disagrees')
            for field in ('Data_Inicio_Negociacao', 'Data_Inicio_Listagem'):
                _require(bool(unit.get(field)) and _day(unit[field]) <= start,
                         'unit_listing_does_not_cover_return_window')
            for field in ('Data_Fim_Negociacao', 'Data_Fim_Listagem'):
                _require(not unit.get(field) or _day(unit[field]) > cutoff, 'unit_listing_ended_in_window')
            ratios, cnpj = current, filing['CNPJ_CIA']
            seen.append((filing, members))
            evidence.append({'file': relative, 'sha256': cache.receipts[relative]['sha256'],
                             'filing_id': filing['ID_DOC'], 'published_at': filing['DT_RECEB'],
                             'reference_date': filing['DT_REFER'], 'year': year})
        latest = max(seen, key=lambda pair: (pair[0]['DT_REFER'], int(pair[0]['VERSAO'])))
        if year == start.year:
            _require(any(_day(item['DT_REFER']) <= start for item, _ in seen),
                     'unit_composition_window_start_unverified')
        latest_securities = latest[1]
    components = []
    for share, count in sorted(ratios.items()):
        tickers = {item['Codigo_Negociacao'] for item in latest_securities if _class(item) == share
                   and re.fullmatch(r'[A-Z][A-Z0-9]{3}\d{1,2}', item['Codigo_Negociacao'])}
        components.append({'share_class': share, 'shares': count,
                           'ticker': next(iter(tickers)) if len(tickers) == 1 else None})
    return {'ticker': ticker, 'cvm': cvm, 'cnpj': cnpj, 'components': components,
            'status': 'verified', 'window_start': start.isoformat(), 'window_end': cutoff.isoformat(),
            'evidence': evidence,
            'basis': ('primary_IPO_identity_and_annual_FCA_constant_composition_retrospective' if ipo_bridge_used
                      else 'annual_FCA_observed_constant_composition_retrospective'),
            'point_in_time_archive_verified': False}


def _prices(ticker, cache, cutoff):
    legacy = f'raw/expanded/{ticker}-prices.json'
    relative = legacy if (cache.folder / legacy).is_file() else f'raw/inventory/daily-prices/{ticker}.json'
    content, _ = cache.read(relative, 'unit_daily_prices_and_actions')
    payload = json.loads(content)['chart']['result'][0]
    days = [datetime.fromtimestamp(stamp, timezone.utc).date() for stamp in payload['timestamp']]
    _require(len(set(days)) == len(days), 'duplicate_daily_price_dates')
    _, actions = split_price_history(content, ticker, cutoff, allow_recent_actions=True)
    _require(all(_day(action['date']) <= cutoff for action in actions),
             'post_cutoff_share_action_requires_reconciliation')
    return content, actions, relative


def _resolve_components(composition, codes):
    isin_classes = {'OR': 'ON', 'PR': 'PN', 'PA': 'PNA', 'PB': 'PNB', 'PC': 'PNC', 'PD': 'PND'}
    for component in composition['components']:
        share, ticker = component['share_class'], component['ticker']
        matches = []
        for code, isin in codes.items():
            match = re.fullmatch(r'BR[A-Z0-9]{4}ACN(OR|PR|PA|PB|PC|PD)\d', isin)
            if match and isin_classes[match[1]] == share:
                matches.append(code)
        _require(len(matches) == 1 and ticker in (None, matches[0]), 'unit_exact_component_ticker_unresolved')
        ticker = matches[0]
        component.update(ticker=ticker, isin=codes[ticker])


def _component_calendar(composition, codes, rows, cache, cutoff, actions):
    histories, evidence = [], []
    start = shift_month(cutoff, -60)
    _resolve_components(composition, codes)
    for component in composition['components']:
        share, ticker = component['share_class'], component['ticker']
        original, url = _payments(ticker, cache, cutoff, identity={
            'ticker': ticker, 'cvm': composition['cvm'], 'share_class': share, 'isin': codes[ticker]})
        _, component_actions, _ = _prices(ticker, cache, cutoff)
        _require(sorted(component_actions, key=lambda a: a['date']) == sorted(actions, key=lambda a: a['date']),
                 'unit_component_share_actions_disagree')
        expected, _ = b3_evidence(rows, share)
        component_paid, component_ledger, adjustments = reconcile_payments(ticker, original, expected, actions, cutoff)
        _qualify_history(component_paid, component_ledger, original, cutoff, start)
        factors = {item['record_date']: item['normalization_multiplier'] for item in adjustments}
        groups = defaultdict(list)
        for event in original.installments:
            _require(event['record_date'] in factors, 'unit_component_cash_comparable_missing')
            groups[event['record_date'], event['payment_date'], event['label']].append(event)
        histories.append((component, original, groups, factors))
        evidence.append({'ticker': ticker, 'share_class': share, 'shares': component['shares'],
                         'payment_url': url, 'file': f'raw/expanded/{ticker}-payments.html',
                         'cash_matches': len(adjustments)})
    keys = set(histories[0][2])
    _require(all(set(groups) == keys for _, _, groups, _ in histories),
             'unit_component_right_payment_dates_or_labels_disagree')
    original = DividendHistory(cutoff, len(keys), basis='payment')
    original.installments, original.semantic_events, original.source_issues = [], [], []
    original.coverage_start = max(history.coverage_start for _, history, _, _ in histories)
    original.source_observed_at = max(history.source_observed_at for _, history, _, _ in histories)
    original.primary_declaration_matches = 0
    _require(original.coverage_start <= start, 'unit_component_calendars_do_not_cover_window')
    for index, key in enumerate(sorted(keys)):
        record, payment, label = key
        legs = [(component, event, factors[record]) for component, _, groups, factors in histories
                for event in groups[key]]
        amount = sum(component['shares'] * event['gross_per_unit'] * factor for component, event, factor in legs)
        _require(_day(record) >= date(start.year, 1, 1), 'unit_pre_window_composition_unverified')
        published = [event.get('published_at') for _, event, _ in legs]
        published = max(published) if all(published) else None
        source_row = f'unit_components:{index}'
        paid_statuses = ('pago', 'paga', 'pagos', 'pagas', 'paid', 'liquidado', 'liquidada', 'pago prescrito')
        state = ('paid_reported' if _day(payment) <= cutoff and all(
                    normalize(event.get('reported_status') or '') in paid_statuses for _, event, _ in legs)
                 else 'past_scheduled' if _day(payment) <= cutoff else 'future_scheduled')
        original.installments.append({'ticker': composition['ticker'], 'record_date': record,
            'payment_date': payment, 'gross_per_unit': amount, 'label': label, 'source_row': source_row,
            'published_at': published, 'status': 'pending_reconciliation', 'reported_status': None,
            'observed_at': original.source_observed_at, 'primary_evidence': [],
            'primary_declaration_id': None, 'component_rows': [
                {'ticker': component['ticker'], 'source_row': event['source_row'], 'shares': component['shares'],
                 'reported_per_share': event['gross_per_unit'], 'normalization_multiplier': factor}
                for component, event, factor in legs]})
        original.semantic_events.append({'record_date': record, 'payment_date': payment, 'label': label,
            'source_row': source_row, 'status': state, 'published_at': published,
            'available_as_of': published is not None and _day(published) <= cutoff})
    return original, evidence


def build_unit_candidate(row, cache, cutoff):
    """Return the inventory_candidates._one contract, None, or a pending error."""
    ticker = row.get('ticker')
    recognized = (normalize(row.get('security_type') or '') == 'units'
                  or row.get('share_class') in ('UNIT', 'UNT', 'UNITS')
                  or bool(row.get('unit_composition')) or ticker in UNIT_COMPOSITIONS)
    if not recognized:
        return None
    cutoff = _day(cutoff)
    _require(cutoff == month_end(cutoff.year, cutoff.month) and cutoff <= date.today(),
             'cutoff_must_be_past_or_current_month_end')
    record = UNIT_COMPOSITIONS.get(ticker)
    if record and record.status == 'not_a_unit':
        _require(row.get('cvm') == record.cvm, 'unit_registry_issuer_mismatch')
        _registry_evidence(record, cache, cutoff)
        return None
    checks = {}
    if cache.registration is not None:
        from .issuer_registration import registration_status
        records, receipt = cache.registration
        status = registration_status(records, row.get('cvm'), cache.registration_reference or cutoff,
                                     observed_on=datetime.fromisoformat(receipt['retrieved_at']).date())
        cache.read(receipt['file'], 'current_issuer_registration')
        cache.receipts[receipt['file']]['url'] = receipt['url']
        checks['issuer_registration'] = status
        _require(not status['registration_blocks_current_purchase'], status['status'])
    composition = unit_composition_for_window(row, cache, cutoff)
    _require(row.get('share_class') in (None, 'UNIT', 'UNT', 'UNITS'), 'unit_inventory_share_class_disagrees')
    detail = cache.json(f"raw/expanded/b3-detail-{row['cvm']}.json", 'unit_exact_identity')
    _require(str(detail.get('codeCVM', '')).isdigit() and int(detail['codeCVM']) == row['cvm'],
             'unit_cached_B3_issuer_mismatch')
    codes = {item['code']: item['isin'] for item in detail.get('otherCodes', [])}
    _require(ticker in codes and re.fullmatch(r'BR[A-Z0-9]{9}\d', codes[ticker]), 'unit_exact_ISIN_unavailable')
    _require(row.get('isin') in (None, codes[ticker]), 'unit_FCA_B3_ISIN_disagrees')
    content, actions, price_file = _prices(ticker, cache, cutoff)
    rows = _cash_pages(ticker, codes, row['cvm'], cache)
    expected, quotes = b3_evidence(rows, 'UNT')
    start = shift_month(cutoff, -60)
    _require(any(day <= start for day in expected), 'unit_independent_cash_window_start_unverified')
    prices, price_matches, mode = reconcile_prices(content, ticker, actions, quotes, cutoff)
    supplement_file = f'raw/expanded/b3-supplement-{ticker[:4]}.json'
    if actions and (cache.folder / supplement_file).is_file():
        supplement = cache.json(supplement_file, 'unit_share_actions')
        if isinstance(supplement, list):
            _require(len(supplement) == 1, 'cached_supplement_identity_ambiguous')
            supplement = supplement[0]
        for event in supplement.get('stockDividends', []):
            if event.get('isinCode') == codes[ticker] and start < br_day(event.get('lastDatePrior')) <= cutoff:
                _require(normalize(event.get('label', '')) in ('desdobramento', 'grupamento', 'bonificacao'),
                         'non_cash_corporate_action_requires_reconciliation')
    if (cache.folder / f'raw/expanded/{ticker}-payments.html').is_file():
        original, payment_url = _payments(ticker, cache, cutoff)
        calendar_evidence = []
        calendar_basis = 'reported_unit_payment_calendar'
    else:
        original, calendar_evidence = _component_calendar(composition, codes, rows, cache, cutoff, actions)
        payment_url = None
        calendar_basis = 'sum_exact_component_calendars_same_right_payment_dates'
    paid, ledger, adjustments = reconcile_payments(ticker, original, expected, actions, cutoff)
    _qualify_history(paid, ledger, original, cutoff, start)
    checks.update(identity='primary_FCA_exact_CVM_unit_cached_B3_ISIN',
        composition=composition, calendar_basis=calendar_basis, component_calendars=calendar_evidence,
        prices='61_consecutive_monthly_anchors', independent_cash_matches=len(adjustments),
        independent_price_matches=len(price_matches), independent_price_class='UNT',
        share_regime=mode, corporate_action_validation='independent_UNIT_cash_and_raw_price_regimes',
        cash_history_qualification='reported_calendar_crosschecked_not_settlement_confirmed',
        point_in_time_archive_verified=False,
        payment_states=dict(Counter(event['status'] for event in original.semantic_events)))
    latest = max((_day(action['date']) for action in actions), default=date(2014, 1, 1))
    return {'ticker': ticker, 'name': row.get('name') or detail['companyName'], 'cvm': row['cvm'],
        'identity_status': row['identity_status'],
        'share_class': 'UNIT', 'isin': codes[ticker], 'sector_code': row['sector_code'],
        'industry_classification': detail.get('industryClassification'), 'unit_composition': composition,
        'prices': prices, 'payments': paid, 'ledger': ledger,
        'independent_cash_matches': [item['record_date'] for item in adjustments],
        'independent_price_matches': price_matches, 'share_actions': actions,
        'share_normalization': {'share_basis_date': cutoff.isoformat(), 'price_source_basis': mode,
            'price_output_basis': 'cutoff_share_units_no_dividend_reinvestment', 'payments': adjustments,
            'actions': actions, 'calendar_basis': calendar_basis},
        'payment_coverage_start': start.isoformat(), 'financial_basis_start': latest.isoformat(),
        'training_since': shift_month(start, 12), 'payment_url': payment_url,
        'price_url': cache.receipts[price_file]['url'], 'price_source_file': price_file,
        'statement_spec': (row['cvm'], 'UNIT', row['sector_code']), 'checks': checks,
        'readiness_scope': 'reconciled_sources_only_financial_checks_required', 'financially_validated': False}
