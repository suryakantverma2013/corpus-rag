"""NFR-SEC-07 — rate limiting on authentication, chat and upload (T-602).

`tests/test_rate_limit.py` (T-105) owns the key functions and proves the limiter works on
`/auth/login`. This module owns the claim NFR-SEC-07 actually makes, which is about a *set* of
endpoints: seven of the twelve limited routes had never been driven to their limit, nothing
asserted that two users have separate budgets at the route level, and nothing asserted that
send and regenerate share one — which is the only reason `shared_limit` is used at all.

**No threshold is pinned.** The counts are `# TBD(§8.4)` provisional values, so every test
reads `get_settings().ratelimit` or lowers it through `monkeypatch`, following
`test_rate_limit.py::_login_limit_count`. What is normative is that a limit exists on those
endpoints, that it answers `429`, and that the `429` stays distinguishable from the two other
refusals that share its status code.

**Reaching the limiter at all is the subtle part.** slowapi's decorator wraps the *endpoint
function*, and FastAPI validates path, query and body parameters before calling it — so a
request that fails validation answers `422` without ever touching the bucket. Every payload
below is therefore shaped to pass validation and be refused *inside* the handler, which is
exactly the trick `test_rate_limit.py` uses when it drives invalid credentials through
`/auth/login`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.security.rate_limit import RATE_LIMITED_COPY, limiter
from tests.security import ROUTE_DECISIONS, RouteDecision, nfr
from tests.security.conftest import Actor, seed_user

pytestmark = pytest.mark.usefixtures("patch_jwks")

_LIMITED = tuple(d for d in ROUTE_DECISIONS if d.bucket is not None)

#: A minimal real PDF — the upload routes sniff magic bytes (R-33(5)), so four bytes of
#: nonsense would be refused as `415` before the handler and never reach the bucket.
_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def _ident(decision: RouteDecision) -> str:
    return f"{decision.method} {decision.path}"


def _stub_upstream(respx_mock) -> None:  # noqa: ANN001
    """Keycloak and the provider, stubbed — this module asserts throttling, not linking."""
    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(
        400, json={"error": "invalid_grant", "error_description": "Invalid user credentials"}
    )
    respx_mock.route().respond(json={"access_token": "stub", "expires_in": 60, "files": []})


def _request(decision: RouteDecision, actor: Actor) -> dict[str, object]:
    """A call to `decision` that passes validation and so reaches the limiter.

    Each entry is chosen to be refused *inside* the handler — bad credentials, an absent id —
    rather than by FastAPI's own validation, because a `422` never reaches the bucket.
    """
    url = decision.path.replace("{provider}", "google")
    url = url.replace("{document_id}", str(uuid.uuid4()))
    url = url.replace("{message_id}", str(uuid.uuid4()))
    url = url.replace("{conversation_id}", str(uuid.uuid4()))

    kwargs: dict[str, object] = {"headers": actor.headers}
    match decision.name:
        case "login":
            kwargs["json"] = {"email": "nobody@corpus.test", "password": "wrong"}
        case "change_password":
            kwargs["json"] = {"current_password": "old", "new_password": "new"}
        case "import_document":
            kwargs["json"] = {"provider": "google", "file_id": "abc", "scope": "global"}
        case "send_message":
            kwargs["json"] = {"query": "hello"}
        case "regenerate_message":
            kwargs["json"] = {}
        case "upload_document" | "replace_document":
            kwargs["files"] = {"file": ("doc.pdf", _PDF, "application/pdf")}
    return {"method": decision.method, "url": url, "kwargs": kwargs}


async def _drive(client: httpx.AsyncClient, decision: RouteDecision, actor: Actor) -> int:
    spec = _request(decision, actor)
    response = await client.request(
        spec["method"],
        spec["url"],
        **spec["kwargs"],  # type: ignore[arg-type]
    )
    return response.status_code


# --- every limited route ---------------------------------------------------------------


@nfr("NFR-SEC-07")
@pytest.mark.parametrize("decision", _LIMITED, ids=_ident)
async def test_every_limited_route_trips_its_limit(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,  # noqa: ANN001
    decision: RouteDecision,
) -> None:
    """NFR-SEC-07 over its whole surface, not just the login route it was first built for.

    The limit is lowered rather than the configured count exhausted: the numbers are §8.4
    provisional values, and a test that drove twenty uploads would be asserting one of them.
    The resolvers are callables slowapi re-evaluates per request, so a monkeypatched setting
    takes effect immediately without rebuilding the process-global limiter.
    """
    _stub_upstream(respx_mock)
    monkeypatch.setattr(get_settings().ratelimit, decision.bucket.value, "2/minute")
    actor = await seed_user(session, make_token, admin=True)

    first = await _drive(client, decision, actor)
    second = await _drive(client, decision, actor)
    third = await _drive(client, decision, actor)

    assert first != 429 and second != 429, (
        f"{_ident(decision)} was throttled before its limit — the request never reached the "
        f"handler, so the bucket filled on something other than a real call ({first}, {second})"
    )
    assert third == 429, (
        f"{_ident(decision)} answered {third} on the third call with a 2/minute limit. "
        "Either the route is not limited, or validation refused the payload before slowapi's "
        "wrapper ran and the bucket never filled."
    )


@nfr("NFR-SEC-07")
async def test_the_limit_counts_refused_requests_too(
    client: httpx.AsyncClient, respx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """Stated once, because every other test here rests on it.

    slowapi runs before the handler, so a *failed* login still spends budget. That is what
    makes the limiter a brute-force control rather than a fair-use quota — a limit that only
    counted successes would let an attacker guess passwords indefinitely.
    """
    _stub_upstream(respx_mock)
    monkeypatch.setattr(get_settings().ratelimit, "login", "2/minute")

    body = {"email": "nobody@corpus.test", "password": "wrong"}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(3)]

    assert codes[:2] == [401, 401], codes
    assert codes[2] == 429


# --- who owns a bucket -----------------------------------------------------------------


@nfr("NFR-SEC-07")
async def test_one_users_spending_does_not_throttle_another(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`principal_or_ip_key`, proven from the route rather than from the key function.

    Both callers arrive over the same ASGI transport and therefore the same client address, so
    if the chat bucket were keyed on the address instead of the principal this would fail —
    and one noisy user would be able to silence everyone behind a shared egress IP.
    """
    monkeypatch.setattr(get_settings().ratelimit, "chat", "2/minute")
    spender = await seed_user(session, make_token)
    bystander = await seed_user(session, make_token)
    conversation = str(uuid.uuid4())
    body = {"query": "hello"}

    for _ in range(3):
        await client.post(
            f"/api/v1/conversations/{conversation}/messages", headers=spender.headers, json=body
        )
    assert (
        await client.post(
            f"/api/v1/conversations/{conversation}/messages", headers=spender.headers, json=body
        )
    ).status_code == 429

    other = await client.post(
        f"/api/v1/conversations/{conversation}/messages", headers=bystander.headers, json=body
    )
    assert other.status_code != 429, (
        "a second user was throttled by the first user's spending — the chat bucket is keyed "
        "on the client address rather than on the principal"
    )


