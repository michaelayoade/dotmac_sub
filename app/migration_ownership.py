"""Migrator identity checks and the in-transaction ownership hand-back.

Migrations run as ``app_admin`` (``module_database_roles.v1``: LOGIN, BYPASSRLS,
not superuser). The runtime owner of every Sub object is ``dotmac_app``. PostgreSQL
makes ``current_user`` the owner of whatever a migration creates, so without a
hand-back each new table, view or function would belong to the BYPASSRLS
migrator: the runtime would get no access to it, and a new view or SECURITY
DEFINER function would read FORCE-RLS tables with the migrator's bypass.

``alembic/env.py`` therefore snapshots what the migrator owns, runs the
migrations, and hands every NEW object back to the owner inside the same
transaction. The planning here is pure (no database) so it is unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

OWNER_ENV_VAR: Final = "MIGRATION_OBJECT_OWNER"
CONTRACT_NAME: Final = "module_database_roles.v1"

#: Facts about every object the current role owns in this database. Classification
#: happens in Python (`plan_hand_back`); SQL only reports.
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
       c.relpersistence = 't' AS temporary
  FROM owned o JOIN pg_class c ON o.classid = 'pg_class'::regclass AND c.oid = o.objid
UNION ALL
SELECT 'pg_type', t.oid::bigint, t.typtype::text,
       t.oid::regtype::text, t.oid::regtype::text, false,
       (t.typtype IN ('c', 'm') OR t.typcategory = 'A'),
       EXISTS (SELECT 1 FROM ext e WHERE e.classid = 'pg_type'::regclass
                                     AND e.objid = t.oid),
       false
  FROM owned o JOIN pg_type t ON o.classid = 'pg_type'::regclass AND t.oid = o.objid
UNION ALL
SELECT 'pg_proc', p.oid::bigint, p.prokind::text,
       p.oid::regprocedure::text,
       quote_ident(n.nspname) || '.' || quote_ident(p.proname)
           || '(' || oidvectortypes(p.proargtypes) || ')',
       p.prosecdef, false,
       EXISTS (SELECT 1 FROM ext e WHERE e.classid = 'pg_proc'::regclass
                                     AND e.objid = p.oid),
       false
  FROM owned o JOIN pg_proc p ON o.classid = 'pg_proc'::regclass AND p.oid = o.objid
  JOIN pg_namespace n ON n.oid = p.pronamespace
UNION ALL
SELECT o.classid::regclass::text, o.objid::bigint, '',
       o.classid::regclass::text || ':' || o.objid::text,
       o.classid::regclass::text || ':' || o.objid::text,
       false, false, false, false
  FROM owned o
 WHERE o.classid NOT IN ('pg_class'::regclass, 'pg_type'::regclass,
                         'pg_proc'::regclass)
"""

_RELATION_KINDS: Final = {
    "r": "TABLE",
    "p": "TABLE",
    "v": "VIEW",
    "m": "MATERIALIZED VIEW",
    "f": "FOREIGN TABLE",
}


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

    @property
    def key(self) -> tuple[str, int]:
        return (self.catalog, self.oid)


@dataclass(frozen=True, slots=True)
class HandBackPlan:
    statements: tuple[str, ...]
    refusals: tuple[str, ...]


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plan_hand_back(
    new_objects: Iterable[OwnedObject],
    *,
    owner: str,
    definer_allowlist: frozenset[str],
) -> HandBackPlan:
    """Decide how to return each NEW object to ``owner``, or refuse it.

    Objects that follow their parent (indexes, TOAST tables, owned sequences,
    row and array types) are left to the parent's ``ALTER ... OWNER``. A
    SECURITY DEFINER function is refused unless allowlisted; every kind this
    does not know how to hand back is refused rather than skipped.
    """

    target = quote_ident(owner)
    statements: list[str] = []
    refusals: list[str] = []
    ordered = sorted(new_objects, key=lambda o: (o.catalog, o.ident))
    for obj in ordered:
        if obj.extension_member or obj.temporary or obj.follows_parent:
            continue
        if obj.catalog == "pg_class":
            if obj.kind in _RELATION_KINDS:
                statements.append(
                    f"ALTER {_RELATION_KINDS[obj.kind]} {obj.ident} OWNER TO {target}"
                )
            elif obj.kind == "S":
                statements.append(f"ALTER SEQUENCE {obj.ident} OWNER TO {target}")
            else:
                refusals.append(f"{obj.kind!r} relation {obj.ident}")
        elif obj.catalog == "pg_type":
            if obj.kind in {"e", "r"}:
                statements.append(f"ALTER TYPE {obj.ident} OWNER TO {target}")
            elif obj.kind == "d":
                statements.append(f"ALTER DOMAIN {obj.ident} OWNER TO {target}")
            else:
                refusals.append(f"{obj.kind!r} type {obj.ident}")
        elif obj.catalog == "pg_proc":
            if obj.secdef:
                if obj.signature not in definer_allowlist:
                    refusals.append(
                        f"SECURITY DEFINER function {obj.signature} is not in the "
                        "definer allowlist"
                    )
                continue
            if obj.kind in {"f", "p"}:
                statements.append(f"ALTER ROUTINE {obj.ident} OWNER TO {target}")
            else:
                refusals.append(f"{obj.kind!r} routine {obj.signature}")
        else:
            refusals.append(f"{obj.catalog} object {obj.ident}")
    # Sorted by (catalog, identifier) so the emitted script is stable to review.
    return HandBackPlan(tuple(statements), tuple(refusals))


