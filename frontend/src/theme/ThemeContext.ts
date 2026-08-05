/**
 * The theme context object and its value shape.
 *
 * Deliberately its own module, holding no component: `ThemeProvider` and `useTheme` live in
 * separate files so that no module exports both a component and a non-component value, which
 * is what `react/only-export-components` (`.oxlintrc.json`) flags. For the same reason there
 * is no `src/theme/index.ts` barrel.
 */
import { createContext } from 'react';
import type { Theme } from './theme';

export interface ThemeContextValue {
  theme: Theme;
  /** Derived, mirroring the prototype's `renderVals()`, which exposes `isDark`/`isLight`. */
  isDark: boolean;
  isLight: boolean;
  /** Sets a specific theme — FR-HDR-03's control has two segments, not one toggle target. */
  setTheme: (theme: Theme) => void;
  /** Flips dark ↔ light (FR-THM-02). */
  toggleTheme: () => void;
}

/**
 * `null` rather than a plausible default: a component rendered outside the provider is a
 * wiring bug, and silently handing it a working-looking dark theme hides that until someone
 * notices the toggle does nothing. `useTheme` turns the `null` into a named error.
 */
export const ThemeContext = createContext<ThemeContextValue | null>(null);
