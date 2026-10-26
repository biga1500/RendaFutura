"""Current-quote portfolios with independent stock/contract lot channels.

Historical Treasury sample columns are never substituted for current offers.
Equities retain observed joint return/cash histories; current Treasury positions
use dated contracts, observed macro/yield-factor shocks and hypothetical future
same-family lots. Both channels keep principal separate from retirement income.
"""

from datetime import date

import numpy as np
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import Problem
from pymoo.core.repair import Repair
from pymoo.optimize import minimize

from .data_coverage import coverage_summary
from .forecast import get_forecast
from .forecast_paths import forecast_paths
from .market_inventory import market_root
from .portfolios import (asset_priority, candidate_seeds, comparison_choices,
                         feasible_unique, payment_calendar)
from .reinvestment_ledger import reinvestment_factors
from .simulation import block_indices
from .treasury_analysis import analyze_current_treasury, current_data_revision
from .treasury_cash_paths import anniversary
from .treasury_lot_kernels import TreasuryLotProvider
from .treasury_market import read_current_instruments
from .treasury_yield_factors import (INSTRUMENT_FACTOR_POLICY,
    load_treasury_yield_factors, yield_factor_scenarios)
from .universe import profile_reasons, screen_universe
from .cash_calendar_research import calendar_conflict_engine_warnings

POLICY = 'current_quotes_contract_lots_no_cross_asset_rebalance_v1'


def available(dataset, plan, folder=None):
    return (dataset.mode == 'real' and plan.income_mode == 'distributions_only'
            and bool(dataset.metadata.get('stock_universe_preparation'))
            and (market_root(folder) / 'raw/current/official-investir.json').is_file())


def current_coverage(dataset, instruments):
    result = coverage_summary(dataset)
    preparation = dataset.metadata['stock_universe_preparation']
    ready = preparation['ready_stocks'] + len(instruments)
    total = preparation['evaluated_stocks'] + len(instruments)
    result['historicalPreparationCounts'] = dict(result['counts'])
    result['counts'] = {'catalogue': total, 'modelReady': ready, 'catalogueReady': ready,
                       'catalogueGap': total - ready, 'readyOutsideCatalogue': 0}
    result['gaps'] = [row for row in result['gaps'] if row['assetClass'] == 'acoes']
    result['treasuryGaps'] = []
    liquid = sum(item.asset.asset_class == 'liquidez' for item in instruments)
    result['byAssetClass'] = {}
    for kind, catalogue, prepared in [('acoes', preparation['evaluated_stocks'], preparation['ready_stocks']),
        ('liquidez', liquid, liquid), ('renda_fixa', len(instruments) - liquid, len(instruments) - liquid)]:
        result['byAssetClass'][kind] = {'catalogue': catalogue, 'modelReady': prepared,
            'catalogueReady': prepared, 'catalogueGap': catalogue - prepared, 'readyOutsideCatalogue': 0}
    readiness = result.setdefault('readiness', {})
    readiness['historicalSnapshotTickers'] = readiness.get('tickers', [])
    readiness['tickers'] = sorted([asset.ticker for asset in dataset.assets if asset.asset_class == 'acoes']
                                  + [item.asset.ticker for item in instruments])
    readiness['policy'] = 'Observed stock histories and current Treasury contracts; not invented title histories or financial validation.'
    result['currentTreasuryModel'] = {'contracts': len(instruments),
        'basis': 'current_contracts_with_joint_macro_yield_factors_not_historical_price_columns',
        'financiallyValidated': False, 'requiresInstitutionEligibility': ['Tesouro Reserva']}
    return result


def _current_stock(asset):
    quote = asset.current_quote
    if quote is None or quote.price is None:
        raise ValueError('current_stock_purchase_reference_unavailable:' + asset.ticker)
    day = date.fromisoformat(quote.as_of[:10])
    if not 0 <= (date.today() - day).days <= 7:
        raise ValueError('current_stock_purchase_reference_stale:' + asset.ticker)
    if not quote.purchase_available:
        raise ValueError('current_stock_purchase_reference_unavailable:' + asset.ticker)
    return asset.model_copy(update={'price': quote.price, 'as_of': day.isoformat()})


def contract_shortlist(instruments, eligible, retirement_date):
    groups = {}
    for index in eligible:
        item = instruments[index]
        # Reserva is fully valued, but the questionnaire does not establish
        # the required Banco do Brasil access; do not prescribe its purchase.
        if item.pricing.get('institution_restriction'):
            continue
        groups.setdefault(item.offer['kind'], []).append(index)
    selected = []
    for kind in sorted(groups):
        def distance(index):
            item = instruments[index]
            anchor = ((item.spec or {}).get('terms') or {}).get('conversion_date') or item.offer['maturity']
            return abs((date.fromisoformat(anchor) - retirement_date).days), item.duration_years, item.asset.ticker
        selected.append(min(groups[kind], key=distance))
    return selected


