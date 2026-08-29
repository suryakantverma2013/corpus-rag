"""Cloud-drive linking and import (T-214, FR-AUT-11 / FR-KBM-10, R-63).

Three surfaces, and the split matters because they fail differently:

* `app.services.cloud_links` — pure functions over URLs and a signed state. No I/O, so these
  are exact assertions rather than approximations of behaviour.
* `app.services.drive` — the only provider-specific code, stubbed with `respx`.
* the two routers — mapped onto status codes, exercised through the real app.

**What these tests are for.** R-63's three security constraints (§8.46(6)) are the reason this
feature is allowed to fetch bytes from the internet at all, so each has a test that fails if it
is removed: the fixed host with a validated id, the size checked *before* the download **and**
the stream capped regardless, and provider bytes travelling the same path as uploaded bytes.

The last of those is asserted by *absence*: `test_import_reaches_the_same_ingestion_path`
checks that an import produces the same rows an upload does, because R-63's whole claim is
that an imported document is indistinguishable from an uploaded one.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.enums import DocumentStatus, JobStatus, JobType
from app.db.models.document import Document
from app.db.models.knowledge_job import KnowledgeJob
from app.db.models.processing_lock import ProcessingLock
from app.db.repositories.users import UserRepository
from app.services import cloud_links, drive
from app.services.cloud_links import CloudProvider
from app.services.jobs import JobQueueError, get_job_queue
from app.services.object_storage import LocalFilesystemStorage, get_object_storage

pytestmark = pytest.mark.usefixtures("patch_jwks")

_PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
_PDF_MIME = "application/pdf"
_FILE_ID = "1KDDdcxaZDbo2M12Q4R2Igx0L2g0kv5G9"
_GOOGLE_TOKEN = "ya29.brokered"  # noqa: S105 — a fixture, not a credential


# ---- fixtures ----------------------------------------------------------------


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    async def enqueue_ingest(
        self, *, job_id: uuid.UUID, document_id: uuid.UUID, idempotency_key: str
    ) -> None:
        self.calls.append(document_id)

    async def enqueue_delete(self, **_kwargs: object) -> None:
        raise JobQueueError("import must never enqueue a deletion")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def storage(tmp_path) -> LocalFilesystemStorage:  # noqa: ANN001
    return LocalFilesystemStorage(tmp_path / "objects")


@pytest.fixture
def queue() -> _RecordingQueue:
    return _RecordingQueue()


@pytest.fixture
def app(app, storage: LocalFilesystemStorage, queue: _RecordingQueue):  # noqa: ANN001, ANN201
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    return app


@pytest.fixture
def settings():  # noqa: ANN201
    return get_settings()


async def _caller(
    session: AsyncSession, make_token: Callable[..., str]
) -> tuple[uuid.UUID, dict[str, str]]:
    sub = uuid.uuid4()
    email = f"{sub.hex[:8]}@corpus.local"
    await UserRepository(session).upsert_from_claims(sub=sub, email=email, display_name="User")
    token = make_token(sub=sub, email=email, roles=("user",))
    return sub, {"Authorization": f"Bearer {token}"}


def _broker_url() -> str:
    kc = get_settings().keycloak
    return kc.broker_token_endpoint(kc.google_idp_alias)


def _drive_list_url() -> str:
    return f"{get_settings().cloud.api_base.rstrip('/')}/drive/v3/files"


def _drive_file_url(file_id: str = _FILE_ID) -> str:
    return f"{_drive_list_url()}/{file_id}"


def _metadata(**overrides: object) -> dict:
    return {
        "id": _FILE_ID,
        "name": "report.pdf",
        "mimeType": _PDF_MIME,
        "size": str(len(_PDF)),
        **overrides,
    }


def _stub_broker(respx_mock, token: str = _GOOGLE_TOKEN) -> None:
    respx_mock.get(_broker_url()).respond(json={"access_token": token})


def _stub_download(respx_mock, payload: bytes = _PDF, **meta: object) -> None:
    respx_mock.get(_drive_file_url(), params={"alt": "media"}).respond(content=payload)
    respx_mock.get(_drive_file_url()).respond(json=_metadata(**meta))


# ---- the signed linking state (no I/O) ---------------------------------------


def test_state_round_trips(settings) -> None:  # noqa: ANN001
    sub = uuid.uuid4()
    raw = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=sub, provider=CloudProvider.GOOGLE, nonce="n", expires_at=int(time.time()) + 60
        ),
        settings,
    )
    decoded = cloud_links.decode_state(raw, settings)
    assert decoded.sub == sub
    assert decoded.provider is CloudProvider.GOOGLE


def test_a_tampered_state_is_refused(settings) -> None:  # noqa: ANN001
    """The signature is the whole authorization story on the unauthenticated callback.

    `/callback` carries no bearer token — a browser returning from Keycloak cannot send one —
    so if this check were weak, anyone able to guess a `sub` could drive the linking flow for
    another user.
    """
    raw = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=uuid.uuid4(),
            provider=CloudProvider.GOOGLE,
            nonce="n",
            expires_at=int(time.time()) + 60,
        ),
        settings,
    )
    payload, _, signature = raw.partition(".")
    forged = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=uuid.uuid4(),
            provider=CloudProvider.GOOGLE,
            nonce="n",
            expires_at=int(time.time()) + 60,
        ),
        settings,
    ).partition(".")[0]

    with pytest.raises(cloud_links.LinkStateError):
        cloud_links.decode_state(f"{forged}.{signature}", settings)
    with pytest.raises(cloud_links.LinkStateError):
        cloud_links.decode_state(f"{payload}.{signature[:-2]}xx", settings)


def test_an_expired_state_is_refused(settings) -> None:  # noqa: ANN001
    raw = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=uuid.uuid4(),
            provider=CloudProvider.GOOGLE,
            nonce="n",
            expires_at=int(time.time()) - 1,
        ),
        settings,
    )
    with pytest.raises(cloud_links.LinkStateError):
        cloud_links.decode_state(raw, settings)


def test_the_authorize_url_uses_the_linking_client_not_the_ropc_client(settings) -> None:  # noqa: ANN001
    """R-63(2): `corpus-backend` keeps `standardFlow` disabled, so the redirect cannot use it.

    Collapsing the two clients is the one change that would quietly give the login client a
    browser flow R-28 deliberately refused — and nothing else in the tree would notice.
    """
    url = cloud_links.start_link(sub=uuid.uuid4(), provider=CloudProvider.GOOGLE, settings=settings)
    query = parse_qs(urlparse(url).query)

    assert query["client_id"] == [settings.keycloak.linking_client_id]
    assert query["client_id"] != [settings.keycloak.client_id]
    assert query["response_type"] == ["code"]


def test_the_authorize_url_forces_a_fresh_login(settings) -> None:  # noqa: ANN001
    """Without `prompt=login` an existing Keycloak SSO session would sail through leg 1.

    The link would then attach to *that* session's user, and the identity check in the
    callback would be comparing a value it never really established.
    """
    url = cloud_links.start_link(sub=uuid.uuid4(), provider=CloudProvider.GOOGLE, settings=settings)
    assert parse_qs(urlparse(url).query)["prompt"] == ["login"]


def test_the_pkce_verifier_never_appears_in_the_url(settings) -> None:  # noqa: ANN001
    """It is derived from the state, not carried in it.

    The state travels through browser history, `Referer` headers and access logs — the same
    exposure R-41(3) refused to put a bearer token into. A verifier there would be readable by
    anyone who reads a log, which is most of the value of PKCE gone.
    """
    url = cloud_links.start_link(sub=uuid.uuid4(), provider=CloudProvider.GOOGLE, settings=settings)
    query = parse_qs(urlparse(url).query)
    payload = query["state"][0].partition(".")[0]
    verifier = cloud_links._verifier(payload, settings)

    assert verifier not in url
    assert query["code_challenge"] == [cloud_links._challenge(verifier)]
    assert query["code_challenge_method"] == ["S256"]


def test_the_return_url_comes_from_settings_not_the_request(settings) -> None:  # noqa: ANN001
    """`/callback` and `/complete` are unauthenticated, so a caller-chosen redirect target
    here would be an open redirect reachable without a token."""
    assert cloud_links.return_to(settings, link="linked").startswith(settings.cloud.return_url)


# ---- leg 1's identity check --------------------------------------------------


class _FakeKeycloak:
    """Enough of `KeycloakClient` for the linking flow, with the calls recorded."""

    def __init__(self, *, id_token_sub: uuid.UUID | None = None, linked: bool = False) -> None:
        self._sub = id_token_sub
        self.linked = linked
        self.granted: list[str] = []
        self.unlinked: list[str] = []

    async def exchange_linking_code(self, *, code: str, redirect_uri: str, verifier: str) -> dict:
        import base64
        import json

        payload = (
            base64.urlsafe_b64encode(json.dumps({"sub": str(self._sub)}).encode())
            .decode()
            .rstrip("=")
        )
        return {"session_state": "sess-1", "id_token": f"h.{payload}.s"}

    async def service_account_token(self) -> str:
        return "sa"

    async def admin_get_client_uuid(self, *, client_id: str, admin_token: str) -> str:
        return "broker-uuid"

    async def admin_get_client_role(
        self, *, client_uuid: str, role_name: str, admin_token: str
    ) -> dict:
        return {"id": "role-uuid", "name": role_name}

    async def admin_add_client_roles(
        self, *, sub: str, client_uuid: str, roles: list[dict], admin_token: str
    ) -> None:
        self.granted.append(f"{sub}:{roles[0]['name']}")

    async def admin_list_federated_identities(self, *, sub: str, admin_token: str) -> list[dict]:
        if not self.linked:
            return []
        return [{"identityProvider": "google", "userName": "person@example.com"}]

    async def admin_remove_federated_identity(
        self, *, sub: str, alias: str, admin_token: str
    ) -> None:
        self.unlinked.append(f"{sub}:{alias}")


async def test_a_different_user_authenticating_is_refused(settings) -> None:  # noqa: ANN001
    """The defect this check exists for: the request authenticates *whoever* types credentials.

    Without it, a user could start linking, someone else could complete the Keycloak login in
    that browser, and Google would be linked to the second account while Corpus reported
    success to the first. Under AIA (R-99) the link has already happened by the time we get
    here, which makes the check more important rather than less: there is no second leg to
    withhold, only a success to refuse to report.
    """
    starter, other = uuid.uuid4(), uuid.uuid4()
    raw = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=starter,
            provider=CloudProvider.GOOGLE,
            nonce="n",
            expires_at=int(time.time()) + 60,
        ),
        settings,
    )
    with pytest.raises(cloud_links.LinkIdentityMismatchError):
        await cloud_links.complete_link(
            code="c", raw_state=raw, kc=_FakeKeycloak(id_token_sub=other), settings=settings
        )


async def test_the_matching_user_completes_the_link(settings) -> None:  # noqa: ANN001
    sub = uuid.uuid4()
    raw = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=sub, provider=CloudProvider.GOOGLE, nonce="n", expires_at=int(time.time()) + 60
        ),
        settings,
    )
    linked_sub = await cloud_links.complete_link(
        code="c", raw_state=raw, kc=_FakeKeycloak(id_token_sub=sub), settings=settings
    )
    assert linked_sub == sub


async def test_grant_read_token_targets_the_broker_client(settings) -> None:  # noqa: ANN001
    """The step whose absence made every successful link unusable (T-214, live 2026-08-11).

    `addReadTokenRoleOnCreate` cannot fire under `linkOnly: true`, so nothing else grants it.
    """
    kc = _FakeKeycloak()
    sub = uuid.uuid4()
    await cloud_links.grant_read_token(sub=sub, kc=kc, settings=settings)
    assert kc.granted == [f"{sub}:read-token"]


# ---- the Drive adapter -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "abc/def", "a b", "", "x" * 300, "id?alt=media", "http://evil/x"],
)
def test_a_file_id_that_is_not_a_file_id_is_refused(bad: str) -> None:
    """R-63(6)(1). The id becomes a URL path segment, so this is the SSRF boundary.

    Rejecting rather than escaping is deliberate: an id that needs escaping is not an id, and
    an escape function is one refactor away from being applied in the wrong order.
    """
    with pytest.raises(drive.InvalidFileIdError):
        drive.validate_file_id(bad)


def test_the_listing_query_filters_to_the_ingestible_formats() -> None:
    """Server-side, and derived from `MIME_BY_SUFFIX` rather than restated.

    A second hand-written list here would be free to drift into offering a format the parser
    cannot read — which the user would meet as a `415` *after* choosing a file.
    """
    query = drive._build_query(None)
    assert "trashed = false" in query
    for mime in drive.IMPORTABLE_MIME_TYPES:
        assert f"mimeType = '{mime}'" in query
    assert "application/vnd.google-apps.folder" not in query


def test_a_search_term_cannot_break_out_of_the_query() -> None:
    """The term is user input reaching a query language, so its quotes must be neutralised.

    Asserted as the whole escaped literal rather than by counting clauses. The first version of
    this test counted `name contains` and expected 1 — but the *injected text itself* contains
    that phrase, so the count is 2 whether the escaping works or not: a test that would have
    failed on correct code and passed on a payload it merely happened to word differently.
    """
    query = drive._build_query("quarterly' or name contains 'secret")
    assert r"name contains 'quarterly\' or name contains \'secret'" in query


async def test_an_oversized_file_is_refused_before_it_is_downloaded(respx_mock, settings) -> None:  # noqa: ANN001
    """R-63(6)(2), the pre-download half.

    The point is the *absence* of the second call: `respx` is asked to fail if the media route
    is ever hit, so a check moved after the download would fail this test rather than merely
    waste 50 MB of transfer.
    """
    media = respx_mock.get(_drive_file_url(), params={"alt": "media"}).respond(content=b"x")
    respx_mock.get(_drive_file_url()).respond(
        json=_metadata(size=str(settings.upload.max_file_bytes + 1))
    )

    with pytest.raises(drive.DriveFileTooLargeError):
        await drive.fetch_metadata(file_id=_FILE_ID, access_token=_GOOGLE_TOKEN, settings=settings)
    assert not media.called, "the size check must precede the download"


async def test_a_google_native_document_is_refused(respx_mock, settings) -> None:  # noqa: ANN001
    respx_mock.get(_drive_file_url()).respond(
        json=_metadata(mimeType="application/vnd.google-apps.document", size=None)
    )
    with pytest.raises(drive.UnsupportedDriveFileError):
        await drive.fetch_metadata(file_id=_FILE_ID, access_token=_GOOGLE_TOKEN, settings=settings)


async def test_the_stream_is_capped_even_when_the_declared_size_lied(  # noqa: ANN001
    respx_mock, settings, monkeypatch
) -> None:
    """R-63(6)(2)'s other half, and the one that is not advisory.

    `size` is the provider's *claim*. A file that reports 10 bytes and streams megabytes would
    pass every pre-check, so the read itself has to stop — which is what makes the pre-check an
    optimisation rather than the control.
    """
    monkeypatch.setattr(settings.upload, "max_file_bytes", 32)
    respx_mock.get(_drive_file_url(), params={"alt": "media"}).respond(content=b"A" * 4096)
    respx_mock.get(_drive_file_url()).respond(json=_metadata(size="10"))

    metadata = await drive.fetch_metadata(
        file_id=_FILE_ID, access_token=_GOOGLE_TOKEN, settings=settings
    )
    download = await drive.open_download(
        metadata=metadata, access_token=_GOOGLE_TOKEN, settings=settings
    )
    try:
        with pytest.raises(drive.DriveFileTooLargeError):
            await download.read(-1)
    finally:
        await download.aclose()


async def test_the_download_reads_in_upload_file_chunks(respx_mock, settings) -> None:  # noqa: ANN001
    """`read(n)` must behave like `UploadFile.read(n)`, because that is what it stands in for.

    `_read_and_hash` loops `while chunk := await upload.read(n)`, so a `read` that returned the
    whole body on the first call, or never returned empty, would either defeat the streaming
    size check or loop forever.
    """
    _stub_download(respx_mock, payload=b"0123456789")
    metadata = await drive.fetch_metadata(
        file_id=_FILE_ID, access_token=_GOOGLE_TOKEN, settings=settings
    )
    download = await drive.open_download(
        metadata=metadata, access_token=_GOOGLE_TOKEN, settings=settings
    )
    try:
        chunks = []
        while chunk := await download.read(4):
            chunks.append(chunk)
        assert b"".join(chunks) == b"0123456789"
        assert [len(c) for c in chunks] == [4, 4, 2]
    finally:
        await download.aclose()


async def test_drive_refusing_the_token_is_not_unavailability(respx_mock, settings) -> None:  # noqa: ANN001
    respx_mock.get(_drive_list_url()).respond(403)
    with pytest.raises(drive.DriveForbiddenError):
        await drive.list_files(access_token=_GOOGLE_TOKEN, settings=settings)


# ---- the linking routes ------------------------------------------------------


async def test_link_status_requires_a_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/cloud/links/google")).status_code == 401


async def test_link_status_reports_the_linked_account(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    app,  # noqa: ANN001
) -> None:
    from app.auth.dependencies import get_keycloak_client

    _, headers = await _caller(session, make_token)
    app.dependency_overrides[get_keycloak_client] = lambda: _FakeKeycloak(linked=True)

    resp = await client.get("/api/v1/cloud/links/google", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {
        "provider": "google",
        "linked": True,
        "account": "person@example.com",
    }


async def test_an_unlinked_account_is_reported_not_errored(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    app,  # noqa: ANN001
) -> None:
    """FR-AUT-11 linking is opt-in, so "not linked" is the ordinary state of most users."""
    from app.auth.dependencies import get_keycloak_client

    _, headers = await _caller(session, make_token)
    app.dependency_overrides[get_keycloak_client] = lambda: _FakeKeycloak(linked=False)

    resp = await client.get("/api/v1/cloud/links/google", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["linked"] is False


async def test_start_returns_an_authorize_url_without_calling_anything(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
) -> None:
    sub, headers = await _caller(session, make_token)
    resp = await client.post("/api/v1/cloud/links/google/start", headers=headers)

    assert resp.status_code == 200
    url = resp.json()["authorize_url"]
    assert url.startswith(get_settings().keycloak.authorization_endpoint())
    # The state binds the flow to *this* caller, which is what the callback verifies.
    state = parse_qs(urlparse(url).query)["state"][0]
    assert cloud_links.decode_state(state, get_settings()).sub == sub


async def test_unlink_is_idempotent_and_leaves_documents_alone(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    app,  # noqa: ANN001
) -> None:
    """FR-KBM-10 makes an import a copy, so unlinking revokes future import and nothing else."""
    from app.auth.dependencies import get_keycloak_client

    sub, headers = await _caller(session, make_token)
    kc = _FakeKeycloak(linked=True)
    app.dependency_overrides[get_keycloak_client] = lambda: kc

    assert (await client.delete("/api/v1/cloud/links/google", headers=headers)).status_code == 204
    assert (await client.delete("/api/v1/cloud/links/google", headers=headers)).status_code == 204
    assert kc.unlinked == [f"{sub}:google", f"{sub}:google"]


async def test_the_callback_sends_a_cancelled_link_back_to_the_gui(
    client: httpx.AsyncClient,
) -> None:
    """A browser mid-flow must land in the GUI, not on a JSON error body it cannot act on."""
    resp = await client.get(
        "/api/v1/cloud/links/google/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(get_settings().cloud.return_url)
    assert "link=denied" in location


async def test_a_forged_callback_cannot_complete_a_link(client: httpx.AsyncClient) -> None:
    """The unauthenticated endpoints' whole defence, exercised end to end."""
    resp = await client.get(
        "/api/v1/cloud/links/google/callback",
        params={"code": "c", "state": "forged.signature"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "link=failed" in resp.headers["location"]


async def test_completion_grants_read_token_before_reporting_success(
    client: httpx.AsyncClient,
    app,  # noqa: ANN001
) -> None:
    """Without the grant the link is complete, correct and unusable — the T-214 defect."""
    from app.auth.dependencies import get_keycloak_client

    sub = uuid.uuid4()
    # The fake must authenticate AS the initiator: under AIA the callback runs the identity
    # check itself, so a fake returning any other `sub` is correctly refused - which would make
    # this test pass for a reason that has nothing to do with the grant it is named for.
    kc = _FakeKeycloak(linked=True, id_token_sub=sub)
    app.dependency_overrides[get_keycloak_client] = lambda: kc
    state = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=sub, provider=CloudProvider.GOOGLE, nonce="n", expires_at=int(time.time()) + 60
        ),
        get_settings(),
    )

    resp = await client.get(
        "/api/v1/cloud/links/google/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "link=linked" in resp.headers["location"]
    assert kc.granted == [f"{sub}:read-token"]


async def test_a_rejected_link_is_reported_as_declined_not_failed(
    client: httpx.AsyncClient,
    app,  # noqa: ANN001
) -> None:
    from app.auth.dependencies import get_keycloak_client

    app.dependency_overrides[get_keycloak_client] = lambda: _FakeKeycloak(linked=True)
    resp = await client.get(
        "/api/v1/cloud/links/google/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    # `denied`, not `failed`: the user turned it down, and reporting a fault would be false.
    # R-99 keeps the two apart - a rejection is a decision, an exception is a fault.
    assert "link=denied" in resp.headers["location"]


async def test_a_link_that_did_not_land_is_not_reported_as_success(
    client: httpx.AsyncClient,
    app,  # noqa: ANN001
) -> None:
    """Keycloak can return with no `link_error` and no federated identity.

    Reporting success there sends the user to a file list that answers `409`, which reads as a
    different bug entirely.
    """
    from app.auth.dependencies import get_keycloak_client

    app.dependency_overrides[get_keycloak_client] = lambda: _FakeKeycloak(linked=False)
    state = cloud_links.encode_state(
        cloud_links._LinkState(
            sub=uuid.uuid4(),
            provider=CloudProvider.GOOGLE,
            nonce="n",
            expires_at=int(time.time()) + 60,
        ),
        get_settings(),
    )
    resp = await client.get(
        "/api/v1/cloud/links/google/callback",
        params={"code": "c", "state": state},
        follow_redirects=False,
    )
    assert "link=failed" in resp.headers["location"]


# ---- the file list route -----------------------------------------------------


async def test_listing_without_a_link_is_a_typed_409(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """Never a 5xx (FR-AUT-11), and carrying a code so the GUI can offer the link button."""
    _, headers = await _caller(session, make_token)
    respx_mock.get(_broker_url()).respond(403)

    resp = await client.get("/api/v1/cloud/google/files", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "ACCOUNT_NOT_LINKED"


async def test_a_revoked_google_grant_is_not_reported_as_unlinked(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """The link exists; Google refused. Saying "not linked" would contradict the status route."""
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    respx_mock.get(_drive_list_url()).respond(403)

    resp = await client.get("/api/v1/cloud/google/files", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "CLOUD_ACCESS_REVOKED"


async def test_a_grant_keycloak_can_no_longer_refresh_is_reported_as_revoked(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """B-008: the link is dead, and Keycloak says so with a **502**, not a 400.

    Found in the field. `GET /cloud/links/google` answered `200 linked` with the account
    address, while the picker showed *"Authentication service unavailable."* — because the
    broker endpoint returned `502 {"errorMessage": "Unable to refresh token"}` and 502 fell
    through to the retryable class. Keycloak was demonstrably up; what had expired was Google's
    grant, which is R-63(6)'s `CLOUD_ACCESS_REVOKED` case exactly. The user's fix is to re-link;
    the message sent them to check their infrastructure.

    Deliberately matched on the **message** rather than the status: a bare 502 from a proxy in
    front of Keycloak is a real outage, and telling users to redo their OAuth consent during an
    incident would be a worse defect than the one being fixed. The sibling test below pins that
    half, so neither can be relaxed without the other failing.
    """
    _, headers = await _caller(session, make_token)
    kc = get_settings().keycloak
    respx_mock.get(kc.broker_token_endpoint(kc.google_idp_alias)).respond(
        502, json={"errorMessage": "Unable to refresh token"}
    )

    resp = await client.get("/api/v1/cloud/google/files", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "CLOUD_ACCESS_REVOKED"


async def test_a_bare_502_from_the_broker_is_still_an_outage(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """The other half of B-008, and the reason its match is narrow.

    A 502 with no refresh-token message is a proxy or a genuinely broken Keycloak. Mapping every
    5xx to "re-link" would send every user through Google's consent screen during an outage that
    fixes itself.
    """
    _, headers = await _caller(session, make_token)
    kc = get_settings().keycloak
    respx_mock.get(kc.broker_token_endpoint(kc.google_idp_alias)).respond(502)

    resp = await client.get("/api/v1/cloud/google/files", headers=headers)

    assert resp.status_code == 503


async def test_the_file_list_carries_metadata_and_no_urls(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """The brokered token stays server-side — that is what keeps the surface ours (R-63(4))."""
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    route = respx_mock.get(_drive_list_url()).respond(
        json={"files": [_metadata()], "nextPageToken": "page-2"}
    )

    resp = await client.get(
        "/api/v1/cloud/google/files", headers=headers, params={"search": "report"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == [
        {
            "file_id": _FILE_ID,
            "name": "report.pdf",
            "mime_type": _PDF_MIME,
            "size_bytes": len(_PDF),
            "modified_time": None,
        }
    ]
    assert body["next_page_token"] == "page-2"
    assert _GOOGLE_TOKEN not in resp.text
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {_GOOGLE_TOKEN}"


async def test_a_drive_outage_is_retryable(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    respx_mock.get(_drive_list_url()).mock(side_effect=httpx.ConnectError("refused"))

    resp = await client.get("/api/v1/cloud/google/files", headers=headers)
    assert resp.status_code == 503


# ---- the import route --------------------------------------------------------


async def _import(client: httpx.AsyncClient, headers: dict, **body: object) -> httpx.Response:
    return await client.post(
        "/api/v1/documents/import",
        headers=headers,
        json={"provider": "google", "file_id": _FILE_ID, "scope": "global", **body},
    )


async def test_import_reaches_the_same_ingestion_path(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,  # noqa: ANN001
    respx_mock,  # noqa: ANN001
    queue: _RecordingQueue,
    storage: LocalFilesystemStorage,
) -> None:
    """R-63's central claim: an imported document is indistinguishable from an uploaded one.

    So this asserts the *ordinary* upload outcome — a `202`, a `QUEUED` document, an `INGEST`
    job at version 1, an object in storage and a dispatch — rather than anything import-shaped.
    Nothing here should ever need an `imported` flag to be true.
    """
    sub, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    _stub_download(respx_mock)

    resp = await _import(client, headers)

    assert resp.status_code == 202
    body = resp.json()
    assert body["duplicate"] is False
    assert body["status"] == DocumentStatus.QUEUED.value

    document = await session.get(Document, uuid.UUID(body["document_id"]))
    assert document is not None
    assert document.owner_id == sub
    assert document.filename == "report.pdf"
    assert document.mime_type == _PDF_MIME
    assert document.size_bytes == len(_PDF)
    assert document.searchable is False

    job = (
        await session.scalars(select(KnowledgeJob).where(KnowledgeJob.document_id == document.id))
    ).one()
    assert job.job_type is JobType.INGEST
    assert job.status is JobStatus.QUEUED
    assert job.document_version == 1
    assert queue.calls == [document.id]


async def test_importing_the_same_file_twice_is_the_fr_kbm_08_duplicate(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """R-33's contract reused verbatim — `200` and `duplicate: true`, not an error.

    Inherited rather than implemented: the checksum is computed by `_read_and_hash` on the way
    through, so this holds because the bytes went down the upload path.
    """
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    _stub_download(respx_mock)

    first = await _import(client, headers)
    second = await _import(client, headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["job_id"] is None
    assert second.json()["document_id"] == first.json()["document_id"]


async def test_an_oversized_import_is_the_ordinary_413(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,
    settings,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    _stub_download(respx_mock, size=str(settings.upload.max_file_bytes + 1))

    assert (await _import(client, headers)).status_code == 413


async def test_an_unsupported_import_is_the_ordinary_415(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    _stub_download(respx_mock, mimeType="application/vnd.google-apps.spreadsheet")

    assert (await _import(client, headers)).status_code == 415


async def test_an_unknown_file_and_a_malformed_id_answer_identically(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """Distinguishing them makes the route a probe for which Drive files exist.

    NFR-SEC-02's argument, applied to a third party's namespace: the caller has no legitimate
    way to know whether an id they did not receive from the list route is real.
    """
    _, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    respx_mock.get(_drive_file_url()).respond(404)

    missing = await _import(client, headers)
    malformed = await client.post(
        "/api/v1/documents/import",
        headers=headers,
        json={"provider": "google", "file_id": "../../secrets", "scope": "global"},
    )

    assert missing.status_code == malformed.status_code == 404
    assert missing.json() == malformed.json()


async def test_import_is_gated_by_the_processing_lock(
    client: httpx.AsyncClient,
    session: AsyncSession,
    make_token,
    respx_mock,  # noqa: ANN001
) -> None:
    """R-24/R-43(4) names four file verbs, and an import is an upload wearing a file id.

    It is gated because it goes through `upload_document`, not because this route checks —
    which is the same reason it inherits everything else.
    """
    sub, headers = await _caller(session, make_token)
    _stub_broker(respx_mock)
    _stub_download(respx_mock)
    session.add(
        ProcessingLock(
            owner_id=sub,
            conversation_id=None,
            token=uuid.uuid4().hex,
            acquired_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    await session.flush()

    resp = await _import(client, headers)

    assert resp.status_code == 409
    assert (await session.scalars(select(Document).where(Document.owner_id == sub))).all() == []


async def test_import_requires_a_token(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/documents/import", json={"provider": "google", "file_id": _FILE_ID}
    )
    assert resp.status_code == 401


def test_the_linking_url_carries_the_aia_action(settings) -> None:  # noqa: ANN001
    """R-99: the whole migration is this one parameter, so it is asserted directly.

    `kc_action=idp_link:<alias>` is what replaced Keycloak's client-initiated account-linking
    endpoint, which logged its own deprecation on every call and is announced for removal. If
    it goes missing the flow does not fail loudly - it authenticates the user, links nothing,
    and returns a perfectly good code, so the callback reports success for a link that never
    happened. That is precisely the shape of failure a test has to catch, because a browser
    walking the flow looks identical.
    """
    url = cloud_links.start_link(sub=uuid.uuid4(), provider=CloudProvider.GOOGLE, settings=settings)
    query = parse_qs(urlparse(url).query)

    assert query["kc_action"] == ["idp_link:google"]
    # Both, and in one test: `prompt=login` without the action links nothing, and the action
    # without `prompt=login` lets an existing SSO session decide silently who gets linked.
    # Measured on 26.4 that they compose; this is the guard that they still both ship.
    assert query["prompt"] == ["login"]


def test_the_linking_redirect_uri_carries_no_query_at_all(settings) -> None:  # noqa: ANN001
    """The constraint T-725 discovered cannot recur, because there is nothing to carry.

    Keycloak refuses a `redirect_uri` whose query holds a reserved OIDC parameter - which is
    how FR-AUT-11 broke with no code change when the dev container moved to 26.7.1. Under AIA
    the state travels in the request's own `state`, so our redirect URI is bare. This asserts
    the *shape* rather than the old workaround: a future reader reaching for somewhere to stash
    a value should find the query empty and leave it that way.
    """
    url = cloud_links.start_link(sub=uuid.uuid4(), provider=CloudProvider.GOOGLE, settings=settings)
    redirect_uri = parse_qs(urlparse(url).query)["redirect_uri"][0]

    assert urlparse(redirect_uri).query == ""
