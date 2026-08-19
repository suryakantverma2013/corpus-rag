/**
 * §9's copy literals, asserted against the running product.
 *
 * Copy is checked here rather than in a unit test because §9 is an *acceptance* table: the claim
 * is that these strings reach the screen, and a component test can only show that a component
 * given the right props would render them.
 */
import { APP, clickByText, selectAnsweredChat, sleep, waitUntil } from '../harness.mjs';

/** Text of every leaf element currently on screen — the haystack for a literal. */
const LEAVES = `
  return [...document.querySelectorAll('body *')]
    .filter((e) => e.children.length === 0)
    .map((e) => (e.textContent || '').trim())
    .filter(Boolean);`;

const onScreen = async (page, text) =>
  page.evaluate(`return document.body.innerText.includes(${JSON.stringify(text)});`);

export async function checkLoginCopy(page, r, theme) {
  r.context('§9 login copy', theme);

  for (const literal of [
    'Sign in to your knowledge base',
    'EMAIL',
    'PASSWORD',
    'Sign in',
    'Forgot your password? Contact your administrator.',
  ])
    r.truthy(`renders ${JSON.stringify(literal)}`, await onScreen(page, literal));

  // §4.17's card is `border-box` (T-509), so 380 is the OUTER width — the opposite of FR-KBM-01's
  // 520, and the reason that figure is asserted as measured rather than as declared.
  const card = await page.evaluate(`
    const f = document.querySelector('form');
    if (!f) return null;
    const s = getComputedStyle(f);
    return { box: s.boxSizing, w: f.getBoundingClientRect().width };`);
  r.truthy('the login card exists', card !== null);
  if (card !== null) {
    r.eq('the login card is border-box', card.box, 'border-box');
    r.near('the login card measures 380px outer', card.w, 380);
  }
}

export async function checkChatCopy(page, r, theme) {
  r.context('§9 chat copy', theme);

  const placeholder = await page.evaluate(`
    const el = document.querySelector('main textarea, main input[type=text]');
    return el ? el.placeholder : null;`);
  r.eq('composer placeholder', placeholder, 'Ask a question — @ to cite a document');

  const footer = (await page.evaluate(LEAVES)).find((t) => t.includes('Enter to send'));
  r.truthy('composer footer is present', footer !== undefined, `got ${footer}`);
  if (footer !== undefined) {
    r.truthy(
      'composer footer matches `Responses grounded in {N} documents · Enter to send`',
      /^Responses grounded in \d+ documents? · Enter to send$/.test(footer),
      `got ${JSON.stringify(footer)}`,
    );
  }
}

export async function checkStatsCopy(page, r, theme) {
  r.context('§9 stats copy', theme);

  for (const literal of [
    'SESSION',
    'DURATION',
    'MESSAGES',
    'MODEL',
    'CONTEXT WINDOW',
    'Context synthesis · grounding on',
  ])
    r.truthy(`renders ${JSON.stringify(literal)}`, await onScreen(page, literal));

  // §9 "Context window limit / label format" — `3.8K / 10.4K`.
  const meter = (await page.evaluate(LEAVES)).find((t) => /^[\d.]+K \/ [\d.]+K$/.test(t));
  r.truthy(
    'the context meter uses the `{used}K / {limit}K` format',
    meter !== undefined,
    `got ${meter}`,
  );
  if (meter !== undefined)
    r.truthy('the meter limit is 10.4K', meter.endsWith('/ 10.4K'), `got ${meter}`);

  // §9 "DeepEval labels" — only the first two ever carry a score per message (R-50(1)).
  for (const label of ['Relevancy', 'Faithfulness', 'Ctx Precision', 'Ctx Recall'])
    r.truthy(`renders the DeepEval label ${JSON.stringify(label)}`, await onScreen(page, label));

  // §9 "DeepEval score tooltip" — one constant, shared by the chip and the FR-ANL-04 card (R-70).
  const tooltip = await page.evaluate(`
    const el = [...document.querySelectorAll('[title]')].find(e => (e.title||'').includes('DeepEval metric'));
    return el ? el.title : null;`);
  r.eq(
    'the DeepEval tooltip is the R-70 constant',
    tooltip,
    'DeepEval metric — indicative judge score, not an exact measurement',
  );
}

export async function checkKbCopy(page, r, theme) {
  r.context('§9 knowledge-base copy', theme);

  const opened = await clickByText(page, 'nav button', 'Knowledge base');
  r.truthy('the knowledge-base modal opens', opened);
  if (!opened) return;
  r.truthy(
    'its dialog is mounted',
    await waitUntil(page, `document.querySelector('[role="dialog"]')`),
    'no [role="dialog"] appeared within 3s',
  );

  for (const literal of [
    'Global documents are searched in every chat; attachments only in this one.',
    'PDF · DOCX · CSV · MD — max 50 MB',
    'Global',
    'This chat',
  ])
    r.truthy(`renders ${JSON.stringify(literal)}`, await onScreen(page, literal));

  // FR-KBM-01's 520px is a CONTENT width — the prototype ships no `box-sizing` reset — so the
  // panel measures 566 outer (520 + 22×2 padding + 1×2 border). T-508 measured exactly this, and
  // it is the assertion a `border-box` reset would silently break.
  const panel = await page.evaluate(`
    const d = [...document.querySelectorAll('[role="dialog"]')].pop();
    if (!d) return null;
    const s = getComputedStyle(d);
    return { box: s.boxSizing, declared: s.width, outer: d.getBoundingClientRect().width, maxH: s.maxHeight };`);
  r.truthy('the modal panel exists', panel !== null);
  if (panel !== null) {
    r.eq('FR-KBM-01 declares a 520px content width', panel.declared, '520px');
    r.near('the panel measures 566px outer', panel.outer, 566);
    r.truthy('FR-KBM-01 max-height is 78vh', panel.maxH !== 'none', `got ${panel.maxH}`);
  }

  await page.evaluate(`
    const b = [...document.querySelectorAll('[role="dialog"] button')].find(x => /Close/i.test(x.getAttribute('aria-label')||''));
    if (b) b.click(); return true;`);
  await sleep(500);
}

