import { defineConfig, devices } from '@playwright/test';

/**
 * T-603 — end-to-end happy paths against the real stack.
 *
 * **Not part of `npm test`.** `vitest` runs against jsdom with everything mocked; this drives a
 * real browser against a real backend, a real arq worker, real Postgres, real MinIO and a real
 * Keycloak. It is invoked deliberately with `npm run e2e`, which is why a missing stack is a
 * hard failure here rather than a skip — an E2E suite that has only ever skipped is not a
 * passing suite (Rev 0.12's rule, learned the hard way when six live auth tests silently
 * stopped running and reported as "skipped").
 *
 * **Why Playwright, given R-66(5) declined it.** That declination is *fidelity*-scoped: it ruled
 * computed-style assertions a stronger instrument than screenshot diffing, because a screenshot
 * "blurs precisely the 264-vs-266px error an assertion states outright". None of that reasoning
 * touches functional flows. This suite asserts *behaviour over time* — an upload reaching
 * `Ready`, an SSE answer arriving, evaluation chips landing seconds later — where the hard part
 * is waiting correctly, and that is exactly what Playwright's auto-waiting is for. The
 * zero-dependency CDP driver in `fidelity/` keeps its job; this takes the other one. See R-78.
 *
 * **Headless is deliberate here, and it is the opposite of `fidelity/`'s choice.** R-60(4) makes
 * fidelity runs headed because headless Chromium renders no scrollbars and never matches
 * `:focus` — both are *rendering* facts, and neither is asserted here. This suite touches no
 * computed style, so headless is faster and reproducible. Set `E2E_HEADED=1` to watch it.
 */

const HEADED = process.env.E2E_HEADED === '1';

export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e/.artifacts',

  /**
   * Serial, one worker. Every test signs in as the same seeded administrator and mutates shared
   * server state — documents in one knowledge base, conversations in one sidebar. Parallel
   * workers would interleave those and produce failures that look like product defects.
   */
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,

  /**
   * A whole journey: sign in, ingest a document through parse → chunk → embed → index, ask a
   * grounded question of a real `gpt-4o`, then wait for the FR-EVL-02 chips that R-50 measured
   * at ~5.9 s and `useChat` re-reads for at 4/8/16 s. Three minutes is slack, not an estimate.
   */
  timeout: 180_000,
  expect: { timeout: 15_000 },

  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],

  use: {
    /**
     * Always the Vite origin, never the backend's. The backend ships **no CORS middleware by
     * design**, and `vite.config.ts` proxies `/api` and `/health` to it — so the dev server is
     * what reproduces production's single origin. Driving `:8000` directly would fail in a way
     * that looks like an auth bug.
     */
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    headless: !HEADED,
    reducedMotion: 'reduce',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  globalSetup: './e2e/global-setup.ts',
});
