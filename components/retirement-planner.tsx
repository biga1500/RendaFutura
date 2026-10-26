"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ArrowLeft, Calculator, Download, LoaderCircle, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { type Answers, type ProfileResult } from "@/lib/questionnaire";
import { engineInput, type AssetAnalysis, type DataCoverage, type EngineStatus, type PlanSettings, type SavedRecommendation } from "@/lib/recommendation";
import { initialPortfolio } from "@/lib/initial-portfolio";
import { portfolioView } from "@/lib/portfolio-view";
import { recommendationIsOutdated } from "@/lib/recommendation-revision";
import { PortfolioCalendar, PortfolioComparison } from "@/components/portfolio-comparison";
import { MarketInventory } from "@/components/market-inventory";
import { UniverseScreeningTable } from "@/components/universe-screening";
import { sourceLink } from "@/lib/source-link";

const money = (value:number) => value.toLocaleString("pt-BR",{style:"currency",currency:"BRL",maximumFractionDigits:0});
const preciseMoney = (value:number) => value.toLocaleString("pt-BR",{style:"currency",currency:"BRL",minimumFractionDigits:2,maximumFractionDigits:2});
const quoteDate = (value:string) => /^\d{4}-\d{2}-\d{2}/.test(value)?value.slice(0,10).split("-").reverse().join("/"):value;
const percent = (value:number|null|undefined) => value==null?"Não disponível":(value*100).toLocaleString("pt-BR",{maximumFractionDigits:1})+"%";
async function request<T>(path:string, method="GET", data?:unknown, signal?:AbortSignal):Promise<T> {
  const response = await fetch(`/api/recommendation${path}`,{method,signal,headers:data?{"Content-Type":"application/json"}:undefined,body:data?JSON.stringify(data):undefined});
  const body = await response.json() as T & {error?:string};
  if(!response.ok)throw new Error(body.error || "Não foi possível processar o planejamento.");
  return body;
}

function FinancialIndicators({assets}:{assets:AssetAnalysis[]}) {
  return <section className="planner-section"><h2>Indicadores e filtros analisados</h2>
    <p className="help">Valores das fontes financeiras na data-base. {assets.some(a=>a.distributionBasis==="payment_date")?"A coluna de pagamentos usa dividendos, JCP ou cupons brutos pagos nos últimos 12 meses, divididos pelo preço da data-base. Não é uma taxa prometida para o futuro.":"Proventos por data-com não são pagamentos recebidos."} Ativos excluídos não recebem alocação; títulos públicos não têm indicadores de lucro de empresas.</p>
    <div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Ativo</th><th>Situação</th><th>Preço</th><th>P/L</th><th>Proventos / preço</th><th>Lucro / ação</th><th>FCL / ação</th><th>Payout</th><th>Passivo / ativo</th></tr></thead><tbody>{assets.map(a=><tr key={a.ticker}><td><strong>{a.ticker}</strong><span>{a.name}</span></td><td>{a.eligible?"Elegível":"Excluído"}</td><td>{preciseMoney(a.price)}</td><td>{a.indicators?.priceEarnings===null||!a.indicators?"-":a.indicators.priceEarnings.toLocaleString("pt-BR",{maximumFractionDigits:2})}</td><td>{a.annualCashPaymentsYield!=null?percent(a.annualCashPaymentsYield):a.indicators?percent(a.indicators.trailingDistributionYield):"-"}</td><td>{a.indicators?preciseMoney(a.indicators.earningsPerShare):"-"}</td><td>{a.indicators?a.indicators.freeCashFlowPerShare==null?"Indisponível":preciseMoney(a.indicators.freeCashFlowPerShare):"-"}</td><td>{a.indicators?percent(a.indicators.payout):"-"}</td><td>{a.indicators?percent(a.indicators.liabilitiesToAssets):"-"}</td></tr>)}</tbody></table></div>
    {assets.some(a=>a.indicators?.freeCashFlowPerShare===null)&&<p className="help">Instituições financeiras: FCL industrial indisponível, sem substituição pelo caixa operacional ou por zero. Passivo / ativo usa o passivo total menos o patrimônio líquido consolidado.</p>}
    <details className="planner-details"><summary>Origem e motivo de cada filtro</summary>{assets.map(a=><p className="help" key={a.ticker}><strong>{a.ticker}</strong>: {a.exclusionReasons.length?a.exclusionReasons.join(" "):"Compatível com o perfil informado."} Risco cadastrado {a.riskLevel}; prazo mínimo {a.minimumYears} anos; liquidez {a.liquidityDays} dia(s). {a.historyMonths>0?<>Volatilidade histórica {percent(a.historicalAnnualVolatility)}; histórico {a.historyMonths} meses.</>:<>Oscilação anual do lote nos cenários {percent(a.scenarioAnnualVolatility)}; sem série histórica criada para este título.</>} {a.source} Referência {a.asOf}.</p>)}</details>
    <p className="help">P/L: preço dividido pelo lucro por ação. FCL: fluxo de caixa livre por ação. Payout: proventos divididos pelo lucro. Passivo / ativo: proporção de obrigações sobre os ativos da empresa. Esses indicadores vêm dos fundamentos disponíveis; não se aplicam a títulos públicos.</p>
  </section>;
}

