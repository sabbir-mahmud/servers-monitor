// Not connected: enter the dashboard's 6-digit code. Just connected: what's shared and the
// permission prompts, once. Then: connected, as whom, and what the phone reports.
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { ActivityIndicator, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { PairScreen } from './src/PairScreen';
import { PermissionsScreen } from './src/PermissionsScreen';
import { StatusScreen } from './src/StatusScreen';
import { loadSession, type Session } from './src/session';
import { useColors } from './src/theme';

export default function App() {
  const c = useColors();
  // undefined: still reading the Keychain; null: not connected.
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  useEffect(() => {
    void loadSession().then(setSession);
  }, []);

  let screen;
  if (session === undefined) {
    screen = (
      <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: c.bg }}>
        <ActivityIndicator color={c.ink2} />
      </View>
    );
  } else if (session === null) {
    screen = <PairScreen onPaired={setSession} />;
  } else if (!session.permissionsAsked) {
    screen = <PermissionsScreen session={session} onDone={setSession} />;
  } else {
    screen = <StatusScreen session={session} onUnpaired={() => setSession(null)} />;
  }

  return (
    <SafeAreaProvider>
      <StatusBar style="auto" />
      {screen}
    </SafeAreaProvider>
  );
}
