#!/usr/bin/env node
/**
 * T-614 — the axe pass, committed as a runnable script.
 *
 * `frontend/ACCESSIBILITY.md` is a prose write-up of an injection performed by hand during T-511
 * and T-514, so NFR-A11Y-06's conformance claim was the one acceptance statement in the product
 * that nobody could re-run. This is that pass, on `frontend/fidelity/`'s precedent — same CDP
 * driver, same reporter, same exit-code-is-failures contract.
 *
 * **It does not assert zero violations.** See `accepted.mjs`: the palette's contrast failures are
 * NFR-A11Y-06's recorded exceptions, so the assertion is *no violation outside the enumerated
 * set* — a rule that is not `color-contrast` or `region`, or a colour used as text that the
 * enumeration never sanctioned. A new pair appearing is the failure this exists to catch.
 *
 * Run it with the dev server (or any built origin) and the backend up:
 *
 *   cd frontend && CORPUS_PASSWORD=... node a11y/run.mjs
 *   cd frontend && CORPUS_PASSWORD=... APP_URL=http://localhost:8088/ node a11y/run.mjs
 *
 * Headless is fine here, unlike fidelity: nothing below measures a scrollbar or a focus ring,
 * which are R-60(4)'s reasons that harness must run headed.
 *
 * Exit code is the number of failed checks.
 */
import { launch } from '../fidelity/cdp.mjs';
import {
  APP,
  THEMES,
  clickByText,
  reporter,
  selectAnsweredChat,
  setTheme,
  signIn,
  sleep,
  waitUntil,
} from '../fidelity/harness.mjs';
import { injectAxe, runAxe } from './axe.mjs';
import { ACCEPTED_FOREGROUNDS, ACCEPTED_RULES, normaliseColour } from './accepted.mjs';

const HEADLESS = process.env.A11Y_HEADLESS !== '0';
const r = reporter();
/** Every accepted contrast finding, for the summary — the enumeration's own evidence. */
const seen = new Map();

/**
 * Wait until a surface has finished animating before measuring it.
 *
 * **This is a correctness requirement, not politeness — measured.** Popovers and dialogs open
 * through the global `.animate-fade-up` utility, and axe reads *computed* colour: sampled at
 * opacity 0.4 a menu item's foreground is blended into the panel beneath it, and axe reported
 * `#1d222f` on `#181d2a` at **1.05:1** — a "new colour pair" that exists for 150ms and belongs
 * to no token. It fired in the dark pass only, because dark ran first and was therefore the one
 * that raced. A harness that is occasionally red is one people learn to re-run until it is
 * green (§8.64), so the wait is on the animation's own `playState` rather than a sleep, which
 * would be an assumption about timing wearing an assertion's clothes.
 */
async function settle(page, selector) {
  const target =
    selector === null
      ? 'document.documentElement'
      : `document.querySelector(${JSON.stringify(selector)})`;
  return waitUntil(
    page,
    `(() => {
      const el = ${target};
      if (!el) return false;
      const running = typeof el.getAnimations === 'function' ? el.getAnimations({ subtree: true }) : [];
      if (!running.every((a) => a.playState === 'finished' || a.playState === 'idle')) return false;
      return selectorOpacityOk(el);
      function selectorOpacityOk(node) {
        for (let n = node; n instanceof Element; n = n.parentElement)
          if (Number(getComputedStyle(n).opacity) < 1) return false;
        return true;
      }
    })()`,
  );
}

/**
 * Run axe over one surface and hold it to the enumerated set.
 *
 * `selector === null` scopes to the document, which is correct only for surfaces with nothing
 * open over them. Anything modal or popover is scoped to its own panel — see `runAxe`.
 */
async function probe(page, surface, theme, selector) {
  r.context(surface, theme);
  await injectAxe(page);
  const still = await settle(page, selector);
  r.truthy(
    'the surface stopped animating before it was measured',
    still,
    'still animating after 3s — any contrast read here is a blend, not a token pair',
  );
  const result = await runAxe(page, selector);

  if (result === null || result === undefined || result.error !== undefined) {
    r.truthy(
      'the surface was reachable to measure',
      false,
      result === null || result === undefined ? 'axe returned nothing' : result.error,
    );
    return;
  }

  for (const violation of result.violations) {
    // The WCAG claim: nothing outside the two accepted rules.
    r.truthy(
      `no unaccepted rule fires (${violation.id})`,
      ACCEPTED_RULES.has(violation.id),
      `${violation.id} [${violation.impact}] on ${violation.nodes} node(s), ` +
        `e.g. ${violation.example} — tags ${violation.tags.join(',')}`,
    );

    // Rule 5: no NEW colour pair below threshold.
    for (const node of violation.contrast) {
      const fg = normaliseColour(node.fg);
      if (ACCEPTED_FOREGROUNDS.has(fg)) {
        const entry = seen.get(fg) ?? { min: Infinity, nodes: 0 };
        entry.min = Math.min(entry.min, node.ratio);
        entry.nodes += 1;
        seen.set(fg, entry);
      } else {
        r.truthy(
          `no new colour pair below threshold (${fg})`,
          false,
          `${fg} on ${normaliseColour(node.bg)} at ${node.ratio}:1 (${node.fontSize}, ` +
            `weight ${node.fontWeight}) on ${node.target} — not in NFR-A11Y-06's enumeration`,
        );
      }
    }
  }

  // Anti-vacuity: a probe that measured an empty root reports a clean surface. Without this, a
  // selector that stopped matching would read as a pass — §8.64(3)'s lesson about instruments.
  r.truthy(
    'axe evaluated a non-empty rule set',
    typeof result.tested === 'number' && result.tested > 0,
    `only ${result.tested} rules were applicable — the scoped root was probably empty`,
  );
}

