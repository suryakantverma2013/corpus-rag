/**
 * The §4.6 derived-state rules, tested without rendering — `chat/messages.test.ts`'s shape.
 *
 * Several of these pin corrections to the prototype's own arithmetic (`RAG Chatbot.dc.html`
 * lines 380–392), so the comments say which number the prototype would have produced.
 */
import { describe, expect, it } from 'vitest';
import type { CitationSegment, Evaluation, Message, Segment } from '../api';
import type { TranscriptEntry } from '../chat/messages';
import {
  contextCaption,
  documentTypeBadge,
  evalAverages,
  formatDuration,
  formatScore,
  messageCount,
  modelNameOf,
  overallLabel,
  percentUsed,
  sourcesReferenced,
  tokensLabel,
} from './stats';

function cite(doc: string): CitationSegment {
  return { isCite: true, doc, quote: 'q', chunkId: `${doc}:1` };
}

function ai(segs: Segment[], extra: Partial<Message> = {}): TranscriptEntry {
  return {
    message: {
      id: 'a',
      role: 'ai',
      segs,
      created_at: '2026-07-16T09:12:00Z',
      ungrounded: false,
      ungrounded_offerable: false,
      ...extra,
    },
  };
}

function scored(evaluation: Evaluation): TranscriptEntry {
  return ai([{ text: 'answer' }], { evaluation });
}

function user(text: string): TranscriptEntry {
  return {
    message: {
      id: 'u',
      role: 'user',
      segs: [{ text }],
      created_at: '2026-07-16T09:00:00Z',
      ungrounded: false,
      ungrounded_offerable: false,
    },
  };
}

/** The R-54(3) served-but-unstored turn: no `created_at`, no metadata of any kind. */
const DEGRADED: TranscriptEntry = {
  message: { id: null, role: 'ai', segs: [{ text: 'Please try again shortly.' }] },
  outcome: 'error',
};

describe('FR-ANL-01 — formatDuration', () => {
  it('zero-pads both fields', () => {
    expect(formatDuration(0)).toBe('00:00');
    expect(formatDuration(9)).toBe('00:09');
    expect(formatDuration(59)).toBe('00:59');
  });

  it('rolls minutes at 60 seconds, and puts them on the LEFT', () => {
    // 65 rather than 60: `pad(m):pad(s)` and `pad(s):pad(m)` both render 60 as `01:00`.
    expect(formatDuration(60)).toBe('01:00');
    expect(formatDuration(65)).toBe('01:05');
    expect(formatDuration(3599)).toBe('59:59');
  });

  it('does not roll into hours', () => {
    // The prototype's behaviour, and FR-ANL-01 says `mm:ss`. `padStart` leaves longer strings be.
    expect(formatDuration(3600)).toBe('60:00');
    expect(formatDuration(6000)).toBe('100:00');
  });

  it('floors fractions and clamps a negative elapsed time', () => {
    expect(formatDuration(1.9)).toBe('00:01');
    expect(formatDuration(-5)).toBe('00:00');
  });
});

describe('FR-ANL-01 — messageCount', () => {
  it('counts every row in the transcript, including a degraded turn', () => {
    // The degraded turn is served but never stored (R-54(3)), so it will not be in `messages` —
    // this card counts what the user is looking at, which is the requirement's own wording.
    expect(messageCount([user('q'), ai([{ text: 'a' }]), DEGRADED])).toBe(3);
    expect(messageCount([])).toBe(0);
  });
});

describe('FR-ANL-02 — modelNameOf', () => {
  it('takes the NEWEST answer’s model, not the first', () => {
    const entries = [
      ai([{ text: 'a' }], { model_name: 'gpt-4o-mini' }),
      ai([{ text: 'b' }], { model_name: 'gpt-4o' }),
    ];
    expect(modelNameOf(entries)).toBe('gpt-4o');
  });

  it('skips user turns, degraded turns and rows with no model', () => {
    expect(modelNameOf([ai([{ text: 'a' }], { model_name: 'gpt-4o' }), DEGRADED, user('q')])).toBe(
      'gpt-4o',
    );
    expect(modelNameOf([ai([{ text: 'a' }])])).toBeNull();
    expect(modelNameOf([ai([{ text: 'a' }], { model_name: '' })])).toBeNull();
    expect(modelNameOf([])).toBeNull();
  });
});

