'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { candidates } = require('../src/paths');

test('macOS: system and per-user folders', () => {
  const c = candidates({ platform: 'darwin', env: {}, home: '/Users/kid' });
  assert.deepEqual(c.map((x) => x.dir), ['/Library/Application Support/NexGuard', '/Users/kid/Library/Application Support/NexGuard']);
  assert.equal(c[0].log, '/Library/Application Support/NexGuard/logs/nexguard.log');
  assert.equal(c[0].status, '/Library/Application Support/NexGuard/status.json');
});

test('Linux: /etc and XDG data home', () => {
  const c = candidates({ platform: 'linux', env: { XDG_DATA_HOME: '/data' }, home: '/home/a' });
  assert.deepEqual(c.map((x) => x.dir), ['/etc/nexguard', '/data/nexguard']);
  const d = candidates({ platform: 'linux', env: {}, home: '/home/a' });
  assert.equal(d[1].dir, '/home/a/.local/share/nexguard');
});

test('Windows: ProgramData and LOCALAPPDATA with backslashes', () => {
  const c = candidates({ platform: 'win32', env: { ProgramData: 'D:\\PD', LOCALAPPDATA: 'C:\\Users\\a\\AppData\\Local' }, home: 'C:\\Users\\a' });
  assert.deepEqual(c.map((x) => x.dir), ['D:\\PD\\NexGuard', 'C:\\Users\\a\\AppData\\Local\\NexGuard']);
  assert.equal(c[0].log, 'D:\\PD\\NexGuard\\logs\\nexguard.log');
});

test('NEXGUARD_CONFIG_DIR comes first', () => {
  const c = candidates({ platform: 'linux', env: { NEXGUARD_CONFIG_DIR: '/tmp/ng' }, home: '/h' });
  assert.equal(c[0].id, 'custom');
  assert.equal(c[0].dir, '/tmp/ng');
});
