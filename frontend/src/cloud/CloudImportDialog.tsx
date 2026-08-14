/**
 * FR-KBM-10's selection surface — the searchable flat list, rendered in the Corpus design
 * language rather than as a provider widget.
 *
 * **A nested `ui/Dialog`, and the second real user of T-508's dialog stack.** It sits over the
 * FR-KBM-01 modal rather than replacing its contents, because the user reached it from that
 * modal's footer and returns there: the imported file appears in the list behind this panel, so
 * dismantling that list to show the picker would hide the outcome of the action.
 *
 * **Flat, with no folder tree, by requirement** (FR-KBM-10, R-63(4)) — only four formats are
 * ingestible, so a tree would mostly display files the user cannot pick. The filter runs at the
 * provider, not here; a client-side filter would page through the user's whole Drive to render a
 * handful of rows.
 *
 * **NFR-A11Y-03's listbox**, on the FR-CMP-04 mention-menu precedent it names: DOM focus stays in
 * the search input and `aria-activedescendant` carries a virtual focus through the rows, which
 * are `<button role="option" tabIndex={-1}>` so the pointer path has real controls while tabbing
 * past fifty files is not the keyboard trap NFR-A11Y-04 forbids.
 */
import { useEffect, useRef, useState } from 'react';

import type { DriveFile, UploadScope } from '../api';
import type { Outcome } from '../kb/mutations';
import { documentTypeBadge } from '../stats/stats';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { Dialog } from '../ui/Dialog';
import {
  CLOUD_HEADING_ID,
  CLOUD_LIST_ID,
  CLOUD_PROVIDER_NAME,
  cloudOptionId,
  driveMeta,
  nextCloudIndex,
} from './cloud';
import type { CloudFilesStore } from './useCloudFiles';
import type { CloudLinkStore } from './useCloudLink';
import styles from './CloudImportDialog.module.css';

/* Every string below is TBD(§8.4) — §9 carries no literal for this surface and the prototype has
   no markup for it at all, so this component invents no acceptance-critical value. The two
   FR-AUT-11 `409`s are deliberately absent: their copy is the server's and is rendered verbatim
   (R-57(4)), which is what keeps the two causes distinguishable without this file matching on a
   code to pick a sentence. */
const TITLE = `Import from ${CLOUD_PROVIDER_NAME}`;
const HEADING = 'YOUR DRIVE FILES';
const SEARCH_LABEL = 'Search your Drive';
const SEARCH_PLACEHOLDER = 'Search files…';
const LOADING = 'Loading…';
const EMPTY = 'No importable files in your Drive.';
const EMPTY_SEARCH = 'No files match that search.';
const LOAD_MORE = 'Load more';
const CLOSE = 'Close';
const UNLINK = 'Unlink';
const RELINK = 'Reconnect account';
const DISMISS = 'Dismiss';
const IMPORT = 'Import';
const IMPORTING = 'Importing…';
const IMPORTED = 'Imported';
const PAUSED = 'Importing is paused while a response is being generated.';
const FORMATS = 'PDF · DOCX · CSV · MD';

const UNLINK_TITLE = `Disconnect ${CLOUD_PROVIDER_NAME}?`;
/** FR-AUT-11 is explicit that unlinking does not affect documents already imported — they are
 *  copies (FR-KBM-10) — and saying so is the whole point of confirming: without it a user with
 *  fifty imported documents cannot tell this apart from deleting them. */
const UNLINK_BODY =
  'Corpus will no longer be able to list or import your Drive files. Documents you have ' +
  'already imported are copies and are not affected — they stay in your knowledge base.';
const UNLINK_CONFIRM = 'Disconnect';
const UNLINK_CANCEL = 'Cancel';

/** Which rows this session has already sent. Keyed by Drive file id — the server's identity for
 *  the file, and the only stable one here (FR-KBM-08 rejects filename as identity). */
type ImportState = 'importing' | 'imported';

export interface CloudImportDialogProps {
  files: CloudFilesStore;
  link: CloudLinkStore;
  /** The scope the FR-KBM-05 control is set to. Passed rather than owned: it is the same "where
   *  does this document go" decision, and a second control here could disagree with the one the
   *  user set on the drop zone a moment ago. */
  scope: UploadScope;
  /** R-71(1)'s in-flight signal, reaching this surface through the same store the four document
   *  verbs read it from. */
  paused: boolean;
  onImport: (file: DriveFile, scope: UploadScope) => Promise<Outcome>;
  onClose: () => void;
}

