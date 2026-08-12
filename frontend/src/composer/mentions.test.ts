/**
 * FR-CMP-03/04/05's specified behaviour, without a DOM.
 *
 * The spec says the FR-CMP-05 matrix was audited against the prototype's script, so these
 * assert the matrix as written — including the two cases a reasonable implementation gets
 * wrong (a button-opened menu closing on the next keystroke; an earlier `@` left alone).
 */
import { describe, expect, it } from 'vitest';

import {
  CHAT_SCOPE_META,
  hasMentionTrigger,
  insertMention,
  mentionMeta,
  nextActiveIndex,
  outgoingText,
  sendBlock,
} from './mentions';
import type { MentionDocument } from './mentions';

const doc = (over: Partial<MentionDocument> = {}): MentionDocument => ({
  id: 'd1',
  name: 'Q3_Market_Report.pdf',
  type: 'PDF',
  meta: '58 pages',
  scope: 'global',
  ...over,
});

describe('mentionMeta (FR-CMP-04)', () => {
  it('shows the size metadata for a global document', () => {
    expect(mentionMeta(doc())).toBe('58 pages');
  });

  it('replaces it with "this chat" for an attachment, rather than joining the two', () => {
    const attachment = doc({ scope: 'chat', meta: '24 pages' });
    expect(mentionMeta(attachment)).toBe(CHAT_SCOPE_META);
    expect(mentionMeta(attachment)).not.toContain('24 pages');
  });
});

describe('hasMentionTrigger (FR-CMP-05)', () => {
  it('is true only when the value ends with @', () => {
    expect(hasMentionTrigger('@')).toBe(true);
    expect(hasMentionTrigger('what does @')).toBe(true);
    expect(hasMentionTrigger('')).toBe(false);
    expect(hasMentionTrigger('what does @Q3 say')).toBe(false);
  });

  it('is false for a trailing space after the @, which is what closes the menu', () => {
    expect(hasMentionTrigger('@ ')).toBe(false);
  });
});

describe('insertMention (FR-CMP-05)', () => {
  it('replaces a trailing @ rather than doubling it', () => {
    expect(insertMention('what does @', 'Q3.pdf')).toBe('what does @Q3.pdf ');
  });

  it('appends when there is no trailing @ — the button-opened path', () => {
    expect(insertMention('what does ', 'Q3.pdf')).toBe('what does @Q3.pdf ');
  });

  it('leaves an earlier @ alone', () => {
    // An `@` already in the text is a mention the user completed; rewriting it would edit a
    // citation they never touched.
    expect(insertMention('per @A.pdf and @', 'B.pdf')).toBe('per @A.pdf and @B.pdf ');
  });

  it('always ends with a space, so the next keystroke does not reopen the menu', () => {
    const inserted = insertMention('@', 'Q3.pdf');
    expect(inserted.endsWith(' ')).toBe(true);
    expect(hasMentionTrigger(inserted)).toBe(false);
  });

  it('inserts into an empty composer', () => {
    expect(insertMention('', 'Q3.pdf')).toBe('@Q3.pdf ');
  });
});

describe('sendBlock (FR-CMP-03, FR-STA-04)', () => {
  const ok = { pending: false, overBudget: false };

  it('allows a real message', () => {
    expect(sendBlock('hello', ok)).toBeNull();
  });

  it('ignores empty and whitespace-only input', () => {
    expect(sendBlock('', ok)).toBe('empty');
    expect(sendBlock('   \t\n ', ok)).toBe('empty');
  });

  it('ignores a send while a reply is pending', () => {
    expect(sendBlock('hello', { ...ok, pending: true })).toBe('pending');
  });

  it('blocks an over-budget send', () => {
    expect(sendBlock('hello', { ...ok, overBudget: true })).toBe('over-budget');
  });

  it('reports emptiness before anything else', () => {
    // An empty box is not the message that broke the budget, and the user is not waiting on a
    // reply they have not asked for — so neither of those may be the reported reason.
    expect(sendBlock('  ', { pending: true, overBudget: true })).toBe('empty');
  });
});

describe('outgoingText (FR-CMP-03)', () => {
  it('trims', () => {
    expect(outgoingText('  hello  ')).toBe('hello');
  });
});

describe('nextActiveIndex (NFR-A11Y-03)', () => {
  it('enters the list at the first row going down and the last going up', () => {
    expect(nextActiveIndex(-1, 1, 3)).toBe(0);
    expect(nextActiveIndex(-1, -1, 3)).toBe(2);
  });

  it('wraps at both ends', () => {
    expect(nextActiveIndex(2, 1, 3)).toBe(0);
    expect(nextActiveIndex(0, -1, 3)).toBe(2);
  });

  it('stays at -1 for an empty list', () => {
    expect(nextActiveIndex(-1, 1, 0)).toBe(-1);
  });
});
