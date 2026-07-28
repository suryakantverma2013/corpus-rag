"""Domain enums, mapped as portable VARCHAR + CHECK (`native_enum=False`).

Avoids Postgres `ALTER TYPE` churn while keeping Python-side type safety and
matching the source DDL's `VARCHAR(32)` intent (`AdditionalChatBotRequirements.md`
§5). Values are the stored strings.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def str_enum(enum_cls: type[enum.Enum], **kwargs: object) -> SAEnum:
    """Portable VARCHAR + CHECK column type storing the enum ``.value`` strings.

    `native_enum=False` avoids Postgres `ALTER TYPE`; `values_callable` ensures the
    stored string is the member *value* (e.g. ``'user'``), not its name (``'USER'``).
    """

    return SAEnum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [m.value for m in cls],
        **kwargs,
    )


class DocumentStatus(enum.StrEnum):
    """The 11 document lifecycle states (FR-ING-01, §4.15)."""

    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    DELETE_PENDING = "DELETE_PENDING"
    DELETING = "DELETING"
    DELETED = "DELETED"


class JobType(enum.StrEnum):
    """Knowledge-job kinds (FR-ING-02/05)."""

    INGEST = "INGEST"
    DELETE = "DELETE"


class JobStatus(enum.StrEnum):
    """Knowledge-job execution states, incl. dead-letter (FR-ING-04/06)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class MessageRole(enum.StrEnum):
    """Message author (FR-MSG-06)."""

    USER = "user"
    AI = "ai"


class Feedback(enum.StrEnum):
    """Thumbs feedback on an AI message (FR-MSG-06/08)."""

    UP = "up"
    DOWN = "down"


class KBVisibility(enum.StrEnum):
    """Knowledge-base scope (R-25): the user's default KB vs a per-chat KB."""

    GLOBAL = "GLOBAL"
    CONVERSATION = "CONVERSATION"


class AuditEventType(enum.StrEnum):
    """Security- and lifecycle-relevant audit categories (NFR-SEC-08)."""

    AUTH = "AUTH"
    USER_ROLE_CHANGE = "USER_ROLE_CHANGE"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    DOCUMENT_REPLACE = "DOCUMENT_REPLACE"
    DOCUMENT_DELETE = "DOCUMENT_DELETE"
    PERMISSION_CHANGE = "PERMISSION_CHANGE"