describe('FR-ANL-03 — the context meter', () => {
  const ROOMY = { used: 240, limit: 10_400 };
  const FULL = { used: 8_900, limit: 10_400 };

  it('formats the label to one decimal on both sides', () => {
    expect(tokensLabel(ROOMY)).toBe('0.2K / 10.4K');
    expect(tokensLabel(FULL)).toBe('8.9K / 10.4K');
  });

  it('rounds the percentage to a whole number', () => {
    // The wire's `percent_used` carries one decimal (`budget.py`); FR-ANL-03 renders `{pct}%`.
    expect(percentUsed(ROOMY)).toBe(2);
    expect(percentUsed(FULL)).toBe(86);
    expect(percentUsed({ used: 0, limit: 10_400 })).toBe(0);
  });

  it('writes the caption FR-ANL-03 specifies', () => {
    expect(contextCaption(ROOMY)).toBe('2% used · 10K tokens remaining');
    expect(contextCaption(FULL)).toBe('86% used · 2K tokens remaining');
  });

  it('clamps at the limit rather than reporting a debt', () => {
    expect(percentUsed({ used: 11_000, limit: 10_400 })).toBe(100);
    expect(contextCaption({ used: 11_000, limit: 10_400 })).toBe('100% used · 0K tokens remaining');
    // The LABEL is deliberately not clamped: the bar cannot exceed its track, but the token
    // count must stay honest.
    expect(tokensLabel({ used: 11_000, limit: 10_400 })).toBe('11.0K / 10.4K');
  });

  it('answers 100 for a zero limit instead of NaN', () => {
    // `width: "NaN%"` is invalid, the browser drops the declaration, and the fill falls back to
    // `width: auto` — a silently full bar that no jsdom test could see. 100 also matches
    // `ContextUsage.percent_used` in `app/rag/budget.py`.
    expect(percentUsed({ used: 0, limit: 0 })).toBe(100);
    expect(contextCaption({ used: 0, limit: 0 })).toBe('100% used · 0K tokens remaining');
  });
});

describe('FR-ANL-04 / FR-EVL-04 — session averages', () => {
  it('always reports four rows in FR-ANL-04’s order', () => {
    const { rows } = evalAverages([scored({ relevancy: 0.94, faithfulness: 0.97 })]);
    expect(rows.map((r) => r.label)).toEqual([
      'Relevancy',
      'Faithfulness',
      'Ctx Precision',
      'Ctx Recall',
    ]);
  });

  it('leaves Ctx Precision and Ctx Recall unscored even when the other two are scored', () => {
    // R-52(1): reference-based, permanently em-dashed. `EvaluationResponse` has no field for
    // them, so this can never become data-driven.
    const { rows } = evalAverages([scored({ relevancy: 0.94, faithfulness: 0.97 })]);
    expect(rows[2].score).toBeNull();
    expect(rows[2].band).toBeNull();
    expect(rows[2].width).toBeNull();
    expect(rows[3].score).toBeNull();
  });

  it('averages a metric across the evaluated answers', () => {
    const { rows } = evalAverages([
      scored({ relevancy: 0.9, faithfulness: 1 }),
      scored({ relevancy: 0.8, faithfulness: 0.9 }),
    ]);
    expect(rows[0].score).toBe('0.85');
    expect(rows[1].score).toBe('0.95');
  });

  it('skips a metric that failed while its sibling succeeded', () => {
    // R-50(3) guards the two independently, so this is a correct result rather than an error.
    const { rows } = evalAverages([scored({ relevancy: 0.86, faithfulness: null })]);
    expect(rows[0].score).toBe('0.86');
    expect(rows[1].score).toBeNull();
  });

  it('takes the overall as the mean of the metrics PRESENT, not of four', () => {
    // The denominator is the point: the prototype divides by `evalAgg.length`, which is always 4,
    // so on the seeded chat it would answer 0.48 against real scores of 0.94 and 0.97.
    const { overall } = evalAverages([scored({ relevancy: 0.94, faithfulness: 0.97 })]);
    expect(overall).toBeCloseTo(0.955, 6);
    // Rendered 0.95, not 0.96: 0.955 is not representable and the nearest double sits just below
    // the half-way point, so `toFixed` rounds down. Left alone — R-70 rules these numerals
    // indicative, and chasing the second decimal is the precision the ruling declines to claim.
    expect(overallLabel(overall)).toBe('0.95 overall');
  });

  it('takes the overall from the raw averages, not from their printed forms', () => {
    // Relevancy averages 0.895 and faithfulness 0.905, so the true overall is exactly 0.90.
    // Averaging the `toFixed(2)` strings — the prototype's `parseFloat(m.score)` — would work
    // from 0.90 and 0.91 (0.895 rounds up here, 0.905 down) and answer 0.91.
    const { overall } = evalAverages([
      scored({ relevancy: 0.89, faithfulness: 0.9 }),
      scored({ relevancy: 0.9, faithfulness: 0.91 }),
    ]);
    expect(overall).toBeCloseTo(0.9, 6);
    expect(overallLabel(overall)).toBe('0.90 overall');
  });

  it('takes the overall from ONE present metric when only one has values', () => {
    const { overall } = evalAverages([scored({ relevancy: 0.86, faithfulness: null })]);
    expect(overallLabel(overall)).toBe('0.86 overall');
  });

  it('bands and sizes the bar from the ROUNDED score, so the three always agree', () => {
    // Raw mean 0.8951: banding the raw value paints an amber bar beside a numeral reading 0.90,
    // and sizes it at 89% while the number says 90.
    const { rows } = evalAverages([
      scored({ relevancy: 0.89, faithfulness: null }),
      scored({ relevancy: 0.9002, faithfulness: null }),
    ]);
    expect(rows[0].score).toBe('0.90');
    expect(rows[0].band).toBe('good');
    expect(rows[0].width).toBe('90%');
  });

  it('reports nothing at all when no answer has been evaluated', () => {
    for (const entries of [[], [user('q'), ai([{ text: 'a' }])], [scored({})], [DEGRADED]]) {
      const { rows, overall } = evalAverages(entries);
      expect(overall).toBeNull();
      expect(overallLabel(overall)).toBe('—');
      // The invariant the card leans on: no overall ⇔ no row has a score.
      expect(rows.every((r) => r.score === null)).toBe(true);
    }
  });

  it('treats an evaluation whose every metric is null as unevaluated', () => {
    expect(evalAverages([scored({ relevancy: null, faithfulness: null })]).overall).toBeNull();
  });

  it('formats every score to two decimals (R-70)', () => {
    expect(formatScore(0.9)).toBe('0.90');
    expect(formatScore(1)).toBe('1.00');
  });
});

