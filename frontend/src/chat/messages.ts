/**
 * Pure helpers for the §4.3 transcript — no React, no DOM (the `sidebar/conversations.ts` shape).
 *
 * Everything here is derived state the prototype computes in `renderVals()`. It lives in its own
 * module for the reason that one does: this is index arithmetic and singular/plural rules, the
 * kind of thing that is wrong by one for a year unless it is testable without rendering.
 */
import type {
  CitationSegment,
  DegradedMessage,
  Evaluation,
  Message,
  Segment,
  TurnOutcome,
} from '../api';

/**
 * One row of the transcript.
 *
 * `outcome` is **not** on `MessageResponse` — it rides the SSE `message` frame
 * (`MessageFrameData.outcome`) and `GET /api/v1/conversations/{id}/messages` does not carry it.
 * So it is known for turns that arrived live this session and unknown for a reloaded transcript,
 * which is why it is optional here and why it feeds **only** the NFR-A11Y-05 announcement and
 * never the rendering: a surface that looked different depending on how the page was reached
 * would be a defect. Flagged for the author as a candidate backend addition (the T-407 shape).
 */
export interface TranscriptEntry {
  message: Message | DegradedMessage;
  outcome?: TurnOutcome | null;
}

/**
 * The served-but-unstored branch — an FR-ORC-05 failure or an FR-ORC-02 denial (R-54(3)).
 *
 * Narrowed on `created_at`, **not** on `id === null`: `DegradedMessage.id` is `string | null` and
 * a *regenerate* failure carries the target's id so the client can say which bubble the error
 * belongs to, so an id test would misclassify exactly the case that matters.
 */
export function isDegraded(message: Message | DegradedMessage): message is DegradedMessage {
  return !('created_at' in message);
}

/**
 * FR-MSG-06's union is **not tagged**: `isCite` is absent on a text run rather than `false`, so
 * `'isCite' in seg` is the narrowing, and `seg.isCite === true` would be a type error.
 */
export function isCitation(segment: Segment): segment is CitationSegment {
  return 'isCite' in segment;
}

/**
 * The whole answer as plain text — FR-MSG-03's user bubble and FR-ERR-04's server copy.
 *
 * Citation segments contribute nothing: their `doc` is a chip, not prose, and their `quote` is
 * document text that belongs only in the FR-CIT-03 card.
 */
export function plainText(segments: readonly Segment[]): string {
  return segments.map((seg) => (isCitation(seg) ? '' : seg.text)).join('');
}

export function citationsOf(segments: readonly Segment[]): CitationSegment[] {
  return segments.filter(isCitation);
}

/**
 * FR-MSG-04(2) — `grounded in 2 passages · Q3_Market_Report.pdf · Onboarding_Playbook.pdf`.
 *
 * The count is **passages** (citation segments) while the list is **distinct documents**, so two
 * citations into one file read `grounded in 2 passages · A.pdf`. That is the requirement's own
 * wording ("lists distinct cited documents"; "passage is singular when exactly one citation
 * exists"), and it is why the two halves are computed from different things.
 *
 * Returns `null` — not `''` — when there are no citations, so the caller omits the element
 * rather than rendering an empty one into the content column's 8px `gap`.
 */
export function sourceLine(citations: readonly CitationSegment[]): string | null {
  if (citations.length === 0) return null;
  const documents = [...new Set(citations.map((c) => c.doc))];
  return `grounded in ${passageCount(citations.length)} · ${documents.join(' · ')}`;
}

/**
 * `1 passage` / `2 passages` — FR-MSG-04(2)'s source line and FR-ANL-05's source card.
 *
 * Extracted for the second caller rather than copied. FR-ANL-05 writes the count as `{N} passages`
 * with no singular case, but the requirement one section away is explicit that "passage is
 * singular when exactly one citation exists" — and a chat citing one document once is the common
 * case, not a corner, so the literal reading renders `1 passages` on the stats panel while the
 * message above it says `1 passage`. One rule, one copy.
 */
export function passageCount(passages: number): string {
  return `${passages} passage${passages === 1 ? '' : 's'}`;
}

