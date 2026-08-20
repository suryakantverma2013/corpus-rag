"""The structural guards: the manifest must match the running application (T-602).

These are the tests that make the matrix an instrument rather than a snapshot. Every other
module here asserts a status code; these four assert that the *set* of things being asserted
is still the right set. Without them the suite would keep passing while the product grew past
it — which is exactly how the gap this task exists to close was created.

Guard 2 is the highest-value single assertion in the package, and it is worth saying why:
removing `CurrentUser` from `GET /jobs/{job_id}` leaves **every existing behaviour test in
the repository green**, because the route still works perfectly for the authenticated caller
each of those tests supplies. The route is simply open. Guard 2 fails on it by reading the
dependency graph, and the anonymous sweep fails on it by asking — two independent oracles,
which is the property §8.65(5) asks for after two T-601 scenarios passed while naming guards
they never reached.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import tests.security
from app.auth.dependencies import get_current_user, require_admin
from app.main import create_app
from app.security.rate_limit import limiter
from tests.security import (
    API,
    DEFERRED_NFRS,
    NFR_SEC_ROWS,
    ROUTE_DECISIONS,
    Bucket,
    Gate,
    covered_nfrs,
    nfr,
)

#: FastAPI's own documentation surface — not application routes, and not this task's business.
_DOC_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

_SPEC = Path(__file__).resolve().parents[3] / "Nexus_AI_Detailed_Specification.md"
requires_spec = pytest.mark.skipif(
    not _SPEC.is_file(), reason="the specification is gitignored and absent from this checkout"
)


def _flatten(dependant):  # noqa: ANN001, ANN202
    yield dependant
    for sub in dependant.dependencies:
        yield from _flatten(sub)


def _live_routes() -> dict[tuple[str, str], object]:
    """Every application route, keyed `(METHOD, full path)`.

    `iter_route_contexts` is the seam `test_openapi_contract.py` already uses; it hands back
    the *router-local* path, so the `/api/v1` mount has to be reapplied here. The health
    probes are mounted at the root and are the exception.
    """
    from fastapi.routing import iter_route_contexts

    live: dict[tuple[str, str], object] = {}
    for ctx in iter_route_contexts(create_app().routes):
        route = ctx.original_route
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or not hasattr(route, "dependant"):
            continue
        if path in _DOC_PATHS:
            continue
        full = path if path.startswith("/health") else f"{API}{path}"
        for method in set(methods) - {"HEAD", "OPTIONS"}:
            live[(method, full)] = route
    return live


@nfr("NFR-SEC-01")
def test_every_live_route_has_an_authorization_decision() -> None:
    """Guard 1 — the manifest and the application describe the same set of routes.

    This is the anti-rot mechanism. A new endpoint fails the suite until someone writes down
    who may call it and which ruling says so, which is the only way a matrix survives contact
    with a growing product.
    """
    live = set(_live_routes())
    declared = {d.key for d in ROUTE_DECISIONS}

    undeclared = sorted(f"{m} {p}" for m, p in live - declared)
    assert not undeclared, (
        f"{len(undeclared)} route(s) are live with no T-602 authorization decision: "
        f"{undeclared}. Add a `RouteDecision` to `tests/security/__init__.py` naming the "
        "gate, the ownership behaviour and the ruling that fixes its status codes."
    )

    stale = sorted(f"{m} {p}" for m, p in declared - live)
    assert not stale, (
        f"{len(stale)} declared route(s) no longer exist: {stale}. Remove the decision, or "
        "the matrix is asserting against a surface the product does not have."
    )


@nfr("NFR-SEC-01")
@pytest.mark.parametrize("decision", ROUTE_DECISIONS, ids=lambda d: f"{d.method} {d.path}")
def test_the_declared_gate_matches_the_dependency_graph(decision) -> None:  # noqa: ANN001
    """Guard 2 — the manifest's `gate` is derived from the app and compared, never trusted.

    Two oracles for one fact. The behavioural sweep asks the route; this reads the dependency
    tree. A route that lost its `CurrentUser` still answers every existing test correctly, so
    only the structural read catches it before a caller does.
    """
    route = _live_routes().get(decision.key)
    assert route is not None, f"{decision.method} {decision.path} is not live"

    calls = {d.call for d in _flatten(route.dependant)}
    if require_admin in calls:
        derived = Gate.ADMIN
    elif get_current_user in calls:
        derived = Gate.USER
    else:
        derived = Gate.PUBLIC

    assert derived is decision.gate, (
        f"{decision.method} {decision.path} resolves {derived} but the manifest declares "
        f"{decision.gate}. Either the route's authentication changed — in which case this is "
        f"the failure that was supposed to catch it — or the decision is wrong. The manifest "
        f"cites: {decision.why}"
    )


@nfr("NFR-SEC-07")
def test_the_declared_rate_limit_buckets_match_the_limiter() -> None:
    """Guard 3 — every limited route is declared, and every declared bucket is real.

    slowapi's registry is keyed `module.function` and knows nothing about paths, which is why
    `RouteDecision.name` exists. `_chat_rate_limit` is a shared **dependency**, so it maps onto
    the two chat routes rather than onto a route of its own.

    **Known blind spot, stated rather than papered over:** the registry cannot see the
    `scope=` string, so this guard would not notice `_CHAT_BUCKET` being split into two
    scopes and handing one user two chat budgets. That is what
    `test_rate_limits.py::test_send_and_regenerate_share_one_bucket` is for.
    """
    registered = set(limiter._route_limits) | set(limiter._dynamic_route_limits)
    create_app()  # decorators bind at import; this guarantees the modules are loaded

    chat_routes = {d.name for d in ROUTE_DECISIONS if d.bucket is Bucket.CHAT}
    derived: set[str] = set()
    for key in registered:
        handler = key.rsplit(".", 1)[-1]
        derived |= chat_routes if handler == "_chat_rate_limit" else {handler}

    declared = {d.name for d in ROUTE_DECISIONS if d.bucket is not None}
    assert derived == declared, (
        "the limiter's registry and the manifest disagree about which routes are throttled. "
        f"limited but undeclared: {sorted(derived - declared)}; declared but unlimited: "
        f"{sorted(declared - derived)}. NFR-SEC-07 names authentication, chat and upload."
    )


@nfr("NFR-SEC-01")
@pytest.mark.parametrize(
    "decision",
    [d for d in ROUTE_DECISIONS if d.gate is not Gate.PUBLIC],
    ids=lambda d: f"{d.method} {d.path}",
)
def test_every_authorization_status_is_a_declared_one(
    decision,
    openapi_document: dict,  # noqa: ANN001
) -> None:
    """R-77(2) — a route may not answer a status its contract does not declare.

    The generated TypeScript client (R-57) derives its error handling from this document, so an
    undeclared status is a response the frontend has no type for, and a declared-but-impossible
    one is a branch nobody can ever reach. T-602 found both on the same two routes:
    `PATCH`/`DELETE /users/{id}` declared `403` with the self-mutation copy while the handlers
    raise `409`, and `DELETE` declared no `409` at all.

    `test_openapi_contract.py` did not catch it because it asks whether secured operations
    *declare* 401 and 403 — never whether what they declare is what they return. This closes
    that direction for the statuses this package actually drives.
    """
    path = decision.path
    operation = openapi_document["paths"].get(path, {}).get(decision.method.lower())
    assert operation is not None, f"{decision.method} {path} is missing from the OpenAPI document"

    declared = {int(code) for code in operation.get("responses", {}) if str(code).isdigit()}
    observed = {
        status
        for status in (401, 403, decision.foreign, decision.admin_foreign)
        if status is not None and status >= 400
    }

    undeclared = sorted(observed - declared)
    assert not undeclared, (
        f"{decision.method} {path} answers {undeclared} but declares {sorted(declared)}. "
        "A client generated from this document has no type for that response."
    )


@nfr("NFR-SEC-01", "NFR-SEC-02", "NFR-SEC-05", "NFR-SEC-06", "NFR-SEC-07", "NFR-SEC-08")
def test_every_security_requirement_has_a_test_or_a_recorded_deferral() -> None:
    """Guard 4 — no NFR-SEC row is silently uncovered.

    A requirement with no test and no recorded reason is indistinguishable from one that was
    forgotten. The deferrals are as much a part of the claim as the coverage: NFR-SEC-03 and
    -04 left application code at R-28 and are realm configuration, and saying so here is what
    stops a later reader treating their absence as an oversight.
    """
    covered: set[str] = set()
    for info in pkgutil.iter_modules(tests.security.__path__):
        if not info.name.startswith("test_"):
            continue
        covered |= covered_nfrs(importlib.import_module(f"tests.security.{info.name}"))

    accounted = covered | set(DEFERRED_NFRS)
    missing = sorted(set(NFR_SEC_ROWS) - accounted)
    assert not missing, (
        f"{missing} have neither a stamped test nor a recorded deferral. Stamp a test with "
        "`@nfr(...)`, or record why the requirement is out of scope in `DEFERRED_NFRS`."
    )


def test_no_test_claims_a_requirement_that_does_not_exist() -> None:
    """The guard on the guard — `@nfr` raises at import, so this pins that it still does."""
    with pytest.raises(ValueError, match="unknown NFR-SEC id"):
        nfr("NFR-SEC-99")


@requires_spec
@nfr("NFR-SEC-01")
def test_the_manifest_still_matches_the_specification() -> None:
    """Drift check — §5.2's requirement ids, compared to the committed copy.

    Spec-gated on T-601's precedent: the specification is gitignored, so a guard that parsed
    it directly would *skip* in the public repo, and a test that has only ever skipped is not
    a passing test. The hard rule above runs everywhere against the committed manifest; this
    catches the manifest drifting away from its source.
    """
    text = _SPEC.read_text(encoding="utf-8")
    # `(?:~~)?` is load-bearing: a requirement whose `(D)` has been resolved keeps the old
    # heading struck through beside the new one — `~~**NFR-SEC-03 (D)**~~ **NFR-SEC-03**` — which
    # is the convention NFR-OBS-04 established in Rev 0.46. Without it, resolving a `(D)` makes
    # the requirement vanish from this guard's view and the manifest looks like it drifted.
    # `tests/acceptance`'s twin already tolerated the form; this one had never been asked to.
    found = re.findall(r"^- (?:~~)?\*\*(NFR-SEC-\d+)", text, flags=re.MULTILINE)
    assert found, "§5.2 could not be located — the requirement bullets changed shape"
    assert set(found) == set(NFR_SEC_ROWS), (
        f"§5.2 declares {sorted(set(found))} but the manifest carries "
        f"{sorted(NFR_SEC_ROWS)}. Update `NFR_SEC_ROWS` in the same commit as the spec."
    )
