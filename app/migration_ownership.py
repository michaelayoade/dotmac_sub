"""Migrator identity checks and the in-transaction runtime grant step.

Migrations run as ``app_admin`` (``module_database_roles.v1``: LOGIN, BYPASSRLS,
not superuser). PostgreSQL makes ``current_user`` the owner of whatever a
migration creates, so every NEW object belongs to ``app_admin``; the runtime
role (``dotmac_app``) never owns new objects and holds no ``CREATE`` on
``public``. This is the model Starter and ERP already use: the migrator owns,
the runtime gets explicit DML grants.

``alembic/env.py`` snapshots what the migrator owns, runs the migrations, and
grants the runtime role exactly what it needs on every NEW object inside the
same transaction: tables and views get SELECT/INSERT/UPDATE/DELETE, sequences
get USAGE/SELECT/UPDATE. Types and ordinary functions need nothing (PUBLIC
holds USAGE/EXECUTE). It refuses what it cannot make safe: SECURITY DEFINER
functions off the allowlist, materialized views, views without
``security_invoker=true`` (an app_admin-owned view would read RLS tables with
the owner's BYPASSRLS), and every kind it does not know. The planning here is
pure (no database) so it is unit-testable.

Objects that ``dotmac_app`` already owns stay as they are (no REASSIGN OWNED);
the migrator reaches them through its inherited membership.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

#: The runtime role that receives grants on new objects (and is the owner the
#: migrator inherits from for pre-existing objects).
RUNTIME_ROLE_ENV_VAR: Final = "MIGRATION_RUNTIME_ROLE"
#: Pre-grant-model name of the same setting (it used to name the hand-back
#: owner). Still read so an older deploy script keeps working; the meaning is
#: now "grant to this role", never "transfer ownership to it".
LEGACY_OWNER_ENV_VAR: Final = "MIGRATION_OBJECT_OWNER"
OWNER_ENV_VAR: Final = LEGACY_OWNER_ENV_VAR
CONTRACT_NAME: Final = "module_database_roles.v1"

#: Facts about every object the current role owns in this database. Classification
#: happens in Python (`plan_runtime_grants`); SQL only reports.
OWNED_OBJECTS_SQL: Final = """
WITH owned AS (
    SELECT d.classid, d.objid
      FROM pg_shdepend d
     WHERE d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
       AND d.deptype = 'o'
       AND d.refclassid = 'pg_authid'::regclass
       AND d.refobjid = (SELECT oid FROM pg_roles WHERE rolname = current_user)
), ext AS (
    SELECT classid, objid FROM pg_depend WHERE deptype = 'e'
)
SELECT 'pg_class' AS catalog, c.oid::bigint AS oid, c.relkind::text AS kind,
       c.oid::regclass::text AS ident, c.oid::regclass::text AS signature,
       false AS secdef,
       (c.relkind IN ('i', 'I', 't')
        OR (c.relkind = 'S' AND EXISTS (
              SELECT 1 FROM pg_depend x
               WHERE x.classid = 'pg_class'::regclass AND x.objid = c.oid
                 AND x.deptype IN ('a', 'i')))) AS follows_parent,
       EXISTS (SELECT 1 FROM ext e WHERE e.classid = 'pg_class'::regclass
                                     AND e.objid = c.oid) AS extension_member,
       c.relpersistence = 't' AS temporary,
       COALESCE(EXISTS (
           SELECT 1 FROM unnest(c.reloptions) AS opt
            WHERE lower(opt) ~ '^security_invoker=(t|tr|tru|true|y|ye|yes|on|1)$'
       ), false) AS security_invoker
  FROM owned o JOIN pg_class c ON o.classid = 'pg_class'::regclass AND c.oid = o.objid
UNION ALL
SELECT 'pg_type', t.oid::bigint, t.typtype::text,
       t.oid::regtype::text, t.oid::regtype::text, false,
       (t.typtype IN ('c', 'm') OR t.typcategory = 'A'),
       EXISTS (SELECT 1 FROM ext e WHERE e.classid = 'pg_type'::regclass
                                     AND e.objid = t.oid),
       false, false
  FROM owned o JOIN pg_type t ON o.classid = 'pg_type'::regclass AND t.oid = o.objid
