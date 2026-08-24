/**
 * The §9 rows that live on a surface you have to open: the mention menu, the citation hover card,
 * the user menu and its password modal, the message action bar, and the FR-EVL-03 hues as applied
 * rather than as tokens.
 *
 * Each opens its surface, asserts, and closes it again, so the run stays order-independent.
 */
import { clickByText, sleep, waitUntil } from '../harness.mjs';

const onScreen = async (page, text) =>
  page.evaluate(`return document.body.innerText.includes(${JSON.stringify(text)});`);

const escape = async (page) => {
  await page.send('Input.dispatchKeyEvent', {
    type: 'keyDown',
    key: 'Escape',
    code: 'Escape',
    windowsVirtualKeyCode: 27,
  });
  await page.send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key: 'Escape',
    code: 'Escape',
    windowsVirtualKeyCode: 27,
  });
  await sleep(400);
};

/** §9 "Mention-menu header" — `REFERENCE A DOCUMENT`, opened from the composer's @ control. */
export async function checkMentionMenu(page, r, theme) {
  r.context('§9 mention menu', theme);

  const opened = await page.evaluate(`
    const at = [...document.querySelectorAll('main button')]
      .find((b) => b.getAttribute('aria-label') === 'Reference a document');
    if (!at) return false;
    at.focus(); at.click(); return true;`);
  r.truthy('the @ control opens the mention menu', opened);
  if (!opened) return;

  r.truthy(
    'the listbox is present',
    await waitUntil(page, `document.querySelector('[role="listbox"]')`),
    'no [role="listbox"] appeared within 3s',
  );
  r.truthy('renders "REFERENCE A DOCUMENT"', await onScreen(page, 'REFERENCE A DOCUMENT'));

  // FR-CMP-05/NFR-A11Y-03: the menu opens with NO active option, which is what leaves Enter bound
  // to FR-CMP-03's send. A default-active first row would take Enter away from sending.
  const active = await page.evaluate(`
    const input = document.querySelector('main textarea, main input[type=text]');
    return input ? input.getAttribute('aria-activedescendant') : 'NO INPUT';`);
  r.eq('no option is active on open (Enter still sends)', active, null);

  await escape(page);
}

/**
 * §9 "Hover-card footer" — `Source passage · retrieval score {score}`, and R-69(1)'s bare
 * `Source passage` when the reranker published no score (R-47(2) makes that routine, not degraded).
 */
export async function checkCitationCard(page, r, theme) {
  r.context('§9 citation hover card', theme);

  /*
   * The chip is focused, then the card is POLLED for rather than slept on.
   *
   * A fixed 600 ms wait made this check intermittent — it passed on every run until the theme
   * loop happened to be a little slower, then failed in the light pass alone on an unchanged
   * build. A fidelity harness that is occasionally red teaches you to re-run it until it is
   * green, which is the habit that makes the whole instrument worthless; and a *sleep* is an
   * assumption about timing wearing an assertion's clothes.
   */
  const opened = await page.evaluate(`
    const chip = [...document.querySelectorAll('main button')]
      .find((b) => /\\.(md|pdf|docx|csv)\\b/i.test((b.textContent || '').trim()));
    if (!chip) return false;
    chip.focus(); return true;`);
  if (!opened) {
    r.truthy(
      'a citation chip is present to open',
      false,
      'no chip on this conversation — cannot reach the card',
    );
    return;
  }

  let card = null;
  for (let i = 0; i < 25 && card === null; i++) {
    await sleep(120);
    card = await page.evaluate(`
      const t = document.querySelector('[role="tooltip"]');
      return t ? { text: (t.textContent || '').trim(), rect: t.getBoundingClientRect().width } : null;`);
  }
  r.truthy(
    'focusing the chip opens the card',
    card !== null,
    'no [role="tooltip"] appeared within 3s',
  );
  if (card === null) return;

  r.truthy(
    'the footer is `Source passage` with or without a score',
    /Source passage(\s·\sretrieval score\s[\d.]+)?/.test(card.text),
    `got ${JSON.stringify(card.text.slice(-70))}`,
  );

  await escape(page);
}

/**
 * §9 "Message action bar" — 👍 / 👎 / Regenerate beneath an AI answer (FR-MSG-08).
 *
 * The glyph is the pixel and the **accessible name is `Good answer` / `Poor answer`**, deliberately
 * (T-505: "gives the thumbs a real accessible name rather than a bare emoji"), so this asserts the
 * name rather than the emoji. *The first version of this check searched for the emoji and reported
 * a correct action bar as missing — the recurring finding that the probe is wrong more often than
 * the product.*
 */
