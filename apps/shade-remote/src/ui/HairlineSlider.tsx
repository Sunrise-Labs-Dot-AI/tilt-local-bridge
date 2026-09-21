import { useRef, useState } from 'react';
import { PanResponder, StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';

import { useTheme } from './theme';

const THUMB = 26;

/**
 * A one-pixel track with a dot. Dragging previews the value; letting go
 * commits it. A tap commits too. Pure JS, so no native slider module.
 *
 * The value is committed on release rather than continuously because every
 * commit is a Bluetooth round trip and a shade takes tens of seconds to
 * answer; a stream of intermediate targets would only queue behind each other.
 */
export function HairlineSlider({
  value,
  onCommit,
  disabled = false,
  accessibilityLabel,
}: {
  value: number;
  onCommit: (percent: number) => void;
  disabled?: boolean;
  accessibilityLabel: string;
}) {
  const theme = useTheme();
  const [width, setWidth] = useState(0);
  const [dragValue, setDragValue] = useState<number | null>(null);
  const state = useRef({ width: 0, start: 0, disabled, onCommit });
  state.current.width = width;
  state.current.disabled = disabled;
  state.current.onCommit = onCommit;

  const clamp = (percent: number) => Math.max(0, Math.min(100, Math.round(percent)));
  const percentAt = (x: number) => clamp((x / Math.max(1, state.current.width)) * 100);

  const responder = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => !state.current.disabled,
      onMoveShouldSetPanResponder: () => !state.current.disabled,
      onPanResponderGrant: (event) => {
        const percent = percentAt(event.nativeEvent.locationX);
        state.current.start = percent;
        setDragValue(percent);
      },
      onPanResponderMove: (_event, gesture) => {
        const percent = clamp(state.current.start + (gesture.dx / Math.max(1, state.current.width)) * 100);
        setDragValue(percent);
      },
      onPanResponderRelease: (_event, gesture) => {
        const percent = clamp(state.current.start + (gesture.dx / Math.max(1, state.current.width)) * 100);
        setDragValue(null);
        state.current.onCommit(percent);
      },
      onPanResponderTerminate: () => setDragValue(null),
      onPanResponderTerminationRequest: () => false,
    }),
  ).current;

  const shown = dragValue ?? value;
  const left = width > 0 ? (shown / 100) * (width - THUMB) : 0;

  return (
    <View
      style={styles.wrap}
      accessible
      accessibilityRole="adjustable"
      accessibilityLabel={accessibilityLabel}
      accessibilityValue={{ min: 0, max: 100, now: shown, text: `${shown} percent open` }}
      accessibilityActions={[{ name: 'increment' }, { name: 'decrement' }]}
      onAccessibilityAction={(event) => {
        if (disabled) return;
        onCommit(clamp(value + (event.nativeEvent.actionName === 'increment' ? 10 : -10)));
      }}
    >
      <View
        style={styles.touch}
        onLayout={(event: LayoutChangeEvent) => setWidth(event.nativeEvent.layout.width)}
        {...responder.panHandlers}
      >
        <View style={[styles.track, { backgroundColor: theme.track }]} />
        <View style={[styles.fill, { backgroundColor: disabled ? theme.track : theme.accent, width: left + THUMB / 2 }]} />
        <View
          style={[
            styles.thumb,
            { left, backgroundColor: disabled ? theme.inkMuted : theme.accent, borderColor: theme.card },
            dragValue !== null && styles.thumbActive,
          ]}
        />
      </View>
      <Text style={[styles.value, { color: dragValue !== null ? theme.accent : theme.inkMuted }]}>{shown}%</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  touch: { flex: 1, height: 44, justifyContent: 'center' },
  track: { height: 2, borderRadius: 1 },
  fill: { position: 'absolute', left: 0, height: 2, borderRadius: 1 },
  thumb: {
    position: 'absolute',
    width: THUMB,
    height: THUMB,
    borderRadius: THUMB / 2,
    borderWidth: 3,
  },
  thumbActive: { transform: [{ scale: 1.15 }] },
  value: { width: 44, textAlign: 'right', fontVariant: ['tabular-nums'], fontSize: 15 },
});