describe('FR-ANL-05 — sources referenced', () => {
  const chat: TranscriptEntry[] = [
    user('q'),
    ai([
      { text: 'a' },
      cite('Q3_Market_Report.pdf'),
      { text: 'b' },
      cite('Onboarding_Playbook.pdf'),
    ]),
    user('q2'),
    ai([{ text: 'c' }, cite('Q3_Market_Report.pdf')]),
  ];

  it('gives one row per distinct document, in first-cited order', () => {
    // Insertion order, not alphabetical — alphabetical would put Onboarding first, which is not
    // the order the user met them in.
    expect(sourcesReferenced(chat).map((s) => s.name)).toEqual([
      'Q3_Market_Report.pdf',
      'Onboarding_Playbook.pdf',
    ]);
  });

  it('counts passages, not messages', () => {
    // Q3 is cited once in each of two answers; the count is citation segments either way.
    expect(sourcesReferenced(chat).map((s) => s.passages)).toEqual([2, 1]);
  });

  it('counts two citations to one document within one answer as two passages', () => {
    expect(sourcesReferenced([ai([cite('a.pdf'), cite('a.pdf')])])[0].passages).toBe(2);
  });

  it('is empty for a chat with no citations', () => {
    expect(sourcesReferenced([user('q'), ai([{ text: 'a' }]), DEGRADED])).toEqual([]);
  });

  it('derives the type badge from the extension', () => {
    expect(documentTypeBadge('Q3_Market_Report.pdf')).toBe('PDF');
    expect(documentTypeBadge('Pricing_Strategy.docx')).toBe('DOC');
    expect(documentTypeBadge('Customer_Feedback.csv')).toBe('CSV');
    expect(documentTypeBadge('notes.md')).toBe('MD');
  });

  it('is case-insensitive and falls back for anything else', () => {
    expect(documentTypeBadge('REPORT.PDF')).toBe('PDF');
    expect(documentTypeBadge('README')).toBe('DOC');
    expect(documentTypeBadge('archive.zip')).toBe('DOC');
  });
});

describe('FR-EVL-04 — FR-MSG-09 answers are excluded (R-98(4))', () => {
  it('ignores an ungrounded answer even when it carries scores', () => {
    // The fixture is deliberately IMPOSSIBLE today: R-50 skips a message that cites nothing,
    // so the backend never writes an evaluation onto an ungrounded row and a realistic fixture
    // would be excluded by the `evaluation === null` line whether the FR-MSG-09 guard existed
    // or not - a test that passes for the wrong reason. Scoring it forces the guard to be the
    // only thing that can exclude it, so deleting the guard fails this and nothing else.
    const grounded = ai([{ text: 'grounded' }], {
      evaluation: { relevancy: 1, faithfulness: 1 },
    });
    const invented = ai([{ text: 'invented' }], {
      ungrounded: true,
      evaluation: { relevancy: 0, faithfulness: 0 },
    });

    const { rows, overall } = evalAverages([grounded, invented]);

    expect(rows[0].score).toBe('1.00');
    expect(rows[1].score).toBe('1.00');
    expect(overall).toBe(1);
  });

  it('shows the empty state when the only answer is ungrounded', () => {
    // A chat whose one answer came from training has nothing evaluated in it, so FR-ANL-04
    // reads its em dash rather than averaging a single invented row.
    const { rows, overall } = evalAverages([
      ai([{ text: 'invented' }], { ungrounded: true, evaluation: { relevancy: 0.9 } }),
    ]);

    expect(overall).toBeNull();
    expect(rows.every((row) => row.score === null)).toBe(true);
  });
});