def _yield_paths(instrument, sampled, months):
    key = INSTRUMENT_FACTOR_POLICY[instrument.offer['kind']]['yield_factor']
    if key is None:
        return np.zeros((len(sampled['inflation']), months + 1))
    initial = instrument.offer['buy_rate']
    shifted = (sampled[key][:, :months] - sampled['initial_levels'][key]) * 100 + initial
    return np.column_stack([np.full(len(shifted), initial), shifted])


def contract_risk(instruments, history, count, block, seed):
    sampled = yield_factor_scenarios(history, 14, count, block, seed)
    output = {}
    for item in instruments:
        provider = TreasuryLotProvider(item, sampled, _yield_paths(item, sampled, 12),
                                       months=12, annual_valuations=True)
        lot = provider(0, 12, count)
        total = lot.ex_cash_market_value[:, -1] + lot.gross_cash.sum(axis=1) - 1
        output[item.asset.ticker] = float(np.std(total, ddof=1))
    return output


def scenario_factors(dataset, stock_indices, assets, instruments, forecasts, model, metrics, plan,
                     count, seed, history):
    from .linear_scenarios import build_stock_factors
    months = 12 * (plan.end_age - plan.age)
    # Same sampled month blocks preserve observed equity/macro/yield dependence.
    extra = months + 2
    indices = block_indices(len(dataset.dates), extra, count, plan.block_months, seed)
    sampled = yield_factor_scenarios(history, months, count, plan.block_months, seed,
        indices=indices, index_dates=dataset.dates, padding_months=2)
    stock_assets = [asset for asset in assets if asset.asset_class == 'acoes']
    by_ticker = {dataset.assets[index].ticker: index for index in stock_indices}
    output = {}
    if stock_assets:
        columns = [by_ticker[asset.ticker] for asset in stock_assets]
        paths, cash = forecast_paths(dataset.returns[:, columns], dataset.cash_returns[:, columns],
            dataset.inflation, stock_assets, forecasts, model, metrics['appliedMethod'],
            months, count, plan.block_months, seed, scenario_indices=indices[:, :months])
        stock_factors = build_stock_factors(paths, cash, plan, include_retirement_unit=True)
        for position, asset in enumerate(stock_assets):
            output[asset.ticker] = {key: values[..., position] for key, values in stock_factors.items()
                                   if key in ('wealth_factors', 'annual_income_factors', 'annual_capital_loss_factors',
                                              'annual_weighted_income_factors', 'retirement_unit_annual_income_factors')}
    by_contract = {item.asset.ticker: item for item in instruments}
    for asset in assets:
        if asset.asset_class == 'acoes':
            continue
        item = by_contract[asset.ticker]
        provider = TreasuryLotProvider(item, sampled, _yield_paths(item, sampled, months), months=months,
            annual_cost_percent=plan.annual_cost_percent, annual_valuations=True)
        output[asset.ticker] = reinvestment_factors(provider, kind=item.offer['kind'],
            risk_level=asset.risk_level, months=months,
            accumulation_months=12 * (plan.retirement_age - plan.age), count=count, include_retirement_unit=True)
    fields = ('wealth_factors', 'annual_income_factors', 'annual_capital_loss_factors',
              'annual_weighted_income_factors', 'retirement_unit_annual_income_factors')
    combined = {key: np.stack([output[asset.ticker][key] for asset in assets], axis=-1) for key in fields}
    combined['provenance'] = {'policy': POLICY, 'jointBlockIndices': True,
        'factorRevision': history['revision'], 'yieldProjection': sampled['projection_policy'],
        'familyYieldProxyNotInstrumentHistory': True, 'taxBasis': 'gross_before_individual_taxes',
        'annualCrossAssetRebalance': False, 'futureContractsObserved': False,
        'principalCountedAsIncome': False}
    return combined


