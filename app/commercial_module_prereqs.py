"""Typed deployment prerequisites for composed module storage.

Cluster roles and database schemas are deployment prerequisites, not ordinary
application migration effects.  Alembic may verify this contract, but the
privileged bootstrap script is the only owner that creates or repairs it, and
the restricted migration role deliberately never holds database-level
``CREATE`` (ADR-0011; ``docs/runbooks/PRODUCTION_DEPLOYMENT.md``).

**The required schema set is derived, never hand-listed.**  Every composed
lineage in ``alembic.ini`` names an installed distribution; that distribution
declares its own ``short_code`` on its ``ModuleManifest``; and the kernel's
``module_schema`` turns a short code into the one immutable ``mod_*`` name.
Deriving through that chain is what keeps the bootstrap, the verifier and the
rendered documentation from drifting into parallel lists — which is exactly how
``mod_inbox`` came to be declared in code, absent from every prose list, and
present in no environment when #2819 composed the inbox lineage on 2026-08-31.

**PUBLIC denial is a forbidden-access assertion.**  ``has_schema_privilege``
cannot be asked about ``PUBLIC``, because ``PUBLIC`` has no role OID.  So the
contract carries a dedicated no-privilege probe role, ``dotmac_public_probe``:
anything ``PUBLIC`` can reach, the probe can reach too.  An absent grant row is
corroboration, never the proof.
"""

from __future__ import annotations

import configparser
import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Final

from dotmac_kernel.namespaces import module_schema
from psycopg import Connection, Error
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
ALEMBIC_INI: Final[Path] = REPO_ROOT / "alembic.ini"

_LINEAGE_SUFFIX: Final[str] = ".migrations:versions"

#: The probe role that makes "no privileges for PUBLIC" testable as denial.
PUBLIC_PROBE_ROLE: Final[str] = "dotmac_public_probe"

#: The dedicated schema-creation credential.  Provisioned out of band by an
#: elevated operator; the deployment only ever *uses* it.  It is deliberately
#: NOCREATEROLE, so the deployment's repair leg can create schemas and nothing
#: else — see ``scripts/bootstrap_commercial_module_prereqs.py --repair-schemas``.
SCHEMA_BOOTSTRAP_ROLE: Final[str] = "dotmac_schema_bootstrap"

#: Schema privileges the probe must NOT hold.  ``CREATE`` matters as much as
#: ``USAGE``: a PUBLIC ``CREATE`` grant lets any role plant objects in a module
#: schema even when it cannot read the ones already there.
PROBED_SCHEMA_PRIVILEGES: Final[tuple[str, ...]] = ("USAGE", "CREATE")


class BillingRuntimeLoginRefusal(str, Enum):
    """Closed refusals for the actual-session diagnostic boundary."""

    INACTIVE_TRANSACTION = "billing_runtime_login.inactive_transaction"
    UNSUPPORTED_POSTGRES = "billing_runtime_login.unsupported_postgres"
    ISOLATION = "billing_runtime_login.isolation"
    IDENTITY = "billing_runtime_login.identity"
    PRIVILEGED_ROLE = "billing_runtime_login.privileged_role"
    ROLE_PATH = "billing_runtime_login.role_path"
    OWNERSHIP = "billing_runtime_login.ownership"
    CATALOG_UNAVAILABLE = "billing_runtime_login.catalog_unavailable"


@dataclass(frozen=True)
class BillingRuntimeLoginQuery:
    """Fixed policy: an authenticated, unprivileged standalone app_user login.

    No caller-selected role, owner exemption or trusted identity is accepted.
    """


@dataclass(frozen=True)
class BillingRuntimeLoginObservation:
    """Transient identity diagnostics, never an admission or reusable proof.

    The caller owns the root transaction. INTRANS cannot prove the absence of
    manually issued SAVEPOINTs. This observation proves neither Billing ACL,
    PUBLIC/helper closure, nor the complete preflip prerequisite contract.
    """

    database_name: str
    database_oid: int
    backend_pid: int
    session_role_oid: int
    current_role_oid: int
    transaction_id: str | None
    server_version: int


