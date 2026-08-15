"""slowapi rate limiting — the authoritative auth/chat/upload throttle (T-105).

Bounds abuse and runaway LLM cost per NFR-SEC-07. This supersedes the best-effort
Keycloak brute-force 429 mapping in ``keycloak_client.py`` as the app-level throttle
and drives FR-AUT-04's *"Too many attempts — try again later."* login state.

Design notes:

* The ``limiter`` is a **module-global** singleton because ``@limiter.limit(...)``
  decorators are bound at route-import time (a per-app instance cannot be).
  ``create_app`` attaches it to ``app.state`` and registers
  :func:`rate_limit_exceeded_handler`; no ``SlowAPIMiddleware`` is needed because we
  use explicit per-route decorators, not app-wide ``default_limits``.
* Storage is a `limits` string from :class:`app.config.RateLimitSettings`. slowapi
  0.1.10 calls the storage ``.hit()`` **synchronously** even from async routes, so
  the production URI must be the sync ``redis://`` scheme; tests use ``memory://``.
* ``swallow_errors=True`` fails **open** — a Redis blip must not 500 every request;
  the throttle degrades to allow rather than deny (availability > strict limiting).
* Keys: :func:`client_ip_key` (unauthenticated — login/refresh) and
  :func:`principal_or_ip_key` (authenticated — change-password now; chat/upload once
  T-402/T-202 land). The latter reads the principal stashed on ``request.state`` by
  ``app.auth.dependencies.get_principal``.
"""

from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.config import get_settings

# FR-AUT-04 copy — the single source of the throttle message (route modules import
# this instead of re-declaring their own ``_RATE_LIMITED`` string).
RATE_LIMITED_COPY = "Too many attempts — try again later."


def client_ip_key(request: Request) -> str:
    """Key by client IP — for unauthenticated routes (login, refresh)."""
    return get_remote_address(request)


def principal_or_ip_key(request: Request) -> str:
    """Key by the authenticated principal when present, else by client IP.

    ``get_principal`` stashes the :class:`~app.auth.principal.Principal` on
    ``request.state`` so this runs without re-validating the token.
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"user:{principal.sub}"
    return get_remote_address(request)


# Per-target limit resolvers — callables so ``RATELIMIT_*`` env overrides apply at
# request time (slowapi re-evaluates callable limits per request). Values are
# provisional pending §8.4.
def login_limit() -> str:
    return get_settings().ratelimit.login


def refresh_limit() -> str:
    return get_settings().ratelimit.refresh


def change_password_limit() -> str:
    return get_settings().ratelimit.change_password


def chat_limit() -> str:  # staged for T-402
    return get_settings().ratelimit.chat


#: The shared scope for every upload-family route (R-77(3), T-602).
#:
#: **Not cosmetic — without it the limit is not the limit §8.4 documents.** slowapi's
#: `key_style` defaults to ``"url"``, so a route decorated with a plain `limiter.limit(...)`
#: buckets on the *concrete request path*, id and all. The three id-addressed routes here
#: (`DELETE /documents/{id}`, `/retry`, `/replace`) therefore had one budget **per document**
#: rather than per user: measured at T-602, the same id tripped a 2/minute limit on the third
#: call while a fresh id every time never tripped at all. A caller with N documents got N × the
#: allowance for `replace`, which re-uploads bytes and re-runs embedding — precisely the
#: "runaway cost" NFR-SEC-07 exists to bound.
#:
#: An explicit scope overrides the url-derived key, which is why the two chat routes were never
#: affected: they have shared one since T-402. Any new route on this budget must use
#: `shared_limit(..., scope=UPLOAD_BUCKET)` for the same reason.
UPLOAD_BUCKET = "upload"


def upload_limit() -> str:  # staged for T-202
    return get_settings().ratelimit.upload


_settings = get_settings().ratelimit

limiter = Limiter(
    key_func=client_ip_key,
    storage_uri=_settings.storage_uri,
    enabled=_settings.enabled,
    swallow_errors=True,
    headers_enabled=True,
    key_prefix="corpus-rl",
)


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Return the FR-AUT-04 copy (not slowapi's default ``{"error": ...}`` body).

    Mirrors slowapi's own handler header injection so ``Retry-After`` /
    ``X-RateLimit-*`` are still emitted.
    """
    response = JSONResponse({"detail": RATE_LIMITED_COPY}, status_code=429)
    return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)
