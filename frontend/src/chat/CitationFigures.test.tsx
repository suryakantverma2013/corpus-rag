/**
 * FR-CIT-07's figure block (T-716, R-94).
 *
 * The assertions that matter are the ones about what does *not* render: a citation with no
 * figure, and a fetch that fails, must both leave the answer exactly as it was before this
 * component existed. A broken image under someone's answer is worse than no image, because a
 * document mid-replace legitimately has none (T-715, R-40(3)).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';

import { CitationFigures } from './CitationFigures';
import { readSource, stripTsComments } from '../test/css-source';
import type { CitationFigure, CitationSegment } from '../api';

const get = vi.fn();
vi.mock('../api/client', () => ({ api: { GET: (...args: unknown[]) => get(...args) } }));

const DOC_ID = '11111111-1111-4111-8111-111111111111';
const SHA = 'a'.repeat(64);

function figure(overrides: Partial<CitationFigure> = {}): CitationFigure {
  return {
    documentId: DOC_ID,
    contentSha256: SHA,
    caption: 'FIGURE 3 The intermediate value theorem',
    widthPx: 320,
    heightPx: 240,
    ...overrides,
  };
}

function cite(figures: CitationFigure[] | undefined, overrides: Partial<CitationSegment> = {}) {
  return {
    isCite: true,
    doc: 'handbook.pdf',
    page: 'p. 157',
    quote: 'the passage',
    chunkId: 'c1',
    ...(figures ? { figures } : {}),
    ...overrides,
  } as CitationSegment;
}

let created: string[];
let revoked: string[];

beforeEach(() => {
  created = [];
  revoked = [];
  let n = 0;
  // jsdom implements neither, and the pair is the whole lifetime story this component owns.
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: () => {
      const url = `blob:test/${(n += 1)}`;
      created.push(url);
      return url;
    },
    revokeObjectURL: (url: string) => revoked.push(url),
  });
  get.mockResolvedValue({ data: new Blob(['png'], { type: 'image/png' }) });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  get.mockReset();
});

describe('CitationFigures', () => {
  it('renders nothing at all when no citation carries a figure', async () => {
    const { container } = render(<CitationFigures citations={[cite(undefined)]} />);
    await act(async () => {});

    // FR-CIT-07: "a citation with no figure renders exactly as it does today". Not an empty
    // wrapper — nothing, so no margin, no border, no gap in the answer.
    expect(container.firstChild).toBeNull();
    expect(get).not.toHaveBeenCalled();
  });

  it('renders the figure with alt text naming its caption, document and page', async () => {
    render(<CitationFigures citations={[cite([figure()])]} />);

    // NFR-A11Y-03 — all three, because the alt is the only thing a screen reader gets.
    const image = await screen.findByRole('img', {
      name: 'Figure from handbook.pdf, p. 157: FIGURE 3 The intermediate value theorem',
    });
    expect(image.getAttribute('src')).toBe('blob:test/1');
  });

  it('captions an uncaptioned figure with its document and page', async () => {
    render(<CitationFigures citations={[cite([figure({ caption: null })])]} />);

    // FR-CIT-07: "the document's own caption where it has one and its page otherwise" — and it
    // still names the document, so it reads as the document's figure rather than as ours.
    expect(await screen.findByText('Figure from handbook.pdf, p. 157')).toBeTruthy();
  });

  it('reserves the box from the figure’s own dimensions', async () => {
    render(<CitationFigures citations={[cite([figure()])]} />);

    const image = await screen.findByRole('img');
    // So a late blob does not shift the transcript under a reader mid-sentence.
    expect(image.style.aspectRatio).toBe('320 / 240');
  });

  it('omits the ratio rather than emitting an invalid one', async () => {
    render(<CitationFigures citations={[cite([figure({ heightPx: 0 })])]} />);

    const image = await screen.findByRole('img');
    // An invalid `aspect-ratio` is *dropped* by the browser, which collapses the box instead of
    // being ignored — so a bad dimension must produce no declaration, not a broken one.
    expect(image.style.aspectRatio).toBe('');
  });

  it('draws one picture when two citations in the block name the same figure', async () => {
    render(
      <CitationFigures citations={[cite([figure()]), cite([figure()], { chunkId: 'c2' })]} />,
    );

    await waitFor(() => expect(screen.getAllByRole('img')).toHaveLength(1));
  });

  it('draws both panels of a figure split across two regions', async () => {
    // R-94(2): an (a)/(b) split extracts as two regions under one caption. Both belong to the
    // cited page, and dropping one would silently show half a picture.
    render(
      <CitationFigures
        citations={[cite([figure(), figure({ contentSha256: 'b'.repeat(64), caption: null })])]}
      />,
    );

    await waitFor(() => expect(screen.getAllByRole('img')).toHaveLength(2));
  });

  it('renders nothing when the figure cannot be fetched', async () => {
    get.mockResolvedValue({ error: { detail: 'Figure not found.' } });
    const { container } = render(<CitationFigures citations={[cite([figure()])]} />);
    await act(async () => {});

    // No broken image and no error text: a document mid-replace has no figure, which FR-CIT-07
    // sanctions, and the reader of an answer is not the audience for that fact.
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toBe('');
  });

  it('revokes the object URL when it unmounts', async () => {
    const view = render(<CitationFigures citations={[cite([figure()])]} />);
    await screen.findByRole('img');

    view.unmount();

    // An object URL pins its blob until revoked, so a long transcript would otherwise hold
    // every image it ever scrolled past.
    expect(revoked).toEqual(created);
  });

  it('fetches through the typed client as a blob, not by hand', async () => {
    render(<CitationFigures citations={[cite([figure()])]} />);
    await screen.findByRole('img');

    // Going through `api` is what inherits `sessionMiddleware` — the freshened bearer and the
    // 401 handling. A bare `fetch` would render the same picture today and silently stop
    // working the first time a token expired mid-session.
    expect(get).toHaveBeenCalledWith('/api/v1/documents/{document_id}/figures/{content_sha256}', {
      params: { path: { document_id: DOC_ID, content_sha256: SHA } },
      parseAs: 'blob',
    });
  });
});

describe('the renderer still creates no image', () => {
  it('markdown.ts names no img element', () => {
    // The prohibition at the head of `markdown.ts` is unchanged by FR-CIT-07: a figure is a
    // segment-level block the caller supplies, never a markdown node with a content-chosen src.
    const source = stripTsComments(readSource('src/chat/markdown.ts'));
    expect(source).not.toMatch(/['"]img['"]/);
  });
});
