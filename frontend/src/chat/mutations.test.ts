/**
 * `classifyChat` against every status the chat surface documents.
 *
 * A table test with no transport, on `kb/mutations.test.ts`'s precedent: the calls are thin and
 * the classifier is the substance. The two `409`s are the reason the file exists — they share a
 * status and mean opposite things, and the client's behaviour diverges completely between them.
 */
import { describe, expect, it } from 'vitest';

import { CONTEXT_WINDOW_EXCEEDED, NOT_LATEST_ANSWER, classifyChat } from './mutations';
import { readSource, stripTsComments } from '../test/css-source';

const FALLBACK = 'fallback copy';

const budgetBody = {
  detail: {
    error_code: CONTEXT_WINDOW_EXCEEDED,
    message: 'This conversation has reached its length limit.',
    used_tokens: 9_100,
    limit_tokens: 10_400,
    overflow_tokens: 200,
  },
};

describe('success', () => {
  it.each([200, 201])('%d carries the body through', (status) => {
    expect(classifyChat({ status, data: { id: 'c1' } }, FALLBACK)).toEqual({
      kind: 'ok',
      data: { id: 'c1' },
    });
  });

  it('204 is a success with no body — DELETE', () => {
    // Grouped with the others deliberately: a caller should not have to know which verb it
    // called to find out whether it worked.
    expect(classifyChat({ status: 204, data: undefined }, FALLBACK)).toEqual({
      kind: 'ok',
      data: undefined,
    });
  });
});

describe('the two 409s are different facts', () => {
  it('NOT_LATEST_ANSWER is stale, and carries the server copy', () => {
    const outcome = classifyChat(
      { status: 409, error: { detail: { error_code: NOT_LATEST_ANSWER, message: 'Only the…' } } },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'stale', detail: 'Only the…' });
  });

  it('CONTEXT_WINDOW_EXCEEDED is frozen, and carries NUMBERS not copy', () => {
    // R-51(6)/R-69(1): the composer blocks before a request exists and FR-STA-04's own line is
    // the source for both sides, so this response's `message` must never reach a user. What
    // the client needs from it is the usage its own copy was stale about.
    const outcome = classifyChat({ status: 409, error: budgetBody }, FALLBACK);
    expect(outcome).toEqual({ kind: 'frozen', usedTokens: 9_100, limitTokens: 10_400 });
    expect(JSON.stringify(outcome)).not.toContain('length limit');
  });

  it('defaults the counts rather than trusting a malformed body', () => {
    const outcome = classifyChat(
      { status: 409, error: { detail: { error_code: CONTEXT_WINDOW_EXCEEDED, used_tokens: 'x' } } },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'frozen', usedTokens: 0, limitTokens: 0 });
  });

  it('an unrecognised 409 code is an ordinary refusal, not a guess', () => {
    // Conservative on purpose: a code this build has never heard of must render the server's
    // copy, not be forced into whichever branch looks closest.
    expect(
      classifyChat(
        { status: 409, error: { detail: { error_code: 'WHO_KNOWS', message: 'no' } } },
        FALLBACK,
      ),
    ).toEqual({ kind: 'refused', detail: 'no', status: 409 });
  });
});

describe('the rest of the documented statuses', () => {
  it('404 is gone, with no copy — the store was stale', () => {
    expect(
      classifyChat({ status: 404, error: { detail: 'Conversation not found.' } }, FALLBACK),
    ).toEqual({ kind: 'gone' });
  });

  it('429 carries the copy and the Retry-After', () => {
    expect(
      classifyChat({ status: 429, error: { detail: 'Too many' }, retryAfter: '30' }, FALLBACK),
    ).toEqual({ kind: 'throttled', detail: 'Too many', retryAfter: '30' });
  });

  it('429 without the header still classifies', () => {
    const outcome = classifyChat({ status: 429, error: { detail: 'Too many' } }, FALLBACK);
    expect(outcome).toEqual({ kind: 'throttled', detail: 'Too many', retryAfter: null });
  });

  it('503 is unavailable — the chat is intact and the retry is meaningful (R-54(5))', () => {
    expect(classifyChat({ status: 503, error: { detail: "Couldn't delete" } }, FALLBACK)).toEqual({
      kind: 'unavailable',
      detail: "Couldn't delete",
    });
  });

  it('422 is invalid and carries NO detail — it is a validation array', () => {
    const outcome = classifyChat(
      { status: 422, error: { detail: [{ loc: ['body', 'feedback'], msg: 'Field required' }] } },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'invalid' });
    expect(JSON.stringify(outcome)).not.toContain('Field required');
  });

  it('401 says nothing — the transport already reported the session', () => {
    expect(classifyChat({ status: 401, error: { detail: 'Not authenticated' } }, FALLBACK)).toEqual(
      {
        kind: 'unauthorized',
      },
    );
  });

  it('falls back only when the server sent no copy', () => {
    expect(classifyChat({ status: 500 }, FALLBACK)).toEqual({
      kind: 'refused',
      detail: FALLBACK,
      status: 500,
    });
    expect(classifyChat({ status: 500, error: { detail: 'boom' } }, FALLBACK)).toEqual({
      kind: 'refused',
      detail: 'boom',
      status: 500,
    });
  });
});

describe('the copy rule (R-57(4))', () => {
  it('this folder keeps no copy of a server-owned string', () => {
    // The same guard `src/kb` carries. A second copy of `detail` text drifts silently, and the
    // originals are all `TBD(§8.4)` — they *will* change.
    const SERVER_OWNED = [
      'Conversation not found.',
      'Message not found.',
      'Only the most recent answer',
      'This conversation has reached its length limit',
      "Couldn't delete this chat",
    ];
    for (const file of ['mutations.ts', 'turns.ts', 'useChat.ts', 'useChatStream.ts']) {
      const code = stripTsComments(readSource(`src/chat/${file}`));
      for (const copy of SERVER_OWNED) {
        expect(code, `${file} duplicates server-owned copy: ${copy}`).not.toContain(copy);
      }
    }
  });
});
