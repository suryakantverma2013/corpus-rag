/**
 * The §4.7 knowledge-base modal (FR-KBM-01..09).
 *
 * Composed from `ui/Dialog`, which was written in T-503 for exactly this surface: it carries the
 * NFR-A11Y-04 contract (trap, Escape, restore) and the prototype's overlay and panel values, and
 * this task only supplies FR-KBM-01's 520px / 78vh panel and the contents.
 *
 * **Structure follows the prototype exactly** (`RAG Chatbot.dc.html` lines 242–277): header with
 * ✕, subtitle, GLOBAL section, THIS CHAT section, drop zone, footer. Note the drop zone sits
 * *below* both lists, not above them.
 */
import { useState } from 'react';

import type { DocumentEvent, UploadScope } from '../api';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { Dialog } from '../ui/Dialog';
import { DocumentRow } from './DocumentRow';
import { DropZone } from './DropZone';
import { sectionHeading, splitByScope } from './documents';
import type { RowAction } from './documents';
import type { DocumentsStore } from './useDocuments';
import styles from './KnowledgeBaseModal.module.css';

/** §9's literals, verbatim. */
const TITLE = 'Knowledge base';
const SUBTITLE = 'Global documents are searched in every chat; attachments only in this one.';

/** TBD(§8.4) — no empty state is specified for this modal. NFR-USE-04 enumerates three and this
 *  is not one, and §9 carries no string; the T-506 mention-menu precedent is to ship one and
 *  record it, because a real knowledge base starts empty and a heading over nothing reads as a
 *  failure rather than as a beginning. */
const EMPTY_GLOBAL = 'No documents yet — add one below.';
const EMPTY_CHAT = 'No attachments in this chat.';

/** R-71(1): the pause is surfaced, not merely enforced. TBD(§8.4). */
const PAUSED_NOTICE = 'Paused while a response is being generated.';

/** FR-KBM-07's confirmation. All four strings are TBD(§8.4) — the spec parks document-delete
 *  copy there by name, so this component invents no §9 literal. */
const CONFIRM_TITLE = 'Delete this document?';
const CONFIRM_BODY =
  'It will stop being used in answers immediately, and its stored file and index entries are ' +
  'removed. This cannot be undone.';
const CONFIRM_DELETE = 'Delete';
const CONFIRM_CANCEL = 'Cancel';

const CLOSE = 'Close';
const CLOUD = 'Add from cloud drive';
const DISMISS = 'Dismiss';

export interface KnowledgeBaseModalProps {
  store: DocumentsStore;
  /** FR-KBM-03's second section, and the target of a `scope=chat` upload. */
  conversationId: string | null;
  onClose: () => void;
  /** FR-KBM-06. T-512 owns the picker; until then this presents the FR-AUT-11 unlinked state. */
  onCloudImport: () => void;
}

