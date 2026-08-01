"""FR-CIT-06(4) groundedness measurement and the FR-RET-05 retry lever (T-308, R-49).

FR-CIT-06 asks five things of a citation before an answer is served. **Four of them are
already discharged when this module runs** (R-48, T-308 board line (b)): `generate`'s
fail-closed `fetch_chunks` read-back goes through the *same* `RetrievalFilter` `retrieve`
built, so (1) the chunk exists, (2) it was in the context passed, (3) the caller is
authorized and (5) the document is still ACTIVE are settled for the whole grounding set
before the first token — and an out-of-range `[S<n>]` is already dropped from the text
(R-48(6)), so only resolvable citations reach here. Re-running any of those would be a
second construction of the FR-ORC-06 scope, which is exactly the R-46(2) hole reopened one
node later (R-47(5)).

That leaves check **(4)**, "the cited passage supports the associated claim", and R-49 rules
it is measured **structurally**: *supported claim sentences ÷ substantive claim sentences*,
computed from the answer and `reranked_chunk_ids` alone. No chunk read, no model call.

**This is a recorded deviation, not a claim to have implemented (4) semantically** — the
R-37(2) (`ts_rank_cd` is not BM25) and R-47(1) (this is not a cross-encoder) disposition, third
instance. Why, in the order the reasons carry weight:

1. **T-309 already ships the semantic judge, and it is the same judge.** DeepEval Faithfulness
   (FR-EVL-01) is precisely "does the retrieved context support the answer", OpenAI-judged
   (R-15), post-hoc, rendered per message (FR-MSG-04). A pre-serve judge would measure one
   property twice from one vendor — and when the two disagreed the product would show a user a
   gate-approved answer carrying a 0.58 faithfulness chip.
2. **A judge's latency is unhideable by construction**, because it *is* the thing gating the
   stream: ~1.5 s on an already ≈5–6 s wait, and ~13 s before a retried turn abstains.
3. **A judge would have to fail closed** — "we could not verify it, here it is" is not a
   degradation of FR-CIT-06 but its absence — which puts one provider blip in a position to
   fail every turn at the last node, after all upstream cost is sunk.
4. **A judge is injectable and arguable.** R-47(4) recorded exactly that for the reranker and
   drew the binding that nothing may threshold on its number; a judge reading the same fenced
   untrusted passages inherits the limitation at a *serve/don't-serve* call site.

**Named limitation** (R-44(2) / R-47(4) form — what the control does *not* stop): coverage
measures *that the model cited*, not *that the passage supports*. An answer citing a real,
in-scope, ACTIVE chunk for a wrong claim passes here. The dominant failure mode is caught,
because "answered from pre-training" has an observable signature — nothing to cite, score
0.0 — and the residual mode is the one T-309 exists to surface. **Revisit instrument: T-309.**
Once faithfulness lands on `messages.evaluation`, correlating it against coverage is the
evidence for whether a pre-serve judge earns its 1.5 s. Do not guess it before then.

**Binds T-402:** `groundedness` is *not* the FR-EVL faithfulness chip. `messages` has no
groundedness column and `evaluation` is DeepEval's; conflating them would launder a structural
proxy as a semantic score.

**Imports no langgraph**, like `errors.py`, `prompts.py`, `router.py`, `search.py`, `rerank.py`
and `generation.py`: `app.rag.graph` calls ``apply_strict_msgpack()`` at import time, and
T-402's persist step and T-309's evaluation job reach this module without needing that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.rag.generation import AnswerSegment, cited_chunk_ids

__all__ = [
    "GroundednessReport",
    "assess",
    "hypothetical_probe",
]

#: Telemetry names. `rag.*` rather than `graph.turn.*` on the R-45(8) / R-47(6) / R-48
#: precedent: the closed `graph.turn.*` vocabulary is a span-pairing contract (R-43(5)) and
#: this is neither end of a span. Counts, codes and the score only — **never** the query, the
#: answer, a passage or a filename.
GATE_COMPLETED = "rag.gate.completed"
GATE_RETRY = "rag.gate.retry"
GATE_ABSTAINED = "rag.gate.abstained"
GATE_ADAPTED = "rag.gate.adapted"

#: A sentence ends at terminal punctuation followed by whitespace or the end of the line.
#: Requiring whitespace after is what makes `3.5%` and `v1.2` cost nothing to handle — the
#: period there is followed by a digit, so it never matches.
_BOUNDARY_RE = re.compile(r"""[.!?]+["')\]]*(?=\s|$)""")

#: The token immediately before a candidate boundary, used to veto it.
_TOKEN_TAIL_RE = re.compile(r"[A-Za-z][A-Za-z.]*$")

