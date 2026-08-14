/**
 * The chat stream over a real `fetch` and a hand-fed `ReadableStream` — the
 * `useDocumentStream.test.tsx` harness, one route over.
 *
 * Two things here are not covered anywhere else. **A POST-bodied SSE request has never existed
 * in this codebase** — `streamFrames` had only ever done a bodyless GET for the document
 * channel — so the request shape is asserted rather than assumed. And **every chat refusal is a
 * pre-first-byte HTTP status**: `admit_send` and `admit_regeneration` resolve ownership, the
 * budget and the rate limit as dependencies, so a 404/409/429 must arrive as a `StreamError`
 * and be classified by the same function the non-streaming calls use.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ChatFrame } from '../api';
import { setAccessToken } from '../api';
import { streamRegenerate, streamSend } from './useChatStream';

afterEach(() => {
  vi.unstubAllGlobals();
  setAccessToken(null);
});

/** One SSE frame, exactly as the backend writes it: a named event plus the whole envelope. */
function wire(frame: ChatFrame): string {
  return `event: ${frame.event}\ndata: ${JSON.stringify(frame)}\n\n`;
}

const ANSWER: ChatFrame = {
  event: 'message',
  data: {
    outcome: 'answered',
    error_code: null,
    message: {
      id: 'a1',
      role: 'ai',
      segs: [{ text: 'The refund window is 30 days.' }],
      created_at: '2026-08-13T09:00:00Z',
    },
  },
};

/** Serve a body the test pushes into, and hand back the `RequestInit` the caller used. */
function serveStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }),
  );
  const encoder = new TextEncoder();
  return {
    calls,
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
  };
}

function serveError(status: number, body: string, headers: Record<string, string> = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(body, { status, headers })),
  );
}

describe('the request', () => {
  it('POSTs the query and the mentioned documents as JSON', async () => {
    const stream = serveStream();
    const frames: ChatFrame[] = [];
    const done = streamSend('c1', 'What is the refund window?', ['d1', 'd2'], {
      onFrame: (frame) => frames.push(frame),
      signal: new AbortController().signal,
    });
    stream.close();
    await done;

    const [{ url, init }] = stream.calls;
    expect(url).toBe('/api/v1/conversations/c1/messages');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      query: 'What is the refund window?',
      document_ids: ['d1', 'd2'],
    });
  });

  it('sends Content-Type AND Accept — the header set is added to, not replaced', async () => {
    // A plain object literal, never a `Headers`: `streamFrames` spreads `init.headers` into its
    // own set, and spreading a `Headers` instance yields `{}` — the body would go out as
    // text/plain and FastAPI would answer 422 (§8.58(9), the same trap one layer up).
    const stream = serveStream();
    setAccessToken('tok', 300);
    const done = streamSend('c1', 'q', [], {
      onFrame: () => undefined,
      signal: new AbortController().signal,
    });
    stream.close();
    await done;

    const headers = stream.calls[0].init.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers.Accept).toBe('text/event-stream');
    expect(headers.Authorization).toBe('Bearer tok');
  });

  it('regenerate POSTs to the message route with no body at all', async () => {
    const stream = serveStream();
    const done = streamRegenerate('m1', {
      onFrame: () => undefined,
      signal: new AbortController().signal,
    });
    stream.close();
    await done;

    expect(stream.calls[0].url).toBe('/api/v1/messages/m1/regenerate');
    expect(stream.calls[0].init.body).toBeUndefined();
  });
});

describe('the frames', () => {
  it('delivers stage, message and done in order', async () => {
    const stream = serveStream();
    const frames: ChatFrame[] = [];
    const done = streamSend('c1', 'q', [], {
      onFrame: (frame) => frames.push(frame),
      signal: new AbortController().signal,
    });
    stream.push(wire({ event: 'stage', data: { stage: 'preparing' } }));
    stream.push(wire({ event: 'stage', data: { stage: 'generating' } }));
    stream.push(wire(ANSWER));
    stream.push(wire({ event: 'done', data: { outcome: 'answered' } }));
    stream.close();

    expect(await done).toBeNull();
    expect(frames.map((f) => f.event)).toEqual(['stage', 'stage', 'message', 'done']);
  });

  it('reassembles a frame split across two chunks', async () => {
    // The buffering path in `streamFrames`, exercised for real: a 6 s answer arrives in
    // whatever pieces the socket hands over, and the split can land anywhere.
    const stream = serveStream();
    const frames: ChatFrame[] = [];
    const done = streamSend('c1', 'q', [], {
      onFrame: (frame) => frames.push(frame),
      signal: new AbortController().signal,
    });
    const text = wire(ANSWER);
    stream.push(text.slice(0, 30));
    stream.push(text.slice(30));
    stream.close();
    await done;

    expect(frames).toHaveLength(1);
    expect(frames[0]).toEqual(ANSWER);
  });

  it('stops delivering once the caller aborts', async () => {
    const stream = serveStream();
    const controller = new AbortController();
    const frames: ChatFrame[] = [];
    const done = streamSend('c1', 'q', [], {
      onFrame: (frame) => frames.push(frame),
      signal: controller.signal,
    });
    controller.abort();
    stream.push(wire(ANSWER));
    stream.close();

    expect(await done).toBeNull();
    expect(frames).toEqual([]);
  });
});

describe('the pre-first-byte refusals', () => {
  const start = () =>
    streamSend('c1', 'q', [], {
      onFrame: () => undefined,
      signal: new AbortController().signal,
    });

  it('409 NOT_LATEST_ANSWER is stale, with the server copy', async () => {
    serveError(
      409,
      JSON.stringify({ detail: { error_code: 'NOT_LATEST_ANSWER', message: 'Only the most…' } }),
    );
    expect(
      await streamRegenerate('m1', {
        onFrame: () => undefined,
        signal: new AbortController().signal,
      }),
    ).toEqual({ kind: 'stale', detail: 'Only the most…' });
  });

  it('409 CONTEXT_WINDOW_EXCEEDED is frozen, and carries numbers rather than copy', async () => {
    serveError(
      409,
      JSON.stringify({
        detail: {
          error_code: 'CONTEXT_WINDOW_EXCEEDED',
          message: 'This conversation has reached its length limit.',
          used_tokens: 9_100,
          limit_tokens: 10_400,
        },
      }),
    );
    expect(await start()).toEqual({ kind: 'frozen', usedTokens: 9_100, limitTokens: 10_400 });
  });

  it('429 exposes Retry-After', async () => {
    serveError(429, JSON.stringify({ detail: 'Slow down.' }), { 'Retry-After': '12' });
    expect(await start()).toEqual({
      kind: 'throttled',
      detail: 'Slow down.',
      retryAfter: '12',
    });
  });

  it('404 is gone', async () => {
    serveError(404, JSON.stringify({ detail: 'Conversation not found.' }));
    expect(await start()).toEqual({ kind: 'gone' });
  });

  it('a transport failure is a refusal with status 0', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    const failure = await start();
    expect(failure).toMatchObject({ kind: 'refused', status: 0 });
  });

  it('a body that is not JSON does not throw — a proxy error page', async () => {
    serveError(502, '<html>Bad gateway</html>');
    const failure = await start();
    expect(failure).toMatchObject({ kind: 'refused', status: 502 });
    // Nothing from that page reaches a user.
    expect(JSON.stringify(failure)).not.toContain('html');
  });
});
