/**
 * `classifyList` driven directly with literal results — no transport, the `kb/mutations.test.ts`
 * shape. Every status the route documents is exercised here.
 */
import { describe, expect, it } from 'vitest';

import {
  ACCOUNT_NOT_LINKED,
  CLOUD_ACCESS_REVOKED,
  classifyList,
  isLinkRequired,
} from './mutations';

const FALLBACK = 'fallback copy';

const page = {
  files: [
    {
      file_id: 'f1',
      name: 'a.pdf',
      mime_type: 'application/pdf',
      size_bytes: 10,
      modified_time: null,
    },
  ],
  next_page_token: 'page-2',
};

describe('classifyList — the 200 path', () => {
  it('returns the page and the provider token verbatim', () => {
    const result = classifyList({ status: 200, data: page }, FALLBACK);
    expect(result).toEqual({
      kind: 'page',
      files: page.files,
      nextPageToken: 'page-2',
    });
  });

  it('reads a final page as one with no token', () => {
    const result = classifyList(
      { status: 200, data: { files: [], next_page_token: null } },
      FALLBACK,
    );
    expect(result).toEqual({ kind: 'page', files: [], nextPageToken: null });
  });

  it('treats a bodyless 200 as an empty page rather than a refusal', () => {
    expect(classifyList({ status: 200 }, FALLBACK)).toEqual({
      kind: 'page',
      files: [],
      nextPageToken: null,
    });
  });
});

describe('classifyList — FR-AUT-11’s two 409s', () => {
  // §8.53(5): kept distinct because a link that never existed and a grant withdrawn at Google are
  // different facts. Collapsing them would have this list contradict the status route.
  it.each([ACCOUNT_NOT_LINKED, CLOUD_ACCESS_REVOKED])(
    'carries %s through with its copy',
    (code) => {
      const result = classifyList(
        {
          status: 409,
          error: { detail: { error_code: code, message: 'server copy', provider: 'google' } },
        },
        FALLBACK,
      );
      expect(result).toEqual({ kind: 'link-required', code, detail: 'server copy' });
    },
  );

  it('falls back to our copy when the server sent a code and no message', () => {
    const result = classifyList(
      { status: 409, error: { detail: { error_code: ACCOUNT_NOT_LINKED } } },
      FALLBACK,
    );
    expect(result).toEqual({ kind: 'link-required', code: ACCOUNT_NOT_LINKED, detail: FALLBACK });
  });

  it('does not claim a 409 it cannot identify', () => {
    // A server one revision behind, or a proxy. `refused` renders the copy and offers no
    // Re-link button — claiming an unlinked account here would send the user through a consent
    // flow that fixes nothing.
    const result = classifyList({ status: 409, error: { detail: 'something else' } }, FALLBACK);
    expect(result).toEqual({ kind: 'refused', detail: 'something else', status: 409 });
  });

  it('does not treat the KB modal’s lock code as a link problem', () => {
    const result = classifyList(
      { status: 409, error: { detail: { error_code: 'PROCESSING_LOCKED', message: 'busy' } } },
      FALLBACK,
    );
    expect(result).toEqual({ kind: 'refused', detail: 'busy', status: 409 });
  });
});

describe('classifyList — everything else', () => {
  it('reports 401 as unauthorized, which T-509’s handler owns', () => {
    expect(classifyList({ status: 401 }, FALLBACK)).toEqual({ kind: 'unauthorized' });
  });

  it.each([403, 429, 500, 503])('refuses %d with the server’s copy', (status) => {
    const result = classifyList({ status, error: { detail: 'nope' } }, FALLBACK);
    expect(result).toEqual({ kind: 'refused', detail: 'nope', status });
  });

  it('renders the fallback when the body carried no copy', () => {
    expect(classifyList({ status: 503 }, FALLBACK)).toEqual({
      kind: 'refused',
      detail: FALLBACK,
      status: 503,
    });
  });

  it('never renders a 422’s validation array', () => {
    const result = classifyList(
      { status: 422, error: { detail: [{ loc: ['query', 'page_size'], msg: 'bad' }] } },
      FALLBACK,
    );
    expect(result).toEqual({ kind: 'refused', detail: FALLBACK, status: 422 });
  });
});

describe('isLinkRequired', () => {
  it.each([ACCOUNT_NOT_LINKED, CLOUD_ACCESS_REVOKED])('accepts %s', (code) => {
    expect(isLinkRequired(code)).toBe(true);
  });

  it.each([[null], ['PROCESSING_LOCKED'], ['NOT_LATEST_ANSWER'], ['']])('rejects %s', (code) => {
    expect(isLinkRequired(code)).toBe(false);
  });

  it('pins the two codes to the strings the backend enum publishes', () => {
    // They are a wire contract, not a local name: `CloudLinkRequiredDetail.error_code` is a
    // Literal on the generated schema, so a rename here would silently stop matching.
    expect(ACCOUNT_NOT_LINKED).toBe('ACCOUNT_NOT_LINKED');
    expect(CLOUD_ACCESS_REVOKED).toBe('CLOUD_ACCESS_REVOKED');
  });
});
