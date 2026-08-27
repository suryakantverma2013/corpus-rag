"""The T-602 security manifests — one committed decision per route, per NFR (NFR-SEC-*).

**Why a manifest and not a pile of assertions.** The suite reached 1,446 tests with
authorization touched in thirteen files, and the coverage was shaped by the build order
rather than by the requirement: 401 was asserted on thirteen routes and missing on sixteen,
the disabled-account 403 was asserted on exactly two, and role loss mid-session was asserted
nowhere. Every one of those gaps is invisible from inside the task that created it, because
each route's own tests looked complete. NFR-SEC-01 is a claim about a **set** — *"enforced
server-side on every request"* — and a claim about a set can only be tested by enumerating
the set and failing when it grows.

So the shape here is T-601's `SPEC_11_ROWS` and T-607's `spec_issues.json` one level over:
a committed table, a stamp, and a guard that reads the **live application** back and fails
when the two disagree. A new route cannot ship without an authorization decision, because
`test_completeness.py::test_every_live_route_has_an_authorization_decision` fails until one
is written here.

**What is declared versus what is derived.** Each row states six facts. The ~340 cells of a
nine-principal by thirty-eight-route matrix are *generated* from them by
:func:`expected_status`. Reviewers read thirty-eight rows; the matrix is arithmetic. Writing
the cells out instead would produce a table nobody re-reads, which is the failure mode this
package exists to prevent.

**The `why` column is load-bearing.** Every status here is fixed by a ruling, and several are
counter-intuitive on purpose — an administrator gets `404` on another user's *conversation*
(R-54, closing OI-33) and `200` on another user's *document* (FR-USR-04). Someone will
eventually "fix" one of those to match the other. The citation is what stops them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "API",
    "NFR_SEC_ROWS",
    "PRINCIPALS",
    "ROUTE_DECISIONS",
    "Bucket",
    "Gate",
    "Owns",
    "Principal",
    "RouteDecision",
    "covered_nfrs",
    "expected_status",
    "nfr",
    "nfrs_of",
    "owned_routes",
    "secured_routes",
]

#: Every application route is mounted here (`app/main.py`); the three health probes are not.
API: Final = "/api/v1"


class Gate(StrEnum):
    """Which authentication dependency chain a route resolves."""

    #: No credential required. A short, explicitly justified list — see `ROUTE_DECISIONS`.
    PUBLIC = "public"
    #: Resolves `get_current_user`: a valid token **and** a live, enabled local `users` row.
    USER = "user"
    #: Resolves `require_admin`, which itself depends on `get_current_user`.
    ADMIN = "admin"


class Owns(StrEnum):
    """The kind of resource whose ownership the route checks, if any.

    Not decoration: it names which seeded row a foreign-caller cell must be driven against.
    A cell driven at a random UUID passes whether the ownership predicate exists or not,
    which is the vacuity trap §8.65(5) records.
    """

    NONE = "none"
    CONVERSATION = "conversation"
    MESSAGE = "message"
    DOCUMENT = "document"
    JOB = "job"
    #: Not a path parameter — a `conversation_id` query parameter naming the chat to scope to.
    CHAT_SCOPE = "chat_scope"


class Bucket(StrEnum):
    """The NFR-SEC-07 rate-limit bucket, or absence of one.

    `CHAT` is shared by two routes through one dependency (`_chat_rate_limit`), which is the
    only reason `shared_limit` is used at all.
    """

    LOGIN = "login"
    REFRESH = "refresh"
    CHANGE_PASSWORD = "change_password"
    CHAT = "chat"
    UPLOAD = "upload"


class Principal(StrEnum):
    """The caller classes the matrix drives.

    Deliberately factored. `MALFORMED`, `WRONG_SIGNATURE` and `EXPIRED` all collapse into one
    production statement — `get_principal` translating `TokenValidationError` into `401` — and
    `tests/test_auth.py` already unit-tests each rejection at the token layer, so they are
    driven against one representative route rather than all thirty-five. `NO_LOCAL_ROW` and
    `DEACTIVATED` get the full sweep, because those two live in `get_current_user` and a route
    that resolved `CurrentPrincipal` without `CurrentUser` would silently skip both while every
    existing per-route test stayed green.
    """

    ANONYMOUS = "anonymous"
    MALFORMED = "malformed"
    WRONG_SIGNATURE = "wrong_signature"
    EXPIRED = "expired"
    NO_LOCAL_ROW = "no_local_row"
    DEACTIVATED = "deactivated"
    STRANGER = "stranger"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One route's authorization decision, and the ruling that fixes it."""

    method: str
    #: The full request path, `/api/v1/...`, with FastAPI's `{param}` templates intact.
    path: str
    #: The handler's function name — the join key to slowapi's registry, which is keyed
    #: `module.function` and knows nothing about paths.
    name: str
    gate: Gate
    why: str
    owns: Owns = Owns.NONE
    #: Whether an administrator reaches another user's row. `None` when `owns` is `NONE`.
    #: The two answers are both correct and they are the pair most at risk of being
    #: "harmonised" by a later reader: FR-USR-04 widens documents and jobs, R-54 refuses
    #: conversations and messages.
    admin_widens: bool | None = None
    #: Status a non-owning non-administrator receives. Always `404`, never `403` — a `403`
    #: would confirm the id exists (NFR-SEC-02).
    foreign: int | None = None
    #: Status an administrator receives on the same row. Read with `admin_widens`.
    admin_foreign: int | None = None
    bucket: Bucket | None = None
    #: An `EventSourceResponse` route. A successful call to one never completes on its own, so
    #: the driver must open it as a stream and close it rather than awaiting a whole body. A
    #: *refusal* still arrives as an ordinary JSON response, because every refusal on these
    #: routes is resolved in a dependency — R-57(2): a generator handler's body runs after the
    #: status line, where no HTTP status can carry a refusal.
    sse: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


