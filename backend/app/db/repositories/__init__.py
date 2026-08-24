"""Repositories own all queries; retrieval behind a swappable interface (OI-18, T-102)."""

from __future__ import annotations

from app.db.repositories.audit_log import AuditLogRepository
from app.db.repositories.base import BaseRepository
from app.db.repositories.chunks import DocumentChunkRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.documents import DocumentListing, DocumentRepository
from app.db.repositories.figures import DocumentFigureRepository
from app.db.repositories.jobs import KnowledgeJobRepository
from app.db.repositories.knowledge_bases import KnowledgeBaseRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.processing_lock import ProcessingLockRepository
from app.db.repositories.retrieval import PgVectorRetriever
from app.db.repositories.users import UserRepository

__all__ = [
    "AuditLogRepository",
    "BaseRepository",
    "ConversationRepository",
    "DocumentChunkRepository",
    "DocumentFigureRepository",
    "DocumentListing",
    "DocumentRepository",
    "KnowledgeBaseRepository",
    "KnowledgeJobRepository",
    "MessageRepository",
    "PgVectorRetriever",
    "ProcessingLockRepository",
    "UserRepository",
]
