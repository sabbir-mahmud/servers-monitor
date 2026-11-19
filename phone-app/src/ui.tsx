// Small shared pieces: buttons, cards, rows, fields.
import type { ReactNode, Ref } from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  type TextInputInstance,
  type TextInputProps,
  View,
} from 'react-native';

import { radius, space, type, useColors } from './theme';

type ButtonProps = {
  label: string;
  onPress: () => void;
  kind?: 'primary' | 'secondary' | 'danger';
  busy?: boolean;
  disabled?: boolean;
  hint?: string;
};

export function Button({ label, onPress, kind = 'primary', busy, disabled, hint }: ButtonProps) {
  const c = useColors();
  const primary = kind === 'primary';
  const off = disabled || busy;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={hint}
      accessibilityState={{ disabled: !!off, busy: !!busy }}
      disabled={off}
      onPress={onPress}
      hitSlop={6}
      style={({ pressed }) => [
        styles.button,
        primary
          ? { backgroundColor: c.accent }
          : { backgroundColor: c.surface, borderColor: c.line, borderWidth: 1 },
        { opacity: off ? 0.5 : pressed ? 0.75 : 1 },
      ]}
    >
      {busy ? (
        <ActivityIndicator color={primary ? c.onAccent : c.ink} />
      ) : (
        <Text
          style={[
            styles.buttonText,
            { color: primary ? c.onAccent : kind === 'danger' ? c.bad : c.ink },
          ]}
        >
          {label}
        </Text>
      )}
    </Pressable>
  );
}

export function Card({ children }: { children: ReactNode }) {
  const c = useColors();
  return (
    <View style={[styles.card, { backgroundColor: c.surface, borderColor: c.line }]}>
      {children}
    </View>
  );
}

export function Row({ label, value, last }: { label: string; value: string; last?: boolean }) {
  const c = useColors();
  return (
    <View
      style={[styles.row, !last && { borderBottomColor: c.line, borderBottomWidth: StyleSheet.hairlineWidth }]}
      accessible
      accessibilityLabel={`${label}: ${value}`}
    >
      <Text style={[styles.rowLabel, { color: c.ink2 }]}>{label}</Text>
      <Text style={[styles.rowValue, { color: c.ink }]} numberOfLines={2}>
        {value}
      </Text>
    </View>
  );
}

// React 19: `ref` is a plain prop, passed on to the TextInput with the rest.
export function Field({ label, ...props }: TextInputProps & { label: string; ref?: Ref<TextInputInstance> }) {
  const c = useColors();
  return (
    <View style={styles.field}>
      <Text style={[styles.fieldLabel, { color: c.ink2 }]}>{label}</Text>
      <TextInput
        accessibilityLabel={label}
        placeholderTextColor={c.ink2}
        autoCapitalize="none"
        autoCorrect={false}
        style={[styles.input, { color: c.ink, backgroundColor: c.surface, borderColor: c.line }]}
        {...props}
      />
    </View>
  );
}

export function Muted({ children, center }: { children: ReactNode; center?: boolean }) {
  const c = useColors();
  return <Text style={[styles.muted, { color: c.ink2 }, center && { textAlign: 'center' }]}>{children}</Text>;
}

export function ErrorText({ children }: { children: ReactNode }) {
  const c = useColors();
  return (
    <Text accessibilityRole="alert" style={[styles.muted, { color: c.bad }]}>
      {children}
    </Text>
  );
}

const styles = StyleSheet.create({
  button: {
    minHeight: 50,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
  },
  buttonText: { fontSize: type.body, fontWeight: '600' },
  card: { borderRadius: radius.lg, borderWidth: StyleSheet.hairlineWidth, padding: space.lg },
  row: { flexDirection: 'row', justifyContent: 'space-between', gap: space.md, paddingVertical: space.md },
  rowLabel: { fontSize: type.body },
  rowValue: { fontSize: type.body, fontWeight: '500', flexShrink: 1, textAlign: 'right' },
  field: { gap: space.xs },
  fieldLabel: { fontSize: type.small, fontWeight: '500' },
  input: {
    minHeight: 50,
    borderRadius: radius.md,
    borderWidth: 1,
    paddingHorizontal: space.md,
    fontSize: type.body,
  },
  muted: { fontSize: type.small, lineHeight: 20 },
});
