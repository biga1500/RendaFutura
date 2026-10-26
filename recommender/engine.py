import os

import numpy as np
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import Problem
from pymoo.core.repair import Repair
from pymoo.optimize import minimize
from scipy.optimize import linprog, minimize as scipy_minimize

from .contracts import RecommendationRequest
from .datasets import get_dataset
from .data_coverage import coverage_summary
from .forecast import get_forecast
from .forecast_paths import forecast_paths
from .rollover import apply_rollover, rollover_schedule, worst_volatility
from .planning import inverse_planning
from .portfolios import asset_priority, candidate_seeds, comparison_choices, feasible_unique, payment_calendar
from .simulation import bootstrap_cash_paths, bootstrap_paths, simulate
from .universe import profile_reasons, screen_universe
from .treasury_analysis import analyze_current_treasury, current_data_revision
from .cash_calendar_research import calendar_conflict_engine_warnings

ENGINE_VERSION = "RF-MOTOR-1.13"
MAX_OPTIMIZATION_ASSETS = 24
MAX_OPTIMIZATION_ANCHORS = 12
SHORTLIST_RULE = "all_eligible_anchors_then_round_robin_stock_safety_income_payment_months_sector"
SHORTLIST_REASON = "Ativo elegivel adiado pelo limite computacional da otimizacao; nao e exclusao por risco."


def eligible_assets(dataset, investor):
    selected, excluded = [], []
    for index, asset in enumerate(dataset.assets):
        reasons = profile_reasons(asset, investor)
        if asset.asset_class == "acoes" and asset.features is None:
            reasons.append("Dados insuficientes para estimar proventos.")
        if asset.asset_class != "acoes" and asset.annual_cash_yield is None:
            reasons.append("Fluxo contratual de caixa não documentado.")
        if reasons:
            excluded.append({"ticker": asset.ticker, "reasons": reasons})
        else:
            selected.append(index)
    return selected, excluded


def bounds_for(assets, level):
    equity = np.array([a.asset_class == "acoes" for a in assets], dtype=float)
    liquid = np.array([a.asset_class == "liquidez" for a in assets], dtype=float)
    upper = np.array([0.25 if a.asset_class == "acoes" else 1.0 for a in assets])
    equity_cap = [0, 0.4, 0.65][level]
    liquid_min = [0.25, 0.15, 0.1][level]
    rows, limits = [equity, -liquid], [equity_cap, -liquid_min]
    issuers = {}
    for index, asset in enumerate(assets):
        if asset.asset_class == "acoes" and asset.issuer_id is not None:
            issuers.setdefault(asset.issuer_id, []).append(index)
    for issuer in sorted(issuers):
        members = issuers[issuer]
        if len(members) > 1:
            row = np.zeros(len(assets))
            row[members] = 1
            rows.append(row)
            limits.append(0.25)
    matrix, bounds = np.asarray(rows), np.asarray(limits)
    solution = linprog(np.zeros(len(assets)), A_ub=matrix, b_ub=bounds,
                       A_eq=np.ones((1, len(assets))), b_eq=[1], bounds=list(zip(np.zeros(len(assets)), upper)), method="highs")
    if not solution.success:
        raise ValueError("Não há carteira viável dentro dos limites de risco e liquidez; as restrições não foram reduzidas.")
    return upper, matrix, bounds, solution.x, equity_cap, liquid_min


def project_weights(values, upper, matrix, bounds, feasible):
    constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1, "jac": lambda w: np.ones(len(w))},
                   {"type": "ineq", "fun": lambda w: bounds - matrix @ w, "jac": lambda w: -matrix}]
    result = scipy_minimize(lambda w: np.sum((w-values)**2), feasible,
                            jac=lambda w: 2*(w-values), method="SLSQP",
                            bounds=list(zip(np.zeros(len(values)), upper)), constraints=constraints,
                            options={"maxiter": 60, "ftol": 1e-10})
    if result.success and abs(result.x.sum()-1) < 1e-6 and np.all(matrix @ result.x <= bounds+1e-7):
        return result.x
    return feasible.copy()


