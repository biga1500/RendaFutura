"""Build a paid-cash snapshot; never infer a payment from an ex/record date."""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

from lxml import html
import openpyxl

from .collect_market_data import (
    ROOT, STOCKS, TREASURY_URL, DividendHistory, cached_source, cvm_statements,
    financial_features, month_end, normalize, publish_pointer, shift_month,
    stock_history, stock_observations, write_csv, write_json,
)
from .datasets import FEATURES, real_dataset
from .dividend_scope import allowed_tickers, scope_metadata
from .forecast import dataset_forecast

PETROBRAS_URL = 'https://www.investidorpetrobras.com.br/acoes-dividendos-e-dividas/dividendos-e-jcp/'
VALE_URL = 'https://www.vale.com/pt/dividendos-dividas-e-debentures'
VALE_RECONCILIATION_URL = 'https://www.vale.com/pt/w/vale-informa-sobre-deliberacoes-pelo-conselho-de-administracao'
COUPON_URL = ('https://www.tesourotransparente.gov.br/ckan/dataset/'
              'f30db6e4-6123-416c-b094-be8dfc823601/resource/'
              'b04812f4-c453-4298-a12e-0ad190998243/download/cupomjurostesourodireto.csv')
PAYMENT_START = date(2015, 1, 1)
PAYMENT_TARGET = {
    'id': 'cash_payments_12m_v2', 'event_basis': 'payment_date', 'date_field': 'paymentDate',
    'unit': 'BRL/share', 'target': 'Dividendos e JCP brutos pagos nos 12 meses seguintes.',
    'previous_cash': 'Dividendos e JCP brutos pagos nos 12 meses anteriores.',
    'regularity': 'Fracao de meses com pagamentos nos 12 meses anteriores.',
    'warning': 'Valores brutos, sem tributacao individual; nao constituem promessa de renda futura.',
    'metrics_comparable_to_rights_target': False,
}


def br_number(value):
    return float(value.replace('.', '').replace(',', '.'))


def payment_in_window(day, first_month, cutoff):
    return first_month.replace(day=1) <= day <= cutoff


def iso_timestamp(value):
    return date.fromisoformat(value[:10]) if value else None


def verified_raw_prices(content, cutoff):
    records = json.loads(content)['results'][0]['data']['historicalDataPrice']
    prices = {}
    for row in records:
        day = datetime.fromtimestamp(row['date'], timezone.utc).date()
        if day > cutoff:
            continue
        raw, adjusted = row.get('rawClose'), row.get('adjustedClose')
        if raw is None or adjusted is None or not all(math.isfinite(v) and v > 0 for v in (raw, adjusted)):
            raise ValueError('Preco bruto rawClose ausente: proibido somar pagamentos a um close de base ambigua.')
        key = month_end(day.year, day.month)
        if key not in prices or day > prices[key][0]:
            prices[key] = (day, float(adjusted), float(raw))
    if len(prices) < 61 or cutoff not in prices:
        raise ValueError('Historico de precos brutos insuficiente ou desatualizado.')
    return prices


