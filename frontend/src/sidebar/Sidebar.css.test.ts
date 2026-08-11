/**
 * Structural guards on the sidebar stylesheets — the `AppShell.css.test.ts` pattern, including
 * the CSS-Modules key guard T-502 requires every component task to copy.
 */
import { describe, expect, it } from 'vitest';
import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['Sidebar', 'src/sidebar/Sidebar.module.css', 'src/sidebar/Sidebar.tsx'],
  [
    'ConversationList',
    'src/sidebar/ConversationList.module.css',
    'src/sidebar/ConversationList.tsx',
  ],
  ['Dialog', 'src/ui/Dialog.module.css', 'src/ui/Dialog.tsx'],
  ['ConfirmDialog', 'src/ui/ConfirmDialog.module.css', 'src/ui/ConfirmDialog.tsx'],
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
    // R-59: the baseline is per-animation and lives in tokens.css. Every duration below must
    // be a --motion-* token, or reduced motion cannot reach it.
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour, apart from the prototype’s own #fff (NFR-VIS-02)', () => {
    // `#fff` on the accent brand mark is the prototype's literal and a recorded NFR-A11Y-06
    // exception (3.17:1 in dark); R-58 logged it as a palette decision, not an implementation
    // one. Nothing else may be a raw colour.
    expect(code.replace(/#fff\b/g, '')).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });
});

describe('ConversationList — FR-SBR-07 without disturbing the row (NFR-VIS-01)', () => {
  const css = readSource('src/sidebar/ConversationList.module.css');

  it('reveals the overflow control by opacity, never by removing it', () => {
    // THE assertion for this requirement. `display: none` / `visibility: hidden` look
    // identical to a mouse user and make the affordance pointer-only — the exact defect
    // NFR-A11Y-04 names — while `opacity` keeps it in the DOM, in the tab order, and out of
    // layout, so the row's default appearance is unchanged as FR-SBR-07 requires.
    const button = stripBlockComments(blockAfter(css, '\n.menuButton {'));
    expect(button).toMatch(/opacity:\s*0/);
    expect(button).toMatch(/position:\s*absolute/);
    expect(button).not.toMatch(/display:\s*none/);
    expect(button).not.toMatch(/visibility:\s*hidden/);
  });

  it('reveals it on focus-within as well as hover (NFR-A11Y-04)', () => {
    // Hover alone leaves a sighted keyboard user tabbing onto an invisible control.
    //
    // Asserted against the COMMENT-STRIPPED source, and that is the whole point: the first
    // version of this guard read the raw file, and the prose above the rule ("`:focus-within`
    // on the row is therefore load-bearing") satisfied it — so deleting the actual selector
    // left the test green. Mutation testing found it. Negative and existence assertions on
    // these files must never see the comments that discuss them.
    const stripped = stripBlockComments(css);
    expect(stripped).toContain('.row:focus-within .menuButton');
    expect(stripped).toContain('.row:hover .menuButton');
  });

  it('positions the menu fixed, because the list is a scroll container', () => {
    // `.list` sets `overflow-y: auto`, which would clip an absolutely positioned popover —
    // worst on the bottom row, and invisible in jsdom.
    expect(stripBlockComments(blockAfter(css, '\n.menu {'))).toMatch(/position:\s*fixed/);
    expect(stripBlockComments(blockAfter(css, '\n.list {'))).toMatch(/overflow-y:\s*auto/);
  });

  it('keeps the menu width in step with the clamp arithmetic in the component', () => {
    // The width is duplicated in TSX because the viewport clamp needs it in JS. If they drift,
    // the menu is right-aligned to the wrong edge and only a human notices.
    const cssWidth = /\n\.menu \{[^}]*width:\s*(\d+)px/.exec(css)?.[1];
    const tsWidth = /MENU_WIDTH = (\d+)/.exec(readSource('src/sidebar/ConversationList.tsx'))?.[1];
    expect(cssWidth).toBeDefined();
    expect(tsWidth).toBe(cssWidth);
  });

  it('sizes the menu border-box, or that width is not the width', () => {
    // Measured: under the content-box default the declared 148 rendered as 158 (padding +
    // border) and the menu sat 10px past its trigger's right edge. The guard above compares
    // two numbers that would then describe different things.
    expect(stripBlockComments(blockAfter(css, '\n.menu {'))).toMatch(/box-sizing:\s*border-box/);
  });

  it('makes the row button inherit font and colour from the page', () => {
    // A <button> inherits neither `font-size` nor `color`. Measured against the prototype's
    // <div>: 13.33px/black instead of 14px/--text. Invisible today only because `.title` and
    // `.meta` set both themselves, and live the moment anyone adds text directly to a row —
    // which is why this is a guard and not a comment. jsdom applies no CSS, so no render test
    // can reach it.
    const button = stripBlockComments(blockAfter(css, '\n.rowButton {'));
    expect(button).toMatch(/font:\s*inherit/);
    expect(button).toMatch(/color:\s*inherit/);
  });

  it('strips the UA list styling the prototype’s <div> never had', () => {
    const listBlock = stripBlockComments(blockAfter(css, '\n.list {'));
    expect(listBlock).toMatch(/list-style:\s*none/);
    expect(listBlock).toMatch(/margin:\s*0/);
  });
});

describe('Dialog — the containing-block rule', () => {
  const css = readSource('src/ui/Dialog.module.css');

  it('keeps backdrop-filter on the overlay, never on a layout box', () => {
    // `backdrop-filter` makes an element the containing block for `position: fixed`
    // descendants — forbidden on AppShell's boxes for that reason. Here it is correct and
    // matches the prototype: the overlay is itself the fixed element.
    const overlay = stripBlockComments(blockAfter(css, '.overlay {'));
    expect(overlay).toMatch(/position:\s*fixed/);
    expect(overlay).toMatch(/backdrop-filter/);
    expect(stripBlockComments(blockAfter(css, '\n.panel {'))).not.toMatch(/backdrop-filter/);
  });
});
