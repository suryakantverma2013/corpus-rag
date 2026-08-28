/**
 * Pure helpers for the §4.6 stats panel — no React, no DOM (the `chat/messages.ts` shape).
 *
 * Everything here is what the prototype computes in `renderVals()` (`RAG Chatbot.dc.html`
 * lines 360–444). It lives in its own module for the same reason that one does: this is
 * averaging, rounding and singular/plural arithmetic, which is wrong by one for a year unless it
 * is testable without rendering. FR-CST-02 names exactly this set as "derived per active chat".
 */
import type { Evaluation } from '../api';
import { citationsOf, evalBand, isDegraded, isUngrounded } from '../chat/messages';
import type { EvalBand, TranscriptEntry } from '../chat/messages';

/** FR-ANL-04's "no evaluations" cell, and FR-EVL-04's permanent one. U+2014. */
export const EM_DASH = '—';

/* ── FR-ANL-01 — session cards ─────────────────────────────────────────────── */

/**
 * FR-ANL-01's `mm:ss`.
 *
 * **No hour rollover**, matching the prototype: at 90 minutes this reads `90:00`, not `1:30:00`.
 * A third field would change the card's width behaviour, and the requirement says `mm:ss`.
 */
export function formatDuration(elapsedSeconds: number): string {
  const total = Math.max(0, Math.floor(elapsedSeconds));
  return `${pad(Math.floor(total / 60))}:${pad(total % 60)}`;
}

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/**
 * FR-ANL-01's MESSAGES value — "messages in the active chat", so **every** row in the transcript.
 *
 * Includes the R-54(3) degraded turn, which is served but never stored. That makes this card
 * disagree slightly with the FR-ANL-03 meter, which is derived server-side from `messages` rows
 * and therefore cannot see it — and both are right: this counts what the user is looking at,
 * the meter counts what the next turn will be charged for (R-51(1)'s cost-vs-length split, one
 * level down). The FR-MSG-05 typing indicator is not an entry and so is not counted.
 */
export function messageCount(entries: readonly TranscriptEntry[]): number {
  return entries.length;
}

/* ── FR-ANL-02 — the model card ────────────────────────────────────────────── */

/**
 * The newest answered turn's `model_name`, or `null` in a chat with no AI message yet.
 *
 * FR-SYS-03/FR-ANL-02 describe the *configured* model id, and **no API exposes it** — the only
 * source on the wire is `MessageResponse.model_name`, which is per message. Newest rather than
 * first because the operator may have changed models mid-conversation, in which case the card
 * should name the one that would answer next.
 */
export function modelNameOf(entries: readonly TranscriptEntry[]): string | null {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const { message } = entries[i];
    if (isDegraded(message) || message.role !== 'ai') continue;
    const name = message.model_name;
    if (typeof name === 'string' && name !== '') return name;
  }
  return null;
}

/* ── FR-ANL-03 — the context-window meter ──────────────────────────────────── */

/** The half of `ContextWindowResponse` this card needs. `reserve` is the composer's (R-51(3)). */
export interface ContextUsage {
  used: number;
  limit: number;
}

/**
 * The meter's percentage, **clamped to 0..100**, as a whole number.
 *
 * The clamp is not defensive tidying: R-51(3) reserves headroom for the answer, so a chat can
 * legitimately sit at its FR-STA-04 block with `used` close to `limit`, and any drift between
 * the server's count and this one must not render "104% used" or a fill wider than its track.
 * The card at the limit simply stands full — frozen, not broken (R-67(1)).
 *
 * `limit <= 0` answers **100**, matching `ContextUsage.percent_used` in `app/rag/budget.py`
 * rather than inventing a second convention for a misconfiguration. It also cannot be omitted:
 * `0 / 0` is `NaN`, `width: "NaN%"` is an invalid declaration, the browser **drops** it, and the
 * fill falls back to `width: auto` — a silently full bar. jsdom computes no layout, so no unit
 * test could ever see that.
 *
 * Whole numbers here, deliberately: FR-ANL-03 writes `{pct}% used` and the prototype rounds to an
 * integer, while the wire's `ContextWindowResponse.percent_used` carries **one decimal**. Binding
 * that field straight through would render `85.6% used`. So `App` passes `used`/`limit` and this
 * derives (T-513); nothing may bind `percent_used`.
 */
export function percentUsed({ used, limit }: ContextUsage): number {
  if (limit <= 0) return 100;
  return Math.min(100, Math.max(0, Math.round((100 * used) / limit)));
}

