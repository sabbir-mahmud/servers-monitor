// Check-ins go to the Redis relay (Upstash's HTTPS REST API), never to the dashboard: the
// phone reports from anywhere and the dashboard stays private. Pairing gave this phone its
// own Redis login, allowed only to RPUSH to its own inbox, and a key to sign with. The
// dashboard's relay process picks the messages up (main/apps/agent_api/bridge.py).
import { hmac } from '@noble/hashes/hmac.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js';

import { ApiError } from './api';

export type RelayLogin = {
  url: string; // https://<db>.upstash.io
  token: string; // REST token of this phone's ACL user
  inbox: string; // sm:d:<device id>:in
  key: string; // hex HMAC-SHA256 key; Redis never sees it
};

const TIMEOUT_MS = 20_000;

/** {"t":"ci","body":"<JSON text>","sig":"<hex>"}: the exact body text is what's signed, so
 * the dashboard needn't serialise JSON the way JavaScript does. */
export function sealCheckin(login: RelayLogin, report: object, at = Date.now()): string {
  const body = JSON.stringify({ at: Math.floor(at / 1000), report });
  const sig = bytesToHex(hmac(sha256, hexToBytes(login.key), utf8ToBytes(body)));
  return JSON.stringify({ t: 'ci', body, sig });
}

/** RPUSH one sealed check-in. A 401 means the login was removed (the phone was removed on
 * the dashboard, or paired again elsewhere). */
export async function pushCheckin(login: RelayLogin, sealed: string): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(login.url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${login.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(['RPUSH', login.inbox, sealed]),
      signal: controller.signal,
    });
  } catch {
    throw new ApiError("Can't reach the relay. Check the phone's internet connection.", 0);
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401) {
    throw new ApiError('This phone was removed on the dashboard. Pair it again.', 401);
  }
  if (!response.ok) {
    throw new ApiError(`The relay answered HTTP ${response.status}.`, response.status);
  }
}
