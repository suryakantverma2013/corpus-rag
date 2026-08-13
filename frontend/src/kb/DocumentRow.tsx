/**
 * One FR-KBM-04 row, plus FR-KBM-07's three lifecycle actions.
 *
 * The prototype's row is five inline-styled `<div>`s ending in a hard-coded `Ready`. The status
 * label set (FR-KBM-04) and the actions (FR-KBM-07) are Rev 0.5 additions where **the spec is the
 * baseline, not the prototype** (NFR-VIS-01's carve-out) — so the box, the badge, the ellipsised
 * name and the meta line are transcribed per property, and everything that moves is derived from
 * the requirement.
 *
 * **The actions are inline and revealed, not a `⋯` menu.** T-503 needed a menu because a 264px
 * sidebar row has no space, and paid for it with `position: fixed` and hand-computed coordinates
 * — its own measurement showed an absolutely-positioned popover is clipped by the scroll
 * container it lives in, and this panel is a scroll container (`max-height: 78vh`). At 520px
 * there is room for the buttons themselves, which removes that whole failure mode and gives each
 * action a real name in the tab order instead of hiding three behind one unlabelled control.
 */
import { useRef } from 'react';

import type { DocumentEvent } from '../api';
import {
  actionBlock,
  badgeType,
  metaLine,
  replaceFailed,
  showsProgress,
  statusLabel,
} from './documents';
import type { RowAction } from './documents';
import styles from './DocumentRow.module.css';

/** FR-ING-02's four formats. A picker hint only — drag-and-drop bypasses it and the backend
 *  sniffs magic bytes regardless (R-31(3)), so this can never be the enforcement. */
export const ACCEPT = '.pdf,.docx,.csv,.md';

const ACTION_LABELS: Record<RowAction, string> = {
  retry: 'Retry',
  replace: 'Replace',
  delete: 'Delete',
};

export interface DocumentRowProps {
  document: DocumentEvent;
  /** OI-31 / R-71(1) — our own in-flight signal, or a server `409` this client had not predicted. */
  paused: boolean;
  onAction: (document: DocumentEvent, action: RowAction, file?: File) => void;
}

export function DocumentRow({ document, paused, onAction }: DocumentRowProps) {
  const fileRef = useRef<HTMLInputElement>(null);

  const label = statusLabel(document.status);
  // The state rule only: `paused` disables the buttons rather than removing them, because a
  // control that vanishes for two seconds is harder to understand than one that is greyed with a
  // reason beside it — which is what R-71(1) means by surfacing the pause.
  const actions = (['retry', 'replace', 'delete'] as const).filter(
    (action) => actionBlock(document, action, { turnInFlight: false }) === null,
  );

  return (
    <li className={styles.row}>
      <span className={`${styles.badge} mono`}>{badgeType(document.filename)}</span>

      <div className={styles.body}>
        <div className={styles.name} title={document.filename}>
          {document.filename}
        </div>
        <div className={styles.meta}>{metaLine(document)}</div>
      </div>

      {label !== null && (
        <span className={statusClass(document)}>
          {label}
          {/* FR-KBM-04's in-progress motion. `stalled` keeps the label and drops the animation,
              which R-41(5) makes a sufficient response needing no new copy. The dots use the
              GLOBAL utility: a keyframe named inside a CSS Module compiles to a hashed name
              matching no `@keyframes` and silently never runs (R-69 / T-505). */}
          {showsProgress(document) && (
            <span className={`${styles.dots} animate-dot-pulse`} aria-hidden="true" />
          )}
        </span>
      )}

      {actions.length > 0 && (
        <div className={styles.actions}>
          {actions.map((action) => (
            <button
              key={action}
              type="button"
              className={action === 'delete' ? styles.destructive : styles.action}
              disabled={paused}
              // The row is one of many, so the name alone ("Delete") would repeat down the list
              // with nothing to tell a screen-reader user which document it belongs to.
              aria-label={`${ACTION_LABELS[action]} ${document.filename}`}
              onClick={() => {
                if (action === 'replace') fileRef.current?.click();
                else onAction(document, action);
              }}
            >
              {ACTION_LABELS[action]}
            </button>
          ))}

          {/* R-40(1): Replace is an explicit endpoint on a document id — never a same-named
              re-upload, which would make every FR-KBM-05 drag-and-drop silently destructive. */}
          <input
            ref={fileRef}
            className="visually-hidden"
            type="file"
            accept={ACCEPT}
            tabIndex={-1}
            aria-hidden="true"
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Reset first: choosing the same file twice fires no `change` otherwise, so a
              // failed replace could not be retried with the identical file.
              event.target.value = '';
              if (file !== undefined) onAction(document, 'replace', file);
            }}
          />
        </div>
      )}
    </li>
  );
}

/**
 * The status label's colour, per FR-KBM-04.
 *
 * A literal map, never `styles[...]`: the CSS-Modules key guard matches `styles.name` textually,
 * so a computed key is invisible to it and a typo ships as `class="undefined"`.
 *
 * NFR-A11Y-06: colour is never the sole carrier — the label's own text says what it says, and
 * OI-29's failed-but-serving replace is disambiguated in words on the meta line, not by a hue.
 * `failedServing` exists so that row is not painted the same alarming red as a document that is
 * genuinely silent, while keeping the `Failed` word the requirement fixes.
 */
function statusClass(document: DocumentEvent): string {
  if (document.status === 'ACTIVE') return `${styles.status} ${styles.ready}`;
  if (document.status === 'FAILED') {
    return replaceFailed(document)
      ? `${styles.status} ${styles.failedServing}`
      : `${styles.status} ${styles.failed}`;
  }
  if (document.status === 'DELETE_PENDING' || document.status === 'DELETING') {
    return `${styles.status} ${styles.deleting}`;
  }
  return `${styles.status} ${styles.progress}`;
}
