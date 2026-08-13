/**
 * The §4.7 store and the FR-KBM-04 derivations.
 *
 * No DOM and no mocks: the whole R-41 contract is a reducer over plain data, which is the point
 * of keeping it out of the component.
 */
import { describe, expect, it } from 'vitest';

import type { DocumentEvent, DocumentStatus } from '../api';
import {
  EMPTY_DOCUMENTS,
  actionBlock,
  actionFor,
  announceTransition,
  asEventRow,
  badgeType,
  documentCount,
  documentsReducer,
  impliesProcessingLock,
  indexedOn,
  metaLine,
  replaceFailed,
  sectionHeading,
  showsProgress,
  splitByScope,
  statusLabel,
  toMentionDocuments,
} from './documents';

const ALL_STATUSES: readonly DocumentStatus[] = [
  'UPLOADED',
  'QUEUED',
  'PARSING',
  'CHUNKING',
  'EMBEDDING',
  'INDEXING',
  'ACTIVE',
  'FAILED',
  'DELETE_PENDING',
  'DELETING',
  'DELETED',
];

function doc(overrides: Partial<DocumentEvent> = {}): DocumentEvent {
  return {
    document_id: 'd1',
    filename: 'Q3_Market_Report.pdf',
    mime_type: 'application/pdf',
    status: 'ACTIVE',
    current_version: 1,
    searchable: true,
    size_bytes: 2048,
    page_count: 58,
    chunk_count: 12,
    error_message: null,
    knowledge_base_id: 'kb1',
    scope: 'global',
    conversation_id: null,
    latest_job_id: 'j1',
    latest_job_error_code: null,
    latest_job_document_version: 1,
    created_at: '2026-07-10T09:00:00Z',
    updated_at: '2026-07-10T09:05:00Z',
    deleted_at: null,
    stalled: false,
    ...overrides,
  };
}

const seeded = (...rows: DocumentEvent[]) =>
  documentsReducer(EMPTY_DOCUMENTS, { type: 'snapshot', documents: rows });

describe('the R-41 reducer', () => {
  it('replaces the set on a snapshot rather than merging it', () => {
    // R-41(6) makes every connect authoritative. A merge would resurrect a row deleted while the
    // stream was disconnected — precisely what snapshot-per-connect exists to prevent.
    const before = seeded(doc({ document_id: 'a' }), doc({ document_id: 'b' }));
    const after = documentsReducer(before, {
      type: 'snapshot',
      documents: [doc({ document_id: 'a' })],
    });
    expect(after.rows.map((r) => r.document_id)).toEqual(['a']);
  });

  it('upserts one row and leaves the others IDENTICAL', () => {
    // Object identity, not just equality: the stream re-reads every row every 1.5s, so a reducer
    // that rebuilt them all would re-render the whole list to change one status.
    const before = seeded(
      doc({ document_id: 'a', created_at: '2026-07-10T09:00:00Z' }),
      doc({ document_id: 'b', created_at: '2026-07-09T09:00:00Z' }),
    );
    const after = documentsReducer(before, {
      type: 'document',
      document: doc({ document_id: 'a', status: 'FAILED', created_at: '2026-07-10T09:00:00Z' }),
    });
    expect(after.rows[0].status).toBe('FAILED');
    expect(after.rows[1]).toBe(before.rows[1]);
  });

  it('inserts a row it has never seen at its ordered position', () => {
    const before = seeded(
      doc({ document_id: 'a', created_at: '2026-07-10T09:00:00Z' }),
      doc({ document_id: 'c', created_at: '2026-07-01T09:00:00Z' }),
    );
    const after = documentsReducer(before, {
      type: 'document',
      document: doc({ document_id: 'b', created_at: '2026-07-05T09:00:00Z' }),
    });
    expect(after.rows.map((r) => r.document_id)).toEqual(['a', 'b', 'c']);
  });

  it('removes by id', () => {
    const before = seeded(doc({ document_id: 'a' }), doc({ document_id: 'b' }));
    const after = documentsReducer(before, { type: 'removed', documentId: 'a' });
    expect(after.rows.map((r) => r.document_id)).toEqual(['b']);
  });

  it('returns the SAME state object for a removal it does not know', () => {
    // A `removed` for a row an earlier snapshot already dropped is ordinary (R-41(4)); a fresh
    // array would re-render the list for nothing.
    const before = seeded(doc({ document_id: 'a' }));
    expect(documentsReducer(before, { type: 'removed', documentId: 'zzz' })).toBe(before);
  });

  it('widens a REST list into the same row type, stalled false', () => {
    const listed = documentsReducer(EMPTY_DOCUMENTS, {
      type: 'list',
      documents: [asEventRow(doc({ stalled: true }))].map(({ stalled: _s, ...rest }) => rest),
    });
    expect(listed.rows[0].stalled).toBe(false);
  });

  it('applies a verb’s own 202 to one field, and never inserts', () => {
    const before = seeded(doc({ document_id: 'a', status: 'FAILED' }));
    const after = documentsReducer(before, {
      type: 'status',
      documentId: 'a',
      status: 'QUEUED',
    });
    expect(after.rows[0].status).toBe('QUEUED');

    const unknown = documentsReducer(before, {
      type: 'status',
      documentId: 'nope',
      status: 'QUEUED',
    });
    expect(unknown).toBe(before);
  });

  it('discriminates frames on the envelope’s own `event` key', () => {
    expect(actionFor({ event: 'snapshot', data: [] })).toEqual({ type: 'snapshot', documents: [] });
    expect(actionFor({ event: 'removed', data: { document_id: 'x' } })).toEqual({
      type: 'removed',
      documentId: 'x',
    });
  });
});

