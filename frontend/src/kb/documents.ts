/**
 * The §4.7 knowledge-base modal's decidable half: the R-41 store, the FR-KBM-04 derivations and
 * the FR-KBM-07 action rules.
 *
 * Pure — no React, no `fetch`, no DOM. Everything the spec *enumerates* lives here so it can be
 * tested without rendering anything, on the `mentions.ts` / `stats.ts` precedent; the component
 * keeps focus, ARIA, drag events and paint, and `useDocumentStream` keeps the transport.
 */
import { documentTypeBadge } from '../stats/stats';
import type { Document, DocumentEvent, DocumentFrame, DocumentStatus } from '../api';

/**
 * The store.
 *
 * One row type, never `Document | DocumentEvent`: the REST list and the SSE stream describe the
 * same rows and no component should have to know which one a row arrived on. `asEventRow` widens
 * the list's rows on the way in.
 */
export interface DocumentState {
  /** Ordered `created_at DESC, id DESC` — the order both server surfaces already use (R-40). */
  readonly rows: readonly DocumentEvent[];
  /** NFR-A11Y-05's polite announcement for the most recent transition, or null. */
  readonly announcement: string | null;
}

export const EMPTY_DOCUMENTS: DocumentState = { rows: [], announcement: null };

export type DocumentAction =
  | { type: 'list'; documents: readonly Document[] }
  | { type: 'snapshot'; documents: readonly DocumentEvent[] }
  | { type: 'document'; document: DocumentEvent }
  | { type: 'removed'; documentId: string }
  /** A `202`/`200` body: the server's own answer, one field of one row it already knows. */
  | { type: 'status'; documentId: string; status: DocumentStatus };

/**
 * `GET /documents` rows, widened.
 *
 * `stalled` is derived by the poll loop and the list route cannot know it, so `false` is the
 * honest default: its only consequence is showing the in-progress animation on a row that may
 * have gone quiet, which is the pre-R-41 behaviour and self-corrects on the first snapshot.
 */
export function asEventRow(document: Document): DocumentEvent {
  return { ...document, stalled: false };
}

/** The frame's own `event` key is the discriminator — never the SSE `event:` line (R-57(5)). */
export function actionFor(frame: DocumentFrame): DocumentAction {
  switch (frame.event) {
    case 'snapshot':
      return { type: 'snapshot', documents: frame.data };
    case 'document':
      return { type: 'document', document: frame.data };
    case 'removed':
      return { type: 'removed', documentId: frame.data.document_id };
  }
}

export function documentsReducer(state: DocumentState, action: DocumentAction): DocumentState {
  switch (action.type) {
    case 'list':
      return { rows: sortRows(action.documents.map(asEventRow)), announcement: null };

    // REPLACES, never merges. R-41(6) makes the snapshot authoritative on every connect — and a
    // merge would resurrect a row that was deleted while the stream was disconnected, which is
    // precisely the case the snapshot-per-connect design exists to handle.
    case 'snapshot':
      return { rows: sortRows(action.documents), announcement: null };

    case 'document': {
      const previous = state.rows.find((row) => row.document_id === action.document.document_id);
      return {
        rows: upsert(state.rows, action.document),
        announcement: announceTransition(previous, action.document),
      };
    }

    case 'removed': {
      const rows = state.rows.filter((row) => row.document_id !== action.documentId);
      // Identity, not a new array: a `removed` for an id a snapshot already dropped is ordinary
      // (R-41(4)), and returning a fresh array would re-render the list for nothing.
      if (rows.length === state.rows.length) return state;
      return { rows, announcement: null };
    }

    case 'status': {
      const row = state.rows.find((r) => r.document_id === action.documentId);
      // Never inserts. An upload has no row yet and is carried as a pending entry instead; this
      // action exists only to apply a verb's own `202` to a row the store already holds.
      if (row === undefined || row.status === action.status) return state;
      return {
        rows: state.rows.map((r) =>
          r.document_id === action.documentId ? { ...r, status: action.status } : r,
        ),
        announcement: null,
      };
    }
  }
}

