import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const [output,fixturesPath,base='http://127.0.0.1:5175']=process.argv.slice(2);
const out=resolve(output);
const require=createRequire('C:/Users/gabri/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/package.json');
const {chromium}=require('playwright');
const browser=await chromium.launch({headless:true,channel:'msedge'});
const errors=[],warnings=[];
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(pathToFileURL(resolve(out,'Relatorio_Integracao.html')).href);
 await page.evaluate(()=>{document.querySelectorAll('details').forEach(d=>d.open=true);document.querySelectorAll('img').forEach(i=>i.loading='eager');});
 await page.waitForFunction(()=>[...document.images].length===10&&[...document.images].every(i=>i.complete&&i.naturalWidth>100));
 assert.equal(await page.locator('section').count(),8);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.screenshot({path:resolve(out,'relatorio-desktop.png')});
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.screenshot({path:resolve(out,'relatorio-mobile.png')});
 // Isolated rendering after the chart sizing fix; live API evidence remains
 // in browser-results.json. No accounts or server-side data are changed here.
 const fixture=JSON.parse(await readFile(fixturesPath,'utf8')).personas.at(-1);
 const audit=JSON.parse(await readFile(resolve(out,'engine-results.json'),'utf8'));
 const result=audit.cases.at(-1).experimental;
 const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:2});
 const ui=await context.newPage();
 ui.on('pageerror',e=>errors.push(e.message));ui.on('console',m=>{if(['warning','error'].includes(m.type()))warnings.push(m.text());});
 await context.route('**/api/**',async route=>{
  assert.equal(route.request().method(),'GET');
  const path=new URL(route.request().url()).pathname;
  const body=path==='/api/me'?{user:{id:'render-fixture',alias:'Persona fictícia',email:'render@example.test'},profile:{answers:fixture.answers,step:3,result:fixture.result,updated_at:audit.finishedAt}}
   :path==='/api/recommendation/status'?{engineVersion:result.engineVersion,realDataAvailable:true,realDataReference:result.referenceDate,realDataAssets:result.assetAnalysis.map(a=>a.ticker),demoAvailable:true}
   :{recommendation:{result,updatedAt:audit.finishedAt,settings:{incomeMode:'distributions_only',forecastPolicy:'xgboost_experimental',endAge:90,monthlyContribution:fixture.contribution,annualCostPercent:.5,simulations:500,blockMonths:6,seed:42,dataMode:'real',consent:true}}};
  await route.fulfill({json:body});
 });
 await ui.goto(`${base}/#planejamento`);
 await ui.getByRole('heading',{name:'Indicadores e filtros analisados',exact:true}).waitFor();
 await ui.waitForFunction(()=>document.querySelectorAll('.scenario-chart path.recharts-area-curve').length===3);
 assert.equal(await ui.locator('#plan-forecast').inputValue(),'xgboost_experimental');
 await ui.locator('.planner-results').screenshot({path:resolve(out,'08-final-ui-only.png')});
 await ui.setViewportSize({width:390,height:844});
 await ui.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
 assert.equal(warnings.filter(m=>/width\(-1\)|height\(-1\)/.test(m)).length,0);
 assert.deepEqual(errors,[]);
 await writeFile(resolve(out,'render-verification.json'),JSON.stringify({passed:true,reportImages:10,reportPersonas:8,isolatedFinalUi:true,errors,warnings},null,2));
 console.log('PASS: report images, desktop/mobile layout, final isolated UI render and no negative chart-size warning.');
}finally{await browser.close();}
