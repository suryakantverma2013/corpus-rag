"""FastAPI application factory (T-003 scaffold).

Boots the app with structured logging and mounts the routers (system probes at the
root, the versioned API under `/api/v1`). CORS and the LangGraph wiring land in their
own tasks. Run: `uv run uvicorn app.main:app`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute
from slowapi.errors import RateLimitExceeded

from app.api import health
from app.api.router import api_router
from app.config import Settings, get_settings
from app.logging_config import configure_logging
from app.logging_context import request_context_middleware
from app.openapi import customize_openapi
from app.rag.graph import close_graph
from app.security.rate_limit import limiter, rate_limit_exceeded_handler
from app.services.checkpointer import close_checkpointer
from app.services.embeddings import close_embedding_client
from app.services.jobs import close_job_queue
from app.services.llm import close_chat_client
from app.services.object_storage import close_object_storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Process-lifetime resources.

    Object storage (T-201), the arq job queue (T-202), the embedding client (T-205), the chat
    client (T-304) and the LangGraph checkpointer (T-301) all pool connections bound to this loop,
    so all of them are released on shutdown. Nothing is *opened* here: each client is built
    lazily on first use, so a cold MinIO, Redis, OpenAI or Postgres cannot stop the API from
    booting (the readiness probe, NFR-REL-02, is what reports the ones it covers; OpenAI is
    deliberately not probed).

    The graph is dropped before the checkpointer it closes over — the reverse order would
    leave a compiled graph holding a saver whose pool is already gone.
    """
    try:
        yield
    finally:
        await close_graph()
        await close_checkpointer()
        await close_object_storage()
        await close_job_queue()
        await close_embedding_client()
        await close_chat_client()


#: Tag descriptions, so `/docs` and the generated client group operations under something a
#: reader recognises. The order here is the order Swagger renders them in.
_OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "auth", "description": "Login, refresh, logout and the caller's own profile (§4.17)."},
    {"name": "conversations", "description": "Chats: create, list, rename, delete (FR-SBR-*)."},
    {"name": "messages", "description": "Ask, read, rate and regenerate answers (FR-MSG-*)."},
    {"name": "documents", "description": "Knowledge-base files and their live status (FR-KBM-*)."},
    {"name": "jobs", "description": "Ingestion and deletion job status (FR-ING-*)."},
    {"name": "users", "description": "Administrator user management (FR-USR-*)."},
    {"name": "audit", "description": "Audit trail (NFR-OBS-03)."},
    {"name": "system", "description": "Unversioned liveness and readiness probes (NFR-REL-02)."},
]

_DESCRIPTION = """\
Backend for the Corpus RAG chatbot GUI.

Every operation outside `auth` and `system` requires a realm-signed RS256 bearer token from
`POST /api/v1/auth/login` (R-28), and is scoped to the caller: a resource belonging to another
user answers `404`, never `403`, for an administrator too (R-54(1), NFR-SEC-02).

Three operations stream Server-Sent Events rather than returning a body. They authenticate with
the ordinary `Authorization` header, which `EventSource` cannot send, so a browser client must
consume them with `fetch` + `ReadableStream` (R-41(3)). Each frame's `data:` line is one JSON
object carrying its own `event` discriminator.
"""


def _operation_id(route: APIRoute) -> str:
    """Use the handler's own name as the `operationId`.

    The default is a path-mangled `send_message_api_v1_conversations__conversation_id_..._post`,
    which becomes the generated TypeScript symbol verbatim. Uniqueness is **not** left to
    convention: FastAPI only `warnings.warn`s on a duplicate and emits it anyway, silently
    collapsing two operations into one in the generated `operations` map, so
    `test_every_operation_id_is_unique` enforces it.
    """
    return route.name


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    configure_logging()
    settings = settings or get_settings()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        summary="Corpus RAG Chatbot backend (Nexus AI platform).",
        description=_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        # Same-origin: production serves the SPA and the API behind one reverse proxy, and the
        # Vite dev server proxies `/api` to reproduce that locally. So there is no CORS
        # middleware anywhere in this app, and adding one would be dev-only configuration
        # living permanently in production code.
        servers=[{"url": "/", "description": "Same origin as the GUI."}],
        # Pinned rather than left at FastAPI's default `True`, which splits any model used as
        # both a request body and a response into `-Input`/`-Output` pairs. Verified a no-op on
        # today's document — pinning it means the first DTO reused that way does not silently
        # rename two components and break every generated import.
        separate_input_output_schemas=False,
        generate_unique_id_function=_operation_id,
        lifespan=lifespan,
    )

    # Rate limiting (T-105, NFR-SEC-07). The limiter is a module-global bound to the
    # route decorators; the factory only needs to expose it on app.state and register
    # the handler that renders the FR-AUT-04 copy. No SlowAPIMiddleware: limits are
    # applied per-route, not app-wide.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Request correlation (T-604, NFR-OBS-01, R-79(2)). The one piece of app-wide
    # middleware in this app, and it is the kind the CORS note above argues against adding:
    # it is production behaviour, not dev-only configuration. Every log line a request
    # produces carries the id, and the response echoes it, so a user reporting a problem
    # quotes one string that finds the whole request. It reads and writes no body.
    app.middleware("http")(request_context_middleware)

    # System probes (T-106, NFR-REL-02): liveness + dependency readiness. Mounted at
    # the root — orchestrators expect unversioned probe paths (R-29), so these sit
    # outside the /api/v1 surface.
    app.include_router(health.router)

    # Source A §6 REST surface is versioned under /api/v1 (R-25).
    app.include_router(api_router, prefix="/api/v1")

    # After both inclusions: the derived polish (T-405) reads the finished document, and the
    # 401/403 injection keys on the `security` block every authenticated route contributes.
    customize_openapi(app)

    return app


app = create_app()