/**
 * `created_at DESC, id DESC` — R-40's order, restated here rather than trusted.
 *
 * The list route already sorts, but a `document` frame arriving for a row the client has never
 * seen has to be *placed*, and placing it anywhere else would make the modal's order depend on
 * when a row happened to change.
 */
function sortRows(rows: readonly DocumentEvent[]): readonly DocumentEvent[] {
  return [...rows].sort((a, b) => {
    if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1;
    return a.document_id < b.document_id ? 1 : -1;
  });
}

/** Replace in place if present, insert in order if not. Untouched rows keep their identity. */
function upsert(rows: readonly DocumentEvent[], document: DocumentEvent): readonly DocumentEvent[] {
  const at = rows.findIndex((row) => row.document_id === document.document_id);
  if (at === -1) return sortRows([...rows, document]);
  const next = [...rows];
  next[at] = document;
  return next;
}

// --- FR-KBM-03: the two sections ------------------------------------------------------

export interface DocumentSections {
  readonly global: readonly DocumentEvent[];
  readonly chat: readonly DocumentEvent[];
}

/**
 * Split one unscoped set into FR-KBM-03's two sections.
 *
 * The modal reads **one** stream with no `scope` and divides it here, rather than opening a
 * second scoped stream: `SSE_MAX_STREAMS_PER_USER` is 4 *per user* and each stream is a recurring
 * 1.5 s query, so a second one costs a slot and a query per tab for a strict subset of rows this
 * one already carries. It also makes switching conversations a re-selection over state already in
 * memory rather than a teardown and reconnect.
 */
export function splitByScope(
  rows: readonly DocumentEvent[],
  conversationId: string | null,
): DocumentSections {
  return {
    global: rows.filter((row) => row.scope === 'global'),
    chat:
      conversationId === null
        ? []
        : rows.filter((row) => row.scope === 'chat' && row.conversation_id === conversationId),
  };
}

/**
 * FR-SBR-05 / FR-CMP-06's count: global documents **plus the active chat's attachments**.
 *
 * One derivation feeding both surfaces, because the two requirements name the same FR-ORC-06
 * retrieval scope and two constants is how they stop agreeing.
 */
export function documentCount(
  rows: readonly DocumentEvent[],
  conversationId: string | null,
): number {
  const { global, chat } = splitByScope(rows, conversationId);
  return global.length + chat.length;
}

// --- FR-KBM-04: labels, meta, status --------------------------------------------------

export type StatusLabel =
  'Queued' | 'Parsing' | 'Chunking' | 'Embedding' | 'Indexing' | 'Ready' | 'Failed' | 'Deleting';

/**
 * The eight FR-KBM-04 labels over FR-ING-01's eleven states.
 *
 * `UPLOADED` renders as `Queued` and `DELETE_PENDING` as `Deleting`, both named by the
 * requirement. `DELETED` is `null` because a tombstone never reaches this surface — the list
 * route excludes it and the stream reports its departure as `removed` (R-41(4)) — so there is
 * nothing for it to label, and inventing a ninth label for a row that cannot render would be the
 * mistake R-41(5) spends a paragraph forbidding.
 */
const LABELS: Record<DocumentStatus, StatusLabel | null> = {
  UPLOADED: 'Queued',
  QUEUED: 'Queued',
  PARSING: 'Parsing',
  CHUNKING: 'Chunking',
  EMBEDDING: 'Embedding',
  INDEXING: 'Indexing',
  ACTIVE: 'Ready',
  FAILED: 'Failed',
  DELETE_PENDING: 'Deleting',
  DELETING: 'Deleting',
  DELETED: null,
};

export function statusLabel(status: DocumentStatus): StatusLabel | null {
  return LABELS[status];
}

/** The five in-flight ingestion states — the ones an animation would describe. */
const IN_PROGRESS: ReadonlySet<DocumentStatus> = new Set<DocumentStatus>([
  'UPLOADED',
  'QUEUED',
  'PARSING',
  'CHUNKING',
  'EMBEDDING',
  'INDEXING',
]);

