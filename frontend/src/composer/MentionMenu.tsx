/**
 * FR-CMP-04 — the @-mention popover.
 *
 * **The one place NFR-A11Y-03 sanctions ARIA over a native element**, and it says so by name:
 * *"where a native element cannot express the pattern (the FR-CMP-04 mention menu), the
 * corresponding ARIA pattern shall be used with its full keyboard contract"*. A `<datalist>`
 * cannot carry the badge/name/meta row, and a `<select>` is not a text input's companion — so
 * this is a listbox owned by the composer's combobox, and the keyboard contract lives in
 * `Composer`, which owns the input that keeps focus throughout.
 *
 * Rows are `<button>`s inside an `<ul>`: `aria-activedescendant` moves a *virtual* focus while
 * DOM focus stays in the text input, but the rows must still be real controls for the pointer
 * path and for NFR-A11Y-04. They are deliberately **not** in the tab order (`tabIndex={-1}`) —
 * tabbing through five documents to leave the composer is the keyboard trap that requirement
 * forbids, and the arrow keys are the specified way in.
 */
import styles from './MentionMenu.module.css';
import { MENTION_HEADING_ID, MENTION_LIST_ID, mentionMeta, mentionOptionId } from './mentions';
import type { MentionDocument } from './mentions';

/** §9's literal, verbatim. */
const HEADING = 'REFERENCE A DOCUMENT';

/** Not in the prototype (its document list is never empty) and not in §9. `TBD(§8.4)`. */
const EMPTY = 'No documents yet'; // TBD(§8.4)

export interface MentionMenuProps {
  documents: readonly MentionDocument[];
  /** The virtual-focus row, or `-1` for none — see `nextActiveIndex` for why it starts there. */
  activeIndex: number;
  onSelect: (document: MentionDocument) => void;
  /** Hovering moves the virtual focus, so the pointer and keyboard cannot disagree on "active". */
  onHover: (index: number) => void;
}

export function MentionMenu({ documents, activeIndex, onSelect, onHover }: MentionMenuProps) {
  return (
    // `animate-fade-up` is the global utility, never a local `animation:` — R-69's predecessor
    // finding: CSS Modules rewrites `animation-name` even for a keyframe the module does not
    // declare, so `animation: fadeUp .15s` inside this file would compile to a hashed name
    // matching no @keyframes and silently never run. The prototype's duration is .15s, which is
    // `--motion-fast`, passed through the custom property so NFR-A11Y-01's zeroing still reaches
    // it. A repo-wide guard fails if this file names a keyframe.
    <div
      className={`${styles.menu} animate-fade-up`}
      style={{ '--fade-up-duration': 'var(--motion-fast)' } as React.CSSProperties}
    >
      <div className={styles.heading} id={MENTION_HEADING_ID}>
        {HEADING}
      </div>
      {documents.length === 0 ? (
        <div className={styles.empty}>{EMPTY}</div>
      ) : (
        <ul
          className={styles.list}
          id={MENTION_LIST_ID}
          role="listbox"
          aria-labelledby={MENTION_HEADING_ID}
        >
          {documents.map((document, index) => (
            <li key={document.id} role="presentation">
              <button
                type="button"
                id={mentionOptionId(index)}
                role="option"
                aria-selected={index === activeIndex}
                data-active={index === activeIndex}
                tabIndex={-1}
                className={styles.row}
                // `onMouseDown` with `preventDefault`, not `onClick`: a click steals focus from
                // the input first, and FR-CMP-05 puts the caret back in the composer after an
                // insert. Preventing the default keeps focus where it already is, so no
                // refocus dance is needed and no blur handler can close the menu underneath us.
                onMouseDown={(event) => {
                  event.preventDefault();
                  onSelect(document);
                }}
                onMouseEnter={() => onHover(index)}
              >
                <span className={`${styles.badge} mono`}>{document.type}</span>
                <span className={styles.name}>{document.name}</span>
                <span className={styles.meta}>{mentionMeta(document)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
