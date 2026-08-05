"""The OpenAPI contract and its drift guards (T-405, R-57).

The generated TypeScript client is only as good as the document it is generated from, and every
way that document can quietly stop describing the app is a way the GUI silently gets a wrong
type. So each link in the chain is a test:

* `backend/openapi.json` no longer matches the app
* an `operationId` collision (FastAPI only warns, and emits the duplicate anyway)
* a new authenticated route with no 401/403
* a throttled route with no 429
* an SSE route that quietly stopped being a stream
* a segment key written with no field to publish it
* an error code that drifted from the constant the route sends

The frontend half of the chain (`openapi.json` → `schema.d.ts`) is not testable from here and
does not need to be: `npm run build` regenerates it, so a stale `.d.ts` cannot reach a build.

Every test here uses the DB-free `openapi_document` fixture. The `app` fixture would skip when
Postgres is down, and a contract that stops being checked without saying so is the failure mode
this whole module exists to prevent.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from typing import Any, get_args

import pytest

from app.api.errors import ContextWindowExceededDetail, NotLatestAnswerDetail
from app.api.events import TurnOutcome, TurnStage
from app.openapi import OPENAPI_PATH, render
from app.rag.citations import CitationLocatorKind, CitationSegment, _citation
from app.rag.errors import CONTEXT_WINDOW_EXCEEDED_CODE, NOT_LATEST_ANSWER_CODE
from app.rag.retrieval import RetrievedChunk

_STALE = f"""\
{OPENAPI_PATH.name} is out of date with the FastAPI app.

Regenerate it and commit the result:
    cd backend && uv run python -m app.openapi --write

Then regenerate the TypeScript client:
    cd frontend && npm run api:generate