/**
 * Whether the row shows the FR-KBM-04 in-progress animation.
 *
 * `stalled` keeps the label and drops the motion — R-41(5) is explicit that this is a sufficient
 * response and needs no new copy. Deletion states are excluded because they are not ingestion
 * progress, and R-39(7) parks a dead-lettered purge in `Deleting` indefinitely on purpose.
 */
export function showsProgress(document: DocumentEvent): boolean {
  return IN_PROGRESS.has(document.status) && !document.stalled;
}

/**
 * OI-29 (R-71(2)): a *replace* failed while the previous version keeps answering.
 *
 * The job targeted `current_version + 1`, so a `FAILED` document whose newest job aimed higher
 * than the version still serving is not silent — R-36(3) leaves v(n) searchable throughout. The
 * bare red badge conflates that with a document that never ingested at all, and this is the
 * relationship that separates them (R-40(6) puts both fields on the DTO for exactly this).
 */
export function replaceFailed(document: DocumentEvent): boolean {
  return (
    document.status === 'FAILED' &&
    document.latest_job_document_version !== null &&
    document.latest_job_document_version > document.current_version
  );
}

/**
 * FR-KBM-04's meta line: `{unit} · indexed {date}`, plus OI-29's qualifier when it applies.
 *
 * **The unit falls back to chunks (R-71(4)).** `page_count` is PDF-only by R-34 — DOCX and
 * Markdown store no pagination and a synthesised page number would be a citation the user cannot
 * verify — and no row count is carried anywhere on the wire, so a CSV cannot render the
 * prototype's "1,204 rows" either. `chunk_count` is on the DTO and FR-KBM-08 sanctions showing
 * it. When neither exists the clause is dropped rather than rendered as a zero.
 */
export function metaLine(document: DocumentEvent): string {
  const parts: string[] = [];
  const unit = unitCount(document);
  if (unit !== null) parts.push(unit);
  parts.push(`indexed ${indexedOn(document)}`);
  if (replaceFailed(document)) {
    parts.push(`update failed, v${document.current_version} still answering`);
  }
  return parts.join(' · ');
}

