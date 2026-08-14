/**
 * FR-SYS-03's configured model id, read once per session.
 *
 * Small, but the `enabled` gate is not optional here for the same reason it is not optional on
 * `useDocuments`: a hook cannot be called conditionally, so without it this fires during the
 * pre-auth phase and its 401 reaches FR-AUT-07's handler (§8.59).
 */
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const GET = vi.fn();
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  api: { GET: (...a: unknown[]) => GET(...a) },
}));

const { useConfig, fetchConfig } = await import('./useConfig');

beforeEach(() => {
  GET.mockReset().mockResolvedValue({ data: { chat_model: 'gpt-4o' }, error: undefined });
});

describe('useConfig', () => {
  it('reads the configured model', async () => {
    const { result } = renderHook(() => useConfig({ enabled: true }));
    await waitFor(() => expect(result.current?.chat_model).toBe('gpt-4o'));
    expect(GET).toHaveBeenCalledWith('/api/v1/config');
  });

  it('fires nothing while the session is still resuming', async () => {
    renderHook(() => useConfig({ enabled: false }));
    await Promise.resolve();
    expect(GET).not.toHaveBeenCalled();
  });

  it('stays null when the read fails rather than inventing an id', async () => {
    // The card falls back to the answering message's own `model_name`, and an em dash beats a
    // hard-coded literal that drifts from `OPENAI_CHAT_MODEL`.
    GET.mockResolvedValue({ data: undefined, error: { detail: 'boom' } });
    const { result } = renderHook(() => useConfig({ enabled: true }));
    await Promise.resolve();
    expect(result.current).toBeNull();
  });

  it('survives a transport throw', async () => {
    GET.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(fetchConfig()).resolves.toBeNull();
  });
});
