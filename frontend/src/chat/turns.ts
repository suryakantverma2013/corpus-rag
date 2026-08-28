/**
 * The §4.3 transcript as pure state — no React, no DOM, no transport (the `documents.ts` shape).
 *
 * Everything a turn does to the screen is a reducer case here, so the sequence a user actually
 * hits — ask, watch, answer, regenerate, fail, reload — is table-testable without a stream.
 *
 * **Three ideas carry the whole file.**
 *
 * 1. **Server rows and local rows are separate lists.** `messages` is whatever
 *    `GET /conversations/{id}/messages` last returned; `pendingTurn` holds the entries this
 *    client made during the turn in flight — the user's own bubble, which the stream never
 *    echoes because `record_question` writes it before the first byte, and the answer that
 *    arrives on the `message` frame. Merging them would mean inventing a `seq` for the local
 *    ones; keeping them apart means the post-turn reload simply **replaces** `messages` and
 *    drops the tail, with nothing to reconcile.
 * 2. **`outcomes` is a map keyed by message id.** `TurnOutcome` rides the SSE `message` frame
 *    and is *not* on the transcript route, so a reload would otherwise erase what the client
 *    learned live. Keyed by id, it survives every reload — and it feeds only NFR-A11Y-05's
 *    announcement, never the rendering (a surface that looked different depending on how the
 *    page was reached would be a defect).
 * 3. **A regenerate that fails leaves the answer alone.** Its copy becomes a `notice` anchored
 *    to the target's **id**, never an index, because the transcript is re-fetched twice more
 *    after every turn and an index would slide. R-56: the row on screen is still the truth,
 *    with its chips, its rating and its action bar intact.
 */
import type { ContextWindow, DegradedMessage, Message, TurnOutcome } from '../api';
import type { TranscriptEntry } from './messages';

/**
 * The prefix on an id this client made up.
 *
 * A user bubble is synthesized the moment Send is pressed and reconciled by the post-turn
 * reload discarding it — never by matching it against a server row. §8.58(7)'s rule ("never
 * invent a document") is about optimism that has to *reconcile on a non-identity key*; there
 * is no key here at all, because nothing ever addresses a user bubble: it is not rateable, not
 * regenerable and never sent back. `isLocalId` is the guard that keeps it that way.
 */
export const LOCAL_ID_PREFIX = 'local:';

export function isLocalId(id: string | null): boolean {
  return id !== null && id.startsWith(LOCAL_ID_PREFIX);
}

/** Degraded copy anchored beneath an existing answer — a regenerate that failed. */
export interface AnchoredNotice {
  /** The message id this renders under. An id, never an index — the list is re-fetched. */
  readonly afterId: string;
  readonly entry: TranscriptEntry;
}

export interface Chat {
  /** The server's transcript, oldest first (`messages.seq`). */
  readonly messages: readonly Message[];
  /** This turn's local entries, rendered after the server's rows. */
  readonly pendingTurn: readonly TranscriptEntry[];
  readonly notices: readonly AnchoredNotice[];
  /** Live-only `TurnOutcome`s, by message id — see the module docstring. */
  readonly outcomes: Readonly<Record<string, TurnOutcome>>;
  /** FR-ANL-03's meter. `null` until the first `GET /conversations/{id}` lands. */
  readonly usage: ContextWindow | null;
  /** Whether the transcript has been fetched — distinct from "the transcript is empty". */
  readonly loaded: boolean;
}

/** The one turn in flight. Singular: R-43(1) keys the R-24 lock on the *caller*. */
export interface Turn {
  readonly conversationId: string;
  readonly kind: 'send' | 'regenerate';
  /** The answer being replaced, for a regenerate. */
  readonly targetId?: string;
}

export interface ChatState {
  readonly byConversation: Readonly<Record<string, Chat>>;
  readonly turn: Turn | null;
}

export const EMPTY_CHAT: Chat = {
  messages: [],
  pendingTurn: [],
  notices: [],
  outcomes: {},
  usage: null,
  loaded: false,
};

export const EMPTY_CHAT_STATE: ChatState = { byConversation: {}, turn: null };

