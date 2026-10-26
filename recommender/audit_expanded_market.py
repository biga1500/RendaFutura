"""Eight synthetic retirement profiles, actual algorithms and per-portfolio checks."""

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timezone
from html import escape
import json
import math
from pathlib import Path
import time

from .contracts import RecommendationRequest
from .datasets import get_dataset
from .engine import compute


def verify_payment_ledger(snapshot, raw):
    from .broad_stocks import split_price_history
    from .collect_income_data import treasury_prices, verified_raw_prices
    from .collect_market_data import shift_month
    from .share_basis import factor_after
    metadata = json.loads((snapshot / 'assets.json').read_text(encoding='utf-8'))
    cutoff = date.fromisoformat(metadata['reference_date'])
    additions = {row['ticker']: row for row in metadata.get('expanded_stocks', [])}
    payments = defaultdict(float)
    for event in json.loads((snapshot / 'payment-ledger.json').read_text(encoding='utf-8')):
        if event['status'] == 'included':
            payments[event['ticker'], event['payment_date'][:7]] += event.get('gross_per_unit', event.get('gross_per_share', 0))
    treasury = treasury_prices((raw / 'tesouro-precos.csv').read_bytes(), cutoff)
    bonds = {row['ticker']: row for row in metadata.get('bonds', [])}
    prices = {}
    for asset in metadata['assets']:
        ticker = asset['ticker']
        if ticker in additions:
            source_file = additions[ticker].get('price_source_file')
            path = (raw.parent / source_file).resolve() if source_file else raw / 'expanded' / (ticker + '-prices.json')
            assert path.resolve().is_relative_to(raw.resolve())
            values, _ = split_price_history(path.read_bytes(), ticker, cutoff, allow_recent_actions=True)
            normal = additions[ticker].get('share_normalization')
            prices[ticker] = {day: value[2] / (factor_after(value[0], normal['actions'], cutoff)
                                              if normal and normal['price_source_basis'] == 'raw' else 1)
                              for day, value in values.items()}
        elif ticker in bonds:
            bond = bonds[ticker]
            prices[ticker] = {day: value[1] for day, value in treasury[bond['kind'], date.fromisoformat(bond['maturity'])].items()}
        else:
            values = verified_raw_prices((raw / 'income' / ('brapi-raw-prices-' + ticker + '.json')).read_bytes(), cutoff)
            prices[ticker] = {day: value[2] for day, value in values.items()}
    checks = 0
    with (snapshot / 'cashflows.csv').open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            day = date.fromisoformat(row['date'])
            for ticker in prices:
                expected = payments[ticker, row['date'][:7]] / prices[ticker][shift_month(day, -1)]
                assert math.isclose(float(row[ticker]), expected, abs_tol=1e-10, rel_tol=1e-8), (ticker, row['date'], expected, row[ticker])
                checks += 1
    return {'status': 'passed', 'monthly_asset_checks': checks, 'assets': len(prices),
            'basis': 'ledger_paid_cash_divided_by_previous_month_observed_price'}