UNION ALL
SELECT 'pg_proc', p.oid::bigint, p.prokind::text,
       p.oid::regprocedure::text,
       quote_ident(n.nspname) || '.' || quote_ident(p.proname)
           || '(' || oidvectortypes(p.proargtypes) || ')',
       p.prosecdef, false,
       EXISTS (SELECT 1 FROM ext e WHERE e.classid = 'pg_proc'::regclass
                                     AND e.objid = p.oid),
       false, false
  FROM owned o JOIN pg_proc p ON o.classid = 'pg_proc'::regclass AND p.oid = o.objid
  JOIN pg_namespace n ON n.oid = p.pronamespace
UNION ALL
SELECT o.classid::regclass::text, o.objid::bigint, '',
       o.classid::regclass::text || ':' || o.objid::text,
       o.classid::regclass::text || ':' || o.objid::text,
       false, false, false, false, false
  FROM owned o
 WHERE o.classid NOT IN ('pg_class'::regclass, 'pg_type'::regclass,
                         'pg_proc'::regclass)
"""

#: Relation kinds that receive table DML. Views are granted only when they are
#: security_invoker (see `plan_runtime_grants`).
_TABLE_KINDS: Final = frozenset({"r", "p"})
TABLE_PRIVILEGES: Final = ("SELECT", "INSERT", "UPDATE", "DELETE")
SEQUENCE_PRIVILEGES: Final = ("USAGE", "SELECT", "UPDATE")
READ_ONLY_PRIVILEGES: Final = ("SELECT",)


class MigrationOwnershipError(RuntimeError):
    """The migrator identity or a migration's new objects violate the contract."""


@dataclass(frozen=True, slots=True)
class OwnedObject:
    catalog: str
    oid: int
    kind: str
    ident: str
    signature: str
    secdef: bool = False
    follows_parent: bool = False
    extension_member: bool = False
    temporary: bool = False
    security_invoker: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return (self.catalog, self.oid)


@dataclass(frozen=True, slots=True)
class GrantItem:
    """One GRANT the runtime role needs, with the privileges to prove after."""

    oid: int
    ident: str
    is_sequence: bool
    privileges: tuple[str, ...]

    def statement(self, role: str) -> str:
        kind = "SEQUENCE" if self.is_sequence else "TABLE"
        return (
            f"GRANT {', '.join(self.privileges)} ON {kind} {self.ident} "
            f"TO {quote_ident(role)}"
        )


@dataclass(frozen=True, slots=True)
class GrantPlan:
    items: tuple[GrantItem, ...]
    refusals: tuple[str, ...]

    def statements(self, role: str) -> tuple[str, ...]:
        return tuple(item.statement(role) for item in self.items)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plan_runtime_grants(
    new_objects: Iterable[OwnedObject],
    *,
    definer_allowlist: frozenset[str],
    table_privileges: tuple[str, ...] = TABLE_PRIVILEGES,
) -> GrantPlan:
    """Decide what the runtime role is granted on each migrator-owned object, or
    refuse it.

    Tables and security_invoker views get ``table_privileges``; sequences
    (standalone and column-owned alike) get USAGE/SELECT/UPDATE. Indexes, TOAST
    tables, row and array types follow their parent. Enum/domain/range types and
    ordinary functions need no grant. Refused rather than skipped: a SECURITY
    DEFINER function off the allowlist, a view that is not security_invoker, a
    materialized view, and every kind not named here.
    """

    items: list[GrantItem] = []
    refusals: list[str] = []
    ordered = sorted(new_objects, key=lambda o: (o.catalog, o.ident))
    for obj in ordered:
        if obj.extension_member or obj.temporary:
            continue
        if obj.catalog == "pg_class":
            if obj.kind == "S":
                items.append(GrantItem(obj.oid, obj.ident, True, SEQUENCE_PRIVILEGES))
            elif obj.follows_parent:
                continue
            elif obj.kind in _TABLE_KINDS:
                items.append(GrantItem(obj.oid, obj.ident, False, table_privileges))
            elif obj.kind == "v":
                if obj.security_invoker:
                    items.append(GrantItem(obj.oid, obj.ident, False, table_privileges))
                else:
                    refusals.append(
                        f"view {obj.ident} is not security_invoker=true; an "
                        "app_admin-owned view would read row-level-security "
                        "tables with the owner's BYPASSRLS"
                    )
            elif obj.kind == "m":
                refusals.append(f"materialized view {obj.ident}")
            else:
                refusals.append(f"{obj.kind!r} relation {obj.ident}")
        elif obj.follows_parent:
            continue
        elif obj.catalog == "pg_type":
            if obj.kind not in {"e", "r", "d"}:
                refusals.append(f"{obj.kind!r} type {obj.ident}")
        elif obj.catalog == "pg_proc":
            if obj.secdef:
                if obj.signature not in definer_allowlist:
                    refusals.append(
                        f"SECURITY DEFINER function {obj.signature} is not in the "
                        "definer allowlist"
                    )
            elif obj.kind not in {"f", "p"}:
                refusals.append(f"{obj.kind!r} routine {obj.signature}")
        else:
            refusals.append(f"{obj.catalog} object {obj.ident}")
    # Sorted by (catalog, identifier) so the emitted script is stable to review.
    return GrantPlan(tuple(items), tuple(refusals))


