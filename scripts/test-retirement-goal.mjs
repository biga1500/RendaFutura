import assert from 'node:assert/strict';
import test from 'node:test';
import { classify, QUESTIONS, retirementAnswers, validateAnswers } from '../lib/questionnaire.ts';

const goal = 'Planejar minha aposentadoria';

test('the questionnaire only offers retirement as its investment goal', () => {
  assert.deepEqual(QUESTIONS.find(question => question.id === 'goal').options, [goal]);
  assert.equal(QUESTIONS.find(question => question.id === 'goal').fixedValue, goal);
  assert.equal(QUESTIONS.length, 18);
  assert.deepEqual(validateAnswers({ goal }), { goal });
});

test('old investment goals cannot change the fixed retirement scope', () => {
  for (const unsupported of ['Gerar renda complementar', 'Construir uma reserva', 'Buscar independência financeira']) {
    assert.deepEqual(validateAnswers({ goal: unsupported }), { goal });
    assert.deepEqual(retirementAnswers({ goal: unsupported, age: '30' }), { goal, age: '30' });
  }
  assert.deepEqual(validateAnswers({}), { goal });
  assert.deepEqual(retirementAnswers(), { goal });
  assert.throws(() => validateAnswers(null), /Respostas inválidas/);
});

test('retirement keeps the existing risk classification', () => {
  const answers = Object.fromEntries(QUESTIONS.map(question => [
    question.id,
    question.type === 'checklist' ? ['Ações'] : question.options?.at(-1) ?? '0',
  ]));
  Object.assign(answers, { age: '30', retirementAge: '60', targetIncome: '5000', initialCapital: '10000' });
  const result = classify(validateAnswers(answers, true));
  assert.equal(result.profile, 'Arrojado');
  assert.equal(result.tolerance, 100);
  assert.equal(result.capacity, 100);
  assert.equal(result.horizon, 30);

  const noReserve = classify(validateAnswers({ ...answers, reserve: 'Ainda não tenho reserva' }, true));
  assert.equal(noReserve.profile, 'Conservador');
  assert.ok(noReserve.restrictions.some(reason => reason.includes('reserva de emergência')));

  const shortHorizon = classify(validateAnswers({ ...answers, withdrawal: 'Entre 1 e 3 anos' }, true));
  assert.equal(shortHorizon.profile, 'Moderado');

  const highLiquidity = classify(validateAnswers({ ...answers, liquidity: 'Muito provável' }, true));
  assert.equal(highLiquidity.profile, 'Conservador');
});
