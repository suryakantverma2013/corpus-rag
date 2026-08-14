/**
 * Shared machinery for the T-510 fidelity checks: the reporter, theme switching, sign-in, and
 * the small vocabulary of assertions the surface modules are written in.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const APP = process.env.APP_URL ?? 'http://localhost:5173/';
export const EMAIL = process.env.CORPUS_EMAIL ?? 'admin@corpus.local';
export const PASSWORD = process.env.CORPUS_PASSWORD ?? '';
export const SHOTS = process.env.FIDELITY_SHOTS ?? join(process.cwd(), 'fidelity', 'screenshots');

export const THEMES = ['dark', 'light'];

/**
 * A PASS/FAIL reporter that groups by surface and returns the failure count as an exit code.
 *
 * Every check records the value it *measured*, not just a boolean, because the one thing this
 * harness must never do is report a wrong expectation as a product defect — three separate tasks
 * (T-507, T-508, T-506) found their probe was wrong rather than the code, and the measured value
 * beside the expected one is what makes that visible in the log.
 */
export function reporter() {
  const rows = [];
  let surface = '';
  let theme = '';

  const context = (nextSurface, nextTheme) => {
    surface = nextSurface;
    theme = nextTheme;
  };

  const record = (name, ok, detail) => {
    rows.push({ surface, theme, name, ok, detail });
    if (!ok) console.log(`    FAIL  [${theme}] ${name} — ${detail}`);
    return ok;
  };

  /** Deep-equality-free comparison: everything here is a string or a number. */
  const eq = (name, actual, expected) =>
    record(
      name,
      actual === expected,
      `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );

  /** Geometry, with a tolerance — subpixel layout means an exact === is the wrong instrument. */
  const near = (name, actual, expected, tolerance = 0.5) =>
    record(
      name,
      typeof actual === 'number' && Math.abs(actual - expected) <= tolerance,
      `expected ${expected}±${tolerance}, got ${actual}`,
    );

  const truthy = (name, actual, detail = '') =>
    record(name, !!actual, detail || `got ${JSON.stringify(actual)}`);

  const oneOf = (name, actual, expected) =>
    record(
      name,
      expected.includes(actual),
      `expected one of ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );

  const summary = () => {
    const failed = rows.filter((r) => !r.ok);
    const bySurface = new Map();
    for (const r of rows) {
      const key = r.surface;
      const entry = bySurface.get(key) ?? { total: 0, bad: 0 };
      entry.total += 1;
      if (!r.ok) entry.bad += 1;
      bySurface.set(key, entry);
    }
    console.log('\n' + '='.repeat(64));
    for (const [name, { total, bad }] of bySurface)
      console.log(`  ${bad === 0 ? 'ok  ' : 'FAIL'}  ${name.padEnd(30)} ${total - bad}/${total}`);
    console.log('='.repeat(64));
    console.log(`  ${rows.length - failed.length}/${rows.length} checks passed`);
    if (failed.length > 0) {
      console.log('\nFAILURES:');
      for (const f of failed)
        console.log(`  [${f.surface}/${f.theme}] ${f.name}\n      ${f.detail}`);
    }
    return failed.length;
  };

  return { context, record, eq, near, truthy, oneOf, summary, rows };
}

/** Apply a theme the way the product does, and let the transition settle. */
export async function setTheme(page, theme) {
  await page.evaluate(
    `document.documentElement.setAttribute('data-theme', ${JSON.stringify(theme)});
     localStorage.setItem('corpus.theme', ${JSON.stringify(theme)});
     return true;`,
  );
  await sleep(180);
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Poll until an expression is truthy, and report whether it ever became so.
 *
 * Prefer this to `sleep()` before any assertion that something has appeared. A fixed wait is an
 * assumption about timing wearing an assertion's clothes: it passes until the machine is busy,
 * then fails on an unchanged build — and an occasionally-red fidelity harness teaches you to
 * re-run it until it is green, which is how the whole instrument stops meaning anything.
 */
export async function waitUntil(page, expression, timeoutMs = 3000, step = 120) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await page.evaluate(`return !!(${expression})`)) return true;
    if (Date.now() > deadline) return false;
    await sleep(step);
  }
}