/** FR-ANL-03 / §9 — `3.8K / 10.4K`. One decimal on both sides, always. */
export function tokensLabel({ used, limit }: ContextUsage): string {
  return `${(used / 1000).toFixed(1)}K / ${(limit / 1000).toFixed(1)}K`;
}

/**
 * FR-ANL-03's caption — `31% used · 7K tokens remaining`.
 *
 * The prototype splits this across a binding ending in `K tokens` and the literal ` remaining`;
 * it is one string here because it is one sentence, and splitting it invites a translator or a
 * later editor to fix the spacing in only one half.
 */
export function contextCaption(usage: ContextUsage): string {
  const remaining = Math.round(Math.max(0, usage.limit - usage.used) / 1000);
  return `${percentUsed(usage)}% used · ${remaining}K tokens remaining`;
}

/* ── FR-ANL-04 / FR-EVL-04 — session averages ──────────────────────────────── */

export type EvalMetricKey = 'relevancy' | 'faithfulness' | 'precision' | 'recall';

export interface EvalAverageRow {
  key: EvalMetricKey;
  label: string;
  /** `0.90`, or `null` when this chat has no value for the metric ({@link EM_DASH} is rendered). */
  score: string | null;
  /** `null` exactly when `score` is: an unscored metric has no FR-EVL-03 band and no bar. */
  band: EvalBand | null;
  /** `'90%'`, or `null` when unscored. Derived from the **same rounded value** as `score`. */
  width: string | null;
}

export interface EvalAverages {
  /** Always four rows, in FR-ANL-04's order (NFR-VIS-01, prototype `evalAgg`). */
  rows: EvalAverageRow[];
  /**
   * FR-EVL-04's mean of the metric averages **that exist**; `null` when none do.
   *
   * `overall === null` **iff every row's `score` is null** iff the card shows its empty state —
   * an invariant the component leans on to decide between the sentence and the rows, and one the
   * tests pin.
   */
  overall: number | null;
}

/**
 * FR-ANL-04's four rows.
 *
 * **Deliberately four, and deliberately not `chat/messages.ts`'s `METRICS`.** That list is two
 * entries because FR-EVL-02 renders one chip per metric *actually present* and must never be
 * padded (R-50(1)); this card is the opposite case — R-52(1) fixes its shape at four rows, of
 * which `Ctx Precision` and `Ctx Recall` are em-dashed **permanently**, because they are
 * reference-based and a live turn has no reference answer. That is a property of the metrics,
 * not a gap awaiting work, so the two lists must not be merged.
 */
const METRIC_ROWS: readonly (readonly [EvalMetricKey, string])[] = [
  ['relevancy', 'Relevancy'],
  ['faithfulness', 'Faithfulness'],
  ['precision', 'Ctx Precision'],
  ['recall', 'Ctx Recall'],
];

/**
 * One metric's value from one evaluation, or `null`.
 *
 * The narrowing is load-bearing rather than ceremonial: `EvaluationResponse` carries exactly
 * `relevancy` and `faithfulness`, so the two reference-based keys have **no field to read** and
 * the `null` below is structural, not a missing value. If the wire ever grows them this stops
 * compiling, which is the right place to find out.
 */
