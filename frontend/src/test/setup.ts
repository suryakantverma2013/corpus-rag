/**
 * Vitest setup. `globals: false` in `vite.config.ts` means React Testing Library cannot
 * register its own `afterEach(cleanup)` hook, so it is registered here — without it, every
 * `render()` leaks its container into the next test and the DOM accumulates across a file.
 */
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(cleanup);
