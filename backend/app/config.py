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
    # `SSE_PING_SECONDS` was retired at T-405. The keepalive is now FastAPI's own
    # (`fastapi.routing._PING_INTERVAL`), a module constant fixed at 15.0 — which is exactly
    # what this setting defaulted to, so nothing about the wire changed. Keeping the knob
    # would have left a documented setting wired to nothing, which is the fault
    # `EVAL_MAX_CONCURRENCY` and `EVAL_BACKEND` were both removed for.
    # None = derive from WORKER_JOB_TIMEOUT_SECONDS; see the class docstring.
    stall_after_seconds: float | None = Field(default=None)  # TBD(§8.4)
    max_streams_per_user: int = Field(default=4)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> SseSettings:
        if self.poll_interval_seconds <= 0:
            raise ValueError("SSE_POLL_INTERVAL_SECONDS must be > 0")
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
    # The FR-RET-02 reranker (T-306, R-47(1)). A third model id, and a third job: scoring a
    # passage against a query is a judgement, not a composition, so it wants the same cheap
    # schema-follower the router uses rather than `chat_model`. Separate from `router_model`
    # even though both default to the same id — they are tuned against different evidence
    # (classification accuracy vs ranking quality), and one knob per job is what stops
    # tuning either silently moving the other's cost. # TBD(§8.4)
    rerank_model: str = Field(default="gpt-4o-mini")
    # The FR-EVL-01 DeepEval judge (T-309, R-50). A fifth model id, and the first one whose
    # call site is *off* the request path entirely — it runs post-hoc in the worker. That
    # buys patience, not licence: each evaluated message costs five judge calls (three for
    # Faithfulness, two for Answer Relevancy). Separate from `rerank_model` for the reason
    # given there.
    #
    # **Was `gpt-4o-mini`; raised to `gpt-4o` on 2026-08-02 (Rev 0.20.1), and the §8.4 TBD is
    # closed by measurement rather than preference.** On T-312's golden set the cheap judge
    # scored a *verbatim restatement* of the passage it cited at 0.33–0.67 (once 0.00) against
    # a realistic 8-passage grounding set, where this model scores it 1.00 — and FR-EVL-02
    # renders that number to a user. Across the band the swap corrects six chips and regresses
    # two, so it is better on balance and **not an oracle**: this model also scored two
    # verbatim-grounded answers 0.50. That residual imprecision is OI-35, not a model choice.
    judge_model: str = Field(default="gpt-4o")
    # The T-314 escalation judge: a second, stronger model re-scores the minority of chips
    # that come back below `EVAL_ESCALATE_BELOW`. Sized by measurement, not preference — on
    # T-312's golden set `gpt-4o-mini` returned faithfulness 0.00 on an answer that was a
    # verbatim restatement of the passage it cited, and relevancy 0.00 on a correct direct
    # answer; `gpt-4o` scores both 1.00. Because the errors cluster in the tail (15 of 18
    # items score exactly 1.00 on both metrics), escalating only the low scores buys the
    # stronger judge on ~28% of messages rather than all of them. # TBD(§8.4)
    #
    # **Currently equal to `judge_model`, which means escalation is dormant** — `_scored`
    # skips a second tier that would ask the same model twice. That is the intended resting
    # state while volume is low and accuracy matters more than cost: setting `judge_model`
    # back to `gpt-4o-mini` re-arms escalation with one variable and no code change.
    judge_escalation_model: str = Field(default="gpt-4o")
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

    # The answer ceiling (T-307, R-48). Here rather than in a `GenerationSettings` group of
    # its own — unlike the router and the reranker, generation has exactly one knob, and
    # this group already owns generation's timeout and retry budget. It is a *ceiling*, not
    # a target: a truncated answer raises `ChatResponseError` and the turn fails closed
    # (R-48(2)), so this must sit well above a normal answer rather than trim one.
    max_output_tokens: int = Field(default=1_500)  # TBD(§8.4)

    # The FR-RET-03 router. Short and nearly retry-free on purpose: the call sits *before*
    # retrieval on a path NFR-PRF-02 already makes the user wait through, and R-45(2) makes
    # the router fail open — so waiting 90s for a classification is strictly worse than
    # giving up at 8s and retrieving with `hybrid`, which is what an unclassified query gets
    # anyway.
    router_timeout_seconds: float = Field(default=8.0)  # TBD(§8.4)
    router_max_retries: int = Field(default=1)  # TBD(§8.4)

    # The FR-RET-02 reranker (T-306, R-47). Longer than the router's leash and shorter than
    # generation's: this call carries whole passages rather than one query, so it is a bigger
    # request, but it sits on the same pre-first-token path — and R-47(2) makes it fail open,
    # so waiting is strictly worse than giving up and grounding in the RRF order.
    rerank_timeout_seconds: float = Field(default=20.0)  # TBD(§8.4)
    rerank_max_retries: int = Field(default=1)  # TBD(§8.4)

    # The FR-EVL-01 judge (T-309, R-50). The most patient budget of the four, and the only
    # one whose patience is free: this call site is post-hoc in the worker, so nothing is
    # waiting on it — a slow judge delays a chip, never an answer. Retries are worth more
    # here than anywhere else for the same reason, but stay bounded because arq's own
    # backoff sits underneath and a message not worth a third attempt simply keeps its
    # `evaluation` NULL (R-50: evaluation fails open).
    eval_timeout_seconds: float = Field(default=60.0)  # TBD(§8.4)
    eval_max_retries: int = Field(default=2)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> LlmSettings:
        if (
            self.timeout_seconds <= 0
            or self.router_timeout_seconds <= 0
            or self.rerank_timeout_seconds <= 0
            or self.eval_timeout_seconds <= 0
        ):
            raise ValueError("LLM_*_TIMEOUT_SECONDS values must be > 0")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("LLM_CONNECT_TIMEOUT_SECONDS must be > 0")
        if (
            self.max_retries < 0
            or self.router_max_retries < 0
            or self.rerank_max_retries < 0
            or self.eval_max_retries < 0
        ):
            raise ValueError("LLM_*_MAX_RETRIES values must be >= 0")
        if self.max_output_tokens < 1:
            raise ValueError("LLM_MAX_OUTPUT_TOKENS must be >= 1")
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

    # How many fused candidates hybrid search returns, *per probe*. This is the reranker's
    # input, NOT the FR-RET-02 top-K — that one counts the *reranked* passages that become
    # the grounding context and lives in `RerankSettings.top_k` (T-306, R-47(3)).
    fusion_top_k: int = Field(default=30)  # TBD(§8.4)

    # How many candidates survive the R-46(3) cross-probe merge — the reranker's input when
    # the T-304 router fanned a turn out over several probes. Deliberately larger than
    # `fusion_top_k` rather than equal to it (R-46(4)): capping the merged set at one probe's
    # worth would discard most of the diversity the fan-out just paid for, and since a
    # single-probe turn produces at most `fusion_top_k` candidates anyway, a larger ceiling
    # cannot change its result. Settle together with the FR-RET-02 top-K (§8.4).
    merged_top_k: int = Field(default=50)  # TBD(§8.4)

    # HNSW search-list size. pgvector's default is 40, so any deeper candidate fetch
    # silently loses recall without raising this — the index simply cannot produce that
    # many neighbours from a list it never grew.
    hnsw_ef_search: int = Field(default=100)  # TBD(§8.4)

    #: Must match the T-101 index expression verbatim — see the class docstring.
    fts_language: str = Field(default="english")

    #: R-35(5) / R-37(6) adjacent-overlap dedupe. A kill switch for diagnosis, not a taste.
    dedupe_adjacent: bool = Field(default=True)

    #: T-311: start the original query's retrieval arm inside `route`, concurrently with the
    #: T-304 classification call, instead of after it. Sound because R-45(3) makes probes
    #: additive — the query is searched on every turn regardless of what the router says, so
    #: its arm depends on nothing the router produces.
    #:
    #: A kill switch on the `dedupe_adjacent` precedent, and the least consequential one in
    #: this file: **off is not a degraded mode, it is the previous release's timing**. Same
    #: probes, same order, same merge, same failure directions — only the wall clock differs,
    #: which is why the whole test suite is required to pass with it either way.
    prefetch_query_arm: bool = Field(default=True)

    @model_validator(mode="after")
    def _coherent(self) -> RetrievalSettings:
        if self.dense_candidates < 1 or self.sparse_candidates < 1:
            raise ValueError("RETRIEVAL_*_CANDIDATES must be >= 1")
        if self.rrf_k < 1:
            raise ValueError("RETRIEVAL_RRF_K must be >= 1")
        if self.fusion_top_k < 1:
            raise ValueError("RETRIEVAL_FUSION_TOP_K must be >= 1")
        if self.merged_top_k < 1:
            raise ValueError("RETRIEVAL_MERGED_TOP_K must be >= 1")
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


