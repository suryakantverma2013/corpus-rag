"""OpenAPI customisation and the schema-export CLI (T-405, R-57).

Two jobs, deliberately in one module because they must never diverge: the polish applied to the
**served** document, and the command that writes that same document to `backend/openapi.json`
for the frontend's type generator.

    uv run python -m app.openapi --write     # rewrite backend/openapi.json
    uv run python -m app.openapi             # print it

`python -m` rather than a `[project.scripts]` entry: it needs no reinstall to appear, works
under a bare `uv run`, and this repo has no console-script precedent.

**`customize_openapi` wraps `app.openapi` rather than re-calling `get_openapi(...)`.** FastAPI's
own documented recipe re-invokes the generator with a hand-copied argument list, which is
precisely the drift this task exists to remove: miss `webhooks=` or
`separate_input_output_schemas=` and the served document silently stops matching the app that
serves it. Wrapping inherits every present and future argument for free.

What it derives — and *derives* is the operative word, since nothing here is hand-maintained:

* **401/403 on every secured operation.** `HTTPBearer(auto_error=False)` means `get_principal`
  raises these itself, so FastAPI cannot see them and 24 operations declared neither. The
  `security` block is an exact proxy for "this operation resolved the bearer scheme", so the
  injection keys on it and a route added tomorrow inherits the declaration.

Everything *not* derivable stays an explicit `responses=` at the route, out of shared fragments
in `app/api/errors.py`. The split is the point: a fact the document already contains is read
from it, and a fact only the author knows is written down once.

This module stays cheap to import — `main()` imports `create_app` inside the function, both to
break the `app.main` → here → `app.main` cycle and because `app/rag/errors.py:226` already
records that schema generation is a consumer it defers heavy imports for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.api.errors import ErrorResponse

__all__ = ["OPENAPI_PATH", "customize_openapi", "main", "render"]

#: The committed artifact. `backend/openapi.json` — the backend's own output, so it lives in the
#: backend's tree; writing it into `frontend/` would make the producer reach into the consumer.
OPENAPI_PATH = Path(__file__).resolve().parent.parent / "openapi.json"

#: Injected into every operation that declares `security`. The copy describes what a client
#: should *do*, since neither is actionable by retrying the same request.
_AUTH_FAILURES: dict[str, str] = {
    "401": "Missing, malformed or expired bearer token, or no local user record.",
    "403": "The account is deactivated, or the operation is administrator-only (NFR-SEC-01).",
}

_ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}


def customize_openapi(app: FastAPI) -> None:
    """Install the derived polish on ``app.openapi``. Idempotent per app instance."""
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        _ensure_error_component(schema)
        _declare_auth_failures(schema)
        _repair_stream_media_types(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def _ensure_error_component(schema: dict[str, Any]) -> None:
    """Guarantee `ErrorResponse` is a component before anything `$ref`s it.

    It is referenced by ~20 routes today, so this is belt-and-braces — but a dangling `$ref` is
    the one failure mode that breaks the *generator* rather than a route, and it would surface
    as an unreadable TypeScript error rather than as a missing status code.
    """
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    if "ErrorResponse" not in components:
        components["ErrorResponse"] = ErrorResponse.model_json_schema()


def _declare_auth_failures(schema: dict[str, Any]) -> None:
    """Add 401/403 to every operation carrying `security` (see the module docstring).

    `setdefault` throughout: a route that declares its own 401 or 403 — with sharper copy for
    its own case — keeps it. This only fills the silence.
    """
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "security" not in operation:
                continue
            responses = operation.setdefault("responses", {})
            for code, description in _AUTH_FAILURES.items():
                responses.setdefault(
                    code,
                    {
                        "description": description,
                        "content": {"application/json": {"schema": _ERROR_REF}},
                    },
                )


#: The success status whose body on an SSE route *is* the stream. Every other declared status on
#: such a route is an ordinary JSON error body.
_STREAM_STATUS = "200"

_SSE_MEDIA_TYPE = "text/event-stream"
_JSON_MEDIA_TYPE = "application/json"


def _repair_stream_media_types(schema: dict[str, Any]) -> None:
    """Fix what FastAPI files under the wrong media type on an SSE route.

    A route whose `response_class` is an `EventSourceResponse` has `text/event-stream` as its
    *route* media type, and FastAPI files **every** declared `responses={...: {"model": ...}}`
    under it — including the `404`/`409`/`429` error bodies, which are plain JSON. Left alone, a
    generated client would type those errors as a streaming media type it has no reader for.

    Two derived repairs, both keyed on the document rather than on a list of SSE routes, so a
    fourth stream added later is covered without touching this function:

    1. **Error bodies move to `application/json`.** The marker is a non-`200` response carrying
       a plain `schema` under `text/event-stream`.

    2. **The stream's own schema is folded into `itemSchema`.** FastAPI 0.139.2 has a defect
       here: `RouteContext` forwards `is_sse_stream` but not `stream_item_field`, so a route
       reached through `include_router` — every route in this app — emits the generic SSE event
       schema with an untyped `data`. The routes therefore declare the frame union explicitly,
       and this folds it into the OpenAPI 3.2 `itemSchema` where a streaming-aware tool looks
       for it. The `schema` key is deliberately **left in place**: `openapi-typescript` reads
       that and does not understand `itemSchema`, so it is what actually types the client.

    If a later FastAPI populates `contentSchema` itself, (2) becomes a no-op that overwrites an
    identical value — checked by `test_the_stream_item_schema_carries_the_frame_union`.
    """
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for code, response in operation.get("responses", {}).items():
                content = response.get("content")
                if not isinstance(content, dict):
                    continue
                stream = content.get(_SSE_MEDIA_TYPE)
                if not isinstance(stream, dict) or "schema" not in stream:
                    continue
                if code != _STREAM_STATUS:
                    content[_JSON_MEDIA_TYPE] = content.pop(_SSE_MEDIA_TYPE)
                    continue
                item_schema = stream.get("itemSchema")
                if isinstance(item_schema, dict):
                    item_schema["required"] = ["data"]
                    item_schema.setdefault("properties", {})["data"] = {
                        "type": "string",
                        "contentMediaType": _JSON_MEDIA_TYPE,
                        "contentSchema": stream["schema"],
                    }


def render(schema: dict[str, Any]) -> str:
    """Serialise deterministically.

    Byte-stability is a requirement, not tidiness: `test_openapi_contract.py` compares the
    committed file against a freshly generated document, and on a Windows checkout an
    unconstrained `write_text` would introduce CRLF and make that test flap. `ensure_ascii=False`
    for the same reason — the copy strings contain en dashes.
    """
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Print the OpenAPI document, or write it to :data:`OPENAPI_PATH` with ``--write``."""
    parser = argparse.ArgumentParser(prog="python -m app.openapi", description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"rewrite {OPENAPI_PATH.name} in place instead of printing to stdout",
    )
    args = parser.parse_args(argv)

    # Local import: `app.main` imports `customize_openapi` from this module at module level.
    from app.main import create_app

    document = render(create_app().openapi())

    if args.write:
        # `newline=""` leaves the "\n" from `render` untouched on Windows.
        OPENAPI_PATH.write_text(document, encoding="utf-8", newline="")
        print(f"wrote {OPENAPI_PATH}")
    else:
        sys.stdout.write(document)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess, not imported
    raise SystemExit(main())
