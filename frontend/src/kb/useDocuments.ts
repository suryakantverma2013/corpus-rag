/**
 * The §4.7 store: one document set, the four verbs, and the R-71(1) reconciliation.
 *
 * **A hook, not a fourth context.** `App` is already the composition root for every other piece
 * of FR-CST-01 state and hands it down as props, and the three consumers here — the modal,
 * FR-SBR-05's sidebar count and FR-CMP-06's composer footer — are all one hop from it. R-58(5)
 * made `theme` a provider because it is read at arbitrary depth by components that must not
 * receive it as a prop; nothing here is. A context would also be a shape T-513 might have to
 * unpick when it consolidates the in-flight signal.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import type { DocumentEvent, UploadScope } from '../api';
import { EMPTY_DOCUMENTS, actionBlock, documentsReducer, impliesProcessingLock } from './documents';
import type { RowAction } from './documents';
import {
  deleteDocument,
  listDocuments,
  replaceDocument,
  retryDocument,
  uploadDocument,
} from './mutations';
import type { Outcome } from './mutations';
import { useDocumentStream } from './useDocumentStream';
import type { ConnectionState } from './useDocumentStream';

/**
 * An upload in flight — deliberately **not** a document.
 *
 * The alternative, an optimistic row, would have to invent `document_id`, `created_at`, `scope`
 * and `knowledge_base_id`, and then reconcile against the real row 1.5 s later on the only key
 * available before the `202`: the filename. R-40/FR-KBM-08 reject filename as identity in as many
 * words, so filename-matched optimism would re-introduce the exact rule this surface refused.
 * A pending entry gives the same instant feedback, invents nothing, and hands off on the
 * `document_id` the server itself returns.
 */
export interface PendingUpload {
  readonly localId: string;
  readonly filename: string;
  readonly documentId: string | null;
}

export interface DocumentsStore {
  readonly rows: readonly DocumentEvent[];
  readonly pending: readonly PendingUpload[];
  readonly connection: ConnectionState;
  /** NFR-A11Y-05 — the newest status transition, announced politely. */
  readonly announcement: string | null;
  /** The last mutation's message, rendered verbatim from the server (R-57(4)). */
  readonly notice: string | null;
  /** R-71(1) — FR-KBM-07's four verbs are unavailable, and the modal says so. */
  readonly paused: boolean;
  upload: (files: readonly File[], scope: UploadScope) => void;
  act: (document: DocumentEvent, action: RowAction, file?: File) => void;
  dismissNotice: () => void;
}

export interface UseDocumentsOptions {
  /** The FR-CST-01 `docsOpen` flag — the stream follows it. */
  open: boolean;
  /** The active conversation, for FR-KBM-03's second section and `scope=chat` uploads. */
  conversationId: string | null;
  /** OI-31/OI-36's signal, owned by `App` and shared with the FR-MSG-05 indicator. */
  turnInFlight: boolean;
  /**
   * Whether there is a session to call the server with (T-509, FR-AUT-07).
   *
   * A hook cannot be called conditionally, so this runs on every render of the composition
   * root — including before the boot probe has answered "is anyone signed in?", which is not
   * knowable locally because the refresh cookie is httpOnly (R-72(1)). Without this flag the
   * list below fires unauthenticated on mount, and its `401` reaches the FR-AUT-07 handler and
   * **signs out the user whose session was about to resume** — a race whose loser depends on
   * which request returns first.
   *
   * This is the 401 the effect's own comment deferred to T-509.
   */
  enabled: boolean;
}

