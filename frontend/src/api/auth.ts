/**
 * Where the bearer token is kept, when it is renewed, and who is told when it stops working.
 *
 * T-508 created this module holding the token alone, and left the rest to T-509 — which is
 * what let the knowledge-base modal ship correctly authenticated without pre-empting the auth
 * task. T-509 adds the *policy* here and the *surfaces* in `src/auth/`. The split is the same
 * one as before: nothing in this file knows what a login screen is, and nothing in `src/auth/`
 * is imported by `client.ts`.
 *
 * **Module scope, not React context**, because both consumers are outside React:
 * `openapi-fetch` middleware is registered once on the `api` singleton (see `client.ts`), and
 * `streamFrames` is called from an effect with an explicit `init` that middleware never sees.
 *
 * **An accessor, never an exported value.** A token read once and captured in a closure goes
 * stale the moment it is refreshed — and the document stream's closure is the longest-lived
 * one in the app. Every consumer reads it per request, or per connect attempt.
 *
 * **The refresh token is not here, and cannot be.** R-72(1)/FR-AUT-07 put it in an httpOnly
 * cookie, so no JavaScript in this application can read it; `POST /auth/refresh` sends it
 * automatically because it is same-origin, and the server rotates it in the response.
 */

let accessToken: string | null = null;
/** Epoch ms at which `accessToken` stops being accepted. `0` when there is no token. */
let expiresAt = 0;

/**
 * Renew slightly early, so a request never races its own token's expiry.
 *
 * 30s covers request latency plus the clock skew between this browser and Keycloak — the
 * access token lives 300s on the shipped realm, so the cost is one extra refresh per ten
 * minutes of continuous use and the benefit is that a 401 means something.
 */
const SKEW_MS = 30_000;

type Refresher = () => Promise<void>;

let refresher: Refresher | null = null;
let onUnauthorized: (() => void) | null = null;
/** De-duplicates concurrent renewals: five parallel requests must produce one refresh. */
let inFlight: Promise<void> | null = null;

/**
 * T-509's entry point. `null` signs out.
 *
 * `expiresInSeconds` is the login/refresh response's own `expires_in`. Omitting it leaves the
 * token with no known expiry, which makes {@link ensureFreshToken} a no-op for it — correct
 * for a caller that genuinely has no expiry information, and never a silent half-state.
 */
export function setAccessToken(token: string | null, expiresInSeconds?: number): void {
  accessToken = token;
  expiresAt =
    token === null || expiresInSeconds === undefined ? 0 : Date.now() + expiresInSeconds * 1000;
}

export function currentAccessToken(): string | null {
  return accessToken;
}

/** Registered once by `AuthProvider`. Passing `null` unregisters (used on unmount). */
export function registerTokenRefresher(fn: Refresher | null): void {
  refresher = fn;
}

/** Registered once by `AuthProvider` — FR-AUT-07's "any API 401 returns to the login screen". */
export function registerUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

/**
 * Renew the access token if it is about to expire. Never rejects.
 *
 * **This is why the session has an inactivity timeout at all (R-72(2)).** Renewal is driven by
 * requests, not by a timer: a `setInterval` refreshing every few minutes would keep the
 * Keycloak SSO session alive for its full 10h `ssoSessionMaxLifespan` no matter how long the
 * user had been away, and OI-09's inactivity timeout would quietly stop existing. Refreshing
 * on demand means an idle user's refresh cookie ages out on the realm's own
 * `ssoSessionIdleTimeout`, and the next thing they click returns them to the login screen —
 * which is exactly what FR-AUT-06 describes.
 *
 * It also covers the case a timer could not: a backgrounded tab has its timers throttled, so a
 * scheduled refresh may simply not run. Checking at the point of use cannot be throttled out.
 *
 * A failed renewal is deliberately swallowed rather than propagated to the caller: the request
 * proceeds with the stale token, the server answers 401, and {@link reportUnauthorized} runs
 * the one return-to-login path. Rejecting here would give every call site a second one.
 */
export async function ensureFreshToken(): Promise<void> {
  if (refresher === null || accessToken === null || expiresAt === 0) return;
  if (Date.now() < expiresAt - SKEW_MS) return;
  if (inFlight === null) {
    inFlight = refresher().finally(() => {
      inFlight = null;
    });
  }
  await inFlight.catch(() => undefined);
}

/**
 * A request came back 401 from a route that had no business answering one.
 *
 * The *caller* decides whether a given 401 is a session event, because two of them are not:
 * `POST /auth/login` answers 401 for wrong credentials, and `POST /auth/change-password`
 * answers 401 for a wrong **current password** (FR-AUT-09's own inline error). Signing the
 * user out because they mistyped the password they are in the middle of changing is the
 * defect the literal reading of FR-AUT-07 produces — see `SESSION_401_EXEMPT` in `client.ts`.
 */
export function reportUnauthorized(): void {
  onUnauthorized?.();
}

/**
 * The one `Authorization` header, or nothing at all.
 *
 * Returning `{}` rather than `undefined` when signed out is deliberate: every call site spreads
 * this unconditionally (`{ ...authHeaders() }`), so no caller grows a conditional and no caller
 * can forget one. An unauthenticated request then fails as an ordinary `401`, which is the
 * behaviour the handler above attaches to.
 */
export function authHeaders(): Readonly<Record<string, string>> {
  return accessToken === null ? {} : { Authorization: `Bearer ${accessToken}` };
}
