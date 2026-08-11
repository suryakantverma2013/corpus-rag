"""Regenerate endpoint (T-404, FR-MSG-08, R-56).

Runs the **real** graph against the test transaction on the deterministic backends, so the
turn is a genuine re-run rather than a stubbed one. Two corpora, deliberately:

* most tests seed no documents, so the re-run abstains (R-23) — which is the honest shape for
  a user whose knowledge base is empty and exercises every node, the replace and the stream;
* the grounded tests seed a `document_chunks` row, because `_fake_generate` emits `[S<n>]`
  markers whenever the fenced context carries them. That reaches `outcome == "answered"`
  without a key, which is the only way to exercise the evaluation enqueue at all.

Four assertions here are load-bearing rather than routine:

* **The message count does not change.** An append implementation passes a content assertion
  and fails this one — and the count is what FR-STA-04's meter (R-51(4)) and the FR-ANL-01 card
  rest on.
* **A failed re-run leaves every column byte-identical.** This is the test a route-level
  pre-clear of `evaluation` fails: clearing first and then erroring destroys the scores of an
  answer that still exists.
* **`content` and `citations` move together.** T-309 replays `split_answer_segments` against
  `citations.source_ids`, so a row with new text beside an old envelope validates against the
  wrong grounding set *and passes*.
* **The R-24 lock does not refuse this route** (R-55(1) honoured). A published gate for a
  *different* conversation must not turn a regenerate into a `409`.

Assertions are scoped to the caller each test mints (T-109): the suite runs against the shared
local database and nothing truncates it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.enums import DocumentStatus, Feedback, MessageRole
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.message import Message
from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.users import UserRepository
from app.rag import graph as graph_module
from app.rag import telemetry
from app.rag.citations import SEGMENTS_KEY, SOURCE_IDS_KEY
from app.rag.graph import ABSTAIN_EMPTY_SCOPE
from app.services import chat as chat_service

pytestmark = pytest.mark.usefixtures("patch_jwks")

_QUESTION = "What is the refund window?"
_ANSWER = "Refunds are accepted within 30 days."
_PASSAGE = "Refund requests must be submitted within 30 days of delivery."


@pytest.fixture(autouse=True)
def _serial_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn off the T-311 query-arm prefetch. **A harness limit, not a product one.**

    `conftest` binds every session to one connection so the test can be rolled back, and one
    connection cannot hold the two concurrent savepoints `route`'s `asyncio.gather` opens — see
    `tests/test_chat_api.py::_serial_retrieval` for the full argument. Off is the previous
    release's timing, not a degraded mode.
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


async def _turn(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    question: str = _QUESTION,
    answer: str = _ANSWER,
    source_ids: list[str] | None = None,
) -> tuple[Message, Message]:
    """One persisted exchange, with the envelope shape a real answer carries."""
    user = Message(conversation_id=conversation_id, role=MessageRole.USER, content=question)
    session.add(user)
    await session.flush()
    ai = Message(
        conversation_id=conversation_id,
        role=MessageRole.AI,
        content=answer,
        citations={
            SEGMENTS_KEY: [{"text": answer}],
            SOURCE_IDS_KEY: source_ids if source_ids is not None else ["c1"],
        },
        model_name="gpt-4o",
        prompt_tokens=100,
        completion_tokens=20,
        latency_ms=1234,
    )
    session.add(ai)
    await session.flush()
    return user, ai


async def _chat(
    session: AsyncSession, make_token: Callable[..., str], *, admin: bool = False, **turn: object
) -> tuple[uuid.UUID, dict[str, str], Conversation, Message]:
    owner, headers = await _caller(session, make_token, admin=admin)
    conversation = await _conversation(session, owner_id=owner)
    _, answer = await _turn(session, conversation_id=conversation.id, **turn)  # type: ignore[arg-type]
    return owner, headers, conversation, answer


async def _seed_corpus(session: AsyncSession, *, owner_id: uuid.UUID) -> None:
    """One ACTIVE document of one passage, on the deterministic embedding backend.

    Enough for the turn to retrieve, cite and be `answered` — `_fake_generate` emits `[S<n>]`
    whenever the fenced context carries markers — which is what makes the evaluation enqueue
    reachable without an API key.
    """
    from app.services.embeddings import get_embedding_client

    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner_id)
    document = Document(
        owner_id=owner_id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="policy.pdf",
        storage_uri=f"s3://corpus/{uuid.uuid4()}.pdf",
        checksum_sha256=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        status=DocumentStatus.ACTIVE,
        searchable=True,
        current_version=1,
    )
    session.add(document)
    await session.flush()

    (vector,) = await get_embedding_client().embed_texts([_PASSAGE])
    session.add(
        DocumentChunk(
            document_id=document.id,
            document_version=1,
            chunk_index=0,
            chunk_hash=hashlib.sha256(_PASSAGE.encode()).hexdigest(),
            embedding_fingerprint=hashlib.sha256(f"fp:{document.id}".encode()).hexdigest(),
            knowledge_base_id=kb.id,
            tenant_id=DEFAULT_TENANT_ID,
            chunk_text=_PASSAGE,
            embedding=list(vector),
            meta={"locator": {"kind": "page", "page": 1, "label": "p. 1"}},
        )
    )
    await session.flush()


def _url(message_id: uuid.UUID) -> str:
    return f"/api/v1/messages/{message_id}/regenerate"


def _frames(text: str) -> list[tuple[str, dict]]:
    """`(event, payload)` pairs. See `tests/test_chat_api.py::_frames` — since T-405 each
    `data:` line carries the whole frame envelope, and the two event names must agree."""
    out: list[tuple[str, dict]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:") and event is not None:
            frame = json.loads(line.removeprefix("data:").strip())
            assert frame["event"] == event, f"SSE event line {event!r} != payload {frame!r}"
            out.append((event, frame["data"]))
            event = None
    return out


async def _messages(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """The transcript, **re-read from the database rather than from the identity map**.

    A regenerate UPDATEs a row this session already holds, in the graph's own session (R-42(1):
    nodes never take the request's). Without the expiry SQLAlchemy hands back the stale instance
    it loaded before the request and every replacement assertion reads the *old* answer — a
    harness trap the send tests cannot hit, because an INSERT produces a row the identity map
    has never seen.
    """
    session.expire_all()
    rows = await session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(rows)


# ---- replace in place ----


async def test_a_regenerate_replaces_the_row_in_place(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-MSG-08's "replacing the answer" — the row is updated, never appended.

    The **count** is the assertion that matters. An implementation that appended would pass any
    content check while showing the user one question answered twice, inflating the FR-ANL-01
    card and charging the conversation twice against the NFR-CAP-01 budget R-51(4) derives from
    `messages`. `seq` and `created_at` hold for the same reason: `seq` carries display order
    (T-108) and there is no `updated_at`, so moving `created_at` would misreport when the
    exchange happened.
    """
    _, headers, conversation, answer = await _chat(session, make_token)
    before = (answer.id, answer.seq, answer.created_at)

    response = await client.post(_url(answer.id), headers=headers)

    assert response.status_code == 200, response.text
    rows = await _messages(session, conversation.id)
    assert [row.role for row in rows] == [MessageRole.USER, MessageRole.AI], (
        "a regenerate must not append a second answer"
    )
    replaced = rows[1]
    assert (replaced.id, replaced.seq, replaced.created_at) == before
    assert replaced.content == ABSTAIN_EMPTY_SCOPE
    assert replaced.content != _ANSWER


