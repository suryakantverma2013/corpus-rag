/**
 * Transcript fixtures — the §4.3 branch matrix, in one place.
 *
 * These drove `App.tsx` until T-513 wired the real API; they are kept because between them they
 * cover every branch the surface has and rebuilding that inside five test files is how the
 * branches quietly stop being covered: citations with and without a score (R-47(2)), both eval
 * metrics and one (R-50(3)), an empty chat, an abstention (a real, rateable row), a degraded
 * failure (no row, no action bar), a turn in flight, and a frozen conversation.
 *
 * **In `src/test/` on purpose.** That directory is excluded from `tsconfig.app.json` and
 * included by `tsconfig.test.json`, so a fixture here *cannot* be imported by shipping code —
 * the guarantee that actually matters once the seeds are no longer the product's data source.
 * It is not a `*.test.ts`, so Vitest does not collect it (`css-source.ts`'s precedent).
 *
 * `typing` and `frozen` are still here and are still fixture-only: in production both are
 * derived — `typing` from the chat store's turn, `frozen` from `isFrozen(usage)` — but a test
 * that wants the frozen surface should be able to say so in one word.
 */
import type { ContextWindow, CitationSegment, Message, Segment } from '../api';
import type { TranscriptEntry } from '../chat/messages';

export interface SampleChat {
  entries: TranscriptEntry[];
  typing: boolean;
  frozen: boolean;
  /** The FR-ANL-03 meter this chat would read back, as `ConversationDetailResponse.context`. */
  usage: ContextWindow;
}

/** Room to spare, and the boundary at which no message of any length fits (R-67(1)). */
export const ROOMY_USAGE: ContextWindow = {
  used_tokens: 240,
  limit_tokens: 10_400,
  remaining_tokens: 10_160,
  percent_used: 2.3,
  answer_reserve_tokens: 1_500,
};
export const FULL_USAGE: ContextWindow = {
  used_tokens: 8_900,
  limit_tokens: 10_400,
  remaining_tokens: 1_500,
  percent_used: 85.6,
  answer_reserve_tokens: 1_500,
};

let seq = 0;

function user(text: string): TranscriptEntry {
  seq += 1;
  return {
    message: {
      id: `u${seq}`,
      role: 'user',
      segs: [{ text }],
      // FR-MSG-09 — false on both counts for a question, and stated rather than defaulted: the
      // server always sends them, so `MessageResponse` is non-optional here and a fixture that
      // omitted them would be describing a message the API cannot produce.
      ungrounded: false,
      ungrounded_offerable: false,
      created_at: '2026-07-16T09:12:00Z',
    },
  };
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
      // FR-MSG-09's defaults, **before** the spread so a fixture can override either — an
      // ungrounded answer and an abstention that offers the control are both built this way.
      ungrounded: false,
      ungrounded_offerable: false,
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

export const TRANSCRIPT_FIXTURES: Record<string, SampleChat> = {
  // Citations, both eval metrics, and the whole FR-MSG-07 surface in one answer.
  'sample-analyzing-market-trends': {
    entries: [
      user('What are the biggest trends in the Q3 market data?'),
      ai(MARKET_ANSWER, { evaluation: { relevancy: 0.94, faithfulness: 0.97 } }, 'answered'),
    ],
    typing: false,
    frozen: false,
    usage: ROOMY_USAGE,
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
    usage: ROOMY_USAGE,
  },

  // FR-MSG-02.
  'sample-customer-persona-refinement': {
    entries: [],
    typing: false,
    frozen: false,
    usage: ROOMY_USAGE,
  },

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
    usage: ROOMY_USAGE,
  },

  // FR-MSG-05, and FR-MSG-01's fourth scroll trigger.
  'sample-q4-forecast-draft': {
    entries: [user('Draft the Q4 forecast narrative from the market report.')],
    typing: true,
    frozen: false,
    usage: ROOMY_USAGE,
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
    usage: FULL_USAGE,
  },
};
