/**
 * Seeded transcripts, standing in for `GET /api/v1/conversations/{id}/messages` until T-513.
 *
 * The T-503 convention, one directory on: shaped as the **generated** `Message` model so the swap
 * is a data-source change rather than a type change, with every mutation shaped as the API call
 * that replaces it. No timer and no simulated stream — the seed models the *effect* of a call;
 * the intermediate `stage` frames belong to T-513.
 *
 * Extracted from `App.tsx` rather than inlined there because between them these cover every
 * branch the §4.3 surface has, and that is more data than a composition root should carry:
 * citations with and without a score, both eval metrics and one, an empty chat, an abstention, a
 * degraded failure, the typing state and a frozen conversation.
 */
import type { CitationSegment, Message, Segment } from '../api';
import type { TranscriptEntry } from './messages';

export interface SampleChat {
  entries: TranscriptEntry[];
  typing: boolean;
  frozen: boolean;
}

let seq = 0;

function user(text: string): TranscriptEntry {
  seq += 1;
  return {
    message: { id: `u${seq}`, role: 'user', segs: [{ text }], created_at: '2026-07-16T09:12:00Z' },
  };
}

/**
 * A user turn created at runtime by FR-CMP-03's send, rather than seeded.
 *
 * Shares `user()` and its counter so a sent message is indistinguishable from a seeded one —
 * the id is local and provisional either way, and T-513 replaces both with the row the send
 * route returns. `created_at` is stamped rather than fixed because this one really did just
 * happen; the seeds keep their frozen dates so the transcript reads the same on every run.
 */
export function asked(text: string): TranscriptEntry {
  const entry = user(text);
  return { ...entry, message: { ...entry.message, created_at: new Date().toISOString() } };
}

function ai(segs: Segment[], extra: Partial<Message> = {}, outcome?: TranscriptEntry['outcome']) {
  seq += 1;
  return {
    message: {
      id: `a${seq}`,
      role: 'ai' as const,
      segs,
      created_at: '2026-07-16T09:12:20Z',
      model_name: 'gpt-4o',
      ...extra,
    },
    outcome,
  };
}

/** The prototype's own sample citations (lines 292–308), plus the `chunkId` the wire adds. */
function cite(
  doc: string,
  label: string,
  quote: string,
  extra: Partial<CitationSegment> = {},
): CitationSegment {
  return {
    isCite: true,
    doc,
    page: label,
    locator: { label },
    quote,
    chunkId: `${doc}:1`,
    ...extra,
  };
}

const MARKET_ANSWER: Segment[] = [
  {
    text:
      '### Three trends this quarter\n\n' +
      'Mid-market is the story. Growth there **outpaced enterprise for the first time since ' +
      '2024**, and the gap is widening ',
  },
  cite(
    'Q3_Market_Report.pdf',
    'p. 14',
    'Mid-market segment growth reached 34% QoQ, outpacing enterprise (12%) for the first time since 2024.',
  ),
  {
    text:
      '.\n\n' +
      '- Churn concentrates in accounts onboarded before the new playbook\n' +
      '- Retention is materially better once the guided flow com',
  },
  cite(
    'Onboarding_Playbook.pdf',
    '§ Onboarding › Guided flow',
    'Accounts completing the guided 14-day onboarding retain at 94% vs. 71% for legacy onboarding.',
    {
      locator: {
        kind: 'section',
        section_path: ['Onboarding', 'Guided flow'],
        label: '§ Onboarding › Guided flow',
      },
    },
  ),
  {
    text:
      'pletes\n- Self-serve analytics is the most requested next step\n\n' +
      '| Segment | QoQ | Retention |\n' +
      '| :-- | :-: | --: |\n' +
      '| Mid-market | 34% | 94% |\n' +
      '| Enterprise | 12% | 88% |\n\n' +
      'Run `corpus report --segment mid-market` for the full breakdown, or read the ' +
      '[methodology note](https://example.com/methodology).',
  },
];

