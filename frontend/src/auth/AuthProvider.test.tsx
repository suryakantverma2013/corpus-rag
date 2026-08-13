/**
 * The session state machine (FR-AUT-06/07).
 *
 * The transport is mocked at `./session` — `session.ts` is where a status becomes a meaning,
 * and this file is about what the *phases* do with it. The one thing under test that no other
 * file can see is the difference between "no session" and "your session ended": the server
 * answers the identical 401 for both, deliberately (NFR-SEC-02), so only the client knows.
 */
import { StrictMode } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Me } from '../api';
import type { AccessGrant, GrantResult } from './session';
import type { AuthResult } from './AuthContext';

const refreshSession = vi.fn<() => Promise<AccessGrant | null>>();
const fetchMe = vi.fn<() => Promise<Me | null>>();
const signInRequest = vi.fn<() => Promise<GrantResult>>();
const signOutRequest = vi.fn<() => Promise<void>>();
const changePasswordRequest = vi.fn<() => Promise<AuthResult>>();

vi.mock('./session', () => ({
  refreshSession: () => refreshSession(),
  fetchMe: () => fetchMe(),
  signInRequest: () => signInRequest(),
  signOutRequest: () => signOutRequest(),
  changePasswordRequest: () => changePasswordRequest(),
}));

const setAccessToken = vi.fn();
const registerTokenRefresher = vi.fn<(fn: (() => Promise<void>) | null) => void>();
const registerUnauthorizedHandler = vi.fn<(fn: (() => void) | null) => void>();

vi.mock('../api/auth', () => ({
  setAccessToken: (...a: unknown[]) => setAccessToken(...a),
  registerTokenRefresher: (fn: (() => Promise<void>) | null) => registerTokenRefresher(fn),
  registerUnauthorizedHandler: (fn: (() => void) | null) => registerUnauthorizedHandler(fn),
}));

const { AuthProvider } = await import('./AuthProvider');
const { useAuth } = await import('./useAuth');

const ME: Me = {
  id: 'user-1',
  email: 'maya.jensen@example.com',
  display_name: 'Maya Jensen',
  roles: ['user'],
  is_active: true,
};

/** Renders the context as text, so every assertion reads the value the app would. */
function Probe() {
  const { phase, user, expired } = useAuth();
  return (
    <div>
      <span data-testid="phase">{phase}</span>
      <span data-testid="user">{user?.email ?? '-'}</span>
      <span data-testid="expired">{String(expired)}</span>
    </div>
  );
}

let latest: ReturnType<typeof useAuth> | null = null;
function Capture() {
  latest = useAuth();
  return null;
}

function renderProvider() {
  return render(
    <AuthProvider>
      <Probe />
      <Capture />
    </AuthProvider>,
  );
}

const phase = () => screen.getByTestId('phase').textContent;
const expired = () => screen.getByTestId('expired').textContent;

beforeEach(() => {
  vi.clearAllMocks();
  latest = null;
  refreshSession.mockResolvedValue(null);
  fetchMe.mockResolvedValue(ME);
  signOutRequest.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.resetAllMocks();
});

describe('boot', () => {
  it('resumes a session when the refresh cookie is still good', async () => {
    // The cookie is httpOnly, so "am I signed in?" is not answerable locally — the boot probe
    // is the only way to find out (R-72(1)).
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));
    expect(screen.getByTestId('user').textContent).toBe('maya.jensen@example.com');
    expect(setAccessToken).toHaveBeenCalledWith('a', 300);
  });

  it('falls to the login screen with NO expiry banner on a first visit', async () => {
    // The whole reason `established` exists. A 401 here means "no cookie", and telling a
    // first-time visitor their session expired is a small lie with a confusing implication.
    refreshSession.mockResolvedValue(null);
    renderProvider();
    await waitFor(() => expect(phase()).toBe('anonymous'));
    expect(expired()).toBe('false');
  });

  it('starts in `starting`, so the login card never flashes at a signed-in user', () => {
    let resolve!: (value: AccessGrant | null) => void;
    refreshSession.mockReturnValue(new Promise((r) => (resolve = r)));
    renderProvider();
    expect(phase()).toBe('starting');
    act(() => resolve(null));
  });

  it('does not sign in a token that cannot read its own profile', async () => {
    // A session with no identity would render a sidebar row with no name and a user menu with
    // no email — worse than the login screen, and harder to get out of.
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    fetchMe.mockResolvedValue(null);
    renderProvider();
    await waitFor(() => expect(phase()).toBe('anonymous'));
    expect(setAccessToken).toHaveBeenLastCalledWith(null);
  });

  it('probes once, not once per render', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));
    expect(refreshSession).toHaveBeenCalledTimes(1);
  });

  describe('under StrictMode — which is how the app actually runs', () => {
    /**
     * THE DEFECT THIS PINS blanked the entire application, and every test above passed while it
     * did: Testing Library's `render` does not wrap in StrictMode, but `main.tsx` does.
     *
     * StrictMode runs each effect mount → cleanup → mount. With the probe guarded by a
     * `bootstrapped` ref (so it fires once) *and* its result guarded by a `cancelled` flag
     * closed over by the same effect, the throwaway cleanup cancels the only probe there will
     * ever be — `phase` stays `starting`, and `Corpus` renders `null` for ever. Found in a
     * browser, against a backend that was answering perfectly.
     */
    function renderStrict() {
      return render(
        <StrictMode>
          <AuthProvider>
            <Probe />
          </AuthProvider>
        </StrictMode>,
      );
    }

    it('still resolves to a phase when a session exists', async () => {
      refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
      renderStrict();
      await waitFor(() => expect(phase()).toBe('authenticated'));
      expect(refreshSession).toHaveBeenCalledTimes(1);
    });

    it('still resolves to a phase when there is none — the blank-app case', async () => {
      refreshSession.mockResolvedValue(null);
      renderStrict();
      await waitFor(() => expect(phase()).toBe('anonymous'));
      expect(refreshSession).toHaveBeenCalledTimes(1);
    });
  });
});

