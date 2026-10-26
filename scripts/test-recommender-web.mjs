import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
import { QUESTIONS } from '../lib/questionnaire.ts';

const base=process.argv[2] || 'http://127.0.0.1:5175';
const pass='Fictitious-QA-2026';
const userEmail=`qa-motor-${Date.now()}@example.test`;
let cookie='';
let apiChecks=0;
const answers={};
for(const question of QUESTIONS){
  answers[question.id]=question.type==='checklist'?[question.options[2]]:question.options?question.options.at(-1):'0';
}
Object.assign(answers,{age:'30',retirementAge:'50',targetIncome:'100000',initialCapital:'0',contribution:'De R$ 501 a R$ 1.500',reaction:QUESTIONS.find(q=>q.id==='reaction').options[2],preference:QUESTIONS.find(q=>q.id==='preference').options[2],loss:QUESTIONS.find(q=>q.id==='loss').options[2],scenario:QUESTIONS.find(q=>q.id==='scenario').options[2]});
const settings={endAge:90,monthlyContribution:1500,annualCostPercent:.5,simulations:500,blockMonths:6,seed:42,dataMode:'demo',consent:true};
async function api(path,method='GET',data,expected=200,origin=base,withCookie=true){
  const response=await fetch(`${base}/api/${path}`,{method,headers:{Origin:origin,...(withCookie&&cookie?{Cookie:cookie}:{}),...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});
  const text=await response.text();
  let body;try{body=JSON.parse(text);}catch{body={error:text};}
  assert.equal(response.status,expected,JSON.stringify(body));
  apiChecks++;
  const next=response.headers.get('set-cookie');if(next)cookie=next.split(';')[0];
  return body;
}
await api('recommendation','GET',undefined,401,base,false);
await api('auth/register','POST',{alias:'Teste fictício',email:userEmail,password:pass,consent:true},201);
await api('recommendation','POST',settings,409);
await api('profile','POST',{answers,complete:true,step:3,consent:true});
const status=await api('recommendation/status');assert.equal(status.realDataAvailable,false);
await api('recommendation','POST',{...settings,monthlyContribution:9000},400);
await api('recommendation','POST',{...settings,risk_level:2},400);
await api('recommendation','POST',settings,403,'http://example.test');
await api('recommendation','POST',{...settings,dataMode:'real'},503);
const start=Date.now();const result=(await api('recommendation','POST',settings)).recommendation;
assert.equal(result.result.dataMode,'demo');assert.equal(result.result.financiallyValidated,false);
assert.ok(Math.abs(result.result.allocation.reduce((s,a)=>s+a.weight,0)-1)<1e-6);
assert.equal(result.result.simulation.successFrequency,0);
assert.deepEqual((await api('recommendation')).recommendation,result);
assert.ok((await api('data')).recommendation);
const firstCookie=cookie;
await api('auth/register','POST',{alias:'Outra conta fictícia',email:`other-${Date.now()}@example.test`,password:pass,consent:true},201);
assert.equal((await api('recommendation')).recommendation,null);
await api('account','DELETE',{password:pass});
cookie=firstCookie;
await api('recommendation','DELETE');assert.equal((await api('recommendation')).recommendation,null);
const revokedCalculation=api('recommendation','POST',settings,409);
await new Promise(resolve=>setTimeout(resolve,400));
await api('recommendation','DELETE');
await revokedCalculation;
assert.equal((await api('recommendation')).recommendation,null);
await api('profile','POST',{answers,complete:true,step:3,consent:true});
assert.equal((await api('data')).recommendation,null);
await api('account','DELETE',{password:pass});await api('me','GET',undefined,401);
console.log(JSON.stringify({apiChecks,generationSeconds:(Date.now()-start)/1000,goalSuccessFrequency:result.result.simulation.successFrequency,engine:result.result.engineVersion}));
if(process.argv.includes('--browser-fixture')){
  await api('auth/register','POST',{alias:'Demonstração fictícia',email:`browser-${Date.now()}@example.test`,password:pass,consent:true},201);
  await api('profile','POST',{answers,complete:true,step:3,consent:true});
  const account=await api('me');
  writeFileSync('.sites-runtime/browser-fixture.json',JSON.stringify({email:account.user.email,password:pass,cookie,answers}));
}
