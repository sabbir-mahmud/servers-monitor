'use strict';
// One line of the agent's log: a JSON object from Go's slog ({"time","level","msg",...}).
// Anything else (an older agent, a hand edit) is shown as it is.

const LEVELS = new Set(['DEBUG', 'INFO', 'WARN', 'ERROR']);

/**
 * @param {string} line
 * @returns {{time: string|null, level: string, msg: string, attrs: Record<string, string>} | null}
 */
function parseLine(line) {
  const text = line.replace(/\r$/, '');
  if (!text.trim()) return null;
  let obj;
  try {
    obj = JSON.parse(text);
  } catch {
    return { time: null, level: 'INFO', msg: text, attrs: {} };
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    return { time: null, level: 'INFO', msg: text, attrs: {} };
  }
  const { time, level, msg, ...rest } = obj;
  const attrs = {};
  for (const [k, v] of Object.entries(rest)) {
    attrs[k] = typeof v === 'string' ? v : JSON.stringify(v);
  }
  const lvl = String(level || 'INFO').toUpperCase().replace(/[+-]\d+$/, '');
  return {
    time: typeof time === 'string' ? time : null,
    level: LEVELS.has(lvl) ? lvl : 'INFO',
    msg: typeof msg === 'string' ? msg : JSON.stringify(msg ?? ''),
    attrs,
  };
}

module.exports = { parseLine };