function MarketCoverage({coverage}:{coverage:DataCoverage}) {
  const reasons:Record<string,string>={outside_collector_universe:"Fora do coletor validado",share_scale_normalization_required:"Eventos em ações exigem normalização",less_than_61_consecutive_prices:"Histórico mensal insuficiente",missing_observed_coupon:"Pagamento de cupom ausente na fonte",unsupported_treasury_structure:"Estrutura ainda sem suporte",not_evaluated_or_diagnostics_unavailable:"Diagnóstico indisponível",
    B3_cash_event_missing_from_payment_table:"Evento da B3 ausente no histórico de parcelas e pagamentos",
    fractional_alias_not_distinct_asset:"Código fracionário do mesmo ativo",unit_or_other_share_structure_not_reconciled:"Composição ou classe ainda sem conciliação",B3_active_issuer_mapping_unavailable:"Identidade da companhia não confirmada na B3",public_source_http_error:"Fonte pública indisponível ou acesso recusado",payment_history_too_short:"Histórico de pagamentos insuficiente",recent_share_action_requires_reconciliation:"Evento societário recente sem conciliação completa",cash_amount_or_share_scale_disagrees_with_B3:"Valores ou escala por ação divergem da B3",mature_B3_cash_event_missing_from_payment_table:"Pagamento histórico sem correspondência entre fontes",insufficient_independent_cash_matches:"Poucos pagamentos confirmados entre fontes",current_CVM_accounts_or_share_scale_unavailable:"Fundamentos ou escala por ação indisponíveis na CVM",fewer_than_24_valid_training_observations:"Poucos exemplos históricos válidos para treinamento",stock_source_validation_failed:"Dados reprovados nas verificações de fonte, preço ou identidade"};
  const checks:Record<string,string>={basic_quote:"Cotação básica",short_history:"Histórico curto",long_history:"Histórico longo",dividends:"Dividendos",sandbox_raw:"Preço bruto no sandbox"};
  return <details className="planner-details"><summary>Cobertura e limitações dos dados</summary><p className="help">Catálogo: {coverage.counts.catalogue??"não informado"} códigos e títulos. Preparados para o motor: {coverage.counts.modelReady??"não informado"}. A chave {coverage.credentialConfigured?"está configurada":"não está configurada"}; isso não comprova acesso a todos os dados. Códigos fracionários e units não são contados como empresas distintas.</p><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Pendência</th><th>Quantidade</th><th>Exemplos</th></tr></thead><tbody>{coverage.gaps.map(gap=><tr key={`${gap.assetClass}-${gap.reason}`}><td>{reasons[gap.reason]??gap.reason}</td><td>{gap.count}</td><td>{gap.examples.slice(0,3).join(", ")}</td></tr>)}</tbody></table></div><p className="help">ITUB4 e MGLU3 têm acesso de teste, mas exigem reconciliação de desdobramentos, grupamentos e bonificações, além dos pagamentos e fundamentos por ação. A limitação não é apenas da API. Dados ausentes não são substituídos por valores fictícios.</p><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Consulta registrada</th><th>Código</th><th>HTTP</th></tr></thead><tbody>{coverage.accessProbe.checks.map(check=><tr key={check.name}><td>{checks[check.name]??check.name}</td><td>{check.symbol}</td><td>{check.httpStatus??"não registrado"}</td></tr>)}</tbody></table></div><p className="help">São consultas registradas, não uma verificação em tempo real. Uma resposta 400 sozinha não identifica a causa ou o plano contratado. A data de coleta de uma fonte não comprova quando ela estava disponível no passado.</p></details>;
}

function InitialPortfolio({result}:{result:NonNullable<SavedRecommendation>["result"]}) {
  if(result.status==="income_data_unavailable"||!result.assetAnalysis)return null;
  const initial=initialPortfolio(result.allocation,result.assetAnalysis,result.plan.initial_capital,result.plan.monthly_contribution);
  const cash=initial.annualCash??(initial.allocated>0?initial.knownAnnualCash:null);
  const partial=initial.annualCash===null&&cash!==null;
  const incomeAlerts=initial.rows.filter(row=>row.amount>0).flatMap(row=>{
    const allocation=result.allocation.find(asset=>asset.ticker===row.ticker);
    const earnings=result.assetAnalysis?.find(asset=>asset.ticker===row.ticker)?.indicators?.earningsPerShare;
    return allocation?.predictedCashPerShare!=null&&earnings!=null&&allocation.predictedCashPerShare>0&&
      (earnings<=0||allocation.predictedCashPerShare>earnings)?[row.ticker]:[];
  });
  return <section className="planner-section" aria-labelledby="initial-portfolio-title"><h2 id="initial-portfolio-title">Carteira para começar</h2>
    <p>Distribuição indicativa do seu patrimônio atual de {preciseMoney(initial.capital)}. Cada posição informa a data da cotação observada. As ações usam histórico até {result.referenceDate}; os títulos usam seus contratos e cotações de referência. Não são ordens de compra.</p>
    <div className="planner-metrics"><div><span>Valor alocado após arredondamento</span><strong>{preciseMoney(initial.allocated)}</strong><small>Saldo não alocado: {preciseMoney(initial.remainder)}.</small></div><div><span>{partial?"Proventos estimados da parcela cotada em 12 meses":"Proventos brutos estimados em 12 meses"}</span><strong>{cash===null?"Não disponível":preciseMoney(cash)}</strong><small>{partial?"Somente posições com quantidade calculada; exclui o saldo não alocado.":"Sobre a posição inicial indicativa, sem aportes futuros."}</small></div><div><span>{partial?"Média mensal equivalente da parcela cotada":"Média mensal equivalente inicial"}</span><strong>{cash===null?"Não disponível":preciseMoney(cash/12)}</strong><small>Valor anual dividido por 12, não pagamento mensal garantido.</small></div></div>
    {initial.capital===0&&<p className="planner-warning">Você informou patrimônio atual igual a zero. Não há compra inicial nem renda sobre esse patrimônio. A coluna de aporte mostra a divisão indicativa do aporte mensal informado.</p>}
    {incomeAlerts.length>0&&<p className="planner-warning">Sustentabilidade da renda não confirmada: em {incomeAlerts.join(", ")}, a previsão supera o lucro por ação do último demonstrativo disponível, ou esse lucro não é positivo. A estimativa do XGBoost é experimental e não comprova renda recorrente nesse valor.</p>}
    <div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Ativo</th><th>Percentual-alvo</th><th>Valor-alvo atual</th><th>Preço de referência</th><th>Quantidade indicativa</th><th>Valor estimado</th><th>Divisão do aporte mensal</th></tr></thead><tbody>{initial.rows.map(row=><tr key={row.ticker}><td><strong>{row.ticker}</strong><span>{row.name}</span><span>Referência: {quoteDate(row.asOf)}</span></td><td>{percent(row.weight)}</td><td>{preciseMoney(row.budget)}</td><td>{row.price===null?"Não disponível":preciseMoney(row.price)}</td><td>{row.quantity===null?(row.quoteBasis==="not_in_current_purchase_catalogue"?"Fora da oferta atual":"Cotação não confirmada"):row.quantity.toLocaleString("pt-BR",{maximumFractionDigits:2})}</td><td>{preciseMoney(row.amount)}</td><td>{preciseMoney(row.nextContribution)}</td></tr>)}</tbody></table></div>
    {initial.rows.some(row=>row.price===null)&&<p className="planner-warning">Parte da carteira histórica não tem cotação atual de compra confirmada. Essas posições não recebem quantidade nem renda inicial presumida. Os cenários históricos não equivalem a uma carteira integralmente disponível para comprar hoje.</p>}
    {initial.rows.filter(row=>row.quoteBasis==="provider_snapshot_not_confirmed_trade_or_executable_order").map(row=><p className="help" key={`snapshot-${row.ticker}`}>{row.ticker}: referência informada pelo provedor em {row.snapshotAt?quoteDate(row.snapshotAt):"data não informada"}, coletada em {row.retrievedAt?quoteDate(row.retrievedAt):"data não informada"}. Última negociação conhecida: {row.lastTradeAt?quoteDate(row.lastTradeAt):"não confirmada"}. A atualização do provedor não comprova nova negociação nem oferta executável.</p>)}
    <p className="help">Quantidades indicativas arredondadas para baixo: ações inteiras e fração mínima informada para o Tesouro. Cotações observadas não são ofertas executáveis; o mercado pode estar fechado. Preço atual não multiplica a taxa histórica para fabricar renda: o cálculo preserva os proventos por unidade. Custos e impostos não estão incluídos. O saldo remanescente não recebe renda presumida. Os cenários usam pesos contínuos, não estas quantidades arredondadas.</p>
    <p className="help">Confira preços, taxas, disponibilidade e limites na instituição antes de investir. Esta tela não envia ordens. Regras: <a href="https://www.tesourodireto.com.br/sobre-o-tesouro/regras-e-regulamento" target="_blank" rel="noreferrer">Tesouro Direto</a> e <a href="https://borainvestir.b3.com.br/tipos-de-investimentos/renda-variavel/acoes/o-que-e-o-mercado-fracionario-como-investir-nele/" target="_blank" rel="noreferrer">mercado fracionário da B3</a>.</p>
  </section>;
}