/** `getComputedStyle` for one element, returning only the properties asked for. */
export function computed(selector, props) {
  return `
    const el = document.querySelector(${JSON.stringify(selector)});
    if (el === null) return null;
    const s = getComputedStyle(el);
    const out = {};
    for (const p of ${JSON.stringify(props)}) out[p] = s.getPropertyValue(p).trim();
    const r = el.getBoundingClientRect();
    out._w = r.width; out._h = r.height; out._x = r.left; out._y = r.top;
    out._ow = el.offsetWidth; out._oh = el.offsetHeight;
    out._text = (el.textContent || '').trim();
    return out;`;
}

/** Resolve a custom property off `:root`, as the browser computes it. */
export function token(name) {
  return `return getComputedStyle(document.documentElement).getPropertyValue(${JSON.stringify(name)}).trim();`;
}

/** Sign in with the keyboard-free path: fill the fields directly and submit the form. */
export async function signIn(page) {
  if (PASSWORD === '') {
    throw new Error(
      'CORPUS_PASSWORD is not set. It is `KEYCLOAK_LIVE_ADMIN_PASSWORD` in backend/.env — this ' +
        'harness signs in against the real Keycloak, because most surfaces do not exist until it has.',
    );
  }
  await page.waitFor(`document.querySelector('form input[type=email]')`, 'the login form');
  const ok = await page.evaluate(`
    const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    const email = document.querySelector('form input[type=email]');
    const pw = document.querySelector('form input[type=password]');
    set.call(email, ${JSON.stringify(EMAIL)}); email.dispatchEvent(new Event('input', { bubbles: true }));
    set.call(pw, ${JSON.stringify(PASSWORD)}); pw.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('form').requestSubmit();
    return true;`);
  if (!ok) throw new Error('could not fill the login form');
  await page.waitFor(`document.querySelector('main')`, 'the shell', 30000);
  await sleep(1800);
}

/** Capture a screenshot into the (gitignored) screenshots directory. */
export async function shoot(page, name) {
  mkdirSync(SHOTS, { recursive: true });
  const data = await page.screenshot();
  writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(data, 'base64'));
}

/**
 * Select a conversation that already carries an answer, so state-dependent surfaces are reached
 * deliberately rather than inherited from whatever the previous run left selected.
 *
 * This exists because the first version of the harness passed and then failed on an identical
 * build: run 1 ended by creating a fresh chat, so run 2 signed in onto it and the FR-ANL-04 card
 * showed its empty state where the run before had shown the four DeepEval labels. **An assertion
 * whose truth depends on the order of earlier checks is not a fidelity check.**
 */
export async function selectAnsweredChat(page) {
  const picked = await page.evaluate(`
    const rows = [...document.querySelectorAll('nav li button, nav li a')];
    // The meta line carries "· {n} messages"; anything above zero has a turn in it.
    const withTurns = rows.filter((b) => {
      const m = (b.textContent || '').match(/(\\d+)\\s+messages?/);
      return m !== null && Number(m[1]) > 0;
    });
    const target = withTurns[0];
    if (!target) return null;
    target.click();
    return (target.textContent || '').trim().slice(0, 40);`);
  await sleep(1600);
  return picked;
}

/** Click the first element under `selector` whose label or text contains `text`. */
export async function clickByText(page, selector, text) {
  const ok = await page.evaluate(`
    const el = [...document.querySelectorAll(${JSON.stringify(selector)})]
      .find((b) => ((b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')).includes(${JSON.stringify(text)}));
    if (!el) return false;
    el.focus(); el.click(); return true;`);
  await sleep(700);
  return ok;
}
