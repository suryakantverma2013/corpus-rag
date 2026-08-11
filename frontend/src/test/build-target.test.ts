/**
 * NFR-CMP-01 (R-61) — the supported browser matrix must not drift.
 *
 * Before this was pinned, the floor was whatever `build.target: 'baseline-widely-available'`
 * happened to resolve to in the installed Vite. That made a **product commitment** a side
 * effect of a dependency's default: a Vite upgrade could drop a browser from support, or
 * start emitting syntax an in-matrix browser cannot parse, and nothing would fail.
 *
 * This asserts the config against the matrix the spec commits to. If a Vite upgrade wants a
 * different floor, that is a requirement change — amend NFR-CMP-01 and this list together.
 */
import { describe, expect, it } from 'vitest';
import config from '../../vite.config';

/** NFR-CMP-01, spec §5.6, ruling R-61. Keep in sync with the requirement, not with Vite. */
const SUPPORTED = ['chrome111', 'edge111', 'firefox114', 'safari16.4', 'ios16.4'];

describe('browser matrix (NFR-CMP-01, R-61)', () => {
  it('pins build.target to the spec-committed matrix', () => {
    expect(config.build?.target).toEqual(SUPPORTED);
  });

  it('does not inherit a bundler default', () => {
    // The literal string is what made the floor implicit. If it comes back, the matrix is
    // once again whatever the installed Vite decides.
    expect(config.build?.target).not.toBe('baseline-widely-available');
    expect(config.build?.target).not.toBe('modules');
  });

  it('leaves cssTarget to follow build.target, so the CSS and JS floors cannot diverge', () => {
    expect(config.build?.cssTarget).toBeUndefined();
  });
});
