"""The route-level authorization matrix (T-602, NFR-SEC-01/02).

R-76(3) scopes this task to *who may call what*; `tests/scenarios/test_scope.py::S08` owns the
complementary claim about the retrieval predicate, and neither restates the other.

Three tiers, all generated from `ROUTE_DECISIONS`:

* **A — the gate sweep.** Every secured route refuses every unauthenticated class. Cheap,
  exhaustive, and the tier that catches a route shipped without a gate.
* **B — the role matrix.** The five administrator routes refuse a non-administrator.
* **C — the ownership matrix.** The tier that carries the rulings, including the pair most
  likely to be "harmonised" by a later reader: an administrator gets `404` on another user's
  *conversation* (R-54, closing OI-33) and `200` on another user's *document* (FR-USR-04).

**The vacuity rule this module is built around.** A `404` for a foreign resource passes
identically whether the ownership predicate refused it or the row was simply never there — so
a cell driven at a random UUID proves nothing, and would keep passing after the predicate was
deleted. Every cell expecting `404` therefore drives a row that **exists and belongs to
someone else**, and asserts a positive control: the owner reaching the same URL. Ordering is
part of it — the foreign call comes first, because a control that deletes the row would make
the very next assertion vacuous again. This is §8.65(5)'s lesson applied before the fact
rather than discovered by mutation afterwards.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pymupdf
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.models.conversation import Conversation
from app.db.models.document import Document, DocumentStatus
from app.db.models.document_figure import DocumentFigure
from app.db.models.knowledge_job import JobStatus, JobType, KnowledgeJob
from app.db.models.message import Message, MessageRole
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.services.object_storage import artifact_key
from tests.security import (
    ROUTE_DECISIONS,
    Gate,
    Owns,
    Principal,
    RouteDecision,
    expected_status,
    nfr,
    owned_routes,
    secured_routes,
)
from tests.security.conftest import Actor, call, fill_path, seed_user

pytestmark = pytest.mark.usefixtures("patch_jwks")

_SECURED = tuple(secured_routes())
_ADMIN_ROUTES = tuple(d for d in ROUTE_DECISIONS if d.gate is Gate.ADMIN)
_OWNED = tuple(owned_routes())


def _ident(decision: RouteDecision) -> str:
    return f"{decision.method} {decision.path}"


# --- Tier A: the gate sweep -----------------------------------------------------------


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", _SECURED, ids=_ident)
async def test_an_anonymous_caller_is_refused_by_every_secured_route(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], decision: RouteDecision
) -> None:
    """NFR-SEC-01's "every request", asserted over the whole surface rather than route by route.

    The request carries **no body**. That is not a shortcut: FastAPI resolves security
    dependencies before body validation, so a `401` here is positive evidence that nothing
    downstream ran — not validation, not the ownership predicate, not the rate limiter, not
    the handler. A route that answered `422` first would be reporting the shape of a payload
    to an unauthenticated caller.
    """
    response = await call(client, decision, actors[Principal.ANONYMOUS])

    assert response.status_code == 401, (
        f"{_ident(decision)} answered {response.status_code} to an anonymous caller. "
        f"The manifest declares it {decision.gate}: {decision.why}"
    )
    # The `HTTPBearer(auto_error=False)` consequence recorded in `app/openapi.py` — our own
    # 401 carries the challenge header, and nothing else in the suite asserts it.
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", _SECURED, ids=_ident)
async def test_a_token_whose_local_user_row_is_gone_is_refused(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], decision: RouteDecision
) -> None:
    """A valid, unexpired, correctly signed token for a `sub` with no `users` row.

    This is the state an administrator's `DELETE /users/{id}` leaves a live session in, and it
    is a per-route fact rather than a global one: it is enforced in `get_current_user`, so a
    route resolving `CurrentPrincipal` without `CurrentUser` would skip it silently while
    every one of that route's own tests stayed green.
    """
    response = await call(client, decision, actors[Principal.NO_LOCAL_ROW])
    assert response.status_code == 401, f"{_ident(decision)} accepted a token with no user row"


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", _SECURED, ids=_ident)
async def test_a_deactivated_account_is_refused_by_every_secured_route(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], decision: RouteDecision
) -> None:
    """The immediate half of revocation, swept across the surface.

    The actor carries the `admin` claim deliberately. Driven against an administrator route a
    caller with no role would be refused anyway, and the cell would pass without ever reaching
    the `is_active` check it names — the exact shape of §8.65(5)'s two vacuous scenarios. With
    the claim present the only remaining reason for a refusal is deactivation, and the copy is
    asserted to prove it.
    """
    response = await call(client, decision, actors[Principal.DEACTIVATED])

    assert response.status_code == 403, (
        f"{_ident(decision)} answered {response.status_code} to a disabled account"
    )
    assert response.json()["detail"] == "User account is disabled", (
        "the refusal came from somewhere other than the `is_active` check — most likely the "
        "role gate, which would make this cell vacuous"
    )


@nfr("NFR-SEC-01")
@pytest.mark.parametrize(
    "principal",
    [Principal.MALFORMED, Principal.WRONG_SIGNATURE, Principal.EXPIRED],
)
async def test_an_unusable_token_is_refused(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], principal: Principal
) -> None:
    """The three token-rejection classes, against one representative route.

    Deliberately not swept. All three collapse into a single production statement —
    `get_principal` translating `TokenValidationError` into `401` — and `tests/test_auth.py`
    already unit-tests each rejection at the token layer. Replicating them across thirty-five
    routes would add a hundred parameters and no information; what is worth asserting here is
    that the translation reaches the HTTP surface at all.
    """
    decision = next(d for d in _SECURED if d.name == "me")
    response = await call(client, decision, actors[principal])

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@nfr("NFR-SEC-01")
@pytest.mark.parametrize(
    "decision", [d for d in ROUTE_DECISIONS if d.gate is Gate.PUBLIC], ids=_ident
)
async def test_the_public_routes_do_not_demand_a_credential(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], decision: RouteDecision
) -> None:
    """The mirror of the sweep, and the reason the public list has to be justified per row.

    Without this, `Gate.PUBLIC` would be an unfalsifiable escape hatch: marking a route public
    would remove it from the sweep and assert nothing in exchange. These routes may answer all
    sorts of things to an empty request — `401` for bad credentials at `/auth/login`, `303` at
    the link callback, `503` from a readiness probe — but never `401 Not authenticated`, which
    is the one answer that would mean the gate is on.
    """
    response = await call(client, decision, actors[Principal.ANONYMOUS])

    if response.status_code == 401:
        assert response.json().get("detail") != "Not authenticated", (
            f"{_ident(decision)} is declared public but demands a bearer token: {decision.why}"
        )


# --- Tier B: the role matrix ----------------------------------------------------------


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", _ADMIN_ROUTES, ids=_ident)
async def test_a_non_administrator_is_refused_by_every_administrator_route(
    client: httpx.AsyncClient, actors: dict[Principal, Actor], decision: RouteDecision
) -> None:
    """FR-USR-07, as a table rather than as four inline assertions in one file.

    The caller's local row is seeded, so the gate resolves to a **role** refusal rather than a
    missing-user `401` — without that the cell would pass for the wrong reason.
    """
    response = await call(client, decision, actors[Principal.STRANGER])

    assert response.status_code == 403, (
        f"{_ident(decision)} answered {response.status_code} to a non-administrator"
    )
    assert response.json()["detail"] == "Administrator role required"


# --- Tier C: the ownership matrix -----------------------------------------------------


def _png() -> bytes:
    """A real 1x1 PNG, rendered rather than pasted as a hex blob.

    It must genuinely be a PNG: the positive control asserts the media type the route
    declares, and a placeholder would make that assertion vacuous. Built with the same
    library `render_figure` uses, so "what this route serves" and "what the pipeline
    produces" cannot drift into different formats.
    """
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1, 1))
    # **`clear_with` is not tidiness.** A bare `Pixmap` allocates its sample buffer without
    # initialising it, so two calls encode different pixels and the "same bytes" assertion
    # below fails against a route that is working perfectly. `render_figure` never hits this:
    # `get_pixmap` fills every sample from the page.
    pixmap.clear_with(255)
    return pixmap.tobytes("png")


@dataclass(frozen=True, slots=True)
class Owned:
    """One seeded resource per kind, all owned by `owner`."""

    owner: Actor
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    document_id: uuid.UUID
    job_id: uuid.UUID
    #: The content-derived id of a figure of `document_id`, with its raster really in the
    #: bucket — so the figure route's owner cell is a `200` for the right reason and its
    #: foreign cell is a `404` attributable to ownership rather than to an absent row.
    figure_sha256: str


@pytest.fixture
async def owned(session: AsyncSession, make_token: Callable[..., str], object_store: Any) -> Owned:
    """A conversation, an AI answer, an ACTIVE document and a job — all one owner's.

    Every Tier C cell drives one of these. They exist so a foreign caller's `404` is
    attributable to the ownership predicate rather than to an absent row.
    """
    owner = await seed_user(session, make_token)
    assert owner.id is not None

    conversation = Conversation(owner_id=owner.id, tenant_id=DEFAULT_TENANT_ID, title="Owned")
    session.add(conversation)
    await session.flush()

    question = Message(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content="What does the handbook say?",
    )
    session.add(question)
    await session.flush()
    answer = Message(
        conversation_id=conversation.id,
        role=MessageRole.AI,
        content="It says nothing in particular.",
    )
    session.add(answer)

    kb = await KnowledgeBaseRepository(session).get_or_create_default(owner.id)
    document = Document(
        owner_id=owner.id,
        knowledge_base_id=kb.id,
        tenant_id=DEFAULT_TENANT_ID,
        filename="handbook.pdf",
        storage_uri="file:///owned",
        checksum_sha256=uuid.uuid4().hex * 2,
        size_bytes=16,
        status=DocumentStatus.ACTIVE,
        current_version=1,
        searchable=True,
    )
    session.add(document)
    await session.flush()

    figure_png = _png()
    figure_sha256 = hashlib.sha256(figure_png).hexdigest()
    figure_key = artifact_key(
        tenant_id=DEFAULT_TENANT_ID,
        knowledge_base_id=kb.id,
        document_id=document.id,
        version=1,
        name=f"figures/{figure_sha256}.png",
    )
    await object_store.put(figure_key, figure_png, content_type="image/png")
    session.add(
        DocumentFigure(
            document_id=document.id,
            document_version=1,
            page_number=1,
            figure_index=0,
            content_sha256=figure_sha256,
            storage_uri=object_store.uri_for(figure_key),
            caption="FIGURE 1",
            bbox_x0=10.0,
            bbox_y0=20.0,
            bbox_x1=110.0,
            bbox_y1=140.0,
            width_px=100,
            height_px=120,
            byte_size=len(figure_png),
        )
    )
    await session.flush()

    job = KnowledgeJob(
        document_id=document.id,
        job_type=JobType.INGEST,
        status=JobStatus.SUCCEEDED,
        progress=100,
        attempt_count=1,
        document_version=1,
        idempotency_key=f"ingest:{document.id}:v1",
    )
    session.add(job)
    await session.flush()

    return Owned(
        owner=owner,
        conversation_id=conversation.id,
        message_id=answer.id,
        document_id=document.id,
        job_id=job.id,
        figure_sha256=figure_sha256,
    )


def _url(decision: RouteDecision, owned: Owned) -> tuple[str, dict[str, str] | None]:
    """The URL addressing `owned`'s resource for this route, plus any query scope."""
    match decision.owns:
        case Owns.CONVERSATION:
            return fill_path(decision.path, conversation_id=owned.conversation_id), None
        case Owns.MESSAGE:
            return fill_path(decision.path, message_id=owned.message_id), None
        case Owns.DOCUMENT:
            # `content_sha256` is filled for every document route; `fill_path` substitutes only
            # the templates a path actually carries, so it reaches the figure route alone.
            return (
                fill_path(
                    decision.path,
                    document_id=owned.document_id,
                    content_sha256=owned.figure_sha256,
                ),
                None,
            )
        case Owns.JOB:
            return fill_path(decision.path, job_id=owned.job_id), None
        case Owns.CHAT_SCOPE:
            # Not a path parameter: the scope is named by a query parameter, and the same
            # ownership check applies to it (`resolve_scope_kb_id`).
            return decision.path, {"scope": "chat", "conversation_id": str(owned.conversation_id)}
    raise AssertionError(decision.owns)


