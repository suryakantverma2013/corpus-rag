/**
 * A destructive-action confirmation, composed from {@link Dialog}.
 *
 * FR-SBR-07 requires one before deleting a conversation and FR-KBM-07 before deleting a
 * document (T-508), so the shape is shared and only the copy differs.
 *
 * **All copy is supplied by the caller and is `TBD(§8.4)`** — the spec's remaining-TBD list
 * names "confirmation-dialog copy for conversation delete (FR-SBR-07) and document delete
 * (FR-KBM-07)" explicitly, so this component invents none of it and §9 gains no literal.
 */
import styles from './ConfirmDialog.module.css';
import { Dialog } from './Dialog';

export interface ConfirmDialogProps {
  title: string;
  /** The consequence, in the caller's words. Kept a plain string: a destructive confirmation
   *  that can render arbitrary nodes invites a link or a form into the one dialog whose whole
   *  job is to ask a single question. */
  body: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog title={title} onClose={onCancel} panelClassName={styles.panel}>
      <p className={styles.body}>{body}</p>
      <div className={styles.actions}>
        {/* Cancel is first in DOM order, so it takes initial focus (Dialog focuses the first
            focusable element). On a destructive dialog the safe action is the right default —
            a stray Enter should dismiss, never delete. */}
        <button type="button" className={styles.cancel} onClick={onCancel}>
          {cancelLabel}
        </button>
        <button type="button" className={styles.confirm} onClick={onConfirm}>
          {confirmLabel}
        </button>
      </div>
    </Dialog>
  );
}
