"""Aggregate v1 API router (T-103).

Mounted at ``/api/v1`` by ``create_app()``. Future routers (users, knowledge
bases, documents, jobs, conversations, messages) register here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import audit, auth, cloud, conversations, documents, jobs, messages, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(audit.router)
api_router.include_router(documents.router)
# FR-AUT-11 linking + the FR-KBM-10 file list (T-214). The *import* route is not here — it
# lives on `/documents`, beside the upload it shares a response contract with.
api_router.include_router(cloud.router)
api_router.include_router(jobs.router)
api_router.include_router(conversations.router)
# Nested under `/conversations/{id}` but its own module: the send route is the chat surface
# and shares nothing with conversation lifecycle. Registered after `conversations` so the
# static `/conversations` paths match before the parameterised ones (T-401/T-402).
api_router.include_router(messages.router)
# FR-MSG-08 spells a flat `/messages/{id}/feedback`, which the chat router's `/conversations`
# prefix cannot produce, so the same module exports a second router (T-403). The two prefixes
# do not overlap, so registration order is free here — unlike `conversations` before `messages`.
api_router.include_router(messages.message_router)