#: The positive control for a route whose *success* is an endless SSE stream.
#:
#: `httpx.ASGITransport` accumulates a whole response before returning it, so a successful
#: call to one of these hangs forever — recorded by T-210 at `test_document_events.py`'s
#: `_open_stream`, and it is a property of the transport rather than of the route. Each
#: substitute checks the **same ownership predicate against the same resource** and returns a
#: finite body, which is all the control has to establish: that the row exists and its owner
#: can reach it. Driving the stream itself would prove nothing extra and cannot be done here.
_CONTROL_SIBLING: dict[str, str] = {
    # Both resolve the chat scope through `resolve_scope_kb_id`.
    "stream_documents": "list_documents",
    # Both take `{conversation_id}` through `_owned_or_404`.
    "send_message": "list_messages",
    # Both take `{message_id}` through `MessageRepository.get_owned`.
    "regenerate_message": "set_feedback",
}


def _control_for(decision: RouteDecision) -> RouteDecision:
    sibling = _CONTROL_SIBLING.get(decision.name)
    if sibling is None:
        return decision
    return next(d for d in ROUTE_DECISIONS if d.name == sibling)


def _files(decision: RouteDecision) -> dict[str, tuple[str, bytes, str]] | None:
    """A multipart payload for the one owned route that demands a file.

    Required rather than optional: FastAPI validates the form before the ownership check runs,
    so a `replace` with no file answers `422` and the cell would never reach the predicate it
    names. The bytes are a minimal real PDF — the route sniffs magic bytes (R-33(5)) — and are
    distinct from the seeded document's checksum, or an identical-bytes replace would answer
    `200 duplicate` (R-40(1)) instead of the `202` the widening claim expects.
    """
    if decision.name != "replace_document":
        return None
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    return {"file": ("replacement.pdf", pdf, "application/pdf")}


