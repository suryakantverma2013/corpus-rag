/**
 * FR-SBR-03 (the list), FR-SBR-04 (selection) and FR-SBR-07 (rename / delete).
 *
 * FR-SBR-07 is a Rev 0.5 addition with **no prototype precedent** — the prototype has no
 * rename or delete affordance anywhere, and its rows are `<div onClick>` — so for this control
 * the spec is the acceptance baseline (the NFR-VIS-01 carve-out) and the row's *default,
 * non-hover* appearance is what must stay pixel-identical. That is the constraint driving the
 * structure below: the `⋯` button is always in the DOM and always in the tab order, and is
 * revealed by `opacity` alone, so it occupies no layout space and changes no pixel until the
 * row is hovered or focused.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import styles from './ConversationList.module.css';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import {
  displayTitle,
  formatConversationDate,
  formatMessageCount,
  type SidebarConversation,
} from './conversations';

/**
 * FR-SBR-07's confirmation copy. **All `TBD(§8.4)`** — the spec's remaining-TBD list names
 * "confirmation-dialog copy for conversation delete (FR-SBR-07)" explicitly, so these are
 * provisional and §9 gains no literal until the author settles them.
 */
const CONFIRM_TITLE = 'Delete conversation?'; // TBD(§8.4)
const CONFIRM_BODY = // TBD(§8.4)
  'This conversation and its messages will be permanently deleted. This cannot be undone.';
const CONFIRM_DELETE = 'Delete'; // TBD(§8.4)
const CONFIRM_CANCEL = 'Cancel'; // TBD(§8.4)

/** §9/Appendix A S1. */
const SECTION_LABEL = 'CONVERSATIONS';

/** Menu geometry. The width is duplicated in `ConversationList.module.css` because the
 *  clamp arithmetic needs it in JS; the CSS guard asserts the two agree. */
const MENU_WIDTH = 148;
const MENU_GAP = 4;
const MENU_VIEWPORT_MARGIN = 8;

export interface ConversationListProps {
  conversations: readonly SidebarConversation[];
  activeId: string | null;
  onSelect: (id: string) => void;
  /** FR-SBR-07 — `PATCH /conversations/{id}`. Never called with an unchanged or empty title. */
  onRename: (id: string, title: string) => void;
  /**
   * FR-SBR-07 — `DELETE /conversations/{id}`, after the confirmation below.
   *
   * Resolves to `null` when the chat is gone, or to the server's copy when it is not: R-54(5)
   * purges the LangGraph thread *before* committing, so a checkpointer outage answers `503`
   * with the conversation **intact** and the retry meaningful. The confirmation stays open and
   * shows that copy — a row that silently refuses to disappear reads as a defect, and the
   * sidebar has no other surface to say so on.
   */
  onDelete: (id: string) => Promise<string | null>;
}

