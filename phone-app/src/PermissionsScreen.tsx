// Shown once, right after the phone connects: what the account will see, then the system
// permission prompts. A "no" still leaves the phone connected; location can be allowed
// later from the status screen.
import * as Location from 'expo-location';
import { useState } from 'react';
import { ScrollView, StyleSheet, Text } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { startBackgroundCheckins } from './checkin';
import { saveSession, type Session } from './session';
import { space, type, useColors } from './theme';
import { Button, Card, Muted } from './ui';

export function PermissionsScreen({
  session,
  onDone,
}: {
  session: Session;
  onDone: (session: Session) => void;
}) {
  const c = useColors();
  const [busy, setBusy] = useState(false);

  async function finish(askLocation: boolean) {
    setBusy(true);
    if (askLocation) {
      await Location.requestForegroundPermissionsAsync().catch(() => null);
    }
    await startBackgroundCheckins();
    const next = { ...session, permissionsAsked: true };
    await saveSession(next).catch(() => undefined);
    onDone(next);
  }

  const d = session.device;
  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
      <ScrollView contentContainerStyle={styles.content}>
        <Text style={[styles.brand, { color: c.accent }]}>Connected</Text>
        <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
          Connected as {session.name}
        </Text>
        <Card>
          <Text style={[styles.lead, { color: c.ink }]}>
            {session.disclosure ||
              `${d.account} will see this phone's last online time, battery, storage, network and location.`}
          </Text>
          <Muted>
            This phone is “{d.name}” in {d.account}
            {d.person ? `, for ${d.person}` : ''}.
          </Muted>
        </Card>
        <Muted>
          Next, the phone asks once for location access, so {d.account} can see where it is.
          Without it, everything else is still shared. You can stop at any time by unpairing in
          this app.
        </Muted>
        <Button label="Allow location" busy={busy} onPress={() => void finish(true)} />
        <Button label="Not now" kind="secondary" disabled={busy} onPress={() => void finish(false)} />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { padding: space.xl, gap: space.lg, paddingBottom: space.xxl },
  brand: { fontSize: type.small, fontWeight: '700', letterSpacing: 0.5, textTransform: 'uppercase', marginTop: space.lg },
  title: { fontSize: type.title, fontWeight: '700', letterSpacing: -0.5 },
  lead: { fontSize: type.body, lineHeight: 23 },
});
