/**
 * FR-AUT-07's guard, end to end through the real `App`.
 *
 * `AuthProvider.test.tsx` proves the phase machine; this proves what the composition root does
 * with it — which is the requirement's actual content ("every GUI route except login requires
 * authentication"). The product has no router, so the enforceable form is that the shell is
 * **not mounted** while unauthenticated: hiding views while leaving them mounted would keep
 * their effects — T-508's document stream, and T-513's chat stream — running against a session
 * that does not exist.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Me } from '../api';
import type { AccessGrant, GrantResult } from './session';
import { SESSION_EXPIRED } from './copy';

const refreshSession = vi.fn<() => Promise<AccessGrant | null>>();
const fetchMe = vi.fn<() => Promise<Me | null>>();
const signInRequest = vi.fn<() => Promise<GrantResult>>();
const signOutRequest = vi.fn<() => Promise<void>>();
const changePasswordRequest = vi.fn();

vi.mock('./session', () => ({
  refreshSession: () => refreshSession(),
  fetchMe: () => fetchMe(),
  signInRequest: (...a: unknown[]) => signInRequest(...(a as [])),
  signOutRequest: () => signOutRequest(),
  changePasswordRequest: (...a: unknown[]) => changePasswordRequest(...a),
}));

/** T-508's store fetches on mount; the guard test is about whether it gets the chance to. */
const listDocuments = vi.fn(async () => []);
vi.mock('../kb/mutations', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listDocuments: () => listDocuments(),
}));
vi.mock('../kb/useDocumentStream', () => ({ useDocumentStream: () => 'idle' }));

/**
 * T-513's stores fetch on mount too, and for the same reason they are mocked here: this file
 * asks whether an unauthenticated app *calls the server at all*, so the transport has to be
 * observable rather than real.
 */
const conversationRow = (id: string, title: string) => ({
  id,
  title,
  archived: false,
  created_at: '2026-07-16T09:12:00Z',
  updated_at: id === 'chat-1' ? '2026-08-01T00:00:00Z' : '2026-07-01T00:00:00Z',
  message_count: 2,
});
const listConversations = vi.fn(async () => ({
  kind: 'ok' as const,
  data: [
    conversationRow('chat-1', 'Analyzing Market Trends'),
    conversationRow('chat-2', 'Pricing'),
  ],
}));
vi.mock('../chat/mutations', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listConversations: () => listConversations(),
  listMessages: async () => ({ kind: 'ok', data: [] }),
  getConversation: async (id: string) => ({
    kind: 'ok',
    data: {
      ...conversationRow(id, id === 'chat-1' ? 'Analyzing Market Trends' : 'Pricing'),
      context: {
        used_tokens: 0,
        limit_tokens: 10_400,
        remaining_tokens: 10_400,
        percent_used: 0,
        answer_reserve_tokens: 1_500,
      },
    },
  }),
}));
vi.mock('../chat/useChatStream', () => ({
  streamSend: async () => null,
  streamRegenerate: async () => null,
}));

const App = (await import('../App')).default;

const ME: Me = {
  id: 'user-1',
  email: 'maya.jensen@example.com',
  display_name: 'Maya Jensen',
  roles: ['user'],
  is_active: true,
};

const shell = () => screen.queryByRole('navigation', { name: 'Conversations' });
const loginCard = () => screen.queryByRole('button', { name: 'Sign in' });

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  fetchMe.mockResolvedValue(ME);
  signOutRequest.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.resetAllMocks();
});

describe('FR-AUT-07 — the shell requires authentication', () => {
  it('renders neither surface while the session is still unknown', () => {
    let resolve!: (value: AccessGrant | null) => void;
    refreshSession.mockReturnValue(new Promise((r) => (resolve = r)));
    render(<App />);
    // Not a spinner: the page is already painted in the right theme by the pre-paint script
    // (R-58(1)), and a splash that appears and vanishes inside 50ms is worse than nothing.
    expect(shell()).toBeNull();
    expect(loginCard()).toBeNull();
    act(() => resolve(null));
  });

  it('shows the login screen and mounts NO part of the shell when signed out', async () => {
    refreshSession.mockResolvedValue(null);
    render(<App />);
    await waitFor(() => expect(loginCard()).not.toBeNull());

    expect(shell()).toBeNull();
    // The shell's own landmarks, identified by the labels only it supplies. `queryByRole('main')`
    // used to stand in for this and no longer can: since T-511 the login screen is itself a
    // <main>, because a page with no landmark at all gives a screen-reader user nothing to
    // navigate to (axe: `landmark-one-main` + 8 × `region`). Asserting on the *labelled* shell
    // landmarks says what this test means instead of relying on the login screen having none.
    expect(screen.queryByRole('navigation', { name: 'Conversations' })).toBeNull();
    expect(screen.queryByRole('complementary')).toBeNull();
    // ...and there is exactly one <main> — the login screen's — never two.
    expect(screen.getAllByRole('main')).toHaveLength(1);
    // The substantive half: no authenticated request was even attempted.
    expect(listDocuments).not.toHaveBeenCalled();
  });

  it('shows the shell, and the real identity, once signed in', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    render(<App />);
    await waitFor(() => expect(shell()).not.toBeNull());
    expect(loginCard()).toBeNull();
    // FR-SBR-06 now reads `GET /auth/me`, not the prototype's sample identity.
    expect(screen.getByText('Maya Jensen')).not.toBeNull();
  });

  it('moves focus into the shell when it replaces the login screen (NFR-A11Y-04)', async () => {
    // T-511, measured live: signing in swaps two entirely different trees, so the element that
    // had focus is detached and `document.activeElement` falls back to <body> — leaving a
    // keyboard user at the top of the document with no signal, and a screen-reader user with no
    // cursor in the new page. <main> is the target because the skip link already uses it, so no
    // new focusable element exists and no pixel moves (the ring is `:focus-visible`-only).
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    render(<App />);
    await waitFor(() => expect(shell()).not.toBeNull());

    const main = screen.getByRole('main');
    await waitFor(() => expect(document.activeElement).toBe(main));
    expect(document.activeElement).not.toBe(document.body);
  });

  it('replaces the login screen with the shell on a successful sign-in', async () => {
    refreshSession.mockResolvedValue(null);
    signInRequest.mockResolvedValue({ ok: true, grant: { accessToken: 'a', expiresIn: 300 } });
    render(<App />);
    await waitFor(() => expect(loginCard()).not.toBeNull());

    await act(async () => {
      loginCard()!.click();
    });
    await waitFor(() => expect(shell()).not.toBeNull());
  });
});

