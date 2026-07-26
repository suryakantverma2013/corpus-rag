"""User repository (T-102). Identity mirrors the Keycloak subject (R-28)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.models.users import User
from app.db.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self.session.scalars(stmt)).first()

    async def upsert_from_claims(
        self, *, sub: uuid.UUID, email: str, display_name: str | None = None
    ) -> User:
        """Create or refresh a local user from validated Keycloak claims (R-28)."""
        user = await self.get(sub)
        if user is None:
            user = User(id=sub, email=email, display_name=display_name)
            self.session.add(user)
        else:
            user.email = email
            if display_name is not None:
                user.display_name = display_name
        await self.session.flush()
        return user

    async def set_active(self, user: User, *, is_active: bool) -> User:
        user.is_active = is_active
        await self.session.flush()
        return user
