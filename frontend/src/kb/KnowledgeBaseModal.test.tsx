/**
 * The §4.7 modal's behaviour: FR-KBM-01's dismissal, FR-KBM-03's sections, FR-KBM-07's actions
 * and R-71(1)'s pause.
 *
 * Geometry lives in `KnowledgeBaseModal.css.test.ts` and is verified for real in a browser —
 * jsdom applies no external CSS, so nothing here is a claim about pixels.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { DocumentEvent } from '../api';
import { KnowledgeBaseModal } from './KnowledgeBaseModal';
import type { DocumentsStore } from './useDocuments';

function doc(overrides: Partial<DocumentEvent> = {}): DocumentEvent {
  return {
    document_id: 'd1',
    filename: 'Q3_Market_Report.pdf',
    mime_type: 'application/pdf',
    status: 'ACTIVE',
    current_version: 1,
    searchable: true,
    size_bytes: 2048,
    page_count: 58,
    chunk_count: 12,
    error_message: null,
    knowledge_base_id: 'kb1',
    scope: 'global',
    conversation_id: null,
    latest_job_id: 'j1',
    latest_job_error_code: null,
    latest_job_document_version: 1,
    created_at: '2026-07-10T09:00:00Z',
    updated_at: '2026-07-10T09:05:00Z',
    deleted_at: null,
    stalled: false,
    ...overrides,
  };
}

/** The FR-KBM-10 picker's two stores, shut. T-512's own surface is tested in
 *  `cloud/CloudImportDialog.test.tsx`; what this file cares about is that the modal composes it
 *  and that nothing else moved. */
function cloudProps(open = false) {
  return {
    open,
    files: {
      files: [],
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
    },
    link: {
      linked: true,
      account: 'person@example.com',
      loaded: true,
      notice: null,
      refresh: vi.fn(),
      beginLink: vi.fn(async () => undefined),
      unlink: vi.fn(async () => true),
      dismissNotice: vi.fn(),
    },
    onClose: vi.fn(),
  };
}

function setup(
  store: Partial<DocumentsStore> = {},
  props: { conversationId?: string | null; cloudOpen?: boolean } = {},
) {
  const upload = vi.fn();
  const importFile = vi.fn(async () => ({
    kind: 'accepted' as const,
    documentId: 'd9',
    status: 'QUEUED' as const,
  }));
  const act = vi.fn();
  const dismissNotice = vi.fn();
  const onClose = vi.fn();
  const onCloudImport = vi.fn();
  const full: DocumentsStore = {
    rows: [],
    pending: [],
    connection: 'open',
    announcement: null,
    notice: null,
    paused: false,
    upload,
    importFile,
    act,
    dismissNotice,
    ...store,
  };
  const cloud = cloudProps(props.cloudOpen ?? false);
  render(
    <KnowledgeBaseModal
      store={full}
      conversationId={'conversationId' in props ? props.conversationId! : 'c1'}
      onClose={onClose}
      onCloudImport={onCloudImport}
      cloud={cloud}
    />,
  );
  return { upload, importFile, act, dismissNotice, onClose, onCloudImport, cloud };
}

const rowFor = (name: string) => screen.getByText(name).closest('li') as HTMLElement;