def _body(decision: RouteDecision) -> dict[str, object] | None:
    """The smallest body that gets past validation for the routes that require one."""
    match decision.name:
        case "rename_conversation":
            return {"title": "renamed"}
        case "set_feedback":
            return {"feedback": "up"}
        case "regenerate_message":
            return {}
        case "send_message":
            return {"query": "hello"}
    return None


@nfr("NFR-SEC-01", "NFR-SEC-02")
@pytest.mark.parametrize("decision", _OWNED, ids=_ident)
async def test_a_stranger_cannot_reach_another_users_resource(
    client: httpx.AsyncClient,
    actors: dict[Principal, Actor],
    owned: Owned,
    decision: RouteDecision,
) -> None:
    """Every owned route answers `404` — never `403` — to a caller who does not own the row.

    `403` would confirm the id exists, turning each of these routes into an existence oracle
    (NFR-SEC-02). The positive control at the end is what makes the `404` mean anything: it
    proves the row was there and reachable, so the refusal came from the ownership predicate
    and not from an empty table. It runs *after* the foreign call deliberately — several of
    these routes mutate, and a control that deleted the row first would restore exactly the
    vacuity it exists to remove.
    """
    expected = expected_status(decision, Principal.STRANGER)
    assert expected == 404, "every owned route refuses a stranger with 404 (NFR-SEC-02)"

    url, params = _url(decision, owned)
    response = await call(
        client,
        decision,
        actors[Principal.STRANGER],
        path=url,
        params=params,
        json=_body(decision),
        files=_files(decision),
    )

    assert response.status_code == 404, (
        f"{_ident(decision)} answered {response.status_code} to a stranger; "
        f"the manifest cites: {decision.why}"
    )

    control_route = _control_for(decision)
    control_url, control_params = _url(control_route, owned)
    control = await call(
        client,
        control_route,
        owned.owner,
        path=control_url,
        params=control_params,
        json=_body(control_route),
        files=_files(control_route),
    )
    assert control.status_code != 404, (
        f"the owner also got 404 from {_ident(control_route)}, so the stranger's 404 says "
        "nothing about the ownership predicate — the resource was unreachable for both of them"
    )


