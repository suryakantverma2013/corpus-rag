/**
 * FR-MSG-03 — the user's own message.
 *
 * Transcribed from `RAG Chatbot.dc.html` lines 92–95. **Plain text, deliberately**: FR-MSG-07
 * scopes Markdown to AI body text and says "user messages remain plain text", so a question
 * containing `*` or `#` renders as the user typed it.
 */
import styles from './UserMessage.module.css';

export interface UserMessageProps {
  text: string;
  /** FR-SBR-06's initials, from the signed-in user. */
  initials: string;
}

export function UserMessage({ text, initials }: UserMessageProps) {
  return (
    <article className={`${styles.row} animate-fade-up`}>
      {/* The avatar repeats the author visually only; the label is what carries it to assistive
          technology, since a screen-reader user gets no cue from a right-aligned bubble. */}
      <span className="visually-hidden">You said:</span>
      <div className={styles.bubble}>{text}</div>
      <div className={styles.avatar} aria-hidden="true">
        {initials}
      </div>
    </article>
  );
}
