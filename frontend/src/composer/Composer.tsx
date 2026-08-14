/**
 * §4.4 — the composer: FR-CMP-01..06, NFR-USE-03, and FR-STA-04's pre-submission block.
 *
 * Presentational in the T-503 sense — every mutation is a prop and nothing here fetches;
 * `onSend` reaches `POST /conversations/{id}/messages` through the chat store (T-513), and
 * `usage` is `ContextWindowResponse`, `null` until it is read. The composer's *own* state (the
 * draft, whether the menu is open, which row is virtually focused) lives here rather than in
 * `App`: FR-CST-01 lists `composer` and `mentionOpen` as client state, and R-58(5)'s rule is
 * about not threading state through the shell, not about hoisting every input.
 *
 * **The FR-CMP-05 matrix is the part to read carefully**, and it is implemented as the spec
 * words it rather than as it might be guessed: the menu's open state is *assigned* from the
 * trailing-`@` test on every keystroke, never OR-ed with what it was. That single assignment
 * is what makes a button-opened menu close on the next keystroke — recorded in the Rev 0.5.1
 * legacy delta as intended, and the one behaviour a "smarter" implementation would break.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

import styles from './Composer.module.css';
import { MentionMenu } from './MentionMenu';
import {
  MENTION_LIST_ID,
  hasMentionTrigger,
  insertMention,
  mentionOptionId,
  nextActiveIndex,
  outgoingText,
  sendBlock,
} from './mentions';
import type { MentionDocument } from './mentions';
import { projectSubmission } from '../tokens';

/** §9's literal table, verbatim. */
const PLACEHOLDER = 'Ask a question — @ to cite a document';
/** FR-CMP-02's native tooltips — the prototype's `title` attributes, verbatim. */
const AT_TITLE = 'Reference a document';
const ADD_TITLE = 'Add documents';
const SEND_LABEL = 'Send';

/**
 * NFR-A11Y-03 requires the input to have a label; FR-CMP-01's design has none, so it is visually
 * hidden. Not the placeholder: a placeholder disappears on the first keystroke and is not an
 * accessible name.
 */
const INPUT_LABEL = 'Ask a question'; // TBD(§8.4)

export interface ComposerProps {
  /** FR-CMP-04's rows — the FR-ORC-06 scope: global documents plus this chat's attachments. */
  documents: readonly MentionDocument[];
  /** FR-CMP-03 — a reply is in flight, so a send is ignored. Same flag as FR-MSG-05's dots. */
  pending: boolean;
  /**
   * FR-STA-04 / R-51(3) — all three numbers are the server's, off `ContextWindowResponse`.
   *
   * `reserve` is `CONTEXT_ANSWER_RESERVE_TOKENS`, which the projection needs and which T-513
   * added to that response for exactly this: without it the browser cannot reproduce
   * `check_submission`, and would start accepting what the server refuses the moment an
   * operator raised `LLM_MAX_OUTPUT_TOKENS`. Passed in rather than assumed so this component
   * applies the server's rule and never its own.
   *
   * `null` while the meter is unread — see `overBudget` below for why that is permissive.
   */
  usage: { used: number; limit: number; reserve: number } | null;
  /** FR-CMP-06's count: global documents + the active chat's attachments (same as FR-SBR-05). */
  documentCount: number;
  onSend: (text: string) => void;
  /** FR-CMP-02 — the `+` button opens the §4.7 KB modal (T-508). */
  onOpenKnowledgeBase: () => void;
  /**
   * FR-CST-01's `docsOpen`, for FR-CMP-05's "close when the KB modal opens".
   *
   * The `+` button closes the menu itself, but FR-KBM-01 says **either** entry point closes it —
   * and FR-SBR-05's sidebar button is the other one, which this component never hears about.
   * A boolean rather than lifting `mentionOpen` into `App`: FR-CST-01 enumerates state *content*,
   * not topology (R-58(5)), and one prop is a smaller change than moving the FR-CMP-05 matrix
   * out of the component whose source guard pins it.
   */
  knowledgeBaseOpen?: boolean;
}