def load_definer_allowlist(path: Path) -> frozenset[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    )


@dataclass(frozen=True, slots=True)
class MigratorPosture:
    current_user: str
    owner_exists: bool
    bypassrls: bool
    superuser: bool
    inherits_owner: bool
    can_set_owner: bool
    owner_reaches_migrator: bool


def migrator_violations(posture: MigratorPosture, *, owner: str) -> tuple[str, ...]:
    """Name every reason this connection is not the contract's migrator."""

    me = posture.current_user
    violations: list[str] = []
    if me == owner:
        violations.append(
            f"connected as the object owner {owner!r}; migrations must run as "
            "the BYPASSRLS migrator, not the runtime owner"
        )
    if not posture.owner_exists:
        violations.append(f"object owner role {owner!r} does not exist")
        return tuple(violations)
    if posture.superuser:
        violations.append(f"{me} is a superuser; the migrator must not be")
    if not posture.bypassrls:
        violations.append(f"{me} lacks BYPASSRLS; backfill migrations need it")
    if me != owner:
        if not posture.inherits_owner:
            violations.append(f"{me} is not a member of {owner} with INHERIT")
        if not posture.can_set_owner:
            violations.append(f"{me} cannot SET ROLE {owner}")
        if posture.owner_reaches_migrator:
            violations.append(
                f"{owner} is a member of {me}; the runtime owner must not reach "
                "the migrator"
            )
    return tuple(violations)


MIGRATOR_POSTURE_SQL: Final = """
SELECT current_user::text,
       EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :owner),
       (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user),
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
"""


def _row_to_object(row: Sequence[Any]) -> OwnedObject:
    return OwnedObject(
        catalog=str(row[0]),
        oid=int(row[1]),
        kind=str(row[2]),
        ident=str(row[3]),
        signature=str(row[4]),
        secdef=bool(row[5]),
        follows_parent=bool(row[6]),
        extension_member=bool(row[7]),
        temporary=bool(row[8]),
        security_invoker=bool(row[9]),
    )


_NAMED_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _query(connection: Any, sql: str, params: Mapping[str, Any] | None = None) -> list:
    """Run a read on either a SQLAlchemy connection (Alembic) or a psycopg one
    (the pre-deploy identity check), so neither needs its own engine."""
    if hasattr(connection, "exec_driver_sql"):
        from sqlalchemy import text

        return list(connection.execute(text(sql), params or {}))
    if params:
        sql = _NAMED_PARAM.sub(r"%(\1)s", sql)
    return list(connection.execute(sql, params).fetchall())


def _statement(connection: Any, sql: str) -> None:
    if hasattr(connection, "exec_driver_sql"):
        connection.exec_driver_sql(sql)
    else:
        connection.execute(sql)


def snapshot_owned_objects(connection: Any) -> tuple[OwnedObject, ...]:
    return tuple(_row_to_object(row) for row in _query(connection, OWNED_OBJECTS_SQL))


