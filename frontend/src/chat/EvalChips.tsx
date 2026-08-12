/**
 * FR-EVL-02 / FR-EVL-03 — the per-message DeepEval chip row.
 *
 * Transcribed from `RAG Chatbot.dc.html` lines 112–119. The prototype's sample data carries four
 * metrics; **the wire carries exactly two** (R-50(1)) — Contextual Precision and Contextual Recall
 * are reference-based, a live turn has no reference answer, and they belong to the T-312 offline
 * harness. `evalChips` is where that list lives; this file only paints it.
 *
 * The numeral beside the dot is **load-bearing, not decoration**: NFR-A11Y-06 accepts the
 * palette's contrast failures — all three FR-EVL-03 hues fail as text in the light theme — on the
 * condition that colour is never the sole carrier of information. Dropping the number to leave a
 * coloured dot would break that condition.
 */
import styles from './EvalChips.module.css';
import { evalChips } from './messages';
import type { Evaluation } from '../api';

/** §9's literal, and normative: FR-EVL-02 names the native tooltip explicitly. */
const CHIP_TOOLTIP = 'DeepEval metric';

export interface EvalChipsProps {
  evaluation: Evaluation | null | undefined;
}

export function EvalChips({ evaluation }: EvalChipsProps) {
  const chips = evalChips(evaluation);
  // Nothing at all until the job lands — and possibly for ever, which R-50(3) makes a correct
  // end state rather than an error. FR-MSG-04's content column has an 8px gap, so an empty
  // element here would be visible.
  if (chips.length === 0) return null;

  return (
    <div className={styles.row}>
      {chips.map((chip) => (
        <span key={chip.key} className={`${styles.chip} mono`} title={CHIP_TOOLTIP}>
          {/* `data-band` rather than a class name for the test seam: Vitest stubs CSS Modules
              with a Proxy answering ANY key with a truthy string, so asserting on
              `styles.dotGood` would pass against a typo. The class still carries the colour. */}
          <span
            className={`${styles.dot} ${DOT_CLASS[chip.band]}`}
            data-band={chip.band}
            aria-hidden="true"
          />
          {`${chip.label} ${chip.score}`}
        </span>
      ))}
    </div>
  );
}

/** A literal map rather than `styles[`dot${band}`]`: the CSS-Modules key guard matches
 *  `styles.name` textually, so a computed key would be invisible to it — and a typo there ships
 *  as `class="undefined"`, i.e. a dot with no colour at all. */
const DOT_CLASS: Record<'good' | 'warn' | 'bad', string> = {
  good: styles.dotGood,
  warn: styles.dotWarn,
  bad: styles.dotBad,
};
