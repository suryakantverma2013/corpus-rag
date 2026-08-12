/**
 * The §4.3 derived-state rules, tested without rendering — `sidebar/conversations.test.ts`'s shape.
 */
import { describe, expect, it } from 'vitest';
import type { CitationSegment, DegradedMessage, Message, Segment } from '../api';
import {
  announcementFor,
  citationFooter,
  citationsOf,
  evalBand,
  evalChips,
  isCitation,
  isDegraded,
  locatorLabel,
  plainText,
  sourceLine,
} from './messages';

function cite(doc: string, extra: Partial<CitationSegment> = {}): CitationSegment {
  return { isCite: true, doc, quote: 'q', chunkId: 'c', ...extra };
}

function answer(segs: Segment[], extra: Partial<Message> = {}): Message {
  return {
    id: 'm1',
    role: 'ai',
    segs,
    created_at: '2026-07-16T09:12:00Z',
    ...extra,
  };
}

describe('segment narrowing (FR-MSG-06)', () => {
  it('narrows on the PRESENCE of isCite, not on its value', () => {
    // The union is not tagged: a text run omits the key entirely rather than setting it false,
    // so `seg.isCite === false` is not merely wrong, it does not typecheck.
    expect(isCitation({ text: 'hello' })).toBe(false);
    expect(isCitation(cite('a.pdf'))).toBe(true);
  });

  it('drops citation segments from plain text', () => {
    // `doc` is a chip and `quote` is document text bound for the FR-CIT-03 card — neither is prose.
    expect(plainText([{ text: 'see ' }, cite('a.pdf'), { text: ' for detail' }])).toBe(
      'see  for detail',
    );
  });

  it('collects citations in order', () => {
    expect(citationsOf([{ text: 'a' }, cite('a.pdf'), cite('b.pdf')]).map((c) => c.doc)).toEqual([
      'a.pdf',
      'b.pdf',
    ]);
  });
});

describe('isDegraded (R-54(3))', () => {
  const degraded: DegradedMessage = { id: null, role: 'ai', segs: [{ text: 'System Failure.' }] };
  const regenerateFailure: DegradedMessage = { id: 'm1', role: 'ai', segs: [{ text: 'oops' }] };

  it('narrows on created_at rather than on a null id', () => {
    // The id test is the tempting one and it is wrong for exactly the case that matters: a
    // regenerate failure carries the TARGET's id so the client can say which bubble it belongs
    // to, so `id === null` would classify it as a stored message and offer to rate it.
    expect(isDegraded(degraded)).toBe(true);
    expect(isDegraded(regenerateFailure)).toBe(true);
    expect(isDegraded(answer([{ text: 'hi' }]))).toBe(false);
  });

  it('treats an abstained turn as stored, because it is', () => {
    // `_should_persist` excludes only `error`, so abstain and injection-blocked turns get a real
    // row — they are rateable and regenerable, and must not render through the degraded path.
    expect(isDegraded(answer([{ text: "I couldn't ground an answer to that." }]))).toBe(false);
  });
});

describe('sourceLine (FR-MSG-04)', () => {
  it('is null with no citations, so the element is omitted rather than emptied', () => {
    expect(sourceLine([])).toBeNull();
  });

  it('uses the singular at exactly one passage', () => {
    expect(sourceLine([cite('Q3.pdf')])).toBe('grounded in 1 passage · Q3.pdf');
  });

  it('uses the plural above one', () => {
    expect(sourceLine([cite('Q3.pdf'), cite('Onboarding.pdf')])).toBe(
      'grounded in 2 passages · Q3.pdf · Onboarding.pdf',
    );
  });

  it('counts passages but lists DISTINCT documents', () => {
    // Two citations into one file: the count is 2 (passages) and the list is one name. The
    // requirement words the two halves differently on purpose.
    expect(sourceLine([cite('Q3.pdf'), cite('Q3.pdf')])).toBe('grounded in 2 passages · Q3.pdf');
  });

  it('de-duplicates preserving first-seen order', () => {
    expect(sourceLine([cite('b.pdf'), cite('a.pdf'), cite('b.pdf')])).toBe(
      'grounded in 3 passages · b.pdf · a.pdf',
    );
  });
});

