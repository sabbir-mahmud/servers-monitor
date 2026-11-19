// Paired: what this phone reports, when it last did, and a way to stop.
import * as Location from 'expo-location';
import { useEffect, useState } from 'react';
import { Alert, AppState, Linking, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { backgroundAvailable, checkinNow, stopBackgroundCheckins, type CheckinResult } from './checkin';
import type { Report } from './report';
import { clearSession, type Session } from './session';
import { space, type, useColors } from './theme';
import { Button, Card, ErrorText, Muted, Row } from './ui';

function bytes(n: number): string {
  const gb = n / 1e9;
  return gb >= 100 ? `${Math.round(gb)} GB` : `${gb.toFixed(1)} GB`;
}

function time(d: Date): string {
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function readings(r: Report | undefined, locationOn: boolean) {
  const network = { wifi: 'Wi-Fi', cellular: 'Mobile data', ethernet: 'Wired', none: 'Offline' };
  return [
    {
      label: 'Battery',
      value: r?.battery ? `${r.battery.percent}%${r.battery.charging ? ', charging' : ''}` : '—',
    },
    {
      label: 'Storage free',
      value: r?.storage ? `${bytes(r.storage.free)} of ${bytes(r.storage.total)}` : '—',
    },
    { label: 'Network', value: r?.network ? network[r.network.type] : '—' },
    {
      label: 'Location',
      value: !locationOn
        ? 'Off'
        : r?.location
          ? `Shared (±${r.location.accuracy ?? '?'} m)`
          : 'No fix yet',
    },
  ];
}

export function StatusScreen({ session, onUnpaired }: { session: Session; onUnpaired: () => void }) {
  const c = useColors();
  const [result, setResult] = useState<CheckinResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [locationOn, setLocationOn] = useState(true);
  const [background, setBackground] = useState<boolean | null>(null);

  // State changes only after the awaits: the effect below calls this too.
  async function report() {
    const r = await checkinNow();
    const perm = await Location.getForegroundPermissionsAsync().catch(() => null);
    setLocationOn(!!perm?.granted);
    if (r === null || (!r.ok && r.unpaired)) {
      if (r && !r.ok) Alert.alert('Not paired any more', r.message);
      onUnpaired();
      return;
    }
    setResult(r);
  }

  async function check() {
    setBusy(true);
    await report();
    setBusy(false);
  }

  // Check in now, then on the dashboard's interval while open, and each time the app comes
  // back to the front (in Expo Go that's the only time it can).
  useEffect(() => {
    void backgroundAvailable().then(setBackground);
    const first = setTimeout(() => void report(), 0);
    const timer = setInterval(() => void report(), Math.max(60, session.checkinSeconds) * 1000);
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'active') void report();
    });
    return () => {
      clearTimeout(first);
      clearInterval(timer);
      sub.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per pairing
  }, [session.token, session.checkinSeconds]);

  async function allowLocation() {
    const answer = await Location.requestForegroundPermissionsAsync();
    if (answer.granted) return void report();
    if (!answer.canAskAgain) {
      Alert.alert('Location is off', 'Turn it on for NexGuard in Settings.', [
        { text: 'Not now', style: 'cancel' },
        { text: 'Open Settings', onPress: () => void Linking.openSettings() },
      ]);
    }
  }

  function unpair() {
    Alert.alert(
      'Stop sharing?',
      `This phone stops reporting to ${session.device.account}. The dashboard keeps showing it as offline until it's removed there.`,
      [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Unpair',
          style: 'destructive',
          onPress: async () => {
            await stopBackgroundCheckins();
            await clearSession();
            onUnpaired();
          },
        },
      ],
    );
  }

  const d = session.device;
  const status = !result
    ? 'Checking in…'
    : result.ok
      ? `Reported at ${time(result.at)}`
      : `Couldn't report at ${time(result.at)}`;
  const minutes = Math.round(session.checkinSeconds / 60);

  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={[styles.brand, { color: c.accent }]}>NexGuard</Text>
        <View style={styles.head}>
          <View
            style={[styles.dot, { backgroundColor: result && !result.ok ? c.bad : c.good }]}
            accessibilityElementsHidden
          />
          <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header" numberOfLines={2}>
            {d.name}
          </Text>
        </View>
        <Muted>
          Connected to {d.account}
          {d.person ? `, for ${d.person}` : ''}. {status}.
        </Muted>
        {result && !result.ok ? <ErrorText>{result.message}</ErrorText> : null}

        <Card>
          {readings(result?.ok ? result.report : undefined, locationOn).map((row, i, all) => (
            <Row key={row.label} label={row.label} value={row.value} last={i === all.length - 1} />
          ))}
        </Card>
        {!locationOn ? (
          <Button label="Share location" kind="secondary" onPress={() => void allowLocation()} />
        ) : null}

        <Muted>
          {background
            ? `Reports about every ${minutes} minutes while open, and in the background when the system allows (at most every 15 minutes).`
            : `Reports about every ${minutes} minutes while NexGuard is open, and each time you open it. ` +
              'Expo Go can’t run apps in the background; an installed build of NexGuard can.'}
        </Muted>

        <Button label="Report now" busy={busy} onPress={() => void check()} />
        <Button label="Unpair this phone" kind="danger" disabled={busy} onPress={unpair} />
        <Muted center>{session.server}</Muted>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { padding: space.xl, gap: space.lg, paddingBottom: space.xxl },
  brand: { fontSize: type.small, fontWeight: '700', letterSpacing: 0.5, textTransform: 'uppercase', marginTop: space.lg },
  head: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  dot: { width: 12, height: 12, borderRadius: 6 },
  title: { fontSize: type.title, fontWeight: '700', letterSpacing: -0.5, flexShrink: 1 },
});
