#!/usr/bin/env python3
"""Create or adopt the composed-module outbox dispatcher roles.

This is an explicitly privileged cluster bootstrap, separate from ordinary
Alembic execution. It never sets or prints a password. It owns the role and
schema prerequisites migration 557 needs before it can harden relay functions.

Role-graph direction (``module_database_roles.v1``): the migrator ``app_admin``
is a member of the runtime owner ``dotmac_app`` (INHERIT and SET, no ADMIN) so
migrations can alter what the owner owns. The owner is never a member of the
migrator: ``--repair`` converges to that edge and never re-grants the old one.

Usage::

    BOOTSTRAP_DATABASE_URL=postgresql://postgres@host/db \\
        python scripts/bootstrap_outbox_dispatcher_roles.py [--dry-run] [--repair]

    MIGRATION_DATABASE_URL=postgresql://app_admin@host/db \\
        python scripts/bootstrap_outbox_dispatcher_roles.py --verify-only

Exit codes: 0 satisfied (or created), 1 contract drift, 2 usage/connection
error.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql

from app.outbox_dispatcher_roles import (
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
    RoleGraphObservation,
    RolePosture,
    relay_dispatcher_violations,
    relay_ownership_violations,
)

BOOTSTRAP_URL_VAR = "BOOTSTRAP_DATABASE_URL"
MIGRATION_URL_VAR = "MIGRATION_DATABASE_URL"


def _attributes(posture: RolePosture) -> str:
    can_login, bypass_rls, superuser = posture
    return (
        f"{'LOGIN' if can_login else 'NOLOGIN'} "
        f"{'BYPASSRLS' if bypass_rls else 'NOBYPASSRLS'} "
        f"{'SUPERUSER' if superuser else 'NOSUPERUSER'}"
    )


def observe(conn: psycopg.Connection) -> dict[str, RolePosture]:
    """Read only the three posture flags the checked-in contract owns."""

    rows = conn.execute(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
        "FROM pg_roles WHERE rolname = ANY(%s)",
        (list(RELAY_DISPATCHER_CONTRACT),),
    ).fetchall()
    return {str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3])) for row in rows}


_BYPASS_REACHERS_SQL = """
SELECT a.rolname, t.rolname
  FROM pg_roles a
  JOIN pg_roles t ON t.rolbypassrls AND t.oid <> a.oid
 WHERE a.rolcanlogin AND NOT a.rolsuper AND a.rolname <> %s
   AND pg_has_role(a.oid, t.oid, 'MEMBER')
 ORDER BY 1, 2
"""


def observe_ownership(
    conn: psycopg.Connection,
) -> tuple[RoleGraphObservation, dict[str, bool]]:
    """Read the role graph and the schema privileges migration 557 requires."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    migrator, owner = contract.migration_role, contract.owner_role
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
        ([migrator, owner, contract.definer_role],),
    ).fetchall()
    roles = {str(row[0]) for row in rows}
    migrator_exists = migrator in roles
    owner_exists = owner in roles
    inherits = can_set = admin = old_direction = False
    if migrator_exists and owner_exists:
        inherits = bool(
            conn.execute(
                "SELECT pg_has_role(%s, %s, 'USAGE')", (migrator, owner)
            ).fetchone()[0]
        )
        can_set = bool(
            conn.execute(
                "SELECT pg_has_role(%s, %s, 'SET')", (migrator, owner)
            ).fetchone()[0]
        )
        admin = bool(
            conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
                "WHERE m.roleid = %s::regrole AND m.member = %s::regrole "
                "AND m.admin_option)",
                (owner, migrator),
            ).fetchone()[0]
        )
        old_direction = bool(
            conn.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER')", (owner, migrator)
            ).fetchone()[0]
        )
    reachers = tuple(
        f"{row[0]}->{row[1]}"
        for row in conn.execute(_BYPASS_REACHERS_SQL, (migrator,)).fetchall()
    )
    graph = RoleGraphObservation(
        migrator_exists=migrator_exists,
        owner_exists=owner_exists,
        migrator_inherits_owner=inherits,
        migrator_can_set_owner=can_set,
        migrator_admin_option=admin,
        owner_reaches_migrator=old_direction,
        bypass_reachers=reachers,
    )
    if contract.definer_role not in roles:
        return graph, dict.fromkeys(contract.schema_privileges, False)
    privileges = {
        privilege: bool(
            conn.execute(
                "SELECT has_schema_privilege(%s, %s, %s)",
                (contract.definer_role, contract.schema, privilege),
            ).fetchone()[0]
        )
        for privilege in contract.schema_privileges
    }
    return graph, privileges


def _apply(
    conn: psycopg.Connection,
    statement: sql.Composable,
    *,
    dry_run: bool,
    would: str,
    done: str,
) -> None:
    if dry_run:
        print(f"would {would}")
    else:
        conn.execute(statement)
        print(done)