@nfr("NFR-SEC-01")
@pytest.mark.parametrize(
    "decision",
    [d for d in _OWNED if d.admin_widens is False],
    ids=_ident,
)
async def test_an_administrator_is_refused_a_foreign_conversation(
    client: httpx.AsyncClient,
    actors: dict[Principal, Actor],
    owned: Owned,
    decision: RouteDecision,
) -> None:
    """R-54(1)/OI-33 — no administrator widening on the chat surface, and a `404` at that.

    The deciding argument was never retrieval scope: `finalize` would persist a turn into
    someone else's durable transcript, which §4.16 makes exportable and the FR-ANL cards count.
    Paired with the widening test below, which asserts the opposite for documents — the two
    together are what stop a later reader making them consistent.
    """
    url, params = _url(decision, owned)
    response = await call(
        client,
        decision,
        actors[Principal.ADMIN],
        path=url,
        params=params,
        json=_body(decision),
        files=_files(decision),
    )

    assert response.status_code == 404, (
        f"{_ident(decision)} widened for an administrator; R-54 refuses it: {decision.why}"
    )

    control_route = _control_for(decision)
    control_url, control_params = _url(control_route, owned)
    control = await call(
        client,
        control_route,
        owned.owner,
        path=control_url,
        params=control_params,
        json=_body(control_route),
        files=_files(control_route),
    )
    assert control.status_code != 404, "the owner could not reach it either — vacuous cell"


