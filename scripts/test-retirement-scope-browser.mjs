import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { GROUPS, QUESTIONS, classify } from '../lib/questionnaire.ts';

const base = process.argv[2] || 'http://127.0.0.1:5175';
const runtimeModules = process.argv[3];
if (!runtimeModules) throw new Error('Pass the runtime node_modules directory as the second argument.');
const require = createRequire(`${runtimeModules}/playwright/package.json`);
const { chromium } = require('playwright');
const password = 'Retirement-Scope-Test-2026';
const output = '.sites-runtime/retirement-scope-check';
const browser = await chromium.launch({ headless: true, channel: 'msedge' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const answers = Object.fromEntries(QUESTIONS.map(question => [question.id,
  question.type === 'checklist' ? ['Ações'] : question.options?.at(-1) ?? '0',
]));
Object.assign(answers, { age: '30', retirementAge: '60', targetIncome: '5000', initialCapital: '10000' });
let registered = false;
try {
  await page.goto(`${base}/#cadastro`);
  await page.getByLabel('Nome ou apelido', { exact: true }).fill('Teste aposentadoria');
  await page.getByLabel('E-mail', { exact: true }).fill(`retirement-scope-${Date.now()}@example.test`);
  await page.getByLabel('Senha', { exact: true }).fill(password);
  await page.getByLabel('Confirme a senha').fill(password);
  await page.locator('#consent').check();
  await page.getByRole('button', { name: 'Criar conta', exact: true }).click();
  await page.getByRole('heading', { name: GROUPS[0].title, exact: true }).waitFor();
  registered = true;
  const goal = page.locator('[data-question="goal"]');
  assert.equal(await goal.getByRole('combobox').count(), 0);
  assert.equal(await goal.locator('input').inputValue(), answers.goal);
  assert.equal(await goal.locator('input').getAttribute('readonly'), '');
  await page.locator('#profile-consent').check();
  mkdirSync(output, { recursive: true });
  for (let step = 0; step < GROUPS.length; step++) {
    for (const question of GROUPS[step].questions) {
      if (question.fixedValue !== undefined) continue;
      const field = page.locator(`[data-question="${question.id}"]`);
      const value = answers[question.id];
      if (question.type === 'money' || question.type === 'number') await field.locator('input').fill(value);
      else if (question.type === 'select') {
        await field.getByRole('combobox').click();
        await page.getByRole('option', { name: value, exact: true }).click();
      } else if (question.type === 'radio') await field.getByRole('radio', { name: value, exact: true }).check();
      else for (const product of value) await field.getByRole('checkbox', { name: product, exact: true }).check();
    }
    if (step === 0) {
      await page.screenshot({ path: `${output}/desktop.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    await page.getByRole('button', { name: step === 3 ? 'Concluir e ver meu perfil' : 'Salvar e continuar', exact: true }).click();
    await page.getByRole('heading', { name: step === 3 ? 'Seu perfil de investidor' : GROUPS[step + 1].title, exact: true }).waitFor();
  }
  const me = await (await context.request.get(`${base}/api/me`)).json();
  assert.deepEqual(me.profile.answers, answers);
  assert.deepEqual(me.profile.result, classify(answers));
  const legacy = await context.request.post(`${base}/api/profile`, {
    headers: { Origin: base },
    data: { answers: { ...answers, goal: 'Construir uma reserva' }, complete: true, consent: true, step: 3 },
  });
  assert.equal(legacy.status(), 200, await legacy.text());
  const current = await (await context.request.get(`${base}/api/me`)).json();
  assert.equal(current.profile.answers.goal, answers.goal);
  assert.deepEqual(current.profile.result, me.profile.result);
  await page.reload();
  await page.getByRole('heading', { name: 'Seu perfil de investidor' }).waitFor();
  assert.equal(await page.getByText('Construir uma reserva', { exact: false }).count(), 0);
  await page.getByRole('button', { name: 'Planejar aposentadoria', exact: true }).click();
  await page.getByRole('heading', { name: 'Planejamento de aposentadoria', exact: true }).waitFor();
  assert.equal(await page.getByText('Objetivo principal declarado:', { exact: false }).count(), 0);
  assert.deepEqual(errors, []);
  console.log('PASS: fixed retirement goal, all questionnaire steps, unchanged profile, legacy normalization, persistence, desktop/mobile and planner.');
} finally {
  try {
    if (registered) {
      const cleanup = await context.request.delete(`${base}/api/account`, { headers: { Origin: base }, data: { password } });
      assert.equal(cleanup.status(), 200, await cleanup.text());
      console.log('PASS: disposable test account deleted.');
    }
  } finally { await browser.close(); }
}
