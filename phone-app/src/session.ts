// The pairing this phone holds, in the Keychain / Keystore only: its relay login and the
// key it signs with. Nothing about the dashboard itself: the phone never contacts it.
import * as SecureStore from 'expo-secure-store';

import type { DeviceInfo } from './api';
import type { RelayLogin } from './relay';

export type Session = {
  device: DeviceInfo;
  checkinSeconds: number;
  relay: RelayLogin;
  name: string; // who it's connected as (shown on the status screen)
  disclosure: string; // what the account sees, shown when asking for permissions
  permissionsAsked: boolean; // the permissions step after connecting is done
  // Controls can be turned on for this phone (a child's or staff member's, on Android):
  // then it can't be unpaired from the phone, only removed on the dashboard.
  controlled: boolean;
  settingsDigest: string; // the dashboard's settings this phone has ("" = none yet)
  controls: Record<string, unknown> | null; // what they turn on
};

// v3: paired through the relay. v2 (paired over HTTP by an older app, which also kept the
// dashboard's address and a token) still checks in the same way, so it's read too.
const KEY = 'nexguard.session.v3';
const OLD_KEY = 'nexguard.session.v2';

function parse(raw: string | null): Session | null {
  if (!raw) return null;
  const value = JSON.parse(raw) as Partial<Session>;
  const relay = value.relay;
  if (
    !value.device ||
    typeof relay?.url !== 'string' ||
    typeof relay.token !== 'string' ||
    typeof relay.inbox !== 'string' ||
    typeof relay.key !== 'string'
  ) {
    return null;
  }
  return {
    device: value.device,
    checkinSeconds: typeof value.checkinSeconds === 'number' ? value.checkinSeconds : 900,
    relay: {
      url: relay.url,
      token: relay.token,
      inbox: relay.inbox,
      key: relay.key,
      ...(typeof relay.config === 'string' ? { config: relay.config } : {}),
      ...(typeof relay.command === 'string' ? { command: relay.command } : {}),
    },
    name:
      typeof value.name === 'string' && value.name
        ? value.name
        : (value.device.person ?? value.device.account),
    disclosure: typeof value.disclosure === 'string' ? value.disclosure : '',
    permissionsAsked: value.permissionsAsked !== false, // older pairings asked when pairing
    controlled: value.controlled === true,
    settingsDigest: typeof value.settingsDigest === 'string' ? value.settingsDigest : '',
    controls: value.controls && typeof value.controls === 'object' ? value.controls : null,
  };
}

export async function loadSession(): Promise<Session | null> {
  try {
    return (
      parse(await SecureStore.getItemAsync(KEY)) ?? parse(await SecureStore.getItemAsync(OLD_KEY))
    );
  } catch {
    return null;
  }
}

export async function saveSession(session: Session): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(session), {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK, // readable by a background check-in
  });
  await SecureStore.deleteItemAsync(OLD_KEY).catch(() => undefined);
}

export async function clearSession(): Promise<void> {
  await SecureStore.deleteItemAsync(KEY);
  await SecureStore.deleteItemAsync(OLD_KEY).catch(() => undefined);
}
