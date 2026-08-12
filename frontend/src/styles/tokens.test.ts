/**
 * Structural guards on `tokens.css`.
 *
 * These are drift guards, not behaviour tests — jsdom evaluates neither media queries nor
 * cascade, so the *behaviour* of these rules is verified in a real browser (and will be
 * locked in by T-511's automated accessibility pass). What they defend against is the rule
 * being deleted or "simplified" by someone who does not know why it is shaped this way,
 * which is exactly what happened to each of the cases below during design.
 *
 * The `readSource`/`stripBlockComments`/`blockAfter` helpers were written here and moved to
 * `src/test/css-source.ts` in T-502, so the six component stylesheets that follow do not each
 * carry their own copy.
 */
import { describe, expect, it } from 'vitest';
import { readdirSync } from 'node:fs';
import { blockAfter, readSource, stripBlockComments } from '../test/css-source';

const css = readSource('src/styles/tokens.css');

/** Declarations only. The comments in this file *discuss* the rules they warn against
 *  ("do not replace it with `outline: none`"), so negative assertions must not see them. */
const code = stripBlockComments(css);

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

describe('motion durations (NFR-VIS-05)', () => {
  // The positive half of the pair with the reduced-motion exception below, and with
  // `chat/MessageList.css.test.ts`, which asserts the typing dots read these through `var()`.
  // The prototype writes `dotPulse 1.2s infinite` staggered `.2s`/`.4s`; the third dot is
  // `calc(var(--motion-dot-delay) * 2)`, so only two tokens are needed.
  it.each([
    ['--motion-fast', '0.15s'],
    ['--motion', '0.2s'],
    ['--motion-slow', '0.25s'],
    ['--motion-bar', '0.5s'],
    ['--motion-dot', '1.2s'],
    ['--motion-dot-delay', '0.2s'],
  ])('declares %s as %s', (token, value) => {
    expect(code).toMatch(new RegExp(`${token}:\\s*${value}\\s*;`));
  });
});

describe('layout dimensions (§9, FR-LAY-01)', () => {
  // The positive half of a pair: this is where the §9 literals live, and
  // `shell/AppShell.css.test.ts` asserts the shell reads them through `var()` rather than
  // repeating them. Either guard alone lets the two drift.
  it.each([
    ['--sidebar-w', '264px'],
    ['--stats-w', '272px'],
    ['--app-min-h', '640px'],
  ])('declares %s as %s', (token, value) => {
    expect(code).toMatch(new RegExp(`${token}:\\s*${value}\\s*;`));
  });
});

describe('stacking order (FR-LAY-03)', () => {
  const zIndex = (token: string) => {
    const match = code.match(new RegExp(`${token}:\\s*(\\d+)\\s*;`));
    expect(match, `${token} not declared in tokens.css`).not.toBeNull();
    return Number(match![1]);
  };

  it('promotes the prototype’s three literals, in its order', () => {
    // 30 / 50 / 60 are the prototype's own numbers. The hover card above the modal is its
    // order too, and is harmless — the card is pointer-events:none and a citation chip cannot
    // be hovered while the modal covers it.
    expect(zIndex('--z-menu')).toBe(30);
    expect(zIndex('--z-modal')).toBe(50);
    expect(zIndex('--z-hovercard')).toBe(60);
  });

  it('keeps the scale strictly increasing', () => {
    expect(zIndex('--z-menu')).toBeLessThan(zIndex('--z-modal'));
    expect(zIndex('--z-modal')).toBeLessThan(zIndex('--z-hovercard'));
  });
});