function unitCount(document: DocumentEvent): string | null {
  // Singular is rendered as such — R-70(7)'s `1 passages` finding, one surface over.
  if (document.page_count !== null) return plural(document.page_count, 'page');
  if (document.chunk_count !== null) return plural(document.chunk_count, 'chunk');
  return null;
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString('en-US')} ${noun}${count === 1 ? '' : 's'}`;
}

/**
 * The prototype's `Jul 08` — month short, day zero-padded.
 *
 * `updated_at`, not `created_at`: "indexed" is when the document last *became* what it is, which
 * is the timestamp every FR-ING-01 stage write moves. (R-40 orders the *list* by `created_at`
 * for a different reason — so live rows do not leapfrog each other — and says as much.)
 *
 * The locale is pinned rather than left to the runtime: an unpinned `toLocaleDateString` renders
 * `08 Jul` in half the world and the prototype's column would jump.
 */
export function indexedOn(document: DocumentEvent): string {
  return new Date(document.updated_at).toLocaleDateString('en-US', {
    month: 'short',
    day: '2-digit',
  });
}

/** FR-KBM-03's section headings. Pluralised — FR-KBM-03's literal has no singular case. */
export function sectionHeading(kind: 'global' | 'chat', count: number): string {
  return kind === 'global'
    ? `GLOBAL · ${count} DOCUMENT${count === 1 ? '' : 'S'}`
    : `THIS CHAT · ${count} ATTACHMENT${count === 1 ? '' : 'S'}`;
}

/** The 34px mono badge. Shared with FR-ANL-05 rather than re-derived — `.docx` is `DOC`. */
export function badgeType(filename: string): string {
  return documentTypeBadge(filename);
}

/**
 * FR-CMP-04's mention rows, from the same set the modal shows.
 *
 * The composer's menu names the FR-ORC-06 retrieval scope, which is exactly `splitByScope`'s two
 * halves — so it is derived here rather than fetched again. `mentionMeta` performs FR-CMP-04's
 * `this chat` substitution itself, so the `meta` supplied for a chat-scoped row is never read;
 * it is filled honestly anyway rather than left blank, because a value that is only correct
 * because nobody looks at it is the kind that surfaces later.
 */
export function toMentionDocuments(
  rows: readonly DocumentEvent[],
  conversationId: string | null,
): { id: string; name: string; type: string; meta: string; scope: 'global' | 'chat' }[] {
  const { global, chat } = splitByScope(rows, conversationId);
  return [...global, ...chat].map((row) => ({
    id: row.document_id,
    name: row.filename,
    type: badgeType(row.filename),
    meta: unitCount(row) ?? '',
    scope: row.scope,
  }));
}

// --- FR-KBM-07: the three lifecycle actions -------------------------------------------

export type RowAction = 'retry' | 'replace' | 'delete';

/** Why an action is unavailable, or `null` when it is available. */
export type ActionBlock = 'turn-in-flight' | 'wrong-state' | null;

/**
 * Whether FR-KBM-07's action is offered on this row, and if not, which reason.
 *
 * Ordered deliberately, like `sendBlock` in `mentions.ts`: the **state** rule comes first because
 * it is permanent for this row, while the turn gate is momentary. A Retry offered on an `ACTIVE`
 * document would be wrong even in a perfectly idle app, and reporting "paused" for it would send
 * the user to wait for something that will not help.
 */
export function actionBlock(
  document: DocumentEvent,
  action: RowAction,
  context: { turnInFlight: boolean },
): ActionBlock {
  if (!allowedFromState(document, action)) return 'wrong-state';
  if (context.turnInFlight) return 'turn-in-flight';
  return null;
}

function allowedFromState(document: DocumentEvent, action: RowAction): boolean {
  switch (action) {
    // FR-ING-06 / R-39(5): only from FAILED. R-38(5) means a retryable failure never lands there,
    // so any other state is either in flight or already succeeded.
    case 'retry':
      return document.status === 'FAILED';
    // R-40(2): ACTIVE or FAILED only — anything else is a build in progress.
    case 'replace':
      return document.status === 'ACTIVE' || document.status === 'FAILED';
    // Deleting an already-deleting document is a no-op the server answers `200` to, but offering
    // the button invites a second confirmation dialog for nothing.
    case 'delete':
      return document.status !== 'DELETE_PENDING' && document.status !== 'DELETING';
  }
}

/**
 * Could this `409` only have been the R-24 processing lock?
 *
 * R-71(1) makes the GUI reconcile the lock's `409` rather than render it as an error, which
 * requires telling it from the other `409`s these routes answer. Since Rev 0.38 the lock's body
 * carries `error_code: "PROCESSING_LOCKED"` and that is the authority; this is the fallback for a
 * server that predates it, and it is deliberately **conservative** — it claims certainty only
 * where the route has no other way to produce a `409`.
 *
 * - `upload` and `delete` map only `ProcessingLockedError` to `409`.
 * - `retry` also answers `NotRetryableError`, which cannot arise from a row the store shows as
 *   `FAILED` — so it is certain exactly then.
 * - `replace` additionally answers `DuplicateChecksumError`, reachable from the same states, so
 *   it is **never** certain. That gap is why the `error_code` was added.
 */
export function impliesProcessingLock(
  action: 'upload' | RowAction,
  document: DocumentEvent | null,
): boolean {
  switch (action) {
    case 'upload':
    case 'delete':
      return true;
    case 'retry':
      return document !== null && document.status === 'FAILED';
    case 'replace':
      return false;
  }
}

// --- NFR-A11Y-05 ----------------------------------------------------------------------

/**
 * The polite announcement for one row's transition, or `null` when there is nothing new to say.
 *
 * Only *transitions* are announced — never the opening snapshot, which would read twenty
 * documents aloud to someone who has just opened a modal, and never unchanged content, which
 * NFR-A11Y-05 forbids in as many words. A row the client has not seen before is also silent: its
 * arrival is the user's own upload, which the pending entry already reports.
 */
export function announceTransition(
  previous: DocumentEvent | undefined,
  next: DocumentEvent,
): string | null {
  if (previous === undefined || previous.status === next.status) return null;
  const label = statusLabel(next.status);
  return label === null ? null : `${next.filename} is now ${label}.`;
}
