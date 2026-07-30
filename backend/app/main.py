"""FastAPI application factory (T-003 scaffold).

Boots the app with structured logging and mounts the routers (system probes at the
root, the versioned API under `/api/v1`). CORS and the LangGraph wiring land in their
own tasks. Run: `uv run uvicorn app.main:app`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from app.api import health
from app.api.router import api_router
from app.config import Settings, get_settings
from app.rag.graph import close_graph
from app.security.rate_limit import limiter, rate_limit_exceeded_handler
from app.services.checkpointer import close_checkpointer
from app.services.embeddings import close_embedding_client
from app.services.jobs import close_job_queue
from app.services.llm import close_chat_client
from app.services.object_storage import close_object_storage


def _configure_logging() -> None:
    """Route stdlib logging through structlog with a JSON-friendly processor chain.

    Logger caching comes from `LOG_CACHE_LOGGERS` rather than being hard-coded, and that is
    load-bearing rather than fussy. `cache_logger_on_first_use=True` latches every
    module-level `structlog.get_logger(__name__)` onto whichever processor chain was
    configured at its first use, and **nothing un-latches it** — not `structlog.configure`,
    not `structlog.testing.capture_logs`, which is how log assertions are written. This
    module builds the app at import time (`app = create_app()` below, the uvicorn
    entrypoint), so the latch closes before any test fixture can run; only a decision made
    in the environment *before* the import can keep it open. `tests/conftest.py` does that.
    The processor chain is identical either way, so the suite still exercises production's.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=get_settings().log_cache_loggers,
    )


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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    _configure_logging()
    settings = settings or get_settings()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        summary="Corpus RAG Chatbot backend (Nexus AI platform).",
        lifespan=lifespan,
    )

    # Rate limiting (T-105, NFR-SEC-07). The limiter is a module-global bound to the
    # route decorators; the factory only needs to expose it on app.state and register
    # the handler that renders the FR-AUT-04 copy. No SlowAPIMiddleware: limits are
    # applied per-route, not app-wide.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # System probes (T-106, NFR-REL-02): liveness + dependency readiness. Mounted at
    # the root — orchestrators expect unversioned probe paths (R-29), so these sit
    # outside the /api/v1 surface.
    app.include_router(health.router)

    # Source A §6 REST surface is versioned under /api/v1 (R-25).
    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
