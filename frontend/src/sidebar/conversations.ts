/**
 * Pure helpers for the FR-SBR-03 conversation row. No React, no DOM — the `theme.ts` shape.
 */
import type { Conversation } from '../api';

/**
 * A conversation as the sidebar renders it — the generated API model, unadorned.
 *
 * It was `Conversation & { messageCount: number }` until T-407 put `message_count` on
 * `ConversationResponse`. FR-SBR-03 requires "· N messages" on *every* row, and the count
 * cannot be derived client-side — only the active chat's messages are ever loaded, so every
 * other row would have rendered "· 0 messages", which is worse than wrong because it is
 * plausible. Now the server sends it and the alias is a rename, kept because "the sidebar's
 * row model" is worth naming even when it happens to equal the response.
 */
export type SidebarConversation = Conversation;

/**
 * FR-SBR-03 sidebar order, reproducing `ConversationRepository.list_by_owner`.
 *
 * The client needs its own copy because the list is mutated locally between fetches — a
 * rename moves a row and a send touches `updated_at` — and re-fetching the whole list to
 * learn an order we can compute is a round trip for nothing. Non-mutating: `sort` in place
 * would rewrite React state a caller still holds.
 *
 * Compares parsed instants, not the strings. ISO-8601 does happen to sort lexicographically,
 * but only while every value carries the same offset and the same fractional precision — and
 * `created_at` and `updated_at` come from two different server clocks (`now()` and
 * `clock_timestamp()`), so that is an assumption about serialisation, not about time.
 * `created_at` breaks a genuine tie, exactly as the repository's `order_by` does.
 */
export function sortConversations(
  conversations: readonly SidebarConversation[],
): SidebarConversation[] {
  const at = (value: string): number => {
    const parsed = Date.parse(value);
    // An unparseable timestamp sorts oldest rather than poisoning every comparison it takes
    // part in: `NaN` makes the comparator return `NaN`, which leaves the order unspecified.
    return Number.isNaN(parsed) ? -Infinity : parsed;
  };
  return [...conversations].sort(
    (a, b) => at(b.updated_at) - at(a.updated_at) || at(b.created_at) - at(a.created_at),
  );
}

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
