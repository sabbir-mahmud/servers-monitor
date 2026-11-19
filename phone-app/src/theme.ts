// Colours and spacing, light and dark, in step with the dashboard's look.
import { useColorScheme } from 'react-native';

const light = {
  bg: '#f5f5f7',
  surface: '#ffffff',
  ink: '#1d1d1f',
  ink2: '#6e6e73',
  line: '#e5e5ea',
  accent: '#0a66d8',
  onAccent: '#ffffff',
  good: '#1f8a4c',
  bad: '#c62828',
  scrim: 'rgba(0,0,0,0.55)',
};

const dark: typeof light = {
  bg: '#000000',
  surface: '#1c1c1e',
  ink: '#f5f5f7',
  ink2: '#a1a1a6',
  line: '#2c2c2e',
  accent: '#3d8bfd',
  onAccent: '#ffffff',
  good: '#34c76f',
  bad: '#ff6b6b',
  scrim: 'rgba(0,0,0,0.65)',
};

export type Colors = typeof light;

export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };
export const radius = { sm: 8, md: 12, lg: 16 };
export const type = { title: 28, heading: 18, body: 16, small: 14, tiny: 12 };

export function useColors(): Colors {
  return useColorScheme() === 'dark' ? dark : light;
}
