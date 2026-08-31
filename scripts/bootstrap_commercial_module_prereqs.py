#!/usr/bin/env python3
"""Create or verify commercial module database prerequisites.

This is an explicitly privileged bootstrap, separate from ordinary Alembic
execution. It never sets or prints passwords. Alembic verifies the resulting
catalog contract before module migrations run; this script is the owner that
creates or repairs cluster roles and module schemas.

Usage::

    BOOTSTRAP_DATABASE_URL=postgresql://postgres@host/db \\
        python scripts/bootstrap_commercial_module_prereqs.py [--dry-run] [--repair]

    MIGRATION_DATABASE_URL=postgresql://dotmac_app@host/db \\
        python scripts/bootstrap_commercial_module_prereqs.py --verify-only

Exit codes: 0 satisfied (or created), 1 contract drift, 2 usage/connection
error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    COMMERCIAL_MODULE_SCHEMA_CONTRACT,
    DatabaseRoleContract,
    ModuleSchemaObservation,
    RolePosture,
    commercial_bootstrap_role_violations,
    commercial_schema_violations,
)

BOOTSTRAP_URL_VAR = "BOOTSTRAP_DATABASE_URL"
MIGRATION_URL_VAR = "MIGRATION_DATABASE_URL"


def _attributes(contract: DatabaseRoleContract | RolePosture) -> str:
    if isinstance(contract, DatabaseRoleContract):
        can_login, bypass_rls, superuser = contract.posture
    else:
        can_login, bypass_rls, superuser = contract
    return (
        f"{'LOGIN' if can_login else 'NOLOGIN'} "
        f"{'BYPASSRLS' if bypass_rls else 'NOBYPASSRLS'} "
        f"{'SUPERUSER' if superuser else 'NOSUPERUSER'}"
    )


def observe_roles(conn: psycopg.Connection) -> dict[str, RolePosture]:
    """Read only the posture flags owned by the checked-in contract."""

    rows = conn.execute(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
        "FROM pg_roles WHERE rolname = ANY(%s)",
        (list(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT),),
    ).fetchall()
    return {str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3])) for row in rows}


def _public_schema_privileges(
    conn: psycopg.Connection, schema_name: str
) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT acl.privilege_type
          FROM pg_namespace AS namespace
          CROSS JOIN LATERAL aclexplode(
            COALESCE(namespace.nspacl, acldefault('n', namespace.nspowner))
          ) AS acl
         WHERE namespace.nspname = %s
           AND acl.grantee = 0
           AND acl.privilege_type IN ('USAGE', 'CREATE')
         ORDER BY acl.privilege_type
        """,
        (schema_name,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def observe_schemas(conn: psycopg.Connection) -> dict[str, ModuleSchemaObservation]:
    """Read the catalog state for every declared commercial module schema."""

    schema_names = [item.schema for item in COMMERCIAL_MODULE_SCHEMA_CONTRACT]
    rows = conn.execute(
        "SELECT n.nspname, pg_get_userbyid(n.nspowner) "
        "FROM pg_namespace AS n WHERE n.nspname = ANY(%s)",
        (schema_names,),
    ).fetchall()
    owners = {str(row[0]): str(row[1]) for row in rows}
    existing_roles = set(observe_roles(conn))
    observed: dict[str, ModuleSchemaObservation] = {}
    for expected in COMMERCIAL_MODULE_SCHEMA_CONTRACT:
        owner = owners.get(expected.schema)
        if owner is None:
            continue
        usage_roles = []
        for role in expected.usage_roles:
            if role not in existing_roles:
                continue
            has_usage = conn.execute(
                "SELECT has_schema_privilege(%s, %s, 'USAGE')",
                (role, expected.schema),
            ).fetchone()
            if has_usage and bool(has_usage[0]):
                usage_roles.append(role)
        observed[expected.schema] = ModuleSchemaObservation(
            owner_role=owner,
            public_privileges=_public_schema_privileges(conn, expected.schema),
            usage_roles=tuple(usage_roles),
        )
    return observed


def _bootstrap_roles(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    observed = observe_roles(conn)
    wrong_existing = [
        violation
        for violation in commercial_bootstrap_role_violations(observed)
        if not violation.endswith("is missing")
    ]
    if wrong_existing and not repair:
        for violation in wrong_existing:
            print(
                f"DRIFT: {violation}. Re-run with --repair to correct it; "
                "rewriting cluster access is deliberately opt-in.",
                file=sys.stderr,
            )
        return 1

    for role, wanted_contract in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT.items():
        wanted = _attributes(wanted_contract)
        identifier = sql.Identifier(role)
        actual = observed.get(role)
        if actual is None:
            statement = sql.SQL("CREATE ROLE {} {}").format(identifier, sql.SQL(wanted))
            if dry_run:
                print(f"would create role: {role} {wanted}")
            else:
                conn.execute(statement)
                print(f"created role: {role} {wanted}")
            continue
        if actual == wanted_contract.posture:
            print(f"adopted role: {role} already {wanted}")
            continue

        have = _attributes(actual)
        if dry_run:
            print(f"would repair role: {role} {have} -> {wanted}")
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} {}").format(identifier, sql.SQL(wanted))
            )
            print(f"repaired role: {role} {have} -> {wanted}")
    return 0


