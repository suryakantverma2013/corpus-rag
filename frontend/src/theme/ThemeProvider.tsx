/**
 * Owns the runtime half of §4.8: the `data-theme` switch (FR-THM-01/02), its cross-session
 * persistence (FR-THM-02, R-58(1)) and the `accent` prop override (FR-THM-03, R-58(2)).
 *
 * All three write to `document.documentElement`, which is outside React's tree — the tokens
 * live on `:root` (`src/styles/tokens.css`) so that surfaces rendered outside the three-column
 * shell, notably the §4.17 login screen, are themed too.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { ThemeContext } from './ThemeContext';
import type { ThemeContextValue } from './ThemeContext';
import { DEFAULT_THEME, nextTheme, readStoredTheme, writeStoredTheme } from './theme';
import type { Theme } from './theme';

/**
 * What `--accent-soft` becomes while an FR-THM-03 override is in force.
 *
 * `--accent-soft` is the only token that derives from `--accent`, and it backs the *surface*
 * behind every accent-coloured element (New chat button, Grounded pill, citation chips, the
 * empty-state mark, the login brand square…). Overriding `--accent` alone would leave all of
 * them showing the theme's original tint behind the new colour — teal text on a purple wash.
 *
 * Written as one theme-independent string on purpose: the 14%/10% difference between the
 * themes rides in on `var(--accent-soft-alpha)`, so both numbers stay in `tokens.css` and
 * this effect need not re-run when the theme flips.
 */
const ACCENT_SOFT_OVERRIDE =
  'color-mix(in srgb, var(--accent) var(--accent-soft-alpha), transparent)';

export interface ThemeProviderProps {
  /**
   * FR-SYS-04 / FR-THM-03. A CSS color that overrides `--accent` in **both** themes.
   *
   * Leave it `undefined` to keep the per-theme defaults from §5.1 (dark `#7C86F8`, light
   * `#5B66E8`). Do **not** default it to `#7C86F8`: that would write the dark accent onto the
   * light theme and silently destroy the distinct light `--accent` NFR-VIS-02 specifies. This
   * is the one deliberate behavioural deviation from the prototype, whose design tool always
   * passes the prop's default so its `if (this.props.accent)` guard never fires — see R-58(2).
   */
  accent?: string;
  children?: ReactNode;
}

export function ThemeProvider({ accent, children }: ThemeProviderProps) {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme() ?? DEFAULT_THEME);

  // A layout effect, not a passive one: on toggle a passive effect lets the browser paint one
  // frame with the outgoing theme still applied, which reads as a flicker.
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  // Passive and separate, so the storage write never delays paint. It also runs on mount,
  // which is what records a first-time visitor's `dark` for the FR-AUT-01 login screen (and
  // the pre-paint script) to read on the next load.
  useEffect(() => {
    writeStoredTheme(theme);
  }, [theme]);

  // Not keyed on `theme`, by design: an inline style on the root element outranks every
  // selector, so these declarations override `:root` and `:root[data-theme='light']` alike
  // and need no re-application when the theme flips. Writing NOTHING when the prop is absent
  // is what keeps the per-theme CSS defaults — and their exact NFR-VIS-02 values — intact.
  //
  // The early return needs no matching removal: React runs the previous cleanup *before* this
  // body, so an accent that goes away has already been torn down by the time we get here.
  // (Removing it here as well was dead code — a mutation of those two lines failed nothing.)
  //
  // The value is not validated: `setProperty` ignores a value the CSSOM cannot parse, so a
  // bad accent is a no-op rather than a broken stylesheet — the right failure for a cosmetic
  // prop, and it keeps a colour parser out of the bundle.
  useLayoutEffect(() => {
    if (accent === undefined) return;
    const root = document.documentElement;
    root.style.setProperty('--accent', accent);
    root.style.setProperty('--accent-soft', ACCENT_SOFT_OVERRIDE);
    return () => {
      root.style.removeProperty('--accent');
      root.style.removeProperty('--accent-soft');
    };
  }, [accent]);

  const toggleTheme = useCallback(() => setTheme(nextTheme), []);

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme,
      isDark: theme === 'dark',
      isLight: theme === 'light',
      setTheme,
      toggleTheme,
    }),
    [theme, toggleTheme],
  );

  return <ThemeContext value={value}>{children}</ThemeContext>;
}
