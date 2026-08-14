/**
 * The composition root — the wiring `main.tsx` actually renders.
 *
 * `AppShell.test.tsx` proves the shell in isolation; this proves that the FR-SYS-04 defaults
 * resolve here, that the shell is mounted *inside* `ThemeProvider` as R-58(5) requires, and
 * — since T-513 — that the four stores are joined up: one usage figure feeding two surfaces,
 * one document count feeding two more, and one `turnInFlight` feeding three.
 *
 * **Only the transport is mocked.** The real hooks, the real reducer and the real components
 * all run, so a test here fails when the wiring is wrong rather than when a mock is. The
 * fixtures come from `src/test/transcripts.ts`, which is where the §4.3 branch matrix lives.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { AuthContext } from './auth/AuthContext';
import type { AuthContextValue } from './auth/AuthContext';
import type { ChatFrame, Conversation, ConversationDetail, LinkStatus, Me, Message } from './api';
import { ROOMY_USAGE, TRANSCRIPT_FIXTURES } from './test/transcripts';
import type { SampleChat } from './test/transcripts';
import { readSource, stripTsComments } from './test/css-source';

/**
 * A signed-in session, supplied synchronously.
 *
 * The real `AuthProvider` boots by asking the server whether a refresh cookie exists (it is
 * httpOnly, so there is no other way to know — R-72(1)), which makes mounting the app
 * genuinely asynchronous. Every test below is about the *shell*, so it is mocked out at the
 * provider rather than the transport: that keeps these tests focused and, more to the point,
 * keeps them testing what they say they test. The phase machine and FR-AUT-07's guard have
 * their own file, `auth/guard.test.tsx`, which uses the real provider.
 */
const ME: Me = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'maya.jensen@example.com',
  display_name: 'Maya Jensen',
  roles: ['user'],
  is_active: true,
};

const SESSION: AuthContextValue = {
  phase: 'authenticated',
  user: ME,
  expired: false,
  signIn: vi.fn(async () => ({ ok: true }) as const),
  signOut: vi.fn(async () => undefined),
  changePassword: vi.fn(async () => ({ ok: true }) as const),
};

vi.mock('./auth/AuthProvider', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => (
    <AuthContext value={SESSION}>{children}</AuthContext>
  ),
}));

// --- the transport ------------------------------------------------------------

/** The fixture chats, as the conversations list would report them. */
const TITLES: Record<string, string> = {
  'sample-analyzing-market-trends': 'Analyzing Market Trends',
  'sample-product-launch-strategy': 'Product Launch Strategy',
  'sample-customer-persona-refinement': 'Customer Persona Refinement',
  'sample-pricing-experiment-review': 'Pricing Experiment Review',
  'sample-q4-forecast-draft': 'Q4 Forecast Draft',
  'sample-vendor-security-review': 'Vendor Security Review',
};
const IDS = Object.keys(TITLES);

/** Newest first, so the list arrives already in FR-SBR-03 order and stays there. */
const stamp = (index: number) => `2026-08-${String(20 - index).padStart(2, '0')}T09:00:00.000Z`;

/** Only rows the server would actually return: a degraded turn has none (R-54(3)). */
function serverMessages(chat: SampleChat | undefined): Message[] {
  return (chat?.entries ?? [])
    .map((entry) => entry.message)
    .filter((message): message is Message => 'created_at' in message);
}

function conversationRow(id: string): Conversation {
  const index = IDS.indexOf(id);
  return {
    id,
    title: TITLES[id] ?? 'New chat',
    archived: false,
    created_at: stamp(index < 0 ? IDS.length : index),
    updated_at: stamp(index < 0 ? -1 : index),
    message_count: serverMessages(TRANSCRIPT_FIXTURES[id]).length,
  };
}

function conversationDetail(id: string): ConversationDetail {
  return {
    ...conversationRow(id),
    context: TRANSCRIPT_FIXTURES[id]?.usage ?? ROOMY_USAGE,
  };
}

const listConversations = vi.fn();
const getConversation = vi.fn();
const listMessages = vi.fn();
const createConversation = vi.fn();
const renameConversation = vi.fn();
const deleteConversation = vi.fn();
const setFeedback = vi.fn();

