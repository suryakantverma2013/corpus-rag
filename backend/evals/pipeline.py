"""Running the golden set through the *shipped* retrieval and generation path (T-312).

The point of this module is that it contains no retrieval or generation logic of its own. It
seeds a corpus, drives the real `route → retrieve → rerank → generate → gate` node functions,
and reads the state they leave behind. A harness that reimplemented any of that would be
measuring itself.

Three deliberate departures from `app.rag.graph.build_state_graph`, each for a reason:

* **`govern`, `telemetry_start`, `lock` and `finalize` are absent.** They authorize a caller,
  open a span, take the R-24 gate and persist a `messages` row — none of which is under
  evaluation, and the last of which this harness must never do (R-52: it writes nothing to
  `messages`).
* **No error handler.** `build_state_graph` routes a node exception to `finalize` with a
  failure class, which is right for a user-facing turn and wrong here: a failed item must
  surface as a failed item, not as a plausible-looking `SYSTEM_FAILURE` string that would be
  scored as if it were an answer.
* **`gate` runs but its verdict is not acted on.** `abstain` would replace the answer with
  fixed copy (R-49(8)), and the answer the model actually produced is the thing being judged.
  The verdict is recorded beside the scores instead — which is what makes the R-49 correlation
  computable at all.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.users import User
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.rag.graph import gate, generate, rerank, retrieve, route
from app.rag.state import RAGContext, RAGState
from evals.corpus import GoldenSet, Question

__all__ = ["SeededCorpus", "TurnResult", "build_eval_graph", "run_question", "seed", "teardown"]


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    """What the seed created, and the mapping every later step needs.

    ``chunk_by_passage`` is the reason this is returned rather than discarded: retrieval
    answers in chunk ids and the golden set is written in passage ids, and without the map
    recall@k cannot be computed without guessing.
    """

    owner_id: uuid.UUID
    knowledge_base_id: uuid.UUID
    document_id: uuid.UUID
    chunk_by_passage: dict[str, uuid.UUID]
    passage_by_chunk: dict[uuid.UUID, str]
    text_by_chunk: dict[uuid.UUID, str]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """One item's trip through the pipeline. Scored later; this module never judges."""

    question_id: str
    answer: str = ""
    grounding_chunk_ids: tuple[uuid.UUID, ...] = ()
    cited_chunk_ids: tuple[str, ...] = ()
    gate_verdict: str | None = None
    groundedness: float | None = None
    query_class: str | None = None
    sub_queries: tuple[str, ...] = ()
    error: str | None = None
    context: tuple[str, ...] = field(default_factory=tuple)
    grounding_passage_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: object,
    golden: GoldenSet,
) -> SeededCorpus:
    """Write the authored corpus into `document_chunks` with real embeddings.

    One owner, one GLOBAL knowledge base, one document — the simplest shape that satisfies the
    FR-ORC-06 scope predicate, since the harness is not testing multi-KB isolation (T-305's
    live tests already do, against the same database).
    """
    texts = [p.text for p in golden.passages]
    vectors = await embeddings.embed_texts(texts)  # type: ignore[attr-defined]

    chunk_by_passage: dict[str, uuid.UUID] = {}
    passage_by_chunk: dict[uuid.UUID, str] = {}
    text_by_chunk: dict[uuid.UUID, str] = {}

    async with sessionmaker() as session:
        user = await UserRepository(session).upsert_from_claims(
            sub=uuid.uuid4(), email=f"evals-{uuid.uuid4().hex[:8]}@corpus.test"
        )
        await session.flush()
        kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
        await session.flush()

        doc = Document(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            filename="golden-set.md",
            storage_uri="s3://corpus/evals/golden-set.md",
            checksum_sha256=_sha(uuid.uuid4().hex),
            status=DocumentStatus.ACTIVE,
            searchable=True,
            current_version=1,
        )
        session.add(doc)
        await session.flush()

        for index, (passage, vector) in enumerate(zip(golden.passages, vectors, strict=True)):
            chunk = DocumentChunk(
                document_id=doc.id,
                document_version=1,
                chunk_index=index,
                chunk_hash=_sha(f"{passage.id}:{passage.text}"),
                embedding_fingerprint=_sha(f"fp:{doc.id}:{passage.id}"),
                knowledge_base_id=kb.id,
                tenant_id=DEFAULT_TENANT_ID,
                chunk_text=passage.text,
                embedding=vector,
                is_active=True,
                # The R-35(12) payload the FR-CIT-03 card reads. `block_order` is unique per
                # passage on purpose: R-37(6)'s adjacent-overlap dedupe drops neighbours within
                # one block, and authored passages are not neighbours of each other.
                meta={
                    "locator": passage.locator,
                    "block_order": index,
                    "block_chunk_index": 0,
                    "passage_id": passage.id,
                },
            )
            session.add(chunk)
            await session.flush()
            chunk_by_passage[passage.id] = chunk.id
            passage_by_chunk[chunk.id] = passage.id
            text_by_chunk[chunk.id] = passage.text

        await session.commit()
        return SeededCorpus(
            owner_id=user.id,
            knowledge_base_id=kb.id,
            document_id=doc.id,
            chunk_by_passage=chunk_by_passage,
            passage_by_chunk=passage_by_chunk,
            text_by_chunk=text_by_chunk,
        )


