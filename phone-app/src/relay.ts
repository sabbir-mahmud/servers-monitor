// Everything this app sends or reads goes through the Redis relay (Upstash's HTTPS REST
// API), never to the dashboard: the phone works from anywhere and the dashboard stays
// private. The dashboard's relay process picks messages up and leaves answers
// (main/apps/agent_api/relay.py, bridge.py).
//
// Pairing: the app's built-in login (config.ts) may only RPUSH to <prefix>:pair:in and
// GET <prefix>:pair:out:<nonce>. The phone sends the 6-digit code with a random nonce and
// a key it made, then polls for the answer under that nonce: its own check-in login.
// Check-ins and SOS: that login may only RPUSH to the phone's inbox, signed with the key.
import { hmac } from '@noble/hashes/hmac.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js';
import { getRandomBytes } from 'expo-crypto';

import { ApiError, type DeviceInfo } from './api';
import { RELAY, relayProblem } from './config';

export type RelayLogin = {
  url: string; // https://<db>.upstash.io
  token: string; // REST token of this phone's ACL user
  inbox: string; // <prefix>:d:<device id>:in
  key: string; // hex HMAC-SHA256 key the phone made; Redis and the dashboard's answers never carry it
};

const TIMEOUT_MS = 20_000;

/** One Redis command over Upstash REST: ["RPUSH", key, value] -> its result. */
async function rest(url: string, token: string, command: string[]): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(command),
      signal: controller.signal,
    });
  } catch {
    throw new ApiError("Can't reach the relay. Check the phone's internet connection.", 0);
  } finally {
    clearTimeout(timer);
  }
  let data = null as { result?: unknown; error?: unknown } | null;
  try {
    data = (await response.json()) as { result?: unknown; error?: unknown } | null;
  } catch {
    // not JSON: a proxy's error page, say
  }
  if (response.status === 401) {
    throw new ApiError('The relay refused this login.', 401);
  }
  if (!response.ok || (data && typeof data.error === 'string')) {
    const said = typeof data?.error === 'string' ? `: ${data.error}` : '';
    throw new ApiError(`The relay answered HTTP ${response.status}${said}.`, response.status);
  }
  return data?.result;
}

function sign(key: Uint8Array, body: string): string {
  return bytesToHex(hmac(sha256, key, utf8ToBytes(body)));
}

// ------------------------------------------------------------------------------ pairing

export type Paired = {
  login: RelayLogin;
  device: DeviceInfo;
  name: string; // who it's connected as
  disclosure: string; // what the account will see
  checkinSeconds: number;
};

type Answer = {
  state?: 'paired' | 'error';
  message?: string;
  relay?: Omit<RelayLogin, 'key'>;
  device?: DeviceInfo;
  name?: string;
  disclosure?: string;
  checkin_seconds?: number;
};

/** Send the 6-digit code and wait for the dashboard's relay process to answer (it picks
 * messages up within seconds while it runs). Throws ApiError with what to tell the person. */
export async function pairWithCode(code: string, info: object, waitMs = 45_000): Promise<Paired> {
  const problem = relayProblem();
  if (problem) throw new ApiError(problem, 0);
  const key = bytesToHex(getRandomBytes(32));
  const nonce = bytesToHex(getRandomBytes(12));
  const body = JSON.stringify({ at: Math.floor(Date.now() / 1000), nonce, code, secret: key, info });
  try {
    await rest(RELAY.url, RELAY.token, ['RPUSH', `${RELAY.prefix}:pair:in`, JSON.stringify({ t: 'pair', body })]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      throw new ApiError('The relay refused this app’s login. Update NexGuard.', 401);
    }
    throw error;
  }

  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    let raw: unknown;
    try {
      raw = await rest(RELAY.url, RELAY.token, ['GET', `${RELAY.prefix}:pair:out:${nonce}`]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 0) continue; // a network blip
      throw error;
    }
    if (typeof raw !== 'string') continue;
    let answer: Answer;
    try {
      answer = JSON.parse(raw) as Answer;
    } catch {
      continue;
    }
    if (answer.state === 'error') {
      throw new ApiError(answer.message ?? 'The dashboard refused that code.', 409);
    }
    if (answer.state === 'paired' && answer.relay && answer.device) {
      return {
        login: { ...answer.relay, key },
        device: answer.device,
        name: answer.name ?? answer.device.person ?? answer.device.account,
        disclosure: answer.disclosure ?? '',
        checkinSeconds: answer.checkin_seconds ?? 900,
      };
    }
  }
  throw new ApiError(
    'The dashboard didn’t answer. Its relay process may be stopped: start it (./run.sh), then try again.',
    504,
  );
}

// ------------------------------------------------------------------- check-ins and SOS

/** {"t":"ci","body":"<JSON text>","sig":"<hex>"}: the exact body text is what's signed, so
 * the dashboard needn't serialise JSON the way JavaScript does. */
export function sealCheckin(login: RelayLogin, report: object, at = Date.now()): string {
  const body = JSON.stringify({ at: Math.floor(at / 1000), report });
  return JSON.stringify({ t: 'ci', body, sig: sign(hexToBytes(login.key), body) });
}

/** An SOS (or its cancel: "I'm OK"), signed the same way: {"t":"sos","body":…,"sig":…}. */
export function sealSos(
  login: RelayLogin,
  location: object | undefined,
  cancel: boolean,
  at = Date.now(),
): string {
  const body = JSON.stringify({ at: Math.floor(at / 1000), location, cancel });
  return JSON.stringify({ t: 'sos', body, sig: sign(hexToBytes(login.key), body) });
}

/** RPUSH one sealed message. A 401 means the login was removed (the phone was removed on
 * the dashboard, or paired again elsewhere). */
export async function pushCheckin(login: RelayLogin, sealed: string): Promise<void> {
  try {
    await rest(login.url, login.token, ['RPUSH', login.inbox, sealed]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      throw new ApiError('This phone was removed on the dashboard. Pair it again.', 401);
    }
    throw error;
  }
}
