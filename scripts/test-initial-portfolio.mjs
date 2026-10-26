import assert from 'node:assert/strict';
import test from 'node:test';
import { initialPortfolio } from '../lib/initial-portfolio.ts';
const quoteDate=new Date().toISOString().slice(0,10);

const allocation=[
  {ticker:'PETR4',name:'Petrobras',assetClass:'acoes',weight:.4,cashYield12m:.06,asOf:'2026-09-30'},
  {ticker:'TD-IPCA-CUPOM-2035',name:'Tesouro IPCA+',assetClass:'renda_fixa',weight:.6,cashYield12m:.05,asOf:'2026-09-30'},
];
const analysis=[{ticker:'PETR4',price:49.12,eligible:true,asOf:'2026-09-30'},
  {ticker:'TD-IPCA-CUPOM-2035',price:4418,eligible:true,asOf:'2026-09-30'}];

test('initial capital is distinct from future contributions and retirement wealth',()=>{
  const result=initialPortfolio(allocation,analysis,10000,1000);
  assert.deepEqual(result.rows.map(row=>row.budget),[4000,6000]);
  assert.deepEqual(result.rows.map(row=>row.nextContribution),[400,600]);
  assert.equal(result.rows[0].quantity,81);
  assert.equal(result.rows[1].quantity,1.35);
  assert.ok(result.allocated<=10000);
  assert.equal(result.remainder,Math.round((10000-result.allocated)*100)/100);
  assert.equal(result.annualCash,result.rows[0].amount*.06+result.rows[1].amount*.05);
  assert.equal(result.monthlyAverage,result.annualCash/12);
});

test('zero capital never manufactures an initial position or income from future contributions',()=>{
  const result=initialPortfolio(allocation,analysis,0,3000);
  assert.equal(result.allocated,0);assert.equal(result.annualCash,0);
  assert.ok(result.rows.every(row=>row.quantity===0&&row.amount===0));
  assert.deepEqual(result.rows.map(row=>row.nextContribution),[1200,1800]);
});

test('a budget below one trading increment remains unallocated',()=>{
  const result=initialPortfolio(allocation,analysis,1,1);
  assert.equal(result.allocated,0);assert.equal(result.remainder,1);
  assert.equal(result.annualCash,0);
});

test('unknown prices and excluded assets do not produce quantities or a fictitious return',()=>{
  const missing=initialPortfolio(allocation,[],10000,0);
  assert.ok(missing.rows.every(row=>row.quantity===null&&row.amount===0));
  assert.equal(missing.annualCash,null);
  const excluded=initialPortfolio(allocation,analysis.map(row=>({...row,eligible:false})),10000,0);
  assert.equal(excluded.allocated,0);
});

test('an unknown minimum trading fraction is not replaced by an invented Treasury lot',()=>{
  const rows=analysis.map(asset=>({...asset,currentQuote:{price:asset.price,as_of:quoteDate,
    purchase_available:true,minimum_fraction:null,minimum_investment:1,basis:'synthetic_unknown_fraction'}}));
  const result=initialPortfolio(allocation,rows,10000,0);
  assert.equal(result.rows.find(row=>row.ticker.startsWith('TD-')).quantity,null);
  assert.equal(result.rows.find(row=>row.ticker.startsWith('TD-')).amount,0);
});

test('rounding never spends more than the available budget',()=>{
  for(const capital of [0.01,0.03,49.12,999.99,10000,100000000]){
    const result=initialPortfolio(allocation,analysis,capital,0);
    assert.ok(result.allocated<=capital);
    assert.ok(result.remainder>=0);
    for(const row of result.rows)assert.ok(row.amount<=row.budget);
  }
  const rounded=allocation.map(row=>({...row,weight:row.weight+0.0000001}));
  assert.ok(initialPortfolio(rounded,analysis,100000000,0).allocated<=100000000);
});

test('invalid budgets or weights are rejected',()=>{
  assert.throws(()=>initialPortfolio(allocation,analysis,-1,0));
  assert.throws(()=>initialPortfolio(allocation,analysis,NaN,0));
  assert.throws(()=>initialPortfolio([{...allocation[0],weight:2}],analysis,100,0));
});

