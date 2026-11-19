// The pairing this phone holds. The token lives in the Keychain / Keystore only.
import * as SecureStore from 'expo-secure-store';

import type { DeviceInfo } from './api';
import type { RelayLogin } from './relay';

export type Session = {
  server: string;
  token: string;
  device: DeviceInfo;
  checkinSeconds: number;
  relay: RelayLogin;
};

// v2: check-ins through the relay. A v1 pairing (HTTP check-ins) has to pair again.
const KEY = 'nexguard.session.v2';

export async function loadSession(): Promise<Session | null> {
  try {
    const raw = await SecureStore.getItemAsync(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Session>;
    const relay = value.relay;
    if (
      typeof value.server !== 'string' ||
      typeof value.token !== 'string' ||
      !value.device ||
      typeof relay?.url !== 'string' ||
      typeof relay.token !== 'string' ||
      typeof relay.inbox !== 'string' ||
      typeof relay.key !== 'string'
    ) {
      return null;
    }
    return { checkinSeconds: 900, ...value } as Session;
  } catch {
    return null;
  }
}

export async function saveSession(session: Session): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(session), {
    keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK, // readable by a background check-in
  });
}

export async function clearSession(): Promise<void> {
  await SecureStore.deleteItemAsync(KEY);
}
