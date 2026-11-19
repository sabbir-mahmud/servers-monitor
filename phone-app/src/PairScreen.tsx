// Pair this phone: scan the dashboard's QR code (or type the address and code), read what
// the account will see, and only then pair. Nothing is sent until "Allow".
import { CameraView, useCameraPermissions } from 'expo-camera';
import * as Location from 'expo-location';
import { useRef, useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  type TextInputInstance,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiError, pair, previewPairing, type PairPreview } from './api';
import { startBackgroundCheckins } from './checkin';
import { isCode, isLoopback, normalizeCode, normalizeServer, parsePairLink, type PairTarget } from './pairLink';
import { deviceInfo } from './report';
import { saveSession, type Session } from './session';
import { space, type, useColors } from './theme';
import { Button, Card, ErrorText, Field, Muted } from './ui';

type Step =
  | { name: 'start' }
  | { name: 'scan' }
  | { name: 'review'; target: PairTarget; preview: PairPreview };

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error ? error.message : 'Something went wrong.';
}

export function PairScreen({ onPaired }: { onPaired: (session: Session) => void }) {
  const c = useColors();
  const [step, setStep] = useState<Step>({ name: 'start' });
  const [server, setServer] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [permission, requestPermission] = useCameraPermissions();
  const codeInput = useRef<TextInputInstance>(null);
  const scanned = useRef(false);

  async function review(target: PairTarget) {
    setError('');
    if (isLoopback(target.server)) {
      setStep({ name: 'start' });
      setError(
        `${target.server} is the dashboard computer's own address, which a phone can't reach. ` +
          "Open the dashboard by its network address (or set DASHBOARD_URL) and make a new code.",
      );
      return;
    }
    setBusy(true);
    try {
      const preview = await previewPairing(target.server, target.code, deviceInfo());
      setStep({ name: 'review', target, preview });
    } catch (e) {
      setStep({ name: 'start' });
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  async function startScan() {
    setError('');
    if (!permission?.granted) {
      const answer = await requestPermission();
      if (!answer.granted) {
        setError('Camera access is off, so type the address and code instead.');
        return;
      }
    }
    scanned.current = false;
    setStep({ name: 'scan' });
  }

  function onScanned(data: string) {
    if (scanned.current) return; // the camera reports the same code many times a second
    const target = parsePairLink(data);
    if (!target) {
      setError("That QR code isn't a NexGuard pairing code. Scan the one on the dashboard's pair page.");
      return;
    }
    scanned.current = true;
    setStep({ name: 'start' });
    setServer(target.server);
    setCode(`${target.code.slice(0, 4)}-${target.code.slice(4)}`);
    void review(target);
  }

  function submitTyped() {
    const s = normalizeServer(server);
    const k = normalizeCode(code);
    if (!s) return setError("Type the dashboard's address, like monitor.example.com.");
    if (!isCode(k)) return setError('The code has 8 letters and numbers, like K7PQ-3MXA.');
    void review({ server: s, code: k });
  }

  async function allow(target: PairTarget) {
    setBusy(true);
    setError('');
    try {
      const paired = await pair(target.server, target.code, deviceInfo());
      if (!paired.relay) {
        throw new Error('This dashboard is too old for this app: update it, then make a new code.');
      }
      const session: Session = {
        server: target.server,
        token: paired.token,
        device: paired.device,
        checkinSeconds: paired.checkin_seconds,
        relay: paired.relay,
      };
      await saveSession(session);
      // Location is part of what was just agreed to; the system asks once. A "no" still pairs.
      await Location.requestForegroundPermissionsAsync().catch(() => null);
      await startBackgroundCheckins();
      onPaired(session);
    } catch (e) {
      setError(message(e));
      setBusy(false);
    }
  }

  if (step.name === 'scan') {
    return (
      <View style={[styles.fill, { backgroundColor: '#000' }]}>
        <CameraView
          style={StyleSheet.absoluteFill}
          facing="back"
          barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
          onBarcodeScanned={({ data }) => onScanned(data)}
        />
        <SafeAreaView style={styles.scanOverlay} edges={['top', 'bottom']}>
          <View style={[styles.scanHint, { backgroundColor: c.scrim }]}>
            <Text style={styles.scanText}>Point the camera at the QR code on the dashboard’s pair page.</Text>
            {error ? <Text style={[styles.scanText, { color: '#ffb4b4' }]}>{error}</Text> : null}
          </View>
          <View style={styles.frame} accessibilityElementsHidden importantForAccessibility="no-hide-descendants" />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Cancel scanning"
            hitSlop={12}
            onPress={() => {
              setError('');
              setStep({ name: 'start' });
            }}
            style={({ pressed }) => [styles.scanCancel, { backgroundColor: c.scrim, opacity: pressed ? 0.7 : 1 }]}
          >
            <Text style={styles.scanText}>Cancel</Text>
          </Pressable>
        </SafeAreaView>
      </View>
    );
  }

  if (step.name === 'review') {
    const { preview, target } = step;
    return (
      <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
        <ScrollView contentContainerStyle={styles.content}>
          <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
            Before you allow it
          </Text>
          <Card>
            <Text style={[styles.lead, { color: c.ink }]}>{preview.disclosure}</Text>
            <View style={{ height: space.md }} />
            <Muted>
              This phone is “{preview.device.name}” in {preview.device.account}
              {preview.device.person ? `, for ${preview.device.person}` : ''}. Dashboard: {target.server}
            </Muted>
          </Card>
          <Muted>
            After you allow it, the phone asks once for location access. Without it, everything else
            is still shared. You can stop at any time by unpairing in this app.
          </Muted>
          {error ? <ErrorText>{error}</ErrorText> : null}
          <Button label="Allow" busy={busy} onPress={() => void allow(target)} />
          <Button
            label="Cancel"
            kind="secondary"
            disabled={busy}
            onPress={() => {
              setError('');
              setStep({ name: 'start' });
            }}
          />
        </ScrollView>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={[styles.fill, { backgroundColor: c.bg }]}>
      <KeyboardAvoidingView style={styles.fill} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
          <Text style={[styles.brand, { color: c.accent }]}>NexGuard</Text>
          <Text style={[styles.title, { color: c.ink }]} accessibilityRole="header">
            Connect this phone
          </Text>
          <Muted>
            On the dashboard, add this phone, then scan the QR code its pair page shows. Nothing is
            shared until you’ve read what will be and chosen Allow.
          </Muted>
          <Button label="Scan the QR code" busy={busy} onPress={() => void startScan()} />
          {error ? <ErrorText>{error}</ErrorText> : null}

          <View style={[styles.divider, { borderTopColor: c.line }]}>
            <Text style={[styles.dividerText, { color: c.ink2, backgroundColor: c.bg }]}>or type them</Text>
          </View>
          <Field
            label="Dashboard address"
            value={server}
            onChangeText={setServer}
            placeholder="monitor.example.com"
            keyboardType="url"
            textContentType="URL"
            returnKeyType="next"
            onSubmitEditing={() => codeInput.current?.focus()}
            submitBehavior="submit"
          />
          <Field
            ref={codeInput}
            label="Pairing code"
            value={code}
            onChangeText={(t) => setCode(t.toUpperCase())}
            placeholder="K7PQ-3MXA"
            autoCapitalize="characters"
            maxLength={12}
            returnKeyType="go"
            onSubmitEditing={submitTyped}
          />
          <Button label="Continue" kind="secondary" disabled={busy} onPress={submitTyped} />
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  content: { padding: space.xl, gap: space.lg, paddingBottom: space.xxl },
  brand: { fontSize: type.small, fontWeight: '700', letterSpacing: 0.5, textTransform: 'uppercase', marginTop: space.lg },
  title: { fontSize: type.title, fontWeight: '700', letterSpacing: -0.5 },
  lead: { fontSize: type.body, lineHeight: 23 },
  divider: { borderTopWidth: StyleSheet.hairlineWidth, marginVertical: space.sm, alignItems: 'center' },
  dividerText: { marginTop: -10, paddingHorizontal: space.sm, fontSize: type.tiny },
  scanOverlay: { flex: 1, justifyContent: 'space-between', alignItems: 'center', padding: space.xl },
  scanHint: { borderRadius: 12, padding: space.md, gap: space.xs },
  scanText: { color: '#fff', fontSize: type.body, textAlign: 'center' },
  frame: { width: 240, height: 240, borderRadius: 24, borderWidth: 3, borderColor: 'rgba(255,255,255,0.9)' },
  scanCancel: { minHeight: 48, minWidth: 140, borderRadius: 24, alignItems: 'center', justifyContent: 'center' },
});