class RerankSettings(BaseSettings):
    """FR-RET-02 reranking — the second stage that picks the grounding context (T-306, R-47).

    Quality and cost knobs, not limits: R-47(2) makes every failure here fall back to the
    incoming RRF order, so nothing in this group can fail a turn.

    ``top_k`` is the **FR-RET-02 top-K** — the §8.4 TBD that §8.4 itself says must be settled
    together with `RETRIEVAL_MERGED_TOP_K` and `ROUTER_MAX_SUB_QUERIES`, because the three
    bound one budget between them: the router decides how many probes run, the merge ceiling
    decides how many candidates survive them, and this decides how many of those the model
    actually reads. It also carries R-30's constraint — retrieved chunk text is excluded from
    the 10.4K conversation budget and bounded *only* here, so this number is what keeps
    history + chunks + system prompt inside the real model window.
    """

    model_config = SettingsConfigDict(env_prefix="RERANK_", env_file=".env", extra="ignore")

    #: The FR-RET-02 top-K. 8 × R-35's ~2,000-character chunks ≈ 4K tokens of grounding.
    top_k: int = Field(default=8)  # TBD(§8.4)

    #: Candidates per model call. The whole merged set in one call would be a single ~15K-token
    #: request whose *output* — one score per candidate — is what dominates latency; batching
    #: turns that into concurrent calls whose wall-clock is one batch, not fifty scores.
    #:
    #: **Measured, not guessed** (T-306 live, `gpt-4o-mini`, 50 candidates, 5 runs each):
    #: 5/10 → median **1,520 ms**; 10/5 → 2,090 ms; 25/2 → 3,579 ms. Smaller batches cost
    #: ~14% more prompt tokens, because the rubric is repeated per call, and buy a third of
    #: the wall-clock in front of NFR-PRF-02's already-delayed first token.
    batch_size: int = Field(default=5)  # TBD(§8.4)

    #: Concurrent batches. 50 candidates ÷ 5 = 10, so the defaults score a full merged set in
    #: one wave. Raising `RETRIEVAL_MERGED_TOP_K` without raising this adds a second wave and
    #: roughly doubles the stage's latency — the coupling to watch when tuning either.
    max_concurrency: int = Field(default=10)  # TBD(§8.4)

    #: Per-passage ceiling **for scoring only** — the full text still reaches generation.
    #: Relevance is decidable from the opening of a chunk far more cheaply than from all of
    #: it, and this is the single biggest lever on the call's cost.
    max_passage_chars: int = Field(default=1_200)  # TBD(§8.4)

    #: Generous, on `ROUTER_MAX_OUTPUT_TOKENS`' reasoning: a cap that truncates the JSON
    #: mid-object produces exactly the malformed payload R-47(2) then has to throw away.
    max_output_tokens: int = Field(default=400)  # TBD(§8.4)

    #: The integer scale the model scores on; the FR-CIT-04 number is `score / scale`. Ten
    #: keeps the output one token per passage, at the cost of a hover card that can only ever
    #: show tenths where the prototype shows `0.91`. Raising it to 100 is a one-value change —
    #: settle it when the card is built (T-506).
    score_scale: int = Field(default=10)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> RerankSettings:
        if self.top_k < 1:
            raise ValueError("RERANK_TOP_K must be >= 1")
        if self.batch_size < 1:
            raise ValueError("RERANK_BATCH_SIZE must be >= 1")
        if self.max_concurrency < 1:
            raise ValueError("RERANK_MAX_CONCURRENCY must be >= 1")
        if self.max_passage_chars < 1:
            raise ValueError("RERANK_MAX_PASSAGE_CHARS must be >= 1")
        if self.max_output_tokens < 1:
            raise ValueError("RERANK_MAX_OUTPUT_TOKENS must be >= 1")
        if self.score_scale < 1:
            raise ValueError("RERANK_SCORE_SCALE must be >= 1")
        return self


