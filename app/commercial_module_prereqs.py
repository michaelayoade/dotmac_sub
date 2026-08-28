"""Typed deployment prerequisites for composed commercial module storage.

Cluster roles and database schemas are deployment prerequisites, not ordinary
application migration effects.  Alembic may verify this contract, but the
privileged bootstrap script is the only owner that creates or repairs it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

RolePosture = tuple[bool, bool, bool]


@dataclass(frozen=True)
class DatabaseRoleContract:
    """Exact role posture owned by the privileged prerequisite bootstrap."""

    name: str
    can_login: bool
    bypass_rls: bool
    superuser: bool

    @property
    def posture(self) -> RolePosture:
        return (self.can_login, self.bypass_rls, self.superuser)


@dataclass(frozen=True)
class ModuleSchemaContract:
    """One composed module schema and the least privileges Sub expects."""

    module: str
    distribution: str
    import_name: str
    schema: str
    owner_role: str = "dotmac_app"
    usage_roles: tuple[str, ...] = ("app_admin", "app_user", "platform_api")


@dataclass(frozen=True)
class ModuleSchemaObservation:
    """Read-only catalog observation for one module schema."""

    owner_role: str | None
    public_privileges: tuple[str, ...]
    usage_roles: tuple[str, ...]


MODULE_DATABASE_ROLE_CONTRACT: Final[dict[str, DatabaseRoleContract]] = {
    "app_admin": DatabaseRoleContract(
        name="app_admin",
        can_login=True,
        bypass_rls=True,
        superuser=False,
    ),
    "app_user": DatabaseRoleContract(
        name="app_user",
        can_login=True,
        bypass_rls=False,
        superuser=False,
    ),
    "platform_api": DatabaseRoleContract(
        name="platform_api",
        can_login=True,
        bypass_rls=False,
        superuser=False,
    ),
}

COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT: Final[dict[str, DatabaseRoleContract]] = {
    "dotmac_app": DatabaseRoleContract(
        name="dotmac_app",
        can_login=True,
        bypass_rls=False,
        superuser=False,
    ),
    **MODULE_DATABASE_ROLE_CONTRACT,
}

COMMERCIAL_MODULE_SCHEMA_CONTRACT: Final[tuple[ModuleSchemaContract, ...]] = (
    ModuleSchemaContract(
        module="billing",
        distribution="dotmac-billing",
        import_name="dotmac_billing",
        schema="mod_billing",
    ),
    ModuleSchemaContract(
        module="collections",
        distribution="dotmac-collections",
        import_name="dotmac_collections",
        schema="mod_coll",
    ),
    ModuleSchemaContract(
        module="payments",
        distribution="dotmac-payments",
        import_name="dotmac_payments",
        schema="mod_payments",
    ),
    ModuleSchemaContract(
        module="service_orders",
        distribution="dotmac-service-orders",
        import_name="dotmac_service_orders",
        schema="mod_serviceorders",
    ),
    ModuleSchemaContract(
        module="subscriptions",
        distribution="dotmac-subscriptions",
        import_name="dotmac_subscriptions",
        schema="mod_subscriptions",
    ),
)

COMMERCIAL_MODULE_SCHEMAS: Final[frozenset[str]] = frozenset(
    item.schema for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT
)


def role_posture_violations(
    contract: Mapping[str, DatabaseRoleContract],
    observed: Mapping[str, RolePosture],
) -> tuple[str, ...]:
    """Describe missing or over-privileged roles without mutating the database."""

    violations: list[str] = []
    for role, expected in contract.items():
        actual = observed.get(role)
        if actual is None:
            violations.append(f"database role {role!r} is missing")
        elif actual != expected.posture:
            violations.append(
                f"{role} has (rolcanlogin, rolbypassrls, rolsuper)={actual!r}; "
                f"expected {expected.posture!r}"
            )
    return tuple(violations)


def module_database_role_violations(
    observed: Mapping[str, RolePosture],
) -> tuple[str, ...]:
    """Validate only the three roles that satisfy ``module_database_roles.v1``."""

    return role_posture_violations(MODULE_DATABASE_ROLE_CONTRACT, observed)


def commercial_bootstrap_role_violations(
    observed: Mapping[str, RolePosture],
) -> tuple[str, ...]:
    """Validate every cluster role the schema bootstrap needs."""

    return role_posture_violations(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT, observed)


def commercial_schema_violations(
    observed: Mapping[str, ModuleSchemaObservation],
) -> tuple[str, ...]:
    """Describe commercial module schema drift without mutating the database."""

    violations: list[str] = []
    for expected in COMMERCIAL_MODULE_SCHEMA_CONTRACT:
        actual = observed.get(expected.schema)
        if actual is None:
            violations.append(f"schema {expected.schema!r} is missing")
            continue
        if actual.owner_role != expected.owner_role:
            violations.append(
                f"schema {expected.schema!r} is owned by "
                f"{actual.owner_role!r}; expected {expected.owner_role!r}"
            )
        if actual.public_privileges:
            violations.append(
                f"schema {expected.schema!r} grants "
                f"{', '.join(actual.public_privileges)} to PUBLIC"
            )
        missing_usage = tuple(
            role for role in expected.usage_roles if role not in actual.usage_roles
        )
        if missing_usage:
            violations.append(
                f"schema {expected.schema!r} does not grant USAGE to "
                f"{', '.join(missing_usage)}"
            )
    return tuple(violations)


__all__ = [
    "COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT",
    "COMMERCIAL_MODULE_SCHEMA_CONTRACT",
    "COMMERCIAL_MODULE_SCHEMAS",
    "DatabaseRoleContract",
    "MODULE_DATABASE_ROLE_CONTRACT",
    "ModuleSchemaContract",
    "ModuleSchemaObservation",
    "RolePosture",
    "commercial_bootstrap_role_violations",
    "commercial_schema_violations",
    "module_database_role_violations",
    "role_posture_violations",
]
