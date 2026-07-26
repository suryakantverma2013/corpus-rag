"""Aggregate v1 API router (T-103).

Mounted at ``/api/v1`` by ``create_app()``. Future routers (users, knowledge
bases, documents, jobs, conversations, messages) register here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import audit, auth, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(audit.router)