def _r(*args: object, **kwargs: object) -> RouteDecision:
    return RouteDecision(*args, **kwargs)  # type: ignore[arg-type]


#: Rulings cited more than once, spelled out where they are used rather than in a legend a
#: reader has to hold in their head.
_R54 = "R-54(1)/FR-ORC-02 — conversation routes are owner-only and answer 404 for an "
_R54 += "administrator too; 403 would confirm the id exists (NFR-SEC-02). Closes OI-33."
_R55 = "R-55(2) — absent, foreign and non-AI targets answer one identical 404, so the route "
_R55 += "cannot be used to probe which ids exist or whose they are."
_USR04 = "FR-USR-04 — administrators have unrestricted access to any user's documents."

ROUTE_DECISIONS: Final[tuple[RouteDecision, ...]] = (
    # --- unauthenticated, by design ---------------------------------------------------
    _r(
        "GET",
        "/health",
        "liveness",
        Gate.PUBLIC,
        "NFR-REL-02 — a liveness probe cannot hold a credential.",
    ),
    _r(
        "GET",
        "/health/ready",
        "readiness",
        Gate.PUBLIC,
        "R-29 — readiness is read by the orchestrator, not a user.",
    ),
    _r(
        "GET",
        "/health/ready/worker",
        "worker_readiness",
        Gate.PUBLIC,
        "R-38 — the worker's own readiness arm.",
    ),
    _r(
        "POST",
        f"{API}/auth/login",
        "login",
        Gate.PUBLIC,
        "FR-AUT-02 — this is where a credential is obtained.",
        bucket=Bucket.LOGIN,
    ),
    _r(
        "POST",
        f"{API}/auth/refresh",
        "refresh",
        Gate.PUBLIC,
        "R-72(1) — authenticated by the httpOnly refresh cookie, never a bearer token.",
        bucket=Bucket.REFRESH,
    ),
    _r(
        "POST",
        f"{API}/auth/logout",
        "logout",
        Gate.PUBLIC,
        "FR-AUT-08 — sign-out must leave the browser signed out even when the upstream "
        "revoke cannot be delivered, so it takes no bearer token and clears the cookie on "
        "every path. Requiring auth would strand a user holding a live session cookie.",
    ),
    _r(
        "GET",
        f"{API}/cloud/links/{{provider}}/callback",
        "link_callback",
        Gate.PUBLIC,
        "R-63(3)/§8.53(1) — Keycloak returns the browser here on a full page load, so there "
        "is no bearer token to present; trust rides on the signed `state`, and the callback "
        "refuses a `sub` that does not match the initiator.",
    ),
    _r(
        "GET",
        f"{API}/cloud/links/{{provider}}/complete",
        "link_complete",
        Gate.PUBLIC,
        "R-63(3) — the second leg of the same browser redirect; identity comes from the "
        "signed `state`, not from a header.",
    ),
    # --- authenticated, no owned resource ---------------------------------------------
    _r(
        "GET",
        f"{API}/auth/me",
        "me",
        Gate.USER,
        "FR-AUT-09 — the caller's own profile, by definition.",
    ),
    _r(
        "POST",
        f"{API}/auth/change-password",
        "change_password",
        Gate.USER,
        "FR-USR-09 — a non-administrator changes only their own password.",
        bucket=Bucket.CHANGE_PASSWORD,
    ),
    _r(
        "GET",
        f"{API}/config",
        "get_config",
        Gate.USER,
        "R-73(1) — a product-wide operator setting, read by an authenticated client.",
    ),
    _r(
        "POST",
        f"{API}/conversations",
        "create_conversation",
        Gate.USER,
        "FR-SBR-02 — a new chat is created for the caller.",
    ),
    _r(
        "GET",
        f"{API}/conversations",
        "list_conversations",
        Gate.USER,
        "FR-SBR-03 — scoped to the caller in the query, never filtered afterward.",
    ),
    _r(
        "GET",
        f"{API}/documents",
        "list_documents",
        Gate.USER,
        "R-40(5) — caller-scoped list with deliberately no administrator widening.",
        owns=Owns.CHAT_SCOPE,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "POST",
        f"{API}/documents",
        "upload_document",
        Gate.USER,
        "FR-KBM-05 — the upload is charged to the caller's FR-ERR-02 quota.",
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "POST",
        f"{API}/documents/import",
        "import_document",
        Gate.USER,
        "FR-KBM-10 — imported bytes take the upload's path (R-63(5)).",
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "GET",
        f"{API}/documents/events",
        "stream_documents",
        Gate.USER,
        "R-41(3) — an ordinary bearer stream, caller-scoped, no administrator widening.",
        owns=Owns.CHAT_SCOPE,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
        sse=True,
    ),
    _r(
        "GET",
        f"{API}/cloud/links/{{provider}}",
        "get_link_status",
        Gate.USER,
        "FR-AUT-11 — link state is per-principal; no id is accepted from the client.",
    ),
    _r(
        "POST",
        f"{API}/cloud/links/{{provider}}/start",
        "start_link",
        Gate.USER,
        "R-63(3) — linking is opt-in and initiated by the caller.",
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "DELETE",
        f"{API}/cloud/links/{{provider}}",
        "delete_link",
        Gate.USER,
        "FR-AUT-11 — a caller unlinks their own account.",
    ),
    _r(
        "GET",
        f"{API}/cloud/{{provider}}/files",
        "list_cloud_files",
        Gate.USER,
        "R-74(6) — shares the import budget, so a read is limited too.",
        bucket=Bucket.UPLOAD,
    ),
    # --- authenticated, owned resource ------------------------------------------------
    _r(
        "GET",
        f"{API}/conversations/{{conversation_id}}",
        "get_conversation",
        Gate.USER,
        _R54,
        owns=Owns.CONVERSATION,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "PATCH",
        f"{API}/conversations/{{conversation_id}}",
        "rename_conversation",
        Gate.USER,
        _R54,
        owns=Owns.CONVERSATION,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "DELETE",
        f"{API}/conversations/{{conversation_id}}",
        "delete_conversation",
        Gate.USER,
        _R54,
        owns=Owns.CONVERSATION,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "GET",
        f"{API}/conversations/{{conversation_id}}/messages",
        "list_messages",
        Gate.USER,
        _R54,
        owns=Owns.CONVERSATION,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "POST",
        f"{API}/conversations/{{conversation_id}}/messages",
        "send_message",
        Gate.USER,
        _R54,
        owns=Owns.CONVERSATION,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
        bucket=Bucket.CHAT,
        sse=True,
    ),
    _r(
        "POST",
        f"{API}/messages/{{message_id}}/regenerate",
        "regenerate_message",
        Gate.USER,
        _R55,
        owns=Owns.MESSAGE,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
        bucket=Bucket.CHAT,
        sse=True,
    ),
    _r(
        "POST",
        f"{API}/messages/{{message_id}}/feedback",
        "set_feedback",
        Gate.USER,
        _R55,
        owns=Owns.MESSAGE,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "GET",
        f"{API}/documents/{{document_id}}",
        "get_document",
        Gate.USER,
        _USR04,
        owns=Owns.DOCUMENT,
        admin_widens=True,
        foreign=404,
        admin_foreign=200,
    ),
    _r(
        "GET",
        f"{API}/documents/{{document_id}}/figures/{{content_sha256}}",
        "get_document_figure",
        Gate.USER,
        "NFR-SEC-10 (R-94(6)) - owner-only, and the ONE route under /documents that does not "
        "widen for an administrator. Its siblings disclose management (a listing, a status, a "
        "job id) under FR-USR-04; this discloses content - a rendered region of a page - and "
        "widening it would make it the first route by which an administrator reads another "
        "user's document. NFR-SEC-10 says 'the same predicate as FR-RET-04', which has no "
        "administrator branch. Do not harmonise this with the four rows around it.",
        owns=Owns.DOCUMENT,
        admin_widens=False,
        foreign=404,
        admin_foreign=404,
    ),
    _r(
        "DELETE",
        f"{API}/documents/{{document_id}}",
        "delete_document",
        Gate.USER,
        "R-39(1) — deletion is owner-or-administrator; FR-USR-06 amended.",
        owns=Owns.DOCUMENT,
        admin_widens=True,
        foreign=404,
        admin_foreign=202,
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "POST",
        f"{API}/documents/{{document_id}}/retry",
        "retry_document",
        Gate.USER,
        "R-39(5) — owner-or-administrator, and FAILED-only, so an administrator's success "
        "status depends on the document's state rather than on authorization.",
        owns=Owns.DOCUMENT,
        admin_widens=True,
        foreign=404,
        admin_foreign=None,
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "POST",
        f"{API}/documents/{{document_id}}/replace",
        "replace_document",
        Gate.USER,
        "R-40(2) — owner-or-administrator, permitted from ACTIVE/FAILED.",
        owns=Owns.DOCUMENT,
        admin_widens=True,
        foreign=404,
        admin_foreign=202,
        bucket=Bucket.UPLOAD,
    ),
    _r(
        "GET",
        f"{API}/jobs/{{job_id}}",
        "get_job",
        Gate.USER,
        "R-39(6) — scoped through the job's document, owner-or-administrator.",
        owns=Owns.JOB,
        admin_widens=True,
        foreign=404,
        admin_foreign=200,
    ),
    # --- administrator only -------------------------------------------------------------
    _r(
        "POST",
        f"{API}/users",
        "create_user",
        Gate.ADMIN,
        "FR-USR-03 — account creation is administrator-only, and R-64 declined to delegate it.",
    ),
    _r(
        "GET",
        f"{API}/users",
        "list_users",
        Gate.ADMIN,
        "FR-USR-07 — non-administrators may not enumerate accounts.",
    ),
    _r(
        "PATCH",
        f"{API}/users/{{user_id}}",
        "update_user",
        Gate.ADMIN,
        "FR-USR-07 — role and account-state changes are administrator-only.",
    ),
    _r(
        "DELETE",
        f"{API}/users/{{user_id}}",
        "delete_user",
        Gate.ADMIN,
        "FR-USR-07 — account deletion is administrator-only.",
    ),
    _r(
        "GET",
        f"{API}/audit",
        "list_audit_events",
        Gate.ADMIN,
        "NFR-SEC-08 — the audit trail is an administrator surface.",
    ),
    _r(
        "GET",
        f"{API}/admin/documents/stale",
        "list_stale_documents",
        Gate.ADMIN,
        "R-84(5) — the FR-ING-03 re-embed report is an operator surface. Administrator-only "
        "rather than owner-scoped because it enumerates across owners, and because deciding "
        "to re-embed is a cost decision no document's owner makes.",
    ),
    _r(
        "POST",
        f"{API}/admin/documents/{{document_id}}/reembed",
        "reembed_document",
        Gate.ADMIN,
        "R-84(5) — administrator-only, and deliberately NOT owner-or-administrator like the "
        "other document verbs (R-39(1)): a user re-embedding their own document spends the "
        "deployment's embedding budget. `owns` is therefore NONE — there is no owner cell to "
        "drive, since a non-administrator never reaches the ownership check at all.",
        bucket=Bucket.UPLOAD,
    ),
)


