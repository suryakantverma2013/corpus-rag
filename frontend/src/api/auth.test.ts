/**
 * The token slot and its renewal policy (R-72(2)).
 *
 * Module-scope state, so every test resets it explicitly — a leaked token from one case makes
 * the next one pass for the wrong reason.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  authHeaders,
  currentAccessToken,
  ensureFreshToken,
  registerTokenRefresher,
  registerUnauthorizedHandler,
  reportUnauthorized,
  setAccessToken,
} from './auth';

afterEach(() => {
  setAccessToken(null);
  registerTokenRefresher(null);
  registerUnauthorizedHandler(null);
  vi.useRealTimers();
});

describe('the token slot', () => {
  it('is read per call, never captured', () => {
    setAccessToken('a', 300);
    expect(authHeaders()).toEqual({ Authorization: 'Bearer a' });
    setAccessToken('b', 300);
    expect(authHeaders()).toEqual({ Authorization: 'Bearer b' });
    expect(currentAccessToken()).toBe('b');
  });

  it('returns an empty object when signed out, never undefined', () => {
    // Every call site spreads this unconditionally; `undefined` would make each one grow a
    // conditional, and one of them would eventually forget.
    setAccessToken(null);
    expect(authHeaders()).toEqual({});
  });
});

describe('ensureFreshToken', () => {
  it('does nothing while the token has time left', async () => {
    const refresh = vi.fn(async () => undefined);
    registerTokenRefresher(refresh);
    setAccessToken('a', 300);
    await ensureFreshToken();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('renews once the token is inside the skew window', async () => {
    const refresh = vi.fn(async () => setAccessToken('fresh', 300));
    registerTokenRefresher(refresh);
    // 20s left, against a 30s skew: renew *before* the request rather than after its 401.
    setAccessToken('stale', 20);
    await ensureFreshToken();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(currentAccessToken()).toBe('fresh');
  });

  it('renews once for concurrent callers', async () => {
    // Five parallel requests must produce one refresh, not five — and five would also mean
    // four of them racing to rotate a single-use cookie.
    let release: (() => void) | null = null;
    const refresh = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = () => {
            setAccessToken('fresh', 300);
            resolve();
          };
        }),
    );
    registerTokenRefresher(refresh);
    setAccessToken('stale', 0);

    const waiting = [ensureFreshToken(), ensureFreshToken(), ensureFreshToken()];
    await vi.waitFor(() => expect(release).not.toBeNull());
    release!();
    await Promise.all(waiting);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it('renews again after the previous renewal settled', async () => {
    // The in-flight guard must clear, or the second expiry of the session is never handled —
    // the same "a flag that only ever latches" shape as R-71(1)'s deadlock.
    const refresh = vi.fn(async () => setAccessToken('fresh', 0));
    registerTokenRefresher(refresh);
    setAccessToken('stale', 0);
    await ensureFreshToken();
    await ensureFreshToken();
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it('never rejects when the refresher throws', async () => {
    // The request proceeds on the stale token and takes the server's 401, which is the ONE
    // return-to-login path. Rejecting here would give every call site a second one.
    registerTokenRefresher(async () => {
      throw new Error('network down');
    });
    setAccessToken('stale', 0);
    await expect(ensureFreshToken()).resolves.toBeUndefined();
  });

  it('does nothing when signed out, or when no refresher is registered', async () => {
    const refresh = vi.fn(async () => undefined);
    registerTokenRefresher(refresh);
    setAccessToken(null);
    await ensureFreshToken();

    registerTokenRefresher(null);
    setAccessToken('a', 0);
    await ensureFreshToken();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('does nothing for a token whose expiry is unknown', async () => {
    // `setAccessToken(token)` with no `expires_in` is a caller that genuinely has no expiry
    // information. Renewing on every request would be the alternative, and it would hammer
    // /auth/refresh rather than fail visibly.
    const refresh = vi.fn(async () => undefined);
    registerTokenRefresher(refresh);
    setAccessToken('a');
    await ensureFreshToken();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('is driven by requests and NOT by a timer (R-72(2))', async () => {
    // THE POINT: a periodic refresh would keep the Keycloak SSO session alive through any
    // amount of idleness, and OI-09's inactivity timeout — which is `ssoSessionIdleTimeout`,
    // enforced by the realm — would quietly stop existing. Nothing here may schedule.
    vi.useFakeTimers();
    const refresh = vi.fn(async () => undefined);
    registerTokenRefresher(refresh);
    setAccessToken('a', 300);
    await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe('reportUnauthorized', () => {
  it('calls the registered handler, and is inert without one', () => {
    const handler = vi.fn();
    registerUnauthorizedHandler(handler);
    reportUnauthorized();
    expect(handler).toHaveBeenCalledTimes(1);

    registerUnauthorizedHandler(null);
    expect(() => reportUnauthorized()).not.toThrow();
  });
});
