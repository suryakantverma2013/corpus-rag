"""FR-ORC-05 / FR-ERR-04 failure classes and their user-facing copy (T-302, R-43).

FR-ERR-04 asks for "a per-failure-class descriptive message", with
``"System Failure: Please try again."`` as "the last-resort fallback for unclassified
errors". Before T-302 the graph wrote ``type(exc).__name__`` into ``RAGState.error_code`` —
a Python class name, which is neither a class of failure nor something a user can act on,
and which put internal type names into a durable store.

**A failure class exists only if it changes what the user should do.** That admission test
is what keeps this set from growing to match the exception tree, and what justifies
exceeding the two classes FR-ERR-04 names by exactly two.

This module deliberately **imports no langgraph**. `app.rag.graph` calls
``apply_strict_msgpack()`` at import time, and T-402's chat route, T-505's error display and
T-604's telemetry sink all need a copy string without triggering that.
"""

from __future__ import annotations

import enum

__all__ = [
    "ABSTAIN_LOW_GROUNDEDNESS",
    "ACCESS_DENIED",
    "ACCESS_DENIED_CODE",
    "BLOCKED_INJECTION",
    "CONTEXT_WINDOW_EXCEEDED",
    "CONTEXT_WINDOW_EXCEEDED_CODE",
    "FAILURE_COPY",
    "SYSTEM_FAILURE",
    "FailureClass",
    "classify",
    "copy_for",
]

#: FR-ORC-02, verbatim from the §4.12 pseudocode and §9's literal-values table — normative,
#: not a §8.4 TBD.
ACCESS_DENIED = "Error: Access Denied"

#: The code that carries it. A *governance outcome*, not a failure class: the turn did
#: exactly what it should, and folding it into :class:`FailureClass` would make
#: ``outcome == "error"`` mean two different things.
ACCESS_DENIED_CODE = "ACCESS_DENIED"

#: FR-ERR-04's named last resort. Also normative copy.
SYSTEM_FAILURE = "System Failure: Please try again."

#: NFR-SEC-05 / R-44(5) — what a turn blocked by the T-303 injection screen answers.
#:
#: It lives here rather than in `app.rag.prompts` because this module already holds every
#: user-facing *outcome* string (`ACCESS_DENIED` is likewise not a `FailureClass`), so T-402's
#: persist step and T-505's display have one import for all of them.
#:
#: R-43(6) asserted that injection-`blocked` has "[its] own copy and routing"; until T-303 the
#: `abstain` node answered `SYSTEM_FAILURE` on that branch, which FR-ERR-04 reserves for
#: *unclassified errors* — a blocked prompt is neither unclassified nor an error.
#:
#: The copy deliberately **names no rule and echoes no part of the query**: telling an attacker
#: which pattern fired turns each attempt into a probe, and echoing the input would render
#: attacker-chosen text back into the transcript. `RAGState.injection_rule` carries the code
#: for operators; the user gets an instruction they can act on.
#:
#: There is no `error_code` on this path, matching the abstain path, which also sets none:
#: `injection_verdict`/`injection_rule` are already the discriminators, and a third name for
#: the same fact would be one more thing to keep in step.
BLOCKED_INJECTION = (  # TBD(§8.4)
    "I can't answer that as written — it reads as an attempt to change how I work rather "
    "than as a question about your documents. Try rephrasing it as a question."
)