def optimization_shortlist(assets, ranking):
    """Return positions in the eligible asset list, before any scenario allocation."""
    anchors = [i for i, asset in enumerate(assets) if asset.asset_class != "acoes"]
    if len(anchors) > MAX_OPTIMIZATION_ANCHORS:
        raise ValueError("Mais de 12 ancoras elegiveis: amplie o limite explicitamente antes de otimizar; nenhum titulo foi descartado.")
    if len(assets) <= MAX_OPTIMIZATION_ASSETS:
        return list(range(len(assets)))
    by_ticker = {asset.ticker: i for i, asset in enumerate(assets)}
    safety = sorted((row for row in ranking if row["assetClass"] == "acoes"),
                    key=lambda row: row["safetyOrder"])
    income = sorted(safety, key=lambda row: (-row["cashYield12m"], row["safetyOrder"], row["ticker"]))
    selected = set(anchors)
    covered = {month for row in ranking if by_ticker[row["ticker"]] in selected
               for month in row["historicalPaymentMonths"]}
    sectors = set()
    for slot in range(MAX_OPTIMIZATION_ASSETS - len(anchors)):
        remaining = [row for row in safety if by_ticker[row["ticker"]] not in selected]
        if slot % 3 == 0:
            row = remaining[0]
        elif slot % 3 == 1:
            row = next(row for row in income if by_ticker[row["ticker"]] not in selected)
        else:
            row = min(remaining, key=lambda item: (
                -len(set(item["historicalPaymentMonths"]) - covered),
                assets[by_ticker[item["ticker"]]].features[6] in sectors,
                item["safetyOrder"], -item["cashYield12m"], item["ticker"]))
        index = by_ticker[row["ticker"]]
        selected.add(index)
        covered.update(row["historicalPaymentMonths"])
        sectors.add(assets[index].features[6])
    # Keep the original column order; ranking positions are never dataset indices.
    return sorted(selected)


def asset_diagnostics(dataset, indices, excluded, optimized_indices=(), deferred=(), forecasts=None):
    excluded_by_ticker = {row["ticker"]: row["reasons"] for row in excluded}
    deferred_by_ticker = {row["ticker"]: row["reasons"] for row in deferred}
    eligible, optimized = set(indices), set(optimized_indices)
    forecasts = forecasts or {}
    paid_basis = (dataset.metadata.get("forecast_target") or {}).get("event_basis") == "payment_date"
    output = []
    for index, asset in enumerate(dataset.assets):
        real = (1 + dataset.returns[:, index]) / (1 + dataset.inflation) - 1
        indicators = None
        if asset.features is not None:
            previous, regularity, profit, free_cash, payout, debt, _ = asset.features
            indicators = {("previousAnnualCashPerShare" if paid_basis else "previousAnnualRightsPerShare"): previous,
                          "trailingDistributionYield": previous / asset.price,
                          "distributionMonthFraction": regularity, "earningsPerShare": profit,
                          "freeCashFlowPerShare": free_cash, "payout": payout,
                          "freeCashFlowAbsenceReason": ("industrial_fcf_not_applicable_to_financial_issuer"
                                                       if free_cash is None else None),
                          "liabilitiesToAssets": debt,
                          "priceEarnings": asset.price / profit if profit > 0 else None}
        output.append({"ticker": asset.ticker, "name": asset.name, "price": asset.price,
                       "currentQuote": asset.current_quote.model_dump() if asset.current_quote else None,
                       "distributionBasis": "payment_date" if paid_basis else "record_date",
                       "annualCashPaymentsYield": (asset.features[0] / asset.price if asset.features is not None else asset.annual_cash_yield) if paid_basis else None,
                       "eligible": index in eligible, "exclusionReasons": excluded_by_ticker.get(asset.ticker, []),
                       "optimized": index in optimized,
                       "optimizationDeferralReasons": deferred_by_ticker.get(asset.ticker, []),
                       "predictedCashPerShare": forecasts.get(asset.ticker),
                       "indicators": indicators, "financialMethod": asset.financial_method,
                       "riskLevel": asset.risk_level,
                       "minimumYears": asset.minimum_years, "liquidityDays": asset.liquidity_days,
                       "historicalAnnualRealReturn": float(np.expm1(np.log1p(real).mean() * 12)),
                       "historicalAnnualVolatility": float(np.std(real, ddof=1) * np.sqrt(12)),
                       "historyMonths": len(real), "source": asset.source, "asOf": asset.as_of})
    return output


