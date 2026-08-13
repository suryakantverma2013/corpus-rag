/**
 * The session context object and its value shape (§4.17, FR-AUT-06/07).
 *
 * Its own module holding no component, for the same reason `ThemeContext` is: no module may
 * export both a component and a non-component value (`react/only-export-components`).
 */
import { createContext } from 'react';
import type { Me } from '../api';

/**
 * `starting` exists so the app never flashes the login screen at a signed-in user.
 *
 * On load the session is genuinely unknown — the refresh cookie is httpOnly, so the only way
 * to find out whether one exists is to ask the server (R-72(1)). Rendering the login card
 * while that request is in flight would show it for a moment to every returning user on every
 * reload, which reads as "signed out" and is the single most visible thing this phase prevents.
 */
export type AuthPhase = 'starting' | 'anonymous' | 'authenticated';

/** What a sign-in or password change reports back to its form. */
export type AuthResult = { ok: true } | { ok: false; message: string };

export interface AuthContextValue {
  phase: AuthPhase;
  /** `GET /auth/me`. Non-null exactly when `phase === 'authenticated'`. */
  user: Me | null;
  /**
   * FR-AUT-06's banner. True only when a session that *existed* went away — never on a first
   * visit, where the same 401 means "no session", not "your session ended". Showing "Session
   * expired" to someone who has never signed in is a small lie with a confusing implication.
   */
  expired: boolean;
  signIn: (email: string, password: string) => Promise<AuthResult>;
  signOut: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<AuthResult>;
}

/** `null` rather than a plausible default: a component rendered outside the provider is a
 *  wiring bug, and `useAuth` turns it into a named error rather than a silent signed-out UI. */
export const AuthContext = createContext<AuthContextValue | null>(null);
