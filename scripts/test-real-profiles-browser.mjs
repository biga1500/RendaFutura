import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { QUESTIONS, classify } from '../lib/questionnaire.ts';

const base = process.argv[2];
const require = createRequire(`${process.argv[3]}/package.json`);
const { chromium } = require('playwright');
const output = process.argv[4] || '.sites-runtime/real-profiles-20261022';
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const password = 'Disposable-Profile-Verification-2026';
const option = (id, index) => QUESTIONS.find(q => q.id === id).options[index];
const cases = [
  { id: 'C1', expected: 'Conservador', age: 24, retirement: 65, capital: 0, contribution: 500, target: 1000,
    edits: { reaction: 0, preference: 0, loss: 0, scenario: 0, withdrawal: 0, liquidity: 0 } },
  { id: 'C2', expected: 'Conservador', age: 30, retirement: 50, capital: 0, contribution: 1500, target: 100000,
    edits: { reserve: 0 } },
  { id: 'M1', expected: 'Moderado', age: 40, retirement: 60, capital: 100000, contribution: 1500, target: 3000,
    edits: { reaction: 2, preference: 2, loss: 1, scenario: 1 } },
  { id: 'M2', expected: 'Moderado', age: 35, retirement: 60, capital: 30000, contribution: 500, target: 2500,
    edits: { stability: 1, knowledge: 0 } },
  { id: 'A1', expected: 'Arrojado', age: 30, retirement: 50, capital: 50000, contribution: 1500, target: 5000, edits: {} },
  { id: 'A2', expected: 'Arrojado', age: 60, retirement: 65, capital: 2000000, contribution: 0, target: 5000, edits: {} },
];
const report = existsSync(`${output}/web-audit.json`) ? JSON.parse(readFileSync(`${output}/web-audit.json`, 'utf8')) : { date: '2026-10-22', purpose: 'Synthetic users and real market snapshot; operational testing, not financial validation', cases: [], checks: [] };
const settings = { endAge: 90, annualCostPercent: 0.5, simulations: 500, blockMonths: 6, seed: 42, dataMode: 'real', consent: true };
let firstResult;
try {
  for (const fixture of cases) {
    if (report.cases.some(c => c.id === fixture.id)) continue;
    const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, deviceScaleFactor: 2 });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    let registered = false;
    const request = async (path, method = 'GET', data) => {
      const response = await context.request.fetch(`${base}/api/${path}`, { method, headers: { Origin: base }, ...(data === undefined ? {} : { data }), timeout: 180000 });
      return { status: response.status(), body: await response.json() };
    };
    const api = async (...args) => { const r = await request(...args); assert.ok(r.status < 300, JSON.stringify(r)); return r.body; };
    try {
      assert.equal((await request('recommendation')).status, 401);
      await api('auth/register', 'POST', { alias: `Perfil de teste ${fixture.id}`, email: `profile-${fixture.id.toLowerCase()}-${Date.now()}@example.test`, password, consent: true });
      registered = true;
      assert.equal((await request('recommendation', 'POST', { ...settings, monthlyContribution: fixture.contribution })).status, 409);
      assert.equal((await api('recommendation')).recommendation, null);
      const answers = Object.fromEntries(QUESTIONS.map(q => [q.id, q.type === 'checklist' ? ['Ações'] : q.options?.at(-1) || '0']));
      Object.assign(answers, { age: String(fixture.age), retirementAge: String(fixture.retirement), initialCapital: String(fixture.capital), targetIncome: String(fixture.target), contribution: option('contribution', fixture.contribution === 0 ? 0 : fixture.contribution <= 500 ? 1 : 2) });
      for (const [id, index] of Object.entries(fixture.edits)) answers[id] = option(id, index);
      const profile = classify(answers);
      assert.equal(profile.profile, fixture.expected);
      await api('profile', 'POST', { answers, complete: true, step: 3, consent: true });
      assert.equal((await request('recommendation', 'POST', { ...settings, monthlyContribution: 999999 })).status, 400);
      await page.goto(base);
      await page.getByRole('button', { name: 'Planejar aposentadoria', exact: false }).click();
      await page.getByText('Base real disponível', { exact: false }).waitFor();
      assert.equal(await page.getByLabel('Usar dados fictícios para demonstração.', { exact: false }).isChecked(), false);
      await page.getByLabel('Aporte mensal confirmado (R$)').fill(String(fixture.contribution));
      await page.locator('details').filter({ hasText: 'Parâmetros dos cenários' }).locator('summary').first().click();
      await page.getByLabel('Simulações finais').selectOption('500');
      await page.getByLabel('Autorizo o processamento do perfil derivado', { exact: false }).check();
      const pending = page.waitForResponse(r => r.url() === `${base}/api/recommendation` && r.request().method() === 'POST', { timeout: 180000 });
      await page.getByRole('button', { name: 'Calcular planejamento', exact: true }).click();
      const response = await pending;
      const responseBody = await response.json();
      assert.equal(response.status(), 200, JSON.stringify(responseBody));
      const result = responseBody.recommendation.result;
      assert.equal(result.dataMode, 'real');
      assert.equal(result.forecastTarget.id, 'rights_by_record_date_v1');
      assert.equal(result.financiallyValidated, false);
      assert.equal(result.marketData.historyMonths, 67);
      assert.ok(result.marketData.sources.length >= 20);
      assert.ok(result.marketData.sources.every(source => /^https:\/\//.test(source.url) && /^[0-9a-f]{64}$/.test(source.sha256)));
      assert.ok(Math.abs(result.allocation.reduce((s, a) => s + a.weight, 0) - 1) < 1e-6);
      assert.ok(result.allocation.every(a => a.weight >= 0 && ['PETR4', 'VALE3', 'TD-SELIC-2027', 'TD-IPCA-2035'].includes(a.ticker)));
      const equityWeight = result.allocation.filter(a => a.assetClass === 'acoes').reduce((s, a) => s + a.weight, 0);
      assert.ok(equityWeight <= result.parameters.equityMaximum + 1e-6);
      assert.ok(result.allocation.filter(a => a.assetClass === 'acoes').every(a => a.weight <= result.parameters.stockMaximum + 1e-6));
      assert.ok(result.portfolio.annualVolatility <= result.parameters.annualVolatilityMaximum + 1e-6);
      assert.ok(result.allocation.filter(a => a.assetClass !== 'acoes').every(a => a.cashYield12m === 0));
      if (fixture.expected !== 'Arrojado') {
        assert.deepEqual(result.allocation.map(a => a.ticker), ['TD-SELIC-2027']);
        assert.equal(result.methods.includes('NSGA-II'), false);
        assert.equal(result.methods.includes('XGBoost'), false);
      } else {
        assert.ok(result.methods.includes('NSGA-II') && result.methods.includes('XGBoost'));
        assert.ok(Number.isFinite(result.model.xgboostMae) && Number.isFinite(result.model.baselineMae));
        assert.equal(result.model.appliedMethod, 'baseline');
        assert.equal(result.model.applicationPolicy, 'real_baseline_pending_prospective_validation');
        assert.ok(result.allocation.filter(a => a.assetClass === 'acoes').every(a => a.cashMethod.startsWith('Referência simples')));
      }
      if (fixture.id === 'C2') assert.equal(result.simulation.successFrequency, 0);
      await page.getByRole('heading', { name: 'Resultado experimental' }).waitFor();
      await page.screenshot({ path: `${output}/${fixture.id}-desktop.png`, fullPage: true });
      if (fixture.id === 'A1') {
        firstResult = result;
        const sections = page.locator('.planner-section');
        for (const [title, filename] of [['Alocação selecionada', 'allocation'], ['Trajetórias de patrimônio', 'trajectories'], ['Fontes e parâmetros', 'sources']]) {
          await sections.filter({ has: page.getByRole('heading', { name: title, exact: true }) }).screenshot({ path: `${output}/${filename}.png` });
        }
        const resultImage = await page.locator('.planner-results').screenshot();
        const sharp = require('sharp');
        const dimensions = await sharp(resultImage).metadata();
        const summaryHeight = await page.locator('.planner-results').evaluate(e => e.querySelector('.planner-section').getBoundingClientRect().top - e.getBoundingClientRect().top);
        await sharp(resultImage).extract({ left: 0, top: 0, width: dimensions.width, height: Math.min(Math.floor(summaryHeight * 2), dimensions.height) }).toFile(`${output}/summary.png`);
        await page.locator('form').filter({ has: page.getByLabel('Aporte mensal confirmado (R$)') }).screenshot({ path: `${output}/parameters.png` });
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: `${output}/${fixture.id}-mobile.png`, fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.deepEqual((await api('recommendation')).recommendation.result, result);
      assert.deepEqual(errors, []);
      await api('profile', 'POST', { answers: { ...answers, targetIncome: String(fixture.target + 1) }, complete: true, step: 3, consent: true });
      assert.equal((await api('recommendation')).recommendation, null);
      report.cases.push({ ...fixture, profile, result, pageErrors: errors, mobileOverflow: false, savedResultMatches: true, profileEditInvalidates: true });
      report.checks.push(`${fixture.id}: unauthenticated rejection, missing profile, account isolation, invalid contribution, real engine, constraints, browser, mobile, persistence, invalidation`);
      writeFileSync(`${output}/web-audit.json`, JSON.stringify(report, null, 2));
      console.log(JSON.stringify({ id: fixture.id, profile: profile.profile, successFrequency: result.simulation.successFrequency, annualVolatility: result.portfolio.annualVolatility, methods: result.methods, allocation: result.allocation.map(a => ({ ticker: a.ticker, percent: a.percent })) }));
    } finally {
      if (registered) await api('account', 'DELETE', { password });
      await context.close();
    }
  }
  assert.ok(firstResult);
  report.completed = true;
  writeFileSync(`${output}/web-audit.json`, JSON.stringify(report, null, 2));
} finally {
  await browser.close();
}
