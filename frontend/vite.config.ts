// `vitest/config` rather than `vite` so the `test` block below is typed without a second
// config file duplicating the plugin and proxy setup. It re-exports Vite's own defineConfig.
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
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
    // The theme mechanism writes to `document.documentElement`, so the tests need a DOM.
    environment: 'jsdom',
    // Explicit `import { describe, it, expect } from 'vitest'` — keeps `tsconfig.app.json`
    // free of a `vitest/globals` types entry. The cost is that React Testing Library's
    // auto-cleanup does not engage, which is what `setupFiles` below is for.
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
  },
});