describe('FR-KBM-04 — the eight labels over eleven states', () => {
  it('maps every backend state, and only DELETED to nothing', () => {
    const mapped = ALL_STATUSES.map(statusLabel);
    expect(mapped).toEqual([
      'Queued', // UPLOADED renders as Queued — the requirement says so
      'Queued',
      'Parsing',
      'Chunking',
      'Embedding',
      'Indexing',
      'Ready', // ACTIVE
      'Failed',
      'Deleting', // DELETE_PENDING renders as Deleting
      'Deleting',
      null, // a tombstone never reaches this surface (R-41(4) sends `removed`)
    ]);
    // Exactly eight labels, and no ninth ever (R-41(5)).
    expect(new Set(mapped.filter((l) => l !== null)).size).toBe(8);
  });

  it('drops the animation for a stalled row but keeps its label', () => {
    const stalled = doc({ status: 'EMBEDDING', stalled: true });
    expect(showsProgress(stalled)).toBe(false);
    expect(statusLabel(stalled.status)).toBe('Embedding');
    expect(showsProgress(doc({ status: 'EMBEDDING' }))).toBe(true);
  });

  it('never animates a deletion, however long it persists (R-39(7))', () => {
    for (const status of ['DELETE_PENDING', 'DELETING'] as const) {
      expect(showsProgress(doc({ status, stalled: false }))).toBe(false);
    }
  });
});

describe('FR-KBM-04 — the meta line', () => {
  it('prefers pages and renders the date as the prototype does', () => {
    expect(metaLine(doc())).toBe('58 pages · indexed Jul 10');
    expect(indexedOn(doc({ updated_at: '2026-07-08T12:00:00Z' }))).toBe('Jul 08');
  });

  it('falls back to chunks when there is no page count (R-71(4))', () => {
    // R-34 makes `page_count` PDF-only and nothing on the wire carries a CSV row count, so the
    // prototype's "1,204 rows" is unsatisfiable for three of the four formats.
    expect(metaLine(doc({ page_count: null, chunk_count: 1204 }))).toBe(
      '1,204 chunks · indexed Jul 10',
    );
  });

  it('drops the count clause entirely when neither exists', () => {
    expect(metaLine(doc({ page_count: null, chunk_count: null }))).toBe('indexed Jul 10');
  });

  it('renders singulars (the R-70(7) `1 passages` finding)', () => {
    expect(metaLine(doc({ page_count: 1 }))).toBe('1 page · indexed Jul 10');
    expect(metaLine(doc({ page_count: null, chunk_count: 1 }))).toBe('1 chunk · indexed Jul 10');
  });

  it('qualifies a failed replace with the version still answering (OI-29 / R-71(2))', () => {
    const failedReplace = doc({
      status: 'FAILED',
      current_version: 1,
      latest_job_document_version: 2,
    });
    expect(replaceFailed(failedReplace)).toBe(true);
    expect(metaLine(failedReplace)).toContain('update failed, v1 still answering');
  });

  it('does not qualify an ordinary failure', () => {
    // A first ingestion that failed targets the version it never built, so the two are equal —
    // and that document really is silent.
    const failed = doc({ status: 'FAILED', current_version: 1, latest_job_document_version: 1 });
    expect(replaceFailed(failed)).toBe(false);
    expect(metaLine(failed)).not.toContain('still answering');
  });
});

