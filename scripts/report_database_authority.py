"""Read-only, bounded catalog evidence for a reviewed database role cutover.

Use REPORT_DATABASE_URL for a credential with catalog visibility. The report
contains role flags, schema/object owners and relation privilege names only;
it never reads application rows or prints the connection URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import psycopg

from app.commercial_module_prereqs import module_schemas

MAX_RELATIONS = 2000
ROLES = ("app_admin", "app_user", "platform_api", "dotmac_app")


def catalog_report(conn: psycopg.Connection) -> dict[str, object]:
    """Observe the exact named objects needing a separate ownership/grant plan."""

    schemas = ("public", *sorted(module_schemas()))
    roles = conn.execute(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
        "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(ROLES),),
    ).fetchall()
    present_roles = {row[0] for row in roles}
    namespace_rows = conn.execute(
        "SELECT nspname, pg_get_userbyid(nspowner) "
        "FROM pg_namespace WHERE nspname = ANY(%s) ORDER BY nspname",
        (list(schemas),),
    ).fetchall()
    present_schemas = {row[0] for row in namespace_rows}
    relation_rows = conn.execute(
        "SELECT n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner), "
        "       COALESCE(( "
        "         SELECT jsonb_object_agg(r.rolname, ARRAY( "
        "           SELECT p.privilege FROM unnest( "
        "             CASE WHEN c.relkind = 'S' "
        "               THEN ARRAY['SELECT', 'USAGE', 'UPDATE'] "
        "               ELSE ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] "
        "             END "
        "           ) WITH ORDINALITY AS p(privilege, position) "
        "           WHERE CASE WHEN c.relkind = 'S' "
        "             THEN has_sequence_privilege(r.oid, c.oid, p.privilege) "
        "             ELSE has_table_privilege(r.oid, c.oid, p.privilege) END "
        "           ORDER BY p.position "
        "         )) FROM pg_roles r WHERE r.rolname = ANY(%s) "
        "       ), '{}'::jsonb) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s) "
        "ORDER BY n.nspname, c.relname LIMIT %s",
        (list(ROLES), list(schemas), ["r", "p", "v", "m", "f", "S"], MAX_RELATIONS + 1),
    ).fetchall()
    if len(relation_rows) > MAX_RELATIONS:
        raise RuntimeError("catalog exceeds the bounded report limit")

    relations = []
    for schema, name, kind, owner, effective in relation_rows:
        path = f"{schema}.{name}"
        effective_privileges = {role: effective.get(role, []) for role in ROLES}
        relations.append(
            {
                "path": path,
                "kind": kind,
                "owner": owner,
                "effective_privileges": effective_privileges,
            }
        )

    owner_counts: dict[str, int] = {}
    without_app_user_dml: list[str] = []
    for relation in relations:
        owner_counts[relation["owner"]] = owner_counts.get(relation["owner"], 0) + 1
        if (
            relation["kind"] in {"r", "p", "f"}
            and not relation["effective_privileges"]["app_user"]
        ):
            without_app_user_dml.append(relation["path"])

    report = {
        "database": conn.execute("SELECT current_database()").fetchone()[0],
        "roles": [
            {
                "name": name,
                "login": login,
                "bypass_rls": bypass,
                "superuser": superuser,
            }
            for name, login, bypass, superuser in roles
        ],
        "schemas": [{"name": name, "owner": owner} for name, owner in namespace_rows],
        "relations": relations,
        "summary": {
            "missing_roles": sorted(set(ROLES) - present_roles),
            "missing_schemas": sorted(set(schemas) - present_schemas),
            "relation_count": len(relations),
            "relation_owner_counts": owner_counts,
            "app_user_no_effective_table_dml_count": len(without_app_user_dml),
            "app_user_no_effective_table_dml_paths": without_app_user_dml,
        },
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"))
    report["catalog_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return report


def main() -> int:
    url = os.environ.get("REPORT_DATABASE_URL", "").strip()
    if not url:
        print("REPORT_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1)
        ) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '10s'")
            report = catalog_report(conn)
    except (psycopg.Error, RuntimeError):
        print("catalog report failed; connection details withheld", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
