"""Alembic async migration environment (T-101).

The DB URL comes from `app.config.get_settings().database.url` (async asyncpg DSN)
and `target_metadata` from the app models, so `alembic revision --autogenerate` and
`alembic check` stay in sync with the ORM. `include_object` (a) leaves tables the
app does not own alone — notably the LangGraph checkpointer tables that share the
`corpus` database — and (b) ignores the hand-maintained functional indexes
(halfvec-cast HNSW + `to_tsvector` GIN) that autogenerate cannot represent.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

import pgvector.sqlalchemy  # noqa: F401  (registers the Vector type for rendering)
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.models import Base  # noqa: F401  (populates Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database.url)

target_metadata = Base.metadata

# Functional/vector indexes created by hand in the migration (op.execute); excluded
# from autogenerate so `alembic check` does not try to drop or recreate them.
MANUAL_INDEXES = {
    "ix_document_chunks_embedding",
    "ix_document_chunks_chunk_text_fts",
}


def include_object(obj, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001
    if type_ == "table" and name not in target_metadata.tables:
        return False  # not app-owned (e.g. LangGraph checkpointer tables)
    if type_ == "index" and name in MANUAL_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