export function ConversationList({
  conversations,
  activeId,
  onSelect,
  onRename,
  onDelete,
}: ConversationListProps) {
  const labelId = useId();
  const [menuFor, setMenuFor] = useState<string | null>(null);
  /**
   * Viewport coordinates for the open menu.
   *
   * The menu is `position: fixed`, not `absolute`, because the list it lives in is the
   * FR-SBR-03 scroll container (`overflow-y: auto`) and an absolutely-positioned popover would
   * be **clipped by it** — invisibly in jsdom, and worst for the bottom row, which is exactly
   * where a long history puts most of them. Fixed escapes ancestor overflow, and T-502's
   * containing-block guard is what guarantees no shell box turns it back into a clipped one.
   * This is the same mechanism the prototype uses for the FR-CIT-03 hover card.
   */
  const [menuPos, setMenuPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [pendingDelete, setPendingDelete] = useState<SidebarConversation | null>(null);
  /** The server's copy when a delete was refused — rendered in the dialog's own body. */
  const [deleteError, setDeleteError] = useState<string | null>(null);

  /** The `⋯` button that opened the current menu, so closing can return focus to it — without
   *  this, dismissing the menu with Escape drops focus to <body> (NFR-A11Y-04). */
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  const closeMenu = useCallback((restoreFocus: boolean) => {
    setMenuFor(null);
    if (restoreFocus) triggerRef.current?.focus();
    triggerRef.current = null;
  }, []);

  // An open menu must close when the user goes elsewhere. `pointerdown` rather than `click` so
  // it closes on press, matching how every native menu behaves.
  //
  // It must also close on scroll: the menu is positioned from a rect captured when it opened,
  // so scrolling the list would leave it floating beside the wrong row. Closing is the honest
  // answer — repositioning on every scroll frame would keep a popover glued to a row the user
  // is actively scrolling away from. `true` for the capture phase, since scroll does not bubble
  // from the list to the document.
  useEffect(() => {
    if (menuFor === null) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && triggerRef.current?.parentElement?.contains(target) === true) {
        return;
      }
      closeMenu(false);
    };
    const onScroll = () => closeMenu(false);
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [menuFor, closeMenu]);

  const startRename = (conversation: SidebarConversation) => {
    setDraft(displayTitle(conversation));
    setEditingId(conversation.id);
    closeMenu(false);
  };

  /**
   * Escape needs no flag to suppress this. It sets `editingId` to null, which unmounts the
   * input in the same flush — so React's `onBlur` no longer exists to fire, and a blur the
   * browser dispatches on the detached node reaches nothing. A `cancelledRef` guard was
   * written for this and shipped; a mutation deleting it changed no test, and inspecting the
   * path showed it could never run. Removed rather than tested (R-49's rule) — the behaviour
   * it was protecting is real and is still asserted, by the unmount.
   */
  const commitRename = (conversation: SidebarConversation) => {
    setEditingId(null);
    const title = draft.trim();
    // An empty title is a cancel, not a rename: `RenameConversationRequest.title` is required
    // and a blank one would leave a row the user cannot tell apart from any other blank row.
    // An unchanged title is a cancel too — no reason to spend a PATCH on it.
    if (title === '' || title === displayTitle(conversation)) return;
    onRename(conversation.id, title);
  };

  const onEditKeyDown = (event: React.KeyboardEvent<HTMLInputElement>, c: SidebarConversation) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitRename(c);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      // Stop the key here: an Escape meant for the rename must not also reach anything else
      // listening at the document (NFR-USE-03's mention menu, a Dialog above us).
      event.stopPropagation();
      setEditingId(null);
    }
  };

  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
    } else if (event.key === 'Tab') {
      // NFR-A11Y-04 (T-511). Tab is not part of the menu's contract, and this handler only
      // sees keys while focus is *inside* the menu — so letting Tab through walked focus out
      // of the last item and left the menu open with no way to dismiss it from the keyboard:
      // Escape was then delivered to whatever the browser had focused instead, and this
      // handler never ran again. Measured live: focus on an unrelated conversation row, menu
      // still open, Escape inert. (The user menu had the same escape but survived it, because
      // its listener is on `document` — the two menus were built differently, which is exactly
      // the kind of gap that only shows up in an end-to-end sweep.)
      event.preventDefault();
      closeMenu(true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      items[(index + 1) % items.length]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length]?.focus();
    }
  };

  return (
    <>
      <h2 className={styles.sectionLabel} id={labelId}>
        {SECTION_LABEL}
      </h2>

      <ul className={styles.list} aria-labelledby={labelId}>
        {conversations.map((conversation) => {
          const isActive = conversation.id === activeId;
          const isEditing = conversation.id === editingId;
          const title = displayTitle(conversation);

          return (
            <li
              key={conversation.id}
              className={isActive ? `${styles.row} ${styles.rowActive}` : styles.row}
            >
              {isEditing ? (
                <input
                  className={styles.titleInput}
                  // A visually hidden <label> is impossible per row without an id dance; the
                  // control is transient and its purpose is stated by its own value, so an
                  // explicit accessible name naming the conversation is clearer than a label.
                  aria-label={`Rename ${title}`}
                  value={draft}
                  ref={(node) => node?.select()}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => onEditKeyDown(event, conversation)}
                  onBlur={() => commitRename(conversation)}
                />
              ) : (
                <>
                  <button
                    type="button"
                    className={styles.rowButton}
                    // The row is a <button>, not a <div onClick> — NFR-A11Y-03, and the reason
                    // the whole list departs from the prototype's markup.
                    aria-current={isActive ? 'true' : undefined}
                    onClick={() => onSelect(conversation.id)}
                  >
                    <span className={styles.title}>{title}</span>
                    <span className={styles.meta}>
                      <span className="mono">{formatConversationDate(conversation)}</span>
                      <span>{formatMessageCount(conversation.message_count)}</span>
                    </span>
                  </button>

                  <button
                    type="button"
                    className={styles.menuButton}
                    aria-haspopup="menu"
                    aria-expanded={menuFor === conversation.id}
                    // Names the row it acts on: twenty buttons all called "More" are
                    // indistinguishable in a screen reader's control list.
                    aria-label={`Actions for ${title}`}
                    onClick={(event) => {
                      if (menuFor === conversation.id) {
                        closeMenu(false);
                        return;
                      }
                      const rect = event.currentTarget.getBoundingClientRect();
                      triggerRef.current = event.currentTarget;
                      // Right-aligned to the trigger, then clamped to the viewport — the
                      // FR-CIT-03 hover card's own rule (`Math.min(left, innerWidth - 350)`).
                      setMenuPos({
                        left: Math.max(
                          MENU_VIEWPORT_MARGIN,
                          Math.min(
                            rect.right - MENU_WIDTH,
                            window.innerWidth - MENU_WIDTH - MENU_VIEWPORT_MARGIN,
                          ),
                        ),
                        top: rect.bottom + MENU_GAP,
                      });
                      setMenuFor(conversation.id);
                    }}
                  >
                    ⋯
                  </button>

                  {menuFor === conversation.id && (
                    <div
                      className={`${styles.menu} animate-fade-up`}
                      style={{ left: `${menuPos.left}px`, top: `${menuPos.top}px` }}
                      role="menu"
                      aria-label={`Actions for ${title}`}
                      // -1, so the container is focusable without becoming a tab stop. The
                      // menu carries an interactive role and a key handler, and a focusable
                      // container is what the ARIA menu pattern expects of that pair; it also
                      // gives focus somewhere to land if an item is removed while the menu is
                      // open. `jsx-a11y/interactive-supports-focus` flags the absence.
                      tabIndex={-1}
                      onKeyDown={onMenuKeyDown}
                    >
                      <button
                        type="button"
                        role="menuitem"
                        className={styles.menuItem}
                        ref={(node) => node?.focus()}
                        onClick={() => startRename(conversation)}
                      >
                        Rename
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        className={styles.menuItem}
                        onClick={() => {
                          setPendingDelete(conversation);
                          // Not `closeMenu(true)`: the Dialog captures the focused element on
                          // mount to restore later, and refocusing the trigger first is what
                          // makes focus land back on the row's ⋯ after the dialog closes.
                          closeMenu(true);
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  )}
                </>
              )}
            </li>
          );
        })}
      </ul>

      {pendingDelete !== null && (
        <ConfirmDialog
          title={CONFIRM_TITLE}
          body={deleteError ?? CONFIRM_BODY}
          confirmLabel={CONFIRM_DELETE}
          cancelLabel={CONFIRM_CANCEL}
          onCancel={() => {
            setPendingDelete(null);
            setDeleteError(null);
          }}
          onConfirm={() => {
            const target = pendingDelete;
            void onDelete(target.id).then((failure) => {
              // Closed only on success. The dialog is the one surface this flow has, so a
              // refusal that dismissed it would leave the row back in the list with no
              // explanation of why (R-54(5)).
              if (failure === null) {
                setPendingDelete(null);
                setDeleteError(null);
              } else {
                setDeleteError(failure);
              }
            });
          }}
        />
      )}
    </>
  );
}
