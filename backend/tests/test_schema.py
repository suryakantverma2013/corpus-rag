"""Schema invariants for the initial migration (T-101).

Metadata-only assertions (no live DB) so they run anywhere. They pin the
spec-load-bearing shape: the table set, R-28 (no password_hash), the FR-RET-04
access-filter columns, the pgvector embedding dimension, FR-KBM-08 per-KB dedup,
FR-ING-04 idempotency, and the FR-MSG-06 message columns. Migration-level checks
(the hand-managed functional indexes) are asserted against the migration source.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.db.base import EMBEDDING_DIM
from app.db.enums import DocumentStatus, Feedback, MessageRole
from app.db.models import Base

EXPECTED_TABLES = {
    "users",
    "knowledge_bases",
    "documents",
    "document_chunks",
    "knowledge_jobs",
    "conversations",
    "messages",
    "audit_log",
}


def _unique_column_sets(table) -> list[set[str]]:  # noqa: ANN001
    return [
        {c.name for c in con.columns}
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    ]


def test_all_expected_tables_present() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_users_keyed_to_keycloak_subject_no_password() -> None:
    users = Base.metadata.tables["users"]
    assert [c.name for c in users.primary_key.columns] == ["id"]
    # R-28: credentials live in Keycloak — no local hash, no authoritative role.
    assert "password_hash" not in users.columns
    assert "role" not in users.columns


def test_access_filter_columns_present() -> None:
    # FR-RET-04 / NFR-SEC-06: filter in-query by tenant/owner, KB, active/searchable.
    chunks = Base.metadata.tables["document_chunks"]
    for col in ("tenant_id", "knowledge_base_id", "is_active"):
        assert col in chunks.columns
    docs = Base.metadata.tables["documents"]
    for col in ("tenant_id", "owner_id", "knowledge_base_id", "searchable"):
        assert col in docs.columns


def test_access_filter_index_declared() -> None:
    chunks = Base.metadata.tables["document_chunks"]
    index_cols = {tuple(c.name for c in idx.columns) for idx in chunks.indexes}
    assert ("tenant_id", "knowledge_base_id", "is_active") in index_cols


def test_embedding_dimension() -> None:
    embedding = Base.metadata.tables["document_chunks"].columns["embedding"]
    assert embedding.type.dim == EMBEDDING_DIM == 3072


def test_incremental_embedding_fields() -> None:
    # FR-ING-03 chunk field set.
    chunks = Base.metadata.tables["document_chunks"]
    for col in (
        "document_version",
        "chunk_index",
        "chunk_hash",
        "embedding_fingerprint",
        "token_count",
        "is_active",
    ):
        assert col in chunks.columns


def test_per_kb_checksum_dedup_is_partial_over_live_rows() -> None:
    """FR-KBM-08 dedup, narrowed to undeleted rows by R-39(4).

    A full constraint would make re-uploading a previously deleted file a `503`:
    `find_by_checksum` filters `deleted_at IS NULL`, so it misses the tombstone while the
    constraint still sees it. The predicate must match the query's.
    """
    docs = Base.metadata.tables["documents"]
    matching = [
        index
        for index in docs.indexes
        if index.unique
        and {column.name for column in index.columns} == {"knowledge_base_id", "checksum_sha256"}
    ]
    assert len(matching) == 1, "the per-KB checksum uniqueness index is missing"
    predicate = matching[0].dialect_options["postgresql"].get("where")
    assert predicate is not None, "uniqueness must not cover deleted rows"
    assert "deleted_at IS NULL" in str(predicate)
    # And the old table-level constraint is gone, or it would still block the re-upload.
    assert {"knowledge_base_id", "checksum_sha256"} not in _unique_column_sets(docs)


def test_chunk_version_uniqueness() -> None:
    chunks = Base.metadata.tables["document_chunks"]
    assert {"document_id", "document_version", "chunk_index"} in _unique_column_sets(chunks)


def test_job_idempotency_key_unique() -> None:
    # FR-ING-04: idempotent jobs.
    jobs = Base.metadata.tables["knowledge_jobs"]
    assert {"idempotency_key"} in _unique_column_sets(jobs)


def test_message_columns() -> None:
    # FR-MSG-06 message model + spec-added feedback; FR-EVL evaluation.
    messages = Base.metadata.tables["messages"]
    for col in (
        "role",
        "content",
        "citations",
        "evaluation",
        "feedback",
        "model_name",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
    ):
        assert col in messages.columns


def test_enum_stored_values() -> None:
    # Stored strings are member values, not names (FR-MSG-06: 'user'|'ai').
    assert [m.value for m in MessageRole] == ["user", "ai"]
    assert [m.value for m in Feedback] == ["up", "down"]
    # All 11 lifecycle states (FR-ING-01).
    assert len(list(DocumentStatus)) == 11
    assert DocumentStatus.DELETE_PENDING.value == "DELETE_PENDING"


def test_migration_defines_functional_indexes() -> None:
    # The halfvec-cast HNSW + GIN FTS indexes are hand-managed in the migration
    # (autogenerate cannot represent them); guard that they stay present.
    versions = Path(__file__).resolve().parent.parent / "app" / "db" / "migrations" / "versions"
    sql = "\n".join(p.read_text(encoding="utf-8") for p in versions.glob("*.py"))
    assert "USING hnsw" in sql and "halfvec_cosine_ops" in sql
    assert "USING gin" in sql and "to_tsvector" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
