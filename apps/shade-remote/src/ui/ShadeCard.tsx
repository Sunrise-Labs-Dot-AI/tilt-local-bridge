import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { ShadeState } from '../protocol/messages';
import { HairlineSlider } from './HairlineSlider';
import { radius, useTheme } from './theme';

const PRESETS: { label: string; value: number }[] = [
  { label: 'Closed', value: 0 },
  { label: '25', value: 25 },
  { label: '50', value: 50 },
  { label: '75', value: 75 },
  { label: 'Open', value: 100 },
];

/** What the card says in large type: a position in words, or where it is going. */
export function describeShade(shade: ShadeState, requested: number | undefined): string {
  if (!shade.available) return 'Unavailable';
  const target = requested ?? shade.target;
  if (target !== null && target !== undefined && target !== shade.position) {
    return `Moving to ${positionWord(target)}`;
  }
  if (shade.position === null) return 'Position unknown';
  return positionWord(shade.position, true);
}

function positionWord(position: number, capital = false): string {
  if (position === 0) return capital ? 'Closed' : 'closed';
  if (position === 100) return capital ? 'Open' : 'open';
  return `${position}% open`;
}

export function ShadeCard({
  shade,
  requested,
  writes,
  error,
  onSet,
}: {
  shade: ShadeState;
  requested: number | undefined;
  writes: boolean;
  error: string | undefined;
  onSet: (position: number) => void;
}) {
  const theme = useTheme();
  const disabled = !writes || !shade.available;
  const sliderValue = requested ?? shade.target ?? shade.position ?? 0;
  const moving = requested !== undefined || (shade.target !== null && shade.target !== shade.position);
  return (
    <View style={[styles.card, { backgroundColor: theme.card, borderColor: theme.hairline }]}>
      <View style={styles.header}>
        <Text style={[styles.name, { color: theme.inkSoft }]}>{shade.name}</Text>
        {shade.battery !== null && (
          <Text style={[styles.meta, { color: shade.battery < 20 ? theme.warn : theme.inkMuted }]}>
            Battery {shade.battery}%
          </Text>
        )}
      </View>
      <Text
        style={[styles.headline, { color: shade.available ? (moving ? theme.accent : theme.ink) : theme.inkMuted }]}
        accessibilityRole="header"
      >
        {describeShade(shade, requested)}
      </Text>
      <View style={styles.presets}>
        {PRESETS.map((preset) => {
          const active = sliderValue === preset.value;
          return (
            <Pressable
              key={preset.value}
              disabled={disabled}
              onPress={() => onSet(preset.value)}
              accessibilityRole="button"
              accessibilityLabel={`${shade.name} ${preset.label === 'Open' || preset.label === 'Closed' ? preset.label : `${preset.label} percent open`}`}
              accessibilityState={{ disabled, selected: active }}
              style={({ pressed }) => [
                styles.chip,
                {
                  backgroundColor: active ? theme.accent : theme.accentWash,
                  opacity: disabled ? 0.45 : pressed ? 0.7 : 1,
                },
              ]}
            >
              <Text style={[styles.chipText, { color: active ? theme.onAccent : theme.accent }]}>{preset.label}</Text>
            </Pressable>
          );
        })}
      </View>
      <HairlineSlider
        value={sliderValue}
        onCommit={onSet}
        disabled={disabled}
        accessibilityLabel={`${shade.name} position`}
      />
      {error ? <Text style={[styles.error, { color: theme.error }]}>{error}</Text> : null}
      {!writes && shade.available ? (
        <Text style={[styles.note, { color: theme.inkMuted }]}>Read-only bridge: positions are shown, not set.</Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderRadius: radius.card, borderWidth: StyleSheet.hairlineWidth, padding: 18, gap: 12 },
  header: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'baseline' },
  name: { fontSize: 15, fontWeight: '600', letterSpacing: 0.2 },
  meta: { fontSize: 13 },
  headline: { fontSize: 28, fontWeight: '700', letterSpacing: -0.5 },
  presets: { flexDirection: 'row', gap: 8 },
  chip: { flex: 1, paddingVertical: 10, borderRadius: radius.chip, alignItems: 'center' },
  chipText: { fontSize: 14, fontWeight: '600' },
  error: { fontSize: 13 },
  note: { fontSize: 13 },
});
