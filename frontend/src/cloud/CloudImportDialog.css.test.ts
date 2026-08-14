/**
 * Structural guards on FR-KBM-10's stylesheet — the `KnowledgeBaseModal.css.test.ts` set,
 * including the CSS-Modules key guard T-502 requires every component task to copy.
 *
 * Drift guards, not behaviour tests: jsdom applies no external CSS, so the *effect* of every rule
 * is verified in a real browser and formally by T-510. They matter more here than anywhere else
 * in §4.7, because this surface has **no prototype markup at all** — the docx row it derives from
 * says only "a secondary interface", so there is not even an unrendered declaration to compare a
 * change against.
 */
import { describe, expect, it } from 'vitest';

import { readSource, stripBlockComments, stripTsComments } from '../test/css-source';

const CSS_PATH = 'src/cloud/CloudImportDialog.module.css';
const TSX_PATH = 'src/cloud/CloudImportDialog.tsx';

const css = readSource(CSS_PATH);
const code = stripBlockComments(css);
const tsxCode = stripTsComments(readSource(TSX_PATH));

describe('CloudImportDialog stylesheet', () => {
  it('declares every class the component reads', () => {
    // The CSS-Modules hole T-502 documented: `vite/client` types the import as an index
    // signature and Vitest stubs it with a Proxy answering any key, so `styles.typo` passes
    // `tsc`, passes every render test, and ships as class="undefined".
    const declared = new Set([...css.matchAll(/^\.([A-Za-z][\w-]*)/gm)].map((m) => m[1]));
    const used = [...tsxCode.matchAll(/\bstyles\.([A-Za-z]\w*)/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(0);
    for (const key of used) {
      expect([...declared], `styles.${key} is not declared in ${CSS_PATH}`).toContain(key);
    }
  });

  it('never indexes the styles object dynamically', () => {
    // A computed key defeats the guard above — it cannot be read statically, so a typo inside
    // one is invisible again.
    expect(tsxCode).not.toMatch(/styles\[/);
  });

  it('never disables the focus ring (NFR-A11Y-02)', () => {
    expect(code).not.toMatch(/outline:\s*none/);
    expect(code).not.toMatch(/outline:\s*0\b/);
  });

  it('names no keyframe (R-69 / T-505)', () => {
    // CSS Modules rewrites `animation-name` even for a keyframe the module does not declare, so
    // any `animation:` here would compile to a hashed name matching no @keyframes and silently
    // never run. The panel's fadeUp comes from Dialog's global `.animate-fade-up`.
    expect(code).not.toMatch(/animation(-name)?\s*:/);
    expect(code).not.toContain('@keyframes');
  });

  it('writes no reduced-motion block and hard-codes no duration (NFR-A11Y-01)', () => {
    expect(code).not.toContain('prefers-reduced-motion');
    expect(code).not.toMatch(/\b\d+(?:\.\d+)?m?s\b/);
  });

  it('uses only tokens for colour (NFR-VIS-02)', () => {
    // No exemption here, unlike the KB modal's `#fff` on its accent-filled FR-KBM-06 button:
    // this surface has no filled button, by design — its primary action is a row.
    expect([...code.matchAll(/#[0-9a-f]{3,8}\b/gi)].map((m) => m[0])).toEqual([]);
    expect(code).not.toMatch(/\brgba?\(/);
  });

  it('restates no scrollbar styling of its own (NFR-USE-05, R-60)', () => {
    expect(code).not.toContain('scrollbar');
  });

  it('creates no containing block for the nested dialog’s fixed overlay', () => {
    // This surface nests a ConfirmDialog, whose overlay is `position: fixed; inset: 0`. Any of
    // these properties on a layout box here would make that element the containing block, and
    // the confirmation would be laid out inside the picker panel instead of over the viewport.
    const layoutRules = code
      .split('}')
      .filter((rule) => !rule.includes(':hover'))
      .join('}');
    expect(layoutRules).not.toMatch(
      /(^|[\s;{])(transform|backdrop-filter|perspective|will-change|contain)\s*:/m,
    );
  });

  it('restates none of Dialog’s panel treatment', () => {
    // `ui/Dialog` owns the overlay, background, border, radius, shadow and 22px padding.
    // Restating any of them here would double the padding and pin a second modal treatment
    // beside the shared one.
    const panel = code.slice(code.indexOf('.panel'), code.indexOf('}', code.indexOf('.panel')));
    for (const property of ['position', 'background', 'border-radius', 'box-shadow', 'padding']) {
      expect(panel, `.panel must not restate ${property}`).not.toContain(`${property}:`);
    }
  });

  it('sets box-sizing on the two full-width boxes', () => {
    // There is no reset anywhere in this codebase (the T-502 finding), so `width: 100%` plus
    // horizontal padding overflows the panel.
    for (const selector of ['.search', '.row']) {
      const rule = code.slice(code.indexOf(selector), code.indexOf('}', code.indexOf(selector)));
      expect(rule, `${selector} sets width: 100% and must set box-sizing`).toContain(
        'box-sizing: border-box',
      );
    }
  });
});
