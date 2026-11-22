// Before pairing: who will see this phone (the dashboard's preview, which doesn't use the
// code), then each permission NexGuard needs, one at a time, saying why. On a phone that
// can have controls (Android, a child's or staff member's), that's screen time, the
// website filter, removal protection and staying awake; otherwise just location and
// notifications. Saying no to one still pairs: the dashboard shows what's missing.
import * as Location from 'expo-location';
import { useCallback, useEffect, useState } from 'react';
import { AppState, PermissionsAndroid, Platform, ScrollView, StyleSheet, Text } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { guard, type GuardPermission } from '../modules/nexguard-guard';
import type { Preview } from './relay';
import { space, type, useColors } from './theme';
import { Button, Card, ErrorText, Muted } from './ui';

type Step = {
  id: string;
  title: string;
  why: string;
  allow: string; // the button
  granted: () => Promise<boolean>;
  request: () => Promise<void>;
  optional?: boolean; // a tip, not a permission
};

async function locationGranted(background: boolean): Promise<boolean> {
  const fg = await Location.getForegroundPermissionsAsync();
  if (!fg.granted || !background) return fg.granted;
  return (await Location.getBackgroundPermissionsAsync().catch(() => null))?.granted === true;
}

function open(name: GuardPermission): Promise<void> {
  guard?.open(name);
  return Promise.resolve();
}

function steps(preview: Preview, controls: boolean): Step[] {
  const who = preview.device.account;
  const list: Step[] = [
    {
      id: 'location',
      title: 'Location',
      why: controls
        ? `${who} sees where this phone is. Choose “Allow all the time” on the next screen, so it works when NexGuard isn’t open.`
        : `${who} sees where this phone is, while NexGuard checks in.`,
      allow: 'Allow location',
      granted: () => locationGranted(controls),
      request: async () => {
        const fg = await Location.requestForegroundPermissionsAsync();
        if (fg.granted && controls) await Location.requestBackgroundPermissionsAsync().catch(() => null);
      },
    },
  ];
  if (Platform.OS === 'android' && guard) {
    list.push({
      id: 'notifications',
      title: 'Notifications',
      why: `NexGuard shows a notification while it’s on, and rings with one when ${who} beeps this phone to find it.`,
      allow: 'Allow notifications',
      granted: () => Promise.resolve(guard!.status().notifications),
      request: async () => {
        if (Number(Platform.Version) >= 33) {
          const result = await PermissionsAndroid.request('android.permission.POST_NOTIFICATIONS');
          if (result === PermissionsAndroid.RESULTS.NEVER_ASK_AGAIN) await open('notifications');
        }
      },
    });
  }
  if (controls && guard) {
    list.push(
      {
        id: 'usage',
        title: 'Screen time',
        why: `Which apps are used, and for how long, if ${who} turns screen time on. Find NexGuard in the list and switch it on, then come back.`,
        allow: 'Open usage access',
        granted: () => Promise.resolve(guard!.status().usage),
        request: () => open('usage'),
      },
      {
        id: 'vpn',
        title: 'Website filter',
        why: `Blocks the sites ${who} chooses, and records the sites browsers open (names only, never pages or searches). It runs on this phone as a VPN that sees only website lookups; your other traffic doesn’t go through it.`,
        allow: 'Allow the filter',
        granted: () => Promise.resolve(guard!.status().vpn),
        request: () => open('vpn'),
      },
      {
        id: 'admin',
        title: 'Removal protection',
        why: `NexGuard can’t be uninstalled while this is on. It can be turned off in Settings, but ${who} is told straight away. It gives NexGuard no other power over the phone.`,
        allow: 'Turn on protection',
        granted: () => Promise.resolve(guard!.status().admin),
        request: () => open('admin'),
      },
    );
  }
  if (guard) {
    list.push({
      id: 'battery',
      title: 'Stay on',
      why: 'So Android doesn’t put NexGuard to sleep to save battery, which would stop it checking in and ringing. It uses very little.',
      allow: 'Allow',
      granted: () => Promise.resolve(guard!.status().battery),
      request: () => open('battery'),
    });
  }
  if (controls && guard) {
    list.push({
      id: 'always_on',
      title: 'Keep the filter on',
      why: 'Optional: in VPN settings, tap the gear next to NexGuard and turn on “Always-on VPN”, so the filter comes back by itself after a restart.',
      allow: 'Open VPN settings',
      granted: () => Promise.resolve(false),
      request: () => open('always_on'),
      optional: true,
    });
  }
  return list;
}

