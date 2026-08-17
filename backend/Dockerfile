# syntax=docker/dockerfile:1.7
#
# Corpus backend image (T-605). ONE image serves three roles; compose picks with `command:`
#
#   api     uvicorn app.main:app ...            (the default CMD below)
#   worker  arq workers.main.WorkerSettings
#   init    alembic upgrade head && python -m app.services.checkpointer
#
# Build context is the REPOSITORY ROOT (see /.dockerignore), so every path below is
# repo-relative:  docker build -f backend/Dockerfile .

# ---------------------------------------------------------------- builder
# This image IS python:3.14-slim-trixie plus the uv binary. The pairing with the runtime
# stage is deliberate and load-bearing: the virtualenv copied out of here hard-codes an
# interpreter path (/usr/local/bin/python3.14) that must exist identically in the runtime
# stage. Do not "optimise" the runtime to Alpine — musl would invalidate every manylinux
# wheel in the lock.
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, as their own layer, so the ~130-package install is cached until
# pyproject.toml or uv.lock actually moves.
#
# `--frozen` is correct here because uv.lock is committed on purpose: it makes a stale
# lock a build FAILURE rather than a silent re-resolution that ships something the
# repository never tested. Verified for this lock: 131 packages, zero sdist-only, cp314 or
# abi3 manylinux wheels for every C/Rust extension — so no compiler is needed in this stage.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=backend/pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=backend/uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself. The layout is FLAT — `app` and `workers` are both top-level
# import packages (pyproject: packages = ["app", "workers"]) — so /app is the backend root,
# not a package directory inside it.
COPY backend/ /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime
FROM python:3.14-slim-trixie AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    # deepeval writes a hidden cache directory into the CURRENT WORKING DIRECTORY
    # (HIDDEN_DIR = os.getenv("DEEPEVAL_CACHE_FOLDER", ".deepeval")). With a non-root user
    # and a root-owned /app that is a PermissionError inside the evaluate task — which runs
    # in the worker, after the answer was already served, so it would surface as messages
    # that silently never get their FR-EVL-02 chips.
    DEEPEVAL_CACHE_FOLDER=/tmp/deepeval

RUN useradd --create-home --uid 10001 app

WORKDIR /app

# uv itself is deliberately NOT in the runtime image. Everything the three roles need —
# uvicorn, arq, alembic, python — is on PATH from the venv's bin directory.
COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

# --proxy-headers is NOT optional behind nginx. Two things read the client address:
# slowapi's login limiter keys on get_remote_address (app/security/rate_limit.py), and
# uvicorn's default forwarded_allow_ips is 127.0.0.1 while the peer here is nginx's
# container IP — so without it every user on the deployment shares one 10/minute login
# bucket, and the audit trail records the proxy.
#
# There is deliberately NO `--loop app.runtime:selector_loop`: that flag exists for the
# Windows dev box, where psycopg's async driver needs add_reader and ProactorEventLoop
# lacks it. On Linux uvloop provides it, and selector_loop() returns a plain new event
# loop off win32 — so passing it here would merely discard uvloop for nothing.
#
# And no `--workers N`: scale with container replicas, which keeps one lifespan and one
# log stream per process.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