const REGENERATED: Segment[] = [
  {
    text:
      'Re-reading the source material, the clearest signal is mid-market growth at 34% QoQ ' +
      'against enterprise at 12% ',
  },
  cite(
    'Q3_Market_Report.pdf',
    'p. 14',
    'Mid-market segment growth reached 34% QoQ, outpacing enterprise (12%) for the first time since 2024.',
  ),
  { text: '. Retention follows the same split.' },
];

/** What a successful `POST /messages/{id}/regenerate` leaves behind: the row replaced **in
 *  place** — same id, seq and created_at — with `evaluation` and `feedback` cleared in the same
 *  write, because both are judgements about text that no longer exists (R-56(3)). */
export function regenerated(message: Message): Message {
  return { ...message, segs: REGENERATED, evaluation: null, feedback: null };
}

export const SAMPLE_TRANSCRIPTS: Record<string, SampleChat> = {
  // Citations, both eval metrics, and the whole FR-MSG-07 surface in one answer.
  'sample-analyzing-market-trends': {
    entries: [
      user('What are the biggest trends in the Q3 market data?'),
      ai(MARKET_ANSWER, { evaluation: { relevancy: 0.94, faithfulness: 0.97 } }, 'answered'),
    ],
    typing: false,
    frozen: false,
  },

  // A plain answer (no citations, no evaluation → no source line, no chip row), then an answer
  // whose citation carries NO score at all — R-47(2)'s reranker-failed-open case, which the card
  // must render with no number rather than substituting one — and a partial evaluation.
  'sample-product-launch-strategy': {
    entries: [
      user('Give me a one-line summary.'),
      ai([{ text: 'The launch is on track; pricing is the only open decision.' }], {}, 'answered'),
      user('Where does the pricing decision sit?'),
      ai(
        [
          { text: 'Pricing is gated on the Q3 market read ' },
          cite(
            'Project_Alpha_Brief.pdf',
            'p. 4',
            'Alpha launches with three parallel workstreams; pricing is gated on the Q3 market read.',
          ),
          { text: '.' },
        ],
        { evaluation: { relevancy: 0.86, faithfulness: null }, feedback: 'up' },
        'answered',
      ),
    ],
    typing: false,
    frozen: false,
  },

  // FR-MSG-02.
  'sample-customer-persona-refinement': { entries: [], typing: false, frozen: false },

  // An abstention — a real, stored, rateable row (R-54(3) keeps only `error` out of `messages`)
  // — followed by a degraded failure, which has no row and therefore no action bar.
  'sample-pricing-experiment-review': {
    entries: [
      user('What did the Q1 pricing experiment conclude?'),
      ai(
        [
          {
            text:
              "I couldn't ground an answer to that in your documents — what I found doesn't " +
              "support a reliable response, and I'd rather say so than guess. Try rephrasing " +
              'the question, or check that the document you have in mind has finished processing.',
          },
        ],
        { evaluation: null },
        'abstained',
      ),
      user('Try again with the pricing deck.'),
      {
        message: {
          id: null,
          role: 'ai',
          segs: [
            {
              text: "I couldn't reach your documents just now, so I can't ground an answer. Please try again shortly.",
            },
          ],
        },
        outcome: 'error',
      },
    ],
    typing: false,
    frozen: false,
  },

  // FR-MSG-05, and FR-MSG-01's fourth scroll trigger.
  'sample-q4-forecast-draft': {
    entries: [user('Draft the Q4 forecast narrative from the market report.')],
    typing: true,
    frozen: false,
  },

  // FR-STA-04 / R-67 — frozen, not broken.
  'sample-vendor-security-review': {
    entries: [
      user('Summarise the vendor security questionnaire.'),
      ai(
        [{ text: 'The questionnaire covers access control, retention and sub-processors.' }],
        {},
        'answered',
      ),
      user('Which answers were incomplete?'),
      ai([{ text: 'Two: data residency and the breach-notification window.' }], {}, 'answered'),
    ],
    typing: false,
    frozen: true,
  },
};
