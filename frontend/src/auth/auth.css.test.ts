/**
 * Structural guards on the §4.17 stylesheets — the `KnowledgeBaseModal.css.test.ts` pattern,
 * including the CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every
 * rule below is verified in a real browser (see the headed pass in §8.59) and formally by
 * T-510.
 */
import { describe, expect, it } from 'vitest';

import { blockAfter, readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const SHEETS = [
  ['LoginScreen', 'src/auth/LoginScreen.module.css', 'src/auth/LoginScreen.tsx'],
  ['UserMenu', 'src/auth/UserMenu.module.css', 'src/auth/UserMenu.tsx'],
  [
    'ChangePasswordModal',
    'src/auth/ChangePasswordModal.module.css',
    'src/auth/ChangePasswordModal.tsx',
  ],
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
    for (const key of used) {
      expect([...declared], `styles.${key} is not declared in ${cssPath}`).toContain(key);
    }
  });

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    // `outline: none` on the three field rules is the ONE exception and is sanctioned: each
    // replaces it with a `:focus` border-colour change that FR-AUT-02 specifies by name
    // ("--line border → --accent on focus"). Everything else here is a control that relies on
    // the global ring, so the guard is scoped to inputs rather than dropped.
    for (const rule of code.split('}')) {
      if (/outline:\s*(none|0)\b/.test(rule)) {
        expect(rule, 'only the FR-AUT-02 field may clear its outline').toMatch(/\.input\b/);
      }
    }
    // …and where it is cleared, the specified focus affordance must actually be there.
    if (/outline:\s*none/.test(code)) {
      expect(code).toMatch(/\.input:focus\s*\{[^}]*border-color:\s*var\(--accent\)/);
    }
  });

  it('names no keyframe (R-69 / T-505)', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe the module does not declare,
    // so any `animation:` here compiles to a hashed name matching no @keyframes and silently
    // never runs. FR-AUT-03's dots therefore use the GLOBAL `.animate-dot-pulse`, and every
    // `fadeUp` in §4.17 uses `.animate-fade-up`.
    expect(code).not.toMatch(/animation\s*:/);
    expect(code).not.toMatch(/animation-name\s*:/);
    expect(code).not.toContain('@keyframes');
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    // Components must not add their own: `tokens.css` redefines the keyframes inside the media
    // query, which is correct under either authoring style, and a second block here would
    // fight it.
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/(transition|animation-delay)[^;]*\d+(\.\d+)?m?s/);
  });

  it('takes every colour from a token, bar the two documented literals', () => {
    // `#fff` on --accent (the brand mark and the primary buttons) and `#e86a8a` (FR-AUT-04's
    // error) are the addendum's own literals. The error hue deliberately does NOT use
    // --eval-bad: that token means "a judge scored this below 0.80" (FR-EVL-03), and sharing
    // it would couple login errors to the scoring palette.
    const literals = [...code.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0].toLowerCase());
    for (const literal of literals) {
      expect(['#fff', '#ffffff', '#e86a8a']).toContain(literal);
    }
  });
});

describe('FR-AUT-01/09 — §4.17 boxes are border-box', () => {
  // The one place §4.17 departs from FR-KBM-01, deliberately (R-72(4)): the addendum declares
  // `box-sizing: border-box` on every box it draws, so 380px and 420px are OUTER widths. The
  // KB modal measures 566 outer for a declared 520 only because the *prototype* has no reset
  // anywhere — an artifact T-508 measured, not a decision. Without these declarations the
  // cards silently grow by their padding and border, which no unit test would see.
  it('the login card and its banner size to 380 outer', () => {
    const css = readSource('src/auth/LoginScreen.module.css');
    for (const header of ['.card', '.expired']) {
      const rule = blockAfter(css, header);
      expect(rule).toMatch(/width:\s*380px/);
      expect(rule).toMatch(/box-sizing:\s*border-box/);
    }
  });

  it('the change-password panel sizes to 420 outer', () => {
    const rule = blockAfter(readSource('src/auth/ChangePasswordModal.module.css'), '.panel');
    expect(rule).toMatch(/width:\s*420px/);
    expect(rule).toMatch(/box-sizing:\s*border-box/);
  });

  it('every full-width field is border-box too', () => {
    // `width: 100%` plus 12px of horizontal padding overflows its card by 26px without this —
    // the T-503 finding, and the reason the addendum sets it on `.inp`.
    for (const path of [
      'src/auth/LoginScreen.module.css',
      'src/auth/ChangePasswordModal.module.css',
    ]) {
      const rule = blockAfter(readSource(path), '.input');
      expect(rule).toMatch(/width:\s*100%/);
      expect(rule).toMatch(/box-sizing:\s*border-box/);
    }
  });
});

describe('FR-AUT-08 — the popover is positioned against the sidebar footer', () => {
  it('is absolute, and the footer is its containing block', () => {
    const popover = blockAfter(readSource('src/auth/UserMenu.module.css'), '.popover');
    expect(popover).toMatch(/position:\s*absolute/);
    expect(popover).toMatch(/z-index:\s*var\(--z-menu\)/);

    // The other half of one fact, in another file: without `position: relative` there the
    // popover escapes to the viewport and lands at the bottom-left of the window.
    const footer = blockAfter(readSource('src/sidebar/Sidebar.module.css'), '.footer');
    expect(footer, 'Sidebar .footer must stay the popover’s containing block').toMatch(
      /position:\s*relative/,
    );
  });
});