vi.mock('./chat/mutations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./chat/mutations')>()),
  listConversations: (...a: unknown[]) => listConversations(...a),
  getConversation: (...a: unknown[]) => getConversation(...a),
  listMessages: (...a: unknown[]) => listMessages(...a),
  createConversation: (...a: unknown[]) => createConversation(...a),
  renameConversation: (...a: unknown[]) => renameConversation(...a),
  deleteConversation: (...a: unknown[]) => deleteConversation(...a),
  setFeedback: (...a: unknown[]) => setFeedback(...a),
}));

/** The stream, played by hand. */
let turn: { frames: (frame: ChatFrame) => void; finish: (failure: unknown) => void } | null = null;
const streamSend = vi.fn(
  (
    _id: string,
    _q: string,
    _docs: readonly string[],
    options: { onFrame: (frame: ChatFrame) => void },
  ) =>
    new Promise((resolve) => {
      turn = { frames: options.onFrame, finish: resolve };
    }),
);
vi.mock('./chat/useChatStream', () => ({
  streamSend: (...a: never[]) => (streamSend as never as (...x: never[]) => unknown)(...a),
  streamRegenerate: async () => null,
}));

/** T-508's surface: the modal opens, the set is legitimately empty with no documents seeded. */
vi.mock('./kb/mutations', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listDocuments: async () => [],
}));
vi.mock('./kb/useDocumentStream', () => ({ useDocumentStream: () => 'idle' }));

/** T-512's surface. `linked` is driven per test, because FR-KBM-06's button branches on it. */
const getLinkStatus = vi.fn(async (): Promise<LinkStatus> => ({
  provider: 'google',
  linked: true,
  account: 'maya@example.com',
}));
const startLink = vi.fn(async () => ({ url: 'https://keycloak.test/authorize' }));
const listCloudFiles = vi.fn(async () => ({
  kind: 'page' as const,
  files: [
    {
      file_id: 'f1',
      name: 'Drive report.pdf',
      mime_type: 'application/pdf',
      size_bytes: 1024,
      modified_time: null,
    },
  ],
  nextPageToken: null,
}));
vi.mock('./cloud/mutations', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getLinkStatus: () => getLinkStatus(),
  startLink: () => startLink(),
  listCloudFiles: (...a: unknown[]) => listCloudFiles(...(a as [])),
  unlinkAccount: async () => true,
}));

/** FR-SYS-03's configured id, which the MODEL card falls back to. */
vi.mock('./stats/useConfig', () => ({ useConfig: () => ({ chat_model: 'gpt-4o' }) }));

const App = (await import('./App')).default;

const root = () => document.documentElement;
const headerTitle = () => within(screen.getByRole('main')).getByRole('heading', { level: 2 });
const stats = () => screen.getByRole('complementary', { name: 'Session statistics' });

/**
 * Mount and wait for the list and the first transcript to land.
 *
 * The generous timeout is not padding: mounting the real tree resolves four stores across
 * several microtask turns, and the default 1 s was observed to lapse when this file ran beside
 * another jsdom suite — a flake that looks exactly like a wiring bug.
 */
const SETTLE = { timeout: 5_000 } as const;

/**
 * The header comes from the *conversations* store and the transcript from the *chat* store, so
 * waiting on the title alone leaves every later assertion racing the second one.
 *
 * The meter is the cheapest signal that the chat store has landed: `usage` is `null` for the one
 * round trip after activation and renders R-73(6)'s unread em dash, so a `N.NK / ` figure means
 * the conversation detail has arrived. Waiting on it here rather than in each test is what keeps
 * a wiring assertion from failing as a timing one — which is how it presented when T-512 added a
 * fifth store and shifted the mount by one microtask turn.
 */
async function chatLoaded() {
  await waitFor(() => expect(within(stats()).getByText(/K \/ 10\.4K/)).not.toBeNull(), SETTLE);
}

async function mounted(props: React.ComponentProps<typeof App> = {}) {
  const view = render(<App {...props} />);
  await waitFor(() => expect(headerTitle().textContent).toBe(TITLES[IDS[0]]), SETTLE);
  await chatLoaded();
  return view;
}

