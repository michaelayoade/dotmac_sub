#!/usr/bin/env python3
"""Create or adopt the composed-module outbox dispatcher roles.

This is an explicitly privileged cluster bootstrap, separate from ordinary
Alembic execution. It never sets or prints a password. The default path checks
current app_admin ownership. It refuses the retired dotmac_app membership.

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
    RelayOwnershipObservation,
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


def observe_ownership(
    conn: psycopg.Connection,
) -> RelayOwnershipObservation:
    """Read operational ownership context without changing cluster roles."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT

    rows = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
        ([contract.migration_role, contract.definer_role],),
    ).fetchall()
    roles = {str(row[0]) for row in rows}
    if contract.definer_role not in roles:
        return RelayOwnershipObservation(
            contract.migration_role in roles,
            False,
            False,
            dict.fromkeys(contract.schema_privileges, False),
        )
    if contract.migration_role not in roles:
        member = False
    elif contract.migration_role == contract.definer_role:
        member = True
    else:
        member_row = conn.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER')",
            (contract.migration_role, contract.definer_role),
        ).fetchone()
        member = bool(member_row is not None and member_row[0])

    def has_schema_privilege(privilege: str) -> bool:
        row = conn.execute(
            "SELECT has_schema_privilege(%s, %s, %s)",
            (contract.definer_role, contract.schema, privilege),
        ).fetchone()
        return bool(row is not None and row[0])

    privileges = {
        privilege: has_schema_privilege(privilege)
        for privilege in contract.schema_privileges
    }
    return RelayOwnershipObservation(
        contract.migration_role in roles, True, member, privileges
    )


def observe_historical_membership(conn: psycopg.Connection) -> bool:
    """A retired direct or indirect link must not pass operational verification."""

    roles = conn.execute(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
        (["dotmac_app", "app_admin"],),
    ).fetchall()
    if {str(row[0]) for row in roles} != {"dotmac_app", "app_admin"}:
        return False
    member_row = conn.execute(
        "SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER')"
    ).fetchone()
    return bool(member_row is None or member_row[0])


def ensure_current_schema_privileges(
    conn: psycopg.Connection, *, dry_run: bool
) -> None:
    """Apply the current app_admin public-schema contract to this database."""

    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    ownership = observe_ownership(conn)
    if not ownership.definer_role_exists:
        raise ValueError("app_admin is required for public-schema grants")
    missing = tuple(
        privilege
        for privilege in contract.schema_privileges
        if not ownership.definer_schema_privileges.get(privilege, False)
    )
    if not missing:
        return
    statement = sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
        sql.SQL(", ").join(sql.SQL(privilege) for privilege in missing),
        sql.Identifier(contract.schema),
        sql.Identifier(contract.definer_role),
    )
    if dry_run:
        print(
            "would grant schema privileges: "
            f"{', '.join(missing)} on {contract.schema} "
            f"to {contract.definer_role}"
        )
    else:
        conn.execute(statement)
        print(
            "granted schema privileges: "
            f"{', '.join(missing)} on {contract.schema} "
            f"to {contract.definer_role}"
        )


def bootstrap(
    conn: psycopg.Connection,
    *,
    dry_run: bool,
    repair: bool,
) -> int:
    if observe_historical_membership(conn):
        print(
            "DRIFT: retired dotmac_app membership in app_admin remains; "
            "separate authority must retire it before operational repair.",
            file=sys.stderr,
        )
        return 1
    contract = OUTBOX_RELAY_OWNERSHIP_CONTRACT
    observed = observe(conn)
    ownership = observe_ownership(conn)
    if not ownership.definer_role_exists or not ownership.migration_role_exists:
        for violation in relay_ownership_violations(ownership, contract=contract):
            print(f"DRIFT: {violation}", file=sys.stderr)
        return 1
    wrong_existing = [
        violation
        for violation in (
            *relay_dispatcher_violations(observed),
            *relay_ownership_violations(
                ownership,
                contract=contract,
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

    ensure_current_schema_privileges(conn, dry_run=dry_run)
    return 0


def verify(conn: psycopg.Connection) -> int:
    ownership = observe_ownership(conn)
    violations = (
        *relay_dispatcher_violations(observe(conn)),
        *relay_ownership_violations(ownership),
        *(
            ("retired dotmac_app membership in app_admin remains",)
            if observe_historical_membership(conn)
            else ()
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
        parser.error("--verify-only cannot be combined with repair")

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
            return bootstrap(
                conn,
                dry_run=args.dry_run,
                repair=args.repair,
            )
    except psycopg.Error:
        print(
            "database connection or role operation failed; connection details "
            "were deliberately not logged",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
