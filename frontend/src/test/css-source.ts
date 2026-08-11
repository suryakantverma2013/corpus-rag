/**
 * Helpers for structural drift guards that read a source file from disk.
 *
 * Extracted from `src/styles/tokens.test.ts`, which is where they were written. Every
 * component task from T-502 on ships a stylesheet whose shape is load-bearing for reasons a
 * later reader cannot infer from the declarations — so each gets a guard, and six copies of
 * `blockAfter` is six chances for them to drift apart.
 *
 * Reading from disk rather than importing is deliberate and is explained in `tokens.test.ts`:
 * Vitest runs with `css: false`, so a plain `*.css` import evaluates to `""` and `?raw` hands
 * back the CSS pipeline's output rather than the source. These guards assert what is *written
 * in the file*.
 *
 * This module lives under `src/test/`, which `tsconfig.app.json` excludes and
 * `tsconfig.test.json` includes — so it gets Node types and can never be reached from code
 * that ships to a browser. It is not a `*.test.ts` file, so Vitest does not collect it.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** Reads a project-relative source file. Vitest's cwd is `frontend/`. */
export function readSource(relativePath: string): string {
  return readFileSync(resolve(process.cwd(), relativePath), 'utf8');
}

/**
 * Strips `/* … *\/` comments.
 *
 * Negative assertions need this: these files *discuss* the rules they warn against ("do not
 * replace it with `outline: none`"), so a `not.toContain` run over the raw text would fail on
 * the very comment explaining why the rule exists.
 */
export function stripBlockComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * Strips both comment forms. For TypeScript/TSX only — `//` is not a comment in CSS, and a
 * stylesheet containing `url(//host/x)` would be mangled by it.
 */
export function stripTsComments(source: string): string {
  return stripBlockComments(source).replace(/\/\/[^\n]*/g, '');
}

/**
 * The rule or at-rule beginning at `header`, matched by brace balance, comments included.
 *
 * Throws rather than asserting, so this module does not depend on vitest: a missing header
 * means the guard is pointed at a rule that no longer exists, which must fail loudly rather
 * than pass vacuously.
 */
export function blockAfter(source: string, header: string): string {
  const start = source.indexOf(header);
  if (start === -1) throw new Error(`${header} not found — the guard is pointed at nothing`);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces after ${header}`);
}