function metricValue(evaluation: Evaluation, key: EvalMetricKey): number | null {
  if (key !== 'relevancy' && key !== 'faithfulness') return null;
  const value = evaluation[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function evaluationsOf(entries: readonly TranscriptEntry[]): Evaluation[] {
  const found: Evaluation[] = [];
  for (const { message } of entries) {
    if (isDegraded(message) || message.role !== 'ai') continue;
    // FR-EVL-04 (R-98(4)) — an FR-MSG-09 answer is excluded from the session averages.
    //
    // **Explicit, although the backend never scores one.** R-50 skips a message that cites
    // nothing, so an ungrounded row arrives with `evaluation: null` and the next line would drop
    // it anyway — today. That makes this look redundant and it is not: the card's meaning is the
    // requirement, and leaning on a *different* subsystem's skip rule to enforce it is how a
    // later change to the judge silently redefines what the panel measures. R-98(3)'s discipline
    // one layer up: make the invariant a property of this code, not a fact about another's.
    if (isUngrounded(message)) continue;
    const { evaluation } = message;
    if (evaluation === null || evaluation === undefined) continue;
    found.push(evaluation);
  }
  return found;
}

/**
 * FR-ANL-04 / FR-EVL-04 — averaged over the evaluated responses in the active chat.
 *
 * **Averages of raw values, rounded once at display.** The prototype averages the already
 * `toFixed(2)`-ed strings and divides by four *including the metrics it has no value for* — so
 * one scored answer would show an overall of 0.48 against real scores of 0.94 and 0.97. Both are
 * corrected here: FR-EVL-04 says "the mean of the metric averages that exist", and rounding
 * twice loses a digit for no reason.
 *
 * A metric that failed while its sibling succeeded (R-50(3) guards them independently) simply
 * contributes nothing to its own row and still counts nowhere else.
 *
 * **The row's band and bar width come from the ROUNDED score, not the raw average**, and that is
 * the subtle one. A mean has as many decimals as it likes — `(0.89 + 0.9002) / 2 = 0.8951` — so
 * banding the raw value paints an **amber** bar beside a numeral that reads `0.90`, and sizes the
 * bar at 89% while the number says 90. `evalChips` bands the raw value quite correctly, because a
 * per-message judge score already *is* the printed number; an average is not. NFR-A11Y-06 makes
 * that numeral the load-bearing carrier of the band, so the numeral, the hue and the width must
 * agree **by construction** rather than by luck.
 */
export function evalAverages(entries: readonly TranscriptEntry[]): EvalAverages {
  const evaluations = evaluationsOf(entries);
  const raw = METRIC_ROWS.map(([key, label]) => {
    const values = evaluations
      .map((evaluation) => metricValue(evaluation, key))
      .filter((value): value is number => value !== null);
    return { key, label, average: values.length === 0 ? null : mean(values) };
  });
  const rows = raw.map(({ key, label, average }) => {
    if (average === null) return { key, label, score: null, band: null, width: null };
    const rounded = Number(formatScore(average));
    return {
      key,
      label,
      score: formatScore(rounded),
      band: evalBand(rounded),
      width: `${Math.round(rounded * 100)}%`,
    };
  });
  const present = raw
    .map((row) => row.average)
    .filter((average): average is number => average !== null);
  return { rows, overall: present.length === 0 ? null : mean(present) };
}

function mean(values: readonly number[]): number {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

/**
 * FR-ANL-04's header value — `0.92 overall`, or `—` when nothing has been evaluated.
 *
 * Two decimals, and **R-70 rules that deliberate**: the judges disagree by more than the second
 * decimal is worth (T-314 measured ≥0.25 on 22% of scores), so the digit is indicative rather
 * than exact — which is said in the tooltip and the visually-hidden text, not by dropping it.
 * See `INDICATIVE_TOOLTIP`.
 */
export function overallLabel(overall: number | null): string {
  return overall === null ? EM_DASH : `${formatScore(overall)} overall`;
}

/** FR-EVL-02's format, shared with the per-message chip so the two surfaces cannot drift. */
export function formatScore(value: number): string {
  return value.toFixed(2);
}

/* ── FR-ANL-05 — sources referenced ────────────────────────────────────────── */

export interface SourceRow {
  name: string;
  type: string;
  passages: number;
}

/**
 * FR-ANL-05 — one row per **distinct cited document**, with its passage count.
 *
 * Keyed on `CitationSegment.doc`, which is a filename: a citation carries no document id, so the
 * name is the only identity available (and `chunkId` is explicitly not one — R-36(6) makes a
 * historical chunk id dangle by design). Insertion order is preserved, so rows appear in
 * first-cited order — the order the user met them in, and stable across re-renders.
 *
 * Every entry is scanned rather than only AI ones: a user turn's segments are text runs and
 * contribute nothing, so a role guard would only be a second thing to keep in step.
 */
export function sourcesReferenced(entries: readonly TranscriptEntry[]): SourceRow[] {
  const passagesByName = new Map<string, number>();
  for (const { message } of entries) {
    for (const citation of citationsOf(message.segs)) {
      passagesByName.set(citation.doc, (passagesByName.get(citation.doc) ?? 0) + 1);
    }
  }
  return [...passagesByName].map(([name, passages]) => ({
    name,
    type: documentTypeBadge(name),
    passages,
  }));
}

/**
 * The FR-ANL-05 type badge, derived from the filename extension.
 *
 * The prototype looks the name up in its document list; that is declined because a cited document
 * need not be in the composer's FR-CMP-04 list, which is scoped, and a citation carries no type
 * of its own. FR-ING-02 admits exactly four formats, so the extension is total — the fallback
 * exists only for a name the backend should never have accepted.
 */
export function documentTypeBadge(filename: string): string {
  const dot = filename.lastIndexOf('.');
  const extension = dot === -1 ? '' : filename.slice(dot + 1).toLowerCase();
  return TYPE_BY_EXTENSION[extension] ?? 'DOC';
}

const TYPE_BY_EXTENSION: Record<string, string> = {
  pdf: 'PDF',
  docx: 'DOC',
  doc: 'DOC',
  csv: 'CSV',
  md: 'MD',
};
