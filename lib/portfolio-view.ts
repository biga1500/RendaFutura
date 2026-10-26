import type { EngineCalculation } from "./recommendation";

export function portfolioView(result:EngineCalculation, id?:string):EngineCalculation {
  const selected=result.portfolios?.find(portfolio=>portfolio.id===(id??result.selectedPortfolioId))??result.portfolios?.[0];
  if(!selected)return result;
  const tickers=new Set(selected.allocation.filter(asset=>asset.weight>1e-6).map(asset=>asset.ticker));
  return {...result,allocation:selected.allocation,simulation:selected.simulation,planning:selected.planning,
    portfolio:selected.portfolio,incomeAnalysis:selected.incomeAnalysis,coverage:selected.coverage,
    selectedPortfolioId:selected.id,comparisonLabel:selected.label,
    historicalPaymentCalendar:selected.historicalPaymentCalendar,
    historicalCoverageMonths:selected.historicalCoverageMonths,usesEquityForecast:selected.usesEquityForecast,
    rollover:result.rollover?{...result.rollover,events:result.rollover.events.filter(event=>tickers.has(event.referenceTicker))}:undefined};
}
