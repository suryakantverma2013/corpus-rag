/**
 * One turn over SSE — FR-CMP-03's send and FR-MSG-08's Regenerate, which are the same stream.
 *
 * **A module function, not a hook**, and that is the difference from `useDocumentStream`. That
 * one holds a *connection* open for as long as a boolean says so, and owns reconnection. This is
 * one-shot: a user action starts it, three frames later it is over, and there is nothing to
 * reconnect to — a turn that died mid-stream cannot be resumed by asking again, because asking
 * again is a second turn with a second row. So the caller gets a promise, not a state machine.
 *
 * **Everything that can refuse the turn happens before the first byte.** `admit_send` and
 * `admit_regeneration` resolve ownership, the FR-STA-04 budget and the rate limit as
 * dependencies, so a 404 / 409 / 429 arrives as an HTTP status and reaches us as a
 * `StreamError` — never as a broken `200`. That is what lets this classify a refusal through
 * the *same* `classifyChat` the non-streaming calls use, so the two paths cannot disagree about
 * a nested `error_code`.
 */
import {
  CHAT_REGENERATE_PATH,
  CHAT_SEND_PATH,
  StreamError,
  expandPath,
  streamFrames,
} from '../api';
import type { ChatFrame } from '../api';
import { NETWORK, classifyChat } from './mutations';
import type { ChatOutcome } from './mutations';

/** Where a turn ended: `null` if the stream ran, otherwise why it never did. */
export type TurnFailure = Exclude<ChatOutcome<never>, { kind: 'ok' }>;

export interface StreamOptions {
  /** Every frame, in order. `stage` included — the caller decides it has nothing to render. */
  onFrame: (frame: ChatFrame) => void;
  signal: AbortSignal;
}

/** FR-CMP-03 — `POST /conversations/{id}/messages`. */
export function streamSend(
  conversationId: string,
  query: string,
  documentIds: readonly string[],
  options: StreamOptions,
): Promise<TurnFailure | null> {
  return runTurn(
    expandPath(CHAT_SEND_PATH, { conversation_id: conversationId }),
    {
      method: 'POST',
      // A plain object literal, never a `Headers`: `streamFrames` spreads `init.headers` into
      // its own set, and spreading a `Headers` instance yields `{}` — the request would go out
      // as `text/plain` and FastAPI would answer 422.
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, document_ids: documentIds }),
    },
    options,
  );
}

/** FR-MSG-08 — `POST /messages/{id}/regenerate`. No body: the query is already a row. */
export function streamRegenerate(
  messageId: string,
  options: StreamOptions,
): Promise<TurnFailure | null> {
  return runTurn(
    expandPath(CHAT_REGENERATE_PATH, { message_id: messageId }),
    { method: 'POST' },
    options,
  );
}

async function runTurn(
  url: string,
  init: RequestInit,
  { onFrame, signal }: StreamOptions,
): Promise<TurnFailure | null> {
  try {
    for await (const frame of streamFrames<ChatFrame>(url, { ...init, signal })) {
      // Aborting rejects the pending read, but a frame already buffered can still be yielded
      // in the same tick — and delivering it would write into a store the caller has moved on
      // from. The caller's own `stopped` flag is the second guard; this is the cheap first one.
      if (signal.aborted) return null;
      onFrame(frame);
    }
    return null;
  } catch (error) {
    // An abort is the caller's own doing, not a failure to report.
    if (signal.aborted) return null;
    if (error instanceof StreamError) {
      const outcome = classifyChat(
        { status: error.status, error: parseBody(error.body), retryAfter: error.retryAfter },
        NETWORK,
      );
      // `ok` is unreachable — `streamFrames` only throws on a non-2xx — but the union has to be
      // narrowed and inventing a failure for a success would be worse than saying nothing.
      return outcome.kind === 'ok' ? null : outcome;
    }
    return { kind: 'refused', detail: NETWORK, status: 0 };
  }
}

/** The error body arrives as text, because `streamFrames` reads it before throwing. */
function parseBody(body: string | null): unknown {
  if (body === null) return undefined;
  try {
    return JSON.parse(body);
  } catch {
    // A proxy's HTML error page, say. There is nothing here a user should read.
    return undefined;
  }
}
