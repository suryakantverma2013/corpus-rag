#!/usr/bin/env node
/**
 * T-510 — the pixel-fidelity pass, as R-66(5) rescoped it.
 *
 * **Not a screenshot diff against the prototype.** `support.js` was never shipped in the design
 * handoff, so the prototype's template runtime never executes: every binding renders as literal
 * text, all 18 `<sc-for>` blocks stay unexpanded and all 36 `<sc-if>` regions never appear. Its
 * inline `style` declarations are complete and *are* the NFR-VIS-01 baseline, so fidelity is
 * verified as **computed values against those declarations and against §9's literal table**, in
 * both themes — which is what T-502..T-514 each did for their own surface, and this is the
 * end-to-end pass that also owns the rows belonging to no single component.
 *
 * Screenshots are written for human review only. They are deliberately **not** committed and not
 * diffed: R-66(5) records that a screenshot comparison is sensitive to font-load timing and
 * subpixel antialiasing, and blurs precisely the 264-vs-266px error an assertion states outright.
 *
 * Run it with the dev server and the backend up:
 *
 *   cd frontend && CORPUS_PASSWORD=... node fidelity/run.mjs
 *
 * Exit code is the number of failed checks.
 */
import { launch } from './cdp.mjs';
import {
  APP,
  SHOTS,
  THEMES,
  reporter,
  selectAnsweredChat,
  setTheme,
  shoot,
  signIn,
} from './harness.mjs';
import { checkFonts, checkMotion, checkScrollbars, checkTokens } from './checks/global.mjs';
import { checkShell, checkStatsExpansion } from './checks/layout.mjs';
import {
  checkChatCopy,
  checkCloudPicker,
  checkEmptyStates,
  checkKbCopy,
  checkLoginCopy,
  checkStatsCopy,
} from './checks/copy.mjs';
import {
  checkActionBar,
  checkCitationCard,
  checkCitationFigure,
  checkEvalHues,
  checkMentionMenu,
  checkUserMenu,
} from './checks/surfaces.mjs';

const HEADLESS = process.env.FIDELITY_HEADLESS === '1';
const r = reporter();

console.log(
  `\nT-510 fidelity pass — ${APP} (${HEADLESS ? 'HEADLESS: scrollbar checks are inconclusive' : 'headed'})\n`,
);

const page = await launch({ port: 9500, headless: HEADLESS });
try {
  await page.send('Page.bringToFront');
  await page.navigate(APP);

  // ── Pre-authentication: the login screen is the only surface reachable without a session ──
  for (const theme of THEMES) {
    await setTheme(page, theme);
    console.log(`  login / ${theme}`);
    await checkTokens(page, r, theme);
    await checkFonts(page, r, theme);
    await checkLoginCopy(page, r, theme);
    await shoot(page, `login-${theme}`);
  }
  await setTheme(page, 'dark');

  await signIn(page);

  // Pin the state the shell checks read. Without this the run inherits whichever conversation was
  // last selected — and the previous run ends by creating an empty one, which swaps the FR-ANL-04
  // labels for their empty state and fails eight assertions on an unchanged build.
  const chat = await selectAnsweredChat(page);
  r.context('harness', 'dark');
  r.truthy(
    'a conversation with at least one turn is available to measure',
    chat !== null,
    'none found — the answered-state surfaces below cannot be reached; send a message first',
  );

  // ── The shell and everything inside it ──
  for (const theme of THEMES) {
    await setTheme(page, theme);
    console.log(`  shell / ${theme}`);
    await checkShell(page, r, theme);
    await checkChatCopy(page, r, theme);
    await checkStatsCopy(page, r, theme);
    await checkScrollbars(page, r, theme);
    await shoot(page, `shell-${theme}`);

    await checkMentionMenu(page, r, theme);
    await checkCitationCard(page, r, theme);
    await checkCitationFigure(page, r, theme);
    await checkActionBar(page, r, theme);
    await checkEvalHues(page, r, theme);
    await checkUserMenu(page, r, theme);

    await checkKbCopy(page, r, theme);
    await shoot(page, `kb-modal-${theme}`);

    await checkCloudPicker(page, r, theme);
    await shoot(page, `cloud-picker-${theme}`);
  }

  // Motion is emulated globally, so it runs once rather than per theme — the tokens it reads are
  // theme-independent and live in their own bare `:root` block (R-59(3)).
  await setTheme(page, 'dark');
  await checkMotion(page, r, 'dark');

  await checkStatsExpansion(page, r, 'dark');

  for (const theme of THEMES) {
    await setTheme(page, theme);
    console.log(`  empty states / ${theme}`);
    await checkEmptyStates(page, r, theme);
    await shoot(page, `empty-${theme}`);
  }

  console.log(`\n  screenshots → ${SHOTS}`);
} catch (error) {
  r.context('harness', '-');
  r.truthy(
    'the run completed without throwing',
    false,
    String(error && error.message ? error.message : error),
  );
  console.error(error);
} finally {
  const failures = r.summary();
  await page.close();
  process.exit(failures);
}