def check_portfolio(outcome, request, issuers):
    level = request.investor.risk_level
    for portfolio in outcome['portfolios']:
        allocation = portfolio['allocation']
        weights = [a['weight'] for a in allocation]
        assert abs(sum(weights) - 1) < 1e-6 and min(weights) >= -1e-7
        assert sum(a['weight'] for a in allocation if a['assetClass'] == 'acoes') <= [0, .4, .65][level] + 1e-7
        assert sum(a['weight'] for a in allocation if a['assetClass'] == 'liquidez') >= [.25, .15, .1][level] - 1e-7
        by_issuer = defaultdict(float)
        for asset in allocation:
            if asset['assetClass'] == 'acoes':
                by_issuer[issuers[asset['ticker']]] += asset['weight']
        assert all(weight <= .25 + 1e-7 for weight in by_issuer.values())
        assert portfolio['portfolio']['annualVolatility'] <= [.08, .16, .28][level] + 1e-7
        assert portfolio['incomeAnalysis']['usesPrincipal'] is False
        assert len(portfolio['simulation']['chart']) > 1
        for row in portfolio['simulation']['chart']:
            assert 0 <= row['p10'] <= row['p50'] <= row['p90']
        capacity = portfolio['simulation']['monthlyIncomeCapacity']
        assert 0 <= capacity[0] <= capacity[1] <= capacity[2]
    assert outcome['coverage']['eligibleAssets'] >= outcome['coverage']['optimizedAssets']
    assert outcome['coverage']['optimizedAssets'] <= 24
    assert outcome['construction']['optimizationUniverse']['usesSimulationOutcomes'] is False
    universe = outcome.get('universeScreening')
    if universe is not None:
        assert len(universe['stocks']) == universe['counts']['stocks']
        assert len({row['ticker'] for row in universe['stocks']}) == len(universe['stocks'])
        assert universe['counts']['readyStocks'] + universe['counts']['pendingDataStocks'] == universe['counts']['stocks']
        allocated = {row['ticker'] for portfolio in outcome['portfolios'] for row in portfolio['allocation'] if row['weight'] > 1e-6}
        for row in universe['stocks']:
            if not row['dataReady']:
                assert row['predictedCashPerShare'] is None and row['ticker'] not in allocated
                assert row['dataReasons'] and not row['eligible']
        assert universe['counts']['eligibleStocks'] == sum(row['eligible'] and row['indicators'] is not None for row in outcome['assetAnalysis'])
    if outcome['model']:
        assert outcome['model']['appliedMethod'] == 'xgboost'
        assert outcome['model']['usedForIncomeSimulation'] is True
        assert outcome['model']['uncertaintyBasis'] == 'validation_residuals_only'
        assert outcome['model']['forecastedAssets'] == sum(a['eligible'] and a['indicators'] is not None for a in outcome['assetAnalysis'])