def secured_routes() -> Iterator[RouteDecision]:
    """Every route that demands a credential — the NFR-SEC-01 sweep's domain."""
    return (d for d in ROUTE_DECISIONS if d.gate is not Gate.PUBLIC)


def owned_routes() -> Iterator[RouteDecision]:
    """Every route that checks ownership of an addressable resource."""
    return (d for d in ROUTE_DECISIONS if d.owns is not Owns.NONE)


def expected_status(decision: RouteDecision, principal: Principal) -> int | None:
    """The status `principal` must receive from `decision`, or `None` for no claim.

    The matrix is derived rather than tabulated. `None` means this package deliberately makes
    no claim — an administrator's `retry` outcome, for instance, depends on the document's
    state (R-39(5) makes `/retry` `FAILED`-only), so pinning one status here would assert a
    fixture detail as though it were a requirement.
    """
    if decision.gate is Gate.PUBLIC:
        return None

    match principal:
        case (
            Principal.ANONYMOUS
            | Principal.MALFORMED
            | Principal.WRONG_SIGNATURE
            | Principal.EXPIRED
            | Principal.NO_LOCAL_ROW
        ):
            # `get_principal` rejects the first four; `get_current_user` rejects the fifth.
            # One status, two seams — which is why the sweep asserts the *copy* as well.
            return 401
        case Principal.DEACTIVATED:
            # `get_current_user` runs before `require_admin`'s role check, so a deactivated
            # administrator gets this rather than the role 403.
            return 403
        case Principal.STRANGER:
            if decision.gate is Gate.ADMIN:
                return 403
            return decision.foreign
        case Principal.ADMIN:
            if decision.gate is Gate.ADMIN:
                return None  # reaching it is the claim; the status is the route's own
            return decision.admin_foreign
    return None


