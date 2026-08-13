/**
 * §4.17's copy, verbatim, in one place (R-69(1)'s rule: a string lives at its requirement, and
 * every surface derives from it rather than from another surface).
 *
 * The §9 literal table governs the brand and version strings; these are FR-AUT-01..09's own.
 */
import type { ApiError } from '../api';

// --- FR-AUT-01 ---------------------------------------------------------------
export const LOGIN_SUBTITLE = 'Sign in to your knowledge base';

// --- FR-AUT-02 ---------------------------------------------------------------
export const EMAIL_LABEL = 'EMAIL';
export const PASSWORD_LABEL = 'PASSWORD';
export const SHOW_PASSWORD = 'Show';
export const HIDE_PASSWORD = 'Hide';

// --- FR-AUT-03 ---------------------------------------------------------------
export const SIGN_IN = 'Sign in';
/** NFR-A11Y-05: the `dotPulse` state replaces the label, so the button needs an accessible
 *  name while it is signing in — a control whose name vanishes is announced as "button". */
export const SIGNING_IN = 'Signing in…';

// --- FR-AUT-04 ---------------------------------------------------------------
export const INVALID_CREDENTIALS = 'Invalid email or password.';
export const TOO_MANY_ATTEMPTS = 'Too many attempts — try again later.';
/** FR-ERR-04's last-resort fallback, reached only when the server sends no `detail`. */
export const SYSTEM_FAILURE = 'System Failure: Please try again.';

// --- FR-AUT-05 ---------------------------------------------------------------
export const FORGOT_PASSWORD = 'Forgot your password? Contact your administrator.';

// --- FR-AUT-06 ---------------------------------------------------------------
export const SESSION_EXPIRED = 'Session expired — please sign in again.';

// --- FR-AUT-08 ---------------------------------------------------------------
export const CHANGE_PASSWORD_ITEM = 'Change password…';
export const SIGN_OUT = 'Sign out';
export const USER_MENU_LABEL = 'Account';

// --- FR-AUT-09 ---------------------------------------------------------------
export const CHANGE_PASSWORD_TITLE = 'Change password';
export const CURRENT_PASSWORD_LABEL = 'CURRENT PASSWORD';
export const NEW_PASSWORD_LABEL = 'NEW PASSWORD';
export const CONFIRM_PASSWORD_LABEL = 'CONFIRM NEW PASSWORD';
export const PASSWORDS_DONT_MATCH = "Passwords don't match.";
export const CANCEL = 'Cancel';
export const SAVE_PASSWORD = 'Save password';
export const SAVING_PASSWORD = 'Saving…';
/**
 * R-72(5) — the §8.4 "success feedback for password change" TBD.
 *
 * FR-AUT-09 says "Success closes the modal", and the modal closing *is* the feedback for a
 * sighted user. It is not feedback for anyone else: the panel simply disappears and focus
 * returns, with nothing announced. So the closing stands as specified and this line is added
 * to a live region (NFR-A11Y-05) — no new visual element, no toast the design has no pattern
 * for, and nothing that could disagree with the modal about whether the change succeeded.
 */
export const PASSWORD_CHANGED = 'Password changed.';

/**
 * The FR-AUT-04 mapping, shared by the login screen and the change-password modal.
 *
 * **401 and 429 are pinned client-side rather than read from the response**, and only on the
 * login path. FR-AUT-04 requires that the copy never disclose which field failed or whether
 * the account exists, and a rule that renders whatever the server sent cannot make that
 * promise — it would hold only for as long as nobody edits a `detail` string. Everything else
 * renders the server's own `detail`, which is FR-ERR-04's "per-failure-class descriptive
 * message" (`503` is a genuinely more useful sentence than the generic fallback), falling back
 * to FR-ERR-04's last resort when there is none.
 */
export function loginErrorCopy(status: number, body: unknown): string {
  if (status === 401) return INVALID_CREDENTIALS;
  if (status === 429) return TOO_MANY_ATTEMPTS;
  return detailOf(body) ?? SYSTEM_FAILURE;
}

/**
 * FR-AUT-09's errors. `401` here is **not** a session event and not a login failure: it is
 * "the current password you typed is wrong", which is the one inline error the requirement
 * names besides the mismatch.
 */
export function changePasswordErrorCopy(status: number, body: unknown): string {
  if (status === 429) return TOO_MANY_ATTEMPTS;
  return detailOf(body) ?? SYSTEM_FAILURE;
}

/** The ordinary `{detail: string}` envelope. Anything else (a validation error's array form,
 *  an empty body, a proxy's HTML) yields `null` and the caller falls back. */
function detailOf(body: unknown): string | null {
  if (typeof body !== 'object' || body === null) return null;
  const detail = (body as Partial<ApiError>).detail;
  return typeof detail === 'string' && detail.length > 0 ? detail : null;
}