def leftovers(
    before: Iterable[OwnedObject],
    after: Iterable[OwnedObject],
    *,
    definer_allowlist: frozenset[str],
) -> tuple[str, ...]:
    """Objects still owned by the migrator that were not owned before and are
    not allowlisted definers."""

    known = {obj.key for obj in before}
    result: list[str] = []
    for obj in after:
        if obj.key in known or obj.extension_member or obj.temporary:
            continue
        if (
            obj.catalog == "pg_proc"
            and obj.secdef
            and (obj.signature in definer_allowlist)
        ):
            continue
        result.append(f"{obj.catalog} {obj.ident}")
    return tuple(sorted(result))


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
    )


def snapshot_owned_objects(connection: Any) -> tuple[OwnedObject, ...]:
    from sqlalchemy import text

    return tuple(
        _row_to_object(row) for row in connection.execute(text(OWNED_OBJECTS_SQL))
    )


def assert_migrator_identity(connection: Any, *, owner: str) -> None:
    from sqlalchemy import text

    head = connection.execute(text(MIGRATOR_POSTURE_SQL), {"owner": owner}).one()
    owner_exists = bool(head[1])
    inherits = can_set = reaches = False
    if owner_exists and head[0] != owner:
        edge = connection.execute(
            text(
                "SELECT pg_has_role(current_user, :o, 'USAGE'), "
                "pg_has_role(current_user, :o, 'SET'), "
                "pg_has_role(:o, current_user, 'MEMBER')"
            ),
            {"o": owner},
        ).one()
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


def hand_back_new_objects(
    connection: Any,
    *,
    owner: str,
    before: Iterable[OwnedObject],
    definer_allowlist: frozenset[str],
) -> int:
    """Hand everything created since ``before`` to ``owner`` and prove it.

    Runs inside the caller's transaction. Raises (rolling the whole run back)
    on a refused object or if anything unexpected is still migrator-owned.
    Returns the number of ``ALTER`` statements issued.
    """

    before = tuple(before)
    known = {obj.key for obj in before}
    after = snapshot_owned_objects(connection)
    plan = plan_hand_back(
        (obj for obj in after if obj.key not in known),
        owner=owner,
        definer_allowlist=definer_allowlist,
    )
    if plan.refusals:
        raise MigrationOwnershipError(
            "migration created objects that cannot be handed back to "
            f"{owner}: " + "; ".join(plan.refusals)
        )
    for statement in plan.statements:
        connection.exec_driver_sql(statement)
    remaining = leftovers(
        before,
        snapshot_owned_objects(connection),
        definer_allowlist=definer_allowlist,
    )
    if remaining:
        raise MigrationOwnershipError(
            f"objects still owned by the migrator after hand-back to {owner}: "
            + "; ".join(remaining)
        )
    return len(plan.statements)


__all__ = [
    "CONTRACT_NAME",
    "OWNER_ENV_VAR",
    "HandBackPlan",
    "MigrationOwnershipError",
    "MigratorPosture",
    "OwnedObject",
    "assert_migrator_identity",
    "hand_back_new_objects",
    "leftovers",
    "load_definer_allowlist",
    "migrator_violations",
    "plan_hand_back",
    "snapshot_owned_objects",
]
