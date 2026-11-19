'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Tailer, readAll } = require('../src/tail');

const line = (msg) => JSON.stringify({ time: '2026-11-19T08:00:00Z', level: 'INFO', msg }) + '\n';

function tmp() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ngtail-'));
  return path.join(dir, 'nexguard.log');
}

test('readAll: rotated file first, then the current one', async () => {
  const log = tmp();
  fs.writeFileSync(log + '.1', line('a') + line('b'));
  fs.writeFileSync(log, line('c'));
  assert.deepEqual((await readAll(log)).map((e) => e.msg), ['a', 'b', 'c']);
  assert.deepEqual(await readAll(log + '.missing'), []);
});

test('follows appends, holds a half-written line, survives rotation', async () => {
  const log = tmp();
  fs.writeFileSync(log, line('old'));
  const got = [];
  const t = new Tailer(log, (entries) => got.push(...entries.map((e) => e.msg)), { interval: 1e6 });
  await t.start();

  fs.appendFileSync(log, line('one') + '{"msg":"tw');
  await t.poll();
  assert.deepEqual(got, ['one']);

  fs.appendFileSync(log, 'o é"}\n');
  await t.poll();
  assert.deepEqual(got, ['one', 'two é']);

  // The agent rotates after writing one more line we haven't read yet.
  fs.appendFileSync(log, line('last before rotate'));
  fs.renameSync(log, log + '.1');
  fs.writeFileSync(log, line('fresh'));
  await t.poll();
  assert.deepEqual(got, ['one', 'two é', 'last before rotate', 'fresh']);
  t.stop();
});

test('waits for a log that does not exist yet', async () => {
  const log = tmp();
  const got = [];
  const t = new Tailer(log, (entries) => got.push(...entries.map((e) => e.msg)), { interval: 1e6 });
  await t.start();
  await t.poll();
  fs.writeFileSync(log, line('hello'));
  await t.poll();
  assert.deepEqual(got, ['hello']);
  t.stop();
});
