"""FR-SYS-02 grounded generation and the FR-MSG-06 answer segments (T-307, R-48).

FR-SYS-02 says the backend "returns answer segments interleaving text and citations"; FR-MSG-06
fixes the wire shape as ``[{text} | {isCite, doc, page, quote, chunkId}]``. Neither says how a
model that writes prose produces that structure, and `RAGState` has exactly one `answer: str`.
R-48 closes the gap. Four things are worth reading before changing anything:

1. **The model writes prose with `[S<n>]` markers; this module splits it.** The markers come
   from `app.rag.prompts.SYSTEM_PROMPT`, which instructs the model to cite with the markers the
   fenced context carries. Asking for JSON instead would put the whole composition inside a
   string field and buy nothing — and a schema cannot express "cite the claim you just made".

2. **`[S<k>]` resolves to `reranked_chunk_ids[k-1]`, and that is a load-bearing invariant**
   (R-48(5)). R-44(7) binds T-308 to resolve citations through the composer's own `source_ids`
   rather than re-deriving them, but nodes communicate only through checkpointed state and a
   `ComposedPrompt` does not survive its node. So :func:`generate_answer` orders its sources to
   match `reranked_chunk_ids` exactly and the caller asserts it — which makes the mapping
   reconstructible from state alone, and makes "re-derived" and "as composed" the same list by
   construction rather than by hope.

3. **Segments carry chunk ids, never chunk text.** `AnswerSegment` is deliberately not the
   FR-MSG-06 wire shape: no `quote`, no `doc`, no `score`. Those are read from the chunk rows
   at persist time (T-402), because a `quote` is document text and FR-PER-03 keeps document
   text out of the checkpoint — and because R-36(6) already makes `messages.citations` the
   denormalised copy the hover card renders.

4. **A marker outside `1..len(source_ids)` is dropped from the text** (R-48(6)). Resolving it
   is what FR-CIT-06(2) forbids; rendering it shows a user a citation chip that addresses
   nothing. Dropping it is the only option that is neither.

**Imports no langgraph**, like `errors.py`, `prompts.py`, `router.py`, `search.py` and
`rerank.py`: `app.rag.graph` calls ``apply_strict_msgpack()`` at import time, and T-308's gate,
T-402's persist step and T-309's evaluation job all reach :func:`split_answer_segments` without
needing that.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import structlog

from app.config import Settings, get_settings
from app.rag.prompts import ComposedPrompt, PromptSource, compose_messages
from app.security.prompt_injection import SOURCE_MARKER_RE
from app.services.llm import ChatClient

log = structlog.get_logger(__name__)

__all__ = [
    "AnswerSegment",
    "GeneratedAnswer",
    "cited_chunk_ids",
    "generate_answer",
    "split_answer_segments",
]

#: Telemetry names. `rag.*` rather than `graph.turn.*`, on the R-45(8) / R-47(6) precedent: the
#: closed `graph.turn.*` vocabulary is a span-pairing contract (R-43(5)) and this is neither end
#: of a span. Counts, codes and metering only — **never** the query, the answer or a filename.
GENERATION_COMPLETED = "rag.generation.completed"
MARKERS_DROPPED = "rag.generation.markers_dropped"


@dataclass(frozen=True, slots=True)
class AnswerSegment:
    """One run of the answer: prose, or a citation.

    ``chunk_id is None`` means plain text. A citation segment keeps the marker's own ``text``
    (``"[S2]"``) so the raw answer can be reconstructed from the segments — which is what lets
    T-308 reject a citation without having to re-split, and T-402 render either the chip or the
    literal text depending on what survives FR-CIT-06.

    Deliberately **not** the FR-MSG-06 wire shape — see this module's docstring, point 3.
    """

    text: str
    chunk_id: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    """One turn's generation, as the node writes it to state.

    ``source_ids`` is the composer's own marker→chunk map (R-44(7)), returned so the caller can
    assert the R-48(5) invariant against `reranked_chunk_ids` rather than trust it.
    """

    text: str
    source_ids: tuple[str, ...]
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def split_answer_segments(
    answer: str, source_ids: Sequence[str]
) -> tuple[list[AnswerSegment], int]:
    """Split ``answer`` into text and citation runs. Pure, never raises.

    Returns the segments and the number of markers **dropped** for addressing a source that was
    not supplied (R-48(6)). The count is telemetry: a model that regularly cites `[S9]` out of
    eight sources is a prompt problem, and it would otherwise be invisible.

    Matching uses `SOURCE_MARKER_RE`, the same pattern `neutralize` uses to defuse a *forged*
    marker inside untrusted text — one definition, pointed in two directions, because a marker
    the composer emits and this misses is a citation silently rendered as prose.

    Whitespace around a dropped marker is collapsed so the answer does not acquire a double
    space where a citation used to be; text segments are never otherwise altered, because the
    answer is what the user reads and FR-MSG-07 renders it as Markdown.
    """
    segments: list[AnswerSegment] = []
    dropped = 0
    cursor = 0
    pending = ""

    def flush() -> None:
        nonlocal pending
        if pending:
            segments.append(AnswerSegment(text=pending))
            pending = ""

    for match in SOURCE_MARKER_RE.finditer(answer):
        pending += answer[cursor : match.start()]
        cursor = match.end()
        index = int(match.group(1))
        if 1 <= index <= len(source_ids):
            flush()
            segments.append(AnswerSegment(text=match.group(0), chunk_id=source_ids[index - 1]))
        else:
            # Out of range — `[S0]`, or `[S9]` against eight sources. Dropped rather than kept:
            # resolving it would attribute a claim to a passage the model was never shown, and
            # rendering it would put a dead chip in front of a user.
            dropped += 1
            if pending.endswith(" ") and answer[cursor : cursor + 1] == " ":
                # `"the claim [S9] and"` would otherwise become `"the claim  and"`. Only the
                # space a removal actually doubled is taken; text the model wrote is not
                # reflowed.
                pending = pending[:-1]
    pending += answer[cursor:]
    flush()
    return segments, dropped


def cited_chunk_ids(segments: Iterable[AnswerSegment]) -> list[str]:
    """The distinct chunks the answer cites, in first-citation order.

    First-citation order rather than the grounding order, because it is the order the reader
    meets them in: FR-MSG-04's source line lists the documents an answer used, and a list that
    followed the reranker's ranking would name a document the answer cites last first.
    """
    seen: dict[str, None] = {}
    for segment in segments:
        if segment.chunk_id is not None:
            seen.setdefault(segment.chunk_id, None)
    return list(seen)


def compose_answer_prompt(
    *,
    query: str,
    sources: Sequence[PromptSource],
    history: Sequence[Mapping[str, str]] = (),
) -> ComposedPrompt:
    """The R-44(3) payload for one turn.

    A one-line pass-through, and that is the point: R-44(7) says T-307 "supplies history and
    makes the model call but does not re-compose the payload". Naming the call here rather than
    inlining it keeps the composer the single owner of the prompt's shape, so a change to the
    fence, the isolation clause or the message order cannot be half-applied.
    """
    return compose_messages(query=query, sources=sources, history=history)


async def generate_answer(
    *,
    query: str,
    sources: Sequence[PromptSource],
    history: Sequence[Mapping[str, str]] = (),
    chat: ChatClient,
    settings: Settings | None = None,
) -> GeneratedAnswer:
    """Compose, stream and buffer one grounded answer. **Raises on every failure** (R-48(2)).

    The fail-closed contract lives in this interface, the mirror image of `rerank_passages`'
    never-raises one and for the same reason R-45(2) gave: a future call site should inherit the
    behaviour rather than have to remember it. Generation is the one stage with no defensible
    degraded output — `route` falls back to `hybrid` and `rerank` to the RRF order, but a
    generator that "fails open" produces an ungrounded answer, which R-23, FR-SYS-02 and
    FR-RET-05 forbid in the same breath.

    The whole answer is buffered before returning: NFR-PRF-02 puts the FR-CIT-06 gate between
    this and the client, so there is nothing to hand out yet. See `AnswerStream` for what
    streaming buys on the provider side.
    """
    settings = settings or get_settings()
    composed = compose_answer_prompt(query=query, sources=sources, history=history)

    started = time.perf_counter()
    stream = await chat.stream_answer(
        composed.messages, max_output_tokens=settings.llm.max_output_tokens
    )
    result = await stream.collect()

    segments, dropped = split_answer_segments(result.text, composed.source_ids)
    if dropped:
        log.warning(MARKERS_DROPPED, dropped=dropped, sources=len(composed.source_ids))
    log.info(
        GENERATION_COMPLETED,
        sources=len(composed.source_ids),
        history_turns=len(history),
        answer_chars=len(result.text),
        cited=len(cited_chunk_ids(segments)),
        markers_dropped=dropped,
        model=result.model,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    return GeneratedAnswer(
        text=result.text,
        source_ids=composed.source_ids,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
