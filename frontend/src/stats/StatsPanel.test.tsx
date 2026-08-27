/**
 * §4.6 behaviour: FR-ANL-01..05, FR-EVL-03/04, and the NFR-A11Y-03/05/06 obligations the board
 * attaches to this task.
 *
 * Conventions inherited from the twelve test files before this one: `fireEvent` over user-event,
 * and **plain vitest matchers** — jest-dom is not installed, so attributes come from
 * `getAttribute` and elements from `.tagName`.
 *
 * The arithmetic lives in `stats.test.ts`; this file is about what reaches the DOM. Where the two
 * overlap it is on purpose — a correct average rendered into the wrong element is still a defect.
 */
import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { StatsPanel } from './StatsPanel';
import type { StatsPanelProps } from './StatsPanel';
import type { CitationSegment, Evaluation, Message, Segment } from '../api';
import type { TranscriptEntry } from '../chat/messages';

const START = Date.UTC(2026, 7, 12, 10, 0, 0);
const ROOMY = { used: 240, limit: 10_400 };

function cite(doc: string): CitationSegment {
  return { isCite: true, doc, quote: 'q', chunkId: `${doc}:1` };
}

function ai(segs: Segment[], extra: Partial<Message> = {}): TranscriptEntry {
  return { message: { id: 'a', role: 'ai', segs, created_at: '2026-07-16T09:12:00Z', ...extra } };
}

function user(text: string): TranscriptEntry {
  return {
    message: { id: 'u', role: 'user', segs: [{ text }], created_at: '2026-07-16T09:00:00Z' },
  };
}

function scored(evaluation: Evaluation): TranscriptEntry {
  return ai([{ text: 'answer' }], { evaluation });
}

function panel(props: Partial<StatsPanelProps> = {}): HTMLElement {
  const { container } = render(
    <StatsPanel
      sessionStartedAt={START}
      entries={[]}
      usage={ROOMY}
      modelName="gpt-4o"
      {...props}
    />,
  );
  return container;
}

/**
 * The panel item holding the `<h2>` that reads `label`.
 *
 * Resolved as **the container child that contains the heading**, not by walking up a fixed number
 * of parents and not by matching a hashed class substring. Two cards put their heading inside a
 * flex header row and three do not, so a parent walk gets one group or the other wrong; and
 * `[class*="_card_"]` is the T-506 trap, where a substring selector silently hands back another
 * component's element. This also asserts the fragment root in passing — if a wrapper `<div>`
 * appeared, every card would resolve to the same element.
 */
function card(container: HTMLElement, label: string): HTMLElement {
  const heading = screen.getByRole('heading', { name: label });
  const found = [...container.children].find((child) => child.contains(heading));
  if (found === undefined) throw new Error(`${label} is not inside a panel item`);
  return found as HTMLElement;
}

