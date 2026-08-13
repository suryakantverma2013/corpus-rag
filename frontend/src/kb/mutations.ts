/**
 * FR-KBM-05/07's four verbs, and the one place their answers are classified.
 *
 * The calls are thin; `classify` is the substance and is pure, so every documented status is
 * table-testable without a transport. Two rules run through all of it:
 *
 * - **Branch on `response.status`, never on `data`.** R-33/R-39(2)/R-40(1) all chose `200` over
 *   `409` to say "nothing was queued, and that is fine" — a duplicate upload, an already-deleted
 *   document, identical replacement bytes. Those are *successes* with a different meaning, and
 *   the status is what carries it. (`data.duplicate` also only became readable in Rev 0.38, when
 *   the four `200`s finally declared a model.)
 * - **Copy is the server's.** Every rejection renders `detail` verbatim; these strings are
 *   `TBD(§8.4)` and R-57(4) says render them, never match on them. A second copy here would
 *   drift silently, and a test asserts none of them appears in this folder.
 */
import { api } from '../api';
import type { DocumentStatus, UploadScope } from '../api';

/**
 * What happened, in the modal's vocabulary rather than HTTP's.
 *
 * `gone` is deliberately not a rejection: a `404` means the store is stale — the row was deleted
 * in another tab, or the purge finished between the render and the click — so the right response
 * is to drop the row, not to tell the user off for clicking what we were still showing them.
 */
export type Outcome =
  | { kind: 'accepted'; documentId: string; status: DocumentStatus }
  | { kind: 'duplicate'; documentId: string; status: DocumentStatus }
  | { kind: 'gone' }
  /** R-24's gate (R-71(1)) — reconciled, never rendered as an error. */
  | { kind: 'locked'; detail: string }
  | { kind: 'refused'; detail: string; status: number }
  | { kind: 'invalid' }
  | { kind: 'unauthorized' };

/** The `409` code R-71(1) added so the lock is distinguishable from the other conflicts. */
export const PROCESSING_LOCKED = 'PROCESSING_LOCKED';

interface RawResult {
  status: number;
  /** The parsed error body, when there was one. */
  error?: unknown;
  data?: { document_id?: string; status?: DocumentStatus } | undefined;
}

/**
 * The server's answer, classified.
 *
 * `fallbackDetail` is what a caller renders when the body carried none — a network failure, or a
 * status the server documented without copy. It is never used to *replace* a `detail` the server
 * did send.
 */
export function classify(result: RawResult, fallbackDetail: string): Outcome {
  const { status } = result;
  const detail = detailOf(result.error);

  if (status === 200 || status === 202) {
    const documentId = result.data?.document_id;
    const documentStatus = result.data?.status;
    // A 2xx with no readable body is still a success; there is simply nothing to apply. It cannot
    // happen against a current server (Rev 0.38 declared the models) and is not worth an error.
    if (documentId === undefined || documentStatus === undefined) {
      return { kind: 'refused', detail: fallbackDetail, status };
    }
    return {
      kind: status === 200 ? 'duplicate' : 'accepted',
      documentId,
      status: documentStatus,
    };
  }

  if (status === 401) return { kind: 'unauthorized' };
  if (status === 404) return { kind: 'gone' };
  // 422 is a client bug — a mis-spelled form field, not something the user did. `detail` is an
  // array of validation objects and must never be rendered.
  if (status === 422) return { kind: 'invalid' };
  if (status === 409 && detail.errorCode === PROCESSING_LOCKED) {
    return { kind: 'locked', detail: detail.message ?? fallbackDetail };
  }
  return { kind: 'refused', detail: detail.message ?? fallbackDetail, status };
}

/** Read `{detail: string}` and `{detail: {error_code, message}}` without trusting either. */
function detailOf(error: unknown): { message: string | null; errorCode: string | null } {
  if (typeof error !== 'object' || error === null || !('detail' in error)) {
    return { message: null, errorCode: null };
  }
  const { detail } = error as { detail: unknown };
  if (typeof detail === 'string') return { message: detail, errorCode: null };
  if (typeof detail === 'object' && detail !== null && 'error_code' in detail) {
    const { error_code: code, message } = detail as { error_code: unknown; message?: unknown };
    return {
      message: typeof message === 'string' ? message : null,
      errorCode: typeof code === 'string' ? code : null,
    };
  }
  return { message: null, errorCode: null };
}

/**
 * Multipart, hand-built.
 *
 * `openapi-typescript` types a binary part as `string`, so the generated body type cannot hold a
 * `File`. Rather than cast the file into that shape at four call sites, the serializer owns the
 * whole conversion and the casts stay in one function.
 */
function multipart(fields: Record<string, string | File | undefined>): FormData {
  const form = new FormData();
  for (const [name, value] of Object.entries(fields)) {
    if (value !== undefined) form.append(name, value);
  }
  return form;
}

const NETWORK = 'Could not reach the server. Check your connection and try again.'; // TBD(§8.4)

/** Run a call and normalise a thrown transport failure into the same `Outcome` shape. */
async function attempt(run: () => Promise<RawResult>): Promise<Outcome> {
  try {
    return classify(await run(), NETWORK);
  } catch {
    // `status: 0` is not a real status and is never rendered — `refused` carries the copy. It
    // exists so a caller that wants to distinguish "offline" from "the server said no" can.
    return { kind: 'refused', detail: NETWORK, status: 0 };
  }
}

export function uploadDocument(
  file: File,
  scope: UploadScope,
  conversationId: string | null,
): Promise<Outcome> {
  return attempt(async () => {
    const { data, error, response } = await api.POST('/api/v1/documents', {
      body: multipart({
        file,
        scope,
        conversation_id: scope === 'chat' && conversationId !== null ? conversationId : undefined,
      }) as never,
      bodySerializer: (body: unknown) => body as FormData,
    });
    return { status: response.status, error, data };
  });
}

export function retryDocument(documentId: string): Promise<Outcome> {
  return attempt(async () => {
    const { data, error, response } = await api.POST('/api/v1/documents/{document_id}/retry', {
      params: { path: { document_id: documentId } },
    });
    return { status: response.status, error, data };
  });
}

export function replaceDocument(documentId: string, file: File): Promise<Outcome> {
  return attempt(async () => {
    const { data, error, response } = await api.POST('/api/v1/documents/{document_id}/replace', {
      params: { path: { document_id: documentId } },
      body: multipart({ file }) as never,
      bodySerializer: (body: unknown) => body as FormData,
    });
    return { status: response.status, error, data };
  });
}

export function deleteDocument(documentId: string): Promise<Outcome> {
  return attempt(async () => {
    const { data, error, response } = await api.DELETE('/api/v1/documents/{document_id}', {
      params: { path: { document_id: documentId } },
    });
    return { status: response.status, error, data };
  });
}

/** `GET /documents` — the closed-modal set behind FR-SBR-05's count and FR-CMP-04's rows. */
export async function listDocuments() {
  const { data, error, response } = await api.GET('/api/v1/documents', {
    params: { query: { limit: 500 } },
  });
  if (error !== undefined || data === undefined) {
    throw new Error(`documents list failed: ${response.status}`);
  }
  return data;
}
