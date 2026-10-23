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
    """Role graph and privileges required before migration 557 can own relay
    functions.

    Direction matters.  The migrator (``module_database_roles.v1``'s
    ``app_admin``) is a member of the runtime owner role so it can ALTER what
    the owner owns.  The owner is never a member of the migrator: the runtime
    identity must have no ``SET ROLE`` path to a BYPASSRLS role.
    """

    migration_role: str
    owner_role: str
    definer_role: str
    schema: str
    schema_privileges: tuple[str, ...]


OUTBOX_RELAY_OWNERSHIP_CONTRACT: Final[RelayOwnershipContract] = RelayOwnershipContract(
    migration_role="app_admin",
    owner_role="dotmac_app",
    definer_role="app_admin",
    schema="public",
    schema_privileges=("USAGE", "CREATE"),
)


@dataclass(frozen=True, slots=True)
class RoleGraphObservation:
    """What the catalog says about the migrator/owner edge, without mutation."""

    migrator_exists: bool
    owner_exists: bool
    #: ``pg_has_role(migrator, owner, 'USAGE')`` i.e. INHERIT through the edge.
    migrator_inherits_owner: bool
    #: ``pg_has_role(migrator, owner, 'SET')``.
    migrator_can_set_owner: bool
    #: Any ``pg_auth_members`` row for (owner <- migrator) with admin_option.
    migrator_admin_option: bool
    #: ``pg_has_role(owner, migrator, 'MEMBER')``: the OLD direction.
    owner_reaches_migrator: bool
    #: LOGIN, non-superuser roles other than the migrator that reach a
    #: BYPASSRLS role, as ``"role->bypass_role"`` strings.
    bypass_reachers: tuple[str, ...] = ()


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
    role_graph: RoleGraphObservation,
    definer_schema_privileges: Mapping[str, bool],
) -> tuple[str, ...]:
    """Describe role-graph and schema-privilege drift in BOTH directions."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    graph = role_graph
    violations: list[str] = []
    if not graph.migrator_exists:
        violations.append(f"database role {contract.migration_role!r} is missing")
    if not graph.owner_exists:
        violations.append(f"database role {contract.owner_role!r} is missing")
    if graph.migrator_exists and graph.owner_exists:
        if not graph.migrator_inherits_owner:
            violations.append(
                f"{contract.migration_role} is not a member of "
                f"{contract.owner_role} with INHERIT"
            )
        if not graph.migrator_can_set_owner:
            violations.append(
                f"{contract.migration_role} cannot SET ROLE {contract.owner_role}"
            )
        if graph.migrator_admin_option:
            violations.append(
                f"{contract.migration_role} holds ADMIN OPTION on {contract.owner_role}"
            )
        if graph.owner_reaches_migrator:
            violations.append(
                f"{contract.owner_role} is a member of {contract.migration_role} "
                "(old direction; the runtime owner must not reach the migrator)"
            )
    for reacher in graph.bypass_reachers:
        violations.append(
            f"login role path {reacher} reaches a BYPASSRLS role; only "
            f"{contract.migration_role} may"
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
    "RoleGraphObservation",
    "RolePosture",
    "relay_dispatcher_violations",
    "relay_ownership_violations",
]
