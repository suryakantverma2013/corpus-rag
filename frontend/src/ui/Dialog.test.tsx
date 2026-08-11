/**
 * The NFR-A11Y-04 modal contract: trap, Escape, restore.
 *
 * These are the behaviours that are easy to write subtly wrong and invisible to a mouse tester,
 * which is why the primitive exists once rather than four times (T-503 here, T-508's KB modal
 * and document-delete confirm, T-509's change-password modal).
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Dialog } from './Dialog';

const open = (onClose = vi.fn()) => {
  const result = render(
    <Dialog title="Delete conversation?" onClose={onClose}>
      <button type="button">Cancel</button>
      <button type="button">Delete</button>
    </Dialog>,
  );
  return { ...result, onClose };
};

describe('semantics', () => {
  it('is a modal dialog named by its own heading', () => {
    open();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    // Named via aria-labelledby -> the <h2>, so the name and the visible title cannot drift.
    expect(dialog.getAttribute('aria-labelledby')).toBe(
      screen.getByRole('heading', { level: 2 }).id,
    );
  });

  it('does not introduce a second h1 (T-502 owns the document heading)', () => {
    open();
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
  });
});

describe('focus (NFR-A11Y-04)', () => {
  it('moves focus into the dialog on open', () => {
    open();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
  });

  it('falls back to the panel when there is nothing focusable inside', () => {
    render(
      <Dialog title="Notice" onClose={vi.fn()}>
        <p>Nothing to do here.</p>
      </Dialog>,
    );
    expect(document.activeElement).toBe(screen.getByRole('dialog'));
  });

  it('restores focus to the opener on unmount', () => {
    // Without this a dismissed dialog drops focus to <body> and a keyboard user restarts from
    // the top of the page — the half of the contract a trap alone does not give you.
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = open();
    expect(document.activeElement).not.toBe(opener);

    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('wraps Tab from the last control back to the first', () => {
    open();
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const del = screen.getByRole('button', { name: 'Delete' });

    del.focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(cancel);
  });

  it('wraps Shift+Tab from the first control back to the last', () => {
    open();
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const del = screen.getByRole('button', { name: 'Delete' });

    cancel.focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(del);
  });

  it('pulls focus back when it has escaped the panel entirely', () => {
    // Focus can leave without a Tab we saw — an autofill popup, or a caller focusing something
    // itself. Wrapping only from `first`/`last` would leave the trap open in that case.
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    open();
    outside.focus();

    fireEvent.keyDown(document, { key: 'Tab' });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
    outside.remove();
  });

  it('pulls focus back on Shift+Tab too', () => {
    // The forward and backward branches carry the same escape clause and only the forward one
    // was covered — a mutation deleting it from the Shift branch changed nothing. Two branches
    // need two tests, however symmetrical they look.
    const outside = document.createElement('button');
    document.body.appendChild(outside);
    open();
    outside.focus();

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Delete' }));
    outside.remove();
  });
});

describe('dismissal', () => {
  it('closes on Escape', () => {
    const { onClose } = open();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on an overlay click', () => {
    const { onClose } = open();
    fireEvent.click(screen.getByTestId('dialog-overlay'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close on a click inside the panel', () => {
    // The prototype's `stopProp` — without it every click on a control inside the KB modal
    // would also dismiss it.
    const { onClose } = open();
    fireEvent.click(screen.getByRole('dialog'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('stops listening once unmounted', () => {
    const { onClose, unmount } = open();
    unmount();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });
});
