/**
 * The store's stateful half — what it *remembers* between the transport and the reducer.
 *
 * Both transport modules are mocked (`mutations.test.ts` covers what each status means,
 * `useChatStream.test.ts` covers the wire), so what is left is the sequencing: when the turn
 * flag clears, which GETs run and how many, and what a refusal leaves on screen.
 *
 * **The deadlock matrix is the reason this file exists.** `turnInFlight` gates the composer,
 * FR-MSG-08's action bar and — through `App` — the KB modal's four verbs, and it clears in one
 * `finally` covering four different exits. Deleting that line leaves a suite green everywhere
 * except here.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChatFrame, ContextWindow, Message } from '../api';

const listMessages = vi.fn();
const getConversation = vi.fn();
const setFeedbackCall = vi.fn();
const deleteConversationCall = vi.fn();

vi.mock('./mutations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./mutations')>()),
  listMessages: (...a: unknown[]) => listMessages(...a),
  getConversation: (...a: unknown[]) => getConversation(...a),
  setFeedback: (...a: unknown[]) => setFeedbackCall(...a),
  deleteConversation: (...a: unknown[]) => deleteConversationCall(...a),
}));

/** The stream, played by hand: the test decides which frames arrive and how it ends. */
let sendCall: { frames: (frame: ChatFrame) => void; finish: (failure: unknown) => void } | null =
  null;
const streamSend = vi.fn(
  (
    _id: string,
    _q: string,
    _docs: readonly string[],
    options: { onFrame: (f: ChatFrame) => void },
  ) =>
    new Promise((resolve) => {
      sendCall = { frames: options.onFrame, finish: resolve };
    }),
);
const streamRegenerate = vi.fn(
  (_id: string, options: { onFrame: (f: ChatFrame) => void }) =>
    new Promise((resolve) => {
      sendCall = { frames: options.onFrame, finish: resolve };
    }),
);

vi.mock('./useChatStream', () => ({
  streamSend: (...a: never[]) => (streamSend as never as (...x: never[]) => unknown)(...a),
  streamRegenerate: (...a: never[]) =>
    (streamRegenerate as never as (...x: never[]) => unknown)(...a),
}));

const { useChat, EVAL_REFRESH_DELAYS_MS } = await import('./useChat');

const CHAT = 'c1';

function ai(id: string, text: string, over: Partial<Message> = {}): Message {
  return {
    id,
    role: 'ai',
    segs: [{ text }],
    created_at: '2026-08-13T09:00:00Z',
    ...over,
  };
}

function usage(used = 240): ContextWindow {
  return {
    used_tokens: used,
    limit_tokens: 10_400,
    remaining_tokens: 10_400 - used,
    percent_used: (used / 10_400) * 100,
    answer_reserve_tokens: 1_500,
  };
}

function detail(over: Partial<{ used: number; messageCount: number }> = {}) {
  return {
    kind: 'ok' as const,
    data: {
      id: CHAT,
      title: 'A chat',
      archived: false,
      created_at: '2026-08-13T08:00:00Z',
      updated_at: '2026-08-13T09:00:00Z',
      message_count: over.messageCount ?? 2,
      context: usage(over.used),
    },
  };
}

const ANSWER_FRAME: ChatFrame = {
  event: 'message',
  data: { outcome: 'answered', error_code: null, message: ai('a1', 'the answer') },
};