@nfr("NFR-SEC-07")
async def test_login_is_keyed_on_the_address_because_there_is_no_principal_yet(
    client: httpx.AsyncClient, respx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    """The deliberate other half: `/auth/login` cannot key on a principal it has not verified.

    Two different accounts share one login bucket, which is what makes the limit a
    brute-force control — keying on the submitted email would let an attacker reset the
    counter by changing one field of the payload.
    """
    _stub_upstream(respx_mock)
    monkeypatch.setattr(get_settings().ratelimit, "login", "2/minute")

    await client.post("/api/v1/auth/login", json={"email": "a@corpus.test", "password": "x"})
    await client.post("/api/v1/auth/login", json={"email": "a@corpus.test", "password": "x"})
    other_account = await client.post(
        "/api/v1/auth/login", json={"email": "b@corpus.test", "password": "x"}
    )

    assert other_account.status_code == 429, (
        "changing the email reset the login bucket — the limit would then be trivially evaded"
    )


@nfr("NFR-SEC-07")
async def test_send_and_regenerate_share_one_chat_bucket(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`shared_limit(scope=_CHAT_BUCKET)` — one budget per user, not one per route.

    slowapi scopes a limit per endpoint by default, so without the shared scope a user would
    get two chat budgets and could spend twice the intended number of model calls by
    alternating. The completeness guard cannot see this: slowapi's registry records that both
    routes are limited but not which scope string they share.
    """
    monkeypatch.setattr(get_settings().ratelimit, "chat", "2/minute")
    actor = await seed_user(session, make_token)
    conversation = str(uuid.uuid4())

    for _ in range(2):
        await client.post(
            f"/api/v1/conversations/{conversation}/messages",
            headers=actor.headers,
            json={"query": "hello"},
        )

    regenerate = await client.post(
        f"/api/v1/messages/{uuid.uuid4()}/regenerate", headers=actor.headers, json={}
    )
    assert regenerate.status_code == 429, (
        "regenerate had its own budget — send and regenerate must share one bucket, or the "
        "per-user chat limit is twice what it says it is"
    )


@nfr("NFR-SEC-07")
@pytest.mark.parametrize("template", ["/api/v1/documents/{id}", "/api/v1/documents/{id}/retry"])
async def test_a_path_parameter_does_not_multiply_the_budget(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    """R-77(3) — the regression test for a limit that was not bounding anything.

    slowapi's `key_style` defaults to `"url"`, so a plain `limiter.limit(...)` buckets on the
    **concrete request path**. Before the fix these routes had one budget *per document id*: a
    fresh id on every call never tripped the limit at all, so a caller with N documents held N
    times the allowance for `replace` — which re-uploads bytes and re-runs embedding, the most
    expensive thing the product does. §8.4 documents this limit as "upload 20/minute per user",
    and it was per user *and per document*.

    Two ids are enough to show it: with a 2/minute limit and one bucket, the third request is
    refused whichever id it names. This fails on the un-scoped decorator and passes on
    `shared_limit(scope=UPLOAD_BUCKET)`.
    """
    monkeypatch.setattr(get_settings().ratelimit, "upload", "2/minute")
    actor = await seed_user(session, make_token)
    method = "DELETE" if template.endswith("{id}") else "POST"

    codes = [
        (
            await client.request(method, template.format(id=uuid.uuid4()), headers=actor.headers)
        ).status_code
        for _ in range(3)
    ]

    assert codes[2] == 429, (
        f"three calls to {template} with three different ids produced {codes} — the budget "
        "is keyed on the concrete path, so naming a new id buys a fresh allowance"
    )


@nfr("NFR-SEC-07")
async def test_the_upload_family_shares_one_budget_per_user(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8.4's "upload 20/minute per user" is one budget, not one per route.

    The counterpart to the chat bucket: an upload and a delete draw on the same allowance, so
    a caller cannot get 20 of each per minute by alternating. Asserted across two *different*
    routes, which is what distinguishes a shared scope from merely a correctly-keyed one.
    """
    monkeypatch.setattr(get_settings().ratelimit, "upload", "2/minute")
    actor = await seed_user(session, make_token)

    for _ in range(2):
        await client.post(
            "/api/v1/documents",
            headers=actor.headers,
            files={"file": ("d.pdf", _PDF, "application/pdf")},
        )

    other_route = await client.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=actor.headers)
    assert other_route.status_code == 429, (
        "delete had its own budget after upload exhausted the allowance — the upload family "
        "must share one per-user bucket"
    )


@nfr("NFR-SEC-07")
async def test_an_anonymous_flood_cannot_spend_a_users_chat_budget(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authentication precedes the limiter, so an unauthenticated caller has nothing to spend.

    Closes the obvious attack on a per-principal budget: if the limiter ran first and keyed on
    the address, anyone could exhaust a named user's chat allowance without a credential.
    """
    monkeypatch.setattr(get_settings().ratelimit, "chat", "2/minute")
    victim = await seed_user(session, make_token)
    conversation = str(uuid.uuid4())

    for _ in range(5):
        flood = await client.post(
            f"/api/v1/conversations/{conversation}/messages", json={"query": "hello"}
        )
        assert flood.status_code == 401

    theirs = await client.post(
        f"/api/v1/conversations/{conversation}/messages",
        headers=victim.headers,
        json={"query": "hello"},
    )
    assert theirs.status_code != 429, "an anonymous flood consumed an authenticated user's budget"


# --- telling the three 429s apart --------------------------------------------------------


@nfr("NFR-SEC-07")
async def test_the_chat_limit_refuses_before_the_stream_opens(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-57(2) — the refusal is an HTTP status, never a frame inside a 200 stream.

    This is the mirror of `test_openapi_contract.py::test_the_three_sse_routes_are_streams`.
    That one stops the limiter being applied as a decorator, which would silently destroy the
    route's async-generator identity; this one stops the refusal being moved *into* the
    generator, where a client would render a throttle as though it were an answer. Both
    failures are invisible to a suite that only checks status codes on the happy path.
    """
    monkeypatch.setattr(get_settings().ratelimit, "chat", "1/minute")
    actor = await seed_user(session, make_token)
    conversation = str(uuid.uuid4())
    body = {"query": "hello"}

    await client.post(
        f"/api/v1/conversations/{conversation}/messages", headers=actor.headers, json=body
    )
    throttled = await client.post(
        f"/api/v1/conversations/{conversation}/messages", headers=actor.headers, json=body
    )

    assert throttled.status_code == 429
    assert not throttled.headers.get("content-type", "").startswith("text/event-stream"), (
        "the throttle was delivered as an event stream — a client would render it as a turn"
    )
    assert throttled.json()["detail"] == RATE_LIMITED_COPY


@nfr("NFR-SEC-07")
async def test_a_throttle_and_a_processing_lock_are_distinguishable(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-43(4) chose `409` over `429` *because* these routes already answer `429`.

    The two refusals mean different things — "you are going too fast" against "a turn of yours
    is running" — and only one of them is worth retrying immediately. R-71(1) then had to add
    `PROCESSING_LOCKED` so a client could tell the lock from the other `409`s without matching
    on copy. This asserts the pair stays tellable apart by shape: the throttle's `detail` is a
    string, the lock's is an object carrying an `error_code`.
    """
    from datetime import UTC, datetime, timedelta

    from app.db.models.processing_lock import ProcessingLock

    # Set before the first call, not between the two: `limits` folds the limit's amount and
    # window into its storage key, so changing the string mid-test starts a *fresh* counter and
    # the second request would be allowed — a way for this test to pass without a throttle.
    monkeypatch.setattr(get_settings().ratelimit, "upload", "1/minute")

    actor = await seed_user(session, make_token)
    assert actor.id is not None

    # Publish the gate directly, as the graph's `lock` node would (the `_hold_lock` shape
    # `tests/test_documents_api.py` already uses).
    session.add(
        ProcessingLock(
            owner_id=actor.id,
            conversation_id=None,
            token=uuid.uuid4().hex,
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=3),
        )
    )
    await session.flush()

    locked = await client.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=actor.headers)
    assert locked.status_code == 409, locked.text
    assert locked.json()["detail"]["error_code"] == "PROCESSING_LOCKED"

    throttled = await client.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=actor.headers)

    assert throttled.status_code == 429
    assert isinstance(throttled.json()["detail"], str), (
        "the two refusals now have the same body shape and a client cannot tell a throttle "
        "from a processing lock"
    )


# --- degradation --------------------------------------------------------------------------


@nfr("NFR-SEC-07")
async def test_a_storage_failure_fails_open(
    client: httpx.AsyncClient,
    respx_mock,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`swallow_errors=True` — a Redis blip degrades to *allow*, never to `500`.

    The trade is recorded at `app/security/rate_limit.py`: availability over strict limiting.
    It is worth an explicit test because it is the one failure mode nobody sees in development
    — the limiter's storage is in-memory here and Redis in production, so the branch that runs
    when the store is unreachable is only ever exercised deliberately.

    Provoked by making the storage raise rather than by flipping a flag, so what is exercised
    is slowapi's real `except` path.
    """
    from limits.errors import StorageError

    _stub_upstream(respx_mock)
    monkeypatch.setattr(get_settings().ratelimit, "login", "2/minute")

    def boom(*args: object, **kwargs: object) -> int:
        raise StorageError("redis is gone")

    monkeypatch.setattr(limiter._storage, "incr", boom)

    body = {"email": "nobody@corpus.test", "password": "wrong"}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(6)]

    assert set(codes) == {401}, (
        f"a storage outage changed the answers to {sorted(set(codes))}; the limiter must fail "
        "open, so every one of these should still have reached the handler"
    )
