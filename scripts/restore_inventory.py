"""Metadata-only restore comparison foundation, not a Gate0 acceptance adapter.

Run the SAME query on source and scratch. This module neither proves staging
provenance nor binds a capture to a dump snapshot; callers must establish those
separately. No connection, subprocess, dump, credential or PASS emitter lives here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from itertools import product
from typing import Protocol, TypeVar

SCHEMA = "dotmac_sub.restore_metadata.v1"
SCHEMA_PRIVILEGES = ("USAGE", "CREATE")
TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)

# Full effective matrices include both planes and inherited/PUBLIC privileges.
# The source supplies actual allowed/denied observations, never universal denials.
INVENTORY_SQL = """
WITH roles AS (
 SELECT rolname, rolcanlogin, rolbypassrls, rolsuper FROM pg_roles
 WHERE rolname !~ '^pg_'
), schemas AS (
 SELECT oid, nspname, pg_get_userbyid(nspowner) AS owner FROM pg_namespace
 WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
), tables AS (
 SELECT c.oid, n.nspname, c.relname, pg_get_userbyid(c.relowner) AS owner,
        c.relrowsecurity, c.relforcerowsecurity
 FROM pg_class c JOIN schemas n ON n.oid=c.relnamespace
 WHERE c.relkind IN ('r','p','f')
), privileges AS (
 SELECT jsonb_build_array(r.rolname,n.nspname,NULL,p,
        has_schema_privilege(r.rolname,n.oid,p)) AS entry
 FROM roles r CROSS JOIN schemas n
 CROSS JOIN unnest(ARRAY['USAGE','CREATE']) p
 UNION ALL
 SELECT jsonb_build_array(r.rolname,t.nspname,t.relname,p,
        has_table_privilege(r.rolname,t.oid,p))
 FROM roles r CROSS JOIN tables t
 CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                         'REFERENCES','TRIGGER']) p
)
SELECT jsonb_build_object(
 'schema','dotmac_sub.restore_metadata.v1',
 'roles',(SELECT jsonb_agg(jsonb_build_array(rolname,rolcanlogin,rolbypassrls,rolsuper)) FROM roles),
 'schemas',(SELECT jsonb_agg(jsonb_build_array(nspname,owner)) FROM schemas),
 'tables',(SELECT jsonb_agg(jsonb_build_array(nspname,relname,owner,relrowsecurity,relforcerowsecurity)) FROM tables),
 'policies',COALESCE((SELECT jsonb_agg(jsonb_build_array(t.nspname,t.relname,p.polname))
     FROM pg_policy p JOIN tables t ON t.oid=p.polrelid),'[]'::jsonb),
 'heads',(SELECT jsonb_agg(version_num) FROM public.alembic_version),
 'privileges',(SELECT jsonb_agg(entry) FROM privileges)
);
"""


@dataclass(frozen=True)
class Role:
    name: str
    can_login: bool
    bypass_rls: bool
    superuser: bool


@dataclass(frozen=True)
class Schema:
    name: str
    owner: str


@dataclass(frozen=True)
class Table:
    schema: str
    name: str
    owner: str
    rls_enabled: bool
    rls_forced: bool


@dataclass(frozen=True)
class Policy:
    schema: str
    table: str
    name: str


@dataclass(frozen=True)
class Privilege:
    role: str
    schema: str
    table: str | None
    privilege: str
    allowed: bool


@dataclass(frozen=True)
class Inventory:
    roles: tuple[Role, ...]
    schemas: tuple[Schema, ...]
    tables: tuple[Table, ...]
    policies: tuple[Policy, ...]
    heads: tuple[str, ...]
    privileges: tuple[Privilege, ...]


class MetadataReader(Protocol):
    def scalar(self, sql_text: str) -> str: ...


T = TypeVar("T")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("duplicate metadata field")
    return dict(pairs)


def _records(
    constructor: Callable[..., T],
    raw: object,
    shape: tuple[type | tuple[type, ...], ...],
) -> tuple[T, ...]:
    if not isinstance(raw, list):
        raise ValueError("invalid metadata collection")
    rows: list[T] = []
    for row in raw:
        if not isinstance(row, list) or len(row) != len(shape):
            raise ValueError("invalid metadata record")
        if any(
            type(v) not in (s if isinstance(s, tuple) else (s,))
            or (isinstance(v, str) and not v)
            for v, s in zip(row, shape, strict=True)
        ):
            raise ValueError("invalid metadata value")
        rows.append(constructor(*row))
    if len(set(rows)) != len(rows):
        raise ValueError("duplicate metadata record")
    return tuple(sorted(rows, key=repr))


def parse_inventory(raw: str) -> Inventory:
    """Strictly decode catalog output, rejecting incomplete matrix coverage."""
    try:
        document: object = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError):
        raise ValueError("invalid metadata JSON") from None
    keys = {"schema", "roles", "schemas", "tables", "policies", "heads", "privileges"}
    if (
        not isinstance(document, dict)
        or set(document) != keys
        or document["schema"] != SCHEMA
    ):
        raise ValueError("unsupported metadata shape")
    roles = _records(Role, document["roles"], (str, bool, bool, bool))
    schemas = _records(Schema, document["schemas"], (str, str))
    tables = _records(Table, document["tables"], (str, str, str, bool, bool))
    policies = _records(Policy, document["policies"], (str, str, str))
    heads = _records(
        str,
        [[v] for v in document["heads"]]
        if isinstance(document["heads"], list)
        else None,
        (str,),
    )
    privileges = _records(
        Privilege, document["privileges"], (str, str, (str, type(None)), str, bool)
    )
    role_names, schema_names = {r.name for r in roles}, {s.name for s in schemas}
    table_names = {(t.schema, t.name) for t in tables}
    if (
        not roles
        or not schemas
        or not tables
        or not heads
        or len(role_names) != len(roles)
        or len(schema_names) != len(schemas)
        or len(table_names) != len(tables)
        or any(
            s.owner not in role_names and not s.owner.startswith("pg_") for s in schemas
        )
        or any(
            t.schema not in schema_names
            or (t.owner not in role_names and not t.owner.startswith("pg_"))
            for t in tables
        )
        or any((p.schema, p.table) not in table_names for p in policies)
    ):
        raise ValueError("incomplete metadata scope")
    expected: set[tuple[str, str, str | None, str]] = {
        (r, s, None, p)
        for r, s, p in product(role_names, schema_names, SCHEMA_PRIVILEGES)
    }
    expected.update(
        (r, s, t, p)
        for r, (s, t), p in product(role_names, table_names, TABLE_PRIVILEGES)
    )
    observed = {(p.role, p.schema, p.table, p.privilege) for p in privileges}
    if observed != expected or len(observed) != len(privileges):
        raise ValueError("incomplete or unsupported effective privilege matrix")
    return Inventory(roles, schemas, tables, policies, heads, privileges)


def collect_inventory(reader: MetadataReader) -> Inventory:
    return parse_inventory(reader.scalar(INVENTORY_SQL))


def first_divergence(source: Inventory, restored: Inventory) -> str | None:
    """Return only a fixed category, never catalog values or customer rows."""
    for category in ("roles", "schemas", "tables", "policies", "heads", "privileges"):
        if getattr(source, category) != getattr(restored, category):
            return category
    return None
