"""Invariants of the test harness itself (T-302).

Two `conftest` guarantees earned their own tests the hard way: each was violated during
T-302, and each produced a failure that appeared **only in a full-suite run**, in a file
that had nothing to do with the change. That is the worst shape a test failure can take —
it looks like flakiness, it bisects to the wrong commit, and it tempts a re-run rather than
a diagnosis. Asserting the guarantees directly turns either regression into an immediate,
locally-named failure.
"""

from __future__ import annotations

import structlog

from app.config import get_settings
from app.services.jobs import NullJobQueue, get_job_queue


def test_loggers_are_not_cached_so_log_assertions_work(app) -> None:  # noqa: ANN001
    """`capture_logs` must be able to see a module-level logger from **any** module.

    `cache_logger_on_first_use=True` latches a `structlog.get_logger(__name__)` proxy onto
    whichever processor chain was configured when it was first used, and nothing un-latches
    it afterwards — so a log assertion silently observes nothing. It cannot be fixed by a
    fixture: `app.main` builds the app at import time (`app = create_app()`), so the latch
    closes during the first `from app...` import. `conftest` therefore sets
    `LOG_CACHE_LOGGERS=false` in the environment before any app import, and the `app`
    fixture is requested here deliberately to prove a later `create_app()` cannot undo it.

    Asserted against a module that is *not* `app.rag.telemetry`: the point is that the
    guarantee is global, not that one module worked around it locally.
    """
    assert structlog.get_config()["cache_logger_on_first_use"] is False

    from app.services import documents as documents_module

    with structlog.testing.capture_logs() as logs:
        documents_module.log.info("harness.probe", marker="visible")

    assert [entry["marker"] for entry in logs if entry["event"] == "harness.probe"] == ["visible"]


def test_the_suite_never_builds_a_real_job_queue() -> None:
    """No test may construct an arq Redis pool through dependency injection.

    FastAPI resolves `JobQueueDep` **before** the handler body, so any route carrying it
    builds and module-globally caches a pool bound to the current test's event loop — even
    for a request that is refused before it enqueues. The failure then surfaces in
    `test_job_queue`'s teardown ("Event loop is closed") hundreds of tests later. Selecting
    the null backend in `conftest` makes it impossible rather than relying on every future
    test module remembering to override the dependency.

    The arq path is still covered: `test_job_queue` constructs `ArqJobQueue` directly, and
    `test_live_enqueue_lands_on_the_broker` round-trips a real broker.
    """
    assert get_settings().queue.backend == "none"
    assert isinstance(get_job_queue(), NullJobQueue)
