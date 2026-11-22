// What a check-in reports: battery, storage, network, and location when it's allowed.
// Each reading is best effort: one that fails is left out, never the whole check-in.
import * as Application from 'expo-application';
import * as Battery from 'expo-battery';
import * as Device from 'expo-device';
import { Paths } from 'expo-file-system';
import * as Location from 'expo-location';
import * as Network from 'expo-network';
import { Platform } from 'react-native';

import { guard } from '../modules/nexguard-guard';

// Expo Go reports its own version, and has none of the Android controls: say so, so the
// dashboard doesn't offer them (it only accepts a plain "1.2.0").
export const APP_VERSION = guard
  ? `nexguard-phone ${Application.nativeApplicationVersion ?? '1.0.0'}`
  : `nexguard-phone ${Application.nativeApplicationVersion ?? '1.0.0'}-nocontrols`;

/** Who this phone is, sent when pairing and with each check-in. */
export function deviceInfo() {
  const os = Platform.OS === 'ios' ? (Device.osName ?? 'iOS') : 'Android';
  return {
    platform: Platform.OS === 'ios' ? 'ios' : 'android',
    model: [Device.manufacturer, Device.modelName].filter(Boolean).join(' ') || 'Phone',
    os_version: `${os} ${Device.osVersion ?? ''}`.trim(),
    app_version: APP_VERSION,
  };
}

export type Report = ReturnType<typeof deviceInfo> & {
  battery?: { percent: number; charging: boolean };
  storage?: { free: number; total: number };
  network?: { type: 'wifi' | 'cellular' | 'ethernet' | 'none' };
  location?: { lat: number; lon: number; accuracy?: number; at: string; source: 'gps' };
  // Whether location is allowed: turning it off is reported, so it isn't silently lost.
  permissions?: { location: boolean; [name: string]: boolean };
  // Android controls (modules/nexguard-guard): new screen time and websites, protections
  // the filter service saw turned off, and which dashboard settings this phone has.
  activity?: object;
  tamper?: string[];
  config?: string;
};

async function battery(): Promise<Report['battery']> {
  const level = await Battery.getBatteryLevelAsync(); // -1 where unknown (simulators)
  if (level < 0) return undefined;
  const state = await Battery.getBatteryStateAsync();
  return {
    percent: Math.round(level * 100),
    charging: state === Battery.BatteryState.CHARGING || state === Battery.BatteryState.FULL,
  };
}

function storage(): Report['storage'] {
  const total = Paths.totalDiskSpace;
  const free = Paths.availableDiskSpace;
  return total > 0 && free >= 0 && free <= total ? { free, total } : undefined;
}

async function network(): Promise<Report['network']> {
  const state = await Network.getNetworkStateAsync();
  if (!state.isConnected) return { type: 'none' };
  switch (state.type) {
    case Network.NetworkStateType.WIFI:
      return { type: 'wifi' };
    case Network.NetworkStateType.CELLULAR:
      return { type: 'cellular' };
    case Network.NetworkStateType.ETHERNET:
      return { type: 'ethernet' };
    default:
      return undefined;
  }
}

/** Only with permission already given: a check-in never asks (the pairing screen does). */
export async function location(): Promise<Report['location']> {
  const { granted } = await Location.getForegroundPermissionsAsync();
  if (!granted) return undefined;
  const fix =
    (await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced }).catch(
      () => null,
    )) ?? (await Location.getLastKnownPositionAsync({ maxAge: 30 * 60_000 }));
  if (!fix) return undefined;
  return {
    lat: fix.coords.latitude,
    lon: fix.coords.longitude,
    accuracy: fix.coords.accuracy != null ? Math.round(fix.coords.accuracy) : undefined,
    at: new Date(fix.timestamp).toISOString(),
    source: 'gps',
  };
}

/** What the dashboard can show as missing on an Android phone with NexGuard 1.2. */
function guardPermissions(): Record<string, boolean> {
  if (!guard) return {};
  const s = guard.status();
  return {
    usage_access: s.usage,
    filter: s.running && s.vpn,
    notifications: s.notifications,
    removal_protection: s.admin,
    private_dns: s.privateDns,
  };
}

const settle = <T,>(work: () => Promise<T> | T): Promise<T | undefined> =>
  Promise.resolve()
    .then(work)
    .catch(() => undefined);

export async function collectReport(): Promise<Report> {
  const [b, s, n, l, perm] = await Promise.all([
    settle(battery),
    settle(storage),
    settle(network),
    settle(location),
    settle(() => Location.getForegroundPermissionsAsync()),
  ]);
  const permissions = perm ? { location: perm.granted, ...guardPermissions() } : undefined;
  return { ...deviceInfo(), battery: b, storage: s, network: n, location: l, permissions };
}
