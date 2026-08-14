/**
 * The §4.3 surface's non-streaming calls, and the one place their answers are classified.
 *
 * `src/kb/mutations.ts`'s shape, and its two rules hold here unchanged:
 *
 * - **Branch on `response.status`, never on `data`.** The status is what carries meaning —
 *   `201` is a chat that now exists, `204` is one that no longer does, `409` is a state that
 *   moved under a correct client.
 * - **Copy is the server's.** Every rejection renders `detail` verbatim; those strings are
 *   `TBD(§8.4)` and R-57(4) says render them, never match on them. A test asserts none of them
 *   is duplicated in this folder.
 *
 * One thing is *not* the KB's shape and is the substance of this file: the two chat `409`s are
 * different facts and the client behaves differently for each, so `classifyChat` splits them on
 * `detail.error_code` (R-56(1), R-57(4)) rather than reporting a single "conflict".
 *
 * **`frozen` is not an error.** `CONTEXT_WINDOW_EXCEEDED` must never be rendered from the
 * response: R-51(6) has the composer block *before* a request exists, and R-69(1) makes
 * FR-STA-04's own line the source for both sides. Reaching it means the client's `usage` was
 * stale, so the outcome carries the server's **numbers** and no copy — the caller adopts them
 * and the composer's own block takes over. See `useChat`.
 */
import { api } from '../api';
import type { Conversation, ConversationDetail, Feedback, Message } from '../api';
import { detailOf } from '../api/detail';

/** The stable codes this surface branches on. `PROCESSING_LOCKED` is the KB modal's. */
export const CONTEXT_WINDOW_EXCEEDED = 'CONTEXT_WINDOW_EXCEEDED';
export const NOT_LATEST_ANSWER = 'NOT_LATEST_ANSWER';

/**
 * What happened, in the chat's vocabulary rather than HTTP's.
 *
 * `gone` is deliberately not a rejection, exactly as in the KB modal: a `404` means our copy of
 * the world is stale — the chat was deleted in another tab, a later turn replaced the row — so
 * the response is to reconcile, not to tell the user off for clicking what we were showing them.
 */
export type ChatOutcome<T = unknown> =
  | { kind: 'ok'; data: T }
  | { kind: 'gone' }
  /** FR-STA-04's budget. Numbers, no copy — see the module docstring. */
  | { kind: 'frozen'; usedTokens: number; limitTokens: number }
  /** R-56(1) — a later turn landed while the action bar was open. Resolves on reload. */
  | { kind: 'stale'; detail: string }
  /** NFR-SEC-07's per-user chat budget. `retryAfter` is logged, never rendered (R-72(4)). */
  | { kind: 'throttled'; detail: string; retryAfter: string | null }
  /** R-54(5) — the checkpointer could not be reached, so the chat is intact and retryable. */
  | { kind: 'unavailable'; detail: string }
  /** A client bug. `detail` is a validation array and must never be rendered. */
  | { kind: 'invalid' }
  | { kind: 'unauthorized' }
  | { kind: 'refused'; detail: string; status: number };

interface RawResult<T> {
  status: number;
  /** The parsed error body, when there was one. */
  error?: unknown;
  data?: T | undefined;
  /** `Retry-After`, when the response carried it. */
  retryAfter?: string | null;
}

/**
 * The server's answer, classified. Pure, so every documented status is table-testable.
 *
 * `fallbackDetail` is what a caller renders when the body carried none — a network failure, or a
 * status documented without copy. It never *replaces* a `detail` the server did send.
 */
