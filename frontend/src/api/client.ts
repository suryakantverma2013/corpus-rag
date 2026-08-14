/**
 * The typed HTTP client (T-405, R-57).
 *
 * `openapi-fetch` over the generated `paths` — every request path, parameter, body and response
 * is checked against `schema.d.ts`, which is regenerated from `backend/openapi.json` on every
 * `npm run build`. Nothing here is hand-typed, and nothing here should become hand-typed.
 *
 * **Same origin by design.** Production serves the SPA and the API behind one reverse proxy, and
 * the Vite dev server proxies `/api` and `/health` to reproduce that locally — which is why the
 * backend carries no CORS middleware and must not acquire one.
 *
 * **The three SSE endpoints are not reachable through this client** — see `streamFrames`.
 */
import createClient from 'openapi-fetch';
import type { Middleware } from 'openapi-fetch';

import { authHeaders, currentAccessToken, ensureFreshToken, reportUnauthorized } from './auth';
import type { components, paths } from './schema';

export const api = createClient<paths>({ baseUrl: '' });

/**
 * The routes that carry no bearer at all — so there is nothing to freshen.
 *
 * `refresh` being here is not an optimisation, it is required: it is reached through this same
 * client, so freshening it would call the refresher from inside the refresher and await a
 * promise that cannot settle until it returns. The symptom is a hung boot, not an error.
 *
 * **Not "everything under /auth"**: `/auth/me` and `/auth/change-password` are ordinary
 * authenticated calls and must be freshened like any other.
 */
const UNAUTHENTICATED: readonly string[] = [
  '/api/v1/auth/login',
  '/api/v1/auth/refresh',
  '/api/v1/auth/logout',
];

/**
 * The routes whose 401 is *not* the end of the session — the correction FR-AUT-07 needs.
 *
 * Read literally ("any API 401 triggers the return-to-login flow") it signs the user out when
 * `login` rejects a typo, and when `change-password` reports that the **current** password was
 * wrong — the latter being an inline error FR-AUT-09 specifies, delivered instead by throwing
 * the user back to a login screen.
 *
 * **A superset of the list above, and deliberately not the same list.** `change-password`
 * belongs here and *not* there: its 401 means "wrong current password", but it still needs a
 * live bearer, and skipping its renewal would let a stale token produce a 401 that this list
 * then swallows — leaving the user with "Not authenticated" as an inline error and no way out.
 */
const SESSION_401_EXEMPT: readonly string[] = [...UNAUTHENTICATED, '/api/v1/auth/change-password'];

/** Compares the *path*, never a substring: `startsWith` would exempt `/auth/login-history`,
 *  and `includes` would exempt anything carrying `?next=/api/v1/auth/login`. */
const pathIn = (list: readonly string[], url: string): boolean =>
  list.includes(new URL(url, 'http://placeholder').pathname);

/**
 * The bearer header, on every request this client makes — renewed first if it is about to
 * expire (R-72(2)).
 *
 * Registered here rather than left to the surfaces because T-508 authenticates its SSE stream
 * (which cannot go through middleware at all) and would otherwise send its four mutations *un*
 * authenticated — a split-brain that presents as a backend bug.
 *
 * `request.headers.set(...)` is **additive**, and that is load-bearing: `openapi-fetch` calls
 * `fetch(Request)`, so a shim written as `fetch(url, {...init, headers})` replaces the
 * Request's headers wholesale — stripping a multipart `Content-Type` *and its boundary*, which
 * FastAPI answers with a 422 that looks nothing like a header problem (§8.58(9)).
 */
export const sessionMiddleware: Middleware = {
  async onRequest({ request }) {
    if (!pathIn(UNAUTHENTICATED, request.url)) await ensureFreshToken();
    const token = currentAccessToken();
    if (token !== null) request.headers.set('Authorization', `Bearer ${token}`);
    return request;
  },
  onResponse({ request, response }) {
    if (response.status === 401 && !pathIn(SESSION_401_EXEMPT, request.url)) {
      reportUnauthorized();
    }
    return response;
  },
};

/**
 * Exported and then registered, rather than written inline, so it can be tested at all.
 *
 * `api` is built on `baseUrl: ''` (R-57's single origin), and `openapi-fetch` constructs a
 * `Request` from the raw path — which the browser resolves against `location` and Node's
 * `undici` rejects outright. So a test cannot reach this logic through `api`; it reaches it
 * here instead, with a real `Request`. `client.test.ts` guards the registration below by
 * reading this file, on the same reasoning as the `tokens.ts` cross-language guard (§8.56(3)).
 */
api.use(sessionMiddleware);

/**
 * An SSE endpoint answered with a non-2xx.
 *
 * A typed error rather than `new Error('stream failed: ' + status)`, because callers must branch
 * on the status and matching a formatted message is the anti-pattern R-57(4) forbids for the two
 * chat `409`s. The distinctions are real and lead to different behaviour: `429` is R-41(7)'s
 * per-user stream cap, where retrying cannot free a slot another tab holds; `401` is T-509's; and
 * `400`/`404` mean the request itself was wrong, so a reconnect loop is superstition.
 */
