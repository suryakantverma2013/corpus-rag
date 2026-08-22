"""Every HTTP route the documentation names still exists (T-705).

This is the fourth link in a chain whose first three are already guarded, which is the only reason
it can read a committed file rather than build the application:

    the running app  ->  backend/openapi.json  ->  docs/HTTP_API.md  ->  the prose that cites it
                     (test_openapi_contract)   (test_http_docs)      (here)

`docs/HTTP_API.md` is excluded: it *is* the route list, generated from the same document and
compared to it byte for byte, so re-checking it here would be a second copy of an existing guard
rather than coverage.

**Only one direction is asserted.** Every route named in hand-written prose must exist; the inverse
-- every route appears somewhere hand-written -- is deliberately not required, because the generated
contract carries all 38 and prose should name the ones worth explaining.

Two normalisations, both earned by real citations rather than invented: a path parameter is matched
by position and not by name (`{id}` in `docs/MODULE_MAP.md` is `{user_id}` in the contract), and the
`/api/v1` prefix may be elided in prose that has already established it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from tests.docs import BACKEND_ROOT, ascii_only, documents

_MENTION: Final = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-{}/.]*)")
_PARAMETER: Final = re.compile(r"\{[^}]*\}")
_API: Final = "/api/v1"

#: Routes belonging to somebody else's API, name -> reason. A set would grow by one token in a
#: diff and leave the reviewer nothing to review.
FOREIGN_ROUTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "GET /clients": (
            "Keycloak's admin REST API, not ours. deployment/keycloak/README.md documents "
            "GET /clients?clientId=broker, the lookup KeycloakClient.admin_get_client_uuid makes "
            "-- the note exists because the lighter query-clients role answers it 200 [] rather "
            "than 403, so the broker client reads as not existing (T-214)"
        ),
    }
)


def _contract() -> frozenset[tuple[str, str]]:
    """`(METHOD, normalised path)` for every operation the committed contract declares."""
    spec = json.loads((BACKEND_ROOT / "openapi.json").read_text(encoding="utf-8"))
    return frozenset(
        (method.upper(), _PARAMETER.sub("{}", path))
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    )


def _mentions() -> dict[tuple[str, str], set[str]]:
    """Every `METHOD /path` written outside the generated contract, mapped to where."""
    found: dict[tuple[str, str], set[str]] = {}
    for page in documents():
        if page.doc.generated:
            continue
        for match in _MENTION.finditer(page.text):
            method = match.group(1)
            path = _PARAMETER.sub("{}", match.group(2).rstrip(".,;)"))
            found.setdefault((method, path), set()).add(page.path)
    return found


def _resolves(mention: tuple[str, str], contract: frozenset[tuple[str, str]]) -> bool:
    method, path = mention
    return (method, path) in contract or (method, _API + path) in contract


def test_every_documented_route_exists_in_the_committed_contract() -> None:
    contract = _contract()
    missing: list[str] = []
    for mention, where in sorted(_mentions().items()):
        if _resolves(mention, contract) or f"{mention[0]} {mention[1]}" in FOREIGN_ROUTES:
            continue
        missing.append(f"  {mention[0]} {ascii_only(mention[1])}  ({', '.join(sorted(where))})")

    assert not missing, (
        f"{len(missing)} route(s) named in the documentation are not in backend/openapi.json:\n"
        + "\n".join(missing)
        + "\n\nA reader following the documentation gets a 404. Path parameters are matched by "
        "position rather than by name and a missing /api/v1 prefix is tolerated, so this is a "
        "route that moved or was removed -- not a spelling difference. If it belongs to another "
        "system, add it to FOREIGN_ROUTES with a sentence saying whose."
    )


def test_every_foreign_route_exclusion_is_still_cited_and_carries_a_reason() -> None:
    """Delete the paragraph that mentions it and the exclusion must go with it."""
    mentions = _mentions()
    stale: list[str] = []
    for name, reason in FOREIGN_ROUTES.items():
        method, _, path = name.partition(" ")
        if not reason.strip():
            stale.append(f"  {name}: excluded with no reason")
        elif (method, path) not in mentions:
            stale.append(f"  {name}: excluded, but no document names it any more")

    assert not stale, "the foreign-route exclusions have drifted:\n" + "\n".join(sorted(stale))


def test_the_documentation_mentions_routes_across_several_areas() -> None:
    """Anti-vacuity: a scanner matching nothing would satisfy the assertions above."""
    contract = _contract()
    mentions = _mentions()
    resolved = {m for m in mentions if _resolves(m, contract)}
    pages = {path for mention in resolved for path in mentions[mention]}

    assert len(contract) > 30, "the committed contract looks truncated"
    assert len(resolved) >= 10, (
        f"only {len(resolved)} documented routes resolved: {sorted(resolved)}"
    )
    assert len(pages) >= 3, f"routes are cited in only {len(pages)} document(s): {sorted(pages)}"
