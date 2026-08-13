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

describe('nesting — only the topmost dialog owns the keyboard', () => {
  // T-508 is the first surface to nest one: the FR-KBM-01 modal renders FR-KBM-07's delete
  // confirmation inside itself. Two capture-phase listeners on `document` fire in registration
  // order and `stopPropagation` does not reach a sibling on the same node, so without the stack
  // in Dialog.tsx the OUTER dialog answers Escape and the whole modal closes.
  const Nested = ({
    onOuterClose,
    onInnerClose,
    inner,
  }: {
    onOuterClose: () => void;
    onInnerClose: () => void;
    inner: boolean;
  }) => (
    <Dialog title="Knowledge base" onClose={onOuterClose}>
      <button type="button">Close</button>
      {inner && (
        <Dialog title="Delete document?" onClose={onInnerClose}>
          <button type="button">Cancel</button>
          <button type="button">Delete</button>
        </Dialog>
      )}
    </Dialog>
  );

  const openNested = () => {
    const onOuterClose = vi.fn();
    const onInnerClose = vi.fn();
    const view = render(
      <Nested onOuterClose={onOuterClose} onInnerClose={onInnerClose} inner={false} />,
    );
    // Focus the control that "opens" the confirmation, so the restore target is meaningful.
    const closeButton = screen.getByRole('button', { name: 'Close' });
    closeButton.focus();
    view.rerender(<Nested onOuterClose={onOuterClose} onInnerClose={onInnerClose} inner />);
    return { ...view, onOuterClose, onInnerClose, closeButton, Nested };
  };

  it('routes Escape to the inner dialog only', () => {
    const { onOuterClose, onInnerClose } = openNested();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onInnerClose).toHaveBeenCalledTimes(1);
    expect(onOuterClose).not.toHaveBeenCalled();
  });

  it('leaves the Tab trap to the inner dialog, and the outer moves no focus', () => {
    // The final focus position ALONE does not discriminate here, and that is worth recording:
    // the inner listener registers second, so it runs last, and its `!panel.contains(active)`
    // escape clause repairs whatever the outer just did. Both handlers running therefore
    // converges on the right element — via two `focus()` calls and a visible flicker.
    // What the guard actually prevents is the outer's move happening at all, so that is what
    // this asserts; without it, `Close` is focused mid-keystroke and this spy fires.
    const { closeButton } = openNested();
    const outerMove = vi.spyOn(closeButton, 'focus');

    // `Delete` is the last focusable in the OUTER panel too (the inner is a descendant), so the
    // outer's wrap-from-last branch is armed on this exact keystroke.
    screen.getByRole('button', { name: 'Delete' }).focus();
    fireEvent.keyDown(document, { key: 'Tab' });

    expect(outerMove).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
  });

  it('restores focus to the opener inside the outer dialog when the inner closes', () => {
    const { rerender, onOuterClose, onInnerClose, closeButton } = openNested();
    rerender(<Nested onOuterClose={onOuterClose} onInnerClose={onInnerClose} inner={false} />);
    expect(document.activeElement).toBe(closeButton);
  });

  it('hands the keyboard back to the outer dialog once the inner unmounts', () => {
    const { rerender, onOuterClose, onInnerClose } = openNested();
    rerender(<Nested onOuterClose={onOuterClose} onInnerClose={onInnerClose} inner={false} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onOuterClose).toHaveBeenCalledTimes(1);
  });
});