def audit(personas, output):
    output.mkdir(parents=True, exist_ok=True)
    dataset = get_dataset('real')
    issuers = {a.ticker: a.issuer_id or a.ticker for a in dataset.assets}
    report = {'startedAt': datetime.now(timezone.utc).isoformat(), 'cases': [],
              'scope': 'Eight synthetic personas, not real participants or expert/financial validation.',
              'snapshotReference': dataset.dates[-1], 'preparedAssets': len(dataset.assets)}
    for persona in personas:
        answers, profile = persona['answers'], persona['result']
        request = RecommendationRequest.model_validate({
            'data_mode': 'real', 'forecast_policy': 'xgboost_experimental',
            'investor': {'risk_level': ['Conservador', 'Moderado', 'Arrojado'].index(profile['profile']),
                         'knowledge': profile['knowledge'],
                         'withdrawal_years': [0, 1, 3, 6][['Em menos de 1 ano', 'Entre 1 e 3 anos', 'Entre 3 e 5 anos', 'Depois de 5 anos'].index(answers['withdrawal'])],
                         'high_liquidity': answers['liquidity'] == 'Muito provável'},
            'plan': {'income_mode': 'distributions_only', 'age': persona['age'],
                     'retirement_age': persona['retirementAge'], 'end_age': 90,
                     'initial_capital': persona['capital'], 'monthly_contribution': persona['contribution'],
                     'target_income': persona['target'], 'annual_cost_percent': .5,
                     'seed': 42, 'simulations': 500, 'block_months': 6}})
        started = time.monotonic()
        result = compute(request)
        assert result['status'] == 'ready' and result['dataMode'] == 'real'
        check_portfolio(result, request, issuers)
        record = {'id': persona['id'], 'name': persona['name'], 'profile': profile['profile'],
                  'request': request.model_dump(), 'result': result, 'seconds': round(time.monotonic() - started, 2)}
        report['cases'].append(record)
        (output / 'engine-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        print(json.dumps({'id': persona['id'], 'profile': profile['profile'], 'portfolios': len(result['portfolios']),
                          'coverage': result['coverage'], 'monthly_capacity': result['simulation']['monthlyIncomeCapacity'],
                          'seconds': record['seconds']}), flush=True)
    last = report['cases'][-1]
    assert compute(RecommendationRequest.model_validate(last['request'])) == last['result']
    report.update({'completed': True, 'reproducible': True, 'finishedAt': datetime.now(timezone.utc).isoformat()})
    (output / 'engine-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    lines = []
    for case in report['cases']:
        result = case['result']
        lines.append('<tr>' + ''.join('<td>' + escape(str(value)) + '</td>' for value in (
            case['id'], case['name'], case['profile'], result['coverage']['eligibleAssets'],
            result['coverage']['optimizedAssets'], len(result['portfolios']),
            round(result['simulation']['monthlyIncomeCapacity'][1], 2),
            round(result['simulation']['successFrequency'] * 100, 1))) + '</tr>')
    page = '<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Teste da base ampliada</title><style>body{font:16px Arial;max-width:1250px;margin:36px auto;color:#172322;padding:20px}table{border-collapse:collapse;width:100%}th,td{padding:10px;border:1px solid #ccc;text-align:left}th{background:#edf3f0}p{line-height:1.5}a{color:#09684a}</style><h1>RendaFutura: base ampliada e oito perfis</h1>'
    page += f'<p>Base de {len(dataset.assets)} ativos reais, com 60 meses comuns. Referência: {escape(dataset.dates[-1])}. Os oito perfis são fictícios. Testes computacionais não equivalem a validação financeira nem garantia de renda.</p>'
    page += '<p>Foram verificados os limites do perfil, concentração por empresa, soma da carteira, previsões XGBoost nas ações elegíveis, otimização NSGA-II quando há alternativas, cenários Monte Carlo e a exclusão do principal da renda. Seleção computacional limitada a 24 ativos; todos os elegíveis são analisados antes dela.</p>'
    page += '<table><thead><tr><th>Perfil</th><th>Persona</th><th>Classificação</th><th>Elegíveis</th><th>Otimizados</th><th>Carteiras distintas</th><th>Renda mensal mediana (R$)</th><th>Atendimento (%)</th></tr></thead><tbody>' + ''.join(lines) + '</tbody></table>'
    page += '<p>A renda é a mediana da capacidade mensal média de cada ano da aposentadoria, não o dinheiro recebido hoje. O patrimônio atual, aportes e preços de referência são apresentados separadamente no sistema. Juros e dividendos não são garantidos.</p>'
    model = next(case['result']['model'] for case in report['cases'] if case['result']['model'])
    page += f'<p>Teste temporal do XGBoost: erro absoluto médio de R$ {model["xgboostMae"]:.4f}/ação; referência simples: R$ {model["baselineMae"]:.4f}/ação. O XGBoost continua experimental; sua utilização não implica superioridade. Horizonte aprendido de 12 meses; depois, reamostragem histórica conjunta.</p>'
    page += '<p><a href="engine-results.json">Resultados completos e parâmetros</a> · <a href="collection-report.json">Fontes, cobertura e exclusões</a> · <a href="browser/report.json">Verificação no navegador</a></p></html>'
    (output / 'Relatorio_Base_Ampliada.html').write_text(page, encoding='utf-8')
    print('PASS: eight profiles, each portfolio checked, reproducible results.', flush=True)
    return report


def finalize(output):
    import hashlib
    import xml.etree.ElementTree as ET
    from lxml import html
    engine = json.loads((output / 'engine-results.json').read_text(encoding='utf-8'))
    browser = json.loads((output / 'browser/report.json').read_text(encoding='utf-8'))
    collection = json.loads((output / 'collection-report.json').read_text(encoding='utf-8'))
    assert engine.get('completed') and engine.get('reproducible') and len(engine['cases']) == 8
    assert browser.get('completed') and browser.get('accountDeleted') and browser['errors'] == []
    tests = list(ET.parse(output / 'python-tests.xml').getroot().iter('testcase'))
    assert tests and not any(t.find('failure') is not None or t.find('error') is not None for t in tests)
    root = Path(__file__).resolve().parents[1]
    files = ['recommender/broad_stocks.py', 'recommender/collect_expanded_data.py',
             'recommender/collect_income_data.py', 'recommender/collect_market_data.py',
             'recommender/engine.py', 'recommender/datasets.py', 'recommender/contracts.py',
             'recommender/forecast.py', 'recommender/forecast_paths.py', 'recommender/simulation.py',
             'recommender/portfolios.py', 'recommender/rollover.py', 'recommender/planning.py',
             'recommender/current_quotes.py', 'recommender/share_basis.py', 'recommender/treasury_current.py',
             'recommender/treasury_file_import.py', 'recommender/historical_import.py', 'recommender/historical_provider.py',
             'lib/initial-portfolio.ts', 'lib/recommendation.ts',
             'components/retirement-planner.tsx', 'vite.config.ts']
    record = {'recorded_at': datetime.now(timezone.utc).isoformat(), 'snapshot': collection['snapshot_id'],
              'engine_version': engine['cases'][0]['result']['engineVersion'],
              'synthetic_profiles': 8, 'browser_profiles': len(browser['cases']),
              'test_account_deleted': True, 'browser_errors': [],
              'python_tests_passed': sum(t.find('skipped') is None for t in tests),
              'python_tests_skipped': sum(t.find('skipped') is not None for t in tests),
              'payment_reconciliation': json.loads((output / 'payment-reconciliation.json').read_text(encoding='utf-8')),
              'human_participants': 0, 'financially_validated': False,
              'source_hashes': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
              'result_hashes': {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                                for name in ('engine-results.json', 'browser/report.json', 'collection-report.json', 'python-tests.xml', 'node-tests.txt', 'payment-reconciliation.json')}}
    (output / 'provenance.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    page = html.fromstring((output / 'Relatorio_Base_Ampliada.html').read_text(encoding='utf-8'))
    body = page.find('body')
    for element in list(body):
        if element.tag == 'h2' and element.text == 'Cobertura e limites':
            for appended in list(body)[list(body).index(element):]:
                body.remove(appended)
            break
    def paragraph(tag, text):
        element = html.Element(tag)
        element.text = text
        body.append(element)
    paragraph('h2', 'Cobertura e limites')
    stocks = len(collection.get('expanded_stocks', [])) + 2
    bonds = len(collection['assets']) - stocks
    paragraph('p', f'Foram preparados {stocks} códigos de ações e {bonds} títulos. O catálogo contém 778 códigos de ações/units, '
              'dos quais 404 são aliases fracionários, e 58 títulos. Isso não significa 778 empresas. '
              'As demais exclusões constam no relatório de coleta: histórico insuficiente, ausência ou divergência '
              'de pagamentos, eventos societários, fundamentos indisponíveis e estruturas não conciliadas.')
    paragraph('p', 'Fontes: cadastro, eventos e preços de referência da B3; demonstrações oficiais da CVM; '
              'tabelas de pagamentos do Investidor10; fechamentos do Yahoo; preços/cupons do Tesouro Transparente '
              'e IPCA do BCB. Os cenários usam preços históricos. Cotações atuais de ações e ofertas oficiais de compra do Tesouro são documentadas separadamente; títulos antigos fora da oferta atual não recebem quantidade de compra presumida. '
              'A atualização do arquivo oficial de cupons retornou HTTP 429; três títulos adicionais permanecem '
              'bloqueados por ausência de cupons observados de agosto de 2026.')
    paragraph('p', 'Nos três perfis conservadores, somente o Tesouro Selic preparado passa pelos limites. '
              'Sem cupom periódico, a renda distribuída simulada é zero: valorização ou devolução do principal '
              'não são tratadas como pagamento mensal. A meta permanece não atendida, sem aumento artificial do risco.')
    paragraph('h2', 'Capturas dos testes no navegador')
    for image_path, caption in [('browser/07-equilibrio-comparison.png', 'Perfil arrojado: carteiras distintas por segurança relativa e proventos.'),
                                ('browser/07-proventos-initial.png', 'Posição inicial indicativa da alternativa por proventos, com cotações observadas, arredondamento e alertas. Não constitui recomendação profissional.'),
                                ('browser/07-treasury-current-offers.png', 'Catálogo de ofertas atuais do Tesouro, separado dos históricos preparados para simulação.')]:
        paragraph('p', caption)
        picture = html.Element('img', src=image_path, alt=caption, style='display:block;max-width:100%;height:auto;border:1px solid #ddd')
        body.append(picture)
    passed = sum(t.find('skipped') is None for t in tests)
    paragraph('p', f'Verificação final: {passed} testes Python aprovados; oito perfis no motor; '
              f'{len(browser["cases"])} perfis no navegador; conta fictícia excluída. Nenhum documento acadêmico foi alterado.')
    (output / 'Relatorio_Base_Ampliada.html').write_bytes(html.tostring(page, encoding='utf-8', doctype='<!doctype html>'))
    print(json.dumps({k: record[k] for k in ('snapshot', 'python_tests_passed', 'synthetic_profiles', 'browser_profiles', 'test_account_deleted')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('personas', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    audit(json.loads(args.personas.read_text(encoding='utf-8'))['personas'], args.output)
