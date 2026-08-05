"""The typed error surface (T-405, R-57).

Every failure this API can report had a `description` in the OpenAPI document and no *shape*.
A generated TypeScript client therefore knew that `POST /messages/{id}/regenerate` can answer
`409` and nothing whatever about what the body contains — which is the one thing a client has
to branch on, since two different `409`s arrive there and they are resolved by different user
actions (R-56(1)).

Three parts, in the order they matter:

1. :class:`ErrorResponse` — the envelope FastAPI produces for an `HTTPException` with a string
   detail, which is ~40 of the ~42 raise sites plus `rate_limit_exceeded_handler`. One model,
   referenced from every declared 4xx/5xx.

2. The two **object**-shaped details. FastAPI wraps whatever is passed as `detail` in
   ``{"detail": ...}``, so the model that describes the wire is the *envelope*, not the payload
   — hence the `…Detail` / `…Response` pairing. The raisers construct these models rather than
   dict literals: a model that merely *describes* a hand-built dict drifts the first time
   someone edits the literal, and the drift is invisible because the schema still validates.

3. The shared ``responses=`` fragments. Nine names, spread at ~20 route sites, replacing the
   nothing that was there before — `auth.py`, `users.py` and `audit.py` declared no `responses=`
   at all, which is the gap T-110 left explicitly to this task.

**What is deliberately *not* here:** `401` and `403`. They apply to every authenticated
operation, and `app/openapi.py` injects them from the `security` block instead — derived from
the truth rather than copied to 24 call sites. See `_declare_auth_failures`.

Kept free of `app.rag.errors`' heavier imports on that module's own principle (its lazy
`openai`/`sqlalchemy` imports exist so schema generation stays cheap); the two `error_code`
constants are the only things imported from it, and a test asserts the `Literal`s here still
equal them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "MISCONFIGURED",
    "PROCESSING_LOCKED",
    "QUOTA_EXCEEDED",
    "RATE_LIMITED",
    "STORAGE_DOWN",
    "TOO_LARGE",
    "UNSUPPORTED_TYPE",
    "UPSTREAM_DOWN",
    "ChatConflictResponse",
    "ContextWindowExceededDetail",
    "ContextWindowExceededResponse",
    "ErrorResponse",
    "NotLatestAnswerDetail",
    "NotLatestAnswerResponse",
    "error_responses",
]


class ErrorResponse(BaseModel):
    """The ordinary error body: ``{"detail": "..."}``.

    Not a bespoke envelope — this *is* what `HTTPException(status, "some copy")` serialises to,
    and re-shaping it now would break every route at once for no gain. Declaring it simply
    stops the generated client typing an error body as `unknown`.

    The copy is frequently a `# TBD(§8.4)` string, so a client must **render** it and never
    match on it. Where a decision hangs on the error, there is a code — see the two models
    below, and R-43(6)'s `FailureClass` on the SSE path.
    """

    detail: str = Field(description="Human-readable copy. Render it; never branch on it.")


class ContextWindowExceededDetail(BaseModel):
    """FR-STA-04's refusal (R-51(5)), on both `send` and `regenerate`.

    A refusal, not an FR-ORC-05 failure: nothing was attempted, so it carries no
    `FailureClass`. The three token counts are here because the GUI's FR-ANL-03 meter is the
    thing the user must act on, and re-fetching the conversation to learn why the composer
    refused would race the very state that refused it.

    `used_tokens` is the conversation's **real** usage on both paths, never the adjusted
    projection a regenerate checks against — a card showing a number the meter never displays
    would read as a bug in the meter.
    """

    error_code: Literal["CONTEXT_WINDOW_EXCEEDED"]
    message: str
    used_tokens: int
    limit_tokens: int
    overflow_tokens: int


class NotLatestAnswerDetail(BaseModel):
    """FR-MSG-08 / R-56(1) — the target is no longer the latest AI answer.

    A **second code** rather than a second copy of the one above, because the two are resolved
    differently: the budget refusal ends the conversation until the user starts a new chat,
    while this one clears the moment the client reloads the transcript. `app/rag/errors.py:99`
    records that the distinction reaches the client only if the codes stay distinct here; the
    `Literal` is what carries it into the generated types.
    """

    error_code: Literal["NOT_LATEST_ANSWER"]
    message: str


class ContextWindowExceededResponse(BaseModel):
    """The wire body. FastAPI wraps `detail`, so the envelope is what a client parses."""

    detail: ContextWindowExceededDetail


class NotLatestAnswerResponse(BaseModel):
    """The wire body for R-56(1)'s refusal."""

    detail: NotLatestAnswerDetail


#: The `409` on `POST /messages/{id}/regenerate`, which can be either.
#:
#: Not a Pydantic discriminated union: the tag lives one level down (`detail.error_code`) and
#: Pydantic cannot discriminate through a field. It costs nothing — TypeScript narrows on the
#: nested literal without help, and this type is never *parsed* by us, only published.
type ChatConflictResponse = ContextWindowExceededResponse | NotLatestAnswerResponse


def error_responses(*entries: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """Build a ``responses=`` mapping of plain :class:`ErrorResponse` bodies.

    A helper rather than eight hand-written dicts so the model reference cannot be forgotten on
    one of them — a `responses={404: {"description": ...}}` with no `model` is exactly the
    half-declared state this task exists to remove, and it looks correct in review.
    """
    return {
        code: {"model": ErrorResponse, "description": description} for code, description in entries
    }


# --- shared fragments -----------------------------------------------------------------
#
# Spread into a route's `responses=` (``responses={**RATE_LIMITED, **UPSTREAM_DOWN}``). Nine
# greppable names beat ~30 copied literals: when the copy or the reason changes, it changes in
# one place, and `grep -n QUOTA_EXCEEDED` enumerates every route that can answer it.

#: NFR-SEC-07 (T-105). Applied to every route carrying a slowapi decorator or dependency;
#: `test_openapi_contract.py` asserts that set matches the limiter's own registry.
RATE_LIMITED = error_responses(
    (429, "NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown.")
)

#: R-28 / T-110's narrowing: `503` means **unreachable**, so a retry is meaningful.
UPSTREAM_DOWN = error_responses((503, "The identity provider is unreachable (R-28)."))

#: T-110's other half — our own credentials or request were wrong, which a retry cannot fix.
#: `500` rather than `403` because these routes are admin-gated and the caller *is* authorized.
MISCONFIGURED = error_responses(
    (500, "Server-side identity configuration error. Check the server logs.")
)

#: R-33 / T-213: a down object-storage daemon, translated at the seam.
STORAGE_DOWN = error_responses((503, "Object storage is unreachable."))

#: R-33(6) — normative status codes; the copy stays a §8.4 TBD.
TOO_LARGE = error_responses((413, "The upload exceeds the per-file size ceiling."))
UNSUPPORTED_TYPE = error_responses((415, "Not a supported document format (FR-KBM-02)."))
QUOTA_EXCEEDED = error_responses((507, "FR-ERR-02 — the caller's storage quota is exhausted."))

#: R-24 / R-43(4) — the processing lock, at exactly the four file verbs it gates.
PROCESSING_LOCKED = error_responses(
    (409, "R-24 — another file action is in flight for this caller.")
)
