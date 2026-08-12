/**
 * The §4.6 stats panel — FR-ANL-01..05, the third FR-LAY-01 region.
 *
 * Presentational in the T-503 sense: every value is a prop or is derived from one by `stats.ts`,
 * and nothing here fetches. T-513 owns the data layer and swaps `App`'s seeds for the API without
 * touching a derivation.
 *
 * **The root is a fragment, and that is load-bearing.** `AppShell`'s `.stats` already owns the
 * column's width, border, background, 18px padding, `overflow-y` *and its 14px gap*, so the six
 * children below are the panel's six flex items. A wrapper `<div>` would make them one item and
 * swallow every gap — invisible to jsdom, which computes no layout.
 *
 * One file, five local components, on the `MessageList` precedent: nothing here is reused, all of
 * it shares one card box, and five stylesheets would mean five copies of that box.
 *
 * Everything is transcribed per property from `RAG Chatbot.dc.html` lines 161–226, which under
 * R-66 has never rendered — so the geometry is verified headed rather than assumed.
 */
import { useEffect, useId, useState } from 'react';

import styles from './StatsPanel.module.css';
import {
  EM_DASH,
  contextCaption,
  evalAverages,
  formatDuration,
  overallLabel,
  percentUsed,
  sourcesReferenced,
  tokensLabel,
} from './stats';
import type { ContextUsage, EvalAverageRow, SourceRow } from './stats';
import { EVAL_SCORE_TOOLTIP, passageCount } from '../chat/messages';
import type { EvalBand, TranscriptEntry } from '../chat/messages';

/* §4.6 / §9 copy. Normative and byte-exact — NFR-USE-04 requires the two empty states verbatim. */
const SESSION_LABEL = 'SESSION';
const DURATION_LABEL = 'DURATION';
const MESSAGES_LABEL = 'MESSAGES';
const MODEL_LABEL = 'MODEL';
const MODEL_CAPTION = 'Context synthesis · grounding on';
const CONTEXT_LABEL = 'CONTEXT WINDOW';
const EVAL_LABEL = 'DEEPEVAL · SESSION AVG';
const EVAL_EMPTY = 'Scores appear once a response is evaluated.';
const SOURCES_LABEL = 'SOURCES REFERENCED';
const SOURCES_EMPTY = 'None yet — answers will list their sources here.';

/**
 * R-70's qualification, for a reader who cannot hover a `title`.
 *
 * Once per card rather than once per row: this card is a per-chat summary rendered a single time,
 * so one sentence says it without turning four rows into four disclaimers. The per-message
 * FR-EVL-02 chips carry the same point on `EVAL_SCORE_TOOLTIP` alone, because a row of chips
 * repeats under every answer.
 */
const EVAL_INDICATIVE_NOTE = 'Indicative judge scores, not exact measurements.';

/**
 * The two permanently em-dashed FR-EVL-04 cells, spoken.
 *
 * Spec-authored: R-52(1) created a state the prototype never renders, so §9 has no string for it.
 * Without this a screen-reader user hears "Ctx Precision" and then nothing — most assistive
 * technology skips a bare em dash at default punctuation levels.
 */
const NOT_SCORED = 'not scored';

/** FR-ANL-01's ticker. The prototype's `setInterval(…, 1000)`. */
const TICK_MS = 1_000;

export interface StatsPanelProps {
  /**
   * FR-CST-01's session `startTime`, ms since epoch.
   *
   * A prop rather than state here because FR-ANL-01 scopes DURATION to the **session** (R-14),
   * while FR-LAY-02's `showStats={false}` *unmounts* this component — so a panel-owned start
   * would restart the clock every time the user hid and re-showed the column.
   */
  sessionStartedAt: number;
  /** The active chat's transcript. FR-ANL-01's count, FR-ANL-04's averages, FR-ANL-05's sources. */
  entries: readonly TranscriptEntry[];
  /**
   * FR-ANL-03's meter. **The same object the composer projects FR-STA-04 against**, so the two
   * cannot disagree about how full the conversation is.
   */
  usage: ContextUsage;
  /** FR-ANL-02 / FR-SYS-03 — the model id. Never null: `App` supplies the configured fallback. */
  modelName: string;
}

export function StatsPanel({ sessionStartedAt, entries, usage, modelName }: StatsPanelProps) {
  const elapsed = useElapsedSeconds(sessionStartedAt);

  return (
    <>
      <h2 className={styles.sectionLabel}>{SESSION_LABEL}</h2>

      <div className={styles.sessionGrid}>
        <StatCard label={DURATION_LABEL} value={formatDuration(elapsed)} />
        <StatCard label={MESSAGES_LABEL} value={String(entries.length)} />
      </div>

      <div className={`${styles.card} ${styles.panelCard}`}>
        <h2 className={styles.cardLabel}>{MODEL_LABEL}</h2>
        <div className={`${styles.modelId} mono`}>{modelName}</div>
        <div className={styles.modelCaption}>{MODEL_CAPTION}</div>
      </div>

      <ContextMeter usage={usage} />
      <EvalAverages entries={entries} />
      <SourcesReferenced entries={entries} />
    </>
  );
}

/**
 * Recomputed from the clock on every tick rather than incremented, so `setInterval` drift never
 * accumulates in the displayed value — it can only skip or repeat a second. The prototype's own
 * shape (`s.now - s.start`).
 *
 * The empty dependency list is deliberate: a changed `startedAt` must not restart the interval,
 * and it does not need to, since the value is derived on the next tick. Under StrictMode the
 * effect mounts, cleans up and mounts again, leaving exactly one live interval.
 */
