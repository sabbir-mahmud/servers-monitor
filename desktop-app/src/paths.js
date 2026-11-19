'use strict';
// Where the NexGuard device agent keeps its files on each system. Mirrors config.Dir() in
// device-agent/internal/config and the user-mode folders the dashboard's connect scripts use.
const path = require('node:path');

/**
 * @param {{platform: string, env: Record<string, string|undefined>, home: string}} o
 * @returns {{id: string, label: string, dir: string}[]}
 */
function candidates({ platform, env, home }) {
  const p = platform === 'win32' ? path.win32 : path.posix;
  const out = [];
  if (env.NEXGUARD_CONFIG_DIR) {
    out.push({ id: 'custom', label: 'Custom folder', dir: env.NEXGUARD_CONFIG_DIR });
  }
  switch (platform) {
    case 'win32':
      out.push({ id: 'system', label: 'Installed for this computer', dir: p.join(env.ProgramData || 'C:\\ProgramData', 'NexGuard') });
      if (env.LOCALAPPDATA) {
        out.push({ id: 'user', label: 'Installed for you', dir: p.join(env.LOCALAPPDATA, 'NexGuard') });
      }
      break;
    case 'darwin':
      out.push({ id: 'system', label: 'Installed for this computer', dir: '/Library/Application Support/NexGuard' });
      out.push({ id: 'user', label: 'Installed for you', dir: p.join(home, 'Library', 'Application Support', 'NexGuard') });
      break;
    default:
      out.push({ id: 'system', label: 'Installed for this computer', dir: '/etc/nexguard' });
      out.push({ id: 'user', label: 'Installed for you', dir: p.join(env.XDG_DATA_HOME || p.join(home, '.local', 'share'), 'nexguard') });
  }
  return out.map((c) => ({
    ...c,
    log: p.join(c.dir, 'logs', 'nexguard.log'),
    status: p.join(c.dir, 'status.json'),
  }));
}

module.exports = { candidates };
