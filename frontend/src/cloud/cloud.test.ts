/**
 * FR-KBM-10 / FR-AUT-11's pure half. No DOM, no transport — the `mentions.test.ts` shape.
 */
import { describe, expect, it } from 'vitest';

import type { DriveFile } from '../api';
import { driveMeta, fileSize, modifiedOn, nextCloudIndex, readLinkReturn } from './cloud';

function file(overrides: Partial<DriveFile> = {}): DriveFile {
  return {
    file_id: '1KDDdcxaZDbo2M12Q4R2Igx0L2g0kv5G9',
    name: 'Q3 report.pdf',
    mime_type: 'application/pdf',
    size_bytes: 2_200_000,
    modified_time: '2026-07-10T09:00:00Z',
    ...overrides,
  };
}

describe('readLinkReturn — the closed §9 vocabulary', () => {
  it.each([
    ['?link=linked&provider=google', 'linked'],
    ['?link=failed&provider=google', 'failed'],
    ['?link=denied&provider=google', 'denied'],
    ['?provider=google&link=linked', 'linked'],
  ])('reads %s as %s', (search, expected) => {
    expect(readLinkReturn(search)).toBe(expected);
  });

  it.each([
    ['an ordinary load', ''],
    ['an unrelated query', '?chat=c1'],
    ['an empty value', '?link='],
    // The vocabulary is three words wide. Anything else is a URL someone typed or a server one
    // revision ahead, and inventing a fourth state from it is how a stray link opens a surface.
    ['a value outside the vocabulary', '?link=maybe'],
    ['a near miss', '?link=Linked'],
  ])('yields null for %s', (_label, search) => {
    expect(readLinkReturn(search)).toBeNull();
  });
});

describe('fileSize — binary units, to agree with FR-ERR-01', () => {
  it.each([
    [0, '0 bytes'],
    [512, '512 bytes'],
    [1024, '1.0 KB'],
    [1536, '1.5 KB'],
    [1024 * 1024, '1.0 MB'],
    [2_306_867, '2.2 MB'],
    [3 * 1024 * 1024 * 1024, '3.0 GB'],
  ])('renders %d as %s', (bytes, expected) => {
    expect(fileSize(bytes)).toBe(expected);
  });

  it('renders the FR-ERR-01 ceiling as exactly 50 MB', () => {
    // The drop zone one surface up says "max 50 MB". A decimal formatter would call the largest
    // acceptable file 52.4 MB, so the caption and the picker would disagree about the limit.
    expect(fileSize(50 * 1024 * 1024)).toBe('50.0 MB');
  });

  it.each([
    ['an absent size', null],
    ['a negative size', -1],
    ['a non-finite size', Number.NaN],
  ])('yields null for %s rather than claiming zero', (_label, bytes) => {
    expect(fileSize(bytes)).toBeNull();
  });
});

describe('modifiedOn', () => {
  it('pins the locale, so both lists in the modal agree on one machine', () => {
    expect(modifiedOn('2026-07-08T09:00:00Z')).toBe('Jul 08');
  });

  it.each([
    ['an absent time', null],
    ['an unparseable time', 'not-a-date'],
  ])('yields null for %s rather than letting Invalid Date reach a row', (_label, iso) => {
    expect(modifiedOn(iso)).toBeNull();
  });
});

describe('driveMeta — the FR-KBM-04 meta-line shape', () => {
  it('joins size and modified with the separator the document row uses', () => {
    expect(driveMeta(file())).toBe('2.1 MB · modified Jul 10');
  });

  it('drops the size clause when the provider omitted it', () => {
    expect(driveMeta(file({ size_bytes: null }))).toBe('modified Jul 10');
  });

  it('drops the modified clause when the provider omitted it', () => {
    expect(driveMeta(file({ modified_time: null }))).toBe('2.1 MB');
  });

  it('is empty when neither exists, so the component renders no line at all', () => {
    expect(driveMeta(file({ size_bytes: null, modified_time: null }))).toBe('');
  });
});

describe('nextCloudIndex — NFR-A11Y-03 listbox movement', () => {
  it('enters the list from nothing active, at either end', () => {
    expect(nextCloudIndex(-1, 1, 3)).toBe(0);
    expect(nextCloudIndex(-1, -1, 3)).toBe(2);
  });

  it('wraps at both ends', () => {
    expect(nextCloudIndex(2, 1, 3)).toBe(0);
    expect(nextCloudIndex(0, -1, 3)).toBe(2);
  });

  it('stays at nothing-active for an empty list', () => {
    expect(nextCloudIndex(-1, 1, 0)).toBe(-1);
  });
});
