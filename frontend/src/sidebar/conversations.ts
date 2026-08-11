/**
 * Pure helpers for the FR-SBR-03 conversation row. No React, no DOM — the `theme.ts` shape.
 */
import type { Conversation } from '../api';

/**
 * A conversation as the sidebar renders it: the generated API model plus the message count.
 *
 * **`messageCount` is not on `ConversationResponse` and has to be supplied by the caller**,
 * because FR-SBR-03 requires "· N messages" on *every* row while `GET /api/v1/conversations`
 * returns no count at all. It cannot be derived client-side either — only the active chat's
 * messages are ever loaded, so every other row would render "· 0 messages", which is worse
 * than wrong: it is plausible. Recorded as **T-407** (add `message_count` to the list route);
 * until that lands, T-509's wiring has nothing truthful to pass here.
 *
 * Deliberately an intersection rather than a redeclared interface, so a change to the
 * generated model reaches this file as a type error (the frontend-dev "never hand-write a
 * response type" rule).
 */
export type SidebarConversation = Conversation & { messageCount: number };

/**
 * FR-SBR-02's title for a chat that has none.
 *
 * `ConversationResponse.title` is `string | null` — `POST /api/v1/conversations` takes an
 * optional title — so the null case is reachable in production, not defensive padding. The
 * label is the one FR-SBR-02 names for a newly created chat.
 */
export const UNTITLED_CONVERSATION = 'New chat';

export function displayTitle(conversation: Pick<Conversation, 'title'>): string {
  const title = conversation.title?.trim();
  return title === undefined || title === '' ? UNTITLED_CONVERSATION : title;
}

/**
 * The mono date on a conversation row — `"Jul 16"`, matching the prototype's hardcoded strings.
 *
 * `en-US` is pinned rather than left to the runtime locale: every §9 literal in this product is
 * English, and a locale-dependent format would silently render "16 juil." beside English copy
 * and break T-510's pixel comparison on a machine with different regional settings.
 *
 * Reads `updated_at`, not `created_at` — the list is the user's recent activity, and a chat
 * they returned to yesterday showing last month's date reads as stale.
 */
export function formatConversationDate(conversation: Pick<Conversation, 'updated_at'>): string {
  const date = new Date(conversation.updated_at);
  // An unparseable timestamp must not render "Invalid Date" into the row. Empty is honest:
  // FR-SBR-03's row still shows its title and count, and the flex `gap` collapses cleanly.
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: '2-digit' }).format(date);
}

/** FR-SBR-03's "· N messages". Kept beside the date so the two cannot drift apart. */
export function formatMessageCount(count: number): string {
  return `· ${count} messages`;
}

/**
 * FR-SBR-07: after deleting the active conversation, "selects the next conversation or the
 * empty state". Returns the id to activate, or `null` for the empty state.
 *
 * "Next" is the row that takes the deleted one's place — the following item, falling back to
 * the preceding one when the deleted row was last. Pure, so the rule is testable without
 * rendering: it is the kind of index arithmetic that is wrong by one for a year.
 */
export function nextActiveId(
  conversations: readonly Pick<Conversation, 'id'>[],
  deletedId: string,
): string | null {
  const index = conversations.findIndex((c) => c.id === deletedId);
  if (index === -1) return conversations[0]?.id ?? null;
  const remaining = conversations.filter((c) => c.id !== deletedId);
  if (remaining.length === 0) return null;
  return (remaining[index] ?? remaining[remaining.length - 1]).id;
}