async def test_content_and_citations_are_replaced_together(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The envelope may never outlive the text it describes.

    `workers/evaluate.py` replays `split_answer_segments` against `citations.source_ids` to
    rebuild what the answer was grounded in (R-50(5)). A row carrying new content beside the old
    `source_ids` therefore validates against the **wrong** grounding set and passes — a silent,
    permanent mis-scoring, which is why these two columns are written in one statement.
    """
    _, headers, conversation, answer = await _chat(
        session, make_token, source_ids=["c1", "c2", "c3"]
    )
    assert answer.citations[SOURCE_IDS_KEY] == ["c1", "c2", "c3"]

    await client.post(_url(answer.id), headers=headers)

    replaced = (await _messages(session, conversation.id))[1]
    assert replaced.content == ABSTAIN_EMPTY_SCOPE
    assert replaced.citations[SOURCE_IDS_KEY] == [], "the old grounding set survived the text"
    assert replaced.citations[SEGMENTS_KEY] == [{"text": ABSTAIN_EMPTY_SCOPE}]


async def test_a_regenerate_clears_the_evaluation_and_the_thumb(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Both judgements *about the text* are cleared, and clearing both is the point (R-56).

    `evaluation` must go or the worker's already-evaluated skip drops the re-run before it
    starts (R-50(5)). `feedback` goes with it: 👎 → Regenerate is the common sequence, so a
    carried-forward thumb would attach a negative rating to a new and possibly good answer with
    nothing recording that the text changed. Clearing one and keeping the other leaves the row
    half-stale undetectably.
    """
    _, headers, conversation, answer = await _chat(session, make_token)
    answer.evaluation = {"relevancy": 1.0, "faithfulness": 0.9}
    answer.feedback = Feedback.DOWN
    await session.flush()

    await client.post(_url(answer.id), headers=headers)

    replaced = (await _messages(session, conversation.id))[1]
    assert replaced.evaluation is None
    assert replaced.feedback is None


async def test_the_metric_columns_are_rewritten_for_the_new_answer(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-43(5) makes these columns the FR-ORC-03 telemetry record, so they describe *this* run.

    An abstention calls no model, so it meters nothing — leaving the previous answer's
    `model_name` and token counts in place would report the replacement as having cost what its
    predecessor did. This is the T-406 carry-over defect one layer up, at the row rather than
    the channel.
    """
    _, headers, conversation, answer = await _chat(session, make_token)

    await client.post(_url(answer.id), headers=headers)

    replaced = (await _messages(session, conversation.id))[1]
    assert replaced.model_name is None
    assert replaced.prompt_tokens is None
    assert replaced.completion_tokens is None
    assert replaced.latency_ms is not None, "the turn still happened and still took time"


# ---- refusals ----


async def test_absent_foreign_and_user_role_targets_are_all_404_with_one_copy(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-55(2) at a second route, and the **identity of the copy** is the property.

    Distinguishing "no such message" from "not yours" from "that is your own question" would
    turn the route into a probe for which ids exist and whose they are (NFR-SEC-02). An
    administrator gets the same answer on a foreign chat — R-54(1), no widening.
    """
    _, headers = await _caller(session, make_token)
    _, admin_headers = await _caller(session, make_token, admin=True)
    stranger, _ = await _caller(session, make_token)
    foreign_chat = await _conversation(session, owner_id=stranger)
    _, foreign_answer = await _turn(session, conversation_id=foreign_chat.id)

    mine = await _conversation(session, owner_id=(await _caller(session, make_token))[0])
    question, _ = await _turn(session, conversation_id=mine.id)

    absent = await client.post(_url(uuid.uuid4()), headers=headers)
    foreign = await client.post(_url(foreign_answer.id), headers=headers)
    as_admin = await client.post(_url(foreign_answer.id), headers=admin_headers)
    own_question = await client.post(_url(question.id), headers=headers)

    responses = [absent, foreign, as_admin, own_question]
    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    details = {r.json()["detail"] for r in responses}
    assert len(details) == 1, f"the four refusals must be indistinguishable, got {details}"


async def test_only_the_latest_answer_may_be_regenerated(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """`409` + `NOT_LATEST_ANSWER`, and the superseded row is untouched.

    A `409` rather than the `404` above because the caller owns this row and is looking at it:
    a later turn landed while the action bar was open, which is the "correct client, state moved
    under it" case this surface reserves `409` for. A **distinct code** from
    `CONTEXT_WINDOW_EXCEEDED` because a client resolves them differently — this one by reloading
    the transcript — and T-405's generated types only carry that distinction if it exists here.
    """
    _, headers, conversation, first = await _chat(session, make_token)
    await _turn(
        session, conversation_id=conversation.id, question="and exchanges?", answer="Later."
    )

    response = await client.post(_url(first.id), headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "NOT_LATEST_ANSWER"
    await session.refresh(first)
    assert first.content == _ANSWER, "a refused regenerate must not touch the row"
    assert len(await _messages(session, conversation.id)) == 4


async def test_an_over_budget_regenerate_is_refused_without_touching_the_row(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-STA-04 still applies, and a refusal leaves the answer as it was (R-51(5))."""
    from app.config import get_settings

    _, headers, conversation, answer = await _chat(session, make_token)
    monkeypatch.setattr(get_settings().context, "window_tokens", 1)

    response = await client.post(_url(answer.id), headers=headers)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "CONTEXT_WINDOW_EXCEEDED"
    assert detail["limit_tokens"] == 1
    await session.refresh(answer)
    assert answer.content == _ANSWER


async def test_a_full_chat_can_still_regenerate_its_last_answer(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route must use `check_regeneration`, and this is the only test that can tell.

    `test_budget.py` proves the *function* projects correctly; nothing there reaches the call
    site. Every other test in this file uses a short conversation, where both projections allow
    — so swapping the route back to `check_submission` passes all of them. The limit is chosen
    from the live numbers so the two projections **disagree**: a submission would be refused,
    a regeneration must not be, because it adds no question and its answer replaces one already
    counted. This is precisely the full chat a user reaches for Regenerate in.
    """
    from app.config import get_settings
    from app.rag.budget import conversation_usage

    _, headers, conversation, answer = await _chat(session, make_token, answer="x" * 4_000)
    settings = get_settings()
    usage = await conversation_usage(session, conversation.id, settings=settings)
    reserve = settings.context.answer_reserve_tokens
    # Submission projects `used + reserve` (> limit); regeneration projects
    # `used - old answer + reserve`, which the 1,000-token answer above puts comfortably under.
    monkeypatch.setattr(settings.context, "window_tokens", usage.used_tokens + reserve - 1)

    response = await client.post(_url(answer.id), headers=headers)

    assert response.status_code == 200, (
        f"a full chat could not regenerate: {response.text} — the route is projecting this as a "
        "submission, which double-counts the answer it is about to replace"
    )
    assert (await _messages(session, conversation.id))[1].content == ABSTAIN_EMPTY_SCOPE


async def test_a_regenerate_is_not_refused_while_another_chat_is_generating(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-55(1) honoured, not contradicted — the load-bearing lock test.

    Regenerate *takes* the R-24 gate (the graph's `lock` node does it), which is what pauses the
    caller's document affordances. It must not *check* it at the route: the gate is keyed on the
    **caller** (R-43(1)), so a route-level `409` would refuse a regenerate in one chat because a
    **different** chat of the same user is mid-turn — the precise defect R-55(1) rejected for
    feedback, and exactly the unpredicted `409` OI-31 leaves unspecified.
    """
    owner, headers, conversation, answer = await _chat(session, make_token)
    # A gate the same caller holds for a **different** conversation — the exact situation a
    # route-level check would misread, since `processing_locks` is keyed on `owner_id` alone.
    other_chat = await _conversation(session, owner_id=owner)
    session.add(
        ProcessingLock(
            owner_id=owner,
            conversation_id=other_chat.id,
            token=uuid.uuid4().hex,
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await session.flush()

    response = await client.post(_url(answer.id), headers=headers)

    assert response.status_code == 200, response.text
    replaced = (await _messages(session, conversation.id))[1]
    assert replaced.content == ABSTAIN_EMPTY_SCOPE


# ---- failure ----


async def test_a_failed_re_run_leaves_the_answer_completely_untouched(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-54(3) doing double duty, and the reason `evaluation` clears in the UPDATE.

    `_should_persist` excludes `error`, so the replace is simply never reached and the previous
    answer survives a provider outage intact. A route-level pre-clear of `evaluation` would fail
    this test — it would destroy the scores of an answer that still exists — which is why that
    clearing lives in the same statement as the replacement.
    """

    async def _boom(state: object, runtime: object) -> object:
        raise RuntimeError("screening is down")

    monkeypatch.setattr(graph_module, "screen", _boom)

    _, headers, conversation, answer = await _chat(session, make_token)
    answer.evaluation = {"relevancy": 1.0, "faithfulness": 0.9}
    answer.feedback = Feedback.UP
    await session.flush()

    response = await client.post(_url(answer.id), headers=headers)

    assert response.status_code == 200, "the failure is reported inside the stream"
    frame = next(data for event, data in _frames(response.text) if event == "message")
    assert frame["outcome"] == "error"
    assert frame["error_code"]
    assert frame["message"]["id"] == str(answer.id), (
        "the client already has this bubble; a null id leaves it unable to place the error"
    )

    await session.refresh(answer)
    assert answer.content == _ANSWER
    assert answer.citations[SOURCE_IDS_KEY] == ["c1"]
    assert answer.evaluation == {"relevancy": 1.0, "faithfulness": 0.9}
    assert answer.feedback is Feedback.UP
    assert answer.model_name == "gpt-4o"
    assert len(await _messages(session, conversation.id)) == 2


# ---- the stream ----


async def test_the_stream_has_the_same_shape_as_a_send(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """`stage`* → one `message` → `done`, and the stage frames carry no text (R-43(5)).

    "The same shape as a send" is a contract, not a resemblance: T-508 renders one component for
    both, so a divergence in the event sequence would be a second client-side code path.
    """
    _, headers, _, answer = await _chat(session, make_token)

    response = await client.post(_url(answer.id), headers=headers)

    frames = _frames(response.text)
    events = [event for event, _ in frames]
    assert events[-1] == "done"
    assert events.count("message") == 1
    assert events.index("message") == len(events) - 2
    assert "stage" in events

    for event, data in frames:
        if event == "stage":
            assert set(data) == {"stage"}
            assert "refund" not in json.dumps(data).lower()


async def test_the_turn_index_is_the_original_turns_not_the_transcript_length(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """FR-ORC-03 correlates one exchange's logs by `turn_index`.

    `count_by_conversation` now returns 2 for this conversation, so re-deriving the index from it
    would file the regenerated turn under an index no turn ever had — and the send that created
    this row logged 0.
    """
    _, headers, _, answer = await _chat(session, make_token)

    with structlog.testing.capture_logs() as logs:
        await client.post(_url(answer.id), headers=headers)

    starts = [entry for entry in logs if entry["event"] == telemetry.TURN_START]
    assert [entry["turn_index"] for entry in starts] == [0]


async def test_the_completion_is_recorded_with_no_payload_text(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """`chat.regenerate.completed` — outside the closed `graph.turn.*` set (R-43(5), T-403's shape).

    Asserted against the actual question and answer strings rather than against a key set: a
    future kwarg carrying text would slip past a key-set assertion, which is the failure mode
    the rule exists to prevent.
    """
    owner, headers, conversation, answer = await _chat(session, make_token)

    with structlog.testing.capture_logs() as logs:
        await client.post(_url(answer.id), headers=headers)

    recorded = [entry for entry in logs if entry["event"] == chat_service.REGENERATE_COMPLETED]
    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["message_id"] == str(answer.id)
    assert entry["conversation_id"] == str(conversation.id)
    assert entry["owner_id"] == str(owner)
    assert entry["turn_index"] == 0
    assert entry["outcome"] == "abstained"

    rendered = " ".join(str(value) for value in entry.values())
    assert _QUESTION not in rendered
    assert _ANSWER not in rendered


# ---- re-evaluation ----


async def test_an_identical_regeneration_is_still_a_new_evaluation_job(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug whose only symptom is "no chip, ever, on some messages" (T-404).

    `evaluation_idempotency_key` hashes the answer text, and its docstring used to claim that
    made a regenerated answer a new job. It does not: the deterministic backend reproduces the
    answer **byte for byte**, arq drops the duplicate, and because the UPDATE cleared
    `evaluation` the message stays unscored for ever. Nothing else in the suite can see it —
    which is why this test seeds a corpus so the turn is `answered` and the enqueue is reached.
    """
    from app.services.jobs import NullJobQueue

    keys: list[str] = []

    async def _record(self: object, *, message_id: uuid.UUID, idempotency_key: str) -> None:
        keys.append(idempotency_key)

    monkeypatch.setattr(NullJobQueue, "enqueue_evaluate", _record)

    owner, headers, conversation, answer = await _chat(session, make_token)
    await _seed_corpus(session, owner_id=owner)

    first = await client.post(_url(answer.id), headers=headers)
    replaced = (await _messages(session, conversation.id))[1]
    assert first.status_code == 200, first.text
    assert replaced.content != ABSTAIN_EMPTY_SCOPE, "the corpus must make this turn answerable"

    second = await client.post(_url(answer.id), headers=headers)
    assert second.status_code == 200, second.text
    reran = (await _messages(session, conversation.id))[1]

    assert reran.content == replaced.content, "the deterministic backend must repeat itself here"
    assert len(keys) == 2, "both regenerations must enqueue"
    assert keys[0] != keys[1], (
        "an identical regeneration produced an identical key, so the second job is dropped as a "
        "duplicate and the message stays unscored for ever"
    )
