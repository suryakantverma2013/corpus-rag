"""SQLAlchemy ORM models (T-101).

Importing this package registers every model on `Base.metadata`, which Alembic
uses as `target_metadata`. `users` is keyed to the Keycloak subject; no
password_hash (R-28).
"""

from __future__ import annotations

from app.db.base import Base
from app.db.models.audit_log import AuditLog
from app.db.models.conversation import Conversation
from app.db.models.document import Document
from app.db.models.document_chunk import DocumentChunk
from app.db.models.knowledge_base import KnowledgeBase
from app.db.models.knowledge_job import KnowledgeJob
from app.db.models.message import Message
from app.db.models.users import User

__all__ = [
    "Base",
    "AuditLog",
    "Conversation",
    "Document",
    "DocumentChunk",
    "KnowledgeBase",
    "KnowledgeJob",
    "Message",
    "User",
]