PRINCIPALS: Final[tuple[Principal, ...]] = tuple(Principal)


# --- NFR coverage ---------------------------------------------------------------------
#
# The second manifest, on T-601's `SPEC_11_ROWS` precedent and for the same reason: the spec
# is gitignored, so a guard that parsed §5.2 directly would *skip* in the public repo — and a
# test that has only ever skipped is not a passing test (Rev 0.12's rule). The hard rule runs
# against this committed copy everywhere; a spec-gated drift check compares it to §5.2.

#: §5.2's requirement ids, mapped to their first line verbatim.
NFR_SEC_ROWS: Final[dict[str, str]] = {
    "NFR-SEC-01": (
        "Role-based access control with the two-role model (§4.9), enforced server-side on "
        "every request (Governance Agent)."
    ),
    "NFR-SEC-02": (
        "Per-user/per-scope document isolation: non-global documents are never retrievable "
        "by unauthorized users, including during retrieval/grounding."
    ),
    "NFR-SEC-03": (
        "TLS 1.2+ in transit for all client–server communication; server-side access control "
        "(and recommended encryption) at rest; hashed/salted password storage."
    ),
    "NFR-SEC-04": "Password policy and failed-login lockout/throttling — values TBD.",
    "NFR-SEC-05": (
        "Prompt-injection defence: retrieved document text and user input are treated as "
        "data, never as system instructions."
    ),
    "NFR-SEC-06": (
        "Access control enforced at the data layer (FR-RET-04): authorization filters are "
        "applied in the query to the vector/metadata store, not post-hoc in application code."
    ),
    "NFR-SEC-07": (
        "Rate limiting on authentication and chat/upload endpoints, to bound abuse and "
        "runaway cost."
    ),
    "NFR-SEC-08": (
        "Audit logging: security- and lifecycle-relevant events are recorded in an "
        "append-only audit trail distinct from operational telemetry."
    ),
    "NFR-SEC-09": (
        "Recognition runs isolated: FR-ING-07 renders attacker-supplied content, so the "
        "image decoders and the OCR engine run outside the worker process, and no page "
        "raster, image or recognised text leaves the deployment."
    ),
    "NFR-SEC-10": (
        "Figure serving: a figure is a raster re-encoded from a page region, never the "
        "uploaded file; served only over an authenticated route, only to a caller "
        "authorized for the source document under FR-RET-04's predicate, inline, and never "
        "for a document that is not ACTIVE."
    ),
    "NFR-SEC-11": (
        "Browser content restrictions: the origin serving the GUI sends a deny-by-default "
        "Content-Security-Policy with no 'unsafe-inline' and no 'unsafe-eval', admitting "
        "blob: for images, beside nosniff, Referrer-Policy and Permissions-Policy; scoped "
        "to the documents this origin serves and not to a proxied surface with its own."
    ),
}

