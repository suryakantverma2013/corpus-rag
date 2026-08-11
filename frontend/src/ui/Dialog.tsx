/**
 * The modal primitive: overlay, focus trap, Escape, focus restore.
 *
 * Built here rather than inside the sidebar because NFR-A11Y-04 requires the same three
 * behaviours of every modal surface in the product — FR-SBR-07's delete confirmation (T-503),
 * the FR-KBM-01 knowledge-base modal and its FR-KBM-07 delete confirmation (T-508), and
 * FR-AUT-09's change-password modal (T-509). A trap is easy to write and easy to write
 * *subtly* wrong, and four independent copies is four chances to get the restore path wrong in
 * a way only a keyboard user ever notices.
 *
 * Visual shell only — the overlay and the panel box. Each caller supplies its own panel size
 * and contents, because FR-KBM-01 (520px, `max-height:78vh`, radius 16) and FR-AUT-09 (420px)
 * differ. The overlay values are the prototype's, promoted to tokens in T-501/T-502.
 */
import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import styles from './Dialog.module.css';

export interface DialogProps {
  /** Accessible name. Rendered by the caller; this is what `aria-labelledby` points at. */
  title: string;
  /** Called on Escape, on an overlay click, and by the caller's own close controls. */
  onClose: () => void;
  /** Extra class for the panel, so a caller can set its own width/height (FR-KBM-01's 520px). */
  panelClassName?: string;
  children: ReactNode;
}

/** Everything focusable, in DOM order. `:not([disabled])` matters — a disabled control is a
 *  dead stop, and a trap that lands on one strands the user. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({ title, onClose, panelClassName, children }: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;

    // Captured before the first focus move, and restored on unmount. Without this, dismissing
    // the dialog drops focus to <body> and a keyboard user restarts from the top of the page —
    // which is why NFR-A11Y-04 names restoring focus and not just trapping it.
    const previous = document.activeElement;

    const focusable = () => [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)];
    // The panel itself is the fallback target, so a dialog whose body is pure text still
    // receives focus rather than leaving it behind on the element that opened it.
    (focusable()[0] ?? panel).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;

      const items = focusable();
      if (items.length === 0) {
        // Nothing to move to; keep focus on the panel rather than letting it escape.
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      // Wrap at both ends. The `!panel.contains(active)` case is what catches focus that was
      // moved out from underneath us — a browser autofill popup, or a caller focusing
      // something itself — rather than assuming focus is always on one of `items`.
      if (event.shiftKey && (active === first || !panel.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [onClose]);

  return (
    // The overlay is not a button and takes no keyboard handler: Escape already closes the
    // dialog from anywhere, so an overlay tab stop would be a duplicate control that also
    // breaks the trap. `jsx-a11y` would flag a click handler here if it were enabled; the
    // element is deliberately inert to assistive technology (`aria-hidden` is wrong — it is a
    // sibling of the panel, not an ancestor), and the click is a pointer convenience only.
    <div className={styles.overlay} onClick={onClose} data-testid="dialog-overlay">
      <div
        ref={panelRef}
        className={
          panelClassName === undefined ? styles.panel : `${styles.panel} ${panelClassName}`
        }
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        // Clicks inside must not reach the overlay's close handler (the prototype's `stopProp`).
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className={styles.title} id={titleId}>
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