export function useDocuments({
  open,
  conversationId,
  turnInFlight,
  enabled,
}: UseDocumentsOptions): DocumentsStore {
  const [state, dispatch] = useReducer(documentsReducer, EMPTY_DOCUMENTS);
  const [pending, setPending] = useState<readonly PendingUpload[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  /**
   * The server refused an action with R-24's `409` although our own signal said it was free — a
   * second tab, or a turn this client never saw start. R-71(1) makes this the *reconciliation*
   * path: it disables the affordances exactly as `turnInFlight` does, and clears itself rather
   * than persisting, because the condition it describes ends on its own.
   */
  const [serverSaysBusy, setServerSaysBusy] = useState(false);
  const nextLocalId = useRef(0);

  // `enabled` gates the stream too: after a session ends with the modal still open, `open` is
  // momentarily still true, and a reconnect would authenticate with a token that is gone.
  const connection = useDocumentStream(open && enabled, dispatch);

  // The closed-modal set. FR-SBR-05's count and FR-CMP-04's mention rows are visible without the
  // modal ever being opened, so the list cannot wait for the stream — and holding a stream open
  // for them would spend one of four per-user slots on a surface nobody is looking at.
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void listDocuments()
      .then((documents) => {
        if (!cancelled) dispatch({ type: 'list', documents });
      })
      // A failed list is not worth a notice: the modal is not open, and the stream's own snapshot
      // supersedes this the moment it is.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  /**
   * The server's refusal is momentary, so the flag standing in for it must expire on its own.
   *
   * Clearing it only when `turnInFlight` falls is not enough and **deadlocks** — found by the
   * tests below. The whole point of this flag is the case where our signal never saw the turn
   * start (a second tab), so `turnInFlight` is already false and has no transition left to make;
   * meanwhile the flag disables the very actions whose success would clear it. The modal would
   * stay paused for the rest of the session after one unpredicted `409`.
   *
   * So it lapses, and the server is left to refuse again if it is still busy — which is the same
   * shape as the gate itself: R-43(3) makes the lock advisory with an expiry, never a correctness
   * mechanism. Both exits are kept: an early one when our own signal reports the turn has ended.
   */
  useEffect(() => {
    if (!serverSaysBusy) return;
    const timer = setTimeout(() => setServerSaysBusy(false), SERVER_BUSY_LAPSE_MS);
    return () => clearTimeout(timer);
  }, [serverSaysBusy]);

  useEffect(() => {
    if (!turnInFlight) setServerSaysBusy(false);
  }, [turnInFlight]);

  const applyOutcome = useCallback(
    (outcome: Outcome, verb: 'upload' | RowAction, document: DocumentEvent | null) => {
      switch (outcome.kind) {
        case 'accepted':
        case 'duplicate':
          // The server's own answer, ~1.5 s before the stream repeats it. Not optimism.
          dispatch({
            type: 'status',
            documentId: outcome.documentId,
            status: outcome.status,
          });
          setServerSaysBusy(false);
          setNotice(outcome.kind === 'duplicate' ? DUPLICATE_NOTICE : null);
          return outcome;
        case 'gone':
          // The store was stale. Dropping the row *is* the correct user-visible outcome of
          // deleting something that has already gone; an error would blame the user for it.
          if (document !== null) {
            dispatch({ type: 'removed', documentId: document.document_id });
          }
          setNotice(null);
          return outcome;
        case 'locked':
          setServerSaysBusy(true);
          setNotice(null);
          return outcome;
        case 'refused':
          // A `409` from a server predating R-71(1)'s `error_code`, on a verb whose only other
          // `409` is impossible from this row's state, is still the lock. Conservative by design:
          // Replace is never claimed, which is why the code was added.
          if (outcome.status === 409 && impliesProcessingLock(verb, document)) {
            setServerSaysBusy(true);
            setNotice(null);
          } else {
            setNotice(outcome.detail);
          }
          return outcome;
        case 'invalid':
          setNotice(INVALID_NOTICE);
          return outcome;
        case 'unauthorized':
          // T-509 owns the response. Saying nothing is better than inventing copy for a state
          // this build cannot yet reach.
          return outcome;
      }
    },
    [],
  );

  const upload = useCallback(
    (files: readonly File[], scope: UploadScope) => {
      for (const file of files) {
        nextLocalId.current += 1;
        const localId = `upload-${nextLocalId.current}`;
        setPending((current) => [...current, { localId, filename: file.name, documentId: null }]);
        void uploadDocument(file, scope, conversationId).then((outcome) => {
          const applied = applyOutcome(outcome, 'upload', null);
          setPending((current) =>
            applied.kind === 'accepted' || applied.kind === 'duplicate'
              ? current.map((entry) =>
                  entry.localId === localId ? { ...entry, documentId: applied.documentId } : entry,
                )
              : current.filter((entry) => entry.localId !== localId),
          );
        });
      }
    },
    [applyOutcome, conversationId],
  );

  const act = useCallback(
    (document: DocumentEvent, action: RowAction, file?: File) => {
      if (
        actionBlock(document, action, { turnInFlight: turnInFlight || serverSaysBusy }) !== null
      ) {
        return;
      }
      const call =
        action === 'retry'
          ? retryDocument(document.document_id)
          : action === 'delete'
            ? deleteDocument(document.document_id)
            : replaceDocument(document.document_id, file as File);
      void call.then((outcome) => applyOutcome(outcome, action, document));
    },
    [applyOutcome, serverSaysBusy, turnInFlight],
  );

  /**
   * A pending entry is DROPPED — not filtered — the moment its real row arrives.
   *
   * Matched on the id the server returned, never on the filename (R-40/FR-KBM-08 reject filename
   * as identity). It has to be a state change rather than a render-time filter, and the live pass
   * is what proved it: filtering on "the row is present" makes the hand-off *reversible*, so
   * deleting the document weeks later flips the condition back and an `Uploading…` row for a
   * file that no longer exists reappears above the list. Observed exactly that, on the delete.
   */
  useEffect(() => {
    setPending((current) => {
      const settled = current.filter(
        (entry) =>
          entry.documentId === null ||
          !state.rows.some((row) => row.document_id === entry.documentId),
      );
      return settled.length === current.length ? current : settled;
    });
  }, [state.rows]);

  return {
    rows: state.rows,
    pending,
    connection,
    announcement: state.announcement,
    notice: notice ?? (connection === 'capped' ? STREAM_CAPPED_NOTICE : null),
    paused: turnInFlight || serverSaysBusy,
    upload,
    act,
    dismissNotice: useCallback(() => setNotice(null), []),
  };
}

/**
 * How long an unpredicted lock `409` keeps the affordances disabled. TBD(§8.4).
 *
 * Short on purpose: a turn is seconds, and the cost of lapsing early is one more `409` that
 * re-arms it, while the cost of lapsing late is a modal that looks broken. Deliberately NOT
 * `GRAPH_LOCK_TTL_SECONDS` (180 s) — that is the server's crash-release ceiling, not the
 * expected duration of anything.
 */
const SERVER_BUSY_LAPSE_MS = 5_000;

// Copy this surface owns rather than the server. Each is a state the server has no answer for:
// a duplicate is a `200` with no body to render, a `422` must never show its validation array,
// and the stream cap is reported by a route the modal is still working without.
const DUPLICATE_NOTICE = 'That file is already in your knowledge base.'; // TBD(§8.4) FR-KBM-08
const INVALID_NOTICE = 'That request could not be sent. Reload the page and try again.'; // TBD(§8.4)
const STREAM_CAPPED_NOTICE = // TBD(§8.4) — R-41(7)
  'Live updates are paused — too many Corpus tabs are open. Close one and reopen this panel.';
