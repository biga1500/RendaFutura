"""Attempt every inventoried stock and build an atomic, auditable model candidate."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .collect_income_data import PAYMENT_TARGET, payment_in_window
from .collect_market_data import ROOT, financial_features, publish_pointer, shift_month, stock_observations, write_csv, write_json
from .current_quotes import stock_quote
from .datasets import FEATURES, real_dataset
from .forecast import dataset_forecast
from .market_inventory import load_inventory, market_root, publish_inventory


def _read_table(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames or [], list(reader)


def prepare(folder, *, activate=False):
    from .inventory_candidates import build_candidates
    from .inventory_financials import prepare_financials
    from .dividend_scope import OUTSIDE_REASON, filter_records, scope_metadata

    folder = market_root(folder)
    inventory = load_inventory(folder)
    selection = scope_metadata(folder)
    selected_stocks = filter_records(folder, inventory['stocks'])
    selected_codes = {row['ticker'] for row in selected_stocks}
    pointer = json.loads((folder / 'current.json').read_text(encoding='utf-8'))
    base = folder / pointer['snapshot']
    real_dataset(base)
    metadata = json.loads((base / 'assets.json').read_text(encoding='utf-8'))
    previous_report = json.loads((base / 'collection-report.json').read_text(encoding='utf-8'))
    cutoff = date.fromisoformat(metadata['reference_date'])
    print(json.dumps({'stage': 'prepare_selected_stocks', 'stocks': len(selected_stocks),
                      'catalog_stocks': len(inventory['stocks']), 'reference': cutoff.isoformat()}), flush=True)
    scoped_inventory = {**inventory, 'stocks': selected_stocks}
    candidates, pending, candidate_sources = build_candidates(folder, scoped_inventory, cutoff)
    pending.extend({'ticker': row['ticker'], 'reason': OUTSIDE_REASON,
                    'checks': {'user_selection_id': selection['id'], 'not_a_profile_rejection': True}}
                   for row in inventory['stocks'] if row['ticker'] not in selected_codes)
    print(json.dumps({'stage': 'cash_price_reconciled', 'candidates': len(candidates), 'pending': len(pending)}), flush=True)
    statements, financial_audit, financial_sources = prepare_financials(folder, selected_stocks, cutoff)
    unit_candidates = [row for row in candidates if row['share_class'] == 'UNIT']
    if unit_candidates:
        from .unit_financials import prepare_unit_financials
        unit_statements, unit_audit, unit_sources = prepare_unit_financials(folder, unit_candidates, cutoff)
        statements.extend(unit_statements)
        financial_audit.extend(unit_audit)
        financial_sources.extend(unit_sources)
    old_tickers = {asset['ticker'] for asset in metadata['assets'] if asset['asset_class'] == 'acoes'}
    assets = [asset for asset in metadata['assets'] if asset['asset_class'] != 'acoes']
    preserved_tickers = {asset['ticker'] for asset in assets}
    return_fields, returns = _read_table(base / 'returns.csv')
    cash_fields, cash = _read_table(base / 'cashflows.csv')
    observation_fields, observations = _read_table(base / 'observations.csv')
    return_fields = [field for field in return_fields if field not in old_tickers]
    cash_fields = [field for field in cash_fields if field not in old_tickers]
    returns = [{field: row[field] for field in return_fields} for row in returns]
    cash = [{field: row[field] for field in cash_fields} for row in cash]
    observations = [row for row in observations if row.get('ticker') in preserved_tickers]
    ledger = [row for row in json.loads((base / 'payment-ledger.json').read_text(encoding='utf-8'))
              if row.get('ticker') in preserved_tickers]
    rebuilt_stocks, quote_sources = [], []
    for candidate in sorted(candidates, key=lambda row: row['ticker']):
        ticker = candidate['ticker']
        spec = candidate['cvm'], candidate['share_class'], candidate['sector_code']
        current = financial_features(statements, candidate['payments'], cutoff, spec)
        basis = date.fromisoformat(candidate['financial_basis_start'])
        if current is None or date.fromisoformat(current[1]['fiscal_end']) < basis:
            pending.append({'ticker': ticker, 'reason': 'financial_features_unavailable',
                            'checks': candidate.get('checks', {})})
            continue
        if candidate['cvm'] == 11070 and str(current[1]['filing_id']) == '155797':
            # The exact native note conflicts with reported EPS for both classes.
            # Retain source numbers for audit; neither quotient becomes a model input.
            from .wlm_financial_class_research import audit as wlm_financial_audit
            proof = wlm_financial_audit(folder, cutoff)
            financial_sources.extend(proof['sources'])
            pending.append({'ticker': ticker, 'reason': 'native_financial_eps_source_conflict',
                'checks': {**candidate.get('checks', {}), 'financial_source_conflict': {
                    'filing_id': '155797', 'scope': 'reported_EPS_vs_native_note_32',
                    'reported_ON_reconciles': proof['actual_reported_ON_EPS_reconciles_with_note'],
                    'reported_PN_reconciles': proof['actual_reported_PN_EPS_reconciles_with_note'],
                    'replacement_EPS_created': False, 'evidence_sources': proof['sources']}}})
            continue
        training, _ = stock_observations(ticker, candidate['prices'], candidate['payments'], statements, cutoff, spec=spec)
        training = [row | {'forecast_target_id': PAYMENT_TARGET['id']} for row in training
                    if date.fromisoformat(row['as_of']) >= candidate['training_since']
                    and date.fromisoformat(row['fiscal_end']) >= basis]
        asset = {'ticker': ticker, 'name': candidate['name'][:100], 'issuer_id': 'CVM:' + str(candidate['cvm']),
                 'asset_class': 'acoes', 'risk_level': 2, 'liquidity_days': 2, 'minimum_years': 5,
                 'price': candidate['prices'][cutoff][2], 'features': current[0], 'as_of': cutoff.isoformat(),
                 'financial_method': current[1].get('financial_method'),
                 'source': 'Cadastro CVM FCA; calendario historico reportado com datas e valores cruzados com '
                           'evidencia independente de caixa, precos e escala acionaria; disponibilidade retrospectiva '
                           'pela data de pagamento, sem certificar liquidacao ou arquivo point-in-time; '
                           'fundamentos anuais CVM publicados antes da data-base.'}
        try:
            raw = folder / candidate.get('current_quote_source_file',
                candidate.get('price_source_file', 'raw/expanded/' + ticker + '-prices.json'))
            content = raw.read_bytes()
            asset['current_quote'] = stock_quote(content, ticker, cutoff)
            if not asset['current_quote']['purchase_available']:
                from .current_quote_sources import load_snapshot
                quote, receipt = load_snapshot(folder, ticker, cutoff, content)
                if quote and quote['purchase_available']:
                    asset['current_quote'] = quote
                    quote_sources.append(receipt)
        except (OSError, ValueError, KeyError):
            asset['current_quote'] = {'price': None, 'as_of': inventory['as_of'], 'source_url': candidate['price_url'],
                                      'purchase_available': False, 'basis': 'quote_or_share_basis_not_confirmed'}
        monthly = defaultdict(float)
        for day, _, value in candidate['payments']:
            monthly[day.isoformat()[:7]] += value
        for total_row, cash_row in zip(returns, cash):
            day = date.fromisoformat(total_row['date'])
            previous = shift_month(day, -1)
            start, end = candidate['prices'][previous][2], candidate['prices'][day][2]
            paid = monthly[day.isoformat()[:7]] / start
            total_row[ticker] = end / start - 1 + paid
            cash_row[ticker] = paid
        assets.append(asset)
        return_fields.append(ticker)
        cash_fields.append(ticker)
        observations.extend(training)
        first = date.fromisoformat(returns[0]['date'])
        ledger.extend(row for row in candidate['ledger'] if payment_in_window(date.fromisoformat(row['payment_date']), first, cutoff))
        rebuilt = {key: candidate.get(key) for key in (
            'ticker', 'cvm', 'share_class', 'isin', 'sector_code', 'industry_classification',
            'payment_coverage_start', 'independent_cash_matches', 'independent_price_matches',
            'share_actions', 'financial_basis_start', 'share_normalization', 'payment_url', 'price_url', 'price_source_file',
            'availability_basis', 'point_in_time_archive_verified', 'cash_settlement_verified', 'current_quote_source_file')}
        rebuilt.update(training_observations=len(training), training_since=candidate['training_since'].isoformat(),
                        filing_id=current[1]['filing_id'], fiscal_end=current[1]['fiscal_end'],
                        inference_policy='pooled_cross_company_model')
        rebuilt_stocks.append(rebuilt)
    ready = {asset['ticker'] for asset in assets if asset['asset_class'] == 'acoes'}
    from .cash_calendar_research import native_calendar_metadata, calendar_conflict_engine_warnings
    native_metadata = native_calendar_metadata(candidates, ready, candidate_sources)
    added = sorted(ready - old_tickers)
    removed = sorted(old_tickers - ready)
    revalidated = sorted(ready & old_tickers)
    reasons = defaultdict(list)
    reviews = defaultdict(list)
    source_checks = {row['ticker']: row.get('checks', {}) for row in [*candidates, *pending]}
    for row in pending:
        reviews[row['ticker']].append(row['reason'])
        if row['ticker'] not in ready:
            reasons[row['ticker']].append(row['reason'])
    annual = {row['ticker']: row for row in financial_audit if row.get('kind') == 'candidate'}
    annual_values = {}
    financial_source_urls = {source['id']: source['url'] for source in financial_sources
                             if source.get('id') and source.get('url')}
    for record in statements:
        key = record['cvm'], record['share_class']
        previous = annual_values.get(key)
        if previous is None or (record['fiscal_end'], int(record['version'])) > (previous['fiscal_end'], int(previous['version'])):
            annual_values[key] = record
    inventory_pointer = json.loads((folder / 'inventory/current.json').read_text(encoding='utf-8'))
    prepared_at = datetime.now(timezone.utc).isoformat()
    stocks = []
    for row in inventory['stocks']:
        ticker = row['ticker']
        account = annual.get(ticker, {})
        values = annual_values.get((row.get('cvm'), account.get('share_class', row.get('share_class'))), {}) if account.get('status') == 'included' else {}
        modeled = next((asset for asset in assets if asset['ticker'] == ticker), None)
        quote = modeled.get('current_quote') if modeled else None
        observed_quote = row['quote']
        if quote and quote.get('snapshot_at'):
            observed_quote = {'value': quote['price'], 'unit': 'BRL', 'as_of': quote['as_of'],
                'status': 'available', 'source_url': quote['source_url'], 'basis': quote['basis'],
                'snapshot_at': quote['snapshot_at'], 'retrieved_at': quote['retrieved_at'],
                'last_trade_at': quote['last_trade_at'], 'last_trade_source_url': quote['last_trade_source_url']}
        stocks.append({'ticker': ticker, 'name': row['name'], 'model_ready': ticker in ready,
                       'reason_codes': sorted(set(reasons[ticker])) if ticker not in ready else [],
                       'source_review_reason_codes': sorted(set(reviews[ticker])),
                       'source_checks': source_checks.get(ticker, {}),
                       'preparation_basis': 'rebuilt_from_reconciled_sources' if ticker in ready else 'pending',
                       'annual_accounts': {'available': bool(values), 'accounts_scope': values.get('accounts_scope'), 'fiscal_end': values.get('fiscal_end'),
                            'published_at': values.get('published_at'), 'filing_id': values.get('filing_id'),
                            'earnings_per_share': values.get('profit_per_share'), 'free_cash_per_share': values.get('free_cash_per_share'),
                            'liabilities_to_assets': values.get('debt_ratio'), 'source_url': financial_source_urls.get(values.get('source_ref'))},
                       'quote': observed_quote, 'financial_period': row['fundamentals']['fields'].get('earnings_per_share', {}).get('period_end'),
                       'checks': {'identity': row['identity_status'], 'payments': row['payments']['status'],
                                  'quote': row['quote']['status'], 'fundamentals': row['fundamentals']['status']}})
    assert ready.issubset({row['ticker'] for row in stocks})
    assert all(row['model_ready'] or row['reason_codes'] for row in stocks)
    source_policy = {
        'data_quality_policy': 'RF-CALENDAR-RECONCILED-1',
        'source_policy_version': 'RF-CALENDAR-RECONCILED-1',
        'cashflow_basis': 'reported_historical_cash_reconciled',
        'availability_basis': 'payment_date_retrospective_observation_not_publication',
        'point_in_time_archive_verified': False, 'cash_settlement_verified': False,
        'source_review_policy': 'All stocks, including prior snapshot members, require complete reported historical '
            'payment dates and amounts, independent cash/price/share-scale reconciliation and the same annual '
            'financial_features and stock_observations window checks. Missing publication dates remain unknown; '
            'retrospective payment-date availability does not certify cash settlement or a point-in-time archive.',
    }
    preparation = {'as_of': inventory['as_of'], 'prepared_at': prepared_at,
                   **source_policy,
                   'inventory_sha256': inventory_pointer['sha256'], 'stocks': stocks,
                   'evaluated_stocks': len(stocks), 'ready_stocks': len(ready),
                   'annual_accounts_stocks': sum(row['annual_accounts']['available'] for row in stocks),
                   'pending_stocks': len(stocks) - len(ready), 'added_stocks': added,
                   'removed_stocks': removed, 'revalidated_stocks': revalidated,
                   'data_pending_is_profile_rejection': False, 'missing_data_imputed': False}
    if selection is not None:
        unmatched = sorted(set(selection['selectedTickers']) - {row['ticker'] for row in inventory['stocks']})
        preparation['dividend_yield_selection'] = {**selection,
            'matchedCatalogTickersCount': len(selected_codes), 'unmatchedTickers': unmatched,
            'outsideScopeStocks': len(inventory['stocks']) - len(selected_codes),
            'selectedReadyStocks': len(ready), 'selectedPendingStocks': len(selected_codes - ready)}
    snapshot = folder / 'snapshots' / ('universe-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    snapshot.mkdir(parents=True)
    write_csv(snapshot / 'returns.csv', return_fields, returns)
    write_csv(snapshot / 'cashflows.csv', cash_fields, cash)
    write_json(snapshot / 'payment-ledger.json', ledger)
    observations.sort(key=lambda row: (row['as_of'], row['ticker']))
    observation_fields = list(dict.fromkeys([*observation_fields, 'ticker', 'as_of', 'published_at', 'label_end', *FEATURES,
        'target', 'fiscal_end', 'filing_id', 'forecast_target_id', 'event_basis', 'event_date_field',
        'previous_window_start', 'features_available_at']))
    write_csv(snapshot / 'observations.csv', observation_fields, observations)
    sources = {(entry.get('file'), entry['sha256'], entry['url'], entry.get('retrieved_at')): entry
               for entry in metadata['sources'] + candidate_sources + financial_sources + quote_sources
               if entry.get('sha256') and entry.get('url')}
    for source in native_metadata['cash_calendar_evidence_receipts']:
        sources[source['file'], source['sha256'], source.get('url'), source.get('retrieved_at')] = source
    calendar_qualifications = [
        {'ticker': row['ticker'], 'record_date': row.get('record_date'),
         'payment_date': row['payment_date'], 'date_basis': row['payment_date_basis'],
         'full_settlement_on_date_verified': False}
        for row in ledger if row.get('payment_date_basis') == 'explicit_payment_start_date_not_settlement']
    metadata.update(assets=assets, stock_universe_preparation=preparation, collected_at=prepared_at,
                    **source_policy, forecast_target=PAYMENT_TARGET,
                    cash_calendar_date_qualifications=calendar_qualifications,
                    **native_metadata,
                    sources=list(sources.values()), collector_stocks=[row['ticker'] for row in stocks],
                    expanded_stocks=rebuilt_stocks,
                    cashflows_sha256=hashlib.sha256((snapshot / 'cashflows.csv').read_bytes()).hexdigest(),
                    payment_ledger_sha256=hashlib.sha256((snapshot / 'payment-ledger.json').read_bytes()).hexdigest())
    calendar_conflict_engine_warnings(metadata, ready)
    metadata['source'] = ('Acoes reconstruidas de calendarios historicos reportados e cruzados com evidencia '
                          'independente de caixa, precos e escala; fundamentos anuais CVM; disponibilidade '
                          'retrospectiva nao certifica liquidacao nem arquivo point-in-time. Titulos e series '
                          'oficiais nao acoes preservados do snapshot anterior. Dados pendentes nao recebem alocacao.')
    write_json(snapshot / 'assets.json', metadata)
    write_json(snapshot / 'stock-universe-preparation.json', preparation)
    write_json(snapshot / 'financial-account-audit.json', financial_audit)
    write_json(snapshot / 'candidate-source-audit.json', {'candidates': [row['ticker'] for row in candidates],
        'pending': pending, 'sources': candidate_sources, 'policy': preparation['source_review_policy']})
    data = real_dataset(snapshot)
    _, metrics = dataset_forecast(data)
    excluded = [{'ticker': row['ticker'], 'reason': code} for row in stocks for code in row['reason_codes']]
    report = {**previous_report, 'snapshot_id': snapshot.name, 'base_snapshot': base.name, 'status': 'partial',
              'assets': [asset['ticker'] for asset in assets], 'observations': len(observations),
              'model': metrics, 'stock_universe_preparation': preparation,
              'forecast_target': PAYMENT_TARGET,
              'added_stocks': added, 'removed_stocks': removed, 'revalidated_stocks': revalidated,
              'payment_events': sum(row['status'] == 'included' for row in ledger),
              'expanded_stocks': metadata['expanded_stocks'], 'excluded_stocks': excluded,
              'exclusion_counts': dict(Counter(row['reason'] for row in excluded)),
              'source_manifest': metadata['sources'], 'financially_validated': False}
    write_json(snapshot / 'collection-report.json', report)
    if activate:
        activate_candidate(folder, snapshot)
    print(json.dumps({'snapshot': str(snapshot), 'evaluated_stocks': len(stocks), 'ready_stocks': len(ready),
                      'added_stocks': added, 'removed_stocks': removed, 'revalidated_stocks': revalidated,
                      'pending_stocks': len(stocks) - len(ready), 'published': activate}), flush=True)
    return snapshot, report


def activate_candidate(folder, snapshot):
    folder, snapshot = market_root(folder), Path(snapshot).resolve()
    if snapshot.parent != folder / 'snapshots':
        raise ValueError('Candidato fora da pasta de snapshots.')
    real_dataset(snapshot)
    report = json.loads((snapshot / 'collection-report.json').read_text(encoding='utf-8'))
    current = json.loads((folder / 'current.json').read_text(encoding='utf-8'))
    if Path(current['snapshot']).name != report['base_snapshot']:
        raise ValueError('A base ativa mudou desde a preparacao; nenhum ponteiro foi substituido.')
    inventory = load_inventory(folder)
    preparation = report['stock_universe_preparation']
    candidate_metadata = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    if any(row.get('cvm') == 11070 and str(row.get('filing_id')) == '155797'
           for row in candidate_metadata.get('expanded_stocks', [])):
        raise ValueError('O lucro por acao da WLM diverge da nota nativa; candidato nao publicado.')
    from .dividend_scope import allowed_tickers, scope_metadata
    allowed = allowed_tickers(folder)
    selection = scope_metadata(folder)
    if allowed is not None:
        selected_id = preparation.get('dividend_yield_selection', {}).get('id')
        if selected_id != selection['id'] or any(row['model_ready'] and row['ticker'] not in allowed
                                                for row in preparation['stocks']):
            raise ValueError('A selecao de dividend yield mudou; reavalie antes de publicar.')
    if {row['ticker'] for row in inventory['stocks']} != {row['ticker'] for row in preparation['stocks']}:
        raise ValueError('O universo mudou desde a preparacao; reavalie os codigos antes de publicar.')
    publish_pointer(folder, snapshot, report)
    sync_inventory(folder, inventory, preparation, snapshot.name)


def sync_inventory(folder, inventory, preparation, snapshot_name):
    by_ticker = {row['ticker']: row for row in preparation['stocks']}
    for row in inventory['stocks']:
        result = by_ticker[row['ticker']]
        row.update(model_ready=result['model_ready'], model_pending_reason=';'.join(result['reason_codes']) or None)
    inventory['counts']['model_ready_stocks'] = preparation['ready_stocks']
    inventory['model_preparation'] = {'snapshot': snapshot_name, 'evaluated_stocks': preparation['evaluated_stocks']}
    inventory['model_sample_unchanged'] = not (preparation['added_stocks'] or preparation['removed_stocks'])
    publish_inventory(folder, inventory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--activate-candidate', type=Path)
    args = parser.parse_args()
    if args.activate_candidate:
        activate_candidate(args.data_dir, args.activate_candidate)
        print(json.dumps({'published': True, 'snapshot': str(args.activate_candidate)}))
    else:
        prepare(args.data_dir, activate=args.publish)