def assert_migrator_identity(connection: Any, *, owner: str) -> None:
    head = _query(connection, MIGRATOR_POSTURE_SQL, {"owner": owner})[0]
    owner_exists = bool(head[1])
    inherits = can_set = reaches = False
    if owner_exists and head[0] != owner:
        edge = _query(
            connection,
            "SELECT pg_has_role(current_user, :o, 'USAGE'), "
            "pg_has_role(current_user, :o, 'SET'), "
            "pg_has_role(:o, current_user, 'MEMBER')",
            {"o": owner},
        )[0]
        inherits, can_set, reaches = bool(edge[0]), bool(edge[1]), bool(edge[2])
    posture = MigratorPosture(
        current_user=str(head[0]),
        owner_exists=owner_exists,
        bypassrls=bool(head[2]),
        superuser=bool(head[3]),
        inherits_owner=inherits,
        can_set_owner=can_set,
        owner_reaches_migrator=reaches,
    )
    problems = migrator_violations(posture, owner=owner)
    if problems:
        raise MigrationOwnershipError(
            f"migration identity does not satisfy {CONTRACT_NAME}: "
            + "; ".join(problems)
        )


def runtime_role_from_environ(environ: Mapping[str, str]) -> str:
    """The runtime role to grant to, or ``""`` when migrations run unscoped.

    ``MIGRATION_RUNTIME_ROLE`` is canonical; ``MIGRATION_OBJECT_OWNER`` is the
    pre-grant-model spelling. Setting both to different roles is ambiguous and
    refused.
    """

    new = environ.get(RUNTIME_ROLE_ENV_VAR, "").strip()
    legacy = environ.get(LEGACY_OWNER_ENV_VAR, "").strip()
    if new and legacy and new != legacy:
        raise MigrationOwnershipError(
            f"{RUNTIME_ROLE_ENV_VAR}={new!r} conflicts with "
            f"{LEGACY_OWNER_ENV_VAR}={legacy!r}; set only {RUNTIME_ROLE_ENV_VAR}"
        )
    return new or legacy


def unowned_bypass_violation(
    *, current_user: str, bypassrls: bool, superuser: bool
) -> str | None:
    """A BYPASSRLS non-superuser migrating without a runtime role would leave
    new tables the runtime cannot reach and new views that read RLS tables with
    the migrator's bypass."""

    if bypassrls and not superuser:
        return (
            f"{current_user} is a BYPASSRLS non-superuser and {RUNTIME_ROLE_ENV_VAR} "
            f"is not set; {CONTRACT_NAME} migrations must grant new objects to the "
            "runtime role (set the variable, or migrate as the owner)"
        )
    return None


def assert_owner_variable_set_for_bypass(connection: Any) -> None:
    row = _query(
        connection,
        "SELECT current_user::text, rolbypassrls, rolsuper "
        "FROM pg_roles WHERE rolname = current_user",
    )
    if not row:
        return
    message = unowned_bypass_violation(
        current_user=str(row[0][0]),
        bypassrls=bool(row[0][1]),
        superuser=bool(row[0][2]),
    )
    if message:
        raise MigrationOwnershipError(message)


_MISSING_TABLE_PRIVILEGES_SQL: Final = """
SELECT p FROM unnest(CAST(:privs AS text[])) AS p
 WHERE NOT has_table_privilege(CAST(:role AS text), CAST(:oid AS oid), p)
"""
_MISSING_SEQUENCE_PRIVILEGES_SQL: Final = """
SELECT p FROM unnest(CAST(:privs AS text[])) AS p
 WHERE NOT has_sequence_privilege(CAST(:role AS text), CAST(:oid AS oid), p)
"""


def _privilege_gaps(connection: Any, *, role: str, item: GrantItem) -> list[str]:
    sql = (
        _MISSING_SEQUENCE_PRIVILEGES_SQL
        if item.is_sequence
        else _MISSING_TABLE_PRIVILEGES_SQL
    )
    rows = _query(
        connection,
        sql,
        {"privs": list(item.privileges), "role": role, "oid": item.oid},
    )
    return [str(row[0]) for row in rows]


def missing_grants(
    connection: Any, *, role: str, items: Iterable[GrantItem]
) -> tuple[str, ...]:
    """Prove ``role`` holds every planned privilege (catalog check, not trust)."""

    return tuple(
        f"{item.ident}: {role} lacks {priv}"
        for item in items
        for priv in _privilege_gaps(connection, role=role, item=item)
    )


