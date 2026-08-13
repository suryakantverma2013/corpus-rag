/**
 * FR-AUT-01..05 — the login screen.
 *
 * Replaces the shell entirely rather than living inside it (FR-AUT-07): the app has no router,
 * so "every GUI route except login requires authentication" is enforced by `App` mounting one
 * or the other. Nothing behind this screen is mounted, so no effect of T-508's document stream
 * or T-513's chat stream can run against a session that does not exist.
 *
 * The five states FR-AUT-01 enumerates are two pieces of state, not five: `pending` (FR-AUT-03)
 * and `error` (FR-AUT-04), with *expired* supplied by the provider and *default* being neither.
 */
import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import styles from './LoginScreen.module.css';
import { useAuth } from './useAuth';
import {
  EMAIL_LABEL,
  FORGOT_PASSWORD,
  HIDE_PASSWORD,
  LOGIN_SUBTITLE,
  PASSWORD_LABEL,
  SESSION_EXPIRED,
  SHOW_PASSWORD,
  SIGN_IN,
  SIGNING_IN,
} from './copy';

export interface LoginScreenProps {
  /** FR-SYS-04 — resolved in `App`, because the brand appears on both sides of the shell
   *  boundary and this screen is the side that replaces it. */
  brandName: string;
  /** The §9 mono version tag ("v1.4"), same literal as FR-SBR-06's sidebar row. */
  version: string;
}

export function LoginScreen({ brandName, version }: LoginScreenProps) {
  const { expired, signIn } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [revealed, setRevealed] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    // Cleared on submit, not on keystroke: FR-AUT-04's message describes the last *attempt*,
    // and wiping it as the user corrects a typo removes the thing they are reading.
    setError(null);
    const result = await signIn(email, password);
    // On success this component unmounts as `App` switches to the shell, so only the failure
    // path has anything to write back.
    if (!result.ok) {
      setError(result.message);
      setPending(false);
    }
  }

  return (
    <div className={styles.screen}>
      {/* FR-AUT-06. Rendered above the card, and only after a real session ended — never on a
          first visit, where the server's 401 means "no session" rather than "expired".
          `role="status"` so it is announced rather than silently appearing (NFR-A11Y-05). */}
      {expired && (
        <div className={`${styles.expired} animate-fade-up`} role="status">
          {SESSION_EXPIRED}
        </div>
      )}

      <form className={`${styles.card} animate-fade-up`} onSubmit={onSubmit} noValidate>
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            C
          </span>
          {/* The document's only <h1> while this screen is mounted. T-502 owns the shell's,
              and the two are never mounted at the same time. */}
          <h1 className={styles.brandName}>{brandName}</h1>
          <p className={styles.subtitle}>{LOGIN_SUBTITLE}</p>
        </div>

        <label className={styles.label} htmlFor={emailId}>
          {EMAIL_LABEL}
        </label>
        <div className={styles.fieldRow}>
          <input
            id={emailId}
            className={styles.input}
            type="email"
            autoComplete="username"
            // FR-AUT-02. The one autofocus in the product: this screen has a single obvious
            // starting point and no content above it to skip past.
            autoFocus
            required
            disabled={pending}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={error !== null}
            aria-describedby={error === null ? undefined : errorId}
          />
        </div>

        <label className={styles.label} htmlFor={passwordId}>
          {PASSWORD_LABEL}
        </label>
        <div className={styles.fieldRow}>
          <input
            id={passwordId}
            className={`${styles.input} ${styles.password}`}
            type={revealed ? 'text' : 'password'}
            autoComplete="current-password"
            required
            disabled={pending}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-invalid={error !== null}
            aria-describedby={error === null ? undefined : errorId}
          />
          {/* FR-AUT-02's Show/Hide. `aria-pressed` carries the state for a screen reader, the
              way FR-HDR-03's theme segments do (T-504) — the label alone says what the next
              click does, not what the field is currently doing. */}
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={revealed}
            aria-controls={passwordId}
            disabled={pending}
            onClick={() => setRevealed((current) => !current)}
          >
            {revealed ? HIDE_PASSWORD : SHOW_PASSWORD}
          </button>
        </div>

        {/* FR-AUT-04. `role="alert"` because it appears in response to the user's action and
            must be announced without them going looking for it. */}
        {error !== null && (
          <div className={`${styles.error} animate-fade-up`} id={errorId} role="alert">
            {error}
          </div>
        )}

        {/* FR-AUT-03. Enter in either field submits by virtue of being a <form> with a submit
            button — no key handler, which is also what makes the browser's own validation and
            password-manager integration work. */}
        <button type="submit" className={styles.submit} disabled={pending}>
          {pending ? (
            <>
              {/* The label is replaced by the dots, so the accessible name has to come from
                  somewhere — a control that loses its name mid-action is announced as
                  "button" (NFR-A11Y-03). */}
              <span className="visually-hidden">{SIGNING_IN}</span>
              <span className={`${styles.dot} animate-dot-pulse`} aria-hidden="true" />
              <span className={`${styles.dot} animate-dot-pulse`} aria-hidden="true" />
              <span className={`${styles.dot} animate-dot-pulse`} aria-hidden="true" />
            </>
          ) : (
            SIGN_IN
          )}
        </button>

        {/* FR-AUT-05 — no reset link and no sign-up affordance, deliberately: there is no
            self-service reset (FR-USR-09) and accounts are administrator-created (FR-USR-03). */}
        <div className={styles.footer}>{FORGOT_PASSWORD}</div>
        <div className={`${styles.version} mono`}>{version}</div>
      </form>
    </div>
  );
}
