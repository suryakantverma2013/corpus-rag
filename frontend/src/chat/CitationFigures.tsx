/**
 * FR-CIT-07 — the document's own figure, beneath the passage that cites its page (T-716, R-94).
 *
 * **What this is not.** It is not a diagram the model produced: R-94(3) selects the figure by the
 * citation's *locator*, so nothing the LLM wrote chooses, names or describes what appears here,
 * and the caption says whose picture it is. A rendered figure is a pointer to the cited page —
 * **not** a claim that the figure supports the sentence.
 *
 * **On the two "no images" claims this appears to contradict, and does not.** NFR-CMP-03 says the
 * product ships no image *assets*: the brand mark is a glyph, icons are glyphs and CSS shapes,
 * and nothing under `frontend/` is a tracked binary — all still true. `markdown.ts` separately
 * refuses to render an image *node*, because a content-chosen `src` would be an outbound request
 * on every render — also still true, and this is why the figure is a **segment-level block the
 * caller supplies** rather than a markdown node. What is drawn here is a raster the ingestion
 * pipeline re-encoded from a page region (NFR-SEC-10), fetched from the product's own
 * authenticated same-origin route. The uploaded file itself remains unservable (R-31).
 *
 * **The fetch goes through `api`, never a bare `fetch`.** T-715's route is authenticated, so the
 * bytes cannot be reached by putting a URL in `src` — and reaching them by hand would skip
 * `sessionMiddleware`'s token freshening and its 401 handling, which is the split-brain
 * `api/client.ts` warns about. `parseAs: 'blob'` keeps the typed path and yields bytes.
 *
 * **A failure renders nothing at all** — no broken image, no error text. T-715 already recorded
 * that a document mid-replace shows no figure for the duration, and FR-CIT-07 sanctions it: "a
 * citation with no figure renders exactly as it does today". A missing picture is a normal state
 * here, not an incident to report to the reader of an answer.
 *
 * **CSP note (T-716).** The `<img>` src is a `blob:` URL, not a same-origin one. The deployment
 * serves no Content-Security-Policy today — `deployment/nginx/default.conf` sets only
 * `Cache-Control` — so nothing constrains it; if one is ever added it must admit `blob:` in
 * `img-src`, or every figure silently disappears. `backend/tests/test_figure_route.py` guards
 * exactly that, and passes vacuously until such a policy exists.
 */
import { useEffect, useState } from 'react';

import styles from './CitationFigures.module.css';
import { api } from '../api/client';
import type { CitationFigure, CitationSegment } from '../api';

/** FR-CIT-07's label, naming the document so the figure reads as *its* picture, not ours. */
// TBD(§8.4) — copy.
function caption(figure: CitationFigure, doc: string, page: string | null): string {
  const where = page ?? '';
  const source = where ? `${doc}, ${where}` : doc;
  return figure.caption ? `${figure.caption} — ${source}` : `Figure from ${source}`;
}

/**
 * NFR-A11Y-03's text alternative: caption, document and page.
 *
 * It repeats what the visible `<figcaption>` says, and that is deliberate. Hiding the caption
 * from assistive technology to avoid the echo would take document content away from a screen
 * reader user to spare a sighted one a duplicate — so the figcaption is **not** `aria-hidden`.
 */
// TBD(§8.4) — copy.
function altText(figure: CitationFigure, doc: string, page: string | null): string {
  const where = page ? `${doc}, ${page}` : doc;
  return figure.caption ? `Figure from ${where}: ${figure.caption}` : `Figure from ${where}`;
}

/**
 * The figure's bytes as an object URL, or `null` while loading and for ever after a failure.
 *
 * The URL is revoked on unmount and whenever the figure changes: an object URL pins its blob
 * until it is revoked, and a long transcript scrolled through would otherwise hold every image
 * it ever showed.
 */
function useFigureBlob(documentId: string, contentSha256: string): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    void (async () => {
      const { data } = await api.GET('/api/v1/documents/{document_id}/figures/{content_sha256}', {
        params: { path: { document_id: documentId, content_sha256: contentSha256 } },
        parseAs: 'blob',
      });
      // `data` is typed from the `image/png` response, which the generator cannot narrow.
      const blob = data as Blob | undefined;
      if (cancelled || !blob) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    })();

    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [documentId, contentSha256]);

  return url;
}

interface FigureProps {
  figure: CitationFigure;
  doc: string;
  page: string | null;
}

function Figure({ figure, doc, page }: FigureProps) {
  const url = useFigureBlob(figure.documentId, figure.contentSha256);
  if (url === null) return null;

  // The box is reserved from the figure's own recorded dimensions so a late-arriving image does
  // not shift the transcript under someone who is reading it. Guarded rather than trusted: a
  // zero or absent dimension would make `aspect-ratio` invalid, and an invalid declaration is
  // *dropped*, which collapses the box instead of merely being ignored.
  const ratio =
    Number.isFinite(figure.widthPx) && Number.isFinite(figure.heightPx) && figure.heightPx > 0
      ? `${figure.widthPx} / ${figure.heightPx}`
      : undefined;

  return (
    <figure className={styles.figure}>
      <img
        className={styles.image}
        src={url}
        alt={altText(figure, doc, page)}
        style={ratio ? { aspectRatio: ratio } : undefined}
      />
      <figcaption className={styles.caption}>{caption(figure, doc, page)}</figcaption>
    </figure>
  );
}

export interface CitationFiguresProps {
  /** The citations carried by one rendered block, in document order. */
  citations: readonly CitationSegment[];
}

export function CitationFigures({ citations }: CitationFiguresProps) {
  // Deduped by (document, content) **within this block**: two chips naming the same page under
  // one paragraph would otherwise draw the same picture twice. Deliberately not deduped across
  // blocks — FR-CIT-07 puts the figure beneath the citing passage, so a page cited by two
  // paragraphs belongs beneath each of them.
  const seen = new Set<string>();
  const items: { figure: CitationFigure; doc: string; page: string | null }[] = [];
  for (const citation of citations) {
    for (const figure of citation.figures ?? []) {
      const key = `${figure.documentId}:${figure.contentSha256}`;
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ figure, doc: citation.doc, page: citation.page ?? null });
    }
  }

  if (items.length === 0) return null;

  return (
    <div className={styles.strip}>
      {items.map(({ figure, doc, page }) => (
        <Figure
          key={`${figure.documentId}:${figure.contentSha256}`}
          figure={figure}
          doc={doc}
          page={page}
        />
      ))}
    </div>
  );
}
