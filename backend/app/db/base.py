"""Declarative base, shared mixins, and schema constants (T-101).

`Base` carries a MetaData naming convention so Alembic autogenerate and every
future migration produce deterministic, stable constraint/index names. All ORM
models inherit from `Base`; `models/__init__.py` imports each module so
`Base.metadata` is fully populated (Alembic `target_metadata`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Embedding vector dimension for `document_chunks.embedding`. text-embedding-3-large
# is 3072-dim; the model is not yet fixed by the spec, so this is provisional.
# Because 3072 > pgvector's 2000-dim ceiling for hnsw/ivfflat on the `vector` type,
# the cosine index is built on a halfvec(3072) cast (see the initial migration).
EMBEDDING_DIM = 3072  # text-embedding-3-large # TBD(§8.4)

# Single-tenant sentinel carried on every scoped row. **Not provisional — decided.**
# R-62(4) dispositioned OI-21: the product is single-org for MVP, and `tenant_id` is
# *retained and enforced* in every access predicate (FR-RET-04 / NFR-SEC-06) against this
# sentinel precisely because that costs nothing now and makes multi-tenancy later a **data**
# change rather than a schema-and-query rewrite. Revisit when a second organisation is
# onboarded — a product decision, not an engineering one.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")  # single-org (OI-21)

# Stable naming templates — keeps Alembic diffs clean across revisions.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all Corpus ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    """`created_at` with a server-side default (TIMESTAMPTZ)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TimestampMixin(CreatedAtMixin):
    """`created_at` + `updated_at`, both server-defaulted (TIMESTAMPTZ)."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
