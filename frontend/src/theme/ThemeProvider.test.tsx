import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ThemeProvider } from './ThemeProvider';
import { useTheme } from './useTheme';
import { THEME_STORAGE_KEY } from './theme';

/** Surfaces the context so the tests can drive it the way T-504's control will. */
function Probe() {
  const { theme, isDark, isLight, setTheme, toggleTheme } = useTheme();
  return (
    <>
      <span data-testid="theme">{theme}</span>
      <span data-testid="derived">{`${isDark}/${isLight}`}</span>
      <button onClick={toggleTheme}>toggle</button>
      <button onClick={() => setTheme('light')}>set-light</button>
    </>
  );
}

const root = () => document.documentElement;
const accent = () => root().style.getPropertyValue('--accent');
const accentSoft = () => root().style.getPropertyValue('--accent-soft');

beforeEach(() => {
  localStorage.clear();
  root().removeAttribute('data-theme');
  root().removeAttribute('style');
});

describe('theme selection (FR-THM-01/02)', () => {
  it('applies dark when nothing is stored (NFR-USE-01)', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(root().dataset.theme).toBe('dark');
    expect(screen.getByTestId('theme').textContent).toBe('dark');
    expect(screen.getByTestId('derived').textContent).toBe('true/false');
  });

  it('applies the stored preference on the first commit (R-58(1), FR-AUT-01)', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(root().dataset.theme).toBe('light');
    expect(screen.getByTestId('derived').textContent).toBe('false/true');
  });

  it('falls back to dark when the stored value is unrecognised', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system');
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    expect(root().dataset.theme).toBe('dark');
  });

  it('toggleTheme flips the attribute and persists the choice', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    fireEvent.click(screen.getByText('toggle'));
    expect(root().dataset.theme).toBe('light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');

    fireEvent.click(screen.getByText('toggle'));
    expect(root().dataset.theme).toBe('dark');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });

  it('setTheme targets a specific theme (FR-HDR-03 has two segments)', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
    fireEvent.click(screen.getByText('set-light'));
    expect(root().dataset.theme).toBe('light');
    // Idempotent: clicking the already-active segment must not flip back.
    fireEvent.click(screen.getByText('set-light'));
    expect(root().dataset.theme).toBe('light');
  });

  it('records the default on mount so the next load has something to read', () => {
    render(<ThemeProvider />);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });
});

describe('accent override (FR-THM-03, R-58(2))', () => {
  it('writes the supplied accent to the root element', () => {
    render(<ThemeProvider accent="#4ec3a6" />);
    expect(accent()).toBe('#4ec3a6');
  });

  it('re-derives --accent-soft so the tint tracks the accent', () => {
    // --accent-soft is the surface *behind* every accent-coloured element. Overriding
    // --accent alone leaves the theme's original wash there — teal text on purple.
    // The alpha rides in on var(--accent-soft-alpha), which is why this string carries no
    // percentage and the effect need not re-run per theme.
    render(<ThemeProvider accent="#4ec3a6" />);
    expect(accentSoft()).toBe(
      'color-mix(in srgb, var(--accent) var(--accent-soft-alpha), transparent)',
    );
  });

  it('leaves --accent-soft at its exact NFR-VIS-02 literal when no accent is supplied', () => {
    render(<ThemeProvider />);
    expect(accentSoft()).toBe('');
  });

  it('writes NOTHING when the prop is absent, so the light theme keeps #5B66E8', () => {
    // The load-bearing assertion of this task. A JS-side default of `#7C86F8` — the obvious
    // reading of FR-SYS-04 — would write the dark accent onto :root, where it outranks
    // :root[data-theme='light'] and destroys the distinct light accent NFR-VIS-02 specifies.
    // Invisible to tsc, to oxlint, and to any dark-theme screenshot. This is the only guard.
    render(<ThemeProvider />);
    expect(accent()).toBe('');
    expect(root().getAttribute('style')).toBeNull();
  });

  it('removes the override when the prop goes away', () => {
    const { rerender } = render(<ThemeProvider accent="#4ec3a6" />);
    expect(accent()).toBe('#4ec3a6');
    rerender(<ThemeProvider />);
    expect(accent()).toBe('');
    expect(accentSoft()).toBe('');
  });

  it('removes the override when the provider unmounts', () => {
    // The effect's cleanup, which the prop-goes-away case above does NOT exercise — that
    // path is handled by the effect body's `undefined` branch. Without a cleanup the accent
    // outlives the component that set it, because it was written to an element React does
    // not own; found by mutation testing.
    const { unmount } = render(<ThemeProvider accent="#4ec3a6" />);
    expect(accent()).toBe('#4ec3a6');
    unmount();
    expect(accent()).toBe('');
    expect(accentSoft()).toBe('');
  });

  it('survives a theme toggle without re-application', () => {
    // An inline style on the root element outranks both :root and :root[data-theme='light'],
    // which is why the effect is not keyed on `theme`.
    render(
      <ThemeProvider accent="#4ec3a6">
        <Probe />
      </ThemeProvider>,
    );
    fireEvent.click(screen.getByText('toggle'));
    expect(root().dataset.theme).toBe('light');
    expect(accent()).toBe('#4ec3a6');
  });

  it('applies a changed accent', () => {
    const { rerender } = render(<ThemeProvider accent="#4ec3a6" />);
    rerender(<ThemeProvider accent="#e8a34c" />);
    expect(accent()).toBe('#e8a34c');
  });
});

describe('useTheme', () => {
  it('throws outside a provider rather than returning a plausible default', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Probe />)).toThrow(/within a <ThemeProvider>/);
  });
});