def bootstrap(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    observed = observe(conn)
    ownership = observe_ownership(conn)
    wrong_existing = [
        violation
        for violation in (
            *relay_dispatcher_violations(observed),
            *relay_ownership_violations(
                role_graph=ownership[0],
                definer_schema_privileges=ownership[1],
            ),
        )
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

    for role, wanted_posture in RELAY_DISPATCHER_CONTRACT.items():
        wanted = _attributes(wanted_posture)
        identifier = sql.Identifier(role)
        actual = observed.get(role)
        if actual is None:
            statement = sql.SQL("CREATE ROLE {} {}").format(identifier, sql.SQL(wanted))
            if dry_run:
                print(f"would create: {role} {wanted}")
            else:
                conn.execute(statement)
                print(f"created: {role} {wanted}")
            continue
        if actual == wanted_posture:
            print(f"adopted: {role} already {wanted}")
            continue

        have = _attributes(actual)
        if dry_run:
            print(f"would repair: {role} {have} -> {wanted}")
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} {}").format(identifier, sql.SQL(wanted))
            )
            print(f"repaired: {role} {have} -> {wanted}")

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    graph, privileges = observe_ownership(conn)
    migrator = sql.Identifier(contract.migration_role)
    owner = sql.Identifier(contract.owner_role)
    pair = f"{contract.migration_role} / {contract.owner_role}"
    if graph.migrator_exists and graph.owner_exists:
        # Converge to the one permitted direction. The old edge is revoked
        # FIRST and is never granted again by this script.
        if graph.owner_reaches_migrator:
            _apply(
                conn,
                sql.SQL("REVOKE {} FROM {}").format(migrator, owner),
                dry_run=dry_run,
                would=f"revoke old-direction membership of {pair}",
                done=f"revoked old-direction membership of {pair}",
            )
        if not (graph.migrator_inherits_owner and graph.migrator_can_set_owner):
            _apply(
                conn,
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                    owner, migrator
                ),
                dry_run=dry_run,
                would=f"grant {contract.owner_role} to {contract.migration_role} "
                "(INHERIT, SET)",
                done=f"granted {contract.owner_role} to {contract.migration_role} "
                "(INHERIT, SET)",
            )
        if graph.migrator_admin_option:
            _apply(
                conn,
                sql.SQL("REVOKE ADMIN OPTION FOR {} FROM {}").format(owner, migrator),
                dry_run=dry_run,
                would=f"revoke ADMIN OPTION on {pair}",
                done=f"revoked ADMIN OPTION on {pair}",
            )
    else:
        print(
            f"skipped role-graph repair: {contract.migration_role} or "
            f"{contract.owner_role} does not exist",
            file=sys.stderr,
        )
    if not dry_run:
        # The old edge itself makes the owner a login role that reaches the
        # migrator; judge reachers on the graph AFTER the repair above.
        graph, privileges = observe_ownership(conn)
    elif graph.owner_reaches_migrator:
        graph = replace(
            graph,
            bypass_reachers=tuple(
                r
                for r in graph.bypass_reachers
                if not r.startswith(f"{contract.owner_role}->")
            ),
        )
    if graph.bypass_reachers:
        # Never auto-revoked: removing an arbitrary membership could cut a
        # live login. Report and leave it to a governed change.
        for reacher in graph.bypass_reachers:
            print(
                f"DRIFT: login role path {reacher} reaches a BYPASSRLS role; "
                "repair does not remove it.",
                file=sys.stderr,
            )
        return 1
    missing_privileges = tuple(
        privilege
        for privilege in contract.schema_privileges
        if not privileges.get(privilege, False)
    )
    if missing_privileges:
        statement = sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
            sql.SQL(", ").join(sql.SQL(privilege) for privilege in missing_privileges),
            sql.Identifier(contract.schema),
            sql.Identifier(contract.definer_role),
        )
        if dry_run:
            print(
                "would grant schema privileges: "
                f"{', '.join(missing_privileges)} on {contract.schema} "
                f"to {contract.definer_role}"
            )
        else:
            conn.execute(statement)
            print(
                "granted schema privileges: "
                f"{', '.join(missing_privileges)} on {contract.schema} "
                f"to {contract.definer_role}"
            )
    return 0


def verify(conn: psycopg.Connection) -> int:
    graph, privileges = observe_ownership(conn)
    violations = (
        *relay_dispatcher_violations(observe(conn)),
        *relay_ownership_violations(
            role_graph=graph,
            definer_schema_privileges=privileges,
        ),
    )
    for violation in violations:
        print(f"DISPATCHER CONTRACT: {violation}", file=sys.stderr)
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
            f"{url_var} is not set; dispatcher bootstrap is separate from the "
            "application connection string.",
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
            "database connection or role operation failed; connection details "
            "were deliberately not logged",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
