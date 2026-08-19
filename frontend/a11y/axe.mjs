/**
 * T-614 — injecting axe-core over CDP and running it **scoped**.
 *
 * `axe-core` has been a devDependency since T-511 with zero tracked call sites: the results in
 * `frontend/ACCESSIBILITY.md` came from an injection performed by hand. §8.64(2) is the precedent
 * for why that is not enough — *a verification instrument that exists only as a written result
 * certifies rather than verifies* — and the enumerated exception list is exactly the kind that
 * grows silently.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

/** Read from the installed package, so the version audited is the version locked. */
export function axeSource() {
  return readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
}

export async function injectAxe(page) {
  const already = await page.evaluate(`return typeof window.axe !== 'undefined';`);
  if (already === true) return;
  await page.evaluate(`${axeSource()}\nreturn typeof window.axe;`);
}

/**
 * Run axe against `selector`, or the whole document when it is null.
 *
 * **Scoping is not tidiness, it is correctness — measured (§8.63(7)).** Run over the whole
 * document with a modal open, axe measures the content *behind the scrim*: with the unlink
 * confirmation up, the picker's rows and the sidebar report **1.01:1**, light `--text` against
 * the overlay's dark wash. It reads as a catastrophic defect and means nothing, because that
 * text is obscured by design. Every surface below that opens over another one is therefore
 * scoped to its own panel, which is how T-514's figures were taken.
 *
 * The result is reduced **inside the page**: a full axe report is megabytes of node HTML, and
 * `returnByValue` would ship all of it over the socket for data no assertion reads.
 */
export async function runAxe(page, selector) {
  const context = selector === null ? 'document' : JSON.stringify(selector);
  const expression = `
    const root = ${selector === null ? 'document' : `document.querySelector(${context})`};
    if (!root) return { error: 'no element matched ' + ${JSON.stringify(String(selector))} };

    // 'best-practice' is included on purpose: \`region\` is one, and it is an accepted finding
    // rather than an ignored one — the difference is that an accepted finding is still counted.
    const result = await window.axe.run(root, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] },
      resultTypes: ['violations'],
    });

    return {
      violations: result.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        tags: v.tags,
        nodes: v.nodes.length,
        // Only \`color-contrast\` carries the colour data the accepted set is checked against.
        contrast: v.id !== 'color-contrast' ? [] : v.nodes.flatMap((n) =>
          [...(n.any || [])].filter((c) => c.data && c.data.fgColor).map((c) => ({
            fg: String(c.data.fgColor),
            bg: String(c.data.bgColor),
            ratio: Number(c.data.contrastRatio),
            fontSize: String(c.data.fontSize || ''),
            fontWeight: String(c.data.fontWeight || ''),
            target: String((n.target && n.target[0]) || ''),
          })),
        ),
        // One example per rule is enough to find it; the whole node list is not.
        example: v.nodes.length > 0 ? String(v.nodes[0].target && v.nodes[0].target[0]) : '',
      })),
      tested: result.passes.length + result.violations.length,
    };`;
  return page.evaluate(expression);
}
