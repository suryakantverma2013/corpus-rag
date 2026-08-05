/**
 * Reads the theme context. The whole JS-side consumption of §4.8 — everything else restyles
 * through the CSS custom properties, which is the point of FR-THM-01.
 *
 * T-504's FR-HDR-03 segmented control is the first caller.
 */
import { use } from 'react';
import { ThemeContext } from './ThemeContext';
import type { ThemeContextValue } from './ThemeContext';

export function useTheme(): ThemeContextValue {
  const value = use(ThemeContext);
  if (value === null) {
    throw new Error('useTheme must be used within a <ThemeProvider>.');
  }
  return value;
}
