// The Android side of NexGuard's controls (android/…/GuardModule.kt): screen time, the
// website filter, beep and removal protection. Not there in Expo Go or on an iPhone:
// `guard` is then null and the app does what it always did (location, battery, SOS).
import { requireOptionalNativeModule } from 'expo';

export type GuardStatus = {
  usage: boolean; // Usage access: screen time
  vpn: boolean; // the website filter may run (VPN consent given)
  notifications: boolean;
  admin: boolean; // removal protection
  battery: boolean; // not put to sleep by battery optimisation
  privateDns: boolean; // a Private DNS hostname is set: it gets around the filter
  running: boolean;
  websites: boolean; // Android 10+: lookups can be tied to the browser that made them
  sdk: number;
};

export type GuardPermission =
  | 'usage'
  | 'vpn'
  | 'admin'
  | 'battery'
  | 'notifications'
  | 'always_on'
  | 'network';

type Native = {
  status(): GuardStatus;
  open(name: GuardPermission): boolean;
  start(login: string): void;
  update(controls: string): void;
  stop(): void;
  wasUnpaired(): boolean;
  takeActivity(screenTime: boolean, webActivity: boolean): string | null;
  commitActivity(batch: string): void;
  takeTamper(): string[];
  stopBeep(): void;
};

export const guard = requireOptionalNativeModule<Native>('NexguardGuard');