test('zero weights and numerical dust are not presented as purchase positions',()=>{
  const result=initialPortfolio([{...allocation[0],weight:1},{...allocation[1],weight:1e-16}],analysis,10000,1000);
  assert.deepEqual(result.rows.map(row=>row.ticker),['PETR4']);
});

test('current price changes quantities, never scales the forecast cash per share',()=>{
  const positions=[{...allocation[0],weight:1,predictedCashPerShare:3}];
  const observed=[{...analysis[0],currentQuote:{price:25,as_of:quoteDate,purchase_available:true,minimum_fraction:1,minimum_investment:null,basis:'last_observed_trade_not_order_offer'}}];
  const result=initialPortfolio(positions,observed,1000,0);
  assert.equal(result.rows[0].quantity,40);
  assert.equal(result.rows[0].asOf,quoteDate);
  assert.equal(result.annualCash,120);
});

test('an old Treasury not offered for purchase has no fabricated purchase quantity',()=>{
  const observed=analysis.map(row=>({...row,currentQuote:{price:null,as_of:'2026-10-23',purchase_available:false,minimum_fraction:.01,minimum_investment:null,basis:'not_in_current_purchase_catalogue'}}));
  const result=initialPortfolio(allocation,observed,10000,0);
  assert.equal(result.allocated,0);
  assert.equal(result.annualCash,null);
  assert.ok(result.rows.every(row=>row.quantity===null));
});

test('current Treasury income uses historical cash per unit, not the old yield times a new price',()=>{
  const positions=[{...allocation[1],weight:1}];
  const observed=[{...analysis[1],annualCashPaymentsYield:.04,currentQuote:{price:4000,as_of:quoteDate,purchase_available:true,minimum_fraction:.01,minimum_investment:40,basis:'official_purchase_catalogue_not_executable_order'}}];
  const result=initialPortfolio(positions,observed,10000,0);
  assert.equal(result.rows[0].quantity,2.5);
  assert.ok(Math.abs(result.annualCash-2.5*4418*.04)<1e-8);
});

test('stale or future observed quotes never become current purchase quantities',()=>{
  for(const as_of of ['2020-01-01','2099-01-01','invalid']){
    const observed=analysis.map(row=>({...row,currentQuote:{price:25,as_of,purchase_available:true,minimum_fraction:1,minimum_investment:null,basis:'last_observed_trade_not_order_offer'}}));
    const result=initialPortfolio(allocation,observed,10000,0);
    assert.equal(result.allocated,0);
    assert.ok(result.rows.every(row=>row.quantity===null));
  }
});

test('zero capital has zero income even when a current quote is unavailable',()=>{
  const observed=analysis.map(row=>({...row,currentQuote:{price:null,as_of:'2026-10-23',purchase_available:false,minimum_fraction:.01,minimum_investment:null,basis:'not_in_current_purchase_catalogue'}}));
  const result=initialPortfolio(allocation,observed,0,1000);
  assert.equal(result.allocated,0);
  assert.equal(result.annualCash,0);
  assert.equal(result.monthlyAverage,0);
});

test('income on confirmed positions remains separate from unquoted target allocations',()=>{
  const observed=[{...analysis[0],currentQuote:{price:25,as_of:quoteDate,purchase_available:true,minimum_fraction:1,minimum_investment:null,basis:'last_observed_trade_not_order_offer'}},
    {...analysis[1],currentQuote:{price:null,as_of:quoteDate,purchase_available:false,minimum_fraction:.01,minimum_investment:null,basis:'not_in_current_purchase_catalogue'}}];
  const positions=allocation.map(row=>({...row,predictedCashPerShare:row.assetClass==='acoes'?3:null}));
  const result=initialPortfolio(positions,observed,10000,0);
  assert.equal(result.annualCash,null);
  assert.equal(result.knownAnnualCash,480);
  assert.equal(result.unquotedBudget,6000);
  assert.equal(result.allocated,4000);
});
