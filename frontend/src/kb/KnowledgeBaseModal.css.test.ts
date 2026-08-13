/**
 * Structural guards on the §4.7 stylesheets — the `StatsPanel.css.test.ts` pattern, including the
 * CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every rule
 * below is verified in a real browser and formally by T-510. What they defend against is a rule
 * being deleted or "simplified" by someone who does not know why it is shaped that way — which
 * matters more here than anywhere else, because R-66 means the prototype's KB modal sits inside an
 * `<sc-if>` and **has never painted**, so there is no screenshot anyone could check a change
 * against.
 */
import { describe, expect, it } from 'vitest';

import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['KnowledgeBaseModal', 'src/kb/KnowledgeBaseModal.module.css', 'src/kb/KnowledgeBaseModal.tsx'],
  ['DocumentRow', 'src/kb/DocumentRow.module.css', 'src/kb/DocumentRow.tsx'],
  ['DropZone', 'src/kb/DropZone.module.css', 'src/kb/DropZone.tsx'],
] as const;

describe.each(SHEETS)('%s stylesheet', (name, cssPath, tsxPath) => {
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
    for (const key of used) {
      expect([...declared], `styles.${key} is not declared in ${cssPath}`).toContain(key);
    }
  });

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    // The one sanctioned `outline: none` in the codebase is the composer's borderless input.
    // Nothing here has that excuse — and this surface is almost entirely controls.
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('names no keyframe (R-69 / T-505)', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe the module does not declare, so
    // any `animation:` here compiles to a hashed name matching no @keyframes and silently never
    // runs. FR-KBM-04's in-progress indicator therefore uses the GLOBAL `.animate-dot-pulse`.
    expect(code).not.toMatch(/animation(-name)?\s*:/);
    expect(code).not.toContain('@keyframes');
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour (NFR-VIS-02)', () => {
    // One exemption, and it is narrow: `#fff` on the accent-filled FR-KBM-06 button, which is the
    // prototype's own literal and the same recorded NFR-A11Y-06 exception as the composer's Send
    // glyph. Asserted by name rather than by loosening the pattern.
    const hexes = [...code.matchAll(/#[0-9a-f]{3,8}\b/gi)].map((m) => m[0]);
    expect(hexes).toEqual(name === 'KnowledgeBaseModal' ? ['#fff'] : []);
    expect(code).not.toMatch(/\brgba?\(/);
  });

  it('restates no scrollbar styling of its own (NFR-USE-05, R-60)', () => {
    // §8.41(3) names T-508: `--scrollbar-thumb` is a token and the `@supports` gate is global.
    expect(code).not.toContain('scrollbar');
  });

  it('creates no containing block for the fixed-position overlays', () => {
    // `transform`/`filter`/`backdrop-filter`/`perspective`/`will-change`/`contain` each make an
    // element the containing block for its `position: fixed` descendants — and the FR-KBM-07
    // confirmation nests a `position: fixed; inset: 0` overlay INSIDE this panel. A failure only
    // a real browser could show. `filter` on the footer's button is the prototype's hover
    // brightness and is not a layout box, so it is matched as a property declaration on a rule
    // that also lays something out — hence the narrow list below excludes `:hover`.
    const layoutRules = code
      .split('}')
      .filter((rule) => !rule.includes(':hover'))
      .join('}');
    // Anchored on the start of a declaration rather than `\b`, which fires after a hyphen and so
    // matches `text-transform` — a false positive the earlier copies of this guard only escaped
    // by not having one. The property must be the whole property name.
    expect(layoutRules).not.toMatch(
      /(^|[\s;{])(transform|backdrop-filter|perspective|will-change|contain)\s*:/m,
    );
  });
});

describe('KnowledgeBaseModal — Dialog owns the modal treatment', () => {
  const code = stripBlockComments(readSource('src/kb/KnowledgeBaseModal.module.css'));

  it('restates none of Dialog’s overlay or panel rule', () => {
    // `panelClassName` supplies width and height only. Re-declaring the padding would double it,
    // and a second `position: fixed` overlay or `--radius-modal` here would pin a second modal
    // treatment beside the shared one — which is how the four modal surfaces stop matching.
    expect(code).not.toMatch(/position:\s*fixed/);
    expect(code).not.toContain('--overlay');
    expect(code).not.toContain('--radius-modal');
    expect(code).not.toContain('--shadow');
    expect(code).not.toMatch(/padding:\s*22px/);
  });

  it('gives the panel FR-KBM-01’s three values and nothing more', () => {
    const panel = stripBlockComments(blockAfter(readSource(SHEETS[0][1]), '\n.panel {'));
    expect(panel).toMatch(/width:\s*520px/);
    expect(panel).toMatch(/max-height:\s*78vh/);
    // The scroll container FR-KBM-01 names — and the reason the notice slot is not per-row.
    expect(panel).toMatch(/overflow-y:\s*auto/);
  });

  it('resets the UA margin on every element the prototype had as a <div>', () => {
    // The subtitle is a <p>, the section labels <h3> and the list a <ul>: each arrives with a UA
    // margin (and the list with markers and 40px of padding) that the prototype's <div>s had not.
    // The T-503/T-504/T-507 trap, in a fourth place.
    expect(blockAfter(readSource(SHEETS[0][1]), '\n.subtitle {')).toMatch(/margin:\s*4px 0 0/);
    expect(blockAfter(readSource(SHEETS[0][1]), '\n.sectionLabel {')).toMatch(/margin:\s*18px 0 0/);
    const list = blockAfter(readSource(SHEETS[0][1]), '\n.list {');
    expect(list).toMatch(/list-style:\s*none/);
    expect(list).toMatch(/padding:\s*0/);
  });

  it('does not paint the R-71(1) notice as an error', () => {
    // `--eval-bad` is FR-KBM-04's `Failed` label. Reusing it here would make "this action is
    // unavailable for two seconds" look identical to "this document is broken".
    const notice = stripBlockComments(blockAfter(readSource(SHEETS[0][1]), '\n.paused,'));
    expect(notice).not.toContain('--eval-');
    expect(notice).toContain('var(--muted)');
  });
});

describe('DocumentRow — FR-KBM-04 and FR-KBM-07', () => {
  const sheet = readSource('src/kb/DocumentRow.module.css');
  const code = stripBlockComments(sheet);
  const tsx = stripTsComments(readSource('src/kb/DocumentRow.tsx'));

  it('keeps the prototype’s row box', () => {
    const row = stripBlockComments(blockAfter(sheet, '\n.row {'));
    expect(row).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(row).toMatch(/border-radius:\s*var\(--radius-card\)/);
    expect(row).toMatch(/padding:\s*10px 12px/);
    expect(row).toMatch(/background:\s*var\(--panel2\)/);
    expect(row).toMatch(/gap:\s*10px/);
  });

  it('sizes the badge at the prototype’s 34px, not the 32px used elsewhere', () => {
    const badge = stripBlockComments(blockAfter(sheet, '\n.badge {'));
    expect(badge).toMatch(/width:\s*34px/);
    expect(badge).toMatch(/font-size:\s*9\.5px/);
    expect(badge).toMatch(/flex-shrink:\s*0/);
  });

  it('ellipsises the filename and zeroes the flex item’s automatic minimum size', () => {
    // Without `min-width: 0` the item refuses to shrink below its content, so a long filename
    // widens the row and pushes the status label out of the panel instead of truncating.
    expect(blockAfter(sheet, '\n.body {')).toMatch(/min-width:\s*0/);
    const nameRule = blockAfter(sheet, '\n.name {');
    expect(nameRule).toMatch(/white-space:\s*nowrap/);
    expect(nameRule).toMatch(/text-overflow:\s*ellipsis/);
  });

  it('reveals the actions by OPACITY, and to :focus-within as well as :hover', () => {
    // `display: none` / `visibility: hidden` look identical to a mouse user and make the control
    // pointer-only — the exact NFR-A11Y-04 defect T-503 found in the sidebar. Asserted against
    // comment-stripped source, because these files discuss the rules they enforce and a raw
    // `toContain` once passed on the prose above a deleted selector.
    expect(code).toContain(':focus-within');
    const actions = stripBlockComments(blockAfter(sheet, '\n.actions {'));
    expect(actions).toMatch(/opacity:\s*0/);
    expect(actions).not.toMatch(/display:\s*none/);
    expect(actions).not.toMatch(/visibility:\s*hidden/);
  });

  it('maps status to a class literally, never by a computed key', () => {
    // The key guard matches `styles.name` textually, so `styles[label]` is invisible to it and a
    // typo ships as class="undefined" — a status label with no colour at all.
    expect(tsx).not.toMatch(/styles\[/);
  });

  it('uses the global dot-pulse utility rather than its own animation', () => {
    expect(tsx).toContain('animate-dot-pulse');
  });
});

describe('DropZone — FR-KBM-05 and R-71(3)', () => {
  const sheet = readSource('src/kb/DropZone.module.css');
  const tsx = stripTsComments(readSource('src/kb/DropZone.tsx'));

  it('keeps the prototype’s dashed box', () => {
    const zone = stripBlockComments(blockAfter(sheet, '\n.zone {'));
    expect(zone).toMatch(/border:\s*1\.5px dashed var\(--line\)/);
    expect(zone).toMatch(/border-radius:\s*var\(--radius-pop\)/);
    expect(zone).toMatch(/padding:\s*26px/);
    expect(zone).toMatch(/font-size:\s*12\.5px/);
    // A <button> inherits neither, where the prototype's <div> inherited both (the T-503 finding).
    expect(zone).toMatch(/font-family:\s*inherit/);
    expect(zone).toMatch(/box-sizing:\s*border-box/);
  });

  it('moves BOTH properties on hover, as the prototype’s style-hover does', () => {
    // The caption inherits `color`, so restating its colour would freeze it and lose half the
    // effect — the prototype lifts the whole block from `--muted2` to `--muted`.
    const hover = stripBlockComments(blockAfter(sheet, '\n.zone:hover:not(:disabled),'));
    expect(hover).toMatch(/border-color:\s*var\(--accent\)/);
    expect(hover).toMatch(/color:\s*var\(--muted\)/);
    expect(stripBlockComments(blockAfter(sheet, '\n.caption {'))).not.toMatch(/color:/);
  });

  it('pulls the segment focus ring inside the pill (the T-504 finding)', () => {
    // `overflow: hidden` on the pill clips a descendant's outline, and `getComputedStyle` reports
    // the ring either way — only a pixel count shows it. The negative offset is the fix that
    // token's own comment exists for; `outline: none` is prohibited and is not used.
    expect(stripBlockComments(blockAfter(sheet, '\n.segments {'))).toMatch(/overflow:\s*hidden/);
    expect(stripBlockComments(blockAfter(sheet, '\n.segment {'))).toMatch(
      /--focus-ring-offset:\s*-2px/,
    );
  });

  it('carries the active segment in aria-pressed, not in colour alone (NFR-A11Y-06)', () => {
    expect(tsx).toContain('aria-pressed');
  });

  it('renders a real file input beside the drag handlers (NFR-A11Y-04)', () => {
    // Drag-and-drop is pointer-only by nature; the board makes the keyboard alternative normative.
    expect(tsx).toContain('type="file"');
    expect(tsx).toContain('onDrop');
  });
});
