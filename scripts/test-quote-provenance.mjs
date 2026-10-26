import assert from 'node:assert/strict';
import test from 'node:test';
import { isIndicativeQuote, quoteSourceLabel, quoteClock } from '../lib/quote-provenance.ts';

test('source labels follow the actual origin, including a previous Yahoo reference', () => {
  assert.match(quoteSourceLabel({ source_url: 'https://brapi.dev/api/quote/BNBR3' }), /^BRAPI/);
  assert.match(quoteSourceLabel({ source_url: 'https://query1.finance.yahoo.com/v8/finance/chart/BNBR3.SA' }), /^Yahoo Finance/);
  for (const source_url of [undefined, 'invalid', 'https://brapi.dev.example.test/', 'https://query1.finance.yahoo.com.example.test/']) {
    assert.equal(quoteSourceLabel({ source_url, provider: 'brapi' }), 'Fonte da cotação');
  }
});

test('snapshot status and basis never imply a trade', () => {
  assert.equal(isIndicativeQuote({ status: 'indicative_snapshot' }), true);
  assert.equal(isIndicativeQuote({ status: 'stale_indicative_snapshot' }), true);
  assert.equal(isIndicativeQuote({ status: 'available', basis: 'provider_snapshot_not_confirmed_trade_or_executable_order' }), true);
  assert.equal(isIndicativeQuote({ status: 'available' }), false);
});

test('date-only last-trade references stay separate from precise UTC clocks', () => {
  assert.equal(quoteClock('2026-10-14'), '14/10/2026');
  assert.equal(quoteClock('2026-10-23T21:37:25+00:00'), '23/10/2026, 21:37:25 UTC');
  assert.equal(quoteClock('2026-10-24T23:30:00+00:00'), '24/10/2026, 23:30:00 UTC');
  assert.equal(quoteClock('invalid'), 'Não informado');
  assert.equal(quoteClock(null), 'Não informado');
});
