/**
 * FR-HDR-01..03 behaviour. The stylesheets are guarded separately in `ChatHeader.css.test.ts` —
 * jsdom applies no external CSS, so nothing here can see a pixel.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ChatHeader } from './ChatHeader';
import { ThemeProvider } from '../theme/ThemeProvider';
import { THEME_STORAGE_KEY } from '../theme/theme';

const root = () => document.documentElement;

beforeEach(() => {
  // The provider persists to localStorage and writes to the root element, both of which
  // outlive React's cleanup. Same reset as ThemeProvider.test.tsx / App.test.tsx.
  localStorage.clear();
  root().removeAttribute('data-theme');
  root().removeAttribute('style');
});

/** The toggle reads `useTheme()`, so the header is never rendered outside the provider. */
function header(title: string | null = 'Analyzing Market Trends') {
  return render(
    <ThemeProvider>
      <ChatHeader title={title} />
    </ThemeProvider>,
  );
}

const segment = (name: 'Dark' | 'Light') => screen.getByRole('button', { name });

describe('FR-HDR-01 title', () => {
  it('renders the active chat title as a level-2 heading', () => {
    header('Product Launch Strategy');
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Product Launch Strategy');
  });

  it('leaves the document’s only h1 to the shell (T-502)', () => {
    header();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });

  it('falls back to the untitled-chat label when there is no active conversation', () => {
    // Reachable via FR-SBR-07: deleting the last conversation leaves `activeId` null. The
    // heading must not render empty.
    header(null);
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('New chat');
  });
});

describe('FR-HDR-02 Grounded pill', () => {
  it('is always shown (R-23 — grounding is always-on)', () => {
    header();
    expect(screen.getByText('Grounded')).not.toBeNull();
  });

  it('hides its decorative dot from assistive technology', () => {
    const { container } = header();
    // The word beside it carries the meaning; announcing an unlabelled shape adds nothing.
    expect(container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(1);
  });
});

describe('FR-HDR-03 theme toggle', () => {
  it('renders two buttons, not one container control', () => {
    // The decision this task was chartered to make explicitly. The prototype puts a single
    // onClick on the wrapper; FR-HDR-03 specifies two segments.
    header();
    expect(segment('Dark').tagName).toBe('BUTTON');
    expect(segment('Light').tagName).toBe('BUTTON');
  });

  it('names the pair as one control', () => {
    header();
    expect(screen.getByRole('group', { name: 'Theme' })).not.toBeNull();
  });

  it('marks the active segment with aria-pressed, not colour alone (NFR-A11Y-06)', () => {
    // `--muted2` vs `--text` is a recorded, failing contrast pair — the requirement names this
    // very control — so the state must also be exposed programmatically.
    header();
    expect(segment('Dark').getAttribute('aria-pressed')).toBe('true');
    expect(segment('Light').getAttribute('aria-pressed')).toBe('false');
  });

  it('switches to the theme the clicked segment names', () => {
    header();
    fireEvent.click(segment('Light'));

    expect(root().dataset.theme).toBe('light');
    expect(segment('Light').getAttribute('aria-pressed')).toBe('true');
    expect(segment('Dark').getAttribute('aria-pressed')).toBe('false');
  });

  it('and back again', () => {
    // Pins that the two segments are not wired to the same handler: with both calling
    // `setTheme('light')` the test above still passes and this one does not.
    header();
    fireEvent.click(segment('Light'));
    fireEvent.click(segment('Dark'));
    expect(root().dataset.theme).toBe('dark');
  });

  it('does nothing when the already-active segment is clicked', () => {
    // THE decision, asserted. The prototype would flip the theme here.
    header();
    fireEvent.click(segment('Dark'));
    expect(root().dataset.theme).toBe('dark');
    expect(segment('Dark').getAttribute('aria-pressed')).toBe('true');
  });

  it('persists the choice across sessions (FR-THM-02, R-58(1))', () => {
    header();
    fireEvent.click(segment('Light'));
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
  });

  it('reflects a stored theme it did not itself select', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    header();
    expect(segment('Light').getAttribute('aria-pressed')).toBe('true');
  });

  it('reaches both segments as tab stops, in the prototype’s order', () => {
    // NFR-A11Y-04. `tabIndex: -1` or a roving-tabindex refactor would silently remove one.
    const { container } = header();
    const buttons = [...container.querySelectorAll('button')];
    expect(buttons.map((b) => b.textContent)).toEqual(['Dark', 'Light']);
    expect(buttons.every((b) => b.tabIndex === 0)).toBe(true);
  });
});
