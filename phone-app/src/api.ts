// The dashboard's device API (main/apps/devices/api.py): only /device/v1/pair. Check-ins go
// through the Redis relay (relay.ts).
import type { RelayLogin } from './relay';

export type DeviceInfo = {
  id: string;
  name: string;
  platform: string;
  account: string;
  person: string | null;
};

export type PairPreview = { disclosure: string; device: DeviceInfo };
export type Paired = PairPreview & { token: string; checkin_seconds: number; relay?: RelayLogin };

/** An answer from the dashboard that wasn't a success, in its own words where it gave some. */
export class ApiError extends Error {
  readonly status: number; // 0: couldn't reach the dashboard

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

const TIMEOUT_MS = 20_000;

async function post<T>(server: string, path: string, body: object): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${server}/device/v1/${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch {
    throw new ApiError(`Can't reach ${server}. Check the address and the phone's connection.`, 0);
  } finally {
    clearTimeout(timer);
  }
  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    // not JSON: a proxy's error page, say
  }
  if (!response.ok) {
    const said = (data as { error?: unknown } | null)?.error;
    throw new ApiError(
      typeof said === 'string' ? said : `The dashboard answered HTTP ${response.status}.`,
      response.status,
    );
  }
  return data as T;
}

export function previewPairing(server: string, code: string, info: object) {
  return post<PairPreview>(server, 'pair', { ...info, code, preview: true });
}

export function pair(server: string, code: string, info: object) {
  return post<Paired>(server, 'pair', { ...info, code });
}
