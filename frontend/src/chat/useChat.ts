/**
 * The §4.3 store: one transcript per chat, one turn at a time, and the FR-ANL-03 meter.
 *
 * A hook rather than a context, for the reason `useDocuments` gives: `App` is already the
 * composition root for every other piece of FR-CST-01 state and every consumer here is one hop
 * from it.
 *
 * **Four things in this file are load-bearing and each has a test that fails without it.**
 *
 * 1. **The turn clears in a `finally`.** `done` is one of four exits — the others are a
 *    pre-first-byte refusal, an abort, and a stream that ends without one. Clearing it only on
 *    the happy path is the deadlock §8.58 records: the flag disables the very actions whose
 *    success would clear it, so one 429 would freeze the composer *and* the KB modal's four
 *    verbs for the rest of the session.
 * 2. **The two GETs after every turn are not optional.** The meter cannot be recomputed here —
 *    `conversation_usage` sums tokens over the raw `messages.content`, with its `[S<n>]`
 *    markers, and the wire publishes only the derived `segs`. And the user's own row is never
 *    on the stream: `record_question` writes it before the first byte, so a reload is the only
 *    way to learn its real id.
 * 3. **A `CONTEXT_WINDOW_EXCEEDED` never renders.** R-51(6) has the composer block before a
 *    request exists and R-69(1) makes FR-STA-04's own line the source for both sides, so
 *    reaching that response means our `usage` was stale. The client re-reads the meter and lets
 *    the composer's own block take over — it learns a number, it does not show a message.
 * 4. **DeepEval scores arrive after the stream.** The job is enqueued *after* the `message`
 *    frame and takes ~6 s (R-50), so a fresh answer carries `evaluation: null` and FR-EVL-02's
 *    chips would not appear until the next chat switch. A bounded backoff re-fetch delivers
 *    FR-ANL-04's "Scores appear once a response is evaluated" without a polling loop, and
 *    finding nothing costs nothing — FR-EVL-01 says a response *may* carry scores.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import type { ChatFrame, ContextWindow, Feedback, Message } from '../api';
import { isFrozen } from '../tokens';
import type { TranscriptEntry } from './messages';
import {
  answerFromGeneralKnowledge,
  deleteConversation,
  getConversation,
  listMessages,
  setFeedback,
} from './mutations';
import { streamRegenerate, streamSend } from './useChatStream';
import type { TurnFailure } from './useChatStream';
import { EMPTY_CHAT_STATE, chatOf, chatReducer, degradedEntry, entriesOf } from './turns';
import type { ChatAction, ChatState } from './turns';

export interface ChatStore {
  /** The active chat's rows, in render order. */
  readonly entries: readonly TranscriptEntry[];
  /** FR-MSG-05's dots — a turn running **in this chat**. */
  readonly typing: boolean;
  /**
   * OI-31 / R-71(1) — a turn of this caller's is running, in any chat.
   *
   * Per user, not per conversation, because that is what R-43(1)'s lock is keyed on: the server
   * refuses a document verb during a turn in a *different* chat, so a per-chat signal here
   * would disagree with the `409` it exists to pre-empt.
   */
  readonly turnInFlight: boolean;
  /** FR-ANL-03's meter for the active chat. `null` until the first read lands. */
  readonly usage: ContextWindow | null;
  /** FR-STA-04 — derived from `usage`, never the other way round. */
  readonly frozen: boolean;
  readonly loaded: boolean;
  /**
   * FR-CMP-03's send. `targetId` overrides the active chat, and exists for exactly one caller.
   *
   * B-001: a composer sending from the empty state has to create a conversation first, and the
   * callback that resumes after that round trip was built in a render where `conversationId`
   * was still `null` — so it cannot read the new id back out of state it set in the same tick.
   * Passing the id makes that one path explicit instead of leaving it to a closure that is
   * always one render behind, and the guard below still refuses a send with no chat at all.
   */
  send: (text: string, documentIds: readonly string[], targetId?: string) => void;
  regenerate: (messageId: string) => void;
  feedback: (messageId: string, value: Feedback | null) => void;
  /** FR-MSG-09 — ask an abstention for an answer from the model's own training (R-98). */
  answerUngrounded: (messageId: string) => void;
  /**
   * The abstentions whose FR-MSG-09 request is in flight, by message id.
   *
   * A set rather than a boolean because the control is per message and a transcript can hold
   * several abstentions; disabling all of them because one is answering would be wrong, and
   * disabling none of them invites the double-submit the reducer then has to de-duplicate.
   */
  readonly ungroundedBusy: ReadonlySet<string>;
  /** FR-SBR-07 — `DELETE /conversations/{id}`. Resolves to the server's copy, or `null`. */
  remove: (conversationId: string) => Promise<string | null>;
  /** Adopt the meter a `201` already carried, so a new chat needs no follow-up GET. */
  adopt: (conversationId: string, usage: ContextWindow) => void;
}

