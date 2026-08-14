/**
 * FR-KBM-10's picker: the listbox contract, the import states, FR-AUT-11's refusals and unlink.
 *
 * Geometry lives in `CloudImportDialog.css.test.ts` and is verified for real in a browser —
 * jsdom applies no external CSS, so nothing here is a claim about pixels.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { DriveFile } from '../api';
import type { Outcome } from '../kb/mutations';
import { CloudImportDialog } from './CloudImportDialog';
import type { CloudFilesStore } from './useCloudFiles';
import type { CloudLinkStore } from './useCloudLink';

function file(overrides: Partial<DriveFile> = {}): DriveFile {
  return {
    file_id: 'f1',
    name: 'Q3 report.pdf',
    mime_type: 'application/pdf',
    size_bytes: 2_200_000,
    modified_time: '2026-07-10T09:00:00Z',
    ...overrides,
  };
}

const ACCEPTED: Outcome = { kind: 'accepted', documentId: 'd9', status: 'QUEUED' };

function setup(
  files: Partial<CloudFilesStore> = {},
  link: Partial<CloudLinkStore> = {},
  props: { paused?: boolean; onImport?: (f: DriveFile) => Promise<Outcome> } = {},
) {
  const setSearch = vi.fn();
  const loadMore = vi.fn();
  const dismissNotice = vi.fn();
  const beginLink = vi.fn(async () => undefined);
  const unlink = vi.fn(async () => true);
  const refresh = vi.fn();
  const onClose = vi.fn();
  const onImport = vi.fn(props.onImport ?? (async () => ACCEPTED));

  const fullFiles: CloudFilesStore = {
    files: [],
    loading: false,
    loadingMore: false,
    loaded: true,
    canLoadMore: false,
    linkRequired: null,
    notice: null,
    search: '',
    setSearch,
    loadMore,
    dismissNotice,
    ...files,
  };
  const fullLink: CloudLinkStore = {
    linked: true,
    account: 'person@example.com',
    loaded: true,
    notice: null,
    refresh,
    beginLink,
    unlink,
    dismissNotice: vi.fn(),
    ...link,
  };

  const { unmount } = render(
    <CloudImportDialog
      files={fullFiles}
      link={fullLink}
      scope="global"
      paused={props.paused ?? false}
      onImport={onImport}
      onClose={onClose}
    />,
  );
  return {
    setSearch,
    loadMore,
    dismissNotice,
    beginLink,
    unlink,
    refresh,
    onClose,
    onImport,
    unmount,
  };
}

const options = () => screen.getAllByRole('option');

describe('the shell', () => {
  it('is a modal dialog named by its own heading', () => {
    setup();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Import from Google Drive');
  });

  it('closes from the ✕ and from the footer, which have different names', () => {
    const { onClose } = setup();
    // Two controls both called "Close" are indistinguishable in a screen reader's element list.
    fireEvent.click(screen.getByRole('button', { name: 'Close cloud import' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe('FR-KBM-10 — the flat list', () => {
  it('renders a row per file, with its badge and meta line', () => {
    setup({ files: [file()] });
    const row = options()[0];
    expect(within(row).getByText('PDF')).not.toBeNull();
    expect(within(row).getByText('Q3 report.pdf')).not.toBeNull();
    expect(within(row).getByText('2.1 MB · modified Jul 10')).not.toBeNull();
  });

  it('renders no meta line when the provider supplied neither field', () => {
    setup({ files: [file({ size_bytes: null, modified_time: null })] });
    expect(within(options()[0]).queryByText(/modified/)).toBeNull();
  });

  it('is a listbox of options, never a list of links', () => {
    setup({ files: [file(), file({ file_id: 'f2', name: 'b.csv' })] });
    expect(screen.getByRole('listbox')).not.toBeNull();
    expect(options()).toHaveLength(2);
  });

  it('distinguishes an empty Drive from an empty search', () => {
    const { unmount } = setup({ files: [] });
    expect(screen.getByText('No importable files in your Drive.')).not.toBeNull();
    unmount();

    setup({ files: [], search: 'zzz' });
    expect(screen.getByText('No files match that search.')).not.toBeNull();
  });

  it('shows a loading state instead of stale rows for a query that no longer applies', () => {
    setup({ files: [], loading: true });
    expect(screen.getByText('Loading…')).not.toBeNull();
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('offers more only when the provider sent a token', () => {
    const { unmount } = setup({ files: [file()], canLoadMore: false });
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
    unmount();

    const { loadMore } = setup({ files: [file()], canLoadMore: true });
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(loadMore).toHaveBeenCalled();
  });
});

describe('NFR-A11Y-03 — the listbox keyboard contract', () => {
  const search = () => screen.getByRole('combobox');

  it('takes initial focus, or the arrow keys below are bound to nothing', () => {
    // T-514 found this live: `Dialog` focuses the first focusable control, which here is the
    // header ✕, so the picker opened with its own navigation inert until the user tabbed twice
    // to the field — and a screen-reader user was announced a Close button on a surface whose
    // whole purpose is finding a file. Every other assertion in this describe block focuses the
    // input itself through `fireEvent`, so none of them could see it.
    setup({ files: [file()] });
    expect(document.activeElement).toBe(search());
  });

  it('keeps DOM focus in the input and moves a virtual focus through the rows', () => {
    setup({ files: [file(), file({ file_id: 'f2', name: 'b.csv' })] });
    const input = search();

    expect(input.getAttribute('aria-activedescendant')).toBeNull();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input.getAttribute('aria-activedescendant')).toBe('cloud-option-0');
    expect(options()[0].getAttribute('aria-selected')).toBe('true');

    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input.getAttribute('aria-activedescendant')).toBe('cloud-option-1');
    // Wraps, so the list has no dead end at either extreme.
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input.getAttribute('aria-activedescendant')).toBe('cloud-option-0');
  });

  it('imports the active row on Enter', async () => {
    const { onImport } = setup({ files: [file()] });
    fireEvent.keyDown(search(), { key: 'ArrowDown' });
    fireEvent.keyDown(search(), { key: 'Enter' });
    await waitFor(() => expect(onImport).toHaveBeenCalledWith(file(), 'global'));
  });

  it('imports NOTHING on Enter with no row active', () => {
    // The composer's FR-CMP-03 conflict, one surface over: the list opens with nothing active,
    // so Enter must not import whatever happens to be first.
    const { onImport } = setup({ files: [file()] });
    fireEvent.keyDown(search(), { key: 'Enter' });
    expect(onImport).not.toHaveBeenCalled();
  });

  it('rows are out of the tab order, so tabbing past fifty files is not required', () => {
    setup({ files: [file(), file({ file_id: 'f2', name: 'b.csv' })] });
    for (const option of options()) expect(option.getAttribute('tabindex')).toBe('-1');
  });

  it('hover and the arrow keys agree on which row is active', () => {
    setup({ files: [file(), file({ file_id: 'f2', name: 'b.csv' })] });
    fireEvent.mouseEnter(options()[1]);
    expect(screen.getByRole('combobox').getAttribute('aria-activedescendant')).toBe(
      'cloud-option-1',
    );
  });

  it('drops the virtual focus when the rows change under it', () => {
    // Otherwise `aria-activedescendant` names an element that no longer exists, and Enter
    // imports nothing while looking as though it should.
    const { rerender } = render(<Harness files={[file(), file({ file_id: 'f2', name: 'b' })]} />);
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' });
    expect(screen.getByRole('combobox').getAttribute('aria-activedescendant')).toBe(
      'cloud-option-0',
    );

    rerender(<Harness files={[file({ file_id: 'f3', name: 'c.md' })]} />);
    expect(screen.getByRole('combobox').getAttribute('aria-activedescendant')).toBeNull();
  });
});

/** A minimal host so a test can change the file list between renders. */
function Harness({ files }: { files: readonly DriveFile[] }) {
  const store: CloudFilesStore = {
    files,
    loading: false,
    loadingMore: false,
    loaded: true,
    canLoadMore: false,
    linkRequired: null,
    notice: null,
    search: '',
    setSearch: vi.fn(),
    loadMore: vi.fn(),
    dismissNotice: vi.fn(),
  };
  const link: CloudLinkStore = {
    linked: true,
    account: 'a@b.c',
    loaded: true,
    notice: null,
    refresh: vi.fn(),
    beginLink: vi.fn(async () => undefined),
    unlink: vi.fn(async () => true),
    dismissNotice: vi.fn(),
  };
  return (
    <CloudImportDialog
      files={store}
      link={link}
      scope="global"
      paused={false}
      onImport={vi.fn(async () => ACCEPTED)}
      onClose={vi.fn()}
    />
  );
}

