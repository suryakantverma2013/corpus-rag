"""`users` — local identity keyed to the Keycloak subject (R-28, T-101).

The PK ``id`` IS the Keycloak ``sub`` UUID (supplied from validated token claims,
not generated here) so documents/conversations/audit rows keep referential
integrity. There is deliberately **no** ``password_hash`` and no authoritative
``role`` column — Keycloak owns credentials, and role comes from JWT claims.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin


class User(CreatedAtMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)  # Keycloak sub
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
