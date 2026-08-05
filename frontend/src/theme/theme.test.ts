import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  isTheme,
  nextTheme,
  readStoredTheme,
  writeStoredTheme,
} from './theme';

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe('nextTheme', () => {
  it('flips dark to light and back (FR-THM-02)', () => {
    expect(nextTheme('dark')).toBe('light');
    expect(nextTheme('light')).toBe('dark');
  });
});

describe('isTheme', () => {
  it('accepts exactly the two theme names', () => {
    expect(isTheme('dark')).toBe(true);
    expect(isTheme('light')).toBe(true);
  });

  it.each([['Dark'], [''], ['system'], ['auto'], [null], [undefined], [{}], [0]])(
    'rejects %o',
    (value) => {
      expect(isTheme(value)).toBe(false);
    },
  );
});

describe('readStoredTheme', () => {
  it('returns null when nothing is stored, so the caller applies the dark default', () => {
    expect(readStoredTheme()).toBeNull();
    expect(DEFAULT_THEME).toBe('dark'); // NFR-USE-01
  });

  it('returns the stored preference', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    expect(readStoredTheme()).toBe('light');
  });

  it('returns null for a value it does not recognise', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'system');
    expect(readStoredTheme()).toBeNull();
  });

  it('returns null rather than throwing when storage is blocked', () => {
    // localStorage throws SecurityError in some privacy modes. This runs inside useState's
    // initializer, so an uncaught throw would take down the app over a cosmetic preference.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError');
    });
    expect(readStoredTheme()).toBeNull();
  });
});

describe('writeStoredTheme', () => {
  it('persists the preference', () => {
    writeStoredTheme('light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
  });

  it('swallows a storage failure', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError');
    });
    expect(() => writeStoredTheme('light')).not.toThrow();
  });
});

describe('THEME_STORAGE_KEY', () => {
  it('matches the key hard-coded in index.html', () => {
    // The pre-paint inline script in index.html must read the same key this module writes,
    // and it cannot import this constant. Changing the value here without changing
    // index.html silently reintroduces the flash-of-wrong-theme (R-58(1)).
    expect(THEME_STORAGE_KEY).toBe('corpus.theme');
  });
});
