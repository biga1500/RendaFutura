import assert from 'node:assert/strict';
import test from 'node:test';
import { portfolioView } from '../lib/portfolio-view.ts';

const portfolios=['seguranca','proventos'].map((id,index)=>({id,label:id,
  allocation:[{ticker:index?'PETR4':'TD-SELIC-2027',weight:1}],simulation:{retirementWealth:[index,index,index]},
  planning:{requiredMonthlyContribution:index},portfolio:{annualVolatility:index*.1},
  incomeAnalysis:{monthlyCapacity:[index,index,index]},coverage:{allocatedAssets:1},
  historicalPaymentCalendar:[{month:1,tickers:[id]}],historicalCoverageMonths:index,
  usesEquityForecast:index===1}));
const result={selectedPortfolioId:'proventos',portfolios,referenceDate:'2026-09-30',model:{appliedMethod:'xgboost'},
  rollover:{events:[{referenceTicker:'TD-SELIC-2027'},{referenceTicker:'unallocated'}]}};

test('switching portfolios changes all financial results together, not just table labels',()=>{
  const view=portfolioView(result,'seguranca');
  for(const key of ['allocation','simulation','planning','portfolio','incomeAnalysis','coverage','historicalPaymentCalendar','historicalCoverageMonths','usesEquityForecast']){
    assert.deepEqual(view[key],portfolios[0][key]);
  }
  assert.equal(view.referenceDate,result.referenceDate);
  assert.deepEqual(view.model,result.model);
  assert.deepEqual(view.rollover.events,[{referenceTicker:'TD-SELIC-2027'}]);
  assert.equal(result.selectedPortfolioId,'proventos');
  assert.equal(result.rollover.events.length,2);
});

test('the initial view uses the engine-selected portfolio',()=>{
  assert.equal(portfolioView(result).selectedPortfolioId,'proventos');
  assert.deepEqual(portfolioView(result).allocation,portfolios[1].allocation);
});

test('an unknown selection falls back to a portfolio from the same saved result',()=>{
  assert.equal(portfolioView(result,'unknown').selectedPortfolioId,'seguranca');
  const legacy={simulation:{retirementWealth:[1,2,3]}};
  assert.equal(portfolioView(legacy),legacy);
});