def petrobras_payments(content, cutoff):
    sheet = openpyxl.load_workbook(io.BytesIO(content), data_only=True).worksheets[0]
    if 'PETR4' not in str(sheet.cell(2, 13).value):
        raise ValueError('Layout da planilha Petrobras mudou; coluna PETR4 nao identificada.')
    history, audit = DividendHistory(cutoff, 0), []
    seen = set()
    for number, row in enumerate(sheet.values, 1):
        if number <= 3 or not isinstance(row[12], (int, float)):
            continue
        fiscal, status, paid, gross, label = row[12], row[14], row[16], row[19], row[21]
        if not isinstance(paid, datetime):
            if fiscal >= 2015:
                raise ValueError(f'Petrobras linha {number}: data de pagamento ausente.')
            continue
        day = paid.date()
        if day < PAYMENT_START:
            continue
        record = {'ticker': 'PETR4', 'source_row': number, 'payment_date': day.isoformat(),
                  'gross_per_share': gross, 'label': label, 'reported_status': status,
                  'basis': 'primary_paid_status_and_payment_date'}
        if day > cutoff or not str(status).startswith('PAGO'):
            record['status'] = 'excluded_not_paid_by_reference'
            audit.append(record)
            continue
        if not isinstance(gross, (int, float)) or gross <= 0 or not any(k in normalize(str(label)) for k in ('div', 'jcp')):
            raise ValueError(f'Petrobras linha {number}: provento em dinheiro invalido.')
        key = (day, float(gross), str(label), str(row[15]))
        if key in seen:
            raise ValueError('Pagamento Petrobras duplicado; exige conferencia da fonte.')
        seen.add(key)
        history.append((day, day, float(gross)))
        record['status'] = 'included'
        audit.append(record)
    if len(history) < 40 or max(d for d, _, _ in history) < shift_month(cutoff, -3):
        raise ValueError('Historico Petrobras ausente ou desatualizado.')
    history.event_count = len(audit)
    return history, audit


def vale_payments(content, evidence, cutoff):
    data = json.loads(content)['results'][0]
    if data['symbol'] != 'VALE3':
        raise ValueError('Resposta de proventos nao corresponde a VALE3.')
    history, audit, seen = DividendHistory(cutoff, 0), [], set()
    for index, row in enumerate(data['data']['cashDividends']):
        if row['label'] not in ('DIVIDENDO', 'JCP'):
            continue
        day = iso_timestamp(row.get('paymentDate'))
        anchor = iso_timestamp(row.get('lastDatePrior'))
        reconciled = False
        if day is None and anchor == date(2019, 12, 26) and row['label'] == 'JCP' and row['rate'] == 1.414364369:
            # This date is stated explicitly in the issuer's 2020-07-29 release.
            text = ' '.join(html.fromstring(evidence.decode('utf8')).itertext())
            if '07 de agosto de 2020' not in text:
                raise ValueError('Evidencia primaria da reconciliacao Vale nao confere.')
            day, reconciled = date(2020, 8, 7), True
        record = {'ticker': 'VALE3', 'source_event': index, 'payment_date': day.isoformat() if day else None,
                  'gross_per_share': row['rate'], 'label': row['label'], 'provider_verified': row.get('verified', False),
                  'primary_reconciliation': reconciled, 'basis': 'reported_historical_payment_brapi_v2'}
        if day is None:
            if anchor is None or anchor >= PAYMENT_START:
                raise ValueError('Vale: pagamento sem data dentro do recorte documental.')
            record['status'] = 'excluded_before_documented_2015_scope'
        elif day > cutoff:
            record['status'] = 'excluded_future_payment'
        elif day < PAYMENT_START:
            record['status'] = 'excluded_before_documented_2015_scope'
        else:
            if row.get('paymentDateIsDeadline'):
                raise ValueError('Prazo limite de pagamento nao e data efetiva.')
            gross = float(row['rate'])
            key = (day, gross, row['label'])
            if key in seen or gross <= 0:
                raise ValueError('Vale: evento duplicado ou valor invalido.')
            seen.add(key)
            history.append((day, day, gross))
            record['status'] = 'included'
        audit.append(record)
    if len(history) < 20 or max(d for d, _, _ in history) < shift_month(cutoff, -3):
        raise ValueError('Historico Vale ausente ou desatualizado.')
    history.event_count = len(audit)
    return history, audit


def treasury_prices(content, cutoff):
    histories = defaultdict(dict)
    allowed = {'Tesouro Selic', 'Tesouro IPCA+', 'Tesouro Prefixado',
               'Tesouro Prefixado com Juros Semestrais', 'Tesouro IPCA+ com Juros Semestrais'}
    for row in csv.DictReader(io.StringIO(content.decode('utf-8-sig')), delimiter=';'):
        kind = row['Tipo Titulo']
        if kind not in allowed:
            continue
        day = datetime.strptime(row['Data Base'], '%d/%m/%Y').date()
        maturity = datetime.strptime(row['Data Vencimento'], '%d/%m/%Y').date()
        price = br_number(row['PU Venda Manha'])
        if day > cutoff or maturity <= cutoff or price <= 0:
            continue
        month = month_end(day.year, day.month)
        key = (kind, maturity)
        if month not in histories[key] or day > histories[key][month][0]:
            histories[key][month] = (day, price)
    return histories