describe('signIn', () => {
  it('adopts the grant and reads the profile', async () => {
    renderProvider();
    await waitFor(() => expect(phase()).toBe('anonymous'));
    signInRequest.mockResolvedValue({ ok: true, grant: { accessToken: 'a', expiresIn: 300 } });

    await act(async () => {
      await latest!.signIn('maya@example.com', 'pw');
    });
    expect(phase()).toBe('authenticated');
    expect(setAccessToken).toHaveBeenCalledWith('a', 300);
  });

  it('returns the failure copy and stays anonymous', async () => {
    renderProvider();
    await waitFor(() => expect(phase()).toBe('anonymous'));
    signInRequest.mockResolvedValue({ ok: false, message: 'Invalid email or password.' });

    let result: AuthResult | null = null;
    await act(async () => {
      result = await latest!.signIn('maya@example.com', 'bad');
    });
    expect(result).toEqual({ ok: false, message: 'Invalid email or password.' });
    expect(phase()).toBe('anonymous');
  });

  it('clears the expiry banner once the user is back in', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));

    // Drop the session, then sign back in.
    const onUnauthorized = registerUnauthorizedHandler.mock.calls.at(-1)?.[0];
    act(() => onUnauthorized?.());
    expect(expired()).toBe('true');

    signInRequest.mockResolvedValue({ ok: true, grant: { accessToken: 'b', expiresIn: 300 } });
    await act(async () => {
      await latest!.signIn('maya@example.com', 'pw');
    });
    expect(expired()).toBe('false');
  });
});

describe('the 401 handler (FR-AUT-07)', () => {
  it('raises the expiry banner when an established session drops', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));

    const onUnauthorized = registerUnauthorizedHandler.mock.calls.at(-1)?.[0];
    act(() => onUnauthorized?.());

    expect(phase()).toBe('anonymous');
    expect(expired()).toBe('true');
    expect(setAccessToken).toHaveBeenLastCalledWith(null);
  });

  it('does not raise it for a session that never existed', async () => {
    refreshSession.mockResolvedValue(null);
    renderProvider();
    await waitFor(() => expect(phase()).toBe('anonymous'));

    const onUnauthorized = registerUnauthorizedHandler.mock.calls.at(-1)?.[0];
    act(() => onUnauthorized?.());
    expect(expired()).toBe('false');
  });

  it('unregisters both hooks on unmount', async () => {
    // Otherwise a torn-down provider keeps answering 401s and calling `setState` on an
    // unmounted tree — and in a test file, keeps answering for the *next* test.
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    const { unmount } = renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));
    unmount();
    expect(registerTokenRefresher).toHaveBeenLastCalledWith(null);
    expect(registerUnauthorizedHandler).toHaveBeenLastCalledWith(null);
  });
});

describe('the registered refresher', () => {
  const refresher = () => registerTokenRefresher.mock.calls.at(-1)?.[0] ?? null;

  it('installs the new token without disturbing the phase', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));

    refreshSession.mockResolvedValue({ accessToken: 'b', expiresIn: 300 });
    await act(async () => {
      await refresher()?.();
    });
    expect(setAccessToken).toHaveBeenLastCalledWith('b', 300);
    expect(phase()).toBe('authenticated');
  });

  it('ends the session when renewal is refused — the inactivity timeout arriving', async () => {
    // This is what OI-09's timeout looks like from the browser: the user was idle past the
    // realm's `ssoSessionIdleTimeout`, so the cookie no longer buys a token (R-72(2)).
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));

    refreshSession.mockResolvedValue(null);
    await act(async () => {
      await refresher()?.();
    });
    expect(phase()).toBe('anonymous');
    expect(expired()).toBe('true');
  });
});

describe('signOut', () => {
  it('revokes, clears, and does NOT claim the session expired', async () => {
    refreshSession.mockResolvedValue({ accessToken: 'a', expiresIn: 300 });
    renderProvider();
    await waitFor(() => expect(phase()).toBe('authenticated'));

    await act(async () => {
      await latest!.signOut();
    });
    expect(signOutRequest).toHaveBeenCalledTimes(1);
    expect(phase()).toBe('anonymous');
    // The user asked for this. FR-AUT-06's banner would be reporting a fault where none exists.
    expect(expired()).toBe('false');
    expect(setAccessToken).toHaveBeenLastCalledWith(null);
  });
});
