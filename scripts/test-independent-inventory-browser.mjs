import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const [base = 'http://127.0.0.1:5176', personasFile, output = '.sites-runtime/inventory-check'] = process.argv.slice(2);
const persona = JSON.parse(readFileSync(personasFile, 'utf8')).personas[0];
const require = createRequire('C:/Users/gabri/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(45000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const password = 'Independent-Sources-QA-2026';
const report = { startedAt: new Date().toISOString() };
mkdirSync(output, { recursive: true });
let registered = false;
const api = async (path, method = 'GET', data) => {
  const response = await context.request.fetch(`${base}/api/${path}`, {
    method, headers: { Origin: base }, ...(data ? { data } : {}), timeout: 90000,
  });
  return { status: response.status(), body: await response.json() };
};
try {
  assert.equal((await api('recommendation/market')).status, 401);
  const signup = await api('auth/register', 'POST', {
    alias: 'Teste fontes independentes', email: `inventory-${Date.now()}@example.test`, password, consent: true,
  });
  assert.equal(signup.status, 201, JSON.stringify(signup.body));
  registered = true;
  assert.equal((await api('profile', 'POST', { answers: persona.answers, complete: true, consent: true, step: 3 })).status, 200);
  const response = await api('recommendation/market');
  assert.equal(response.status, 200, JSON.stringify(response.body));
  const data = response.body;
  assert.equal(data.counts.stocks, data.stocks.length);
  assert.equal(data.counts.treasury, data.treasury.length);
  assert.ok(data.stocks.length >= 468 && data.treasury.length >= 37);
  assert.equal(new Set(data.stocks.map(row => row.ticker)).size, data.stocks.length);
  assert.ok(data.stocks.every(row => !row.ticker.endsWith('F')));
  const b3 = data.stocks.find(row => row.ticker === 'B3SA3');
  assert.ok(b3.cvm && b3.fundamentals.fields.earnings_per_share.value != null);
  for (const ticker of ['BPAR3', 'BRBI11']) {
    const row = data.stocks.find(item => item.ticker === ticker);
    assert.equal(row.sector_code, 2);
    assert.equal(row.fundamentals.fields.free_cash_flow.value, null);
    assert.equal(row.fundamentals.fields.free_cash_flow.status, 'not_applicable');
  }
  const independent = data.stocks.find(row => row.quote.status === 'available'
    && row.fundamentals.fields.earnings_per_share.value != null && !row.payments.event_count);
  assert.ok(independent, 'A missing payment source must not erase quote/financial data.');
  assert.ok(data.treasury.some(row => row.history.observed_months < 61));
  assert.ok(data.treasury.every(row => row.payments.principal_counted_as_income === false));
  assert.equal(typeof data.model_sample_unchanged, 'boolean');
  const status = await api('recommendation/status');
  assert.equal(status.status, 200);
  assert.equal(status.body.marketInventory.counts.stocks, data.stocks.length);
  assert.equal(status.body.universePreparation.evaluatedStocks, data.stocks.length);
  assert.equal(status.body.universePreparation.readyStocks, data.counts.model_ready_stocks);

  await page.goto(`${base}/#perfil`);
  await page.getByRole('button', { name: 'Planejar aposentadoria', exact: true }).click();
  const panel = page.locator('.market-inventory');
  await panel.locator('summary').click();
  await panel.getByLabel('Buscar ativo no catálogo').waitFor();
  await panel.getByRole('button', { name: 'Próxima página de ativos' }).click();
  await panel.locator('.market-pagination').getByText(`26 a 50 de ${data.stocks.length}`, { exact: true }).waitFor();
  await panel.getByLabel('Buscar ativo no catálogo').fill('B3SA3');
  assert.equal(await panel.locator('tbody tr').count(), 1);
  await panel.getByRole('button', { name: 'Ver fontes de B3SA3' }).click();
  const fields = panel.getByRole('region', { name: 'Fontes e campos de B3SA3' });
  await fields.waitFor();
  assert.ok(await fields.getByRole('link', { name: 'CVM', exact: true }).count());
  assert.match(await fields.innerText(), /não são lucro anual previsto/);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/inventory-b3sa3-desktop.png`, fullPage: true });
  await panel.getByLabel('Buscar ativo no catálogo').fill('BPAR3');
  await panel.getByRole('button', { name: 'Ver fontes de BPAR3' }).click();
  const fcf = panel.locator('.market-fields tbody tr').filter({ has: page.getByText('FCL agregado', { exact: true }) });
  assert.match(await fcf.innerText(), /Não se aplica/);
  await panel.getByLabel('Buscar ativo no catálogo').fill(independent.ticker);
  const first = panel.locator('.planner-table-wrap').first().locator('tbody tr');
  assert.match(await first.innerText(), /R\$/);
  assert.match(await first.innerText(), new RegExp(independent.ticker));
  await panel.getByLabel('Buscar ativo no catálogo').fill('');
  await panel.getByRole('button', { name: 'Tesouro', exact: true }).click();
  await panel.locator('.market-pagination').getByText(`1 a 25 de ${data.treasury.length}`, { exact: true }).waitFor();
  await panel.getByRole('button', { name: 'Próxima página de ativos' }).click();
  assert.equal(await panel.locator('tbody tr').count(), data.treasury.length - 25);
  assert.doesNotMatch(await panel.innerText(), /unknown_|observed_partial_|no_periodic_/);
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: `${output}/inventory-treasury-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await panel.getByRole('button', { name: 'Página anterior de ativos' }).click();
  await page.screenshot({ path: `${output}/inventory-treasury-desktop.png`, fullPage: true });
  assert.deepEqual(errors, []);
  Object.assign(report, { completed: true, counts: data.counts, asOf: data.as_of,
    modelReference: status.body.realDataReference, modelAssets: status.body.realDataAssets.length,
    independentFieldsExample: independent.ticker, errors });
  console.log(JSON.stringify(report));
} finally {
  try {
    if (registered) {
      assert.equal((await api('account', 'DELETE', { password })).status, 200);
      report.accountDeleted = true;
    }
    writeFileSync(`${output}/report.json`, JSON.stringify({ ...report, errors }, null, 2));
  } finally { await browser.close(); }
}
