# Corpus dev infrastructure (T-005)

Local development services for the Corpus backend. By default compose starts
**Redis** (arq broker) only. The developer **reuses existing local services** for
the rest: a local pgvector-enabled Postgres (dedicated `corpus` database) and a
MinIO already listening on `:9000/:9001` (Corpus uses its own `corpus` bucket).
Profile-gated MinIO and Postgres services are here for anyone without a local one.

Dev credentials below are defaults that match `backend/.env(.example)`. Override
via the environment for anything shared.

## Services

| Service | Default | Purpose |
|---|---|---|
| Redis | `redis://localhost:6379/0` | arq background-job broker (started by default) |
| MinIO API | `localhost:9000` | S3 object storage — reuse existing, or `--profile minio` |
| MinIO console | http://localhost:9001 | Web UI — login `minioadmin` / `minioadmin` |
| Postgres *(profile)* | `localhost:5432` | Local instance, or `--profile postgres`; DB `corpus` |
| ClamAV *(profile)* | `localhost:3310` | Malware screening at the head of the ingestion worker (R-32); `--profile clamav` |

## Bring services up / down

All commands are run from the **repo root**.

```bash
# Redis only (the default dev stack — reuse your existing MinIO + local Postgres)
docker compose -f deployment/docker-compose.yml up -d

# ...also a containerized MinIO (only if you have no MinIO on :9000)
docker compose -f deployment/docker-compose.yml --profile minio up -d

# ...on different ports, when something else already holds 9000/9001. Point
# backend/.env's MINIO_ENDPOINT at the same port you publish here.
MINIO_API_PORT=9100 MINIO_CONSOLE_PORT=9101 \
  docker compose -f deployment/docker-compose.yml --profile minio up -d minio

# ...also a containerized pgvector (only if you have no local Postgres)
docker compose -f deployment/docker-compose.yml --profile postgres up -d

# ...also ClamAV, required by the ingestion worker unless SCANNER_BACKEND=structural.
# First start downloads ~1 GB of signatures and can take several minutes before clamd
# accepts connections; the named volume means later starts skip the download.
docker compose -f deployment/docker-compose.yml --profile clamav up -d clamav

# status / logs
docker compose -f deployment/docker-compose.yml ps
docker compose -f deployment/docker-compose.yml logs -f

# stop (keeps volumes) / stop + wipe data
docker compose -f deployment/docker-compose.yml down
docker compose -f deployment/docker-compose.yml down -v
```

Quick checks:

```bash
redis-cli -u redis://localhost:6379/0 ping          # -> PONG
# MinIO: open http://localhost:9001 and sign in with minioadmin / minioadmin

# ClamAV — ask the daemon itself, not `docker ps`:
docker exec deployment-clamav-1 clamdscan --ping 1  # -> PONG
docker exec deployment-clamav-1 clamdscan --version # -> ClamAV <ver>/<sigs>/<date>
```

> **The `clamav` container reports `unhealthy` even when clamd is working.** The
> image's `clamdcheck.sh` runs `echo PING | nc localhost 3310`; `/etc/hosts` resolves
> `localhost` to `::1` first, and `clamd.conf` here pins `TCPAddr 0.0.0.0`, so the
> probe talks to an address clamd does not listen on. `nc 127.0.0.1 3310` answers
> `PONG` and so does the application's own client, which connects by address rather
> than name — the daemon is fine and ingestion screens normally. Verify with the
> `clamdscan --ping` command above rather than the container's health column.
> Nothing in compose gates on that healthcheck, but an orchestrator that restarts on
> it would loop, so override the healthcheck (or drop `TCPAddr`) before deploying.

## Database bootstrap (one-time)

**If you use a local Postgres** (the default for this project), create the
database and enable pgvector once. `CREATE DATABASE` cannot be `IF NOT EXISTS` in
plain SQL, so create it directly (skip if it already exists — this repo's dev DB
is already bootstrapped):

```bash
# create the database (choose one)
createdb -h localhost -U postgres corpus
#   or:  psql -h localhost -U postgres -c "CREATE DATABASE corpus;"

# enable the pgvector extension inside it
psql -h localhost -U postgres -d corpus -f deployment/bootstrap_db.sql
```

**If you use the compose Postgres profile**, the database is created from
`POSTGRES_DB` and `initdb/01-extension.sql` enables pgvector automatically on
first start — no manual step.

Schema and indexes are created later by Alembic (T-101):
`cd backend && uv run alembic upgrade head`.

## Configuration

Backend settings read from `backend/.env` (see `backend/.env.example` for the full
template). Copy and fill in secrets:

```bash
cp backend/.env.example backend/.env   # then set OPENAI_API_KEY, DB password, etc.
```

The `pydantic-settings` config module (`backend/app/config.py`) exposes
`database`, `minio`, `redis`, `openai`, and `keycloak` groups; defaults match the
services above so the app boots without a `.env`.
