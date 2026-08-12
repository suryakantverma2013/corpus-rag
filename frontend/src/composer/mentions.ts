/**
 * FR-CMP-03/04/05 as pure functions — the open/close matrix, the insert, and the send guard.
 *
 * Extracted from the component for the reason T-503 extracted `conversations.ts`: this is the
 * part of the composer that is *specified* (FR-CMP-05 is an enumerated matrix the spec says was
 * audited against the prototype's script), and it is testable without a DOM. What stays in the
 * component is focus, ARIA and paint.
 */

/** A row in the FR-CMP-04 menu. `type` is the badge, `meta` the right-aligned cell. */
export interface MentionDocument {
  id: string;
  name: string;
  /** Badge text — `PDF` / `CSV` / `DOC` in the prototype's data. */
  type: string;
  /** Size-ish metadata: "24 pages", "1,204 rows". Overridden for chat-scoped documents. */
  meta: string;
  /** FR-ORC-06's two scopes. `chat` means an attachment of the active conversation. */
  scope: 'global' | 'chat';
}

/**
 * FR-CMP-04's literal, and it replaces the metadata rather than joining it: *"Chat-scoped
 * attachments display the literal meta `this chat` **in place of** size metadata"*. The
 * prototype does the same substitution one layer up (`d.scope === 'chat' ? 'this chat' : d.meta`).
 */
export const CHAT_SCOPE_META = 'this chat';

/** What the row renders in its right-hand cell. */
export function mentionMeta(document: MentionDocument): string {
  return document.scope === 'chat' ? CHAT_SCOPE_META : document.meta;
}

/**
 * FR-CMP-05's trailing-`@` condition, evaluated **on every keystroke**.
 *
 * This one predicate implements both halves of the matrix that concern typing: the menu opens
 * when the value comes to end with `@`, and closes when it ceases to — *"even if the menu was
 * opened via the button"*, which is why the caller assigns this result rather than OR-ing it
 * with the previous state. A button-opened menu with no trailing `@` therefore closes on the
 * next keystroke, which the Rev 0.5.1 legacy delta records as intended rather than incidental.
 */
export function hasMentionTrigger(value: string): boolean {
  return value.endsWith('@');
}

/**
 * FR-CMP-05's insert: `@Filename ` with a trailing space, replacing a trailing `@` if present.
 *
 * The replacement is what stops `@@Filename` after the ordinary path (type `@`, pick a row).
 * It is deliberately *only* a trailing `@` — an `@` earlier in the text is a mention the user
 * already completed, and rewriting it would edit a citation they did not touch.
 */
export function insertMention(value: string, name: string): string {
  const base = hasMentionTrigger(value) ? value.slice(0, -1) : value;
  return `${base}@${name} `;
}

/** Why a send was refused — `null` when it may proceed. */
export type SendBlock = 'empty' | 'pending' | 'over-budget';

/**
 * FR-CMP-03's guards, plus FR-STA-04's.
 *
 * Order is deliberate. `empty` outranks `pending` because a user who has typed nothing is not
 * waiting on anything, and both outrank `over-budget` because an empty draft is not the
 * message that broke the budget — reporting a length problem for an empty box would be false.
 *
 * `overBudget` is computed by the caller from `projectSubmission`, so this stays free of the
 * token rule: FR-CMP-03 and FR-STA-04 are separate requirements and one of them has a server
 * half.
 */
export function sendBlock(
  value: string,
  { pending, overBudget }: { pending: boolean; overBudget: boolean },
): SendBlock | null {
  if (value.trim() === '') return 'empty';
  if (pending) return 'pending';
  if (overBudget) return 'over-budget';
  return null;
}

/** FR-CMP-03's "the input is trimmed" — the text that becomes the user message. */
export function outgoingText(value: string): string {
  return value.trim();
}

/**
 * The active-row index after an arrow key, for NFR-A11Y-03's listbox contract.
 *
 * Wraps at both ends, and **starts at the first row from `-1`** on ArrowDown / the last on
 * ArrowUp — the menu opens with nothing active so that Enter still sends (FR-CMP-03), and a
 * user only enters the list by deliberately arrowing into it.
 */
export function nextActiveIndex(current: number, delta: 1 | -1, count: number): number {
  if (count === 0) return -1;
  if (current === -1) return delta === 1 ? 0 : count - 1;
  return (current + delta + count) % count;
}

/**
 * Ids the composer's `aria-controls` / `aria-activedescendant` and the menu's elements must
 * agree on. They live here rather than in `MentionMenu.tsx` for the reason T-501 recorded:
 * `react/only-export-components` fires on a module exporting both a component and a plain
 * value, and the fix is a module boundary rather than a suppression.
 */
export const MENTION_LIST_ID = 'mention-listbox';
export const MENTION_HEADING_ID = 'mention-heading';

/** `aria-activedescendant` points at an id, so both sides must derive the same one. */
export function mentionOptionId(index: number): string {
  return `mention-option-${index}`;
}
