"""Harness for the T-602 security suite.

Two things the rest of the suite did not have.

**A principal factory covering the failure classes, not just the happy caller.** Every route
module in `tests/` grows its own `_caller` helper that mints a valid token and seeds the local
row — twelve near-identical copies. None of them can express *a token whose signature is
wrong*, *a token whose `users` row was deleted*, or *a caller who was disabled a moment ago*,
because no per-route test needed them. Those are exactly the cells NFR-SEC-01 is about.

**A driver that can call any route without knowing what it does.** The matrix asserts a
refusal on thirty-eight routes, and a refusal happens *before* body validation — verified: a
request with no body at all answers `401`, never `422`, because FastAPI resolves security
dependencies first. So the driver synthesises path parameters and sends no body, and the
absence of a body is not a shortcut but a load-bearing part of the claim: it proves nothing
downstream of authentication ran.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.users import UserRepository
from tests.security import Principal, RouteDecision

pytestmark = pytest.mark.usefixtures("patch_jwks")


@dataclass(frozen=True, slots=True)
class Actor:
    """A caller and the headers that authenticate them (or fail to)."""

    principal: Principal
    headers: dict[str, str] = field(default_factory=dict)
    #: `None` for the classes that have no local row by construction.
    id: uuid.UUID | None = None
    email: str | None = None


async def seed_user(
    session: AsyncSession,
    make_token: Callable[..., str],
    *,
    admin: bool = False,
    is_active: bool = True,
    email: str | None = None,
) -> Actor:
    """A valid caller: local `users` row **and** a matching token.

    Both halves are required — `get_current_user` answers `401 "User not found"` for a token
    whose `sub` has no row, so a token alone does not authenticate anyone. `test_admin_users.py`
    records the same trap: seed the row, or the admin gate resolves to a missing-user 401 and
    the test proves nothing about the role check it names.
    """
    sub = uuid.uuid4()
    address = email or f"{sub.hex[:12]}@corpus.test"
    user = await UserRepository(session).upsert_from_claims(
        sub=sub, email=address, display_name="Security Suite"
    )
    if not is_active:
        await UserRepository(session).set_active(user, is_active=False)
    roles = ("admin", "user") if admin else ("user",)
    token = make_token(sub=sub, email=address, roles=roles)
    principal = Principal.ADMIN if admin else Principal.STRANGER
    return Actor(
        principal=principal,
        headers={"Authorization": f"Bearer {token}"},
        id=user.id,
        email=address,
    )


@pytest.fixture
async def actors(
    session: AsyncSession,
    make_token: Callable[..., str],
    rsa_keys: tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey],
) -> dict[Principal, Actor]:
    """One actor per :class:`Principal`, built once per test.

    Built together so the matrix pays for them once rather than per cell — the sweep drives
    ~100 requests and each seeded row is a round trip to the shared database.
    """
    stranger = await seed_user(session, make_token)
    admin = await seed_user(session, make_token, admin=True)
    deactivated = await seed_user(session, make_token, admin=True, is_active=False)

    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # A second keypair, so `WRONG_SIGNATURE` is a real signature by the wrong key rather than
    # corrupted bytes — those two take different paths through pyjwt and only one of them is
    # the attack.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    return {
        Principal.ANONYMOUS: Actor(Principal.ANONYMOUS, {}),
        Principal.MALFORMED: Actor(Principal.MALFORMED, bearer("not-a-jwt")),
        Principal.WRONG_SIGNATURE: Actor(
            Principal.WRONG_SIGNATURE, bearer(make_token(signing_key=other_key))
        ),
        Principal.EXPIRED: Actor(
            Principal.EXPIRED,
            bearer(make_token(exp=int(time.time()) - 60, iat=int(time.time()) - 600)),
        ),
        # A valid, correctly signed, unexpired token for a `sub` with no local row — the state
        # a user is left in the instant an administrator deletes their account.
        Principal.NO_LOCAL_ROW: Actor(Principal.NO_LOCAL_ROW, bearer(make_token(sub=uuid.uuid4()))),
        # Carries the `admin` claim deliberately: driven against an administrator route it must
        # still fail on *deactivation*, not on a missing role, or the cell passes for the wrong
        # reason.
        Principal.DEACTIVATED: Actor(
            Principal.DEACTIVATED, deactivated.headers, deactivated.id, deactivated.email
        ),
        Principal.STRANGER: stranger,
        Principal.ADMIN: admin,
    }


# --- the generic driver ---------------------------------------------------------------

#: Stand-ins for path parameters. The refusal classes never reach a handler, so the values
#: only have to parse: a non-UUID would answer `422` from FastAPI's own validation and mask
#: the `401` the cell is asserting.
_PATH_VALUES: dict[str, Callable[[], str]] = {
    "provider": lambda: "google",
}


def fill_path(path: str, **overrides: Any) -> str:
    """Substitute `{param}` templates, using `overrides` where given and a stand-in otherwise."""
    filled = path
    while "{" in filled:
        start = filled.index("{")
        end = filled.index("}", start)
        name = filled[start + 1 : end]
        if name in overrides:
            value = str(overrides[name])
        elif name in _PATH_VALUES:
            value = _PATH_VALUES[name]()
        else:
            value = str(uuid.uuid4())
        filled = filled[:start] + value + filled[end + 1 :]
    return filled


async def call(
    client: httpx.AsyncClient,
    decision: RouteDecision,
    actor: Actor,
    *,
    path: str | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,
    files: Any = None,
) -> httpx.Response:
    """Drive one route as one actor.

    Sends **no body** unless a caller supplies one. That is deliberate and is part of what the
    refusal cells prove: authentication resolves before body validation, so a `401` here is
    evidence that nothing downstream — validation, ownership, the rate limiter, the handler —
    ran at all. Verified against every mutating route: no `422` is ever returned first.

    **Only ever used for calls that are expected to be refused on the SSE routes.**
    `httpx.ASGITransport` accumulates a whole response before returning it, so a *successful*
    call to an endless stream hangs forever — the constraint T-210 already recorded at
    `tests/test_document_events.py::_open_stream`. Refusals are safe because every one of them
    is resolved in a dependency (R-57(2)) and arrives as an ordinary finite JSON body; a
    success is not, which is why the positive controls use a sibling route (see
    `_CONTROL_SIBLING` in `test_authz_matrix.py`).
    """
    kwargs: dict[str, Any] = {"headers": actor.headers}
    if params is not None:
        kwargs["params"] = params
    if json is not None:
        kwargs["json"] = json
    if files is not None:
        kwargs["files"] = files
    url = path or fill_path(decision.path)
    return await getattr(client, decision.method.lower())(url, **kwargs)


class RecordingQueue:
    """The `JobQueue` protocol, recording dispatches and running nothing.

    The matrix drives mutating routes (`delete`, `replace`) as an administrator to assert
    FR-USR-04's widening, and those enqueue real work. This package is about *who may call
    what*, not about what the worker then does — `tests/scenarios/` owns that seam — so the
    queue records and stops there.
    """

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, uuid.UUID]] = []

    async def enqueue_ingest(self, *, job_id, document_id, idempotency_key) -> None:  # noqa: ANN001, ARG002
        self.dispatched.append(("ingest", document_id))

    async def enqueue_delete(self, *, job_id, document_id, idempotency_key) -> None:  # noqa: ANN001, ARG002
        self.dispatched.append(("delete", document_id))

    async def enqueue_evaluate(self, *, message_id, idempotency_key) -> None:  # noqa: ANN001, ARG002
        self.dispatched.append(("evaluate", message_id))

    async def aclose(self) -> None:
        return None


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def app(app, tmp_path, queue: RecordingQueue):  # noqa: ANN001, ANN201
    """The shared app with deterministic storage and a recording queue.

    Without these, a `replace` driven as an administrator reaches the configured object store
    and would answer `503` on a box where MinIO happens to be down — turning an authorization
    assertion into an infrastructure one, and reporting a widening failure that is nothing of
    the kind.
    """
    from app.services.jobs import get_job_queue
    from app.services.object_storage import LocalFilesystemStorage, get_object_storage

    storage = LocalFilesystemStorage(tmp_path / "objects")
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


@pytest.fixture
def cell_id() -> Callable[[RouteDecision], str]:
    """Readable parametrize ids — `POST /api/v1/documents` rather than `routedecision7`."""

    def _id(decision: RouteDecision) -> str:
        return f"{decision.method} {decision.path}"

    return _id


@pytest.fixture(autouse=True)
def _serial_retrieval() -> Iterator[None]:
    """One connection cannot hold two concurrent savepoints.

    T-311's prefetch runs the original query's retrieval arm alongside the router call, which
    under the transactional fixture is a second savepoint on the same connection. It surfaces
    as `RETRIEVAL_UNAVAILABLE` — a fail-closed turn that reads as a retrieval defect rather
    than as a fixture problem, and which could make an injection test pass for entirely the
    wrong reason. Same fixture as `tests/scenarios/conftest.py`; autouse here because any
    module in this package may end up asking a question.
    """
    from app.config import get_settings

    retrieval = get_settings().retrieval
    previous = retrieval.prefetch_query_arm
    retrieval.prefetch_query_arm = False
    try:
        yield
    finally:
        retrieval.prefetch_query_arm = previous
