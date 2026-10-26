import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const [base = 'http://127.0.0.1:5176', output = '.sites-runtime/current-quote-inventory'] = process.argv.slice(2);
const inventory = JSON.parse(execFileSync(resolve('.recommender-venv/Scripts/python.exe'), ['-c',
  'import json; from recommender.market_inventory import load_inventory; from recommender.current_quote_inventory import sync_cached_quotes; print(json.dumps(sync_cached_quotes(None, load_inventory())[0]))',
], { encoding: 'utf8', maxBuffer: 25 * 1024 * 1024 }));
const require = createRequire('C:/Users/gabri/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/package.json');
const { chromium } = require('playwright');
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
mkdirSync(output, { recursive: true });
const errors = [];
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(45000);
page.on('pageerror', error => errors.push(error.message));
let marketCalls = 0;
try {
  await page.route('**/api/**', async route => {
    assert.equal(route.request().method(), 'GET', 'This UI test must not write account or model data.');
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/me') return route.fulfill({ json: {
      user: { id: 'quote-provenance-test', alias: 'Teste de fontes', email: 'quotes@example.test' },
      profile: { answers: { age: '35', retirementAge: '65', targetIncome: '5000' }, step: 3,
        result: { profile: 'Moderado' }, updated_at: '2026-10-24T20:00:00Z' },
    } });
    if (path === '/api/recommendation/status') return route.fulfill({ json: {
      realDataAvailable: true, realDataReference: '2026-09-30', realDataAssets: [],
      marketInventory: { asOf: inventory.as_of, counts: inventory.counts },
    } });
    if (path === '/api/recommendation') return route.fulfill({ json: { recommendation: null } });
    if (path === '/api/recommendation/market') {
      marketCalls += 1;
      return marketCalls === 1
        ? route.fulfill({ status: 503, json: { error: 'Fonte temporariamente indisponível.' } })
        : route.fulfill({ json: inventory });
    }
    throw new Error('Unexpected API call: ' + path);
  });
  await page.goto(`${base}/#planejamento`);
  const panel = page.locator('.market-inventory');
  await panel.locator('summary').click();
  await panel.getByRole('alert').waitFor();
  await panel.getByRole('button', { name: 'Tentar novamente', exact: true }).click();
  await panel.getByLabel('Buscar ativo no catálogo').waitFor();
  await panel.getByRole('status').waitFor({ state: 'hidden' });
  assert.equal(marketCalls, 2);
  await panel.getByLabel('Buscar ativo no catálogo').fill('BNBR3');
  await panel.getByRole('button', { name: 'Ver fontes de BNBR3' }).click();
  const fields = panel.getByRole('region', { name: 'Fontes e campos de BNBR3' });
  const row = inventory.stocks.find(item => item.ticker === 'BNBR3');
  assert.equal(row.quote.status, 'indicative_snapshot');
  assert.equal(await fields.getByRole('link', { name: 'BRAPI, fonte secundária', exact: true }).getAttribute('href'), row.quote.source_url);
  for (const link of await fields.getByRole('link', { name: 'Yahoo Finance, fonte secundária', exact: true }).all()) {
    assert.match(await link.getAttribute('href'), /^https:\/\/query[12]\.finance\.yahoo\.com\//);
  }
  assert.equal(await fields.locator('time').nth(0).getAttribute('datetime'), row.quote.snapshot_at);
  assert.equal(await fields.locator('time').nth(1).getAttribute('datetime'), row.quote.retrieved_at);
  assert.match(await fields.innerText(), /14\/10\/2026/);
  assert.match(await fields.innerText(), /não confirma último negócio nem oferta executável/);
  assert.match(await fields.innerText(), /Referência anterior preservada/);
  assert.match(await panel.locator('tbody').first().innerText(), /Snapshot indicativo/);
  assert.match(await panel.innerText(), new RegExp(`${inventory.counts.quotes_available} cotações de negócios informadas`));
  assert.match(await panel.innerText(), new RegExp(`${inventory.counts.quotes_indicative_snapshots} snapshots indicativos recentes`));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await fields.scrollIntoViewIfNeeded();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await fields.evaluate(element => {
      const rect = element.getBoundingClientRect();
      return rect.left >= 0 && rect.right <= innerWidth;
    }), true);
    await page.screenshot({ path: `${output}/provenance-${width}.png`, fullPage: true });
  }
  await panel.getByLabel('Buscar ativo no catálogo').fill('CLSC3');
  await panel.getByRole('button', { name: 'Ver fontes de CLSC3' }).click();
  assert.match(await panel.locator('.market-fields').innerText(), /15\/10\/2026/);
  await panel.getByLabel('Buscar ativo no catálogo').fill('ALOS3');
  await panel.getByRole('button', { name: 'Ver fontes de ALOS3' }).click();
  assert.equal(await panel.locator('.market-fields').getByRole('link', { name: 'Yahoo Finance, fonte secundária', exact: true }).count(), 1);
  assert.equal(await panel.locator('.market-fields').getByText(/Snapshot indicativo:/).count(), 0);
  assert.deepEqual(errors, []);
  const report = { passed: true, marketCalls, viewports: [1440, 390], counts: inventory.counts, errors };
  writeFileSync(`${output}/browser-report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
