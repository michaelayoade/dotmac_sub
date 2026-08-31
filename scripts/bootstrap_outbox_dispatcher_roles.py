#!/usr/bin/env python3
"""Create or adopt the composed-module outbox dispatcher roles.

This is an explicitly privileged cluster bootstrap, separate from ordinary
Alembic execution. It never sets or prints a password. It owns the role and
schema prerequisites migration 557 needs before it can harden relay functions.

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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql

from app.outbox_dispatcher_roles import (
    OUTBOX_RELAY_OWNERSHIP_CONTRACT,
    RELAY_DISPATCHER_CONTRACT,
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


def observe_ownership(conn: psycopg.Connection) -> tuple[bool, dict[str, bool]]:
    """Read the ownership prerequisites migration 557 requires."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
        ([contract.migration_role, contract.definer_role],),
    ).fetchall()
    roles = {str(row[0]) for row in rows}
    if contract.definer_role not in roles:
        return False, dict.fromkeys(contract.schema_privileges, False)
    if contract.migration_role not in roles:
        member = False
    else:
        member = bool(
            conn.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER')",
                (contract.migration_role, contract.definer_role),
            ).fetchone()[0]
        )
    privileges = {
        privilege: bool(
            conn.execute(
                "SELECT has_schema_privilege(%s, %s, %s)",
                (contract.definer_role, contract.schema, privilege),
            ).fetchone()[0]
        )
        for privilege in contract.schema_privileges
    }
    return member, privileges


def bootstrap(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    observed = observe(conn)
    ownership = observe_ownership(conn)
    wrong_existing = [
        violation
        for violation in (
            *relay_dispatcher_violations(observed),
            *relay_ownership_violations(
                migration_role_is_definer_member=ownership[0],
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
    member, privileges = observe_ownership(conn)
    if not member:
        statement = sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(contract.definer_role),
            sql.Identifier(contract.migration_role),
        )
        if dry_run:
            print(
                f"would grant role membership: {contract.definer_role} "
                f"to {contract.migration_role}"
            )
        else:
            conn.execute(statement)
            print(
                f"granted role membership: {contract.definer_role} "
                f"to {contract.migration_role}"
            )
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
    member, privileges = observe_ownership(conn)
    violations = (
        *relay_dispatcher_violations(observe(conn)),
        *relay_ownership_violations(
            migration_role_is_definer_member=member,
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