def drifting_risk_model(factors, plan):
    wealth = factors['wealth_factors']
    income = factors['annual_income_factors']
    weighted_income = factors.get('annual_weighted_income_factors')
    if weighted_income is None:
        if np.any(income):
            raise ValueError('Risco com pagamentos exige ponderacao temporal dos saques de renda.')
        weighted_income = np.zeros_like(income)
    if wealth.shape[2] < 2:
        raise ValueError('Risco exige pelo menos dois cenarios independentes.')
    funded_wealth = plan.initial_capital * wealth[0] + plan.monthly_contribution * wealth[1]
    funded_income = plan.initial_capital * income[0] + plan.monthly_contribution * income[1]
    funded_weighted_income = plan.initial_capital * weighted_income[0] + plan.monthly_contribution * weighted_income[1]
    accumulation = plan.retirement_age - plan.age
    contributions = np.zeros(income.shape[1])
    contributions[:accumulation] = 12 * plan.monthly_contribution
    weighted_flows = contributions * (5.5 / 12)
    # Modified Dietz with end-of-month external deposits and income withdrawals.
    # Monthly rather than daily weights are an explicit scenario approximation:
    # https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/
    def returns(numerator, denominator):
        invalid = (denominator <= 1e-12) & (np.abs(numerator) > 1e-12)
        values = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-12)
        return values, invalid.any(axis=(1, 2))
    def risk(weights):
        weights = np.atleast_2d(weights)
        unit = np.einsum('ca,yna->cyn', weights, wealth[0])
        paid = np.einsum('ca,yna->cyn', weights, income[0])
        weighted_paid = np.einsum('ca,yna->cyn', weights, weighted_income[0])
        unit_returns, invalid_unit = returns(unit[:, 1:] + paid - unit[:, :-1], unit[:, :-1] - weighted_paid)
        values = np.einsum('ca,yna->cyn', weights, funded_wealth)
        paid = np.einsum('ca,yna->cyn', weights, funded_income)
        weighted_paid = np.einsum('ca,yna->cyn', weights, funded_weighted_income)
        sums = weights.sum(axis=1)[:, None, None]
        denominator = values[:, :-1] + sums * weighted_flows[None, :, None] - weighted_paid
        numerator = values[:, 1:] + paid - values[:, :-1] - sums * contributions[None, :, None]
        funded_returns, invalid_funded = returns(numerator, denominator)
        result = np.maximum(np.std(unit_returns, axis=2, ddof=1).max(axis=1),
                            np.std(funded_returns, axis=2, ddof=1).max(axis=1))
        result[invalid_unit | invalid_funded] = np.inf
        return result
    return risk


