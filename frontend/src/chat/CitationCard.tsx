/**
 * FR-CIT-03 / FR-CIT-04 — the citation hover card.
 *
 * Transcribed per property from `RAG Chatbot.dc.html` lines 231–239, which is one of the
 * `<sc-if>` regions the prototype never paints (R-66) — so the declarations are the baseline and
 * a rendered comparison was never available for it.
 *
 * Everything renders from `cite.segment`, which is the **message row's own denormalised citation
 * payload**. That is normative, not incidental: R-36(6)(b) makes a stored `chunkId` dangle once
 * its document is replaced, and the design survives that only because the quote travels with the
 * message. Any future affordance that resolves a citation back to live document content must key
 * on document + locator, never on chunk id.
 */
import styles from './CitationCard.module.css';
import { citationFooter, locatorLabel } from './messages';
import { useCitationHover } from './useCitationHover';

export function CitationCard() {
  const { cite, cardId } = useCitationHover();
  if (cite === null) return null;

  const locator = locatorLabel(cite.segment);

  return (
    <div
      id={cardId}
      role="tooltip"
      className={`${styles.card} animate-fade-up`}
      // The one sanctioned inline style: computed geometry, not styling. The clamp lives in
      // `CitationHoverProvider.open`, beside the rect it clamps.
      style={{ left: `${cite.x}px`, top: `${cite.y}px` }}
    >
      <div className={styles.head}>
        <span className={styles.mark} aria-hidden="true" />
        <span className={`${styles.doc} mono`}>{cite.segment.doc}</span>
        {/* Omitted rather than emptied when the backend published no label — only PDF has
            pages, and R-34 synthesises nothing for the other formats. */}
        {locator !== null && <span className={`${styles.locator} mono`}>{locator}</span>}
      </div>
      <div className={styles.quote}>{`“${cite.segment.quote}”`}</div>
      <div className={styles.footer}>{citationFooter(cite.segment)}</div>
    </div>
  );
}