export function Composer({
  documents,
  pending,
  usage,
  documentCount,
  onSend,
  onOpenKnowledgeBase,
  knowledgeBaseOpen = false,
}: ComposerProps) {
  const [value, setValue] = useState('');
  const [mentionOpen, setMentionOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  // FR-STA-04, run on the draft as it is typed — the same rule as `check_submission`, which is
  // what R-51(2) requires and `tokens.ts` explains. The server stays authoritative; this only
  // stops a request that would certainly be refused (R-51(6)).
  // Permissive while the meter is unknown — the one round trip after activating a chat. R-44's
  // standing rule: a control that refuses legitimate use is worse than no control, and the
  // server refuses anyway (R-51(5)), so the cost of being wrong here is one recoverable `409`
  // against a composer that would otherwise be dead on arrival in every new chat.
  const overBudget = usage !== null && !projectSubmission(value, usage).allowed;
  const blocked = sendBlock(value, { pending, overBudget });

  const closeMenu = useCallback(() => {
    setMentionOpen(false);
    setActiveIndex(-1);
  }, []);

  // FR-KBM-01: opening the modal from EITHER entry point closes the menu. The `+` button below
  // does it directly; this covers FR-SBR-05's sidebar button, which this component cannot see.
  // Not folded into the FR-CMP-05 keystroke assignment — that line is `setMentionOpen(hasTrigger)`
  // exactly, and a second condition inside it is what would break the matrix a guard pins.
  useEffect(() => {
    if (knowledgeBaseOpen) closeMenu();
  }, [knowledgeBaseOpen, closeMenu]);

  const send = useCallback(() => {
    // FR-CMP-03: every guard is checked here rather than only on the button, because Enter is
    // the other trigger and a disabled button does not disable a key.
    if (sendBlock(value, { pending, overBudget }) !== null) return;
    onSend(outgoingText(value));
    setValue('');
    closeMenu();
  }, [closeMenu, onSend, overBudget, pending, value]);

  const choose = useCallback(
    (document: MentionDocument) => {
      setValue((current) => insertMention(current, document.name));
      closeMenu();
      // The caret belongs back in the composer after an insert — the row prevented the focus
      // change on mousedown, so this only matters for the keyboard path, where focus never left.
      inputRef.current?.focus();
    },
    [closeMenu],
  );

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    // NFR-USE-03's two bindings plus NFR-A11Y-03's listbox contract. Escape first: it closes
    // the menu whether or not a row is active, and must not also send.
    if (event.key === 'Escape') {
      if (mentionOpen) {
        event.preventDefault();
        closeMenu();
      }
      return;
    }

    if (mentionOpen && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      setActiveIndex((current) =>
        nextActiveIndex(current, event.key === 'ArrowDown' ? 1 : -1, documents.length),
      );
      return;
    }

    if (event.key === 'Enter') {
      // The one genuine conflict between two requirements: FR-CMP-03 says Enter sends, the
      // listbox contract says Enter selects the active option. Resolved by *which* is active —
      // the menu opens with `activeIndex === -1`, so Enter still sends unless the user has
      // deliberately arrowed into the list. Neither requirement loses its default case.
      const active = mentionOpen ? documents[activeIndex] : undefined;
      if (active !== undefined) {
        event.preventDefault();
        choose(active);
        return;
      }
      event.preventDefault();
      send();
    }
  };

  return (
    <div className={styles.composer}>
      {mentionOpen && (
        <MentionMenu
          documents={documents}
          activeIndex={activeIndex}
          onSelect={choose}
          onHover={setActiveIndex}
        />
      )}

      <div className={styles.bar}>
        <button
          type="button"
          className={`${styles.iconButton} ${styles.at}`}
          title={AT_TITLE}
          // The `title` is a tooltip, not an accessible name in every AT; FR-CMP-02's string is
          // the right name for both, so it is given twice deliberately.
          aria-label={AT_TITLE}
          aria-expanded={mentionOpen}
          aria-controls={mentionOpen ? MENTION_LIST_ID : undefined}
          onClick={() => {
            // FR-CMP-05: the `@` button *toggles*. It does not insert an `@` — the Rev 0.5.1
            // legacy delta records that as the docx behaviour Corpus deliberately dropped.
            setMentionOpen((open) => !open);
            setActiveIndex(-1);
            inputRef.current?.focus();
          }}
        >
          @
        </button>

        <button
          type="button"
          className={`${styles.iconButton} ${styles.add}`}
          title={ADD_TITLE}
          aria-label={ADD_TITLE}
          onClick={() => {
            // FR-CMP-05 — "close … when the KB modal opens".
            closeMenu();
            onOpenKnowledgeBase();
          }}
        >
          +
        </button>

        <label className="visually-hidden" htmlFor={inputId}>
          {INPUT_LABEL}
        </label>
        <input
          id={inputId}
          ref={inputRef}
          className={styles.input}
          type="text"
          placeholder={PLACEHOLDER}
          value={value}
          // NFR-A11Y-03's combobox half. `aria-autocomplete="list"` rather than `"both"`: the
          // menu never writes into the field as you type — an insert happens only on selection.
          role="combobox"
          aria-expanded={mentionOpen}
          aria-controls={mentionOpen ? MENTION_LIST_ID : undefined}
          aria-autocomplete="list"
          aria-activedescendant={
            mentionOpen && activeIndex >= 0 ? mentionOptionId(activeIndex) : undefined
          }
          autoComplete="off"
          onChange={(event) => {
            const next = event.target.value;
            setValue(next);
            // FR-CMP-05's re-evaluation, as an assignment. See this file's header.
            setMentionOpen(hasMentionTrigger(next));
            setActiveIndex(-1);
          }}
          onKeyDown={onKeyDown}
        />

        <button
          type="button"
          className={styles.send}
          // FR-CMP-03's guards, surfaced. `blocked === 'pending'` disables it too: FR-MSG-08
          // disables its bar while generating for the same reason, and an enabled button that
          // ignores the click is the affordance lie NFR-A11Y-04 exists to prevent.
          disabled={blocked !== null}
          onClick={send}
        >
          {SEND_LABEL}
        </button>
      </div>

      {/* FR-CMP-06. `aria-live` is deliberately absent: the count changes only when documents
          are added or removed, which the user just did, and NFR-A11Y-05 covers state that
          changes *without* user action. */}
      <div className={`${styles.footer} mono`}>
        {`Responses grounded in ${documentCount} documents · Enter to send`}
      </div>
    </div>
  );
}