export function CloudImportDialog({
  files,
  link,
  scope,
  paused,
  onImport,
  onClose,
}: CloudImportDialogProps) {
  const [activeIndex, setActiveIndex] = useState(-1);
  const [states, setStates] = useState<Readonly<Record<string, ImportState>>>({});
  const [confirmUnlink, setConfirmUnlink] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const rows = files.files;

  // The listbox contract above puts DOM focus in the search input and moves a *virtual* focus
  // with `aria-activedescendant` — so the arrow keys are bound to the input, and with focus
  // anywhere else this surface has no keyboard navigation at all. `Dialog` focuses the first
  // focusable control on mount, which here is the header ✕, so the picker opened with its own
  // navigation inert until the user tabbed to the field: two Tabs before ArrowDown did anything,
  // and a screen-reader user was announced a Close button rather than the search box on a
  // surface whose entire purpose is finding a file. (T-514 measured it; `searchRef` had been
  // declared and wired since T-512 and never read, which is the intent it was missing.)
  //
  // This runs *after* Dialog's focus move — React commits child effects before parent ones — and
  // is the case Dialog's Tab handler already anticipates with `!panel.contains(active)`. Focus
  // restore is unaffected: Dialog captures the opener before its own move, so it is captured
  // before this one too.
  useEffect(() => {
    searchRef.current?.focus();
  }, []);

  // A shrinking list must not leave the virtual focus pointing past its end: `aria-activedescendant`
  // would name an element that no longer exists, and Enter would import nothing while looking as
  // though it should. Reset rather than clamp — after a new search, row 0 is not the row the user
  // had chosen, so inheriting the position would be a guess.
  useEffect(() => {
    setActiveIndex(-1);
  }, [rows]);

  const runImport = async (file: DriveFile) => {
    if (paused || states[file.file_id] !== undefined) return;
    setStates((current) => ({ ...current, [file.file_id]: 'importing' }));
    const outcome = await onImport(file, scope);
    setStates((current) => {
      const next = { ...current };
      // `duplicate` counts as imported: FR-KBM-08 means the document is in the knowledge base,
      // which is what the user asked for. Everything else releases the row so it can be retried.
      if (outcome.kind === 'accepted' || outcome.kind === 'duplicate') {
        next[file.file_id] = 'imported';
      } else {
        delete next[file.file_id];
      }
      return next;
    });
    // A `409` naming the link is the picker's own state, and re-reading the status is what turns
    // a revoked grant into the Re-link affordance rather than a notice the user cannot act on.
    if (outcome.kind === 'link-required') link.refresh();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (rows.length === 0) return;
      event.preventDefault();
      setActiveIndex((current) =>
        nextCloudIndex(current, event.key === 'ArrowDown' ? 1 : -1, rows.length),
      );
      return;
    }
    if (event.key === 'Enter' && activeIndex >= 0) {
      // Only with a deliberately-arrowed-to row. The composer's FR-CMP-03 conflict one surface
      // over: with nothing active, Enter must not import whatever happens to be first.
      event.preventDefault();
      const file = rows[activeIndex];
      if (file !== undefined) void runImport(file);
    }
  };

  return (
    <Dialog
      title={TITLE}
      onClose={onClose}
      panelClassName={styles.panel}
      headerAction={
        <button
          type="button"
          className={styles.close}
          aria-label="Close cloud import"
          onClick={onClose}
        >
          ✕
        </button>
      }
    >
      {/* FR-AUT-11's account line and its unlink affordance. The address is what makes "which
          Google account?" answerable without leaving Corpus, and the unlink sits beside it for
          the same reason — it is the one control that acts on the account rather than on a file. */}
      {link.linked && (
        <p className={styles.account}>
          <span className={`${styles.address} mono`}>{link.account ?? CLOUD_PROVIDER_NAME}</span>
          <button type="button" className={styles.unlink} onClick={() => setConfirmUnlink(true)}>
            {UNLINK}
          </button>
        </p>
      )}

      <div className={styles.notices}>
        {paused && (
          <p className={styles.paused} role="status">
            {PAUSED}
          </p>
        )}
        {/* Both FR-AUT-11 `409`s land here, with the SERVER's copy — which is the whole reason
            T-214 kept `ACCOUNT_NOT_LINKED` and `CLOUD_ACCESS_REVOKED` distinct (§8.53(5)). One
            action behind two sentences: a link that never existed and a grant withdrawn at
            Google both end at the same consent screen. */}
        {files.linkRequired !== null && (
          <p className={styles.notice} role="status">
            <span>{files.linkRequired.detail}</span>
            <button type="button" className={styles.relink} onClick={() => void link.beginLink()}>
              {RELINK}
            </button>
          </p>
        )}
        {files.notice !== null && (
          <p className={styles.notice} role="status">
            <span>{files.notice}</span>
            <button type="button" className={styles.dismiss} onClick={files.dismissNotice}>
              {DISMISS}
            </button>
          </p>
        )}
        {link.notice !== null && (
          <p className={styles.notice} role="status">
            <span>{link.notice}</span>
            <button type="button" className={styles.dismiss} onClick={link.dismissNotice}>
              {DISMISS}
            </button>
          </p>
        )}
      </div>

      <label className="visually-hidden" htmlFor="cloud-search">
        {SEARCH_LABEL}
      </label>
      <input
        ref={searchRef}
        id="cloud-search"
        className={styles.search}
        type="text"
        placeholder={SEARCH_PLACEHOLDER}
        value={files.search}
        autoComplete="off"
        role="combobox"
        aria-expanded={rows.length > 0}
        aria-controls={rows.length > 0 ? CLOUD_LIST_ID : undefined}
        aria-autocomplete="list"
        aria-activedescendant={activeIndex >= 0 ? cloudOptionId(activeIndex) : undefined}
        onChange={(event) => files.setSearch(event.target.value)}
        onKeyDown={onKeyDown}
      />

      <div className={styles.heading} id={CLOUD_HEADING_ID}>
        <span>{HEADING}</span>
        {/* FR-KBM-10 filters to the four FR-ING-02 formats at the provider, so a file the user
            expects to see may simply not be ingestible. Naming them is how that absence reads as
            a rule rather than as a missing file. */}
        <span className={`${styles.formats} mono`}>{FORMATS}</span>
      </div>

      {files.loading ? (
        <p className={styles.empty}>{LOADING}</p>
      ) : rows.length === 0 ? (
        files.linkRequired !== null ? null : (
          <p className={styles.empty}>{files.search.length > 0 ? EMPTY_SEARCH : EMPTY}</p>
        )
      ) : (
        <ul
          className={styles.list}
          id={CLOUD_LIST_ID}
          role="listbox"
          aria-labelledby={CLOUD_HEADING_ID}
        >
          {rows.map((file, index) => (
            <FileRow
              key={file.file_id}
              file={file}
              active={index === activeIndex}
              optionId={cloudOptionId(index)}
              state={states[file.file_id]}
              paused={paused}
              onHover={() => setActiveIndex(index)}
              onSelect={() => void runImport(file)}
            />
          ))}
        </ul>
      )}

      {files.canLoadMore && (
        <button type="button" className={styles.more} onClick={files.loadMore}>
          {LOAD_MORE}
        </button>
      )}
      {files.loadingMore && <p className={styles.empty}>{LOADING}</p>}

      <div className={styles.footer}>
        <button type="button" className={styles.secondary} onClick={onClose}>
          {CLOSE}
        </button>
      </div>

      {confirmUnlink && (
        <ConfirmDialog
          title={UNLINK_TITLE}
          body={UNLINK_BODY}
          confirmLabel={UNLINK_CONFIRM}
          cancelLabel={UNLINK_CANCEL}
          onCancel={() => setConfirmUnlink(false)}
          onConfirm={() => {
            setConfirmUnlink(false);
            // Closing on success: with no link there is nothing left for this surface to list,
            // and leaving it open on an empty list would say "no files" where the truth is "no
            // account". A failure keeps it open, with `link.notice` explaining why.
            void link.unlink().then((ok) => {
              if (ok) onClose();
            });
          }}
        />
      )}
    </Dialog>
  );
}

