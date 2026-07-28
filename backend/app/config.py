"""Application settings (T-005).

Configuration groups (database, OpenAI, MinIO/S3, Redis/arq) plus the Keycloak
auth settings introduced by ruling R-28 (Rev 0.6.2). Each group is its own
`BaseSettings` with an `env_prefix` and is composed onto the top-level `Settings`.
Values come from the environment / a local `.env`; defaults align with the
`deployment/docker-compose.yml` services so the app boots without a `.env`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """PostgreSQL connection. `url` binds `DATABASE_URL` (async asyncpg DSN).

    Points at the developer's local pgvector-enabled `corpus` database; the schema
    and indexes are owned by Alembic (T-101).
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_", env_file=".env", extra="ignore")

    url: str = Field(default="postgresql+asyncpg://postgres:postgres@localhost:5432/corpus")


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

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    chunker: ChunkerSettings = Field(default_factory=ChunkerSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (FastAPI dependency-friendly)."""
    return Settings()