function useElapsedSeconds(startedAt: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);
  // Clamped: a clock stepped backwards by NTP would otherwise render `-1:-1`.
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className={`${styles.card} ${styles.sessionCard}`}>
      <h3 className={styles.cardLabel}>{label}</h3>
      <div className={`${styles.statValue} mono`}>{value}</div>
    </div>
  );
}

/**
 * FR-ANL-03.
 *
 * The track is `aria-hidden`: it is a redundant visual encoding of two strings already rendered
 * beside it — `8.9K / 10.4K` and `86% used · 2K tokens remaining`. A `role="progressbar"` here
 * would announce the same percentage a third time, as a widget. The board's "give them accessible
 * text" is discharged by *guaranteeing the text*, not by duplicating it.
 */
function ContextMeter({ usage }: { usage: ContextUsage }) {
  return (
    <div className={`${styles.card} ${styles.panelCard}`}>
      <div className={styles.cardHeader}>
        <h2 className={styles.cardLabel}>{CONTEXT_LABEL}</h2>
        <span className={`${styles.tokens} mono`}>{tokensLabel(usage)}</span>
      </div>
      <div className={styles.track} aria-hidden="true">
        <div className={styles.fill} style={{ width: `${percentUsed(usage)}%` }} />
      </div>
      <div className={styles.caption}>{contextCaption(usage)}</div>
    </div>
  );
}

/**
 * FR-ANL-04 / FR-EVL-04.
 *
 * Four rows whenever anything has been evaluated (NFR-VIS-01, prototype `evalAgg`), of which
 * Ctx Precision and Ctx Recall are em-dashed permanently — R-52(1). **Zero** rows when nothing
 * has, which is the prototype's `evs.length ? evalAgg : []` and is why the empty sentence never
 * appears above a column of dashes.
 */
function EvalAverages({ entries }: { entries: readonly TranscriptEntry[] }) {
  const { rows, overall } = evalAverages(entries);
  const scored = overall !== null;

  return (
    <div className={`${styles.card} ${styles.panelCard}`} title={EVAL_SCORE_TOOLTIP}>
      <div className={styles.cardHeader}>
        <h2 className={styles.cardLabel}>{EVAL_LABEL}</h2>
        {/* The dash is aria-hidden because the sentence below says the same thing in words, and
            the two are rendered under exactly the same condition. */}
        <span className={`${styles.overall} mono`} aria-hidden={scored ? undefined : 'true'}>
          {overallLabel(overall)}
        </span>
      </div>
      {scored ? (
        <>
          <span className="visually-hidden">{EVAL_INDICATIVE_NOTE}</span>
          <div className={styles.metrics}>
            {rows.map((row) => (
              <MetricRow key={row.key} row={row} />
            ))}
          </div>
        </>
      ) : (
        <div className={styles.evalEmpty}>{EVAL_EMPTY}</div>
      )}
    </div>
  );
}

function MetricRow({ row }: { row: EvalAverageRow }) {
  return (
    <div>
      <div className={styles.metricHead}>
        <span className={styles.metricLabel}>{row.label}</span>
        {row.score === null ? (
          <span className={`${styles.score} ${styles.scoreAbsent} mono`}>
            <span aria-hidden="true">{EM_DASH}</span>
            <span className="visually-hidden">{NOT_SCORED}</span>
          </span>
        ) : (
          <span className={`${styles.score} mono`}>{row.score}</span>
        )}
      </div>
      <div className={styles.metricTrack} aria-hidden="true">
        {/* No fill element at all for an unscored metric. A zero-width one would have to carry a
            band class, and `evalBand(0)` is `bad` — a red claim about a metric that was never
            scored. The track keeps the row's height either way. */}
        {row.band !== null && row.width !== null && (
          <div
            className={`${styles.metricFill} ${FILL_CLASS[row.band]}`}
            data-band={row.band}
            style={{ width: row.width }}
          />
        )}
      </div>
    </div>
  );
}

/** A literal map, never `styles[\`metricFill${band}\`]`: the CSS-Modules key guard matches
 *  `styles.name` textually, so a computed key is invisible to it and a typo ships as
 *  `class="undefined"` — a bar with no colour. The `EvalChips.tsx` rule, one surface over. */
const FILL_CLASS: Record<EvalBand, string> = {
  good: styles.metricFillGood,
  warn: styles.metricFillWarn,
  bad: styles.metricFillBad,
};

/** FR-ANL-05. A `<ul>` where the prototype had bare divs — this is a list of documents. */
function SourcesReferenced({ entries }: { entries: readonly TranscriptEntry[] }) {
  const labelId = useId();
  const sources = sourcesReferenced(entries);

  return (
    <div className={styles.sources}>
      <h2 className={styles.sectionLabel} id={labelId}>
        {SOURCES_LABEL}
      </h2>
      {sources.length === 0 ? (
        <div className={styles.sourcesEmpty}>{SOURCES_EMPTY}</div>
      ) : (
        <ul className={styles.list} aria-labelledby={labelId}>
          {sources.map((source) => (
            <SourceCard key={source.name} source={source} />
          ))}
        </ul>
      )}
    </div>
  );
}

function SourceCard({ source }: { source: SourceRow }) {
  return (
    <li className={`${styles.card} ${styles.sourceRow}`}>
      {/* The badge restates the filename's own extension, so it is decoration to a screen reader
          that is about to read the filename. */}
      <span className={`${styles.badge} mono`} aria-hidden="true">
        {source.type}
      </span>
      <div className={styles.sourceBody}>
        <div className={styles.sourceName}>{source.name}</div>
        <div className={`${styles.sourceHits} mono`}>{passageCount(source.passages)}</div>
      </div>
    </li>
  );
}
