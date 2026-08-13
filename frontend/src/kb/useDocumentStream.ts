/**
 * FR-KBM-09's live channel, consumed the one way R-41(3) permits.
 *
 * **`fetch` + `ReadableStream`, never `EventSource`** — the stream authenticates with an ordinary
 * `Authorization` header, which `EventSource` cannot send, and every query-string alternative was
 * rejected because it writes a credential into access logs, proxy logs, `Referer` and browser
 * history where it stays valid long after the tab closes. The cost is that native auto-reconnect
 * is given up and implemented here; R-41(6) makes that cheap, because there is no `Last-Event-ID`
 * replay and a reconnect simply applies the fresh `snapshot` every connect sends.
 *
 * The hook takes a `dispatch` rather than owning the store, so the R-41 reducer stays pure and
 * this file is only transport.
 */
import { useEffect, useRef, useState } from 'react';

import { DOCUMENT_EVENTS_URL, StreamError, streamFrames } from '../api';
import type { DocumentFrame } from '../api';
import { actionFor } from './documents';
import type { DocumentAction } from './documents';

/**
 * Where a connection ended up. Four of the six are terminal on purpose.
 *
 * `capped` is R-41(7)'s per-user limit: retrying cannot free a slot another tab holds, so an
 * automatic loop would burn the rate limiter to no effect. `unauthorized` is T-509's to resolve.
 * `failed` covers `400`/`404`, which mean the request itself was wrong — a reconnect is
 * superstition — and an exhausted retry budget.
 */
export type ConnectionState =
  'idle' | 'connecting' | 'open' | 'retrying' | 'capped' | 'unauthorized' | 'failed';

/** Never shorter than the server's own 1.5 s poll (R-41(2)): a faster retry cannot be fresher. */
const BASE_DELAY_MS = 1_500;
const MAX_DELAY_MS = 30_000;
export const MAX_RECONNECT_ATTEMPTS = 6;

/**
 * Exponential backoff with jitter, floored at the poll interval and capped.
 *
 * `random` is a parameter rather than a call to `Math.random` so the function is pure and the
 * tests are deterministic — the same reason `stats.ts` takes its clock rather than reading one.
 */
export function backoffDelay(attempt: number, random: number): number {
  const growth = Math.min(BASE_DELAY_MS * 2 ** (attempt - 1), MAX_DELAY_MS);
  return Math.round(growth * (0.5 + random / 2));
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
    function finish() {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    }
  });
}

/**
 * Hold the stream open while `enabled`, dispatching every frame.
 *
 * **Open on modal open, not on mount.** A closed modal has no live surface to update, and holding
 * one of four per-user slots plus a recurring query for a modal nobody opened is what exhausts
 * the cap across two tabs. The provider's one-shot `GET /documents` is what keeps the FR-SBR-05
 * count alive in the meantime.
 */
export function useDocumentStream(
  enabled: boolean,
  dispatch: (action: DocumentAction) => void,
): ConnectionState {
  const [state, setState] = useState<ConnectionState>('idle');

  /**
   * `dispatch` is read through a ref so it cannot appear in the effect's dependencies.
   *
   * Not a tidy-up: with it in the deps, a caller passing an inline callback re-runs the effect on
   * **every render**, and each run aborts the live connection and opens another. That burns
   * R-41(7)'s four-slot budget and hammers a route whose whole design is about bounded load —
   * and it fails silently, because the stream still works. The `useReducer` dispatch this ships
   * against happens to be stable; a hook whose correctness depends on the caller knowing that is
   * one refactor away from the bug.
   */
  const dispatchRef = useRef(dispatch);
  useEffect(() => {
    dispatchRef.current = dispatch;
  }, [dispatch]);

  useEffect(() => {
    if (!enabled) {
      setState('idle');
      return;
    }

    const controller = new AbortController();
    let stopped = false;
    let attempt = 0;

    const run = async () => {
      while (!stopped) {
        try {
          setState('connecting');
          for await (const frame of streamFrames<DocumentFrame>(DOCUMENT_EVENTS_URL, {
            signal: controller.signal,
          })) {
            // Reset only once a frame has proved the stream works. Resetting on connect would
            // hot-loop against a server that accepts and immediately closes.
            attempt = 0;
            setState('open');
            dispatchRef.current(actionFor(frame));
          }
          // A clean end of stream — a proxy idle timeout, say — is not an error but is not the
          // end of the caller's interest either, so it falls through to the backoff below.
        } catch (error) {
          // `stopped` is the whole abort story. Aborting rejects the pending read AND the
          // generator's `finally` cancel, both as `AbortError` — but the cleanup below sets this
          // flag in the SAME SYNCHRONOUS TICK as the abort, and a rejection cannot be observed
          // before the next microtask, so by the time either lands the flag is already true.
          // (Which line comes first is therefore irrelevant — checked under mutation, where
          // swapping them changes nothing.) An explicit `isAbortError` clause beside this was
          // written first and proved unreachable for the same reason, so it is deleted rather
          // than tested — R-49's rule for a survivor that is dead code and not a missing test.
          if (stopped) return;
          if (error instanceof StreamError) {
            if (error.status === 429) return setState('capped');
            if (error.status === 401) return setState('unauthorized');
            if (error.status === 400 || error.status === 404) return setState('failed');
          }
        }
        if (stopped) return;
        attempt += 1;
        if (attempt > MAX_RECONNECT_ATTEMPTS) return setState('failed');
        setState('retrying');
        await sleep(backoffDelay(attempt, Math.random()), controller.signal);
      }
    };

    void run();
    return () => {
      // Abort synchronously rather than only setting the flag: React 19's StrictMode
      // double-invokes this effect in development, and the backend releases the slot in its own
      // `finally` when the response is cancelled — so the second connect must genuinely close
      // the first, not wait for it to notice a boolean.
      stopped = true;
      controller.abort();
    };
  }, [enabled]);

  return state;
}