function TreasuryReturns({result}:{result:NonNullable<SavedRecommendation>["result"]}) {
  if(result.status==="income_data_unavailable")return null;
  const bonds=result.assetAnalysis?.filter(asset=>asset.ticker.startsWith("TD-"))??[];
  if(!bonds.length)return null;
  if(result.rollover?.contractualRedemptionPricing)return <section className="planner-section"><h2>Rendimento e pagamentos do Tesouro</h2><p>As posições usam as ofertas atuais e os pagamentos previstos no contrato. Não é criada uma série de preços históricos para um título novo. A devolução do custo de aquisição permanece investida, separada da renda de juros.</p><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Título</th><th>Vencimento</th><th>Renda real em 12 meses / preço</th><th>Oscilação anual do lote nos cenários</th><th>Fluxo</th></tr></thead><tbody>{bonds.map(asset=>{const bond=result.marketData?.bonds.find(row=>row.ticker===asset.ticker);return <tr key={asset.ticker}><td><strong>{asset.ticker}</strong><span>{asset.name}</span></td><td>{bond?.maturity??"Não informado"}</td><td>{percent(asset.projectedRealIncomeYield12m)}</td><td>{percent(asset.scenarioAnnualVolatility)}</td><td>{bond?.coupon?"Juros semestrais":bond?.kind==="Tesouro Renda+"||bond?.kind==="Tesouro Educa+"?"Parcelas com juros e devolução de capital":"Rendimento recebido no resgate"}</td></tr>;})}</tbody></table></div><p className="help">Estimativas antes de impostos e custos, por unidade comprada na cotação atual. A tabela de ofertas apresenta a separação entre renda e capital devolvido. O reinvestimento na mesma família e risco entra nos cenários da carteira; títulos futuros são hipóteses, não ofertas conhecidas.</p></section>;
  return <section className="planner-section"><h2>Rendimento e pagamentos do Tesouro</h2><p className="help">O retorno real histórico desconta a inflação e inclui a variação do preço e os juros pagos. Não é a taxa contratada até o vencimento. Um título sem cupom acumula rendimento, mas não distribui juros periódicos antes do vencimento.</p><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Título</th><th>Vencimento</th><th>Retorno real histórico ao ano</th><th>Juros pagos / preço ao ano</th><th>Fluxo do título</th></tr></thead><tbody>{bonds.map(asset=>{const bond=result.marketData?.bonds.find(row=>row.ticker===asset.ticker);return <tr key={asset.ticker}><td><strong>{asset.ticker}</strong><span>{asset.name}</span></td><td>{bond?.maturity??"Não informado"}</td><td>{percent(asset.historicalAnnualRealReturn)}</td><td>{asset.annualCashPaymentsYield==null?"Não disponível":percent(asset.annualCashPaymentsYield)}</td><td>{bond?.coupon===true?"Juros semestrais":bond?.coupon===false?"Acumulação; sem cupom periódico":"Consultar a fonte"}</td></tr>;})}</tbody></table></div><p className="help">Histórico de {result.marketData?.historyMonths??"-"} meses até {result.referenceDate}. Juros brutos efetivamente pagos, sem contar devolução do principal como renda. Fonte: Tesouro Transparente.</p></section>;
}

function TreasuryOffers({result}:{result:NonNullable<SavedRecommendation>["result"]}) {
  if(result.status==="income_data_unavailable")return null;
  const offers=result.marketData?.currentTreasuryOffers??[];
  const analysis=result.currentTreasuryAnalysis;
  const contracts=new Map(analysis?.rows.map(row=>[`${row.kind}-${row.maturity}`,row]));
  if(!offers.length)return null;
  return <section className="planner-section"><h2>Ofertas atuais do Tesouro</h2>
    <p className="help">Catálogo oficial de compra, com vencimentos e taxas da fonte. Estar listado não garante adequação ao perfil. Quando a taxa contém &quot;+&quot;, o percentual é adicional ao indexador; não é um cupom mensal.</p>
    {analysis&&<p className="help">Fluxos calculados para {analysis.cashScenariosCalculated} títulos em {analysis.scenarios} cenários de inflação e Selic. Valores por uma unidade do título, em reais de hoje, antes de impostos e custos. Esta tabela acompanha somente o lote original. {analysis.portfolioReinvestmentIncluded?<>A otimização usa {analysis.optimizedContracts} títulos selecionados antes dos cenários, com reinvestimento na mesma família e risco. Os demais permanecem avaliados e não recebem dados fictícios.</>:<>A integração dessas posições à carteira otimizada continua pendente.</>}</p>}
    <div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Título</th><th>Vencimento</th><th>Preço de compra observado</th><th>Taxa informada</th><th>Data da fonte</th><th>Situação do mercado</th>{analysis&&<><th>Perfil</th><th>Renda real em 12 meses por unidade, cenário central</th><th>Capital devolvido em 12 meses por unidade</th></>}</tr></thead><tbody>{offers.map(offer=>{
      const contract=contracts.get(`${offer.kind}-${offer.maturity}`);
      return <tr key={`${offer.kind}-${offer.maturity}`}><td>{offer.name}{contract?.institutionRestriction&&<span>Disponível no {contract.institutionRestriction}</span>}</td><td>{offer.maturity}</td><td>{offer.buy_price===null?"Indisponível":preciseMoney(offer.buy_price)}</td><td>{offer.buy_rate_text??(offer.buy_rate===null?"Não informada":`${offer.buy_rate.toLocaleString("pt-BR")}% a.a.`)}</td><td>{new Date(offer.as_of).toLocaleString("pt-BR")}</td><td>{offer.market_status==="open"?"Aberto":offer.market_status==="closed"?"Fechado":offer.market_status==="suspended"?"Suspenso":"Não confirmado"}</td>{analysis&&<><td>{contract?(contract.profileCompatible?"Compatível":contract.profileReasons.join(" ")):"Não calculado"}</td><td>{contract?preciseMoney(contract.income12MonthsReal[1]):"Não calculada"}</td><td>{contract?preciseMoney(contract.originalCostReturned12MonthsReal):"Não calculado"}</td></>}</tr>;
    })}</tbody></table></div>
    <p className="help">Fonte: <a href="https://www.tesourodireto.com.br/o/rentabilidade/investir" target="_blank" rel="noreferrer">Tesouro Direto</a>. Educa+ e Renda+ devolvem principal junto aos pagamentos; essa devolução não é renda de juros. Os cenários usam referências nominais derivadas dos preços e taxas cotados, não um histórico inventado nem um VNA oficial observado. Sem cupom, o rendimento acumulado não é apresentado como pagamento antes do resgate.</p>
  </section>;
}

