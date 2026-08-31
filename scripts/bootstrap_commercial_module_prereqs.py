#!/usr/bin/env python3
"""Create or verify composed module database prerequisites.

This is an explicitly privileged bootstrap, separate from ordinary Alembic
execution. It never sets or prints passwords. Alembic verifies the resulting
catalog contract before module migrations run; this script is the owner that
creates or repairs cluster roles and module schemas.

Three modes, because two different credentials do two different jobs:

``--repair``
    The elevated provisioning mode. Creates cluster roles *and* schemas. Run
    out of band by an operator holding a superuser connection, once per
    environment.

``--repair-schemas``
    The mode the deployment runs, holding only ``dotmac_schema_bootstrap``:
    CONNECT + CREATE on this database and nothing else. It creates and repairs
    schemas. It cannot create roles — the credential is NOCREATEROLE — so a
    missing or mis-postured role is reported as ``blocked``, never worked
    around.

``--verify-only``
    Read-only, through the restricted migration role.

Usage::

    BOOTSTRAP_DATABASE_URL=postgresql://postgres@host/db \\
        python scripts/bootstrap_commercial_module_prereqs.py --repair

    BOOTSTRAP_DATABASE_URL=postgresql://dotmac_schema_bootstrap@127.0.0.1:9001/db \\
    PGPASSFILE=/etc/dotmac/sub/schema-bootstrap.pgpass \\
        python scripts/bootstrap_commercial_module_prereqs.py --repair-schemas

    MIGRATION_DATABASE_URL=postgresql://dotmac_app@host/db \\
        python scripts/bootstrap_commercial_module_prereqs.py --verify-only

Exit codes: 0 satisfied or repaired, 1 contract drift, 2 usage/connection
error, 3 blocked — repair is required but this credential cannot perform it.
``blocked`` is deliberately its own code: the defect this script was rewritten
to close was a path that returned success both when there was nothing to do and
when nothing could be done.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg import sql

from app.commercial_module_prereqs import (
    COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
    PROBED_SCHEMA_PRIVILEGES,
    PUBLIC_PROBE_ROLE,
    SCHEMA_BOOTSTRAP_ROLE,
    DatabaseRoleContract,
    ModuleSchemaObservation,
    RolePosture,
    commercial_bootstrap_role_violations,
    commercial_schema_violations,
    module_schema_contract,
)

BOOTSTRAP_URL_VAR = "BOOTSTRAP_DATABASE_URL"
MIGRATION_URL_VAR = "MIGRATION_DATABASE_URL"

#: Diagnostics are explicit but bounded: an unbounded dump of a broken catalog
#: buries the first useful line under hundreds of derived ones.
MAX_REPORTED_VIOLATIONS = 40

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3


class Outcome(str, Enum):
    """What actually happened, as a word the deploy owner can branch on."""

    ALREADY_SATISFIED = "already_satisfied"
    REPAIRED = "repaired"
    BLOCKED = "blocked"


@dataclass
class BootstrapResult:
    outcome: Outcome
    exit_code: int
    schemas_total: int = 0
    schemas_created: int = 0
    schemas_adopted: int = 0
    schemas_regranted: int = 0
    roles_created: int = 0
    roles_verified: int = 0
    violations: tuple[str, ...] = ()
    blocked_reason: str | None = None
    notes: list[str] = field(default_factory=list)


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


def _report(violations: tuple[str, ...], prefix: str) -> None:
    """Print every violation, bounded, and say how many were withheld."""

    for violation in violations[:MAX_REPORTED_VIOLATIONS]:
        print(f"{prefix}: {violation}", file=sys.stderr)
    withheld = len(violations) - MAX_REPORTED_VIOLATIONS
    if withheld > 0:
        print(
            f"{prefix}: ... and {withheld} further violation(s) not listed; "
            "fix the ones above and re-run to see the rest.",
            file=sys.stderr,
        )


@contextmanager
def _as_role(conn: psycopg.Connection, role: str) -> Iterator[None]:
    """Run a block as ``role``.

    ``dotmac_schema_bootstrap`` is NOINHERIT on purpose, so holding membership
    in ``dotmac_app`` does not silently confer its privileges. Owner-only DDL
    (REVOKE from PUBLIC, GRANT USAGE) therefore has to SET ROLE explicitly,
    which is also what keeps the schema owner correct rather than incidental.
    """

    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    try:
        yield
    finally:
        try:
            conn.execute("RESET ROLE")
        except psycopg.Error:
            # The block already failed and aborted the transaction, so RESET
            # ROLE cannot run either. Swallowing it lets the ORIGINAL error
            # surface; without this the caller sees InFailedSqlTransaction and
            # the real cause is lost — which is the whole failure mode this
            # change exists to stop.
            pass


def observe_roles(conn: psycopg.Connection) -> dict[str, RolePosture]:
    """Read only the posture flags owned by the checked-in contract."""

    rows = conn.execute(
        "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper "
        "FROM pg_roles WHERE rolname = ANY(%s)",
        (list(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT),),
    ).fetchall()
    return {str(row[0]): (bool(row[1]), bool(row[2]), bool(row[3])) for row in rows}


def _role_exists(conn: psycopg.Connection, role: str) -> bool:
    row = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    return row is not None


def _public_schema_privileges(
    conn: psycopg.Connection, schema_name: str
) -> tuple[str, ...]:
    """ACL rows granted to PUBLIC. Corroboration for the probe, not the proof."""

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


def _probe_schema_privileges(
    conn: psycopg.Connection, schema_name: str
) -> tuple[str, ...]:
    """Schema privileges the no-privilege probe role EFFECTIVELY holds.

    ``has_schema_privilege`` is transitive through role membership, so a probe
    that is a member of nothing and granted nothing answers the question
    "could an unprivileged role reach this schema?" — which is the question
    "no privileges for PUBLIC" actually asks. It must come back empty.
    """

    held: list[str] = []
    for privilege in PROBED_SCHEMA_PRIVILEGES:
        row = conn.execute(
            "SELECT has_schema_privilege(%s, %s, %s)",
            (PUBLIC_PROBE_ROLE, schema_name, privilege),
        ).fetchone()
        if row and bool(row[0]):
            held.append(privilege)
    return tuple(held)


def observe_schemas(conn: psycopg.Connection) -> dict[str, ModuleSchemaObservation]:
    """Read the catalog state for every derived module schema."""

    contract = module_schema_contract()
    schema_names = [item.schema for item in contract]
    rows = conn.execute(
        "SELECT n.nspname, pg_get_userbyid(n.nspowner) "
        "FROM pg_namespace AS n WHERE n.nspname = ANY(%s)",
        (schema_names,),
    ).fetchall()
    owners = {str(row[0]): str(row[1]) for row in rows}
    existing_roles = set(observe_roles(conn))
    probe_present = PUBLIC_PROBE_ROLE in existing_roles
    observed: dict[str, ModuleSchemaObservation] = {}
    for expected in contract:
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
            probe_privileges=(
                _probe_schema_privileges(conn, expected.schema) if probe_present else ()
            ),
            probe_observed=probe_present,
        )
    return observed


def _bootstrap_roles(
    conn: psycopg.Connection,
    *,
    dry_run: bool,
    repair: bool,
    allow_role_creation: bool,
    result: BootstrapResult,
) -> int:
    observed = observe_roles(conn)
    violations = commercial_bootstrap_role_violations(observed)
    result.roles_verified = len(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT) - len(violations)

    if violations and not allow_role_creation:
        # The deployment credential is NOCREATEROLE by design. Say so, name the
        # fix, and refuse — do not degrade into a partial repair.
        _report(violations, "COMMERCIAL MODULE PREREQUISITE")
        result.outcome = Outcome.BLOCKED
        result.violations = violations
        result.blocked_reason = (
            f"cluster roles are not provisioned and {SCHEMA_BOOTSTRAP_ROLE!r} is "
            "NOCREATEROLE by design. Provision the roles out of band with an "
            "elevated connection (--repair), then re-run the deployment."
        )
        return EXIT_BLOCKED

    wrong_existing = [
        violation for violation in violations if not violation.endswith("is missing")
    ]
    if wrong_existing and not repair:
        for violation in wrong_existing:
            print(
                f"DRIFT: {violation}. Re-run with --repair to correct it; "
                "rewriting cluster access is deliberately opt-in.",
                file=sys.stderr,
            )
        return EXIT_DRIFT

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
                result.roles_created += 1
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
    return EXIT_OK


def _bootstrap_schemas(
    conn: psycopg.Connection,
    *,
    dry_run: bool,
    repair: bool,
    result: BootstrapResult,
) -> int:
    contract = module_schema_contract()
    result.schemas_total = len(contract)
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
        return EXIT_DRIFT

    for expected in contract:
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
                result.schemas_created += 1
                print(f"created schema: {expected.schema} owner={expected.owner_role}")
        elif actual.owner_role == expected.owner_role:
            result.schemas_adopted += 1
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
            for role in expected.usage_roles:
                print(f"would grant schema usage: {expected.schema} to {role}")
            continue

        # Owner-only DDL. SET ROLE rather than relying on inheritance, because
        # the deployment credential is NOINHERIT.
        with _as_role(conn, expected.owner_role):
            conn.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema_id)
            )
            print(f"revoked schema public access: {expected.schema}")
            for role in expected.usage_roles:
                conn.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        schema_id, sql.Identifier(role)
                    )
                )
                print(f"granted schema usage: {expected.schema} to {role}")
        result.schemas_regranted += 1
    return EXIT_OK


def _all_violations(conn: psycopg.Connection) -> tuple[str, ...]:
    return (
        *commercial_bootstrap_role_violations(observe_roles(conn)),
        *commercial_schema_violations(observe_schemas(conn)),
    )


def _print_summary(result: BootstrapResult, *, mode: str) -> None:
    """One structured line the deploy owner and a human can both read."""

    print(
        "PREREQUISITE SUMMARY: "
        f"mode={mode} "
        f"outcome={result.outcome.value} "
        f"schemas_total={result.schemas_total} "
        f"schemas_created={result.schemas_created} "
        f"schemas_adopted={result.schemas_adopted} "
        f"schemas_regranted={result.schemas_regranted} "
        f"roles_created={result.roles_created} "
        f"roles_verified={result.roles_verified} "
        f"violations={len(result.violations)}"
    )
    # Machine-readable and last, so a caller can tail one line. `blocked` is a
    # distinct word AND a distinct exit code; nothing downstream should have to
    # infer failure from an empty diff.
    print(f"PREREQUISITE OUTCOME: {result.outcome.value}")


def run_bootstrap(
    conn: psycopg.Connection,
    *,
    dry_run: bool,
    repair: bool,
    allow_role_creation: bool = True,
) -> BootstrapResult:
    """Bring the prerequisites to contract, reporting what actually happened."""

    result = BootstrapResult(outcome=Outcome.ALREADY_SATISFIED, exit_code=EXIT_OK)
    contract = module_schema_contract()
    result.schemas_total = len(contract)

    standing = _all_violations(conn)
    if not standing:
        # Nothing to do. Say so explicitly and mutate nothing — an
        # already-satisfied database must not be written to just because a
        # repair flag was passed.
        result.roles_verified = len(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT)
        result.schemas_adopted = len(contract)
        result.outcome = Outcome.ALREADY_SATISFIED
        result.exit_code = EXIT_OK
        return result

    roles_code = _bootstrap_roles(
        conn,
        dry_run=dry_run,
        repair=repair,
        allow_role_creation=allow_role_creation,
        result=result,
    )
    if roles_code != EXIT_OK:
        result.exit_code = roles_code
        if result.outcome is not Outcome.BLOCKED:
            result.outcome = Outcome.BLOCKED
            result.violations = standing
            result.blocked_reason = result.blocked_reason or (
                "cluster role contract is not satisfied"
            )
        return result

    schemas_code = _bootstrap_schemas(
        conn, dry_run=dry_run, repair=repair, result=result
    )
    if schemas_code != EXIT_OK:
        result.exit_code = schemas_code
        result.outcome = Outcome.BLOCKED
        result.violations = standing
        result.blocked_reason = "module schema contract is not satisfied"
        return result

    if dry_run:
        result.outcome = Outcome.ALREADY_SATISFIED
        result.exit_code = EXIT_OK
        return result

    # Prove the repair rather than assume it: re-read the catalog through the
    # same connection and require a clean contract before claiming `repaired`.
    remaining = _all_violations(conn)
    if remaining:
        _report(remaining, "COMMERCIAL MODULE PREREQUISITE")
        result.outcome = Outcome.BLOCKED
        result.violations = remaining
        result.blocked_reason = "the repair ran but the contract is still not satisfied"
        result.exit_code = EXIT_DRIFT
        return result

    result.outcome = Outcome.REPAIRED
    result.exit_code = EXIT_OK
    return result


def bootstrap(conn: psycopg.Connection, *, dry_run: bool, repair: bool) -> int:
    """Backwards-compatible integer entry point used by the CI bootstrapper."""

    return run_bootstrap(conn, dry_run=dry_run, repair=repair).exit_code


def verify(conn: psycopg.Connection) -> int:
    violations = _all_violations(conn)
    _report(violations, "COMMERCIAL MODULE PREREQUISITE")
    if violations:
        return EXIT_DRIFT
    contract = module_schema_contract()
    print(
        "PREREQUISITE SUMMARY: mode=verify-only "
        f"outcome={Outcome.ALREADY_SATISFIED.value} "
        f"schemas_total={len(contract)} "
        f"schemas_verified={len(contract)} "
        f"roles_verified={len(COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT)} "
        f"public_denial_probe={PUBLIC_PROBE_ROLE} "
        "violations=0"
    )
    print(f"PREREQUISITE OUTCOME: {Outcome.ALREADY_SATISFIED.value}")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument(
        "--repair-schemas",
        action="store_true",
        help=(
            "repair schemas only, holding the NOCREATEROLE deployment "
            "credential; a missing cluster role is reported as blocked"
        ),
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only and (args.dry_run or args.repair or args.repair_schemas):
        parser.error(
            "--verify-only cannot be combined with --dry-run, --repair or "
            "--repair-schemas"
        )
    if args.repair and args.repair_schemas:
        parser.error("--repair and --repair-schemas are different credentials")

    url_var = MIGRATION_URL_VAR if args.verify_only else BOOTSTRAP_URL_VAR
    url = os.environ.get(url_var, "").strip()
    if not url:
        print(
            f"{url_var} is not set; commercial module bootstrap is separate "
            "from the application connection string.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    mode = (
        "verify-only"
        if args.verify_only
        else "repair-schemas"
        if args.repair_schemas
        else "repair"
        if args.repair
        else "create-missing"
    )

    try:
        connect_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(connect_url, autocommit=False) as conn:
            if args.verify_only:
                return verify(conn)
            result = run_bootstrap(
                conn,
                dry_run=args.dry_run,
                repair=args.repair or args.repair_schemas,
                allow_role_creation=not args.repair_schemas,
            )
            if result.blocked_reason:
                print(
                    f"COMMERCIAL MODULE PREREQUISITE BLOCKED: {result.blocked_reason}",
                    file=sys.stderr,
                )
            _print_summary(result, mode=mode)
            if result.outcome is Outcome.BLOCKED and result.exit_code == EXIT_OK:
                # Defensive: a blocked outcome must never leave with success.
                return EXIT_BLOCKED
            return result.exit_code
    except psycopg.OperationalError:
        # Connection-shaped failure: the credential, host or database is wrong.
        # Named separately from other psycopg errors so the deploy owner can
        # tell "cannot reach/authenticate" from "the statement failed".
        print(
            f"COMMERCIAL MODULE PREREQUISITE BLOCKED: could not open a "
            f"{mode} connection using {url_var}; the credential holder is "
            "unavailable or rejected. Connection details were deliberately "
            "not logged.",
            file=sys.stderr,
        )
        print(f"PREREQUISITE OUTCOME: {Outcome.BLOCKED.value}")
        return EXIT_BLOCKED
    except psycopg.Error:
        print(
            "database connection or prerequisite operation failed; connection "
            "details were deliberately not logged",
            file=sys.stderr,
        )
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
