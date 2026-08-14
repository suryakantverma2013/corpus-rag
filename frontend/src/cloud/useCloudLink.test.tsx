/**
 * FR-AUT-11's linked state — the fact FR-KBM-06's button branches on.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LinkStatus } from '../api';

const getLinkStatus = vi.fn(async (): Promise<LinkStatus> => ({
  provider: 'google',
  linked: false,
  account: null,
}));
const startLink = vi.fn(async (): Promise<{ url: string } | { detail: string }> => ({
  url: 'https://keycloak.test/authorize',
}));
const unlinkAccount = vi.fn(async () => true);

vi.mock('./mutations', () => ({
  getLinkStatus: () => getLinkStatus(),
  startLink: () => startLink(),
  unlinkAccount: () => unlinkAccount(),
}));

const { useCloudLink } = await import('./useCloudLink');

const assign = vi.fn();

beforeEach(() => {
  getLinkStatus.mockReset();
  getLinkStatus.mockResolvedValue({ provider: 'google', linked: false, account: null });
  startLink.mockReset();
  startLink.mockResolvedValue({ url: 'https://keycloak.test/authorize' });
  unlinkAccount.mockReset();
  unlinkAccount.mockResolvedValue(true);
  assign.mockReset();
  vi.stubGlobal('location', { ...window.location, assign });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('reading the status', () => {
  it('reports the linked account address', async () => {
    getLinkStatus.mockResolvedValue({
      provider: 'google',
      linked: true,
      account: 'person@example.com',
    });
    const { result } = renderHook(() => useCloudLink({ enabled: true }));

    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.linked).toBe(true);
    expect(result.current.account).toBe('person@example.com');
  });

  it('does not call the server without a session', () => {
    renderHook(() => useCloudLink({ enabled: false }));
    // §8.59: an unauthenticated call here answers 401, and FR-AUT-07's handler reads that as
    // "the session ended" — signing out the user whose session was still resuming.
    expect(getLinkStatus).not.toHaveBeenCalled();
  });

  it('still settles when the status cannot be read, defaulting to not-linked', async () => {
    getLinkStatus.mockRejectedValue(new Error('503'));
    const { result } = renderHook(() => useCloudLink({ enabled: true }));

    // `loaded` must flip or FR-KBM-06's button is inert forever after one transient failure;
    // `linked` stays false because offering to link is idempotent while opening a picker that
    // cannot list anything is a dead end.
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.linked).toBe(false);
  });

  it('forgets the previous user’s link when the session ends', async () => {
    getLinkStatus.mockResolvedValue({ provider: 'google', linked: true, account: 'a@b.c' });
    const { result, rerender } = renderHook(({ enabled }) => useCloudLink({ enabled }), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(result.current.linked).toBe(true));

    rerender({ enabled: false });
    expect(result.current.linked).toBe(false);
    expect(result.current.account).toBeNull();
    // Back to unloaded, so the button waits rather than guessing on behalf of whoever signs in.
    expect(result.current.loaded).toBe(false);
  });

  it('re-reads on refresh — twice in a row', async () => {
    const { result } = renderHook(() => useCloudLink({ enabled: true }));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(getLinkStatus).toHaveBeenCalledTimes(1);

    act(() => result.current.refresh());
    await waitFor(() => expect(getLinkStatus).toHaveBeenCalledTimes(2));
    act(() => result.current.refresh());
    await waitFor(() => expect(getLinkStatus).toHaveBeenCalledTimes(3));
  });
});

describe('beginLink — leg 1', () => {
  it('navigates the whole page to the authorize URL', async () => {
    const { result } = renderHook(() => useCloudLink({ enabled: true }));
    await waitFor(() => expect(result.current.loaded).toBe(true));

    await act(async () => {
      await result.current.beginLink();
    });
    // A redirect chain through Keycloak's login page and Google's consent screen — neither can
    // be satisfied by an XHR, which is why the route answers JSON rather than a 302.
    expect(assign).toHaveBeenCalledWith('https://keycloak.test/authorize');
  });

  it('renders the refusal instead of navigating to nowhere', async () => {
    startLink.mockResolvedValue({ detail: 'Too many requests.' });
    const { result } = renderHook(() => useCloudLink({ enabled: true }));
    await waitFor(() => expect(result.current.loaded).toBe(true));

    await act(async () => {
      await result.current.beginLink();
    });
    expect(assign).not.toHaveBeenCalled();
    expect(result.current.notice).toBe('Too many requests.');
  });
});

describe('unlink', () => {
  it('drops the link locally on success', async () => {
    getLinkStatus.mockResolvedValue({ provider: 'google', linked: true, account: 'a@b.c' });
    const { result } = renderHook(() => useCloudLink({ enabled: true }));
    await waitFor(() => expect(result.current.linked).toBe(true));

    let ok = false;
    await act(async () => {
      ok = await result.current.unlink();
    });
    expect(ok).toBe(true);
    expect(result.current.linked).toBe(false);
    expect(result.current.account).toBeNull();
  });

  it('keeps the link and explains when the server refused', async () => {
    getLinkStatus.mockResolvedValue({ provider: 'google', linked: true, account: 'a@b.c' });
    unlinkAccount.mockResolvedValue(false);
    const { result } = renderHook(() => useCloudLink({ enabled: true }));
    await waitFor(() => expect(result.current.linked).toBe(true));

    let ok = true;
    await act(async () => {
      ok = await result.current.unlink();
    });
    expect(ok).toBe(false);
    expect(result.current.linked).toBe(true);
    expect(result.current.notice).not.toBeNull();

    act(() => result.current.dismissNotice());
    expect(result.current.notice).toBeNull();
  });
});