describe('FR-AUT-06 — the expiry banner', () => {
  it('is absent on a first visit', async () => {
    refreshSession.mockResolvedValue(null);
    render(<App />);
    await waitFor(() => expect(loginCard()).not.toBeNull());
    expect(screen.queryByText(SESSION_EXPIRED)).toBeNull();
  });

  it('appears when an established session drops, and unmounts the shell with it', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    render(<App />);
    await waitFor(() => expect(shell()).not.toBeNull());

    // A 401 from any ordinary route — what `client.ts` reports.
    const { reportUnauthorized } = await import('../api/auth');
    act(() => reportUnauthorized());

    await waitFor(() => expect(screen.queryByText(SESSION_EXPIRED)).not.toBeNull());
    expect(shell()).toBeNull();
  });
});

describe('FR-AUT-06 (D) — the active chat after re-login (R-72(3))', () => {
  /** The FR-HDR-01 header, which follows the active conversation. Scoped to `main`: the
   *  sidebar's list label is an `<h2>` too, so an unscoped query matches two. */
  const header = () =>
    within(screen.getByRole('main')).getAllByRole('heading', { level: 2 })[0].textContent;

  /**
   * Opens a conversation that is NOT the default first row, so a restored pointer is
   * distinguishable from a reset one. Derived from the list rather than from a hardcoded
   * title, for the reason `App.test.tsx` records: adding seeds later must not silently stop
   * this reaching the state it asserts.
   */
  async function selectSecondConversation(): Promise<void> {
    const items = within(screen.getByRole('navigation', { name: 'Conversations' })).getAllByRole(
      'listitem',
    );
    const rowButton = within(items[1]).getAllByRole('button')[0];
    await act(async () => {
      rowButton.click();
    });
  }

  async function expireAndSignIn(): Promise<void> {
    const { reportUnauthorized } = await import('../api/auth');
    act(() => reportUnauthorized());
    await waitFor(() => expect(loginCard()).not.toBeNull());
    signInRequest.mockResolvedValue({ ok: true, grant: { accessToken: 'b', expiresIn: 300 } });
    await act(async () => {
      loginCard()!.click();
    });
    await waitFor(() => expect(shell()).not.toBeNull());
  }

  it('restores the conversation the user was in when the SAME user signs back in', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    render(<App />);
    // The shell mounts before `GET /conversations` answers, so the header reads the untitled
    // label for a frame. Waiting for the list is what makes `first` the *first conversation*
    // rather than that placeholder — and the reset returns to it, not to the placeholder.
    await waitFor(() => expect(header()).toBe('Analyzing Market Trends'));
    const first = header();
    await selectSecondConversation();
    const chosen = header();
    expect(chosen).not.toBe(first);

    await expireAndSignIn();
    expect(header()).toBe(chosen);
  });

  it('resets when a DIFFERENT user signs in on the expiry screen', async () => {
    // The half that matters. Without it a colleague signing in at the expiry screen lands on
    // the previous user's chat pointer — which 404s under R-54(1), but should never be tried.
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    render(<App />);
    // The shell mounts before `GET /conversations` answers, so the header reads the untitled
    // label for a frame. Waiting for the list is what makes `first` the *first conversation*
    // rather than that placeholder — and the reset returns to it, not to the placeholder.
    await waitFor(() => expect(header()).toBe('Analyzing Market Trends'));
    const first = header();
    await selectSecondConversation();
    expect(header()).not.toBe(first);

    fetchMe.mockResolvedValue({
      ...ME,
      id: 'user-2',
      email: 'sam@example.com',
      display_name: 'Sam Okafor',
    });
    await expireAndSignIn();

    expect(screen.getByText('Sam Okafor')).not.toBeNull();
    expect(header()).toBe(first);
  });
});
