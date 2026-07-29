"""Event-loop selection for the API process (T-301, R-42(12)).

**Why this module exists.** FR-PER-01 pins the LangGraph checkpointer to
`AsyncPostgresSaver`, which is psycopg-backed, and psycopg's async driver waits on
sockets with ``loop.add_reader``. Windows' default `ProactorEventLoop` does not implement
it, so on this dev box *every* checkpointer connection fails with::

    InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

uvicorn selects `ProactorEventLoop` on win32 unless it is running with a subprocess
(`uvicorn/loops/asyncio.py`), which means ``--reload`` happens to work and a bare
``uvicorn app.main:app`` does not — a trap worth removing rather than documenting alone.
:func:`selector_loop` is a uvicorn ``--loop`` target (uvicorn imports a custom value and
calls it as the loop factory)::

    uv run uvicorn app.main:app --loop app.runtime:selector_loop

Both drivers were verified working side by side on a selector loop: asyncpg (SQLAlchemy)
and psycopg (checkpointer) in the same loop, same process.

**Linux and macOS are unaffected** — their default selector-based loops already implement
``add_reader``, as does uvloop, so :func:`selector_loop` returns the platform default
there and deployments need no flag.
"""

from __future__ import annotations

import asyncio
import selectors
import sys

__all__ = ["event_loop_is_psycopg_compatible", "selector_loop"]


def selector_loop() -> asyncio.AbstractEventLoop:
    """Return a new event loop that psycopg's async driver can use.

    A uvicorn ``--loop`` target: zero-argument, returns a *fresh* loop each call.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def event_loop_is_psycopg_compatible(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Whether ``loop`` (default: the running one) supports psycopg's async driver.

    Deliberately the *same* test psycopg makes in `connection_async.py` —
    ``isinstance(loop, asyncio.ProactorEventLoop)`` — so this can never disagree with the
    library it is guarding. `asyncio.ProactorEventLoop` exists only on Windows, hence the
    platform check; uvloop and every selector-based loop pass.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True  # No loop running — nothing to be incompatible with yet.
    if sys.platform != "win32":
        return True
    return not isinstance(loop, asyncio.ProactorEventLoop)
