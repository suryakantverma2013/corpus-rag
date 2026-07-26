"""The authenticated principal — identity distilled from validated JWT claims.

Built by ``tokens.validate_access_token`` and carried by the request via the
``get_principal`` dependency. Token-only: no database row is required to
construct it, so role-gated endpoints can authorize without a DB hit
(NFR-SEC-01). The local ``users`` row (and ``is_active``) is loaded separately
by ``get_current_user``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.auth.roles import Role


@dataclass(frozen=True, slots=True)
class Principal:
    sub: uuid.UUID
    email: str
    display_name: str | None
    roles: frozenset[Role]
    azp: str
    claims: dict[str, Any]

    @property
    def is_administrator(self) -> bool:
        return Role.ADMINISTRATOR in self.roles
