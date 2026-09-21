import { StatusBar } from 'expo-status-bar';
import { useEffect, useRef, useState } from 'react';
import { AppState, StyleSheet, Text, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';

import { RemoteController, type RemoteSnapshot } from './src/session/remoteController';
import { createTransport } from './src/transport';
import { PairingView, RadioView, SearchingView, ShadesView, UnpairedView } from './src/ui/Screens';
import { useTheme } from './src/ui/theme';

export default function App() {
  const controllerRef = useRef<RemoteController | null>(null);
  if (!controllerRef.current) {
    controllerRef.current = new RemoteController({ transport: createTransport() });
  }
  const controller = controllerRef.current;
  const [snapshot, setSnapshot] = useState<RemoteSnapshot>(controller.state);

  useEffect(() => {
    const unsubscribe = controller.subscribe(setSnapshot);
    void controller.start();
    const appState = AppState.addEventListener('change', (state) => {
      if (state === 'active') controller.resume();
    });
    return () => {
      appState.remove();
      unsubscribe();
      void controller.stop();
    };
  }, [controller]);

  return (
    <SafeAreaProvider>
      <Shell snapshot={snapshot} controller={controller} />
    </SafeAreaProvider>
  );
}

function Shell({ snapshot, controller }: { snapshot: RemoteSnapshot; controller: RemoteController }) {
  const theme = useTheme();
  let body: React.ReactNode;
  switch (snapshot.phase) {
    case 'radio':
      body = <RadioView snapshot={snapshot} />;
      break;
    case 'unpaired':
      body = <UnpairedView snapshot={snapshot} controller={controller} />;
      break;
    case 'pairing':
      body = <PairingView snapshot={snapshot} />;
      break;
    case 'ready':
      body = <ShadesView snapshot={snapshot} controller={controller} />;
      break;
    default:
      body = <SearchingView snapshot={snapshot} />;
  }
  const subtitle = snapshot.phase === 'ready'
    ? `${snapshot.bridgeName ?? 'Bridge'} · Bluetooth`
    : 'Direct to the bridge, no Wi-Fi needed';
  return (
    <SafeAreaView style={[styles.screen, { backgroundColor: theme.paper }]} edges={['top', 'left', 'right']}>
      <StatusBar style="auto" />
      <View style={styles.header}>
        <Text style={[styles.brand, { color: theme.ink }]}>Shade Remote</Text>
        <Text style={[styles.subtitle, { color: theme.inkMuted }]}>{subtitle}</Text>
      </View>
      {body}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  header: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 4, gap: 2 },
  brand: { fontSize: 20, fontWeight: '700', letterSpacing: -0.3 },
  subtitle: { fontSize: 13 },
});
