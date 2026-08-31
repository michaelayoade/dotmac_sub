"""Pure contract for the composed-module outbox dispatcher identities.

Role creation is an explicitly elevated cluster operation.  Migrations and
online application paths may verify these identities, but only
``scripts/bootstrap_outbox_dispatcher_roles.py`` creates or repairs them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

RolePosture = tuple[bool, bool, bool]

#: Exact ``(rolcanlogin, rolbypassrls, rolsuper)`` contract.  A dispatcher is a
#: login only so it can call its plane's two SECURITY DEFINER functions.  It
#: must not bypass RLS and must never be a superuser.
RELAY_DISPATCHER_CONTRACT: Final[dict[str, RolePosture]] = {
    "outbox_dispatcher": (True, False, False),
    "platform_outbox_dispatcher": (True, False, False),
}


@dataclass(frozen=True, slots=True)
class RelayOwnershipContract:
    """Privileges required before migration 557 can own relay functions."""

    migration_role: str
    definer_role: str
    schema: str
    schema_privileges: tuple[str, ...]


OUTBOX_RELAY_OWNERSHIP_CONTRACT: Final[RelayOwnershipContract] = RelayOwnershipContract(
    migration_role="dotmac_app",
    definer_role="app_admin",
    schema="public",
    schema_privileges=("USAGE", "CREATE"),
)


def relay_dispatcher_violations(
    observed: Mapping[str, RolePosture],
) -> tuple[str, ...]:
    """Describe every absent or wrong-shaped dispatcher without mutating it."""

    violations: list[str] = []
    for role, expected in RELAY_DISPATCHER_CONTRACT.items():
        actual = observed.get(role)
        if actual is None:
            violations.append(f"database role {role!r} is missing")
        elif actual != expected:
            violations.append(
                f"{role} has (rolcanlogin, rolbypassrls, rolsuper)={actual!r}; "
                f"expected {expected!r}"
            )
    return tuple(violations)


def relay_ownership_violations(
    *,
    migration_role_is_definer_member: bool,
    definer_schema_privileges: Mapping[str, bool],
) -> tuple[str, ...]:
    """Describe missing relay ownership prerequisites without mutating them."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    violations: list[str] = []
    if not migration_role_is_definer_member:
        violations.append(
            f"{contract.migration_role} is not a member of {contract.definer_role}"
        )
    for privilege in contract.schema_privileges:
        if not definer_schema_privileges.get(privilege, False):
            violations.append(
                f"{contract.definer_role} lacks {privilege} on schema {contract.schema}"
            )
    return tuple(violations)


__all__ = [
    "OUTBOX_RELAY_OWNERSHIP_CONTRACT",
    "RELAY_DISPATCHER_CONTRACT",
    "RelayOwnershipContract",
    "RolePosture",
    "relay_dispatcher_violations",
    "relay_ownership_violations",
]
