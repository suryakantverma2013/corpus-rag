/**
 * The FR-CIT-03 hover card's shared state — context object only.
 *
 * Its own module with no component in it, for `src/theme/ThemeContext.ts`'s reason: a module
 * exporting both a component and a hook trips `react/only-export-components`, which is why that
 * directory has three files and no barrel. This one follows it exactly.
 */
import { createContext } from 'react';
import type { CitationSegment } from '../api';

/** FR-CST-01's `hoverCite` — `{doc, page, quote, x, y} | null`, with the whole segment kept
 *  rather than four copied fields, and the chip's id so a stale close can be ignored. */
export interface HoverCite {
  segment: CitationSegment;
  x: number;
  y: number;
  chipId: string;
}

export interface CitationHover {
  cite: HoverCite | null;
  /** The DOM id the open chip points `aria-describedby` at, so the quote is announced. */
  cardId: string;
  open: (chip: HTMLElement, segment: CitationSegment, chipId: string) => void;
  /**
   * Close, but **only if `chipId` is the one currently open**.
   *
   * Load-bearing rather than defensive: with two adjacent chips the browser fires A's
   * `mouseleave` *after* B's `mouseenter`, so an unconditional close would blank the card the
   * user just opened. The same ordering applies to `blur` then `focus` when tabbing between them.
   */
  close: (chipId: string) => void;
  /** Unconditional close, for the paths that are not about a particular chip: Escape, a scroll
   *  or resize that invalidates the captured rect, and switching conversation. */
  dismiss: () => void;
}

export const CitationHoverContext = createContext<CitationHover | null>(null);