def compute(request: RecommendationRequest, *, generations=24, population=32, search_count=96):
    dataset = get_dataset(request.data_mode)
    calendar_warnings = calendar_conflict_engine_warnings(dataset.metadata, [asset.ticker for asset in dataset.assets])
    if (os.environ.get('RF_CURRENT_CONTRACT_PORTFOLIOS') == '1' and dataset.mode == 'real'
            and request.plan.income_mode == 'distributions_only'):
        from .current_portfolio import available, compute_current
        if not available(dataset, request.plan):
            raise ValueError('Base atual de contratos do Tesouro indisponivel; nao foi substituida por titulos antigos.')
        return compute_current(request, dataset, generations=generations, population=population, search_count=search_count)
    data_coverage = coverage_summary(dataset) if dataset.mode == "real" else None
    plan, investor = request.plan, request.investor
    indices, excluded = eligible_assets(dataset, investor)
    if not indices:
        raise ValueError("Poucos ativos elegíveis. Amplie a base financeira sem alterar o perfil do usuário.")
    assets = [dataset.assets[i] for i in indices]
    cash_history = None
    if plan.income_mode == "distributions_only" and dataset.mode == "real":
        if dataset.cash_returns is not None:
            cash_history = dataset.cash_returns[:, indices]
        elif all(a.asset_class != "acoes" and a.annual_cash_yield == 0 for a in assets):
            cash_history = np.zeros((len(dataset.dates), len(assets)))
        else:
            missing = [a.ticker for a in assets if a.asset_class == "acoes" or a.annual_cash_yield != 0]
            return {"status": "income_data_unavailable", "engineVersion": ENGINE_VERSION,
                    "dataMode": dataset.mode, "dataRevision": current_data_revision(dataset), "referenceDate": dataset.dates[-1], "plan": plan.model_dump(),
                    "message": "Nao e possivel estimar renda apenas de dividendos e juros com esta base: faltam pagamentos efetivos documentados. Direitos por data-com nao sao caixa recebido.",
                    "missingCashAssets": missing, "assetAnalysis": asset_diagnostics(dataset, indices, excluded),
                    "excluded": excluded, "deferredFromOptimization": [], "methods": ["Content-Based Filtering"],
                    "coverage": {"catalogAssets": len(dataset.assets), "eligibleAssets": len(assets),
                                 "optimizedAssets": 0, "deferredAssets": 0,
                                 "allocatedAssets": 0, "status": "missing_payment_history", "multiplePositions": False},
                    "dataCoverage": data_coverage, "universeScreening": screen_universe(dataset, investor, indices),
                    "datasetSource": dataset.source, "financiallyValidated": False,
                    "requiresPrincipalWithdrawal": False}
    cash, forecasts, metrics, model = [], {}, None, None
    stocks = [a for a in assets if a.asset_class == "acoes"]
    if stocks:
        model, metrics = get_forecast(request.data_mode, dataset=dataset)
        experimental = request.forecast_policy == "xgboost_experimental"
        apply_xgboost = experimental or metrics["selectedMethod"] == "xgboost"
        metrics = {**metrics, "appliedMethod": "xgboost" if apply_xgboost else "baseline",
                   "usedForIncomeSimulation": plan.income_mode == "distributions_only",
                   "applicationPolicy": request.forecast_policy,
                   "forecastHorizonMonths": 12,
                   "longTermPolicy": "historical_joint_bootstrap_after_month_12",
                   "appliedTestMae": metrics["xgboostMae"] if apply_xgboost else metrics["baselineMae"]}
        if apply_xgboost:
            predictor = getattr(model, "predict_xgboost", model.predict) if experimental else model.predict
            predictions = predictor(np.asarray([asset.features for asset in stocks], dtype=float))
        else:
            predictions = [asset.features[0] for asset in stocks]
        forecasts = {asset.ticker: max(0, float(value)) for asset, value in zip(stocks, predictions)}
        metrics = {**metrics, "forecastedAssets": len(stocks), "forecastedTickers": [a.ticker for a in stocks]}
    for asset in assets:
        cash.append(forecasts[asset.ticker] / asset.price if asset.asset_class == "acoes" else asset.annual_cash_yield)
    cash = np.asarray(cash)
    if plan.income_mode == "distributions_only":
        if cash_history is None:
            # Synthetic data only: an explicit demonstration assumption, never a real-data fallback.
            cash_history = np.broadcast_to(cash / 12, (len(dataset.dates), len(assets))).copy()
        cash = np.array([forecasts[a.ticker] / a.price if a.ticker in forecasts else
                         cash_history[:, i].mean() * 12 for i, a in enumerate(assets)])
    history = dataset.returns[:, indices]
    real_history = (1 + history) / (1 + dataset.inflation[:, None]) - 1
    ranking_cash = cash_history
    if ranking_cash is None and dataset.cash_returns is not None:
        ranking_cash = dataset.cash_returns[:, indices]
    ranking = asset_priority(assets, real_history, ranking_cash, dataset.dates, cash)
    positions = optimization_shortlist(assets, ranking)
    selected_tickers = [assets[i].ticker for i in positions]
    admitted = set(selected_tickers)
    deferred = [{"ticker": asset.ticker, "code": "bounded_optimization_shortlist", "reasons": [SHORTLIST_REASON]}
                for asset in assets if asset.ticker not in admitted]
    optimization_counts = {
        "eligibleAssets": len(assets), "optimizedAssets": len(positions), "deferredAssets": len(deferred),
        "eligibleStocks": len(stocks), "forecastedStocks": len(forecasts),
        "optimizedStocks": sum(assets[i].asset_class == "acoes" for i in positions),
        "preservedAnchors": sum(assets[i].asset_class != "acoes" for i in positions)}
    optimization_universe = {
        "maxAssets": MAX_OPTIMIZATION_ASSETS, "maxAnchors": MAX_OPTIMIZATION_ANCHORS,
        **optimization_counts, "counts": optimization_counts,
        "selectedTickers": selected_tickers, "admittedTickers": selected_tickers,
        "deferredTickers": [row["ticker"] for row in deferred],
        "rule": SHORTLIST_RULE, "usesSimulationOutcomes": False, "seedIndependent": True,
        "sectorBasis": "financial_features_sector_code",
        "calendarBasis": "observed_payment_months_not_future_schedule"}
    optimized_indices = [indices[i] for i in positions]
    assets = [assets[i] for i in positions]
    history, real_history, cash = history[:, positions], real_history[:, positions], cash[positions]
    if cash_history is not None:
        cash_history = cash_history[:, positions]
    upper, matrix, bounds, feasible, equity_cap, liquid_min = bounds_for(assets, investor.risk_level)
    covariance = np.atleast_2d(np.cov(real_history, rowvar=False)) * 12
    months = 12 * (plan.end_age - plan.age)
    rollover_mapping, rollover_events, rollover_states = rollover_schedule(
        assets, dataset.metadata.get("bonds", []), dataset.dates[-1], months)
    cash = np.einsum("mab,b->ma", rollover_mapping[:12], cash).mean(axis=0)

    def volatility_of(weights):
        return worst_volatility(weights, covariance, rollover_states)

    def scenarios(count, seed):
        if plan.income_mode == "distributions_only":
            paths, cash_paths = forecast_paths(history, cash_history, dataset.inflation, assets, forecasts, model,
                                              metrics["appliedMethod"] if metrics else "baseline",
                                              months, count, plan.block_months, seed)
        else:
            paths, cash_paths = bootstrap_paths(history, dataset.inflation, months, count, plan.block_months, seed), None
        return apply_rollover(paths, cash_paths, rollover_mapping)

    optimization_ranking = asset_priority(assets, real_history, cash_history, dataset.dates, cash)
    if not deferred:
        ranking = optimization_ranking
    ranking = [row | {"optimized": row["ticker"] in admitted,
                      "predictedCashPerShare": forecasts.get(row["ticker"])} for row in ranking]
    search, search_cash = scenarios(search_count, plan.seed)
    max_volatility = [0.08, 0.16, 0.28][investor.risk_level]

    class PortfolioRepair(Repair):
        def _do(self, problem, x, **kwargs):
            return np.array([project_weights(w, upper, matrix, bounds, feasible) for w in x])

    class PortfolioProblem(Problem):
        def __init__(self):
            super().__init__(n_var=len(assets), n_obj=3, n_ieq_constr=1, xl=0, xu=upper)

        def _evaluate(self, x, out, *args, **kwargs):
            outcome = simulate(x, search, plan, cash_paths=search_cash)
            volatility = volatility_of(x)
            # Binary success alone is flat when every portfolio fails the goal.
            out["F"] = np.column_stack([outcome["unfundedIncomeFraction"], volatility, -x @ cash])
            out["G"] = (volatility - max_volatility)[:, None]

    if len(assets) == 1:
        if float(volatility_of([1])[0]) > max_volatility + 1e-7:
            raise ValueError("O unico ativo elegivel supera o limite de volatilidade.")
        candidates = np.ones((1, 1))
    else:
        references = candidate_seeds(assets, optimization_ranking, cash, upper, matrix, bounds, feasible,
                                     volatility_of, max_volatility, project_weights)
        sampling = list(references)
        rng = np.random.default_rng(plan.seed)
        while len(sampling) < population:
            sampling.append(rng.dirichlet(np.ones(len(assets))))
        algorithm = NSGA2(pop_size=population, sampling=np.asarray(sampling[:population]),
                          repair=PortfolioRepair(), eliminate_duplicates=True)
        optimized = minimize(PortfolioProblem(), algorithm, ("n_gen", generations), seed=plan.seed, verbose=False)
        if optimized.X is None and not len(references):
            raise ValueError("Nenhuma carteira respeitou o limite de volatilidade. As restrições foram mantidas.")
        # Limit finalist count deterministically; re-evaluate with independent common paths.
        if optimized.X is not None:
            objectives = np.atleast_2d(optimized.F)
            ordering = np.lexsort((objectives[:, 2], objectives[:, 1], objectives[:, 0]))
            finalists = list(np.atleast_2d(optimized.X)[ordering[:6]])
        else:
            finalists = []
        candidates = feasible_unique([*references, *finalists], upper, matrix, bounds,
                                     volatility_of, max_volatility)
        if not len(candidates):
            raise ValueError("Nenhuma carteira viavel apos a verificacao dos limites.")
    final_seed = (plan.seed + 104729) % 2_000_000_001
    final_paths, final_cash = scenarios(plan.simulations, final_seed)
    final_outcome = simulate(candidates, final_paths, plan, cash_paths=final_cash)
    frequency = final_outcome["success"]
    unfunded = final_outcome["unfundedIncomeFraction"]
    volatility = volatility_of(candidates)
    final_order = np.lexsort((-candidates @ cash, volatility, unfunded, -frequency))
    chosen = candidates[final_order[0]]
    simulation = simulate(chosen, final_paths, plan, detailed=True, cash_paths=final_cash)
    simulation.pop("success")
    planning = inverse_planning(chosen, final_paths, final_cash, plan) if final_cash is not None else None
    historical_reference_return = float(np.expm1(np.log1p(real_history @ chosen).mean() * 12))
    baseline = project_weights(np.ones(len(assets))/len(assets), upper, matrix, bounds, feasible)
    baseline_volatility = float(volatility_of(baseline)[0])
    baseline_frequency = (float(simulate(baseline, final_paths, plan, cash_paths=final_cash)["success"][0])
                          if baseline_volatility <= max_volatility + 1e-7 else None)
    warnings = [
        "Simulação educacional, não uma recomendação profissional ou garantia de renda.",
        "Os limites de alocação são parâmetros acadêmicos, não percentuais prescritos pela CVM.",
        "Renda e aportes estão em reais da data-base; os aportes mantêm poder de compra.",
        "O custo anual informado é uma hipótese efetiva. A tributação individual não é calculada.",
        "Retornos totais e pagamentos sao tratados separadamente para evitar dupla contagem de proventos.",
        "A frequência de atendimento depende dos dados e hipóteses; não é a probabilidade verdadeira de aposentadoria.",
    ]
    if dataset.mode == "demo":
        warnings.insert(0, "Todos os ativos, preços, históricos e exemplos de treinamento são fictícios. Não use estes resultados para investir.")
    else:
        warnings.extend(str(value) for value in dataset.metadata.get("limitations", [])
                        if not (rollover_events and "rolagem e reinvestimento entre titulos futuros nao sao modelados" in str(value)))
    if dataset.metadata.get("cashflow_basis") == "reported_historical_cash_reconciled":
        warnings.append("Os historicos de acoes usam calendarios reportados com valores e escala por acao conciliados. Nao comprovam liquidacao bancaria; publicacoes ausentes permanecem desconhecidas. A avaliacao e retrospectiva, nao uma auditoria point-in-time.")
    if dataset.metadata.get('cash_calendar_date_qualifications'):
        warnings.append('Algumas datas historicas indicam o inicio declarado de pagamento de proventos, nao a quitacao integral comprovada naquele dia. O calendario usa essa referencia declarada; os cenarios nao comprovam recebimentos efetivos nessa data.')
    warnings.extend(calendar_warnings)
    if rollover_events:
        warnings.append("O principal e reinvestido na mesma classe e risco apos o vencimento, sem ser contado como renda. Usa-se o proximo vencimento preparado da amostra; quando nao existe, usa-se uma cesta historica da mesma classe como hipotese, nao a oferta ou taxa de um titulo futuro.")
        warnings.append("A rolagem e uma aproximacao mensal do valor de mercado simulado, nao precificacao contratual do resgate. Nao calcula lotes, tributacao individual ou o cupom final exato. A volatilidade e limitada pelo pior estado historico de reinvestimento, nao somente pela carteira inicial.")
    if deferred:
        warnings.append(f"Todos os {len(indices)} ativos elegiveis foram avaliados e ordenados; a busca de carteiras usa {len(assets)} deles por limite computacional. {len(deferred)} permanecem elegiveis, adiados da otimizacao. A selecao usa dados e previsoes anteriores aos cenarios, sem ajuste aos resultados simulados.")
    preparation = dataset.metadata.get('stock_universe_preparation')
    if preparation:
        warnings.append(f"Foram processados {preparation['evaluated_stocks']} codigos de acoes; {preparation['pending_stocks']} permanecem com dados pendentes. Acoes antigas e novas usam os mesmos criterios de admissao. Ativos sem dados reconciliados nao receberam alocacao ou cenarios ficticios.")
    if len(assets) == 1:
        warnings.append("Somente um ativo respeita o perfil nesta base restrita. Alocacao integral, sem diversificacao e sem busca NSGA-II; limites nao foram reduzidos.")
    if months > len(dataset.dates):
        warnings.append("O horizonte do plano excede o historico disponivel. As trajetorias extrapolam a amostra; a renda estimada nao e uma previsao aprendida para todas essas decadas.")
    if plan.income_mode == "distributions_only":
        warnings.append("A meta usa somente distribuicoes: nao ha venda do patrimonio para cobrir renda insuficiente. Valorizacao nao e renda recebida; custos e oscilacoes ainda alteram o valor do patrimonio.")
        warnings.append("A renda mensal e uma media dos pagamentos de cada ano simulado, nao promessa de pagamentos todos os meses. Na aposentadoria, os pagamentos sao retirados uma vez do retorno total e nao sao reinvestidos.")
        if not np.any(cash_history):
            warnings.append("Nenhum ativo elegivel possui pagamento periodico documentado nesta base. Renda de proventos igual a zero; isto nao e uma recomendacao de carteira de renda.")
    if plan.initial_capital == 0 and plan.monthly_contribution == 0:
        warnings.append("Sem capital inicial e sem aportes, a meta não é financiada.")
    if simulation["successFrequency"] < 0.8:
        warnings.append("A meta não foi sustentada em pelo menos 80% dos cenários. Revise aporte, prazo ou renda-alvo; o limite de risco não foi aumentado.")
    if metrics:
        warnings.append("A previsao de caixa influencia o objetivo do NSGA-II. No modo de dividendos e juros, substitui a parcela de caixa dos primeiros 12 meses dos cenarios; depois usa-se reamostragem historica conjunta, sem projetar o XGBoost por decadas. Erros da validacao representam incerteza da previsao, nao um intervalo de confianca garantido.")
        if metrics["appliedMethod"] == "baseline":
            warnings.append("A validacao temporal selecionou a referencia simples. O XGBoost foi testado, mas nao e aplicado neste calculo; o teste final nao escolhe o metodo.")
        if request.forecast_policy == "xgboost_experimental":
            warnings.append("XGBoost experimental solicitado explicitamente: aplicado mesmo quando perde para a referencia. Isto permite comparar efeitos, nao comprova superioridade nem adequacao financeira.")
    if (dataset.metadata.get("forecast_target") or {}).get("id") == "rights_by_record_date_v1":
        warnings.append("O indicador anual das acoes mede direitos brutos a dividendos e JCP pela data-com; exclui correcoes posteriores e nao representa recebimentos mensais, calendario de pagamento ou caixa disponivel para saque. Metricas deste alvo nao sao comparaveis as da versao de pagamentos.")
    priority = {row["ticker"]: row["safetyOrder"] for row in optimization_ranking}

    def coverage_for(weights):
        allocated = int(np.sum(np.asarray(weights) > 1e-6))
        return {"catalogAssets": len(dataset.assets), "eligibleAssets": len(indices),
                "optimizedAssets": len(assets), "deferredAssets": len(deferred),
                "allocatedAssets": allocated, "multiplePositions": allocated > 1,
                "status": "bounded_optimization_universe" if deferred else
                          "single_eligible_asset" if len(indices) == 1 else "limited_catalog"}

    def allocation_for(weights):
        records = []
        for asset, weight, flow in sorted(zip(assets, weights, cash), key=lambda row: priority[row[0].ticker]):
            records.append({"ticker": asset.ticker, "name": asset.name, "assetClass": asset.asset_class,
                        "weight": float(weight), "percent": float(weight*100), "cashYield12m": float(flow),
                        "predictedCashPerShare": forecasts.get(asset.ticker),
                        "source": asset.source, "asOf": asset.as_of,
                        "cashMethod": ("XGBoost: previsao de pagamentos em 12 meses" if metrics["appliedMethod"] == "xgboost" else "Referencia: pagamentos anteriores em 12 meses") if asset.asset_class == "acoes" else "Historico de juros pagos; sem previsao XGBoost",
                            "reason": f"Compatível com risco {asset.risk_level}, prazo mínimo de {asset.minimum_years} anos e liquidez de {asset.liquidity_days} dia(s)."})
        return records

    records = allocation_for(chosen)
    choices, selected_id = comparison_choices(candidates, volatility, cash, int(final_order[0]))
    portfolios = []
    for rank, (index, identifier, label) in enumerate(choices, 1):
        weights = candidates[index]
        if index == final_order[0]:
            detail, target = simulation, planning
        else:
            detail = simulate(weights, final_paths, plan, detailed=True, cash_paths=final_cash)
            detail.pop("success")
            target = inverse_planning(weights, final_paths, final_cash, plan) if final_cash is not None else None
        calendar, covered = payment_calendar(assets, weights, ranking)
        has_cash = cash_history is not None and bool(np.any(cash_history[:, weights > 1e-6]))
        portfolios.append({"id": identifier, "label": label, "safetyRank": rank,
                           "allocation": allocation_for(weights), "simulation": detail, "planning": target,
                           "portfolio": {"annualVolatility": float(volatility[index]),
                                         "cashYield12m": float(weights @ cash),
                                         "historicalAnnualRealReturn": float(np.expm1(np.log1p(real_history @ weights).mean() * 12)),
                                         "usesFixedReturnRate": False, "baselineSuccessFrequency": baseline_frequency},
                           "incomeAnalysis": {"method": "annual_cash_distributions" if plan.income_mode == "distributions_only" else "pathwise_real_withdrawal_capacity",
                                              "status": "experimental" if has_cash else "no_periodic_payments",
                                              "usesPrincipal": plan.income_mode == "total_return",
                                              "monthlyCapacity": detail["monthlyIncomeCapacity"],
                                              "targetMonthlyIncome": plan.target_income,
                                              "medianMonthlyGap": max(0, plan.target_income - detail["monthlyIncomeCapacity"][1]),
                                              "endAge": plan.end_age, "isDividendForecast": plan.income_mode == "distributions_only"},
                           "coverage": coverage_for(weights),
                           "historicalPaymentCalendar": calendar, "historicalCoverageMonths": covered,
                           "usesEquityForecast": bool(any(a.asset_class == "acoes" and w > 1e-6 for a, w in zip(assets, weights)))})
    alternatives = [{"successFrequency": float(frequency[i]), "annualVolatility": float(volatility[i]),
                     "meanUnfundedIncomeFraction": float(unfunded[i]),
                     "cashYield12m": float(candidates[i] @ cash),
                     "weights": [{"ticker": a.ticker, "percent": float(w*100)} for a, w in zip(assets, candidates[i])]}
                    for i in final_order[:3]]
    treasury_analysis = analyze_current_treasury(request, dataset) if dataset.mode == 'real' else None
    if treasury_analysis:
        warnings.append('Os fluxos dos titulos atuais sao calculados separadamente por unidade comprada na cotacao '
            'de referencia. A rolagem dessas novas posicoes ainda nao integra a carteira otimizada. Fluxos do lote '
            'original nao sao o plano completo de aposentadoria e nao incluem compras futuras ou reinvestimento.')
    return {"status": "ready", "engineVersion": ENGINE_VERSION, "dataMode": dataset.mode, "datasetSource": dataset.source,
            "referenceDate": dataset.dates[-1], "dataRevision": current_data_revision(dataset), "plan": plan.model_dump(), "allocation": records,
            "currentTreasuryAnalysis": treasury_analysis,
            "forecastTarget": dataset.metadata.get("forecast_target"),
            "marketData": {"collectedAt": dataset.metadata.get("collected_at"),
                           "cashflowBasis": dataset.metadata.get("cashflow_basis"),
                           "dataQualityPolicy": dataset.metadata.get("data_quality_policy"),
                           "pointInTimeArchiveVerified": dataset.metadata.get("point_in_time_archive_verified"),
                           "cashSettlementVerified": dataset.metadata.get("cash_settlement_verified"),
                           "cashflowTaxBasis": dataset.metadata.get("cashflow_tax_basis"),
                           "totalReturnMethod": dataset.metadata.get("total_return_method"),
                           "currentTreasuryOffers": dataset.metadata.get("current_treasury_offers", []),
                           "historyStart": dataset.dates[0], "historyMonths": len(dataset.dates),
                           "sources": dataset.metadata.get("sources", []), "bonds": dataset.metadata.get("bonds", [])},
            "excluded": excluded, "deferredFromOptimization": deferred, "simulation": simulation, "model": metrics,
            "dataCoverage": data_coverage,
            "planning": planning,
            "rollover": {"policy": "same_class_same_risk", "events": rollover_events,
                         "monthlyApproximation": True, "contractualRedemptionPricing": False,
                         "principalCountedAsIncome": False,
                         "unknownFutureInstruments": "historical_same_class_proxy"},
            "assetAnalysis": asset_diagnostics(dataset, indices, excluded, optimized_indices, deferred, forecasts),
            "universeScreening": screen_universe(dataset, investor, indices, forecasts),
            "coverage": coverage_for(chosen),
            "incomeAnalysis": {"method": "annual_cash_distributions" if plan.income_mode == "distributions_only" else "pathwise_real_withdrawal_capacity",
                               "status": "no_periodic_payments" if plan.income_mode == "distributions_only" and not np.any(cash_history) else "experimental",
                               "usesPrincipal": plan.income_mode == "total_return", "monthlyCapacity": simulation["monthlyIncomeCapacity"],
                               "targetMonthlyIncome": plan.target_income,
                               "medianMonthlyGap": max(0, plan.target_income - simulation["monthlyIncomeCapacity"][1]),
                               "endAge": plan.end_age, "isDividendForecast": plan.income_mode == "distributions_only"},
            "portfolio": {"annualVolatility": float(volatility[final_order[0]]), "cashYield12m": float(chosen @ cash),
                          "historicalAnnualRealReturn": historical_reference_return, "usesFixedReturnRate": False,
                          "baselineSuccessFrequency": baseline_frequency},
            "alternatives": alternatives, "warnings": warnings,
            "portfolios": portfolios, "selectedPortfolioId": selected_id, "assetRanking": ranking,
            "construction": {"method": "profile_filter_ranked_coverage_seeds_nsga2",
                             "optimizationUniverse": optimization_universe,
                             "referenceProject": "dividendos-br", "candidateCount": len(candidates),
                             "rankingRule": "risk_level_quality_alerts_volatility_then_income",
                             "portfolioOrder": "volatility_ascending_then_income_descending",
                             "calendarBasis": "observed_payment_months_not_future_schedule",
                             "calendarThreshold": .5, "commonFinalSeed": final_seed,
                             "commonFinalScenarios": plan.simulations,
                             "safetyIsGuarantee": False},
            "parameters": {"population": population if len(assets) > 1 else 0,
                           "generations": generations if len(assets) > 1 else 0,
                           "searchScenarios": search_count if len(assets) > 1 else 0,
                           "finalScenarios": plan.simulations, "searchSeed": plan.seed, "finalSeed": final_seed,
                           "blockMonths": plan.block_months, "annualRebalance": True,
                           "volatilityBasis": "maximum_across_rollover_states",
                           "equityMaximum": equity_cap, "liquidityMinimum": liquid_min,
                           "stockMaximum": 0.25, "issuerMaximum": 0.25,
                           "issuerConstraintBasis": "explicit_issuer_id_stocks_only",
                           "annualVolatilityMaximum": max_volatility},
            "methods": ["Content-Based Filtering", *(["XGBoost"] if stocks else []),
                        "NSGA-II" if len(assets) > 1 else "Alocacao no unico ativo elegivel", "Monte Carlo"],
            "financiallyValidated": False}