/** Close whatever is open, so the next surface starts from the shell. */
async function dismissAll(page) {
  for (let i = 0; i < 5; i++) {
    const open = await page.evaluate(
      `return document.querySelectorAll('[role="dialog"],[role="menu"],[role="listbox"]').length;`,
    );
    if (open === 0) return;
    for (const type of ['keyDown', 'keyUp'])
      await page.send('Input.dispatchKeyEvent', {
        type,
        key: 'Escape',
        code: 'Escape',
        windowsVirtualKeyCode: 27,
        nativeVirtualKeyCode: 27,
      });
    await sleep(180);
  }
}

console.log(`\nT-614 accessibility pass (axe-core) — ${APP}\n`);

const page = await launch({ port: 9600, headless: HEADLESS });
try {
  await page.send('Page.bringToFront');
  await page.navigate(APP);

  // Pre-authentication: the login screen is the only surface reachable without a session.
  for (const theme of THEMES) {
    await setTheme(page, theme);
    console.log(`  login / ${theme}`);
    await probe(page, 'login (§4.17)', theme, null);
  }
  await setTheme(page, 'dark');

  await signIn(page);
  const chat = await selectAnsweredChat(page);
  r.context('harness', 'dark');
  r.truthy(
    'a conversation with at least one turn is available',
    chat !== null,
    'none found — the citation card cannot be reached; send a message first',
  );

  for (const theme of THEMES) {
    await setTheme(page, theme);
    console.log(`  authenticated surfaces / ${theme}`);

    await dismissAll(page);
    await probe(page, 'shell (§4.2)', theme, null);

    // Conversation menu (FR-SBR-07) — scoped: it is a popover over the shell.
    const menuOpened = await page.evaluate(`
      const b = [...document.querySelectorAll('nav button')]
        .find((x) => /^Actions for /.test(x.getAttribute('aria-label') || ''));
      if (!b) return false;
      b.click(); return true;`);
    r.context('conversation menu (FR-SBR-07)', theme);
    if (menuOpened === true && (await waitUntil(page, `document.querySelector('[role="menu"]')`))) {
      await probe(page, 'conversation menu (FR-SBR-07)', theme, '[role="menu"]');
    } else {
      r.truthy('the actions menu opens', false, 'no [role="menu"] appeared');
    }
    await dismissAll(page);

    // User menu, then the change-password modal it opens (§4.17).
    if (await clickByText(page, 'nav button[aria-haspopup="menu"]', 'Corpus')) {
      await waitUntil(page, `document.querySelector('[role="menu"]')`);
      await probe(page, 'user menu (§4.17)', theme, '[role="menu"]');
      if (await clickByText(page, '[role="menuitem"]', 'Change password')) {
        await waitUntil(page, `document.querySelector('[role="dialog"]')`);
        await probe(page, 'change password (§4.17)', theme, '[role="dialog"]');
      }
    }
    await dismissAll(page);

    // Mention menu (FR-CMP-04) — a listbox, not a dialog.
    const at = await page.evaluate(`
      const b = [...document.querySelectorAll('main button')]
        .find((x) => x.getAttribute('aria-label') === 'Reference a document');
      if (!b) return false;
      b.focus(); b.click(); return true;`);
    if (at === true && (await waitUntil(page, `document.querySelector('[role="listbox"]')`))) {
      await probe(page, 'mention menu (FR-CMP-04)', theme, '[role="listbox"]');
    } else {
      r.context('mention menu (FR-CMP-04)', theme);
      r.truthy('the @ control opens the mention menu', false, 'no [role="listbox"] appeared');
    }
    await dismissAll(page);

    // Citation hover card (FR-CIT-03) — waited for, never slept on (§8.64's intermittency lesson).
    const chip = await page.evaluate(`
      const c = [...document.querySelectorAll('main button')]
        .find((b) => /\\.(md|pdf|docx|csv)\\b/i.test((b.textContent || '').trim()));
      if (!c) return false;
      c.focus(); return true;`);
    if (chip === true && (await waitUntil(page, `document.querySelector('[role="tooltip"]')`))) {
      await probe(page, 'citation card (FR-CIT-03)', theme, '[role="tooltip"]');

      // FR-CIT-07 (T-716). Scoped to the figure rather than the document, and probed only when
      // one is present: figure extraction ships off, so a run against a corpus without figures
      // has nothing to measure. `image-alt` is NOT in ACCEPTED_RULES, so a missing text
      // alternative fails the run on its own — that is the guard, and it costs nothing.
      const hasFigure = await page.evaluate(
        `return document.querySelector('main figure img') !== null;`,
      );
      if (hasFigure) {
        await probe(page, 'citation figure (FR-CIT-07)', theme, 'main figure');
      } else {
        r.context('citation figure (FR-CIT-07)', theme);
        r.truthy(
          'a figure is present to measure',
          false,
          'no <figure> in the transcript — enable PARSER_FIGURES_ENABLED and ingest a ' +
            'figure-bearing PDF (frontend/fidelity/README.md)',
        );
      }
    } else {
      r.context('citation card (FR-CIT-03)', theme);
      r.truthy('a citation chip opens the card', false, 'no [role="tooltip"] appeared');
    }
    await dismissAll(page);

    // Knowledge-base modal (§4.7), and the FR-KBM-10 picker nested over it.
    if (await clickByText(page, 'nav button', 'Knowledge base')) {
      await waitUntil(page, `document.querySelector('[role="dialog"]')`);
      await probe(page, 'knowledge base (§4.7)', theme, '[role="dialog"]');

      await clickByText(page, '[role="dialog"] button', 'cloud drive');
      const nested = await waitUntil(
        page,
        `document.querySelectorAll('[role="dialog"]').length > 1`,
        8000,
      );
      r.context('cloud picker (FR-KBM-10)', theme);
      if (nested) {
        // Scoped to the picker itself. Unscoped here, axe measures the KB modal and the shell
        // behind the scrim and reports 1.01:1 — §8.63(7), and the reason `runAxe` takes a root.
        await probe(page, 'cloud picker (FR-KBM-10)', theme, '[role="dialog"]:last-of-type');

        /*
         * FR-AUT-11's unlink confirmation — a THIRD dialog, and the surface that makes the
         * scoping rule observable: §8.63(7) measured the picker's rows and the sidebar at
         * 1.01:1 with this open, because unscoped axe reads `--text` through the scrim. It is
         * opened, measured and CANCELLED — never confirmed, since `Disconnect` would revoke the
         * Drive link the picker above depends on. `ui/ConfirmDialog` is also the only place the
         * FR-EVL-03 hues occur as text (`--eval-bad` at 3.06:1 light), which is the pair T-511
         * reported as not occurring and T-514 corrected.
         */
        if (await clickByText(page, '[role="dialog"] button', 'Unlink')) {
          const third = await waitUntil(
            page,
            `document.querySelectorAll('[role="dialog"]').length > 2`,
          );
          r.context('unlink confirmation (FR-AUT-11)', theme);
          if (third) {
            await probe(
              page,
              'unlink confirmation (FR-AUT-11)',
              theme,
              '[role="dialog"]:last-of-type',
            );
          } else {
            r.truthy('the unlink confirmation opens', false, 'no third [role="dialog"] appeared');
          }
        }
      } else {
        // T-606's finding, encoded: with no linked account this button is a FULL PAGE REDIRECT
        // (`useCloudLink` -> `window.location.assign`, R-74(5)), and the fidelity harness recorded
        // that as a pass and then measured Keycloak for 39 checks. Not reaching a surface is a
        // failure to measure, never a pass — and leaving the origin is worse, because every check
        // after it silently measures another product.
        const here = await page.evaluate(`return location.origin + location.pathname;`);
        const stayed = typeof here === 'string' && here.startsWith(new URL(APP).origin);
        r.truthy(
          'the cloud picker was reached',
          false,
          stayed
            ? 'no second dialog — is a Drive account linked in this environment?'
            : `the browser LEFT the app origin (now ${here}) — nothing below was measured`,
        );
        if (!stayed) throw new Error(`navigated away from ${APP} to ${here}`);
      }
    }
    await dismissAll(page);
  }
} catch (error) {
  r.context('harness', '-');
  r.truthy('the run completed without throwing', false, String(error?.message ?? error));
  console.error(error);
} finally {
  // The enumeration's own evidence: what was accepted, and the worst ratio seen for each.
  if (seen.size > 0) {
    console.log('\n  accepted color-contrast foregrounds (NFR-A11Y-06):');
    for (const [fg, { min, nodes }] of [...seen.entries()].sort())
      console.log(
        `    ${fg}  min ${min.toFixed(2)}:1  ${String(nodes).padStart(4)} nodes  — ` +
          ACCEPTED_FOREGROUNDS.get(fg),
      );
  }
  const failures = r.summary();
  await page.close();
  process.exit(failures);
}
