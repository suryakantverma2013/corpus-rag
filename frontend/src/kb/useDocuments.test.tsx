/**
 * The store's stateful half: the pending-upload hand-off and R-71(1)'s reconciliation.
 *
 * The transport is mocked here — `mutations.test.ts` covers what each status means and
 * `useDocumentStream.test.tsx` covers the wire. What is left is the part neither can see: what
 * the hook *remembers* between them.
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Document, DocumentEvent } from '../api';
import type { DocumentAction } from './documents';

const uploadDocument = vi.fn();
const importDocument = vi.fn();
const deleteDocument = vi.fn();
const retryDocument = vi.fn();
const replaceDocument = vi.fn();
const listDocuments = vi.fn(async () => [] as Document[]);

vi.mock('./mutations', () => ({
  uploadDocument: (...a: unknown[]) => uploadDocument(...a),
  importDocument: (...a: unknown[]) => importDocument(...a),
  deleteDocument: (...a: unknown[]) => deleteDocument(...a),
  retryDocument: (...a: unknown[]) => retryDocument(...a),
  replaceDocument: (...a: unknown[]) => replaceDocument(...a),
  listDocuments: () => listDocuments(),
  PROCESSING_LOCKED: 'PROCESSING_LOCKED',
}));

/** Hand the test the hook's own `dispatch`, so it can play the stream. */
let dispatch: ((action: DocumentAction) => void) | null = null;
let streamEnabled: boolean | null = null;
vi.mock('./useDocumentStream', () => ({
  useDocumentStream: (enabled: boolean, d: (action: DocumentAction) => void) => {
    dispatch = d;
    streamEnabled = enabled;
    return 'open';
  },
}));

const { useDocuments } = await import('./useDocuments');

function doc(overrides: Partial<DocumentEvent> = {}): DocumentEvent {
  return {
    document_id: 'd1',
    filename: 'live.md',
    mime_type: 'text/markdown',
    status: 'ACTIVE',
    current_version: 1,
    searchable: true,
    size_bytes: 10,
    page_count: null,
    chunk_count: 1,
    error_message: null,
    knowledge_base_id: 'kb1',
    scope: 'global',
    conversation_id: null,
    latest_job_id: 'j1',
    latest_job_error_code: null,
    latest_job_document_version: 1,
    created_at: '2026-08-12T09:00:00Z',
    updated_at: '2026-08-12T09:00:00Z',
    deleted_at: null,
    stalled: false,
    ...overrides,
  };
}

const file = () => new File(['x'], 'live.md', { type: 'text/markdown' });

beforeEach(() => {
  dispatch = null;
  uploadDocument.mockReset();
  deleteDocument.mockReset();
  listDocuments.mockReset();
  listDocuments.mockResolvedValue([]);
});

afterEach(() => vi.clearAllMocks());

const mount = (turnInFlight = false) =>
  renderHook(
    ({ busy }) =>
      useDocuments({ open: true, conversationId: 'c1', turnInFlight: busy, enabled: true }),
    {
      initialProps: { busy: turnInFlight },
    },
  );

describe('the pending-upload hand-off', () => {
  it('shows an entry while the upload is in flight, and drops it when the row arrives', async () => {
    uploadDocument.mockResolvedValue({ kind: 'accepted', documentId: 'd1', status: 'QUEUED' });
    const { result } = mount();

    act(() => result.current.upload([file()], 'global'));
    expect(result.current.pending.map((p) => p.filename)).toEqual(['live.md']);

    await waitFor(() => expect(result.current.pending[0]?.documentId).toBe('d1'));
    act(() => dispatch?.({ type: 'snapshot', documents: [doc()] }));

    await waitFor(() => expect(result.current.pending).toHaveLength(0));
  });

  it('does not RESURRECT the entry when that document is later deleted', async () => {
    // The live pass found this: filtering on "the row is present" makes the hand-off reversible,
    // so removing the document flips the condition back and an `Uploading…` row reappears for a
    // file that no longer exists. Dropping it from state is what makes the hand-off one-way.
    uploadDocument.mockResolvedValue({ kind: 'accepted', documentId: 'd1', status: 'QUEUED' });
    const { result } = mount();

    act(() => result.current.upload([file()], 'global'));
    await waitFor(() => expect(result.current.pending[0]?.documentId).toBe('d1'));
    act(() => dispatch?.({ type: 'snapshot', documents: [doc()] }));
    await waitFor(() => expect(result.current.pending).toHaveLength(0));

    act(() => dispatch?.({ type: 'removed', documentId: 'd1' }));

    expect(result.current.rows).toHaveLength(0);
    expect(result.current.pending).toHaveLength(0);
  });

  it('drops the entry outright when the upload is refused', async () => {
    uploadDocument.mockResolvedValue({ kind: 'refused', detail: 'Too large.', status: 413 });
    const { result } = mount();

    act(() => result.current.upload([file()], 'global'));
    await waitFor(() => expect(result.current.pending).toHaveLength(0));
    expect(result.current.notice).toBe('Too large.');
  });

  it('reports a duplicate without an error', async () => {
    uploadDocument.mockResolvedValue({ kind: 'duplicate', documentId: 'd1', status: 'ACTIVE' });
    const { result } = mount();

    act(() => result.current.upload([file()], 'global'));
    await waitFor(() => expect(result.current.notice).toContain('already in your knowledge base'));
  });
});

