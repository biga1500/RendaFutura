import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { GROUPS, QUESTIONS } from '../lib/questionnaire.ts';

const runtime = '.sites-runtime/e2e-audit';
const state = JSON.parse(readFileSync('.sites-runtime/recommender-local.json', 'utf8').replace(/^\uFEFF/, ''));
const base = process.argv[2] || state.url;
const require = createRequire(`${process.argv[3]}/package.json`);
const { chromium } = require('playwright');
const email = `e2e-${Date.now()}@example.test`;
const password = 'Fictitious-E2E-2026';
const answers = Object.fromEntries(QUESTIONS.map(q => [q.id, q.type === 'checklist' ? ['Ações'] : q.options?.at(-1) || '0']));
Object.assign(answers, {
  age: '30', retirementAge: '50', targetIncome: '100000', initialCapital: '0',
  contribution: 'De R$ 501 a R$ 1.500',
  reaction: QUESTIONS.find(q => q.id === 'reaction').options[2],
  preference: QUESTIONS.find(q => q.id === 'preference').options[2],
  loss: QUESTIONS.find(q => q.id === 'loss').options[1],
  scenario: QUESTIONS.find(q => q.id === 'scenario').options[1],
});
mkdirSync(runtime, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
const page = await context.newPage();
const pageErrors = [];
const consoleWarnings = [];
const failedRequests = [];
const apiResponses = [];
const checks = [];
let registered = false;
let deleted = false;
page.on('pageerror', error => pageErrors.push(error.message));
page.on('console', message => {
  if (message.type() === 'warning') consoleWarnings.push(message.text());
});
page.on('requestfailed', request => {
  if (!request.failure()?.errorText.includes('ERR_ABORTED')) failedRequests.push({ url: request.url(), error: request.failure()?.errorText });
});
page.on('response', response => {
  if (response.url().startsWith(`${base}/api/`)) apiResponses.push({ path: new URL(response.url()).pathname, status: response.status() });
});
async function check(name, action) {
  await action();
  checks.push(name);
  console.log(`PASS ${name}`);
}
async function fillQuestion(q, value) {
  const field = page.locator(`[data-question="${q.id}"]`);
  if (q.fixedValue !== undefined) {
    assert.equal(await field.locator('input').inputValue(), q.fixedValue);
    assert.equal(await field.locator('input').getAttribute('readonly'), '');
  } else if (q.type === 'number' || q.type === 'money') await field.locator('input').fill(value);
  else if (q.type === 'select') {
    await field.getByRole('combobox').click();
    await page.getByRole('option', { name: value, exact: true }).click();
  } else if (q.type === 'radio') await field.getByRole('radio', { name: value, exact: true }).check();
  else for (const option of value) await field.getByRole('checkbox', { name: option, exact: true }).check();
}
async function downloadJson(button) {
  const pending = page.waitForEvent('download');
  await button.click();
  const download = await pending;
  return JSON.parse(readFileSync(await download.path(), 'utf8'));
}
async function mobileScreenshot(name) {
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: `${runtime}/${name}-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
}
async function generatePlan() {
  await page.getByLabel('Aporte mensal confirmado (R$)').fill('1500');
  const details = page.locator('details').filter({ has: page.getByText('Parâmetros dos cenários', { exact: true }) });
  if (!await details.evaluate(element => element.open)) await details.locator('summary').click();
  await page.getByLabel('Simulações finais').selectOption('500');
  await page.getByLabel('Usar dados fictícios para demonstração.', { exact: false }).check();
  await page.getByLabel('Autorizo o processamento do perfil derivado', { exact: false }).check();
  const pending = page.waitForResponse(response => response.url() === `${base}/api/recommendation` && response.request().method() === 'POST', { timeout: 120000 });
  await page.getByRole('button', { name: 'Calcular planejamento', exact: true }).click();
  const response = await pending;
  const body = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(body));
  await page.getByRole('heading', { name: 'Resultado demonstrativo' }).waitFor();
  return body.recommendation.result;
}
try {
  await page.goto(base);
  await page.getByRole('heading', { name: 'Acesse sua conta' }).waitFor();
  await mobileScreenshot('login');
  await check('cadastro exige consentimento separado', async () => {
    await page.getByRole('button', { name: 'Criar conta', exact: true }).click();
    await page.getByLabel('Nome ou apelido', { exact: true }).fill('Auditoria ficticia');
    await page.getByLabel('E-mail', { exact: true }).fill(email);
    await page.getByLabel('Senha', { exact: true }).fill(password);
    await page.getByLabel('Confirme a senha').fill(password);
    await mobileScreenshot('cadastro');
    assert.equal(await page.getByRole('checkbox').isChecked(), false);
    await page.getByRole('button', { name: 'Criar conta', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Autorize o uso dos dados' }).waitFor();
    await page.getByRole('checkbox').check();
    await page.getByRole('button', { name: 'Criar conta', exact: true }).click();
    await page.getByRole('heading', { name: 'Objetivo e horizonte', exact: true }).waitFor();
    registered = true;
  });
  await check('aviso de privacidade acessivel', async () => {
    await page.getByRole('button', { name: 'Privacidade', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    assert.match(await page.getByRole('dialog').innerText(), /LGPD/);
    await page.keyboard.press('Escape');
  });
  await check('questionario bloqueia ausencia de autorizacao e respostas', async () => {
    assert.equal(await page.locator('#profile-consent').isChecked(), false);
    await page.getByRole('button', { name: 'Salvar e continuar' }).click();
    await page.getByRole('alert').filter({ hasText: 'Autorize o uso das respostas' }).waitFor();
    await page.locator('#profile-consent').check();
    await page.getByRole('button', { name: 'Salvar e continuar' }).click();
    await page.getByRole('alert').filter({ hasText: 'Responda' }).waitFor();
  });
  for (let step = 0; step < GROUPS.length; step++) {
    await check(`questionario etapa ${step + 1} (${GROUPS[step].questions.length} perguntas)`, async () => {
      for (const q of GROUPS[step].questions) await fillQuestion(q, answers[q.id]);
      await page.screenshot({ path: `${runtime}/questionario-${step + 1}.png`, fullPage: true });
      await mobileScreenshot(`questionario-${step + 1}`);
      await page.getByRole('button', { name: step === 3 ? 'Concluir e ver meu perfil' : 'Salvar e continuar', exact: true }).click();
      await page.getByRole('heading', { name: step === 3 ? 'Seu perfil de investidor' : GROUPS[step + 1].title, exact: true }).waitFor();
      if (step === 0) {
        await page.reload();
        await page.getByRole('heading', { name: GROUPS[1].title, exact: true }).waitFor();
      }
    });
  }
  await check('18 respostas persistidas e perfil moderado calculado', async () => {
    const me = await (await page.request.get(`${base}/api/me`)).json();
    assert.equal(Object.keys(me.profile.answers).length, 18);
    assert.equal(me.profile.result.profile, 'Moderado');
    assert.equal(me.profile.result.tolerance, 50);
    await page.screenshot({ path: `${runtime}/perfil-desktop.png`, fullPage: true });
    await mobileScreenshot('perfil');
    await page.getByRole('button', { name: 'Planejar aposentadoria' }).click();
    await page.getByText('Base financeira real não configurada.', { exact: false }).waitFor();
  });
  await check('planejamento exige autorizacao e aporte compativel', async () => {
    assert.equal(await page.locator('#plan-consent').isChecked(), false);
    assert.equal(await page.locator('#plan-demo').isChecked(), false);
    assert.equal(await page.getByRole('button', { name: 'Calcular planejamento', exact: true }).isEnabled(), false);
    await page.getByLabel('Aporte mensal confirmado (R$)').fill('9000');
    await page.locator('#plan-demo').check();
    await page.locator('#plan-consent').check();
    await page.getByRole('button', { name: 'Calcular planejamento', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'faixa declarada' }).waitFor();
  });
  const started = Date.now();
  const result = await generatePlan();
  const generationSeconds = (Date.now() - started) / 1000;
  await check('motor integrado executa os quatro metodos e sinaliza meta inviavel', async () => {
    assert.equal(result.dataMode, 'demo');
    assert.equal(result.financiallyValidated, false);
    assert.match(result.methods.join(' '), /Content-Based/i);
    assert.match(result.methods.join(' '), /XGBoost/);
    assert.match(result.methods.join(' '), /NSGA-II/);
    assert.match(result.methods.join(' '), /Monte Carlo/);
    assert.ok(result.model && result.model.trainRows > 0 && Number.isFinite(result.model.rmse));
    assert.equal(result.simulation.successFrequency, 0);
    assert.ok(Math.abs(result.allocation.reduce((sum, item) => sum + item.weight, 0) - 1) < 1e-6);
    assert.ok(result.allocation.filter(item => item.assetClass === 'acoes').every(item => item.weight <= result.parameters.stockMaximum + 1e-7));
    assert.ok(result.allocation.filter(item => item.assetClass === 'acoes').reduce((sum, item) => sum + item.weight, 0) <= result.parameters.equityMaximum + 1e-7);
    assert.ok(result.allocation.filter(item => item.assetClass === 'liquidez').reduce((sum, item) => sum + item.weight, 0) >= result.parameters.liquidityMinimum - 1e-7);
    assert.ok(result.portfolio.annualVolatility <= result.parameters.annualVolatilityMaximum + 1e-7);
    const chart = page.locator('.scenario-chart path.recharts-area-curve');
    await chart.first().waitFor();
    assert.equal(await chart.count(), 3);
    await page.screenshot({ path: `${runtime}/planejamento-desktop.png`, fullPage: true });
  });
  await check('exportacao do planejamento contem resultado salvo', async () => {
    const exported = await downloadJson(page.getByRole('button', { name: 'Exportar', exact: true }));
    assert.deepEqual(exported.result, result);
  });
  await check('persistencia apos recarregar sem autorizar novo calculo automaticamente', async () => {
    await page.reload();
    await page.getByRole('heading', { name: 'Resultado demonstrativo' }).waitFor();
    assert.equal(await page.locator('#plan-consent').isChecked(), false);
  });
  await check('tela mobile sem transbordamento horizontal', async () => {
    await mobileScreenshot('planejamento');
  });
  await check('alterar respostas invalida planejamento e aplica perfil conservador', async () => {
    await page.getByRole('button', { name: 'Meu perfil', exact: true }).click();
    await page.getByRole('button', { name: 'Revisar respostas', exact: true }).click();
    await page.getByRole('button', { name: 'Situação financeira', exact: false }).click();
    await fillQuestion(QUESTIONS.find(q => q.id === 'reserve'), 'Ainda não tenho reserva');
    await page.getByRole('button', { name: 'Preferência de risco', exact: false }).click();
    await page.getByRole('button', { name: 'Concluir e ver meu perfil' }).click();
    await page.getByRole('heading', { name: 'Seu perfil de investidor' }).waitFor();
    assert.equal((await (await page.request.get(`${base}/api/me`)).json()).profile.result.profile, 'Conservador');
    assert.equal((await (await page.request.get(`${base}/api/recommendation`)).json()).recommendation, null);
    await page.getByRole('button', { name: 'Planejar aposentadoria' }).click();
  });
  const conservative = await generatePlan();
  await check('perfil conservador nao recebe acoes nem previsao desnecessaria', async () => {
    assert.equal(conservative.parameters.equityMaximum, 0);
    assert.equal(conservative.model, null);
    assert.ok(conservative.allocation.length > 0);
    assert.ok(conservative.allocation.every(item => item.assetClass !== 'acoes'));
    assert.ok(!conservative.methods.includes('XGBoost'));
    await page.screenshot({ path: `${runtime}/planejamento-conservador.png`, fullPage: true });
  });
  await check('exclusao e revogacao do planejamento', async () => {
    page.once('dialog', dialog => dialog.accept());
    await page.getByRole('button', { name: 'Excluir', exact: true }).click();
    await page.getByRole('heading', { name: 'Resultado demonstrativo' }).waitFor({ state: 'hidden' });
    assert.equal((await (await page.request.get(`${base}/api/recommendation`)).json()).recommendation, null);
    assert.equal(await page.locator('#plan-consent').isChecked(), false);
  });
  await check('exportacao e exclusao das respostas pelo titular', async () => {
    await page.getByRole('button', { name: 'Auditoria ficticia', exact: true }).click();
    const exported = await downloadJson(page.getByRole('button', { name: 'Exportar meus dados' }));
    assert.equal(exported.account.email, email);
    assert.equal(Object.keys(exported.questionnaire.answers).length, 18);
    assert.equal(JSON.stringify(exported).includes(password), false);
    await page.getByRole('button', { name: 'Excluir respostas e revogar autorização' }).click();
    await page.getByRole('button', { name: 'Excluir respostas', exact: true }).click();
    await page.getByRole('heading', { name: 'Objetivo e horizonte', exact: true }).waitFor();
    assert.equal((await (await page.request.get(`${base}/api/me`)).json()).profile, null);
    assert.equal(await page.locator('#profile-consent').isChecked(), false);
  });
  await check('logout, rejeicao de senha incorreta e novo login', async () => {
    await page.getByRole('button', { name: 'Sair da conta' }).click();
    await page.getByLabel('E-mail', { exact: true }).fill(email);
    await page.getByLabel('Senha', { exact: true }).fill('Incorrect-Password');
    await page.getByRole('button', { name: 'Entrar', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'E-mail ou senha inválidos' }).waitFor();
    await page.getByLabel('Senha', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Entrar', exact: true }).click();
    await page.getByRole('heading', { name: 'Objetivo e horizonte', exact: true }).waitFor();
  });
  await check('exclusao da conta exige senha e encerra acesso', async () => {
    await page.getByRole('button', { name: 'Auditoria ficticia', exact: true }).click();
    await page.getByRole('button', { name: 'Excluir minha conta' }).click();
    await page.getByLabel('Senha para excluir a conta').fill('Incorrect-Password');
    await page.getByRole('button', { name: 'Excluir definitivamente' }).click();
    await page.getByRole('alertdialog').getByRole('alert').filter({ hasText: 'Senha incorreta' }).waitFor();
    await page.getByLabel('Senha para excluir a conta').fill(password);
    await page.getByRole('button', { name: 'Excluir definitivamente' }).click();
    await page.getByRole('heading', { name: 'Acesse sua conta' }).waitFor();
    assert.equal((await page.request.get(`${base}/api/me`)).status(), 401);
    deleted = true;
  });
  assert.deepEqual(pageErrors, []);
  assert.deepEqual(failedRequests, []);
  const report = {
    testedAt: new Date().toISOString(), base, checks, pageErrors, consoleWarnings, failedRequests, apiResponses,
    methods: result.methods, generationSeconds,
    model: result.model, impossibleGoalSuccessFrequency: result.simulation.successFrequency,
    allocation: result.allocation.map(({ ticker, weight, assetClass }) => ({ ticker, weight, assetClass })),
    parameters: result.parameters,
    conservativeEquityMaximum: conservative.parameters.equityMaximum,
    accountDeleted: deleted, dataMode: result.dataMode, financiallyValidated: result.financiallyValidated,
  };
  writeFileSync(`${runtime}/report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  await page.screenshot({ path: `${runtime}/failure.png`, fullPage: true }).catch(() => {});
  writeFileSync(`${runtime}/failure.json`, JSON.stringify({ checks, error: String(error), pageErrors, consoleWarnings, failedRequests, apiResponses }, null, 2));
  throw error;
} finally {
  if (registered && !deleted) {
    const cleanup = await page.request.delete(`${base}/api/account`, { headers: { Origin: base }, data: { password } }).catch(() => null);
    if (!cleanup?.ok()) console.error('A conta ficticia requer limpeza manual apos a falha.');
  }
  await browser.close();
}
