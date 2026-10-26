import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { initialPortfolio } from '../lib/initial-portfolio.ts';
import { sourceLink } from '../lib/source-link.ts';

const [base='http://127.0.0.1:5175',personasFile,output='.sites-runtime/live-portfolio-check']=process.argv.slice(2);
if(!personasFile)throw new Error('Pass the personas JSON path.');
const requestedIds=process.env.RF_PROFILE_IDS?.split(',');
const expectPaymentStartWarning=process.env.RF_EXPECT_PAYMENT_START_WARNING==='1';
const expectUnknownSourceDate=process.env.RF_EXPECT_UNKNOWN_SOURCE_DATE==='1';
const expectNativeSourceWarning=process.env.RF_EXPECT_NATIVE_SOURCE_WARNING==='1';
const simulateStaleStatus=process.env.RF_SIMULATE_STALE_STATUS==='1';
const simulateFailedStatusRefresh=process.env.RF_FAIL_STATUS_REFRESH==='1';
const personas=JSON.parse(readFileSync(personasFile,'utf8')).personas.filter(persona=>!requestedIds||requestedIds.includes(persona.id));
assert.ok(personas.length,'No requested test personas found.');
const require=createRequire('C:/Users/gabri/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const {chromium}=require('playwright');
const browser=await chromium.launch({headless:true,channel:'msedge'});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const page=await context.newPage();page.setDefaultTimeout(45000);
const errors=[];page.on('pageerror',error=>errors.push(error.message));
page.on('console',message=>{
  if(message.type()!=='error')return;
  if(simulateFailedStatusRefresh&&message.location().url===`${base}/api/recommendation/status`&&message.text().includes('503'))return;
  errors.push(message.text());
});
const password='Live-Portfolio-QA-2026';
const report={startedAt:new Date().toISOString(),cases:[]};
let pageStatusRequests=0;
if(simulateStaleStatus||simulateFailedStatusRefresh)await page.route('**/api/recommendation/status',async route=>{
  const response=await route.fetch();
  const body=await response.json();
  pageStatusRequests++;
  if(simulateFailedStatusRefresh&&pageStatusRequests===2)return route.fulfill({status:503,json:{error:'Falha simulada ao conferir a versão.'}});
  await route.fulfill({response,json:simulateStaleStatus&&pageStatusRequests===1?{...body,realDataRevision:'page-open-before-market-update'}:body});
});
mkdirSync(output,{recursive:true});
let registered=false;
const api=async(path,method='GET',data)=>{
  const response=await context.request.fetch(`${base}/api/${path}`,{method,headers:{Origin:base},...(data?{data}:{}),timeout:360000});
  return{status:response.status(),body:await response.json()};
};
try{
  const signup=await api('auth/register','POST',{alias:'Teste carteira real',email:`live-portfolio-${Date.now()}@example.test`,password,consent:true});
  assert.equal(signup.status,201,JSON.stringify(signup.body));registered=true;
  const engineStatus=await api('recommendation/status');
  assert.equal(engineStatus.status,200);
  for(const id of personas.map(persona=>persona.id)){
    const persona=personas.find(row=>row.id===id);
    const profile=await api('profile','POST',{answers:persona.answers,complete:true,consent:true,step:3});
    assert.equal(profile.status,200,JSON.stringify(profile.body));
    if(id==='01'){
      await page.goto(`${base}/#questionario`);
      await page.locator('.step-item').first().click();
      for(const width of [1440,1024,768,390]){
        await page.setViewportSize({width,height:1000});
        if(width>560)for(const [left,right] of [['goal','age'],['retirementAge','targetIncome'],['withdrawal','initialCapital']]){
          const a=await page.locator(`#${left}`).boundingBox(),b=await page.locator(`#${right}`).boundingBox();
          assert.ok(Math.abs(a.y-b.y)<1,`Unaligned ${left}/${right} at ${width}: ${a.y}, ${b.y}`);
        }
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        await page.screenshot({path:`${output}/questionnaire-${width}.png`,fullPage:true});
      }
      await page.setViewportSize({width:1440,height:1000});
      for(const settings of [{dataMode:'demo',forecastPolicy:'xgboost_experimental'}, {dataMode:'real',forecastPolicy:'validation'}]){
        const blocked=await api('recommendation','POST',{...settings,incomeMode:'distributions_only',endAge:90,monthlyContribution:persona.contribution,annualCostPercent:.5,simulations:500,blockMonths:6,seed:42,consent:true});
        assert.equal(blocked.status,400,JSON.stringify(blocked.body));
      }
    }
    await page.goto(`${base}/#perfil`);
    await page.reload();
    await page.getByRole('button',{name:'Planejar aposentadoria',exact:true}).click();
    await page.getByRole('heading',{name:'Planejamento de aposentadoria',exact:true}).waitFor();
    assert.equal(await page.locator('#plan-demo,#plan-forecast').count(),0);
    await page.getByText('Previsão de proventos: XGBoost',{exact:true}).waitFor();
    await page.getByLabel('Aporte mensal confirmado (R$)').fill(String(persona.contribution));
    await page.getByLabel('Idade final dos cenários').fill('90');
    await page.getByText('Parâmetros dos cenários',{exact:true}).click();
    await page.getByLabel('Simulações finais').selectOption('500');
    await page.locator('#plan-consent').check();
    const pending=page.waitForResponse(response=>response.url()===`${base}/api/recommendation`&&response.request().method()==='POST',{timeout:360000});
    await page.getByRole('button',{name:'Calcular planejamento',exact:true}).click();
    const response=await pending,body=await response.json();
    assert.equal(response.status(),200,JSON.stringify(body));
    const sent=response.request().postDataJSON();
    assert.equal(sent.dataMode,'real');assert.equal(sent.forecastPolicy,'xgboost_experimental');
    const result=body.recommendation.result;
    const paymentStartWarning=result.warnings.some(warning=>warning.includes('inicio declarado de pagamento'));
    const nativeSourceWarning=result.warnings.find(warning=>warning.startsWith('PNVL3:'));
    if(expectPaymentStartWarning)assert.equal(paymentStartWarning,true);
    if(expectNativeSourceWarning){
      assert.match(nativeSourceWarning??'',/datas programadas.*formulario nativo exato da CVM/);
      assert.match(nativeSourceWarning,/conflito permanece sem resolucao/);
      assert.doesNotMatch(nativeSourceWarning,/inicio declarado/);
    }
    assert.equal(result.engineVersion,engineStatus.body.engineVersion);
    assert.equal(result.dataRevision,engineStatus.body.realDataRevision);
    assert.ok(result.dataRevision);
    assert.equal(result.dataMode,'real');assert.equal(result.incomeAnalysis.usesPrincipal,false);
    assert.equal(result.assetAnalysis.length,engineStatus.body.realDataAssets.length);
    assert.equal(result.universeScreening.counts.stocks,468);
    assert.equal(result.universeScreening.stocks.length,468);
    assert.equal(result.universeScreening.counts.readyStocks+result.universeScreening.counts.pendingDataStocks+(result.universeScreening.counts.excludedBySelectionStocks??0),468);
    assert.equal(result.universeScreening.counts.readyStocks,engineStatus.body.universePreparation.readyStocks);
    assert.ok(result.universeScreening.stocks.filter(row=>row.dataReady).every(row=>row.preparationBasis==='rebuilt_from_reconciled_sources'));
    assert.equal(result.marketData.dataQualityPolicy,'RF-CALENDAR-RECONCILED-1');
    assert.equal(result.marketData.pointInTimeArchiveVerified,false);
    assert.equal(result.marketData.cashSettlementVerified,false);
    assert.equal(result.universeScreening.counts.eligibleStocks,result.assetAnalysis.filter(row=>row.eligible&&row.indicators!==null).length);
    assert.ok(result.universeScreening.stocks.filter(row=>!row.dataReady).every(row=>!row.eligible&&row.predictedCashPerShare===null&&(row.dataReasons.length||row.selectionReasons?.length)));
    const dividendSelection=result.universeScreening.dividendYieldSelection;
    if(process.env.RF_EXPECT_POSITIVE_DY_SCOPE==='1')assert.ok(dividendSelection);
    if(dividendSelection){
      assert.equal(dividendSelection.selectedTickersCount,262);
      assert.equal(dividendSelection.matchedCatalogTickersCount,258);
      assert.equal(dividendSelection.outsideScopeStocks,210);
      assert.equal(dividendSelection.sourceDate,null);
      assert.equal(dividendSelection.treasuryUnchanged,true);
      assert.ok(result.universeScreening.stocks.filter(row=>row.dataReady).every(row=>row.selectionCompatible));
      assert.ok(result.universeScreening.stocks.filter(row=>!row.selectionCompatible).every(row=>!row.dataReady&&!row.eligible&&row.predictedCashPerShare===null));
    }
    assert.equal(result.marketData.currentTreasuryOffers.length,37);
    assert.equal(result.currentTreasuryAnalysis.cashScenariosCalculated,37);
    assert.equal(result.currentTreasuryAnalysis.evaluatedContracts,37);
    if(engineStatus.body.currentContractPortfolios){
      assert.ok(result.currentTreasuryAnalysis.optimizedContracts>0);
      assert.equal(result.currentTreasuryAnalysis.portfolioReinvestmentIncluded,true);
      assert.equal(result.parameters.annualRebalance,false);
      assert.equal(result.rollover.contractualRedemptionPricing,true);
      assert.ok(result.assetAnalysis.filter(row=>row.ticker.startsWith('TD-')).every(row=>row.historyMonths===0&&row.historicalAnnualRealReturn===null));
    }else assert.equal(result.currentTreasuryAnalysis.optimizedContracts,0);
    assert.ok(result.currentTreasuryAnalysis.rows.every(row=>row.principalCountedAsIncome===false&&row.reinvestmentIncluded===false));
    assert.deepEqual((await api('recommendation')).body.recommendation.result,result);
    assert.deepEqual((await api('data')).body.recommendation.result,result);
    if(result.model){assert.equal(result.model.appliedMethod,'xgboost');assert.equal(result.model.usedForIncomeSimulation,true);}
    assert.ok(result.assetAnalysis.every(row=>!row.ticker.startsWith('ACAO-')&&!row.ticker.startsWith('CAIXA-')));
    const initial=initialPortfolio(result.allocation,result.assetAnalysis,result.plan.initial_capital,result.plan.monthly_contribution);
    assert.ok(initial.allocated<=persona.capital);
    if(persona.capital===0)assert.equal(initial.annualCash,0);
    if(simulateFailedStatusRefresh){
      await page.getByText('O planejamento foi salvo, mas a versão atual da base não pôde ser conferida.',{exact:false}).waitFor();
      assert.equal(await page.locator('.planner-results').count(),0,'An unverified saved result must not be presented as current.');
      await page.getByRole('button',{name:'Tentar conexão novamente',exact:true}).click();
    }
    await page.getByRole('heading',{name:'Sua carteira de aposentadoria',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Carteira para começar',exact:true}).waitFor();
    const unknownSourceDates=result.marketData.sources.filter(source=>!source.retrieved_at).length;
    const localAuditSources=result.marketData.sources.filter(source=>!sourceLink(source.url)).length;
    if(expectNativeSourceWarning){
      assert.ok(localAuditSources>0,'The real native-calendar source set must exercise local audit receipts.');
      const sources=page.locator('details').filter({has:page.getByText('Fontes consultadas e integridade',{exact:true})});
      assert.equal(await sources.locator('p.help').count(),result.marketData.sources.length);
      assert.equal(await sources.locator('p.help').filter({hasText:/Arquivo de auditoria:|Registro de auditoria local/}).count(),localAuditSources);
      assert.equal(await sources.locator('a').count(),result.marketData.sources.length-localAuditSources);
    }
    if(expectUnknownSourceDate){
      assert.ok(unknownSourceDates>0,'The missing source-date regression was not exercised.');
      assert.equal(await page.locator('p.help').filter({hasText:'coleta não registrada'}).count(),unknownSourceDates);
    }
    assert.equal(new URL(page.url()).hash,'#carteira');
    assert.equal(await page.getByRole('button',{name:'Calcular planejamento',exact:true}).count(),0);
    if(expectNativeSourceWarning)await page.getByText(nativeSourceWarning,{exact:true}).waitFor();
    if(simulateStaleStatus)assert.ok(pageStatusRequests>=2,'The status retained when the page opened was not refreshed after generation.');
    if(simulateFailedStatusRefresh)assert.ok(pageStatusRequests>=3,'The saved calculation was not recovered after status verification.');
    await page.getByRole('heading',{name:'Carteiras por segurança e proventos',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Universo analisado pelo motor',exact:true}).waitFor();
    const universe=page.locator('.universe-screening');
    if(dividendSelection){
      await universe.getByTestId('dividend-yield-selection').waitFor();
      await universe.getByLabel('Filtrar situação das ações').selectOption('scope');
      assert.equal(await universe.locator('tbody tr').count(),25);
      assert.match(await universe.locator('tbody tr').first().innerText(),/Fora do recorte de DY positivo/);
    }
    await universe.getByLabel('Filtrar situação das ações').selectOption('pending');
    const pendingTicker=result.universeScreening.stocks.find(row=>!row.dataReady&&row.selectionCompatible!==false).ticker;
    await universe.getByLabel('Buscar ação na análise do motor').fill(pendingTicker);
    assert.equal(await universe.locator('tbody tr').count(),1);
    assert.match(await universe.locator('tbody tr').innerText(),/Dados pendentes/);
    await universe.getByLabel('Buscar ação na análise do motor').fill('');
    await universe.getByLabel('Filtrar situação das ações').selectOption('all');
    await universe.screenshot({path:`${output}/${id}-full-universe.png`});
    assert.ok(result.portfolios.length>=1&&result.portfolios.length<=3);
    const volatility=result.portfolios.map(portfolio=>portfolio.portfolio.annualVolatility);
    assert.deepEqual(volatility,[...volatility].sort((a,b)=>a-b));
    for(const portfolio of result.portfolios){
      if(!engineStatus.body.currentContractPortfolios&&portfolio.allocation.filter(asset=>asset.weight>1e-12).every(asset=>asset.cashYield12m===0)){
        assert.deepEqual(portfolio.simulation.monthlyIncomeCapacity,[0,0,0]);
        assert.equal(portfolio.planning.requiredRetirementCapital,null);
      }
      if(engineStatus.body.currentContractPortfolios){
        assert.ok(portfolio.simulation.monthlyIncomeCapacity.every(Number.isFinite));
        assert.equal(portfolio.incomeAnalysis.usesPrincipal,false);
        assert.equal(portfolio.planning.requiredRetirementCapitalBasis,'independently_modeled_retirement_start_unit_annual_income');
        if(result.plan.initial_capital===0&&result.plan.monthly_contribution===0)
          assert.deepEqual(portfolio.simulation.monthlyIncomeCapacity,[0,0,0]);
      }
      await page.locator(`input[name="portfolio-choice"][value="${portfolio.id}"]`).check();
      await page.getByText(`Carteira em análise: ${portfolio.label}`,{exact:true}).waitFor();
      await page.getByRole('heading',{name:'Meses com pagamentos no histórico',exact:true}).waitFor();
      const waiting=page.waitForEvent('download');
      await page.getByRole('button',{name:'Exportar',exact:true}).click();
      const download=await waiting;
      const exported=JSON.parse(readFileSync(await download.path(),'utf8'));
      assert.equal(exported.activePortfolio.id,portfolio.id);
      assert.deepEqual(exported.activePortfolio.simulation,portfolio.simulation);
      assert.deepEqual(exported.activePortfolio.allocation,portfolio.allocation);
      assert.deepEqual(exported.activePortfolio.planning,portfolio.planning);
      assert.ok(exported.activePortfolio.initialPosition.allocated<=persona.capital);
      await page.locator('section[aria-labelledby="portfolio-comparison-title"]').screenshot({path:`${output}/${id}-${portfolio.id}-comparison.png`});
      await page.locator('section[aria-labelledby="initial-portfolio-title"]').screenshot({path:`${output}/${id}-${portfolio.id}-initial.png`});
    }
    await page.getByRole('heading',{name:'Como ler os cenários',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Rendimento e pagamentos do Tesouro',exact:true}).waitFor();
    if(engineStatus.body.currentContractPortfolios){
      await page.getByText(/· sem rebalanceamento anual\./).waitFor();
      await page.getByText(/os títulos usam fluxos contratuais nas condições simuladas/).waitFor();
      assert.equal(await page.getByText(/Os cenários reamostram históricos; não representam os pagamentos contratuais futuros/).count(),0);
    }
    await page.getByRole('heading',{name:'Ofertas atuais do Tesouro',exact:true}).waitFor();
    if(id==='07'){
      const offers=page.locator('section').filter({has:page.getByRole('heading',{name:'Ofertas atuais do Tesouro',exact:true})});
      assert.equal(await offers.locator('tbody tr').count(),37);
      assert.ok(await offers.getByRole('columnheader',{name:'Renda real em 12 meses por unidade, cenário central',exact:true}).count());
      if(engineStatus.body.currentContractPortfolios){
        assert.match(await offers.innerText(),/reinvestimento na mesma família e risco/);
        assert.doesNotMatch(await offers.innerText(),/integração dessas posições à carteira otimizada continua pendente/);
      }else assert.match(await offers.innerText(),/integração dessas posições à carteira otimizada continua pendente/);
      await offers.screenshot({path:`${output}/07-treasury-current-offers.png`});
    }
    assert.doesNotMatch(await page.locator('.planner-results').innerText(),/Companhia fictícia|CAIXA-A|FIXA-A/);
    await page.waitForFunction(()=>document.querySelectorAll('.scenario-chart path.recharts-area-curve').length===3);
    await page.screenshot({path:`${output}/${id}-desktop.png`,fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:`${output}/${id}-mobile.png`,fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('button',{name:'Voltar ao planejamento',exact:true}).click();
    await page.getByRole('heading',{name:'Planejamento de aposentadoria',exact:true}).waitFor();
    assert.equal(await page.getByLabel('Aporte mensal confirmado (R$)').inputValue(),String(persona.contribution));
    await page.getByRole('button',{name:'Ver carteira salva',exact:true}).click();
    await page.reload();
    await page.getByRole('heading',{name:'Sua carteira de aposentadoria',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Carteira para começar',exact:true}).waitFor();
    if(id==='07'){
      // Simulate an archived demonstration response only in this isolated browser.
      await page.route('**/api/recommendation',async route=>{
        if(route.request().method()!=='GET')return route.continue();
        return route.fulfill({json:{recommendation:{...body.recommendation,result:{...result,dataMode:'demo'}}}});
      });
      await page.reload();
      await page.getByText('Há um resultado anterior a esta versão.',{exact:false}).waitFor();
      assert.equal(await page.locator('.planner-results').count(),0);
      await page.unroute('**/api/recommendation');
      await page.route('**/api/recommendation',async route=>{
        if(route.request().method()!=='GET')return route.continue();
        return route.fulfill({json:{recommendation:{...body.recommendation,result:{...result,dataRevision:'archived-data-revision'}}}});
      });
      await page.reload();
      await page.getByText('Há um resultado anterior a esta versão.',{exact:false}).waitFor();
      assert.equal(await page.locator('.planner-results').count(),0);
      await page.unroute('**/api/recommendation');
    }
    report.cases.push({id,profile:persona.result.profile,referenceDate:result.referenceDate,dataRevision:result.dataRevision,paymentStartWarning,nativeSourceWarning,unknownSourceDates,localAuditSources,universeCounts:result.universeScreening.counts,model:result.model?.appliedMethod??'no_eligible_stocks',initial,allocation:result.allocation,portfolios:result.portfolios});
    console.log(`PASS ${id}: ${persona.result.profile}, real data, ${result.model?.appliedMethod??'Treasury cash flows'}, separate result screen, initial holdings and back/reload.`);
  }
  assert.deepEqual(errors,[]);report.completed=true;report.pageStatusRequests=pageStatusRequests;
}catch(error){
  report.failure={message:error.message,url:page.url(),body:(await page.locator('body').innerText().catch(()=>'' )).slice(0,8000)};
  await page.screenshot({path:`${output}/failure.png`,fullPage:true}).catch(()=>{});
  throw error;
}finally{
  try{
    await page.unrouteAll({behavior:'wait'});
    await page.close();
  }finally{
    try{
      if(registered){const cleanup=await api('account','DELETE',{password});assert.equal(cleanup.status,200);report.accountDeleted=true;}
      writeFileSync(`${output}/report.json`,JSON.stringify({...report,errors},null,2));
    }finally{await browser.close();}
  }
}
