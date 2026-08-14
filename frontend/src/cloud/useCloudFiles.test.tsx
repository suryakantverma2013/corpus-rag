/**
 * The FR-KBM-10 list store: the debounce, the pages, and the states that stop both.
 *
 * `api` is never mocked — the mutations module is, which is `useDocuments.test.tsx`'s shape and
 * keeps these tests about the store's decisions rather than about openapi-fetch.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DriveFile } from '../api';
import type { ListResult } from './mutations';

const listCloudFiles = vi.fn(
  async (_search: string, _pageToken: string | null): Promise<ListResult> => ({
    kind: 'page',
    files: [],
    nextPageToken: null,
  }),
);

vi.mock('./mutations', () => ({
  listCloudFiles: (search: string, pageToken: string | null) => listCloudFiles(search, pageToken),
}));

const { useCloudFiles } = await import('./useCloudFiles');

const DEBOUNCE = 350;

function file(id: string): DriveFile {
  return {
    file_id: id,
    name: `${id}.pdf`,
    mime_type: 'application/pdf',
    size_bytes: 10,
    modified_time: null,
  };
}

beforeEach(() => {
  listCloudFiles.mockReset();
  listCloudFiles.mockResolvedValue({ kind: 'page', files: [], nextPageToken: null });
});

describe('the first page', () => {
  it('lists once the picker opens, with no search term', async () => {
    listCloudFiles.mockResolvedValue({ kind: 'page', files: [file('a')], nextPageToken: null });
    const { result } = renderHook(() => useCloudFiles({ active: true }));

    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(listCloudFiles).toHaveBeenCalledWith('', null);
    expect(result.current.files.map((f) => f.file_id)).toEqual(['a']);
  });

  it('does not call the provider while the picker is shut', () => {
    renderHook(() => useCloudFiles({ active: false }));
    // This read spends a third party's quota against a rate limit shared with import, so it
    // must not run for a surface nobody is looking at.
    expect(listCloudFiles).not.toHaveBeenCalled();
  });
});

describe('search — the rate limit is the reason this is debounced', () => {
  it('collapses a burst of keystrokes into ONE request', async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useCloudFiles({ active: true }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE + 10);
      });
      listCloudFiles.mockClear();

      for (const value of ['r', 're', 'rep', 'repo', 'report']) {
        act(() => result.current.setSearch(value));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
        });
      }
      // Still inside the window: five keystrokes, no request yet.
      expect(listCloudFiles).not.toHaveBeenCalled();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE);
      });
      expect(listCloudFiles).toHaveBeenCalledTimes(1);
      expect(listCloudFiles).toHaveBeenCalledWith('report', null);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the typed value immediately, whatever the request is doing', () => {
    const { result } = renderHook(() => useCloudFiles({ active: true }));
    act(() => result.current.setSearch('budget'));
    expect(result.current.search).toBe('budget');
  });

  it('sends an empty search as absent, never as an empty filter', async () => {
    const { result } = renderHook(() => useCloudFiles({ active: true }));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    // `search: ''` would build `name contains ''` at the provider — a filter that matches
    // everything by accident rather than by intent.
    expect(listCloudFiles).toHaveBeenCalledWith('', null);
  });
});

describe('pagination', () => {
  it('appends the next page and carries the provider’s opaque token', async () => {
    listCloudFiles.mockResolvedValueOnce({
      kind: 'page',
      files: [file('a')],
      nextPageToken: 'page-2',
    });
    const { result } = renderHook(() => useCloudFiles({ active: true }));
    await waitFor(() => expect(result.current.canLoadMore).toBe(true));

    listCloudFiles.mockResolvedValueOnce({
      kind: 'page',
      files: [file('b')],
      nextPageToken: null,
    });
    act(() => result.current.loadMore());

    await waitFor(() => expect(result.current.files).toHaveLength(2));
    expect(listCloudFiles).toHaveBeenLastCalledWith('', 'page-2');
    // Appended, not replaced — the flat list grows downward.
    expect(result.current.files.map((f) => f.file_id)).toEqual(['a', 'b']);
    expect(result.current.canLoadMore).toBe(false);
  });

  it('does not offer more when the provider sent no token', async () => {
    const { result } = renderHook(() => useCloudFiles({ active: true }));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.canLoadMore).toBe(false);
  });

  it('does not crawl: a delivered page requests nothing further on its own', async () => {
    listCloudFiles.mockResolvedValue({
      kind: 'page',
      files: [file('a')],
      nextPageToken: 'page-2',
    });
    const { result } = renderHook(() => useCloudFiles({ active: true }));
    await waitFor(() => expect(result.current.canLoadMore).toBe(true));

    // The guard is that the page token is a ref rather than an effect dependency. If it were a
    // dependency, each response would trigger the next request and walk the user's whole Drive
    // at 20 calls a minute.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(listCloudFiles).toHaveBeenCalledTimes(1);
  });

  it('resets to the first page when the search changes', async () => {
    vi.useFakeTimers();
    try {
      listCloudFiles.mockResolvedValue({
        kind: 'page',
        files: [file('a')],
        nextPageToken: 'page-2',
      });
      const { result } = renderHook(() => useCloudFiles({ active: true }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE + 10);
      });

      act(() => result.current.setSearch('report'));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(DEBOUNCE + 10);
      });
      // A new query replaces the list, so carrying the old cursor would page into results for a
      // search the user has moved off.
      expect(listCloudFiles).toHaveBeenLastCalledWith('report', null);
      expect(result.current.files.map((f) => f.file_id)).toEqual(['a']);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('FR-AUT-11’s refusal', () => {
  it('stops the list and carries the code and the server’s copy', async () => {
    listCloudFiles.mockResolvedValue({
      kind: 'link-required',
      code: 'CLOUD_ACCESS_REVOKED',
      detail: 'Google refused access to your Drive.',
    });
    const { result } = renderHook(() => useCloudFiles({ active: true }));

    await waitFor(() => expect(result.current.linkRequired).not.toBeNull());
    expect(result.current.linkRequired).toEqual({
      code: 'CLOUD_ACCESS_REVOKED',
      detail: 'Google refused access to your Drive.',
    });
    expect(result.current.files).toEqual([]);
    expect(result.current.canLoadMore).toBe(false);
  });

  it('clears once a later page succeeds', async () => {
    listCloudFiles.mockResolvedValueOnce({
      kind: 'link-required',
      code: 'ACCOUNT_NOT_LINKED',
      detail: 'not linked',
    });
    const { result, rerender } = renderHook(({ active }) => useCloudFiles({ active }), {
      initialProps: { active: true },
    });
    await waitFor(() => expect(result.current.linkRequired).not.toBeNull());

    listCloudFiles.mockResolvedValue({ kind: 'page', files: [file('a')], nextPageToken: null });
    rerender({ active: false });
    rerender({ active: true });

    await waitFor(() => expect(result.current.files).toHaveLength(1));
    expect(result.current.linkRequired).toBeNull();
  });
});

describe('refusals and closing', () => {
  it('renders the server’s copy for anything else — including the 429 this route can answer', async () => {
    listCloudFiles.mockResolvedValue({
      kind: 'refused',
      detail: 'Too many requests.',
      status: 429,
    });
    const { result } = renderHook(() => useCloudFiles({ active: true }));

    await waitFor(() => expect(result.current.notice).toBe('Too many requests.'));
    act(() => result.current.dismissNotice());
    expect(result.current.notice).toBeNull();
  });

  it('forgets the list when the picker closes', async () => {
    listCloudFiles.mockResolvedValue({ kind: 'page', files: [file('a')], nextPageToken: 'p2' });
    const { result, rerender } = renderHook(({ active }) => useCloudFiles({ active }), {
      initialProps: { active: true },
    });
    await waitFor(() => expect(result.current.files).toHaveLength(1));

    rerender({ active: false });
    // Third-party data behind a rate limit: a stale page rendered on reopen would be
    // indistinguishable from a fresh one.
    expect(result.current.files).toEqual([]);
    expect(result.current.search).toBe('');
    expect(result.current.canLoadMore).toBe(false);
    expect(result.current.loaded).toBe(false);
  });
});