class GateSettings(BaseSettings):
    """FR-RET-05 / FR-CIT-06 groundedness gate — the pre-serve check (T-308, R-49).

    One group per stage, as `RouterSettings` and `RerankSettings` are. Everything here is a
    **policy** knob rather than a budget: R-49 makes the gate a pure function of checkpointed
    state — no chunk read, no model call — so there is no timeout, no retry count and no model
    id to configure, and the whole stage costs sub-millisecond.

    **There is deliberately no `GATE_ENABLED`.** R-48(3)'s argument verbatim: `screen`, `route`
    and `rerank` each carry a switch whose off state is a sanctioned degradation, and this
    one's would be "serve unverified answers". A flag that can only ever remove a requirement
    is a liability, not a diagnosis path.
    """

    model_config = SettingsConfigDict(env_prefix="GATE_", env_file=".env", extra="ignore")

    #: The FR-RET-05 groundedness threshold, closing a **(D)** open since Rev 0.5.
    #:
    #: The decision-relevant boundary in this design is **0 vs non-zero**, not this value:
    #: zero citations is the fabrication signature and is unambiguous at any threshold, so
    #: this only decides pass-vs-abstain for *partially* cited answers and a wrong value has
    #: bounded blast radius. Demanding 1.0 would fail healthy answers on their connective
    #: tissue ("In summary…"), which the substantive-claim filter narrows but cannot remove.
    #:
    #: The disposition on ties is **permissive**, on R-44's rule that a control which refuses
    #: real questions is worse than no control. If a live corpus shows healthy answers
    #: clustering below this, the *metric* is wrong and that is the finding — not a knob to
    #: nudge until the symptom goes away.
    min_groundedness: float = Field(default=0.5)  # TBD(§8.4)

    #: Words below which a sentence is connective tissue rather than a claim needing a
    #: citation. This filter is what keeps the score sane: counting "In summary:" and bare
    #: headings as unsupported claims would fail answers that are perfectly grounded.
    min_claim_words: int = Field(default=4)  # TBD(§8.4)

    #: Floor on the FR-RET-05 retry probe. Below this the rejected answer carries too little
    #: text to retrieve differently from the query, and R-45(3)'s additive probes mean the
    #: cost of a junk one is wasted recall rather than a wrong answer — so the floor is about
    #: not paying for a retry that cannot help, not about safety.
    min_probe_chars: int = Field(default=40)  # TBD(§8.4)

    @model_validator(mode="after")
    def _coherent(self) -> GateSettings:
        if not 0.0 <= self.min_groundedness <= 1.0:
            raise ValueError("GATE_MIN_GROUNDEDNESS must be between 0.0 and 1.0")
        if self.min_claim_words < 1:
            raise ValueError("GATE_MIN_CLAIM_WORDS must be >= 1")
        if self.min_probe_chars < 1:
            raise ValueError("GATE_MIN_PROBE_CHARS must be >= 1")
        return self