describe('FR-KBM-01/02 — the shell', () => {
  it('is a modal dialog named by its own heading', () => {
    setup();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByRole('heading', { level: 2 }).textContent).toBe('Knowledge base');
  });

  it('renders §9’s subtitle verbatim', () => {
    setup();
    expect(
      screen.getByText(
        'Global documents are searched in every chat; attachments only in this one.',
      ),
    ).not.toBeNull();
  });

  it('closes on the ✕, on the footer Close, and on the overlay', () => {
    // FR-KBM-01 names all three. The ✕ and the footer button are separately named on purpose:
    // two controls called "Close" are indistinguishable in a screen reader's element list.
    const { onClose } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Close knowledge base' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(screen.getByTestId('dialog-overlay'));
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('does not close on a click inside the panel', () => {
    const { onClose } = setup();
    fireEvent.click(screen.getByRole('dialog'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('owns no <h1> and starts its own headings at <h3>', () => {
    // T-502 owns the document's only <h1>; Dialog's title is the <h2>.
    setup();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 3 }).length).toBe(2);
  });
});

describe('FR-KBM-03 — the two sections', () => {
  it('separates global documents from this chat’s attachments', () => {
    setup({
      rows: [
        doc({ document_id: 'g', filename: 'Global.pdf', scope: 'global' }),
        doc({
          document_id: 'a',
          filename: 'Attached.pdf',
          scope: 'chat',
          conversation_id: 'c1',
        }),
        doc({
          document_id: 'x',
          filename: 'Elsewhere.pdf',
          scope: 'chat',
          conversation_id: 'other',
        }),
      ],
    });

    expect(screen.getByText('GLOBAL · 1 DOCUMENT')).not.toBeNull();
    expect(screen.getByText('THIS CHAT · 1 ATTACHMENT')).not.toBeNull();
    // FR-KBM-03 is explicit that another chat's attachments are not in scope.
    expect(screen.queryByText('Elsewhere.pdf')).toBeNull();
  });

  it('shows an empty state rather than a heading over nothing', () => {
    setup();
    expect(screen.getByText('No documents yet — add one below.')).not.toBeNull();
  });
});

describe('FR-KBM-04 — the row', () => {
  it('renders the badge, the name and the meta line', () => {
    setup({ rows: [doc()] });
    const row = rowFor('Q3_Market_Report.pdf');
    expect(within(row).getByText('PDF')).not.toBeNull();
    expect(within(row).getByText('58 pages · indexed Jul 10')).not.toBeNull();
    expect(within(row).getByText('Ready')).not.toBeNull();
  });

  it('renders a text label beside the colour, on every state', () => {
    // NFR-A11Y-06 accepts the palette's contrast failures strictly on the condition that colour
    // is never the sole carrier, and names FR-KBM-04's label as one of the two things that make
    // it so. A status rendered as a dot or a bare hue would break the exception.
    setup({
      rows: [
        doc({ document_id: '1', filename: 'a.pdf', status: 'EMBEDDING' }),
        doc({ document_id: '2', filename: 'b.pdf', status: 'FAILED' }),
        doc({ document_id: '3', filename: 'c.pdf', status: 'DELETING' }),
      ],
    });
    expect(screen.getByText('Embedding')).not.toBeNull();
    expect(screen.getByText('Failed')).not.toBeNull();
    expect(screen.getByText('Deleting')).not.toBeNull();
  });
});

describe('FR-KBM-07 — the three actions', () => {
  it('offers Replace and Delete on an active document, and no Retry', () => {
    setup({ rows: [doc()] });
    const row = rowFor('Q3_Market_Report.pdf');
    expect(within(row).getByRole('button', { name: /^Replace/ })).not.toBeNull();
    expect(within(row).getByRole('button', { name: /^Delete/ })).not.toBeNull();
    expect(within(row).queryByRole('button', { name: /^Retry/ })).toBeNull();
  });

  it('offers Retry only on a failed document', () => {
    setup({ rows: [doc({ status: 'FAILED' })] });
    expect(
      within(rowFor('Q3_Market_Report.pdf')).getByRole('button', { name: /^Retry/ }),
    ).not.toBeNull();
  });

  it('names the document in each action’s accessible name', () => {
    // The label alone repeats down the list with nothing to say which row it belongs to.
    setup({ rows: [doc()] });
    expect(screen.getByRole('button', { name: 'Delete Q3_Market_Report.pdf' })).not.toBeNull();
  });

  it('retries immediately, but confirms before deleting', () => {
    // R-56(5)'s no-undo mitigation: deletion is asynchronous and irreversible, retry is neither.
    const { act } = setup({ rows: [doc({ status: 'FAILED' })] });

    fireEvent.click(screen.getByRole('button', { name: /^Retry/ }));
    expect(act).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: /^Delete Q3/ }));
    expect(act).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Delete this document?')).not.toBeNull();

    // The confirmation's own button, whose name is exactly "Delete" — the row's is
    // "Delete Q3_Market_Report.pdf", so an anchored pattern separates them.
    fireEvent.click(screen.getByRole('button', { name: /^Delete$/ }));
    expect(act).toHaveBeenCalledTimes(2);
  });

  it('deletes nothing when the confirmation is cancelled', () => {
    const { act } = setup({ rows: [doc()] });
    fireEvent.click(screen.getByRole('button', { name: /^Delete Q3/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(act).not.toHaveBeenCalled();
    expect(screen.queryByText('Delete this document?')).toBeNull();
  });
});

describe('R-71(1) — the pause is surfaced, not just enforced', () => {
  it('says why the actions are unavailable', () => {
    // A silently-disabled control is indistinguishable from a defect, which is the half of OI-31
    // that was open.
    setup({ rows: [doc()], paused: true });
    expect(screen.getByText('Paused while a response is being generated.')).not.toBeNull();
  });

  it('disables the row actions and the drop zone', () => {
    setup({ rows: [doc()], paused: true });
    expect((screen.getByRole('button', { name: /^Delete Q3/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (screen.getByRole('button', { name: /Drag & drop/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it('leaves them enabled when nothing is generating', () => {
    // The discriminating half: `disabled` is over-determined otherwise, since a row in the wrong
    // state has its buttons removed entirely rather than disabled.
    setup({ rows: [doc()], paused: false });
    expect((screen.getByRole('button', { name: /^Delete Q3/ }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it('renders a refusal as a dismissible notice, not an error', () => {
    const { dismissNotice } = setup({ notice: 'Storage limit reached.' });
    const notice = screen.getByText('Storage limit reached.');
    expect(notice.closest('[role="status"]')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(dismissNotice).toHaveBeenCalled();
  });
});

describe('FR-KBM-05 — the drop zone and R-71(3)’s scope control', () => {
  it('renders §9’s prompt and caption verbatim', () => {
    setup();
    expect(screen.getByText('Drag & drop files here, or click to browse')).not.toBeNull();
    expect(screen.getByText('PDF · DOCX · CSV · MD — max 50 MB')).not.toBeNull();
  });

  it('uploads to the global scope by default', () => {
    const { upload } = setup();
    const input = document.querySelector('input[type="file"][multiple]') as HTMLInputElement;
    const file = new File(['x'], 'a.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [file] } });
    expect(upload).toHaveBeenCalledWith([file], 'global');
  });

  it('uploads to this chat once the segment is chosen', () => {
    // Without this control FR-KBM-03's second section could never be populated by any affordance
    // in the GUI — the reason R-71(3) added it.
    const { upload } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'This chat' }));
    const input = document.querySelector('input[type="file"][multiple]') as HTMLInputElement;
    const file = new File(['x'], 'a.pdf', { type: 'application/pdf' });
    fireEvent.change(input, { target: { files: [file] } });
    expect(upload).toHaveBeenCalledWith([file], 'chat');
  });

  it('carries the active scope in aria-pressed, not in colour alone', () => {
    setup();
    expect(screen.getByRole('button', { name: 'Global' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
    expect(screen.getByRole('button', { name: 'This chat' }).getAttribute('aria-pressed')).toBe(
      'false',
    );
  });

  it('cannot target a chat that does not exist yet', () => {
    setup({}, { conversationId: null });
    expect((screen.getByRole('button', { name: 'This chat' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('offers a keyboard-operable alternative to dragging', () => {
    // Drag-and-drop is pointer-only by nature, so the zone is a real <button> driving a file
    // input — NFR-A11Y-04, and the reason it is not the prototype's <div>.
    setup();
    const zone = screen.getByRole('button', { name: /Drag & drop/ });
    expect(zone.tagName).toBe('BUTTON');
  });
});

describe('FR-KBM-06 — the footer', () => {
  it('keeps the prototype’s two buttons and wires the primary one', () => {
    // §8.52: a prototype primary action is retargeted, never left inert.
    const { onCloudImport } = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Add from cloud drive' }));
    expect(onCloudImport).toHaveBeenCalled();
  });
});

describe('NFR-A11Y-05 — announcements', () => {
  it('announces a transition politely, in a hidden live region', () => {
    setup({ announcement: 'Q3_Market_Report.pdf is now Ready.' });
    const region = screen.getByText('Q3_Market_Report.pdf is now Ready.');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.className).toContain('visually-hidden');
  });
});
