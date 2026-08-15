// `vitest/config` rather than `vite` so the `test` block below is typed without a second
// config file duplicating the plugin and proxy setup. It re-exports Vite's own defineConfig.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
/**
 * NFR-CMP-01's supported browser matrix (R-61), pinned rather than inherited.
 *
 * These are exactly the versions Vite 8's `baseline-widely-available` default resolved to, so
 * pinning changes no output today. The point is that it stops being a *default*: while the
 * matrix was whatever the bundler happened to choose, a Vite upgrade could move the floor —
 * dropping browsers, or silently widening what the build emits — with nothing failing. It is
 * a product commitment, so it belongs in this file where changing it is a visible decision.
 *
 * `build.cssTarget` follows `build.target` unless set, which is what keeps the CSS floor and
 * the JS floor from diverging. Every CSS feature in `tokens.css` sits at or below this floor;
 * the two engine-gated ones (`@supports selector()`, `scrollbar-width`/`-color`) degrade to
 * nothing rather than breaking, so they cost nothing below it either.
 *
 * Guarded by `src/test/build-target.test.ts` — an upgrade that moves the floor fails there.
 */
const BROWSER_TARGET = ['chrome111', 'edge111', 'firefox114', 'safari16.4', 'ios16.4'];

export default defineConfig({
  plugins: [react()],
  build: { target: BROWSER_TARGET },
  server: {
    // The API is same-origin in production (one reverse proxy in front of both halves), and
    // this reproduces that in development. Deliberately a proxy rather than CORS on the
    // backend: CORS would be dev-only configuration living permanently in production code,
    // and would need `Authorization` in `allow_headers` plus a preflight on every mutating
    // call. See `backend/app/main.py` — there is no CORS middleware, by design.
    //
    // SSE survives this: Vite pipes the response stream and does not buffer it.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    // Unit tests only, and all of them live under `src/` (T-603). Without this, vitest's
    // default glob also matches `e2e/*.spec.ts` — the Playwright journey — and `npm test`
    // tries to run it under jsdom, where `@playwright/test` throws on import. The two runners
    // now have disjoint territory: vitest owns `src/`, Playwright owns `e2e/`.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    // The theme mechanism writes to `document.documentElement`, so the tests need a DOM.
    environment: 'jsdom',
    // Explicit `import { describe, it, expect } from 'vitest'` — keeps `tsconfig.app.json`
    // free of a `vitest/globals` types entry. The cost is that React Testing Library's
    // auto-cleanup does not engage, which is what `setupFiles` below is for.
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
  },
});
