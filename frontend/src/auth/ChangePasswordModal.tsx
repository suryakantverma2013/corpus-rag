/**
 * FR-AUT-09 — the change-password modal.
 *
 * Built on `ui/Dialog`, which has carried this surface in its docstring since T-503: overlay,
 * focus trap, Escape and focus restore are all inherited, so the only thing here is the form.
 *
 * **The mismatch check is local; everything else is the server's answer.** "Passwords don't
 * match." is decidable in the browser and FR-AUT-09 names the string, so it never becomes a
 * request. Password *policy* is not decidable here — NFR-SEC-04's rules are Keycloak realm
 * configuration (R-28), so the realm is the only thing that knows them, and inventing a
 * client-side rule set would produce a modal that refuses passwords the server would accept
 * (and, worse, accepts ones it will not).
 */
import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import { Dialog } from '../ui/Dialog';
import styles from './ChangePasswordModal.module.css';
import { useAuth } from './useAuth';
import {
  CANCEL,
  CHANGE_PASSWORD_TITLE,
  CONFIRM_PASSWORD_LABEL,
  CURRENT_PASSWORD_LABEL,
  NEW_PASSWORD_LABEL,
  PASSWORDS_DONT_MATCH,
  SAVE_PASSWORD,
  SAVING_PASSWORD,
} from './copy';

export interface ChangePasswordModalProps {
  onClose: () => void;
  /** Announced in the shell's live region on success — R-72(5). The modal closing is the
   *  feedback for a sighted user and no feedback at all for anyone else. */
  onChanged: () => void;
}

export function ChangePasswordModal({ onClose, onChanged }: ChangePasswordModalProps) {
  const { changePassword } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentId = useId();
  const nextId = useId();
  const confirmId = useId();
  const errorId = useId();

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setError(null);
    if (next !== confirm) {
      setError(PASSWORDS_DONT_MATCH);
      return;
    }
    setPending(true);
    const result = await changePassword(current, next);
    if (result.ok) {
      onChanged();
      onClose();
      return;
    }
    setError(result.message);
    setPending(false);
  }

  const describedBy = error === null ? undefined : errorId;

  return (
    <Dialog
      title={CHANGE_PASSWORD_TITLE}
      onClose={onClose}
      panelClassName={styles.panel}
      headerAction={
        <button type="button" className={styles.close} onClick={onClose} aria-label={CANCEL}>
          ✕
        </button>
      }
    >
      <form onSubmit={onSubmit} noValidate>
        <label className={styles.label} htmlFor={currentId}>
          {CURRENT_PASSWORD_LABEL}
        </label>
        <input
          id={currentId}
          className={styles.input}
          type="password"
          autoComplete="current-password"
          required
          disabled={pending}
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          aria-describedby={describedBy}
        />

        <label className={styles.label} htmlFor={nextId}>
          {NEW_PASSWORD_LABEL}
        </label>
        <input
          id={nextId}
          className={styles.input}
          type="password"
          autoComplete="new-password"
          required
          disabled={pending}
          value={next}
          onChange={(event) => setNext(event.target.value)}
          aria-describedby={describedBy}
        />

        <label className={styles.label} htmlFor={confirmId}>
          {CONFIRM_PASSWORD_LABEL}
        </label>
        <input
          id={confirmId}
          className={styles.input}
          type="password"
          autoComplete="new-password"
          required
          disabled={pending}
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          aria-invalid={error === PASSWORDS_DONT_MATCH}
          aria-describedby={describedBy}
        />

        {error !== null && (
          <div className={`${styles.error} animate-fade-up`} id={errorId} role="alert">
            {error}
          </div>
        )}

        <div className={styles.footer}>
          {/* Ghost first, primary second — FR-AUT-09's "right-aligned Cancel + Save password",
              the same order as FR-KBM-06's footer. */}
          <button type="button" className={styles.secondary} onClick={onClose} disabled={pending}>
            {CANCEL}
          </button>
          <button type="submit" className={styles.primary} disabled={pending}>
            {pending ? SAVING_PASSWORD : SAVE_PASSWORD}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