/**
 * The FR-KBM-10 cloud picker, nested over the knowledge-base modal.
 *
 * Spec-authored under NFR-VIS-01's Rev 0.5 carve-out — the prototype has no markup for it at all
 * (§8.61(7)) — so what is checkable is that it introduces no new visual vocabulary: it reuses the
 * modal's own panel treatment and the four FR-ING-02 formats it filters to. It needs a linked
 * Drive account (FR-AUT-11); without one the picker cannot open, and that is reported rather than
 * failed, because it is an environment fact and not a fidelity defect.
 */
export async function checkCloudPicker(page, r, theme) {
  r.context('FR-KBM-10 cloud picker', theme);

  const openedModal = await clickByText(page, 'nav button', 'Knowledge base');
  if (!openedModal) {
    r.truthy('the knowledge-base modal opens', false);
    return;
  }
  await waitUntil(page, `document.querySelector('[role="dialog"]')`);
  await clickByText(page, '[role="dialog"] button', 'cloud drive');
  // The picker lists real Drive files over the network, so it is given longer than a local render.
  await waitUntil(page, `document.querySelectorAll('[role="dialog"]').length > 1`, 8000);

  const depth = await page.evaluate(`return document.querySelectorAll('[role="dialog"]').length;`);
  if (depth < 2) {
    r.record(
      'the picker opens',
      true,
      'no linked Drive account in this environment — not a fidelity failure',
    );
    // FR-KBM-06 branches on the linked state (R-74(5)) and with no linked account the button is
    // **not inert** — `useCloudLink` calls `window.location.assign` and the whole browser leaves
    // for Keycloak's account-link endpoint. So there is no Close button to press here, and every
    // later check in the run would otherwise measure Keycloak's login page. Restore the app.
    //
    // T-606 found this by looking at a screenshot: the light-theme pass, motion, expansion and
    // both empty states — 39 checks — had run against PatternFly. This branch had never executed
    // before, because the audit account was still linked when T-510 and T-514 wrote it.
    const inApp = await page.evaluate(`return !!document.querySelector('[role="dialog"]');`);
    if (inApp) {
      await page.evaluate(`
        const b = [...document.querySelectorAll('[role="dialog"] button')].find(x => /Close/i.test(x.getAttribute('aria-label')||''));
        if (b) b.click(); return true;`);
      await sleep(400);
      return;
    }
    await page.navigate(APP);
    await selectAnsweredChat(page);
    return;
  }

  r.truthy(
    'the picker nests over the modal rather than replacing it',
    depth === 2,
    `depth=${depth}`,
  );
  r.truthy('renders the four ingestible formats', await onScreen(page, 'PDF · DOCX · CSV · MD'));
  r.truthy('renders its own section heading', await onScreen(page, 'YOUR DRIVE FILES'));

  const panel = await page.evaluate(`
    const d = [...document.querySelectorAll('[role="dialog"]')].pop();
    const s = getComputedStyle(d);
    return { radius: s.borderTopLeftRadius, bg: s.backgroundColor, border: s.borderTopWidth };`);
  const modal = await page.evaluate(`
    const d = [...document.querySelectorAll('[role="dialog"]')][0];
    const s = getComputedStyle(d);
    return { radius: s.borderTopLeftRadius, bg: s.backgroundColor, border: s.borderTopWidth };`);
  r.eq('the picker reuses the modal’s corner radius', panel.radius, modal.radius);
  r.eq('the picker reuses the modal’s surface colour', panel.bg, modal.bg);
  r.eq('the picker reuses the modal’s border weight', panel.border, modal.border);

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
  await sleep(500);
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
  await sleep(500);
}

/**
 * §9's three empty states, reached rather than waited for.
 *
 * A fresh conversation produces all three at once — no messages, no scores, no sources — so this
 * clicks New chat first. Reporting "not applicable in this state" would have been a check that
 * always passes, which is the vacuous-assertion trap T-507 recorded.
 */
export async function checkEmptyStates(page, r, theme) {
  r.context('§9 empty states', theme);

  /*
   * Reuse an empty conversation if one exists, and only create one if none does.
   *
   * The first version clicked New chat unconditionally, which left two fresh conversations in the
   * database on every run — visible as a sidebar full of "0 messages" rows after a few passes. A
   * check that has to mutate durable state to observe a state should reuse what is already there:
   * this harness is expected to run often, and it is measuring the product, not adding to it.
   */
  const reused = await page.evaluate(`
    const row = [...document.querySelectorAll('nav li button, nav li a')]
      .find((b) => /\\b0\\s+messages\\b/.test(b.textContent || ''));
    if (!row) return false;
    row.click();
    return true;`);
  if (reused) {
    await sleep(1200);
  } else {
    const started = await clickByText(page, 'nav button', 'New chat');
    r.truthy('an empty chat is reachable', started);
    if (!started) return;
    await sleep(1200);
  }

  for (const [name, literal] of [
    ['chat empty state', 'Ask your knowledge base'],
    ['eval empty state', 'Scores appear once a response is evaluated.'],
    ['sources empty state', 'None yet — answers will list their sources here.'],
  ])
    r.truthy(`${name}: ${JSON.stringify(literal)}`, await onScreen(page, literal));
}
