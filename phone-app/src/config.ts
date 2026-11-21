// The relay this app pairs through, from .env at build time (EXPO_PUBLIC_* values are
// built into the app). Make them on the dashboard with `python manage.py phone_app_login`:
// a login that may only send pairing requests and read answers, never the dashboard's.
export const RELAY = {
  url: (process.env.EXPO_PUBLIC_REDIS_URL ?? '').replace(/\/+$/, ''),
  token: process.env.EXPO_PUBLIC_REDIS_TOKEN ?? '',
  prefix: process.env.EXPO_PUBLIC_REDIS_PREFIX || 'sm',
};

/** Why this build can't pair, or "" when it can. */
export function relayProblem(): string {
  if (!RELAY.url || !RELAY.token) {
    return 'This build of NexGuard has no relay set. Set EXPO_PUBLIC_REDIS_URL and EXPO_PUBLIC_REDIS_TOKEN in .env, then rebuild.';
  }
  if (!/^https:\/\//.test(RELAY.url)) {
    return 'EXPO_PUBLIC_REDIS_URL must be the relay’s https:// REST address.';
  }
  return '';
}