#: Vetoes, deliberately biased toward **not** splitting. A missed boundary merges two
#: sentences, and a citation anywhere in the merged span then supports both — which errs
#: toward passing. A false boundary does the opposite: it invents an uncited claim out of half
#: a sentence and fails a healthy answer. R-44's rule that a control which refuses real
#: questions is worse than no control applies to the metric as much as to the threshold.
_ABBREVIATIONS = frozenset(
    {
        "al",
        "approx",
        "cf",
        "dr",
        "eg",
        "esp",
        "etc",
        "fig",
        "ie",
        "inc",
        "jr",
        "ltd",
        "mr",
        "mrs",
        "ms",
        "no",
        "prof",
        "sr",
        "st",
        "vs",
    }
)

#: Leading markdown furniture: heading hashes, block quotes, bullets, ordered-list markers.
#: Stripped only when **counting words**, never from the buffer — offsets have to stay true
#: or a citation attaches to the wrong sentence.
_DECORATION_RE = re.compile(r"^(?:[>#*+\-•]+[ \t]*|\d+[.)][ \t]+)+")

#: What counts as a word for the substantive-claim filter.
_WORD_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z'’-]*")

#: Fenced code blocks are not claims. A ``` line toggles the state and is itself skipped.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


@dataclass(frozen=True, slots=True)
class GroundednessReport:
    """One answer's FR-CIT-06(4) measurement. Pure data — the node decides the verdict.

    ``score`` is the FR-RET-05 groundedness figure written to `RAGState.groundedness`:
    supported claim sentences over substantive ones, in ``[0.0, 1.0]``. ``reason`` is a
    **code**, never prose, because `RAGState.gate_reason` says so.
    """

    score: float
    claims: int
    supported: int
    citations: int
    citation_ids: tuple[str, ...] = ()
    markers_dropped: int = 0
    reason: str | None = None


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans over ``text`` as ``(start, end)`` offsets. Never raises.

    Lines are the outer unit, because in Markdown they already are one: a heading, a bullet
    and a table row are each a self-contained statement whether or not they end in a period,
    and FR-MSG-07 renders this text as Markdown. Fenced code blocks are skipped entirely —
    a code block is not a claim that wants a citation.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        start = position
        position += len(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        lead = len(line) - len(line.lstrip())
        content_start = start + lead
        content_end = start + len(line.rstrip())
        if content_end <= content_start:
            continue
        content = text[content_start:content_end]

        cursor = 0
        for match in _BOUNDARY_RE.finditer(content):
            if not _is_boundary(content, match.start()):
                continue
            end = match.end()
            if end > cursor:
                spans.append((content_start + cursor, content_start + end))
            cursor = len(content) - len(content[end:].lstrip())
        if cursor < len(content):
            spans.append((content_start + cursor, content_end))
    return spans


def _is_boundary(content: str, at: int) -> bool:
    """Is the terminal punctuation at ``at`` a real sentence end?"""
    token_match = _TOKEN_TAIL_RE.search(content, 0, at)
    if token_match is None:
        return True
    token = token_match.group(0)
    # `U.S`, `e.g` — an internal dot means an initialism or an abbreviation, either of which
    # is far more often mid-sentence than not.
    if "." in token:
        return False
    if len(token) == 1:  # `J. Smith`
        return False
    return token.lower() not in _ABBREVIATIONS


def _is_claim(sentence: str, min_words: int) -> bool:
    """Does this sentence assert enough to need a citation?

    The filter is what keeps the score sane rather than what makes it lenient: "In summary:",
    "Both documents cover this." and a bare heading are legitimately uncited, and counting
    them as unsupported claims would fail healthy answers on their connective tissue.
    """
    stripped = _DECORATION_RE.sub("", sentence).strip()
    return len(_WORD_RE.findall(stripped)) >= min_words


def assess(
    segments: Sequence[AnswerSegment],
    *,
    markers_dropped: int = 0,
    settings: Settings | None = None,
) -> GroundednessReport:
    """Measure FR-CIT-06(4) over already-split segments. **Pure, and never raises.**

    Takes segments rather than the raw answer deliberately: `split_answer_segments` is the
    one parser (R-48(4)), and re-splitting here is precisely how the chip a user hovers stops
    matching the passage the gate approved. The never-raises contract lives in this signature
    on the `parse_scores` / `split_answer_segments` precedent, so a future call site inherits
    it instead of having to remember a `try`.

    **A citation covers the run of claims ending at its marker**, back to the previous citation
    or the start of its block — not the one sentence it touches. That is the prose citation
    convention, and it is what `SYSTEM_PROMPT` actually elicits: measured live on `gpt-4o`
    (T-308, 2026-07-31), a model given eight chunks writes two or three sentences drawn from
    one passage and puts a single marker at the end of the paragraph or list. A per-sentence
    rule scored those **0.25–0.5** and would have abstained on 2 of 12 answers that were
    perfectly grounded — the false-positive failure R-44 calls worse than no control at all.

    The discrimination that matters survives: an answer citing **nothing** still scores 0.0
    (the fabrication signature), and claims written *after* the last marker in a block are
    still uncovered, so "cite once, then ramble" is still caught. What the rule concedes is
    that it cannot tell a grounded paragraph carrying one trailing marker from a fabricated one
    carrying the same — which is the module docstring's named limitation, restated at the level
    where it actually bites, and T-309's job rather than this one's.

    A marker before any prose at all attaches **forward** to the first claim of its block —
    there is nothing behind it — but only when nothing precedes it in that block, or a marker
    on an uncited heading would vouch for the paragraph below it.
    """
    settings = settings or get_settings()
    min_words = settings.gate.min_claim_words

    buffer: list[str] = []
    offsets: list[int] = []
    length = 0
    for segment in segments:
        if segment.chunk_id is None:
            buffer.append(segment.text)
            length += len(segment.text)
        else:
            # The marker itself is not prose the reader sees, so it stays out of the buffer —
            # but its position in the prose is the whole measurement.
            offsets.append(length)
    text = "".join(buffer)

    citation_ids = tuple(cited_chunk_ids(segments))
    spans = [
        span for span in _sentence_spans(text) if _is_claim(text[span[0] : span[1]], min_words)
    ]

    supported = _cover(spans, offsets, text)

    claims = len(spans)
    citations = len(offsets)
    if claims:
        score = len(supported) / claims
    else:
        # No substantive claims at all — a one-line answer, a bare heading, an empty string.
        # Citing something is the honest signal here; citing nothing is the fabrication
        # signature and scores 0.0 exactly as an uncited paragraph would.
        score = 1.0 if citations else 0.0

    if not citations:
        reason = "no_citations"
    elif not claims:
        reason = "no_claims"
    elif len(supported) < claims:
        reason = "partial_coverage"
    else:
        reason = None

    return GroundednessReport(
        score=score,
        claims=claims,
        supported=len(supported),
        citations=citations,
        citation_ids=citation_ids,
        markers_dropped=markers_dropped,
        reason=reason,
    )


def _block_starts(text: str) -> list[int]:
    """Offset of the first line of each block, where a blank line ends one.

    Blocks are the unit a citation's reach is bounded by. A paragraph and a bulleted list are
    each one block, so a marker at the end of either covers it — and cannot reach back across
    a blank line into the paragraph before, which is a different point being made.
    """
    starts = [0]
    position = 0
    blank = False
    for line in text.splitlines(keepends=True):
        if blank and line.strip():
            starts.append(position)
            blank = False
        elif not line.strip():
            blank = True
        position += len(line)
    return starts


def _cover(spans: Sequence[tuple[int, int]], offsets: Sequence[int], text: str) -> set[int]:
    """Indices of the claim spans the citations at ``offsets`` support.

    **A citation covers every claim in its own block that starts at or before it.** The rule
    reads as "back to the previous citation *or* the start of the block", and an earlier draft
    tracked the previous citation explicitly — mutation testing showed that bookkeeping could
    be deleted with no test noticing, because it is genuinely redundant: the two bounds always
    select the same set, since anything an earlier citation covered lies inside this one's
    reach too and `supported` is a set. The block bound alone is the rule; the extra state only
    implied a distinction that is not there.
    """
    supported: set[int] = set()
    if not spans:
        return supported

    blocks = _block_starts(text)
    for offset in offsets:
        block_start = max((start for start in blocks if start <= offset), default=0)
        reach = [index for index, (start, _) in enumerate(spans) if start <= offset]
        if not reach:
            # Nothing precedes it in the claim list. Attach forward only when nothing precedes
            # it in the block either — otherwise a marker on an uncited heading would vouch
            # for the paragraph below it.
            if not text[block_start:offset].strip():
                supported.add(0)
            continue
        # When every preceding claim sits in an earlier block — a marker alone on its own line
        # after a blank one — cover only the nearest rather than reaching across the break.
        in_block = [index for index in reach if spans[index][0] >= block_start]
        supported.update(in_block or reach[-1:])
    return supported


def hypothetical_probe(
    segments: Iterable[AnswerSegment], *, settings: Settings | None = None
) -> str | None:
    """The FR-RET-05 retry lever: the rejected answer, as a HyDE passage (R-49).

    **The rejected answer is a hypothetical document.** That is what makes `adapt` a real
    "modified retrieval strategy" for zero model calls: `route` does not re-run on the back
    edge and `retrieve_for_turn` reads only `query` and `sub_queries`, so changing `strategy`
    alone would be a no-op dressed as a modification — while embedding an *answer* retrieves
    different neighbours than embedding a *question*, which is the whole premise of HyDE and
    exactly the use R-45(3) already sanctioned ("HyDE's passage is an ordinary probe").

    Returns ``None`` when the text is too short to be worth a probe. When the rejected answer
    was itself a decline the probe is junk, and that is bounded rather than fatal: probes are
    additive (R-45(3)), a derived probe may fail without failing the turn (R-46(6)), and
    `build_probes` drops one equal to the query.
    """
    settings = settings or get_settings()
    text = " ".join(segment.text for segment in segments if segment.chunk_id is None)
    probe = " ".join(text.split())[: settings.router.max_probe_chars].strip()
    return probe if len(probe) >= settings.gate.min_probe_chars else None