describe('R-71(1) — reconciling the server’s 409', () => {
  it('is paused by the client signal alone', () => {
    const { result } = mount(true);
    expect(result.current.paused).toBe(true);
  });

  it('pauses on a lock 409 the client had not predicted, and LAPSES rather than deadlocking', async () => {
    // The second-tab case OI-31 names: this client never saw the turn start, so its own signal
    // says free while the server refuses. That is also why the flag cannot wait for
    // `turnInFlight` to fall — it is already false and has no transition left to make, while the
    // flag itself disables the actions whose success would clear it. Written after the first
    // implementation deadlocked exactly so.
    vi.useFakeTimers();
    try {
      deleteDocument.mockResolvedValue({ kind: 'locked', detail: 'paused' });
      const { result } = mount(false);
      act(() => dispatch?.({ type: 'snapshot', documents: [doc()] }));

      await act(async () => {
        result.current.act(doc(), 'delete');
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.paused).toBe(true);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(result.current.paused).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears early when the client’s own signal reports the turn has ended', async () => {
    deleteDocument.mockResolvedValue({ kind: 'locked', detail: 'paused' });
    const { result, rerender } = renderHook(
      ({ busy }) =>
        useDocuments({ open: true, conversationId: 'c1', turnInFlight: busy, enabled: true }),
      { initialProps: { busy: true } },
    );
    act(() => dispatch?.({ type: 'snapshot', documents: [doc()] }));
    await waitFor(() => expect(result.current.paused).toBe(true));

    rerender({ busy: false });
    expect(result.current.paused).toBe(false);
  });

  it('does not send an action that is already blocked', () => {
    const { result } = mount(true);
    act(() => result.current.act(doc(), 'delete'));
    expect(deleteDocument).not.toHaveBeenCalled();
  });

  it('drops a row the server says is already gone, rather than showing an error', async () => {
    deleteDocument.mockResolvedValue({ kind: 'gone' });
    const { result } = mount(false);
    act(() => dispatch?.({ type: 'snapshot', documents: [doc()] }));
    expect(result.current.rows).toHaveLength(1);

    act(() => result.current.act(doc(), 'delete'));
    await waitFor(() => expect(result.current.rows).toHaveLength(0));
    expect(result.current.notice).toBeNull();
  });
});

describe('the session gate (T-509, FR-AUT-07)', () => {
  it('fetches nothing while there is no session', async () => {
    // A hook cannot be called conditionally, so this runs during the `starting` phase too —
    // and an unauthenticated `GET /documents` answers 401, which FR-AUT-07's handler reads as
    // "the session ended", signing out the very user whose session was resuming.
    renderHook(() =>
      useDocuments({ open: true, conversationId: 'c1', turnInFlight: false, enabled: false }),
    );
    await waitFor(() => expect(streamEnabled).toBe(false));
    expect(listDocuments).not.toHaveBeenCalled();
  });

  it('does not open the stream when the session ends with the modal still open', async () => {
    // `open` stays true across the transition — the composition root is not unmounted, it just
    // renders the login screen — so a reconnect would authenticate with a token that is gone.
    const { rerender } = renderHook(
      ({ enabled }) =>
        useDocuments({ open: true, conversationId: 'c1', turnInFlight: false, enabled }),
      { initialProps: { enabled: true } },
    );
    await waitFor(() => expect(streamEnabled).toBe(true));

    rerender({ enabled: false });
    await waitFor(() => expect(streamEnabled).toBe(false));
  });

  it('fetches once the session arrives', async () => {
    const { rerender } = renderHook(
      ({ enabled }) =>
        useDocuments({ open: false, conversationId: 'c1', turnInFlight: false, enabled }),
      { initialProps: { enabled: false } },
    );
    expect(listDocuments).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(listDocuments).toHaveBeenCalledTimes(1));
  });
});

describe('FR-KBM-10 — the import takes the upload’s path (T-512)', () => {
  const driveFile = {
    file_id: 'f1',
    name: 'Q3 report.pdf',
    mime_type: 'application/pdf',
    size_bytes: 10,
    modified_time: null,
  };

  it('shows a pending entry named after the Drive file, then hands off on the server’s id', async () => {
    importDocument.mockResolvedValue({ kind: 'accepted', documentId: 'd7', status: 'QUEUED' });
    const { result } = renderHook(() =>
      useDocuments({ open: true, conversationId: 'c1', turnInFlight: false, enabled: true }),
    );

    await act(async () => {
      await result.current.importFile(driveFile, 'global');
    });
    // Not an optimistic document row: reconciling one would need the filename as identity, which
    // R-40/FR-KBM-08 reject in as many words.
    expect(result.current.pending).toHaveLength(1);
    expect(result.current.pending[0]).toMatchObject({
      filename: 'Q3 report.pdf',
      documentId: 'd7',
    });

    act(() => dispatch?.({ type: 'snapshot', documents: [doc({ document_id: 'd7' })] }));
    await waitFor(() => expect(result.current.pending).toHaveLength(0));
  });

  it('sends the file id and the scope, and a conversation only for scope=chat', async () => {
    importDocument.mockResolvedValue({ kind: 'accepted', documentId: 'd7', status: 'QUEUED' });
    const { result } = renderHook(() =>
      useDocuments({ open: true, conversationId: 'c1', turnInFlight: false, enabled: true }),
    );

    await act(async () => {
      await result.current.importFile(driveFile, 'chat');
    });
    expect(importDocument).toHaveBeenCalledWith('f1', 'chat', 'c1');
  });

  it('drops the pending entry when the import was refused', async () => {
    importDocument.mockResolvedValue({ kind: 'refused', detail: 'Too large.', status: 413 });
    const { result } = renderHook(() =>
      useDocuments({ open: true, conversationId: null, turnInFlight: false, enabled: true }),
    );

    await act(async () => {
      await result.current.importFile(driveFile, 'global');
    });
    expect(result.current.pending).toHaveLength(0);
    // The server's copy, verbatim — the import inherits R-33's whole response contract.
    expect(result.current.notice).toBe('Too large.');
  });

  it('reconciles the R-24 lock exactly as an upload does', async () => {
    importDocument.mockResolvedValue({ kind: 'locked', detail: 'busy' });
    const { result } = renderHook(() =>
      useDocuments({ open: true, conversationId: null, turnInFlight: false, enabled: true }),
    );

    await act(async () => {
      await result.current.importFile(driveFile, 'global');
    });
    expect(result.current.paused).toBe(true);
    // R-71(1): reconciled, never rendered as an error.
    expect(result.current.notice).toBeNull();
  });

  it('sets NO modal notice for a link problem — the picker owns that state', async () => {
    importDocument.mockResolvedValue({
      kind: 'link-required',
      code: 'ACCOUNT_NOT_LINKED',
      detail: 'not linked',
    });
    const { result } = renderHook(() =>
      useDocuments({ open: true, conversationId: null, turnInFlight: false, enabled: true }),
    );

    let outcome;
    await act(async () => {
      outcome = await result.current.importFile(driveFile, 'global');
    });
    // The action that answers it (Re-link) is a control only the picker has, so saying it here
    // would repeat it in the surface behind the one the user is looking at.
    expect(result.current.notice).toBeNull();
    expect(outcome).toMatchObject({ kind: 'link-required' });
    expect(result.current.pending).toHaveLength(0);
  });
});