@nfr("NFR-SEC-01")
@pytest.mark.parametrize(
    "decision",
    [d for d in _OWNED if d.admin_widens and d.admin_foreign is not None],
    ids=_ident,
)
async def test_an_administrator_reaches_another_users_document(
    client: httpx.AsyncClient,
    actors: dict[Principal, Actor],
    owned: Owned,
    decision: RouteDecision,
) -> None:
    """FR-USR-04 / R-39(1) — the widening that is *not* a defect, asserted as a status.

    These are the cells that prove the matrix distinguishes two rulings rather than blanket
    404-ing everything: the same administrator who is refused a foreign conversation reaches a
    foreign document. No positive control is needed here — a success **is** the proof the row
    existed.
    """
    url, params = _url(decision, owned)
    response = await call(
        client,
        decision,
        actors[Principal.ADMIN],
        path=url,
        params=params,
        json=_body(decision),
        files=_files(decision),
    )

    assert response.status_code == decision.admin_foreign, (
        f"{_ident(decision)} answered {response.status_code}; {decision.why}"
    )


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("method", ["patch", "delete"])
async def test_the_self_mutation_refusal_is_the_status_the_contract_declares(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    openapi_document: dict,
    method: str,
) -> None:
    """R-62(2)'s admin-lockout guard, and R-77(2)'s contract rule, in one cell.

    An administrator may not demote, disable or delete **themselves**, which is what makes zero
    administrators unreachable sequentially — the actor always survives their own request.

    This is also the cell that catches the defect R-77(2) records, and it exists because the
    document-level guard in `test_completeness.py` provably did **not**: mutation testing showed
    that restoring the wrong declaration left that guard green, because it only cross-checks the
    statuses the *matrix* drives (401/403 and the ownership pair) and the matrix never drives
    self-mutation. A guard that cannot fail on the defect it was written for is worse than none,
    so the observed status is cross-checked against the declaration **here**, where it is
    actually produced. §8.65(5), one more time: it is not enough that an instrument can fail.
    """
    actor = await seed_user(session, make_token, admin=True)
    assert actor.id is not None

    url = f"/api/v1/users/{actor.id}"
    if method == "patch":
        response = await client.patch(url, headers=actor.headers, json={"role": "user"})
    else:
        response = await client.delete(url, headers=actor.headers)

    assert response.status_code == 409, (
        f"{method.upper()} {url} answered {response.status_code} to a self-mutation; R-62(2) "
        "refuses it, and the status is what a client branches on"
    )
    assert response.json()["detail"] == "You cannot perform this action on your own account."

    operation = openapi_document["paths"]["/api/v1/users/{user_id}"][method]
    declared = {int(code) for code in operation["responses"] if str(code).isdigit()}
    assert 409 in declared, (
        f"{method.upper()} /api/v1/users/{{user_id}} answers 409 but does not declare it. The "
        "generated TypeScript client (R-57) derives its error handling from this document, so "
        "an undeclared status is a refusal the frontend has no type for."
    )


@nfr("NFR-SEC-02")
async def test_the_refusals_are_indistinguishable_from_one_another(
    client: httpx.AsyncClient,
    actors: dict[Principal, Actor],
    owned: Owned,
    session: AsyncSession,
) -> None:
    """R-55(2) — absent, foreign, foreign-as-administrator and wrong-role answer one string.

    Distinguishing them would make the route a probe for which ids exist and whose they are.
    Asserted as the *identity* of the copy rather than four equal status codes, because two
    `404`s with different wording leak exactly as much as two different statuses.
    """
    question = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == owned.conversation_id)
            .where(Message.role == MessageRole.USER)
        )
    ).one()

    async def detail(message_id: object, actor: Actor) -> str:
        response = await client.post(
            f"/api/v1/messages/{message_id}/feedback",
            headers=actor.headers,
            json={"feedback": "up"},
        )
        assert response.status_code == 404, response.text
        return response.json()["detail"]

    details = {
        await detail(uuid.uuid4(), actors[Principal.STRANGER]),  # absent
        await detail(owned.message_id, actors[Principal.STRANGER]),  # foreign
        await detail(owned.message_id, actors[Principal.ADMIN]),  # foreign, as an administrator
        await detail(question.id, owned.owner),  # own, but a question rather than an answer
    }

    assert len(details) == 1, f"the four refusals are distinguishable: {details}"