describe('importing', () => {
  it('sends the file id with the scope the drop zone is set to', async () => {
    const { onImport } = setup({ files: [file()] });
    fireEvent.click(options()[0]);
    await waitFor(() => expect(onImport).toHaveBeenCalledWith(file(), 'global'));
  });

  it('marks the row and refuses a second send', async () => {
    const { onImport } = setup({ files: [file()] });
    fireEvent.click(options()[0]);

    await waitFor(() => expect(within(options()[0]).getByText('Imported')).not.toBeNull());
    expect(options()[0].getAttribute('aria-disabled')).toBe('true');
    fireEvent.click(options()[0]);
    expect(onImport).toHaveBeenCalledTimes(1);
  });

  it('counts an FR-KBM-08 duplicate as imported', async () => {
    // The document is in the knowledge base, which is what the user asked for. Releasing the
    // row would invite them to send it again for the same answer.
    setup(
      { files: [file()] },
      {},
      {
        onImport: async () => ({ kind: 'duplicate', documentId: 'd1', status: 'ACTIVE' }),
      },
    );
    fireEvent.click(options()[0]);
    await waitFor(() => expect(within(options()[0]).getByText('Imported')).not.toBeNull());
  });

  it('releases the row when the import failed, so it can be retried', async () => {
    const { onImport } = setup(
      { files: [file()] },
      {},
      {
        onImport: async () => ({ kind: 'refused', detail: 'too large', status: 413 }),
      },
    );
    fireEvent.click(options()[0]);

    await waitFor(() => expect(within(options()[0]).getByText('Import')).not.toBeNull());
    expect(options()[0].getAttribute('aria-disabled')).toBe('false');
    fireEvent.click(options()[0]);
    await waitFor(() => expect(onImport).toHaveBeenCalledTimes(2));
  });

  it('re-reads the link when the import came back naming it', async () => {
    const { refresh } = setup(
      { files: [file()] },
      {},
      {
        onImport: async () => ({
          kind: 'link-required',
          code: 'CLOUD_ACCESS_REVOKED',
          detail: 'revoked',
        }),
      },
    );
    fireEvent.click(options()[0]);
    // What turns a revoked grant into the Re-link affordance rather than a dead notice.
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });
});

