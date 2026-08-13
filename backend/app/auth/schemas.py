"""Request/response models for the ``/auth`` surface (R-25, T-103).

The spec adopts Source A §6's REST surface by reference without pinning body
shapes (§10.1: the generated OpenAPI schema is authoritative), so the concrete
contract is defined here.

**Amended at T-509 (R-72(1), FR-AUT-07):** login/refresh no longer return both
tokens in the JSON body. The access token is the body; the refresh token is an
httpOnly cookie and exists nowhere a script can reach it.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator

from app.auth.roles import Role


class LoginRequest(BaseModel):
    # Plain str — Keycloak is the authority on identity/format; avoids the
    # email-validator dependency.
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """The half of the token pair a browser is allowed to hold (R-72(1), FR-AUT-07).

    **There is deliberately no ``refresh_token`` field.** T-509 moved it into an httpOnly
    cookie, and a cookie set beside a body copy protects nothing — script would still read
    the body. ``RefreshRequest`` and ``LogoutRequest`` were removed for the same reason:
    with no way for a client to *obtain* a refresh token, a body that accepts one is a
    second channel that can only ever be a bypass.

    ``expires_in`` is Keycloak's ``accessTokenLifespan`` (300s on the shipped realm), and
    the client refreshes against it rather than waiting for a 401.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    roles: list[str]
    is_active: bool


class CreateUserRequest(BaseModel):
    # Plain str email — Keycloak validates identity/format (mirrors LoginRequest).
    email: str = Field(min_length=1)
    display_name: str | None = None
    password: str = Field(min_length=1)
    role: Role = Role.NON_ADMINISTRATOR


class UpdateUserRequest(BaseModel):
    """Admin PATCH — every field optional; at least one required (FR-USR-05/07)."""

    display_name: str | None = None
    is_active: bool | None = None
    role: Role | None = None
    new_password: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_one(self) -> UpdateUserRequest:
        if (
            self.display_name is None
            and self.is_active is None
            and self.role is None
            and self.new_password is None
        ):
            raise ValueError("at least one field must be provided")
        return self


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    roles: list[str]
    is_active: bool
