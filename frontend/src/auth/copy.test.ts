/**
 * FR-AUT-04's mapping, and the one rule that makes it a security control rather than a
 * formatting choice: the two credential-related messages are OURS, not the server's.
 */
import { describe, expect, it } from 'vitest';

import { readSource, stripTsComments } from '../test/css-source';
import {
  INVALID_CREDENTIALS,
  SYSTEM_FAILURE,
  TOO_MANY_ATTEMPTS,
  changePasswordErrorCopy,
  loginErrorCopy,
} from './copy';

describe('loginErrorCopy (FR-AUT-04)', () => {
  it('says the same thing for a bad password and an unknown account', () => {
    // The requirement in one assertion: "never disclose which field failed or whether the
    // account exists". The server already sends this exact sentence — the point is that the
    // screen does not *depend* on it doing so.
    expect(loginErrorCopy(401, { detail: 'User admin@corpus.local does not exist' })).toBe(
      INVALID_CREDENTIALS,
    );
    expect(loginErrorCopy(401, { detail: 'Invalid password for admin@corpus.local' })).toBe(
      INVALID_CREDENTIALS,
    );
  });

  it('maps a throttle to the rate-limit copy, whatever the body says', () => {
    expect(loginErrorCopy(429, { detail: 'Retry in 60s' })).toBe(TOO_MANY_ATTEMPTS);
    // Both the slowapi limit and Keycloak's brute-force backstop answer 429 with this text
    // already; the constant is what keeps them indistinguishable to the user, which is the
    // point — one reveals a per-IP throttle, the other that the account exists and is locked.
    expect(loginErrorCopy(429, null)).toBe(TOO_MANY_ATTEMPTS);
  });

  it('renders the server’s own copy for every other failure (FR-ERR-04)', () => {
    // A 503 saying "Authentication service unavailable." is a genuinely more useful sentence
    // than the generic fallback, and it tells the user something true about what to do next.
    expect(loginErrorCopy(503, { detail: 'Authentication service unavailable.' })).toBe(
      'Authentication service unavailable.',
    );
  });

  it('falls back to FR-ERR-04’s last resort when there is no readable detail', () => {
    expect(loginErrorCopy(500, null)).toBe(SYSTEM_FAILURE);
    expect(loginErrorCopy(0, undefined)).toBe(SYSTEM_FAILURE);
    expect(loginErrorCopy(502, '<html>502 Bad Gateway</html>')).toBe(SYSTEM_FAILURE);
    expect(loginErrorCopy(422, { detail: [{ msg: 'field required' }] })).toBe(SYSTEM_FAILURE);
    expect(loginErrorCopy(500, { detail: '' })).toBe(SYSTEM_FAILURE);
  });
});

describe('changePasswordErrorCopy (FR-AUT-09)', () => {
  it('renders the server’s 401 rather than the login screen’s', () => {
    // A 401 here is "the current password you typed is wrong" — an inline error, not a
    // credential-disclosure risk and not a session event. Reusing `loginErrorCopy` would
    // answer "Invalid email or password." inside a modal that asks for neither.
    expect(changePasswordErrorCopy(401, { detail: 'Current password is incorrect.' })).toBe(
      'Current password is incorrect.',
    );
  });

  it('shares the throttle copy', () => {
    expect(changePasswordErrorCopy(429, null)).toBe(TOO_MANY_ATTEMPTS);
  });

  it('renders a policy rejection verbatim (NFR-SEC-04 lives in the realm)', () => {
    // Password policy is Keycloak realm configuration (R-28), so the realm is the only thing
    // that knows the rules and its wording is the only accurate wording available.
    expect(changePasswordErrorCopy(400, { detail: 'Invalid password: minimum length 12.' })).toBe(
      'Invalid password: minimum length 12.',
    );
  });
});

describe('the copy lives here and nowhere else (R-69(1))', () => {
  it('no §4.17 component hard-codes a literal from this module', () => {
    // Two copies is how a string at a requirement and a string on a screen stop agreeing —
    // exactly what R-69(1) moved four T-505 strings into their requirements to prevent.
    const literals = [INVALID_CREDENTIALS, TOO_MANY_ATTEMPTS, SYSTEM_FAILURE];
    for (const file of [
      'src/auth/LoginScreen.tsx',
      'src/auth/ChangePasswordModal.tsx',
      'src/auth/UserMenu.tsx',
      'src/auth/AuthProvider.tsx',
      'src/auth/session.ts',
    ]) {
      const code = stripTsComments(readSource(file));
      for (const literal of literals) {
        expect(code, `${file} restates a copy.ts literal`).not.toContain(literal);
      }
    }
  });
});