def _bootstrap_schemas(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    observed = observe_schemas(conn)
    wrong_existing = [
        violation
        for violation in commercial_schema_violations(observed)
        if not violation.endswith("is missing")
    ]
    if wrong_existing and not repair:
        for violation in wrong_existing:
            print(
                f"DRIFT: {violation}. Re-run with --repair to correct it; "
                "schema ownership and grants are deliberately opt-in.",
                file=sys.stderr,
            )
        return 1

    for expected in COMMERCIAL_MODULE_SCHEMA_CONTRACT:
        schema_id = sql.Identifier(expected.schema)
        owner_id = sql.Identifier(expected.owner_role)
        actual = observed.get(expected.schema)
        if actual is None:
            statement = sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                schema_id, owner_id
            )
            if dry_run:
                print(
                    f"would create schema: {expected.schema} "
                    f"owner={expected.owner_role}"
                )
            else:
                conn.execute(statement)
                print(f"created schema: {expected.schema} owner={expected.owner_role}")
        elif actual.owner_role == expected.owner_role:
            print(
                f"adopted schema: {expected.schema} already owner={expected.owner_role}"
            )
        elif dry_run:
            print(
                f"would repair schema owner: {expected.schema} "
                f"{actual.owner_role} -> {expected.owner_role}"
            )
        else:
            conn.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema_id, owner_id)
            )
            print(
                f"repaired schema owner: {expected.schema} "
                f"{actual.owner_role} -> {expected.owner_role}"
            )

        if dry_run:
            print(f"would revoke schema public access: {expected.schema}")
        else:
            conn.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema_id)
            )
            print(f"revoked schema public access: {expected.schema}")

        for role in expected.usage_roles:
            role_id = sql.Identifier(role)
            if dry_run:
                print(f"would grant schema usage: {expected.schema} to {role}")
            else:
                conn.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_id, role_id)
                )
                print(f"granted schema usage: {expected.schema} to {role}")
    return 0


def bootstrap(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    roles = _bootstrap_roles(conn, dry_run=dry_run, repair=repair)
    if roles != 0:
        return roles
    return _bootstrap_schemas(conn, dry_run=dry_run, repair=repair)


def verify(conn: psycopg.Connection) -> int:
    role_violations = commercial_bootstrap_role_violations(observe_roles(conn))
    schema_violations = commercial_schema_violations(observe_schemas(conn))
    violations = (*role_violations, *schema_violations)
    for violation in violations:
        print(f"COMMERCIAL MODULE PREREQUISITE: {violation}", file=sys.stderr)
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only and (args.dry_run or args.repair):
        parser.error("--verify-only cannot be combined with --dry-run or --repair")

    url_var = MIGRATION_URL_VAR if args.verify_only else BOOTSTRAP_URL_VAR
    url = os.environ.get(url_var, "").strip()
    if not url:
        print(
            f"{url_var} is not set; commercial module bootstrap is separate "
            "from the application connection string.",
            file=sys.stderr,
        )
        return 2

    try:
        connect_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(connect_url, autocommit=False) as conn:
            if args.verify_only:
                return verify(conn)
            return bootstrap(conn, dry_run=args.dry_run, repair=args.repair)
    except psycopg.Error:
        print(
            "database connection or prerequisite operation failed; connection "
            "details were deliberately not logged",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