def treasury_payments(content, cutoff):
    payments = defaultdict(dict)
    for row in csv.DictReader(io.StringIO(content.decode('utf-8-sig')), delimiter=';'):
        key = (row['Tipo Titulo'], datetime.strptime(row['Vencimento do Titulo'], '%d/%m/%Y').date())
        day = datetime.strptime(row['Data Resgate'], '%d/%m/%Y').date()
        if day > cutoff:
            continue
        # This resource contains coupons only, including the final coupon but not principal.
        unit = br_number(row['PU'])
        quantity, total = br_number(row['Quantidade']), br_number(row['Valor'])
        if unit <= 0 or abs(unit * quantity - total) > max(.05, total * .000001):
            raise ValueError('Cupom Tesouro inconsistente com quantidade e valor pagos.')
        if day in payments[key] and abs(payments[key][day] - unit) > 1e-8:
            raise ValueError('Cupons divergentes na mesma data.')
        payments[key][day] = unit
    return payments


def select_bonds(histories, payments, cutoff):
    selected, excluded = [], []
    for key in sorted(histories):
        kind, maturity = key
        history = histories[key]
        if not all(shift_month(cutoff, -i) in history for i in range(61)):
            excluded.append({'kind': kind, 'maturity': maturity.isoformat(), 'reason': 'less_than_61_consecutive_prices'})
            continue
        if 'Juros' in kind:
            expected = (1, 7) if 'Prefixado' in kind else (5, 11) if maturity.month == 5 else (2, 8)
            by_month = {month_end(d.year, d.month) for d in payments.get(key, {})}
            missing = [shift_month(cutoff, -i).isoformat() for i in range(60)
                       if shift_month(cutoff, -i).month in expected and shift_month(cutoff, -i) not in by_month]
            if missing:
                excluded.append({'kind': kind, 'maturity': maturity.isoformat(),
                                 'reason': 'missing_observed_coupon', 'months': missing})
                continue
        selected.append(key)
    if not any('Juros' in kind for kind, _ in selected):
        raise ValueError('Nenhum titulo com cupons efetivos completos.')
    return selected, excluded


