/**
 * Reading `{"detail": ...}` — the one error envelope the API has, in the one place that parses it.
 *
 * R-57(4) fixes the shape: every failure is `{"detail": "<copy>"}`, and the conflicts a client
 * must *branch* on carry an object instead — `{"detail": {"error_code": "...", "message": ...}}`.
 * The discriminator therefore lives one level down, which is why this is a function and not a
 * type narrowing: Pydantic could not discriminate through a nested field, so the union is not
 * tagged at the top level on the wire either.
 *
 * **Nothing here trusts the body.** An error can arrive from a proxy, a server one revision
 * behind, or a network failure with no body at all, so every branch is checked and the result is
 * two nullable strings. A caller renders `message` and switches on `errorCode`; it never does the
 * reverse (R-57(4): render `detail`, never match on it — the copy is `TBD(§8.4)`).
 *
 * Shared by `kb/mutations.ts` and `chat/mutations.ts`. It was the KB modal's private helper until
 * the chat surface needed the same two `409`s read the same way; two parsers is how one surface
 * starts recognising `PROCESSING_LOCKED` and the other stops.
 */

export interface ErrorDetail {
  /** The server's own copy, when it sent any. Rendered verbatim; never matched on. */
  readonly message: string | null;
  /** The stable code a client branches on — `PROCESSING_LOCKED`, `NOT_LATEST_ANSWER`, … */
  readonly errorCode: string | null;
}

const NOTHING: ErrorDetail = { message: null, errorCode: null };

export function detailOf(error: unknown): ErrorDetail {
  if (typeof error !== 'object' || error === null || !('detail' in error)) return NOTHING;
  const { detail } = error as { detail: unknown };
  if (typeof detail === 'string') return { message: detail, errorCode: null };
  if (typeof detail === 'object' && detail !== null && 'error_code' in detail) {
    const { error_code: code, message } = detail as { error_code: unknown; message?: unknown };
    return {
      message: typeof message === 'string' ? message : null,
      errorCode: typeof code === 'string' ? code : null,
    };
  }
  // A 422's `detail` is an array of validation objects. Falling through to nulls is deliberate:
  // that array must never reach a user, so there is nothing here worth extracting.
  return NOTHING;
}
