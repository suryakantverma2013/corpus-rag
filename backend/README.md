# Corpus backend

FastAPI + LangGraph + PostgreSQL/pgvector backend for the Corpus RAG Chatbot
(Nexus AI platform). Python 3.14, dependencies managed with **uv**. Conventions
live in the `backend-dev` skill; requirements in `../Nexus_AI_Detailed_Specification.md`.

## Layout

```
app/
  api/          routers: auth, users, knowledge_bases, documents, jobs, conversations, messages
  auth/         Keycloak token validation (RS256/JWKS), ROPC token exchange, role deps (R-28)
  db/           models/, repositories/, migrations/ (Alembic)
  ingestion/    parser, chunker, incremental, pipeline
  rag/          graph, state, retrievers, reranker, generator, citations, evaluation
  security/     prompt_injection, authorization, content_validation
  services/     object_storage, telemetry, audit
  config.py     pydantic-settings (minimal here; full module is T-005)
  main.py       FastAPI app factory + /health
workers/        arq worker entrypoints + tasks
tests/
```

## Auth (R-28)

Keycloak (OIDC) is the authentication baseline, backend-mediated (ROPC): the API
exchanges login credentials with Keycloak's token endpoint and validates
realm-signed RS256 JWTs against the realm JWKS. There is **no local password
hashing**. Configure via `KEYCLOAK_*` env vars (see `app/config.py`). Wired in T-103.

## Object storage (R-19, T-201)

Originals and derived artifacts live in object storage; `documents` rows keep only
metadata plus the `storage_uri`. `app/services/object_storage.py` exposes one
`ObjectStorage` protocol with two backends, chosen by `STORAGE_BACKEND`:

- `s3` (default) — MinIO or AWS S3 over `aioboto3`, configured by the `MINIO_*` vars.
- `local` — a directory tree under `STORAGE_LOCAL_ROOT`; **development only** (R-19).

Keys are built by `original_key()` / `artifact_key()` as
`tenants/{tenant}/kb/{kb}/documents/{doc}/v{n}/…`, so deleting a document (FR-ING-05)
or replacing a version is a single prefix delete. There is deliberately **no download,
presigned-URL, or preview helper** — adding one is R-31's revisit trigger and would
make a real malware scanner mandatory.

## Uploads (T-202, R-33)

`POST /api/v1/documents` — `multipart/form-data` with `file`, `scope` (`global` or
`chat`), and `conversation_id` when `scope=chat`. The knowledge base is resolved
server-side (the user's GLOBAL default, or the conversation's implicit attachment KB),
so clients never handle KB ids. Returns `202 {document_id, job_id, status:"QUEUED",
duplicate:false}`; an identical checksum in the same KB returns `200` with
`duplicate:true`, the existing `document_id`, and a null `job_id`.

Two controls are required here by R-31(3) and are regression-tested:

- **Type is decided by content, not the extension** — `app/security/content_validation.py`.
  PDF magic must sit at offset 0; a DOCX must really be an OOXML package; CSV/MD have no
  magic bytes, so they must instead pass a binary/markup deny-list plus whole-payload
  UTF-8 and control-byte validation. A file whose content contradicts its extension is
  rejected `415`.
- **The 50 MB limit is enforced before anything is written to storage**, during the read
  loop — a rejected upload leaves no object behind.

Malware scanning is *not* here: R-31 moved it to the head of the ingestion worker (T-207)
so a 50 MB scan never blocks the `202`.

Background dispatch goes through `app/services/jobs.py`. `QUEUE_BACKEND=none` selects a
no-op queue so the API runs without Redis; the job row is still written, so nothing is
lost. If the broker is down the upload still returns `202` and the job is marked
`error_code="ENQUEUE_FAILED"` for T-207's sweeper.

## Running

```bash
docker compose -f ../deployment/docker-compose.yml up -d   # infra: redis (default)
                                          #   reuse local MinIO/Postgres; add
                                          #   `--profile minio` / `--profile postgres` if none
cp .env.example .env                      # then fill in secrets (OPENAI_API_KEY, DB password)
uv sync                                   # create venv + install deps
uv run uvicorn app.main:app --reload      # API  -> http://127.0.0.1:8000  (/health, /docs)
uv run pytest                             # tests
uv run ruff check . && uv run ruff format .
uv run alembic upgrade head               # apply DB migrations
uv run python -m app.services.checkpointer  # provision LangGraph checkpointer tables (once)
uv run arq workers.main.WorkerSettings    # background worker (after T-207)
```

> Windows dev: uvloop is Unix-only — uvicorn falls back to asyncio locally; it
> engages on Linux deployments.

> **Windows dev, event loop (T-301).** The conversation checkpointer (FR-PER-01) is
> psycopg-backed, and psycopg's async driver needs `loop.add_reader`, which Windows'
> default `ProactorEventLoop` does not implement. uvicorn picks that loop unless it is
> running with a subprocess, so **`--reload` works but a bare `uvicorn app.main:app` does
> not**. Either use `--reload`, or be explicit:
>
> ```
> uv run uvicorn app.main:app --loop app.runtime:selector_loop
> ```
>
> On the wrong loop the app still boots; the first chat turn raises a
> `CheckpointerConfigError` naming both fixes. Linux/macOS and uvloop are unaffected.

> The checkpointer tables are **not** owned by Alembic — see `deployment/README.md`. The
> bootstrap command above is idempotent and belongs beside `alembic upgrade head` in any
> deploy script.
