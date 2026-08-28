/**
 * §4.3 — the scrollable transcript: FR-MSG-01 (scroll), FR-MSG-02 (empty state), FR-MSG-05
 * (typing indicator), FR-STA-04/R-67 (the frozen conversation) and NFR-A11Y-05 (the live region).
 *
 * The second of `<main>`'s three flex children. `AppShell` supplies only the column direction, so
 * this owns its own `flex`, padding and overflow — see `AppShellProps.chat`.
 *
 * Presentational: every mutation is a prop and nothing here fetches. `App` holds the state,
 * which since T-513 is the chat store's rather than a seed's — this file did not change for
 * that, which is the point of the shape (T-503's).
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import styles from './MessageList.module.css';
import { AiAvatar, AiMessage, DegradedAnswer } from './AiMessage';
import { UserMessage } from './UserMessage';
import { announcementFor, isDegraded, plainText } from './messages';
import type { TranscriptEntry } from './messages';
import { useCitationHover } from './useCitationHover';
import type { Feedback } from '../api';

/** §9's literal. */
const EMPTY_TITLE = 'Ask your knowledge base';
/**
 * FR-MSG-02's helper, **amended**: the prototype's copy ends "…or attach new ones with the
 * paperclip", and R-12 removed the paperclip. The `+` renders plain — the requirement writes it
 * as `**+**`, which is Markdown emphasis inside requirement prose rather than a styling
 * instruction, and the prototype's helper line has no bold anywhere. Wording is `TBD(§8.4)`.
 */
const EMPTY_HELPER_LEAD = 'Answers are grounded in your documents. Type ';
const EMPTY_HELPER_TAIL = ' to reference a specific file, or attach new ones with the + button.'; // TBD(§8.4)

/** The brand mark, matching FR-AUT-01's login card and the FR-SBR-01 brand row. */
const EMPTY_MARK = 'C';

/**
 * FR-STA-04's own line, adopted by R-67(2).
 *
 * **Derived from the requirement, never from the `409` body.** The composer blocks before a
 * request exists (R-51(6)), so no server `message` can reach either surface — both sides derive
 * from FR-STA-04 so the two cannot drift. It names no number (R-51(5)): the limit is a
 * `# TBD(§8.4)` knob, and a user can act on "start a new chat" but not on "10,400".
 *
 * **Wording author-confirmed by R-69(1) — no longer provisional.** This is FR-STA-04's own
 * line, byte-identical to the backend's `CONTEXT_WINDOW_EXCEEDED` because both copy the
 * requirement. Change it there first, or the two stop deriving from one parent.
 */
const FROZEN_NOTICE =
  'This conversation has reached its length limit. Start a new chat to keep going — your ' +
  'documents and their answers stay where they are.';

export interface MessageListProps {
  entries: readonly TranscriptEntry[];
  /** FR-MSG-05, FR-MSG-01's fourth scroll trigger, and FR-MSG-08's disabled affordance. */
  typing: boolean;
  /** FR-MSG-01's "when a chat is selected"; also resets the FR-CIT-03 card. */
  conversationId: string | null;
  /** FR-STA-04 / R-67 — frozen, not broken. */
  frozen: boolean;
  userInitials: string;
  onFeedback: (messageId: string, feedback: Feedback | null) => void;
  onRegenerate: (messageId: string) => void;
  /** FR-MSG-09 (R-98) — offered on an abstention only, and only where the server says so. */
  onAnswerUngrounded: (messageId: string) => void;
  /** The message ids whose FR-MSG-09 request is in flight. */
  ungroundedBusy: ReadonlySet<string>;
}

