/**
 * Structural guards on the §4.4 stylesheets — the `MessageList.css.test.ts` pattern, including
 * the CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every
 * rule below is verified in a real browser and formally by T-510. What they defend against is a
 * rule being deleted or "simplified" by someone who does not know why it is shaped that way.
 */
import { describe, expect, it } from 'vitest';

import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['Composer', 'src/composer/Composer.module.css', 'src/composer/Composer.tsx'],
  ['MentionMenu', 'src/composer/MentionMenu.module.css', 'src/composer/MentionMenu.tsx'],
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

  it('names no keyframe (R-69 / T-505 finding)', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe the module does not declare,
    // so any `animation:` here compiles to a hashed name matching no @keyframes and silently
    // never runs. The global `.animate-fade-up` utility is the only way to animate.
    expect(code).not.toMatch(/animation(-name)?\s*:/);
    expect(code).not.toContain('@keyframes');
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour, apart from the prototype’s own #fff (NFR-VIS-02)', () => {
    // `#fff` on the accent Send button is the prototype's literal and a recorded NFR-A11Y-06
    // exception (3.17:1 in dark), the same disposition as T-505's AI avatar.
    expect(code.replace(/#fff\b/g, '')).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(code).not.toMatch(/\brgba?\(/);
  });

  it('restates no scrollbar styling of its own (NFR-USE-05, R-60)', () => {
    expect(code).not.toContain('scrollbar');
  });
});

describe('Composer — FR-CMP-01 declarations', () => {
  const css = readSource('src/composer/Composer.module.css');

  it('keeps the positioning context the FR-CMP-04 popover is anchored to', () => {
    // The menu is `position: absolute` and is deliberately NOT in AppShell's overlays slot;
    // dropping this would reparent it to the viewport.
    const composer = stripBlockComments(blockAfter(css, '\n.composer {'));
    expect(composer).toMatch(/position:\s*relative/);
    expect(composer).toMatch(/padding:\s*14px 30px 18px/);
  });

  it('gives the bar the prototype’s box', () => {
    const bar = stripBlockComments(blockAfter(css, '\n.bar {'));
    expect(bar).toMatch(/border-radius:\s*var\(--radius-pop\)/);
    expect(bar).toMatch(/background:\s*var\(--panel\)/);
    expect(bar).toMatch(/padding:\s*8px 10px/);
    expect(bar).toMatch(/gap:\s*6px/);
  });

  it('accents the border on focus-within, not on focus (FR-CMP-01)', () => {
    // The prototype writes `style-focus` on the bar, which is a <div> and can never receive
    // focus; `:focus-within` is the only selector expressing what it plainly means.
    expect(css).toContain('.bar:focus-within');
    expect(stripBlockComments(blockAfter(css, '.bar:focus-within {'))).toMatch(
      /border-color:\s*var\(--accent\)/,
    );
  });

  it('declares the multi-line properties a <textarea> does not get for free (R-91(6))', () => {
    // `font-family: inherit` is the one that would fail silently: the UA stylesheet gives a
    // textarea `monospace`, so without it the composer renders in the wrong typeface at exactly
    // the right size, and every geometry-only fidelity check still passes.
    const input = stripBlockComments(blockAfter(css, '\n.input {'));
    expect(input).toMatch(/font-family:\s*inherit/);
    expect(input).toMatch(/resize:\s*none/);
    // The five-line ceiling (R-91(3)) and the explicit line-height it is derived from. The
    // component reads `max-height` back at runtime, so a missing declaration here is not a style
    // nit — it removes the cap and the field grows without bound.
    //
    // 18px is measured rather than chosen: it is what `normal` resolves to for Inter at 13.5px,
    // which is what makes the resting control exactly the prototype input's 30px. The plausible
    // guess, `1.2`, measures 28px in Chromium — a two-pixel NFR-VIS-01 regression invisible to
    // every test in this file and to jsdom.
    expect(input).toMatch(/line-height:\s*18px/);
    expect(input).toMatch(/max-height:\s*calc\(5 \* 18px \+ 12px\)/);
    expect(input).toMatch(/overflow-y:\s*auto/);
  });

  it('keeps the input shrinkable so Send cannot be pushed out of the bar', () => {
    // Not the prototype's — a flex <input> carries an intrinsic ~20-character minimum size.
    const input = stripBlockComments(blockAfter(css, '\n.input {'));
    expect(input).toMatch(/min-width:\s*0/);
    expect(input).toMatch(/flex:\s*1/);
  });

  it('relocates the input’s focus indicator rather than removing it (NFR-A11Y-02)', () => {
    // The one sanctioned `outline: none` in the codebase: the indicator moves to the bar's
    // accent border via `:focus-within`, so the control still has a visible focus state. The
    // shared guard above deliberately does not cover this file for that reason.
    const input = stripBlockComments(blockAfter(css, '\n.input {'));
    expect(input).toMatch(/outline:\s*none/);
    expect(css).toContain('.bar:focus-within');
  });
});

