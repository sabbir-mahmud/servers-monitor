#!/usr/bin/env node
'use strict';
// Copies the device agent builds (device-agent/build.sh -> device-agent/dist) into
// agent-bin/<os>-<arch>/, where electron-builder picks the one for each installer
// (extraResources in package.json). Checks each against dist/SHA256SUMS.
//   node scripts/agent-bin.js            every build that exists
//   node scripts/agent-bin.js --require mac   fail unless both mac builds are there (CI)
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const dist = process.env.NEXGUARD_AGENT_DIST || path.join(__dirname, '..', '..', 'device-agent', 'dist');
const out = path.join(__dirname, '..', 'agent-bin');
const TARGETS = [
  ['mac', 'x64', 'nexguard-darwin-amd64', 'nexguard'],
  ['mac', 'arm64', 'nexguard-darwin-arm64', 'nexguard'],
  ['win', 'x64', 'nexguard-windows-amd64.exe', 'nexguard.exe'],
  ['win', 'arm64', 'nexguard-windows-arm64.exe', 'nexguard.exe'],
  ['linux', 'x64', 'nexguard-linux-amd64', 'nexguard'],
  ['linux', 'arm64', 'nexguard-linux-arm64', 'nexguard'],
];

const required = process.argv.includes('--require') ? process.argv[process.argv.indexOf('--require') + 1] : null;
const sums = {};
try {
  for (const line of fs.readFileSync(path.join(dist, 'SHA256SUMS'), 'utf8').split('\n')) {
    const [hash, name] = line.trim().split(/\s+/);
    if (hash && name) sums[name.replace(/^\*/, '')] = hash.toLowerCase();
  }
} catch {
  console.error(`No ${path.join(dist, 'SHA256SUMS')}: build the agent first (make device-agent).`);
  process.exit(1);
}

let missing = 0;
for (const [os, arch, build, name] of TARGETS) {
  const src = path.join(dist, build);
  if (!fs.existsSync(src)) {
    if (required === os) {
      console.error(`missing ${src}`);
      missing++;
    }
    continue;
  }
  const hash = crypto.createHash('sha256').update(fs.readFileSync(src)).digest('hex');
  if (sums[build] !== hash) {
    console.error(`${build} doesn't match SHA256SUMS: rebuild the agent.`);
    process.exit(1);
  }
  const dir = path.join(out, `${os}-${arch}`);
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(src, path.join(dir, name));
  fs.chmodSync(path.join(dir, name), 0o755);
  console.log(`agent-bin/${os}-${arch}/${name} <- ${build}`);
}
if (missing) process.exit(1);
