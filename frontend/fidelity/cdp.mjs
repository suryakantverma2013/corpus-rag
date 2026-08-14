/**
 * A minimal Chrome DevTools Protocol driver — zero dependencies, Node's global `WebSocket`.
 *
 * Committed rather than left in a scratchpad because T-510's whole point is that the fidelity
 * check must be **repeatable**: every component task from T-502 to T-514 measured its own surface
 * in a headed browser and threw the script away, so nothing in the tree could tell you that a
 * later edit had moved a pixel.
 *
 * Playwright would give screenshot diffing, and was declined: R-66(5) rules the computed-property
 * assertions the stronger instrument (a screenshot diff is sensitive to font-load timing and
 * subpixel antialiasing, and blurs exactly the 264-vs-266px error an assertion states outright),
 * and this harness needs none of what the dependency would buy.
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/** Where Chromium might be. `CHROME` wins; otherwise the first path that exists. */
function findChrome() {
  if (process.env.CHROME !== undefined) return process.env.CHROME;
  const candidates = [
    join(
      process.env.LOCALAPPDATA ?? '',
      'ms-playwright',
      'chromium-1228',
      'chrome-win64',
      'chrome.exe',
    ),
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    '/usr/bin/chromium',
    '/usr/bin/google-chrome',
  ];
  const found = candidates.find((p) => p !== '' && existsSync(p));
  if (found === undefined) {
    throw new Error(
      'No Chromium found. Set CHROME=<path to chrome.exe>. Checked:\n  ' + candidates.join('\n  '),
    );
  }
  return found;
}

/**
 * Launch a browser and attach to its first page.
 *
 * **Headed by default, and that is a measured constraint rather than a preference** (R-60(4)):
 * headless renders no scrollbars at all — every width reads 0px, including an element forced to
 * `scrollbar-width: auto` — and never matches `:focus`, because the page has no window focus.
 * A 0px scrollbar reading is inconclusive, never a pass.
 */
export async function launch({ headless = false, port = 9500, width = 1440, height = 900 } = {}) {
  const profile = mkdtempSync(join(tmpdir(), 'corpus-fidelity-'));
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    // Device scale 1, or every measured pixel is multiplied by the display's scaling and the
    // §9 literals stop being comparable (the DPI trap T-504 recorded for pointer input).
    '--force-device-scale-factor=1',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-features=Translate,MediaRouter',
    `--window-size=${width},${height}`,
    'about:blank',
  ];
  if (headless) args.unshift('--headless=new');
  const proc = spawn(findChrome(), args, { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 100 && target === null; i++) {
    await new Promise((r) => setTimeout(r, 150));
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      target = list.find((t) => t.type === 'page') ?? null;
    } catch {
      /* not up yet */
    }
  }
  if (target === null) throw new Error('Chrome did not expose a page target');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });

  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    const entry = pending.get(msg.id);
    if (entry === undefined) return;
    pending.delete(msg.id);
    if (msg.error) entry.reject(new Error(JSON.stringify(msg.error)));
    else entry.resolve(msg.result);
  });

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const messageId = ++id;
      pending.set(messageId, { resolve, reject });
      ws.send(JSON.stringify({ id: messageId, method, params }));
    });

  await send('Page.enable');
  await send('Runtime.enable');

  /** Evaluate an expression in the page. The body is wrapped in an async IIFE, so use `return`. */
  const evaluate = async (expression) => {
    const { result, exceptionDetails } = await send('Runtime.evaluate', {
      expression: `(async () => { ${expression} })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    if (exceptionDetails)
      throw new Error(exceptionDetails.exception?.description ?? JSON.stringify(exceptionDetails));
    return result.value;
  };

  const navigate = async (url) => {
    await send('Page.navigate', { url });
    // Wait for the SPA to mount, never for `load`: a probe against an empty document "passes"
    // against `null` everywhere, which is T-507's recorded lesson about the instrument.
    for (let i = 0; i < 150; i++) {
      await new Promise((r) => setTimeout(r, 100));
      if (await evaluate(`return document.querySelector('#root')?.childElementCount > 0`)) return;
    }
    throw new Error(`the app never mounted at ${url} — is \`npm run dev\` running?`);
  };

  const waitFor = async (expression, label = expression, timeoutMs = 15000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      if (await evaluate(`return !!(${expression})`)) return true;
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
      await new Promise((r) => setTimeout(r, 100));
    }
  };

  const screenshot = async () => (await send('Page.captureScreenshot', { format: 'png' })).data;

  const close = async () => {
    try {
      await send('Browser.close');
    } catch {
      /* already gone */
    }
    ws.close();
    proc.kill();
  };

  return { send, evaluate, navigate, waitFor, screenshot, close };
}