async def teardown(sessionmaker: async_sessionmaker[AsyncSession], corpus: SeededCorpus) -> None:
    """Remove everything :func:`seed` created. Best effort, and always attempted."""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "delete from document_chunks where document_id in "
                "(select id from documents where owner_id = :owner)"
            ),
            {"owner": corpus.owner_id},
        )
        await session.execute(delete(Document).where(Document.owner_id == corpus.owner_id))
        await session.execute(
            delete(KnowledgeBase).where(KnowledgeBase.owner_id == corpus.owner_id)
        )
        await session.execute(delete(User).where(User.id == corpus.owner_id))
        await session.commit()


def build_eval_graph():  # noqa: ANN201 — CompiledStateGraph is generic over four params
    """The shipped nodes, wired start → route → retrieve → rerank → generate → gate → end."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    builder: StateGraph = StateGraph(RAGState, context_schema=RAGContext)
    builder.add_node("route", route)
    builder.add_node("retrieve", retrieve)
    builder.add_node("rerank", rerank)
    builder.add_node("generate", generate)
    builder.add_node("gate", gate)
    builder.add_edge(START, "route")
    builder.add_edge("route", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "generate")
    builder.add_edge("generate", "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=InMemorySaver())


async def run_question(
    graph: object,
    *,
    question: Question,
    corpus: SeededCorpus,
    context: RAGContext,
) -> TurnResult:
    """Run one item and collect what the scorers need.

    The retrieval context is resolved from the seed's own map rather than re-read from the
    database: the harness authored this text, so a second read would only be a chance for the
    two to disagree.
    """
    config = {
        "configurable": {"thread_id": str(context.conversation_id)},
        "recursion_limit": 25,
    }
    try:
        state = await graph.ainvoke(  # type: ignore[attr-defined]
            {"query": question.question, "turn_index": 0},
            config,
            context=context,
            durability="sync",
        )
    except Exception as exc:  # noqa: BLE001 — one failed item must not end the run
        return TurnResult(question_id=question.id, error=f"{type(exc).__name__}: {exc}")

    grounding: tuple[uuid.UUID, ...] = ()
    raw_ids = state.get("reranked_chunk_ids") or []
    parsed: list[uuid.UUID] = []
    for value in raw_ids:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError, AttributeError, TypeError:
            continue
    grounding = tuple(parsed)

    return TurnResult(
        question_id=question.id,
        answer=state.get("answer") or "",
        grounding_chunk_ids=grounding,
        cited_chunk_ids=tuple(str(c) for c in state.get("citation_ids") or ()),
        gate_verdict=state.get("gate_verdict"),
        groundedness=state.get("groundedness"),
        query_class=state.get("query_class"),
        sub_queries=tuple(state.get("sub_queries") or ()),
        context=tuple(
            corpus.text_by_chunk[cid] for cid in grounding if cid in corpus.text_by_chunk
        ),
        grounding_passage_ids=tuple(
            corpus.passage_by_chunk[cid] for cid in grounding if cid in corpus.passage_by_chunk
        ),
    )


def make_context(
    corpus: SeededCorpus,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    embeddings: object,
    chat: object,
    conversation_id: uuid.UUID | None = None,
) -> RAGContext:
    """A fresh context per item — a new `conversation_id` so no history bleeds between items."""
    return RAGContext(
        owner_id=corpus.owner_id,
        tenant_id=DEFAULT_TENANT_ID,
        conversation_id=conversation_id or uuid.uuid4(),
        sessionmaker=sessionmaker,
        embeddings=embeddings,  # type: ignore[arg-type]
        chat=chat,  # type: ignore[arg-type]
    )


def recall_at_k(result: TurnResult, question: Question) -> float | None:
    """Fraction of the authored supporting passages that reached the grounding set.

    Deterministic and free, and it is the control on the two paid metrics: a low Contextual
    Recall with a high recall@k means the judge disagrees with the corpus author, while a low
    one with a low recall@k means retrieval simply did not find the passage. Without it, every
    bad score looks like the same problem.

    ``None`` for a band with no supporting passages — there is nothing to recall.
    """
    if not question.supporting_passage_ids:
        return None
    found = set(result.grounding_passage_ids) & set(question.supporting_passage_ids)
    return len(found) / len(question.supporting_passage_ids)


def cited_passage_ids(result: TurnResult, corpus: SeededCorpus) -> tuple[str, ...]:
    """The passages the answer actually cited, in citation order."""
    out: list[str] = []
    for raw in result.cited_chunk_ids:
        try:
            chunk_id = uuid.UUID(raw)
        except ValueError, AttributeError, TypeError:
            continue
        passage = corpus.passage_by_chunk.get(chunk_id)
        if passage is not None:
            out.append(passage)
    return tuple(out)


def sequence_str(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "—"
