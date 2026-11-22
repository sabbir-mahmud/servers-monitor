// Colours and spacing, light and dark: the dashboard's own (main/static/css/app.css), so
// the app and the dashboard look like one product. Brand blue is the logo's #4a6cf0.
import { useColorScheme } from 'react-native';

const light = {
  bg: '#f3f3f0',
  surface: '#ffffff',
  ink: '#1d1d1f',
  ink2: '#55555b',
  line: '#e3e2dc',
  accent: '#3b62d9',
  onAccent: '#ffffff',
  good: '#2f9e5b',
  bad: '#d24848',
  scrim: 'rgba(0,0,0,0.55)',
};

const dark: typeof light = {
  bg: '#111113',
  surface: '#18181b',
  ink: '#e7e7ea',
  ink2: '#a5a5ad',
  line: '#28282d',
  accent: '#7d96f5',
  onAccent: '#0f1430',
  good: '#4cbd78',
  bad: '#ec6a6a',
  scrim: 'rgba(0,0,0,0.65)',
};

export type Colors = typeof light;

export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 };
export const radius = { sm: 8, md: 12, lg: 16 };
export const type = { title: 28, heading: 18, body: 16, small: 14, tiny: 12 };

export function useColors(): Colors {
  return useColorScheme() === 'dark' ? dark : light;
}