export function KnowledgeBaseModal({
  store,
  conversationId,
  onClose,
  onCloudImport,
}: KnowledgeBaseModalProps) {
  const [scope, setScope] = useState<UploadScope>('global');
  const [pendingDelete, setPendingDelete] = useState<DocumentEvent | null>(null);

  const sections = splitByScope(store.rows, conversationId);
  const chatAvailable = conversationId !== null;

  const onAction = (document: DocumentEvent, action: RowAction, file?: File) => {
    // R-56(5)'s no-undo mitigation, one surface over: deletion is asynchronous and irreversible,
    // so it confirms first. Retry and Replace are neither.
    if (action === 'delete') setPendingDelete(document);
    else store.act(document, action, file);
  };

  return (
    <Dialog
      title={TITLE}
      onClose={onClose}
      panelClassName={styles.panel}
      headerAction={
        /* The glyph needs a name, and it must not be the footer button's — two controls called
           "Close" are indistinguishable in a screen reader's element list. */
        <button
          type="button"
          className={styles.close}
          aria-label="Close knowledge base"
          onClick={onClose}
        >
          ✕
        </button>
      }
    >
      <p className={styles.subtitle}>{SUBTITLE}</p>

      {/* One notice slot, inside the panel and above the lists — never per row. The panel is a
          scroll container (`max-height: 78vh`), so a notice attached to a row can be scrolled out
          of sight, which is the same measurement T-503 made about its menu. One slot also means a
          second notice replaces the first instead of stacking a column of them. */}
      <div className={styles.notices}>
        {store.paused && (
          <p className={styles.paused} role="status">
            {PAUSED_NOTICE}
          </p>
        )}
        {store.notice !== null && (
          <p className={styles.notice} role="status">
            <span>{store.notice}</span>
            <button type="button" className={styles.dismiss} onClick={store.dismissNotice}>
              {DISMISS}
            </button>
          </p>
        )}
      </div>

      {/* NFR-A11Y-05: FR-KBM-09's status transitions, announced politely. Transitions only —
          never the opening snapshot, which would read the whole list aloud to someone who has
          just opened a modal, and never unchanged content. */}
      <div className="visually-hidden" role="status" aria-live="polite">
        {store.announcement}
      </div>

      <Section
        heading={sectionHeading('global', sections.global.length)}
        empty={EMPTY_GLOBAL}
        documents={sections.global}
        pending={store.pending}
        paused={store.paused}
        onAction={onAction}
      />

      <Section
        heading={sectionHeading('chat', sections.chat.length)}
        empty={chatAvailable ? EMPTY_CHAT : null}
        documents={sections.chat}
        pending={[]}
        paused={store.paused}
        onAction={onAction}
      />

      <DropZone
        scope={scope}
        onScopeChange={setScope}
        chatAvailable={chatAvailable}
        disabled={store.paused}
        onFiles={(files) => store.upload(files, scope)}
      />

      <div className={styles.footer}>
        <button type="button" className={styles.secondary} onClick={onClose}>
          {CLOSE}
        </button>
        {/* FR-KBM-06 keeps its prototype position and accent-primary styling. T-512 owns the
            selection surface; until it lands this initiates FR-AUT-11 linking, which is what the
            requirement says the button does for an unlinked account — and §8.52's rule is that a
            withdrawn or pending feature's prototype control is retargeted, never left inert. */}
        <button type="button" className={styles.primary} onClick={onCloudImport}>
          {CLOUD}
        </button>
      </div>

      {pendingDelete !== null && (
        <ConfirmDialog
          title={CONFIRM_TITLE}
          body={CONFIRM_BODY}
          confirmLabel={CONFIRM_DELETE}
          cancelLabel={CONFIRM_CANCEL}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            store.act(pendingDelete, 'delete');
            setPendingDelete(null);
          }}
        />
      )}
    </Dialog>
  );
}

interface SectionProps {
  heading: string;
  /** `null` suppresses the empty state — a chat that does not exist yet has nothing to say. */
  empty: string | null;
  documents: readonly DocumentEvent[];
  pending: DocumentsStore['pending'];
  paused: boolean;
  onAction: (document: DocumentEvent, action: RowAction, file?: File) => void;
}

function Section({ heading, empty, documents, pending, paused, onAction }: SectionProps) {
  const headingId = heading.replace(/[^a-z]+/gi, '-').toLowerCase();
  return (
    <>
      {/* <h3>: T-502 owns the document's only <h1> and Dialog's title is the <h2>. */}
      <h3 className={styles.sectionLabel} id={headingId}>
        {heading}
      </h3>
      {documents.length === 0 && pending.length === 0 ? (
        empty === null ? null : (
          <p className={styles.empty}>{empty}</p>
        )
      ) : (
        <ul className={styles.list} aria-labelledby={headingId}>
          {pending.map((entry) => (
            <li key={entry.localId} className={styles.pending}>
              <span className={styles.pendingName}>{entry.filename}</span>
              <span className={styles.pendingState}>Uploading…</span>
            </li>
          ))}
          {documents.map((document) => (
            <DocumentRow
              key={document.document_id}
              document={document}
              paused={paused}
              onAction={onAction}
            />
          ))}
        </ul>
      )}
    </>
  );
}