class BillingRuntimeLoginRefused(RuntimeError):
    """Safe refusal without credentials, connection strings or catalog payloads."""

    def __init__(self, code: BillingRuntimeLoginRefusal) -> None:
        self.code = code
        super().__init__(code.value)


def require_billing_runtime_login(
    conn: Connection,
    *,
    query: BillingRuntimeLoginQuery = BillingRuntimeLoginQuery(),
) -> BillingRuntimeLoginObservation:
    """Observe the actual supplied login without connecting or changing roles.

    Requires an already active READ COMMITTED transaction on PostgreSQL 16+,
    where effective SET reachability and membership options are available.
    Conservatively refuses every direct membership, including all-false dormant
    grants, and every other effective MEMBER/USAGE/SET path. No opening/admin/owner
    role-name allowlist is guessed here.
    Refuses relation/schema/function ownership anywhere in the current database,
    including catalog and temporary namespaces; no owner exemption is trusted.
    The final owner must re-observe; this is not a ledger activation gate.
    """

    if type(query) is not BillingRuntimeLoginQuery:
        raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.IDENTITY)
    if conn.closed or conn.info.transaction_status != TransactionStatus.INTRANS:
        raise BillingRuntimeLoginRefused(
            BillingRuntimeLoginRefusal.INACTIVE_TRANSACTION
        )
    if conn.info.server_version < 160000:
        raise BillingRuntimeLoginRefused(
            BillingRuntimeLoginRefusal.UNSUPPORTED_POSTGRES
        )
    # libpq retains the startup login even after SET SESSION AUTHORIZATION.
    # SQL session_user/current_user alone cannot distinguish that impersonation.
    if conn.info.user != "app_user":
        raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.IDENTITY)
    try:
        with conn.cursor(row_factory=tuple_row) as cursor:
            # Anchor identities independently of pg_roles view-provided OIDs;
            # the view must match both those anchors and the actual role names.
            cursor.execute(
                """
                SELECT pg_catalog.current_database(), d.oid, pg_catalog.pg_backend_pid(),
                       session_user::pg_catalog.regrole::pg_catalog.oid,
                       current_user::pg_catalog.regrole::pg_catalog.oid,
                       pg_catalog.pg_current_xact_id_if_assigned()::pg_catalog.text,
                       pg_catalog.current_setting('transaction_isolation'),
                       session_user OPERATOR(pg_catalog.=) 'app_user'
                           AND current_user OPERATOR(pg_catalog.=) 'app_user'
                           AND s.oid OPERATOR(pg_catalog.=) r.oid,
                       r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls
                           OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication)
                  FROM pg_catalog.pg_database AS d
                  JOIN pg_catalog.pg_roles AS s
                    ON s.oid OPERATOR(pg_catalog.=) session_user::pg_catalog.regrole::pg_catalog.oid
                   AND s.rolname OPERATOR(pg_catalog.=) session_user
                  JOIN pg_catalog.pg_roles AS r
                    ON r.oid OPERATOR(pg_catalog.=) current_user::pg_catalog.regrole::pg_catalog.oid
                   AND r.rolname OPERATOR(pg_catalog.=) current_user
                 WHERE d.datname OPERATOR(pg_catalog.=) pg_catalog.current_database()
                """
            )
            row = cursor.fetchone()
            if row is None:
                raise BillingRuntimeLoginRefused(
                    BillingRuntimeLoginRefusal.CATALOG_UNAVAILABLE
                )
            if row[6] != "read committed":
                raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.ISOLATION)
            if row[7] is not True:
                raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.IDENTITY)
            if row[8] is not True:
                raise BillingRuntimeLoginRefused(
                    BillingRuntimeLoginRefusal.PRIVILEGED_ROLE
                )
            observation = BillingRuntimeLoginObservation(
                database_name=str(row[0]),
                database_oid=int(row[1]),
                backend_pid=int(row[2]),
                session_role_oid=int(row[3]),
                current_role_oid=int(row[4]),
                transaction_id=None if row[5] is None else str(row[5]),
                server_version=conn.info.server_version,
            )
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_roles AS r
                     WHERE r.oid OPERATOR(pg_catalog.<>) %s AND (
                         pg_catalog.pg_has_role(%s, r.oid, 'MEMBER')
                         OR pg_catalog.pg_has_role(%s, r.oid, 'USAGE')
                         OR pg_catalog.pg_has_role(%s, r.oid, 'SET'))
                ) OR EXISTS (
                    SELECT 1 FROM pg_catalog.pg_auth_members AS m
                     WHERE m.member OPERATOR(pg_catalog.=) %s
                )
                """,
                (observation.session_role_oid,) * 5,
            )
            role_paths = cursor.fetchone()
            if role_paths is None:
                raise BillingRuntimeLoginRefused(
                    BillingRuntimeLoginRefusal.CATALOG_UNAVAILABLE
                )
            if role_paths[0] is not False:
                raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.ROLE_PATH)
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_class AS c
                     WHERE c.relowner OPERATOR(pg_catalog.=) %s
                ) OR EXISTS (
                    SELECT 1 FROM pg_catalog.pg_namespace AS n
                     WHERE n.nspowner OPERATOR(pg_catalog.=) %s
                ) OR EXISTS (
                    SELECT 1 FROM pg_catalog.pg_proc AS p
                     WHERE p.proowner OPERATOR(pg_catalog.=) %s
                ) OR EXISTS (
                    SELECT 1 FROM pg_catalog.pg_database AS d
                     WHERE d.oid OPERATOR(pg_catalog.=) %s
                       AND d.datdba OPERATOR(pg_catalog.=) %s
                )
                """,
                (
                    observation.session_role_oid,
                    observation.session_role_oid,
                    observation.session_role_oid,
                    observation.database_oid,
                    observation.session_role_oid,
                ),
            )
            owners = cursor.fetchone()
            if owners is None:
                raise BillingRuntimeLoginRefused(
                    BillingRuntimeLoginRefusal.CATALOG_UNAVAILABLE
                )
            if owners[0] is not False:
                raise BillingRuntimeLoginRefused(BillingRuntimeLoginRefusal.OWNERSHIP)
            return observation
    except Error:
        # Do not complete or repair the caller's transaction, or expose a DSN.
        raise BillingRuntimeLoginRefused(
            BillingRuntimeLoginRefusal.CATALOG_UNAVAILABLE
        ) from None


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
    """Read-only catalog observation for one module schema.

    ``probe_privileges`` is the load-bearing field: the schema privileges the
    dedicated no-privilege probe role *effectively* holds.  It must be empty.
    ``public_privileges`` is the corroborating ACL-row read; a contract that
    trusted only that would be asserting an absent row rather than denial.
    ``probe_observed`` distinguishes "the probe holds nothing" from "the probe
    role does not exist, so nothing was actually asked" — without it a missing
    probe would read as a clean result.
    """

    owner_role: str | None
    public_privileges: tuple[str, ...]
    usage_roles: tuple[str, ...]
    probe_privileges: tuple[str, ...] = ()
    probe_observed: bool = False


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
    # NOLOGIN on purpose: this role is a measuring instrument, never an
    # identity anything authenticates as.  A probe that could log in would be
    # one more credential to protect for no gain.
    PUBLIC_PROBE_ROLE: DatabaseRoleContract(
        name=PUBLIC_PROBE_ROLE,
        can_login=False,
        bypass_rls=False,
        superuser=False,
    ),
}


