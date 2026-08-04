"""One real grounded turn, end to end through the chat route (T-402).

Skipped without `OPENAI_API_KEY`, on the T-304/T-306/T-307 convention. It is here because
every other test of this surface runs with `LLM_BACKEND=fake` and an empty retrieval scope,
so the turn abstains — which exercises the topology, the stream and the persist step, and
proves nothing about the claim T-402 actually makes: **that the citation persisted at
finalization addresses the passage the answer rests on, with that passage's own text**.

The corpus is seeded directly as `document_chunks` rows with real embeddings rather than
uploaded and ingested. That is a deliberate limit and worth stating: nothing here exercises
the parser, the chunker or the worker (they have their own tests). What it does exercise is
router → retrieval → rerank → generate → gate → persist against real models and a real
pgvector index, which is the whole path between a question and a `messages` row.

**A live assertion about model behaviour is a probability, not a property** (T-313's lesson).
So this asserts the *property* — that the answer cites a passage from the seeded corpus and
the persisted quote is that passage's own text — and not that it cites one particular chunk
and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.message import Message
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY

pytestmark = pytest.mark.usefixtures("patch_jwks")

_QUESTION = "How long do I have to return an item for a refund?"

#: One answering passage and two on-topic distractors. The distractors matter: a corpus of
#: one passage cannot tell "the reranker works" from "there was nothing else to pick".
_PASSAGES = [
    "Employees may carry over up to five unused annual leave days into the following year.",
    "Refund requests must be submitted within 30 days of delivery. After 30 days the order "
    "is final and no refund is issued.",
    "Invoices are issued monthly and payment is due within 14 days of the invoice date.",
]
_ANSWERING = 1


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def _live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real models on both seams, and the T-311 overlap off for the harness's sake.

    `conftest` binds every session to one connection so the test can be rolled back, and one
    connection cannot hold the two concurrent savepoints `route`'s `asyncio.gather` opens —
    see `tests/test_chat_api.py::_serial_retrieval`. Prefetching off is the previous release's
    timing, not a degraded mode.
    """
    settings = get_settings()
    if not settings.openai.api_key:
        pytest.skip("OPENAI_API_KEY is empty; live chat test skipped")
    monkeypatch.setattr(settings.embedding, "backend", "openai")
    monkeypatch.setattr(settings.llm, "backend", "openai")
    monkeypatch.setattr(settings.retrieval, "prefetch_query_arm", False)


async def _seed(session: AsyncSession, make_token: Callable[..., str]):  # noqa: ANN202
    """A user with one ACTIVE document of three embedded passages, and an empty chat."""
    from app.services.embeddings import get_embedding_client

    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    user = await UserRepository(session).upsert_from_claims(sub=sub, email=email)
    headers = {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=('user',))}"}

    kb = await KnowledgeBaseRepository(session).get_or_create_default(user.id)
    document = Document(
        owner_id=user.id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="policy.pdf",
        storage_uri=f"s3://corpus/{uuid.uuid4()}.pdf",
        checksum_sha256=_sha(uuid.uuid4().hex),
        status=DocumentStatus.ACTIVE,
        searchable=True,
        current_version=1,
    )
    session.add(document)
    await session.flush()

    vectors = await get_embedding_client().embed_texts(list(_PASSAGES))
    for index, (text, vector) in enumerate(zip(_PASSAGES, vectors, strict=True)):
        session.add(
            DocumentChunk(
                document_id=document.id,
                document_version=1,
                chunk_index=index,
                chunk_hash=_sha(text),
                embedding_fingerprint=_sha(f"fp:{document.id}:{index}"),
                knowledge_base_id=kb.id,
                tenant_id=DEFAULT_TENANT_ID,
                chunk_text=text,
                embedding=list(vector),
                is_active=True,
                meta={"locator": {"kind": "page", "page": index + 1, "label": f"p. {index + 1}"}},
            )
        )
    conversation = Conversation(owner_id=user.id, tenant_id=DEFAULT_TENANT_ID, title="Live")
    session.add(conversation)
    await session.flush()
    return headers, conversation


