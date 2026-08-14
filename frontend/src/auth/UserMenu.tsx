/**
 * FR-AUT-08 — the user-menu popover: the signed-in email, "Change password…", "Sign out".
 *
 * Rendered into the sidebar footer (which is `position: relative` for it), not into the
 * overlay slot: FR-AUT-08 says "anchored above it", and an overlay-hosted popover would have
 * to be positioned by measuring the row, which breaks the moment the column resizes.
 *
 * **`role="menu"` with `<button role="menuitem">`, and arrow keys, because that contract is
 * what the trigger already promises**: T-503 shipped the user row with `aria-haspopup="menu"`.
 * Escape, an outside click, and selecting an item all close it (FR-AUT-08), and closing always
 * returns focus to the row — a popover that drops focus on the body strands a keyboard user
 * at the top of the page (NFR-A11Y-04).
 */
import { useEffect, useRef } from 'react';
import styles from './UserMenu.module.css';
import { CHANGE_PASSWORD_ITEM, SIGN_OUT, USER_MENU_LABEL } from './copy';

export interface UserMenuProps {
  email: string;
  onChangePassword: () => void;
  onSignOut: () => void;
  /** Closes without choosing anything — Escape or an outside click. */
  onClose: () => void;
}

export function UserMenu({ email, onChangePassword, onSignOut, onClose }: UserMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  // Registered once, so the handler must reach the *current* callbacks through a ref — the
  // Dialog's reason (T-508): threading them through the deps re-runs the effect on every
  // render of a caller passing inline arrows, which would re-run the initial focus move.
  const handlers = useRef({ onClose });
  useEffect(() => {
    handlers.current = { onClose };
  }, [onClose]);

  useEffect(() => {
    const menu = ref.current;
    if (menu === null) return;

    const opener = document.activeElement;
    const items = () => [...menu.querySelectorAll<HTMLButtonElement>('button')];
    items()[0]?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        handlers.current.onClose();
        return;
      }
      // NFR-A11Y-04 (T-511). Tab is not part of a menu's own keyboard contract, so without
      // this the browser walks focus straight out of the popover while it stays open —
      // measured live: focus landed on the trigger with the menu still rendered behind it.
      // An open menu that no longer holds focus is indistinguishable from a stuck overlay.
      // `preventDefault` then close: the cleanup below returns focus to the opener, so the
      // user resumes from the control they opened, exactly as Escape leaves them.
      if (event.key === 'Tab') {
        event.preventDefault();
        handlers.current.onClose();
        return;
      }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      const list = items();
      const at = list.indexOf(document.activeElement as HTMLButtonElement);
      // Wraps at both ends. `at === -1` (focus moved out from under us) lands on the first
      // item for ArrowDown and the last for ArrowUp, rather than doing nothing.
      const next =
        event.key === 'ArrowDown'
          ? list[(at + 1) % list.length]
          : list[(at <= 0 ? list.length : at) - 1];
      event.preventDefault();
      next?.focus();
    };

    // Pointer-down rather than click: a click that starts inside and ends outside (a drag off
    // an item) would otherwise close the menu after the item had already fired.
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !menu.contains(target)) handlers.current.onClose();
    };

    document.addEventListener('keydown', onKeyDown, true);
    // NOT capture: the sidebar row's own toggle must run first, or clicking the row while the
    // menu is open closes it here and the row's handler immediately reopens it.
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      document.removeEventListener('pointerdown', onPointerDown);
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, []);

  return (
    <div
      ref={ref}
      className={`${styles.popover} animate-fade-up`}
      role="menu"
      aria-label={USER_MENU_LABEL}
    >
      {/* Not a menuitem: it is the menu's heading, not an action. Announced as the group's
          label so a screen-reader user knows *whose* account this is before choosing. */}
      <div className={`${styles.email} mono`}>{email}</div>
      <button type="button" role="menuitem" className={styles.item} onClick={onChangePassword}>
        {CHANGE_PASSWORD_ITEM}
      </button>
      <button type="button" role="menuitem" className={styles.item} onClick={onSignOut}>
        {SIGN_OUT}
      </button>
    </div>
  );
}