def composed_lineage_import_names() -> tuple[str, ...]:
    """Every module lineage this assembly composes, from ``alembic.ini``.

    This is the declaration of record for "which modules does Sub store data
    for".  Reading it here rather than restating it is what makes the schema
    set derived instead of remembered.
    """

    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(ALEMBIC_INI):
        raise RuntimeError(
            f"cannot read the composed lineage declaration: {ALEMBIC_INI}"
        )
    entries = parser["alembic"]["version_locations"].split()
    return tuple(
        entry.removesuffix(_LINEAGE_SUFFIX)
        for entry in entries
        if entry.endswith(_LINEAGE_SUFFIX)
    )


@cache
def module_schema_contract() -> tuple[ModuleSchemaContract, ...]:
    """Derive the required schema set from the composed module declarations.

    Deliberately lazy and cached rather than evaluated at import: it imports
    each module's ``manifest`` submodule, and nothing should pay that cost — or
    inherit that failure mode — merely by importing this module.  A module that
    declares no ``short_code`` is a hard error, not a silent omission: silently
    skipping it is how a schema goes unprovisioned.
    """

    contracts: list[ModuleSchemaContract] = []
    for import_name in composed_lineage_import_names():
        try:
            manifest = importlib.import_module(f"{import_name}.manifest").module
        except Exception as error:  # noqa: BLE001 - re-raised with the cause named
            raise RuntimeError(
                f"composed lineage {import_name!r} declares no importable "
                f"{import_name}.manifest; the module schema set cannot be "
                "derived, so the deployment prerequisite contract is unknown."
            ) from error
        short_code = getattr(manifest, "short_code", None)
        if not short_code:
            raise RuntimeError(
                f"composed module {import_name!r} declares no short_code on its "
                "ModuleManifest; its mod_* schema name cannot be derived."
            )
        contracts.append(
            ModuleSchemaContract(
                module=manifest.code,
                distribution=import_name.replace("_", "-"),
                import_name=import_name,
                schema=module_schema(short_code),
            )
        )
    return tuple(sorted(contracts, key=lambda contract: contract.schema))


