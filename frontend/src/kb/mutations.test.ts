/**
 * Every status the four verbs can answer with, and what the modal does about it.
 *
 * `classify` is pure, so this is a table rather than a transport test. The three that are easy to
 * get wrong are the ones with the most words: the `200`s are successes, `404` is a stale store
 * and `409` is two different things.
 */
import { describe, expect, it } from 'vitest';

import { PROCESSING_LOCKED, classify } from './mutations';

const FALLBACK = 'fallback copy';
const accepted = { document_id: 'd1', status: 'QUEUED' as const };

describe('the two 2xx bodies', () => {
  it('reads a 202 as accepted, carrying the server’s own status', () => {
    expect(classify({ status: 202, data: accepted }, FALLBACK)).toEqual({
      kind: 'accepted',
      documentId: 'd1',
      status: 'QUEUED',
    });
  });

  it('reads a 200 as a duplicate — a SUCCESS, not an error', () => {
    // R-33(3)/R-39(2)/R-40(1) all chose `200` over `409` on purpose: nothing was queued, and the
    // drop zone should point at the row already present rather than show a failure.
    expect(
      classify({ status: 200, data: { document_id: 'x', status: 'ACTIVE' } }, FALLBACK),
    ).toEqual({ kind: 'duplicate', documentId: 'x', status: 'ACTIVE' });
  });

  it('branches on the STATUS, not on `data.duplicate`', () => {
    // The four `200`s only began carrying a body in Rev 0.38. Branching on the flag would have
    // read `undefined` before that and would change meaning again if the field were ever dropped.
    const asDuplicate = classify(
      { status: 200, data: { ...accepted, ...{ duplicate: false } } },
      FALLBACK,
    );
    expect(asDuplicate.kind).toBe('duplicate');
  });
});

describe('the 409s', () => {
  it('recognises R-24’s gate by its error_code and never by its copy', () => {
    const outcome = classify(
      {
        status: 409,
        error: { detail: { error_code: PROCESSING_LOCKED, message: 'Actions are paused.' } },
      },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'locked', detail: 'Actions are paused.' });
  });

  it('treats every other 409 as an ordinary refusal to render', () => {
    const outcome = classify(
      { status: 409, error: { detail: 'Only a failed document can be retried.' } },
      FALLBACK,
    );
    expect(outcome).toEqual({
      kind: 'refused',
      detail: 'Only a failed document can be retried.',
      status: 409,
    });
  });
});

describe('the rest of the table', () => {
  const cases = [
    [400, 'refused'],
    [403, 'refused'],
    [413, 'refused'],
    [415, 'refused'],
    [429, 'refused'],
    [503, 'refused'],
    [507, 'refused'],
  ] as const;

  it.each(cases)('renders %i as a refusal carrying the server’s detail', (status, kind) => {
    const outcome = classify({ status, error: { detail: 'server copy' } }, FALLBACK);
    expect(outcome.kind).toBe(kind);
    expect(outcome).toMatchObject({ detail: 'server copy' });
  });

  it('treats 404 as a stale store, not as a rejection', () => {
    // The row was deleted in another tab, or the purge finished between the render and the click.
    // Dropping it is the correct outcome; an error would blame the user for clicking what we were
    // still showing them.
    expect(classify({ status: 404, error: { detail: 'Document not found.' } }, FALLBACK)).toEqual({
      kind: 'gone',
    });
  });

  it('never renders a 422’s validation array', () => {
    const outcome = classify(
      { status: 422, error: { detail: [{ loc: ['body', 'file'], msg: 'field required' }] } },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'invalid' });
  });

  it('hands a 401 to the auth seam rather than inventing copy', () => {
    expect(classify({ status: 401, error: { detail: 'Not authenticated' } }, FALLBACK)).toEqual({
      kind: 'unauthorized',
    });
  });

  it('falls back only when the server sent no detail', () => {
    expect(classify({ status: 503 }, FALLBACK)).toEqual({
      kind: 'refused',
      detail: FALLBACK,
      status: 503,
    });
  });
});

describe('FR-KBM-10 — the import route’s other 409 (T-512)', () => {
  // `ImportConflictResponse` is a union: the same status carries "a turn is running" and "your
  // Drive is not connected", and they have different actions, so both branches must survive.
  it.each(['ACCOUNT_NOT_LINKED', 'CLOUD_ACCESS_REVOKED'])(
    'reads %s as link-required, carrying the code and the server’s copy',
    (code) => {
      const outcome = classify(
        {
          status: 409,
          error: { detail: { error_code: code, message: 'server copy', provider: 'google' } },
        },
        FALLBACK,
      );
      expect(outcome).toEqual({ kind: 'link-required', code, detail: 'server copy' });
    },
  );

  it('still reads the processing lock, which shares the status', () => {
    const outcome = classify(
      { status: 409, error: { detail: { error_code: 'PROCESSING_LOCKED', message: 'busy' } } },
      FALLBACK,
    );
    expect(outcome).toEqual({ kind: 'locked', detail: 'busy' });
  });

  it('claims neither for a 409 it cannot identify', () => {
    const outcome = classify({ status: 409, error: { detail: 'Duplicate bytes.' } }, FALLBACK);
    expect(outcome).toEqual({ kind: 'refused', detail: 'Duplicate bytes.', status: 409 });
  });
});
