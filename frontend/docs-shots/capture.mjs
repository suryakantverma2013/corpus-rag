/**
 * Capture the screenshots `docs/USER_GUIDE.md` embeds (T-706).
 *
 *   npm run docs:shots        # against http://localhost:5173 by default
 *   APP_URL=http://localhost:8088/ CORPUS_EMAIL=demo@corpus.local CORPUS_PASSWORD=... npm run docs:shots
 *
 * **Why a script rather than someone with a cropping tool.** These are the only files in the
 * repository that a test cannot check the *contents* of, so the one guarantee worth having is
 * that regenerating them is a single command against a known corpus — `tools.seed_demo`'s.
 * Hand-taken screenshots drift the moment two of them are taken on different days, and the
 * reader cannot tell which.
 *
 * **It reuses `fidelity/`'s driver deliberately.** The CDP launcher, `signIn`, `setTheme` and the
 * technique for opening each interactive surface are all solved there, and every one of them was
 * solved by getting it wrong first: the citation card is *polled* for rather than slept on
 * (T-722 — a fixed wait made it intermittent), the `@` menu is reached by its accessible name
 * rather than its glyph (T-510 — the probe searched for the emoji and reported a working control
 * as missing), and the shell shot pins an answered conversation because a run that ends by
 * creating an empty one changes what the next run photographs.
 *
 * **What it will not do is invent state.** Every surface here exists because the seeded corpus
 * puts it there. If a shot is missing, seed first (`uv run python -m tools.seed_demo run --yes`)
 * — the script says which surface it could not reach rather than writing a blank frame, because
 * a screenshot of the wrong thing is worse than an absent one and impossible to spot in review.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { launch } from '../fidelity/cdp.mjs';
import { APP, clickByText, setTheme, signIn, sleep, waitUntil } from '../fidelity/harness.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = process.env.DOCS_SHOTS ?? join(HERE, '..', '..', 'docs', 'images');

const missed = [];
let taken = 0;

/** Write one frame. Unlike `fidelity/`'s, this directory is COMMITTED — see the guide's guard. */
async function shot(page, name) {
  mkdirSync(OUT, { recursive: true });
  const data = await page.screenshot();
  writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, 'base64'));
  taken += 1;
  console.log(`  captured  ${name}.png`);
}

/** Reach a surface, then photograph it. A surface that cannot be reached is REPORTED, not faked. */
async function capture(page, name, reach) {
  const reached = reach === undefined ? true : await reach();
  if (!reached) {
    missed.push(name);
    console.log(`  MISSING   ${name}.png — could not reach the surface`);
    return false;
  }
  // Reaching a surface and *photographing* it are different waits. Every popover here animates
  // in over --motion-fast (0.15s), and `waitUntil` returns the instant the element exists - so
  // the first citation-card frame caught the card at partial opacity, mid fadeUp, and read as a
  // smudge overlapping the message above it. Polling for existence is still right (T-722); it
  // is simply not sufficient for a picture.
  await sleep(400);
  await shot(page, name);
  return true;
}

/**
 * Dismiss whatever `selector` matches, and **prove** it went away.
 *
 * Escape first, because that is the dismissal every dialog and popover here implements
 * (T-508's dialog stack); the visible `Close` button is the fallback. The wait at the end
 * is the whole point: three frames of this run were photographs of a modal the script
 * believed it had already closed.
 */
async function dismiss(page, selector, label) {
  const gone = `return document.querySelector(${JSON.stringify(selector)}) === null`;
  if (await page.evaluate(gone)) return true;

  for (const key of ['Escape']) {
    await page.send('Input.dispatchKeyEvent', {
      type: 'keyDown', key, code: key, windowsVirtualKeyCode: 27,
    });
    await page.send('Input.dispatchKeyEvent', {
      type: 'keyUp', key, code: key, windowsVirtualKeyCode: 27,
    });
  }
  if (await waitUntil(page, gone.replace('return ', ''), 2000)) return true;

  await clickByText(page, `${selector} button`, 'Close');
  if (await waitUntil(page, gone.replace('return ', ''), 2000)) return true;

  console.log(`  WARNING   ${label} would not dismiss - later frames may show it`);
  missed.push(`${label} (stuck open)`);
  return false;
}

/** Click the sidebar row whose title starts with `title`, and let the transcript settle. */
async function selectChat(page, title) {
  const picked = await page.evaluate(`
    const row = [...document.querySelectorAll('nav li button')]
      .find((b) => (b.textContent || '').trim().startsWith(${JSON.stringify(title)}));
    if (!row) return false;
    row.click(); return true;`);
  if (picked) await sleep(1600);
  return picked;
}

const page = await launch({ port: 9600, headless: false });