#: The runtime reads the revision; only the migrator writes it.
_READ_ONLY_TABLES: Final = frozenset({"alembic_version", "public.alembic_version"})


def grant_runtime_privileges(
    connection: Any,
    *,
    runtime_role: str,
    definer_allowlist: frozenset[str],
) -> int:
    """Make ``runtime_role`` fully granted on everything the migrator owns.

    Runs inside the caller's transaction. The refusal checks run over EVERY
    migrator-owned object, not only the ones this run created, so a migration
    that weakens an existing one (a view losing ``security_invoker``, a
    function turned SECURITY DEFINER) is caught too. Grants are issued for
    every migrator-owned relation or sequence that still lacks a planned
    privilege, so a retry after a partial commit (``autocommit_block``,
    ``CONCURRENTLY``) still ends fully granted. Raises (rolling the run back)
    on a refusal or if a planned privilege is not held afterwards. Returns the
    number of ``GRANT`` statements issued.
    """

    plan = plan_runtime_grants(
        snapshot_owned_objects(connection),
        definer_allowlist=definer_allowlist,
    )
    if plan.refusals:
        raise MigrationOwnershipError(
            f"migrator-owned objects that cannot be granted to {runtime_role}: "
            + "; ".join(plan.refusals)
        )
    issued = 0
    for item in plan.items:
        if not item.is_sequence and item.ident in _READ_ONLY_TABLES:
            item = replace(item, privileges=READ_ONLY_PRIVILEGES)
        if _privilege_gaps(connection, role=runtime_role, item=item):
            _statement(connection, item.statement(runtime_role))
            issued += 1
            gaps = _privilege_gaps(connection, role=runtime_role, item=item)
            if gaps:
                raise MigrationOwnershipError(
                    f"runtime role {runtime_role} lacks planned privileges after "
                    f"the grant step: {item.ident}: " + ", ".join(gaps)
                )
    return issued


PUBLIC_POSTURE_SQL: Final = (
    "SELECT has_schema_privilege(current_user, 'public', 'CREATE'), "
    "has_schema_privilege(CAST(:role AS text), 'public', 'CREATE')"
)


def public_schema_findings(
    *, migrator_can_create: bool, runtime_can_create: bool, runtime_role: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(refusals, reports)`` for CREATE on ``public``.

    The migrator must be able to create there or every migration fails. The
    runtime role holding CREATE is legacy state: reported, never refused.
    """

    refusals = (
        ()
        if migrator_can_create
        else (
            "the migrator lacks CREATE on schema public; migrations cannot create "
            "objects (the runtime role must not be given it instead)",
        )
    )
    reports = (
        (
            f"{runtime_role} holds CREATE on schema public; the grant model does not need it",
        )
        if runtime_can_create
        else ()
    )
    return refusals, reports


def assert_public_schema_posture(
    connection: Any, *, runtime_role: str
) -> tuple[str, ...]:
    """Refuse if the migrator lacks CREATE on public; return reports otherwise."""

    row = _query(connection, PUBLIC_POSTURE_SQL, {"role": runtime_role})[0]
    refusals, reports = public_schema_findings(
        migrator_can_create=bool(row[0]),
        runtime_can_create=bool(row[1]),
        runtime_role=runtime_role,
    )
    if refusals:
        raise MigrationOwnershipError(
            f"migration identity does not satisfy {CONTRACT_NAME}: "
            + "; ".join(refusals)
        )
    return reports


__all__ = [
    "CONTRACT_NAME",
    "LEGACY_OWNER_ENV_VAR",
    "OWNER_ENV_VAR",
    "RUNTIME_ROLE_ENV_VAR",
    "GrantItem",
    "GrantPlan",
    "MigrationOwnershipError",
    "MigratorPosture",
    "OwnedObject",
    "assert_migrator_identity",
    "assert_owner_variable_set_for_bypass",
    "assert_public_schema_posture",
    "grant_runtime_privileges",
    "load_definer_allowlist",
    "migrator_violations",
    "missing_grants",
    "plan_runtime_grants",
    "public_schema_findings",
    "runtime_role_from_environ",
    "snapshot_owned_objects",
    "unowned_bypass_violation",
]