beforeEach(() => {
  sendCall = null;
  listMessages.mockReset().mockResolvedValue({ kind: 'ok', data: [] });
  getConversation.mockReset().mockResolvedValue(detail());
  setFeedbackCall.mockReset();
  deleteConversationCall.mockReset();
  streamSend.mockClear();
  streamRegenerate.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

const mount = (over: Partial<Parameters<typeof useChat>[0]> = {}) =>
  renderHook(() => useChat({ conversationId: CHAT, enabled: true, ...over }));

/** Wait for the activation reads to settle so a test's own counts start from zero. */
async function activated(over: Partial<Parameters<typeof useChat>[0]> = {}) {
  const view = mount(over);
  await waitFor(() => expect(view.result.current.loaded).toBe(true));
  listMessages.mockClear();
  getConversation.mockClear();
  return view;
}

// --- activation ---------------------------------------------------------------

describe('activation', () => {
  it('reads the transcript and the meter together', async () => {
    listMessages.mockResolvedValue({ kind: 'ok', data: [ai('a1', 'hello')] });
    const { result } = mount();

    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    expect(result.current.usage).toEqual(usage());
    expect(listMessages).toHaveBeenCalledWith(CHAT);
    expect(getConversation).toHaveBeenCalledWith(CHAT);
  });

  it('fires nothing while the session is still resuming', async () => {
    // A hook cannot be called conditionally, so this runs during the `starting` phase — and an
    // unauthenticated 401 would reach FR-AUT-07's handler and sign out the very user whose
    // session was coming back (§8.59).
    mount({ enabled: false });
    await Promise.resolve();
    expect(listMessages).not.toHaveBeenCalled();
    expect(getConversation).not.toHaveBeenCalled();
  });

  it('fires nothing with no active conversation', async () => {
    mount({ conversationId: null });
    await Promise.resolve();
    expect(listMessages).not.toHaveBeenCalled();
  });

  it('reports a 404 once and drops the chat', async () => {
    const onMissing = vi.fn();
    listMessages.mockResolvedValue({ kind: 'gone' });
    getConversation.mockResolvedValue({ kind: 'gone' });
    mount({ onMissing });
    await waitFor(() => expect(onMissing).toHaveBeenCalledWith(CHAT));
  });
});

// --- the send lifecycle -------------------------------------------------------

describe('a send', () => {
  it('shows the bubble and the dots synchronously', async () => {
    const { result } = await activated();
    act(() => result.current.send('hello', []));

    // Synchronously: the user pressed a button and the UI must answer in that frame, not after
    // a round trip.
    expect(result.current.turnInFlight).toBe(true);
    expect(result.current.typing).toBe(true);
    expect(result.current.entries).toHaveLength(1);
    expect(streamSend).toHaveBeenCalledWith(CHAT, 'hello', [], expect.anything());
  });

  it('renders the answer beside the question, then reloads both', async () => {
    const { result } = await activated();
    act(() => result.current.send('hello', []));
    act(() => sendCall!.frames(ANSWER_FRAME));
    expect(result.current.entries).toHaveLength(2);

    listMessages.mockResolvedValue({
      kind: 'ok',
      data: [ai('u1', 'hello'), ai('a1', 'the answer')],
    });
    await act(async () => sendCall!.finish(null));

    // Exactly two: the transcript (the only source of the user row's real id — the stream never
    // carries it) and the meter (which the client cannot recompute).
    await waitFor(() => expect(listMessages).toHaveBeenCalledTimes(1));
    expect(getConversation).toHaveBeenCalledTimes(1);
    expect(result.current.turnInFlight).toBe(false);
  });

  it('tells the caller what the sidebar row should now say', async () => {
    const onTurnSettled = vi.fn();
    const { result } = await activated({ onTurnSettled });
    getConversation.mockResolvedValue(detail({ messageCount: 4 }));
    act(() => result.current.send('hello', []));
    await act(async () => sendCall!.finish(null));

    await waitFor(() =>
      expect(onTurnSettled).toHaveBeenCalledWith(CHAT, {
        updatedAt: '2026-08-13T09:00:00Z',
        messageCount: 4,
      }),
    );
  });

  it('ignores stage frames entirely', async () => {
    // R-54(2) makes them content-free by construction and §4.3 has no §9 string for a stage
    // name, so storing one would be state nothing renders.
    const { result } = await activated();
    act(() => result.current.send('hello', []));
    act(() => sendCall!.frames({ event: 'stage', data: { stage: 'retrieving' } }));
    expect(result.current.entries).toHaveLength(1);
  });

  it('does nothing with no active conversation', () => {
    const { result } = mount({ conversationId: null });
    act(() => result.current.send('hello', []));
    expect(streamSend).not.toHaveBeenCalled();
  });
});

// --- THE DEADLOCK MATRIX ------------------------------------------------------

describe('the turn always clears (§8.58)', () => {
  it.each([
    ['done', null],
    ['a 429', { kind: 'throttled', detail: 'Slow down.', retryAfter: '9' }],
    ['a 404', { kind: 'gone' }],
    ['a network failure', { kind: 'refused', detail: 'offline', status: 0 }],
    ['a budget refusal', { kind: 'frozen', usedTokens: 9_100, limitTokens: 10_400 }],
  ])('after %s', async (_label, failure) => {
    const { result } = await activated();
    act(() => result.current.send('hello', []));
    expect(result.current.turnInFlight).toBe(true);

    await act(async () => sendCall!.finish(failure));

    // If this clears only on the happy path, one 429 disables the composer AND the KB modal's
    // four verbs for the rest of the session — the flag gates the actions whose success would
    // clear it.
    await waitFor(() => expect(result.current.turnInFlight).toBe(false));
    expect(result.current.typing).toBe(false);
  });

  it('after unmount, without writing to a dead component', async () => {
    const { result, unmount } = await activated();
    act(() => result.current.send('hello', []));
    unmount();
    await act(async () => sendCall!.finish(null));
    // Nothing to assert on the store — the assertion is that React logs no update-after-unmount
    // and that the post-turn reload did not fire.
    expect(listMessages).not.toHaveBeenCalled();
  });
});

// --- what a refusal leaves on screen ------------------------------------------

describe('refusals', () => {
  it('a 429 renders the server copy as a degraded answer, keeping the question', async () => {
    const { result } = await activated();
    act(() => result.current.send('hello', []));
    await act(async () =>
      sendCall!.finish({ kind: 'throttled', detail: 'Slow down.', retryAfter: null }),
    );

    const texts = result.current.entries.map((entry) =>
      entry.message.segs.map((seg) => ('text' in seg ? seg.text : '')).join(''),
    );
    expect(texts).toEqual(['hello', 'Slow down.']);
  });

  it('a budget refusal renders NOTHING and re-reads the meter instead', async () => {
    // R-51(6)/R-69(1) — the composer blocks before a request exists and FR-STA-04's own line is
    // the source for both sides. Reaching this response means our usage was stale, so the fix
    // is a number, not a message.
    const { result } = await activated();
    getConversation.mockResolvedValue(detail({ used: 9_100 }));
    act(() => result.current.send('hello', []));
    await act(async () =>
      sendCall!.finish({ kind: 'frozen', usedTokens: 9_100, limitTokens: 10_400 }),
    );

    await waitFor(() => expect(result.current.usage?.used_tokens).toBe(9_100));
    expect(result.current.entries).toEqual([]);
    expect(result.current.frozen).toBe(true);
  });

  it('a 401 renders nothing — the transport already reported the session', async () => {
    const { result } = await activated();
    act(() => result.current.send('hello', []));
    await act(async () => sendCall!.finish({ kind: 'unauthorized' }));
    expect(result.current.entries.map((e) => e.message.role)).toEqual(['user']);
  });
});

// --- regenerate ---------------------------------------------------------------

describe('regenerate', () => {
  beforeEach(() => {
    listMessages.mockResolvedValue({
      kind: 'ok',
      data: [ai('u1', 'q', { role: 'user' }), ai('a1', 'first', { feedback: 'up' })],
    });
  });

  it('replaces the answer in place', async () => {
    const { result } = await activated();
    act(() => result.current.regenerate('a1'));
    act(() =>
      sendCall!.frames({
        event: 'message',
        data: { outcome: 'answered', error_code: null, message: ai('a1', 'second') },
      }),
    );

    const texts = result.current.entries.map((entry) =>
      entry.message.segs.map((seg) => ('text' in seg ? seg.text : '')).join(''),
    );
    expect(texts).toEqual(['q', 'second']);
  });

  it('a failure leaves the answer and its rating, and adds the copy beneath', async () => {
    const { result } = await activated();
    act(() => result.current.regenerate('a1'));
    await act(async () =>
      sendCall!.finish({ kind: 'refused', detail: 'The model is unavailable.', status: 503 }),
    );

    const texts = result.current.entries.map((entry) =>
      entry.message.segs.map((seg) => ('text' in seg ? seg.text : '')).join(''),
    );
    expect(texts).toEqual(['q', 'first', 'The model is unavailable.']);
  });

  it('NOT_LATEST_ANSWER shows the server copy and reloads', async () => {
    const { result } = await activated();
    act(() => result.current.regenerate('a1'));
    await act(async () =>
      sendCall!.finish({ kind: 'stale', detail: 'Only the most recent answer…' }),
    );

    await waitFor(() => expect(listMessages).toHaveBeenCalled());
    const texts = result.current.entries.map((entry) =>
      entry.message.segs.map((seg) => ('text' in seg ? seg.text : '')).join(''),
    );
    expect(texts).toContain('Only the most recent answer…');
  });
});

// --- feedback -----------------------------------------------------------------

describe('feedback', () => {
  beforeEach(() => {
    listMessages.mockResolvedValue({ kind: 'ok', data: [ai('a1', 'answer')] });
  });

  it('writes optimistically and adopts the 200 body', async () => {
    setFeedbackCall.mockResolvedValue({ kind: 'ok', data: ai('a1', 'answer', { feedback: 'up' }) });
    const { result } = await activated();

    act(() => result.current.feedback('a1', 'up'));
    const rated = result.current.entries[0].message;
    expect('created_at' in rated && rated.feedback).toBe('up');
    await waitFor(() => expect(setFeedbackCall).toHaveBeenCalledWith('a1', 'up'));
  });

  it('reverts on failure and adds NO row', async () => {
    // A failed thumb must never become a transcript row — the asymmetry with a failed turn is
    // deliberate: nobody asked a question here, so the control springing back is the message.
    setFeedbackCall.mockResolvedValue({ kind: 'refused', detail: 'nope', status: 500 });
    const { result } = await activated();

    act(() => result.current.feedback('a1', 'down'));
    await waitFor(() => {
      const row = result.current.entries[0].message;
      expect('created_at' in row && row.feedback).toBeFalsy();
    });
    expect(result.current.entries).toHaveLength(1);
  });

  it('sends null as the third state, never an omission', async () => {
    setFeedbackCall.mockResolvedValue({ kind: 'ok', data: ai('a1', 'answer') });
    const { result } = await activated();
    act(() => result.current.feedback('a1', null));
    await waitFor(() => expect(setFeedbackCall).toHaveBeenCalledWith('a1', null));
  });
});

// --- the eval refresh ---------------------------------------------------------

describe('the DeepEval refresh (R-50)', () => {
  it('looks again after the turn, and stops once a score lands', async () => {
    vi.useFakeTimers();
    const { result } = mount();
    await vi.waitFor(() => expect(result.current.loaded).toBe(true));
    act(() => result.current.send('hello', []));
    // The post-turn reload is what identifies the answer to watch; a turn whose reload shows
    // no AI row has nothing a judge will ever score.
    listMessages.mockResolvedValue({ kind: 'ok', data: [ai('a1', 'the answer')] });
    await act(async () => sendCall!.finish(null));

    listMessages.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EVAL_REFRESH_DELAYS_MS[0] + 10);
    });
    expect(listMessages).toHaveBeenCalledTimes(1);

    // Now the judge has answered — no further attempts.
    listMessages.mockResolvedValue({
      kind: 'ok',
      data: [ai('a1', 'the answer', { evaluation: { relevancy: 0.94, faithfulness: 0.97 } })],
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EVAL_REFRESH_DELAYS_MS[1] + 10);
    });
    listMessages.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EVAL_REFRESH_DELAYS_MS[2] + 10);
    });
    expect(listMessages).not.toHaveBeenCalled();
  });

  it('schedules nothing when the turn produced no answer to score', async () => {
    vi.useFakeTimers();
    const { result } = mount();
    await vi.waitFor(() => expect(result.current.loaded).toBe(true));
    act(() => result.current.send('hello', []));
    await act(async () => sendCall!.finish({ kind: 'refused', detail: 'x', status: 500 }));

    listMessages.mockClear();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(EVAL_REFRESH_DELAYS_MS[2] + 100);
    });
    expect(listMessages).not.toHaveBeenCalled();
  });
});

// --- deletion -----------------------------------------------------------------

describe('remove (FR-SBR-07)', () => {
  it('resolves null on a 204', async () => {
    deleteConversationCall.mockResolvedValue({ kind: 'ok', data: undefined });
    const { result } = await activated();
    await expect(result.current.remove(CHAT)).resolves.toBeNull();
  });

  it('resolves null on a 404 — already gone is deleted', async () => {
    deleteConversationCall.mockResolvedValue({ kind: 'gone' });
    const { result } = await activated();
    await expect(result.current.remove(CHAT)).resolves.toBeNull();
  });

  it('returns the server copy on a 503, and the chat survives', async () => {
    // R-54(5) — the thread purge is attempted before the commit, so nothing was deleted and
    // the retry is meaningful. A row that silently stays reads as a defect.
    deleteConversationCall.mockResolvedValue({
      kind: 'unavailable',
      detail: "Couldn't delete this chat just now. Please try again shortly.",
    });
    const { result } = await activated();
    await expect(result.current.remove(CHAT)).resolves.toContain('try again shortly');
  });
});