#: FR-STA-04 / NFR-CAP-01 — what a refused submission answers when the conversation is full.
#:
#: **This is a refusal, not an FR-ORC-05 failure**, so it takes no `FailureClass`: R-43(6)
#: fixed that set at five and admits a member only where the class changes what the user
#: should *do*, and nothing here failed — the request was never attempted. It is the same
#: shape as the R-24 processing lock's `409`: a state the caller resolves by acting.
#:
#: The code is separate from the copy because the two have different owners. **`OI-26 (c)`
#: is open** — the escape path and the blocked-chat wording are GUI questions belonging to
#: T-505 — so this string is provisional and deliberately minimal, while the code is stable
#: and is what T-402's response and the GUI branch on. Copy may be rewritten without
#: touching a client; the code may not.
#:
#: Names no number: the limit is a `# TBD(§8.4)` knob, and a user cannot act on "10,400"
#: any more than on the groundedness score `ABSTAIN_LOW_GROUNDEDNESS` withholds.
CONTEXT_WINDOW_EXCEEDED_CODE = "CONTEXT_WINDOW_EXCEEDED"
CONTEXT_WINDOW_EXCEEDED = (  # TBD(§8.4) — copy owned by T-505 via OI-26(c)
    "This conversation has reached its length limit. Start a new chat to keep going — your "
    "documents and their answers stay where they are."
)

#: FR-RET-05 / R-49 — what a turn answers when the FR-CIT-06(4) groundedness gate rejects it.
#:
#: It lives beside `BLOCKED_INJECTION` for that constant's reason, and sets **no `error_code`**
#: for the same one: an abstention is a decision, not a failure, and `groundedness`/
#: `gate_reason` are already the discriminators an operator reads.
#:
#: **This copy replaces the rejected answer rather than accompanying it**, which is the whole
#: point. Serving the model's own text under an `abstained` outcome would be strictly worse
#: than serving it as an answer: the user would read ungrounded prose carrying no citations
#: *and* an honest-looking label. The cost is real and accepted — when the model declined in
#: its own words we lose that phrasing — because telling a decline from a fabrication needs
#: exactly the semantic read R-49 declines to make before serving.
#:
#: Deliberately does not name a threshold or a score: the number is diagnostic, it moves with
#: a §8.4 knob, and it would invite the user to argue with it rather than rephrase.
ABSTAIN_LOW_GROUNDEDNESS = (  # TBD(§8.4)
    "I couldn't ground an answer to that in your documents — what I found doesn't support a "
    "reliable response, and I'd rather say so than guess. Try rephrasing the question, or "
    "check that the document you have in mind has finished processing."
)


class FailureClass(enum.StrEnum):
    """The closed FR-ORC-05 set. Stored as its ``.value`` in ``RAGState.error_code``.

    ``error_code`` stays ``str | None`` rather than being retyped to this enum: retyping a
    checkpointed field is a breaking change under R-42(2), and an old checkpoint holding a
    string this enum no longer knows must still load.
    """

    #: FR-ERR-04's "LLM processing error".
    LLM_ERROR = "LLM_ERROR"
    #: FR-ERR-04's "knowledge-base connection timeout", widened to the whole grounding path
    #: (vector store, FTS, query embedding) — FR-RET-05 names the same failure again as
    #: "if the vector store is unavailable, return a graceful error and do not fabricate".
    RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
    #: Provider throttling past the SDK's own retry budget. Earns its own class because
    #: "wait a moment" is a different instruction from "something broke" — and it is the
    #: single most likely production failure.
    RATE_LIMITED = "RATE_LIMITED"
    #: `GRAPH_NODE_TIMEOUT_SECONDS` fired. Different instruction again ("ask something
    #: narrower"), and a different cause from a provider error.
    TIMEOUT = "TIMEOUT"
    #: Unclassified. FR-ERR-04's fallback, reached by construction rather than by omission.
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


#: Every string here is # TBD(§8.4) — §8.4 lists "per-failure-class error messages" as an
#: open copy item. `SYSTEM_FAILURE`'s is the one the spec fixes.
FAILURE_COPY: dict[FailureClass, str] = {
    FailureClass.LLM_ERROR: (  # TBD(§8.4)
        "I couldn't finish generating an answer. Please try again."
    ),
    FailureClass.RETRIEVAL_UNAVAILABLE: (  # TBD(§8.4)
        "I couldn't reach your documents just now, so I can't ground an answer. "
        "Please try again shortly."
    ),
    FailureClass.RATE_LIMITED: (  # TBD(§8.4)
        "The service is busy right now — wait a moment and try again."
    ),
    FailureClass.TIMEOUT: (  # TBD(§8.4)
        "That took too long to answer. Try asking something more specific."
    ),
    FailureClass.SYSTEM_FAILURE: SYSTEM_FAILURE,
}

