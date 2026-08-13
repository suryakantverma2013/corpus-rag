/**
 * The four `/auth` calls, classified — the transport half of §4.17.
 *
 * Split from `AuthProvider` so the classification is testable without React and without a
 * DOM, on T-508's `kb/mutations.ts` precedent. The rules are the same two:
 *
 * - **Branch on `response.status`, never on `data`.**
 * - **Copy is derived in one place** (`copy.ts`), so the login screen and the change-password
 *   modal cannot drift apart on what a `429` says.
 *
 * Nothing here touches the refresh token, because nothing *can*: it is an httpOnly cookie the
 * browser attaches to these three same-origin requests by itself (R-72(1)). That is the whole
 * reason `refreshSession` and `signOutRequest` take no arguments.
 */
import { api } from '../api';
import type { Me } from '../api';
import { changePasswordErrorCopy, loginErrorCopy } from './copy';
import type { AuthResult } from './AuthContext';

/** What a successful login or refresh yields: the access token and how long it lasts. */
export interface AccessGrant {
  accessToken: string;
  /** Seconds — the response's own `expires_in` (300 on the shipped realm). */
  expiresIn: number;
}

export type GrantResult = { ok: true; grant: AccessGrant } | { ok: false; message: string };

export async function signInRequest(email: string, password: string): Promise<GrantResult> {
  const { data, error, response } = await api.POST('/api/v1/auth/login', {
    body: { email, password },
  });
  if (response.status === 200 && data !== undefined) {
    return { ok: true, grant: { accessToken: data.access_token, expiresIn: data.expires_in } };
  }
  return { ok: false, message: loginErrorCopy(response.status, error) };
}

/**
 * Exchange the refresh cookie for a new access token.
 *
 * A `401` here is the ordinary, expected answer in two quite different situations — a first
 * visit with no cookie, and a session that has aged past the realm's `ssoSessionIdleTimeout` —
 * and the server deliberately does not distinguish them (NFR-SEC-02). The *caller* knows which
 * it is, because only it knows whether a session was ever established, which is why this
 * returns a bare boolean rather than an opinion about what to render.
 */
export async function refreshSession(): Promise<AccessGrant | null> {
  const { data, response } = await api.POST('/api/v1/auth/refresh', {});
  if (response.status !== 200 || data === undefined) return null;
  return { accessToken: data.access_token, expiresIn: data.expires_in };
}

/**
 * FR-AUT-08's sign out. Never throws, and deliberately ignores the status.
 *
 * The server clears the cookie on every path including its own failures, and the client clears
 * its access token unconditionally — so the user ends up signed out whatever happened upstream.
 * Surfacing a 503 here would offer them a choice they have no way to act on, in front of a
 * session that is already gone.
 */
export async function signOutRequest(): Promise<void> {
  try {
    await api.POST('/api/v1/auth/logout', {});
  } catch {
    // A network failure has the same outcome as a successful revoke, from here.
  }
}

/** `GET /auth/me` — FR-SBR-06's identity and FR-AUT-08's email. `null` on any failure, which
 *  the caller treats as "not signed in" rather than as a signed-in user with no name. */
export async function fetchMe(): Promise<Me | null> {
  const { data, response } = await api.GET('/api/v1/auth/me');
  return response.status === 200 && data !== undefined ? data : null;
}

export async function changePasswordRequest(
  currentPassword: string,
  newPassword: string,
): Promise<AuthResult> {
  const { error, response } = await api.POST('/api/v1/auth/change-password', {
    body: { current_password: currentPassword, new_password: newPassword },
  });
  if (response.status === 204) return { ok: true };
  return { ok: false, message: changePasswordErrorCopy(response.status, error) };
}
