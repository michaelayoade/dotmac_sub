"""Private attachment-privilege closure owned by the legacy archive fence.

The four immutable helper policies below are statically extracted from migration
557, never imported or executed. This module never rewrites a helper or grants
privileges. The future coordinator must prelock all 48 relations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from app.services.billing.legacy_archive_fence_contracts import ArchiveRelation
from app.services.billing.legacy_retained_columns_contracts import RetainedRelation
from app.services.domain_errors import DomainError


class ClosurePopulation(StrEnum):
    ARCHIVE = "archive"
    RETAINED = "retained"


@dataclass(frozen=True)
class RelayHelperPolicy:
    name: str
    arguments: str
    returns: str
    language: str
    body: str
    dispatcher: str


@dataclass(frozen=True)
class AttachmentGrant:
    relation: str
    relation_oid: int
    grantee_oid: int
    grantee: str | None
    privilege: str
    column: str | None
    grantable: bool
    grantor_oid: int


@dataclass(frozen=True)
class PrivilegeClosureObservation:
    population: ClosurePopulation
    owner_oid: int
    grants: tuple[AttachmentGrant, ...]
    catalog: tuple[str, ...]


class PrivilegeClosureRefused(DomainError):
    """The complete owning transaction must roll back."""


def _refuse(message: str) -> None:
    raise PrivilegeClosureRefused(
        code="billing_privilege_closure.refused", message=message
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


_RELAY_TABLES = ("outbox_events", "platform_outbox_events")
_RUNTIME_ROLES = (
    "app_user",
    "platform_api",
    "outbox_dispatcher",
    "platform_outbox_dispatcher",
)
_RELAY_HELPERS = (
    RelayHelperPolicy(
        "claim_outbox_batch",
        "p_worker text, p_batch integer, p_stale_seconds integer",
        "SETOF public.outbox_events",
        "sql",
        "\n            UPDATE public.outbox_events\n               SET status = 'claimed', leased_by = p_worker, leased_at = now()\n             WHERE id IN (\n               SELECT id FROM public.outbox_events\n                WHERE (status = 'pending' AND available_at <= now())\n                   OR (status = 'claimed'\n                       AND leased_at < now()\n                                       - make_interval(secs => p_stale_seconds))\n                ORDER BY available_at\n                FOR UPDATE SKIP LOCKED\n                LIMIT p_batch\n             )\n             RETURNING *;\n        ",
        "outbox_dispatcher",
    ),
    RelayHelperPolicy(
        "settle_outbox_event",
        "p_id uuid, p_worker text, p_status text, p_available_at timestamptz, p_attempts integer, p_last_error text",
        "boolean",
        "plpgsql",
        "\n        DECLARE n integer;\n        BEGIN\n            UPDATE public.outbox_events\n               SET status = p_status,\n                   attempts = p_attempts,\n                   last_error = p_last_error,\n                   available_at = COALESCE(p_available_at, available_at),\n                   sent_at = CASE WHEN p_status = 'sent' THEN now() ELSE sent_at END,\n                   leased_by = CASE WHEN p_status IN ('sent', 'dead')\n                                    THEN NULL ELSE leased_by END,\n                   leased_at = CASE WHEN p_status IN ('sent', 'dead')\n                                    THEN NULL ELSE leased_at END\n             WHERE id = p_id AND leased_by = p_worker AND status = 'claimed';\n            GET DIAGNOSTICS n = ROW_COUNT;\n            RETURN n = 1;\n        END\n        ",
        "outbox_dispatcher",
    ),
    RelayHelperPolicy(
        "claim_platform_outbox_batch",
        "p_worker text, p_batch integer, p_stale_seconds integer",
        "SETOF public.platform_outbox_events",
        "sql",
        "\n            UPDATE public.platform_outbox_events\n               SET status = 'claimed', leased_by = p_worker, leased_at = now()\n             WHERE id IN (\n               SELECT id FROM public.platform_outbox_events\n                WHERE (status = 'pending' AND available_at <= now())\n                   OR (status = 'claimed'\n                       AND leased_at < now()\n                                       - make_interval(secs => p_stale_seconds))\n                ORDER BY available_at\n                FOR UPDATE SKIP LOCKED\n                LIMIT p_batch\n             )\n             RETURNING *;\n        ",
        "platform_outbox_dispatcher",
    ),
    RelayHelperPolicy(
        "settle_platform_outbox_event",
        "p_id uuid, p_worker text, p_status text, p_available_at timestamptz, p_attempts integer, p_last_error text",
        "boolean",
        "plpgsql",
        "\n        DECLARE n integer;\n        BEGIN\n            UPDATE public.platform_outbox_events\n               SET status = p_status,\n                   attempts = p_attempts,\n                   last_error = p_last_error,\n                   available_at = COALESCE(p_available_at, available_at),\n                   sent_at = CASE WHEN p_status = 'sent' THEN now() ELSE sent_at END,\n                   leased_by = CASE WHEN p_status IN ('sent', 'dead')\n                                    THEN NULL ELSE leased_by END,\n                   leased_at = CASE WHEN p_status IN ('sent', 'dead')\n                                    THEN NULL ELSE leased_at END\n             WHERE id = p_id AND leased_by = p_worker AND status = 'claimed';\n            GET DIAGNOSTICS n = ROW_COUNT;\n            RETURN n = 1;\n        END\n        ",
        "platform_outbox_dispatcher",
    ),
)


def _catalog_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_catalog_value(item) for item in value]
    _refuse("Unsupported catalog value refuses canonical serialization.")
    raise AssertionError("unreachable")


def _catalog_rows(rows: list[RowMapping]) -> str:
    return json.dumps(
        [
            {str(key): _catalog_value(value) for key, value in row.items()}
            for row in rows
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _population(population: ClosurePopulation) -> tuple[str, ...]:
    if not isinstance(population, ClosurePopulation):
        _refuse("Closure requires its closed typed population.")
    protected = (
        ArchiveRelation if population is ClosurePopulation.ARCHIVE else RetainedRelation
    )
    return tuple(sorted((*[relation.value for relation in protected], *_RELAY_TABLES)))


def _rows(
    connection: Connection, sql: str, parameters: dict[str, object]
) -> list[RowMapping]:
    return list(connection.execute(text(sql), parameters).mappings())


def _lock_privilege_population(
    connection: Connection, *, population: ClosurePopulation
) -> None:
    # One statement: no mutation can precede acquisition of the entire set.
    connection.exec_driver_sql(
        "LOCK TABLE "
        + ", ".join("public." + _quote(name) for name in _population(population))
        + " IN ACCESS EXCLUSIVE MODE"
    )


def _roles(connection: Connection, owner_oid: int) -> tuple[str, ...]:
    rows = _rows(
        connection,
        """
        SELECT r.oid, r.rolname,
               r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR
                 r.rolcreaterole OR r.rolcreatedb OR r.rolreplication) AS safe,
               NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS no_membership,
               NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles p WHERE p.oid<>r.oid AND
                 (pg_catalog.pg_has_role(r.oid,p.oid,'MEMBER') OR
                  pg_catalog.pg_has_role(r.oid,p.oid,'USAGE') OR
                  pg_catalog.pg_has_role(r.oid,p.oid,'SET'))) AS no_paths,
               NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner=r.oid)
                 AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.proowner=r.oid)
                 AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspowner=r.oid)
                 AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba=r.oid) AS no_ownership,
               NOT pg_catalog.has_schema_privilege(r.oid,'public','CREATE')
                 AND NOT pg_catalog.has_database_privilege(r.oid,pg_catalog.current_database(),'CREATE') AS no_create
          FROM pg_catalog.pg_roles r WHERE r.rolname=ANY(:names) ORDER BY r.rolname
    """,
        {"names": list(_RUNTIME_ROLES)},
    )
    if len(rows) != len(_RUNTIME_ROLES) or any(
        row["oid"] == owner_oid
        or not all(
            row[key]
            for key in (
                "safe",
                "no_membership",
                "no_paths",
                "no_ownership",
                "no_create",
            )
        )
        for row in rows
    ):
        _refuse(
            "Actual online roles have unsafe attributes, ownership, membership or CREATE paths."
        )
    public = _rows(
        connection,
        """
        SELECT a.privilege_type FROM pg_catalog.pg_namespace n
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) a
        WHERE n.nspname='public' AND a.grantee=0 AND a.privilege_type='CREATE'
        UNION ALL
        SELECT a.privilege_type FROM pg_catalog.pg_database d
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(d.datacl,pg_catalog.acldefault('d',d.datdba))) a
        WHERE d.datname=pg_catalog.current_database() AND a.grantee=0 AND a.privilege_type='CREATE'
    """,
        {},
    )
    if public:
        _refuse("PUBLIC has schema or database CREATE capability.")
    # TEMP alone is not attachment capability; all helper relations are qualified.
    return tuple(f"role:{row['rolname']}:{row['oid']}" for row in rows)


def _helpers(connection: Connection) -> tuple[str, ...]:
    observed: list[str] = []
    for policy in _RELAY_HELPERS:
        rows = _rows(
            connection,
            """
            SELECT p.oid,p.proowner AS owner_oid,p.proargtypes::text AS argument_oids,
                   p.prorettype=pg_catalog.to_regtype(:return_type)::oid AS expected_return,p.prosrc,p.prosecdef,p.proconfig,p.proretset,
                   pg_catalog.pg_get_function_arguments(p.oid) AS arguments,
                   p.prorettype AS return_type_oid,
                   p.prokind,p.provolatile,p.proisstrict,p.proleakproof,p.proparallel,
                   l.lanname,o.rolname AS owner, o.rolsuper,o.rolbypassrls,
                   pg_catalog.has_table_privilege(o.oid,'public.outbox_events','SELECT') AS tenant_select,
                   pg_catalog.has_table_privilege(o.oid,'public.outbox_events','UPDATE') AS tenant_update,
                   pg_catalog.has_table_privilege(o.oid,'public.platform_outbox_events','SELECT') AS platform_select,
                   pg_catalog.has_table_privilege(o.oid,'public.platform_outbox_events','UPDATE') AS platform_update,
                   p.proargdefaults IS NULL AS no_defaults,
                   p.provariadic=0 AND p.prosupport=0 AS ordinary
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
              JOIN pg_catalog.pg_language l ON l.oid=p.prolang
              JOIN pg_catalog.pg_roles o ON o.oid=p.proowner
             WHERE n.nspname='public' AND p.proname=:name
        """,
            {
                "name": policy.name,
                "return_type": policy.returns.removeprefix("SETOF ")
                if policy.returns.startswith("SETOF ")
                else "pg_catalog.bool",
            },
        )
        if len(rows) != 1:
            _refuse("Relay helper missing or overloaded.")
        row = rows[0]
        arguments = policy.arguments.replace("timestamptz", "timestamp with time zone")
        # Return identity and set semantics are stable catalog facts. Deparsed
        # return names vary with search_path (including TEMP shadowing), so never
        # use their presentation as catalog identity or digest input.
        expected = {
            "prosrc": policy.body,
            "expected_return": True,
            "argument_oids": "25 23 23"
            if policy.returns.startswith("SETOF ")
            else "2950 25 25 1184 23 25",
            "prosecdef": True,
            "proconfig": ['search_path=""'],
            "arguments": arguments,
            "prokind": "f",
            "provolatile": "v",
            "proisstrict": False,
            "proleakproof": False,
            "proparallel": "u",
            "lanname": policy.language,
            "owner": "app_admin",
            "rolsuper": False,
            "rolbypassrls": True,
            "tenant_select": True,
            "tenant_update": True,
            "platform_select": True,
            "platform_update": True,
            "no_defaults": True,
            "ordinary": True,
            "proretset": policy.returns.startswith("SETOF "),
        }
        if any(row[key] != value for key, value in expected.items()):
            _refuse(
                "Relay helper source, signature, return, language, config or owner drift."
            )
        oid = cast(int, row["oid"])
        acl = _rows(
            connection,
            """
            SELECT r.rolname,a.grantee,a.grantor,a.privilege_type,a.is_grantable
              FROM pg_catalog.pg_proc p
              CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) a
              LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee
             WHERE p.oid=:oid ORDER BY a.grantee,a.grantor,a.privilege_type
        """,
            {"oid": oid},
        )
        if (
            len(acl) != 2
            or {a["rolname"] for a in acl} != {"app_admin", policy.dispatcher}
            or any(
                a["privilege_type"] != "EXECUTE"
                or a["is_grantable"]
                or a["grantor"] != row["owner_oid"]
                for a in acl
            )
        ):
            _refuse("Relay helper exact EXECUTE ACL drift.")
        for role in _RUNTIME_ROLES:
            effective = _rows(
                connection,
                "SELECT pg_catalog.has_function_privilege(:role,:oid,'EXECUTE') AS allowed",
                {"role": role, "oid": oid},
            )
            if effective[0]["allowed"] != (role == policy.dispatcher):
                _refuse("Relay helper effective online EXECUTE drift.")
        observed.append(
            f"helper:{policy.name}:{_catalog_rows(rows)}:{_catalog_rows(acl)}"
        )
    return tuple(observed)


def _attachment_grants(
    connection: Connection, name: str, oid: int, owner_oid: int
) -> tuple[AttachmentGrant, ...]:
    rows = _rows(
        connection,
        """
        SELECT a.grantee,r.rolname,a.grantor,a.privilege_type,a.is_grantable,NULL::text AS column_name
          FROM pg_catalog.pg_class c
          CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner))) a
          LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee
         WHERE c.oid=:oid AND a.grantee<>:owner AND a.privilege_type IN ('TRIGGER','REFERENCES')
        UNION ALL
        SELECT a.grantee,r.rolname,a.grantor,a.privilege_type,a.is_grantable,att.attname
          FROM pg_catalog.pg_attribute att
          CROSS JOIN LATERAL pg_catalog.aclexplode(att.attacl) a
          LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee
         WHERE att.attrelid=:oid AND att.attnum>0 AND NOT att.attisdropped
           AND a.grantee<>:owner AND a.privilege_type='REFERENCES'
         ORDER BY grantee,column_name,privilege_type,grantor,is_grantable
    """,
        {"oid": oid, "owner": owner_oid},
    )
    return tuple(
        AttachmentGrant(
            name,
            oid,
            cast(int, r["grantee"]),
            cast(str | None, r["rolname"]),
            cast(str, r["privilege_type"]),
            cast(str | None, r["column_name"]),
            cast(bool, r["is_grantable"]),
            cast(int, r["grantor"]),
        )
        for r in rows
    )


def _observe_privilege_closure(
    connection: Connection, *, population: ClosurePopulation, owner_oid: int
) -> PrivilegeClosureObservation:
    catalog = [*_roles(connection, owner_oid), *_helpers(connection)]
    grants: list[AttachmentGrant] = []
    for name in _population(population):
        rows = _rows(
            connection,
            """
            SELECT c.oid,c.relowner,c.relkind,c.relispartition,
                   n.nspowner=:owner AS schema_owned,
                   NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS ordinary,
                   NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class=c.oid) AS no_rules
              FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname=:name
        """,
            {"name": name, "owner": owner_oid},
        )
        if (
            len(rows) != 1
            or rows[0]["relowner"] != owner_oid
            or rows[0]["relkind"] != "r"
            or rows[0]["relispartition"]
            or not all(rows[0][k] for k in ("schema_owned", "ordinary", "no_rules"))
        ):
            _refuse("Protected relation owner, ordinary shape or rule drift.")
        oid = cast(int, rows[0]["oid"])
        grants.extend(_attachment_grants(connection, name, oid, owner_oid))
        acl = _rows(
            connection,
            """
            SELECT COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner))::text AS acl,
                   ARRAY(SELECT att.attname || ':' || COALESCE(att.attacl::text,'')
                     FROM pg_catalog.pg_attribute att WHERE att.attrelid=c.oid AND att.attnum>0
                       AND NOT att.attisdropped ORDER BY att.attnum) AS columns
              FROM pg_catalog.pg_class c WHERE c.oid=:oid
        """,
            {"oid": oid},
        )
        catalog.append(f"relation:{name}:{oid}:{owner_oid}:{_catalog_rows(acl)}")
        if name in _RELAY_TABLES:
            catalog.extend(_relay_topology(connection, name, oid))
    if any(grant.grantor_oid != owner_oid for grant in grants):
        _refuse(
            "Nonowner attachment grantor chain requires separate reviewed remediation; RESTRICT never cascades."
        )
    return PrivilegeClosureObservation(
        population, owner_oid, tuple(grants), tuple(catalog)
    )


def _close_attachment_privileges(
    connection: Connection, *, observation: PrivilegeClosureObservation
) -> None:
    if not isinstance(observation, PrivilegeClosureObservation):
        _refuse("Attachment closure requires its typed catalog observation.")
    for name in _population(observation.population):
        table = "public." + _quote(name)
        observed = tuple(g for g in observation.grants if g.relation == name)
        grantees = {"PUBLIC"}
        for grant in observed:
            if grant.grantee_oid and grant.grantee is None:
                _refuse("Unknown attachment ACL grantee.")
            grantees.add(
                "PUBLIC" if grant.grantee_oid == 0 else _quote(cast(str, grant.grantee))
            )
        for grantee in sorted(grantees):
            connection.exec_driver_sql(
                f"REVOKE TRIGGER, REFERENCES ON TABLE {table} FROM {grantee} RESTRICT"
            )
        for grant in observed:
            if grant.column is not None:
                grantee = (
                    "PUBLIC"
                    if grant.grantee_oid == 0
                    else _quote(cast(str, grant.grantee))
                )
                connection.exec_driver_sql(
                    f"REVOKE REFERENCES ({_quote(grant.column)}) ON TABLE {table} FROM {grantee} RESTRICT"
                )


def _relay_topology(connection: Connection, name: str, oid: int) -> tuple[str, ...]:
    columns = _rows(
        connection,
        """
        SELECT a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
               a.attnotnull,a.attgenerated,a.attidentity
          FROM pg_catalog.pg_attribute a WHERE a.attrelid=:oid AND a.attnum>0
           AND NOT a.attisdropped ORDER BY a.attnum
    """,
        {"oid": oid},
    )
    expected_columns = (("tenant_id", "uuid", True),) if name == "outbox_events" else ()
    expected_columns += (
        ("id", "uuid", True),
        ("event_type", "character varying(120)", True),
        ("payload", "jsonb", True),
        ("status", "character varying(20)", True),
        ("attempts", "integer", True),
        ("available_at", "timestamp with time zone", True),
        ("correlation_id", "character varying(200)", False),
        ("sent_at", "timestamp with time zone", False),
        ("last_error", "character varying(500)", False),
        ("leased_by", "character varying(200)", False),
        ("leased_at", "timestamp with time zone", False),
        ("created_at", "timestamp with time zone", True),
        ("updated_at", "timestamp with time zone", True),
    )
    if tuple(
        (r["attname"], r["type"], r["attnotnull"]) for r in columns
    ) != expected_columns or any(
        r["attgenerated"] or r["attidentity"] for r in columns
    ):
        _refuse("Relay source column/type shape drift.")
    posture = _rows(
        connection,
        """
        SELECT c.relrowsecurity,c.relforcerowsecurity FROM pg_catalog.pg_class c WHERE c.oid=:oid
    """,
        {"oid": oid},
    )[0]
    # Displayed expressions prove the closed shape only. pg_depend binds the
    # actual public helper identity. Only the tenant relation/column, that helper
    # and bootstrap pg_catalog expression dependencies are admitted: custom
    # operators, casts, types or other dependency classes refuse. Stored trees/OIDs bind
    # replay independently of search_path-dependent qualification in deparsing.
    policies = _rows(
        connection,
        """
        SELECT p.oid,p.polname,p.polcmd,p.polpermissive,p.polroles,
               p.polqual::text AS using_tree,p.polwithcheck::text AS check_tree,
               pg_catalog.pg_get_expr(p.polqual,p.polrelid) IN
                 ('(tenant_id = app_current_tenant_id())',
                  '(tenant_id = public.app_current_tenant_id())') AS expected_using,
               pg_catalog.pg_get_expr(p.polwithcheck,p.polrelid) IN
                 ('(tenant_id = app_current_tenant_id())',
                  '(tenant_id = public.app_current_tenant_id())') AS expected_check,
               pg_catalog.to_regprocedure('public.app_current_tenant_id()')::oid AS tenant_function_oid,
               ARRAY(SELECT DISTINCT d.refobjid
                       FROM pg_catalog.pg_depend d
                       JOIN pg_catalog.pg_proc f ON f.oid=d.refobjid
                       JOIN pg_catalog.pg_namespace n ON n.oid=f.pronamespace
                      WHERE d.classid='pg_catalog.pg_policy'::regclass
                        AND d.objid=p.oid AND d.objsubid=0
                        AND d.refclassid='pg_catalog.pg_proc'::regclass
                        AND (f.oid>=16384 OR n.nspname<>'pg_catalog')
                      ORDER BY d.refobjid) AS function_dependencies,
               ARRAY(SELECT d.refclassid::text || ':' || d.refobjid::text || ':' ||
                            d.refobjsubid::text || ':' || d.deptype::text
                       FROM pg_catalog.pg_depend d
                      WHERE d.classid='pg_catalog.pg_policy'::regclass
                        AND d.objid=p.oid AND d.objsubid=0
                      ORDER BY d.refclassid,d.refobjid,d.refobjsubid,d.deptype) AS expression_dependencies,
               NOT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_depend d
                   LEFT JOIN pg_catalog.pg_proc f
                     ON d.refclassid='pg_catalog.pg_proc'::regclass AND f.oid=d.refobjid
                   LEFT JOIN pg_catalog.pg_operator op
                     ON d.refclassid='pg_catalog.pg_operator'::regclass AND op.oid=d.refobjid
                   LEFT JOIN pg_catalog.pg_type ty
                     ON d.refclassid='pg_catalog.pg_type'::regclass AND ty.oid=d.refobjid
                   LEFT JOIN pg_catalog.pg_collation co
                     ON d.refclassid='pg_catalog.pg_collation'::regclass AND co.oid=d.refobjid
                   LEFT JOIN pg_catalog.pg_namespace ns
                     ON ns.oid=COALESCE(f.pronamespace,op.oprnamespace,ty.typnamespace,co.collnamespace)
                    WHERE d.classid='pg_catalog.pg_policy'::regclass
                      AND d.objid=p.oid AND d.objsubid=0
                      AND NOT COALESCE((
                        (d.refclassid='pg_catalog.pg_class'::regclass AND d.refobjid=p.polrelid
                          AND (d.refobjsubid=0 OR d.refobjsubid=(
                            SELECT a.attnum FROM pg_catalog.pg_attribute a
                             WHERE a.attrelid=p.polrelid AND a.attname='tenant_id' AND NOT a.attisdropped)))
                        OR (d.refclassid='pg_catalog.pg_proc'::regclass AND d.refobjsubid=0
                          AND d.refobjid=pg_catalog.to_regprocedure('public.app_current_tenant_id()')::oid)
                        OR (d.refclassid IN ('pg_catalog.pg_proc'::regclass,'pg_catalog.pg_operator'::regclass,
                                            'pg_catalog.pg_type'::regclass,'pg_catalog.pg_collation'::regclass)
                          AND d.refobjsubid=0 AND d.refobjid<16384 AND ns.nspname='pg_catalog')
                      ),false)
               ) AS dependencies_closed
          FROM pg_catalog.pg_policy p WHERE p.polrelid=:oid ORDER BY p.polname
    """,
        {"oid": oid},
    )
    tenant = name == "outbox_events"
    if posture["relrowsecurity"] != tenant or posture["relforcerowsecurity"] != tenant:
        _refuse("Relay source RLS posture drift.")
    if tenant:
        if (
            len(policies) != 1
            or any(
                policies[0][k] != v
                for k, v in {
                    "polname": "outbox_events_tenant_isolation",
                    "polcmd": "*",
                    "polpermissive": True,
                    "polroles": [0],
                    "expected_using": True,
                    "expected_check": True,
                    "dependencies_closed": True,
                }.items()
            )
            or policies[0]["tenant_function_oid"] is None
            or policies[0]["function_dependencies"]
            != [policies[0]["tenant_function_oid"]]
        ):
            _refuse("Tenant relay source policy drift.")
    elif policies:
        _refuse("Platform relay unexpected policy.")
    constraints = _rows(
        connection,
        """
        SELECT c.oid,c.conname,c.conrelid,c.confrelid,c.conkey,c.confkey,
               c.confupdtype,c.confdeltype,c.confmatchtype,c.condeferrable,c.condeferred,c.convalidated,
               c.conparentid,
               ARRAY(SELECT a.attname FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) ORDER BY a.attnum) AS source_columns,
               ARRAY(SELECT a.attname FROM pg_catalog.pg_attribute a WHERE a.attrelid=c.confrelid AND a.attnum=ANY(c.confkey) ORDER BY a.attnum) AS target_columns,
               c.confrelid='public.tenants'::regclass AS tenant_target
          FROM pg_catalog.pg_constraint c WHERE c.contype='f' AND (c.conrelid=:oid OR c.confrelid=:oid)
          ORDER BY c.oid
    """,
        {"oid": oid},
    )
    if name == "platform_outbox_events":
        if constraints:
            _refuse("Platform outbox unexpectedly participates in a foreign key.")
    else:
        expected = {
            "conname": "fk_outbox_events_tenant",
            "conrelid": oid,
            "confupdtype": "a",
            "confdeltype": "c",
            "confmatchtype": "s",
            "condeferrable": False,
            "condeferred": False,
            "convalidated": True,
            "conparentid": 0,
            "source_columns": ["tenant_id"],
            "target_columns": ["id"],
            "tenant_target": True,
        }
        if len(constraints) != 1 or any(
            constraints[0][k] != v for k, v in expected.items()
        ):
            _refuse("Tenant outbox foreign-key topology drift.")
    triggers = _rows(
        connection,
        """
        SELECT t.oid,t.tgrelid,t.tgfoid,t.tgtype,t.tgenabled,t.tgisinternal,t.tgnargs,
               t.tgargs,t.tgattr::text AS attributes,t.tgqual,t.tgconstraint,t.tgconstrrelid,
               t.tgdeferrable,t.tginitdeferred,t.tgparentid,t.tgoldtable,t.tgnewtable,
               p.proname,n.nspname
          FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
          JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
         WHERE t.tgrelid=:oid OR t.tgconstraint=ANY(:constraints) ORDER BY t.oid
    """,
        {"oid": oid, "constraints": [cast(int, c["oid"]) for c in constraints]},
    )
    expected_ri = (
        {
            ("RI_FKey_check_ins", 5),
            ("RI_FKey_check_upd", 17),
            ("RI_FKey_cascade_del", 9),
            ("RI_FKey_noaction_upd", 17),
        }
        if constraints
        else set()
    )
    if (
        len(triggers) != len(expected_ri)
        or {(r["proname"], r["tgtype"]) for r in triggers} != expected_ri
    ):
        _refuse("Unexpected outbox trigger or missing original RI trigger.")
    for r in triggers:
        constraint = constraints[0]
        check = r["proname"] in ("RI_FKey_check_ins", "RI_FKey_check_upd")
        expected = {
            "tgrelid": oid if check else constraint["confrelid"],
            "tgconstrrelid": constraint["confrelid"] if check else oid,
            "tgenabled": "O",
            "tgisinternal": True,
            "tgnargs": 0,
            "attributes": "",
            "tgqual": None,
            "tgconstraint": constraint["oid"],
            "tginitdeferred": False,
            "tgparentid": 0,
            "tgoldtable": None,
            "tgnewtable": None,
            "nspname": "pg_catalog",
            "tgdeferrable": False,
        }
        if any(r[k] != v for k, v in expected.items()) or bytes(r["tgargs"]) != b"":
            _refuse("Outbox RI trigger catalog drift.")
    return (
        "rls:" + _catalog_rows([posture]) + ":" + _catalog_rows(policies),
        "columns:" + _catalog_rows(columns),
        "fk:" + _catalog_rows(constraints),
        "ri:" + _catalog_rows(triggers),
    )


def _verify_attachment_denial(
    connection: Connection, *, observation: PrivilegeClosureObservation
) -> None:
    if observation.grants:
        _refuse("Attachment ACL remains after closure or drifted on exact replay.")
    for name in _population(observation.population):
        rows = _rows(
            connection,
            """
            SELECT r.rolname,
                   pg_catalog.has_table_privilege(r.oid,pg_catalog.to_regclass(:table),'TRIGGER') OR
                   pg_catalog.has_table_privilege(r.oid,pg_catalog.to_regclass(:table),'REFERENCES') OR
                   pg_catalog.has_any_column_privilege(r.oid,pg_catalog.to_regclass(:table),'REFERENCES') AS attachment
              FROM pg_catalog.pg_roles r WHERE r.rolname=ANY(:roles)
        """,
            {"table": "public." + name, "roles": list(_RUNTIME_ROLES)},
        )
        if len(rows) != 4 or any(r["attachment"] for r in rows):
            _refuse("Actual online role retains effective attachment capability.")


def _verify_replay_digest(
    connection: Connection, *, population: ClosurePopulation, digest: str
) -> None:
    event = (
        "billing.legacy_archive_fence.staged"
        if population is ClosurePopulation.ARCHIVE
        else "billing.legacy_retained_columns.staged"
    )
    rows = _rows(
        connection,
        """
        SELECT payload->>'catalog_digest' AS digest FROM public.event_store
         WHERE event_type=:event AND payload->'envelope'->>'producer_owner'='runtime.billing_legacy_archive_fence'
    """,
        {"event": event},
    )
    if not rows or any(r["digest"] != digest for r in rows):
        _refuse("Installed component catalog differs from its original staged audit.")