describe('FR-KBM-03 — the two sections', () => {
  const rows = [
    doc({ document_id: 'g', scope: 'global' }),
    doc({ document_id: 'mine', scope: 'chat', conversation_id: 'c1' }),
    doc({ document_id: 'theirs', scope: 'chat', conversation_id: 'c2' }),
  ];

  it('shows this chat’s attachments and no other chat’s', () => {
    const { global, chat } = splitByScope(rows, 'c1');
    expect(global.map((r) => r.document_id)).toEqual(['g']);
    expect(chat.map((r) => r.document_id)).toEqual(['mine']);
  });

  it('shows no attachments at all when there is no conversation', () => {
    expect(splitByScope(rows, null).chat).toEqual([]);
  });

  it('counts global plus THIS chat, which is the FR-ORC-06 scope', () => {
    // FR-SBR-05 and FR-CMP-06 must show the same number; one derivation is what guarantees it.
    expect(documentCount(rows, 'c1')).toBe(2);
    expect(documentCount(rows, 'c2')).toBe(2);
    expect(documentCount(rows, null)).toBe(1);
  });

  it('pluralises the section headings', () => {
    expect(sectionHeading('global', 1)).toBe('GLOBAL · 1 DOCUMENT');
    expect(sectionHeading('global', 4)).toBe('GLOBAL · 4 DOCUMENTS');
    expect(sectionHeading('chat', 1)).toBe('THIS CHAT · 1 ATTACHMENT');
    expect(sectionHeading('chat', 0)).toBe('THIS CHAT · 0 ATTACHMENTS');
  });

  it('derives the FR-CMP-04 mention rows from the same set', () => {
    const rendered = toMentionDocuments(rows, 'c1');
    expect(rendered.map((d) => d.id)).toEqual(['g', 'mine']);
    expect(rendered[0]).toMatchObject({
      name: 'Q3_Market_Report.pdf',
      type: 'PDF',
      scope: 'global',
    });
  });

  it('badges a .docx as DOC, not DOCX', () => {
    // A four-character badge overflows the 34px cell; the prototype's own sample proves it.
    expect(badgeType('Pricing_Strategy.docx')).toBe('DOC');
    expect(badgeType('Customer_Feedback.csv')).toBe('CSV');
  });
});

describe('FR-KBM-07 — when an action is offered', () => {
  const idle = { turnInFlight: false };

  it('offers Retry only from FAILED', () => {
    expect(actionBlock(doc({ status: 'FAILED' }), 'retry', idle)).toBeNull();
    for (const status of ['ACTIVE', 'QUEUED', 'INDEXING'] as const) {
      expect(actionBlock(doc({ status }), 'retry', idle)).toBe('wrong-state');
    }
  });

  it('offers Replace only from ACTIVE or FAILED (R-40(2))', () => {
    expect(actionBlock(doc({ status: 'ACTIVE' }), 'replace', idle)).toBeNull();
    expect(actionBlock(doc({ status: 'FAILED' }), 'replace', idle)).toBeNull();
    expect(actionBlock(doc({ status: 'CHUNKING' }), 'replace', idle)).toBe('wrong-state');
  });

  it('does not offer Delete on a document already deleting', () => {
    expect(actionBlock(doc({ status: 'DELETE_PENDING' }), 'delete', idle)).toBe('wrong-state');
    expect(actionBlock(doc({ status: 'ACTIVE' }), 'delete', idle)).toBeNull();
  });

  it('reports the state before the pause, because only one of them can be fixed by waiting', () => {
    // A Retry offered on an ACTIVE document is wrong in a perfectly idle app, so telling the user
    // to wait for the answer to finish would send them to wait for something that cannot help.
    const busy = { turnInFlight: true };
    expect(actionBlock(doc({ status: 'ACTIVE' }), 'retry', busy)).toBe('wrong-state');
    expect(actionBlock(doc({ status: 'FAILED' }), 'retry', busy)).toBe('turn-in-flight');
  });
});

describe('R-71(1) — which 409s can only be the processing lock', () => {
  it('is certain for upload and delete, which have no other 409', () => {
    expect(impliesProcessingLock('upload', null)).toBe(true);
    expect(impliesProcessingLock('delete', doc())).toBe(true);
  });

  it('is certain for retry exactly when the row is FAILED', () => {
    expect(impliesProcessingLock('retry', doc({ status: 'FAILED' }))).toBe(true);
    expect(impliesProcessingLock('retry', doc({ status: 'ACTIVE' }))).toBe(false);
  });

  it('is NEVER certain for replace — which is why the error_code was added', () => {
    // `DuplicateChecksumError` is reachable from ACTIVE and FAILED, the same states Replace is
    // offered from, so no client-side rule can separate them.
    expect(impliesProcessingLock('replace', doc({ status: 'ACTIVE' }))).toBe(false);
    expect(impliesProcessingLock('replace', doc({ status: 'FAILED' }))).toBe(false);
  });
});

describe('NFR-A11Y-05 — what is announced', () => {
  it('announces a status transition', () => {
    expect(announceTransition(doc({ status: 'INDEXING' }), doc({ status: 'ACTIVE' }))).toBe(
      'Q3_Market_Report.pdf is now Ready.',
    );
  });

  it('says nothing for a row it has not seen before', () => {
    // Otherwise opening the modal reads twenty documents aloud, which is the failure mode
    // NFR-A11Y-05's "shall not re-announce unchanged content" is guarding against.
    expect(announceTransition(undefined, doc())).toBeNull();
  });

  it('says nothing when the status did not change', () => {
    expect(announceTransition(doc({ chunk_count: 4 }), doc({ chunk_count: 9 }))).toBeNull();
  });

  it('never announces on the opening snapshot', () => {
    expect(seeded(doc(), doc({ document_id: 'b' })).announcement).toBeNull();
  });
});
