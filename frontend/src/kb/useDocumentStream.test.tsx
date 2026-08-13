/**
 * The R-41 transport: framing, cancellation, and which failures are terminal.
 *
 * Driven by a hand-fed `ReadableStream` rather than a mocked generator, so the buffering in
 * `streamFrames` is exercised for real — a frame split across two chunks is the branch that loop
 * exists for and the one a mock would skip.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { backoffDelay, useDocumentStream } from './useDocumentStream';
import type { DocumentAction } from './documents';

/** A stream the test pushes into, plus the `init` the hook passed to `fetch`. */
function stubStream(status = 200) {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const inits: RequestInit[] = [];
  const fetchMock = vi.fn((_url: string, init: RequestInit) => {
    inits.push(init);
    return Promise.resolve(
      new Response(status === 200 ? body : 'nope', {
        status,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return {
    fetchMock,
    inits,
    push: (text: string) => act(() => controller.enqueue(encoder.encode(text))),
    close: () => act(() => controller.close()),
  };
}

/** A STABLE dispatch. An inline `vi.fn()` is a new identity every render, which is exactly
 *  the caller mistake the hook's ref now absorbs — see the last test in this file. */
const noop = () => undefined;

const frame = (event: string, data: unknown) =>
  `event: ${event}\ndata: ${JSON.stringify({ event, data })}\n\n`;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('the environment this suite relies on', () => {
  it('has a same-realm TextDecoderStream (jsdom supplies none; Node’s survives)', () => {
    // `streamFrames` pipes through one. jsdom defines neither it nor `ReadableStream`, so Node's
    // globals remain visible and the pipe is same-realm. If a future jsdom ships its own
    // `ReadableStream`, `pipeThrough` starts throwing across realms — this names the cause
    // instead of leaving a mystery failure in every stream test at once.
    expect(typeof TextDecoderStream).toBe('function');
    expect(typeof ReadableStream).toBe('function');
  });
});

describe('framing', () => {
  it('dispatches a snapshot frame', async () => {
    const stream = stubStream();
    const dispatch = vi.fn<(action: DocumentAction) => void>();
    renderHook(() => useDocumentStream(true, dispatch));

    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalled());
    stream.push(frame('snapshot', []));

    await waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'snapshot', documents: [] }));
  });

  it('buffers a frame split across two chunks', async () => {
    // The branch the read loop exists for: a chunk boundary can fall anywhere, including inside
    // the JSON, and a naive parse-per-chunk would throw on exactly this.
    const stream = stubStream();
    const dispatch = vi.fn<(action: DocumentAction) => void>();
    renderHook(() => useDocumentStream(true, dispatch));
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalled());

    stream.push('event: removed\ndata: {"event":"remov');
    expect(dispatch).not.toHaveBeenCalled();
    stream.push('ed","data":{"document_id":"d9"}}\n\n');

    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: 'removed', documentId: 'd9' }),
    );
  });

  it('ignores a keepalive, which carries no data line', async () => {
    const stream = stubStream();
    const dispatch = vi.fn<(action: DocumentAction) => void>();
    renderHook(() => useDocumentStream(true, dispatch));
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalled());

    stream.push(': ping\n\n');
    stream.push(frame('snapshot', []));

    await waitFor(() => expect(dispatch).toHaveBeenCalledTimes(1));
  });
});

