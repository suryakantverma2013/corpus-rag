"""``/cloud`` routes — account linking (FR-AUT-11) and the Drive file list (FR-KBM-10).

T-214, under R-63 (§8.46). Thin handlers on this API's established shape: orchestration lives
in `app.services.cloud_links` and `app.services.drive`, and this layer maps their typed errors
to status codes.

**Two of these routes are deliberately unauthenticated**, and it is the only place in this API
that is true. `/callback` and `/complete` are reached by a *browser navigation* returning from
Keycloak, which carries no `Authorization` header and cannot be made to — the same constraint
R-41(3) hit from the other side, where the answer was to keep the header and change the client.
Here there is no client to change: the redirect is Keycloak's.

What replaces the bearer token is the signed `state` (`app.services.cloud_links`), which binds
the flow to the Corpus user who started it, expires, and is checked with `compare_digest`. The
identity is then *proved* rather than asserted, by exchanging the authorization code and
comparing the authenticated `sub` — so a forged callback cannot link an account, and a genuine
callback for a different user is refused.

The **import** route is not here. It lives on `/documents` beside the upload it mirrors,
because R-63 makes an import a one-time copy through the existing ingestion path and the two
therefore share a response contract, a scope vocabulary, a rate limit and the R-24 lock.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.errors import (
    ACCESS_REVOKED_COPY,
    MISCONFIGURED,
    NOT_LINKED_COPY,
    RATE_LIMITED,
    UPSTREAM_DOWN,
    CloudLinkRequiredResponse,
    cloud_link_required,
    error_responses,
)
from app.auth.dependencies import CurrentAccessToken, CurrentUser, Keycloak, SettingsDep
from app.auth.keycloak_client import (
    AccountNotLinkedError,
    BrokerGrantExpiredError,
    KeycloakForbiddenError,
    KeycloakRejectedError,
    KeycloakUnavailableError,
)
from app.config import Settings
from app.security.rate_limit import (
    UPLOAD_BUCKET,
    limiter,
    principal_or_ip_key,
    upload_limit,
)
from app.services import cloud_links, drive
from app.services.cloud_links import CloudProvider

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/cloud", tags=["cloud"])

_UPSTREAM = "Authentication service unavailable."
_DRIVE_DOWN = "The cloud drive is unavailable — please try again."  # TBD(§8.4)
# Shared with `app/api/documents.py`'s import route — one refusal, one wording.
_NOT_LINKED = NOT_LINKED_COPY
_DRIVE_FORBIDDEN = ACCESS_REVOKED_COPY
_MISCONFIGURED = (
    "Cloud linking is not configured correctly on the server. "
    "Check the server logs for details."
)  # TBD(§8.4)

#: The outcomes the browser is sent back to the GUI with. A stable, closed vocabulary rather
#: than copy: this crosses a redirect into a page the backend does not render, and T-512 has to
#: branch on it. The copy for each lives in the GUI.
_LINK_OK = "linked"
_LINK_FAILED = "failed"
_LINK_DENIED = "denied"


class LinkStatusResponse(BaseModel):
    """Whether the caller has linked this provider (FR-AUT-11).

    `account` is the provider-side address, so the GUI can say *which* Google account it will
    import from. It is metadata, never a credential and never a provider user id.
    """

    provider: CloudProvider
    linked: bool
    account: str | None = None


class LinkStartResponse(BaseModel):
    """Where to send the browser to begin linking.

    A URL the client *navigates to*, not one it fetches: the flow is a redirect chain through
    Keycloak's login page and Google's consent screen, neither of which can be satisfied by
    an XHR. Returned as JSON rather than as a `302` so the caller — an authenticated fetch
    from the KB modal — can open it deliberately.
    """

    authorize_url: str


class DriveFileResponse(BaseModel):
    """One row of the FR-KBM-10 selection surface.

    Metadata only, on `DocumentResponse`'s principle: no URL, no download link, nothing that
    points at bytes. The client sends `file_id` back to the import route and the backend
    resolves it against the provider's fixed host (R-63(6)(1)).
    """

    file_id: str
    name: str
    mime_type: str
    size_bytes: int | None
    modified_time: str | None


class DriveListResponse(BaseModel):
    """A page of the flat list. `next_page_token` is the provider's, opaque, and echoed back."""

    files: list[DriveFileResponse]
    next_page_token: str | None = None


@router.get(
    "/links/{provider}",
    response_model=LinkStatusResponse,
    responses={**MISCONFIGURED, **UPSTREAM_DOWN},
    summary="Whether the caller has linked a cloud provider",
)
async def get_link_status(
    provider: CloudProvider,
    user: CurrentUser,
    kc: Keycloak,
    settings: SettingsDep,
) -> LinkStatusResponse:
    """FR-AUT-11's "report linked state" — what T-508 renders the FR-KBM-06 button against."""
    try:
        result = await cloud_links.link_status(
            sub=user.id, provider=provider, kc=kc, settings=settings
        )
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
    return LinkStatusResponse(
        provider=result.provider, linked=result.linked, account=result.account
    )


