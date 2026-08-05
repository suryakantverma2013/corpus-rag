/**
 * Theme primitives — pure, no React, no DOM writes (the provider owns those).
 *
 * Spec: FR-THM-01/02 (§4.8), NFR-USE-01 (dark by default), ruling R-58.
 */

/** A union, not a TS `enum` — `tsconfig.app.json` sets `erasableSyntaxOnly`. */
export type Theme = 'dark' | 'light';

/**
 * R-58(1). The key is duplicated by the pre-paint inline script in `index.html`, which has to
 * run before this module is even fetched. `theme.test.ts` pins the value so the two cannot
 * drift silently.
 */
export const THEME_STORAGE_KEY = 'corpus.theme';

/** NFR-USE-01 — "Dark theme by default with a discoverable light-theme toggle". */
export const DEFAULT_THEME: Theme = 'dark';

export function isTheme(value: unknown): value is Theme {
  return value === 'dark' || value === 'light';
}

/**
 * The stored preference, or `null` when there is none to honour — absent, unrecognised, or
 * unreadable. Callers fall back to {@link DEFAULT_THEME}.
 *
 * The `try` is not defensive padding: `localStorage` access *throws* `SecurityError` when
 * site data is blocked (some privacy modes, sandboxed iframes), and an uncaught throw here
 * would happen during `useState`'s initializer and take down the whole app over a cosmetic
 * preference.
 */
export function readStoredTheme(): Theme | null {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(stored) ? stored : null;
  } catch {
    return null;
  }
}

/** Persists the preference. Never throws — see {@link readStoredTheme}. */
export function writeStoredTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Storage unavailable. The theme still applies for this session; it just will not
    // survive a reload, which is strictly better than failing the toggle.
  }
}

/**
 * FR-THM-02's toggle, shaped as a React state updater so `toggleTheme` is `setTheme(nextTheme)`.
 * Mirrors the prototype's `toggleTheme: () => this.setState(st => ({ theme: st.theme === 'dark' ? 'light' : 'dark' }))`.
 */
export function nextTheme(theme: Theme): Theme {
  return theme === 'dark' ? 'light' : 'dark';
}
