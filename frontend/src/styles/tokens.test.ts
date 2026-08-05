/**
 * Structural guards on `tokens.css`.
 *
 * These are drift guards, not behaviour tests — jsdom evaluates neither media queries nor
 * cascade, so the *behaviour* of these rules is verified in a real browser (and will be
 * locked in by T-511's automated accessibility pass). What they defend against is the rule
 * being deleted or "simplified" by someone who does not know why it is shaped this way,
 * which is exactly what happened to each of the three cases below during design.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// Read from disk rather than importing: `import.meta.url` is not a file URL after Vite's
// transform, and `?raw` hands back the CSS pipeline's output rather than the source. The
// point of these guards is to assert what is *written in the file*.
const TOKENS = resolve(process.cwd(), 'src/styles/tokens.css');
const css = readFileSync(TOKENS, 'utf8');

/** Declarations only. The comments in this file *discuss* the rules they warn against
 *  ("do not replace it with `outline: none`"), so negative assertions must not see them. */
const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

/** The body of an at-rule beginning at `header`, matched by brace balance. */
function blockAfter(header: string): string {
  const start = css.indexOf(header);
  expect(start, `${header} not found in tokens.css`).toBeGreaterThan(-1);
  let depth = 0;
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces after ${header}`);
}

describe('token block structure', () => {
  it('declares the motion tokens on a BARE :root, not inside a theme selector list', () => {
    // A media query adds no specificity. While these lived in `:root, :root[data-theme='dark']`
    // (0,2,0) the reduced-motion override at plain `:root` (0,1,0) silently lost — durations
    // stayed at 0.2s/0.5s with the whole suite green. Any theme-independent token declared in
    // a theme block reintroduces that trap for itself.
    const selector = code
      .slice(0, code.indexOf('--motion:'))
      .split('}')
      .pop()!
      .split('{')[0]
      .trim();
    expect(selector).toBe(':root');
  });
});

describe('reduced motion (NFR-A11Y-01, R-59)', () => {
  const block = blockAfter('@media (prefers-reduced-motion: reduce)');

  it('redefines dotPulse to hold full opacity rather than switching it off', () => {
    // `animation: none` and the blanket `animation-duration: .01ms` both fall back to the
    // element's base style, which leaves the typing indicator at whatever resting opacity
    // the component chose — measured at 0.25, i.e. invisible, for a natural authoring.
    expect(block).toContain('@keyframes dotPulse');
    const dots = block.slice(block.indexOf('@keyframes dotPulse'));
    expect(dots).toMatch(/opacity:\s*1/);
    expect(dots).not.toMatch(/opacity:\s*0?\.\d/);
  });

  it('neutralises fadeUp', () => {
    expect(block).toContain('@keyframes fadeUp');
  });

  it('zeroes every motion duration token, so components inherit the behaviour', () => {
    for (const token of ['--motion-fast', '--motion', '--motion-slow', '--motion-bar']) {
      expect(block).toMatch(new RegExp(`${token}:\\s*0s`));
    }
  });

  it('does not use the blanket universal-selector rule', () => {
    expect(block).not.toMatch(/\*\s*,\s*\*::before/);
    expect(block).not.toMatch(/animation-duration:\s*\.01ms/);
  });
});

describe('focus ring (NFR-A11Y-02, R-59)', () => {
  it('is declared globally on :focus-visible, not :focus', () => {
    expect(css).toMatch(/^:focus-visible\s*\{/m);
    // `:focus` would paint the ring on mouse clicks too, which is both wrong and would
    // start showing up in T-510's screenshots.
    expect(css).not.toMatch(/^:focus\s*\{/m);
  });

  it('uses --accent, measured at 3.92–6.00 : 1 on every surface in both themes', () => {
    const ring = blockAfter(':focus-visible');
    expect(ring).toContain('var(--accent)');
    expect(ring).toContain('outline');
  });

  it('never disables the outline outright', () => {
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });
});

describe('scrollbar gate (NFR-USE-05, R-58(3), corrected by R-60)', () => {
  it('gates on a sub-part pseudo, NOT the bare ::-webkit-scrollbar', () => {
    // Firefox whitelists the bare selector for web compat while styling nothing with it, so
    // `not selector(::-webkit-scrollbar)` evaluated false in Gecko too and the block reached
    // no engine at all. `-thumb` is true in Chromium and false in Firefox — measured — which
    // makes it a genuine capability test rather than a vendor sniff.
    expect(code).toContain('@supports not selector(::-webkit-scrollbar-thumb)');
    expect(code).not.toMatch(/@supports not selector\(::-webkit-scrollbar\)/);
  });

  it('keeps the standard properties behind the @supports negation', () => {
    // Chrome 121+ discards every ::-webkit-scrollbar rule on an element that also sets
    // scrollbar-width/scrollbar-color, so ungating these replaces the specified bar.
    const gate = blockAfter('@supports not selector(::-webkit-scrollbar-thumb)');
    expect(gate).toContain('scrollbar-width');
    expect(gate).toContain('scrollbar-color');
    const outside = code.replace(gate.replace(/\/\*[\s\S]*?\*\//g, ''), '');
    expect(outside).not.toContain('scrollbar-width');
    expect(outside).not.toContain('scrollbar-color');
  });
});