try {
  await page.send('Page.bringToFront');
  await page.navigate(APP);
  await setTheme(page, 'dark');

  // 1. The only surface that exists before a session.
  await waitUntil(page, `document.querySelector('form input[type=email]')`, 20000);
  await capture(page, 'sign-in');

  await signIn(page);
  // BY NAME, deliberately, and not `selectAnsweredChat`: that helper picks the first row
  // with any messages, and the seeded abstention is the newest conversation - so it chose a
  // chat with two messages and no citation, and every citation surface below was
  // unreachable. Selecting on message COUNT is exactly the weakness T-722 recorded. Here the
  // corpus is one we seed, so its titles are a contract and the right key to use.
  if (!(await selectChat(page, 'Q3 revenue'))) {
    throw new Error(
      'no "Q3 revenue" conversation - seed first: uv run python -m tools.seed_demo run --yes',
    );
  }

  // 2. The shell, on an answered conversation: transcript, citations, stats, composer.
  await capture(page, 'shell');

  // 3. The FR-CIT-03 hover card. Polled, never slept on (T-722).
  await capture(page, 'citation-card', async () => {
    const opened = await page.evaluate(`
      const chip = [...document.querySelectorAll('main button')]
        .find((b) => /\\.(md|pdf|docx|csv)\\b/i.test((b.textContent || '').trim()));
      if (!chip) return false;
      chip.focus(); return true;`);
    if (!opened) return false;
    return await waitUntil(page, `document.querySelector('[role="tooltip"]')`, 4000);
  });
  await page.evaluate(`document.activeElement && document.activeElement.blur(); true`);
  await sleep(300);

  // 4. The FR-CMP-04 mention menu, opened by accessible name rather than glyph (T-510).
  await capture(page, 'mention-menu', async () => {
    const opened = await page.evaluate(`
      const at = [...document.querySelectorAll('main button')]
        .find((b) => b.getAttribute('aria-label') === 'Reference a document');
      if (!at) return false;
      at.focus(); at.click(); return true;`);
    if (!opened) return false;
    return await waitUntil(page, `document.querySelector('[role="listbox"]')`, 4000);
  });
  await dismiss(page, '[role="listbox"]', 'mention menu');
  await sleep(300);

  // 5. The FR-KBM-01 knowledge-base modal, listing the seeded corpus.
  await capture(page, 'knowledge-base', async () => {
    const opened = await clickByText(page, 'button', 'documents');
    if (!opened) return false;
    return await waitUntil(page, `document.querySelector('[role="dialog"]')`, 5000);
  });
  await dismiss(page, '[role="dialog"]', 'knowledge base modal');
  await sleep(300);

  // 6. The FR-AUT-06 user menu and 7. the change-password modal.
  await capture(page, 'user-menu', async () => {
    const opened = await page.evaluate(`
      // Every conversation row also has aria-haspopup (its ... Rename/Delete menu), so the
      // FR-AUT-06 trigger is identified by NOT being inside a list item.
      const b = [...document.querySelectorAll('button[aria-haspopup="menu"]')]
        .find((x) => x.closest('li') === null);
      if (!b) return false;
      b.click(); return true;`);
    if (!opened) return false;
    return await waitUntil(page, `document.querySelector('[role="menu"]')`, 4000);
  });
  await capture(page, 'change-password', async () => {
    const opened = await clickByText(page, '[role="menu"] button, [role="menuitem"]', 'Change password');
    if (!opened) return false;
    return await waitUntil(page, `document.querySelector('[role="dialog"] input[type=password]')`, 5000);
  });
  await dismiss(page, '[role="dialog"]', 'change-password modal');
  await dismiss(page, '[role="menu"]', 'user menu');
  await sleep(300);

  // 8. An abstention — the shape FR-SYS-02 serves when the corpus cannot answer.
  await capture(page, 'abstention', async () => {
    const picked = await page.evaluate(`
      const row = [...document.querySelectorAll('nav li button, nav li a')]
        .find((b) => (b.textContent || '').includes('Unanswerable'));
      if (!row) return false;
      row.click(); return true;`);
    if (!picked) return false;
    await sleep(1500);
    // `evaluate` wraps its argument in a FUNCTION BODY, so a bare expression returns
    // undefined and reads as 'surface unreachable'. This cost one wrong diagnosis.
    return await page.evaluate(
      `return /could not|couldn|can't ground|cannot ground/i.test(document.querySelector('main').textContent)`,
    );
  });

  // 9. The same shell in the light theme (FR-THM-01).
  await selectChat(page, 'Q3 revenue');
  await setTheme(page, 'light');
  await sleep(500);
  await capture(page, 'shell-light');
  await setTheme(page, 'dark');
} finally {
  await page.close();
}

console.log(`\n${taken} screenshot(s) written to ${OUT}`);
if (missed.length > 0) {
  console.log(`could not reach: ${missed.join(', ')}`);
  console.log('seed the demo corpus first: cd backend && uv run python -m tools.seed_demo run --yes');
  process.exit(1);
}
