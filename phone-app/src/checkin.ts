// One check-in, and what runs them: a timer while the app is open, and a background task
// where the system allows one. Expo Go runs no background tasks (Android or iOS), so there
// the phone reports while NexGuard is open and each time it's opened again. A development
// or store build adds the background check-in, which the system runs at most every 15
// minutes, and less often when it decides to save battery.
import * as BackgroundTask from 'expo-background-task';
import * as TaskManager from 'expo-task-manager';

import { ApiError } from './api';
import { pushCheckin, sealCheckin, sealSos } from './relay';
import { collectReport, location, type Report } from './report';
import { clearSession, loadSession } from './session';

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
  const report = await collectReport();
  try {
    await pushCheckin(session.relay, sealCheckin(session.relay, report));
    return { ok: true, at: new Date(), report };
  } catch (error) {
    const unpaired = error instanceof ApiError && error.status === 401;
    if (unpaired) {
      await clearSession();
      await stopBackgroundCheckins();
    }
    const message = error instanceof Error ? error.message : 'The check-in failed.';
    return { ok: false, at: new Date(), message, unpaired };
  }
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
