/**
 * Holds the FR-CIT-03 hover card's state, and nothing else.
 *
 * **Why a context rather than a prop.** `hoverCite` is enumerated FR-CST-01 client state, and the
 * card must render in `AppShell`'s `overlays` slot — that slot's own doc comment names the
 * FR-CIT-03 card, and it is the DOM position T-502's containing-block guard protects (a `fixed`
 * popover inside the message list's `overflow-y: auto` would be clipped, which is the defect
 * T-503 measured for the conversation menu). But a chip sits several levels inside a markdown
 * AST, so threading the state from `App` down to it is exactly the prop-drilling R-58(5)
 * refused. A context mounted *around* `AppShell` is an ancestor of both slots, keeps the
 * chip↔card coupling inside `src/chat/`, and leaves `App` placing two components and threading
 * nothing.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { CitationHoverContext } from './CitationHoverContext';
import type { HoverCite } from './CitationHoverContext';
import type { CitationSegment } from '../api';

/** FR-CIT-03's `viewportWidth − 350`: the 330px card plus a 20px margin. Duplicated in
 *  `CitationCard.module.css` as the width; the CSS guard asserts the two agree. */
export const CARD_WIDTH = 330;
export const CARD_CLAMP = 350;

export function CitationHoverProvider({ children }: { children?: ReactNode }) {
  const [cite, setCite] = useState<HoverCite | null>(null);
  const cardId = useId();
  /** Read by `close` without making it depend on `cite`, so the callbacks stay stable and a
   *  chip does not re-render every time a *different* chip opens. */
  const openChip = useRef<string | null>(null);

  /** A deferred open, so it can be cancelled by a close, another chip, or unmount. */
  const pending = useRef<number | null>(null);
  const cancelPending = useCallback(() => {
    if (pending.current !== null) {
      cancelAnimationFrame(pending.current);
      pending.current = null;
    }
  }, []);

  const open = useCallback(
    (chip: HTMLElement, segment: CitationSegment, chipId: string) => {
      openChip.current = chipId;
      cancelPending();
      /*
       * Deferred by one frame, and this is a fix rather than a flourish (B-002).
       *
       * `HTMLElement.focus()` scrolls an off-screen element into view, and that scroll reaches
       * the capture-phase `dismiss` registered below — so opening synchronously meant **the
       * focus that opens the card fires the scroll that closes it**. Measured on the
       * containerized stack, with a chip 2277px above a 746px viewport:
       * `["card+@7808", "scroll@7817", "card-@7821"]` — alive for 13ms. It reproduced 3/3 and
       * only when the chip was off screen, which is why it looked like a dark-theme flake: the
       * dark pass runs first, before anything has scrolled the chip into view.
       *
       * Waiting a frame also captures the rect *after* the scroll settles, so the card is
       * positioned where the chip ended up rather than where it started — a second defect the
       * first one was hiding. Nothing here weakens T-503's rule: a genuine scroll *after* the
       * card is open still dismisses it, because the listener arms with `cite`.
       *
       * One frame is enough because the scroll is instant: no `scroll-behavior: smooth` exists
       * anywhere in the stylesheet, and a smooth scroll would emit events over many frames.
       * If one is ever added, this becomes a race again — hence the guard test.
       */
      pending.current = requestAnimationFrame(() => {
        pending.current = null;
        // The chip may have been left in the meantime — a fast hover-out, or another chip
        // opening. Reviving the card here would be §8.58(7)'s resurrection defect: state that
        // means "this happened" must not be recreated from a stale closure.
        if (openChip.current !== chipId) return;
        const rect = chip.getBoundingClientRect();
        // Captured once, exactly as the prototype does. The card is `position: fixed` and the
        // transform lifts it above the chip; only the x clamp is specified, and there is
        // deliberately no lower clamp — below a 350px viewport `x` would go negative, which is
        // out of scope for a desktop GUI rather than something to invent a rule for.
        setCite({
          segment,
          chipId,
          x: Math.min(rect.left, window.innerWidth - CARD_CLAMP),
          y: rect.top,
        });
      });
    },
    [cancelPending],
  );

  const close = useCallback(
    (chipId: string) => {
      if (openChip.current !== chipId) return;
      openChip.current = null;
      cancelPending();
      setCite(null);
    },
    [cancelPending],
  );

  const dismiss = useCallback(() => {
    openChip.current = null;
    cancelPending();
    setCite(null);
  }, [cancelPending]);

  // A deferred open must not outlive the provider: the frame would fire after unmount and call
  // `setCite` on a dead component. Own effect, so it survives StrictMode's throwaway pass
  // (§8.59's rule — separate "has it started" from "is it still mounted").
  useEffect(() => cancelPending, [cancelPending]);

  // Only while the card is open, so the app carries no idle listeners.
  useEffect(() => {
    if (cite === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dismiss();
    };
    // Escape in the BUBBLE phase, deliberately: `Dialog` listens in capture and stops
    // propagation, so an open Regenerate confirmation takes Escape first — as it must, since
    // dismissing a modal is the more urgent of the two.
    document.addEventListener('keydown', onKeyDown);
    // `true` for the capture phase: scroll does not bubble out of the message list. The card
    // is positioned from a rect captured on open, so scrolling would leave it beside the wrong
    // chip; closing is the honest answer (T-503's argument for the conversation menu).
    document.addEventListener('scroll', dismiss, true);
    window.addEventListener('resize', dismiss);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('scroll', dismiss, true);
      window.removeEventListener('resize', dismiss);
    };
  }, [cite, dismiss]);

  const value = useMemo(
    () => ({ cite, cardId, open, close, dismiss }),
    [cite, cardId, open, close, dismiss],
  );
  return <CitationHoverContext value={value}>{children}</CitationHoverContext>;
}
