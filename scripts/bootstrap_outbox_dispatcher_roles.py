#!/usr/bin/env python3
"""Create or adopt the composed-module outbox dispatcher roles.

This is an explicitly privileged cluster bootstrap, separate from ordinary
Alembic execution. It never sets or prints a password and never grants object
privileges; migration 555 owns the schema/function grants.

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
    RELAY_DISPATCHER_CONTRACT,
    RolePosture,
    relay_dispatcher_violations,
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


def bootstrap(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    observed = observe(conn)
    wrong_existing = [
        violation
        for violation in relay_dispatcher_violations(observed)
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
    return 0


def verify(conn: psycopg.Connection) -> int:
    violations = relay_dispatcher_violations(observe(conn))
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
