/**
 * §9 "Layout widths" and the shell's own geometry, measured under real layout.
 *
 * **The box-model point that has now caught three tasks** (T-502's 265/309, T-508's 566, T-509's
 * 380/420): §9's figures are the prototype's declared *content* widths, and the prototype ships no
 * `box-sizing` reset — so the rendered outer width is the declared width plus its border and
 * padding. Both numbers are asserted here, and named as which, because a `border-box` reset would
 * silently narrow every panel while a naive outer-width check kept passing.
 */
import { computed } from '../harness.mjs';

export async function checkShell(page, r, theme) {
  r.context('§9 layout widths', theme);

  const sidebar = await page.evaluate(
    computed('nav[aria-label="Conversations"]', ['width', 'border-right-width', 'min-height']),
  );
  r.truthy('the sidebar landmark exists', sidebar !== null);
  if (sidebar !== null) {
    r.eq('sidebar declared content width is 264px', sidebar.width, '264px');
    r.near('sidebar measures 265px outer (264 + its 1px border)', sidebar._ow, 265);
  }

  const stats = await page.evaluate(
    computed('aside[aria-label="Session statistics"]', [
      'width',
      'padding-left',
      'padding-right',
      'border-left-width',
    ]),
  );
  r.truthy('the stats landmark exists', stats !== null);
  if (stats !== null) {
    r.eq('stats declared content width is 272px', stats.width, '272px');
    // 272 content + 18+18 padding + 1 border = 309 (T-507's figure, unchanged).
    r.near('stats measures 309px outer', stats._ow, 309);
  }

  const root = await page.evaluate(computed('main', ['min-height']));
  r.truthy('the main landmark exists', root !== null);

  // §9 "Layout widths ... min-height 640px" — on the shell root, not on <main>.
  const shellMin = await page.evaluate(`
    const el = document.querySelector('nav[aria-label="Conversations"]')?.parentElement;
    return el ? getComputedStyle(el).minHeight : null;`);
  r.eq('shell min-height is 640px', shellMin, '640px');

  // The three columns tile the viewport with no gap and no overlap: sidebar + main + stats.
  const cols = await page.evaluate(`
    const q = (s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.left, w: r.width }; };
    return { side: q('nav[aria-label="Conversations"]'), main: q('main'), stats: q('aside[aria-label="Session statistics"]') };`);
  if (cols.side && cols.main && cols.stats) {
    r.near('main begins exactly where the sidebar ends', cols.main.x, cols.side.x + cols.side.w, 1);
    r.near(
      'the stats panel begins exactly where main ends',
      cols.stats.x,
      cols.main.x + cols.main.w,
      1,
    );
  }
}

/**
 * FR-LAY-02: with `showStats={false}` the chat column takes the panel's width rather than leaving
 * a gap. T-503 measured 844 → 1153, gaining exactly the panel's 309.
 *
 * **`showStats` is a prop with no user-facing control** (`App.tsx` takes it as `AppProps`), so
 * there is nothing to click and the expansion cannot be driven from the page. What *is* checkable
 * at runtime is the rule that produces it: the two side columns are fixed, non-shrinking flex
 * items and the chat column is the only one that grows — so removing the panel can only give its
 * space to the chat. The rule is asserted directly, and then confirmed by detaching the node and
 * re-measuring; that second step exercises this stylesheet's flex declarations, not the browser's.
 */
export async function checkStatsExpansion(page, r, theme) {
  r.context('FR-LAY-02 expansion', theme);

  const flex = await page.evaluate(`
    const q = (s) => { const e = document.querySelector(s); if (!e) return null; const c = getComputedStyle(e);
      return { grow: c.flexGrow, shrink: c.flexShrink, basis: c.flexBasis, width: c.width }; };
    return { side: q('nav[aria-label="Conversations"]'), main: q('main'), stats: q('aside[aria-label="Session statistics"]') };`);

  r.truthy(
    'all three columns are present',
    flex.side !== null && flex.main !== null && flex.stats !== null,
  );
  if (flex.side === null || flex.main === null || flex.stats === null) return;

  r.eq('the sidebar does not grow', flex.side.grow, '0');
  r.eq('the sidebar does not shrink', flex.side.shrink, '0');
  r.eq('the stats panel does not grow', flex.stats.grow, '0');
  r.eq('the stats panel does not shrink', flex.stats.shrink, '0');
  r.eq('the chat column is the only growing item', flex.main.grow, '1');

  const before = await page.evaluate(
    `return document.querySelector('main').getBoundingClientRect().width;`,
  );
  const statsWidth = await page.evaluate(
    `return document.querySelector('aside[aria-label="Session statistics"]').getBoundingClientRect().width;`,
  );
  const after = await page.evaluate(`
    const a = document.querySelector('aside[aria-label="Session statistics"]');
    const parent = a.parentElement, next = a.nextSibling;
    a.remove();
    const w = document.querySelector('main').getBoundingClientRect().width;
    parent.insertBefore(a, next);
    return w;`);
  r.near(
    'removing the panel gives the chat column exactly its width',
    after - before,
    statsWidth,
    1.5,
  );
  r.near(
    'and the layout is restored afterwards',
    await page.evaluate(`return document.querySelector('main').getBoundingClientRect().width;`),
    before,
    1,
  );
}
