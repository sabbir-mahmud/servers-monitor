'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const p = require('../src/pairing');

test('codes: case, spaces and dashes do not matter; the alphabet does', () => {
  assert.equal(p.normalizeCode('k7pq 3mxa'), 'K7PQ3MXA');
  assert.equal(p.normalizeCode('K7PQ-3MXA'), 'K7PQ3MXA');
  assert.equal(p.normalizeCode('K7PQ3MX0'), null); // 0 isn't in the alphabet
  assert.equal(p.normalizeCode('K7PQ3MXAB'), null);
  assert.equal(p.formatCode('K7PQ3MXA'), 'K7PQ-3MXA');
});

test('dashboard address: https, or http on the local network only', () => {
  assert.deepEqual(p.checkServer('monitor.example.com/'), { ok: true, server: 'https://monitor.example.com' });
  assert.deepEqual(p.checkServer(' https://m.example.com/nexguard/ '), { ok: true, server: 'https://m.example.com/nexguard' });
  assert.equal(p.checkServer('http://192.168.1.5:8000').ok, true);
  assert.equal(p.checkServer('http://localhost:8000').ok, true);
  assert.equal(p.checkServer('http://[::1]:8000').ok, true);
  assert.match(p.checkServer('http://monitor.example.com').error, /https/);
  assert.equal(p.checkServer('ftp://x').ok, false);
  assert.equal(p.checkServer('https://user:pw@x.example.com').ok, false);
  assert.equal(p.checkServer('https://x.example.com/?a=1').ok, false);
  assert.equal(p.checkServer('').ok, false);
});

test('deep links from the connect page and the phone QR code', () => {
  assert.deepEqual(p.parseDeepLink('nexguard://pair?server=https%3A%2F%2Fm.example.com&code=K7PQ3MXA'),
    { server: 'https://m.example.com', code: 'K7PQ3MXA' });
  assert.equal(p.parseDeepLink('nexguard://pair?server=http%3A%2F%2Fevil.example.com&code=K7PQ3MXA'), null);
  assert.equal(p.parseDeepLink('nexguard://wipe?server=https%3A%2F%2Fm.example.com&code=K7PQ3MXA'), null);
  assert.equal(p.parseDeepLink('https://m.example.com/d/K7PQ3MXA'), null);
  assert.equal(p.parseDeepLink('nexguard://pair?server=https%3A%2F%2Fm.example.com&code=bad'), null);
});

test('agent build names match device-agent/build.sh', () => {
  assert.equal(p.agentBuildName('darwin', 'arm64'), 'nexguard-darwin-arm64');
  assert.equal(p.agentBuildName('win32', 'x64'), 'nexguard-windows-amd64.exe');
  assert.equal(p.agentBuildName('linux', 'ia32'), null);
  assert.equal(p.dashboardPlatform('win32'), 'windows');
});

const odd = "/Applications/Nex Guard's.app/Contents/Resources/agent/nexguard";

test('elevated install on macOS: one quoted shell line inside AppleScript', () => {
  const { file, args } = p.elevatedInstall({ platform: 'darwin', agent: odd, server: 'https://m.example.com', code: 'K7PQ3MXA' });
  assert.equal(file, '/usr/bin/osascript');
  assert.match(args[1], /with administrator privileges$/);
  // The shell line inside the AppleScript string, unescaped, runs the agent twice.
  const inner = JSON.parse(args[1].match(/^do shell script ("(?:[^"\\]|\\.)*")/)[1]);
  assert.equal(inner, `'/Applications/Nex Guard'\\''s.app/Contents/Resources/agent/nexguard' 'pair' '--yes' '--via' 'app' 'https://m.example.com' 'K7PQ3MXA' || exit 1; '/Applications/Nex Guard'\\''s.app/Contents/Resources/agent/nexguard' install || exit 2`);
});

test('the shell line really passes each argument through intact', { skip: process.platform === 'win32' }, () => {
  const { args } = p.elevatedInstall({ platform: 'linux', agent: '/bin/echo', server: "https://x.example.com/a b'c", code: 'K7PQ3MXA' });
  assert.equal(args[0], '/bin/sh');
  const out = execFileSync('/bin/sh', ['-c', args[2].replace(' install || exit 2', ' done')]).toString();
  assert.equal(out, "pair --yes --via app https://x.example.com/a b'c K7PQ3MXA\ndone\n");
});

test('elevated install on Windows: UAC prompt around an encoded script', () => {
  const { file, args } = p.elevatedInstall({ platform: 'win32', agent: "C:\\Program Files\\NexGuard's\\agent\\nexguard.exe", server: 'https://m.example.com', code: 'K7PQ3MXA', logFile: 'C:\\Temp\\ng.log' });
  assert.equal(file, 'powershell.exe');
  const outer = args[args.length - 1];
  assert.match(outer, /-Verb RunAs -Wait -PassThru/);
  const inner = Buffer.from(outer.match(/-EncodedCommand (\S+)'/)[1], 'base64').toString('utf16le');
  assert.match(inner, /^& 'C:\\Program Files\\NexGuard''s\\agent\\nexguard\.exe' 'pair' '--yes' '--via' 'app' 'https:\/\/m\.example\.com' 'K7PQ3MXA' \*> 'C:\\Temp\\ng\.log'/);
  assert.match(inner, /install \*>> 'C:\\Temp\\ng\.log'\nif \(\$LASTEXITCODE -ne 0\) \{ exit 2 \}/);
});
