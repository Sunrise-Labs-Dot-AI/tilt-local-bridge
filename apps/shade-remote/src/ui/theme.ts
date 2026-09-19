import { useColorScheme } from 'react-native';

// Warm paper and ink, with one slate accent for the interactive bits. Both
// appearances are full palettes so the same component reads well at night in
// a bedroom, which is where a shade remote gets used.

export interface Palette {
  paper: string;
  card: string;
  ink: string;
  inkSoft: string;
  inkMuted: string;
  hairline: string;
  accent: string;
  onAccent: string;
  accentWash: string;
  good: string;
  warn: string;
  error: string;
  errorWash: string;
  track: string;
}

export const light: Palette = {
  paper: '#F6F3EC',
  card: '#FFFFFF',
  ink: '#1E1A12',
  inkSoft: '#5E5748',
  inkMuted: '#8A8270',
  hairline: 'rgba(30,26,18,0.12)',
  accent: '#2B5F86',
  onAccent: '#FFFFFF',
  accentWash: 'rgba(43,95,134,0.10)',
  good: '#256D45',
  warn: '#8A5A12',
  error: '#9A3524',
  errorWash: 'rgba(154,53,36,0.10)',
  track: '#D9D3C4',
};

export const dark: Palette = {
  paper: '#141310',
  card: '#1F1D18',
  ink: '#F1ECE0',
  inkSoft: '#C2BBA8',
  inkMuted: '#8E8874',
  hairline: 'rgba(241,236,224,0.14)',
  accent: '#7FB2DE',
  onAccent: '#0E1A24',
  accentWash: 'rgba(127,178,222,0.14)',
  good: '#6FCF97',
  warn: '#E0B15A',
  error: '#F0937A',
  errorWash: 'rgba(240,147,122,0.14)',
  track: '#3A3730',
};

export const radius = { card: 20, control: 12, chip: 999 } as const;

export function useTheme(): Palette {
  return useColorScheme() === 'dark' ? dark : light;
}