/** FR-SBR-04 — open a chat by its row, then wait for its transcript. */
async function open(title: string) {
  // Anchored: the row button's name starts with the title, while the FR-SBR-07 affordance
  // beside it is "Actions for <title>" and would otherwise match too.
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${title}`) }));
  await waitFor(() => expect(headerTitle().textContent).toBe(title), SETTLE);
  await chatLoaded();
}

beforeEach(() => {
  // The provider persists to localStorage and writes to the root element, both of which
  // outlive React's cleanup. Same reset as ThemeProvider.test.tsx.
  localStorage.clear();
  root().removeAttribute('data-theme');
  root().removeAttribute('style');

  turn = null;
  listConversations.mockReset().mockResolvedValue({ kind: 'ok', data: IDS.map(conversationRow) });
  getConversation
    .mockReset()
    .mockImplementation(async (id: string) => ({ kind: 'ok', data: conversationDetail(id) }));
  listMessages.mockReset().mockImplementation(async (id: string) => ({
    kind: 'ok',
    data: serverMessages(TRANSCRIPT_FIXTURES[id]),
  }));
  createConversation.mockReset();
  renameConversation.mockReset();
  deleteConversation.mockReset().mockResolvedValue({ kind: 'ok', data: undefined });
  setFeedback.mockReset();
  streamSend.mockClear();

  // The `?link=` return is read from the address bar at mount, so every test starts from a
  // clean URL or the previous one's outcome would leak into it.
  window.history.replaceState(null, '', '/');
  // `mockReset`, not `mockClear`: a test that overrides the linked state with
  // `mockResolvedValue` replaces the implementation permanently, and the next test would then
  // start from the previous one's account.
  getLinkStatus.mockReset().mockResolvedValue({
    provider: 'google',
    linked: true,
    account: 'maya@example.com',
  });
  startLink.mockReset().mockResolvedValue({ url: 'https://keycloak.test/authorize' });
  listCloudFiles.mockReset().mockResolvedValue({
    kind: 'page',
    files: [
      {
        file_id: 'f1',
        name: 'Drive report.pdf',
        mime_type: 'application/pdf',
        size_bytes: 1024,
        modified_time: null,
      },
    ],
    nextPageToken: null,
  });
});

describe('FR-SYS-04 defaults (§9)', () => {
  it('renders the shell exactly as main.tsx does — no props at all', async () => {
    await mounted();
    expect(screen.getByRole('navigation', { name: 'Conversations' })).not.toBeNull();
    expect(screen.getByRole('main')).not.toBeNull();
    expect(screen.getByRole('complementary', { name: 'Session statistics' })).not.toBeNull();
  });

  it('defaults brandName to "Corpus"', async () => {
    await mounted();
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Corpus');
  });

  it('accepts a brandName override', async () => {
    await mounted({ brandName: 'Acme KB' });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Acme KB');
  });

  it('defaults showStats to true and honours false (FR-LAY-02)', async () => {
    const { unmount } = await mounted();
    expect(screen.queryByRole('complementary')).not.toBeNull();
    unmount();
    render(<App showStats={false} />);
    expect(screen.queryByRole('complementary')).toBeNull();
  });
});

describe('the conversations list (FR-SBR-02/03/04/07)', () => {
  it('reads GET /conversations and selects the first row', async () => {
    await mounted();
    expect(listConversations).toHaveBeenCalled();
    expect(headerTitle().textContent).toBe('Analyzing Market Trends');
  });

  it('follows FR-SBR-04 selection, loading that chat’s transcript', async () => {
    await mounted();
    await open('Product Launch Strategy');
    expect(listMessages).toHaveBeenCalledWith('sample-product-launch-strategy');
  });

  it('FR-SBR-02 creates a chat on the server and selects the id it answers with', async () => {
    // No counter anywhere: the id is the server's, which is what makes the very next request
    // for that chat address something that exists.
    await mounted();
    createConversation.mockResolvedValue({
      kind: 'ok',
      data: { ...conversationRow('brand-new'), title: null, context: ROOMY_USAGE },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /New chat/ }));
    });

    expect(createConversation).toHaveBeenCalled();
    await waitFor(() => expect(headerTitle().textContent).toBe('New chat'));
  });

  it('shows the untitled label once the last conversation is deleted', async () => {
    // The only state where no conversation is active. The prototype cannot reach it (its state
    // is an index into a non-empty array), so the rule is ours — and without the null branch in
    // App the header would crash or render an empty heading.
    await mounted();
    // Bounded so a bug that stops the list shrinking fails here rather than hanging the suite.
    for (let i = 0; i < 50; i += 1) {
      const actions = screen.queryAllByRole('button', { name: /^Actions for / });
      if (actions.length === 0) break;
      fireEvent.click(actions[0]);
      fireEvent.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: 'Delete' }));
      await act(async () => {
        fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));
      });
    }
    expect(screen.queryAllByRole('button', { name: /^Actions for / })).toHaveLength(0);
    expect(headerTitle().textContent).toBe('New chat');
  });
});

describe('a turn, end to end (FR-CMP-03 / FR-MSG-05 / R-54(2))', () => {
  const send = () => screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
  const compose = (text: string) =>
    fireEvent.change(screen.getByRole('combobox'), { target: { value: text } });

  it('posts the query, shows the question and the dots, then renders the answer', async () => {
    await mounted();
    await open('Customer Persona Refinement');

    compose('What is the refund window?');
    await act(async () => {
      fireEvent.click(send());
    });

    expect(streamSend).toHaveBeenCalledWith(
      'sample-customer-persona-refinement',
      'What is the refund window?',
      [],
      expect.anything(),
    );
    // The user's own row is never on the stream, so this bubble is local until the reload.
    expect(screen.getByText('What is the refund window?')).not.toBeNull();
    expect(send().disabled).toBe(true);

    act(() =>
      turn!.frames({
        event: 'message',
        data: {
          outcome: 'answered',
          error_code: null,
          message: {
            id: 'answer-1',
            role: 'ai',
            segs: [{ text: 'Thirty days.' }],
            created_at: '2026-08-20T09:00:10Z',
          },
        },
      }),
    );
    expect(screen.getByText('Thirty days.')).not.toBeNull();

    await act(async () => turn!.finish(null));
    // The dots stop and a fresh draft is sendable again. Asserted on a *new* draft because
    // FR-CMP-03 clears the box on send, and `sendBlock` returns `empty` before it considers
    // anything else — so an untouched composer is disabled either way and the check would be
    // vacuous (the trap §8.57's mutation pass found one card over).
    compose('and another');
    await waitFor(() => expect(send().disabled).toBe(false));
  });

  it('pauses the KB modal’s verbs while the turn runs, and un-pauses after (OI-31)', async () => {
    // The OI-31 binding end to end: `turnInFlight` is named once in App and reaches FR-MSG-05's
    // dots, FR-CMP-03's Send guard and — here — FR-KBM-07's four verbs, which the modal renders
    // as its own paused notice. Three surfaces, one fact.
    await mounted();
    await open('Customer Persona Refinement');
    fireEvent.click(screen.getByRole('button', { name: /Knowledge base/ }));
    const modal = () => screen.getByRole('dialog', { name: 'Knowledge base' });
    const paused = () => within(modal()).queryByText(/response is generating|paused/i);
    expect(paused()).toBeNull();

    compose('hello');
    await act(async () => {
      fireEvent.click(send());
    });
    expect(paused()).not.toBeNull();

    await act(async () => turn!.finish(null));
    await waitFor(() => expect(paused()).toBeNull());
  });
});

describe('§4.6 stats panel wiring (T-507)', () => {
  it('fills the FR-LAY-01 third region rather than leaving it empty', async () => {
    await mounted();
    expect(within(stats()).getByRole('heading', { name: 'SESSION' })).not.toBeNull();
    expect(within(stats()).getByRole('heading', { name: 'SOURCES REFERENCED' })).not.toBeNull();
  });

  it('reads the same usage the composer projects FR-STA-04 against', async () => {
    // Two consumers, one object in App. If they ever drift apart the panel will tell a user a
    // conversation has room while the composer refuses their message — the two-copies-of-one-
    // state shape R-71(1) rules on, one surface over. Both numbers now come from the same
    // `ConversationDetailResponse`, which makes the guarantee the server's rather than ours.
    await mounted();
    expect(within(stats()).getByText('0.2K / 10.4K')).not.toBeNull();

    await open('Vendor Security Review');
    expect(within(stats()).getByText('8.9K / 10.4K')).not.toBeNull();
    expect(within(stats()).getByText('86% used · 2K tokens remaining')).not.toBeNull();
    // …and the composer must be refusing at the same moment. Asserting the meter alone passes
    // against a composer still reading another chat's figure, which is precisely the
    // disagreement hoisting `usage` into one expression exists to make impossible.
    //
    // A draft has to be typed first: `sendBlock` returns `empty` before it ever considers the
    // budget, so an untouched composer is disabled either way and the assertion would be
    // vacuous — which is exactly how it read until the mutation check called the bluff.
    const send = () => screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'one more question' } });
    expect(send().disabled).toBe(true);

    await open('Analyzing Market Trends');
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'one more question' } });
    expect(send().disabled).toBe(false);
  });

  it('renders no meter until the server has been read (T-513)', async () => {
    // `{used: 0, limit: 0}` would be worse than nothing: `percentUsed` answers 100 for a zero
    // limit, so the bar would flash **full** — the frozen state, inverted.
    getConversation.mockImplementation(() => new Promise(() => {}));
    render(<App />);
    await waitFor(() => expect(within(stats()).queryByText(/K \/ /)).toBeNull());
    expect(within(stats()).getByText('Reading the conversation…')).not.toBeNull();
  });

  it('keeps FR-ANL-01’s duration session-scoped across a chat switch (R-14)', async () => {
    // Everything else on the panel is per active chat; the duration is not. Owning the start in
    // App is what makes that true — and what stops FR-LAY-02's showStats toggle restarting it.
    //
    // Fake timers from before the mount, because the ticker's interval is created during it;
    // installing them afterwards leaves a real interval nothing can advance. `…Async` so the
    // activation reads, which are promises, still settle.
    vi.useFakeTimers();
    try {
      render(<App />);
      await act(async () => void (await vi.advanceTimersByTimeAsync(5_000)));
      fireEvent.click(screen.getByRole('button', { name: /^Product Launch Strategy/ }));
      await act(async () => void (await vi.advanceTimersByTimeAsync(3_000)));
      expect(within(stats()).getByText('00:08')).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('derives the message count and the sources from the active chat', async () => {
    await mounted();
    // The market-trends fixture: one question, one answer citing two documents.
    expect(within(stats()).getByText('2')).not.toBeNull();
    expect(within(stats()).getAllByRole('listitem')).toHaveLength(2);

    await open('Customer Persona Refinement');
    expect(
      within(stats()).getByText('None yet — answers will list their sources here.'),
    ).not.toBeNull();
  });

  it('falls back to the configured model id for a chat with no answer (FR-SYS-03)', async () => {
    await mounted();
    await open('Customer Persona Refinement');
    expect(within(stats()).getByText('gpt-4o')).not.toBeNull();
  });

  it('leaves the document with exactly one <h1> (T-502 owns it)', async () => {
    await mounted();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});

describe('FR-KBM-01 — the two entry points', () => {
  const modal = () => screen.queryByRole('dialog', { name: 'Knowledge base' });

  it('opens from FR-SBR-05’s sidebar button', async () => {
    await mounted();
    expect(modal()).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Knowledge base/ }));
    expect(modal()).not.toBeNull();
  });

  it('opens from FR-CMP-02’s + button, and closes again', async () => {
    await mounted();
    fireEvent.click(screen.getByRole('button', { name: 'Add documents' }));
    expect(modal()).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Close knowledge base' }));
    expect(modal()).toBeNull();
  });

  it('closes the @-mention menu from EITHER entry point', async () => {
    // The `+` closes it directly (FR-CMP-05), but FR-KBM-01 says either one does — and the
    // sidebar button is in a component the composer never hears from, so without an explicit
    // signal the menu would still be open underneath the overlay and reappear on close.
    // Asserted on the menu's heading rather than its listbox: with no documents the set is
    // legitimately empty, and `MentionMenu` renders its FR-CMP-04 empty state in place of the
    // listbox — so a `queryByRole('listbox')` here would be `null` either way.
    await mounted();
    fireEvent.click(screen.getByRole('button', { name: 'Reference a document' }));
    expect(screen.queryByText('REFERENCE A DOCUMENT')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /Knowledge base/ }));
    expect(screen.queryByText('REFERENCE A DOCUMENT')).toBeNull();
  });

  it('shows FR-SBR-05 and FR-CMP-06 the SAME count', async () => {
    // FR-CMP-06 requires it in as many words, and the two used to be fed by a literal `5` and a
    // seeded array's length — agreeing only by coincidence. One derivation is what guarantees it.
    await mounted();
    const sidebarCount = screen
      .getByRole('button', { name: /Knowledge base/ })
      .textContent?.replace('Knowledge base', '')
      .trim();
    expect(
      screen.getByText(`Responses grounded in ${sidebarCount} documents · Enter to send`),
    ).not.toBeNull();
  });
});

describe('theme wiring', () => {
  it('mounts the shell inside ThemeProvider (R-58(5))', async () => {
    // The provider is the only thing that writes `data-theme`. If a refactor ever hoisted the
    // shell out of it — or dropped the provider on the way to adding T-509's login branch —
    // every token would fall back to the `<html data-theme="dark">` attribute in index.html
    // and the FR-HDR-03 toggle would silently stop working.
    await mounted();
    expect(root().dataset.theme).toBe('dark');
  });

  it('introduces no accent default of its own (R-58(2))', async () => {
    // §8.41 calls this the rule most likely to be silently broken by a later refactor, and the
    // composition root is where a refactor would reach for a "sensible default". Writing
    // FR-SYS-04's `#7C86F8` here would clobber the light theme's `#5B66E8` (NFR-VIS-02).
    await mounted();
    expect(root().getAttribute('style')).toBeNull();
  });

  it('still applies an accent when one is supplied', async () => {
    await mounted({ accent: '#4EC3A6' });
    expect(root().style.getPropertyValue('--accent')).toBe('#4EC3A6');
  });
});

