/**
 * FR-MSG-08 — the message action bar: 👍 / 👎 and Regenerate.
 *
 * A Rev 0.5 addition with **no prototype precedent** (the handoff has no action bar anywhere), so
 * under NFR-VIS-01's carve-out the spec is the baseline and the styling is its own words: "a
 * low-emphasis action row … using the existing icon-button styling (`--muted` default →
 * `--accent` on hover)".
 *
 * **Regenerate asks first.** R-56(5) records that a re-run replaces the row *unconditionally* —
 * an abstained or injection-blocked re-run replaces a good answer too — and that **there is no
 * undo**, because keeping a superseded copy would reintroduce the second-authoritative-transcript
 * problem R-42(1) refused. The ruling hands the warning to the GUI, and a confirmation is the
 * only mitigation available when the server keeps no prior version. It reuses `ConfirmDialog`,
 * the same component FR-SBR-07's delete uses, so the product has one shape for "this cannot be
 * undone" and one NFR-A11Y-04 focus contract behind it.
 *
 * **`busy` disables all three, and the two halves are enforced differently on the server.** For
 * Regenerate the lock is a server concern — it starts a turn, and R-56(6) has the turn *take* the
 * R-24 lock rather than the route check it. For feedback it is a GUI affordance only:
 * `POST /messages/{id}/feedback` is deliberately ungated (R-55(1)), because the lock is keyed on
 * the caller and gating would refuse feedback in one chat because a *different* one is mid-turn.
 */
import { useState } from 'react';
import styles from './MessageActions.module.css';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import type { Feedback } from '../api';

/** The confirmation copy is **FR-MSG-08's**, author-confirmed by R-69(1) and written into the
 *  requirement — no longer provisional, and no longer this file's to reword. The body names all
 *  three losses because R-56 writes `content`, `citations`, `evaluation` and `feedback` in one
 *  statement, and says "cannot be undone" because it cannot: this dialog *is* the mitigation. */
const REGENERATE_LABEL = 'Regenerate';
const CONFIRM_TITLE = 'Regenerate this answer?';
const CONFIRM_BODY =
  'The current answer, its sources and its evaluation scores will be replaced. This cannot be undone.';
const CONFIRM_ACCEPT = 'Regenerate';
const CONFIRM_CANCEL = 'Cancel';

/** A bare emoji is a poor accessible name — "thumbs up sign" tells a screen-reader user nothing
 *  about what it does here. NFR-A11Y-06 also needs the state carried by something other than the
 *  fill colour, which is `aria-pressed`. */
const UP_LABEL = 'Good answer';
const DOWN_LABEL = 'Poor answer';

export interface MessageActionsProps {
  feedback: Feedback | null | undefined;
  busy: boolean;
  /** `POST /api/v1/messages/{id}/feedback` — `null` clears, which is FR-MSG-06's third state. */
  onFeedback: (feedback: Feedback | null) => void;
  /** `POST /api/v1/messages/{id}/regenerate` — called only after the confirmation. */
  onRegenerate: () => void;
}

export function MessageActions({ feedback, busy, onFeedback, onRegenerate }: MessageActionsProps) {
  const [confirming, setConfirming] = useState(false);

  // FR-MSG-08's "feedback toggles": pressing the active one again clears it. Two states on the
  // wire plus null, never a third enum member — "cleared" and "never rated" are different facts
  // and the deferred OI-24 calibration loop is the consumer that must tell them apart.
  const toggle = (value: Feedback) => onFeedback(feedback === value ? null : value);

  return (
    <div className={styles.bar}>
      <button
        type="button"
        className={styles.action}
        aria-label={UP_LABEL}
        aria-pressed={feedback === 'up'}
        disabled={busy}
        onClick={() => toggle('up')}
      >
        <span aria-hidden="true">👍</span>
      </button>
      <button
        type="button"
        className={styles.action}
        aria-label={DOWN_LABEL}
        aria-pressed={feedback === 'down'}
        disabled={busy}
        onClick={() => toggle('down')}
      >
        <span aria-hidden="true">👎</span>
      </button>
      <button
        type="button"
        className={styles.action}
        disabled={busy}
        onClick={() => setConfirming(true)}
      >
        {REGENERATE_LABEL}
      </button>

      {confirming && (
        <ConfirmDialog
          title={CONFIRM_TITLE}
          body={CONFIRM_BODY}
          confirmLabel={CONFIRM_ACCEPT}
          cancelLabel={CONFIRM_CANCEL}
          onCancel={() => setConfirming(false)}
          onConfirm={() => {
            setConfirming(false);
            onRegenerate();
          }}
        />
      )}
    </div>
  );
}
