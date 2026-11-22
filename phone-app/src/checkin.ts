// One check-in, and what runs them: a timer while the app is open, and a background task
// where the system allows one. Expo Go runs no background tasks (Android or iOS), so there
// the phone reports while NexGuard is open and each time it's opened again. A development
// or store build adds the background check-in, which the system runs at most every 15
// minutes, and less often when it decides to save battery.
import * as BackgroundTask from 'expo-background-task';
import * as TaskManager from 'expo-task-manager';

import { guard } from '../modules/nexguard-guard';
import { ApiError } from './api';
import { pushCheckin, readSettings, sealCheckin, sealSos } from './relay';
import { collectReport, location, type Report } from './report';
import { clearSession, loadSession, saveSession, type Session } from './session';

export const BACKGROUND_TASK = 'nexguard-checkin';

export type CheckinResult =
  | { ok: true; at: Date; report: Report }
  | { ok: false; at: Date; message: string; unpaired: boolean };

let inFlight: Promise<CheckinResult | null> | null = null;

/** Checks in once, through the relay, with the stored pairing; null when this phone isn't
 * paired. A 401 means it was removed on the dashboard or paired again elsewhere: the pairing
 * is forgotten. The relay doesn't answer back: success means the message is queued, and
 * the dashboard applies it when its relay process picks it up (within a minute). */
export function checkinNow(): Promise<CheckinResult | null> {
  inFlight ??= run().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

async function run(): Promise<CheckinResult | null> {
  const session = await loadSession();
  if (!session) return null;
  if (guard?.wasUnpaired()) {
    // The filter service heard first that it was removed on the dashboard.
    await forget();
    return { ok: false, at: new Date(), message: 'This phone was removed on the dashboard.', unpaired: true };
  }
  const report = await collectReport();
  // Android controls: what's new since the last check-in that got through, and what the
  // service noticed was turned off but couldn't tell itself.
  const c = session.controls ?? {};
  const raw = guard?.takeActivity(c.screen_time === true, c.web_activity === true) ?? null;
  const activity = raw ? (JSON.parse(raw) as { batch: string }) : null;
  const tamper = guard?.takeTamper() ?? [];
  const full: Report = {
    ...report,
    ...(activity ? { activity } : {}),
    ...(tamper.length ? { tamper } : {}),
    ...(guard ? { config: session.settingsDigest } : {}),
  };
  try {
    await pushCheckin(session.relay, sealCheckin(session.relay, full));
    if (activity) guard?.commitActivity(activity.batch);
    await followSettings(session);
    return { ok: true, at: new Date(), report: full };
  } catch (error) {
    const unpaired = error instanceof ApiError && error.status === 401;
    if (unpaired) await forget();
    const message = error instanceof Error ? error.message : 'The check-in failed.';
    return { ok: false, at: new Date(), message, unpaired };
  }
}

/** Removed on the dashboard (or paired again elsewhere): forget the pairing, stop the
 * filter and release removal protection, so NexGuard can be uninstalled. */
async function forget(): Promise<void> {
  guard?.stop();
  await clearSession();
  await stopBackgroundCheckins();
}

/** After a check-in: the dashboard may have left new settings. Hand them to the filter
 * service and remember which ones this phone has. */
async function followSettings(session: Session): Promise<void> {
  if (!guard) return;
  const settings = await readSettings(session.relay).catch((error: unknown) => {
    if (error instanceof ApiError && error.status === 401) throw error;
    return null;
  });
  if (!settings || settings.digest === session.settingsDigest) return;
  guard.update(JSON.stringify(settings.controls));
  await saveSession({
    ...session,
    settingsDigest: settings.digest,
    controls: settings.controls,
    checkinSeconds: settings.checkinSeconds,
  });
}

/** The SOS button: tells everyone on the account at once, with where the phone is. With
 * `cancel`, "I'm OK": closes the alert. Throws with a message to show when it can't send. */
export async function sendSos(cancel = false): Promise<void> {
  const session = await loadSession();
  if (!session) throw new ApiError('This phone isn’t paired.', 401);
  const where = cancel ? undefined : await location().catch(() => undefined);
  await pushCheckin(session.relay, sealSos(session.relay, where, cancel));
}

/** Unpairing on the phone itself: tell the dashboard first (best effort), so the people
 * who see this phone hear about it instead of finding it "not reporting" hours later. */
export async function sayGoodbye(): Promise<void> {
  const session = await loadSession();
  if (!session) return;
  await pushCheckin(session.relay, sealCheckin(session.relay, { bye: 'unpaired' })).catch(
    () => undefined,
  );
}

// Must be defined when the bundle loads (index.ts imports this file), not in a component.
TaskManager.defineTask(BACKGROUND_TASK, async () => {
  const result = await checkinNow().catch(() => null);
  return result?.ok ? BackgroundTask.BackgroundTaskResult.Success : BackgroundTask.BackgroundTaskResult.Failed;
});

/** Whether this build can check in from the background (false in Expo Go). */
export async function backgroundAvailable(): Promise<boolean> {
  try {
    if (!(await TaskManager.isAvailableAsync())) return false;
    return (await BackgroundTask.getStatusAsync()) === BackgroundTask.BackgroundTaskStatus.Available;
  } catch {
    return false;
  }
}

export async function startBackgroundCheckins(): Promise<boolean> {
  if (!(await backgroundAvailable())) return false;
  try {
    if (!(await TaskManager.isTaskRegisteredAsync(BACKGROUND_TASK))) {
      await BackgroundTask.registerTaskAsync(BACKGROUND_TASK, { minimumInterval: 15 });
    }
    return true;
  } catch {
    return false;
  }
}

export async function stopBackgroundCheckins(): Promise<void> {
  try {
    if (await TaskManager.isTaskRegisteredAsync(BACKGROUND_TASK)) {
      await BackgroundTask.unregisterTaskAsync(BACKGROUND_TASK);
    }
  } catch {
    // Expo Go, or never registered
  }
}
