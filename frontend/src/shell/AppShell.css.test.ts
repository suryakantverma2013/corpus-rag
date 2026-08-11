/**
 * Structural guards on `AppShell.module.css`, following the `styles/tokens.test.ts` pattern.
 *
 * These are drift guards, not behaviour tests — jsdom applies no external CSS, so the *effect*
 * of every rule below is verified in a real browser and formally by T-510. What they defend
 * against is a rule being deleted or "simplified" by someone who does not know why it is
 * shaped that way. Two of them (the containing-block guard and the CSS-Modules key guard)
 * catch failures that are invisible to every other check in the repo.
 */
import { describe, expect, it } from 'vitest';
import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const css = readSource('src/shell/AppShell.module.css');
const tsx = readSource('src/shell/AppShell.tsx');

/** Declarations only: the comments in these files *discuss* the rules they warn against
 *  ("do not add `outline: none`"), so negative assertions must not see them. */
const code = stripBlockComments(css);
const tsxCode = stripTsComments(tsx);

/** The four layout boxes. `.skipLink` is excluded from the per-block guards on purpose. */
const LAYOUT_BLOCKS = ['\n.root {', '\n.sidebar {', '\n.chat {', '\n.stats {'];

describe('§9 dimensions are tokens, never literals', () => {
  it('reads the three layout dimensions from tokens.css', () => {
    expect(code).toContain('var(--sidebar-w)');
    expect(code).toContain('var(--stats-w)');
    expect(code).toContain('var(--app-min-h)');
  });

  it('hard-codes none of them', () => {
    // The other half of the pair: `tokens.test.ts` asserts the tokens hold 264/272/640, this
    // asserts the shell goes through them. A literal here would let the two drift with
    // nothing failing.
    expect(code).not.toMatch(/\b264px\b/);
    expect(code).not.toMatch(/\b272px\b/);
    expect(code).not.toMatch(/\b640px\b/);
  });
});

describe('FR-LAY-01 geometry', () => {
  it('makes the root a full-viewport flex row that does not scroll', () => {
    const root = blockAfter(css, '\n.root {');
    expect(root).toMatch(/display:\s*flex/);
    expect(root).toMatch(/height:\s*100vh/);
    expect(root).toMatch(/min-height:\s*var\(--app-min-h\)/);
    // Each region owns its own overflow; the shell itself must never scroll.
    expect(root).toMatch(/overflow:\s*hidden/);
  });

  it('keeps the chat column able to shrink below its content', () => {
    // Without `min-width: 0` a flex item defaults to `min-width: auto` and refuses to shrink
    // past its content, so a long FR-HDR-01 title or a row of FR-CIT-01 chips pushes this
    // track past its share and squeezes the 272px stats panel off-screen.
    const chat = blockAfter(css, '\n.chat {');
    expect(chat).toMatch(/flex:\s*1/);
    expect(chat).toMatch(/min-width:\s*0/);
    expect(chat).toMatch(/flex-direction:\s*column/);
  });

  it('fixes the two side columns and gives them their borders', () => {
    const sidebar = blockAfter(css, '\n.sidebar {');
    expect(sidebar).toMatch(/flex-shrink:\s*0/);
    expect(sidebar).toMatch(/border-right:\s*1px solid var\(--line\)/);

    const stats = blockAfter(css, '\n.stats {');
    expect(stats).toMatch(/flex-shrink:\s*0/);
    expect(stats).toMatch(/border-left:\s*1px solid var\(--line\)/);
    expect(stats).toMatch(/overflow-y:\s*auto/);
  });
});

describe('FR-LAY-03 — no layout box may become a containing block', () => {
  it.each(LAYOUT_BLOCKS)('%s creates no containing block for position:fixed', (selector) => {
    // `transform`/`filter`/`backdrop-filter`/`perspective`/`will-change`/`contain` each make
    // an element the containing block for its `position: fixed` descendants. Add any of them
    // here and FR-CIT-03's hover card resolves its viewport coordinates against the wrong box
    // while FR-KBM-01's `inset: 0` modal covers the chat column instead of the screen — both
    // only in a real browser, since jsdom computes no layout, so nothing else in this repo
    // would catch it. (`overflow: hidden` is fine; it does not clip fixed descendants.)
    const block = stripBlockComments(blockAfter(css, selector));
    expect(block).not.toMatch(
      /\b(transform|filter|backdrop-filter|perspective|will-change|contain)\s*:/,
    );
  });
});

describe('inherited global rules are not re-declared or defeated', () => {
  it('never disables the focus ring (NFR-A11Y-02)', () => {
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    // R-59: the baseline is per-animation and lives in tokens.css; a component adding its own
    // block is precisely how the dotPulse mistake gets made in one file and not another.
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses no raw colour (NFR-VIS-02)', () => {
    expect(code).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });
});

describe('CSS Module wiring', () => {
  it('declares every class the component reads', () => {
    // This hole is specific to CSS Modules and is invisible three ways: `vite/client` types
    // the import as an index signature so `tsc` accepts any key; Vitest stubs the module with
    // a Proxy that answers *any* key with a truthy hashed string so every render test passes;
    // and in the browser a typo just renders `class="undefined"`, i.e. unstyled. T-503..T-508
    // should copy this guard.
    const declared = new Set([...css.matchAll(/^\.([A-Za-z][\w-]*)/gm)].map((m) => m[1]));
    const used = [...tsxCode.matchAll(/\bstyles\.([A-Za-z]\w*)/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(0);
    for (const name of used) {
      expect([...declared], `styles.${name} is not declared in AppShell.module.css`).toContain(
        name,
      );
    }
  });
});

describe('R-58(5) — the shell does not consume theme', () => {
  it('does not import or call the theme hook', () => {
    // The runtime counterpart is in AppShell.test.tsx (it renders outside the provider without
    // throwing). This one also catches an unused import, which that test would not.
    expect(tsxCode).not.toContain('useTheme');
    expect(tsxCode).not.toContain('ThemeContext');
  });
});
