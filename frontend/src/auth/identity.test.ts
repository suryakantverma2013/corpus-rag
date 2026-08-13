/**
 * `MeResponse` → the FR-SBR-06 row. `display_name` is nullable on the wire (FR-USR-03 makes it
 * optional at creation and T-110 records that Keycloak will hold a user with none), so every
 * case here is one the live realm can actually produce.
 */
import { describe, expect, it } from 'vitest';

import type { Me } from '../api';
import { displayName, initials } from './identity';

const me = (overrides: Partial<Me>): Me => ({
  id: '1',
  email: 'maya.jensen@example.com',
  display_name: 'Maya Jensen',
  roles: ['user'],
  is_active: true,
  ...overrides,
});

describe('displayName', () => {
  it('prefers the display name', () => {
    expect(displayName(me({}))).toBe('Maya Jensen');
  });

  it('falls back to the email’s local part, not the whole address', () => {
    // The row is 236px wide with a version tag on the right; a full address pushes the tag out.
    expect(displayName(me({ display_name: null }))).toBe('maya.jensen');
  });

  it('treats a blank display name as absent', () => {
    // Keycloak stores `firstName`/`lastName` separately (T-110), so a user with only a
    // surname can round-trip to a string of spaces rather than to null.
    expect(displayName(me({ display_name: '   ' }))).toBe('maya.jensen');
  });

  it('handles an address with no @ rather than returning empty', () => {
    expect(displayName(me({ display_name: null, email: 'operator' }))).toBe('operator');
  });
});

describe('initials', () => {
  it('takes the first letter of the first two words', () => {
    expect(initials(me({}))).toBe('MJ');
  });

  it('gives one letter for a one-word name, never two of the same word', () => {
    // "MA" for "Maya" reads as a different person's initials.
    expect(initials(me({ display_name: 'Maya' }))).toBe('M');
  });

  it('splits an email local part on the separators addresses actually use', () => {
    expect(initials(me({ display_name: null }))).toBe('MJ');
    expect(initials(me({ display_name: null, email: 'jane_doe@x.io' }))).toBe('JD');
    expect(initials(me({ display_name: null, email: 'jane-doe@x.io' }))).toBe('JD');
  });

  it('ignores a third word', () => {
    expect(initials(me({ display_name: 'Ada Byron Lovelace' }))).toBe('AB');
  });

  it('is byte-safe for a name outside the BMP', () => {
    // `name[0]` would split a surrogate pair and render a replacement character in the avatar.
    expect(initials(me({ display_name: '𝒜da Lovelace' }))).toBe('𝒜L');
  });

  it('survives extra whitespace', () => {
    expect(initials(me({ display_name: '  Maya   Jensen ' }))).toBe('MJ');
  });
});