describe('R-71(1) — the pause is surfaced, not merely enforced', () => {
  it('says so, and refuses the import', () => {
    const { onImport } = setup({ files: [file()] }, {}, { paused: true });
    expect(
      screen.getByText('Importing is paused while a response is being generated.'),
    ).not.toBeNull();
    fireEvent.click(options()[0]);
    expect(onImport).not.toHaveBeenCalled();
  });

  it('keeps the row in the listbox rather than removing it from the a11y tree', () => {
    // `aria-disabled`, not `disabled`: a disabled <button> stops being reachable by the arrow
    // keys that move the virtual focus, so the row would vanish for a keyboard user.
    setup({ files: [file()] }, {}, { paused: true });
    expect(options()).toHaveLength(1);
    expect(options()[0].getAttribute('aria-disabled')).toBe('true');
  });
});

describe('FR-AUT-11 — the two 409s and unlink', () => {
  it('renders the server’s copy verbatim behind one Re-link action', () => {
    const { beginLink } = setup({
      linkRequired: { code: 'CLOUD_ACCESS_REVOKED', detail: 'Google refused access.' },
    });
    // R-57(4): render `detail`, never match on it. The two codes differ in copy, and the copy
    // is the server's — which is why this component names neither code.
    expect(screen.getByText('Google refused access.')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect account' }));
    expect(beginLink).toHaveBeenCalled();
  });

  it('shows no empty-state beside a link refusal', () => {
    // "No importable files" would be false: the truth is that nothing could be listed at all.
    setup({ files: [], linkRequired: { code: 'ACCOUNT_NOT_LINKED', detail: 'not linked' } });
    expect(screen.queryByText('No importable files in your Drive.')).toBeNull();
  });

  it('names the account it will import from', () => {
    setup({}, { account: 'person@example.com' });
    expect(screen.getByText('person@example.com')).not.toBeNull();
  });

  it('confirms unlink, and says imported documents survive it', async () => {
    const { unlink, onClose } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }));

    const confirmation = screen.getAllByRole('dialog').at(-1) as HTMLElement;
    expect(within(confirmation).getByText(/are copies and are not affected/)).not.toBeNull();

    fireEvent.click(within(confirmation).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(unlink).toHaveBeenCalled());
    // Nothing left to list, so the surface goes with the link.
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('keeps the picker open when unlinking failed', async () => {
    const { onClose } = setup({}, { unlink: vi.fn(async () => false) });
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }));
    const confirmation = screen.getAllByRole('dialog').at(-1) as HTMLElement;
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(screen.queryAllByRole('dialog')).toHaveLength(1));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('cancelling the confirmation leaves the link alone', () => {
    const { unlink } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }));
    const confirmation = screen.getAllByRole('dialog').at(-1) as HTMLElement;
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Cancel' }));
    expect(unlink).not.toHaveBeenCalled();
    expect(screen.queryAllByRole('dialog')).toHaveLength(1);
  });

  it('offers no unlink for an account that is not linked', () => {
    setup({}, { linked: false, account: null });
    expect(screen.queryByRole('button', { name: 'Unlink' })).toBeNull();
  });
});