export function classifyChat<T>(result: RawResult<T>, fallbackDetail: string): ChatOutcome<T> {
  const { status } = result;
  const detail = detailOf(result.error);

  // 204 is `DELETE`'s success and carries no body at all — `data` is undefined and that is
  // correct, not a missing payload. Grouping it with 200/201 is what keeps the caller from
  // having to know which verb it called.
  if (status >= 200 && status < 300) return { kind: 'ok', data: result.data as T };

  if (status === 401) return { kind: 'unauthorized' };
  if (status === 404) return { kind: 'gone' };
  if (status === 422) return { kind: 'invalid' };
  if (status === 429) {
    return {
      kind: 'throttled',
      detail: detail.message ?? fallbackDetail,
      retryAfter: result.retryAfter ?? null,
    };
  }
  if (status === 503) return { kind: 'unavailable', detail: detail.message ?? fallbackDetail };
  if (status === 409 && detail.errorCode === NOT_LATEST_ANSWER) {
    return { kind: 'stale', detail: detail.message ?? fallbackDetail };
  }
  if (status === 409 && detail.errorCode === CONTEXT_WINDOW_EXCEEDED) {
    const body = (result.error as { detail?: Record<string, unknown> } | undefined)?.detail ?? {};
    return {
      kind: 'frozen',
      usedTokens: numberOr(body.used_tokens, 0),
      limitTokens: numberOr(body.limit_tokens, 0),
    };
  }
  return { kind: 'refused', detail: detail.message ?? fallbackDetail, status };
}

/** The two counts off a `CONTEXT_WINDOW_EXCEEDED` body, without trusting either. */
function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/** Run a call and normalise a thrown transport failure into the same shape. */
async function attempt<T>(run: () => Promise<RawResult<T>>): Promise<ChatOutcome<T>> {
  try {
    return classifyChat(await run(), NETWORK);
  } catch {
    // `status: 0` is not a real status and is never rendered — `refused` carries the copy.
    return { kind: 'refused', detail: NETWORK, status: 0 };
  }
}

// --- conversations (FR-SBR-02/03/04/07) --------------------------------------

export function listConversations(): Promise<ChatOutcome<Conversation[]>> {
  return attempt(async () => {
    const { data, error, response } = await api.GET('/api/v1/conversations');
    return { status: response.status, error, data };
  });
}

export function getConversation(id: string): Promise<ChatOutcome<ConversationDetail>> {
  return attempt(async () => {
    const { data, error, response } = await api.GET('/api/v1/conversations/{conversation_id}', {
      params: { path: { conversation_id: id } },
    });
    return { status: response.status, error, data };
  });
}

/** FR-SBR-02. The `201` carries the FR-ANL-03 meter, so a new chat needs no follow-up GET. */
export function createConversation(title?: string): Promise<ChatOutcome<ConversationDetail>> {
  return attempt(async () => {
    const { data, error, response } = await api.POST('/api/v1/conversations', {
      body: { title: title ?? null },
    });
    return { status: response.status, error, data };
  });
}

export function renameConversation(
  id: string,
  title: string,
): Promise<ChatOutcome<ConversationDetail>> {
  return attempt(async () => {
    const { data, error, response } = await api.PATCH('/api/v1/conversations/{conversation_id}', {
      params: { path: { conversation_id: id } },
      body: { title },
    });
    return { status: response.status, error, data };
  });
}

export function deleteConversation(id: string): Promise<ChatOutcome<undefined>> {
  return attempt(async () => {
    const { error, response } = await api.DELETE('/api/v1/conversations/{conversation_id}', {
      params: { path: { conversation_id: id } },
    });
    return { status: response.status, error, data: undefined };
  });
}

// --- messages (FR-MSG-*) ------------------------------------------------------

export function listMessages(conversationId: string): Promise<ChatOutcome<Message[]>> {
  return attempt(async () => {
    const { data, error, response } = await api.GET(
      '/api/v1/conversations/{conversation_id}/messages',
      { params: { path: { conversation_id: conversationId } } },
    );
    return { status: response.status, error, data };
  });
}

/**
 * FR-MSG-08's 👍/👎.
 *
 * The key is **always present** and `null` is a value, not an omission: `{}` is a `422`, because
 * R-55(4) makes "cleared" and "never rated" different facts and a silent erasure of a rating the
 * user gave is the failure that shape prevents. The `200` carries the whole updated message.
 */
export function setFeedback(
  messageId: string,
  feedback: Feedback | null,
): Promise<ChatOutcome<Message>> {
  return attempt(async () => {
    const { data, error, response } = await api.POST('/api/v1/messages/{message_id}/feedback', {
      params: { path: { message_id: messageId } },
      body: { feedback },
    });
    return { status: response.status, error, data };
  });
}

/** Copy this surface owns rather than the server — states the server has no answer for. */
export const NETWORK = 'Could not reach the server. Check your connection and try again.'; // TBD(§8.4)
