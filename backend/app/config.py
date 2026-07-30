"""Application settings (T-005).

Configuration groups (database, OpenAI, MinIO/S3, Redis/arq) plus the Keycloak
auth settings introduced by ruling R-28 (Rev 0.6.2). Each group is its own
`BaseSettings` with an `env_prefix` and is composed onto the top-level `Settings`.
Values come from the environment / a local `.env`; defaults align with the
`deployment/docker-compose.yml` services so the app boots without a `.env`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection. `url` binds `DATABASE_URL` (async asyncpg DSN).

    Points at the developer's local pgvector-enabled `corpus` database; the schema
    and indexes are owned by Alembic (T-101).

    Two drivers read this one setting: SQLAlchemy over **asyncpg** (everything in
    `app/db/`) and **psycopg3** under the LangGraph checkpointer (T-301, FR-PER-01),
    which cannot parse SQLAlchemy's `+driver` scheme. Hence :attr:`psycopg_dsn` —
    derived, never configured separately.
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_", env_file=".env", extra="ignore")

    url: str = Field(default="postgresql+asyncpg://postgres:postgres@localhost:5432/corpus")

    @computed_field
    @property
    def psycopg_dsn(self) -> str:
        """`url` as a psycopg3 conninfo URI (R-42(8)).

        A **scheme-only** rewrite: everything after the scheme is passed through
        untouched, because the credentials in `url` are already percent-encoded and
        re-encoding them is the one way this could corrupt a password.

        Derived rather than a second `CHECKPOINTER_DSN` setting on purpose: two
        independently-set DSNs can point at different databases, and nothing would
        notice until a resume silently found no checkpoint for a live conversation.
        """
        return urlunsplit(urlsplit(self.url)._replace(scheme="postgresql"))

    @model_validator(mode="after")
    def _coherent(self) -> DatabaseSettings:
        if not self.url.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL — pgvector is the R-17 storage "
                "baseline and FR-PER-01 pins the LangGraph AsyncPostgresSaver to the "
                "same database"
            )
        return self


class MinioSettings(BaseSettings):
    """MinIO / S3 connection details — originals + artifacts (R-19, FR-ING-02).

    Defaults match the compose `minio` service. Consumed by the S3 backend of the
    object-storage service (T-201, `app.services.object_storage`); which backend is
    actually used is :class:`StorageSettings.backend`.
    """

    model_config = SettingsConfigDict(env_prefix="MINIO_", env_file=".env", extra="ignore")

    endpoint: str = Field(default="localhost:9000")
    access_key: str = Field(default="minioadmin")
    secret_key: str = Field(default="minioadmin")
    bucket: str = Field(default="corpus")
    secure: bool = Field(default=False)
    # Signature region. MinIO ignores it, but botocore requires *some* region to sign.
    region: str = Field(default="us-east-1")


class StorageSettings(BaseSettings):
    """Object-storage backend selection (T-201, R-19).

    R-19 adopts MinIO/S3 for originals and derived artifacts and explicitly permits a
    filesystem backend for local development, so `backend` chooses between the two;
    both satisfy the same `ObjectStorage` protocol, so nothing above the service
    changes. `local_root` is only read by the `local` backend.
    """

    model_config = SettingsConfigDict(env_prefix="STORAGE_", env_file=".env", extra="ignore")

    backend: Literal["s3", "local"] = Field(default="s3")
    local_root: str = Field(default=".data/objects")
    # Create the bucket on first use if it is missing. Convenient for dev/CI; deployments
    # that provision the bucket out of band (and grant no CreateBucket permission) can
    # turn it off.
    auto_create_bucket: bool = Field(default=True)
    # Wall-clock ceiling for a single object-storage operation.
    timeout_seconds: float = Field(default=30.0)  # TBD(§8.4)


class UploadSettings(BaseSettings):
    """Upload limits and validation knobs (T-202, FR-ERR-01/02/03, R-31).

    Deliberately separate from :class:`StorageSettings`: that group selects the
    storage *backend* (infrastructure), while these are *product policy* enforced by
    the upload API — and T-203's parser decompression caps will want the same home.

    ``max_file_bytes`` and ``user_quota_bytes`` are **spec-normative** (FR-ERR-01 /
    FR-ERR-02, fixed by R-11); only the rejection *copy* is a §8.4 TBD. The tuning
    values below are provisional.
    """

    model_config = SettingsConfigDict(env_prefix="UPLOAD_", env_file=".env", extra="ignore")

    max_file_bytes: int = Field(default=50 * 1024 * 1024)  # FR-ERR-01 — 50 MB, normative
    user_quota_bytes: int = Field(default=10 * 1024 * 1024 * 1024)  # FR-ERR-02 — 10 GB, normative
    enforce_quota: bool = Field(default=True)
    read_chunk_bytes: int = Field(default=1024 * 1024)  # TBD(§8.4)
    # Bytes of the leading chunk handed to the magic-byte sniffer (R-31).
    sniff_head_bytes: int = Field(default=8192)  # TBD(§8.4)
    # Ceiling on uploads buffered in this process at once. `ObjectStorage.put`
    # materialises the body, so this bounds peak RAM at roughly
    # max_file_bytes * max_concurrent.
    max_concurrent: int = Field(default=8)  # TBD(§8.4)


class ParserSettings(BaseSettings):
    """Parser limits — the R-31(3) compensating controls (T-203, FR-KBM-05).

    Distinct from :class:`UploadSettings` on purpose. Those are enforced by the API on
    the way in and bound what a *user* may send; these are enforced by the **worker**
    on content already accepted, and bound what a parser may *expand* a 50 MB original
    into. Naming them ``UPLOAD_*`` would misfile a worker-side control.

    R-31(3) makes the DOCX caps mandatory: a ZIP whose members expand to gigabytes is a
    denial-of-service vector that the magic-byte sniffer cannot see, because a zip bomb
    is a structurally valid OOXML package. A breach **rejects** the document
    (`CONTENT_LIMIT_EXCEEDED` → FR-ING-01 `FAILED`); it never truncates, because a
    silently half-ingested document makes retrieval answer "not in your documents" about
    text the user did upload.

    Every value is provisional.
    """

    model_config = SettingsConfigDict(env_prefix="PARSER_", env_file=".env", extra="ignore")

    # Whole-document ceilings, applied to every format.
    max_pages: int = Field(default=5_000)  # TBD(§8.4)
    max_extracted_chars: int = Field(default=20_000_000)  # TBD(§8.4)
    max_block_chars: int = Field(default=200_000)  # TBD(§8.4)

    # DOCX zip-container caps (R-31(3)), checked against the central directory before
    # a single member is decompressed.
    docx_max_expanded_bytes: int = Field(default=400 * 1024 * 1024)  # TBD(§8.4)
    docx_max_compression_ratio: float = Field(default=200.0)  # TBD(§8.4)
    docx_max_members: int = Field(default=5_000)  # TBD(§8.4)

    # CSV shape caps + the row-grouping factor that decides locator granularity.
    csv_max_rows: int = Field(default=200_000)  # TBD(§8.4)
    csv_max_columns: int = Field(default=1_000)  # TBD(§8.4)
    csv_rows_per_block: int = Field(default=50)  # TBD(§8.4)


#: Ceiling on the characters in one embedding input. `text-embedding-3-*` accepts
#: 8191 tokens; charged pessimistically at one token per character, because CJK text
#: really does tokenise near 1:1 and a chunk that overruns is a hard 400 from the API
#: mid-ingestion. Guards :class:`ChunkerSettings` rather than being enforced per call.
EMBEDDING_MAX_INPUT_CHARS = 8_000  # TBD(§8.4)


class ChunkerSettings(BaseSettings):
    """Chunk sizing — the FR-ING-03 chunking strategy (T-204, ruling R-35).

    Separate from :class:`ParserSettings` on purpose. Those are *rejection* limits on
    hostile input; these are *retrieval-quality* knobs, and nothing here ever fails a
    document.

    Every value is also a fingerprint input: they compose into `chunking_version`
    (`app.ingestion.chunker.effective_chunking_version`), so changing one re-embeds
    the affected corpus on next ingestion. That is FR-ING-03 working as designed —
    a boundary change really does invalidate the vectors — but it is not free, so
    tune deliberately.
    """

    model_config = SettingsConfigDict(env_prefix="CHUNKER_", env_file=".env", extra="ignore")

    target_chars: int = Field(default=2_000)  # TBD(§8.4) — ~500 English tokens
    overlap_chars: int = Field(default=200)  # TBD(§8.4) — 10% of target
    min_chars: int = Field(default=200)  # TBD(§8.4) — orphan-tail merge threshold
    # Fraction of the target a chunk must reach before a coarse separator is accepted.
    # Without it, a blank line just past the cursor yields a 250-char chunk even when a
    # sentence boundary sits at 1,990.
    boundary_floor_ratio: float = Field(default=0.5)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> ChunkerSettings:
        if not 0 <= self.overlap_chars < self.target_chars:
            # Not merely a poor setting: an overlap at or above the target makes the
            # splitter's cursor stop advancing, i.e. an infinite loop in the worker.
            raise ValueError("CHUNKER_OVERLAP_CHARS must be >= 0 and < CHUNKER_TARGET_CHARS")
        if not 0 <= self.min_chars <= self.target_chars:
            raise ValueError("CHUNKER_MIN_CHARS must be >= 0 and <= CHUNKER_TARGET_CHARS")
        if not 0.0 < self.boundary_floor_ratio <= 1.0:
            raise ValueError("CHUNKER_BOUNDARY_FLOOR_RATIO must be in (0, 1]")
        # Tail absorption lets the last chunk of a block reach target + min.
        if self.target_chars + self.min_chars > EMBEDDING_MAX_INPUT_CHARS:
            raise ValueError(
                "CHUNKER_TARGET_CHARS + CHUNKER_MIN_CHARS must not exceed "
                f"{EMBEDDING_MAX_INPUT_CHARS:,} characters"
            )
        return self


class QueueSettings(BaseSettings):
    """Background-job queue selection (T-202 seam; the worker itself is T-207).

    ``backend="none"`` selects the no-op queue for dev/CI so the API runs without
    Redis — the same spirit as R-19's sanctioned filesystem object-storage backend.
    """

    model_config = SettingsConfigDict(env_prefix="QUEUE_", env_file=".env", extra="ignore")

    backend: Literal["arq", "none"] = Field(default="arq")
    enqueue_timeout_seconds: float = Field(default=5.0)  # TBD(§8.4)


class RedisSettings(BaseSettings):
    """Redis broker for arq background jobs (R-18). `url` binds `REDIS_URL`.

    Named distinctly from arq's own `arq.connections.RedisSettings`, which the
    worker builds from this URL (T-207).
    """

    model_config = SettingsConfigDict(env_prefix="REDIS_", env_file=".env", extra="ignore")

    url: str = Field(default="redis://localhost:6379/0")


class WorkerSettings(BaseSettings):
    """How the arq ingestion worker runs (T-207, FR-ING-04, NFR-REL-01).

    Named for the process, not the broker: :class:`QueueSettings` selects *how the API
    dispatches*, :class:`RedisSettings` is *where*, and these are the worker's own
    execution policy. `workers.main.WorkerSettings` (arq's class, same name, different
    module) reads every value here — nothing else does.

    **`max_tries` is load-bearing beyond arq's own use of it.** arq checks it at the top
    of `run_job` and, once exceeded, finishes the job *without invoking the task at all*
    (`arq/worker.py`), so the task can never observe its own dead-letter. `workers.ingest`
    therefore reads this same value to detect the final attempt and write
    `JobStatus.DEAD_LETTER` itself — the two must not drift, which is why there is one
    setting and not an arq knob plus a task constant.

    ``heartbeat_seconds`` is likewise not cosmetic: arq's `record_health` writes the
    health-check key with a TTL of ``interval + 1``, and that TTL *is* the
    `/health/ready/worker` signal (R-38(2)). arq's own default is 3600 s, which would make
    the probe useless.
    """

    model_config = SettingsConfigDict(env_prefix="WORKER_", env_file=".env", extra="ignore")

    max_tries: int = Field(default=5)  # TBD(§8.4)
    # Wall-clock ceiling for one ingestion attempt. Generous because a 50 MB PDF's parse
    # and a multi-batch embedding run are both minutes-scale; note that the parser and
    # chunker run in `asyncio.to_thread`, which **cannot be cancelled**, so this bounds
    # the worker's bookkeeping, not the thread (see `app.ingestion.parsers`).
    job_timeout_seconds: float = Field(default=900.0)  # TBD(§8.4)
    max_jobs: int = Field(default=4)  # TBD(§8.4)

    # Retry backoff: min(base * 2**(job_try - 1), max), jittered. arq does not back off on
    # its own — a plain exception is not even retried — so `workers.ingest` computes this
    # and raises `arq.worker.Retry(defer=...)`.
    retry_base_seconds: float = Field(default=10.0)  # TBD(§8.4)
    retry_max_seconds: float = Field(default=600.0)  # TBD(§8.4)

    heartbeat_seconds: float = Field(default=30.0)  # TBD(§8.4)

    # ENQUEUE_FAILED sweeper (T-202 commits the job row and returns 202 even when the
    # broker is down). `min_age` keeps the sweeper off jobs a live enqueue is still
    # racing to dispatch.
    sweep_interval_seconds: float = Field(default=300.0)  # TBD(§8.4)
    sweep_min_age_seconds: float = Field(default=120.0)  # TBD(§8.4)
    sweep_batch_size: int = Field(default=100)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> WorkerSettings:
        if self.max_tries < 1:
            raise ValueError("WORKER_MAX_TRIES must be >= 1")
        if self.max_jobs < 1:
            raise ValueError("WORKER_MAX_JOBS must be >= 1")
        if self.job_timeout_seconds <= 0:
            raise ValueError("WORKER_JOB_TIMEOUT_SECONDS must be > 0")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("WORKER_RETRY_MAX_SECONDS must be >= WORKER_RETRY_BASE_SECONDS > 0")
        if self.heartbeat_seconds <= 0:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be > 0")
        if self.sweep_min_age_seconds < 0 or self.sweep_interval_seconds <= 0:
            raise ValueError("WORKER_SWEEP_* seconds must be positive")
        return self


class SseSettings(BaseSettings):
    """The live document-status channel (T-210, FR-KBM-09, R-41 §8.22).

    Under R-41(2) the channel learns of changes by **polling**, so every one of these is a
    load knob rather than a preference: an open stream costs one indexed query every
    ``poll_interval_seconds``, and the floor this surface adds to database load is
    ``streams x users / interval``. Shortening the interval to make the FR-KBM-04 badge
    feel livelier raises that floor for every connected user at once — measure first.

    ``stall_after_seconds`` defaults to ``None``, meaning *derive it* from
    :attr:`WorkerSettings.job_timeout_seconds` (see :meth:`Settings.stall_after`). It is
    deliberately not a free number: arq's ``arq:in-progress:{job_id}`` guard has a TTL of
    ``job_timeout + 10`` and no worker re-picks a job while that key lives, so a threshold
    below it would mark a *healthy* long ingestion as stalled — which is the same knob's
    third duty, on top of the two §8.4 already records for it.
    """

    model_config = SettingsConfigDict(env_prefix="SSE_", env_file=".env", extra="ignore")

    poll_interval_seconds: float = Field(default=1.5)  # TBD(§8.4)
    # Keepalive. `EventSourceResponse` sends a comment frame on this interval so idle
    # proxies do not sever a stream that is simply watching an idle knowledge base.
    ping_seconds: float = Field(default=15.0)  # TBD(§8.4)
    # None = derive from WORKER_JOB_TIMEOUT_SECONDS; see the class docstring.
    stall_after_seconds: float | None = Field(default=None)  # TBD(§8.4)
    max_streams_per_user: int = Field(default=4)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> SseSettings:
        if self.poll_interval_seconds <= 0:
            raise ValueError("SSE_POLL_INTERVAL_SECONDS must be > 0")
        if self.ping_seconds <= 0:
            raise ValueError("SSE_PING_SECONDS must be > 0")
        if self.stall_after_seconds is not None and self.stall_after_seconds <= 0:
            raise ValueError("SSE_STALL_AFTER_SECONDS must be > 0 when set")
        if self.max_streams_per_user < 1:
            raise ValueError("SSE_MAX_STREAMS_PER_USER must be >= 1")
        return self


class ScannerSettings(BaseSettings):
    """Malware-screening backend selection (T-207, R-31 §8.12 / R-32 §8.13).

    ``backend="structural"`` disables only the **signature** screen — the in-process
    structural checks (disguised DOCM, PDF active content) always run. That asymmetry is
    deliberate (R-38(7)): a dev box without a 2 GB `clamd` still gets the cheap screens,
    and there is no setting anywhere that turns *all* screening off.

    Note this is not the fail-open escape hatch R-32 forbids. Selecting `structural` is an
    explicit, logged deployment choice; an *unreachable* `clamd` under `backend="clamav"`
    still fails the job closed.
    """

    model_config = SettingsConfigDict(env_prefix="SCANNER_", env_file=".env", extra="ignore")

    backend: Literal["clamav", "structural"] = Field(default="clamav")


class ClamAVSettings(BaseSettings):
    """`clamd` connection for the INSTREAM signature screen (T-207, R-32).

    R-32 selects ClamAV reached by a small in-tree async client rather than the sync,
    unmaintained `clamd`/`pyclamd` PyPI packages — so there is no SDK to configure here,
    only the socket and the framing.

    ``max_stream_bytes`` is a **safety control, not a tuning knob** (R-38(6)). R-32 makes
    `StreamMaxLength > 50 MB` normative because clamd's 25 MB default *fails open*: it
    truncates the stream and reports the truncated prefix clean. No INSTREAM command
    reports the daemon's configured limit, so a misconfigured `clamd` is undetectable at
    runtime. The client therefore refuses to stream a payload above this value rather than
    risk a silent truncation, and :class:`Settings` rejects a value below
    ``UPLOAD_MAX_FILE_BYTES`` at boot. Keep it in step with `deployment/clamav/clamd.conf`.
    """

    model_config = SettingsConfigDict(env_prefix="CLAMAV_", env_file=".env", extra="ignore")

    host: str = Field(default="localhost")
    port: int = Field(default=3310)
    # A cold clamd can take a while on a large archive; this is the whole-scan budget.
    timeout_seconds: float = Field(default=120.0)  # TBD(§8.4)
    # Readiness probes must fail fast — they are not scanning anything.
    ping_timeout_seconds: float = Field(default=2.0)  # TBD(§8.4)
    # INSTREAM chunk size. clamd rejects any single chunk above its own limit; 64 KiB is
    # the size the reference client uses.
    chunk_bytes: int = Field(default=64 * 1024)
    max_stream_bytes: int = Field(default=100 * 1024 * 1024)  # must exceed FR-ERR-01's 50 MB

    @model_validator(mode="after")
    def _coherent(self) -> ClamAVSettings:
        if self.chunk_bytes < 1024:
            raise ValueError("CLAMAV_CHUNK_BYTES must be >= 1024")
        if self.timeout_seconds <= 0 or self.ping_timeout_seconds <= 0:
            raise ValueError("CLAMAV_*_TIMEOUT_SECONDS must be > 0")
        return self


class RateLimitSettings(BaseSettings):
    """slowapi rate limiting — auth/chat/upload throttle (NFR-SEC-07, T-105).

    ``storage_uri`` is a `limits` storage string. Production points at Redis so the
    counters are shared across worker processes; slowapi 0.1.10 hits the storage
    *synchronously* even from async routes, so this must be the **sync** ``redis://``
    scheme (backed by the already-present ``redis`` client) — ``async+redis://`` is
    not driven by slowapi and would break. A distinct DB index (``/1``) keeps the
    keys clear of arq's broker on ``/0`` (see :class:`RedisSettings`). Tests set
    ``memory://`` so the suite needs no Redis.

    The per-target limit strings are provisional pending §8.4; ``chat``/``upload``
    are staged for T-402/T-202 (the endpoints do not exist yet).
    """

    model_config = SettingsConfigDict(env_prefix="RATELIMIT_", env_file=".env", extra="ignore")

    enabled: bool = Field(default=True)
    storage_uri: str = Field(default="redis://localhost:6379/1")
    # Provisional thresholds — # TBD(§8.4). Login/refresh keyed per-IP;
    # change_password/chat/upload keyed per-user (principal.sub).
    login: str = Field(default="10/minute")
    refresh: str = Field(default="30/minute")
    change_password: str = Field(default="5/minute")
    chat: str = Field(default="20/minute")
    upload: str = Field(default="20/minute")


class OpenAISettings(BaseSettings):
    """OpenAI models — chat, embeddings, DeepEval judge (R-15).

    The exact production chat model id is configurable (FR-SYS-03); the value here
    is a development default.
    """

    model_config = SettingsConfigDict(env_prefix="OPENAI_", env_file=".env", extra="ignore")

    api_key: str = Field(default="")
    chat_model: str = Field(default="gpt-4o")
    # The FR-RET-03 router (T-304, R-45(1)). Deliberately *not* `chat_model`: the router
    # emits a handful of tokens of JSON on the critical path before retrieval, so it wants
    # the cheapest, fastest model that can follow a schema, while `chat_model` is chosen for
    # answer quality. One knob per job also means tuning either cannot silently move the
    # other's cost. # TBD(§8.4)
    router_model: str = Field(default="gpt-4o-mini")
    # text-embedding-3-large (3072-dim) matches the `document_chunks.embedding`
    # VECTOR(3072) column (app.db.base.EMBEDDING_DIM). # TBD(§8.4)
    embedding_model: str = Field(default="text-embedding-3-large")


class EmbeddingSettings(BaseSettings):
    """How the worker *drives* the embeddings endpoint (T-205, FR-ING-03).

    Separate from :class:`OpenAISettings` on purpose, following the same split T-203 made
    between `PARSER_*` and `UPLOAD_*`. Those settings are provider/account facts shared by
    chat, embeddings and the DeepEval judge — the key, and which model ids to call. These
    are batching, concurrency and transport for one endpoint; naming them `OPENAI_*` would
    misfile a worker-throughput knob under the provider account, and the first task that
    needs a chat timeout would collide on `OPENAI_TIMEOUT_SECONDS`.

    ``backend="fake"`` selects the deterministic in-process client for dev/CI, in the
    spirit of R-19's sanctioned filesystem object storage and `QUEUE_BACKEND=none`. It is
    never selected implicitly — see :func:`app.services.embeddings.build_embedding_client`.

    Note `OPENAI_EMBEDDING_MODEL` stays where it is: it is an FR-ING-03 fingerprint input
    read by the chunker, so renaming it would invalidate every stored fingerprint.
    """

    model_config = SettingsConfigDict(env_prefix="EMBEDDING_", env_file=".env", extra="ignore")

    backend: Literal["openai", "fake"] = Field(default="openai")

    # Batch shape. Both ceilings come from the API's own documented limits (a 2048-item
    # array, 300k tokens summed per request). Characters, never `token_count` — R-35(7):
    # that field is a len/4 estimate and an underestimate is a hard 400 mid-ingestion.
    max_batch_size: int = Field(default=128)  # TBD(§8.4)
    max_batch_chars: int = Field(default=200_000)  # TBD(§8.4) — 1 token/char, as §138
    max_concurrent_requests: int = Field(default=4)  # TBD(§8.4)

    # Transport. The SDK's default timeout is 600s — inside an ingestion worker that is a
    # hang, not a timeout. Ingestion can afford to ride out a rate-limit blip; a chat turn
    # (T-206) cannot, hence the separate query budget.
    timeout_seconds: float = Field(default=60.0)  # TBD(§8.4)
    query_timeout_seconds: float = Field(default=15.0)  # TBD(§8.4)
    connect_timeout_seconds: float = Field(default=5.0)  # TBD(§8.4)
    max_retries: int = Field(default=4)  # TBD(§8.4) — SDK default is 2
    query_max_retries: int = Field(default=1)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> EmbeddingSettings:
        if not 1 <= self.max_batch_size <= 2048:
            raise ValueError("EMBEDDING_MAX_BATCH_SIZE must be in 1..2048 (the API array limit)")
        if self.max_batch_chars < EMBEDDING_MAX_INPUT_CHARS:
            # Not merely a poor setting: a legal maximum-size chunk would fit in no batch
            # at all, which is a non-terminating loop in the batch planner — the same class
            # of bug ChunkerSettings guards against for overlap >= target.
            raise ValueError(
                "EMBEDDING_MAX_BATCH_CHARS must be >= "
                f"{EMBEDDING_MAX_INPUT_CHARS:,} (EMBEDDING_MAX_INPUT_CHARS)"
            )
        if self.max_concurrent_requests < 1:
            raise ValueError("EMBEDDING_MAX_CONCURRENT_REQUESTS must be >= 1")
        if self.timeout_seconds <= 0 or self.query_timeout_seconds <= 0:
            raise ValueError("EMBEDDING_TIMEOUT_SECONDS values must be > 0")
        if self.max_retries < 0 or self.query_max_retries < 0:
            raise ValueError("EMBEDDING_MAX_RETRIES values must be >= 0")
        return self


class LlmSettings(BaseSettings):
    """How the app *drives* the chat-completions endpoint (T-304, R-45; T-307 extends).

    The same split as `OPENAI_*` vs `EMBEDDING_*`, for the same reason: which model to call
    is a provider/account fact, while transport and per-call-site budgets are properties of
    the call site. ``router_*`` mirrors `EMBEDDING_QUERY_*` exactly — one endpoint, two
    callers with very different patience, and the one on the chat critical path gets the
    short leash.

    ``backend="fake"`` selects the deterministic in-process client for dev and CI, on the
    `EMBEDDING_BACKEND=fake` / `QUEUE_BACKEND=none` precedent. It is never selected
    implicitly — see :func:`app.services.llm.build_chat_client`.
    """

    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    backend: Literal["openai", "fake"] = Field(default="openai")

    # Generation (T-307). The SDK's default is 600s, which on a request path is a hang.
    timeout_seconds: float = Field(default=90.0)  # TBD(§8.4)
    connect_timeout_seconds: float = Field(default=5.0)  # TBD(§8.4)
    max_retries: int = Field(default=2)  # TBD(§8.4)

    # The FR-RET-03 router. Short and nearly retry-free on purpose: the call sits *before*
    # retrieval on a path NFR-PRF-02 already makes the user wait through, and R-45(2) makes
    # the router fail open — so waiting 90s for a classification is strictly worse than
    # giving up at 8s and retrieving with `hybrid`, which is what an unclassified query gets
    # anyway.
    router_timeout_seconds: float = Field(default=8.0)  # TBD(§8.4)
    router_max_retries: int = Field(default=1)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> LlmSettings:
        if self.timeout_seconds <= 0 or self.router_timeout_seconds <= 0:
            raise ValueError("LLM_*_TIMEOUT_SECONDS values must be > 0")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("LLM_CONNECT_TIMEOUT_SECONDS must be > 0")
        if self.max_retries < 0 or self.router_max_retries < 0:
            raise ValueError("LLM_*_MAX_RETRIES values must be >= 0")
        return self


class RouterSettings(BaseSettings):
    """FR-RET-03 query-adaptive routing policy (T-304, R-45).

    Quality and cost knobs, not limits: nothing here rejects a query, and every failure to
    honour one degrades to `hybrid` rather than to an error (R-45(2)).

    The two probe bounds are what keep `RAGState.sub_queries` inside FR-PER-03's
    "ids and scalars" spirit — they are the reason HyDE's hypothetical passage needs no new
    state field (R-45(3)/(4)) — and they bound the retrieval fan-out T-305 pays for: each
    probe is a second dense **and** sparse query.
    """

    model_config = SettingsConfigDict(env_prefix="ROUTER_", env_file=".env", extra="ignore")

    #: Derived probes *in addition to* the original query, which T-305 always retrieves with
    #: (R-45(3)). 3 means at most 4 probes → 8 arm queries, run concurrently.
    max_sub_queries: int = Field(default=3)  # TBD(§8.4)

    #: Per-probe character ceiling. Sized for a HyDE passage — a decomposed sub-question is
    #: far shorter — and enforced by truncation, never by discarding the probe.
    max_probe_chars: int = Field(default=400)  # TBD(§8.4)

    #: Generous rather than tight: a cap that truncates the JSON mid-object produces exactly
    #: the malformed payload R-45(2) has to throw away, so shaving tokens here buys nothing
    #: and costs whole classifications.
    max_output_tokens: int = Field(default=800)  # TBD(§8.4)

    #: How much conversation tail the router sees (R-45(6)). Enough to resolve "what about
    #: the second one?" without turning a cheap classification into a full-history call —
    #: R-30's untruncated history belongs to the *generator*, not to the router.
    history_turns: int = Field(default=4)  # TBD(§8.4)
    history_max_chars: int = Field(default=600)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> RouterSettings:
        if self.max_sub_queries < 0:
            # 0 is meaningful: classify and record, but never fan out.
            raise ValueError("ROUTER_MAX_SUB_QUERIES must be >= 0")
        if self.max_probe_chars < 1:
            raise ValueError("ROUTER_MAX_PROBE_CHARS must be >= 1")
        if self.max_output_tokens < 1:
            raise ValueError("ROUTER_MAX_OUTPUT_TOKENS must be >= 1")
        if self.history_turns < 0:
            raise ValueError("ROUTER_HISTORY_TURNS must be >= 0")
        if self.history_max_chars < 1:
            raise ValueError("ROUTER_HISTORY_MAX_CHARS must be >= 1")
        return self


class RetrievalSettings(BaseSettings):
    """Hybrid retrieval shape — dense + sparse arms and their fusion (T-206, R-37).

    Retrieval-quality knobs, not limits: nothing here rejects a query. Unlike the
    `CHUNKER_*` settings none of these is an FR-ING-03 fingerprint input, so all of them are
    tunable at zero re-embed cost.

    ``fts_language`` is the one setting here with a hard external coupling: the T-101 GIN
    index is built on the literal expression ``to_tsvector('english', chunk_text)``, and
    Postgres serves a functional index only for a query whose expression matches it. Change
    this without rebuilding that index and the sparse arm silently degrades to a sequential
    scan over every chunk — correct answers, unusable latency. Hence the validator.
    """

    model_config = SettingsConfigDict(env_prefix="RETRIEVAL_", env_file=".env", extra="ignore")

    # Per-arm candidate depth, fetched before fusion.
    dense_candidates: int = Field(default=50)  # TBD(§8.4)
    sparse_candidates: int = Field(default=50)  # TBD(§8.4)

    # RRF damping (R-37(1)). 60 is the value from the original Cormack et al. formulation
    # and the de-facto default; it is large enough that one arm's rank-1 hit does not by
    # itself outrank a chunk both arms returned.
    rrf_k: int = Field(default=60)  # TBD(§8.4)

    # How many fused candidates hybrid search returns. This is the reranker's input, NOT
    # the FR-RET-02 top-K — that one is the count of *reranked* passages that become the
    # grounding context, it belongs to T-306, and it is still an open §8.4 TBD.
    fusion_top_k: int = Field(default=30)  # TBD(§8.4)

    # HNSW search-list size. pgvector's default is 40, so any deeper candidate fetch
    # silently loses recall without raising this — the index simply cannot produce that
    # many neighbours from a list it never grew.
    hnsw_ef_search: int = Field(default=100)  # TBD(§8.4)

    #: Must match the T-101 index expression verbatim — see the class docstring.
    fts_language: str = Field(default="english")

    #: R-35(5) / R-37(6) adjacent-overlap dedupe. A kill switch for diagnosis, not a taste.
    dedupe_adjacent: bool = Field(default=True)

    @model_validator(mode="after")
    def _coherent(self) -> RetrievalSettings:
        if self.dense_candidates < 1 or self.sparse_candidates < 1:
            raise ValueError("RETRIEVAL_*_CANDIDATES must be >= 1")
        if self.rrf_k < 1:
            raise ValueError("RETRIEVAL_RRF_K must be >= 1")
        if self.fusion_top_k < 1:
            raise ValueError("RETRIEVAL_FUSION_TOP_K must be >= 1")
        if self.hnsw_ef_search < self.dense_candidates:
            # Not a preference: HNSW cannot return more rows than its search list holds, so
            # this configuration asks for candidates the index will never produce.
            raise ValueError(
                "RETRIEVAL_HNSW_EF_SEARCH must be >= RETRIEVAL_DENSE_CANDIDATES "
                "(HNSW cannot return more neighbours than its search list)"
            )
        if self.fts_language != "english":
            raise ValueError(
                "RETRIEVAL_FTS_LANGUAGE must be 'english' to match the "
                "ix_document_chunks_chunk_text_fts index expression; changing it requires "
                "a migration that rebuilds that index"
            )
        return self


class CheckpointerSettings(BaseSettings):
    """LangGraph checkpointer — the FR-PER-01 execution-state store (T-301, R-42).

    Infrastructure, not orchestration policy: everything here is about the psycopg
    connection pool that persists graph state. The policy knobs (retry bound,
    durability, timeouts) are :class:`GraphSettings` — the same split as
    `OPENAI_`/`EMBEDDING_` and `PARSER_`/`CHUNKER_`.

    ``backend`` is explicit and is never inferred from a missing credential (the
    `EMBEDDING_BACKEND` rule). `memory` is dev/CI only and is refused outright when
    `ENVIRONMENT=production` — see :meth:`Settings._coherent`, the enforceable form of
    FR-PER-01's "never `InMemorySaver` in production".
    """

    model_config = SettingsConfigDict(env_prefix="CHECKPOINTER_", env_file=".env", extra="ignore")

    backend: Literal["postgres", "memory"] = Field(default="postgres")

    # Pool sizing. Separate from the SQLAlchemy engine's pool by construction: this is a
    # psycopg pool and that one is asyncpg, so they cannot be shared even in principle.
    pool_min_size: int = Field(default=1)  # TBD(§8.4)
    pool_max_size: int = Field(default=8)  # TBD(§8.4)
    pool_timeout_seconds: float = Field(default=10.0)  # TBD(§8.4)
    connect_timeout_seconds: int = Field(default=5)  # TBD(§8.4) — psycopg wants whole seconds

    # langgraph's own `LANGGRAPH_STRICT_MSGPACK` defaults to *false*, and its docstring
    # spells out the consequence: "any Python callable stored in checkpoint data will be
    # imported and executed on load". R-42(2) makes `RAGState` scalars and lists of
    # scalars, so strict mode costs nothing and closes a deserialization-RCE surface on a
    # table that anyone with database write access could poison.
    strict_msgpack: bool = Field(default=True)

    @model_validator(mode="after")
    def _coherent(self) -> CheckpointerSettings:
        if self.pool_min_size < 0:
            raise ValueError("CHECKPOINTER_POOL_MIN_SIZE must be >= 0")
        if self.pool_max_size < 1:
            raise ValueError("CHECKPOINTER_POOL_MAX_SIZE must be >= 1")
        if self.pool_max_size < self.pool_min_size:
            raise ValueError("CHECKPOINTER_POOL_MAX_SIZE must be >= CHECKPOINTER_POOL_MIN_SIZE")
        if self.pool_timeout_seconds <= 0 or self.connect_timeout_seconds <= 0:
            raise ValueError("CHECKPOINTER_*_TIMEOUT_SECONDS must be > 0")
        return self


class GraphSettings(BaseSettings):
    """Orchestration policy for the FR-ORC-01/07 workflow (T-301, R-42).

    Nothing here opens a socket — that is :class:`CheckpointerSettings`.
    """

    model_config = SettingsConfigDict(env_prefix="GRAPH_", env_file=".env", extra="ignore")

    # FR-ORC-07 requires `retry_count` to be bounded "to guarantee termination" and never
    # says by what. 1 means at most two full retrieve→rerank→generate→gate cycles: each
    # retry costs a second rerank *and* a second generation, and NFR-PRF-02 already makes
    # the user wait for the whole gate before a single token appears, so a second cycle
    # roughly doubles time-to-first-token for a query that has already failed groundedness.
    # Abstaining (FR-RET-05) is cheaper and more honest. Settle together with the FR-RET-05
    # groundedness threshold — they trade off directly (see §8.4).
    max_retries: int = Field(default=1)  # TBD(§8.4)

    # NOT langgraph's default, which is "async": there a checkpoint is persisted *while*
    # the next step runs, so a process killed mid-turn can lose the last superstep — which
    # is precisely the guarantee FR-PER-01 ("recovery after failures") and NFR-REL-03
    # ("durability comes from the checkpointer, not process memory") are buying. Costs one
    # round-trip per superstep on a pool-local connection.
    durability: Literal["sync", "async", "exit"] = Field(default="sync")  # TBD(§8.4)

    node_timeout_seconds: float = Field(default=120.0)  # TBD(§8.4)

    # langgraph's own default, made explicit because it is a correctness bound here, not a
    # runaway guard: the worst-case path grows with `max_retries`, and a limit that does
    # not admit it turns an FR-RET-05 abstention into a GraphRecursionError → 500. The
    # relationship is asserted from the topology in tests/test_graph.py — config must not
    # import the graph.
    recursion_limit: int = Field(default=25)  # TBD(§8.4)

    # The R-24 processing gate (T-302, R-43). Not a free number, and the tension is the same
    # double duty `WORKER_JOB_TIMEOUT_SECONDS` carries: too short and the gate lifts during a
    # healthy slow turn; too long and a crashed run locks the user out of their own uploads
    # for the whole remainder. Both failures are bounded — R-24 makes the lock advisory, so
    # an early lift degrades UX and never correctness.
    lock_ttl_seconds: float = Field(default=180.0)  # TBD(§8.4)

    # A kill switch for diagnosis, on the `RETRIEVAL_DEDUPE_ADJACENT` precedent — never the
    # handling path for a bug. Turning it off stops the four mutating document routes
    # answering 409; the graph still takes and releases the lock.
    lock_enforced: bool = Field(default=True)

    # The T-303 prompt-injection screen (NFR-SEC-05, R-44). Switchable *because* the screen is
    # defence in depth: R-44(3) puts the requirement on the structural controls (the system
    # prompt carries no untrusted bytes, authorization never reaches the prompt, FR-CIT-06(2)
    # rejects an unsupplied citation), and none of those has a flag. A false-positive storm on
    # a pattern rule needs a diagnosis path that is not "ship a hotfix"; the isolation it backs
    # up must not have one. Note the contrast with R-32's ClamAV pass, which fails *closed* —
    # there the scanner is the only control, so disabling it removes the protection outright.
    screen_enabled: bool = Field(default=True)

    # The T-304 FR-RET-03 router (R-45(2)). Same precedent as the two switches above, and the
    # weakest of the three claims on a flag: the router is a retrieval-quality optimisation
    # whose off state is `hybrid` — which FR-RET-03 itself prescribes for an unclassified
    # query — so turning it off removes no requirement, it removes an improvement. Note this
    # is *not* the failure path: R-45(2) makes the node fail open on its own, so this flag is
    # for a deliberate "stop paying for the extra call", not for coping with a broken one.
    router_enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def _coherent(self) -> GraphSettings:
        if self.max_retries < 0:
            raise ValueError("GRAPH_MAX_RETRIES must be >= 0")
        if self.node_timeout_seconds <= 0:
            raise ValueError("GRAPH_NODE_TIMEOUT_SECONDS must be > 0")
        if self.recursion_limit < 1:
            raise ValueError("GRAPH_RECURSION_LIMIT must be >= 1")
        # A gate that cannot outlive one node is meaningless: `retrieve` alone may run for
        # `node_timeout_seconds`, so a shorter TTL would expire inside a single healthy step.
        if self.lock_ttl_seconds < self.node_timeout_seconds:
            raise ValueError("GRAPH_LOCK_TTL_SECONDS must be >= GRAPH_NODE_TIMEOUT_SECONDS")
        return self


class KeycloakSettings(BaseSettings):
    """Keycloak (OIDC) connection settings — auth baseline per R-28.

    Auth is backend-mediated (ROPC): the API exchanges login credentials with
    Keycloak's token endpoint and validates realm-signed RS256 JWTs against the
    realm JWKS. No local password hashing. Wired up in T-103.
    """

    model_config = SettingsConfigDict(env_prefix="KEYCLOAK_", env_file=".env", extra="ignore")

    server_url: str = Field(default="http://localhost:8080")
    realm: str = Field(default="corpus")
    client_id: str = Field(default="corpus-backend")
    client_secret: str = Field(default="")

    @computed_field
    @property
    def issuer(self) -> str:
        return f"{self.server_url.rstrip('/')}/realms/{self.realm}"

    @computed_field
    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/certs"

    @computed_field
    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    @computed_field
    @property
    def logout_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/logout"

    @computed_field
    @property
    def admin_url(self) -> str:
        # The Admin REST API lives under /admin/realms/<realm>, NOT under the
        # /realms/<realm> issuer path — deriving it from `issuer` is a 404 trap.
        return f"{self.server_url.rstrip('/')}/admin/realms/{self.realm}"


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Corpus"
    environment: str = "development"

    # structlog's `cache_logger_on_first_use`. True is right for a long-lived process and
    # is the default. It is a *setting* rather than a constant because caching latches every
    # module-level logger onto the processor chain configured at its first use, and nothing
    # un-latches it afterwards — not `structlog.configure`, not
    # `structlog.testing.capture_logs`. `app.main` builds the app at import time
    # (`app = create_app()`, the uvicorn entrypoint), so by the time any test fixture runs
    # the latch has already closed; only an environment decision made *before* the import
    # can open it. `tests/conftest.py` sets it false for exactly that reason, and it doubles
    # as a local-debugging knob.
    log_cache_loggers: bool = True

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    chunker: ChunkerSettings = Field(default_factory=ChunkerSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    sse: SseSettings = Field(default_factory=SseSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    clamav: ClamAVSettings = Field(default_factory=ClamAVSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    checkpointer: CheckpointerSettings = Field(default_factory=CheckpointerSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)

    @model_validator(mode="after")
    def _coherent(self) -> Settings:
        # R-38(6): the one invariant that spans two groups, so it cannot live on either.
        # A `clamd` stream ceiling below the upload ceiling means the largest legal upload
        # is unscannable — and the failure mode R-32 names is that clamd *truncates* and
        # reports the prefix clean, i.e. it fails open silently. Refuse to boot instead.
        if self.clamav.max_stream_bytes < self.upload.max_file_bytes:
            raise ValueError(
                f"CLAMAV_MAX_STREAM_BYTES ({self.clamav.max_stream_bytes:,}) must be >= "
                f"UPLOAD_MAX_FILE_BYTES ({self.upload.max_file_bytes:,}) — a smaller value "
                "leaves the largest legal upload unscannable (R-32, §8.13). Raise it here "
                "and in clamd.conf's StreamMaxLength together."
            )
        # R-42(9): the enforceable form of FR-PER-01's "never `InMemorySaver` in
        # production". An in-memory saver loses every conversation on restart, which makes
        # NFR-REL-03's stateless-API claim false — and the failure is silent, since the
        # graph runs perfectly right up until the process dies. Refuse to boot instead.
        if self.environment == "production" and self.checkpointer.backend == "memory":
            raise ValueError(
                "CHECKPOINTER_BACKEND=memory is forbidden when ENVIRONMENT=production "
                "(FR-PER-01): conversation durability comes from the checkpointer "
                "(NFR-REL-03). Set CHECKPOINTER_BACKEND=postgres."
            )
        return self

    # R-41(5): the stall threshold spans two groups, so like the invariant above it cannot
    # live on either one. Derived rather than defaulted to a literal because the value it
    # depends on is a §8.4 TBD — pinning a number here would let the two drift the moment
    # `WORKER_JOB_TIMEOUT_SECONDS` is finally settled.
    STALL_MARGIN_SECONDS: ClassVar[float] = 60.0  # TBD(§8.4)

    @property
    def stall_after(self) -> float:
        """Seconds of silence after which an in-flight document is reported `stalled`.

        The floor is arq's in-progress guard (`job_timeout + 10`), below which a *healthy*
        long ingestion would be flagged; the margin sits above that so the flag means
        "nothing is coming" rather than "this is taking a while". An explicit
        `SSE_STALL_AFTER_SECONDS` wins — an operator who has measured their own corpus
        knows better than this formula, and clamping their value would hide the override.
        """
        if self.sse.stall_after_seconds is not None:
            return self.sse.stall_after_seconds
        return self.worker.job_timeout_seconds + self.STALL_MARGIN_SECONDS


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (FastAPI dependency-friendly)."""
    return Settings()
