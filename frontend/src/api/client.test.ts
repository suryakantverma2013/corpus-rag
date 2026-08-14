/**
 * The two middleware rules on the typed client: what gets a freshened bearer, and which 401s
 * mean "your session is over".
 *
 * The exemption list is the substance. Read literally, FR-AUT-07 ("any API 401 triggers the
 * FR-AUT-06 return-to-login flow") signs the user out when they mistype the password they are
 * in the middle of changing — the change-password route answers 401 for a wrong *current*
 * password, which FR-AUT-09 specifies as an inline error. R-72(6) records the correction; this
 * file is what stops it being re-simplified back.
 *
 * Driven against `sessionMiddleware` directly rather than through `api`: the client is built
 * on `baseUrl: ''`, and `openapi-fetch` hands the raw path to `new Request`, which a browser
 * resolves against `location` and Node's `undici` rejects. The registration itself is guarded
 * by reading the source, at the foot of this file.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  CHAT_REGENERATE_PATH,
  CHAT_SEND_PATH,
  StreamError,
  expandPath,
  sessionMiddleware,
  streamFrames,
} from './client';
import { registerTokenRefresher, registerUnauthorizedHandler, setAccessToken } from './auth';
import { readSource, stripTsComments } from '../test/css-source';

afterEach(() => {
  setAccessToken(null);
  registerTokenRefresher(null);
  registerUnauthorizedHandler(null);
});

const ORIGIN = 'http://localhost';

/** Runs `onRequest` the way openapi-fetch does, and hands back the Request it produced. */
async function send(path: string, init?: RequestInit): Promise<Request> {
  const request = new Request(ORIGIN + path, init);
  // The middleware's other options are unused by this implementation; the cast keeps the test
  // honest about that rather than fabricating a schema-shaped object.
  const result = await sessionMiddleware.onRequest!({ request } as never);
  return (result ?? request) as Request;
}

/** Runs `onResponse` for a given status. */
async function receive(path: string, status: number): Promise<void> {
  await sessionMiddleware.onResponse!({
    request: new Request(ORIGIN + path),
    response: new Response(status === 204 ? null : '{}', { status }),
  } as never);
}

describe('the bearer header', () => {
  it('is attached to an authenticated call', async () => {
    setAccessToken('tok', 300);
    const request = await send('/api/v1/auth/me');
    expect(request.headers.get('authorization')).toBe('Bearer tok');
  });

  it('is added, never rebuilt (§8.58(9))', async () => {
    // `openapi-fetch` calls `fetch(Request)`, so a shim written as `fetch(url, {...init,
    // headers})` REPLACES the Request's headers — stripping a multipart Content-Type and its
    // boundary, which FastAPI answers with a 422 that looks nothing like a header problem.
    setAccessToken('tok', 300);
    const request = await send('/api/v1/documents', {
      method: 'POST',
      headers: { 'X-Probe': 'kept', 'Content-Type': 'multipart/form-data; boundary=abc123' },
    });
    expect(request.headers.get('x-probe')).toBe('kept');
    expect(request.headers.get('content-type')).toContain('boundary=abc123');
    expect(request.headers.get('authorization')).toBe('Bearer tok');
  });

  it('is absent when signed out', async () => {
    setAccessToken(null);
    const request = await send('/api/v1/auth/me');
    expect(request.headers.get('authorization')).toBeNull();
  });
});