export default function RetirementPlanner({answers,profile,onBack}:{answers:Answers;profile:ProfileResult;onBack:()=>void}) {
  const [status,setStatus] = useState<EngineStatus|null>(null);
  const [saved,setSaved] = useState<SavedRecommendation>(null);
  const [portfolioId,setPortfolioId] = useState<string|undefined>();
  const [loading,setLoading] = useState(true);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const [statusError,setStatusError] = useState("");
  const [refresh,setRefresh] = useState(0);
  const [consent,setConsent] = useState(false);
  const [screen,setScreen] = useState<"setup"|"results">("setup");
  const [contribution,setContribution] = useState(answers.contribution === "Não consigo aportar no momento" ? "0" : "");
  const [endAge,setEndAge] = useState(String(Math.min(120,Math.max(90,Number(answers.retirementAge)+10))));
  const [cost,setCost] = useState("0.5");
  const [simulations,setSimulations] = useState<500|1000|2000>(1000);
  const [block,setBlock] = useState<3|6|12>(6);
  const [seed,setSeed] = useState("42");
  const active = useRef<AbortController|null>(null);
  useEffect(()=>{
    const update=()=>setScreen(location.hash==="#carteira"?"results":"setup");
    update();window.addEventListener("hashchange",update);window.addEventListener("popstate",update);
    return()=>{window.removeEventListener("hashchange",update);window.removeEventListener("popstate",update);};
  },[]);
  function openScreen(target:"setup"|"results") {
    setScreen(target);window.location.hash=target==="results"?"carteira":"planejamento";window.scrollTo({top:0,behavior:"instant"});
  }
  useEffect(()=>{
    const controller = new AbortController();
    request<EngineStatus>("/status","GET",undefined,controller.signal).then(setStatus).catch(e=>{if(!controller.signal.aborted)setStatusError(e.message);});
    request<{recommendation:SavedRecommendation}>("","GET",undefined,controller.signal).then(({recommendation})=>{
      setSaved(recommendation);
      setPortfolioId(undefined);
      if(recommendation){const s=recommendation.settings;setContribution(String(s.monthlyContribution));setEndAge(String(s.endAge));setCost(String(s.annualCostPercent));setSimulations(s.simulations);setBlock(s.blockMonths);setSeed(String(s.seed));}
    }).catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>{controller.abort();active.current?.abort();};
  },[refresh]);
  async function generate(event:FormEvent) {
    event.preventDefault();setError("");
    if(!consent){setError("Autorize o processamento dos parâmetros do planejamento.");return;}
    if(contribution.trim()===""){setError("Confirme o valor exato do aporte mensal.");return;}
    const settings:PlanSettings={incomeMode:"distributions_only",forecastPolicy:"xgboost_experimental",endAge:Number(endAge),monthlyContribution:Number(contribution),annualCostPercent:Number(cost),simulations,blockMonths:block,seed:Number(seed),dataMode:"real",consent:true};
    try{engineInput(answers,settings);}catch(e){setError(e instanceof Error?e.message:"Confira os valores informados.");return;}
    active.current?.abort();const controller=new AbortController();active.current=controller;setBusy(true);
    try{
      const out=await request<{recommendation:SavedRecommendation}>("","POST",settings,controller.signal);
      // The market base may change while this page remains open.
      try{
        const current=await request<EngineStatus>("/status","GET",undefined,controller.signal);
        setStatus(current);setStatusError("");
      }catch(e){
        if(controller.signal.aborted)throw e;
        setStatus(null);setStatusError("O planejamento foi salvo, mas a versão atual da base não pôde ser conferida. Verifique a conexão para exibir a carteira.");
      }
      setSaved(out.recommendation);setPortfolioId(undefined);openScreen("results");
    }
    catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Falha ao processar.");}
    finally{if(!controller.signal.aborted)setBusy(false);}
  }
  async function remove() {
    if(!window.confirm("Excluir o planejamento e revogar sua autorização? Seu questionário será mantido."))return;
    setBusy(true);setError("");
    try{await request("","DELETE");setSaved(null);setConsent(false);openScreen("setup");}catch(e){setError(e instanceof Error?e.message:"Falha ao excluir.");}finally{setBusy(false);}
  }
  function download() {
    const activePortfolio=result?{id:result.selectedPortfolioId,label:result.comparisonLabel,
      allocation:result.allocation,simulation:result.simulation,planning:result.planning,
      portfolio:result.portfolio,incomeAnalysis:result.incomeAnalysis,
      initialPosition:initialPortfolio(result.allocation,result.assetAnalysis??[],result.plan.initial_capital,result.plan.monthly_contribution)}:null;
    const url=URL.createObjectURL(new Blob([JSON.stringify({...saved,activePortfolio},null,2)],{type:"application/json"}));
    const link=document.createElement("a");link.href=url;link.download="planejamento-rendafutura.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  const outdated=recommendationIsOutdated(saved,status);
  const verified=!!status&&!statusError;
  const diagnostic=verified&&!outdated&&saved?.result.status==="income_data_unavailable"?saved.result:null;
  const result=verified&&status?.realDataAvailable&&!outdated&&saved?.result.status!=="income_data_unavailable"&&saved?.result?portfolioView(saved.result,portfolioId):undefined;
  const entitlementTarget=result?.plan.income_mode!=="distributions_only"&&result?.forecastTarget?.id==="rights_by_record_date_v1";
  return <main className="planner-page">
    <button className="quiet-button" onClick={()=>screen==="results"?openScreen("setup"):onBack()}><ArrowLeft size={16}/>{screen==="results"?"Voltar ao planejamento":"Meu perfil"}</button>
    <div className="planner-heading"><div><h1>{screen==="results"?"Sua carteira de aposentadoria":"Planejamento de aposentadoria"}</h1><p className="page-description">Perfil {profile.profile.toLowerCase()} · aposentadoria aos {answers.retirementAge} anos · meta informada {money(Number(answers.targetIncome))}/mês</p></div><span className="engine-tag">Dados reais · análise experimental</span></div>
    <p className="page-description">Renda somente de dividendos e juros. O sistema não vende patrimônio para completar a meta. Valorização e proventos anunciados não são tratados como pagamentos recebidos.</p>
    <p className="help">A estratégia considera o patrimônio atual, o perfil, o prazo e a renda desejada para a aposentadoria.</p>
    {loading&&<p role="status">Carregando os parâmetros...</p>}
    {statusError&&<div className="error" role="alert"><p>{statusError}</p><button className="quiet-button" onClick={()=>{setLoading(true);setStatusError("");setStatus(null);setRefresh(n=>n+1);}}><RefreshCw size={16}/>Tentar conexão novamente</button></div>}
    {!status?.realDataAvailable&&status&&<p className="planner-warning">Base financeira real indisponível. O cálculo está bloqueado até os dados estarem disponíveis; não serão usados ativos fictícios.</p>}
    {status?.realDataAvailable&&<p className="page-description">Amostra de simulação: {status.realDataAssets?.length??0} ativos com preços e pagamentos preparados · referência {status.realDataReference}. {status.cashIncomeMessage??status.realDataMessage}</p>}
    {status?.marketInventory?<p className="page-description">Inventário de fontes consultado em {quoteDate(status.marketInventory.asOf)}: {status.marketInventory.counts.stocks} cadastros de ações e outras estruturas; {status.marketInventory.counts.treasury} títulos na oferta atual do Tesouro. Dados parciais permanecem no catálogo, com as pendências indicadas por campo.</p>:status?.catalog&&<p className="page-description">Catálogo de referência: {status.catalog.stocks} códigos de ações e units; {status.catalog.treasury} títulos com preço histórico registrado. Não representa disponibilidade atual para compra.</p>}
    {status?.dataCoverage&&<MarketCoverage coverage={status.dataCoverage}/>}
    <MarketInventory currentContracts={status?.currentContractKeys??(result?.currentTreasuryAnalysis?.rows.map(row=>`${row.kind}|${row.maturity}`))??[]}/>
    {status?.universePreparation&&<p className="help">Preparação do universo completo: {status.universePreparation.evaluatedStocks} códigos de ações processados; {status.universePreparation.readyStocks} com dados preparados e {status.universePreparation.pendingDataStocks} com pendências. O resultado detalha também a compatibilidade de cada código com seu perfil.</p>}
    {outdated&&<p className="planner-warning">Há um resultado anterior a esta versão. Recalcule para comparar as carteiras com dados reais, XGBoost e cenários próprios; a carteira antiga não é exibida como resultado atual.</p>}
    {screen==="results"&&!loading&&!result&&!diagnostic&&<Button variant="outline" onClick={()=>openScreen("setup")}><ArrowLeft size={16}/>Configurar novo planejamento</Button>}
    {screen==="setup"&&<form onSubmit={generate} className="planner-form">
      <fieldset disabled={loading||busy} style={{border:0,padding:0,margin:0,minWidth:0}}>
      <div className="planner-fields">
        <div className="field"><label htmlFor="plan-contribution">Aporte mensal confirmado (R$)</label><Input id="plan-contribution" type="number" required min="0" max="1000000" step="0.01" value={contribution} onChange={e=>setContribution(e.target.value)}/><span className="help">Faixa declarada: {answers.contribution}</span></div>
        <div className="field"><label htmlFor="plan-end-age">Idade final dos cenários</label><Input id="plan-end-age" type="number" required min={Number(answers.retirementAge)+1} max="120" step="1" value={endAge} onChange={e=>setEndAge(e.target.value)}/></div>
        <div className="field"><label htmlFor="plan-cost">Custo efetivo anual (%)</label><Input id="plan-cost" type="number" required min="0" max="5" step="0.1" value={cost} onChange={e=>setCost(e.target.value)}/></div>
      </div>
      <div className="planner-summary"><span className="engine-tag">Previsão de proventos: XGBoost</span><span className="help">Aplicado às ações elegíveis. Títulos usam cotações observadas e pagamentos previstos no contrato.</span></div>
      <details className="planner-details"><summary>Parâmetros dos cenários</summary><div className="planner-fields">
        <div className="field"><label htmlFor="plan-count">Simulações finais</label><select id="plan-count" value={simulations} onChange={e=>setSimulations(Number(e.target.value) as 500|1000|2000)}>{[500,1000,2000].map(n=><option key={n} value={n}>{n}</option>)}</select></div>
        <div className="field"><label htmlFor="plan-block">Blocos históricos (meses)</label><select id="plan-block" value={block} onChange={e=>setBlock(Number(e.target.value) as 3|6|12)}>{[3,6,12].map(n=><option key={n} value={n}>{n}</option>)}</select></div>
        <div className="field"><label htmlFor="plan-seed">Semente de reprodução</label><Input id="plan-seed" type="number" min="0" max="2000000000" step="1" required value={seed} onChange={e=>setSeed(e.target.value)}/></div>
      </div></details>
      <div className="consent"><Checkbox id="plan-consent" checked={consent} onCheckedChange={v=>setConsent(v===true)}/><label htmlFor="plan-consent">Autorizo o processamento do perfil derivado, capital, aporte, prazo e meta no motor. Nome, e-mail, senha e respostas completas não são enviados ao serviço de cálculo.</label></div>
      <p className="help">Valores em poder de compra da data-base. Os aportes mantêm esse poder de compra; a tributação individual não é calculada. A idade final é uma hipótese, não uma previsão de longevidade.</p>
      {error&&<p className="error" role="alert">{error}</p>}
      <Button type="submit" disabled={busy||loading||!status||!!statusError||!consent||!status.realDataAvailable}>{busy?<LoaderCircle size={16} className="spin"/>:<Calculator size={16}/>} {busy?"Calculando cenários...":"Calcular planejamento"}</Button>
      {(result||diagnostic)&&<Button type="button" variant="outline" onClick={()=>openScreen("results")}>Ver carteira salva</Button>}
      </fieldset>
    </form>}
    {screen==="results"&&error&&<p className="error" role="alert">{error}</p>}
    {screen==="results"&&diagnostic&&<div aria-live="polite" className="planner-results"><div className="planner-heading"><h2>Dados insuficientes para estimar renda de proventos</h2><div className="planner-actions"><Button variant="outline" onClick={download}><Download size={16}/>Exportar diagnóstico</Button><Button variant="outline" disabled={busy} onClick={remove}><Trash2 size={16}/>Excluir</Button></div></div><p className="planner-warning">{diagnostic.message}</p><p>A base contém {diagnostic.coverage.catalogAssets} ativos, dos quais {diagnostic.coverage.eligibleAssets} passam pelos filtros. Faltam dados de pagamentos: {diagnostic.missingCashAssets.join(", ")}. Não foi gerada uma renda futura, carteira de renda ou frequência de atendimento. A simulação não foi substituída por saques do principal.</p><FinancialIndicators assets={diagnostic.assetAnalysis}/></div>}
    {screen==="results"&&diagnostic&&<UniverseScreeningTable screening={diagnostic.universeScreening}/>}
    {screen==="results"&&result&&<div aria-live="polite" className="planner-results">
      <div className="planner-heading"><h2>Resultado com dados reais</h2><div className="planner-actions"><Button variant="outline" onClick={download} title="Exportar planejamento"><Download size={16}/><span>Exportar</span></Button><Button variant="outline" disabled={busy} onClick={remove} title="Excluir planejamento e revogar autorização"><Trash2 size={16}/><span>Excluir</span></Button></div></div>
      <PortfolioComparison result={result} onSelect={setPortfolioId}/>
      {result.comparisonLabel&&<div className="planner-summary"><strong>Carteira em análise: {result.comparisonLabel}</strong><span className="help">A composição e todos os resultados abaixo correspondem a esta carteira.</span></div>}
      {result.usesEquityForecast===false&&result.model&&<p className="help">Esta composição não aloca ações. O XGBoost foi avaliado para as ações elegíveis do universo, mas a renda desta carteira vem dos juros dos títulos nas hipóteses dos cenários.</p>}
      <InitialPortfolio result={result}/>
      <PortfolioCalendar result={result}/>
      <p className="help">Resultado salvo em {new Date(saved!.updatedAt).toLocaleString("pt-BR")} · parâmetros do cálculo abaixo. Alterar o formulário não modifica o resultado salvo.</p>
      {result.plan.income_mode!=="distributions_only"&&<p className="planner-warning">Resultado antigo calculado com retiradas do patrimônio. Ele não atende à meta de viver somente de dividendos e juros. Recalcule o planejamento; os valores abaixo não são renda de proventos.</p>}
      {result.coverage?.status==="single_eligible_asset"&&<p className="planner-warning">Cobertura insuficiente para uma carteira diversificada: somente {result.coverage.eligibleAssets} de {result.coverage.catalogAssets} ativos é elegível. Esta composição não é uma recomendação de concentração para todos os perfis.</p>}
      {result.coverage?.optimizedAssets!=null&&<p className="help">Universo preparado: {result.coverage.catalogAssets} ativos. Compatíveis com o perfil: {result.coverage.eligibleAssets}. Selecionados para esta otimização: {result.coverage.optimizedAssets}. A seleção considera segurança relativa, proventos, calendário histórico e setor, sem alterar os limites do perfil.</p>}
      {result.incomeAnalysis?.status==="no_periodic_payments"&&<p className="planner-warning">Nenhum ativo elegível desta base paga dividendos ou juros periódicos documentados. A renda de proventos é zero; crescimento do patrimônio não significa renda recebida.</p>}
      <section className="planner-section" aria-labelledby="percentile-title"><h2 id="percentile-title">Como ler os cenários</h2><p>Os resultados são ordenados do menor para o maior. A letra P significa percentil: a posição de um valor nessa distribuição de simulações.</p><dl className="percentile-guide"><div><dt>Cenário inferior (P10)</dt><dd>10% dos valores simulados ficam até este valor e 90% ficam acima. Não é o pior caso possível.</dd></div><div><dt>Cenário central (P50)</dt><dd>É a mediana: metade dos valores fica abaixo e metade acima. Não é uma promessa de resultado.</dd></div><div><dt>Cenário superior (P90)</dt><dd>90% dos valores ficam até este valor e 10% ficam acima. Não é o melhor caso possível.</dd></div></dl><p className="help">Cada ponto do gráfico tem seus próprios percentis; uma curva não representa a trajetória de uma única pessoa. São cenários condicionados aos dados e hipóteses do modelo.</p></section>
      <div className="planner-metrics"><div><span>Atendimento da renda anual nos cenários</span><strong>{percent(result.simulation.successFrequency)}</strong><small>Margem amostral 95%: {result.simulation.samplingInterval95.map(percent).join(" a ")}</small></div><div><span>Patrimônio central ao aposentar</span><strong>{money(result.simulation.retirementWealth[1])}</strong><small>Inferior (P10): {money(result.simulation.retirementWealth[0])} · Superior (P90): {money(result.simulation.retirementWealth[2])}</small></div><div><span>Renda mensal média estimada na aposentadoria</span><strong>{result.incomeAnalysis&&!result.incomeAnalysis.usesPrincipal?money(result.incomeAnalysis.monthlyCapacity[1]):"Não calculada"}</strong><small>{result.incomeAnalysis&&!result.incomeAnalysis.usesPrincipal?`Inferior (P10): ${money(result.incomeAnalysis.monthlyCapacity[0])} · Superior (P90): ${money(result.incomeAnalysis.monthlyCapacity[2])}`:"O resultado anterior usava saques do principal."}</small></div></div>
      {result.incomeAnalysis&&!result.incomeAnalysis.usesPrincipal&&<p className="help">Em cada cenário, a capacidade de renda considera a menor média mensal anual da aposentadoria. Não é um calendário de recebimentos mensais nem uma garantia. Não há retirada do principal para cobrir a meta. Volatilidade anual estimada: {percent(result.portfolio.annualVolatility)}.</p>}
      {result.marketData?.cashflowTaxBasis==="gross_before_individual_taxes"&&<p className="planner-warning">Renda bruta, antes de impostos individuais. {result.rollover?.contractualRedemptionPricing?"As ações usam históricos e a previsão experimental de proventos; os títulos usam fluxos contratuais nas condições simuladas de inflação, Selic e taxas de mercado. O principal é reinvestido na mesma família e risco. Os contratos dos títulos futuros são hipóteses, não ofertas conhecidas.":<>Os cenários reamostram históricos; não representam os pagamentos contratuais futuros destes vencimentos. {result.rollover?"O principal é reinvestido na mesma classe e risco. Quando não há um vencimento posterior preparado, o cenário usa uma referência histórica da classe, sem inventar um título futuro.":"Este resultado usa a política anterior, sem substituição dos títulos após o vencimento. Recalcule o planejamento."}</>}</p>}
      <p className="planner-warning">Não é a probabilidade verdadeira de aposentadoria. Os intervalos refletem a amostragem do modelo, não todos os riscos financeiros. Não há validação financeira ou com especialista.</p>
      {result.planning&&<section className="planner-section">
        <h2>Patrimônio-alvo e aporte necessário</h2>
        <p className="help">Critério acadêmico: sustentar a meta em pelo menos {percent(result.planning.attainmentCriterion)} dos {result.planning.scenarioCount} cenários, mantendo a carteira selecionada, o prazo, os custos e o reinvestimento. O cálculo não aumenta o risco nem altera suas respostas. Não é garantia de renda.</p>
        <div className="planner-metrics">
          <div><span>Patrimônio-alvo na aposentadoria</span><strong>{result.planning.requiredRetirementCapital===null?result.planning.requiredRetirementCapitalNullReason==="independent_retirement_start_unit_income_factors_not_supplied"?"Não calculado para estes lotes":"Sem solução finita":money(result.planning.requiredRetirementCapital)}</strong><small>Renda bruta de dividendos e juros, sem saques do principal.</small></div>
          <div><span>Aporte mensal necessário</span><strong>{result.planning.requiredMonthlyContribution===null?"Sem solução finita":money(result.planning.requiredMonthlyContribution)}</strong><small>Mantendo o capital inicial informado; reais da data-base.</small></div>
          <div><span>Diferença para o aporte atual</span><strong>{result.planning.monthlyContributionGap===null?"Não calculável":money(result.planning.monthlyContributionGap)}</strong><small>Estimativa, sem aplicação automática ou mudança da faixa declarada.</small></div>
        </div>
        {result.planning.requiredRetirementCapitalNullReason==="independent_retirement_start_unit_income_factors_not_supplied"&&<p className="help">O capital atual e os aportes compram lotes em datas diferentes. Não se deduz um patrimônio-alvo único desses lotes; o aporte necessário usa a renda de cada ano da aposentadoria.</p>}
        {result.planning.status==="no_finite_solution"&&<p className="planner-warning">Há cenários demais sem renda periódica suficiente nesta carteira. Aumentar o patrimônio não resolve um fluxo de proventos igual a zero; não foi inventado um patrimônio-alvo ou aporte finito.</p>}
        {result.portfolio.historicalAnnualRealReturn!==undefined&&<p className="help">Retorno real histórico de referência da carteira inicial: {percent(result.portfolio.historicalAnnualRealReturn)} ao ano. O sistema não aplica essa média como taxa fixa: usa as trajetórias de retornos, pagamentos e inflação dos cenários.</p>}
      </section>}
      <section className="planner-section"><h2>Trajetórias de patrimônio</h2><p className="help">Valores reais · aposentadoria aos {result.plan.retirement_age} anos · horizonte até {result.plan.end_age} anos · aporte {money(result.plan.monthly_contribution)}/mês · meta de renda {money(result.plan.target_income)}/mês</p>
        <div className="scenario-chart" role="img" aria-label="Cenários inferior, central e superior do patrimônio simulado por idade"><ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{width:800,height:320}}><AreaChart data={result.simulation.chart} margin={{top:20,right:12,left:10,bottom:4}}><CartesianGrid strokeDasharray="3 3" vertical={false}/><XAxis dataKey="age" tick={{fontSize:12}}/><YAxis tickFormatter={v=>new Intl.NumberFormat("pt-BR",{notation:"compact",maximumFractionDigits:1}).format(v)} width={70} tick={{fontSize:12}}/><Tooltip formatter={value=>money(Number(value))} labelFormatter={label=>`Idade: ${label} anos`}/><Legend/><Area dataKey="p90" name="Superior (P90)" stroke="#7485ac" fill="#edf0f7" isAnimationActive={false}/><Area dataKey="p50" name="Central (P50)" stroke="#13795b" fill="#e2f0e9" isAnimationActive={false}/><Area dataKey="p10" name="Inferior (P10)" stroke="#a86c40" fill="#faf3ed" isAnimationActive={false}/></AreaChart></ResponsiveContainer></div>
      </section>
      <section className="planner-section"><h2>Alocação selecionada</h2><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Ativo</th><th>Participação</th><th>Caixa anual / preço</th><th>{entitlementTarget?"Direitos indicativos por ação":"Previsão em 12 meses por ação"}</th></tr></thead><tbody>{result.allocation.filter(a=>a.weight>1e-12).map(a=><tr key={a.ticker}><td><strong>{a.ticker}</strong><span>{a.name}</span><span>{a.cashMethod}</span></td><td>{a.percent.toLocaleString("pt-BR",{minimumFractionDigits:2,maximumFractionDigits:2})}%</td><td>{percent(a.cashYield12m)}</td><td>{a.predictedCashPerShare===null?"Não se aplica":a.predictedCashPerShare.toLocaleString("pt-BR",{style:"currency",currency:"BRL"})}</td></tr>)}</tbody></table></div><p className="help">Os percentuais completos somam 100%; a exibição é arredondada. Ações: previsão de 12 meses dividida pelo preço. Títulos: média histórica anualizada dos juros pagos. A previsão substitui a parcela de caixa nos primeiros 12 meses dos cenários, sem dupla contagem; depois, a simulação reamostra o histórico. Não representa pagamentos garantidos.</p>{entitlementTarget&&<p className="planner-warning">O indicador considera dividendos e JCP pela data-com, não o calendário de pagamento. Exclui correções posteriores e não representa renda mensal ou caixa disponível para saque.</p>}</section>
      <TreasuryReturns result={result}/>
      <TreasuryOffers result={result}/>
      {result.marketData?.totalReturnMethod==="split_consistent_price_plus_paid_cash"&&<p className="help">Preços e pagamentos são normalizados para a mesma quantidade de ações na data-base. O retorno soma somente a variação do preço e o caixa pago no mês, sem somar dividendos a preços já ajustados pelo reinvestimento.</p>}
      <UniverseScreeningTable screening={result.universeScreening}/>
      {result.assetAnalysis&&<FinancialIndicators assets={result.assetAnalysis}/>}
      {result.rollover&&result.rollover.events.length>0&&<section className="planner-section"><h2>Reinvestimento após o vencimento</h2><p className="help">O valor do principal permanece investido, sem ser somado à renda de dividendos e juros. A alocação usa títulos de referência e reinvestimento na mesma classe e risco. As datas usam resolução mensal; não são instruções de negociação ou cálculo contratual do resgate.</p><div className="planner-table-wrap"><table className="planner-table"><thead><tr><th>Mês do cenário</th><th>Referência inicial</th><th>De</th><th>Reinvestimento</th></tr></thead><tbody>{result.rollover.events.map((event,i)=><tr key={`${event.referenceTicker}-${event.month}-${i}`}><td>{event.calendarMonth}</td><td>{event.referenceTicker}</td><td>{event.fromTicker}</td><td>{event.toTicker??`Hipótese histórica: ${event.class}`}</td></tr>)}</tbody></table></div><p className="help">A volatilidade considera o pior estado histórico das substituições, além da carteira inicial. Preços de resgate, lotes, impostos e taxas de novos títulos não são conhecidos nem garantidos.</p></section>}
      <section className="planner-section"><h2>Fontes e parâmetros</h2><p>{result.datasetSource} · data-base {result.referenceDate}</p><p className="help">{result.methods.map(m=>m==="XGBoost"&&result.model?.usedForIncomeSimulation===false?"XGBoost (avaliação separada)":m).join(" → ")} · população {result.parameters.population} · gerações {result.parameters.generations} · busca {result.parameters.searchScenarios} cenários · avaliação {result.parameters.finalScenarios} cenários · semente {result.parameters.finalSeed} · blocos {result.parameters.blockMonths} meses · {result.parameters.annualRebalance?"rebalanceamento anual":"sem rebalanceamento anual"}.</p>
        {result.dataMode==="real"&&result.marketData&&<><p className="help">Histórico das ações desde {result.marketData.historyStart} · {result.marketData.historyMonths} meses · coleta em {result.marketData.collectedAt ? new Date(result.marketData.collectedAt).toLocaleString("pt-BR") : "não registrada"}. Títulos sem cupom não geram dividendos mensais. {result.rollover?"Reinvestimento na mesma classe e risco, com hipóteses explícitas para títulos futuros.":"Recalcule para aplicar a política atual de reinvestimento."}{result.marketData.totalReturnMethod==="unadjusted_price_plus_paid_cash"&&" O retorno mensal usa preço não ajustado mais pagamentos do mês. O caixa não é adicionado a uma série já ajustada por proventos."}</p><details className="planner-details"><summary>Fontes consultadas e integridade</summary>{result.marketData.sources.map((s,i)=>{
          const link=sourceLink(s.url);
          return <p className="help" key={`${s.url}-${s.sha256}-${i}`}>{link?<a href={link.href} target="_blank" rel="noreferrer">{link.label}</a>:<span>{s.file?`Arquivo de auditoria: ${s.file}`:"Registro de auditoria local"}</span>} · coleta {s.retrieved_at?quoteDate(s.retrieved_at):"não registrada"} · SHA-256 {s.sha256}</p>;
        })}</details></>}
        {result.model?<><p className="help">XGBoost {result.dataMode==="demo"?"em base fictícia":"experimental"}: MAE {(result.model.xgboostMae??result.model.mae).toFixed(3)}, RMSE {(result.model.xgboostRmse??result.model.rmse).toFixed(3)} e MAE da referência simples {result.model.baselineMae.toFixed(3)} (R$/ação). Treino: {result.model.trainRows}; validação: {result.model.validationRows}; teste temporal: {result.model.testRows}. {(result.model.xgboostMae??result.model.mae)<result.model.baselineMae?"Superou a referência neste teste de previsão, sem comprovar desempenho da carteira.":"Não superou a referência simples."}</p>{result.model.forecastHorizonMonths?<><p className="help"><strong>Previsão aplicada: {result.model.appliedMethod==="baseline"?"referência simples dos pagamentos anteriores":"XGBoost"}.</strong> {result.model.applicationPolicy==="xgboost_experimental"?"Uso experimental solicitado, independentemente do vencedor na validação.":"Método escolhido apenas pela validação temporal; o teste final não decide a escolha."} A previsão influencia a análise dos ativos, o objetivo de caixa do NSGA-II e os primeiros 12 meses do Monte Carlo.</p><p className="help">A incerteza usa erros da validação, sem usar o teste final para calibrar os cenários. Após o mês 12, os cenários usam retornos, pagamentos e inflação históricos conjuntos. As janelas de teste se sobrepõem: os erros não comprovam superioridade da carteira nem desempenho por décadas.</p></>:<p className="planner-warning">Resultado salvo com a política anterior. Recalcule para aplicar a integração de previsão e cenários.</p>}</>:<p className="help">Não há ações elegíveis; o modelo de proventos não foi utilizado nesta carteira.</p>}
        {entitlementTarget&&<p className="help"><strong>Alvo avaliado: direitos brutos a dividendos e JCP em 12 meses pela data-com.</strong> Não é previsão de pagamentos. As métricas deste alvo não são comparáveis às da versão anterior baseada em datas de pagamento.</p>}
        {result.model?.byTicker&&Object.entries(result.model.byTicker).map(([ticker,score])=><p className="help" key={ticker}>Teste {ticker}: {score.rows} observações · MAE XGBoost {score.xgboostMae.toFixed(3)} · referência simples {score.baselineMae.toFixed(3)} (R$/ação).</p>)}
        <details className="planner-details"><summary>Compatibilidade e exclusões</summary>{result.allocation.map(a=><p key={a.ticker}><strong>{a.ticker}</strong>: {a.reason} Fonte: {a.source}; referência: {a.asOf}.</p>)}{result.excluded.map(a=><p key={a.ticker}><strong>{a.ticker} excluído</strong>: {a.reasons.join(" ")}</p>)}</details>
      </section>
      <section className="planner-section"><h2><ShieldCheck size={20}/>Limites e hipóteses</h2><ul className="planner-warnings">{result.warnings.map(w=><li key={w}>{w}</li>)}</ul></section>
    </div>}
  </main>;
}
