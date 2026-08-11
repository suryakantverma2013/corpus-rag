/**
 * FR-HDR-01..03 — the chat header.
 *
 * Renders into T-502's `chat` slot as the first of `<main>`'s three flex children; T-505's
 * message list and T-506's composer follow it.
 *
 * Presentational, following T-503: the title arrives as a prop and T-509 owns wiring it to the
 * conversations API. The single exception is `ThemeToggle`, which reads `useTheme()` directly
 * because R-58(5) forbids threading theme through the shell.
 */
import styles from './ChatHeader.module.css';
import { ThemeToggle } from './ThemeToggle';
import { UNTITLED_CONVERSATION } from '../sidebar/conversations';

/** FR-HDR-02. Always rendered: R-23 ruled grounding **always-on** (superseding the docx's
 *  opt-in model), and FR-HDR-02 words the pill unconditionally — so it carries no prop and no
 *  hidden state. An empty retrieval scope produces an FR-RET-05 abstention in the message
 *  list, not a header that claims the system is ungrounded. */
const GROUNDED_LABEL = 'Grounded';

export interface ChatHeaderProps {
  /**
   * FR-HDR-01 — the active chat's title, already resolved by the caller (`displayTitle`).
   *
   * `null` means **there is no active conversation at all**, which is reachable in the product
   * — FR-SBR-07 deleting the last chat leaves `activeId` at `null` (`nextActiveId` returns it)
   * — and is not reachable in the prototype, whose state is index-based into a non-empty array.
   * So the rule is ours: it renders the label a chat with no title carries, because what the
   * user is looking at is functionally a fresh chat (the FR-MSG-02 empty state, an empty
   * composer). An empty `<h2>` was the alternative and is worse: an empty heading is a defect
   * in its own right.
   */
  title: string | null;
}

export function ChatHeader({ title }: ChatHeaderProps) {
  return (
    // <header>, but deliberately NOT a `banner` landmark: that mapping applies only outside
    // article/aside/main/nav/section, and this lives inside T-502's <main>, so it stays a
    // generic element and adds no landmark to the FR-LAY-01 set.
    <header className={styles.header}>
      {/* An <h2>, not the <div> the prototype uses. T-502 owns the document's only <h1> and
          bound every other heading to h2-or-deeper; the sidebar's CONVERSATIONS label is
          already an h2, so this sits beside it. */}
      <h2 className={styles.title}>{title ?? UNTITLED_CONVERSATION}</h2>

      {/* FR-HDR-02. The dot is decorative — the word beside it carries the meaning, and
          announcing an unlabelled shape adds nothing. */}
      <div className={styles.grounded}>
        <span className={styles.groundedDot} aria-hidden="true" />
        {GROUNDED_LABEL}
      </div>

      <div className={styles.actions}>
        <ThemeToggle />
      </div>
    </header>
  );
}
