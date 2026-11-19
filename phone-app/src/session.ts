// The pairing this phone holds. The token lives in the Keychain / Keystore only.
import * as SecureStore from 'expo-secure-store';

import type { DeviceInfo } from './api';

export type Session = {
  server: string;
  token: string;
  device: DeviceInfo;
  checkinSeconds: number;
};

const KEY = 'nexguard.session.v1';

export async function loadSession(): Promise<Session | null> {
  try {
    const raw = await SecureStore.getItemAsync(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Session>;
    if (typeof value.server !== 'string' || typeof value.token !== 'string' || !value.device) {
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