def compute_current(request, dataset, *, folder=None, generations=24, population=32, search_count=96):
    from .engine import bounds_for, optimization_shortlist, project_weights
    from .linear_scenarios import inverse_planning_factors, simulate_factors
    calendar_conflict_engine_warnings(dataset.metadata, [asset.ticker for asset in dataset.assets])
    root, plan, investor = market_root(folder), request.plan, request.investor
    instruments, receipt = read_current_instruments(root)
    factor_history = load_treasury_yield_factors(root, dataset.dates)
    if not np.allclose(factor_history['inflation'], dataset.inflation, rtol=1e-10, atol=1e-12):
        raise ValueError('Fontes historicas de inflacao divergem; nao foram misturadas silenciosamente.')
    contract_volatility = contract_risk(instruments, factor_history, search_count, plan.block_months, plan.seed)
    treasury_analysis = analyze_current_treasury(request, dataset, root)
    treasury_by_ticker = {row['ticker']: row for row in treasury_analysis['rows']}
    stock_indices = [index for index, asset in enumerate(dataset.assets) if asset.asset_class == 'acoes']
    stocks, excluded, eligible_stock_indices = [], [], []
    for index in stock_indices:
        asset = dataset.assets[index]
        reasons = profile_reasons(asset, investor)
        try:
            current = _current_stock(asset)
        except ValueError as error:
            reasons.append(('Cotacao observada em ' + asset.current_quote.as_of[:10] +
                ' fora do prazo de atualidade de 7 dias; mantida para consulta, sem indicacao de compra.')
                if 'reference_stale' in str(error) and asset.current_quote else
                'Cotacao atual de compra ou escala por unidade ainda nao conciliada; historico mantido para consulta.')
            current = asset
        if reasons:
            excluded.append({'ticker': asset.ticker, 'reasons': reasons})
        else:
            stocks.append(current)
            eligible_stock_indices.append(index)
    eligible_contract_indices = []
    for index, item in enumerate(instruments):
        reasons = profile_reasons(item.asset, investor)
        if item.pricing.get('institution_restriction'):
            reasons.append('Compra exige acesso pelo Banco do Brasil, nao confirmado no questionario.')
        if reasons:
            excluded.append({'ticker': item.asset.ticker, 'reasons': reasons})
        else:
            eligible_contract_indices.append(index)
    all_assets = [*stocks, *[instruments[index].asset for index in eligible_contract_indices]]
    if not all_assets:
        raise ValueError('Nenhum ativo atual elegivel; os limites nao foram reduzidos.')
    forecasts, metrics, model = {}, None, None
    if stocks:
        model, base_metrics = get_forecast('real', dataset=dataset)
        experimental = request.forecast_policy == 'xgboost_experimental'
        use_xgb = experimental or base_metrics['selectedMethod'] == 'xgboost'
        method = 'xgboost' if use_xgb else 'baseline'
        predict = getattr(model, 'predict_xgboost', model.predict) if experimental else model.predict
        values = predict(np.asarray([asset.features for asset in stocks], dtype=float)) if use_xgb else [a.features[0] for a in stocks]
        forecasts = {a.ticker: max(0., float(v)) for a, v in zip(stocks, values)}
        metrics = {**base_metrics, 'appliedMethod': method, 'usedForIncomeSimulation': True,
            'applicationPolicy': request.forecast_policy, 'forecastHorizonMonths': 12,
            'longTermPolicy': 'historical_joint_bootstrap_after_month_12',
            'appliedTestMae': base_metrics['xgboostMae'] if use_xgb else base_metrics['baselineMae'],
            'forecastedAssets': len(stocks), 'forecastedTickers': list(forecasts)}
    ranking = []
    if stocks:
        indices = eligible_stock_indices
        real = (1 + dataset.returns[:, indices]) / (1 + dataset.inflation[:, None]) - 1
        ranking = asset_priority(stocks, real, dataset.cash_returns[:, indices], dataset.dates,
            np.array([forecasts[a.ticker] / a.price for a in stocks]))
    for index in eligible_contract_indices:
        item = instruments[index]
        analysis = treasury_by_ticker[item.asset.ticker]
        ranking.append({'ticker': item.asset.ticker, 'name': item.asset.name, 'assetClass': item.asset.asset_class,
            'riskLevel': item.asset.risk_level, 'annualVolatility': contract_volatility[item.asset.ticker],
            'cashYield12m': max(0., analysis['income12MonthsReal'][1] / item.asset.price),
            'qualityAlerts': [], 'historicalPaymentMonths': [], 'historicalConsistency': None,
            'paymentHistoryMonths': 0, 'cashBasis': 'projected_current_lot_not_observed_historical_payments'})
    retirement_date = anniversary(instruments[0].settlement, 12 * (plan.retirement_age - plan.age))
    chosen_contracts = contract_shortlist(instruments, eligible_contract_indices, retirement_date)
    chosen_tickers = {instruments[index].asset.ticker for index in chosen_contracts}
    shortlist_assets = [asset for asset in all_assets if asset.asset_class == 'acoes' or asset.ticker in chosen_tickers]
    rank_by_ticker = {row['ticker']: row for row in ranking}
    ranking.sort(key=lambda row: (row['riskLevel'], len(row['qualityAlerts']), row['annualVolatility'],
                                  -row['cashYield12m'], row['ticker']))
    for index, row in enumerate(ranking, 1):
        row['safetyOrder'] = index
    shortlist_ranking = [row for row in ranking if row['ticker'] in {a.ticker for a in shortlist_assets}]
    positions = optimization_shortlist(shortlist_assets, shortlist_ranking)
    assets = [shortlist_assets[index] for index in positions]
    admitted = {asset.ticker for asset in assets}
    deferred = [{'ticker': asset.ticker, 'code': 'bounded_current_contract_and_stock_shortlist',
        'reasons': ['Ativo elegivel adiado da busca limitada: um vencimento por familia proximo da aposentadoria, seguido de acoes por seguranca, renda e calendario.']}
        for asset in all_assets if asset.ticker not in admitted]
    for row in ranking:
        row.update(optimized=row['ticker'] in admitted, predictedCashPerShare=forecasts.get(row['ticker']))
    cash = np.array([rank_by_ticker[a.ticker]['cashYield12m'] for a in assets])
    search = scenario_factors(dataset, stock_indices, assets, instruments, forecasts, model, metrics,
                             plan, search_count, plan.seed, factor_history)
    risk_model = drifting_risk_model(search, plan)
    risk = lambda weights: risk_model(weights)
    upper, matrix, limits, feasible, equity_cap, liquid_min = bounds_for(assets, investor.risk_level)
    max_volatility = [.08, .16, .28][investor.risk_level]
    optimized_ranking = [row for row in ranking if row['ticker'] in admitted]
    references = candidate_seeds(assets, optimized_ranking, cash, upper, matrix, limits, feasible,
                                 risk, max_volatility, project_weights)
    class PortfolioRepair(Repair):
        def _do(self, problem, x, **kwargs):
            return np.array([project_weights(w, upper, matrix, limits, feasible) for w in x])
    class PortfolioProblem(Problem):
        def __init__(self):
            super().__init__(n_var=len(assets), n_obj=3, n_ieq_constr=1, xl=0, xu=upper)
        def _evaluate(self, x, out, *args, **kwargs):
            outcome = simulate_factors(x, search, plan)
            out['F'] = np.column_stack([outcome['unfundedIncomeFraction'], risk(x), -x @ cash])
            out['G'] = (risk(x) - max_volatility)[:, None]
    candidates = references
    if len(assets) > 1:
        rng, sampling = np.random.default_rng(plan.seed), list(references)
        while len(sampling) < population:
            sampling.append(rng.dirichlet(np.ones(len(assets))))
        result = minimize(PortfolioProblem(), NSGA2(pop_size=population, sampling=np.asarray(sampling[:population]),
            repair=PortfolioRepair(), eliminate_duplicates=True), ('n_gen', generations), seed=plan.seed, verbose=False)
        if result.X is not None:
            order = np.lexsort((np.atleast_2d(result.F)[:, 2], np.atleast_2d(result.F)[:, 1], np.atleast_2d(result.F)[:, 0]))
            candidates = feasible_unique([*references, *np.atleast_2d(result.X)[order[:6]]],
                upper, matrix, limits, risk, max_volatility)
    if not len(candidates):
        raise ValueError('Nenhuma carteira atual respeita os limites de risco e liquidez.')
    final_seed = (plan.seed + 104729) % 2_000_000_001
    final = scenario_factors(dataset, stock_indices, assets, instruments, forecasts, model, metrics,
                            plan, plan.simulations, final_seed, factor_history)
    risk_model = drifting_risk_model(final, plan)
    candidates = feasible_unique(candidates, upper, matrix, limits, risk, max_volatility)
    if not len(candidates):
        raise ValueError('Nenhuma carteira manteve os limites nos cenarios finais independentes.')
    outcomes = simulate_factors(candidates, final, plan)
    vol = risk(candidates)
    order = np.lexsort((-candidates @ cash, vol, outcomes['unfundedIncomeFraction'], -outcomes['success']))
    choices, selected_id = comparison_choices(candidates, vol, cash, int(order[0]))
    portfolios = []
    for safety_rank, (index, identifier, label) in enumerate(choices, 1):
        weights = candidates[index]
        detail = simulate_factors(weights, final, plan, detailed=True)
        detail.pop('success', None)
        detail.pop('unfundedIncomeFraction', None)
        allocation = [{'ticker': a.ticker, 'name': a.name, 'assetClass': a.asset_class, 'weight': float(w),
            'percent': float(w * 100), 'cashYield12m': float(c), 'predictedCashPerShare': forecasts.get(a.ticker),
            'source': a.source, 'asOf': a.as_of,
            'cashMethod': ('XGBoost: previsao de pagamentos em 12 meses' if metrics['appliedMethod'] == 'xgboost'
                           else 'Referencia de pagamentos anteriores') if a.asset_class == 'acoes' else
                          'Fluxo contratual do lote atual; capital devolvido nao e renda',
            'reason': 'Compativel com risco, prazo e liquidez; quantidade inicial usa cotacao atual de referencia.'}
            for a, w, c in zip(assets, weights, cash)]
        calendar, covered = payment_calendar(assets, weights, ranking)
        portfolios.append({'id': identifier, 'label': label, 'safetyRank': safety_rank, 'allocation': allocation,
            'simulation': detail, 'planning': inverse_planning_factors(weights, final, plan,
                retirement_unit_income_factors=final['retirement_unit_annual_income_factors']),
            'portfolio': {'annualVolatility': float(vol[index]), 'cashYield12m': float(weights @ cash),
                          'usesFixedReturnRate': False, 'baselineSuccessFrequency': None},
            'incomeAnalysis': {'method': 'contract_lots_and_equity_distributions', 'status': 'experimental',
                'usesPrincipal': False, 'monthlyCapacity': detail['monthlyIncomeCapacity'],
                'targetMonthlyIncome': plan.target_income,
                'medianMonthlyGap': max(0, plan.target_income - detail['monthlyIncomeCapacity'][1]),
                'endAge': plan.end_age, 'isDividendForecast': True},
            'coverage': {'catalogAssets': len(stock_indices) + len(instruments), 'eligibleAssets': len(all_assets),
                'optimizedAssets': len(assets), 'deferredAssets': len(deferred),
                'allocatedAssets': int(np.sum(weights > 1e-6)), 'multiplePositions': bool(np.sum(weights > 1e-6) > 1),
                'status': 'bounded_current_quote_universe'},
            'historicalPaymentCalendar': calendar, 'historicalCoverageMonths': covered,
            'usesEquityForecast': bool(any(a.asset_class == 'acoes' and w > 1e-6 for a, w in zip(assets, weights)))})
    selected = next(row for row in portfolios if row['id'] == selected_id)
    return _assemble(request, dataset, instruments, receipt, factor_history, treasury_analysis,
        eligible_stock_indices, all_assets, assets, forecasts, metrics, ranking, excluded, deferred,
        selected, portfolios, final, candidates, outcomes, vol, order,
        generations, population, search_count, final_seed, equity_cap, liquid_min, max_volatility,
        contract_volatility)


