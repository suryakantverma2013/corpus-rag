"""FastAPI application factory (T-003 scaffold).

Boots the app with structured logging and mounts the routers (system probes at the
root, the versioned API under `/api/v1`). CORS and the LangGraph wiring land in their
own tasks. Run: `uv run uvicorn app.main:app`.
"""

from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from app.api import health
from app.api.router import api_router
from app.config import Settings, get_settings
from app.security.rate_limit import limiter, rate_limit_exceeded_handler


def _configure_logging() -> None:
    """Route stdlib logging through structlog with a JSON-friendly processor chain."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    _configure_logging()
    settings = settings or get_settings()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        summary="Corpus RAG Chatbot backend (Nexus AI platform).",
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