export interface UseChatOptions {
  /** The active conversation. `null` is the empty state, and fetches nothing. */
  conversationId: string | null;
  /** T-509/FR-AUT-07 — see `useDocuments`. A hook cannot be called conditionally. */
  enabled: boolean;
  /** What the sidebar row should now say, so the list needs no third GET after a turn. */
  onTurnSettled?: (
    conversationId: string,
    updated: { updatedAt: string; messageCount: number },
  ) => void;
  /** The chat is not there any more (404). The list store owns what happens next. */
  onMissing?: (conversationId: string) => void;
}

/**
 * When to look again for DeepEval scores after a turn. `TBD(§8.4)`.
 *
 * Brackets R-50's measured ~5.9 s median rather than guessing a single delay, and stops at the
 * first read that finds one. Three attempts, because a fourth would be waiting on a judge that
 * has already failed open.
 */
export const EVAL_REFRESH_DELAYS_MS = [4_000, 8_000, 16_000];

export function useChat({
  conversationId,
  enabled,
  onTurnSettled,
  onMissing,
}: UseChatOptions): ChatStore {
  const [state, dispatch] = useReducer(chatReducer, EMPTY_CHAT_STATE);

  /** One controller per turn, aborted synchronously on unmount — T-508's discipline. */
  const controller = useRef<AbortController | null>(null);
  const localSeq = useRef(0);
  const evalTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const mounted = useRef(true);

  /**
   * FR-MSG-09's in-flight set, held twice on purpose.
   *
   * The ref is the authority the callback reads and writes — it is correct in the same tick, so
   * two clicks in one frame cannot both pass the guard, which state would allow because a
   * `setState` is not visible to the closure that scheduled it. The state exists only to make
   * React re-render the disabled control; a ref alone would leave the button live-looking.
   */
  const ungroundedPending = useRef<Set<string>>(new Set());
  const [ungroundedBusy, setUngroundedBusy] = useState<ReadonlySet<string>>(new Set());

  /**
   * State and callbacks, read through refs so neither can reach an effect's dependencies.
   *
   * §8.58(10): a caller passing an inline arrow would otherwise re-run the activation effect on
   * every render — silently, because the fetches still work. `stateRef` is the same trick for
   * `feedback`, which needs the row it is about to overwrite and must not be rebuilt on every
   * keystroke elsewhere in the tree.
   */
  const stateRef = useRef(state);
  stateRef.current = state;
  const callbacks = useRef({ onTurnSettled, onMissing });
  useEffect(() => {
    callbacks.current = { onTurnSettled, onMissing };
  }, [onTurnSettled, onMissing]);

  const cancelEvalRefresh = useCallback(() => {
    for (const timer of evalTimers.current) clearTimeout(timer);
    evalTimers.current = [];
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
      for (const timer of evalTimers.current) clearTimeout(timer);
      evalTimers.current = [];
    };
  }, []);

  /**
   * Re-read one chat: its transcript and its meter, together.
   *
   * Both or neither — a transcript without its usage leaves the composer projecting FR-STA-04
   * against a number that predates the answer already on screen.
   */
  const refresh = useCallback(async (id: string): Promise<Message[] | null> => {
    const [transcript, detail] = await Promise.all([listMessages(id), getConversation(id)]);
    if (!mounted.current) return null;

    if (transcript.kind === 'gone' || detail.kind === 'gone') {
      callbacks.current.onMissing?.(id);
      dispatch({ type: 'removed', conversationId: id });
      return null;
    }
    if (transcript.kind === 'ok') {
      dispatch({ type: 'loaded', conversationId: id, messages: transcript.data });
    }
    if (detail.kind === 'ok') {
      dispatch({ type: 'usage', conversationId: id, usage: detail.data.context });
      callbacks.current.onTurnSettled?.(id, {
        updatedAt: detail.data.updated_at,
        messageCount: detail.data.message_count,
      });
    }
    return transcript.kind === 'ok' ? transcript.data : null;
  }, []);

  // Activation. `enabled` gates it for the reason `useDocuments` gates its list: this runs
  // during the pre-auth phase too, and an unauthenticated 401 would reach FR-AUT-07's handler
  // and sign out the user whose session is resuming. No cancellation flag: every dispatch is
  // keyed by conversation, so a late answer updates the chat it was asked about.
  useEffect(() => {
    if (!enabled || conversationId === null) return;
    void refresh(conversationId).catch(() => undefined);
  }, [conversationId, enabled, refresh]);

  /**
   * Look again for FR-EVL-02's chips, then stop.
   *
   * Keyed on the one answer this turn produced: a turn that produced none — an error, a
   * refusal — schedules nothing, because there is nothing a judge will ever score.
   */
  const scheduleEvalRefresh = useCallback(
    (id: string, answerId: string) => {
      cancelEvalRefresh();
      evalTimers.current = EVAL_REFRESH_DELAYS_MS.map((delay) =>
        setTimeout(() => {
          void refresh(id)
            .then((messages) => {
              if (messages?.find((message) => message.id === answerId)?.evaluation != null) {
                cancelEvalRefresh();
              }
            })
            .catch(() => undefined);
        }, delay),
      );
    },
    [cancelEvalRefresh, refresh],
  );

  /** `stage` carries no content by construction (R-54(2)) and §4.3 renders no stage name, so
   *  `message` is the only frame that changes anything. */
  const handleFrame = useCallback((id: string, frame: ChatFrame, targetId?: string) => {
    if (frame.event !== 'message') return;
    dispatch({
      type: 'answered',
      conversationId: id,
      message: frame.data.message,
      outcome: frame.data.outcome ?? null,
      targetId,
    });
  }, []);

  /**
   * What a refusal does. Every branch but one ends in a row the user can read.
   *
   * The rendering vehicle is a synthesized degraded answer — §4.3's existing vocabulary for
   * "the server said no and there is nothing to rate". `DegradedAnswer` draws it with no action
   * bar and `announcementFor` already speaks its text (NFR-A11Y-05), so this adds no component,
   * no stylesheet and no pixels for T-510 to re-baseline.
   */
  const applyFailure = useCallback(
    (id: string, failure: TurnFailure, targetId?: string) => {
      switch (failure.kind) {
        case 'frozen':
          // Never rendered (R-51(6)/R-69(1)). Nothing was written — `record_question` raises
          // before it inserts — so the bubble goes too, and the re-read meter flips the
          // composer's own FR-STA-04 block, which is what tells the user.
          dispatch({ type: 'discarded', conversationId: id });
          void refresh(id).catch(() => undefined);
          return;
        case 'gone':
          if (targetId === undefined) {
            callbacks.current.onMissing?.(id);
            dispatch({ type: 'removed', conversationId: id });
          } else {
            void refresh(id).catch(() => undefined);
          }
          return;
        case 'unauthorized':
          // The transport already reported the session (R-72(6)). Inventing copy for a screen
          // the user is about to leave would be noise.
          return;
        case 'invalid':
          dispatch(failureRow(id, INVALID_NOTICE, targetId));
          return;
        case 'stale':
          // R-56(1) — a later turn landed under an open action bar. Show the server's copy and
          // reload, which is the thing that actually clears it.
          dispatch(failureRow(id, failure.detail, targetId));
          void refresh(id).catch(() => undefined);
          return;
        default:
          dispatch(failureRow(id, failure.detail, targetId));
      }
    },
    [refresh],
  );

  const runTurn = useCallback(
    async (
      id: string,
      start: (signal: AbortSignal) => Promise<TurnFailure | null>,
      targetId?: string,
    ) => {
      controller.current?.abort();
      const own = new AbortController();
      controller.current = own;
      let ran = false;
      try {
        const failure = await start(own.signal);
        if (own.signal.aborted || !mounted.current) return;
        if (failure === null) ran = true;
        else applyFailure(id, failure, targetId);
      } finally {
        // The line the deadlock tests exist for: `done` is one of four exits and this is the
        // only place that covers all of them.
        if (controller.current === own) controller.current = null;
        if (mounted.current) dispatch({ type: 'settled' });
      }
      if (!ran || !mounted.current || own.signal.aborted) return;
      const messages = await refresh(id).catch(() => null);
      const answerId = targetId ?? latestAnswerId(messages);
      if (answerId !== null && mounted.current) scheduleEvalRefresh(id, answerId);
    },
    [applyFailure, refresh, scheduleEvalRefresh],
  );

  const send = useCallback(
    (text: string, documentIds: readonly string[], targetId?: string) => {
      // One binding, read once: the dispatch, the turn and the stream must all name the same
      // chat, and mixing `targetId` with `conversationId` between them would write the bubble
      // into one transcript and the answer into another.
      const id = targetId ?? conversationId;
      if (id === null) return;
      cancelEvalRefresh();
      localSeq.current += 1;
      dispatch({ type: 'asked', conversationId: id, localId: String(localSeq.current), text });
      void runTurn(id, (signal) =>
        streamSend(id, text, documentIds, {
          signal,
          onFrame: (frame) => handleFrame(id, frame),
        }),
      );
    },
    [cancelEvalRefresh, conversationId, handleFrame, runTurn],
  );

  const regenerate = useCallback(
    (messageId: string) => {
      if (conversationId === null) return;
      cancelEvalRefresh();
      dispatch({ type: 'regenerating', conversationId, targetId: messageId });
      void runTurn(
        conversationId,
        (signal) =>
          streamRegenerate(messageId, {
            signal,
            onFrame: (frame) => handleFrame(conversationId, frame, messageId),
          }),
        messageId,
      );
    },
    [cancelEvalRefresh, conversationId, handleFrame, runTurn],
  );

  const feedback = useCallback(
    (messageId: string, value: Feedback | null) => {
      if (conversationId === null) return;
      const current = findMessage(stateRef.current, conversationId, messageId);
      if (current === undefined) return;
      dispatch({ type: 'feedback', conversationId, message: { ...current, feedback: value } });
      void setFeedback(messageId, value).then((outcome) => {
        if (!mounted.current) return;
        if (outcome.kind === 'ok') {
          dispatch({ type: 'feedback', conversationId, message: outcome.data });
          return;
        }
        // Revert, and render nothing. The control visibly springing back is the message, and a
        // failed thumb must never become a transcript row — the asymmetry with a failed turn is
        // deliberate, because nobody asked a question here.
        dispatch({ type: 'feedback', conversationId, message: current });
        if (outcome.kind === 'gone') void refresh(conversationId).catch(() => undefined);
      });
    },
    [conversationId, refresh],
  );

  /**
   * FR-MSG-09 — ask for an answer from the model's own training (R-98).
   *
   * Three things about the shape, each deliberate:
   *
   * **It appends and never replaces.** The abstention stays on screen beneath its own answer,
   * because it is the record that the corpus could not answer and R-98(1) rests on it — this is
   * the opposite of `regenerate`, whose whole job is to replace.
   *
   * **It does not take the turn.** `state.turn` is the FR-MSG-05 typing indicator and the R-24
   * one-turn-at-a-time signal; this is a single non-streaming call with no stages to report, and
   * claiming the turn would disable the composer and the KB modal for its duration (the §8.58
   * deadlock shape). `pending` is its own local flag instead.
   *
   * **A failure renders beneath the abstention, not in place of it.** `failureRow` anchored on
   * the target is the same treatment a failed regenerate gets, and for the same reason: the user
   * asked for something, it did not happen, and the answer they were reading is still there.
   */
  const answerUngrounded = useCallback(
    (messageId: string) => {
      if (conversationId === null) return;
      if (ungroundedPending.current.has(messageId)) return;
      ungroundedPending.current.add(messageId);
      setUngroundedBusy(new Set(ungroundedPending.current));
      void answerFromGeneralKnowledge(messageId).then((outcome) => {
        if (!mounted.current) return;
        ungroundedPending.current.delete(messageId);
        setUngroundedBusy(new Set(ungroundedPending.current));
        if (outcome.kind === 'ok') {
          dispatch({ type: 'ungrounded', conversationId, message: outcome.data });
          return;
        }
        if (outcome.kind === 'unauthorized') return;
        // Our copy of the world is stale — the chat or the answer is gone. Re-read rather than
        // tell the user off for clicking what we were showing them (`mutations.ts`'s rule).
        if (outcome.kind === 'gone') {
          void refresh(conversationId).catch(() => undefined);
          return;
        }
        const text =
          outcome.kind === 'invalid'
            ? INVALID_NOTICE
            : outcome.kind === 'frozen'
              ? NETWORK_NOTICE
              : outcome.detail;
        dispatch(failureRow(conversationId, text, messageId));
      });
    },
    [conversationId, refresh],
  );

  const remove = useCallback(async (id: string): Promise<string | null> => {
    const outcome = await deleteConversation(id);
    // A chat that is already gone *is* deleted, from the user's point of view.
    if (outcome.kind === 'ok' || outcome.kind === 'gone') {
      dispatch({ type: 'removed', conversationId: id });
      return null;
    }
    if (outcome.kind === 'unauthorized') return null;
    if (outcome.kind === 'invalid') return INVALID_NOTICE;
    if (outcome.kind === 'frozen') return NETWORK_NOTICE;
    // R-54(5) — the checkpointer could not be reached, so nothing was deleted and a retry is
    // meaningful. The server's own copy says so.
    return outcome.detail;
  }, []);

  const adopt = useCallback((id: string, usage: ContextWindow) => {
    dispatch({ type: 'usage', conversationId: id, usage });
  }, []);

  const chat = chatOf(state, conversationId);
  const entries = useMemo(() => entriesOf(chat), [chat]);
  const usage = chat.usage;

  return {
    entries,
    typing: conversationId !== null && state.turn?.conversationId === conversationId,
    turnInFlight: state.turn !== null,
    usage,
    frozen: usage !== null && isFrozen(toProjection(usage)),
    loaded: chat.loaded,
    send,
    regenerate,
    feedback,
    answerUngrounded,
    ungroundedBusy,
    remove,
    adopt,
  };
}

