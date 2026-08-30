# Corpus API

**Generated from `backend/openapi.json` -- do not edit.** Regenerate with `cd backend && uv run python -m tools.httpdocs`; `tests/test_http_docs.py` fails if this file stops matching the specification, and that specification is itself checked against the running application.

API version `0.1.0` -- **40 operations** across 31 paths and 85 schemas.

Authentication is a bearer token on every operation marked **yes**; see [SECURITY.md](SECURITY.md) for how one is obtained and what invalidates it. Error bodies carry a stable `error_code` -- match on that, never on the prose.

## Index

| | Method | Path | Auth | Summary |
|---|---|---|---|---|
| `admin` | **GET** | [`/api/v1/admin/documents/stale`](#list-stale-documents) | yes | List documents whose embeddings predate the configured pipeline |
| `admin` | **POST** | [`/api/v1/admin/documents/{document_id}/reembed`](#reembed-document) | yes | Re-embed one document under the configured pipeline |
| `audit` | **GET** | [`/api/v1/audit`](#list-audit-events) | yes | Read the audit trail |
| `auth` | **POST** | [`/api/v1/auth/login`](#login) | no | Exchange credentials for a token pair |
| `auth` | **POST** | [`/api/v1/auth/refresh`](#refresh) | no | Exchange the refresh cookie for a new access token |
| `auth` | **POST** | [`/api/v1/auth/logout`](#logout) | no | Revoke the session and clear the refresh cookie |
| `auth` | **GET** | [`/api/v1/auth/me`](#me) | yes | The caller's own profile |
| `auth` | **POST** | [`/api/v1/auth/change-password`](#change-password) | yes | Change the caller's own password |
| `cloud` | **GET** | [`/api/v1/cloud/links/{provider}`](#get-link-status) | yes | Whether the caller has linked a cloud provider |
| `cloud` | **DELETE** | [`/api/v1/cloud/links/{provider}`](#delete-link) | yes | Unlink a cloud account |
| `cloud` | **POST** | [`/api/v1/cloud/links/{provider}/start`](#start-link) | yes | Begin linking a cloud account |
| `cloud` | **GET** | [`/api/v1/cloud/{provider}/files`](#list-cloud-files) | yes | List the caller's importable cloud-drive files |
| `config` | **GET** | [`/api/v1/config`](#get-config) | yes | Deployment configuration |
| `conversations` | **GET** | [`/api/v1/conversations`](#list-conversations) | yes | List the caller's chats |
| `conversations` | **POST** | [`/api/v1/conversations`](#create-conversation) | yes | Start a new chat |
| `conversations` | **GET** | [`/api/v1/conversations/{conversation_id}`](#get-conversation) | yes | Get one chat |
| `conversations` | **PATCH** | [`/api/v1/conversations/{conversation_id}`](#rename-conversation) | yes | Rename a chat |
| `conversations` | **DELETE** | [`/api/v1/conversations/{conversation_id}`](#delete-conversation) | yes | Delete a chat |
| `documents` | **GET** | [`/api/v1/documents`](#list-documents) | yes | List documents |
| `documents` | **POST** | [`/api/v1/documents`](#upload-document) | yes | Upload a document |
| `documents` | **POST** | [`/api/v1/documents/import`](#import-document) | yes | Import a document from a linked cloud drive |
| `documents` | **GET** | [`/api/v1/documents/{document_id}`](#get-document) | yes | Get one document |
| `documents` | **DELETE** | [`/api/v1/documents/{document_id}`](#delete-document) | yes | Delete a document |
| `documents` | **POST** | [`/api/v1/documents/{document_id}/retry`](#retry-document) | yes | Retry a failed ingestion |
| `documents` | **GET** | [`/api/v1/documents/events`](#stream-documents) | yes | Live document-status stream (SSE) |
| `documents` | **GET** | [`/api/v1/documents/{document_id}/figures/{content_sha256}`](#get-document-figure) | yes | Get one of a document's figures |
| `documents` | **POST** | [`/api/v1/documents/{document_id}/replace`](#replace-document) | yes | Replace a document with a new version |
| `jobs` | **GET** | [`/api/v1/jobs/{job_id}`](#get-job) | yes | Get ingestion/deletion job status |
| `messages` | **GET** | [`/api/v1/conversations/{conversation_id}/messages`](#list-messages) | yes | List a chat's messages |
| `messages` | **POST** | [`/api/v1/conversations/{conversation_id}/messages`](#send-message) | yes | Ask a question (SSE) |
| `messages` | **POST** | [`/api/v1/messages/{message_id}/regenerate`](#regenerate-message) | yes | Regenerate an answer (SSE) |
| `messages` | **POST** | [`/api/v1/messages/{message_id}/feedback`](#set-feedback) | yes | Rate an answer |
| `messages` | **POST** | [`/api/v1/messages/{message_id}/general-knowledge`](#answer-from-general-knowledge) | yes | Answer an abstention from the model's own training (FR-MSG-09) |
| `system` | **GET** | [`/health`](#liveness) | no | Liveness probe |
| `system` | **GET** | [`/health/ready`](#readiness) | no | API readiness probe |
| `system` | **GET** | [`/health/ready/worker`](#worker-readiness) | no | Worker readiness probe |
| `users` | **GET** | [`/api/v1/users`](#list-users) | yes | List users |
| `users` | **POST** | [`/api/v1/users`](#create-user) | yes | Create a user |
| `users` | **PATCH** | [`/api/v1/users/{user_id}`](#update-user) | yes | Update a user |
| `users` | **DELETE** | [`/api/v1/users/{user_id}`](#delete-user) | yes | Delete a user |

## `admin`

### GET /api/v1/admin/documents/stale

<a id="list-stale-documents"></a>

List documents whose embeddings predate the configured pipeline

Read-only, and safe to run against production at any moment. Declares no `409` and no `503`: it takes no lock, touches no object store and has no state to conflict with. R-77(2) — a route may not advertise a status it cannot return.

- **Operation id:** `list_stale_documents`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `owner_id` | query | no | string (uuid) \| null | Restrict to one owner's documents. |
| `limit` | query | no | integer |  |
| `offset` | query | no | integer |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`StaleDocumentsResponse`](#staledocumentsresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |

### POST /api/v1/admin/documents/{document_id}/reembed

<a id="reembed-document"></a>

Re-embed one document under the configured pipeline

- **Operation id:** `reembed_document`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `202` | [`RebuildResponse`](#rebuildresponse) | Accepted; the rebuild is queued. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such document. |
| `409` | [`RebuildConflictResponse`](#rebuildconflictresponse) | Not ACTIVE, already current, or the stored original is corrupt. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | Object storage is unreachable. |

## `audit`

### GET /api/v1/audit

<a id="list-audit-events"></a>

Read the audit trail

- **Operation id:** `list_audit_events`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `actor_id` | query | no | string (uuid) \| null |  |
| `event_type` | query | no | [`AuditEventType`](#auditeventtype) \| null |  |
| `limit` | query | no | integer |  |
| `offset` | query | no | integer |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`AuditLogResponse`](#auditlogresponse)[] | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

## `auth`

### POST /api/v1/auth/login

<a id="login"></a>

Exchange credentials for a token pair

- **Operation id:** `login`
- **Request body:** [`LoginRequest`](#loginrequest) as `application/json` (required)

| Status | Body | Meaning |
|---|---|---|
| `200` | [`TokenResponse`](#tokenresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Invalid email or password (FR-AUT-04). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### POST /api/v1/auth/refresh

<a id="refresh"></a>

Exchange the refresh cookie for a new access token

No request body: the refresh token comes from the httpOnly cookie and nowhere else (R-72(1)). The rotated token replaces the cookie, so an idle client's cookie lifetime tracks the realm's ``ssoSessionIdleTimeout`` rather than the login time.

- **Operation id:** `refresh`

| Status | Body | Meaning |
|---|---|---|
| `200` | [`TokenResponse`](#tokenresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | The refresh cookie is absent, invalid or expired. |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### POST /api/v1/auth/logout

<a id="logout"></a>

Revoke the session and clear the refresh cookie

Idempotent, and the cookie is cleared on **every** path including the ones that fail. FR-AUT-08's sign out must leave the browser signed out even when the upstream revoke cannot be delivered; the alternative is a user who clicked "Sign out", saw an error, and is still holding a live session cookie.

- **Operation id:** `logout`

| Status | Body | Meaning |
|---|---|---|
| `204` | -- | Successful Response |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### GET /api/v1/auth/me

<a id="me"></a>

The caller's own profile

- **Operation id:** `me`
- **Authentication:** bearer token

| Status | Body | Meaning |
|---|---|---|
| `200` | [`MeResponse`](#meresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |

### POST /api/v1/auth/change-password

<a id="change-password"></a>

Change the caller's own password

- **Operation id:** `change_password`
- **Authentication:** bearer token
- **Request body:** [`ChangePasswordRequest`](#changepasswordrequest) as `application/json` (required)

| Status | Body | Meaning |
|---|---|---|
| `204` | -- | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | The current password is incorrect. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

## `cloud`

### GET /api/v1/cloud/links/{provider}

<a id="get-link-status"></a>

Whether the caller has linked a cloud provider

FR-AUT-11's "report linked state" — what T-508 renders the FR-KBM-06 button against.

- **Operation id:** `get_link_status`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `provider` | path | yes | [`CloudProvider`](#cloudprovider) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`LinkStatusResponse`](#linkstatusresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### DELETE /api/v1/cloud/links/{provider}

<a id="delete-link"></a>

Unlink a cloud account

FR-AUT-11's unlink. Idempotent, and documents already imported are untouched — they are copies (FR-KBM-10), so unlinking revokes future import and nothing else.

- **Operation id:** `delete_link`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `provider` | path | yes | [`CloudProvider`](#cloudprovider) |  |

| Status | Body | Meaning |
|---|---|---|
| `204` | -- | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### POST /api/v1/cloud/links/{provider}/start

<a id="start-link"></a>

Begin linking a cloud account

Mint leg 1's authorize URL (FR-AUT-11). No provider call happens here — the URL is built and signed locally — so this cannot fail on Keycloak or Google being down, and the user learns about an outage at the point they can see it rather than behind a button that silently does nothing.

- **Operation id:** `start_link`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `provider` | path | yes | [`CloudProvider`](#cloudprovider) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`LinkStartResponse`](#linkstartresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |

### GET /api/v1/cloud/{provider}/files

<a id="list-cloud-files"></a>

List the caller's importable cloud-drive files

FR-KBM-10's selection surface: a searchable **flat** list of the caller's importable files. Flat, and filtered to the four FR-ING-02 formats *at the provider*, both by requirement — with only four ingestible formats a folder tree would mostly show files the user cannot pick, and a client-side filter would page through their whole Drive to render a handful of rows. **The brokered token never leaves this process.** That is the property that makes the selection surface ours rather than a provider widget (R-63(4)): the response carries file metadata and an opaque id, and the bytes are fetched server-side at import. Not rate-limited by `upload_limit` — this is a read — but it is the one read in this API that spends a *third party's* quota, so it carries the same per-caller limit rather than the "no read route is limited" convention, which was reasoned about a purely local read.

- **Operation id:** `list_cloud_files`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `provider` | path | yes | [`CloudProvider`](#cloudprovider) |  |
| `search` | query | no | string \| null |  |
| `page_token` | query | no | string \| null |  |
| `page_size` | query | no | integer \| null |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`DriveListResponse`](#drivelistresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `409` | [`CloudLinkRequiredResponse`](#cloudlinkrequiredresponse) | No cloud account is linked, or the provider revoked access. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

## `config`

### GET /api/v1/config

<a id="get-config"></a>

Deployment configuration

FR-SYS-03's model id — the one **in force**, not the one in the environment. `user` is unused and is the point: it is the dependency that makes this authenticated. The read goes through `resolve_models` (T-611, R-83) rather than straight to `settings.openai.chat_model`, because since T-611 those two can differ: an operator may have repointed generation without a deploy. Reporting the environment here would make the FR-ANL-02 card name a model that is not answering — the precise silent drift this route was added to remove, reintroduced one layer down.

- **Operation id:** `get_config`
- **Authentication:** bearer token

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ConfigResponse`](#configresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |

## `conversations`

### GET /api/v1/conversations

<a id="list-conversations"></a>

List the caller's chats

FR-SBR-03 sidebar order — most recently updated first. A bare array, matching `GET /documents` (R-40(5)); no paging, because the sidebar renders the whole list and a user's chat count is bounded by how many they have made. Each row carries `message_count` (T-407) from a correlated `COUNT(*)`, so FR-SBR-03's "· N messages" is true on rows the client has never opened. See the field's docstring for why this does not reopen the R-51(4) argument that keeps `context` off this route.

- **Operation id:** `list_conversations`
- **Authentication:** bearer token

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ConversationResponse`](#conversationresponse)[] | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |

### POST /api/v1/conversations

<a id="create-conversation"></a>

Start a new chat

- **Operation id:** `create_conversation`
- **Authentication:** bearer token
- **Request body:** [`CreateConversationRequest`](#createconversationrequest) as `application/json` (required)

| Status | Body | Meaning |
|---|---|---|
| `201` | [`ConversationDetailResponse`](#conversationdetailresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### GET /api/v1/conversations/{conversation_id}

<a id="get-conversation"></a>

Get one chat

- **Operation id:** `get_conversation`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `conversation_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ConversationDetailResponse`](#conversationdetailresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such chat for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### PATCH /api/v1/conversations/{conversation_id}

<a id="rename-conversation"></a>

Rename a chat

- **Operation id:** `rename_conversation`
- **Authentication:** bearer token
- **Request body:** [`RenameConversationRequest`](#renameconversationrequest) as `application/json` (required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `conversation_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ConversationDetailResponse`](#conversationdetailresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such chat for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### DELETE /api/v1/conversations/{conversation_id}

<a id="delete-conversation"></a>

Delete a chat

FR-SBR-07 — deletes the transcript **and** the LangGraph thread (R-42(11)). The `503` is not a formality: `delete_conversation` purges the thread *before* it commits, so a checkpointer outage leaves the chat intact and retryable rather than orphaning its graph state where nothing will ever collect it (OI-30).

- **Operation id:** `delete_conversation`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `conversation_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `204` | -- | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such chat for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `503` | -- | The checkpointer could not be reached; nothing was deleted. |

## `documents`

### GET /api/v1/documents

<a id="list-documents"></a>

List documents

The FR-KBM-03/09 page for the calling user (R-40(5)). `scope`/`conversation_id` are spelled exactly as the upload form spells them, so the modal uses one vocabulary in both directions; omitting `scope` returns both sections, which is the FR-KBM-09 table view. A legitimate scope with nothing in it yet is an empty list, not a `404`. **Caller-scoped, with no admin widening** — deliberately asymmetric with `GET /{id}`, which is owner-or-admin so an admin can read anything they can act on. There is no GUI surface for browsing another user's knowledge base (FR-KBM-01..09 is the caller's own modal), so a cross-user list would be speculative surface needing its own authorization story. Not rate-limited: no read route in this API is, and a limiter would force a `request`/`response` pair onto a pure read for nothing.

- **Operation id:** `list_documents`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `scope` | query | no | `global` \| `chat` \| null |  |
| `conversation_id` | query | no | string (uuid) \| null |  |
| `status` | query | no | [`DocumentStatus`](#documentstatus) \| null |  |
| `limit` | query | no | integer |  |
| `offset` | query | no | integer |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`DocumentResponse`](#documentresponse)[] | Successful Response |
| `400` | [`ErrorResponse`](#errorresponse) | scope=chat without a conversation_id. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such conversation for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### POST /api/v1/documents

<a id="upload-document"></a>

Upload a document

- **Operation id:** `upload_document`
- **Authentication:** bearer token
- **Request body:** [`Body_upload_document`](#body-upload-document) as `multipart/form-data` (required)

| Status | Body | Meaning |
|---|---|---|
| `200` | [`UploadResponse`](#uploadresponse) | Duplicate checksum — not re-ingested. |
| `202` | [`UploadResponse`](#uploadresponse) | Accepted; ingestion queued. |
| `400` | [`ErrorResponse`](#errorresponse) | Empty file, or scope=chat without a conversation_id. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | scope=chat naming no conversation of this caller's. |
| `409` | [`ProcessingLockedResponse`](#processinglockedresponse) | R-24 — a response is generating for this caller; retry when it finishes. |
| `413` | [`ErrorResponse`](#errorresponse) | The upload exceeds the per-file size ceiling. |
| `415` | [`ErrorResponse`](#errorresponse) | Not a supported document format (FR-KBM-02). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | Object storage is unreachable. |
| `507` | [`ErrorResponse`](#errorresponse) | FR-ERR-02 — the caller's storage quota is exhausted. |

### POST /api/v1/documents/import

<a id="import-document"></a>

Import a document from a linked cloud drive

FR-KBM-10's one-time copy. **It is the same route as upload in every way that matters** — the response model, the duplicate `200`, `413`/`415`/`507`, the R-24 processing lock and the rate limit are all literally the upload route's, because the bytes are handed to `upload_document` itself (R-63: an imported document is thereafter indistinguishable from an uploaded one). The only additions are the two failures that can happen *before* there are any bytes: the account is not linked, and the provider refused. It is a **copy, not a live link**: a later change to the file in Drive does not re-ingest, and no query ever reaches Google. The user re-imports or uses Replace (FR-KBM-07).

- **Operation id:** `import_document`
- **Authentication:** bearer token
- **Request body:** [`ImportRequest`](#importrequest) as `application/json` (required)

| Status | Body | Meaning |
|---|---|---|
| `200` | [`UploadResponse`](#uploadresponse) | Duplicate checksum — not re-ingested. |
| `202` | [`UploadResponse`](#uploadresponse) | Accepted; ingestion queued. |
| `400` | [`ErrorResponse`](#errorresponse) | Empty file, or scope=chat without a conversation_id. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such file for this caller, or scope=chat naming no conversation of theirs. |
| `409` | [`ImportConflictResponse`](#importconflictresponse) | No cloud account is linked, the provider revoked access, or a response is generating. |
| `413` | [`ErrorResponse`](#errorresponse) | The upload exceeds the per-file size ceiling. |
| `415` | [`ErrorResponse`](#errorresponse) | Not a supported document format (FR-KBM-02). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | Object storage is unreachable. |
| `507` | [`ErrorResponse`](#errorresponse) | FR-ERR-02 — the caller's storage quota is exhausted. |

### GET /api/v1/documents/{document_id}

<a id="get-document"></a>

Get one document

One document's metadata, owner-or-admin (R-40(5)). A soft-deleted document is returned with its terminal state rather than 404'd: a client that has just received `DELETE`'s `202` and polls must see `DELETED`, not a sudden `404` it cannot tell apart from a wrong id. The list excludes tombstones; this does not.

- **Operation id:** `get_document`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`DocumentResponse`](#documentresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such document for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### DELETE /api/v1/documents/{document_id}

<a id="delete-document"></a>

Delete a document

- **Operation id:** `delete_document`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`DeleteResponse`](#deleteresponse) | Already deleted — nothing queued. |
| `202` | [`DeleteResponse`](#deleteresponse) | Accepted; the document left retrieval. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such document for this caller. |
| `409` | [`ProcessingLockedResponse`](#processinglockedresponse) | R-24 — a response is generating for this caller; retry when it finishes. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |

### POST /api/v1/documents/{document_id}/retry

<a id="retry-document"></a>

Retry a failed ingestion

- **Operation id:** `retry_document`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `202` | [`RetryResponse`](#retryresponse) | Accepted; ingestion re-queued. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such document for this caller. |
| `409` | [`DocumentConflictResponse`](#documentconflictresponse) | The document is not in FAILED, or a response is generating. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |

### GET /api/v1/documents/events

<a id="stream-documents"></a>

Live document-status stream (SSE)

FR-KBM-09's live surface (R-41). Filters, scope resolution and authorization are deliberately identical to `list_documents` — same parameter spelling, same `400`/`404`, same caller scoping with no admin widening — so the stream and the page it updates can never disagree about what the caller may see. **Authenticated by the ordinary `Authorization` header (R-41(3)).** A browser's `EventSource` cannot send one, so the GUI (T-508) consumes this with `fetch` + `ReadableStream` and parses the frames itself. Every query-string alternative was rejected: a raw token there is written to access logs, proxy logs, `Referer` headers and browser history, and stays valid in all of them long after the tab closes. Scope resolution and the stream-slot reservation are both **dependencies** (`resolve_stream_scope`, `hold_stream_slot`), so a bad request still fails as an ordinary `400`/`404`/`429` — once the `200` and the first byte are out, there is no status code left to fail with, and a generator handler's body does not run until then (T-405). The loop itself uses `sessionmaker`, never the request-scoped session, so a long-lived stream cannot pin a pool slot.

- **Operation id:** `stream_documents`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `scope` | query | no | `global` \| `chat` \| null |  |
| `conversation_id` | query | no | string (uuid) \| null |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`DocumentStreamFrame`](#documentstreamframe) | An SSE stream. `snapshot` carries the full set on connect, `document` one changed row, `removed` a document id that has left the set. |
| `400` | [`ErrorResponse`](#errorresponse) | scope=chat without a conversation_id. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such conversation for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | Too many concurrent streams for this caller. |

### GET /api/v1/documents/{document_id}/figures/{content_sha256}

<a id="get-document-figure"></a>

Get one of a document's figures

One figure this document declared, rendered inline (FR-CIT-07, NFR-SEC-10). **Owner-only, with no administrator branch** — the one route under `/documents` that does not widen under FR-USR-04, and deliberately so: its siblings disclose *management* (a listing, a status, a job id) while this discloses **content**. `get_servable` carries the argument and the whole predicate set; nothing is re-decided here. **`inline`, with no filename.** NFR-SEC-10 forbids a download affordance, and a `filename=` would both suggest one and hand out the uploaded file's name. `nosniff` is an assertion rather than a hope: `render_figure` encodes PNG and nothing else, so the declared type is a fact about our own renderer. `content_sha256` is validated as 64 lower-case hex by the path itself, so a malformed id is a `422` that never reaches a query. It discloses nothing — an id of the wrong shape cannot name a figure that exists.

- **Operation id:** `get_document_figure`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |
| `content_sha256` | path | yes | string |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | any | The figure, inline. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such figure for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `503` | [`ErrorResponse`](#errorresponse) | Object storage is unreachable. |

### POST /api/v1/documents/{document_id}/replace

<a id="replace-document"></a>

Replace a document with a new version

FR-KBM-07's Replace (R-40(1)): new bytes at version n+1, old version keeps serving.

- **Operation id:** `replace_document`
- **Authentication:** bearer token
- **Request body:** [`Body_replace_document`](#body-replace-document) as `multipart/form-data` (required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `document_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ReplaceResponse`](#replaceresponse) | Identical bytes — nothing queued. |
| `202` | [`ReplaceResponse`](#replaceresponse) | Accepted; the new version is queued. |
| `400` | [`ErrorResponse`](#errorresponse) | Empty file, or scope=chat without a conversation_id. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such document for this caller. |
| `409` | [`DocumentConflictResponse`](#documentconflictresponse) | Not ACTIVE/FAILED, the bytes belong to another document, or a response is generating. |
| `413` | [`ErrorResponse`](#errorresponse) | The upload exceeds the per-file size ceiling. |
| `415` | [`ErrorResponse`](#errorresponse) | Not a supported document format (FR-KBM-02). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | Object storage is unreachable. |
| `507` | [`ErrorResponse`](#errorresponse) | FR-ERR-02 — the caller's storage quota is exhausted. |

## `jobs`

### GET /api/v1/jobs/{job_id}

<a id="get-job"></a>

Get ingestion/deletion job status

- **Operation id:** `get_job`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `job_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`JobResponse`](#jobresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such job for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

## `messages`

### GET /api/v1/conversations/{conversation_id}/messages

<a id="list-messages"></a>

List a chat's messages

The transcript, oldest first. Ordered by `messages.seq`, never `created_at` (T-108): a turn writes the question and the answer close together, and `created_at` is the *transaction* timestamp, so any tiebreak on the random UUID `id` is a coin flip on rendering the answer above the question. R-45(6)'s binding — read history through `app.rag.history` rather than mapping rows by hand — governs the **prompt** path, where `MessageRole.AI` is `"ai"` and `compose_messages` silently drops a role it does not know. It is discharged inside `generate`. This is the API's own view and maps the enum explicitly, which is the same trap named once more: the wire value is `"ai"`, not `"assistant"`.

- **Operation id:** `list_messages`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `conversation_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`MessageResponse`](#messageresponse)[] | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such chat for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### POST /api/v1/conversations/{conversation_id}/messages

<a id="send-message"></a>

Ask a question (SSE)

Run one FR-ORC-01 turn and stream its progress. Everything that can refuse the turn happens **before** the response starts: once a `200` and the first SSE frame are on the wire, a failure can only be reported inside the stream, where no HTTP status can carry it. So ownership (R-54), the FR-STA-04 admission check and the rate limit all live in `admit_send` — this body runs only once the turn is admitted, because FastAPI creates a generator without executing a statement of it (T-405). The annotation declares the **schema**; what is yielded is `frame.to_event()`, a `ServerSentEvent`, so the frames keep their named `event:` line — R-41 and T-508 are written against those names. FastAPI supports exactly this (it skips `stream_item_field` validation for a `ServerSentEvent` and still takes the published union from the annotation), and the cost — that the union is not enforced at runtime — is covered by `test_every_chat_frame_builder_returns_a_declared_frame`.

- **Operation id:** `send_message`
- **Authentication:** bearer token
- **Request body:** [`SendMessageRequest`](#sendmessagerequest) as `application/json` (required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `conversation_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ChatStreamFrame`](#chatstreamframe) | An SSE stream. `stage` reports coarse progress and carries no content, `message` the completed and verified answer, `done` closes the turn. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No such chat for this caller. |
| `409` | [`ContextWindowExceededResponse`](#contextwindowexceededresponse) | FR-STA-04 — the conversation's token budget is exhausted. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — shares the regenerate route's chat budget. |

### POST /api/v1/messages/{message_id}/regenerate

<a id="regenerate-message"></a>

Regenerate an answer (SSE)

FR-MSG-08's Regenerate — re-run the question and **replace** the answer (T-404, R-56). Addressed by message id like its sibling `feedback` route, and refused the same way: absent, foreign or non-AI targets are one `404` with one copy (R-55(2)), for an administrator too. The second refusal is this route's own — **only the latest AI answer may be regenerated** (`409` + `NOT_LATEST_ANSWER`), because rewriting a mid-transcript answer silently invalidates every turn generated from it and the spec has no cascade rule. **The row is replaced, never appended.** §4.16 makes the transcript what the user sees and the FR-ANL cards count, and R-51(4) derives the NFR-CAP-01 budget from it — so an appended answer would show one question answered twice and charge the conversation for both. `finalize` does the write, through `RAGState.regenerate_message_id`, which is what keeps it idempotent across a resume. **A failed re-run leaves the answer exactly as it was.** `_should_persist` excludes `error` (R-54(3)), so the UPDATE is simply never reached — and because `evaluation`/`feedback` clear inside that same UPDATE rather than here, a failure cannot destroy the scores or the rating of an answer that still exists. An **abstained or injection-blocked** re-run does replace, on FR-MSG-08's unconditional wording and R-23's "an abstention is a response": the common cause is that the user lost access to the document the old answer cited, and keeping that prose would preserve text whose sources are gone. There is no undo, which is a cost the ruling records rather than hides. **Not gated by the R-24 lock at this route** — R-55(1) honoured, not contradicted. Regenerate *takes* the gate (the graph's `lock` node acquires it exactly as for a send), which is what pauses the caller's document affordances; it does not *check* it. A route-level `409` would refuse a regenerate in one chat because a different chat of the same user is mid-turn, since R-43(1) keys the gate on the caller — the precise defect R-55(1) rejected for feedback — and R-43(2) names "Regenerate over a live turn" as the case its token-matched release exists to make compose. Two concurrent regenerates of one row are last-write-wins and benign: there is no read-modify-write, so either order commits a complete, internally consistent answer. Its refusals — the single `404` and the two `409`s — live in `admit_regeneration`, because a generator handler's body runs after the `200` (see `send_message`, T-405).

- **Operation id:** `regenerate_message`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `message_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ChatStreamFrame`](#chatstreamframe) | An SSE stream, identical in shape to a send: `stage` frames carrying no content, one `message` frame with the replacement, then `done`. The `message` frame always carries the target's id — on failure it carries the *unchanged* answer plus `error_code`. |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No AI message with this id for this caller. |
| `409` | [`ChatConflictResponse`](#chatconflictresponse) | `NOT_LATEST_ANSWER` — a later turn has landed; or `CONTEXT_WINDOW_EXCEEDED` — FR-STA-04's budget. Branch on `error_code`. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — shares the send route's per-user chat budget. |

### POST /api/v1/messages/{message_id}/feedback

<a id="set-feedback"></a>

Rate an answer

FR-MSG-08's 👍/👎, and FR-MSG-06's third state, the clear (T-403, R-55). Addressed by **message id**, not through its conversation, because FR-MSG-08 spells the path that way and the action bar has only the message. Ownership is therefore a join and it lives in the query (`MessageRepository.get_owned`) — R-54: a message in someone else's chat is `404`, never `403`, for an administrator exactly as for anyone else. **One status and one string cover all three ways this can fail.** A message that does not exist, one in a foreign chat, and one whose role is `user` are indistinguishable in the response on purpose: distinguishing them would make the route a probe for which ids exist (NFR-SEC-02). The wrong-role case is a `404` rather than the `409` this surface uses for `NotRetryableError` / `NotReplaceableError` — those answer a request a *correct* client could make against state that moved under it, whereas FR-MSG-04 puts the action bar beneath AI answers only, so no correct client produces this one. It is also already the answer R-54(3) gives an errored turn: nothing was persisted, so there is nothing to rate. **Not gated by the R-24 processing lock**, although FR-MSG-08 names it. R-43(4) enforces that gate at exactly R-24's four *file* verbs and states read routes are never gated; this is a write, but on a row a finished turn already committed and served. The in-flight answer has no row yet, so it can never be this route's target — and because the lock is keyed on the caller, gating would refuse feedback on one message because a *different* one is generating, which is not the requirement's clause but a bug. FR-MSG-08's "disabled while generating" is discharged as the GUI affordance R-71(1) governs (OI-31, now resolved): the client-side in-flight signal disables the control, and the `409` this route deliberately does not raise is one the client would then have to reconcile for nothing. Idempotent: the same value twice is a `200`. The column is state, not an event log. Touches `messages.feedback` and nothing else — in particular never `evaluation`, which is DeepEval's alone (R-49(1), OI-34 as R-50(6) closed it). A human thumb is not a judge score, and the FR-EVL-02 chip must not move because someone disagreed with it.

- **Operation id:** `set_feedback`
- **Authentication:** bearer token
- **Request body:** [`FeedbackRequest`](#feedbackrequest) as `application/json` (required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `message_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`MessageResponse`](#messageresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | -- | No AI message with this id for this caller. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |

### POST /api/v1/messages/{message_id}/general-knowledge

<a id="answer-from-general-knowledge"></a>

Answer an abstention from the model's own training (FR-MSG-09)

FR-MSG-09 (R-98) — a second, explicitly ungrounded answer, appended beneath the refusal. **Not an SSE route, unlike its two siblings, and the difference is the point.** Send and regenerate stream because the graph emits FR-ORC-01 stage frames; this path has no stages to report — it is one `ChatClient` call — so streaming would be ceremony around a single result. The practical gain is that a plain handler can refuse with an ordinary status: a generator's body runs *after* the `200` (T-405), which is why regenerate has to hoist every refusal into `admit_regeneration`, and there is nothing here that needs hoisting. **It appends; the abstention stays.** R-98(1) leans on the abstention as the record that the corpus could not answer — the reason an automatic fallback is declined at all — so replacing it would delete the evidence. That is the deliberate opposite of Regenerate, which replaces (R-56) because there the old answer and the new one answer the same question; here they are different *kinds* of answer and the transcript should show both. Refusals, in order: * absent, foreign or non-AI target → one `404` with one copy, for an administrator too (R-55(2), R-54(1)) — identical to the feedback and regenerate routes; * the deployment has not enabled the control, or the target is not an abstention → `409`. Both are states a correct client cannot reach: the GUI only offers the control when `is_offerable` says so, and the same predicate is what the service re-checks. This is the server half of R-71(1)'s two-copies-of-one-state problem, and it is a reconciliation path rather than an expected outcome. **No R-24 lock and no budget check.** The lock gates the four *file* verbs (R-43(4)), and R-55(1) settled that a chat-side route must not take it — refusing here because a *different* chat is mid-turn is the defect that argument rejected. The FR-STA-04 budget is not checked because this adds no question: R-51(4) derives usage from `messages`, and the answer it appends is counted the next time a turn is submitted, where the refusal belongs.

- **Operation id:** `answer_from_general_knowledge`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `message_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `201` | [`MessageResponse`](#messageresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | No such message for this caller, or it is not an AI answer. |
| `409` | [`ErrorResponse`](#errorresponse) | The control is disabled on this server, or the target is not an abstention. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

## `system`

### GET /health

<a id="liveness"></a>

Liveness probe

Liveness probe — the process is up (no dependency checks).

- **Operation id:** `liveness`

| Status | Body | Meaning |
|---|---|---|
| `200` | [`LivenessResponse`](#livenessresponse) | Successful Response |

### GET /health/ready

<a id="readiness"></a>

API readiness probe

Readiness probe — every dependency the *API* serves requests from (NFR-REL-02).

- **Operation id:** `readiness`

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ReadinessResponse`](#readinessresponse) | Successful Response |
| `503` | [`ReadinessResponse`](#readinessresponse) | At least one dependency failed its probe (NFR-REL-02). |

### GET /health/ready/worker

<a id="worker-readiness"></a>

Worker readiness probe

Readiness probe for the ingestion worker deployable (T-207, R-38(2)).

- **Operation id:** `worker_readiness`

| Status | Body | Meaning |
|---|---|---|
| `200` | [`ReadinessResponse`](#readinessresponse) | Successful Response |
| `503` | [`ReadinessResponse`](#readinessresponse) | At least one dependency failed its probe (NFR-REL-02). |

## `users`

### GET /api/v1/users

<a id="list-users"></a>

List users

- **Operation id:** `list_users`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `first` | query | no | integer |  |
| `limit` | query | no | integer |  |
| `search` | query | no | string \| null |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`UserResponse`](#userresponse)[] | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### POST /api/v1/users

<a id="create-user"></a>

Create a user

- **Operation id:** `create_user`
- **Authentication:** bearer token
- **Request body:** [`CreateUserRequest`](#createuserrequest) as `application/json` (required)

| Status | Body | Meaning |
|---|---|---|
| `201` | [`UserResponse`](#userresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `409` | [`ErrorResponse`](#errorresponse) | A user with that email already exists. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### PATCH /api/v1/users/{user_id}

<a id="update-user"></a>

Update a user

- **Operation id:** `update_user`
- **Authentication:** bearer token
- **Request body:** [`UpdateUserRequest`](#updateuserrequest) as `application/json` (required)

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `user_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `200` | [`UserResponse`](#userresponse) | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | User not found. |
| `409` | [`ErrorResponse`](#errorresponse) | You cannot perform this action on your own account. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

### DELETE /api/v1/users/{user_id}

<a id="delete-user"></a>

Delete a user

- **Operation id:** `delete_user`
- **Authentication:** bearer token

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `user_id` | path | yes | string (uuid) |  |

| Status | Body | Meaning |
|---|---|---|
| `204` | -- | Successful Response |
| `401` | [`ErrorResponse`](#errorresponse) | Missing, malformed or expired bearer token, or no local user record. |
| `403` | [`ErrorResponse`](#errorresponse) | The account is deactivated, or the operation is administrator-only (NFR-SEC-01). |
| `404` | [`ErrorResponse`](#errorresponse) | User not found. |
| `409` | [`ErrorResponse`](#errorresponse) | You cannot perform this action on your own account. |
| `422` | [`HTTPValidationError`](#httpvalidationerror) | Validation Error |
| `429` | [`ErrorResponse`](#errorresponse) | NFR-SEC-07 — too many attempts. `Retry-After` carries the cooldown. |
| `500` | [`ErrorResponse`](#errorresponse) | Server-side identity configuration error. Check the server logs. |
| `503` | [`ErrorResponse`](#errorresponse) | The identity provider is unreachable (R-28). |

## Schemas

### `AuditEventType`

<a id="auditeventtype"></a>

Security- and lifecycle-relevant audit categories (NFR-SEC-08).

One of: `AUTH`, `USER_ROLE_CHANGE`, `DOCUMENT_UPLOAD`, `DOCUMENT_REPLACE`, `DOCUMENT_DELETE`, `PERMISSION_CHANGE`

### `AuditLogResponse`

<a id="auditlogresponse"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `actor_id` | string (uuid) \| null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `details` | object \| null | yes |  |
| `event_type` | [`AuditEventType`](#auditeventtype) | yes |  |
| `id` | string (uuid) | yes |  |
| `target_id` | string \| null | yes |  |
| `target_type` | string \| null | yes |  |

### `Body_replace_document`

<a id="body-replace-document"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | string | yes |  |

### `Body_upload_document`

<a id="body-upload-document"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `conversation_id` | string (uuid) \| null | no |  |
| `file` | string | yes |  |
| `scope` | `global` \| `chat` | no |  |

### `ChangePasswordRequest`

<a id="changepasswordrequest"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `current_password` | string | yes |  |
| `new_password` | string | yes |  |

### `ChatConflictResponse`

<a id="chatconflictresponse"></a>

_No properties._

### `ChatDoneFrame`

<a id="chatdoneframe"></a>

End of turn.

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`DoneData`](#donedata) | yes |  |
| `event` | `done` | no |  |

### `ChatMessageFrame`

<a id="chatmessageframe"></a>

The whole verified answer, in one frame. There is no token streaming and that is a ruling, not an omission: R-48(1)/R-49(3) put the FR-CIT-06 gate between generation and the client, so nothing of the answer exists to send until it has passed (R-54(2)).

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`MessageFrameData`](#messageframedata) | yes |  |
| `event` | `message` | no |  |

### `ChatStageFrame`

<a id="chatstageframe"></a>

Coarse progress. Emitted only when the stage *changes*.

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`StageData`](#stagedata) | yes |  |
| `event` | `stage` | no |  |

### `ChatStreamFrame`

<a id="chatstreamframe"></a>

_No properties._

### `CheckResult`

<a id="checkresult"></a>

Outcome of a single dependency probe.

| Field | Type | Required | Description |
|---|---|---|---|
| `error` | string \| null | no |  |
| `latency_ms` | number \| null | no |  |
| `status` | `ok` \| `error` | yes |  |

### `CitationFigure`

<a id="citationfigure"></a>

One figure printed on the page a citation names (FR-CIT-07, R-94). **Resolved at read time and never persisted** — see :attr:`CitationSegment.RESOLVED_AT_READ` and `DocumentFigureRepository.list_for_citations`, which holds the reasoning. `_citation` does not write this, so a stored `messages.citations` row never carries it. Deliberately **not** carrying a `url`. The client must reach the T-715 route through the generated `paths` to inherit the session middleware's token freshening and 401 handling, and that needs path *parameters*; a URL assembled here would be a second copy of the route template in a place `schema.d.ts` cannot check. Nor a `doc` or a `page`: both are already on the enclosing segment, and the figure is selected *by* that locator's page, so a second copy could only drift from the thing that chose it. `widthPx`/`heightPx` are what let the client reserve the box before the image arrives. They are the figure's own recorded dimensions (T-714), not a layout hint.

| Field | Type | Required | Description |
|---|---|---|---|
| `caption` | string \| null | no |  |
| `contentSha256` | string | yes |  |
| `documentId` | string | yes |  |
| `heightPx` | integer | yes |  |
| `widthPx` | integer | yes |  |

### `CitationLocator`

<a id="citationlocator"></a>

R-34's structured address, beside the rendered label. Every field is optional because `Locator.as_metadata()` drops the ones that do not apply to the kind — a PDF citation carries `page`, a DOCX one `section_path`, a CSV one `row_start`/`row_end`. FR-CIT-04 is explicit that clients read **these fields** and never parse the label, which is why the structured copy is published at all.

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | [`CitationLocatorKind`](#citationlocatorkind) \| null | no |  |
| `label` | string \| null | no |  |
| `line_end` | integer \| null | no |  |
| `line_start` | integer \| null | no |  |
| `page` | integer \| null | no |  |
| `row_end` | integer \| null | no |  |
| `row_start` | integer \| null | no |  |
| `section_index` | integer \| null | no |  |
| `section_path` | string[] \| null | no |  |

### `CitationLocatorKind`

<a id="citationlocatorkind"></a>

One of: `page`, `section`, `rows`

### `CitationSegment`

<a id="citationsegment"></a>

A citation run — the FR-CIT-01 chip, the FR-CIT-03 hover card and the FR-CIT-07 figure. See :func:`_citation` for what each field carries and why `page` holds a label rather than a number.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunkId` | string | yes |  |
| `doc` | string | yes |  |
| `figures` | [`CitationFigure`](#citationfigure)[] | no | The FR-CIT-07 figures printed on the page this citation's locator names, in the document's own order. **Absent** — not null, not `[]` — when there are none, which is the ordinary case: figure extraction ships off, only PDFs have pages, and a page need not carry a figure. Resolved when the citation is served and never persisted, so a stored row never carries this key. Selected by the locator and never by the model (R-94(3)): a figure points at the cited page, and is not a claim that it supports the sentence. |
| `isCite` | `True` | no |  |
| `locator` | [`CitationLocator`](#citationlocator) \| null | no |  |
| `page` | string \| null | no |  |
| `quote` | string | yes |  |
| `score` | number \| null | no | The FR-CIT-04 rerank score. **Absent** — not null — when the reranker failed open and published none (R-47(2)). Render the card with no number. |

### `CloudLinkRequiredDetail`

<a id="cloudlinkrequireddetail"></a>

The caller's cloud drive needs attention before an import can run (T-214, FR-AUT-11). Two codes, one status, because the *cause* differs and the GUI's copy must too, while the action is the same one button: - ``ACCOUNT_NOT_LINKED`` — no link exists. The ordinary state of every user who has never asked for Drive, which is why FR-AUT-11 makes it a "link your account" affordance and never a 5xx. - ``CLOUD_ACCESS_REVOKED`` — a link exists and the *provider* refused the brokered token, typically because the user withdrew the grant at Google. Reporting this as "not linked" would be false, and would send the user to a status surface that says they are linked. `409` on the R-51(5) precedent: a refusal about the caller's state, not a failure, so it carries no `FailureClass`. Not `403` — the caller is permitted to import, they have simply not connected an account yet.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | `ACCOUNT_NOT_LINKED` \| `CLOUD_ACCESS_REVOKED` | yes |  |
| `message` | string | yes |  |
| `provider` | string | yes |  |

### `CloudLinkRequiredResponse`

<a id="cloudlinkrequiredresponse"></a>

The wire body for FR-AUT-11's refusal.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`CloudLinkRequiredDetail`](#cloudlinkrequireddetail) | yes |  |

### `CloudProvider`

<a id="cloudprovider"></a>

Providers a user may link. One member, and NFR-CMP-02 says exactly that: v1 commits to Google Drive, and the *mechanism* is what is provider-agnostic — Keycloak brokers the token, so a second provider is a realm identity provider plus a file-listing adapter. Modelling it as an enum keeps the provider a validated path segment rather than free text reaching a URL, which is the R-63(6)(1) discipline applied one level up from the file id.

One of: `google`

### `ConfigResponse`

<a id="configresponse"></a>

Deployment configuration the GUI renders. One field today, by design.

| Field | Type | Required | Description |
|---|---|---|---|
| `chat_model` | string | yes | The answer model in force, for FR-ANL-02's MODEL card: the operator's runtime override if one is set, else `OPENAI_CHAT_MODEL`. Still not the model that answered any particular turn — that is `MessageResponse.model_name`, which the GUI prefers where it exists. |

### `ConfiguredPipelineResponse`

<a id="configuredpipelineresponse"></a>

The three non-text inputs to FR-ING-03's fingerprint, as configured right now.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunking_version` | string | yes |  |
| `embedding_model` | string | yes |  |
| `preprocessing_version` | string | yes |  |

### `ContextWindowExceededDetail`

<a id="contextwindowexceededdetail"></a>

FR-STA-04's refusal (R-51(5)), on both `send` and `regenerate`. A refusal, not an FR-ORC-05 failure: nothing was attempted, so it carries no `FailureClass`. The three token counts are here because the GUI's FR-ANL-03 meter is the thing the user must act on, and re-fetching the conversation to learn why the composer refused would race the very state that refused it. `used_tokens` is the conversation's **real** usage on both paths, never the adjusted projection a regenerate checks against — a card showing a number the meter never displays would read as a bug in the meter.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | `CONTEXT_WINDOW_EXCEEDED` | yes |  |
| `limit_tokens` | integer | yes |  |
| `message` | string | yes |  |
| `overflow_tokens` | integer | yes |  |
| `used_tokens` | integer | yes |  |

### `ContextWindowExceededResponse`

<a id="contextwindowexceededresponse"></a>

The wire body. FastAPI wraps `detail`, so the envelope is what a client parses.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`ContextWindowExceededDetail`](#contextwindowexceededdetail) | yes |  |

### `ContextWindowResponse`

<a id="contextwindowresponse"></a>

The FR-ANL-03 CONTEXT WINDOW card, for one conversation (R-51 binds T-401 to carry it). **This is conversation length, not model cost.** R-30 counts history + the current query and excludes the system prompt and the retrieved chunks, so a turn's real `messages.prompt_tokens` will routinely exceed `used_tokens` — correctly, because they measure different quantities (R-51(1)). Nothing may derive one from the other.

| Field | Type | Required | Description |
|---|---|---|---|
| `answer_reserve_tokens` | integer | yes | The headroom FR-STA-04's projection reserves for the reply (`CONTEXT_ANSWER_RESERVE_TOKENS`, floored at `LLM_MAX_OUTPUT_TOKENS`). Published so the GUI can reproduce the server's admission decision *before* a request exists (R-51(3)/(6)) — without it the composer accepts what the server refuses the moment an operator raises the answer ceiling. |
| `limit_tokens` | integer | yes |  |
| `percent_used` | number | yes |  |
| `remaining_tokens` | integer | yes |  |
| `used_tokens` | integer | yes |  |

### `ConversationDetailResponse`

<a id="conversationdetailresponse"></a>

A single conversation, with the FR-ANL-03 meter. Carried on the single-conversation responses only. The list route omits it deliberately: the meter is derived from every `messages.content` in the chat (R-51(4) — usage is computed, never stored), so putting it on a list would read the full transcript of every conversation in the sidebar to render a card FR-ANL-03 shows for the *active* one. `message_count`, inherited from the base, is the one count that *is* on the list route — see its docstring for why the same objection does not apply to it.

| Field | Type | Required | Description |
|---|---|---|---|
| `archived` | boolean | yes |  |
| `context` | [`ContextWindowResponse`](#contextwindowresponse) | yes |  |
| `created_at` | string (date-time) | yes |  |
| `id` | string (uuid) | yes |  |
| `message_count` | integer | yes | How many messages this chat holds — FR-SBR-03's `· N messages`, on every row including the ones the sidebar never opens. |
| `title` | string \| null | yes |  |
| `updated_at` | string (date-time) | yes |  |

### `ConversationResponse`

<a id="conversationresponse"></a>

One conversation. `id` is also the LangGraph `thread_id` (FR-PER-02).

| Field | Type | Required | Description |
|---|---|---|---|
| `archived` | boolean | yes |  |
| `created_at` | string (date-time) | yes |  |
| `id` | string (uuid) | yes |  |
| `message_count` | integer | yes | How many messages this chat holds — FR-SBR-03's `· N messages`, on every row including the ones the sidebar never opens. |
| `title` | string \| null | yes |  |
| `updated_at` | string (date-time) | yes |  |

### `CreateConversationRequest`

<a id="createconversationrequest"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string \| null | no |  |

### `CreateUserRequest`

<a id="createuserrequest"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `display_name` | string \| null | no |  |
| `email` | string | yes |  |
| `password` | string | yes |  |
| `role` | [`Role`](#role) | no |  |

### `DegradedMessage`

<a id="degradedmessage"></a>

The served-but-unstored branch: an FR-ORC-05 failure or an FR-ORC-02 denial. R-54(3) keeps those turns out of `messages`, so there is no row and nothing to rate or regenerate — the intended consequence of not persisting a failed turn. **`id` is the one place a regenerate diverges from a send** (T-404), and only here: a send has no row so it is `null`, while a regenerate carries the *target's* id, because the client already has that bubble on screen and a null id would leave it unable to say which answer the error belongs to. `segs` is always exactly one text run.

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string (uuid) \| null | yes |  |
| `role` | `ai` | no |  |
| `segs` | [`TextSegment`](#textsegment)[] | yes |  |

### `DeleteResponse`

<a id="deleteresponse"></a>

FR-ING-05's `202` body, plus the R-39(2) already-deleted signal. `already_deleted` pairs with a `200`: nothing was queued, so `job_id` is null. Same shape as the FR-KBM-08 duplicate, and for the same reason — a second Delete click on a row that has already gone is not an error the GUI should render.

| Field | Type | Required | Description |
|---|---|---|---|
| `already_deleted` | boolean | no |  |
| `document_id` | string (uuid) | yes |  |
| `job_id` | string (uuid) \| null | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |

### `DocumentChangedFrame`

<a id="documentchangedframe"></a>

One row whose state changed. Emitted from **polling**, so it samples state rather than replaying every transition: a fast ingestion was measured going `QUEUED → INDEXING → ACTIVE` inside one interval (T-210). A client must render whatever arrives and never assume it will see each FR-ING-01 stage.

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`DocumentEventResponse`](#documenteventresponse) | yes |  |
| `event` | `document` | no |  |

### `DocumentConflictResponse`

<a id="documentconflictresponse"></a>

_No properties._

### `DocumentEventResponse`

<a id="documenteventresponse"></a>

The list/get DTO plus the one field the live channel adds (R-41(4)/(5)). Subclassed rather than redefined so the stream cannot drift from the route it mirrors: a field added to `DocumentResponse` appears here automatically, and a live table whose contents depended on whether the modal happened to be open when a change landed is exactly the failure this inheritance rules out. `stalled` is **derived, not stored** — no `DocumentStatus` carries it (R-38(3): a state exists to be written by a transition, and nothing transitions into "stalled") and FR-KBM-04 gains no ninth label. It means "an in-flight document has gone quiet longer than a worker could legitimately be silent", which T-212 measured as up to `job_timeout + 10` seconds behind arq's in-progress guard. It is **never** true for `DELETE_PENDING`/`DELETING`; R-39(7) requires those keep rendering as `Deleting`.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunk_count` | integer \| null | yes |  |
| `conversation_id` | string (uuid) \| null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `current_version` | integer | yes |  |
| `deleted_at` | string (date-time) \| null | yes |  |
| `document_id` | string (uuid) | yes |  |
| `error_message` | string \| null | yes |  |
| `filename` | string | yes |  |
| `knowledge_base_id` | string (uuid) | yes |  |
| `latest_job_document_version` | integer \| null | yes |  |
| `latest_job_error_code` | string \| null | yes |  |
| `latest_job_id` | string (uuid) \| null | yes |  |
| `mime_type` | string \| null | yes |  |
| `page_count` | integer \| null | yes |  |
| `scope` | `global` \| `chat` | yes |  |
| `searchable` | boolean | yes |  |
| `size_bytes` | integer \| null | yes |  |
| `stalled` | boolean | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |
| `text_quality_degraded` | boolean | no |  |
| `updated_at` | string (date-time) | yes |  |

### `DocumentRemovedData`

<a id="documentremoveddata"></a>

A document that has left the caller's set. Only an id: a completed deletion has no visible state to transition into, so a tombstone can signal only by disappearing (R-41(4)).

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string (uuid) | yes |  |

### `DocumentRemovedFrame`

<a id="documentremovedframe"></a>

A document left the set — deleted, or moved out of the caller's scope.

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`DocumentRemovedData`](#documentremoveddata) | yes |  |
| `event` | `removed` | no |  |

### `DocumentResponse`

<a id="documentresponse"></a>

One document, for both the list and the single-document read (R-40(5)). **Metadata only.** R-31(4) names T-209 as a likely route to a download/export/preview surface, which would make Corpus a file-distribution vector and turn the malware scanner from optional into required — so `storage_uri` is deliberately absent and no field on this model carries or points at bytes. `checksum_sha256` is absent too: no requirement asks for it, and the duplicate story is already told by the upload and replace responses. No chunk id appears anywhere either (R-36(6)(b)): a replaced document's historical chunk ids dangle by design, and nothing here may become a way to resolve one. **`current_version` is the version serving retrieval, not the version being built.** While a replace is in flight `latest_job_document_version` is `current_version + 1`, and that inequality is the only signal distinguishing "this document failed and is silent" from "this document's replace failed and the previous version still answers". R-71(2) settles what the GUI does with it (OI-29, resolved): the row keeps `Failed` and its Retry affordance, and the FR-KBM-04 meta line reads `update failed, v{current_version} still answering`. The job's own status and progress are deliberately not denormalised here — `GET /jobs/{id}` owns them, and a surface rendering two statuses read at two different moments will eventually render them disagreeing.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunk_count` | integer \| null | yes |  |
| `conversation_id` | string (uuid) \| null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `current_version` | integer | yes |  |
| `deleted_at` | string (date-time) \| null | yes |  |
| `document_id` | string (uuid) | yes |  |
| `error_message` | string \| null | yes |  |
| `filename` | string | yes |  |
| `knowledge_base_id` | string (uuid) | yes |  |
| `latest_job_document_version` | integer \| null | yes |  |
| `latest_job_error_code` | string \| null | yes |  |
| `latest_job_id` | string (uuid) \| null | yes |  |
| `mime_type` | string \| null | yes |  |
| `page_count` | integer \| null | yes |  |
| `scope` | `global` \| `chat` | yes |  |
| `searchable` | boolean | yes |  |
| `size_bytes` | integer \| null | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |
| `text_quality_degraded` | boolean | no |  |
| `updated_at` | string (date-time) | yes |  |

### `DocumentSnapshotFrame`

<a id="documentsnapshotframe"></a>

The full set, sent on **every** connect — including a reconnect. There is no `Last-Event-ID` replay by ruling (R-41(6)): a fresh snapshot is strictly more correct than a delta stream with gaps in it.

| Field | Type | Required | Description |
|---|---|---|---|
| `data` | [`DocumentEventResponse`](#documenteventresponse)[] | yes |  |
| `event` | `snapshot` | no |  |

### `DocumentStatus`

<a id="documentstatus"></a>

The 11 document lifecycle states (FR-ING-01, §4.15).

One of: `UPLOADED`, `QUEUED`, `PARSING`, `CHUNKING`, `EMBEDDING`, `INDEXING`, `ACTIVE`, `FAILED`, `DELETE_PENDING`, `DELETING`, `DELETED`

### `DocumentStreamFrame`

<a id="documentstreamframe"></a>

_No properties._

### `DoneData`

<a id="donedata"></a>

Closes the turn. Carries the outcome again so a client that dropped the `message` frame still learns how it ended.

| Field | Type | Required | Description |
|---|---|---|---|
| `outcome` | [`TurnOutcome`](#turnoutcome) \| null | no |  |

### `DriveFileResponse`

<a id="drivefileresponse"></a>

One row of the FR-KBM-10 selection surface. Metadata only, on `DocumentResponse`'s principle: no URL, no download link, nothing that points at bytes. The client sends `file_id` back to the import route and the backend resolves it against the provider's fixed host (R-63(6)(1)).

| Field | Type | Required | Description |
|---|---|---|---|
| `file_id` | string | yes |  |
| `mime_type` | string | yes |  |
| `modified_time` | string \| null | yes |  |
| `name` | string | yes |  |
| `size_bytes` | integer \| null | yes |  |

### `DriveListResponse`

<a id="drivelistresponse"></a>

A page of the flat list. `next_page_token` is the provider's, opaque, and echoed back.

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | [`DriveFileResponse`](#drivefileresponse)[] | yes |  |
| `next_page_token` | string \| null | no |  |

### `ErrorResponse`

<a id="errorresponse"></a>

The ordinary error body: ``{"detail": "..."}``. Not a bespoke envelope — this *is* what `HTTPException(status, "some copy")` serialises to, and re-shaping it now would break every route at once for no gain. Declaring it simply stops the generated client typing an error body as `unknown`. The copy is frequently a `# TBD(§8.4)` string, so a client must **render** it and never match on it. Where a decision hangs on the error, there is a code — see the two models below, and R-43(6)'s `FailureClass` on the SSE path.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | string | yes | Human-readable copy. Render it; never branch on it. |

### `EvaluationResponse`

<a id="evaluationresponse"></a>

The FR-EVL-02 chips — **exactly two** metrics (R-50(1)). The other two FR-EVL-01 metrics are reference-based and cannot run on a live turn, which is why they belong to the offline harness (T-312) and their FR-ANL-04 cells read `—` permanently. Either field may be `None`: the evaluation path **fails open** (R-50(3)) and metrics are guarded independently, so a partial result is written rather than discarded. The whole object is `None` until the FR-EVL-01 job lands and stays `None` for ever if the judge never answers — a correct end state, not an error. `groundedness` is deliberately absent: the T-308 gate's structural number is not this chip (R-49(1), OI-34 as R-50(6) closed it), and a human thumb is not a judge score either (R-55(5)).

| Field | Type | Required | Description |
|---|---|---|---|
| `faithfulness` | number \| null | no |  |
| `relevancy` | number \| null | no |  |

### `Feedback`

<a id="feedback"></a>

Thumbs feedback on an AI message (FR-MSG-06/08).

One of: `up`, `down`

### `FeedbackRequest`

<a id="feedbackrequest"></a>

FR-MSG-08's thumbs payload — three states, and the key is **required**. FR-MSG-06 types the field `'up'\|'down'\|null`, so clearing is reachable (FR-MSG-08 says the control *toggles*, and a second click on the lit thumb turns it off) and has to be expressible. `Feedback` has no `NONE` member and must not grow one: `NULL` is already the column's absent state, and a third enum value would make "cleared" and "never rated" two representations of one fact. Required rather than defaulted to `None`, which is `RenameConversationRequest.title`'s shape: with a default, `{}` would silently erase a rating the user gave. It is a `422`.

| Field | Type | Required | Description |
|---|---|---|---|
| `feedback` | [`Feedback`](#feedback) \| null | yes |  |

### `HTTPValidationError`

<a id="httpvalidationerror"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`ValidationError`](#validationerror)[] | no |  |

### `ImportConflictResponse`

<a id="importconflictresponse"></a>

_No properties._

### `ImportRequest`

<a id="importrequest"></a>

Import one cloud-drive file into a knowledge base. `scope`/`conversation_id` are spelled exactly as the upload *form* spells them, so the KB modal uses one vocabulary whichever way a document arrives. JSON rather than multipart because there is no file part: the client sends an id it got from `GET /cloud/{provider}/files`, and the backend fetches the bytes from the provider's fixed host (R-63(6)(1)). A client-supplied URL here is exactly what the ruling forbids.

| Field | Type | Required | Description |
|---|---|---|---|
| `conversation_id` | string (uuid) \| null | no |  |
| `file_id` | string | yes |  |
| `provider` | [`CloudProvider`](#cloudprovider) | yes |  |
| `scope` | `global` \| `chat` | no |  |

### `JobResponse`

<a id="jobresponse"></a>

FR-ING-06's job view. `progress` is the coarse stage milestone the worker writes (5/25/45/65/85/100); no stage reports partial progress, so it moves in steps rather than smoothly.

| Field | Type | Required | Description |
|---|---|---|---|
| `attempt_count` | integer | yes |  |
| `completed_at` | string (date-time) \| null | yes |  |
| `created_at` | string (date-time) | yes |  |
| `document_id` | string (uuid) | yes |  |
| `document_version` | integer | yes |  |
| `error_code` | string \| null | yes |  |
| `error_message` | string \| null | yes |  |
| `job_id` | string (uuid) | yes |  |
| `job_type` | [`JobType`](#jobtype) | yes |  |
| `progress` | integer | yes |  |
| `started_at` | string (date-time) \| null | yes |  |
| `status` | [`JobStatus`](#jobstatus) | yes |  |

### `JobStatus`

<a id="jobstatus"></a>

Knowledge-job execution states, incl. dead-letter (FR-ING-04/06).

One of: `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DEAD_LETTER`

### `JobType`

<a id="jobtype"></a>

Knowledge-job kinds (FR-ING-02/05).

One of: `INGEST`, `DELETE`

### `LinkStartResponse`

<a id="linkstartresponse"></a>

Where to send the browser to begin linking. A URL the client *navigates to*, not one it fetches: the flow is a redirect chain through Keycloak's login page and Google's consent screen, neither of which can be satisfied by an XHR. Returned as JSON rather than as a `302` so the caller — an authenticated fetch from the KB modal — can open it deliberately.

| Field | Type | Required | Description |
|---|---|---|---|
| `authorize_url` | string | yes |  |

### `LinkStatusResponse`

<a id="linkstatusresponse"></a>

Whether the caller has linked this provider (FR-AUT-11). `account` is the provider-side address, so the GUI can say *which* Google account it will import from. It is metadata, never a credential and never a provider user id.

| Field | Type | Required | Description |
|---|---|---|---|
| `account` | string \| null | no |  |
| `linked` | boolean | yes |  |
| `provider` | [`CloudProvider`](#cloudprovider) | yes |  |

### `LivenessResponse`

<a id="livenessresponse"></a>

Liveness is a constant: reaching the handler at all is the answer.

| Field | Type | Required | Description |
|---|---|---|---|
| `status` | `ok` | yes |  |

### `LoginRequest`

<a id="loginrequest"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `email` | string | yes |  |
| `password` | string | yes |  |

### `MeResponse`

<a id="meresponse"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `display_name` | string \| null | yes |  |
| `email` | string | yes |  |
| `id` | string (uuid) | yes |  |
| `is_active` | boolean | yes |  |
| `roles` | string[] | yes |  |

### `MessageFrameData`

<a id="messageframedata"></a>

The completed turn: one verified answer, or the copy that replaces it.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | string \| null | no | An FR-ORC-05 `FailureClass`. Absent on `abstained` and injection-`blocked` turns, which are decisions rather than failures and carry their own copy (R-44(5)). |
| `message` | [`MessageResponse`](#messageresponse) \| [`DegradedMessage`](#degradedmessage) | yes |  |
| `outcome` | [`TurnOutcome`](#turnoutcome) \| null | no |  |

### `MessageResponse`

<a id="messageresponse"></a>

One message, in the FR-MSG-06 shape. `segs` is **derived, never stored as such** (R-48(4)): `messages.content` holds the answer with its `[S<n>]` markers intact and `messages.citations` the resolved segments. The raw content is deliberately not exposed — a client rendering it would show the markers, which is precisely what the segmentation exists to prevent. Both JSONB-backed fields are typed as of T-405 — `list[dict[str, Any]]` reached a generated client as `Record<string, never>[]`, so the FR-CIT-01 chip and FR-CIT-03 hover card would have been hand-written types. Neither typing can fail a read: both go through a tolerant coercion (`envelope_segments`, :func:`_evaluation`) that falls back rather than raising.

| Field | Type | Required | Description |
|---|---|---|---|
| `completion_tokens` | integer \| null | no |  |
| `created_at` | string (date-time) | yes |  |
| `evaluation` | [`EvaluationResponse`](#evaluationresponse) \| null | no |  |
| `feedback` | [`Feedback`](#feedback) \| null | no |  |
| `id` | string (uuid) | yes |  |
| `latency_ms` | integer \| null | no |  |
| `model_name` | string \| null | no |  |
| `prompt_tokens` | integer \| null | no |  |
| `role` | [`MessageRole`](#messagerole) | yes |  |
| `segs` | [`Segment`](#segment)[] | yes |  |
| `ungrounded` | boolean | no | FR-MSG-09 - this answer was generated from the model's own training with no retrieved passages. It never carries citations or evaluation scores, and it is excluded from FR-EVL-04's session averages. Render it in a visually distinct treatment so a reader can always tell it from a grounded answer. |
| `ungrounded_offerable` | boolean | no | Whether to offer FR-MSG-09's “Answer from general knowledge” control on this message. True only for an AI answer that abstained - it cites nothing and is not itself ungrounded - on a deployment where the operator enabled the fallback. The last term is not otherwise on the wire, so do not re-derive this. |

### `MessageRole`

<a id="messagerole"></a>

Message author (FR-MSG-06).

One of: `user`, `ai`

### `NotLatestAnswerDetail`

<a id="notlatestanswerdetail"></a>

FR-MSG-08 / R-56(1) — the target is no longer the latest AI answer. A **second code** rather than a second copy of the one above, because the two are resolved differently: the budget refusal ends the conversation until the user starts a new chat, while this one clears the moment the client reloads the transcript. `app/rag/errors.py:99` records that the distinction reaches the client only if the codes stay distinct here; the `Literal` is what carries it into the generated types.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | `NOT_LATEST_ANSWER` | yes |  |
| `message` | string | yes |  |

### `NotLatestAnswerResponse`

<a id="notlatestanswerresponse"></a>

The wire body for R-56(1)'s refusal.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`NotLatestAnswerDetail`](#notlatestanswerdetail) | yes |  |

### `ProcessingLockedDetail`

<a id="processinglockeddetail"></a>

R-24 / R-43(4)'s gate on the four file verbs, with a code the client can branch on. **Why a code and not just copy (R-71(1), OI-31).** These four routes answer `409` for more than one reason — `NotRetryableError`, `NotReplaceableError` and `DuplicateChecksumError` live on the same status — and R-71(1) makes the GUI *reconcile* an unpredicted lock `409` rather than render it as an error: it disables its own affordances and clears them when the turn ends. That reconciliation has to know which `409` arrived, and the only alternative is matching on a `# TBD(§8.4)` string, which R-57(4) forbids in as many words. Three of the four cases are derivable from the route and the row's state; **Replace is not** — it can answer `409` from `ACTIVE`/`FAILED` for either reason — so without this the client is left guessing on exactly the verb it cannot guess about. A refusal about the caller's *session*, not a failure, so it carries no `FailureClass` — the R-51(5) precedent, and the same reasoning that put `409` here rather than `423`/`429`.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | `PROCESSING_LOCKED` | yes |  |
| `message` | string | yes |  |

### `ProcessingLockedResponse`

<a id="processinglockedresponse"></a>

The wire body for R-24's gate.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`ProcessingLockedDetail`](#processinglockeddetail) | yes |  |

### `ReadinessResponse`

<a id="readinessresponse"></a>

NFR-REL-02's readiness body, on both arms (T-405). Typed so the `503` branch is expressible in the schema at all: the status code is set imperatively on the response object, so FastAPI cannot infer it, and before T-405 the whole body was an untyped `dict[str, object]`.

| Field | Type | Required | Description |
|---|---|---|---|
| `checks` | object | yes |  |
| `status` | `ok` \| `degraded` | yes |  |

### `RebuildConflictDetail`

<a id="rebuildconflictdetail"></a>

Why a T-608 re-embed was refused (R-84 → 409). Three codes, one status. All three are refusals about the *document's state*, not failures, so none carries a `FailureClass` — the R-51(5) precedent, and the same reasoning that keeps R-24's gate on `409` rather than `423`. They are separate codes because the operator's next action differs for each and no two of them are derivable from the same follow-up read: - ``NOT_REBUILDABLE`` — the document is not `ACTIVE` (R-84(4)). Transient for anything in flight, permanent for the deletion path; either way, look at the document. - ``NOT_STALE`` — it was already built by the configured pipeline (R-84(3)). **The load-bearing one:** it is what keeps this trigger *controlled* rather than a re-embed button, so a client must be able to tell it from the other two and report it as "nothing to do" rather than as an error. - ``ORIGINAL_CORRUPT`` — the stored original no longer matches `checksum_sha256` (R-84(8)). Neither transient nor a state to wait out: it needs a human, and re-running the batch will report it again forever until one arrives.

| Field | Type | Required | Description |
|---|---|---|---|
| `error_code` | `NOT_REBUILDABLE` \| `NOT_STALE` \| `ORIGINAL_CORRUPT` | yes |  |
| `message` | string | yes |  |

### `RebuildConflictResponse`

<a id="rebuildconflictresponse"></a>

The wire body for R-84's three refusals.

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | [`RebuildConflictDetail`](#rebuildconflictdetail) | yes |  |

### `RebuildResponse`

<a id="rebuildresponse"></a>

What `POST /admin/documents/{id}/reembed` renders. Both versions, because both are true at once: `version` is what the worker will build, `previous_version` what keeps answering questions until the swap commits (R-36(3)). One number for both would be the likeliest source of a bug on this surface — the reason `ReplaceOutcome` makes the same distinction.

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string (uuid) | yes |  |
| `job_id` | string (uuid) | yes |  |
| `previous_version` | integer | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |
| `version` | integer | yes |  |

### `RenameConversationRequest`

<a id="renameconversationrequest"></a>

FR-SBR-04. `title` is required — this route renames and does nothing else.

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | string | yes |  |

### `ReplaceResponse`

<a id="replaceresponse"></a>

R-40(1)'s `202`, plus the identical-bytes `200`. `version` is the version the worker will **build**; `DocumentResponse.current_version` stays on the one still serving until the swap commits (R-36(3)). The names differ on purpose. On the duplicate `200` the two are equal and `job_id` is null.

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string (uuid) | yes |  |
| `duplicate` | boolean | no |  |
| `job_id` | string (uuid) \| null | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |
| `version` | integer | yes |  |

### `RetryResponse`

<a id="retryresponse"></a>

FR-ING-06's retry `202`. Always a fresh job id (R-39(5)).

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string (uuid) | yes |  |
| `job_id` | string (uuid) | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |

### `Role`

<a id="role"></a>

Corpus roles. Values are the Keycloak role names.

One of: `admin`, `user`

### `Segment`

<a id="segment"></a>

_No properties._

### `SendMessageRequest`

<a id="sendmessagerequest"></a>

FR-CMP-01's composer payload. `document_ids` are the FR-CMP-04 `@`-mentions and they **narrow** the retrieval scope (R-46(1)) — they are AND-ed with the caller's ambient scope, never unioned, so a mention naming a document outside it retrieves nothing rather than reaching it.

| Field | Type | Required | Description |
|---|---|---|---|
| `document_ids` | string (uuid)[] | no |  |
| `query` | string | yes |  |

### `StageData`

<a id="stagedata"></a>

R-43(5) made structural: this payload **cannot** carry text. One field, so the "progress frames carry no content" rule of R-54(2) is a property of the type rather than a discipline at the yield site.

| Field | Type | Required | Description |
|---|---|---|---|
| `stage` | [`TurnStage`](#turnstage) | yes |  |

### `StaleDocumentResponse`

<a id="staledocumentresponse"></a>

One document whose live version was built by a different pipeline. Metadata only (R-40(5)): no chunk text, no `storage_uri`, no chunk id — which is also what keeps R-31(4)'s revisit trigger untripped, since nothing here re-serves uploaded content.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunk_count` | integer | yes |  |
| `document_id` | string (uuid) | yes |  |
| `document_version` | integer | yes |  |
| `drifted_inputs` | string[] | yes |  |
| `filename` | string | yes |  |
| `owner_id` | string (uuid) | yes |  |
| `token_count` | integer | yes |  |

### `StaleDocumentsResponse`

<a id="staledocumentsresponse"></a>

What `GET /admin/documents/stale` renders.

| Field | Type | Required | Description |
|---|---|---|---|
| `documents` | [`StaleDocumentResponse`](#staledocumentresponse)[] | yes |  |
| `pipeline` | [`ConfiguredPipelineResponse`](#configuredpipelineresponse) | yes |  |
| `totals` | [`StaleTotalsResponse`](#staletotalsresponse) | yes |  |

### `StaleTotalsResponse`

<a id="staletotalsresponse"></a>

Totals over the whole stale set, not the page. `token_count` is FR-ING-03's `ceil(len/4)` estimate (R-35(7)) summed over every chunk a full run would re-embed — indicative rather than a quotation, and here because "re-embed 4,100 documents" and "spend ~11M embedding tokens" are the same sentence to the database and very different sentences to whoever is paying. `in_flight` is reported separately rather than folded in: a document already being rebuilt is neither work to do nor work done, and omitting it would read as "nothing left" mid-run.

| Field | Type | Required | Description |
|---|---|---|---|
| `chunks` | integer | yes |  |
| `documents` | integer | yes |  |
| `in_flight` | integer | yes |  |
| `token_count` | integer | yes |  |

### `TextSegment`

<a id="textsegment"></a>

A plain run of the answer. The persisted shape is exactly ``{"text": ...}`` and nothing may be added: this is also what `plain_segments` writes for abstentions, blocked turns and any pre-T-402 row.

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | yes |  |

### `TokenResponse`

<a id="tokenresponse"></a>

The half of the token pair a browser is allowed to hold (R-72(1), FR-AUT-07). **There is deliberately no ``refresh_token`` field.** T-509 moved it into an httpOnly cookie, and a cookie set beside a body copy protects nothing — script would still read the body. ``RefreshRequest`` and ``LogoutRequest`` were removed for the same reason: with no way for a client to *obtain* a refresh token, a body that accepts one is a second channel that can only ever be a bypass. ``expires_in`` is Keycloak's ``accessTokenLifespan`` (300s on the shipped realm), and the client refreshes against it rather than waiting for a 401.

| Field | Type | Required | Description |
|---|---|---|---|
| `access_token` | string | yes |  |
| `expires_in` | integer | yes |  |
| `token_type` | string | no |  |

### `TurnOutcome`

<a id="turnoutcome"></a>

One of: `answered`, `abstained`, `blocked`, `error`, `review`

### `TurnStage`

<a id="turnstage"></a>

One of: `preparing`, `retrieving`, `generating`, `verifying`

### `UpdateUserRequest`

<a id="updateuserrequest"></a>

Admin PATCH — every field optional; at least one required (FR-USR-05/07).

| Field | Type | Required | Description |
|---|---|---|---|
| `display_name` | string \| null | no |  |
| `is_active` | boolean \| null | no |  |
| `new_password` | string \| null | no |  |
| `role` | [`Role`](#role) \| null | no |  |

### `UploadResponse`

<a id="uploadresponse"></a>

FR-ING-02's `202` body, plus the FR-KBM-08 duplicate signal. On a duplicate the response is `200` (nothing was queued), `job_id` is null and `status` is the *existing* document's lifecycle state, so the drop zone can point at the row already present instead of showing an error.

| Field | Type | Required | Description |
|---|---|---|---|
| `document_id` | string (uuid) | yes |  |
| `duplicate` | boolean | no |  |
| `job_id` | string (uuid) \| null | yes |  |
| `status` | [`DocumentStatus`](#documentstatus) | yes |  |

### `UserResponse`

<a id="userresponse"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `display_name` | string \| null | yes |  |
| `email` | string | yes |  |
| `id` | string (uuid) | yes |  |
| `is_active` | boolean | yes |  |
| `roles` | string[] | yes |  |

### `ValidationError`

<a id="validationerror"></a>

| Field | Type | Required | Description |
|---|---|---|---|
| `ctx` | object | no |  |
| `input` | any | no |  |
| `loc` | string \| integer[] | yes |  |
| `msg` | string | yes |  |
| `type` | string | yes |  |