class EvalSettings(BaseSettings):
    """FR-EVL-01 post-hoc DeepEval scoring — the worker job (T-309, R-50).

    Stage policy, in the `ROUTER_`/`RERANK_`/`GATE_` shape. The judge's model id is an
    `OPENAI_` setting and its patience an `LLM_EVAL_*` one, per the split this codebase
    keeps between *which model*, *how the caller drives the endpoint*, and *stage policy*.

    Note the contrast with :class:`GateSettings`, which deliberately has no `GATE_ENABLED`:
    ``enabled`` is legitimate here because FR-EVL-01 says a response **may** carry scores, so
    "no chips" is a state the requirement itself sanctions and FR-ANL-04 already ships copy
    for ("Scores appear once a response is evaluated."). Turning the gate off would remove a
    requirement; turning this off costs a quality signal and some money. It is the
    `GRAPH_ROUTER_ENABLED` precedent — a *cost* switch, not a failure path.
    """

    model_config = SettingsConfigDict(env_prefix="EVAL_", env_file=".env", extra="ignore")

    #: Cost switch (see the class docstring). Off means messages keep `evaluation` NULL.
    enabled: bool = Field(default=True)

    #: Per-passage ceiling on the context handed to the judge. Five judge calls per message
    #: each carry the full cited context, so this is the main lever on evaluation cost.
    max_context_chars: int = Field(default=4_000)  # TBD(§8.4)

    # There is deliberately **no concurrency knob here**. Two levels could take one and
    # neither should: across messages it is already `WORKER_MAX_JOBS` (a second duty on that
    # knob, as `WORKER_JOB_TIMEOUT_SECONDS` carries a third), and a second setting governing
    # the same thing is how the two drift apart; within a message the fan-out is a fixed two
    # metrics, which is not a pool. The two run **sequentially on purpose** — this job is
    # post-hoc, so its latency is nearly free, and spending that freedom on lower
    # instantaneous pressure against the key that also serves live turns is the better trade.

    #: DeepEval reports usage to its vendor by default. Opting out is the default here
    #: because the payloads are answers and retrieved passages from a private corpus.
    telemetry_opt_out: bool = Field(default=True)

    #: T-314: a chip scoring below this is re-judged by `OPENAI_JUDGE_ESCALATION_MODEL` and
    #: the second score **replaces** the first. `0.0` disables escalation entirely, which is
    #: why there is no separate `EVAL_ESCALATE_ENABLED` — a bool beside a threshold is two
    #: knobs for one decision, and the one that reads "off" is already expressible.
    #:
    #: 0.9, from T-312's measured distribution: the false zeros sat at 0.00, 0.50 and 0.67
    #: while 15 of 18 healthy items scored exactly 1.00, so this cut catches every observed
    #: judge error and fires on roughly a quarter of messages. # TBD(§8.4)
    escalate_below: float = Field(default=0.9)

    @model_validator(mode="after")
    def _coherent(self) -> EvalSettings:
        if self.max_context_chars < 1:
            raise ValueError("EVAL_MAX_CONTEXT_CHARS must be >= 1")
        if not 0.0 <= self.escalate_below <= 1.0:
            # Above 1.0 would escalate *every* score, which is `OPENAI_JUDGE_MODEL` set to the
            # stronger model with extra steps and twice the bill.
            raise ValueError("EVAL_ESCALATE_BELOW must be between 0.0 and 1.0")
        return self


