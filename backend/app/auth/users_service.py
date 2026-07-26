"""Admin user-management orchestration (T-104, FR-USR-03/05/07).

The transaction-owning layer above ``/api/v1/users``. Every function proxies the
Keycloak Admin API (R-28 — Keycloak owns credentials/roles/the default-admin
seed) via :class:`KeycloakClient`, then syncs the local ``sub``-keyed ``users``
mirror and commits (repositories only flush). Keycloak error subclasses
propagate to the routes, which map them to HTTP status codes.

Self-protection guard: an admin cannot delete, disable, or demote their own
account (grounded in FR-USR-03's "delete *other* user accounts"). A last-admin
guard is intentionally out of scope — see OI-25.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keycloak_client import KeycloakClient
from app.auth.principal import Principal
from app.auth.roles import Role
from app.auth.schemas import UpdateUserRequest, UserResponse
from app.db.repositories.users import UserRepository
from app.services import audit


class SelfMutationError(Exception):
    """An admin attempted a lockout-risking change to their own account → 409."""


async def create_user(
    *,
    email: str,
    display_name: str | None,
    password: str,
    role: Role,
    actor: Principal,
    kc: KeycloakClient,
    session: AsyncSession,
) -> UserResponse:
    admin_token = await kc.service_account_token()
    sub = await kc.admin_create_user(
        email=email, display_name=display_name, password=password, admin_token=admin_token
    )
    # The realm's defaultRoles grant `user`; promote to admin explicitly.
    if role is Role.ADMINISTRATOR:
        role_rep = await kc.admin_get_realm_role(
            name=Role.ADMINISTRATOR.value, admin_token=admin_token
        )
        await kc.admin_add_realm_roles(sub=sub, roles=[role_rep], admin_token=admin_token)

    new_sub = uuid.UUID(sub)
    await UserRepository(session).upsert_from_claims(
        sub=new_sub, email=email, display_name=display_name
    )
    await audit.record_user_change(
        session,
        actor_id=actor.sub,
        target_user_id=new_sub,
        action="create",
        details={"email": email, "role": role.value},
    )
    if role is Role.ADMINISTRATOR:
        await audit.record_permission_change(
            session,
            actor_id=actor.sub,
            target_user_id=new_sub,
            details={"granted": [Role.ADMINISTRATOR.value]},
        )
    await session.commit()

    roles = [Role.ADMINISTRATOR.value] if role is Role.ADMINISTRATOR else []
    return UserResponse(
        id=uuid.UUID(sub),
        email=email,
        display_name=display_name,
        roles=_roles(roles),
        is_active=True,
    )


async def list_users(
    *, kc: KeycloakClient, first: int = 0, limit: int = 100, search: str | None = None
) -> list[UserResponse]:
    admin_token = await kc.service_account_token()
    reps = await kc.admin_list_users(
        admin_token=admin_token, first=first, limit=limit, search=search
    )
    # One call resolves admin membership for the whole page (avoids an N+1).
    admin_users = await kc.admin_get_role_users(
        role_name=Role.ADMINISTRATOR.value, admin_token=admin_token
    )
    admin_ids = {u.get("id") for u in admin_users}
    return [_view_from_rep(rep, is_admin=rep.get("id") in admin_ids) for rep in reps]


async def update_user(
    *,
    sub: uuid.UUID,
    patch: UpdateUserRequest,
    actor: Principal,
    kc: KeycloakClient,
    session: AsyncSession,
) -> UserResponse:
    if sub == actor.sub and (patch.is_active is False or patch.role is Role.NON_ADMINISTRATOR):
        raise SelfMutationError("cannot disable or demote your own account")

    admin_token = await kc.service_account_token()
    sub_str = str(sub)

    # Current roles drive both the promote/demote delta and the response.
    current = _names(await kc.admin_get_user_realm_roles(sub=sub_str, admin_token=admin_token))
    is_admin = Role.ADMINISTRATOR.value in current
    permission_delta: dict[str, list[str]] | None = None
    if patch.role is Role.ADMINISTRATOR and not is_admin:
        rep = await kc.admin_get_realm_role(name=Role.ADMINISTRATOR.value, admin_token=admin_token)
        await kc.admin_add_realm_roles(sub=sub_str, roles=[rep], admin_token=admin_token)
        is_admin = True
        permission_delta = {"granted": [Role.ADMINISTRATOR.value]}
    elif patch.role is Role.NON_ADMINISTRATOR and is_admin:
        rep = await kc.admin_get_realm_role(name=Role.ADMINISTRATOR.value, admin_token=admin_token)
        await kc.admin_remove_realm_roles(sub=sub_str, roles=[rep], admin_token=admin_token)
        is_admin = False
        permission_delta = {"revoked": [Role.ADMINISTRATOR.value]}

    fields: dict[str, Any] = {}
    if patch.is_active is not None:
        fields["enabled"] = patch.is_active
    if patch.display_name is not None:
        fields.update(_name_fields(patch.display_name))
    if fields:
        await kc.admin_update_user(sub=sub_str, admin_token=admin_token, **fields)

    if patch.new_password is not None:
        await kc.admin_reset_password(
            sub=sub_str, new_password=patch.new_password, admin_token=admin_token
        )

    # Re-read authoritative user state, then sync the local mirror.
    user_rep = await kc.admin_get_user(sub=sub_str, admin_token=admin_token)
    repo = UserRepository(session)
    user = await repo.upsert_from_claims(
        sub=sub, email=user_rep.get("email", ""), display_name=_display_name(user_rep)
    )
    await repo.set_active(user, is_active=bool(user_rep.get("enabled", True)))

    # Audit: role/permission changes and the field-update summary (before commit).
    if permission_delta is not None:
        await audit.record_permission_change(
            session, actor_id=actor.sub, target_user_id=sub, details=permission_delta
        )
    changed = _changed_fields(patch)
    if changed:
        await audit.record_user_change(
            session,
            actor_id=actor.sub,
            target_user_id=sub,
            action="update",
            details={"fields": changed},
        )
    await session.commit()

    return _view_from_rep(user_rep, is_admin=is_admin)


async def delete_user(
    *, sub: uuid.UUID, actor: Principal, kc: KeycloakClient, session: AsyncSession
) -> None:
    if sub == actor.sub:
        raise SelfMutationError("cannot delete your own account")

    admin_token = await kc.service_account_token()
    await kc.admin_delete_user(sub=str(sub), admin_token=admin_token)

    # Retain the local row (FK targets from documents/conversations) but disable it;
    # owned-document deletion is a separate admin flow (FR-STA-03).
    user = await UserRepository(session).get(sub)
    if user is not None:
        user.is_active = False
    await audit.record_user_change(session, actor_id=actor.sub, target_user_id=sub, action="delete")
    await session.commit()


def _changed_fields(patch: UpdateUserRequest) -> list[str]:
    """Names of the non-role fields an update touched (values omitted — no secrets)."""
    changed = []
    if patch.is_active is not None:
        changed.append("is_active")
    if patch.display_name is not None:
        changed.append("display_name")
    if patch.new_password is not None:
        changed.append("password")
    return changed


def _view_from_rep(rep: dict[str, Any], *, is_admin: bool) -> UserResponse:
    roles = [Role.ADMINISTRATOR.value] if is_admin else []
    return UserResponse(
        id=uuid.UUID(rep["id"]),
        email=rep.get("email", ""),
        display_name=_display_name(rep),
        roles=_roles(roles),
        is_active=bool(rep.get("enabled", True)),
    )


def _display_name(rep: dict[str, Any]) -> str | None:
    name = f"{rep.get('firstName', '')} {rep.get('lastName', '')}".strip()
    return name or None


def _name_fields(display_name: str) -> dict[str, str]:
    first, _, last = display_name.partition(" ")
    return {"firstName": first, "lastName": last}


def _names(role_reps: Iterable[dict[str, Any]]) -> set[str]:
    return {rep.get("name", "") for rep in role_reps}


def _roles(extra: Iterable[str]) -> list[str]:
    """Every user is at least Non-Administrator (realm default); merge extras."""
    result = {Role.NON_ADMINISTRATOR.value, *extra}
    return sorted(result)
