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
  mentionedIds,
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

describe('mentionedIds — SendMessageRequest.document_ids (FR-RET-04, R-46(1))', () => {
  const q3 = doc({ id: 'q3', name: 'Q3' });
  const q3pdf = doc({ id: 'q3pdf', name: 'Q3.pdf' });
  const runbook = doc({ id: 'rb', name: 'Onboarding Playbook.docx' });

  it('finds nothing in ordinary prose', () => {
    expect(mentionedIds('What is the refund window?', [q3, runbook])).toEqual([]);
  });

  it('finds one mention', () => {
    expect(mentionedIds(insertMention('summarise ', 'Q3'), [q3, runbook])).toEqual(['q3']);
  });

  it('finds several', () => {
    const text = insertMention(insertMention('compare ', 'Q3'), 'Onboarding Playbook.docx');
    expect(mentionedIds(text, [q3, runbook])).toEqual(['q3', 'rb']);
  });

  it('matches a name containing spaces', () => {
    // `insertMention` writes the whole filename, spaces and all — there is no word boundary
    // to lean on, which is why this scans for the literal token.
    expect(mentionedIds('see @Onboarding Playbook.docx please', [runbook])).toEqual(['rb']);
  });

  it('prefers the longest name, so a prefix cannot steal the match', () => {
    // The defect list order would produce: `@Q3.pdf` contains `@Q3`, so scanning in order
    // sends the wrong document and the user is answered from a file they did not name.
    expect(mentionedIds('see @Q3.pdf', [q3, q3pdf])).toEqual(['q3pdf']);
    expect(mentionedIds('see @Q3.pdf', [q3pdf, q3])).toEqual(['q3pdf']);
  });

  it('still finds both when both are mentioned', () => {
    expect(mentionedIds('@Q3 versus @Q3.pdf', [q3, q3pdf]).sort()).toEqual(['q3', 'q3pdf']);
  });

  it('ignores a document that is not in scope', () => {
    // The menu only offers the FR-ORC-06 scope, so a name typed by hand for a document the
    // caller cannot see must not become an id. The server AND-s the filter anyway (R-46(1)),
    // but sending it would be a request we know is meaningless.
    expect(mentionedIds('see @Secret_Plans.pdf', [q3])).toEqual([]);
  });

  it('returns ids in list order, not match order', () => {
    expect(mentionedIds('@Onboarding Playbook.docx then @Q3', [q3, runbook])).toEqual(['q3', 'rb']);
  });
});