class ContextSettings(BaseSettings):
    """The NFR-CAP-01 conversation budget FR-STA-04 enforces and FR-ANL-03 shows (T-310).

    Stage policy, in the `ROUTER_`/`RERANK_`/`GATE_`/`EVAL_` shape. **Not** an LLM budget:
    R-30(2) makes 10.4K a deliberate *product* limit on conversation length rather than a
    model window — the OpenAI models carry far larger ones — so nothing here bounds a request
    and no value here can cause a provider error. What it bounds is how long a chat may run
    before the user must start a new one.

    There is deliberately **no `CONTEXT_ENABLED`**: its off state is "let a conversation grow
    without limit", which removes FR-STA-04 rather than degrading it — the `GATE_ENABLED`
    test, not the `EVAL_ENABLED` one.

    **History compression is out of scope, and `window_tokens` is the lever to reach for
    first** (R-67(3)/(4), spec §8.50, closing OI-26(d)). A rolling window or rolling summary
    is not a feature beside FR-STA-04 — it *removes* it, since the projected total could then
    never exceed the budget — and it redefines FR-ANL-03's meter, which R-30/R-51(1) fix as
    *conversation length*. It would also reopen what R-51(4) closed by construction: usage is
    derived from `messages`, and a summary is not a `messages` row. Because 10.4K is a
    **product** budget an order of magnitude below the model window, the answer to "chats hit
    the wall too often" is to raise this number — the meter's denominator follows it honestly
    — and compression becomes the right instrument only once it sits at the model's ceiling.
    Note the value is NFR-CAP-01's and §9's literal, so raising it is an author's change.
    """

    model_config = SettingsConfigDict(env_prefix="CONTEXT_", env_file=".env", extra="ignore")

    #: NFR-CAP-01's limit, and the denominator of FR-ANL-03's `{used}K / {max}K`. The §9
    #: quick-reference literal `3.8K / 10.4K` is an illustrative *numerator* against this.
    window_tokens: int = Field(default=10_400)  # TBD(§8.4)

    #: Headroom held back for the answer that has not been written yet.
    #:
    #: FR-STA-04 blocks when "a query's projected token total would exceed" the limit, and
    #: the reply is part of that total the moment it lands — a chat at 10.3K that accepts a
    #: short query is over budget as soon as the model answers, with the block firing a turn
    #: too late. Reserving the answer's own ceiling is what makes "the transcript stays under
    #: 10.4K" true rather than approximately true. Mirrors `LLM_MAX_OUTPUT_TOKENS`, and
    #: `Settings._coherent` refuses a reserve below it: an answer may legitimately run to
    #: that ceiling, so a smaller reserve cannot deliver the guarantee. # TBD(§8.4)
    answer_reserve_tokens: int = Field(default=1_500)

    @model_validator(mode="after")
    def _coherent(self) -> ContextSettings:
        if self.window_tokens < 1:
            raise ValueError("CONTEXT_WINDOW_TOKENS must be >= 1")
        if self.answer_reserve_tokens < 0:
            raise ValueError("CONTEXT_ANSWER_RESERVE_TOKENS must be >= 0")
        if self.answer_reserve_tokens >= self.window_tokens:
            # Every submission would be refused, including the first message of a new chat.
            raise ValueError("CONTEXT_ANSWER_RESERVE_TOKENS must be < CONTEXT_WINDOW_TOKENS")
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

    # --- Retention (OI-30, R-65) ----------------------------------------------
    # LangGraph writes one checkpoint per superstep and keeps every one, while only the
    # latest is ever read (nothing calls `aget_state_history`). Left alone the store grows
    # without bound: 43 MB across 547 threads on the dev box before this shipped.

    #: How many checkpoints to keep per thread. 1 is functionally sufficient — resume reads
    #: the latest — so everything above it is deliberate margin for a run whose supersteps
    #: are landing while the pruner is mid-pass.
    retention_keep: int = Field(default=3, ge=1)  # TBD(§8.4)

    #: Nothing younger than this is touched, whatever `retention_keep` says. This, not
    #: `keep`, is what makes the job safe beside a live turn.
    retention_min_age_seconds: float = Field(default=3600.0, ge=0)  # TBD(§8.4)

    #: Orphaned threads removed per pass, so one run cannot lock a large slice of the table.
    retention_orphan_batch: int = Field(default=500, ge=1)  # TBD(§8.4)

    #: Seconds between passes. **0 disables retention entirely** (the `EVAL_ESCALATE_BELOW`
    #: convention) — an off switch rather than a separate boolean, because "how often" and
    #: "whether" are the same question for a periodic sweep.
    retention_interval_seconds: float = Field(default=3600.0, ge=0)  # TBD(§8.4)

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

    # FR-ORC-07 requires `retry_count` to be bounded "to guarantee termination" and never says
    # by what. A retry costs a second rerank *and* a second generation (~4.6 s) on a turn the
    # user has already waited ~5.3 s through, because NFR-PRF-02 holds the stream until the
    # gate passes.
    #
    # **Ships at 0, and that is a measurement rather than a preference** (T-308 live,
    # 2026-07-31). `adapt`'s modification is HyDE from the rejected answer (R-49), and the
    # trigger is an answer that cited nothing — which in practice is a *decline*, since R-48
    # measured that `gpt-4o` declines rather than fabricating. Embedded with the real
    # `text-embedding-3-large`, a decline-shaped probe scores **0.492** against the answering
    # passage where the query itself scores **0.598**: the probe the trigger actually produces
    # is *worse* than the query it accompanies. (A probe built from a fabricated answer scores
    # 0.736 — HyDE works; it is the input this trigger yields that does not.) R-45's router
    # already routes `semantic_gap` queries to HyDE up front, so the retry is a second bite at
    # a case handled at classification time.
    #
    # The whole mechanism still ships and is tested — `adapt`, the single back edge, the bound
    # and the probe — so raising this to 1 is one environment variable, not a code change.
    # Revisit if a corpus ever shows the model fabricating instead of declining. Values above
    # 1 are refused at boot (see `Settings._coherent`): the probe is single-shot.
    max_retries: int = Field(default=0)  # TBD(§8.4)

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

    # The T-306 FR-RET-02 reranker (R-47). A **cost** switch, exactly like `router_enabled`
    # and for the same reason: R-47(2) already makes the node fail open on its own, so this
    # is for a deliberate "stop paying for the extra call", never for coping with a broken
    # one. Off, the grounding context is the top-K of the R-46(3) merged order — which is a
    # defensible ordering, but publishes no FR-CIT-04 score.
    rerank_enabled: bool = Field(default=True)

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

    # --- Cloud-account linking (FR-AUT-11, R-63) ------------------------------
    # Corpus stores no third-party credential: Keycloak brokers the provider OAuth,
    # keeps the tokens (`storeToken`) and refreshes them, and the backend reads the
    # live access token from the broker endpoint below. These two settings are the
    # entire Google-facing surface of the application — everything provider-specific
    # lives in the realm, not here.

    #: Realm identity-provider alias. Changing it changes the broker path, so it must
    #: match `identityProviders[].alias` in the realm import.
    google_idp_alias: str = Field(default="google")

    #: The public client that carries the FR-AUT-11 linking redirect, and nothing else.
    #: Deliberately NOT `client_id`: that one keeps `standardFlowEnabled: false`, which
    #: is what preserves R-28's ROPC login (R-63(2)).
    linking_client_id: str = Field(default="corpus-linking")

    def broker_token_endpoint(self, alias: str) -> str:
        """Where the *provider's* access token is read from, per FR-AUT-11.

        Requires the caller's Keycloak access token as Bearer and the `broker`
        client's `read-token` role on the user — granted at link time by the IdP's
        `addReadTokenRoleOnCreate`.
        """
        return f"{self.issuer}/broker/{alias}/token"


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
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    gate: GateSettings = Field(default_factory=GateSettings)
    eval: EvalSettings = Field(default_factory=EvalSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
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
        # R-47(3): the FR-RET-02 top-K and the R-46(4) merge ceiling span two groups, and
        # §8.4 requires them to be settled together. A top-K above the merge ceiling is not
        # an error the runtime would ever report — retrieval simply never produces that many
        # candidates, so the reranker silently returns everything it was given and the
        # operator concludes the knob works.
        if self.rerank.top_k > self.retrieval.merged_top_k:
            raise ValueError(
                f"RERANK_TOP_K ({self.rerank.top_k}) must be <= RETRIEVAL_MERGED_TOP_K "
                f"({self.retrieval.merged_top_k}) — retrieval never hands the reranker more "
                "candidates than the merge ceiling, so a larger top-K is a silent no-op "
                "(R-46(4)/R-47(3), §8.4)."
            )
        # T-310: the FR-STA-04 reserve and the answer ceiling span two groups, and the whole
        # point of the reserve is that the transcript cannot end up over budget. An answer may
        # legitimately run to `LLM_MAX_OUTPUT_TOKENS`, so a reserve below it buys a guarantee
        # that does not hold — and it would fail silently, one long answer at a time.
        if self.context.answer_reserve_tokens < self.llm.max_output_tokens:
            raise ValueError(
                f"CONTEXT_ANSWER_RESERVE_TOKENS ({self.context.answer_reserve_tokens}) must be "
                f">= LLM_MAX_OUTPUT_TOKENS ({self.llm.max_output_tokens}) — an answer may run "
                "to that ceiling, so a smaller reserve cannot keep the conversation inside "
                "NFR-CAP-01's budget (R-30, FR-STA-04)."
            )
        # R-49: `GRAPH_MAX_RETRIES` and the FR-RET-05 retry lever span two groups, and §8.4
        # already required the retry bound and the groundedness threshold to be settled
        # together. `adapt`'s only modification is HyDE from the rejected answer, which is
        # **single-shot by construction**: a second cycle would compose the *second* rejected
        # answer as a probe on top of the first, drift compounding, for another ~4.6 s on a
        # turn the user has already waited ~10 s through. Same silent-no-op class as the
        # invariant above — the extra cycles would run and change nothing structurally.
        if self.graph.max_retries > 1:
            raise ValueError(
                f"GRAPH_MAX_RETRIES ({self.graph.max_retries}) must be <= 1 — the FR-RET-05 "
                "retry composes the rejected answer as a HyDE probe, which is single-shot: a "
                "second cycle repeats the first's inputs at full latency cost (R-49, §8.4). "
                "Set 0 to abstain on the first gate failure."
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