interface FileRowProps {
  file: DriveFile;
  active: boolean;
  optionId: string;
  state: ImportState | undefined;
  paused: boolean;
  onHover: () => void;
  onSelect: () => void;
}

function FileRow({ file, active, optionId, state, paused, onHover, onSelect }: FileRowProps) {
  const meta = driveMeta(file);
  const label = state === 'imported' ? IMPORTED : state === 'importing' ? IMPORTING : IMPORT;
  return (
    <li role="presentation">
      <button
        type="button"
        id={optionId}
        role="option"
        aria-selected={active}
        data-active={active}
        tabIndex={-1}
        className={styles.row}
        // The row is one of many, so the action word alone would repeat down the list —
        // DocumentRow's rule, and the reason a screen-reader element list is navigable here.
        aria-label={`${label} ${file.name}`}
        // `aria-disabled`, never the `disabled` attribute: a disabled `<button>` is removed from
        // the pointer and accessibility interaction model, so an imported row would stop being
        // hoverable and stop being reachable by the arrow keys that move `aria-activedescendant`
        // — it would vanish from the list a screen-reader user is navigating. The guard that
        // actually refuses the action lives in `runImport`, which is where both callers meet.
        aria-disabled={paused || state !== undefined}
        onMouseEnter={onHover}
        onClick={onSelect}
      >
        <span className={`${styles.badge} mono`}>{documentTypeBadge(file.name)}</span>
        <span className={styles.body}>
          <span className={styles.name}>{file.name}</span>
          {meta.length > 0 && <span className={`${styles.meta} mono`}>{meta}</span>}
        </span>
        <span className={state === 'imported' ? `${styles.state} ${styles.done}` : styles.state}>
          {label}
        </span>
      </button>
    </li>
  );
}
