/**
 * FR-CIT-01..04 — the chip's open/close contract, the card's contents, and the viewport clamp.
 *
 * The card is `position: fixed` and jsdom lays nothing out, so what is asserted here is the
 * *arithmetic* and the DOM wiring. That the card is not clipped by the list's `overflow-y: auto`
 * can only be seen in a real browser (T-510/T-511).
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CitationCard } from './CitationCard';
import { CitationChip } from './CitationChip';
import { CitationHoverProvider } from './CitationHoverProvider';
import type { CitationSegment } from '../api';

function cite(doc: string, extra: Partial<CitationSegment> = {}): CitationSegment {
  return { isCite: true, doc, quote: 'the quoted passage', chunkId: `${doc}:1`, ...extra };
}

/** Two chips, so the adjacent-chip ordering case is always reachable. */
function scene(a: CitationSegment, b: CitationSegment = cite('Other.pdf')) {
  return render(
    <CitationHoverProvider>
      <CitationChip segment={a} />
      <CitationChip segment={b} />
      <CitationCard />
    </CitationHoverProvider>,
  );
}

/** jsdom returns an all-zero rect, so the geometry has to be stated to be tested. */
function placeAt(chip: HTMLElement, left: number, top: number) {
  chip.getBoundingClientRect = () =>
    ({ left, top, right: left, bottom: top, width: 0, height: 0, x: left, y: top }) as DOMRect;
}

const card = () => screen.queryByRole('tooltip');

beforeEach(() => {
  window.innerWidth = 1000;
});

describe('FR-CIT-02 — the mouse contract', () => {
  it('opens on mouse-enter and closes on mouse-leave', () => {
    scene(cite('Q3.pdf'));
    const chip = screen.getByRole('button', { name: 'Q3.pdf' });
    expect(card()).toBeNull();
    fireEvent.mouseEnter(chip);
    expect(card()).not.toBeNull();
    fireEvent.mouseLeave(chip);
    expect(card()).toBeNull();
  });
});

describe('NFR-A11Y-03/04 — the keyboard path the prototype has none of', () => {
  it('is a real <button>, not the prototype’s <span>', () => {
    scene(cite('Q3.pdf'));
    expect(screen.getByRole('button', { name: 'Q3.pdf' }).tagName).toBe('BUTTON');
  });

  it('opens on focus and closes on blur', () => {
    scene(cite('Q3.pdf'));
    const chip = screen.getByRole('button', { name: 'Q3.pdf' });
    fireEvent.focus(chip);
    expect(card()).not.toBeNull();
    fireEvent.blur(chip);
    expect(card()).toBeNull();
  });

  it('closes on Escape', () => {
    scene(cite('Q3.pdf'));
    fireEvent.focus(screen.getByRole('button', { name: 'Q3.pdf' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(card()).toBeNull();
  });

  it('describes the chip with the card only while it is open', () => {
    scene(cite('Q3.pdf'));
    const chip = screen.getByRole('button', { name: 'Q3.pdf' });
    expect(chip.getAttribute('aria-describedby')).toBeNull();
    fireEvent.focus(chip);
    expect(chip.getAttribute('aria-describedby')).toBe(card()?.getAttribute('id'));
    fireEvent.blur(chip);
    expect(chip.getAttribute('aria-describedby')).toBeNull();
  });
});

describe('the stale-close case, which is why chipId exists', () => {
  it('ignores chip A’s mouse-leave once chip B has opened', () => {
    // The browser fires A's `mouseleave` AFTER B's `mouseenter`, so an unconditional close would
    // blank the card the user has just opened. Same ordering for blur-then-focus when tabbing.
    scene(cite('A.pdf'), cite('B.pdf'));
    const a = screen.getByRole('button', { name: 'A.pdf' });
    const b = screen.getByRole('button', { name: 'B.pdf' });
    fireEvent.mouseEnter(a);
    fireEvent.mouseEnter(b);
    fireEvent.mouseLeave(a);
    expect(card()).not.toBeNull();
    expect(card()?.textContent).toContain('B.pdf');
  });
});

describe('FR-CIT-03 — placement', () => {
  it('anchors to the chip’s rect when there is room', () => {
    scene(cite('Q3.pdf'));
    const chip = screen.getByRole('button', { name: 'Q3.pdf' });
    placeAt(chip, 100, 240);
    fireEvent.mouseEnter(chip);
    expect(card()?.style.left).toBe('100px');
    expect(card()?.style.top).toBe('240px');
  });

  it('clamps x to viewportWidth − 350 near the right edge', () => {
    scene(cite('Q3.pdf'));
    const chip = screen.getByRole('button', { name: 'Q3.pdf' });
    placeAt(chip, 900, 240);
    fireEvent.mouseEnter(chip);
    expect(card()?.style.left).toBe('650px');
  });

  it('closes on scroll rather than following a stale rect', () => {
    // The position is captured once on open; the list scrolls under it, so the card would
    // otherwise float beside the wrong chip. Capture phase, since scroll does not bubble.
    scene(cite('Q3.pdf'));
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Q3.pdf' }));
    fireEvent.scroll(document);
    expect(card()).toBeNull();
  });

  it('closes on resize, for the same reason', () => {
    scene(cite('Q3.pdf'));
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Q3.pdf' }));
    fireEvent(window, new Event('resize'));
    expect(card()).toBeNull();
  });
});

describe('FR-CIT-03/04 — contents', () => {
  it('renders the filename, the locator label and the quoted passage', () => {
    scene(
      cite('Q3_Market_Report.pdf', {
        locator: { kind: 'page', page: 14, label: 'p. 14' },
        score: 0.9,
      }),
    );
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Q3_Market_Report.pdf' }));
    const text = card()?.textContent ?? '';
    expect(text).toContain('Q3_Market_Report.pdf');
    expect(text).toContain('p. 14');
    // Curly quotes, as the prototype writes them.
    expect(text).toContain('“the quoted passage”');
    expect(text).toContain('Source passage · retrieval score 0.90');
  });

  it('renders a non-page locator unchanged (R-34)', () => {
    scene(
      cite('Handbook.docx', {
        locator: {
          kind: 'section',
          section_path: ['Setup', 'Install'],
          label: '§ Setup › Install',
        },
      }),
    );
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Handbook.docx' }));
    expect(card()?.textContent).toContain('§ Setup › Install');
  });

  it('renders a citation with NO score, substituting nothing (R-47(2))', () => {
    // The reranker fails open and the RRF order it falls back to is deliberately unpublished,
    // so there is no truthful number — "never substitute one" is the requirement's own wording.
    scene(cite('Q3.pdf'));
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Q3.pdf' }));
    const text = card()?.textContent ?? '';
    expect(text).toContain('Source passage');
    expect(text).not.toContain('retrieval score');
    expect(text).not.toMatch(/\d\.\d\d/);
  });

  it('omits the locator entirely when the backend published none', () => {
    scene(cite('Notes.md'));
    fireEvent.mouseEnter(screen.getByRole('button', { name: 'Notes.md' }));
    expect(card()?.textContent).toBe('Notes.md“the quoted passage”Source passage');
  });
});