/** The three numbers `src/tokens.ts` projects FR-STA-04 against. */
export function toProjection(usage: ContextWindow): {
  used: number;
  limit: number;
  reserve: number;
} {
  return {
    used: usage.used_tokens,
    limit: usage.limit_tokens,
    reserve: usage.answer_reserve_tokens,
  };
}

function failureRow(conversationId: string, text: string, targetId?: string): ChatAction {
  return {
    type: 'answered',
    conversationId,
    message: degradedEntry(text, targetId ?? null).message,
    outcome: 'error',
    targetId,
  };
}

function latestAnswerId(messages: readonly Message[] | null): string | null {
  if (messages === null) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'ai') return messages[index].id;
  }
  return null;
}

function findMessage(
  state: ChatState,
  conversationId: string,
  messageId: string,
): Message | undefined {
  const chat = chatOf(state, conversationId);
  const row = chat.messages.find((message) => message.id === messageId);
  if (row !== undefined) return row;
  const tail = chat.pendingTurn.find((entry) => entry.message.id === messageId);
  return tail !== undefined && 'created_at' in tail.message ? tail.message : undefined;
}

// Copy this surface owns rather than the server — states the server has no answer for.
const INVALID_NOTICE = 'That request could not be sent. Reload the page and try again.'; // TBD(§8.4)
const NETWORK_NOTICE = 'Could not reach the server. Check your connection and try again.'; // TBD(§8.4)
