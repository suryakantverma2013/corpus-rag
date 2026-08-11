/**
 * Structural guards on the chat-header stylesheets — the `AppShell.css.test.ts` /
 * `Sidebar.css.test.ts` pattern, including the CSS-Modules key guard T-502 requires every
 * component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every
 * rule below is verified in a real browser and formally by T-510. What they defend against is a
 * rule being deleted or "simplified" by someone who does not know why it is shaped that way.
 */
import { describe, expect, it } from 'vitest';
import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['ChatHeader', 'src/chat/ChatHeader.module.css', 'src/chat/ChatHeader.tsx'],
  ['ThemeToggle', 'src/chat/ThemeToggle.module.css', 'src/chat/ThemeToggle.tsx'],
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
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour (NFR-VIS-02)', () => {
    expect(code).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });

  it('reads the pill radius from the token, never the literal 20px', () => {
    // §9/§5.1 give `--radius-pill: 20px` and name these two elements in the token's own
    // comment. A literal here and a changed token there is a silent divergence.
    expect(code).toContain('var(--radius-pill)');
    expect(code).not.toMatch(/border-radius:\s*20px/);
  });
});

describe('ChatHeader — FR-HDR-01/02 declarations', () => {
  const css = readSource('src/chat/ChatHeader.module.css');

  it('gives the header the prototype’s box', () => {
    const header = stripBlockComments(blockAfter(css, '\n.header {'));
    expect(header).toMatch(/padding:\s*14px 22px/);
    expect(header).toMatch(/border-bottom:\s*1px solid var\(--line\)/);
    expect(header).toMatch(/background:\s*var\(--panel\)/);
    expect(header).toMatch(/gap:\s*12px/);
  });

  it('resets the UA heading styling the prototype’s <div> never had', () => {
    // An <h2> arrives with a UA margin and its own font-size/weight; the prototype's element
    // inherits the 14px root font and overrides only size and weight. jsdom cannot reach this
    // — it is the same class of defect as T-503's row <button>, found only in a browser.
    const title = stripBlockComments(blockAfter(css, '\n.title {'));
    expect(title).toMatch(/margin:\s*0/);
    expect(title).toMatch(/font-size:\s*15px/);
    expect(title).toMatch(/font-weight:\s*600/);
    expect(title).toMatch(/color:\s*inherit/);
  });

  it('ellipsises the title AND lets it shrink below its content', () => {
    // `overflow: hidden` is not redundant beside `text-overflow`: it is also what zeroes this
    // flex item's automatic minimum size. Without it a long title pushes the Grounded pill and
    // the FR-HDR-03 toggle out of the header instead of ellipsising — T-502's `.chat`
    // `min-width: 0` protects the column, not this row.
    const title = stripBlockComments(blockAfter(css, '\n.title {'));
    expect(title).toMatch(/white-space:\s*nowrap/);
    expect(title).toMatch(/overflow:\s*hidden/);
    expect(title).toMatch(/text-overflow:\s*ellipsis/);
  });

  it('keeps the pill and the actions group from being squeezed by a long title', () => {
    expect(stripBlockComments(blockAfter(css, '\n.grounded {'))).toMatch(/flex-shrink:\s*0/);
    const actions = stripBlockComments(blockAfter(css, '\n.actions {'));
    expect(actions).toMatch(/flex-shrink:\s*0/);
    // FR-HDR-03's "right-aligned".
    expect(actions).toMatch(/margin-left:\s*auto/);
  });

  it('gives the pill FR-HDR-02’s literal values', () => {
    const grounded = stripBlockComments(blockAfter(css, '\n.grounded {'));
    expect(grounded).toMatch(/font-size:\s*11px/);
    expect(grounded).toMatch(/font-weight:\s*600/);
    expect(grounded).toMatch(/color:\s*var\(--accent\)/);
    expect(grounded).toMatch(/background:\s*var\(--accent-soft\)/);

    const dot = stripBlockComments(blockAfter(css, '\n.groundedDot {'));
    expect(dot).toMatch(/width:\s*6px/);
    expect(dot).toMatch(/height:\s*6px/);
    expect(dot).toMatch(/background:\s*var\(--accent\)/);
  });
});

describe('ThemeToggle — FR-HDR-03 declarations', () => {
  const css = readSource('src/chat/ThemeToggle.module.css');

  it('gives the group the bordered pill', () => {
    const group = stripBlockComments(blockAfter(css, '\n.group {'));
    expect(group).toMatch(/border:\s*1px solid var\(--line\)/);
    expect(group).toMatch(/overflow:\s*hidden/);
    expect(group).toMatch(/font-size:\s*11px/);
    expect(group).toMatch(/font-weight:\s*600/);
  });

  it('sizes both segments at the prototype’s 4px/11px', () => {
    expect(stripBlockComments(blockAfter(css, '\n.segment {'))).toMatch(/padding:\s*4px 11px/);
  });

  it('makes the segment <button>s inherit the group’s font', () => {
    // A <button> inherits neither font-family, nor font-size, nor font-weight. Measured in
    // T-503 as 13.33px/black against the prototype's own value — and here the size and weight
    // are declared once on `.group`, so without these the segments render at the UA default.
    const seg = stripBlockComments(blockAfter(css, '\n.segment {'));
    expect(seg).toMatch(/font-family:\s*inherit/);
    expect(seg).toMatch(/font-size:\s*inherit/);
    expect(seg).toMatch(/font-weight:\s*inherit/);
  });

  it('pulls the focus ring inside the border box rather than removing it (NFR-A11Y-02)', () => {
    // `.group` sets `overflow: hidden`, which clips a descendant's outline — so at the global
    // `+2px` offset the ring is invisible on both segments, in a real browser only. Overriding
    // the offset token is the case `tokens.css` documents it for; `outline: none` is not.
    // Asserted against COMMENT-STRIPPED source: the prose above the rule discusses it, and a
    // raw-text assertion would be satisfied by the comment alone (the T-503 mutation finding).
    const seg = stripBlockComments(blockAfter(css, '\n.segment {'));
    expect(seg).toMatch(/--focus-ring-offset:\s*-2px/);
  });

  it('colours the active and inactive segments per FR-HDR-03', () => {
    const active = stripBlockComments(blockAfter(css, '\n.segmentActive {'));
    expect(active).toMatch(/background:\s*var\(--panel2\)/);
    expect(active).toMatch(/color:\s*var\(--text\)/);
    expect(stripBlockComments(blockAfter(css, '\n.segment {'))).toMatch(/color:\s*var\(--muted2\)/);
  });
});

describe('R-58(5) — the toggle reads the theme itself', () => {
  it('calls useTheme rather than taking a prop', () => {
    // Binding: threading `theme` through the shell would set the precedent that drags the other
    // nine FR-CST-01 fields through it in T-505..T-508.
    const toggle = stripTsComments(readSource('src/chat/ThemeToggle.tsx'));
    expect(toggle).toContain('useTheme()');
    const chatHeader = stripTsComments(readSource('src/chat/ChatHeader.tsx'));
    expect(chatHeader).not.toContain('useTheme');
    expect(chatHeader).not.toContain('theme');
  });
});
