/**
 * FR-KBM-05's drop zone, and R-71(3)'s upload-scope control.
 *
 * **Why the scope control exists.** The prototype draws one unscoped drop zone and
 * `POST /api/v1/documents` defaults to `scope=global`, so as drawn nothing in the GUI could ever
 * create a chat attachment — FR-KBM-03's `THIS CHAT · {N} ATTACHMENTS` section would be
 * permanently empty and `scope=chat` would have no caller anywhere in the product. R-71(3) adds
 * the control in FR-HDR-03's two-segment language, so no new component vocabulary appears.
 *
 * **Why the zone is a `<button>`.** NFR-A11Y-03 prohibits click handlers on `<div>`, and
 * drag-and-drop is pointer-only by nature — the board makes a keyboard-operable file picker
 * normative. One native control satisfies both: it is focusable and Enter/Space-activated for
 * free, and the drag handlers ride on the same element.
 */
import { useRef, useState } from 'react';

import type { UploadScope } from '../api';
import { ACCEPT } from './DocumentRow';
import styles from './DropZone.module.css';

/** §9's literals, verbatim. */
const PROMPT = 'Drag & drop files here, or click to browse';
const CAPTION = 'PDF · DOCX · CSV · MD — max 300 MB';
/** TBD(§8.4) — R-71(3). The control is spec-authored, so §9 carries no string for it yet. */
const SCOPE_LABEL = 'Add to';
const SCOPE_SEGMENTS: readonly { value: UploadScope; label: string }[] = [
  { value: 'global', label: 'Global' },
  { value: 'chat', label: 'This chat' },
];

export interface DropZoneProps {
  scope: UploadScope;
  onScopeChange: (scope: UploadScope) => void;
  /** `scope=chat` needs a server conversation to attach to; without one the segment is inert. */
  chatAvailable: boolean;
  disabled: boolean;
  onFiles: (files: readonly File[]) => void;
}

export function DropZone({
  scope,
  onScopeChange,
  chatAvailable,
  disabled,
  onFiles,
}: DropZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const accept = (list: FileList | null) => {
    const files = list === null ? [] : [...list];
    if (files.length > 0) onFiles(files);
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.scope}>
        <span className={styles.scopeLabel} id="kb-scope-label">
          {SCOPE_LABEL}
        </span>
        {/* Each segment SETS its scope, so the active one is a no-op — T-504's FR-HDR-03 ruling,
            for the same reason: a button labelled "Global" that moved you away from global would
            be mislabelled. `aria-pressed` is the non-colour carrier NFR-A11Y-06 requires. */}
        <div className={styles.segments} role="group" aria-labelledby="kb-scope-label">
          {SCOPE_SEGMENTS.map((segment) => (
            <button
              key={segment.value}
              type="button"
              className={
                scope === segment.value ? `${styles.segment} ${styles.on}` : styles.segment
              }
              aria-pressed={scope === segment.value}
              disabled={segment.value === 'chat' && !chatAvailable}
              onClick={() => onScopeChange(segment.value)}
            >
              {segment.label}
            </button>
          ))}
        </div>
      </div>

      <button
        type="button"
        className={dragging ? `${styles.zone} ${styles.dragging}` : styles.zone}
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
        // `preventDefault` on dragover is what makes the element a drop target at all; without
        // it the browser navigates to the file and the page is replaced.
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!disabled) accept(event.dataTransfer.files);
        }}
      >
        {PROMPT}
        <span className={`${styles.caption} mono`}>{CAPTION}</span>
      </button>

      <input
        ref={inputRef}
        className="visually-hidden"
        type="file"
        multiple
        accept={ACCEPT}
        tabIndex={-1}
        aria-hidden="true"
        onChange={(event) => {
          accept(event.target.files);
          // Reset so re-picking the same file fires `change` again — otherwise a rejected upload
          // could not be retried with the identical file.
          event.target.value = '';
        }}
      />
    </div>
  );
}
