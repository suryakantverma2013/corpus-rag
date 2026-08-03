"""The chat route — send (SSE) and list (T-402, FR-CST-03, FR-MSG-06, §4.16).

Runs the **real** graph against the test transaction, on `LLM_BACKEND=fake` and with no
retriever wired, so a turn abstains (R-23) — which is the honest end-to-end shape for a user
with no documents and exercises every node, the persist step and the stream in one pass. The
grounded-answer path is covered where the grounding lives (`test_graph.py`); what belongs
here is the surface: who may send, what is refused before anything is written, what the
frames carry, and what ends up in `messages`.

Three assertions in this file are load-bearing rather than incidental:

* **`404` for an administrator on a foreign chat** — R-54, which is the whole of OI-33's
  closure.
* **The FR-STA-04 refusal writes no row** — R-51's binding. Asserting the status alone would
  pass on the implementation that blocks a chat with its own unanswered question.
* **`stage` frames carry no query or answer text** — R-43(5) at its hardest, since a frame is
  on the wire.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import MessageRole
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.repositories.users import UserRepository
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY
from app.rag.graph import ABSTAIN_EMPTY_SCOPE

pytestmark = pytest.mark.usefixtures("patch_jwks")

_QUESTION = "what do my documents say about refunds?"


@pytest.fixture(autouse=True)
def _serial_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn off the T-311 query-arm prefetch for this file. **A harness limit, not a product one.**

    `route` runs the router call and the original query's retrieval arm under one
    `asyncio.gather`, so two sessions are open at the same instant. In production each comes
    from the pool with its own connection and that is the entire point of the optimisation;
    here `conftest` binds every session to **one** connection so the whole test can be rolled
    back, and one connection cannot hold two concurrent savepoints — asyncpg fails with
    `InvalidSavepointSpecificationError`, which the graph then correctly reports as
    `RETRIEVAL_UNAVAILABLE`.

    Prefetching off is "the previous release's timing, not a degraded mode" (R-45/T-311), so
    nothing this file asserts changes shape. The concurrency itself is tested where it can be:
    `tests/test_search.py`'s two-party barrier and `tests/test_graph.py`, both on stubs.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings().retrieval, "prefetch_query_arm", False)


# ---- helpers ----


async def _caller(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    roles = ("admin", "user") if admin else ("user",)
    return sub, {"Authorization": f"Bearer {make_token(sub=sub, email=email, roles=roles)}"}


async def _conversation(session: AsyncSession, *, owner_id: uuid.UUID) -> Conversation:
    conversation = Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title="A chat")
    session.add(conversation)
    await session.flush()
    return conversation


def _frames(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into `(event, data)` pairs.

    Hand-rolled rather than pulled from a library: the point of these tests is the wire
    format, and a parser that normalised it away would assert nothing about what a browser
    actually receives.
    """
    out: list[tuple[str, dict]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:") and event is not None:
            out.append((event, json.loads(line.removeprefix("data:").strip())))
            event = None
    return out


async def _messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(rows)


# ---- send: admission ----


async def test_a_foreign_conversation_is_404_even_for_an_administrator(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """**R-54, closing OI-33.** Admin chat impersonation is unreachable, not merely scoped.

    The sharper half of OI-33 is not what an admin's turn would *retrieve* — it is that
    `finalize` persists into the conversation, so the owner would later find a message
    attached to a question they never asked, durable under §4.16 and counted by the FR-ANL
    cards. Refusing the send makes the scope question moot.
    """
    stranger, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    conversation = await _conversation(session, owner_id=stranger)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert await _messages(session, conversation.id) == [], "no row for a refused send"


async def test_an_over_budget_send_is_refused_before_the_question_is_written(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-STA-04 / R-51(5) — a refusal, and it must leave no trace.

    The row-count assertion is the one that matters. Checking only the status would pass on
    the implementation R-51 warns about, where the blocked turn's own unanswered question
    stays in the transcript permanently inflating the meter that blocked it — leaving a chat
    the user can neither use nor recover.
    """
    from app.config import get_settings

    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    monkeypatch.setattr(get_settings().context, "window_tokens", 1)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "CONTEXT_WINDOW_EXCEEDED"
    assert detail["overflow_tokens"] > 0
    assert detail["limit_tokens"] == 1
    assert await _messages(session, conversation.id) == []


async def test_a_blank_query_is_rejected(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": "   "},
        headers=headers,
    )

    assert response.status_code == 422


# ---- send: the stream ----


async def test_a_turn_streams_progress_then_the_verified_answer(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """NFR-PRF-02's ordering as the wire actually carries it.

    `stage` frames report progress and hold no content, because R-48(1)/R-49(3) put the
    FR-CIT-06 gate between generation and the client — there is genuinely nothing of the
    answer to send until it has passed. Then exactly one `message` frame, then `done`.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    assert response.status_code == 200
    frames = _frames(response.text)
    events = [event for event, _ in frames]

    assert events[-1] == "done"
    assert events.count("message") == 1
    assert events.index("message") == len(events) - 2, "the answer is the last thing before done"
    assert "stage" in events

    stages = [data["stage"] for event, data in frames if event == "stage"]
    assert stages == sorted(set(stages), key=stages.index), "a stage is announced once"
    assert stages[0] == "preparing"
    assert "generating" in stages


async def test_stage_frames_carry_no_query_or_answer_text(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-43(5), at its hardest: a frame is on the wire.

    The rule is enforced structurally — `StageEvent` has one field and it is a stage name —
    and asserted here because the structural guarantee is only as good as what the route
    serialises.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    stage_payloads = [data for event, data in _frames(response.text) if event == "stage"]
    assert stage_payloads, "no stage frames to check"
    for payload in stage_payloads:
        assert set(payload) == {"stage"}
        assert "refunds" not in json.dumps(payload)


async def test_the_turn_persists_the_question_and_the_answer_in_order(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """§4.16 / FR-MSG-06, and T-108's ordering.

    With no retriever wired the scope is empty and the turn abstains — which is a *response*
    (R-23), so it is persisted like any other. Ordering is by `seq`, which is what stops the
    answer rendering above the question when both rows share a `created_at`.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    rows = await _messages(session, conversation.id)
    assert [row.role for row in rows] == [MessageRole.USER, MessageRole.AI]
    assert rows[0].content == _QUESTION
    assert rows[1].content == ABSTAIN_EMPTY_SCOPE
    assert rows[0].seq < rows[1].seq
    assert rows[1].latency_ms is not None, "FR-ORC-03's telemetry record is these columns"


async def test_an_abstention_persists_an_empty_grounding_set(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The envelope is written even when nothing was cited — and `evaluation` stays NULL.

    Two rulings meet here. `source_ids` is empty because the served text is *our* copy and no
    `[S<n>]` marker indexes into a grounding set (T-402's `_persist_turn`), which is also what
    makes `workers/evaluate.py` skip the row rather than score an abstention against an empty
    context. And `evaluation` is DeepEval's alone — the gate's `groundedness` must never be
    written into it (R-49(1)/board (c), OI-34 as resolved).
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    answer = (await _messages(session, conversation.id))[1]
    assert answer.citations[SOURCE_IDS_KEY] == []
    assert answer.citations[SEGMENTS_KEY] == [{"text": ABSTAIN_EMPTY_SCOPE}]
    assert answer.evaluation is None
    assert "groundedness" not in json.dumps(answer.citations)


async def test_the_message_frame_matches_the_persisted_row(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """What the client is handed is what the transcript holds — one answer, not two copies.

    A frame composed independently of the row is how a served answer and a reloaded one drift
    apart, which the user sees as the chat changing when they refresh.
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"query": _QUESTION},
        headers=headers,
    )

    frame = next(data for event, data in _frames(response.text) if event == "message")
    row = (await _messages(session, conversation.id))[1]

    assert frame["outcome"] == "abstained"
    assert frame["message"]["id"] == str(row.id)
    assert frame["message"]["role"] == "ai", "the wire value is 'ai', never 'assistant'"
    assert frame["message"]["segs"] == [{"text": ABSTAIN_EMPTY_SCOPE}]


# ---- list ----


async def test_list_returns_the_transcript_in_the_fr_msg_06_shape(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """`segs` is derived from the stored envelope; the raw `[S<n>]` content is not exposed."""
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    chunk_id = str(uuid.uuid4())
    session.add_all(
        [
            Message(conversation_id=conversation.id, role=MessageRole.USER, content="a question"),
            Message(
                conversation_id=conversation.id,
                role=MessageRole.AI,
                content="The window is 30 days [S1].",
                citations={
                    SEGMENTS_KEY: [
                        {"text": "The window is 30 days "},
                        {
                            "isCite": True,
                            "doc": "policy.pdf",
                            "page": "p. 4",
                            "quote": "Refunds are accepted within 30 days.",
                            "chunkId": chunk_id,
                            "score": 0.87,
                        },
                        {"text": "."},
                    ],
                    SOURCE_IDS_KEY: [chunk_id],
                },
                evaluation={"relevancy": 1.0, "faithfulness": 0.9},
                model_name="gpt-4o",
            ),
        ]
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["role"] for row in body] == ["user", "ai"]
    assert body[0]["segs"] == [{"text": "a question"}]
    assert body[1]["segs"][1]["chunkId"] == chunk_id
    assert body[1]["segs"][1]["score"] == 0.87
    assert body[1]["evaluation"] == {"relevancy": 1.0, "faithfulness": 0.9}
    assert body[1]["model_name"] == "gpt-4o"
    assert "content" not in body[1], "the raw [S<n>] markers are not a client's to render"


async def test_a_citation_with_no_score_renders_without_one(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-47(2) / board (d) — the FR-CIT-04 score may legitimately be **absent**.

    The reranker fails open and publishes nothing; the R-46(3) cross-probe RRF score must
    never be substituted, since it is comparable within a turn only. A `0.0` here would read
    to a user as "this passage is irrelevant".
    """
    owner, headers = await _caller(session, make_token)
    conversation = await _conversation(session, owner_id=owner)
    chunk_id = str(uuid.uuid4())
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.AI,
            content="Yes [S1].",
            citations={
                SEGMENTS_KEY: [
                    {"text": "Yes "},
                    {
                        "isCite": True,
                        "doc": "policy.pdf",
                        "page": "p. 4",
                        "quote": "…",
                        "chunkId": chunk_id,
                    },
                ],
                SOURCE_IDS_KEY: [chunk_id],
            },
        )
    )
    await session.flush()

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=headers
    )

    citation = response.json()[0]["segs"][1]
    assert "score" not in citation
    assert citation["chunkId"] == chunk_id


async def test_listing_a_foreign_transcript_is_404(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-54 again, on the read side: an admin does not get to read someone else's chat."""
    stranger, _ = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    conversation = await _conversation(session, owner_id=stranger)

    response = await client.get(
        f"/api/v1/conversations/{conversation.id}/messages", headers=admin_headers
    )

    assert response.status_code == 404