@cache
def module_schemas() -> frozenset[str]:
    """Just the derived ``mod_*`` names."""

    return frozenset(contract.schema for contract in module_schema_contract())


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
    """Describe module schema drift without mutating the database."""

    violations: list[str] = []
    for expected in module_schema_contract():
        actual = observed.get(expected.schema)
        if actual is None:
            violations.append(f"schema {expected.schema!r} is missing")
            continue
        if actual.owner_role != expected.owner_role:
            violations.append(
                f"schema {expected.schema!r} is owned by "
                f"{actual.owner_role!r}; expected {expected.owner_role!r}"
            )
        if not actual.probe_observed:
            # Without this the whole PUBLIC-denial assertion passes vacuously
            # whenever the probe role is absent.
            violations.append(
                f"schema {expected.schema!r} PUBLIC denial is unproven: the "
                f"{PUBLIC_PROBE_ROLE!r} probe role is missing, so no "
                "forbidden-access check was performed"
            )
        elif actual.probe_privileges:
            violations.append(
                f"schema {expected.schema!r} is reachable by "
                f"{PUBLIC_PROBE_ROLE}: it effectively holds "
                f"{', '.join(actual.probe_privileges)}"
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
    "ALEMBIC_INI",
    "COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT",
    "MODULE_DATABASE_ROLE_CONTRACT",
    "PROBED_SCHEMA_PRIVILEGES",
    "PUBLIC_PROBE_ROLE",
    "SCHEMA_BOOTSTRAP_ROLE",
    "BillingRuntimeLoginObservation",
    "BillingRuntimeLoginQuery",
    "BillingRuntimeLoginRefusal",
    "BillingRuntimeLoginRefused",
    "DatabaseRoleContract",
    "ModuleSchemaContract",
    "ModuleSchemaObservation",
    "RolePosture",
    "commercial_bootstrap_role_violations",
    "commercial_schema_violations",
    "composed_lineage_import_names",
    "module_database_role_violations",
    "module_schema_contract",
    "module_schemas",
    "role_posture_violations",
    "require_billing_runtime_login",
]
