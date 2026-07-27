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

from pydantic import Field, computed_field
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
    queue: QueueSettings = Field(default_factory=QueueSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (FastAPI dependency-friendly)."""
    return Settings()
