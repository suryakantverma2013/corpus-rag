/**
 * Structural guards on the §4.6 stylesheet — the `Composer.css.test.ts` pattern, including the
 * CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every rule
 * below is verified in a real browser and formally by T-510. What they defend against is a rule
 * being deleted or "simplified" by someone who does not know why it is shaped that way — which
 * matters more here than usual, because under R-66 the prototype's own stats column has never
 * rendered, so there is no screenshot anyone could check a change against.
 */
import { describe, expect, it } from 'vitest';

import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['StatsPanel', 'src/stats/StatsPanel.module.css', 'src/stats/StatsPanel.tsx'],
] as const;

describe.each(SHEETS)('%s stylesheet', (_name, cssPath, tsxPath) => {
  const css = readSource(cssPath);
  const code = stripBlockComments(css);
  const tsxCode = stripTsComments(readSource(tsxPath));

  it('declares every class the component reads', () => {
    // The CSS-Modules hole T-502 documented: `vite/client` types the import as an index
    // signature and Vitest stubs it with a Proxy answering any key, so `styles.typo` passes
    // `tsc`, passes every render test, and ships as class="undefined".
    const declared = new Set([...css.matchAll(/^\.([A-Za-z][\w-]*)/gm)].map((m) => m[1]));
    const used = [...tsxCode.matchAll(/\bstyles\.([A-Za-z]\w*)/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(0);
    for (const name of used) {
      expect([...declared], `styles.${name} is not declared in ${cssPath}`).toContain(name);
    }
  });

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    // The one sanctioned `outline: none` in the codebase is the composer's borderless input,
    // whose indicator moves to the bar's accent border. Nothing here has that excuse — and the
    // panel is non-interactive today, so a ring appearing at all would be the surprise.
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('names no keyframe (R-69 / T-505 finding)', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe the module does not declare,
    // so any `animation:` here compiles to a hashed name matching no @keyframes and silently
    // never runs. Both bars use `transition`, which is unaffected — but the guard stays so a
    // later fade-in reaches for the global `.animate-fade-up` utility instead.
    expect(code).not.toMatch(/animation(-name)?\s*:/);
    expect(code).not.toContain('@keyframes');
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    // `--motion-bar` is zeroed globally under `prefers-reduced-motion`; a literal `.5s` here
    // would keep both meters animating for a user who asked them not to.
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour (NFR-VIS-02)', () => {
    // No `#fff` exemption: unlike the composer's Send glyph and T-505's AI avatar, nothing in
    // this panel sits on an accent fill.
    expect(code).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });

  it('restates no scrollbar styling of its own (NFR-USE-05, R-60)', () => {
    expect(code).not.toContain('scrollbar');
  });
});

describe('StatsPanel — the shell owns the column, this sheet owns the cards', () => {
  const code = stripBlockComments(readSource('src/stats/StatsPanel.module.css'));

  it('restates none of AppShell’s `.stats` rule', () => {
    // Duplicating any of it would double the padding, re-scroll the column inside itself, or
    // pin a second width against `--stats-w`. `AppShellProps.stats` says whose these are.
    expect(code).not.toContain('--stats-w');
    expect(code).not.toMatch(/border-left/);
    expect(code).not.toMatch(/overflow-y/);
    expect(code).not.toMatch(/padding:\s*18px/);
    expect(code).not.toMatch(/gap:\s*14px/);
  });

  it('creates no containing block for the fixed-position overlays', () => {
    // `transform`/`filter`/`backdrop-filter`/`perspective`/`will-change`/`contain` each make an
    // element the containing block for its `position: fixed` descendants — the AppShell rule,
    // and a failure only a real browser could show.
    expect(code).not.toMatch(
      /\b(transform|filter|backdrop-filter|perspective|will-change|contain)\s*:/,
    );
  });
});

describe('StatsPanel — FR-ANL-01..05 declarations', () => {
  const css = readSource('src/stats/StatsPanel.module.css');
  const rule = (header: string) => stripBlockComments(blockAfter(css, header));

  it('keeps the group labels and the card labels apart', () => {
    // The prototype tracks the two group labels at .12em and leaves the five card labels
    // untracked. Merging them into one class is the easy fidelity mistake, so the absence is
    // asserted as firmly as the presence.
    const section = rule('\n.sectionLabel {');
    expect(section).toMatch(/letter-spacing:\s*0\.12em/);
    expect(section).toMatch(/font-size:\s*10\.5px/);
    expect(section).toMatch(/color:\s*var\(--muted2\)/);

    const cardLabel = rule('\n.cardLabel {');
    expect(cardLabel).not.toMatch(/letter-spacing/);
    expect(cardLabel).toMatch(/font-size:\s*10\.5px/);
  });

  it('resets the UA heading margins on both label classes', () => {
    // These are <h2>/<h3> for NFR-A11Y-03's outline where the prototype had bare <div>s, and a
    // UA `margin: 1em 0` would push every card apart. The T-503/T-504/T-506 trap.
    expect(rule('\n.sectionLabel {')).toMatch(/margin:\s*0/);
    expect(rule('\n.cardLabel {')).toMatch(/margin:\s*0/);
  });

  it('gives the shared card box no padding of its own', () => {
    // `.card` and `.sessionCard` are both single-class selectors, so a padding on both would be
    // a specificity tie broken by stylesheet order — the defect T-505 found in the typing
    // avatar. Each usage owns its padding and there is no tie to lose.
    const card = rule('\n.card {');
    expect(card).toMatch(/background:\s*var\(--panel2\)/);
    expect(card).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(card).toMatch(/border-radius:\s*var\(--radius-card\)/);
    expect(card).not.toMatch(/padding/);
    expect(rule('\n.sessionCard {')).toMatch(/padding:\s*10px 12px/);
    expect(rule('\n.panelCard {')).toMatch(/padding:\s*12px/);
  });

  it('lays the FR-ANL-01 grid out in two equal columns', () => {
    const grid = rule('\n.sessionGrid {');
    expect(grid).toMatch(/display:\s*grid/);
    expect(grid).toMatch(/grid-template-columns:\s*1fr 1fr/);
    expect(grid).toMatch(/gap:\s*8px/);
    expect(rule('\n.statValue {')).toMatch(/font-size:\s*16px/);
  });

  it('aligns both card headers on the baseline', () => {
    // `align-items: baseline` is what lines the 10.5px label up with the 11px mono value; the
    // flex default would stretch them and shift the value by a pixel or two.
    const header = rule('\n.cardHeader {');
    expect(header).toMatch(/display:\s*flex/);
    expect(header).toMatch(/align-items:\s*baseline/);
    expect(header).toMatch(/justify-content:\s*space-between/);
  });

  it('gives the FR-ANL-03 meter its 6px track and accent fill', () => {
    const track = rule('\n.track {');
    expect(track).toMatch(/height:\s*6px/);
    expect(track).toMatch(/border-radius:\s*3px/);
    expect(track).toMatch(/background:\s*var\(--line\)/);
    // Without this a rounded fill at 100% squares off the track's ends.
    expect(track).toMatch(/overflow:\s*hidden/);

    const fill = rule('\n.fill {');
    expect(fill).toMatch(/height:\s*100%/);
    expect(fill).toMatch(/background:\s*var\(--accent\)/);
    expect(fill).toMatch(/transition:\s*width var\(--motion-bar\) ease/);
  });

  it('gives the FR-ANL-04 bars their 4px track and the FR-EVL-03 hues', () => {
    const track = rule('\n.metricTrack {');
    expect(track).toMatch(/height:\s*4px/);
    expect(track).toMatch(/border-radius:\s*2px/);
    expect(track).toMatch(/overflow:\s*hidden/);
    expect(rule('\n.metricFill {')).toMatch(/transition:\s*width var\(--motion-bar\) ease/);
    expect(rule('\n.metricFillGood {')).toMatch(/background:\s*var\(--eval-good\)/);
    expect(rule('\n.metricFillWarn {')).toMatch(/background:\s*var\(--eval-warn\)/);
    expect(rule('\n.metricFillBad {')).toMatch(/background:\s*var\(--eval-bad\)/);
  });

  it('resets the <ul> the prototype never had', () => {
    // A UA stylesheet supplies markers and 40px of inline padding; the prototype's bare <div>s
    // had neither, and this is a <ul> only because NFR-A11Y-03 wants the list semantics.
    const list = rule('\n.list {');
    expect(list).toMatch(/list-style:\s*none/);
    expect(list).toMatch(/margin:\s*0/);
    expect(list).toMatch(/padding:\s*0/);
    // The 8px rhythm moves onto the <ul>, because the rows are no longer `.sources`' children.
    expect(list).toMatch(/gap:\s*8px/);
  });

  it('sizes the FR-ANL-05 badge as a fixed 32px column', () => {
    const badge = rule('\n.badge {');
    expect(badge).toMatch(/width:\s*32px/);
    expect(badge).toMatch(/text-align:\s*center/);
    expect(badge).toMatch(/flex-shrink:\s*0/);
    expect(badge).toMatch(/font-size:\s*9\.5px/);
    expect(badge).toMatch(/background:\s*var\(--accent-soft\)/);
    expect(badge).toMatch(/border-radius:\s*var\(--radius-chip\)/);
  });

  it('ellipsises the filename and zeroes its automatic minimum size', () => {
    // Without `min-width: 0` the flex item refuses to shrink below its content, so a long
    // filename widens the row instead of ellipsising and pushes out of the 272px column.
    expect(rule('\n.sourceBody {')).toMatch(/min-width:\s*0/);
    const name = rule('\n.sourceName {');
    expect(name).toMatch(/white-space:\s*nowrap/);
    expect(name).toMatch(/overflow:\s*hidden/);
    expect(name).toMatch(/text-overflow:\s*ellipsis/);
  });
});

describe('StatsPanel — component invariants', () => {
  const code = stripTsComments(readSource('src/stats/StatsPanel.tsx'));
  const stats = stripTsComments(readSource('src/stats/stats.ts'));

  it('returns a fragment, not a wrapper element', () => {
    // AppShell's `.stats` owns the 14px gap, so the six children below must be the panel's own
    // flex items. A wrapper would make them one item and swallow every gap — and jsdom, which
    // computes no layout, would never notice.
    expect(code).toMatch(/return \(\s*<>/);
  });

  it('maps bands to classes literally, never by a computed key', () => {
    // The CSS-Modules key guard matches `styles.name` textually, so `styles[`x${band}`]` is
    // invisible to it and a typo ships as class="undefined" — a bar with no colour.
    expect(code).not.toMatch(/styles\[/);
  });

  it('puts nothing but computed geometry in an inline style', () => {
    expect([...code.matchAll(/style=\{\{\s*([A-Za-z]+)/g)].map((m) => m[1])).toEqual([
      'width',
      'width',
    ]);
  });

  it('hides both bars from assistive technology and declares no live region', () => {
    // The bars restate text rendered beside them. A polite region anywhere in this subtree would
    // read the FR-ANL-01 clock aloud once a second, for ever.
    expect([...code.matchAll(/aria-hidden="true"/g)]).toHaveLength(4);
    // The fifth is conditional — the CONTEXT WINDOW card's em dash before its first read
    // (T-513's `usage: null`), hidden only while the caption is saying the same thing in words.
    expect(code).toMatch(/aria-hidden=\{usage === null \? 'true' : undefined\}/);
    expect(code).not.toMatch(/aria-live|role="(status|alert)"/);
  });

  it('owns no <h1> — T-502 has the document’s only one', () => {
    expect(code).not.toMatch(/<h1[\s>]/);
  });

  it('derives the FR-EVL-03 band rather than re-deciding the thresholds', () => {
    // One definition of good/warn/bad, shared with the FR-EVL-02 chips.
    expect(stats).toContain("from '../chat/messages'");
    expect(stats).toMatch(/evalBand\(/);
    expect(stats).not.toMatch(/>=\s*0\.9\b|>=\s*0\.8\b/);
  });

  it('hard-codes no budget literal (R-51(2))', () => {
    // `used` and `limit` are the server's; a constant here would drift the meter away from the
    // composer's FR-STA-04 projection the moment an operator changed the window.
    expect(stats).not.toMatch(/\b10400\b|\b10_400\b|\b1500\b|\b1_500\b/);
    expect(code).not.toMatch(/\b10400\b|\b10_400\b/);
  });
});
