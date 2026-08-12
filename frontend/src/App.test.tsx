/**
 * The composition root — the wiring `main.tsx` actually renders.
 *
 * `AppShell.test.tsx` proves the shell in isolation; this proves that the FR-SYS-04 defaults
 * resolve here, and that the shell is mounted *inside* `ThemeProvider` as R-58(5) requires.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import App from './App';

const root = () => document.documentElement;

beforeEach(() => {
  // The provider persists to localStorage and writes to the root element, both of which
  // outlive React's cleanup. Same reset as ThemeProvider.test.tsx.
  localStorage.clear();
  root().removeAttribute('data-theme');
  root().removeAttribute('style');
});

describe('FR-SYS-04 defaults (§9)', () => {
  it('renders the shell exactly as main.tsx does — no props at all', () => {
    render(<App />);
    expect(screen.getByRole('navigation', { name: 'Conversations' })).not.toBeNull();
    expect(screen.getByRole('main')).not.toBeNull();
    expect(screen.getByRole('complementary', { name: 'Session statistics' })).not.toBeNull();
  });

  it('defaults brandName to "Corpus"', () => {
    render(<App />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Corpus');
  });

  it('accepts a brandName override', () => {
    render(<App brandName="Acme KB" />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Acme KB');
  });

  it('defaults showStats to true and honours false (FR-LAY-02)', () => {
    const { unmount } = render(<App />);
    expect(screen.queryByRole('complementary')).not.toBeNull();
    unmount();

    render(<App showStats={false} />);
    expect(screen.queryByRole('complementary')).toBeNull();
  });
});

describe('FR-HDR-01 — the header follows the active conversation', () => {
  /** Drives the real FR-SBR-07 flow on the first row still present: ⋯ → Delete → confirm.
   *
   *  Deliberately not driven from a hardcoded list of seeded titles. It was, and adding two
   *  conversations in T-505 made the delete-everything test below stop reaching the state it
   *  asserts — while still passing its earlier steps, so it failed for a reason that had nothing
   *  to do with what it tests. Draining the list keeps it honest as the seeds change. */
  function deleteFirstConversation() {
    const actions = screen.queryAllByRole('button', { name: /^Actions for / });
    if (actions.length === 0) return false;
    fireEvent.click(actions[0]);
    fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: 'Delete' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
    return true;
  }

  const headerTitle = () => within(screen.getByRole('main')).getByRole('heading', { level: 2 });

  it('renders the seeded active chat’s title inside <main>', () => {
    render(<App />);
    expect(headerTitle().textContent).toBe('Analyzing Market Trends');
  });

  it('follows FR-SBR-04 selection', () => {
    render(<App />);
    // Anchored: the row button's name starts with the title, while the FR-SBR-07 affordance
    // beside it is "Actions for Product Launch Strategy" and would otherwise match too.
    fireEvent.click(screen.getByRole('button', { name: /^Product Launch Strategy/ }));
    expect(headerTitle().textContent).toBe('Product Launch Strategy');
  });

  it('shows the untitled label once the last conversation is deleted', () => {
    // The only state where no conversation is active. The prototype cannot reach it (its state
    // is an index into a non-empty array), so the rule is ours — and without the null branch in
    // App the header would crash or render an empty heading.
    render(<App />);
    // Bounded so a bug that stops the list shrinking fails here rather than hanging the suite.
    for (let i = 0; i < 50 && deleteFirstConversation(); i += 1);
    expect(screen.queryAllByRole('button', { name: /^Actions for / })).toHaveLength(0);
    expect(headerTitle().textContent).toBe('New chat');
  });
});

describe('§4.6 stats panel wiring (T-507)', () => {
  const stats = () => screen.getByRole('complementary', { name: 'Session statistics' });

  it('fills the FR-LAY-01 third region rather than leaving it empty', () => {
    render(<App />);
    expect(within(stats()).getByRole('heading', { name: 'SESSION' })).not.toBeNull();
    expect(within(stats()).getByRole('heading', { name: 'SOURCES REFERENCED' })).not.toBeNull();
  });

  it('reads the same usage the composer projects FR-STA-04 against', () => {
    // Two consumers, one object in App. If they ever drift apart the panel will tell a user a
    // conversation has room while the composer refuses their message — the OI-31 shape, one
    // surface over. `Vendor Security Review` is the seeded frozen chat.
    render(<App />);
    expect(within(stats()).getByText('0.2K / 10.4K')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /^Vendor Security Review/ }));
    expect(within(stats()).getByText('8.9K / 10.4K')).not.toBeNull();
    expect(within(stats()).getByText('86% used · 2K tokens remaining')).not.toBeNull();
    // …and the composer must be refusing at the same moment. Asserting the meter alone passes
    // against a composer still reading the roomy seed, which is precisely the disagreement
    // hoisting `usage` into one expression exists to make impossible.
    //
    // A draft has to be typed first: `sendBlock` returns `empty` before it ever considers the
    // budget, so an untouched composer is disabled either way and the assertion would be
    // vacuous — which is exactly how it read until the mutation check called the bluff.
    const send = () => screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'one more question' } });
    expect(send().disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /^Analyzing Market Trends/ }));
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'one more question' } });
    expect(send().disabled).toBe(false);
  });

  it('keeps FR-ANL-01’s duration session-scoped across a chat switch (R-14)', () => {
    // Everything else on the panel is per active chat; the duration is not. Owning the start in
    // App is what makes that true — and what stops FR-LAY-02's showStats toggle restarting it.
    vi.useFakeTimers();
    try {
      render(<App />);
      act(() => void vi.advanceTimersByTime(5_000));
      fireEvent.click(screen.getByRole('button', { name: /^Product Launch Strategy/ }));
      act(() => void vi.advanceTimersByTime(3_000));
      expect(within(stats()).getByText('00:08')).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('derives the message count and the sources from the active chat', () => {
    render(<App />);
    // The seeded market-trends chat: one question, one answer citing two documents.
    expect(within(stats()).getByText('2')).not.toBeNull();
    expect(within(stats()).getAllByRole('listitem')).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: /^Customer Persona Refinement/ }));
    expect(
      within(stats()).getByText('None yet — answers will list their sources here.'),
    ).not.toBeNull();
  });

  it('leaves the document with exactly one <h1> (T-502 owns it)', () => {
    render(<App />);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});

describe('theme wiring', () => {
  it('mounts the shell inside ThemeProvider (R-58(5))', () => {
    // The provider is the only thing that writes `data-theme`. If a refactor ever hoisted the
    // shell out of it — or dropped the provider on the way to adding T-509's login branch —
    // every token would fall back to the `<html data-theme="dark">` attribute in index.html
    // and the FR-HDR-03 toggle would silently stop working.
    render(<App />);
    expect(root().dataset.theme).toBe('dark');
  });

  it('introduces no accent default of its own (R-58(2))', () => {
    // §8.41 calls this the rule most likely to be silently broken by a later refactor, and the
    // composition root is where a refactor would reach for a "sensible default". Writing
    // FR-SYS-04's `#7C86F8` here would clobber the light theme's `#5B66E8` (NFR-VIS-02).
    render(<App />);
    expect(root().getAttribute('style')).toBeNull();
  });

  it('still applies an accent when one is supplied', () => {
    render(<App accent="#4EC3A6" />);
    expect(root().style.getPropertyValue('--accent')).toBe('#4EC3A6');
  });
});