describe('freshening', () => {
  it('renews an about-to-expire token before an ordinary call', async () => {
    const refresh = vi.fn(async () => setAccessToken('fresh', 300));
    registerTokenRefresher(refresh);
    setAccessToken('stale', 0);
    const request = await send('/api/v1/conversations');
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(request.headers.get('authorization')).toBe('Bearer fresh');
  });

  it.each(['/api/v1/auth/refresh', '/api/v1/auth/login', '/api/v1/auth/logout'])(
    'does NOT freshen %s',
    async (path) => {
      // For `refresh` this is not an optimisation: it goes through this same client, so
      // freshening it would call the refresher from inside the refresher and await a promise
      // that cannot settle until it returns. The symptom is a hung boot, not an error.
      const refresh = vi.fn(async () => undefined);
      registerTokenRefresher(refresh);
      setAccessToken('stale', 0);
      await send(path, { method: 'POST' });
      expect(refresh).not.toHaveBeenCalled();
    },
  );

  it.each(['/api/v1/auth/me', '/api/v1/auth/change-password'])(
    'DOES freshen %s — an ordinary authenticated call under the same prefix',
    async (path) => {
      const refresh = vi.fn(async () => setAccessToken('fresh', 300));
      registerTokenRefresher(refresh);
      setAccessToken('stale', 0);
      await send(path, { method: 'POST' });
      expect(refresh).toHaveBeenCalledTimes(1);
    },
  );

  it('matches on the path, not on a substring of the URL', async () => {
    // A `startsWith`/`includes` implementation would exempt `/api/v1/auth/login-history` and
    // anything carrying `?next=/api/v1/auth/login`, silently widening the hole.
    const refresh = vi.fn(async () => setAccessToken('fresh', 300));
    registerTokenRefresher(refresh);
    setAccessToken('stale', 0);
    await send('/api/v1/conversations?next=/api/v1/auth/login');
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});

describe('the 401 policy (FR-AUT-07, corrected)', () => {
  it('treats a 401 from an ordinary route as the end of the session', async () => {
    const expired = vi.fn();
    registerUnauthorizedHandler(expired);
    await receive('/api/v1/conversations', 401);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('does NOT sign the user out when change-password rejects the current password', async () => {
    // The defect the literal reading produces: the user opens FR-AUT-09's modal, mistypes
    // their current password, and is thrown back to the login screen instead of being shown
    // the inline error the requirement specifies.
    const expired = vi.fn();
    registerUnauthorizedHandler(expired);
    await receive('/api/v1/auth/change-password', 401);
    expect(expired).not.toHaveBeenCalled();
  });

  it('does NOT sign the user out when login rejects credentials', async () => {
    const expired = vi.fn();
    registerUnauthorizedHandler(expired);
    await receive('/api/v1/auth/login', 401);
    expect(expired).not.toHaveBeenCalled();
  });

  it('does NOT fire on a 401 from /auth/refresh — the refresher owns that outcome', async () => {
    // Otherwise a first visit with no cookie raises FR-AUT-06's "Session expired" banner at
    // someone who has never signed in.
    const expired = vi.fn();
    registerUnauthorizedHandler(expired);
    await receive('/api/v1/auth/refresh', 401);
    expect(expired).not.toHaveBeenCalled();
  });

  it('ignores non-401 failures', async () => {
    const expired = vi.fn();
    registerUnauthorizedHandler(expired);
    await receive('/api/v1/users', 403);
    await receive('/api/v1/documents', 409);
    expect(expired).not.toHaveBeenCalled();
  });
});

describe('expandPath', () => {
  it('fills a template segment', () => {
    expect(expandPath(CHAT_SEND_PATH, { conversation_id: 'abc' })).toBe(
      '/api/v1/conversations/abc/messages',
    );
    expect(expandPath(CHAT_REGENERATE_PATH, { message_id: 'm1' })).toBe(
      '/api/v1/messages/m1/regenerate',
    );
  });

  it('encodes the value so it cannot escape its segment', () => {
    expect(expandPath(CHAT_REGENERATE_PATH, { message_id: '../../documents' })).toBe(
      '/api/v1/messages/..%2F..%2Fdocuments/regenerate',
    );
  });

  it('throws on a missing parameter rather than emitting a literal brace', () => {
    // A `{message_id}` left in the URL reaches the server as a 404 whose cause is invisible.
    expect(() => expandPath(CHAT_REGENERATE_PATH, {})).toThrow('message_id');
  });
});

describe('StreamError', () => {
  it('carries Retry-After off a 429, which is unreachable anywhere else', async () => {
    // `streamFrames` consumes and discards the Response, so a caller holding only the error
    // knew the status and the body and nothing about when to try again (NFR-SEC-07).
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{"detail":"slow down"}', { status: 429, headers: { 'Retry-After': '30' } }),
      ),
    );
    try {
      await streamFrames('/api/v1/documents/events').next();
      expect.unreachable('a 429 must throw');
    } catch (error) {
      expect(error).toBeInstanceOf(StreamError);
      expect((error as StreamError).status).toBe(429);
      expect((error as StreamError).retryAfter).toBe('30');
      expect((error as StreamError).body).toContain('slow down');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('is null when the server sent no header', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('{}', { status: 404 })),
    );
    try {
      await streamFrames('/api/v1/documents/events').next();
      expect.unreachable('a 404 must throw');
    } catch (error) {
      expect((error as StreamError).retryAfter).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('the middleware is actually registered', () => {
  // The one thing the direct-invocation tests above cannot see. A source guard rather than a
  // behavioural one for the reason given in the file header — and it is the same shape as the
  // `tokens.ts` cross-language guard: cheap, and it fails when someone deletes the line.
  it('client.ts hands sessionMiddleware to api.use', () => {
    const code = stripTsComments(readSource('src/api/client.ts'));
    expect(code).toContain('api.use(sessionMiddleware)');
  });

  it('streamFrames freshens and reports 401 too', () => {
    // SSE never passes through openapi-fetch middleware at all (R-41(3)), so the two rules
    // have to be written a second time there — and this is what notices if one is removed.
    const code = stripTsComments(readSource('src/api/client.ts'));
    const stream = code.slice(code.indexOf('export async function* streamFrames'));
    expect(stream).toContain('await ensureFreshToken()');
    expect(stream).toMatch(/response\.status === 401.*reportUnauthorized\(\)/s);
  });
});