export function SetupScreen({
  preview,
  busy,
  error,
  onPair,
  onBack,
}: {
  preview: Preview;
  busy: boolean;
  error: string;
  onPair: () => void;
  onBack: () => void;
}) {
  const c = useColors();
  const controls = preview.controls && !!guard;
  const [list] = useState(() => steps(preview, controls));
  const [index, setIndex] = useState(-1); // -1: what's shared; list.length: ready to connect
  const [granted, setGranted] = useState(false);
  const step = index >= 0 && index < list.length ? list[index] : null;

  const recheck = useCallback(async () => {
    if (!step) return;
    const ok = await step.granted().catch(() => false);
    setGranted(ok);
  }, [step]);

  // Most permissions are granted on another screen: look again when the person is back.
  useEffect(() => {
    const first = setTimeout(() => void recheck(), 0);
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'active') void recheck();
    });
    return () => {
      clearTimeout(first);
      sub.remove();
    };
  }, [recheck]);

  const d = preview.device;
  if (index === -1) {
    return (
      <Shell>
        <Text style={[styles.brand, { color: c.accent }]}>Before connecting</Text>
        <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
          {d.account} will see this phone
        </Text>
        <Card>
          <Text style={[styles.lead, { color: c.ink }]}>{preview.disclosure}</Text>
          <Muted>
            This phone is “{d.name}” in {d.account}
            {d.person ? `, for ${d.person}` : ''}.
          </Muted>
        </Card>
        <Muted>
          Next, NexGuard asks for {list.length === 1 ? 'one permission' : `${list.length} permissions`}, each
          with the reason. Nothing is shared until you connect at the end.
        </Muted>
        <Button label="Continue" onPress={() => setIndex(0)} />
        <Button label="Not this phone" kind="secondary" onPress={onBack} />
      </Shell>
    );
  }

  if (!step) {
    return (
      <Shell>
        <Text style={[styles.brand, { color: c.accent }]}>Ready</Text>
        <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
          Connect to {d.account}
        </Text>
        <Muted>
          {controls
            ? `From now on ${d.account} sees this phone, and only they can remove NexGuard, from their dashboard.`
            : `From now on ${d.account} sees this phone. You can stop sharing in this app at any time.`}
        </Muted>
        {error ? <ErrorText>{error}</ErrorText> : null}
        <Button label="Connect" busy={busy} onPress={onPair} />
        {busy ? <Muted center>Connecting through the relay…</Muted> : null}
        <Button label="Back" kind="secondary" disabled={busy} onPress={() => setIndex(list.length - 1)} />
      </Shell>
    );
  }

  const next = () => {
    setGranted(false);
    setIndex(index + 1);
  };
  return (
    <Shell>
      <Text style={[styles.brand, { color: c.accent }]}>
        Step {index + 1} of {list.length}
      </Text>
      <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
        {step.title}
      </Text>
      <Card>
        <Text style={[styles.lead, { color: c.ink }]}>{step.why}</Text>
      </Card>
      {granted ? (
        <>
          <Muted>Allowed.</Muted>
          <Button label="Next" onPress={next} />
        </>
      ) : (
        <>
          <Button
            label={step.allow}
            onPress={() => void step.request().then(recheck)}
          />
          <Button label={step.optional ? 'Skip' : 'Not now'} kind="secondary" onPress={next} />
        </>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const c = useColors();
  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
      <ScrollView contentContainerStyle={styles.content}>{children}</ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { padding: space.xl, gap: space.lg, paddingBottom: space.xxl },
  brand: { fontSize: type.small, fontWeight: '700', letterSpacing: 0.5, textTransform: 'uppercase', marginTop: space.lg },
  title: { fontSize: type.title, fontWeight: '700', letterSpacing: -0.5 },
  lead: { fontSize: type.body, lineHeight: 22 },
});