/**
 * FR-CIT-03's right-aligned locator, e.g. `p. 14` · `§ Setup › Install` · `rows 218–340`.
 *
 * **Read, never parsed and never reassembled.** FR-CIT-04 is explicit that clients read the
 * published fields rather than interpreting the label, and R-34 is why the label is published at
 * all: only PDF has pages, so a client that rebuilt `p. N` from `locator.page` would render
 * nothing for a DOCX section or a CSV row range. `locator.label` first, `page` as the legacy
 * fallback, `null` when the backend supplied neither.
 */
export function locatorLabel(citation: CitationSegment): string | null {
  const label = citation.locator?.label ?? citation.page ?? null;
  return label === null || label === '' ? null : label;
}

/** FR-CIT-03's footer with a score. */
const FOOTER_PREFIX = 'Source passage';

/**
 * FR-CIT-03/FR-CIT-04's footer.
 *
 * **The no-score variant drops the clause and adds nothing** — `Source passage`. R-47(2) makes an
 * absent score routine rather than exceptional: the reranker fails open, and the RRF order it
 * falls back to is deliberately unpublished because R-46(3) makes it comparable within a turn
 * only. So there is nothing truthful to put in the number's place, and the two candidates both
 * mislead — "score unavailable" reads as a degraded citation when the citation is not degraded,
 * and dropping the footer entirely changes the card's height between two citations in one answer
 * while discarding the line's actual job, which is labelling the block above it as a *retrieved*
 * passage. That stays true whether or not a score exists.
 *
 * **Author-confirmed by R-69(1) and written into FR-CIT-04** — no longer provisional. Both
 * alternatives were declined on the record for the reasons above.
 */
export function citationFooter(citation: CitationSegment): string {
  const { score } = citation;
  if (typeof score !== 'number' || !Number.isFinite(score)) return FOOTER_PREFIX;
  // Two decimals although the value is always a tenth. R-69(2) measured the reranker across
  // 90 scored passages and found no non-zero second decimal at any scale — the granularity
  // belongs to the model, not to `RERANK_SCORE_SCALE` — so this renders `0.90`, never `0.91`.
  // Kept deliberately: the trailing zero is not a false claim, two decimals is the
  // prototype's form (NFR-VIS-01), and dropping a digit here would split from the eval chips
  // beside it — which **R-70 has since settled at two decimals as well** (Rev 0.37, §8.57),
  // ruling them indicative rather than exact. So the two surfaces agree, for two different
  // reasons that R-69(2)/R-69(3) were careful to keep apart: this number *lacks* the resolution
  // it shows, while a judge score has hundredths that cannot be trusted to them.
  return `${FOOTER_PREFIX} · retrieval score ${score.toFixed(2)}`;
}

/**
 * The native tooltip on every rendered DeepEval score — FR-EVL-02's chip and FR-ANL-04's rows.
 *
 * **One constant for both surfaces, by R-70.** FR-EVL-02 originally named the tooltip
 * `DeepEval metric`; the qualification is the ruling that closed OI-35, which measured two
 * frontier judges disagreeing by ≥0.25 on 22% of scores — both of them scoring answers that are
 * *verbatim restatements of the passage they cite* at 0.50. So the second decimal is real output
 * and is not a measurement, and the honest fix is to say so rather than to drop the digit: the
 * numeral is in the pixel-perfect prototype (NFR-VIS-01), it is what stops colour being the sole
 * carrier of the FR-EVL-03 band (NFR-A11Y-06), and one decimal would be no more *trustworthy*,
 * only less precise. A `title` is invisible until hover, so this costs no pixels.
 *
 * It lives here rather than beside either surface because R-69(3) required both to be settled
 * together, and two copies is how they stop agreeing.
 */
export const EVAL_SCORE_TOOLTIP =
  'DeepEval metric — indicative judge score, not an exact measurement';

/* ── FR-MSG-09 — the ungrounded answer (R-98) ──────────────────────────────── */

