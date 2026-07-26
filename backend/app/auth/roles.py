"""The two-role model (§4.9) mapped onto Keycloak role claims (R-28, T-103).

The spec pins two roles — Administrator and Non-Administrator — and leaves the
realm-vs-client claim source to implementation. We read **realm** roles
(``realm_access.roles``) as the baseline and tolerate client roles
(``resource_access.<client>.roles``) so a realm that models the role on the
client still works. Everyone who authenticates is at least a Non-Administrator.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any


class Role(enum.StrEnum):
    """Corpus roles. Values are the Keycloak role names."""

    ADMINISTRATOR = "admin"
    NON_ADMINISTRATOR = "user"


def extract_roles(claims: Mapping[str, Any], *, client_id: str) -> set[Role]:
    """Collect known :class:`Role` values from a validated token's claims.

    Reads realm roles first, then this client's roles. Unknown role strings are
    ignored. The Non-Administrator role is always implied for an authenticated
    principal so downstream checks can treat it as the floor.
    """
    names: set[str] = set()

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, Mapping):
        names.update(_string_list(realm_access.get("roles")))

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, Mapping):
        client = resource_access.get(client_id)
        if isinstance(client, Mapping):
            names.update(_string_list(client.get("roles")))

    roles = {role for role in Role if role.value in names}
    roles.add(Role.NON_ADMINISTRATOR)
    return roles


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []
