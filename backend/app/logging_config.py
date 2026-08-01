"""The one structlog configuration, shared by every entrypoint (T-309).

Split out of `app.main` because the API was not the only process that logs. The arq
worker had relied on structlog's *library defaults* since T-207, and a library default
is not a choice — it is whatever the installed dependency set happens to produce.

That became concrete when T-309 added `deepeval`, which brings `rich` transitively:
structlog's default `ConsoleRenderer` switches its exception formatter to rich's
box-drawing traceback the moment `rich` is importable, and writing those characters to a
stream whose encoding is not UTF-8 (a pipe on Windows, `LANG=C` under a supervisor)
raises `UnicodeEncodeError` **inside the logging call** — so the failure surfaces as the
node or task that was trying to report an error, not as a logging problem. Configuring
explicitly makes the output a property of this codebase rather than of the lockfile.
"""

from __future__ import annotations

import logging

import structlog

from app.config import get_settings

__all__ = ["configure_logging"]


def configure_logging() -> None:
    """Route stdlib logging through structlog with a JSON-friendly processor chain.

    Logger caching comes from `LOG_CACHE_LOGGERS` rather than being hard-coded, and that is
    load-bearing rather than fussy. `cache_logger_on_first_use=True` latches every
    module-level `structlog.get_logger(__name__)` onto whichever processor chain was
    configured at its first use, and **nothing un-latches it** — not `structlog.configure`,
    not `structlog.testing.capture_logs`, which is how log assertions are written.
    `app.main` builds the app at import time (`app = create_app()`, the uvicorn
    entrypoint), so the latch closes before any test fixture can run; only a decision made
    in the environment *before* the import can keep it open. `tests/conftest.py` does that.
    The processor chain is identical either way, so the suite still exercises production's.

    Every entrypoint must call this before its first log call: `app.main.create_app` for
    the API, `workers.main.on_startup` for the worker. `JSONRenderer` is what keeps the
    output ASCII-safe and machine-readable on both.
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