def _assemble(request, dataset, instruments, receipt, factor_history, treasury_analysis,
              eligible_stock_indices, all_assets, assets, forecasts, metrics, ranking, excluded, deferred,
              selected, portfolios, final, candidates, outcomes, vol, order,
              generations, population, search_count, final_seed, equity_cap, liquid_min, max_volatility,
              contract_volatility):
    from .engine import ENGINE_VERSION, MAX_OPTIMIZATION_ASSETS, asset_diagnostics
    admitted = {a.ticker for a in assets}
    diagnostics = asset_diagnostics(dataset, eligible_stock_indices, excluded, (), deferred, forecasts)
    diagnostics = [row for row in diagnostics if not row['ticker'].startswith('TD-')]
    current_stocks = {asset.ticker: asset for asset in all_assets if asset.asset_class == 'acoes'}
    for row in diagnostics:
        row['optimized'] = row['ticker'] in admitted
        quote = row.get('currentQuote')
        if quote and quote.get('price') is not None:
            row['price'], row['asOf'] = quote['price'], quote['as_of'][:10]
            if row['indicators']:
                indicators = row['indicators']
                profit = indicators['earningsPerShare']
                previous = indicators.get('previousAnnualCashPerShare', indicators.get('previousAnnualRightsPerShare'))
                indicators['priceEarnings'] = row['price'] / profit if profit > 0 else None
                if previous is not None:
                    indicators['trailingDistributionYield'] = previous / row['price']
                    row['annualCashPaymentsYield'] = previous / row['price']
    excluded_by_ticker = {row['ticker']: row['reasons'] for row in excluded}
    eligible_tickers = {asset.ticker for asset in all_assets}
    for item in instruments:
        row = next(row for row in treasury_analysis['rows'] if row['ticker'] == item.asset.ticker)
        row['includedInPortfolioOptimization'] = item.asset.ticker in admitted
        row['scenarioAnnualVolatility'] = contract_volatility[item.asset.ticker]
        diagnostics.append({'ticker': item.asset.ticker, 'name': item.asset.name, 'price': item.asset.price,
            'currentQuote': item.asset.current_quote.model_dump(), 'eligible': item.asset.ticker in eligible_tickers,
            'exclusionReasons': excluded_by_ticker.get(item.asset.ticker, []), 'optimized': item.asset.ticker in admitted,
            'riskLevel': item.asset.risk_level, 'minimumYears': item.asset.minimum_years,
            'liquidityDays': item.asset.liquidity_days, 'historicalAnnualRealReturn': None,
            'historicalAnnualVolatility': None, 'scenarioAnnualVolatility': contract_volatility[item.asset.ticker],
            'historyMonths': 0, 'source': item.asset.source, 'asOf': item.asset.as_of,
            'annualCashPaymentsYield': None, 'projectedRealIncomeYield12m': row['income12MonthsReal'][1] / item.asset.price,
            'indicators': None, 'financialMethod': None, 'distributionBasis': 'projected_contract_not_historical_payment'})
    treasury_analysis.update(optimizedContracts=sum(a.asset_class != 'acoes' for a in assets),
        scope='current_lot_cash_analysis_and_separate_stateful_portfolio_lots',
        optimizationIntegrationStatus='contract_lots_and_same_family_reinvestment_integrated',
        portfolioReinvestmentIncluded=True, futureOffersObserved=False)
    counts = {'eligibleAssets': len(all_assets), 'optimizedAssets': len(assets), 'deferredAssets': len(deferred),
        'eligibleStocks': len(current_stocks), 'forecastedStocks': len(forecasts),
        'optimizedStocks': sum(a.asset_class == 'acoes' for a in assets),
        'preservedAnchors': sum(a.asset_class != 'acoes' for a in assets)}
    bonds = [{'ticker': item.asset.ticker, 'kind': item.offer['kind'], 'maturity': item.offer['maturity'],
              'quote_date': item.offer['as_of'], 'buy_rate': item.offer.get('buy_rate'),
              'coupon': bool(item.spec and item.spec['terms']['coupon_annual_rate_percent'] != '0'),
              'history_created': False} for item in instruments]
    warnings = [
        'Simulacao educacional, sem validacao financeira profissional ou garantia de renda.',
        'Somente dividendos e juros compoem a renda. Devolucao do custo real de aquisicao e reinvestida, nao sacada como renda.',
        'A carteira inicial usa cotacoes de referencia atuais, nao ofertas executadas. As quantidades sao indicativas, sem envio de ordens.',
        'Cada posicao conserva sua classe e risco no reinvestimento. Nao ha rebalanceamento anual entre ativos; os pesos podem variar com o mercado.',
        'Titulos futuros sao contratos hipoteticos da mesma familia com prazo em uma faixa anual do contrato inicial, nao ofertas futuras observadas.',
        'O Tesouro usa datas contratuais e calendario QuantLib. VNA/VNI de referencia sao derivados da cotacao, nao valores oficiais observados.',
        'Choques de taxas usam medianas historicas de familias do Tesouro, nao curvas de prazo constante. Renda+ e Educa+ usam a familia IPCA como proxy.',
        'Os juros e a inflacao sao reamostrados junto com as acoes. As taxas projetadas sao limitadas ao intervalo empirico da familia, ajustado a cotacao atual.',
        'Renda mensal e a media dos pagamentos de cada ano, nao promessa de recebimento mensal. Titulos sem cupom pagam juros no resgate, nao mensalmente.',
        'Os custos anuais sao uma hipotese efetiva; nao ha calculo de tributacao individual, IOF ou faixas oficiais de custodia.',
        'As datas de coleta nao comprovam disponibilidade historica. Os calendarios de acoes sao reconciliados, sem certificacao de liquidacao bancaria.',
        'O risco agrega as posicoes com seus pesos efetivamente variaveis antes de calcular os retornos anuais. Usa o maior desvio entre a unidade inicial e o plano financiado; aportes usam a aproximacao Modified Dietz. Nao e garantia de seguranca.',
        'O patrimonio-alvo usa uma posicao nova hipotetica na aposentadoria, nas mesmas classes e pesos. Os aportes usam seus proprios lotes; ambos exigem caixa suficiente em cada ano.',
    ]
    preparation = dataset.metadata['stock_universe_preparation']
    if dataset.metadata.get('cash_calendar_date_qualifications'):
        warnings.append('Algumas datas historicas indicam o inicio declarado de pagamento de proventos, nao a quitacao integral comprovada naquele dia. O calendario usa essa referencia declarada; os cenarios nao comprovam recebimentos efetivos nessa data.')
    warnings.extend(calendar_conflict_engine_warnings(dataset.metadata, [asset.ticker for asset in dataset.assets]))
    warnings.append(f"{preparation['ready_stocks']} de {preparation['evaluated_stocks']} codigos de acoes preparados; {preparation['pending_stocks']} ainda pendentes. Nenhum dado ausente foi inventado.")
    if metrics:
        warnings.append('O XGBoost influencia os proventos dos primeiros 12 meses e a busca NSGA-II; nao e extrapolado por decadas. O desempenho fora da amostra permanece apresentado, mesmo quando inferior a referencia.')
    if 12 * (request.plan.end_age - request.plan.age) > len(dataset.dates):
        warnings.append('O horizonte excede o historico observado. As trajetorias extrapolam essa amostra por reamostragem, nao por uma previsao aprendida para todas essas decadas.')
    warnings.append('Os limites de alocacao se aplicam a carteira inicial. Sem rebalanceamento entre ativos, as proporcoes futuras podem ultrapassar esses percentuais por oscilacoes de mercado; os cenarios nao sao uma ordem automatica de manutencao da carteira.')
    if deferred:
        warnings.append(f'{len(deferred)} ativos compativeis permanecem adiados da otimizacao limitada, nao excluidos por risco. A selecao de vencimentos e feita antes dos cenarios de busca.')
    if selected['simulation']['successFrequency'] < .8:
        warnings.append('A meta nao foi sustentada em 80% dos cenarios. O sistema nao aumentou o risco para tentar compensar a meta.')
    data = {
        'status': 'ready', 'engineVersion': ENGINE_VERSION, 'dataMode': 'real', 'datasetSource': dataset.source,
        'model': metrics,
        'referenceDate': dataset.dates[-1], 'dataRevision': current_data_revision(dataset), 'plan': request.plan.model_dump(),
        'forecastTarget': dataset.metadata.get('forecast_target'), 'currentTreasuryAnalysis': treasury_analysis,
        'marketData': {'collectedAt': dataset.metadata.get('collected_at'),
            'cashflowBasis': dataset.metadata.get('cashflow_basis'), 'dataQualityPolicy': dataset.metadata.get('data_quality_policy'),
            'pointInTimeArchiveVerified': False, 'cashSettlementVerified': False,
            'cashflowTaxBasis': 'gross_before_individual_taxes',
            'totalReturnMethod': 'observed_equity_returns_and_forward_treasury_lots',
            'currentTreasuryOffers': [item.offer for item in instruments],
            'historyStart': dataset.dates[0], 'historyMonths': len(dataset.dates), 'bonds': bonds,
            'sources': [*dataset.metadata.get('sources', []), receipt, *factor_history['sources']]},
        'assetAnalysis': diagnostics, 'assetRanking': ranking, 'excluded': excluded,
        'deferredFromOptimization': deferred, 'dataCoverage': current_coverage(dataset, instruments),
        'universeScreening': screen_universe(dataset, request.investor, eligible_stock_indices, forecasts),
        'rollover': {'policy': 'same_class_same_risk', 'events': [], 'monthlyApproximation': True,
            'contractualRedemptionPricing': True, 'principalCountedAsIncome': False,
            'unknownFutureInstruments': 'explicit_hypothetical_same_family_contracts',
            'actualExecutionOrSettlement': False},
        'parameters': {'population': population if len(assets) > 1 else 0,
            'generations': generations if len(assets) > 1 else 0, 'searchScenarios': search_count,
            'finalScenarios': request.plan.simulations, 'searchSeed': request.plan.seed, 'finalSeed': final_seed,
            'blockMonths': request.plan.block_months, 'annualRebalance': False,
            'volatilityBasis': 'maximum_annual_drifting_portfolio_unit_and_funded_modified_dietz',
            'equityMaximum': equity_cap, 'liquidityMinimum': liquid_min, 'stockMaximum': .25,
            'issuerMaximum': .25, 'issuerConstraintBasis': 'explicit_issuer_id_stocks_only',
            'annualVolatilityMaximum': max_volatility},
        'construction': {'method': POLICY, 'referenceProject': 'dividendos-br', 'candidateCount': len(candidates),
            'rankingRule': 'risk_quality_then_historical_stock_or_scenario_contract_volatility_income',
            'portfolioOrder': 'volatility_ascending_then_income_descending',
            'calendarBasis': 'observed_stock_payment_months_treasury_contracts_separate', 'calendarThreshold': .5,
            'commonFinalSeed': final_seed, 'commonFinalScenarios': request.plan.simulations, 'safetyIsGuarantee': False,
            'optimizationUniverse': {'maxAssets': MAX_OPTIMIZATION_ASSETS, **counts, 'counts': counts,
                'selectedTickers': [a.ticker for a in assets], 'admittedTickers': [a.ticker for a in assets],
                'deferredTickers': [row['ticker'] for row in deferred],
                'rule': 'one_maturity_per_family_near_retirement_then_ranked_equities',
                'usesSimulationOutcomes': False, 'seedIndependent': True}},
        'methods': ['Content-Based Filtering', *(['XGBoost'] if forecasts else []),
                    'NSGA-II' if len(assets) > 1 else 'Alocacao no unico ativo elegivel', 'Monte Carlo'],
        'warnings': warnings, 'portfolios': portfolios, 'selectedPortfolioId': selected['id'],
        'financiallyValidated': False, 'scenarioModel': final['provenance'],
        'alternatives': [{'successFrequency': float(outcomes['success'][i]), 'annualVolatility': float(vol[i]),
            'meanUnfundedIncomeFraction': float(outcomes['unfundedIncomeFraction'][i]),
            'cashYield12m': float(candidates[i] @ np.array([next(row['cashYield12m'] for row in ranking
                if row['ticker'] == a.ticker) for a in assets])),
            'weights': [{'ticker': a.ticker, 'percent': float(w * 100)} for a, w in zip(assets, candidates[i])]}
            for i in order[:3]],
    }
    for key in ('allocation', 'simulation', 'planning', 'portfolio', 'incomeAnalysis', 'coverage',
                'historicalPaymentCalendar', 'historicalCoverageMonths', 'usesEquityForecast'):
        data[key] = selected[key]
    return data
