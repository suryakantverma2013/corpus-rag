/**
 * FR-MSG-04 — the assistant's answer, and FR-ERR-04's served-but-unstored variant.
 *
 * Transcribed from `RAG Chatbot.dc.html` lines 97–122. The content column's four parts are in the
 * order FR-MSG-04 fixes, each conditional: (1) body with inline chips, (2) the source line when
 * there are citations, (3) the DeepEval chip row when there are scores, (4) the FR-MSG-08 bar.
 *
 * **`DegradedAnswer` renders the server's copy and nothing else.** R-43(6) keeps the FR-ERR-04
 * per-class text on the server — `app/rag/errors.py`, every string a `# TBD(§8.4)` — precisely so
 * the client holds no second copy to drift from it, which is why nothing in this file names a
 * failure class or reads `error_code`. It is also rendered as **plain text, not Markdown**: this
 * is our own copy rather than document content, and `_snake_case_` inside an operator-facing
 * message must not silently become italics. And it carries no action bar, because R-54(3) keeps
 * these turns out of `messages` — there is no row to rate or regenerate.
 *
 * Note that an **abstained or injection-blocked turn is not degraded**: `_should_persist` excludes
 * only `error`, so those arrive as ordinary rows with ids and render through the normal path,
 * rateable and regenerable like any other answer.
 */
import { useMemo } from 'react';
import styles from './AiMessage.module.css';
import { CitationChip } from './CitationChip';
import { CitationFigures } from './CitationFigures';
import { EvalChips } from './EvalChips';
import { MessageActions } from './MessageActions';
import { renderMarkdown } from './markdown';
import {
  UNGROUNDED_ACTION_LABEL,
  UNGROUNDED_LABEL,
  citationsOf,
  sourceLine,
} from './messages';
import type { Feedback, Message } from '../api';

/** The brand mark, as the prototype writes it. `brandName`'s initial is deliberately *not*
 *  used: FR-SYS-04 makes the name configurable, but §9 and the README both fix the mark as the
 *  letter "C", and the FR-AUT-01 login card and FR-SBR-01 brand row already render it that way. */
const AI_AVATAR = 'C';

interface AvatarProps {
  className?: string;
}

export function AiAvatar({ className }: AvatarProps) {
  return (
    <div className={`${styles.avatar} ${className ?? ''}`.trim()} aria-hidden="true">
      {AI_AVATAR}
    </div>
  );
}

export interface AiMessageProps {
  message: Message;
  /** GUI affordance only for feedback (R-55(1)); a server concern for Regenerate (R-56(6)). */
  busy: boolean;
  onFeedback: (feedback: Feedback | null) => void;
  onRegenerate: () => void;
  /** FR-MSG-09 — the user asked this abstention for an answer from the model's own training. */
  onAnswerUngrounded?: () => void;
  /** That request is in flight for *this* message. */
  ungroundedBusy?: boolean;
}

export function AiMessage({
  message,
  busy,
  onFeedback,
  onRegenerate,
  onAnswerUngrounded,
  ungroundedBusy = false,
}: AiMessageProps) {
  const citations = useMemo(() => citationsOf(message.segs), [message.segs]);
  const source = sourceLine(citations);

  // Memoised on `segs` alone: a feedback toggle replaces the message object but not its
  // content, and re-parsing the answer to paint a thumb would remount every chip and close the
  // hover card the user is reading.
  const body = useMemo(
    () =>
      renderMarkdown(message.segs, {
        classes: {
          paragraph: styles.paragraph,
          heading: styles.heading,
          list: styles.list,
          listItem: styles.listItem,
          code: styles.code,
          pre: styles.pre,
          tableWrap: styles.tableWrap,
          table: styles.table,
          link: styles.link,
          alignLeft: styles.alignLeft,
          alignCenter: styles.alignCenter,
          alignRight: styles.alignRight,
        },
        // Injected rather than imported, so `markdown.ts` depends on no component. The index
        // addresses `flatten`'s citation list, which is `citationsOf` in the same order.
        renderCitation: (index) => <CitationChip segment={citations[index]} />,
        // FR-CIT-07 — the figures of the pages this block cites, beneath this block.
        //
        // Safe inside the memo, and that is worth stating because the memo is load-bearing:
        // this closure reads only `citations`, which is already a dependency, so nothing about
        // loading a figure can invalidate it. All the fetch state lives inside
        // `CitationFigures`, which is why a thumb can be clicked mid-load without remounting
        // every chip and closing the hover card the user is reading.
        renderBlockFigures: (indices) =>
          indices.length === 0 ? null : (
            <CitationFigures citations={indices.map((index) => citations[index])} />
          ),
      }),
    [message.segs, citations],
  );

  return (
    <article className={`${styles.row} animate-fade-up`}>
      <span className="visually-hidden">Corpus replied:</span>
      <AiAvatar />
      <div
        className={message.ungrounded ? `${styles.content} ${styles.ungrounded}` : styles.content}
      >
        {/* FR-MSG-09 — **above** the answer, not below it. The label's job is to be read before
            the text it qualifies; underneath, a reader who stops at the end of a confident
            paragraph never reaches it, which is the one failure R-98(6) exists to prevent. */}
        {message.ungrounded && <p className={styles.ungroundedLabel}>{UNGROUNDED_LABEL}</p>}
        <div className={styles.body}>{body}</div>
        {source !== null && <div className={`${styles.sourceLine} mono`}>{source}</div>}
        <EvalChips evaluation={message.evaluation} />
        {message.ungrounded_offerable && onAnswerUngrounded !== undefined && (
          <div className={styles.ungroundedAction}>
            <button
              type="button"
              className={styles.ungroundedButton}
              onClick={onAnswerUngrounded}
              disabled={ungroundedBusy}
            >
              {UNGROUNDED_ACTION_LABEL}
            </button>
          </div>
        )}
        <div className={styles.actions}>
          <MessageActions
            feedback={message.feedback}
            busy={busy}
            onFeedback={onFeedback}
            onRegenerate={onRegenerate}
          />
        </div>
      </div>
    </article>
  );
}

export interface DegradedAnswerProps {
  /** The server's own FR-ERR-04 / FR-ORC-02 copy, rendered verbatim. */
  text: string;
}

export function DegradedAnswer({ text }: DegradedAnswerProps) {
  return (
    <article className={`${styles.row} animate-fade-up`}>
      <span className="visually-hidden">Corpus replied:</span>
      <AiAvatar />
      <div className={styles.content}>
        <div className={styles.body}>
          <p className={styles.paragraph}>{text}</p>
        </div>
      </div>
    </article>
  );
}
