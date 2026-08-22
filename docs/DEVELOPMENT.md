# Development setup

Running Corpus on your own machine, with the **application native and its infrastructure in
containers**. This is the setup the test suites, the end-to-end journey and the visual harnesses all
expect.

> **If you only want to run the product, not change it**, use the containerized stack instead — one
> command, everything included: [DEPLOYMENT.md](DEPLOYMENT.md). The two are independent, and the
> development compose file is untouched by anything there.

---

## 1. Prerequisites

| What | Version | Why |
|---|---|---|
| **Python** | 3.14 | Pinned by `backend/.python-version`; `uv` installs it for you |
| **[uv](https://docs.astral.sh/uv/)** | current | Every backend command is `uv run …`; it owns the virtualenv |
| **Node** | 24 | Matches `frontend/Dockerfile`; nothing enforces it, so check yours |
| **PostgreSQL** | 16+ with **pgvector** | Vectors, full-text search *and* the conversation checkpointer all live here |
| **Docker** + Compose v2 | ≥ 2.17 | The stack uses `--wait` and `service_completed_successfully` |
| **Keycloak** | 26.x | Identity. Not in any compose file here — see §3 |
| **Chrome/Chromium** | current | Only for `npm run fidelity` and `npm run a11y` |
| **An OpenAI API key** | — | Chat, embeddings, rerank and the judge all use it |

**Prove your machine is ready** — every line should answer, not error:

```bash
uv --version
node --version                                          # expect v24.x
docker compose version                                  # v2.17 or newer
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/realms/corpus   # 200 = Keycloak ready

# Postgres and pgvector, without needing psql on PATH — uses the project's own venv
cd backend && uv run python -c "
import asyncio, asyncpg
from app.config import get_settings
async def main():
    c = await asyncpg.connect(get_settings().database.url.replace('postgresql+asyncpg://','postgresql://'))
    print((await c.fetchval('select version()')).split(',')[0])
    print('pgvector:', await c.fetchval(\"select extversion from pg_extension where extname='vector'\"))
    await c.close()
asyncio.run(main())"
```

That last check is deliberately not `psql`. **The PostgreSQL client binaries are frequently not on
`PATH`** — on Windows they install to `C:\Program Files\PostgreSQL\<major>\bin`, and on this machine
`psql` is absent from the shell while the server runs perfectly. You will still need `psql` and
`createdb` for the one-time database creation in §4, so either add that directory to `PATH` or call
them by full path.

*Verified on this machine: PostgreSQL 18.1, pgvector 0.8.1, uv 0.9.21, Node v24.15.0.*

## 2. What runs where, and why

| Component | How | Port |
|---|---|---|
| PostgreSQL + pgvector | **native** | 5432 |
| Keycloak | **native or a standalone container** | 8081 |
| Redis (arq broker, rate limiter) | container | 6379 |
| MinIO | container | 9100 API / 9101 console |
| ClamAV | container | 3310 |
| OCR sidecar *(optional)* | container | 8884 |
| FastAPI, arq worker, Vite | **native processes** | 8000 / — / 5173 |

The split is deliberate: you edit the application constantly and the infrastructure never, so the
parts you change run where you can attach a debugger. One consequence worth knowing — the OCR
sidecar publishes a host port here, and does **not** in the production stack, because there the
worker reaches it over the compose network.

**MinIO on 9100, not 9000.** The port is env-driven precisely because the machine that needs this
profile is usually the machine already running something else on 9000. If you have your own MinIO on
9000, use it and leave `MINIO_ENDPOINT` alone; if you start the profile below, point
`MINIO_ENDPOINT` at whatever you published.

## 3. Keycloak

**Keycloak is in no compose file in this repository** — not the development one, not the production
one's development counterpart. It is a standalone container or a native install, depending on the
machine, and `docker compose up` will never start it.

On a machine where it already exists as a container:

```bash
docker start corpus-keycloak
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/realms/corpus   # 200
```

From nothing, importing the committed realm:

```bash
docker run -d --name corpus-keycloak -p 8081:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  -v "$PWD/deployment/keycloak:/opt/keycloak/data/import:ro" \
  quay.io/keycloak/keycloak:26.4 start-dev --import-realm
```

Then **two manual steps the realm cannot do for you** — the committed JSON ships placeholders that
are live credentials the moment it imports:

1. Clients → `corpus-backend` → Credentials → **Regenerate**, and put the value in
   `KEYCLOAK_CLIENT_SECRET`.
2. Users → `admin@corpus.local` → Credentials → set a **non-temporary** password, and put it in
   `KEYCLOAK_LIVE_ADMIN_PASSWORD`.

**Never set a required action, and never set a temporary password.** Login is backend-mediated ROPC
— there is no browser in the flow, so Keycloak cannot ask the user for anything, and any required
action locks the account out permanently with only `400 "Account is not fully set up"` to show for
it. Full realm details: [`deployment/keycloak/README.md`](../deployment/keycloak/README.md).

## 4. First run, in order

Each step depends on the ones above it.

```bash
# 1. Database — once. CREATE DATABASE has no IF NOT EXISTS, so create it directly.
#    If these are not on PATH, see §1 — on Windows they live in
#    C:\Program Files\PostgreSQL\<major>\bin
createdb -h localhost -U postgres corpus
psql -h localhost -U postgres -d corpus -f deployment/bootstrap_db.sql   # CREATE EXTENSION vector

# 2. Dependencies
cd backend && uv sync
cd ../frontend && npm install
npx playwright install chromium        # only if you will run `npm run e2e`

# 3. Configuration
cp backend/.env.example backend/.env
#    Then set, at minimum:
#      OPENAI_API_KEY               — or embeddings and chat both refuse to start
#      DATABASE_URL                 — your Postgres password
#      KEYCLOAK_CLIENT_SECRET       — from §3; also signs cloud-link state
#      KEYCLOAK_LIVE_ADMIN_PASSWORD — from §3; the live tests and e2e need it
#      KEYCLOAK_SERVER_URL          — 8081 if you followed §3
#      MINIO_ENDPOINT               — 9100 if you start the profile below

# 4. Infrastructure containers, from the repo root
MINIO_API_PORT=9100 MINIO_CONSOLE_PORT=9101 \
  docker compose -f deployment/docker-compose.yml --profile minio --profile clamav up -d
docker exec deployment-clamav-1 clamdscan --ping 1     # PONG — minutes on first start

# 5. Schema and the checkpointer — BOTH, in this order
cd backend
uv run alembic upgrade head
uv run python -m app.services.checkpointer      # LangGraph's tables; Alembic does not own them

# 6. The object-storage bucket. Create `corpus` at http://localhost:9101 (minioadmin/minioadmin),
#    or accept that /health/ready reports 503 until the first upload creates it.

# 7. Three processes, three terminals
cd backend  && uv run uvicorn app.main:app --loop app.runtime:selector_loop
cd backend  && uv run arq workers.main.WorkerSettings
cd frontend && npm run dev -- --port 5173 --strictPort

# 8. Verify — through Vite, never :8000
curl -s localhost:5173/health/ready          # database, broker, object_storage
curl -s localhost:5173/health/ready/worker   # + worker heartbeat + clamav
```

Then open **http://localhost:5173** and sign in as `admin@corpus.local`.

**Do not add `--wait` to step 4.** The development ClamAV reports *unhealthy* forever — its stock
probe pings `localhost`, which resolves to `::1`, while clamd binds `0.0.0.0`. The container is
fine; only the probe is wrong. `clamdscan --ping 1` is the real check.

## 5. Traps

Everything here fails *quietly*. That is why it is a list.

| Trap | What you see |
|---|---|
| **Missing `--loop app.runtime:selector_loop` on Windows** | The app boots, `/health/ready` is green, and the **first chat turn** raises `CheckpointerConfigError`. psycopg's async driver needs `add_reader`, which the default `ProactorEventLoop` lacks. `--reload` works by accident, because it runs uvicorn in a subprocess |
| **Checkpointer bootstrap skipped** | First message raises `CheckpointerNotProvisionedError` — which names the command, rather than a bare `UndefinedTable` |
| **arq worker not running** | Upload returns `202` and the document sits at `Queued` forever. Nothing errors anywhere |
| **Bucket never created** | `/health/ready` returns `503 object_storage` on a fresh MinIO volume until someone uploads. `STORAGE_AUTO_CREATE_BUCKET` fires on *use*, and the probe's `HeadBucket` is not a use |
| **`MINIO_ENDPOINT` not matching the published port** | Storage probe fails with a connection error — looks exactly like MinIO being down |
| **Browsing `http://localhost:8000`** | The backend ships no CORS middleware **by design**; failures look like auth bugs. Vite proxies `/api` and `/health`, which is what reproduces production's single origin |
| **`SSE_STALL_AFTER_SECONDS=`** (empty, not commented out) | The app will not boot: "Input should be a valid number". It must be **absent** |
| **Port 8000 already taken** | uvicorn cannot bind. On some machines an unrelated project's container holds it |
| **A Keycloak required action or temporary password** | The account is bricked permanently; the token endpoint says only `400 invalid_grant` |
| **`REDIS_URL` pointing at `localhost` when the broker is IPv4-only** | Everything that touches the queue times out while `redis-cli`/`redis-py` connect fine. `localhost` resolves to `::1` **before** `127.0.0.1`, and arq connects with a 1-second timeout and no IPv4 fallback, so it retries `::1` five times and gives up. Memurai (the usual native Redis on Windows) binds `127.0.0.1` only. **Use `redis://127.0.0.1:6379/0`.** Verified on this box: the arq round-trip test skips itself with *"Redis not reachable"* under `localhost` and passes in 1.6 s under `127.0.0.1`. Same class as the ClamAV `localhost`→`::1` probe in DEPLOYMENT.md §12 |
| **`OCR_LIVE_TEST` unset** | Eight recognition tests skip rather than fail, so a broken sidecar looks like a clean run. Set it (and start the `ocr` profile) for a genuinely 0-skipped suite |
| **ClamAV first start** | ~1 GB of signatures downloads before clamd accepts anything. Ingestion fails *closed* and retries meanwhile |

## 6. Running the suites

See [TESTING.md](TESTING.md) for what each suite covers and what is deliberately not tested by hand.
The dependency note that matters here: **a backend run is only meaningful at 0 skipped**, and how
many skip depends on what is running — 4 with MinIO down, 8 without `OCR_LIVE_TEST=1` and the OCR
sidecar, about 25 with Keycloak down.

## 7. Resetting

Containers, keeping data:

```bash
docker compose -f deployment/docker-compose.yml --profile minio --profile clamav --profile ocr down
```

Add `-v` to discard the volumes as well — MinIO objects included.

The **native** halves have no `down`:

- **Database:** `dropdb corpus && createdb corpus`, then re-run step 1's extension and step 5.
- **Keycloak:** do *not* re-import the realm over a populated Corpus database. The artifact declares
  no user id, so a re-import mints a fresh subject while `users.email` is unique — which strands the
  administrator. Recreate both together, or fix it in the database.

## 8. Configuration

Which settings actually matter, what breaks when they are wrong, and the three surfaces a value has
to cross to reach a running container: [CONFIGURATION.md](CONFIGURATION.md).