export type ChatAction =
  /** `GET /conversations/{id}/messages` landed. */
  | { type: 'loaded'; conversationId: string; messages: readonly Message[] }
  /** `GET /conversations/{id}` landed, or a 201 carried the meter. */
  | { type: 'usage'; conversationId: string; usage: ContextWindow }
  /** FR-CMP-03 — the user pressed Send. */
  | { type: 'asked'; conversationId: string; localId: string; text: string }
  /** FR-MSG-08 — Regenerate started on `targetId`. */
  | { type: 'regenerating'; conversationId: string; targetId: string }
  /** The SSE `message` frame, either branch. `targetId` marks it as a regenerate's. */
  | {
      type: 'answered';
      conversationId: string;
      message: Message | DegradedMessage;
      outcome: TurnOutcome | null;
      targetId?: string;
    }
  /** FR-MSG-08's 👍/👎 — the `200` body, or an optimistic write. */
  | { type: 'feedback'; conversationId: string; message: Message }
  /** FR-MSG-09 — the ungrounded answer's `201` body, appended beneath its abstention. */
  | { type: 'ungrounded'; conversationId: string; message: Message }
  /** The turn left no trace server-side, so its local entries must go too. */
  | { type: 'discarded'; conversationId: string }
  /** The turn ended, however it ended. */
  | { type: 'settled' }
  /** FR-SBR-07 — the conversation is gone. */
  | { type: 'removed'; conversationId: string };

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'loaded': {
      // The tail is dropped only when it is safe to: the server's list now contains the turn
      // it described. A reload landing while that chat is still answering — the user switched
      // away and back — must leave the bubble and the dots exactly where they were.
      const settled = state.turn?.conversationId !== action.conversationId;
      return patch(state, action.conversationId, (chat) => ({
        ...chat,
        messages: action.messages,
        pendingTurn: settled ? [] : chat.pendingTurn,
        // Anchors are ids, so notices survive the replacement — except the ones whose target
        // no longer exists, which would otherwise never render again and never be collected.
        notices: chat.notices.filter((notice) =>
          action.messages.some((message) => message.id === notice.afterId),
        ),
        loaded: true,
      }));
    }

    case 'usage':
      return patch(state, action.conversationId, (chat) => ({ ...chat, usage: action.usage }));

    case 'asked':
      return {
        byConversation: patch(state, action.conversationId, (chat) => ({
          ...chat,
          // The tail is **replaced**, not appended to. A new turn supersedes the last one's
          // local entries — including the copy of a send that failed, which would otherwise
          // sit at the end of the transcript for the rest of the session.
          pendingTurn: [pendingEntry(action.text, action.localId)],
        })).byConversation,
        turn: { conversationId: action.conversationId, kind: 'send' },
      };

    case 'regenerating':
      return {
        byConversation: patch(state, action.conversationId, (chat) => ({
          ...chat,
          // Clear the previous attempt's copy: it describes a run this one is replacing.
          notices: chat.notices.filter((notice) => notice.afterId !== action.targetId),
        })).byConversation,
        turn: {
          conversationId: action.conversationId,
          kind: 'regenerate',
          targetId: action.targetId,
        },
      };

    case 'answered':
      return patch(state, action.conversationId, (chat) => applyAnswer(chat, action));

    case 'feedback':
      return patch(state, action.conversationId, (chat) => ({
        ...chat,
        messages: chat.messages.map((message) =>
          message.id === action.message.id ? action.message : message,
        ),
        pendingTurn: chat.pendingTurn.map((entry) =>
          entry.message.id === action.message.id ? { ...entry, message: action.message } : entry,
        ),
      }));

    case 'ungrounded':
      // Appended to the **server's** transcript rather than to `pendingTurn`, because the row
      // already exists server-side: the `201` is the created message, so this is adopting a
      // confirmed row and not an optimistic write. It goes last because `messages` is ordered by
      // `seq` and this is the newest — the control is offered only when no turn is in flight, so
      // there is nothing it could be appended out of order with.
      //
      // Guarded against a double dispatch: React 18 StrictMode double-invokes, and a duplicated
      // id would render the answer twice with the same key.
      return patch(state, action.conversationId, (chat) =>
        chat.messages.some((message) => message.id === action.message.id)
          ? chat
          : { ...chat, messages: [...chat.messages, action.message] },
      );

    case 'discarded':
      // FR-STA-04's refusal is the case: `record_question` raises *before* it inserts, so no
      // row exists and leaving the bubble on screen would show a question the server never
      // received. The composer's own block is what explains it (R-51(6)).
      return patch(state, action.conversationId, (chat) =>
        chat.pendingTurn.length === 0 ? chat : { ...chat, pendingTurn: [] },
      );

    case 'settled':
      return state.turn === null ? state : { ...state, turn: null };

    case 'removed': {
      if (!(action.conversationId in state.byConversation)) return state;
      const byConversation = { ...state.byConversation };
      delete byConversation[action.conversationId];
      return {
        byConversation,
        turn: state.turn?.conversationId === action.conversationId ? null : state.turn,
      };
    }
  }
}

