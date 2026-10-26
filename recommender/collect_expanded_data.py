"""Expand the immutable paid-income snapshot; never silently replace failed data."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from .broad_stocks import PublicSources, harvest
from .collect_income_data import collect as collect_base, PAYMENT_TARGET, payment_in_window
from .collect_market_data import (ROOT, cached_source, cvm_statements, financial_features,
                                  publish_pointer, shift_month, stock_observations, write_csv, write_json)
from .datasets import FEATURES, real_dataset
from .forecast import dataset_forecast
from .current_quotes import stock_quote
from .treasury_current import collect_current


def read_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def collect(folder, *, activate=False, refresh_base=False, coupon_file=None):
    # Keep the independently reconciled Petrobras/Vale histories and strict
    # Treasury coupon checks. A failed expansion cannot change current.json.
    manual_coupon_receipt = None
    if coupon_file is not None:
        from .treasury_file_import import import_file
        manual_coupon_receipt = import_file(coupon_file, folder, apply=True)
    base, base_report = collect_base(folder, refresh=refresh_base, activate=False)
    metadata = json.loads((base / 'assets.json').read_text(encoding='utf-8'))
    cutoff = date.fromisoformat(metadata['reference_date'])
    catalogue = json.loads((folder / 'catalog.json').read_text(encoding='utf-8'))
    candidates, excluded, sources = harvest(folder, catalogue, cutoff)
    specs = {r['ticker']: (r['cvm'], r['share_class'], r['sector_code']) for r in candidates}
    statements, financial_audit, cvm_sources = [], [], []
    if specs:
        for year in range(2015, date.today().year):
            url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip'
            content = cached_source(url, f'cvm-dfp-{year}.zip', folder / 'raw', cvm_sources, False)
            statements.extend(cvm_statements(content, year, specs=specs, diagnostics=financial_audit))
            print(json.dumps({'financial_year': year, 'usable_statements': len(statements)}), flush=True)
    returns = read_rows(base / 'returns.csv')
    cash = read_rows(base / 'cashflows.csv')
    observations = read_rows(base / 'observations.csv')
    ledger = json.loads((base / 'payment-ledger.json').read_text(encoding='utf-8'))
    assets, additions = metadata['assets'], []
    current_offers = collect_current(folder)
    quotes = {(r['kind'], r['maturity']): r for r in current_offers['instruments']}
    bond_info = {r['ticker']: r for r in metadata.get('bonds', [])}
    extra_sources = PublicSources(folder / 'raw/expanded')
    for asset in assets:
        if asset['ticker'] in ('PETR4', 'VALE3'):
            asset['issuer_id'] = 'CVM:' + str(9512 if asset['ticker'] == 'PETR4' else 4170)
            ticker = asset['ticker']
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}.SA?range=10y&interval=1d&events=div%2Csplits'
            try:
                asset['current_quote'] = stock_quote(extra_sources.get(url, ticker + '-prices.json'), ticker, cutoff)
            except (ValueError, OSError):
                asset['current_quote'] = {'price': None, 'as_of': cutoff.isoformat(), 'source_url': url,
                                          'purchase_available': False, 'basis': 'current_quote_unavailable'}
        elif asset['ticker'] in bond_info:
            detail = bond_info[asset['ticker']]
            quote = quotes.get((detail['kind'], detail['maturity']))
            asset['current_quote'] = ({'price': quote['buy_price'], 'as_of': quote['as_of'],
                                      'source_url': quote['source_url'],
                                      'purchase_available': quote['offered_for_purchase'] is True and quote['buy_price'] is not None,
                                      'basis': 'official_purchase_catalogue_not_executable_order',
                                      'market_status': quote['market_status'],
                                      'minimum_fraction': quote['minimum_fraction'] or .01,
                                      'minimum_investment': quote['minimum_investment']} if quote else
                                     {'price': None, 'as_of': current_offers['collected_at'],
                                      'source_url': 'https://www.tesourodireto.com.br/o/rentabilidade/investir',
                                      'purchase_available': False, 'basis': 'not_in_current_purchase_catalogue'})
    for stock in candidates:
        ticker, spec = stock['ticker'], specs[stock['ticker']]
        current = financial_features(statements, stock['payments'], cutoff, spec)
        financial_basis = date.fromisoformat(stock['financial_basis_start'])
        if current is None or date.fromisoformat(current[1]['fiscal_end']) < financial_basis:
            excluded.append({'ticker': ticker, 'reason': 'current_CVM_accounts_or_share_scale_unavailable'})
            continue
        training, coverage = stock_observations(ticker, stock['prices'], stock['payments'], statements, cutoff, spec=spec)
        training = [r | {'forecast_target_id': PAYMENT_TARGET['id']} for r in training
                    if date.fromisoformat(r['as_of']) >= stock['training_since']
                    and date.fromisoformat(r['fiscal_end']) >= financial_basis]
        by_month = defaultdict(float)
        for day, _, amount in stock['payments']:
            by_month[(day.year, day.month)] += amount
        asset = {'ticker': ticker, 'name': stock['name'], 'issuer_id': 'CVM:' + str(stock['cvm']), 'asset_class': 'acoes', 'risk_level': 2,
                 'liquidity_days': 2, 'minimum_years': 5, 'price': stock['prices'][cutoff][2],
                 'features': current[0], 'as_of': cutoff.isoformat(),
                 'financial_method': current[1].get('financial_method'),
                 'source': 'Investidor10: datas de pagamento reportadas; valores por data-com cruzados com B3; '
                           f'CVM DFP {current[1]["filing_id"]}; precos e caixa na mesma escala acionaria, '
                           'sem reinvestimento de dividendos no preco. Valores brutos.'}
        try:
            asset['current_quote'] = stock_quote((folder / 'raw/expanded' / (ticker + '-prices.json')).read_bytes(), ticker, cutoff)
        except ValueError as error:
            asset['current_quote'] = {'as_of': cutoff.isoformat(), 'price': None,
                                      'purchase_available': False, 'source_url': stock['price_url'],
                                      'basis': str(error)}
        for row, cash_row in zip(returns, cash):
            day = date.fromisoformat(row['date'])
            previous = shift_month(day, -1)
            start, end = stock['prices'][previous][2], stock['prices'][day][2]
            paid = by_month[(day.year, day.month)] / start
            row[ticker] = end / start - 1 + paid
            cash_row[ticker] = paid
        assets.append(asset)
        observations.extend(training)
        first_month = date.fromisoformat(returns[0]['date'])
        ledger.extend(r for r in stock['ledger'] if payment_in_window(date.fromisoformat(r['payment_date']), first_month, cutoff))
        additions.append({k: stock[k] for k in ('ticker', 'cvm', 'share_class', 'isin', 'sector_code', 'industry_classification', 'payment_coverage_start', 'independent_cash_matches', 'independent_price_matches',
                                               'share_actions', 'financial_basis_start', 'share_normalization', 'payment_url', 'price_url')}
                         | {'training_observations': len(training), 'training_since': stock['training_since'].isoformat(),
                            'filing_id': current[1]['filing_id'], 'fiscal_end': current[1]['fiscal_end'],
                            'inference_policy': 'pooled_cross_company_model',
                            'ticker_training_observations_available': bool(training)})
    if not additions:
        raise ValueError('Nenhuma acao adicional passou nas verificacoes. A base ativa permanece inalterada; consulte expanded-source-audit.json.')
    observations.sort(key=lambda r: (r['as_of'], r['ticker']))
    snapshot = folder / 'snapshots' / ('expanded-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    snapshot.mkdir(parents=True)
    fields = ['date', *[a['ticker'] for a in assets]]
    write_csv(snapshot / 'returns.csv', ['date', 'inflation', *fields[1:]], returns)
    write_csv(snapshot / 'cashflows.csv', fields, cash)
    write_json(snapshot / 'payment-ledger.json', ledger)
    observation_fields = ['ticker', 'as_of', 'published_at', 'label_end', *FEATURES, 'target', 'fiscal_end',
                          'filing_id', 'forecast_target_id', 'event_basis', 'event_date_field',
                          'previous_window_start', 'features_available_at']
    write_csv(snapshot / 'observations.csv', observation_fields, observations)
    all_sources = {r['url']: r for r in metadata['sources'] + sources + cvm_sources + extra_sources.manifest + current_offers['sources'] if r.get('sha256')}
    metadata.update({'assets': assets, 'source': metadata['source'] + ' Universo ampliado: cadastro/eventos B3, '
                      'pagamentos Investidor10, precos Yahoo e fundamentos oficiais CVM.',
                     'sources': list(all_sources.values()),
                     'cashflow_sources': metadata.get('cashflow_sources', []) + [r for r in sources if '-payments.html' in r['file'] or '-b3-cash-' in r['file']],
                     'stock_price_basis': 'original_brapi_rawClose_and_B3_reconciled_split_consistent_close',
                     'total_return_method': 'split_consistent_price_plus_paid_cash',
                     'cashflows_sha256': hashlib.sha256((snapshot / 'cashflows.csv').read_bytes()).hexdigest(),
                     'payment_ledger_sha256': hashlib.sha256((snapshot / 'payment-ledger.json').read_bytes()).hexdigest(),
                     'collected_at': datetime.now(timezone.utc).isoformat(), 'expanded_stocks': additions,
                     'current_treasury_offers': current_offers['instruments'],
                     'collector_stocks': ['PETR4', 'VALE3', *specs],
                     'data_availability': 'partial', 'selection_reference': 'dividendos-br: payment continuity and income ranking'})
    metadata['limitations'][0] = ('Varredura do catalogo publico, com exclusoes documentadas por ativo; apenas acoes '
                                  'com precos, caixa e fundamentos reconciliados entram na simulacao. '
                                  'Permanece vies de disponibilidade e sobrevivencia, nao cobertura integral garantida.')
    metadata['limitations'].extend([
        'A brapi gratuita bloqueou historico longo e dividendos fora do sandbox. A expansao usa fontes publicas independentes, sem acesso premium.',
        'Investidor10 fornece datas historicas de pagamento, nao extratos bancarios. Valores de direitos maduros sao cruzados com B3; isso nao certifica integralidade ou correcao de toda a base.',
        'Desdobramentos, grupamentos e bonus sao normalizados para unidades da data-base somente quando precos e pagamentos reconciliam com a B3 em cada regime usado. Eventos nao conciliados, cisoes ou distribuicoes de outros ativos permanecem excluidos.',
        'Cotacoes usam escala consistente de acoes, sem reinvestimento de dividendos. Caixa e normalizado pelos eventos reportados e mantem datas de pagamento, nao datas ex. Demonstracoes anteriores ao ultimo evento de escala nao fornecem EPS ao treinamento.',
        'O XGBoost e um modelo agrupado entre empresas. Ativos com poucos ou nenhum exemplo proprio podem receber inferencia a partir dos demais emissores; erros por ticker so estao disponiveis quando ha observacoes de teste daquele ticker. Incerteza sem calibracao propria usa residuos individuais agrupados, nunca erro zero.',
        'CVM DFP anual: nao representa resultado trimestral atualizado. Indicadores possuem data fiscal e data de publicacao documentadas; contas ausentes nao recebem zero.',
        'O prazo de dois dias das acoes indica liquidacao operacional, nao garantia de venda imediata ou ausencia de impacto de mercado. Nao ha roteamento de ordens nem certificacao de liquidez individual.',
        'Ofertas atuais do Tesouro sao catalogadas separadamente. Titulos historicos ausentes da oferta atual nao recebem quantidade indicativa de nova compra; nao se inventa historico proprio para vencimentos recentes. Cenarios continuam usando a data-base mensal documentada.',
    ])
    write_json(snapshot / 'assets.json', metadata)
    data = real_dataset(snapshot)
    _, metrics = dataset_forecast(data)
    report = {**base_report, 'status': 'partial', 'snapshot_id': snapshot.name,
              'assets': [a['ticker'] for a in assets], 'observations': len(observations),
              'observation_dates': len({r['as_of'] for r in observations}), 'model': metrics,
              'payment_events': sum(r['status'] == 'included' for r in ledger),
              'expanded_stocks': additions, 'excluded_stocks': sorted(excluded, key=lambda r: r['ticker']),
              'exclusion_counts': dict(Counter(r['reason'] for r in excluded)),
              'source_manifest': metadata['sources'], 'base_snapshot': base.name,
              'manual_coupon_import': manual_coupon_receipt,
              'financially_validated': False}
    write_json(snapshot / 'collection-report.json', report)
    write_json(snapshot / 'financial-account-audit.json', financial_audit)
    if activate:
        publish_pointer(folder, snapshot, report)
    print(json.dumps({'snapshot': str(snapshot), 'assets': len(assets), 'added_stocks': len(additions),
                      'exclusion_counts': report['exclusion_counts'], 'published': activate, 'model': metrics}, indent=2), flush=True)
    return snapshot, report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--refresh-base', action='store_true')
    parser.add_argument('--coupon-file', type=Path)
    args = parser.parse_args()
    collect(args.output.resolve(), activate=args.publish, refresh_base=args.refresh_base, coupon_file=args.coupon_file)
