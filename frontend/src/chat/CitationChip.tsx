/**
 * FR-CIT-01 / FR-CIT-02 — the inline citation chip.
 *
 * Transcribed from `RAG Chatbot.dc.html` line 105, with **one deliberate departure**: the
 * prototype's element is a `<span>` carrying only `onMouseEnter`/`onMouseLeave`, which makes it
 * pointer-only — precisely what NFR-A11Y-04 forbids and what NFR-A11Y-03 names when it says the
 * prototype "is not the authority here and cannot be". It is a real `<button>` restyled to those
 * declarations, so it renders identically and gains the keyboard path: focus opens the card, blur
 * closes it, and the global `:focus-visible` ring applies.
 *
 * **Click does nothing beyond the focus the browser already gives it.** FR-CIT-05's `(D)` — "a
 * click action on chips (e.g. open document) may be added later" — stays declined, and R-31 is
 * why it can be closed rather than left open: the product has no download, export or preview
 * surface anywhere, by design, so there is no document to open.
 */
import { useId } from 'react';
import styles from './CitationChip.module.css';
import { useCitationHover } from './useCitationHover';
import type { CitationSegment } from '../api';

export interface CitationChipProps {
  segment: CitationSegment;
}

export function CitationChip({ segment }: CitationChipProps) {
  const { cite, cardId, open, close } = useCitationHover();
  const chipId = useId();
  const isOpen = cite?.chipId === chipId;

  return (
    <button
      type="button"
      className={`${styles.chip} mono`}
      // Only while this chip is the open one: a description pointing at a card that is not
      // rendered, or that is showing a different passage, is worse than none.
      aria-describedby={isOpen ? cardId : undefined}
      onMouseEnter={(event) => open(event.currentTarget, segment, chipId)}
      onMouseLeave={() => close(chipId)}
      onFocus={(event) => open(event.currentTarget, segment, chipId)}
      onBlur={() => close(chipId)}
    >
      {segment.doc}
    </button>
  );
}
