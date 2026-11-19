// What the dashboard's pair page QR code holds: nexguard://pair?server=…&code=…
// (main/apps/devices/views.py `_qr`). Parsed by hand: React Native's URL has no searchParams.

export type PairTarget = { server: string; code: string };

const ALPHABET = /^[23456789ABCDEFGHJKMNPQRSTWXYZ]{8}$/;

/** "k7pq-3mxa " -> "K7PQ3MXA": case, spaces and dashes don't matter, like on the dashboard. */
export function normalizeCode(raw: string): string {
  return raw.toUpperCase().replace(/[^A-Z0-9]/g, '');
}

export function isCode(code: string): boolean {
  return ALPHABET.test(code);
}

/** "monitor.example.com/" -> "https://monitor.example.com", or null if it isn't an address. */
export function normalizeServer(raw: string): string | null {
  let value = raw.trim().replace(/\/+$/, '');
  if (!value) return null;
  if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
  return /^https?:\/\/[A-Za-z0-9.\-:[\]]+(\/[A-Za-z0-9._~%\-/]*)?$/.test(value) ? value : null;
}

/** A phone can't reach the computer's own 127.0.0.1: say so instead of timing out. */
export function isLoopback(server: string): boolean {
  return /^https?:\/\/(localhost|127\.|\[::1\]|0\.0\.0\.0)/i.test(server);
}

export function parsePairLink(text: string): PairTarget | null {
  const match = /^nexguard:\/\/pair\?(.*)$/i.exec(text.trim());
  if (!match) return null;
  const params: Record<string, string> = {};
  for (const part of match[1].split('&')) {
    const [key, value = ''] = part.split('=');
    try {
      params[decodeURIComponent(key)] = decodeURIComponent(value.replace(/\+/g, ' '));
    } catch {
      return null;
    }
  }
  const server = normalizeServer(params.server ?? '');
  const code = normalizeCode(params.code ?? '');
  return server && isCode(code) ? { server, code } : null;
}
