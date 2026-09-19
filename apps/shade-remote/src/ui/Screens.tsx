import { useEffect, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';

import type { RemoteController, RemoteSnapshot } from '../session/remoteController';
import { ShadeCard } from './ShadeCard';
import { radius, useTheme } from './theme';

function Button({ label, onPress, secondary = false }: { label: string; onPress: () => void; secondary?: boolean }) {
  const theme = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: secondary ? theme.accentWash : theme.accent, opacity: pressed ? 0.75 : 1 },
      ]}
    >
      <Text style={[styles.buttonText, { color: secondary ? theme.accent : theme.onAccent }]}>{label}</Text>
    </Pressable>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <View style={styles.centered}>{children}</View>;
}

export function RadioView({ snapshot }: { snapshot: RemoteSnapshot }) {
  const theme = useTheme();
  const text = {
    off: ['Bluetooth is off', 'Turn Bluetooth on to reach the bridge.'],
    unauthorized: ['Bluetooth permission needed', 'Allow Bluetooth for Shade Remote in Settings. It only ever talks to your bridge.'],
    unsupported: ['No Bluetooth on this device', 'Shade Remote needs Bluetooth Low Energy.'],
    unknown: ['Checking Bluetooth', ''],
    ready: ['Bluetooth is ready', ''],
  }[snapshot.radio];
  return (
    <Centered>
      <Text style={[styles.title, { color: theme.ink }]}>{text[0]}</Text>
      <Text style={[styles.body, { color: theme.inkSoft }]}>{text[1]}</Text>
    </Centered>
  );
}

export function SearchingView({ snapshot }: { snapshot: RemoteSnapshot }) {
  const theme = useTheme();
  const target = snapshot.knownBridge?.name ?? 'your bridge';
  const title = snapshot.phase === 'connecting'
    ? `Connecting to ${snapshot.bridgeName ?? target}`
    : snapshot.phase === 'disconnected'
      ? 'Connection lost'
      : `Looking for ${target}`;
  return (
    <Centered>
      <ActivityIndicator color={theme.accent} />
      <Text style={[styles.title, { color: theme.ink }]}>{title}</Text>
      <Text style={[styles.body, { color: theme.inkSoft }]}>
        Bluetooth reaches a room or two. Stay near the Raspberry Pi that runs the bridge.
      </Text>
      {snapshot.message ? <Text style={[styles.body, { color: theme.error }]}>{snapshot.message}</Text> : null}
    </Centered>
  );
}

export function UnpairedView({ snapshot, controller }: { snapshot: RemoteSnapshot; controller: RemoteController }) {
  const theme = useTheme();
  const outcome = snapshot.pairingOutcome === 'expired'
    ? 'The last request expired before it was approved.'
    : snapshot.pairingOutcome === 'denied'
      ? 'The last request was denied.'
      : null;
  return (
    <Centered>
      <Text style={[styles.eyebrow, { color: theme.inkMuted }]}>Found</Text>
      <Text style={[styles.title, { color: theme.ink }]}>{snapshot.bridgeName ?? 'Tilt Local Bridge'}</Text>
      <Text style={[styles.body, { color: theme.inkSoft }]}>
        This phone is not paired with the bridge yet. Pairing shows a code here and in Home Assistant; a tap
        there approves this phone.
      </Text>
      {outcome ? <Text style={[styles.body, { color: theme.warn }]}>{outcome}</Text> : null}
      {snapshot.message ? <Text style={[styles.body, { color: theme.error }]}>{snapshot.message}</Text> : null}
      <Button label="Pair this phone" onPress={() => void controller.pair()} />
    </Centered>
  );
}

