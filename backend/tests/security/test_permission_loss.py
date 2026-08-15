"""Permission loss mid-session — the two revocation latencies (T-602, NFR-SEC-01, R-77).

**The finding this module exists to record.** Corpus revokes two different things at two
different speeds, and the difference is invisible until you look for it:

* **Account state is read from the database on every request.** `get_current_user` loads the
  local `users` row and refuses a disabled one, so disabling an account takes effect on the
  caller's *very next request*, with a live and perfectly valid token in their hand.
* **The role is read from the token claim.** `Principal.is_administrator` comes from
  `realm_access.roles`, and `users` has **no role column at all** — R-28 gave Keycloak
  credentials and roles, and `app/db/models/users.py` says so in its docstring. So demoting an
  administrator does **not** take effect on the next request. It takes effect when their
  access token expires, which is Keycloak's `accessTokenLifespan` (300 s on the shipped realm,
  by default rather than by declaration) or sooner if they refresh.

NFR-SEC-01 says role-based access control is *"enforced server-side on every request"*, and
that is true of the *check* — every request re-reads the presented claim, nothing is cached.
It is not true of the *data*, and a reader is entitled to conclude otherwise. R-77 records
both latencies at the requirement; this module is the executable half.

**None of it is a defect.** It is what R-28 bought: one source of truth for identity, and no
introspection round trip on the request path. The operational consequence is the thing worth
knowing, and it is what `test_the_immediate_revocation_lever_is_deactivation` pins — when an
administrator must lose access *now*, disable the account; demoting alone leaves a window.

Boundary with `tests/scenarios/test_scope.py::S08`: that owns the retrieval predicate — a
document leaving scope stops grounding the very next turn. This owns the HTTP surface.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db.models.users import User
from app.db.repositories.users import UserRepository
from tests.security import Gate, RouteDecision, nfr, secured_routes
from tests.security.conftest import call, seed_user

pytestmark = pytest.mark.usefixtures("patch_jwks")

_SECURED = tuple(secured_routes())


def _ident(decision: RouteDecision) -> str:
    return f"{decision.method} {decision.path}"


def _params(decision: RouteDecision) -> dict[str, str] | None:
    """Keep the sweep's requests finite.

    `GET /documents/events` with no query parameters resolves the caller's global scope and
    opens an endless stream, which `httpx.ASGITransport` would accumulate for ever (T-210
    recorded the same constraint). Pointing it at a chat that does not exist makes it answer
    `404` before any stream begins, which is all this sweep needs — the claim is about the
    gate, not about the stream.
    """
    if decision.name in {"stream_documents", "list_documents"}:
        return {"scope": "chat", "conversation_id": str(uuid.uuid4())}
    return None


def _stub_keycloak(respx_mock) -> None:  # noqa: ANN001
    """Keep the sweep's *pre*-call about authorization rather than about upstream health.

    `GET /cloud/links/{provider}` reaches Keycloak's Admin API once it is past the gate, so an
    enabled caller's control request would otherwise depend on a live realm — and would fail
    as a `500`, reporting an infrastructure gap as though the account had been refused. The
    stub is deliberately shapeless: this module asserts nothing about linking, only that the
    call got far enough to need it.
    """
    from app.config import get_settings

    kc = get_settings().keycloak
    respx_mock.post(kc.token_endpoint).respond(json={"access_token": "stub", "expires_in": 60})
    respx_mock.route(url__startswith=kc.admin_url).respond(json=[])
    # A catch-all last, since respx matches in registration order. The cloud routes reach the
    # broker endpoint and then Google; naming each one here would be asserting a call sequence
    # this module has no opinion about, and `tests/test_cloud_import.py` already owns it.
    respx_mock.route().respond(json={"access_token": "stub", "expires_in": 60, "files": []})


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", _SECURED, ids=_ident)
async def test_disabling_an_account_takes_effect_on_the_very_next_request(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token: Callable[..., str],
    decision: RouteDecision,
    respx_mock,  # noqa: ANN001
) -> None:
    """The immediate half of revocation, swept across every authenticated route.

    Written as a **transition** rather than as a static gate check, which is the whole
    difference between this and the matrix's `DEACTIVATED` column: the same caller, the same
    token, the same URL, one fact changed in between. The pre-assertion is what makes it a
    revocation test — without it the second call's `403` would be consistent with the account
    having been unusable all along.

    The caller holds the `admin` claim throughout, so on an administrator route the refusal
    cannot be coming from the role gate. `require_admin` depends on `get_current_user`, so the
    disabled check runs first and its copy is the proof of which guard fired.
    """
    _stub_keycloak(respx_mock)
    actor = await seed_user(session, make_token, admin=True)
    assert actor.id is not None

    before = await call(client, decision, actor, params=_params(decision))
    assert before.status_code != 403, (
        f"{_ident(decision)} already refused an enabled account with 403 — this cell cannot "
        "show that disabling changed anything"
    )

    user = await UserRepository(session).get(actor.id)
    await UserRepository(session).set_active(user, is_active=False)
    await session.flush()

    after = await call(client, decision, actor, params=_params(decision))

    assert after.status_code == 403, (
        f"{_ident(decision)} still answered {after.status_code} to a disabled account. "
        "`is_active` is read from the database on every request; a route that skips "
        "`get_current_user` skips it."
    )
    assert after.json()["detail"] == "User account is disabled"


@nfr("NFR-SEC-01")
async def test_deleting_the_local_row_mid_session_is_a_401(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The other database-backed transition: the row goes away under a live token.

    `users_service.delete_user` disables rather than deletes (foreign keys into `users.id` are
    `NO ACTION`), so this is the sharper, rarer case — an operator removing the row directly.
    It answers `401` rather than `403` because there is no account left to call disabled.
    """
    actor = await seed_user(session, make_token)
    assert actor.id is not None

    assert (await client.get("/api/v1/auth/me", headers=actor.headers)).status_code == 200

    await session.delete(await session.get(User, actor.id))
    await session.flush()

    response = await client.get("/api/v1/auth/me", headers=actor.headers)
    assert response.status_code == 401
    assert response.json()["detail"] == "User not found"