@router.post(
    "/links/{provider}/start",
    response_model=LinkStartResponse,
    responses=RATE_LIMITED,
    summary="Begin linking a cloud account",
)
@limiter.shared_limit(upload_limit, scope=UPLOAD_BUCKET, key_func=principal_or_ip_key)
async def start_link(
    request: Request,
    response: Response,
    provider: CloudProvider,
    user: CurrentUser,
    settings: SettingsDep,
) -> LinkStartResponse:
    """Mint leg 1's authorize URL (FR-AUT-11).

    No provider call happens here — the URL is built and signed locally — so this cannot fail
    on Keycloak or Google being down, and the user learns about an outage at the point they
    can see it rather than behind a button that silently does nothing.
    """
    return LinkStartResponse(
        authorize_url=cloud_links.start_link(sub=user.id, provider=provider, settings=settings)
    )


@router.get(
    "/links/{provider}/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
    include_in_schema=False,
    summary="Keycloak's return from leg 1 (browser navigation, not an API call)",
)
async def link_callback(
    provider: CloudProvider,
    kc: Keycloak,
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Finish authentication, then redirect the browser onward to the provider's consent.

    `include_in_schema=False`: this is a browser redirect target, not something a client
    calls. Publishing it would put a route in the generated TypeScript client that no
    TypeScript can usefully invoke.

    Every failure lands the user back in the GUI with a stable outcome code rather than on a
    JSON error page — they are in a browser, mid-flow, and an `{"detail": ...}` body is a dead
    end. The reason is logged; only the outcome crosses the redirect.
    """
    if error or not code or not state:
        # `error` is Keycloak's — the user cancelled at the login page, most often.
        return RedirectResponse(
            cloud_links.return_to(settings, link=_LINK_DENIED, provider=provider.value),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        onward = await cloud_links.complete_authentication(
            code=code, raw_state=state, kc=kc, settings=settings
        )
    except cloud_links.LinkError, KeycloakUnavailableError:
        log.warning("cloud.link_callback_failed", provider=provider.value, exc_info=True)
        return RedirectResponse(
            cloud_links.return_to(settings, link=_LINK_FAILED, provider=provider.value),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(onward, status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/links/{provider}/complete",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
    include_in_schema=False,
    summary="Keycloak's return from leg 2 (browser navigation, not an API call)",
)
async def link_complete(
    provider: CloudProvider,
    kc: Keycloak,
    settings: SettingsDep,
    link_state: Annotated[str | None, Query()] = None,
    link_error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Grant `read-token`, verify the link is real, and return the browser to the GUI.

    **The grant is why this endpoint exists at all** rather than the flow ending at Keycloak:
    `addReadTokenRoleOnCreate` cannot fire under `linkOnly: true`, so without this step every
    link would be complete, correct, and unusable — which is precisely the defect T-214
    measured live (see `cloud_links.grant_read_token`).

    The link is then read back rather than assumed. Keycloak signals leg 2's failures by
    appending `link_error` to this redirect, but a `link_state` that survives with no error and
    no federated identity is possible too, and reporting success for it would send the user to a
    file list that answers 403.

    **The parameter is `link_state` and must never be renamed back to `state`.** Keycloak
    rejects a `redirect_uri` carrying a reserved OIDC parameter in its query, so `?state=` makes
    leg 2 fail with a bare "Invalid Request" — see the writer in `cloud_links`.
    """
    if link_error or not link_state:
        log.warning("cloud.link_rejected", provider=provider.value, link_error=link_error)
        return RedirectResponse(
            cloud_links.return_to(settings, link=_LINK_FAILED, provider=provider.value),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        decoded = cloud_links.decode_state(link_state, settings)
        await cloud_links.grant_read_token(sub=decoded.sub, kc=kc, settings=settings)
        result = await cloud_links.link_status(
            sub=decoded.sub, provider=provider, kc=kc, settings=settings
        )
    except (
        cloud_links.LinkError,
        KeycloakUnavailableError,
        KeycloakForbiddenError,
        KeycloakRejectedError,
    ):
        log.error("cloud.link_complete_failed", provider=provider.value, exc_info=True)
        return RedirectResponse(
            cloud_links.return_to(settings, link=_LINK_FAILED, provider=provider.value),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    outcome = _LINK_OK if result.linked else _LINK_FAILED
    log.info("cloud.link_completed", provider=provider.value, outcome=outcome)
    return RedirectResponse(
        cloud_links.return_to(settings, link=outcome, provider=provider.value),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.delete(
    "/links/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**MISCONFIGURED, **UPSTREAM_DOWN},
    summary="Unlink a cloud account",
)
async def delete_link(
    provider: CloudProvider,
    user: CurrentUser,
    kc: Keycloak,
    settings: SettingsDep,
) -> None:
    """FR-AUT-11's unlink. Idempotent, and documents already imported are untouched — they are
    copies (FR-KBM-10), so unlinking revokes future import and nothing else."""
    try:
        await cloud_links.unlink(sub=user.id, provider=provider, kc=kc, settings=settings)
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc


@router.get(
    "/{provider}/files",
    response_model=DriveListResponse,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": CloudLinkRequiredResponse,
            "description": "No cloud account is linked, or the provider revoked access.",
        },
        **error_responses((503, "The cloud drive is unreachable.")),
        **MISCONFIGURED,
        **UPSTREAM_DOWN,
        **RATE_LIMITED,
    },
    summary="List the caller's importable cloud-drive files",
)
@limiter.shared_limit(upload_limit, scope=UPLOAD_BUCKET, key_func=principal_or_ip_key)
async def list_cloud_files(
    request: Request,
    response: Response,
    provider: CloudProvider,
    user: CurrentUser,
    access_token: CurrentAccessToken,
    kc: Keycloak,
    settings: SettingsDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    page_token: Annotated[str | None, Query(max_length=4096)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> DriveListResponse:
    """FR-KBM-10's selection surface: a searchable **flat** list of the caller's importable files.

    Flat, and filtered to the four FR-ING-02 formats *at the provider*, both by requirement —
    with only four ingestible formats a folder tree would mostly show files the user cannot
    pick, and a client-side filter would page through their whole Drive to render a handful of
    rows.

    **The brokered token never leaves this process.** That is the property that makes the
    selection surface ours rather than a provider widget (R-63(4)): the response carries file
    metadata and an opaque id, and the bytes are fetched server-side at import.

    Not rate-limited by `upload_limit` — this is a read — but it is the one read in this API
    that spends a *third party's* quota, so it carries the same per-caller limit rather than
    the "no read route is limited" convention, which was reasoned about a purely local read.
    """
    token = await _brokered_token(
        provider=provider, access_token=access_token, kc=kc, settings=settings
    )
    try:
        listing = await drive.list_files(
            access_token=token,
            settings=settings,
            search=search,
            page_token=page_token,
            page_size=page_size,
        )
    except drive.DriveForbiddenError as exc:
        raise _link_required(provider, "CLOUD_ACCESS_REVOKED", _DRIVE_FORBIDDEN) from exc
    except drive.DriveError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _DRIVE_DOWN) from exc

    return DriveListResponse(
        files=[
            DriveFileResponse(
                file_id=item.file_id,
                name=item.name,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                modified_time=item.modified_time,
            )
            for item in listing.files
        ],
        next_page_token=listing.next_page_token,
    )


def _link_required(provider: CloudProvider, code: str, message: str) -> HTTPException:
    """FR-AUT-11's `409`. The detail model and copy are shared with `app/api/documents.py`."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        cloud_link_required(provider.value, code, message),  # type: ignore[arg-type]
    )


async def _brokered_token(
    *,
    provider: CloudProvider,
    access_token: str,
    kc: Keycloak,
    settings: Settings,
) -> str:
    """The provider's current access token for this caller (FR-AUT-11, R-63(1)).

    Keycloak holds and refreshes it; we ask for it per request and never store it. Shared by
    the list route and the import route so the failure taxonomy is written once — the two
    disagree about which is worse, and a user who can list files but cannot import them would
    have no way to tell which call was lying.
    """
    try:
        data = await kc.broker_token(
            alias=settings.keycloak.google_idp_alias, user_token=access_token
        )
    except BrokerGrantExpiredError as exc:
        # B-008. The link is present and Google's grant is dead, so this is the *revoked* case,
        # not the unlinked one - the status route would answer `200 linked` beside it.
        raise _link_required(provider, "CLOUD_ACCESS_REVOKED", _DRIVE_FORBIDDEN) from exc
    except AccountNotLinkedError as exc:
        raise _link_required(provider, "ACCOUNT_NOT_LINKED", _NOT_LINKED) from exc
    except (KeycloakForbiddenError, KeycloakRejectedError) as exc:
        # `storeToken` off, or a realm the linking flow cannot have produced. Not the
        # caller's to fix and not retryable — the T-110 distinction, unchanged.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, _MISCONFIGURED) from exc
    except KeycloakUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UPSTREAM) from exc
    return str(data["access_token"])