function applyAnswer(chat: Chat, action: Extract<ChatAction, { type: 'answered' }>): Chat {
  const { message, outcome, targetId } = action;
  const entry: TranscriptEntry = { message, outcome };

  if (targetId === undefined) {
    // A send. The answer joins this turn's tail beside the user's bubble; the reload that
    // follows replaces both with the server's own rows.
    return {
      ...chat,
      pendingTurn: [...chat.pendingTurn, entry],
      outcomes: withOutcome(chat.outcomes, message.id, outcome),
    };
  }

  if (isDegradedMessage(message)) {
    // R-56 — the re-run failed and the original answer is untouched, chips, rating and all.
    // The server's copy renders beneath it as an ordinary degraded row: no action bar, nothing
    // to rate, and NFR-A11Y-05 already speaks a degraded turn's own text.
    return { ...chat, notices: [...chat.notices, { afterId: targetId, entry }] };
  }

  // R-56(2) — replaced **in place**, keeping its position, id, `seq` and `created_at`.
  // `evaluation` and `feedback` arrive already cleared in the same UPDATE; the client neither
  // clears nor preserves them.
  return {
    ...chat,
    messages: chat.messages.map((row) => (row.id === targetId ? message : row)),
    outcomes: withOutcome(chat.outcomes, message.id, outcome),
  };
}

function withOutcome(
  outcomes: Readonly<Record<string, TurnOutcome>>,
  id: string | null,
  outcome: TurnOutcome | null,
): Readonly<Record<string, TurnOutcome>> {
  if (id === null || outcome === null) return outcomes;
  return { ...outcomes, [id]: outcome };
}

function patch(state: ChatState, conversationId: string, update: (chat: Chat) => Chat): ChatState {
  const chat = state.byConversation[conversationId] ?? EMPTY_CHAT;
  return {
    ...state,
    byConversation: { ...state.byConversation, [conversationId]: update(chat) },
  };
}

/** Narrowed on `created_at`, matching `messages.isDegraded` — never on `id === null`, which a
 *  regenerate failure carries the target's id in. */
function isDegradedMessage(message: Message | DegradedMessage): message is DegradedMessage {
  return !('created_at' in message);
}

/**
 * FR-CMP-03's user bubble, made locally the instant Send is pressed.
 *
 * `created_at` is stamped rather than fixed because this one really did just happen, and the
 * timestamp is what the reload overwrites with the server's.
 */
export function pendingEntry(text: string, localId: string): TranscriptEntry {
  return {
    message: {
      id: `${LOCAL_ID_PREFIX}${localId}`,
      role: 'user',
      segs: [{ text }],
      // FR-MSG-09 - a question the user just typed is neither, and the server
      // will say the same when this bubble is replaced by the real row.
      ungrounded: false,
      ungrounded_offerable: false,
      created_at: new Date().toISOString(),
    },
  };
}

/** A failure rendered in §4.3's own vocabulary: an answer bubble with no action bar. */
export function degradedEntry(text: string, id: string | null = null): TranscriptEntry {
  return { message: { id, role: 'ai', segs: [{ text }] }, outcome: 'error' };
}

/** One chat's rows, in render order — the server's, with notices spliced in, then this turn's. */
export function entriesOf(chat: Chat): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  for (const message of chat.messages) {
    entries.push({ message, outcome: chat.outcomes[message.id] ?? null });
    for (const notice of chat.notices) {
      if (notice.afterId === message.id) entries.push(notice.entry);
    }
  }
  return [...entries, ...chat.pendingTurn];
}

/** The chat for an id, or the empty one — so a caller never branches on absence. */
export function chatOf(state: ChatState, conversationId: string | null): Chat {
  if (conversationId === null) return EMPTY_CHAT;
  return state.byConversation[conversationId] ?? EMPTY_CHAT;
}
