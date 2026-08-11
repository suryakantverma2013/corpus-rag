/**
 * T-502 — the FR-LAY-01 shell.
 *
 * What is *not* here, deliberately: any assertion about a rendered dimension. Vitest runs with
 * `css: false`, so jsdom applies no external stylesheet and `getComputedStyle(nav).width` is
 * `''` rather than `264px`. The §9 literals are pinned by a pair instead — `tokens.css`
 * declares `--sidebar-w: 264px` (guarded in `styles/tokens.test.ts`) and `AppShell.module.css`
 * uses `var(--sidebar-w)` and contains no literal (guarded in `AppShell.css.test.ts`). Their
 * composition is verified in a real browser, and formally by T-510.
 */
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { AppShell } from './AppShell';
import type { AppShellProps } from './AppShell';

/** Both required props supplied — the FR-SYS-04 defaults live in `App`, and `App.test.tsx`
 *  is what proves it. */
const shell = (props: Partial<AppShellProps> = {}) =>
  render(<AppShell brandName="Corpus" showStats {...props} />);

const FOLLOWS = Node.DOCUMENT_POSITION_FOLLOWING;

describe('FR-LAY-01 landmarks (NFR-A11Y-03)', () => {
  it('renders exactly one of each of the three region landmarks', () => {
    shell();
    expect(screen.getAllByRole('navigation', { name: 'Conversations' })).toHaveLength(1);
    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.getAllByRole('complementary', { name: 'Session statistics' })).toHaveLength(1);
  });

  it('puts each slot inside its own landmark', () => {
    shell({ sidebar: <p>SIDEBAR</p>, chat: <p>CHAT</p>, stats: <p>STATS</p> });
    expect(within(screen.getByRole('navigation')).getByText('SIDEBAR')).not.toBeNull();
    expect(within(screen.getByRole('main')).getByText('CHAT')).not.toBeNull();
    expect(within(screen.getByRole('complementary')).getByText('STATS')).not.toBeNull();
  });

  it('renders the overlay slot outside all three landmarks (FR-LAY-03)', () => {
    // The prototype puts the hover card and the modal last, as siblings of the columns. Kept
    // there so an overlay can never land between two landmarks and disturb reading order.
    shell({ overlays: <p>OVERLAY</p> });
    const overlay = screen.getByText('OVERLAY');
    for (const role of ['navigation', 'main', 'complementary'] as const) {
      expect(screen.getByRole(role).contains(overlay)).toBe(false);
    }
  });

  it('keeps the root a plain <div>, not sectioning content', () => {
    // An <aside> descended from <section>/<article>/<nav>/<aside> maps to `generic`, not
    // `complementary` — the landmark disappears from getByRole and from a real screen reader
    // alike, with no other symptom. This assertion is the only thing that would notice.
    const { container } = shell();
    expect(container.firstElementChild?.tagName).toBe('DIV');
  });

  it('orders the landmarks nav -> main -> aside', () => {
    shell();
    const nav = screen.getByRole('navigation');
    const main = screen.getByRole('main');
    const aside = screen.getByRole('complementary');
    expect(nav.compareDocumentPosition(main) & FOLLOWS).toBeTruthy();
    expect(main.compareDocumentPosition(aside) & FOLLOWS).toBeTruthy();
  });
});

describe('FR-LAY-02 showStats', () => {
  it('renders the stats panel when true', () => {
    shell({ showStats: true, stats: <p>STATS</p> });
    expect(screen.getByRole('complementary')).not.toBeNull();
    expect(screen.getByText('STATS')).not.toBeNull();
  });

  it('UNMOUNTS the stats panel when false, rather than hiding it', () => {
    const { container } = shell({ showStats: false, stats: <p>STATS</p> });
    expect(screen.queryByRole('complementary')).toBeNull();
    // The load-bearing assertion. With `css: false` a class-hidden element is still "visible"
    // to the role query, so the line above passes on a `display: none` implementation too —
    // which would leave the 272px flex track in place and stop the chat column expanding.
    expect(screen.queryByText('STATS')).toBeNull();
    expect(container.querySelectorAll('aside')).toHaveLength(0);
  });

  it('leaves the other two regions untouched when false', () => {
    shell({ showStats: false, sidebar: <p>SIDEBAR</p>, chat: <p>CHAT</p> });
    expect(within(screen.getByRole('navigation')).getByText('SIDEBAR')).not.toBeNull();
    expect(within(screen.getByRole('main')).getByText('CHAT')).not.toBeNull();
  });
});

describe('the document heading (NFR-A11Y-03)', () => {
  it('is the only level-1 heading even when every slot contributes headings', () => {
    shell({ sidebar: <h2>Conversations</h2>, chat: <h2>A chat</h2>, stats: <h3>Session</h3> });
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('carries brandName', () => {
    shell({ brandName: 'Acme KB' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Acme KB');
  });

  it('stays in the accessibility tree', () => {
    // `.visually-hidden` hides it from sight only. `hidden` or `aria-hidden` would remove it
    // from the accessibility tree, leaving the document with no heading at all — which is
    // exactly the state this task exists to prevent.
    shell();
    const h1 = screen.getByRole('heading', { level: 1 });
    expect(h1.className).toContain('visually-hidden');
    expect(h1.hasAttribute('hidden')).toBe(false);
    expect(h1.getAttribute('aria-hidden')).toBeNull();
  });
});

describe('NFR-A11Y-04 bypass link', () => {
  it('targets the main landmark', () => {
    shell();
    const link = screen.getByRole('link', { name: 'Skip to chat' });
    const main = screen.getByRole('main');
    expect(link.getAttribute('href')).toBe(`#${main.id}`);
    expect(main.id).not.toBe('');
  });

  it('gives <main> a focus target that adds no tab stop', () => {
    // <main> is not focusable by default, so without this several engines send focus nowhere
    // and the link silently does nothing. -1 keeps it out of the tab order.
    shell();
    expect(screen.getByRole('main').getAttribute('tabindex')).toBe('-1');
  });

  it('precedes every landmark, so it is the first tab stop', () => {
    shell();
    const link = screen.getByRole('link', { name: 'Skip to chat' });
    expect(link.compareDocumentPosition(screen.getByRole('navigation')) & FOLLOWS).toBeTruthy();
  });
});

describe('R-58(5) — the shell does not consume theme', () => {
  it('renders with no ThemeProvider above it', () => {
    // `useTheme()` throws outside the provider (pinned by ThemeProvider.test.tsx), so a shell
    // that reached for theme context — the precedent R-58(5) forbids, because it would drag
    // the other nine FR-CST-01 fields through the shell in T-505..T-508 — fails here.
    expect(() => shell()).not.toThrow();
  });
});