describe('nesting — T-508’s dialog stack, second real user', () => {
  it('Escape closes the confirmation ALONE, not the picker under it', () => {
    const { onClose } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }));
    expect(screen.queryAllByRole('dialog')).toHaveLength(2);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryAllByRole('dialog')).toHaveLength(1);
    // Before the stack, the outer handler ran first (registration order) and took both down.
    expect(onClose).not.toHaveBeenCalled();
  });

  it('the outer dialog moves no focus while the inner one owns the keyboard', () => {
    // A green end state is not evidence when two handlers race: the inner listener runs last
    // and repairs whatever the outer did, so the assertion has to be that the outer never acted.
    setup();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }));

    const search = screen.getByRole('combobox');
    const moved = vi.spyOn(search, 'focus');
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(moved).not.toHaveBeenCalled();
  });

  it('Escape closes the picker when nothing is nested over it', () => {
    const { onClose } = setup();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});

describe('notices', () => {
  it('renders the store’s refusal and dismisses it', () => {
    const { dismissNotice } = setup({ notice: 'Too many requests.' });
    expect(screen.getByText('Too many requests.')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(dismissNotice).toHaveBeenCalled();
  });

  it('drives the search through the store rather than holding its own copy', () => {
    const { setSearch } = setup();
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'report' } });
    expect(setSearch).toHaveBeenCalledWith('report');
  });

  it('gives the search input an accessible name', () => {
    setup();
    expect(screen.getByRole('combobox', { name: 'Search your Drive' })).not.toBeNull();
  });
});