describe('the OI-31 binding is named once (R-71(1))', () => {
  it('App reads turnInFlight from the chat store and hands THAT to useDocuments', () => {
    // A source guard, on the `Composer` FR-CMP-05 precedent. The behavioural test above proves
    // the modal pauses; this is what stops a later refactor giving `useDocuments` a second,
    // plausible-looking signal — which is exactly the two-copies-of-one-state defect OI-31 was
    // about, and which would still pass every rendering test on the day it was introduced.
    const code = stripTsComments(readSource('src/App.tsx'));
    expect(code).toMatch(/const turnInFlight = chat\.turnInFlight;/);
    expect(code).toMatch(/useDocuments\(\{[^}]*turnInFlight,/s);
    // And nothing else may compute one.
    expect(code).not.toMatch(/turnInFlight[:=][^;]*(entries|role === 'user'|typing)/);
  });
});

describe('FR-KBM-06 / FR-AUT-11 — the cloud-import entry point (T-512)', () => {
  const cloudButton = () => screen.getByRole('button', { name: 'Add from cloud drive' });
  const openKb = async () => {
    fireEvent.click(screen.getByRole('button', { name: /^Knowledge base/ }));
    await waitFor(() => expect(screen.getByRole('dialog')).not.toBeNull());
  };

  it('opens the picker for a linked account', async () => {
    await mounted();
    await openKb();
    fireEvent.click(cloudButton());

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Import from Google Drive' })).not.toBeNull(),
    );
    // The picker is NESTED — the KB modal stays mounted behind it, because the file being
    // imported appears in its lists.
    expect(screen.getAllByRole('dialog')).toHaveLength(2);
    await waitFor(() => expect(screen.getByText('Drive report.pdf')).not.toBeNull());
  });

  it('starts linking instead, for an account that has never been linked', async () => {
    // jsdom has no navigation, and `beginLink` genuinely calls `location.assign` — without this
    // the run prints "Not implemented: navigation to another Document" and the assertion below
    // would be testing around a logged error rather than a clean path.
    // `stubGlobal`, not `spyOn`: jsdom declares `location.assign` non-configurable, so a spy
    // throws "Cannot redefine property".
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    getLinkStatus.mockResolvedValue({ provider: 'google', linked: false, account: null });
    await mounted();
    await openKb();
    fireEvent.click(cloudButton());

    // FR-KBM-06: "when the user has not yet linked an account it initiates linking rather than
    // opening the selection surface."
    await waitFor(() => expect(startLink).toHaveBeenCalled());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://keycloak.test/authorize'));
    expect(screen.queryByRole('heading', { name: 'Import from Google Drive' })).toBeNull();
    // And it must not have spent the shared rate limit listing a Drive it cannot read.
    expect(listCloudFiles).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('does not list the Drive until the picker is opened', async () => {
    await mounted();
    await openKb();
    // The list route spends a third party's quota against a limit shared with import, so it
    // must not run for a surface nobody has asked for.
    expect(listCloudFiles).not.toHaveBeenCalled();
  });

  it('closes the picker with the modal, so it does not reappear unasked', async () => {
    await mounted();
    await openKb();
    fireEvent.click(cloudButton());
    await waitFor(() => expect(screen.getAllByRole('dialog')).toHaveLength(2));

    fireEvent.click(screen.getByRole('button', { name: 'Close knowledge base' }));
    await waitFor(() => expect(screen.queryAllByRole('dialog')).toHaveLength(0));

    await openKb();
    expect(screen.queryByRole('heading', { name: 'Import from Google Drive' })).toBeNull();
  });
});

describe('FR-AUT-11 — the ?link= return (T-512)', () => {
  it('reopens the modal AND the picker after a successful link', async () => {
    window.history.replaceState(null, '', '/?link=linked&provider=google');
    await mounted();

    // The user pressed a button, was sent to Google and came back on a cold page load; landing
    // them on a plain app would lose that thread.
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Import from Google Drive' })).not.toBeNull(),
    );
    expect(screen.getAllByRole('dialog')).toHaveLength(2);
  });

  it('re-reads the link status, which was read before the redirect', async () => {
    window.history.replaceState(null, '', '/?link=linked&provider=google');
    await mounted();
    // Without the refresh the status still says "not linked" and the button would send the user
    // round the consent flow a second time.
    await waitFor(() => expect(getLinkStatus.mock.calls.length).toBeGreaterThan(1));
  });

  it.each(['failed', 'denied'])('reopens the modal alone after %s', async (outcome) => {
    window.history.replaceState(null, '', `/?link=${outcome}&provider=google`);
    await mounted();

    await waitFor(() => expect(screen.getByRole('dialog')).not.toBeNull());
    expect(screen.queryByRole('heading', { name: 'Import from Google Drive' })).toBeNull();
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });

  it.each([
    ['linked', 'Google Drive connected.'],
    ['failed', 'Connecting Google Drive did not complete.'],
    ['denied', 'Connecting Google Drive was cancelled.'],
  ])('announces %s politely (NFR-A11Y-05)', async (outcome, copy) => {
    window.history.replaceState(null, '', `/?link=${outcome}&provider=google`);
    await mounted();
    // The page has just reloaded, so a screen-reader user has no transition to infer the
    // outcome from — which is exactly what a live region is for.
    await waitFor(() => expect(screen.getByText(copy)).not.toBeNull());
  });

  it('strips the query string, so a refresh cannot re-fire the return', async () => {
    window.history.replaceState(null, '', '/?link=linked&provider=google');
    await mounted();
    await waitFor(() => expect(window.location.search).toBe(''));
  });

  it('opens nothing on an ordinary load', async () => {
    await mounted();
    expect(screen.queryAllByRole('dialog')).toHaveLength(0);
  });

  it('opens nothing for a value outside the closed vocabulary', async () => {
    // The vocabulary is three words wide (§9). A stray URL must not open a surface.
    window.history.replaceState(null, '', '/?link=maybe');
    await mounted();
    expect(screen.queryAllByRole('dialog')).toHaveLength(0);
  });
});
