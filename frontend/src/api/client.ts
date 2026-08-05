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
 * Auth is deliberately absent: the bearer token, silent refresh and 401 handling are T-509's,
 * and belong in middleware here once the login flow exists.
 *
 * **The three SSE endpoints are not reachable through this client** — see `streamFrames`.
 */
import createClient from 'openapi-fetch';

import type { components, paths } from './schema';

export const api = createClient<paths>({ baseUrl: '' });

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
 */
export async function* streamFrames<T>(
  url: string,
  init: RequestInit = {},
): AsyncGenerator<T, void, undefined> {
  const response = await fetch(url, {
    ...init,
    headers: { ...init.headers, Accept: 'text/event-stream' },
  });
  if (!response.ok || !response.body) {
    throw new Error(`stream failed: ${response.status}`);
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