describe('MentionMenu — FR-CMP-04 declarations', () => {
  const css = readSource('src/composer/MentionMenu.module.css');

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    // Deliberately scoped to this sheet rather than shared across both: `Composer.module.css`
    // carries the codebase's one sanctioned `outline: none`, on the borderless input whose
    // indicator moves to the bar's accent border. Nothing in the menu has that excuse, and a
    // blanket guard would have had to exempt the file that actually needed checking.
    const code = stripBlockComments(css);
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('anchors the popover exactly where the prototype puts it', () => {
    const menu = stripBlockComments(blockAfter(css, '\n.menu {'));
    expect(menu).toMatch(/position:\s*absolute/);
    expect(menu).toMatch(/bottom:\s*76px/);
    expect(menu).toMatch(/left:\s*30px/);
    expect(menu).toMatch(/width:\s*320px/);
    expect(menu).toMatch(/z-index:\s*30/);
    expect(menu).toMatch(/border-radius:\s*var\(--radius-pop\)/);
  });

  it('resets the <ul> the prototype never had', () => {
    // A UA stylesheet supplies list markers and 40px of inline padding; the prototype's bare
    // <div> had neither, and this is a <ul> only because NFR-A11Y-03 needs the listbox.
    const list = stripBlockComments(blockAfter(css, '\n.list {'));
    expect(list).toMatch(/list-style:\s*none/);
    expect(list).toMatch(/margin:\s*0/);
    expect(list).toMatch(/padding:\s*0/);
  });

  it('keeps the row inside the 320px popover (T-502’s box-sizing finding)', () => {
    // There is no global reset, so `width: 100%` plus 10px of padding overflows without this.
    const row = stripBlockComments(blockAfter(css, '\n.row {'));
    expect(row).toMatch(/box-sizing:\s*border-box/);
    expect(row).toMatch(/width:\s*100%/);
  });

  it('undoes the <button> defaults the row inherits (T-503’s finding)', () => {
    const row = stripBlockComments(blockAfter(css, '\n.row {'));
    expect(row).toMatch(/font-family:\s*inherit/);
    expect(row).toMatch(/font-size:\s*inherit/);
    expect(row).toMatch(/color:\s*inherit/);
    expect(row).toMatch(/text-align:\s*left/);
  });

  it('gives the keyboard-active row the same fill as a hovered one (NFR-A11Y-03)', () => {
    // `aria-activedescendant` moves a virtual focus; without a visible counterpart the keyboard
    // user cannot see where they are.
    expect(css).toContain(".row[data-active='true']");
    const active = stripBlockComments(blockAfter(css, '.row:hover,'));
    expect(active).toMatch(/background:\s*var\(--panel2\)/);
  });

  it('sizes the type badge as a fixed 32px column (FR-CMP-04)', () => {
    const badge = stripBlockComments(blockAfter(css, '\n.badge {'));
    expect(badge).toMatch(/width:\s*32px/);
    expect(badge).toMatch(/text-align:\s*center/);
    expect(badge).toMatch(/flex-shrink:\s*0/);
    expect(badge).toMatch(/font-size:\s*9\.5px/);
  });

  it('ellipsises the filename and zeroes its automatic minimum size', () => {
    const name = stripBlockComments(blockAfter(css, '\n.name {'));
    expect(name).toMatch(/overflow:\s*hidden/);
    expect(name).toMatch(/text-overflow:\s*ellipsis/);
    expect(name).toMatch(/white-space:\s*nowrap/);
  });
});

describe('Composer — component invariants', () => {
  const code = stripTsComments(readSource('src/composer/Composer.tsx'));

  it('assigns the mention state from the trailing-@ test rather than OR-ing it (FR-CMP-05)', () => {
    // The whole matrix turns on this being an assignment: OR-ing with the previous state would
    // keep a button-opened menu open through the next keystroke, which the Rev 0.5.1 legacy
    // delta explicitly rules out.
    expect(code).toContain('setMentionOpen(hasMentionTrigger(next))');
  });

  it('guards the send in the handler, not only on the button (FR-CMP-03)', () => {
    // A disabled button does not disable the Enter key.
    expect(code).toContain('sendBlock(value, { pending, overBudget }) !== null) return');
  });

  it('applies the server’s budget rule rather than a local constant (R-51(2))', () => {
    expect(code).toContain('projectSubmission(value, usage)');
    expect(code).not.toMatch(/\b1500\b|\b10400\b|\b10_400\b/);
  });

  it('uses the global fade-up utility, never a local animation (R-69)', () => {
    const menu = stripTsComments(readSource('src/composer/MentionMenu.tsx'));
    expect(menu).toContain('animate-fade-up');
  });
});
