/**
 * FR-AUT-08 — the user-menu popover.
 *
 * Every close route the requirement names ("Escape, outside click, or item selection") is one
 * test, plus the thing it does not name and NFR-A11Y-04 requires: where focus goes afterwards.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CHANGE_PASSWORD_ITEM, SIGN_OUT } from './copy';
import { UserMenu } from './UserMenu';

function mount() {
  const onChangePassword = vi.fn();
  const onSignOut = vi.fn();
  const onClose = vi.fn();
  // A real opener, so the focus-restore assertion has somewhere true to land — the sidebar
  // row is a <button> (T-503) and is what `document.activeElement` is when this opens.
  const opener = document.createElement('button');
  opener.textContent = 'Maya Jensen';
  document.body.append(opener);
  opener.focus();

  const result = render(
    <UserMenu
      email="maya.jensen@example.com"
      onChangePassword={onChangePassword}
      onSignOut={onSignOut}
      onClose={onClose}
    />,
  );
  return { ...result, onChangePassword, onSignOut, onClose, opener };
}

const items = () => screen.getAllByRole('menuitem');

describe('contents', () => {
  it('shows the signed-in email and the two items', () => {
    mount();
    expect(screen.getByText('maya.jensen@example.com')).not.toBeNull();
    expect(items().map((item) => item.textContent)).toEqual([CHANGE_PASSWORD_ITEM, SIGN_OUT]);
  });

  it('is a menu, matching what the sidebar row already promises', () => {
    // T-503 shipped the row with `aria-haspopup="menu"`, so this has to actually be one —
    // a promise in one component and a plain <div> in another is worse than neither.
    mount();
    expect(screen.getByRole('menu')).not.toBeNull();
  });

  it('does not announce the email as an action', () => {
    mount();
    expect(items()).toHaveLength(2);
  });
});

describe('closing (FR-AUT-08)', () => {
  it('closes on Escape', () => {
    const { onClose } = mount();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Tab, so focus never leaves an open menu behind (NFR-A11Y-04)', () => {
    // T-511, measured live: the items are ordinary tab stops, so tabbing past the last one put
    // focus back on the trigger with the menu still rendered around it. This menu survived that
    // better than the sidebar's — its listener is on `document`, so Escape still worked — but
    // an open menu that no longer holds focus is a stuck overlay either way. Both menus now
    // dismiss on Tab; they were built differently, which is why only an end-to-end sweep found
    // the pair.
    const { onClose } = mount();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on an outside pointer-down', () => {
    const { onClose } = mount();
    fireEvent.pointerDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stays open for a pointer-down inside it', () => {
    const { onClose } = mount();
    fireEvent.pointerDown(screen.getByRole('menu'));
    expect(onClose).not.toHaveBeenCalled();
  });

  it('reports each item selection to its own handler', () => {
    const { onChangePassword, onSignOut } = mount();
    fireEvent.click(items()[0]);
    expect(onChangePassword).toHaveBeenCalledTimes(1);
    fireEvent.click(items()[1]);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });
});

describe('keyboard (NFR-A11Y-03/04)', () => {
  it('focuses the first item on open', () => {
    mount();
    expect(document.activeElement).toBe(items()[0]);
  });

  it('moves and wraps with the arrow keys', () => {
    mount();
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(items()[1]);
    // Wraps at the end rather than stopping — two items make a stop especially unhelpful.
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(items()[0]);
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(document.activeElement).toBe(items()[1]);
  });

  it('returns focus to the row that opened it', () => {
    // Without this a keyboard user who presses Escape lands on <body> and restarts from the
    // top of the page — the reason NFR-A11Y-04 names restoring focus and not just trapping it.
    const { unmount, opener } = mount();
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});