@nfr("NFR-SEC-01")
def test_there_is_no_server_side_state_that_could_revoke_a_role() -> None:
    """The negative fact, asserted structurally — this is what makes the next test a *finding*.

    If a role column existed, role staleness would be a bug: the request path would have a
    live source and would be ignoring it. There is none. `require_admin` reads
    `principal.is_administrator`, which is derived from the token's `realm_access.roles`, and
    R-28 put that authority in Keycloak deliberately. Adding a column here would create a
    second source of truth for something the realm owns, and this assertion is what forces
    that decision back through a ruling rather than arriving as a quiet migration.
    """
    assert "role" not in {column.key for column in User.__table__.columns}
    assert "roles" not in {column.key for column in User.__table__.columns}

    source = require_admin.__doc__ or ""
    assert "claims" in source, (
        "`require_admin`'s docstring no longer records that the role comes from claims — if "
        "the source of the role changed, R-77's latency statement needs revisiting"
    )


@nfr("NFR-SEC-01")
async def test_an_administrators_role_is_read_from_the_token_on_every_request(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The check is per-request; the *data* it checks arrives with the request.

    Two tokens for one `sub`, differing only in `realm_access.roles`, produce different answers
    from the same route with no server-side state in between. That is the mechanism in one
    assertion: nothing is cached, and equally nothing is consulted beyond the bearer token.

    The consequence is the next test's subject — a token already issued keeps whatever it was
    issued with until it expires.
    """
    actor = await seed_user(session, make_token, admin=True)
    assert actor.id is not None and actor.email is not None

    assert (await client.get("/api/v1/users", headers=actor.headers)).status_code == 200

    demoted = make_token(sub=actor.id, email=actor.email, roles=("user",))
    response = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {demoted}"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Administrator role required"


@nfr("NFR-SEC-01")
async def test_a_token_issued_with_the_role_keeps_it_until_it_expires(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """R-77's second latency, as behaviour: expiry is the only thing that ends an issued role.

    There is no revocation list, no `notBefore` push and no introspection call, so between a
    demotion in Keycloak and the token's `exp` the holder remains an administrator here. The
    bound is real and it is asserted — the same claim set, expired, is refused — which is what
    distinguishes "bounded by the token lifetime" from "unbounded".

    The realm does not declare `accessTokenLifespan`, so the window is Keycloak's 300 s
    default. That number is deliberately **not** asserted: it is realm configuration an
    operator may change, and pinning it here would make a test fail for a decision that is
    theirs to make. What is asserted is the shape — an unexpired token works, an expired one
    does not.
    """
    actor = await seed_user(session, make_token, admin=True)
    assert actor.id is not None and actor.email is not None

    live = make_token(sub=actor.id, email=actor.email, roles=("admin", "user"))
    assert (
        await client.get("/api/v1/users", headers={"Authorization": f"Bearer {live}"})
    ).status_code == 200

    import time

    now = int(time.time())
    expired = make_token(
        sub=actor.id, email=actor.email, roles=("admin", "user"), exp=now - 60, iat=now - 600
    )
    stale = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {expired}"})
    assert stale.status_code == 401, (
        "an expired administrator token was accepted — the role window would then be unbounded"
    )


@nfr("NFR-SEC-01")
async def test_the_immediate_revocation_lever_is_deactivation(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """The operational conclusion, and the reason R-77 is worth writing down.

    An administrator who must lose access *now* is disabled, not demoted. Demotion is
    eventually consistent with the token lifetime; deactivation is read from the database on
    the next request. This is the test an operator's runbook would cite, and it is the answer
    to "we removed their admin role, are they out?" — not yet.
    """
    actor = await seed_user(session, make_token, admin=True)
    assert actor.id is not None

    assert (await client.get("/api/v1/users", headers=actor.headers)).status_code == 200

    repository = UserRepository(session)
    await repository.set_active(await repository.get(actor.id), is_active=False)
    await session.flush()

    refused = await client.get("/api/v1/users", headers=actor.headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "User account is disabled", (
        "deactivation must be what refuses this caller — their token still carries the "
        "administrator claim, so a role-based refusal would mean the lever does not work"
    )


@nfr("NFR-SEC-01")
async def test_a_disabled_administrator_is_refused_as_disabled_not_as_unprivileged(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """Ordering, pinned: `require_admin` depends on `get_current_user`, so disabled wins.

    Both refusals are `403`, so only the copy distinguishes them — and the distinction matters
    twice over. It is the difference an operator needs when reading a log, and it is what stops
    every deactivation cell in this package passing for the wrong reason on an administrator
    route, where an unprivileged caller would have been refused anyway.
    """
    actor = await seed_user(session, make_token, admin=False, is_active=False)
    admin_route = next(d for d in _SECURED if d.gate is Gate.ADMIN)

    response = await call(client, admin_route, actor)

    assert response.status_code == 403
    assert response.json()["detail"] == "User account is disabled"


@nfr("NFR-SEC-01", "NFR-SEC-08")
async def test_a_route_level_refusal_writes_no_audit_row(
    client: httpx.AsyncClient, session: AsyncSession, make_token: Callable[..., str]
) -> None:
    """NFR-SEC-08 records the governance denial, not the route's 404 — and that is deliberate.

    R-43(7) files an FR-ORC-02 denial under `AuditEventType.AUTH`, and `test_graph.py` covers
    it at the only level where it can happen. It is **unreachable from the HTTP surface by
    construction**: `admit_send` resolves `_owned_or_404` first and then builds `RAGContext`
    with `owner_id = user.id`, so `govern`'s `conversation.owner_id != ctx.owner_id` is false
    whenever the request came through the API. The branch is live on *resume*, where R-42(3)
    re-authorizes a checkpointed run against a principal that may have changed.

    Forcing it from a route test would mean driving a branch the API cannot reach — precisely
    the instrument §8.65(5) rules against. So what is asserted here is the property that
    follows from it and is worth having: **guessing ids does not write to the audit trail.**
    Otherwise an unauthenticated enumeration sweep could fill an administrator's audit view,
    which would be an availability problem wearing a security feature's clothes.
    """
    from app.db.models.audit_log import AuditLog

    stranger = await seed_user(session, make_token)
    assert stranger.id is not None

    for _ in range(5):
        response = await client.get(
            f"/api/v1/conversations/{uuid.uuid4()}", headers=stranger.headers
        )
        assert response.status_code == 404

    # Scoped to this actor, never a table count: the suite runs against the shared `corpus`
    # database and nothing truncates it (the T-109 rule).
    from sqlalchemy import func, select

    written = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.actor_id == stranger.id)
    )
    assert written == 0, (
        "a route-level 404 wrote an audit row; id-guessing must not be able to flood the "
        "NFR-SEC-08 trail"
    )