/** A DURATION / MESSAGES cell — its `<h3>`'s parent, since both share the FR-ANL-01 grid. */
function statCell(label: string): HTMLElement {
  const cell = screen.getByRole('heading', { name: label }).parentElement;
  if (cell === null) throw new Error(`no card around ${label}`);
  return cell;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('FR-ANL-01 — the SESSION cards', () => {
  it('labels the group and each card at the right heading level', () => {
    // T-502 owns the document's only <h1>; SESSION is a group over the two stat cards, and the
    // cards sit under it.
    panel();
    expect(screen.getByRole('heading', { name: 'SESSION' }).tagName).toBe('H2');
    expect(screen.getByRole('heading', { name: 'DURATION' }).tagName).toBe('H3');
    expect(screen.getByRole('heading', { name: 'MESSAGES' }).tagName).toBe('H3');
  });

  it('counts every row in the active chat', () => {
    panel({ entries: [user('q'), ai([{ text: 'a' }])] });
    expect(within(statCell('MESSAGES')).getByText('2')).not.toBeNull();
  });

  it('renders both values in the mono face', () => {
    // §9: JetBrains Mono carries durations and counts. The class is global, in tokens.css.
    panel({ entries: [user('q')] });
    for (const label of ['DURATION', 'MESSAGES']) {
      const value = within(statCell(label)).getByText(/\d/);
      expect(value.className).toContain('mono');
    }
  });

  it('ticks once a second', () => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    panel();
    expect(within(statCell('DURATION')).getByText('00:00')).not.toBeNull();

    // 999ms must NOT advance it — a test that only checks 1000ms passes against any interval
    // shorter than one second.
    act(() => void vi.advanceTimersByTime(999));
    expect(within(statCell('DURATION')).getByText('00:00')).not.toBeNull();

    act(() => void vi.advanceTimersByTime(1));
    expect(within(statCell('DURATION')).getByText('00:01')).not.toBeNull();

    act(() => void vi.advanceTimersByTime(64_000));
    expect(within(statCell('DURATION')).getByText('01:05')).not.toBeNull();
  });

  it('clears its interval on unmount', () => {
    vi.useFakeTimers();
    vi.setSystemTime(START);
    panel();
    expect(vi.getTimerCount()).toBe(1);
    cleanup();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('FR-ANL-02 — the MODEL card', () => {
  it('renders the model id in mono and the caption verbatim', () => {
    const container = panel({ modelName: 'gpt-4o' });
    const model = card(container, 'MODEL');
    expect(within(model).getByText('gpt-4o').className).toContain('mono');
    expect(within(model).getByText('Context synthesis · grounding on')).not.toBeNull();
  });
});

describe('FR-ANL-03 — the CONTEXT WINDOW card', () => {
  it('renders the label, the caption and a fill sized to the percentage', () => {
    const container = panel({ usage: ROOMY });
    const meter = card(container, 'CONTEXT WINDOW');
    expect(within(meter).getByText('0.2K / 10.4K')).not.toBeNull();
    expect(within(meter).getByText('2% used · 10K tokens remaining')).not.toBeNull();
    expect(fill(container).style.width).toBe('2%');
  });

  it('stands full at the limit rather than overflowing its track', () => {
    const container = panel({ usage: { used: 11_000, limit: 10_400 } });
    expect(fill(container).style.width).toBe('100%');
  });

  it('hides the bar from assistive technology', () => {
    // It is a redundant encoding of two strings already rendered beside it; a `progressbar` role
    // would announce the same percentage a third time.
    const container = panel();
    expect(fill(container).parentElement?.getAttribute('aria-hidden')).toBe('true');
  });

  it('puts nothing but the width in the inline style', () => {
    // Inline styles are for computed geometry only — a colour or a duration here would escape
    // both the CSS guard and NFR-A11Y-01's global motion zeroing.
    const container = panel();
    expect(fill(container).getAttribute('style')).toBe('width: 2%;');
  });

  /** The only element in the meter carrying an inline width. */
  function fill(container: HTMLElement): HTMLElement {
    const found = card(container, 'CONTEXT WINDOW').querySelector<HTMLElement>('[style*="width"]');
    if (found === null) throw new Error('no meter fill');
    return found;
  }
});

describe('FR-ANL-04 / FR-EVL-04 — the DEEPEVAL card', () => {
  const EVALUATED = [scored({ relevancy: 0.94, faithfulness: 0.97 })];

  it('shows the empty state, an em dash and NO rows before anything is evaluated', () => {
    const container = panel({ entries: [user('q'), ai([{ text: 'a' }])] });
    const evals = card(container, 'DEEPEVAL · SESSION AVG');
    expect(within(evals).getByText('Scores appear once a response is evaluated.')).not.toBeNull();
    expect(within(evals).getByText('—')).not.toBeNull();
    expect(within(evals).queryByText('Relevancy')).toBeNull();
  });

  it('hides the em dash from assistive technology, since the sentence says it in words', () => {
    const container = panel();
    const dash = within(card(container, 'DEEPEVAL · SESSION AVG')).getByText('—');
    expect(dash.getAttribute('aria-hidden')).toBe('true');
  });

  it('renders four rows in FR-ANL-04’s order once anything is evaluated', () => {
    const container = panel({ entries: EVALUATED });
    const evals = card(container, 'DEEPEVAL · SESSION AVG');
    expect(within(evals).queryByText('Scores appear once a response is evaluated.')).toBeNull();
    // getAllByText returns document order, so this pins the sequence as well as the set.
    const labels = within(evals).getAllByText(
      /^(Relevancy|Faithfulness|Ctx Precision|Ctx Recall)$/,
    );
    expect(labels.map((el) => el.textContent)).toEqual([
      'Relevancy',
      'Faithfulness',
      'Ctx Precision',
      'Ctx Recall',
    ]);
  });

  it('renders the overall average in the header', () => {
    const container = panel({ entries: EVALUATED });
    expect(
      within(card(container, 'DEEPEVAL · SESSION AVG')).getByText('0.95 overall'),
    ).not.toBeNull();
  });

  it('em-dashes the two reference-based metrics and gives them NO bar', () => {
    // R-52(1) makes them permanent. A zero-width fill would have to carry a band, and
    // `evalBand(0)` is `bad` — a red claim about a metric that was never scored.
    const container = panel({ entries: EVALUATED });
    expect(container.querySelectorAll('[data-band]')).toHaveLength(2);
    const row = rowFor('Ctx Precision');
    expect(within(row).getByText('—')).not.toBeNull();
    // The TRACK MUST BE EMPTY, not merely bandless. Rendering a fill with `band === null` omits
    // the `data-band` attribute too, so a `[data-band]` query alone cannot tell the two apart —
    // it would pass against a `class="metricFill undefined"` element with no width.
    const track = row.lastElementChild;
    expect(track?.children).toHaveLength(0);
    // …and the scored rows do have exactly one.
    expect(rowFor('Relevancy').lastElementChild?.children).toHaveLength(1);
  });

  it('speaks the em-dashed rows instead of leaving them silent', () => {
    panel({ entries: EVALUATED });
    expect(within(rowFor('Ctx Recall')).getByText('not scored')).not.toBeNull();
  });

  it('bands and sizes each bar from the score it renders (FR-EVL-03)', () => {
    panel({ entries: EVALUATED });
    const relevancy = rowFor('Relevancy');
    expect(within(relevancy).getByText('0.94')).not.toBeNull();
    const bar = relevancy.querySelector<HTMLElement>('[data-band]');
    expect(bar?.getAttribute('data-band')).toBe('good');
    expect(bar?.style.width).toBe('94%');
  });

  it('qualifies the numerals as indicative for a reader who cannot hover (R-70)', () => {
    const container = panel({ entries: EVALUATED });
    const evals = card(container, 'DEEPEVAL · SESSION AVG');
    expect(evals.getAttribute('title')).toBe(
      'DeepEval metric — indicative judge score, not an exact measurement',
    );
    expect(
      within(evals).getByText('Indicative judge scores, not exact measurements.'),
    ).not.toBeNull();
  });

  function rowFor(label: string): HTMLElement {
    const row = screen.getByText(label).closest('div')?.parentElement;
    if (!row) throw new Error(`no metric row for ${label}`);
    return row;
  }
});

describe('FR-ANL-05 — SOURCES REFERENCED', () => {
  const CITED = [
    ai([cite('Q3_Market_Report.pdf'), cite('Onboarding_Playbook.pdf')]),
    ai([cite('Q3_Market_Report.pdf')]),
  ];

  it('shows the empty state verbatim when nothing has been cited', () => {
    panel({ entries: [ai([{ text: 'a' }])] });
    expect(screen.getByText('None yet — answers will list their sources here.')).not.toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('lists one item per distinct document, in first-cited order', () => {
    panel({ entries: CITED });
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText('Q3_Market_Report.pdf')).not.toBeNull();
    expect(within(items[1]).getByText('Onboarding_Playbook.pdf')).not.toBeNull();
  });

  it('counts passages and pluralises them', () => {
    // FR-ANL-05 writes `{N} passages`; FR-MSG-04(2) is explicit that one citation is singular,
    // and a document cited once is the common case, not a corner.
    panel({ entries: CITED });
    const items = screen.getAllByRole('listitem');
    expect(within(items[0]).getByText('2 passages')).not.toBeNull();
    expect(within(items[1]).getByText('1 passage')).not.toBeNull();
  });

  it('names the list from its heading and hides the badge that repeats the extension', () => {
    panel({ entries: CITED });
    const list = screen.getByRole('list');
    const labelId = list.getAttribute('aria-labelledby');
    expect(document.getElementById(labelId ?? '')?.textContent).toBe('SOURCES REFERENCED');
    const badge = within(screen.getAllByRole('listitem')[0]).getByText('PDF');
    expect(badge.getAttribute('aria-hidden')).toBe('true');
  });

  it('is a NAMED tab stop, because R-92(3) made it a scroll container (T-720)', () => {
    panel({ entries: CITED });
    const list = screen.getByRole('list');

    // The pair, asserted together on purpose. `tabIndex` alone satisfies axe's
    // `scrollable-region-focusable` (WCAG 2.1.1/2.1.3) but would create a tab stop with no
    // accessible name, which is a different defect; the name alone leaves the region
    // unreachable by keyboard, which is the one this fixes. Neither half is sufficient.
    expect(list.getAttribute('tabindex')).toBe('0');
    expect(list.getAttribute('aria-labelledby')).not.toBeNull();
  });

  it('does not make the rows themselves tab stops', () => {
    // §8.56(2) and R-74(8): one stop per source is the traversal trap, and the rows are not
    // interactive. The container scrolls; the rows are read.
    panel({ entries: CITED });
    for (const row of screen.getAllByRole('listitem')) {
      expect(row.getAttribute('tabindex')).toBeNull();
    }
  });
});

describe('NFR-A11Y-03 — the panel’s outline and its tab order', () => {
  it('owns no <h1> and puts its five sections at h2', () => {
    panel({ entries: [scored({ relevancy: 0.9, faithfulness: 0.9 })] });
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'SESSION',
      'MODEL',
      'CONTEXT WINDOW',
      'DEEPEVAL · SESSION AVG',
      'SOURCES REFERENCED',
    ]);
  });

  it('adds no tab stop', () => {
    // The panel is pure output. A focusable element here would land between the conversation
    // list and the composer for no reason.
    const container = panel({ entries: [scored({ relevancy: 0.9, faithfulness: 0.9 })] });
    expect(
      container.querySelectorAll('a, button, input, select, textarea, [tabindex]'),
    ).toHaveLength(0);
  });
});

describe('NFR-A11Y-05 / NFR-A11Y-06 — announcement and colour', () => {
  it('declares no live region', () => {
    // The duration ticks every second; a polite region anywhere in this subtree would read the
    // clock aloud forever. MessageList already announces the arrival these numbers follow from.
    const container = panel({ entries: [scored({ relevancy: 0.9, faithfulness: 0.9 })] });
    expect(container.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).toHaveLength(
      0,
    );
  });

  it('never lets the band hue be the only carrier of the score', () => {
    const container = panel({
      entries: [scored({ relevancy: 0.94, faithfulness: 0.75 })],
    });
    const bars = container.querySelectorAll<HTMLElement>('[data-band]');
    expect(bars).toHaveLength(2);
    for (const bar of bars) {
      // The numeral lives in the row's header, one level above the track holding the fill.
      const row = bar.parentElement?.parentElement;
      expect(row?.textContent).toMatch(/\d\.\d\d/);
    }
  });
});
