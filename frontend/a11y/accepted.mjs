/**
 * T-614 — the enumerated set this pass measures against.
 *
 * **The assertion is "no NEW violation", never "no violations".** NFR-A11Y-06 accepts the §5.1
 * palette's contrast failures as recorded exceptions, because closing them means changing token
 * values and that is a visual redesign the design handoff has not authorised (R-59). So a run
 * that reported zero would be reporting a product that does not exist, and a harness asserting
 * zero would fail on every commit until someone deleted it.
 *
 * What it *can* catch is the thing ACCESSIBILITY.md rule 5 forbids — **a new colour pair below
 * threshold** — and any rule outside the two accepted ones. Both are regressions; neither is
 * visible to `vitest`, which renders in jsdom and computes no colour at all.
 */

/**
 * Rules whose violations are accepted wholesale, with the reason NFR-A11Y-06 gives.
 * A violation of any OTHER rule fails the run — that is the WCAG claim this file defends.
 */
export const ACCEPTED_RULES = new Map([
  [
    'color-contrast',
    'NFR-A11Y-06: the §5.1 palette is fixed by NFR-VIS-01/02; 12 of 28 pairs fail dark, 18 of 28 ' +
      'light. Accepted as enumerated exceptions — but only for the foregrounds below.',
  ],
  [
    'region',
    "best-practice, not WCAG: the shell's visually-hidden <h1> sits outside every landmark " +
      "(T-502 owns the document's only <h1>), and the citation tooltip is not in one either.",
  ],
]);

/**
 * The foregrounds NFR-A11Y-06 enumerates, keyed by the value axe reports.
 *
 * **Foreground rather than the (fg, bg) pair, and that is a deliberate narrowing.** The
 * backgrounds are composited — §8.63 measured `--accent` on `--accent-soft` over `--bubble` at
 * 3.56:1 — so a blended background matches no token exactly and pinning pairs would report a
 * *new* pair every time a surface's stacking changed. The foreground is not composited: it is
 * the token the enumeration actually names. A violation whose foreground is absent here is a
 * colour being used as text that NFR-A11Y-06 never sanctioned, which is exactly rule 5's case.
 *
 * `--muted` is deliberately NOT here: it measures ≈5.9:1 and passes, so if it ever appears in a
 * violation something has moved underneath it and the run should say so.
 */
export const ACCEPTED_FOREGROUNDS = new Map([
  ['#5c6377', '--muted2 (dark) — section labels, mono badges, the version tag'],
  ['#9aa0b4', '--muted2 (light) — same'],
  ['#7c86f8', '--accent (dark) — incl. the 4.31:1 citation chip, §8.63(5)'],
  ['#5b66e8', '--accent (light) — reaches 3.56:1 on composited surfaces'],
  ['#ffffff', '#fff on --accent — send glyph, brand mark, AI avatar'],
  ['#4ec3a6', '--eval-good (FR-EVL-03)'],
  ['#e8a34c', '--eval-warn (FR-EVL-03)'],
  ['#e86a8a', "--eval-bad (FR-EVL-03) — ui/ConfirmDialog's destructive button, 3.06:1 light"],
]);

/** axe reports `#rrggbb`; normalise so a case or shorthand difference is not a false new pair. */
export function normaliseColour(value) {
  if (typeof value !== 'string') return '';
  const hex = value.trim().toLowerCase();
  const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/.exec(hex);
  return short === null
    ? hex
    : `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
}
