import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {resolve} from 'node:path';

const [base='http://127.0.0.1:5175',fixturesPath,outputPath]=process.argv.slice(2);
if(!fixturesPath||!outputPath)throw Error('Usage: base personas.json output-folder');
const out=resolve(outputPath);await mkdir(out,{recursive:true});
const fixtures=JSON.parse(await readFile(fixturesPath,'utf8')).personas;
const expected=JSON.parse(await readFile(resolve(out,'engine-results.json'),'utf8'));
assert.equal(expected.completed,true);
const require=createRequire('C:/Users/gabri/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/package.json');
const {chromium}=require('playwright');
const browser=await chromium.launch({headless:true,channel:'msedge'});
const audit={startedAt:new Date().toISOString(),base,testAccounts:2,cases:[]};
const accounts=new Map();
const save=()=>writeFile(resolve(out,'browser-results.json'),JSON.stringify(audit,null,2));
try{
 const guard=await browser.newContext();
 const blocked=await guard.request.post(`${base}/api/recommendation`,{headers:{Origin:'https://untrusted.example'},data:{}});
 assert.equal(blocked.status(),403);assert.equal(blocked.headers()['access-control-allow-origin'],undefined);
 const unauth=await guard.request.get(`${base}/api/recommendation/status`);assert.equal(unauth.status(),401);
 audit.crossOriginBlocked=true;audit.unauthenticatedBlocked=true;await guard.close();
 for(const persona of fixtures){
  const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:2,acceptDownloads:true});
  const page=await context.newPage();page.setDefaultTimeout(45000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  // Two normal accounts exercise profile replacement while keeping all runs
  // below the existing registration and per-user recommendation limits.
  const group=Number(persona.id)<=4?'A':'B';
  let account=accounts.get(group),created=false;
  const record={id:persona.id,name:persona.name,accountGroup:group};audit.cases.push(record);
  const api=async(path,method='GET',data)=>{
   const response=await context.request.fetch(`${base}/api/${path}`,{method,headers:{Origin:base},...(data===undefined?{}:{data}),timeout:180000});
   return{status:response.status(),body:await response.json()};
  };
  try{
   if(!account){
    account={email:`integration-${group}-${Date.now()}@example.test`,password:'Integration-'+randomBytes(16).toString('hex')};
    const registration=await api('auth/register','POST',{alias:`Teste integração ${group}`,...account,consent:true});
    assert.equal(registration.status,201,JSON.stringify(registration.body));accounts.set(group,account);
   }else{
    const login=await api('auth/login','POST',account);assert.equal(login.status,200,JSON.stringify(login.body));
   }
   created=true;
   const profile=await api('profile','POST',{answers:persona.answers,complete:true,step:3,consent:true});assert.equal(profile.status,200);
   assert.deepEqual((await api('me')).body.profile.result,persona.result);
   const status=await api('recommendation/status');assert.equal(status.status,200);
   assert.equal(status.body.engineVersion,expected.cases[0].result.engineVersion);assert.equal(status.body.cashIncomeDataAvailable,true);
   assert.ok(status.body.catalog.stocks>500&&status.body.catalog.treasury>20);
   record.catalog=status.body.catalog;
   await page.goto(`${base}/#planejamento`);
   await page.getByRole('heading',{name:'Planejamento de aposentadoria',exact:true}).waitFor();
   await page.getByLabel('Aporte mensal confirmado (R$)').fill(String(persona.contribution));
   await page.getByLabel('Idade final dos cenários').fill('90');
   await page.locator('details').filter({has:page.getByText('Parâmetros dos cenários',{exact:true})}).locator('summary').click();
   await page.getByLabel('Simulações finais').selectOption('500');await page.locator('#plan-consent').check();
   const caseExpected=expected.cases.find(c=>c.id===persona.id);
   for(const policy of ['validation',...(caseExpected.experimental?['xgboost_experimental']:[])]){
    await page.getByLabel('Previsão de proventos',{exact:true}).selectOption(policy);
    const pending=page.waitForResponse(r=>r.url()===`${base}/api/recommendation`&&r.request().method()==='POST',{timeout:180000});
    await page.getByRole('button',{name:'Calcular planejamento',exact:true}).click();
    const response=await pending,body=await response.json();assert.equal(response.status(),200,JSON.stringify(body));
    const result=body.recommendation.result;
    assert.deepEqual(result,policy==='validation'?caseExpected.result:caseExpected.experimental);
    assert.equal(result.incomeAnalysis.usesPrincipal,false);
    assert.equal(result.rollover.policy,'same_class_same_risk');
    assert.equal(result.rollover.principalCountedAsIncome,false);
    assert.equal(result.planning.portfolioReoptimized,false);
    assert.equal(result.planning.usesPrincipalForIncome,false);
    assert.equal(result.planning.attainmentCriterion,.8);
    assert.ok(result.dataCoverage.counts.catalogue>500);
    await page.getByRole('heading',{name:'Patrimônio-alvo e aporte necessário',exact:true}).waitFor();
    if(result.rollover.events.length)await page.getByRole('heading',{name:'Reinvestimento após o vencimento',exact:true}).waitFor();
    if(result.model){assert.equal(result.model.usedForIncomeSimulation,true);assert.equal(result.model.forecastHorizonMonths,12);}
    await page.waitForFunction(()=>document.querySelectorAll('.scenario-chart path.recharts-area-curve').length===3);
    const screenshot=resolve(out,`${persona.id}-${policy}-resultado.png`);
    await page.locator('.planner-results').screenshot({path:screenshot});
    record[policy]={screenshot,income:result.simulation.monthlyIncomeCapacity,model:result.model,allocation:result.allocation};
    const downloadEvent=page.waitForEvent('download');await page.getByRole('button',{name:'Exportar',exact:true}).click();
    const downloaded=JSON.parse(await readFile(await(await downloadEvent).path(),'utf8'));assert.deepEqual(downloaded.result,result);
   }
   await page.reload();await page.getByRole('heading',{name:'Indicadores e filtros analisados',exact:true}).waitFor();
   assert.equal(await page.locator('#plan-consent').isChecked(),false);
   const saved=await api('recommendation');assert.ok(saved.body.recommendation);
   await page.setViewportSize({width:390,height:844});
   await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
   await page.locator('.planner-metrics').first().scrollIntoViewIfNeeded();
   await page.screenshot({path:resolve(out,`${persona.id}-mobile.png`)});
   page.once('dialog',dialog=>dialog.accept());const removed=page.waitForResponse(r=>r.url()===`${base}/api/recommendation`&&r.request().method()==='DELETE');
   await page.getByRole('button',{name:'Excluir',exact:true}).click();assert.equal((await removed).status(),200);
   await page.waitForFunction(()=>!document.querySelector('.planner-results'));
   assert.equal((await api('recommendation')).body.recommendation,null);
   assert.deepEqual(errors,[]);record.completed=true;
   console.log(JSON.stringify({id:persona.id,policies:Object.keys(record).filter(k=>['validation','xgboost_experimental'].includes(k)),passed:true}));
  }finally{
   if(created){
    if(!record.completed||Number(persona.id)===4||Number(persona.id)===8){
     const removal=await api('account','DELETE',{password:account.password});assert.equal(removal.status,200);
     for(const item of audit.cases.filter(item=>item.accountGroup===group))item.testAccountDeleted=true;
    }else{assert.equal((await api('auth/logout','POST')).status,200);}
   }
   await save();await context.close();
  }
 }
 audit.completed=audit.cases.length===8&&audit.cases.every(c=>c.completed&&c.testAccountDeleted);
 assert.equal(audit.completed,true);audit.finishedAt=new Date().toISOString();await save();
 console.log('PASS: browser to API to real data to algorithms, both forecast policies, CORS and access guards, export, persistence, revocation, mobile.');
}finally{await browser.close();}