#: Requirements this package deliberately does not cover, and who owns them instead.
#: Recorded rather than left silently uncovered — an unlisted gap reads as an oversight.
DEFERRED_NFRS: Final[dict[str, str]] = {
    # NFR-SEC-03's transport and at-rest halves are deployment properties with no request-path
    # surface, and password hashing left the application entirely at R-28 (Keycloak owns
    # credentials; there is no `password_hash` column). §8.4 still carries it as a TBD.
    "NFR-SEC-03": "(D) — deployment/TLS and R-28: Keycloak owns credential storage.",
    # Lockout is realm configuration (bruteForceProtected, failureFactor 30), read and
    # recorded by R-72. Keycloak masks a lockout as `invalid_grant` and sends no `Retry-After`,
    # so there is nothing in this codebase to assert beyond the mapping `test_auth.py` covers.
    # Still deferred *by this package* — there is no request-path surface to drive — but no
    # longer uncovered: R-86(1) set the policy in the realm artifact and
    # `tests/test_account_linking.py::test_the_realm_enforces_a_password_policy` guards it
    # offline, including the assertion that no rotation policy is armed (§8.9).
    "NFR-SEC-04": (
        "(D) — R-28/R-72/R-86(1): Keycloak realm configuration, guarded in "
        "tests/test_account_linking.py rather than here."
    ),
    # NFR-SEC-09 is a *worker* and deployment property: recognition has no request-path
    # surface at all, so there is no route for this package's matrix to drive. It is covered,
    # not uncovered - `tests/test_ocr.py` guards both halves (the engine is reachable only
    # over the sidecar socket, and there is no in-process one to reach instead), and R-31's
    # existing guard keeps rasterisation out of the API process (T-217, R-88 §8.78).
    "NFR-SEC-09": (
        "worker-side isolation with no request-path surface; guarded in tests/test_ocr.py "
        "and deployment/ocr/ rather than here."
    ),
    # NFR-SEC-10's deferral is deliberately GONE, not reworded. T-715 built the route and the
    # matrix gained its row, so this package now owns the requirement outright and the entry
    # above it predicted exactly that. Leaving it would still pass — the guard next door is a
    # containment check, not equality — which is precisely why removing it has to be deliberate:
    # a deferral nobody deletes is how a covered requirement goes on reading as uncovered.
    "NFR-SEC-11": (
        "a response header set by the edge, not by the application — there is no request "
        "this package can drive that would exercise it, because nginx is not in the ASGI "
        "stack. Covered instead by tests/test_security_headers.py, which reads "
        "deployment/nginx/ back (R-95)."
    ),
}

_ROW_ATTR = "__nfr_sec_rows__"


def nfr(*ids: str):  # noqa: ANN201 - a decorator returning its argument unchanged
    """Stamp a test with the NFR-SEC requirement(s) it covers.

    A stamp rather than a naming convention because the guard has to read the ids *back*
    (`tests/scenarios/__init__.py` made the same call). Raises at import on an unknown id, so
    a typo fails collection rather than quietly leaving a requirement uncovered.
    """
    unknown = [i for i in ids if i not in NFR_SEC_ROWS]
    if unknown:
        raise ValueError(f"unknown NFR-SEC id(s): {unknown!r}; known: {sorted(NFR_SEC_ROWS)}")

    def decorate(func):  # noqa: ANN001, ANN202
        setattr(func, _ROW_ATTR, tuple(ids))
        return func

    return decorate


def nfrs_of(obj: object) -> tuple[str, ...]:
    return tuple(getattr(obj, _ROW_ATTR, ()))


def covered_nfrs(module: object) -> set[str]:
    return {
        i
        for name in dir(module)
        if name.startswith("test_")
        for i in nfrs_of(getattr(module, name))
    }