/**
 * The label an FR-MSG-09 answer carries, verbatim from §9.
 *
 * **This string is the safety control**, which is why it is a constant and not markup. R-98(6)
 * requires that a reader can always tell which kind of answer they are reading, and NFR-A11Y-06
 * forbids colour as the sole carrier of information — so the border and the muted tone are
 * decoration, and these words are the whole of the guarantee. The typographic apostrophe and the
 * em dash are the spec's own; a fidelity check reads this constant rather than a copy.
 */
export const UNGROUNDED_LABEL = 'From the model’s general knowledge — not from your documents';

/** FR-MSG-09's control, offered on an abstention only. §9's literal. */
export const UNGROUNDED_ACTION_LABEL = 'Answer from general knowledge';

/**
 * Whether this message is an FR-MSG-09 answer.
 *
 * Reads the server's column rather than inferring from "an AI answer with no citations", which
 * an ordinary abstention also satisfies — the two must never render alike.
 */
export function isUngrounded(message: Message | DegradedMessage): boolean {
  return !isDegraded(message) && message.ungrounded;
}

export type EvalBand = 'good' | 'warn' | 'bad';

export interface EvalChip {
  key: 'relevancy' | 'faithfulness';
  label: string;
  score: string;
  band: EvalBand;
}

/**
 * FR-EVL-03's theme-independent bands. Boundaries are inclusive at the top of the lower band —
 * 0.90 is good, 0.899 is warn — matching the requirement's `≥ 0.90` / `0.80–0.89` / `< 0.80`.
 */
export function evalBand(score: number): EvalBand {
  if (score >= 0.9) return 'good';
  if (score >= 0.8) return 'warn';
  return 'bad';
}

/**
 * FR-EVL-01 names four metrics; **`messages.evaluation` carries exactly two** (R-50(1)) — the
 * other two are reference-based, a live turn has no reference answer, and they belong to the
 * T-312 offline harness. So this list is two entries and must not grow to match the prototype's
 * sample data, whose four-metric object the spec has overridden.
 */
const METRICS = [
  ['relevancy', 'Relevancy'],
  ['faithfulness', 'Faithfulness'],
] as const;

/**
 * FR-EVL-02 — one chip per metric **actually present**, in the order above.
 *
 * The evaluation job fails open and guards its two metrics independently (R-50(3)), so a partial
 * `{relevancy: 0.9, faithfulness: null}` is a correct result, not an error, and the absent metric
 * is simply absent: **the row is never padded with a placeholder number**. `null` and `undefined`
 * for the whole object are the same case — the job has not landed, or never will.
 */
export function evalChips(evaluation: Evaluation | null | undefined): EvalChip[] {
  if (evaluation === null || evaluation === undefined) return [];
  const chips: EvalChip[] = [];
  for (const [key, label] of METRICS) {
    const score = evaluation[key];
    if (typeof score !== 'number' || !Number.isFinite(score)) continue;
    chips.push({ key, label, score: score.toFixed(2), band: evalBand(score) });
  }
  return chips;
}

/**
 * NFR-A11Y-05's polite announcement.
 *
 * Required because R-54(2) streams progress frames with **no content** and then commits the whole
 * answer at once — so without this a non-sighted user gets no signal that a reply exists at all.
 *
 * A degraded turn announces the server's own copy verbatim: R-43(6) puts the FR-ERR-04 per-class
 * text on the server precisely so the client never keeps a second copy, and that applies to what
 * is spoken as much as to what is painted. An ordinary answer announces a fixed phrase rather
 * than the answer itself — a live region reading several hundred words interrupts the user's own
 * reading, and the text is right there to navigate to.
 *
 * **Author-confirmed by R-69(1) and written into NFR-A11Y-05** — no longer provisional.
 */
const ANSWER_ANNOUNCEMENT = 'Answer received.';

export function announcementFor(entry: TranscriptEntry): string | null {
  const { message } = entry;
  if (message.role !== 'ai') return null;
  if (isDegraded(message)) return plainText(message.segs).trim() || ANSWER_ANNOUNCEMENT;
  return ANSWER_ANNOUNCEMENT;
}
