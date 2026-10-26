import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync } from 'node:fs';
import { QUESTIONS } from '../lib/questionnaire.ts';

const base = process.argv[2];
const require = createRequire(`${process.argv[3]}/package.json`);
const { chromium } = require('playwright');
const output = '.sites-runtime/market-verification-20261022';
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const password = 'Local-Market-Test-2026-only';
let registered = false;
async function api(path, method = 'GET', data) {
  const response = await context.request.fetch(`${base}/api/${path}`, {
    method, headers: { Origin: base }, ...(data ? { data } : {}), timeout: 120000,
  });
  const body = await response.json();
  assert.ok(response.ok(), `${response.status()}: ${JSON.stringify(body)}`);
  return body;
}
try {
  await api('auth/register', 'POST', {
    alias: 'Verificacao com dados reais', email: `market-${Date.now()}@example.test`, password, consent: true,
  });
  registered = true;
  const answers = Object.fromEntries(QUESTIONS.map(q => [q.id, q.type === 'checklist' ? ['Ações'] : q.options?.at(-1) || '0']));
  Object.assign(answers, { age: '30', retirementAge: '50', targetIncome: '5000', initialCapital: '50000',
    contribution: 'De R$ 501 a R$ 1.500' });
  await api('profile', 'POST', { answers, complete: true, step: 3, consent: true });
  const status = await api('recommendation/status');
  assert.equal(status.realDataAvailable, true);
  assert.ok(status.realDataAssets.includes('PETR4') && status.realDataAssets.includes('VALE3'));
  await page.goto(base);
  await page.getByRole('button', { name: 'Planejar aposentadoria', exact: false }).click();
  await page.getByText('Base real disponível', { exact: false }).waitFor();
  assert.equal(await page.getByLabel('Usar dados fictícios para demonstração.', { exact: false }).isChecked(), false);
  await page.getByLabel('Aporte mensal confirmado (R$)').fill('1500');
  await page.locator('details').filter({ hasText: 'Parâmetros dos cenários' }).locator('summary').first().click();
  await page.getByLabel('Simulações finais').selectOption('500');
  await page.getByLabel('Autorizo o processamento do perfil derivado', { exact: false }).check();
  const pending = page.waitForResponse(r => r.url() === `${base}/api/recommendation` && r.request().method() === 'POST', { timeout: 120000 });
  await page.getByRole('button', { name: 'Calcular planejamento', exact: true }).click();
  const response = await pending;
  const data = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(data));
  const result = data.recommendation.result;
  assert.equal(result.dataMode, 'real');
  assert.equal(result.financiallyValidated, false);
  assert.ok(result.methods.includes('XGBoost') && result.methods.includes('NSGA-II'));
  assert.equal(result.model.beatsBaseline, false);
  assert.equal(result.marketData.historyMonths, 67);
  assert.ok(Math.abs(result.allocation.reduce((sum, a) => sum + a.weight, 0) - 1) < 1e-6);
  assert.ok(result.allocation.some(a => a.ticker.startsWith('TD-')));
  assert.ok(result.allocation.every(a => !a.ticker.startsWith('ACAO-') && !a.ticker.startsWith('CAIXA-')));
  assert.ok(result.allocation.filter(a => a.assetClass !== 'acoes').every(a => a.cashYield12m === 0));
  assert.equal(result.marketData.sources.length, 20);
  await page.getByRole('heading', { name: 'Resultado experimental' }).waitFor();
  await page.screenshot({ path: `${output}/real-result-desktop.png`, fullPage: true });
  await page.getByRole('heading', { name: 'Fontes e parâmetros' }).scrollIntoViewIfNeeded();
  await page.getByText('Fontes consultadas e integridade', { exact: true }).click();
  await page.screenshot({ path: `${output}/sources-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/real-result-mobile.png`, fullPage: true });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
    console.log(JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('main *')]
      .filter(e => e.getBoundingClientRect().right > innerWidth + 1)
      .map(e => ({ tag: e.tagName, className: e.className.baseVal ?? e.className, width: e.getBoundingClientRect().width, right: e.getBoundingClientRect().right })).slice(0,20))));
  }
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const returned = (await api('recommendation')).recommendation.result;
  assert.deepEqual(returned, result);
  assert.deepEqual(errors, []);
  writeFileSync(`${output}/result.json`, JSON.stringify({ status, result, pageErrors: errors }, null, 2));
  console.log(JSON.stringify({ realDataMode: result.dataMode, reference: result.referenceDate,
    assets: result.allocation.map(a => a.ticker), months: result.marketData.historyMonths,
    modelBeatsBaseline: result.model.beatsBaseline, pageErrors: errors.length, mobileOverflow: false }));
} finally {
  if (registered) await api('account', 'DELETE', { password });
  await browser.close();
}
