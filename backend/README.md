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
uv run arq workers.main.WorkerSettings    # background worker (after T-207)
```

> Windows dev: uvloop is Unix-only — uvicorn falls back to asyncio locally; it
> engages on Linux deployments.