@pytest.mark.usefixtures("_live")
async def test_live_a_grounded_turn_persists_a_citation_to_the_answering_passage(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The claim no mocked test can make: the chip points at the passage the answer rests on.

    Four things are asserted, and each is a different task's contract meeting here:

    * the turn is `answered`, not abstained — retrieval, rerank and the T-308 gate all agreed;
    * the persisted `content` keeps its `[S<n>]` markers (FR-MSG-06 Rev 0.15, R-48(4));
    * the resolved citation's `quote` is a **seeded passage's own text**, which is what proves
      the persist-time `fetch_chunks` read resolved the marker rather than the model inventing
      a source (R-49(3));
    * `evaluation` is NULL — that column is DeepEval's alone, and the gate's `groundedness`
      must never be written into it (R-49(1), OI-34).
    """
    headers, conversation = await _seed(session, make_token)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    rows = list(
        await session.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.seq)
        )
    )
    assert [row.role for row in rows] == [MessageRole.USER, MessageRole.AI]
    answer = rows[1]

    envelope = answer.citations
    diagnostic = (
        f"\nanswer: {answer.content!r}"
        f"\nsource_ids: {envelope.get(SOURCE_IDS_KEY)}"
        f"\nsegments: {json.dumps(envelope.get(SEGMENTS_KEY), indent=2)[:1200]}"
    )

    citations = [seg for seg in envelope[SEGMENTS_KEY] if seg.get("isCite")]
    assert citations, f"the answer cited nothing, so the turn abstained{diagnostic}"
    assert "[S" in answer.content, f"the raw markers must survive persistence{diagnostic}"
    assert envelope[SOURCE_IDS_KEY], f"the grounding set must be recorded{diagnostic}"

    quotes = {citation["quote"] for citation in citations}
    assert quotes <= set(_PASSAGES), f"a citation quoted text no passage contains{diagnostic}"
    assert _PASSAGES[_ANSWERING] in quotes, (
        f"the answering passage was not among the cited ones{diagnostic}"
    )
    assert citations[0]["doc"] == "policy.pdf"
    assert citations[0]["page"].startswith("p. ")

    assert answer.evaluation is None, "messages.evaluation is DeepEval's alone (R-49(1))"
    assert answer.model_name, "FR-ORC-03's telemetry record is these columns (R-43(5))"
    assert answer.latency_ms is not None

    # T-403 rides on the same turn rather than seeding its own: the mocked feedback suite
    # builds a one-text-run envelope, so this is the only place the route is exercised against
    # a row carrying **real resolved citations**. The T-402 lesson is the reason — a fixture
    # where the interesting field is trivial cannot tell a working route from one that flattens
    # it, and `set_feedback` answers with the whole FR-MSG-06 DTO, `segs` included.
    rated = await client.post(
        f"/api/v1/messages/{answer.id}/feedback", json={"feedback": "up"}, headers=headers
    )
    assert rated.status_code == 200, rated.text
    body = rated.json()
    assert body["feedback"] == "up"
    assert [seg for seg in body["segs"] if seg.get("isCite")], (
        f"rating an answer must not cost it its chips{diagnostic}"
    )
    assert body["evaluation"] is None, "a human thumb is not a judge score (R-49(1), OI-34)"
    await session.refresh(answer)
    assert answer.feedback is Feedback.UP

    # T-404 rides on the same turn for the same reason, one task later: this is the only fixture
    # whose row carries real resolved citations *and* a real thumb, so it is the only place that
    # can show a regenerate replacing a genuinely grounded answer rather than a one-run envelope.
    # Both ids are read *before* the expiry below: `expire_all` marks every loaded instance
    # stale, so a later `answer.id` would try to reload it and raise `MissingGreenlet`.
    answer_id, conversation_id = answer.id, conversation.id
    regenerated = await client.post(f"/api/v1/messages/{answer_id}/regenerate", headers=headers)
    assert regenerated.status_code == 200, regenerated.text

    session.expire_all()  # the replace commits in the graph's own session — see T-404's tests
    rows = list(
        await session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
    )
    assert len(rows) == 2, "a regenerate replaces the row; it must not append a second answer"
    replaced = rows[1]
    assert replaced.id == answer_id
    assert replaced.feedback is None, "the thumb refers to text that no longer exists"
    assert replaced.evaluation is None, "cleared for the FR-EVL-01 job to refill"
    citations = [seg for seg in replaced.citations[SEGMENTS_KEY] if seg.get("isCite")]
    assert citations, f"the replacement lost its chips{diagnostic}"
    assert {citation["quote"] for citation in citations} <= set(_PASSAGES)


@pytest.mark.usefixtures("_live")
async def test_live_a_second_turn_is_answered_on_its_own_grounding(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """T-406, against real models — the symptom, not the mechanism.

    The mocked two-turn tests assert that turn 2 writes *a row of its own*. Only a live turn
    can assert the thing that made this worth stopping T-404 for: that the second row is a
    **different, correct answer to the second question**, grounded in the passage that question
    is about. Before the per-turn reset, `finalize` inherited turn 1's `answer_message_id`,
    skipped its INSERT, and the driver reloaded and served turn 1's refund answer to a question
    about annual leave.

    Asserted as a property, not as an exact citation set (T-313): the leave passage must be
    among the cited ones, and the refund answer must not be what came back.
    """
    headers, conversation = await _seed(session, make_token)
    url = f"/api/v1/conversations/{conversation.id}/messages"

    first = await client.post(url, json={"query": _QUESTION}, headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post(
        url,
        json={"query": "How many unused annual leave days can I carry over?"},
        headers=headers,
    )
    assert second.status_code == 200, second.text

    rows = list(
        await session.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.seq)
        )
    )
    assert [row.role for row in rows] == [
        MessageRole.USER,
        MessageRole.AI,
        MessageRole.USER,
        MessageRole.AI,
    ]
    refunds, leave = rows[1], rows[3]
    diagnostic = f"\nturn 1: {refunds.content!r}\nturn 2: {leave.content!r}"

    assert leave.id != refunds.id, f"the second turn served the first turn's row{diagnostic}"
    assert leave.content != refunds.content, f"the second answer is a copy of the first{diagnostic}"

    quotes = {seg["quote"] for seg in leave.citations[SEGMENTS_KEY] if seg.get("isCite")}
    assert quotes, f"the second turn cited nothing{diagnostic}"
    assert _PASSAGES[0] in quotes, (
        f"the second answer is not grounded in the passage its question is about{diagnostic}"
    )
    assert leave.model_name, "a real turn meters its own generation (R-43(5))"
