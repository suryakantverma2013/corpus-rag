/**
 * The four `/auth` calls, and what each status means.
 *
 * `api` is mocked, so this is the classification and nothing else — the same split
 * `kb/mutations.test.ts` uses. The rule under test throughout: branch on `response.status`,
 * never on `data`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { INVALID_CREDENTIALS, TOO_MANY_ATTEMPTS } from './copy';

const GET = vi.fn();
const POST = vi.fn();

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  api: { GET: (...a: unknown[]) => GET(...a), POST: (...a: unknown[]) => POST(...a) },
}));

const { changePasswordRequest, fetchMe, refreshSession, signInRequest, signOutRequest } =
  await import('./session');

/** openapi-fetch's shape: `data` on success, `error` on failure, always a `response`. */
function answer(status: number, body: unknown = undefined) {
  const response = { status } as Response;
  return status >= 200 && status < 300
    ? { data: body, error: undefined, response }
    : { data: undefined, error: body, response };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('signInRequest', () => {
  it('returns the grant on 200', async () => {
    POST.mockResolvedValue(
      answer(200, { access_token: 'a', token_type: 'bearer', expires_in: 300 }),
    );
    await expect(signInRequest('maya@example.com', 'pw')).resolves.toEqual({
      ok: true,
      grant: { accessToken: 'a', expiresIn: 300 },
    });
    expect(POST).toHaveBeenCalledWith('/api/v1/auth/login', {
      body: { email: 'maya@example.com', password: 'pw' },
    });
  });

  it('never asks for a refresh token, because it cannot be given one', async () => {
    // R-72(1): the refresh token is an httpOnly cookie and `TokenResponse` has no such field.
    // If the body regrew one, this is what would notice the client had started reading it.
    POST.mockResolvedValue(
      answer(200, { access_token: 'a', token_type: 'bearer', expires_in: 300 }),
    );
    const result = await signInRequest('maya@example.com', 'pw');
    expect(JSON.stringify(result)).not.toContain('refresh');
  });

  it('maps the two credential failures to FR-AUT-04 copy', async () => {
    POST.mockResolvedValue(answer(401, { detail: 'anything at all' }));
    await expect(signInRequest('a@b.c', 'x')).resolves.toEqual({
      ok: false,
      message: INVALID_CREDENTIALS,
    });

    POST.mockResolvedValue(answer(429, { detail: 'anything at all' }));
    await expect(signInRequest('a@b.c', 'x')).resolves.toEqual({
      ok: false,
      message: TOO_MANY_ATTEMPTS,
    });
  });

  it('renders the server’s copy for an outage', async () => {
    POST.mockResolvedValue(answer(503, { detail: 'Authentication service unavailable.' }));
    await expect(signInRequest('a@b.c', 'x')).resolves.toEqual({
      ok: false,
      message: 'Authentication service unavailable.',
    });
  });

  it('treats a 200 with no readable body as a failure, not as a session', async () => {
    POST.mockResolvedValue({ data: undefined, error: undefined, response: { status: 200 } });
    await expect(signInRequest('a@b.c', 'x')).resolves.toMatchObject({ ok: false });
  });
});

describe('refreshSession', () => {
  it('takes no arguments and sends no body — the cookie is the credential', async () => {
    POST.mockResolvedValue(
      answer(200, { access_token: 'a', token_type: 'bearer', expires_in: 300 }),
    );
    await expect(refreshSession()).resolves.toEqual({ accessToken: 'a', expiresIn: 300 });
    expect(POST).toHaveBeenCalledWith('/api/v1/auth/refresh', {});
  });

  it('returns null on 401, with no opinion about what that means', async () => {
    // The same 401 covers "first visit, no cookie" and "idle past ssoSessionIdleTimeout"; the
    // server will not distinguish them (NFR-SEC-02) and only the caller knows which it is.
    POST.mockResolvedValue(answer(401, { detail: 'Invalid or expired session.' }));
    await expect(refreshSession()).resolves.toBeNull();
  });

  it('returns null when the auth service is down', async () => {
    POST.mockResolvedValue(answer(503, { detail: 'Authentication service unavailable.' }));
    await expect(refreshSession()).resolves.toBeNull();
  });
});

describe('signOutRequest', () => {
  it('posts with no body', async () => {
    POST.mockResolvedValue(answer(204));
    await signOutRequest();
    expect(POST).toHaveBeenCalledWith('/api/v1/auth/logout', {});
  });

  it('never throws — the outcome is the same either way', async () => {
    // The server clears the cookie on every path including its own failures, and the client
    // clears its token unconditionally. Surfacing a 503 would offer the user a choice they
    // cannot act on, about a session that is already gone.
    POST.mockRejectedValue(new Error('network down'));
    await expect(signOutRequest()).resolves.toBeUndefined();
  });
});

describe('fetchMe', () => {
  it('returns the profile on 200', async () => {
    const me = { id: '1', email: 'a@b.c', display_name: null, roles: [], is_active: true };
    GET.mockResolvedValue(answer(200, me));
    await expect(fetchMe()).resolves.toEqual(me);
  });

  it('returns null on anything else', async () => {
    GET.mockResolvedValue(answer(401, { detail: 'Not authenticated' }));
    await expect(fetchMe()).resolves.toBeNull();
  });
});

describe('changePasswordRequest', () => {
  it('reports 204 as success', async () => {
    POST.mockResolvedValue(answer(204));
    await expect(changePasswordRequest('old', 'new')).resolves.toEqual({ ok: true });
    expect(POST).toHaveBeenCalledWith('/api/v1/auth/change-password', {
      body: { current_password: 'old', new_password: 'new' },
    });
  });

  it('renders the server’s 401 rather than the login screen’s', async () => {
    POST.mockResolvedValue(answer(401, { detail: 'Current password is incorrect.' }));
    await expect(changePasswordRequest('wrong', 'new')).resolves.toEqual({
      ok: false,
      message: 'Current password is incorrect.',
    });
  });

  it('renders the misconfiguration copy on 500 rather than a generic failure', async () => {
    // T-110's under-provisioned-service-account condition reaches this route through the Admin
    // API's reset-password call, and its message tells an operator where to look.
    const detail = 'Password change is not configured correctly on the server.';
    POST.mockResolvedValue(answer(500, { detail }));
    await expect(changePasswordRequest('old', 'new')).resolves.toEqual({
      ok: false,
      message: detail,
    });
  });
});
