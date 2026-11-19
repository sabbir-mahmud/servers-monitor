// One check-in, and what runs them: a timer while the app is open, and a background task
// where the system allows one. Expo Go runs no background tasks (Android or iOS), so there
// the phone reports while NexGuard is open and each time it's opened again. A development
// or store build adds the background check-in, which the system runs at most every 15
// minutes, and less often when it decides to save battery.
import * as BackgroundTask from 'expo-background-task';
import * as TaskManager from 'expo-task-manager';

import { ApiError, checkin } from './api';
import { collectReport, type Report } from './report';
import { clearSession, loadSession, saveSession } from './session';

export const BACKGROUND_TASK = 'nexguard-checkin';

export type CheckinResult =
  | { ok: true; at: Date; report: Report }
  | { ok: false; at: Date; message: string; unpaired: boolean };

let inFlight: Promise<CheckinResult | null> | null = null;

/** Checks in once with the stored pairing; null when this phone isn't paired. A 401 means it
 * was removed on the dashboard or paired again elsewhere: the pairing is forgotten. */
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
    const answer = await checkin(session.server, session.token, report);
    const changed =
      answer.checkin_seconds !== session.checkinSeconds ||
      JSON.stringify(answer.device) !== JSON.stringify(session.device);
    if (changed) {
      await saveSession({ ...session, device: answer.device, checkinSeconds: answer.checkin_seconds });
    }
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