export function PairingView({ snapshot }: { snapshot: RemoteSnapshot }) {
  const theme = useTheme();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const remaining = snapshot.pairingExpiresAt ? Math.max(0, Math.round((snapshot.pairingExpiresAt - now) / 1000)) : null;
  const challenge = snapshot.pairingChallenge;
  return (
    <Centered>
      {challenge ? (
        <>
          <Text style={[styles.eyebrow, { color: theme.inkMuted }]}>To finish pairing</Text>
          <Text style={[styles.title, { color: theme.ink }]} accessibilityRole="header">
            Tap <Text style={{ color: theme.accent }}>{challenge.direction}</Text> on{' '}
            <Text style={{ color: theme.accent }}>{challenge.name}</Text>
          </Text>
          <Text style={[styles.body, { color: theme.inkSoft }]}>
            Press that button on the shade itself. The bridge watches for exactly that movement; any other shade or
            direction cancels the request.
          </Text>
        </>
      ) : (
        <Text style={[styles.body, { color: theme.inkSoft }]}>
          No shade is reachable right now, so this request can only be approved from Home Assistant.
        </Text>
      )}
      <Text style={[styles.eyebrow, { color: theme.inkMuted }]}>Or approve with code</Text>
      <Text style={[styles.code, { color: theme.ink }]} accessibilityLabel={`Pairing code ${snapshot.pairingCode ?? ''}`}>
        {snapshot.pairingCode ? `${snapshot.pairingCode.slice(0, 3)} ${snapshot.pairingCode.slice(3)}` : '···'}
      </Text>
      <Text style={[styles.meta, { color: theme.inkMuted }]}>
        Home Assistant shows this code on the Phone pairing request sensor; Approve phone pairing accepts it.
      </Text>
      <View style={styles.row}>
        <ActivityIndicator color={theme.accent} />
        <Text style={[styles.body, { color: theme.inkMuted }]}>
          {remaining !== null ? `Waiting for approval, ${remaining}s left` : 'Waiting for approval'}
        </Text>
      </View>
    </Centered>
  );
}

export function ShadesView({ snapshot, controller }: { snapshot: RemoteSnapshot; controller: RemoteController }) {
  const theme = useTheme();
  return (
    <FlatList
      data={snapshot.shades}
      keyExtractor={(shade) => shade.id}
      contentContainerStyle={styles.list}
      refreshControl={(
        <RefreshControl refreshing={snapshot.refreshing} onRefresh={() => void controller.refresh()} tintColor={theme.accent} />
      )}
      ListHeaderComponent={(
        <View style={styles.listHeader}>
          {snapshot.differentBridge ? (
            <View style={[styles.banner, { backgroundColor: theme.accentWash }]}>
              <Text style={[styles.body, { color: theme.ink }]}>
                This is not the bridge this phone paired with before ({snapshot.knownBridge?.name}). Use this one?
              </Text>
              <Button label="Use this bridge" onPress={() => void controller.connectToDifferentBridge()} secondary />
            </View>
          ) : null}
          {snapshot.message ? (
            <View style={[styles.banner, { backgroundColor: theme.errorWash }]}>
              <Text style={[styles.body, { color: theme.error }]}>{snapshot.message}</Text>
            </View>
          ) : null}
        </View>
      )}
      renderItem={({ item }) => (
        <ShadeCard
          shade={item}
          requested={snapshot.requested[item.id]}
          writes={snapshot.writes}
          error={snapshot.shadeErrors[item.id]}
          onSet={(position) => void controller.setPosition(item.id, position)}
        />
      )}
      ItemSeparatorComponent={() => <View style={styles.gap} />}
      ListEmptyComponent={(
        <Text style={[styles.body, { color: theme.inkMuted }]}>The bridge has no shades configured.</Text>
      )}
      ListFooterComponent={(
        <View style={styles.footer}>
          <Text style={[styles.meta, { color: theme.inkMuted }]}>
            Connected to {snapshot.bridgeName ?? 'the bridge'} over Bluetooth. Pull down to re-read every shade.
          </Text>
          <Pressable onPress={() => void controller.forgetBridge()} accessibilityRole="button" hitSlop={8}>
            <Text style={[styles.link, { color: theme.inkMuted }]}>Forget bridge on this phone</Text>
          </Pressable>
        </View>
      )}
    />
  );
}

const styles = StyleSheet.create({
  centered: { flex: 1, justifyContent: 'center', paddingHorizontal: 28, gap: 14 },
  eyebrow: { fontSize: 13, textTransform: 'uppercase', letterSpacing: 1.2 },
  title: { fontSize: 26, fontWeight: '700', letterSpacing: -0.4 },
  body: { fontSize: 16, lineHeight: 23 },
  code: { fontSize: 44, fontWeight: '700', fontVariant: ['tabular-nums'], letterSpacing: 3 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  button: { paddingVertical: 14, paddingHorizontal: 22, borderRadius: radius.control, alignItems: 'center', marginTop: 8 },
  buttonText: { fontSize: 16, fontWeight: '600' },
  list: { padding: 16, paddingBottom: 40 },
  listHeader: { gap: 12, marginBottom: 12 },
  banner: { borderRadius: radius.control, padding: 14, gap: 8 },
  gap: { height: 12 },
  footer: { marginTop: 22, gap: 10 },
  meta: { fontSize: 13, lineHeight: 18 },
  link: { fontSize: 13, textDecorationLine: 'underline' },
});