"""

#: The three streaming operations, as (path, method).
_SSE_OPERATIONS = [
    ("/api/v1/conversations/{conversation_id}/messages", "post"),
    ("/api/v1/messages/{message_id}/regenerate", "post"),
    ("/api/v1/documents/events", "get"),
]


def _literals(alias: Any) -> set[str]:
    """The members of a PEP-695 `type X = Literal[...]` alias.

    `get_args` on a `TypeAliasType` returns `()` — the alias is a lazy wrapper, and the
    `Literal` lives behind `__value__`.
    """
    return set(get_args(getattr(alias, "__value__", alias)))


def _operations(document: dict[str, Any]):  # noqa: ANN202 — an iterator of (path, method, op)
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if isinstance(operation, dict):
                yield path, method, operation


# ---- the committed artifact ----------------------------------------------------------


def test_the_committed_document_matches_the_app(openapi_document: dict[str, Any]) -> None:
    """`backend/openapi.json` is what `create_app().openapi()` produces, right now."""
    assert OPENAPI_PATH.exists(), _STALE
    committed = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    # Parsed objects, never bytes: a Windows checkout would otherwise fail on CRLF alone.
    assert committed == openapi_document, _STALE


def test_the_committed_document_is_rendered_deterministically() -> None:
    """The file on disk is byte-for-byte what `render` emits, so a rewrite is a no-op diff."""
    raw = OPENAPI_PATH.read_text(encoding="utf-8", newline="")
    assert raw == render(json.loads(raw))


# ---- operation identity --------------------------------------------------------------


def test_every_operation_id_is_unique(openapi_document: dict[str, Any]) -> None:
    """A duplicate would collapse two operations into one in the generated client.

    FastAPI only `warnings.warn`s on a collision and emits the duplicate anyway, so
    `generate_unique_id_function=route.name` is safe only while this passes.
    """
    ids = [operation["operationId"] for _, _, operation in _operations(openapi_document)]
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    assert not duplicates, (
        f"Duplicate operationId(s): {duplicates}. Two handler functions share a name, and "
        "FastAPI emits both — the generated TypeScript would keep only one. Rename the handler "
        "or pass an explicit operation_id= on the route."
    )


def test_operation_ids_are_the_handler_names(openapi_document: dict[str, Any]) -> None:
    """No path mangling reaches the client (`send_message`, not `send_message_api_v1_...`)."""
    ids = {operation["operationId"] for _, _, operation in _operations(openapi_document)}
    assert "send_message" in ids
    assert "set_feedback" in ids
    assert not any("api_v1" in name for name in ids)


# ---- error declarations --------------------------------------------------------------


def test_every_secured_operation_declares_auth_failures(openapi_document: dict[str, Any]) -> None:
    """401/403 are derived from `security`, so a new authenticated route inherits them."""
    missing = [
        f"{method.upper()} {path}"
        for path, method, operation in _operations(openapi_document)
        if "security" in operation and not {"401", "403"} <= set(operation.get("responses", {}))
    ]
    assert not missing, (
        f"Secured operation(s) without 401/403: {missing}. These are injected by "
        "`app/openapi.py::_declare_auth_failures` — if one is missing, that derivation broke."
    )


def test_the_probes_declare_no_auth_failures(openapi_document: dict[str, Any]) -> None:
    """The three probes are reachable without a token, and say so.

    The mirror of the test above: the injection is keyed on `security`, so it must not leak onto
    operations that deliberately have none — an orchestrator's probe would otherwise appear to
    require credentials it will never send. Only the probes are asserted: `/auth/login` also has
    no `security`, but declares its own `401` for a *wrong password*, which is a different fact
    with the same status code.
    """
    for path in ("/health", "/health/ready", "/health/ready/worker"):
        operation = openapi_document["paths"][path]["get"]
        assert "security" not in operation, path
        assert set(operation["responses"]) <= {"200", "503"}, path


def test_every_rate_limited_route_declares_429(openapi_document: dict[str, Any]) -> None:
    """Every route slowapi actually throttles publishes its `429`.

    Read off the limiter's own registry rather than a hand-kept list, so adding a limit without
    declaring the status is a failure here rather than a surprise in the browser.
    """
    from app.security.rate_limit import limiter

    limited = set(limiter._route_limits) | set(limiter._dynamic_route_limits)  # noqa: SLF001
    # The registry keys are `module.function`; map them onto operationIds by handler name.
    limited_names = {key.rsplit(".", 1)[-1] for key in limited}
    # `_chat_rate_limit` is a dependency shared by the two chat routes (T-405), not a handler.
    limited_names.discard("_chat_rate_limit")
    limited_names |= {"send_message", "regenerate_message"}

    missing = [
        operation["operationId"]
        for _, _, operation in _operations(openapi_document)
        if operation["operationId"] in limited_names and "429" not in operation.get("responses", {})
    ]
    assert not missing, f"Rate-limited operation(s) without a declared 429: {missing}"


def test_the_error_codes_match_their_constants() -> None:
    """The two `409` discriminators are the same strings the graph raises.

    `app/rag/errors.py:99` records that a client can tell the two `409`s apart only while the
    codes stay distinct. These `Literal`s are how that distinction reaches the generated types,
    so they must not drift from the constants the route actually sends.
    """
    budget = ContextWindowExceededDetail.model_fields["error_code"].annotation
    latest = NotLatestAnswerDetail.model_fields["error_code"].annotation
    assert get_args(budget) == (CONTEXT_WINDOW_EXCEEDED_CODE,)
    assert get_args(latest) == (NOT_LATEST_ANSWER_CODE,)
    assert CONTEXT_WINDOW_EXCEEDED_CODE != NOT_LATEST_ANSWER_CODE


# ---- the streams ---------------------------------------------------------------------


@pytest.mark.parametrize(("path", "method"), _SSE_OPERATIONS)
def test_the_three_sse_routes_are_streams(path: str, method: str) -> None:
    """`route.is_sse_stream` is True — the guard for the slowapi trap.

    slowapi's decorator wraps an endpoint in an ordinary coroutine function, which makes
    `inspect.isasyncgenfunction` False and silently turns an SSE route into a non-stream that
    returns an un-iterated generator. Every other test in the suite would still pass. Hence the
    rate limit lives in `_chat_rate_limit`, a dependency, and hence this test.
    """
    from fastapi.routing import iter_route_contexts

    from app.main import create_app

    wanted = path.replace("/api/v1", "", 1) if path.startswith("/api/v1") else path
    found = [
        ctx
        for ctx in iter_route_contexts(create_app().routes)
        if getattr(ctx.original_route, "path", None) == wanted
        and method.upper() in getattr(ctx.original_route, "methods", set())
    ]
    assert found, f"no route registered for {method.upper()} {path}"
    assert all(ctx.original_route.is_sse_stream for ctx in found), (
        f"{method.upper()} {path} is no longer an SSE stream. The usual cause is a decorator "
        "(slowapi's especially) wrapping the handler and destroying its async-generator "
        "identity — move it to a dependency."
    )


@pytest.mark.parametrize(("path", "method"), _SSE_OPERATIONS)
def test_the_stream_item_schema_carries_the_frame_union(
    openapi_document: dict[str, Any], path: str, method: str
) -> None:
    """The 200 publishes the frame union, under the media type it is served as.

    Both keys matter and for different readers: `schema` is what `openapi-typescript` emits a
    type from, `itemSchema.data.contentSchema` is the OpenAPI 3.2 streaming construct a
    stream-aware tool reads. `app/openapi.py::_repair_stream_media_types` keeps them equal.
    """
    content = openapi_document["paths"][path][method]["responses"]["200"]["content"]
    assert list(content) == ["text/event-stream"]
    stream = content["text/event-stream"]
    ref = stream["schema"]["$ref"]
    assert ref.startswith("#/components/schemas/")
    assert stream["itemSchema"]["properties"]["data"]["contentSchema"] == stream["schema"]
    assert stream["itemSchema"]["required"] == ["data"]


@pytest.mark.parametrize(("path", "method"), _SSE_OPERATIONS)
def test_an_sse_routes_error_bodies_are_json(
    openapi_document: dict[str, Any], path: str, method: str
) -> None:
    """FastAPI files declared error models under the route's media type; they are JSON."""
    responses = openapi_document["paths"][path][method]["responses"]
    for code, response in responses.items():
        if code == "200":
            continue
        content = response.get("content")
        if content:
            assert "text/event-stream" not in content, f"{code} on {method.upper()} {path}"