describe('visually hidden (NFR-A11Y-03)', () => {
  const block = blockAfter(css, '.visually-hidden {');

  it('hides the element from sight without removing it from the accessibility tree', () => {
    // `display: none` and `visibility: hidden` both remove it from the accessibility tree,
    // which is the one thing this class exists to prevent — the page would render identically
    // and every test would stay green while the document silently lost its <h1>.
    expect(block).not.toMatch(/display:\s*none/);
    expect(block).not.toMatch(/visibility:\s*hidden/);
    expect(block).toMatch(/clip-path:\s*inset\(50%\)/);
    expect(block).toMatch(/width:\s*1px/);
    expect(block).toMatch(/height:\s*1px/);
  });

  it('takes the element out of flow', () => {
    // Load-bearing beyond hiding: the shell's <h1> is a child of the FR-LAY-01 flex row, and
    // a statically positioned "hidden" element would become a fourth flex item.
    expect(block).toMatch(/position:\s*absolute/);
  });
});

describe('reduced motion (NFR-A11Y-01, R-59)', () => {
  const block = blockAfter(css, '@media (prefers-reduced-motion: reduce)');

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

  it('leaves the FR-MSG-05 dot durations ALONE — the one deliberate exception (T-505)', () => {
    // Zeroing a duration is `animation-duration: 0s`, which stops the animation applying and
    // falls the element back to its base style: the exact failure the keyframe redefinition
    // above exists to prevent, reached through a different door. The dots must stay VISIBLE
    // and STILL, and the constant keyframe is what delivers that — so these two must not be
    // "completed" into the list above. Asserted on the comment-stripped block, because the
    // stylesheet's own note names both tokens.
    const declarations = stripBlockComments(block);
    expect(declarations).not.toContain('--motion-dot');
    expect(declarations).not.toContain('--motion-dot-delay');
  });

  it('does not use the blanket universal-selector rule', () => {
    expect(block).not.toMatch(/\*\s*,\s*\*::before/);
    expect(block).not.toMatch(/animation-duration:\s*\.01ms/);
  });
});

describe('animation utilities (NFR-VIS-05)', () => {
  it('declares both keyframe references globally, with an overridable duration', () => {
    expect(code).toMatch(/\.animate-fade-up\s*\{[^}]*animation:\s*fadeUp/);
    // The duration is a per-call-site custom property, so each surface keeps its own --motion-*
    // token and NFR-A11Y-01's zeroing still reaches every one of them.
    expect(code).toMatch(/var\(--fade-up-duration,\s*var\(--motion-slow\)\)/);
    expect(code).toMatch(/\.animate-dot-pulse\s*\{[^}]*animation:\s*dotPulse/);
  });

  it('is the ONLY place a keyframe is referenced — no CSS Module may name one', () => {
    // The defect this exists to prevent, found headed in T-505 and shipping since T-503:
    // **CSS Modules rewrites `animation-name` even for a keyframe it does not declare**, so
    // `animation: fadeUp …` inside a `*.module.css` compiles to `_fadeUp_<hash>`, matches no
    // @keyframes rule, and never runs. It fails silently — the declaration is present and
    // `getComputedStyle` reports an animation-name — so nothing but a rendered check catches it.
    // At the time this guard was written, six modules were affected and all six were dead.
    const modules = readdirSync('src', { recursive: true, encoding: 'utf8' }).filter((f) =>
      f.endsWith('.module.css'),
    );
    expect(modules.length).toBeGreaterThan(5);
    for (const file of modules) {
      const sheet = stripBlockComments(readSource(`src/${file}`.replaceAll('\\', '/')));
      expect(sheet, `${file} references a keyframe; use the global utility`).not.toMatch(
        /animation(-name)?:\s*[A-Za-z_]/,
      );
    }
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
    const ring = blockAfter(css, ':focus-visible');
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
    const gate = blockAfter(css, '@supports not selector(::-webkit-scrollbar-thumb)');
    expect(gate).toContain('scrollbar-width');
    expect(gate).toContain('scrollbar-color');
    const outside = code.replace(stripBlockComments(gate), '');
    expect(outside).not.toContain('scrollbar-width');
    expect(outside).not.toContain('scrollbar-color');
  });
});
