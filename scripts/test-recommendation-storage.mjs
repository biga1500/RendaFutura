import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { encodeRecommendation, decodeRecommendation } from '../lib/recommendation-storage.ts';

const value = { status: 'ready', source: 'Dados reais e proventos', rows: Array.from({ length: 37 }, (_, index) => ({
  index, price: 4838.4215, income: [0, 23.456789, 31], originalCost: 50000, sources: ['https://example.test'],
})), text: 'large-result-fixture '.repeat(130000) };
const stored = await encodeRecommendation(value);
assert.ok(stored.startsWith('rf-result-gzip-v1:'));
assert.ok(stored.length < 1_900_000);
assert.ok(JSON.stringify(value).length > 2_097_152);
assert.deepEqual(await decodeRecommendation(stored), value);
assert.deepEqual(await decodeRecommendation(JSON.stringify(value)), value);
await assert.rejects(decodeRecommendation('rf-result-gzip-v1:not-gzip'));
await assert.rejects(encodeRecommendation({ text: 'x'.repeat(12 * 1024 * 1024) }));
const tooLarge = gzipSync(JSON.stringify({ text: 'x'.repeat(13 * 1024 * 1024) })).toString('base64');
await assert.rejects(decodeRecommendation('rf-result-gzip-v1:' + tooLarge), /limite/);
console.log('PASS: large result lossless storage, legacy JSON, corrupt input and bounded decompression.');
