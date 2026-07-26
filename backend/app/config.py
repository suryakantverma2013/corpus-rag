"""Application settings (T-005).

Configuration groups (database, OpenAI, MinIO/S3, Redis/arq) plus the Keycloak
auth settings introduced by ruling R-28 (Rev 0.6.2). Each group is its own
`BaseSettings` with an `env_prefix` and is composed onto the top-level `Settings`.
Values come from the environment / a local `.env`; defaults align with the
`deployment/docker-compose.yml` services so the app boots without a `.env`.
"""

from __future__ import annotations

from functools import lru_cache

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
    """MinIO / S3 object storage — originals + artifacts (R-19, FR-ING-02).

    Defaults match the compose `minio` service. Client wiring is T-201.
    """

    model_config = SettingsConfigDict(env_prefix="MINIO_", env_file=".env", extra="ignore")

    endpoint: str = Field(default="localhost:9000")
    access_key: str = Field(default="minioadmin")
    secret_key: str = Field(default="minioadmin")
    bucket: str = Field(default="corpus")
    secure: bool = Field(default=False)


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
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    keycloak: KeycloakSettings = Field(default_factory=KeycloakSettings)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (FastAPI dependency-friendly)."""
    return Settings()