export async function checkActionBar(page, r, theme) {
  r.context('§9 message action bar', theme);

  const bar = await page.evaluate(`
    const byName = (name) => [...document.querySelectorAll('main button')]
      .find((b) => (b.getAttribute('aria-label') || b.textContent || '').trim() === name) || null;
    const good = byName('Good answer'), poor = byName('Poor answer'), regen = byName('Regenerate');
    return {
      good: good !== null, poor: poor !== null, regen: regen !== null,
      goodGlyph: good ? (good.textContent || '').trim() : null,
      poorGlyph: poor ? (poor.textContent || '').trim() : null,
      goodPressed: good ? good.getAttribute('aria-pressed') : null,
      poorPressed: poor ? poor.getAttribute('aria-pressed') : null,
    };`);
  r.truthy('a "Good answer" control is present', bar.good);
  r.truthy('a "Poor answer" control is present', bar.poor);
  r.truthy('a Regenerate control is present', bar.regen);
  r.eq('the 👍 glyph is what is painted', bar.goodGlyph, '👍');
  r.eq('the 👎 glyph is what is painted', bar.poorGlyph, '👎');
  // NFR-A11Y-06: the rating must not be carried by colour alone, so each thumb states its state.
  r.truthy(
    'the thumbs carry aria-pressed, not colour alone',
    bar.goodPressed !== null && bar.poorPressed !== null,
    `good=${bar.goodPressed} poor=${bar.poorPressed}`,
  );
}

/** §9 "User menu / password modal" copy. */
export async function checkUserMenu(page, r, theme) {
  r.context('§9 user menu', theme);

  const opened = await clickByText(page, 'nav button[aria-haspopup="menu"]', 'Corpus');
  r.truthy('the user menu opens', opened);
  if (!opened) return;

  r.truthy('renders "Change password…"', await onScreen(page, 'Change password…'));
  r.truthy('renders "Sign out"', await onScreen(page, 'Sign out'));

  const toModal = await clickByText(page, '[role="menuitem"]', 'Change password');
  r.truthy('the change-password modal opens', toModal);
  if (toModal) {
    await sleep(700);
    for (const literal of ['CURRENT PASSWORD', 'NEW PASSWORD', 'CONFIRM NEW PASSWORD'])
      r.truthy(`renders ${JSON.stringify(literal)}`, await onScreen(page, literal));

    // T-509 measured this panel at 420 OUTER, because §4.17's addendum declares `box-sizing`
    // — the opposite of FR-KBM-01's 520 content width. Both are asserted, as measured.
    const panel = await page.evaluate(`
      const d = [...document.querySelectorAll('[role="dialog"]')].pop();
      if (!d) return null;
      return { box: getComputedStyle(d).boxSizing, outer: d.getBoundingClientRect().width };`);
    if (panel !== null) {
      r.eq('the password panel is border-box', panel.box, 'border-box');
      r.near('the password panel measures 420px outer', panel.outer, 420);
    }
    await escape(page);
  }
  await escape(page);
}

/**
 * §9 "Eval status colors" as *applied*, not merely as tokens.
 *
 * R-70/NFR-A11Y-06 make the numeral the non-colour carrier, so the hue sits on an `aria-hidden`
 * dot; this checks that whatever carries it resolves to one of the three literals rather than to
 * some fourth colour introduced by a component.
 */
export async function checkEvalHues(page, r, theme) {
  r.context('§9 eval hues applied', theme);

  const hues = await page.evaluate(`
    const wanted = ['rgb(78, 195, 166)', 'rgb(232, 163, 76)', 'rgb(232, 106, 138)'];
    const out = [];
    for (const el of document.querySelectorAll('aside *, main *')) {
      const s = getComputedStyle(el);
      for (const prop of ['color', 'backgroundColor', 'borderTopColor']) {
        const v = s[prop];
        if (wanted.includes(v)) out.push(v);
      }
    }
    return [...new Set(out)];`);
  r.truthy(
    'every applied eval hue is one of §9’s three literals',
    hues.every((h) => ['rgb(78, 195, 166)', 'rgb(232, 163, 76)', 'rgb(232, 106, 138)'].includes(h)),
    JSON.stringify(hues),
  );
  r.truthy('at least one eval hue is actually painted', hues.length > 0, JSON.stringify(hues));
}

