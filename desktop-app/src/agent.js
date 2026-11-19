'use strict';
// Pairing this computer through the NexGuard agent bundled with the app.
//
// The app shows the dashboard's disclosure itself (a preview, which doesn't use the code),
// then on Allow runs the agent: `pair --yes --via app` (the person already agreed here)
// and `install`, either as the protected service (an administrator password, can block
// websites) or for this person only (`--user`, no administrator, no blocking).
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { agentBuildName, dashboardPlatform, elevatedInstall, formatCode } = require('./pairing');

const CLIENT = 'desktop-app';

/** The bundled agent, or in development the build from device-agent/dist. */
function agentPath({ isPackaged, resourcesPath, appDir, env = process.env }) {
  if (env.NEXGUARD_AGENT_BIN) return env.NEXGUARD_AGENT_BIN;
  if (isPackaged) {
    return path.join(resourcesPath, 'agent', process.platform === 'win32' ? 'nexguard.exe' : 'nexguard');
  }
  const name = agentBuildName();
  return name ? path.join(appDir, '..', 'device-agent', 'dist', name) : '';
}

function run(file, args, { timeout = 120000 } = {}) {
  return new Promise((resolve) => {
    execFile(file, args, { timeout, windowsHide: true, maxBuffer: 1 << 20 }, (err, stdout, stderr) => {
      resolve({
        code: err ? (typeof err.code === 'number' ? err.code : -1) : 0,
        out: `${stdout || ''}${stderr || ''}`.trim(),
        error: err && typeof err.code !== 'number' ? err.message : '',
      });
    });
  });
}

/** The agent's last "Error: …" line, which carries the dashboard's own words. */
function agentError(out, fallback) {
  const lines = String(out || '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const err = lines.reverse().find((l) => l.startsWith('Error:'));
  return err ? err.slice(6).trim() : (lines[0] || fallback);
}

let cachedVersion = null;
async function agentVersion(agent) {
  if (cachedVersion) return cachedVersion;
  const r = await run(agent, ['version'], { timeout: 15000 });
  if (r.code !== 0) throw new Error('The NexGuard agent in this app won’t start. Reinstall the app.');
  cachedVersion = r.out.split(/\s+/)[0];
  return cachedVersion;
}

/**
 * What the dashboard will see, from the dashboard itself. The code stays valid.
 * @returns {Promise<{disclosure: string, device: {name: string, account: string, person: string|null}}>}
 */
async function preview({ agent, server, code, protectedInstall }) {
  const version = await agentVersion(agent);
  let res;
  try {
    res = await fetch(`${server}/device/v1/pair`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': `nexguard-desktop/${version}` },
      body: JSON.stringify({
        code, preview: true, client: CLIENT, platform: dashboardPlatform(),
        app_version: `nexguard-agent ${version}`, agent_mode: protectedInstall ? 'system' : 'user',
        hostname: os.hostname(),
      }),
      signal: AbortSignal.timeout(20000),
      redirect: 'error',
    });
  } catch {
    throw new Error(`Can’t reach ${server}. Check the address and the internet connection.`);
  }
  let data = {};
  try {
    data = await res.json();
  } catch {
    // not JSON: not a NexGuard dashboard, or a proxy page
  }
  if (!res.ok) throw new Error(data.error || `The dashboard answered ${res.status}. Is ${server} a NexGuard dashboard?`);
  if (typeof data.disclosure !== 'string' || !data.device) {
    throw new Error(`${server} doesn’t look like a NexGuard dashboard.`);
  }
  return { disclosure: data.disclosure, device: data.device, code: formatCode(code) };
}

/**
 * Pair and install. Resolves {ok, mode} or {ok: false, error, cancelled}.
 */
async function connect({ agent, server, code, protectedInstall, userDir }) {
  if (!agent || !fs.existsSync(agent)) {
    return { ok: false, error: 'This copy of the app has no NexGuard agent for this computer. Download the app again.' };
  }
  if (!protectedInstall) {
    const base = ['--user', '--dir', userDir];
    const paired = await run(agent, [...base, 'pair', '--yes', '--via', 'app', server, code]);
    if (paired.code !== 0) return { ok: false, error: agentError(paired.out, paired.error || 'Pairing failed.') };
    const installed = await run(agent, [...base, 'install']);
    if (installed.code !== 0) {
      return { ok: false, error: `Paired, but the agent didn’t start: ${agentError(installed.out, 'unknown error')}` };
    }
    return { ok: true, mode: 'user' };
  }
  const logFile = path.join(os.tmpdir(), `nexguard-install-${process.pid}-${Date.now()}.log`);
  const cmd = elevatedInstall({ platform: process.platform, agent, server, code, logFile });
  const r = await run(cmd.file, cmd.args, { timeout: 10 * 60000 });
  let out = r.out;
  if (process.platform === 'win32') {
    try {
      out = fs.readFileSync(logFile, 'utf8') || out;
      fs.rmSync(logFile, { force: true });
    } catch {
      // nothing written: UAC said no before it ran
    }
  }
  if (r.code === 0) return { ok: true, mode: 'system' };
  const cancelled =
    (process.platform === 'darwin' && /-128|User canceled/i.test(out)) ||
    (process.platform === 'win32' && r.code === 3) ||
    (process.platform === 'linux' && r.code === 126);
  if (cancelled) {
    return { ok: false, cancelled: true, error: 'No administrator permission, so nothing was installed. The code still works. Try again, or turn off Protected install to install for you only.' };
  }
  if (process.platform === 'linux' && r.code === 127 && /pkexec/.test(out + r.error)) {
    return { ok: false, error: 'This computer can’t ask for an administrator password (no pkexec). Turn off Protected install to install for you only.' };
  }
  // The script's own status: 1 not paired, 2 paired but the service didn't start.
  // osascript ends its error with "(status)"; pkexec and PowerShell pass it through.
  const status = process.platform === 'darwin' ? Number((out.match(/\((\d+)\)\s*$/) || [])[1]) : r.code;
  const reason = agentError(out.replace(/^.*execution error:\s*/s, '').replace(/\s*\(\d+\)\s*$/, ''), r.error || 'unknown error');
  return { ok: false, error: status === 2 ? `Paired, but the service didn’t start: ${reason}` : reason };
}

module.exports = { agentPath, agentVersion, preview, connect, agentError, CLIENT };
