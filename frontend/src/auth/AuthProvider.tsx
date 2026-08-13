/**
 * The session state machine — FR-AUT-06 (session behaviour) and FR-AUT-07 (guarding).
 *
 * It owns three things and nothing else: which of the three {@link AuthPhase}s the app is in,
 * who is signed in, and whether the last transition to `anonymous` was an *expiry*. Rendering
 * is `App`'s (it chooses the login screen or the shell) and copy is `copy.ts`'s.
 *
 * **Guarding is structural, not a route check.** FR-AUT-07 says "every GUI route except login
 * requires authentication"; the product has no router (R-12 removed the only multi-route
 * surface), so the enforceable form is that `App` renders the shell *only* in the
 * `authenticated` phase. A guard that hides views while leaving them mounted would keep their
 * effects — including T-508's document stream — running against a dead session.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { registerTokenRefresher, registerUnauthorizedHandler, setAccessToken } from '../api/auth';
import type { Me } from '../api';
import { AuthContext } from './AuthContext';
import type { AuthPhase, AuthResult } from './AuthContext';
import { SYSTEM_FAILURE } from './copy';
import {
  changePasswordRequest,
  fetchMe,
  refreshSession,
  signInRequest,
  signOutRequest,
} from './session';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<AuthPhase>('starting');
  const [user, setUser] = useState<Me | null>(null);
  const [expired, setExpired] = useState(false);

  /**
   * Whether a session has ever been established in this page's lifetime.
   *
   * A ref, not state: it is read inside callbacks registered once with the API layer, and a
   * state value captured there would be the value at registration time forever. It is what
   * separates FR-AUT-06's "Session expired" from an ordinary first visit — the server answers
   * the same 401 for both, on purpose.
   */
  const established = useRef(false);

  const clearSession = useCallback((didExpire: boolean) => {
    setAccessToken(null);
    established.current = false;
    setUser(null);
    setExpired(didExpire);
    setPhase('anonymous');
  }, []);

  const adopt = useCallback(async (accessToken: string, expiresIn: number): Promise<boolean> => {
    setAccessToken(accessToken, expiresIn);
    const me = await fetchMe();
    if (me === null) {
      // A token that cannot read its own profile is not a usable session. Treated as a failed
      // sign-in rather than as a signed-in user with no identity, which would render a sidebar
      // row with no name and a user menu with no email.
      setAccessToken(null);
      return false;
    }
    established.current = true;
    setUser(me);
    setExpired(false);
    setPhase('authenticated');
    return true;
  }, []);

  useEffect(() => {
    let cancelled = false;

    /**
     * Renew silently (FR-AUT-06). Registered with the API layer, which calls it *before* a
     * request whose token is about to expire — never on a timer. See `ensureFreshToken`: a
     * timer would keep the Keycloak session alive through any amount of idleness and delete
     * the inactivity timeout OI-09 asks for (R-72(2)).
     */
    registerTokenRefresher(async () => {
      const grant = await refreshSession();
      if (cancelled) return;
      if (grant === null) {
        clearSession(established.current);
        return;
      }
      setAccessToken(grant.accessToken, grant.expiresIn);
    });

    // FR-AUT-07's "any API 401 triggers the FR-AUT-06 return-to-login flow" — minus the two
    // 401s that are not session events at all, which `client.ts` filters before we see them.
    registerUnauthorizedHandler(() => {
      if (!cancelled) clearSession(established.current);
    });

    return () => {
      cancelled = true;
      registerTokenRefresher(null);
      registerUnauthorizedHandler(null);
    };
  }, [clearSession]);

  /**
   * Is this provider still mounted? Its own effect, so that **StrictMode's throwaway pass does
   * not leave it false** — the sequence is mount, cleanup, mount, and the last write wins.
   *
   * The bootstrap below cannot use a `cancelled` flag closed over by its own effect, and that
   * is not a style preference: it is a defect that blanks the entire application. See below.
   */
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  /**
   * Resume an existing session, or fall to the login screen.
   *
   * The refresh cookie is httpOnly, so "am I signed in?" is not a question this code can answer
   * locally — it has to ask. A `401` here is the no-cookie case and must *not* raise the
   * FR-AUT-06 banner, which is why `established` is still false at this point.
   *
   * **`bootstrapped` and `mounted` are two different questions, and conflating them is fatal.**
   * The probe must fire once per provider (not once per StrictMode pass), so the ref that
   * guards *starting* it must survive the throwaway pass — but then the flag that guards
   * *applying the result* must NOT be the one that pass cancels. Written the obvious way, run 1
   * starts the probe, StrictMode's cleanup cancels it, and run 2 returns early on the ref: the
   * result is discarded, `phase` stays `starting` for ever, and the app renders **nothing at
   * all**. It is invisible to the suite because Testing Library's `render` does not wrap in
   * StrictMode — found in a browser, and pinned by the StrictMode test in this folder.
   */
  const bootstrapped = useRef(false);
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void (async () => {
      const grant = await refreshSession();
      if (!mounted.current) return;
      if (grant === null || !(await adopt(grant.accessToken, grant.expiresIn))) {
        if (mounted.current) setPhase('anonymous');
      }
    })();
  }, [adopt]);

  const signIn = useCallback(
    async (email: string, password: string): Promise<AuthResult> => {
      const result = await signInRequest(email, password);
      if (!result.ok) return result;
      if (!(await adopt(result.grant.accessToken, result.grant.expiresIn))) {
        // Credentials were accepted but `/auth/me` was not readable — an authenticated call
        // failing straight after a successful login is a server-side problem, so it takes
        // FR-ERR-04's last-resort copy rather than FR-AUT-04's credential wording.
        return { ok: false, message: SYSTEM_FAILURE };
      }
      return { ok: true };
    },
    [adopt],
  );

  const signOut = useCallback(async () => {
    await signOutRequest();
    // Not an expiry: the user asked for this, and the FR-AUT-06 banner would be telling them
    // something went wrong when nothing did.
    clearSession(false);
  }, [clearSession]);

  const changePassword = useCallback(
    (currentPassword: string, newPassword: string) =>
      changePasswordRequest(currentPassword, newPassword),
    [],
  );

  const value = useMemo(
    () => ({ phase, user, expired, signIn, signOut, changePassword }),
    [phase, user, expired, signIn, signOut, changePassword],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}
