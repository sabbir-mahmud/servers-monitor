// Connect this phone: type the 6-digit code the dashboard shows. It goes through the Redis
// relay (relay.ts): the phone never contacts the dashboard. First a preview (who will see
// it, what can be turned on; the code stays valid), then the permissions (SetupScreen),
// and only then does it pair.
import { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { guard } from '../modules/nexguard-guard';
import { ApiError } from './api';
import { startBackgroundCheckins } from './checkin';
import { relayProblem } from './config';
import { pairWithCode, previewCode, type Preview } from './relay';
import { SetupScreen } from './SetupScreen';
import { deviceInfo } from './report';
import { saveSession, type Session } from './session';
import { space, type, useColors } from './theme';
import { Button, ErrorText, Field, Logo, Muted } from './ui';

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error ? error.message : 'Something went wrong.';
}

export function PairScreen({ onPaired }: { onPaired: (session: Session) => void }) {
  const c = useColors();
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(relayProblem());
  const [preview, setPreview] = useState<Preview | null>(null);
  const digits = code.replace(/\D/g, '');

  async function connect() {
    if (digits.length !== 6) {
      setError('The code has 6 digits, like 482 913.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      setPreview(await previewCode(digits, deviceInfo()));
    } catch (e) {
      setError(message(e));
    }
    setBusy(false);
  }

  async function pair(shown: Preview) {
    setBusy(true);
    setError('');
    try {
      const paired = await pairWithCode(digits, deviceInfo());
      const session: Session = {
        device: paired.device,
        checkinSeconds: paired.checkinSeconds,
        relay: paired.login,
        name: paired.name,
        disclosure: paired.disclosure,
        permissionsAsked: true, // asked before pairing
        controlled: shown.controls && !!guard,
        settingsDigest: '',
        controls: null,
      };
      await saveSession(session);
      guard?.start(JSON.stringify(paired.login));
      await startBackgroundCheckins();
      onPaired(session);
    } catch (e) {
      setError(message(e));
      setBusy(false);
    }
  }

  if (preview) {
    return (
      <SetupScreen
        preview={preview}
        busy={busy}
        error={error}
        onPair={() => void pair(preview)}
        onBack={() => {
          setPreview(null);
          setError('');
        }}
      />
    );
  }

  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
      <KeyboardAvoidingView style={styles.fill} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
          <Logo />
          <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
            Connect this phone
          </Text>
          <Muted>
            On the dashboard, add this phone. It shows a 6-digit code: enter it here. Next you
            see who will see this phone and what they can turn on. Nothing is shared until you
            connect.
          </Muted>
          <Field
            label="Code"
            value={code}
            onChangeText={(t) => setCode(t.replace(/[^\d ]/g, '').slice(0, 7))}
            placeholder="482 913"
            keyboardType="number-pad"
            textContentType="oneTimeCode"
            autoComplete="one-time-code"
            maxLength={7}
            returnKeyType="go"
            onSubmitEditing={() => void connect()}
            style={styles.code}
            editable={!busy}
          />
          {error ? <ErrorText>{error}</ErrorText> : null}
          <Button
            label="Connect"
            busy={busy}
            disabled={digits.length !== 6 || !!relayProblem()}
            onPress={() => void connect()}
          />
          {busy ? <Muted center>Asking the dashboard through the relay…</Muted> : null}
          <Muted center>Codes work once, for 15 minutes. Expired? Make a new one on the dashboard.</Muted>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { padding: space.xl, gap: space.lg, paddingBottom: space.xxl },
  title: { fontSize: type.title, fontWeight: '700', letterSpacing: -0.5 },
  code: { fontSize: 28, letterSpacing: 6, textAlign: 'center', fontVariant: ['tabular-nums'] },
});