def collect(folder, *, refresh=False, activate=True):
    from .market_inventory import market_root

    folder = market_root(folder)
    selected = allowed_tickers(folder)
    specs = {ticker: spec for ticker, spec in STOCKS.items() if selected is None or ticker in selected}
    scope = scope_metadata(folder)
    cutoff = shift_month(month_end(date.today().year, date.today().month), -1)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / 'raw'
    income_raw = raw / 'income'
    income_raw.mkdir(parents=True, exist_ok=True)
    sources = []

    def source(url, name, *, old=False, refresh_old=False):
        print('Fonte: ' + name, flush=True)
        target = raw if old else income_raw
        content = cached_source(url, name, target, sources, refresh and (not old or refresh_old))
        if not old:
            sources[-1]['file'] = 'income/' + name
        return content

    dividends, ledger = {}, []
    if 'PETR4' in specs:
        primary_petro = source(PETROBRAS_URL, 'petr-ri.html')
        document = html.fromstring(primary_petro.decode('utf8'))
        links = document.xpath('//a[contains(normalize-space(.), "Download do arquivo em Excel")]/@href')
        if len(links) != 1:
            raise ValueError('Link da planilha Petrobras ausente ou ambiguo.')
        excel_url = parse_qs(urlparse(links[0]).query)['src'][0]
        if urlparse(excel_url).hostname != 'filemanager-cdn.mziq.com':
            raise ValueError('Host inesperado para a planilha Petrobras.')
        dividends['PETR4'], ledger = petrobras_payments(source(excel_url, 'petr-history.xlsx'), cutoff)
    if 'VALE3' in specs:
        source(VALE_URL, 'vale-ri.html')
        vale_raw = source('https://brapi.dev/api/v2/stocks/dividends?symbols=VALE3&sortOrder=asc', 'brapi-v2-VALE3.json')
        reconciliation = source(VALE_RECONCILIATION_URL, 'vale-jcp-2020.html')
        dividends['VALE3'], vale_ledger = vale_payments(vale_raw, reconciliation, cutoff)
        ledger.extend(vale_ledger)
    stocks = {}
    for ticker in specs:
        content = source(f'https://brapi.dev/api/quote/{ticker}?range=max&interval=1d&dividends=true', f'brapi-{ticker}.json', old=True)
        result, _, _ = stock_history(content, cutoff)
        raw_history = source(f'https://brapi.dev/api/v2/stocks/historical?symbols={ticker}&range=max&interval=1d&includeRaw=true',
                             f'brapi-raw-prices-{ticker}.json')
        prices = verified_raw_prices(raw_history, cutoff)
        stocks[ticker] = (result, prices)
    statements, financial_audit = [], []
    # Annual CVM archives remain cached during explicit market-data refreshes.
    for year in range(2010, date.today().year):
        url = f'https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{year}.zip'
        statements.extend(cvm_statements(source(url, f'cvm-dfp-{year}.zip', old=True), year,
                                         diagnostics=financial_audit,
                                         **({'specs': specs} if selected is not None else {})))
    inflation_url = ('https://api.bcb.gov.br/dados/serie/bcdata.sgs.433/dados?formato=json'
                     f'&dataInicial=01/01/2010&dataFinal={cutoff:%d/%m/%Y}')
    inflation = {month_end(int(r['data'][6:]), int(r['data'][3:5])): float(r['valor']) / 100
                 for r in json.loads(source(inflation_url, 'bcb-ipca.json', old=True, refresh_old=True))}
    histories = treasury_prices(source(TREASURY_URL, 'tesouro-precos.csv', old=True, refresh_old=True), cutoff)
    coupons_raw = source(COUPON_URL, 'treasury-coupons.csv')
    coupons = treasury_payments(coupons_raw, cutoff)
    bonds, excluded_bonds = select_bonds(histories, coupons, cutoff)
    months = [shift_month(cutoff, -i) for i in reversed(range(60))]
    assets, observations = [], []
    cash_by_month = defaultdict(lambda: defaultdict(float))
    for ticker, (result, prices) in stocks.items():
        current = financial_features(statements, dividends[ticker], cutoff, specs[ticker])
        if current is None:
            raise ValueError(ticker + ': fundamentos atuais insuficientes.')
        records, _ = stock_observations(ticker, prices, dividends[ticker], statements, cutoff, spec=specs[ticker])
        observations.extend(r | {'forecast_target_id': PAYMENT_TARGET['id']} for r in records
                            if r['as_of'] >= '2016-01-31')
        assets.append({'ticker': ticker, 'name': result['longName'], 'asset_class': 'acoes', 'risk_level': 2,
                       'liquidity_days': 2, 'minimum_years': 5, 'price': prices[cutoff][2],
                       'features': current[0], 'as_of': cutoff.isoformat(),
                       'source': ('Petrobras RI: status PAGO e data de pagamento, valor bruto declarado; ' if ticker == 'PETR4'
                                  else 'brapi v2: pagamentos historicos; JCP 2020 reconciliado com Vale RI; ')
                                 + f'CVM DFP {current[1]["filing_id"]}. Valores brutos, sem imposto individual.'})
        for day, _, amount in dividends[ticker]:
            cash_by_month[ticker][month_end(day.year, day.month)] += amount
    bond_details = []
    for key in bonds:
        kind, maturity = key
        coupon = 'Juros' in kind
        prefix = 'TD-PRE-CUPOM' if 'Prefixado' in kind and coupon else 'TD-IPCA-CUPOM' if coupon else 'TD-SELIC' if kind == 'Tesouro Selic' else 'TD-PRE' if kind == 'Tesouro Prefixado' else 'TD-IPCA'
        ticker = prefix + '-' + str(maturity.year)
        risk = 0 if kind == 'Tesouro Selic' else 1 if 'Prefixado' in kind else 2
        annual = sum(amount for day, amount in coupons.get(key, {}).items() if shift_month(cutoff, -12) < day <= cutoff) / histories[key][cutoff][1]
        assets.append({'ticker': ticker, 'name': f'{kind} {maturity:%d/%m/%Y}',
                       'asset_class': 'liquidez' if risk == 0 else 'renda_fixa', 'risk_level': risk,
                       'liquidity_days': 1, 'minimum_years': 0 if risk == 0 else 2 if risk == 1 else 5,
                       'price': histories[key][cutoff][1], 'annual_cash_yield': annual if coupon else 0,
                       'as_of': cutoff.isoformat(),
                       'source': 'Tesouro Transparente: PU Venda Manha e cupons efetivamente pagos; principal excluido.' if coupon else
                                 'Tesouro Transparente: PU Venda Manha; titulo sem cupom, pagamento periodico zero.'})
        for day, amount in coupons.get(key, {}).items() if coupon else []:
            cash_by_month[ticker][month_end(day.year, day.month)] += amount
            if payment_in_window(day, months[0], cutoff):
                ledger.append({'ticker': ticker, 'payment_date': day.isoformat(), 'gross_per_unit': amount,
                               'label': 'CUPOM', 'status': 'included', 'basis': 'Treasury_coupon_only_resource'})
        bond_details.append({'ticker': ticker, 'kind': kind, 'maturity': maturity.isoformat(),
                             'coupon': coupon, 'quote_date': histories[key][cutoff][0].isoformat(),
                             'return_method': '(PU final + cupom pago no mes) / PU inicial - 1; principal nao e renda'})
    returns, cash_rows = [], []
    for day in months:
        previous = shift_month(day, -1)
        row, paid_row = {'date': day.isoformat(), 'inflation': inflation[day]}, {'date': day.isoformat()}
        for asset in assets:
            ticker = asset['ticker']
            if ticker in stocks:
                prices = stocks[ticker][1]
                start, end = prices[previous][2], prices[day][2]
            else:
                detail = next(b for b in bond_details if b['ticker'] == ticker)
                history = histories[(detail['kind'], date.fromisoformat(detail['maturity']))]
                start, end = history[previous][1], history[day][1]
            paid_row[ticker] = cash_by_month[ticker][day] / start
            row[ticker] = end / start - 1 + paid_row[ticker]
        returns.append(row)
        cash_rows.append(paid_row)
    observations.sort(key=lambda r: (r['as_of'], r['ticker']))
    snapshot = folder / 'snapshots' / ('income-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
    snapshot.mkdir(parents=True)
    write_json(snapshot / 'payment-ledger.json', ledger)
    coupon_source = next(s for s in sources if s['url'] == COUPON_URL)
    metadata = {
        'assets': assets, 'source': 'Dados reais: Petrobras RI, brapi v2/Vale RI, CVM DFP, cupons e precos Tesouro Transparente, BCB/SGS IPCA 433.',
        'returns_include_distributions': True, 'total_return_method': 'unadjusted_price_plus_paid_cash',
        'stock_price_basis': 'brapi_v2_explicit_rawClose',
        'cashflow_basis': 'verified_paid_cash', 'cashflow_excludes_principal': True,
        'cashflow_source': {'url': COUPON_URL, 'sha256': coupon_source['sha256']},
        'cashflow_sources': [s for s in sources if '/income/' in '/' + s['file']],
        'payment_ledger_sha256': hashlib.sha256((snapshot / 'payment-ledger.json').read_bytes()).hexdigest(),
        'cashflow_tax_basis': 'gross_before_individual_taxes', 'forecast_target': PAYMENT_TARGET,
        'sources': sources, 'bonds': bond_details, 'reference_date': cutoff.isoformat(),
        'collected_at': datetime.now(timezone.utc).isoformat(), 'data_availability': 'ready',
        'limitations': [
            'Universo de estudo restrito, com vies de selecao e sobrevivencia; nao constitui varredura de toda a bolsa.',
            'Renda bruta antes de impostos individuais. Petrobras usa componente bruto declarado de eventos com status PAGO, sem adicionar correcoes posteriores pela Selic.',
            'Vale: datas e valores historicos reportados pela brapi, nao extratos individuais; evento de 2020 reconciliado com comunicado do emissor. Selo do fornecedor nao e garantia independente de completude.',
            'Retorno mensal construido com preco nao ajustado e pagamentos do mes; nao se soma provento a preco ja ajustado.',
            'A convencao mensal pressupoe posicao elegivel para os pagamentos; nao controla quantidades individuais na data-com ou direitos pendentes apos negociacao.',
            'Cenarios reamostram retornos, pagamentos e inflacao conjuntos. Nao sao fluxos contratuais futuros dos vencimentos exibidos; rolagem e reinvestimento entre titulos futuros nao sao modelados.',
            'Cupons com pagamento esperado mas ausente na fonte sao excluidos, nunca preenchidos com zero ou estimativas.',
            'Os niveis de risco, limites e prazos sao parametros academicos; a meta nao altera a classificacao do usuario.',
            'A ausencia de pagamentos nos titulos sem cupom implica renda periodica zero; venda ou vencimento do principal nao e renda.',
            'Dados retrospectivos revisados, nao arquivo point-in-time; limitacoes de demonstracoes CVM e proventos historicos permanecem.',
            'Previsao de pagamentos limitada a 12 meses; cenarios posteriores reamostram o historico, sem extrapolar o XGBoost por decadas.',
        ], 'financially_validated': False,
    }
    if scope is not None:
        metadata['dividend_scope'] = scope
    fields = ['date', *[a['ticker'] for a in assets]]
    write_csv(snapshot / 'returns.csv', ['date', 'inflation', *fields[1:]], returns)
    write_csv(snapshot / 'cashflows.csv', fields, cash_rows)
    write_csv(snapshot / 'observations.csv', ['ticker', 'as_of', 'published_at', 'label_end', *FEATURES,
                                             'target', 'fiscal_end', 'filing_id', 'forecast_target_id', 'event_basis',
                                             'event_date_field', 'previous_window_start', 'features_available_at'], observations)
    metadata['cashflows_sha256'] = hashlib.sha256((snapshot / 'cashflows.csv').read_bytes()).hexdigest()
    write_json(snapshot / 'assets.json', metadata)
    dataset = real_dataset(snapshot)
    _, metrics = dataset_forecast(dataset)
    report = {'status': 'ready', 'reference_date': cutoff.isoformat(), 'snapshot_id': snapshot.name,
              'assets': [a['ticker'] for a in assets], 'months': len(months), 'observations': len(observations),
              'observation_dates': len({r['as_of'] for r in observations}), 'model': metrics,
              'payment_events': sum(r['status'] == 'included' for r in ledger),
              'excluded_bonds': excluded_bonds, 'source_manifest': sources,
              'forecast_target': PAYMENT_TARGET, 'financially_validated': False}
    if scope is not None:
        report['dividend_scope'] = scope
    write_json(snapshot / 'collection-report.json', report)
    if activate:
        publish_pointer(folder, snapshot, report)
    print(json.dumps({'snapshot': str(snapshot), 'assets': report['assets'], 'model': metrics,
                      'payments': report['payment_events'], 'published': activate}, ensure_ascii=True, indent=2))
    return snapshot, report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'recommender/data/market')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--no-publish', action='store_true')
    arguments = parser.parse_args()
    collect(arguments.output.resolve(), refresh=arguments.refresh, activate=not arguments.no_publish)
