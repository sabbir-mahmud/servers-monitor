'use strict';
// Pairing input and the commands that install the agent. Pure functions: tested without
// Electron (test/pairing.test.js).
const net = require('node:net');

// The dashboard's code alphabet (apps/devices/pairing.py): no 0/O, 1/I/L, U/V.
const ALPHABET = '23456789ABCDEFGHJKMNPQRSTWXYZ';
const CODE_RE = new RegExp(`^[${ALPHABET}]{8}$`);

/** "k7pq 3mxa" -> "K7PQ3MXA", or null when it can't be a code. */
function normalizeCode(raw) {
  const code = String(raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  return CODE_RE.test(code) ? code : null;
}

function formatCode(code) {
  return `${code.slice(0, 4)}-${code.slice(4)}`;
}

function isLocalHost(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (h === 'localhost' || h.endsWith('.local') || h.endsWith('.localhost')) return true;
  if (net.isIPv4(h)) {
    const [a, b] = h.split('.').map(Number);
    return a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
  }
  if (net.isIPv6(h)) return h === '::1' || /^f[cd]/.test(h) || /^fe[89ab]/.test(h);
  return false;
}

/**
 * The dashboard address as the agent will accept it (device-agent/internal/api CheckServer):
 * https, or http only on this machine or the local network.
 * @returns {{ok: true, server: string} | {ok: false, error: string}}
 */
function checkServer(raw) {
  let text = String(raw || '').trim();
  if (!text) return { ok: false, error: 'Enter the dashboard address.' };
  if (!text.includes('://')) text = 'https://' + text;
  let u;
  try {
    u = new URL(text);
  } catch {
    return { ok: false, error: `“${raw}” isn’t a dashboard address like https://monitor.example.com` };
  }
  if (!['https:', 'http:'].includes(u.protocol) || !u.host || u.username || u.password || u.search || u.hash) {
    return { ok: false, error: `“${raw}” isn’t a dashboard address like https://monitor.example.com` };
  }
  if (u.protocol === 'http:' && !isLocalHost(u.hostname)) {
    return { ok: false, error: 'Use the dashboard’s https:// address. Over plain http the pairing would cross the internet unencrypted.' };
  }
  // The dashboard pastes server addresses into scripts with the same limits (connect.py).
  const server = (u.protocol + '//' + u.host + u.pathname).replace(/\/+$/, '');
  if (!/^https?:\/\/[A-Za-z0-9.\-:[\]/_~%]+$/.test(server)) {
    return { ok: false, error: 'That address has characters a dashboard address can’t have.' };
  }
  return { ok: true, server };
}

/** nexguard://pair?server=…&code=… (the dashboard's connect page, the phone QR code). */
function parseDeepLink(raw) {
  let u;
  try {
    u = new URL(String(raw));
  } catch {
    return null;
  }
  if (u.protocol !== 'nexguard:') return null;
  const action = (u.hostname || u.pathname.replace(/^\/+/, '')).toLowerCase();
  if (action !== 'pair') return null;
  const code = normalizeCode(u.searchParams.get('code'));
  const server = checkServer(u.searchParams.get('server'));
  if (!code || !server.ok) return null;
  return { server: server.server, code };
}

/** Node's platform -> the dashboard's Platform value. */
function dashboardPlatform(p = process.platform) {
  return { darwin: 'macos', win32: 'windows', linux: 'linux' }[p] || p;
}

/** Go's names for the agent build in device-agent/dist. */
function agentBuildName(platform = process.platform, arch = process.arch) {
  const os = { darwin: 'darwin', win32: 'windows', linux: 'linux' }[platform];
  const a = { x64: 'amd64', arm64: 'arm64' }[arch];
  if (!os || !a) return null;
  return `nexguard-${os}-${a}${os === 'windows' ? '.exe' : ''}`;
}

const shQuote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
const psQuote = (s) => `'${String(s).replace(/'/g, "''")}'`;
const appleScriptString = (s) => `"${String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;

/**
 * How to run "pair, then install the protected service" as an administrator, per system.
 * Exit codes inside: 0 done, 1 not paired, 2 paired but the service didn't start.
 * @returns {{file: string, args: string[]}}
 */
function elevatedInstall({ platform, agent, server, code, logFile }) {
  const pairArgs = ['pair', '--yes', '--via', 'app', server, code];
  if (platform === 'win32') {
    const q = psQuote(agent);
    const log = psQuote(logFile);
    const inner = [
      `& ${q} ${pairArgs.map(psQuote).join(' ')} *> ${log}`,
      'if ($LASTEXITCODE -ne 0) { exit 1 }',
      `& ${q} install *>> ${log}`,
      'if ($LASTEXITCODE -ne 0) { exit 2 }',
      'exit 0',
    ].join('\n');
    const encoded = Buffer.from(inner, 'utf16le').toString('base64');
    // UAC asks; a "No" throws, which we report as 3.
    const outer = [
      'try {',
      `  $p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList '-NoProfile -ExecutionPolicy Bypass -EncodedCommand ${encoded}'`,
      '  exit $p.ExitCode',
      '} catch { exit 3 }',
    ].join('\n');
    return { file: 'powershell.exe', args: ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', outer] };
  }
  const sh = `${shQuote(agent)} ${pairArgs.map(shQuote).join(' ')} || exit 1; ${shQuote(agent)} install || exit 2`;
  if (platform === 'darwin') {
    const script = `do shell script ${appleScriptString(sh)} with prompt ${appleScriptString('NexGuard needs an administrator to install its protected service.')} with administrator privileges`;
    return { file: '/usr/bin/osascript', args: ['-e', script] };
  }
  return { file: 'pkexec', args: ['/bin/sh', '-c', sh] };
}

module.exports = {
  normalizeCode, formatCode, checkServer, parseDeepLink, isLocalHost,
  dashboardPlatform, agentBuildName, elevatedInstall, shQuote, psQuote,
};