describe('lifecycle', () => {
  it('does not connect while the modal is closed', () => {
    const stream = stubStream();
    renderHook(() => useDocumentStream(false, vi.fn()));
    expect(stream.fetchMock).not.toHaveBeenCalled();
  });

  it('aborts the request on unmount', async () => {
    const stream = stubStream();
    const { unmount } = renderHook(() => useDocumentStream(true, noop));
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalled());

    unmount();

    expect(stream.inits[0].signal?.aborted).toBe(true);
  });

  it('aborts when the modal closes, and reconnects when it reopens', async () => {
    const stream = stubStream();
    const { rerender } = renderHook(({ open }) => useDocumentStream(open, noop), {
      initialProps: { open: true },
    });
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalledTimes(1));

    rerender({ open: false });
    expect(stream.inits[0].signal?.aborted).toBe(true);

    rerender({ open: true });
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalledTimes(2));
  });

  it('does not reconnect when only the dispatch identity changes', async () => {
    // Found by this file getting it wrong: an inline callback is a new identity every render, and
    // with `dispatch` in the effect's deps that aborts the live connection and opens another —
    // once per render, silently, against a four-slot-per-user budget. The hook reads it through a
    // ref so a caller cannot cause that.
    const stream = stubStream();
    const { rerender } = renderHook(({ onFrame }) => useDocumentStream(true, onFrame), {
      initialProps: { onFrame: () => undefined },
    });
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalledTimes(1));

    rerender({ onFrame: () => undefined });
    rerender({ onFrame: () => undefined });
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(stream.fetchMock).toHaveBeenCalledTimes(1);
    expect(stream.inits[0].signal?.aborted).toBe(false);
  });

  it('still dispatches through the LATEST callback after it changes', async () => {
    // The other half of the ref: holding the first callback for the stream's lifetime would be a
    // stale-closure bug traded for the reconnect one.
    const stream = stubStream();
    const first = vi.fn<(action: DocumentAction) => void>();
    const second = vi.fn<(action: DocumentAction) => void>();
    const { rerender } = renderHook(({ onFrame }) => useDocumentStream(true, onFrame), {
      initialProps: { onFrame: first },
    });
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalled());

    rerender({ onFrame: second });
    stream.push(frame('snapshot', []));

    await waitFor(() => expect(second).toHaveBeenCalledTimes(1));
    expect(first).not.toHaveBeenCalled();
  });

  it('does not treat its own abort as a failure worth retrying', async () => {
    // Aborting rejects the pending read AND the generator's `finally` cancel, both as
    // `AbortError`. Reading either as transport failure would schedule a reconnect for a stream
    // nobody wants — and, with the modal reopened, a second live connection against the cap.
    const stream = stubStream();
    const { unmount } = renderHook(() => useDocumentStream(true, noop));
    await waitFor(() => expect(stream.fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect(stream.fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('failures that must not become a retry loop', () => {
  it('stops on 429 — the stream cap another tab holds', async () => {
    // R-41(7). Retrying cannot free a slot this client does not own, so a loop would only burn
    // the rate limiter; the modal degrades to the one-shot list instead.
    const stream = stubStream(429);
    const { result } = renderHook(() => useDocumentStream(true, noop));

    await waitFor(() => expect(result.current).toBe('capped'));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(stream.fetchMock).toHaveBeenCalledTimes(1);
  });

  it('stops on 401 rather than looping against an expired token', async () => {
    const stream = stubStream(401);
    const { result } = renderHook(() => useDocumentStream(true, noop));

    await waitFor(() => expect(result.current).toBe('unauthorized'));
    expect(stream.fetchMock).toHaveBeenCalledTimes(1);
  });

  it('stops on 404, which a reconnect cannot fix', async () => {
    const stream = stubStream(404);
    const { result } = renderHook(() => useDocumentStream(true, noop));

    await waitFor(() => expect(result.current).toBe('failed'));
    expect(stream.fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('backoff', () => {
  it('never retries faster than the server’s own poll interval', () => {
    // A retry inside 1.5s cannot return fresher data (R-41(2)) — it only adds load to the surface
    // whose cap exists to bound it.
    expect(backoffDelay(1, 0)).toBeGreaterThanOrEqual(750);
    expect(backoffDelay(1, 1)).toBe(1_500);
  });

  it('grows and then caps', () => {
    expect(backoffDelay(2, 1)).toBe(3_000);
    expect(backoffDelay(3, 1)).toBe(6_000);
    expect(backoffDelay(99, 1)).toBe(30_000);
  });

  it('is pure — the jitter is a parameter, not a call to Math.random', () => {
    expect(backoffDelay(4, 0.25)).toBe(backoffDelay(4, 0.25));
    expect(backoffDelay(4, 0.25)).not.toBe(backoffDelay(4, 1));
  });
});
