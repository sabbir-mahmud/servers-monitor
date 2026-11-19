// Not paired: the pairing screen. Paired: what the phone reports.
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { ActivityIndicator, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { PairScreen } from './src/PairScreen';
import { StatusScreen } from './src/StatusScreen';
import { loadSession, type Session } from './src/session';
import { useColors } from './src/theme';

export default function App() {
  const c = useColors();
  // undefined: still reading the Keychain; null: not paired.
  const [session, setSession] = useState<Session | null | undefined>(undefined);

  useEffect(() => {
    void loadSession().then(setSession);
  }, []);

  return (
    <SafeAreaProvider>
      <StatusBar style="auto" />
      {session === undefined ? (
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: c.bg }}>
          <ActivityIndicator color={c.ink2} />
        </View>
      ) : session === null ? (
        <PairScreen onPaired={setSession} />
      ) : (
        <StatusScreen session={session} onUnpaired={() => setSession(null)} />
      )}
    </SafeAreaProvider>
  );
}