describe('locatorLabel (FR-CIT-04, R-34)', () => {
  it('renders the published label for each of the three locator kinds', () => {
    expect(
      locatorLabel(cite('a.pdf', { locator: { kind: 'page', page: 14, label: 'p. 14' } })),
    ).toBe('p. 14');
    expect(
      locatorLabel(
        cite('a.docx', {
          locator: {
            kind: 'section',
            section_path: ['Setup', 'Install'],
            label: '§ Setup › Install',
          },
        }),
      ),
    ).toBe('§ Setup › Install');
    expect(
      locatorLabel(
        cite('a.csv', {
          locator: { kind: 'rows', row_start: 218, row_end: 340, label: 'rows 218–340' },
        }),
      ),
    ).toBe('rows 218–340');
  });

  it('never reassembles a label from the structured fields', () => {
    // Only PDF has pages. A client that rebuilt "p. N" from locator.page would render nothing
    // here — which is the whole reason the backend publishes a label.
    expect(
      locatorLabel(cite('a.docx', { locator: { kind: 'section', section_index: 3 } })),
    ).toBeNull();
  });

  it('falls back to page, then to null', () => {
    expect(locatorLabel(cite('a.pdf', { page: 'p. 7' }))).toBe('p. 7');
    expect(locatorLabel(cite('a.pdf'))).toBeNull();
    expect(locatorLabel(cite('a.pdf', { page: '' }))).toBeNull();
  });
});

describe('citationFooter (FR-CIT-04, R-47(2))', () => {
  it('renders the score to two decimals when present', () => {
    expect(citationFooter(cite('a.pdf', { score: 0.9 }))).toBe(
      'Source passage · retrieval score 0.90',
    );
  });

  it('drops the clause and substitutes NOTHING when the key is absent', () => {
    // R-47(2): the reranker fails open and the RRF order it falls back to is deliberately
    // unpublished, so there is no truthful number to show — and "never substitute one" is the
    // requirement's own words.
    expect(citationFooter(cite('a.pdf'))).toBe('Source passage');
  });

  it('treats an explicit null the same as an absent key', () => {
    expect(citationFooter(cite('a.pdf', { score: null }))).toBe('Source passage');
  });

  it('renders a zero score rather than treating it as absent', () => {
    // 0 is falsy and is a real score; a truthiness check here would silently hide the worst one.
    expect(citationFooter(cite('a.pdf', { score: 0 }))).toBe(
      'Source passage · retrieval score 0.00',
    );
  });
});

describe('evalBand (FR-EVL-03)', () => {
  it.each([
    [1, 'good'],
    [0.9, 'good'],
    [0.899, 'warn'],
    [0.8, 'warn'],
    [0.799, 'bad'],
    [0, 'bad'],
  ])('bands %s as %s', (score, band) => {
    expect(evalBand(score)).toBe(band);
  });
});

describe('evalChips (FR-EVL-02, R-50(1))', () => {
  it('renders both metrics in order, to two decimals', () => {
    expect(evalChips({ relevancy: 0.94, faithfulness: 0.97 })).toEqual([
      { key: 'relevancy', label: 'Relevancy', score: '0.94', band: 'good' },
      { key: 'faithfulness', label: 'Faithfulness', score: '0.97', band: 'good' },
    ]);
  });

  it('never pads an absent metric with a placeholder', () => {
    // The job guards its two metrics independently and fails open, so a partial result is
    // correct rather than broken — and FR-EVL-02 says the row is never padded.
    expect(evalChips({ relevancy: 0.86, faithfulness: null }).map((c) => c.label)).toEqual([
      'Relevancy',
    ]);
    expect(evalChips({ faithfulness: 0.5 }).map((c) => c.label)).toEqual(['Faithfulness']);
  });

  it('renders nothing until the job lands, and nothing if it never does', () => {
    expect(evalChips(null)).toEqual([]);
    expect(evalChips(undefined)).toEqual([]);
    expect(evalChips({})).toEqual([]);
  });

  it('never emits a third metric', () => {
    // Ctx Precision and Ctx Recall are reference-based and cannot run on a live turn; the
    // prototype's four-metric sample data is exactly what the spec overrode.
    const chips = evalChips({ relevancy: 1, faithfulness: 1 } as never);
    expect(chips).toHaveLength(2);
  });
});

describe('announcementFor (NFR-A11Y-05)', () => {
  it('says nothing for the user’s own message', () => {
    // The user typed it; announcing it back is noise, and the requirement scopes announcements
    // to "state that changes without user action".
    expect(announcementFor({ message: { ...answer([{ text: 'hi' }]), role: 'user' } })).toBeNull();
  });

  it('announces a fixed phrase for an answer rather than reading it aloud', () => {
    expect(announcementFor({ message: answer([{ text: 'a long answer' }]) })).toBe(
      'Answer received.',
    );
  });

  it('announces a degraded turn’s server copy VERBATIM', () => {
    // R-43(6) keeps the FR-ERR-04 per-class copy server-side so the client holds no second
    // copy — which applies to what is spoken as much as to what is painted.
    expect(
      announcementFor({
        message: { id: null, role: 'ai', segs: [{ text: 'System Failure: Please try again.' }] },
        outcome: 'error',
      }),
    ).toBe('System Failure: Please try again.');
  });
});
