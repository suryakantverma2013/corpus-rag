import { describe, expect, it } from 'vitest';
import {
  displayTitle,
  formatConversationDate,
  formatMessageCount,
  nextActiveId,
  sortConversations,
  UNTITLED_CONVERSATION,
} from './conversations';
import type { SidebarConversation } from './conversations';

describe('displayTitle (FR-SBR-02/03)', () => {
  it('falls back to "New chat" for a null title', () => {
    // `ConversationResponse.title` is `string | null` and `POST /api/v1/conversations` takes an
    // optional title, so this is reachable in production, not defensive padding.
    expect(displayTitle({ title: null })).toBe(UNTITLED_CONVERSATION);
  });

  it('falls back for a blank or whitespace-only title', () => {
    expect(displayTitle({ title: '' })).toBe(UNTITLED_CONVERSATION);
    expect(displayTitle({ title: '   ' })).toBe(UNTITLED_CONVERSATION);
  });

  it('trims a real title', () => {
    expect(displayTitle({ title: '  Analyzing Market Trends  ' })).toBe('Analyzing Market Trends');
  });
});

describe('formatConversationDate (FR-SBR-03)', () => {
  it('renders the prototype’s "Jul 16" shape', () => {
    expect(formatConversationDate({ updated_at: '2026-07-16T09:12:00Z' })).toBe('Jul 16');
  });

  it('zero-pads the day, as the prototype does ("Jul 08")', () => {
    expect(formatConversationDate({ updated_at: '2026-07-08T08:30:00Z' })).toBe('Jul 08');
  });

  it('is pinned to en-US regardless of the runtime locale', () => {
    // Left to the host locale this renders "16 juil." beside English copy on a machine with
    // different regional settings, and breaks T-510's pixel comparison there and only there.
    const formatted = formatConversationDate({ updated_at: '2026-12-01T00:00:00Z' });
    expect(formatted).toMatch(/^[A-Z][a-z]{2} \d{2}$/);
  });

  it('renders nothing for an unparseable timestamp rather than "Invalid Date"', () => {
    expect(formatConversationDate({ updated_at: 'not-a-date' })).toBe('');
  });

  it('reads updated_at, not created_at', () => {
    // The list is the user's recent activity: a chat they returned to yesterday must not show
    // last month's date. Every fixture had the two timestamps equal, so a mutation swapping
    // the field changed nothing — the type signature is the only other thing that would have
    // objected, and `Pick<Conversation, 'created_at'>` compiles just as well.
    const conversation = { created_at: '2026-01-02T00:00:00Z', updated_at: '2026-07-16T09:12:00Z' };
    expect(formatConversationDate(conversation)).toBe('Jul 16');
  });
});

describe('formatMessageCount (FR-SBR-03)', () => {
  it('renders the prototype’s "· N messages"', () => {
    expect(formatMessageCount(2)).toBe('· 2 messages');
    expect(formatMessageCount(0)).toBe('· 0 messages');
  });
});

describe('sortConversations (FR-SBR-03)', () => {
  const row = (id: string, updated: string, created = updated): SidebarConversation => ({
    id,
    title: id,
    archived: false,
    created_at: created,
    updated_at: updated,
    message_count: 0,
  });

  it('puts the most recently updated first', () => {
    const sorted = sortConversations([
      row('old', '2026-07-01T00:00:00Z'),
      row('new', '2026-08-01T00:00:00Z'),
      row('mid', '2026-07-15T00:00:00Z'),
    ]);
    expect(sorted.map((c) => c.id)).toEqual(['new', 'mid', 'old']);
  });

  it('breaks a genuine tie on created_at, as the repository does', () => {
    const tie = '2026-08-01T00:00:00Z';
    const sorted = sortConversations([
      row('older', tie, '2026-07-01T00:00:00Z'),
      row('newer', tie, '2026-07-20T00:00:00Z'),
    ]);
    expect(sorted.map((c) => c.id)).toEqual(['newer', 'older']);
  });

  it('does not mutate its input', () => {
    // The list is React state a caller still holds; sorting in place rewrites it under them.
    const input = [row('a', '2026-07-01T00:00:00Z'), row('b', '2026-08-01T00:00:00Z')];
    sortConversations(input);
    expect(input.map((c) => c.id)).toEqual(['a', 'b']);
  });

  it('compares instants, not strings', () => {
    // Same moment, two spellings the server can legitimately produce. Lexicographic ordering
    // would call the second one older by twelve hours.
    const sorted = sortConversations([
      row('offset', '2026-08-01T00:00:00+00:00'),
      row('later', '2026-08-01T00:00:01Z'),
    ]);
    expect(sorted.map((c) => c.id)).toEqual(['later', 'offset']);
  });

  it('sorts an unparseable timestamp last rather than scrambling the order', () => {
    const sorted = sortConversations([
      row('broken', 'not a date'),
      row('a', '2026-07-01T00:00:00Z'),
      row('b', '2026-08-01T00:00:00Z'),
    ]);
    expect(sorted.map((c) => c.id)).toEqual(['b', 'a', 'broken']);
  });
});

describe('nextActiveId (FR-SBR-07)', () => {
  const list = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];

  it('selects the row that takes the deleted one’s place', () => {
    expect(nextActiveId(list, 'a')).toBe('b');
    expect(nextActiveId(list, 'b')).toBe('c');
  });

  it('falls back to the preceding row when the last one is deleted', () => {
    // The off-by-one that matters: there is no following row, so "next" has to mean the one
    // above or the selection lands on nothing while conversations remain.
    expect(nextActiveId(list, 'c')).toBe('b');
  });

  it('returns null — the empty state — when the last conversation goes', () => {
    expect(nextActiveId([{ id: 'only' }], 'only')).toBeNull();
  });

  it('returns null for an empty list', () => {
    expect(nextActiveId([], 'gone')).toBeNull();
  });

  it('falls back to the first row when the id is not in the list', () => {
    expect(nextActiveId(list, 'unknown')).toBe('a');
  });
});