# Subsystem codes → class. Reuses the `code: ClassVar[str]` those exception trees already
# carry, so the mapping is by contract rather than by exception type.
#
# Embedding failures collapse into RETRIEVAL_UNAVAILABLE on purpose: the operator's fix
# differs (a provider key vs a database), but from the user's seat "we couldn't search your
# documents" is one instruction. The distinguishing `exc.code` still rides on the
# `graph.turn.failure` log event — few user-facing classes, rich operator detail.
_CODE_TO_CLASS: dict[str, FailureClass] = {
    "EMBEDDING_FAILED": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_CONFIG": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_AUTH": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_REJECTED": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_INPUT_INVALID": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_DIMENSION_MISMATCH": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_RESPONSE_INVALID": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_UNAVAILABLE": FailureClass.RETRIEVAL_UNAVAILABLE,
    "EMBEDDING_RATE_LIMITED": FailureClass.RATE_LIMITED,
    # `app.services.llm` (T-304). These matter for T-307, not for the router: R-45(2) makes
    # the router fail open, so its exceptions never reach `classify`. They are mapped here
    # because *translating* an SDK error is what stops it being an `openai.OpenAIError`, so
    # without these entries the fall-through below would file every generation failure as
    # SYSTEM_FAILURE — the same defect T-302 fixed for `type(exc).__name__`.
    "CHAT_FAILED": FailureClass.LLM_ERROR,
    "CHAT_CONFIG": FailureClass.LLM_ERROR,
    "CHAT_AUTH": FailureClass.LLM_ERROR,
    "CHAT_REJECTED": FailureClass.LLM_ERROR,
    "CHAT_REFUSED": FailureClass.LLM_ERROR,
    "CHAT_RESPONSE_INVALID": FailureClass.LLM_ERROR,
    "CHAT_UNAVAILABLE": FailureClass.LLM_ERROR,
    "CHAT_RATE_LIMITED": FailureClass.RATE_LIMITED,
}


def classify(exc: BaseException) -> FailureClass:
    """Map a node exception to its FR-ORC-05 class. Never raises, never returns ``None``.

    ``asyncio.CancelledError`` is **not** handled here and must not be: it is a
    `BaseException`, it is what a client disconnect and a shutdown look like, and filing it
    as `TIMEOUT` would report an abandoned turn as a product failure. langgraph's per-node
    ``timeout=`` surfaces as `TimeoutError`, which is what the first branch catches.
    """
    if isinstance(exc, TimeoutError):
        return FailureClass.TIMEOUT

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _CODE_TO_CLASS:
        return _CODE_TO_CLASS[code]

    # Imported lazily so this module stays importable (and cheap) for consumers that only
    # want the copy map — T-405's OpenAPI generation among them.
    import openai
    import sqlalchemy.exc

    if isinstance(exc, openai.RateLimitError):
        return FailureClass.RATE_LIMITED
    if isinstance(exc, openai.APITimeoutError):
        return FailureClass.TIMEOUT
    if isinstance(exc, openai.OpenAIError):
        return FailureClass.LLM_ERROR
    if isinstance(exc, sqlalchemy.exc.SQLAlchemyError):
        # The graph reaches the database only to authorize and to ground, and neither is
        # something the user can act on beyond retrying.
        return FailureClass.RETRIEVAL_UNAVAILABLE
    return FailureClass.SYSTEM_FAILURE


def copy_for(code: str | None) -> str:
    """The message for a stored ``error_code``. Unknown or absent → the FR-ERR-04 fallback.

    Tolerates a code this build does not know, because a checkpoint written by an older
    deploy can resume against a newer one.
    """
    if code == ACCESS_DENIED_CODE:
        return ACCESS_DENIED
    try:
        return FAILURE_COPY[FailureClass(code)]
    except ValueError:
        return SYSTEM_FAILURE