export class StreamError extends Error {
  readonly status: number;
  readonly body: string | null;
  /**
   * The `Retry-After` header, when the server sent one — NFR-SEC-07's 429 does.
   *
   * Read here because it is unreachable anywhere else: the response object is consumed and
   * discarded inside `streamFrames`, so a caller holding only the error had the status and the
   * body and nothing about *when*. Kept as the raw header value rather than parsed seconds —
   * the header admits an HTTP-date too, and no surface renders a countdown (R-72(4)).
   */
  readonly retryAfter: string | null;

  constructor(status: number, body: string | null, retryAfter: string | null = null) {
    super(`stream failed: ${status}`);
    this.name = 'StreamError';
    this.status = status;
    this.body = body;
    this.retryAfter = retryAfter;
  }
}

/**
 * An SSE path, checked against the generated `paths` at compile time.
 *
 * `streamFrames` takes a plain string because it is `fetch`, not `openapi-fetch` — so nothing
 * otherwise stops a typo, or a route rename on the backend, from reaching production as a 404 at
 * runtime. This is the cheapest way to put the one thing that *is* checkable under the generator.
 */
export function streamPath<P extends keyof paths & string>(path: P): P {
  return path;
}

/**
 * Fill a path template's `{param}` segments.
 *
 * `streamPath` checks the *template* against the generated `paths`; the two chat streams are
 * parameterised, so something has to interpolate — and interpolating at each call site is how one
 * of them quietly stops encoding. `encodeURIComponent` per value, so an id that is not a UUID
 * cannot escape its segment.
 */
export function expandPath<P extends keyof paths & string>(
  template: P,
  params: Readonly<Record<string, string>>,
): string {
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => {
    const value = params[name];
    // A missing parameter must not silently produce a literal `{id}` in a URL — that reaches
    // the server as a 404 whose cause is invisible.
    if (value === undefined) throw new Error(`expandPath: no value for ${whole} in ${template}`);
    return encodeURIComponent(value);
  });
}

/** FR-KBM-09's live channel (R-41), consumed by T-508. */
export const DOCUMENT_EVENTS_URL = streamPath('/api/v1/documents/events');

/** FR-CMP-03's send (R-54(2)) and FR-MSG-08's Regenerate (R-56) — one frame shape, two routes.
 *  Templates, not URLs: expand them with {@link expandPath}. */
export const CHAT_SEND_PATH = streamPath('/api/v1/conversations/{conversation_id}/messages');
export const CHAT_REGENERATE_PATH = streamPath('/api/v1/messages/{message_id}/regenerate');

/** One parsed SSE frame: the whole envelope, discriminated by `event`. */
export type ChatFrame = components['schemas']['ChatStreamFrame'];
export type DocumentFrame = components['schemas']['DocumentStreamFrame'];

/**
 * Read an SSE endpoint as a stream of typed frames.
 *
 * Hand-rolled transport, generated types. No code generator emits an SSE client, and
 * `EventSource` is ruled out outright (R-41(3)): the stream authenticates with an ordinary
 * `Authorization` header, which `EventSource` cannot send, and every query-string alternative
 * writes a credential into access logs, proxy logs, `Referer` and browser history.
 *
 * The frame's own `event` key is the discriminator, so a caller narrows on `frame.event` and
 * never has to correlate the SSE `event:` line with the payload — the backend writes the name in
 * both places for exactly that reason.
 *
 * Reconnect is the caller's: for the document stream a reconnect simply applies the fresh
 * `snapshot` every connect sends (R-41(6) — there is no `Last-Event-ID` replay).
 *
 * The `Authorization` header is applied here rather than by each caller, so the stream and the
 * `api` client above can never disagree about who is calling. It is read **per connect attempt**,
 * which is what lets a reconnect after a silent refresh pick up the new token.
 */
export async function* streamFrames<T>(
  url: string,
  init: RequestInit = {},
): AsyncGenerator<T, void, undefined> {
  // Per connect attempt, exactly as the header below is read per connect attempt: a stream
  // opened with a token that expires in five seconds authenticates once and then holds a
  // connection the server will not renew.
  await ensureFreshToken();
  const response = await fetch(url, {
    ...init,
    headers: { ...authHeaders(), ...init.headers, Accept: 'text/event-stream' },
  });
  if (!response.ok || !response.body) {
    // The body is read before throwing: it carries the server's `detail`, which is the copy the
    // R-41(7) stream-cap notice renders. Reading it also releases the connection.
    const body = await response.text().catch(() => null);
    if (response.status === 401) reportUnauthorized();
    throw new StreamError(response.status, body, response.headers.get('Retry-After'));
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;
      // Frames are separated by a blank line. A chunk may split one anywhere, so only
      // complete frames are consumed and the remainder stays buffered.
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const payload = frame
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice('data:'.length).trim())
          .join('\n');
        // Keepalive comments carry no `data:` line at all.
        if (payload) yield JSON.parse(payload) as T;
        boundary = buffer.indexOf('\n\n');
      }
    }
  } finally {
    await reader.cancel();
  }
}
