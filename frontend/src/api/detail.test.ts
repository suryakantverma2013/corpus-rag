/**
 * The one error-envelope reader, driven with the bodies it will actually meet.
 *
 * The cases that matter are the hostile ones: a `422`'s validation array must yield nothing to
 * render, and a body from a proxy or a server one revision behind must not throw. Both were
 * covered indirectly through `kb/mutations.test.ts` while this was a private helper; they are
 * pinned here now that two surfaces depend on it.
 */
import { describe, expect, it } from 'vitest';

import { detailOf } from './detail';

describe('detailOf', () => {
  it('reads the ordinary {detail: string} body', () => {
    expect(detailOf({ detail: 'File is too large.' })).toEqual({
      message: 'File is too large.',
      errorCode: null,
    });
  });

  it('reads a nested {detail: {error_code, message}} conflict', () => {
    expect(
      detailOf({ detail: { error_code: 'NOT_LATEST_ANSWER', message: 'Only the most recent…' } }),
    ).toEqual({ message: 'Only the most recent…', errorCode: 'NOT_LATEST_ANSWER' });
  });

  it('keeps the code when the conflict carries extra fields', () => {
    // `CONTEXT_WINDOW_EXCEEDED` also sends used/limit/overflow; they belong to the caller.
    const parsed = detailOf({
      detail: {
        error_code: 'CONTEXT_WINDOW_EXCEEDED',
        message: 'This conversation has reached its length limit.',
        used_tokens: 9_000,
        limit_tokens: 10_400,
        overflow_tokens: 200,
      },
    });
    expect(parsed.errorCode).toBe('CONTEXT_WINDOW_EXCEEDED');
  });

  it('yields nothing renderable for a 422 validation array', () => {
    // FastAPI's `detail` here is a list of objects. Rendering it would show a user
    // `[{"loc":["body","query"],"msg":"Field required"}]`.
    expect(detailOf({ detail: [{ loc: ['body', 'query'], msg: 'Field required' }] })).toEqual({
      message: null,
      errorCode: null,
    });
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'boom'],
    ['an empty object', {}],
    ['a detail-less object', { error: 'nope' }],
    ['a numeric code', { detail: { error_code: 7, message: 'x' } }],
    ['a missing message', { detail: { error_code: 'PROCESSING_LOCKED' } }],
  ])('survives %s', (_label, body) => {
    const parsed = detailOf(body);
    expect(typeof parsed.message === 'string' || parsed.message === null).toBe(true);
    expect(typeof parsed.errorCode === 'string' || parsed.errorCode === null).toBe(true);
  });

  it('still reports the code when the message is missing', () => {
    expect(detailOf({ detail: { error_code: 'PROCESSING_LOCKED' } })).toEqual({
      message: null,
      errorCode: 'PROCESSING_LOCKED',
    });
  });
});