export function MessageList({
  entries,
  typing,
  conversationId,
  frozen,
  userInitials,
  onFeedback,
  onRegenerate,
  onAnswerUngrounded,
  ungroundedBusy,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const { dismiss } = useCitationHover();

  /**
   * FR-MSG-01, on all four of its triggers.
   *
   * `useLayoutEffect` because the prototype scrolls from `componentDidUpdate`, i.e. before the
   * browser paints — under `useEffect` a long answer is visible at the old offset for a frame.
   *
   * Keyed on `entries.length`, deliberately **not** on `entries`: a feedback toggle and a
   * regenerate both replace the array identity without adding a row, and FR-MSG-01 lists only
   * "when a message is added". The cost is that a regenerate producing a shorter answer does not
   * re-scroll, which is the requirement's literal behaviour.
   */
  useLayoutEffect(() => {
    const element = scrollRef.current;
    // `scrollTop = scrollHeight`, never `scrollIntoView` — FR-MSG-01 is explicit, and
    // `scrollIntoView` scrolls every scrollable ancestor, which here would move the whole app.
    if (element !== null) element.scrollTop = element.scrollHeight;
  }, [conversationId, entries.length, typing]);

  // Switching chat resets the hover card (the handoff README's "chat switching … resets hover
  // card"): its position was captured against a chip that no longer exists.
  useEffect(() => dismiss(), [conversationId, dismiss]);

  const announcement = useAnnouncement(entries, frozen);
  const isEmpty = entries.length === 0 && !typing;

  return (
    <div className={styles.list} ref={scrollRef}>
      {/*
        NFR-A11Y-05. Not `role="log"` on the list itself: that role is an implicit live region,
        so it would announce the whole transcript on a chat switch and double-announce every
        arrival against this one. R-54(2) streams progress frames with no content and commits
        the answer in one go, so without this a non-sighted user has no signal a reply exists.
      */}
      <div className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </div>

      {isEmpty && <EmptyState />}

      {entries.map((entry, index) => (
        <Entry
          // `id` is null on a degraded send, and two failures in one chat would then collide —
          // so the index disambiguates. Rows are append-only within a conversation, and the
          // `conversationId` key on the list is what discards state across a switch.
          key={entry.message.id ?? `degraded-${index}`}
          entry={entry}
          busy={typing}
          userInitials={userInitials}
          onFeedback={onFeedback}
          onRegenerate={onRegenerate}
          onAnswerUngrounded={onAnswerUngrounded}
          ungroundedBusy={ungroundedBusy}
        />
      ))}

      {typing && <TypingIndicator />}

      {frozen && <FrozenNotice />}
    </div>
  );
}

interface EntryProps {
  entry: TranscriptEntry;
  busy: boolean;
  userInitials: string;
  onFeedback: (messageId: string, feedback: Feedback | null) => void;
  onRegenerate: (messageId: string) => void;
  onAnswerUngrounded: (messageId: string) => void;
  ungroundedBusy: ReadonlySet<string>;
}

function Entry({
  entry,
  busy,
  userInitials,
  onFeedback,
  onRegenerate,
  onAnswerUngrounded,
  ungroundedBusy,
}: EntryProps) {
  const { message } = entry;
  if (message.role === 'user') {
    return <UserMessage text={plainText(message.segs)} initials={userInitials} />;
  }
  // R-54(3): an errored or denied turn is served but never stored, so it has no row to rate or
  // regenerate. Everything else — including an abstention and an injection-blocked turn — is a
  // real message and renders through the ordinary path.
  if (isDegraded(message)) return <DegradedAnswer text={plainText(message.segs)} />;
  return (
    <AiMessage
      message={message}
      busy={busy}
      onFeedback={(feedback) => onFeedback(message.id, feedback)}
      onRegenerate={() => onRegenerate(message.id)}
      onAnswerUngrounded={() => onAnswerUngrounded(message.id)}
      ungroundedBusy={ungroundedBusy.has(message.id)}
    />
  );
}

/**
 * NFR-A11Y-05's polite announcement, fired only when the thing being announced actually changed
 * — "shall not re-announce unchanged content" is the requirement's own clause, and a live region
 * whose text is reassigned on every render repeats itself on a feedback click.
 */
function useAnnouncement(entries: readonly TranscriptEntry[], frozen: boolean): string {
  const [announcement, setAnnouncement] = useState('');
  const lastRef = useRef<string | null>(null);

  useEffect(() => {
    const last = entries[entries.length - 1];
    if (last === undefined) {
      lastRef.current = null;
      setAnnouncement('');
      return;
    }
    const signature = `${last.message.id ?? 'degraded'}:${last.outcome ?? ''}:${entries.length}`;
    if (lastRef.current === signature) return;
    lastRef.current = signature;
    const next = announcementFor(last);
    if (next !== null) setAnnouncement(next);
  }, [entries]);

  const frozenRef = useRef(frozen);
  useEffect(() => {
    if (frozen && !frozenRef.current) setAnnouncement(FROZEN_NOTICE);
    frozenRef.current = frozen;
  }, [frozen]);

  return announcement;
}

/** FR-MSG-02. */
function EmptyState() {
  return (
    <div className={styles.empty}>
      <div className={styles.emptyMark} aria-hidden="true">
        {EMPTY_MARK}
      </div>
      {/*
        An <h3>, not an <h2>. T-502 owns the document's only <h1> and T-504's chat title is the
        <h2> inside <main>; a second one here would put two peers in the outline for the same
        region — and `App.test.tsx` asserts exactly one, in the state this block renders in.
      */}
      <h3 className={styles.emptyTitle}>{EMPTY_TITLE}</h3>
      <div className={styles.emptyHelper}>
        {EMPTY_HELPER_LEAD}
        <span className={`${styles.emptyMention} mono`}>@</span>
        {EMPTY_HELPER_TAIL}
      </div>
    </div>
  );
}

/**
 * FR-MSG-05.
 *
 * The dots use the global `dotPulse` and the `--motion-dot*` tokens and add **no**
 * `prefers-reduced-motion` block of their own: NFR-A11Y-01 puts the baseline in `tokens.css`,
 * which redefines the keyframe so the dots stay fully visible and still. A local block here
 * would fall the element back to its resting style, which is the failure that requirement exists
 * to prevent.
 *
 * Note the avatar has no `margin-top` here, unlike FR-MSG-04's — the prototype's one difference
 * between the two, and the easiest thing in this task to copy across by accident.
 */
function TypingIndicator() {
  return (
    <div className={styles.typingRow}>
      {/* No `margin-top` here, unlike FR-MSG-04's avatar — the prototype's one difference
          between the two (line 126 vs line 99). It is expressed by `.row .avatar` in
          AiMessage.module.css rather than by an override, because two single-class rules
          are a specificity tie that stylesheet order decides. */}
      <AiAvatar />
      {/* The visible signal is the animation; the announcement is the live region's job, and
          announcing "thinking" politely would be superseded by the answer moments later. */}
      <div className={styles.typingBubble} aria-hidden="true">
        <span className={`${styles.dot} animate-dot-pulse`} />
        <span className={`${styles.dot} ${styles.dot2} animate-dot-pulse`} />
        <span className={`${styles.dot} ${styles.dot3} animate-dot-pulse`} />
      </div>
    </div>
  );
}

/**
 * FR-STA-04 / R-67(1) — a **designed terminal state, not an error**.
 *
 * Only submission is refused. The transcript stays readable, its citations still resolve, the
 * FR-SBR-07 rename still works, the FR-ANL-03 meter simply stands full, and deleting the chat
 * remains optional — which is the load-bearing part, because a user who believes a full chat is a
 * lost chat will delete it. So this is a quiet notice in the flow rather than an alert: `status`,
 * not `alert`, and no error colour.
 */
function FrozenNotice() {
  return (
    <div className={styles.frozen} role="status">
      {FROZEN_NOTICE}
    </div>
  );
}