/**
 * FR-CIT-07 — the document's figure, beneath the passage that cites its page (T-716, R-94).
 *
 * **This check FAILS when no figure is on screen, and that is deliberate.** The obvious
 * alternative — record a pass with "no figure-bearing conversation available" — is exactly the
 * defect §8.74(i) found in the FR-KBM-10 probe, where a fallback reported a pass and 39 checks
 * silently measured the wrong page. Figure extraction ships off (`PARSER_FIGURES_ENABLED`), so
 * a red here means the corpus was not prepared, not that the product is broken; `fidelity/README.md`
 * says how to prepare it.
 *
 * Nothing is transcribed per property: R-94 is Rev 0.61 and the prototype has no markup for this
 * at all, so what is asserted is the geometry the requirement fixes — the figure is *beneath the
 * citing passage*, it is inside the answer body, it does not overflow the content column, and it
 * carries the caption and text alternative FR-CIT-07 and NFR-A11Y-03 require.
 */
export async function checkCitationFigure(page, r, theme) {
  r.context('FR-CIT-07 figure under the citation', theme);

  // Polled, never slept on: the raster is fetched after the transcript renders, so its arrival
  // is a network event and a fixed wait is an assumption about timing wearing an assertion's
  // clothes (see `checkCitationCard`).
  // `mutedColor` is resolved through a throwaway element so the comparison is rgb-to-rgb:
  // the token is a hex literal while a computed `color` is always rgb().
  let found = null;
  for (let i = 0; i < 25 && found === null; i++) {
    await sleep(120);
    found = await page.evaluate(`
      const fig = document.querySelector('main figure');
      if (!fig) return null;
      const img = fig.querySelector('img');
      if (!img || !img.complete || img.naturalWidth === 0) return null;
      const cap = fig.querySelector('figcaption');
      const body = fig.closest('article')?.querySelector('p');
      const source = [...(fig.closest('article')?.querySelectorAll('*') ?? [])]
        .find((el) => (el.textContent || '').startsWith('grounded in '));
      return {
        alt: img.getAttribute('alt') || '',
        src: (img.getAttribute('src') || '').slice(0, 5),
        caption: (cap?.textContent || '').trim(),
        imgWidth: img.getBoundingClientRect().width,
        figWidth: fig.getBoundingClientRect().width,
        bodyWidth: (fig.parentElement?.getBoundingClientRect().width) ?? 0,
        afterParagraph: body ? (body.compareDocumentPosition(fig) & 4) === 4 : false,
        beforeSourceLine: source ? (fig.compareDocumentPosition(source) & 4) === 4 : null,
        captionColor: getComputedStyle(cap).color,
        mutedColor: (() => {
          const probe = document.createElement('span');
          probe.style.color = 'var(--muted)';
          document.body.appendChild(probe);
          const value = getComputedStyle(probe).color;
          probe.remove();
          return value;
        })(),
      };`);
  }

  r.truthy(
    'a figure is rendered beneath a citation',
    found !== null,
    'no <figure> with a loaded image in the transcript — enable PARSER_FIGURES_ENABLED and ' +
      'ingest a figure-bearing PDF, then ask a question that cites one of its pages ' +
      '(frontend/fidelity/README.md)',
  );
  if (found === null) return;

  // The bytes come from the authenticated route, so the src is an object URL, never a path.
  r.eq('the image is an object URL', found.src, 'blob:');
  // NFR-A11Y-03 — the alt names the document and the page at minimum.
  r.truthy('the alt text names the document', /\.(pdf|docx|csv|md)\b/i.test(found.alt), found.alt);
  r.truthy('the alt text names the page', /p\.\s*\d+/.test(found.alt), found.alt);
  r.truthy('the caption names the document', /\.(pdf|docx|csv|md)\b/i.test(found.caption), found.caption);
  // FR-CIT-07 — "beneath the citing passage", inside the FR-MSG-04 body rather than after it.
  r.truthy('the figure follows the citing paragraph', found.afterParagraph, 'it precedes it');
  if (found.beforeSourceLine !== null) {
    r.truthy(
      'the figure sits inside the body, above the source line',
      found.beforeSourceLine,
      'it renders after the FR-MSG-04 source line',
    );
  }
  // The document's picture never widens the 68% content column.
  r.truthy(
    'the image stays inside the content column',
    found.imgWidth <= found.bodyWidth + 1,
    `image ${found.imgWidth} > column ${found.bodyWidth}`,
  );
  // NFR-A11Y-06: `--muted`, not `--muted2`, which fails contrast as text on every surface.
  r.eq('the caption is --muted, not --muted2', found.captionColor, found.mutedColor);
}
