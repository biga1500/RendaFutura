import assert from 'node:assert/strict';
import { recommendationIsOutdated } from '../lib/recommendation-revision.ts';

const saved = { settings: { forecastPolicy: 'xgboost_experimental' }, result: {
  engineVersion: 'test-engine', dataRevision: 'snapshot-1', dataMode: 'real', portfolios: [{}],
  plan: { income_mode: 'distributions_only' }, model: { appliedMethod: 'xgboost' },
} };
const status = { engineVersion: 'test-engine', realDataRevision: 'snapshot-1' };
assert.equal(recommendationIsOutdated(saved, status), false);
assert.equal(recommendationIsOutdated(null, status), false);
assert.equal(recommendationIsOutdated(saved, { ...status, realDataRevision: 'snapshot-2' }), true);
assert.equal(recommendationIsOutdated(saved, { ...status, engineVersion: 'changed-engine' }), true);
assert.equal(recommendationIsOutdated({ ...saved, result: { ...saved.result, dataRevision: undefined } }, status), true);
assert.equal(recommendationIsOutdated(saved, null), false);
assert.equal(recommendationIsOutdated({ ...saved, result: { ...saved.result, dataMode: 'demo' } }, status), true);
assert.equal(recommendationIsOutdated({ ...saved, result: { ...saved.result, model: null } }, status), false);
assert.equal(recommendationIsOutdated({ ...saved, result: { ...saved.result, model: { appliedMethod: 'baseline' } } }, status), true);
const diagnostic = { ...saved, result: { status: 'income_data_unavailable', engineVersion: 'test-engine', dataRevision: 'snapshot-1', dataMode: 'real' } };
assert.equal(recommendationIsOutdated(diagnostic, status), false);
assert.equal(recommendationIsOutdated(diagnostic, { ...status, realDataRevision: 'snapshot-2' }), true);
console.log('PASS: saved results are invalidated by a data revision, engine change, or obsolete method.');