def test_every_frame_is_discriminated_by_its_event_name(openapi_document: dict[str, Any]) -> None:
    """A client narrows on `event` alone, which is why the frame is an envelope.

    The three chat payloads are not mutually discriminable — `done`'s `{outcome}` is a subset of
    `message`'s keys — so without the envelope the union could not be narrowed at all.
    """
    schemas = openapi_document["components"]["schemas"]
    for union in ("ChatStreamFrame", "DocumentStreamFrame"):
        assert schemas[union]["discriminator"]["propertyName"] == "event", union
        for ref in schemas[union]["oneOf"]:
            member = schemas[ref["$ref"].rsplit("/", 1)[-1]]
            assert member["properties"]["event"]["const"], member


def test_the_stage_vocabulary_matches_the_chat_service() -> None:
    """`TurnStage` is restated in the API layer; it must stay equal to what is emitted."""
    from app.services.chat import _STAGES  # noqa: PLC0415

    assert _literals(TurnStage) == set(_STAGES.values())


def test_the_outcome_vocabulary_matches_the_graph_state() -> None:
    """`TurnOutcome` mirrors `app.rag.state.Outcome`, which the schema would otherwise inline."""
    from app.rag.state import Outcome  # noqa: PLC0415

    assert _literals(TurnOutcome) == _literals(Outcome)


# ---- the FR-MSG-06 envelope ----------------------------------------------------------


def _hit() -> RetrievedChunk:
    """One retrieval hit, shaped like `tests/test_citations.py`'s."""
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="handbook.pdf",
        chunk_index=0,
        chunk_text="the passage",
        score=1.0,
        meta={"locator": {"kind": "page", "label": "p. 4", "page": 4}},
    )


def test_the_citation_model_covers_every_key_written() -> None:
    """A key added to `_citation` without a field here would vanish from the DTO.

    `_citation` builds the model, so this cannot actually drift — the test pins that fact, so a
    future refactor back to a dict literal fails here rather than in the browser.
    """
    written = set(_citation(_hit(), score=0.5))
    assert written == set(CitationSegment.model_fields)


def test_the_score_key_is_absent_rather_than_null() -> None:
    """R-47(2): "no score" and "a score of zero" must stay distinguishable."""
    assert "score" not in _citation(_hit(), score=None)
    assert _citation(_hit(), score=0.0)["score"] == 0.0


def test_the_locator_kinds_match_the_parsers() -> None:
    """`CitationLocatorKind` is restated to keep PyMuPDF out of the API process."""
    from app.ingestion.parsers.base import LocatorKind  # noqa: PLC0415

    assert _literals(CitationLocatorKind) == {kind.value for kind in LocatorKind}


def test_the_segments_field_is_a_typed_union(openapi_document: dict[str, Any]) -> None:
    """`segs` reaches the client as a union, not `Record<string, never>[]`.

    This is the whole reason T-505 may render a citation chip without hand-writing its type.
    """
    schemas = openapi_document["components"]["schemas"]
    items = schemas["MessageResponse"]["properties"]["segs"]["items"]
    assert items == {"$ref": "#/components/schemas/Segment"}
    assert {"$ref": "#/components/schemas/CitationSegment"} in schemas["Segment"]["anyOf"]
    assert {"$ref": "#/components/schemas/TextSegment"} in schemas["Segment"]["anyOf"]


# ---- relocated from test_feedback_api.py (T-403 wrote it for this task) ---------------


def test_the_feedback_key_is_required_in_the_openapi_schema(
    openapi_document: dict[str, Any],
) -> None:
    """R-55(4): `{}` must be a `422`, not a silent erasure of a rating the user gave.

    A default added later would silently make the field optional in the TypeScript type, and the
    composer could then send `{}` — the silent-erasure path — with the type checker's blessing.
    """
    schemas = openapi_document["components"]["schemas"]

    assert schemas["FeedbackRequest"]["required"] == ["feedback"]
    # Required **and** nullable: the generated type is `feedback: Feedback | null`, not
    # `feedback?: Feedback`. The `null` arm is FR-MSG-06's third state, not an optional key.
    variants = schemas["FeedbackRequest"]["properties"]["feedback"]["anyOf"]
    assert {"type": "null"} in variants
    assert {"$ref": "#/components/schemas/Feedback"} in variants
    assert schemas["Feedback"]["enum"] == ["up", "down"]
