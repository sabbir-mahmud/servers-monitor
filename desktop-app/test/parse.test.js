'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseLine } = require('../src/parse');

test('slog JSON line', () => {
  const e = parseLine('{"time":"2026-11-19T08:00:00Z","level":"WARN","msg":"check-in failed","error":"can\'t reach x","n":3}');
  assert.deepEqual(e, { time: '2026-11-19T08:00:00Z', level: 'WARN', msg: 'check-in failed', attrs: { error: "can't reach x", n: '3' } });
});

test('custom slog levels fold into the nearest name', () => {
  assert.equal(parseLine('{"level":"INFO+2","msg":"x"}').level, 'INFO');
  assert.equal(parseLine('{"level":"bogus","msg":"x"}').level, 'INFO');
});

test('plain text, blank lines and CRLF', () => {
  assert.equal(parseLine(''), null);
  assert.equal(parseLine('   '), null);
  assert.deepEqual(parseLine('hello\r'), { time: null, level: 'INFO', msg: 'hello', attrs: {} });
  assert.equal(parseLine('[1,2]').msg, '[1,2]');
});
