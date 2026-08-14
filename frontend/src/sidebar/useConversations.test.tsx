/**
 * The FR-SBR-03 list store — ordering, the optimistic rename, and what each failure leaves.
 *
 * The transport is mocked; `chat/mutations.test.ts` covers what each status means. What is left
 * is the part neither that nor `conversations.ts` can see: that the list stays in
 * `list_by_owner` order through a rename and a turn without re-fetching to find out.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Conversation } from '../api';

const listConversations = vi.fn();
const createConversation = vi.fn();
const renameConversation = vi.fn();

vi.mock('../chat/mutations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../chat/mutations')>()),
  listConversations: (...a: unknown[]) => listConversations(...a),
  createConversation: (...a: unknown[]) => createConversation(...a),
  renameConversation: (...a: unknown[]) => renameConversation(...a),
}));

const { useConversations } = await import('./useConversations');

function row(id: string, title: string | null, updated: string): Conversation {
  return {
    id,
    title,
    archived: false,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: updated,
    message_count: 2,
  };
}

const OLDER = row('older', 'Older', '2026-07-01T00:00:00Z');
const NEWER = row('newer', 'Newer', '2026-08-01T00:00:00Z');

beforeEach(() => {
  listConversations.mockReset().mockResolvedValue({ kind: 'ok', data: [OLDER, NEWER] });
  createConversation.mockReset();
  renameConversation.mockReset();
});

async function mounted(enabled = true) {
  const view = renderHook(() => useConversations({ enabled }));
  if (enabled) await waitFor(() => expect(view.result.current.loaded).toBe(true));
  return view;
}

describe('loading', () => {
  it('sorts into FR-SBR-03 sidebar order, whatever order it arrived in', async () => {
    const { result } = await mounted();
    expect(result.current.rows.map((r) => r.id)).toEqual(['newer', 'older']);
  });

  it('fires nothing while the session is still resuming', async () => {
    await mounted(false);
    await Promise.resolve();
    expect(listConversations).not.toHaveBeenCalled();
  });

  it('leaves the list empty rather than half-loaded when the read fails', async () => {
    listConversations.mockResolvedValue({ kind: 'refused', detail: 'boom', status: 500 });
    const { result } = renderHook(() => useConversations({ enabled: true }));
    await Promise.resolve();
    expect(result.current.rows).toEqual([]);
    expect(result.current.loaded).toBe(false);
  });
});

describe('create (FR-SBR-02)', () => {
  it('prepends the server’s row and returns the detail, meter and all', async () => {
    const created = { ...row('fresh', null, '2026-09-01T00:00:00Z'), context: { x: 1 } };
    createConversation.mockResolvedValue({ kind: 'ok', data: created });
    const { result } = await mounted();

    let returned: unknown;
    await act(async () => {
      returned = await result.current.create();
    });

    expect(returned).toBe(created);
    expect(result.current.rows[0].id).toBe('fresh');
  });

  it('sends no title, so an untitled chat stays untitled', async () => {
    // FR-SBR-02's "New chat" is `displayTitle`'s fallback for a null title. Sending the label
    // would make a chat nobody named indistinguishable from one a user named that.
    createConversation.mockResolvedValue({ kind: 'ok', data: row('fresh', null, 'x') });
    const { result } = await mounted();
    await act(async () => void (await result.current.create()));
    expect(createConversation).toHaveBeenCalledWith();
  });

  it('returns null and changes nothing when the server refuses', async () => {
    createConversation.mockResolvedValue({ kind: 'refused', detail: 'no', status: 500 });
    const { result } = await mounted();
    await act(async () => {
      expect(await result.current.create()).toBeNull();
    });
    expect(result.current.rows).toHaveLength(2);
  });
});

describe('rename (FR-SBR-07)', () => {
  it('shows the new title immediately, then adopts the server row and re-sorts', async () => {
    const renamed = { ...OLDER, title: 'Renamed', updated_at: '2026-09-01T00:00:00Z' };
    renameConversation.mockResolvedValue({ kind: 'ok', data: renamed });
    const { result } = await mounted();

    act(() => result.current.rename('older', 'Renamed'));
    expect(result.current.rows.find((r) => r.id === 'older')?.title).toBe('Renamed');
    // Still in place until the server says its `updated_at` moved — a row that jumps twice
    // reads as a glitch.
    expect(result.current.rows.map((r) => r.id)).toEqual(['newer', 'older']);

    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(['older', 'newer']));
  });

  it('drops the row when the chat turns out to be gone', async () => {
    renameConversation.mockResolvedValue({ kind: 'gone' });
    const { result } = await mounted();
    act(() => result.current.rename('older', 'Renamed'));
    await waitFor(() => expect(result.current.rows.map((r) => r.id)).toEqual(['newer']));
  });

  it('re-reads rather than guessing the old title back', async () => {
    // The optimistic write left a title the server does not have, and nothing here knows what
    // it was — re-reading is the only honest way back.
    renameConversation.mockResolvedValue({ kind: 'refused', detail: 'no', status: 500 });
    const { result } = await mounted();
    act(() => result.current.rename('older', 'Renamed'));
    await waitFor(() => expect(listConversations).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(result.current.rows.find((r) => r.id === 'older')?.title).toBe('Older'),
    );
  });
});

describe('patch and forget', () => {
  it('adopts a turn’s new count and re-sorts, with no third GET', async () => {
    const { result } = await mounted();
    act(() =>
      result.current.patch('older', { updatedAt: '2026-09-01T00:00:00Z', messageCount: 7 }),
    );
    expect(result.current.rows.map((r) => r.id)).toEqual(['older', 'newer']);
    expect(result.current.rows[0].message_count).toBe(7);
    expect(listConversations).toHaveBeenCalledTimes(1);
  });

  it('ignores a patch for a row it does not have', async () => {
    const { result } = await mounted();
    act(() => result.current.patch('ghost', { updatedAt: 'x', messageCount: 1 }));
    expect(result.current.rows).toHaveLength(2);
  });

  it('forget drops the row', async () => {
    const { result } = await mounted();
    act(() => result.current.forget('older'));
    expect(result.current.rows.map((r) => r.id)).toEqual(['newer']);
  });
});
