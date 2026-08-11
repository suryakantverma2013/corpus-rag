"""Conversation lifecycle (T-401, FR-SBR-07, FR-PER-02/04).

Thin by design — four of the five verbs are repository calls the route makes directly. This
module exists for the fifth, because **deleting a conversation is not a row delete**: the
`conversations.id` doubles as the LangGraph `thread_id` (FR-PER-02), so the same act has to
reach a store the application does not own and which holds no foreign key back to it.

R-42(11) is explicit that a conversation delete must call the checkpointer's
``adelete_thread``: without it, FR-SBR-07 leaves the user's questions in `checkpoints` /
`checkpoint_blobs` for ever, which is a privacy defect rather than untidiness. Nothing
cascades on its behalf — the checkpointer deliberately holds no FK to application tables
(R-42(7)) — and **nothing sweeps it either** (OI-30), which is what fixes the ordering below.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DEFAULT_TENANT_ID
from app.db.models.conversation import Conversation
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.messages import MessageRepository

log = structlog.get_logger(__name__)

__all__ = ["ThreadDeletionError", "create_conversation", "delete_conversation"]


class ThreadDeletionError(Exception):
    """The LangGraph thread could not be purged, so the conversation was **not** deleted.

    Named rather than folded into `CheckpointerError` because the failure surface is wider
    than provisioning: psycopg, the connection pool and langgraph's own saver all raise here,
    across versions. The route needs one thing from all of them — "this delete did not happen,
    tell the caller to retry" — which is the same argument `ArqJobQueue._enqueue` makes for its
    own broad catch.
    """


async def create_conversation(
    session: AsyncSession, *, owner_id: uuid.UUID, title: str | None = None
) -> Conversation:
    """Start a new chat (FR-SBR-02). Flushes; the caller commits.

    `tenant_id` takes the `DEFAULT_TENANT_ID` sentinel every other scoped table uses
    (single-org, settled by R-62(4) under OI-21); there is no tenant on `users` to derive one
    from yet.

    No title is generated from anything: FR-SBR-04 renames on the GUI's terms and nothing in
    the spec asks the backend to summarise a first message into a title. `None` renders as
    the client's own placeholder.
    """
    return await ConversationRepository(session).add(
        Conversation(owner_id=owner_id, tenant_id=DEFAULT_TENANT_ID, title=title)
    )


async def delete_conversation(
    session: AsyncSession, conversation: Conversation, *, saver: object | None = None
) -> None:
    """FR-SBR-07 — delete a chat and its graph thread. Commits.

    **The thread is deleted before the application rows are committed, and the order is the
    whole point.** It mirrors R-39(9)'s purge-before-commit ruling for object storage, for the
    symmetric reason:

    * Commit first, purge second → a failure in between leaves a thread nobody can reach.
      The conversation row is gone, so no future request carries its id, and OI-30 records
      that **there is no sweeper**: the user's questions stay in `checkpoints` permanently,
      which is exactly the defect R-42(11) exists to close.
    * Purge first, commit second → a failure in between leaves a conversation whose thread is
      already gone. The transcript in `messages` is untouched and still displays; the next
      turn starts a fresh thread against the same id (the checkpointer treats an unknown
      `thread_id` as a new one), and the user can simply delete again.

    One of the two failure modes is permanent and invisible; the other is recoverable by
    repeating the action. So the recoverable one is the one we take.

    `saver` is injected for tests and defaults to the process checkpointer. It is typed
    loosely on purpose: importing `BaseCheckpointSaver` here would pull langgraph — and with
    it `apply_strict_msgpack()` — into a module the API imports at boot.
    """
    conversation_id = conversation.id

    await MessageRepository(session).delete_by_conversation(conversation_id)
    await ConversationRepository(session).delete(conversation)

    # Translated, never swallowed. Reporting a successful deletion while leaving the thread
    # behind is the one outcome this ordering exists to prevent, so the failure has to reach
    # the caller; `ThreadDeletionError` is what the route turns into a `503`.
    try:
        if saver is None:
            from app.services.checkpointer import get_checkpointer

            saver = await get_checkpointer().get()
        await saver.adelete_thread(str(conversation_id))  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning(
            "conversation.thread_delete_failed",
            conversation_id=str(conversation_id),
            exc_info=True,
        )
        raise ThreadDeletionError(str(conversation_id)) from exc

    await session.commit()
    log.info("conversation.deleted", conversation_id=str(conversation_id))
