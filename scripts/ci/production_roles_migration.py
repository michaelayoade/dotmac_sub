#!/usr/bin/env python3
"""Rehearse the production migration identity on production-shaped roles.

CI and staging migrate as a PostgreSQL superuser, which hid that production
migrates as the NOBYPASSRLS runtime role ``dotmac_app``: the Starter
``dotmac-billing`` migrations ``bi_0006``/``bi_0007``/``bi_0008`` run
``SET LOCAL row_security = off`` and fail closed for any role that cannot
bypass RLS (2026-10-20 production deploy). This job reproduces the production
shape and proves the fix:

1. a superuser (used ONLY here, to provision) creates the database, the
   extensions and, through the repository's own ``--repair`` bootstraps, the
   cluster roles: ``dotmac_app`` (NOBYPASSRLS, owns the schema, FORCE RLS) and
   ``dotmac_sub_migrator`` (BYPASSRLS, NOSUPERUSER);
2. the restricted ``--verify-only`` preflight passes as ``dotmac_app``;
3. the chain is upgraded as the migrator, in production mode, to the
   production pre-deploy revision set;
4. one confirmed settlement is seeded as ``dotmac_app`` with the tenant GUC;
5. the database is cloned, and ``alembic upgrade heads`` runs as the migrator:
   exact heads, the settlement's cash-source claim exists, nothing is left
   owned by the migrator, FORCE RLS and ``dotmac_app`` NOBYPASSRLS are intact;
6. SENSITIVITY: the same chain run on the clone as ``dotmac_app`` (the legacy,
   pre-fix identity) must fail at ``bi_0006`` with InsufficientPrivilege and
   leave the revisions unchanged. If this ever passes, the job no longer
   reproduces the production failure class (for example it drifted back to a
   superuser) and proves nothing;
7. production mode refuses to run as ``dotmac_app``.

Every credential is a per-run random value read by libpq from a 0400 pgpass
file. Nothing secret is printed or placed in a URL.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.commercial_module_prereqs import (  # noqa: E402
    MIGRATOR_ROLE,
    RUNTIME_OWNER_ROLE,
)
from scripts.ci.migrated_test_database import repository_heads  # noqa: E402

HOST = os.environ.get("PRODROLES_PGHOST", "localhost")
PORT = os.environ.get("PRODROLES_PGPORT", "5432")
ADMIN_USER = os.environ.get("PRODROLES_ADMIN_USER", "postgres")
ADMIN_PASSWORD = os.environ.get("PRODROLES_ADMIN_PASSWORD", "postgres")
MAIN_DB = "dotmac_sub_migration_prodroles"
SENSITIVITY_DB = "dotmac_sub_migration_prodroles_legacy"

#: The ``alembic_version`` rows of production immediately before the failed
#: 2026-10-20 deploy (a8 pin not yet applied). Each is applied on its own so the
#: lineage stops exactly there. If a later release removes one of these
#: revisions, move this snapshot forward to the then-current production heads;
#: the job must still stop BEFORE ``bi_0006`` for the sensitivity check to hold.
PRODUCTION_PRE_DEPLOY_REVISIONS: tuple[str, ...] = (
    "690_backdated_invoice_permissions",
    "bi_0001_billing",
    "cl_0001_collections",
    "ib_0001_conversations",
    "nu_0001_numbering",
    "pm_0003_confirmation_immutable",
    "so_0001_service_delivery_orders",
    "su_0003_billing_treatments",
    "ts_0002_notify_identity",
    "tx_0003_result_fingerprint",
)

EXTENSIONS = ("postgis", "postgis_topology", "pg_trgm", "btree_gist")


class Failure(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"FAIL: {message}")


def ok(message: str) -> None:
    print(f"OK: {message}", flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)
    ok(message)


def conninfo(db: str, user: str) -> str:
    # No password: libpq reads it from PGPASSFILE.
    return f"host={HOST} port={PORT} dbname={db} user={user}"


def connect(db: str, user: str) -> psycopg.Connection:
    return psycopg.connect(conninfo(db, user), autocommit=True)


def sa_url(db: str, user: str) -> str:
    return f"postgresql+psycopg://{user}@{HOST}:{PORT}/{db}"


def write_pgpass(directory: Path, passwords: dict[str, str]) -> Path:
    lines = []
    for user, password in passwords.items():
        for db in ("postgres", MAIN_DB, SENSITIVITY_DB):
            lines.append(f"{HOST}:{PORT}:{db}:{user}:{password}")
    path = directory / "pgpass"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o400)
    return path


def child_env(pgpass: Path, db: str, extra: dict[str, str] | None = None) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MIGRATION_") and key != "BOOTSTRAP_DATABASE_URL"
    }
    env["PGPASSFILE"] = str(pgpass)
    # Runtime URL, as the deployed app holds it. In production mode Alembic
    # must not use it for migrating.
    env["DATABASE_URL"] = sa_url(db, RUNTIME_OWNER_ROLE)
    env.update(extra or {})
    return env


def run(
    argv: list[str], env: dict, *, expect_ok: bool, label: str
) -> subprocess.CompletedProcess:
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    output = result.stdout + result.stderr
    if expect_ok and result.returncode != 0:
        print(output[-6000:], file=sys.stderr)
        raise Failure(f"{label} exited {result.returncode}")
    if not expect_ok and result.returncode == 0:
        print(output[-3000:], file=sys.stderr)
        raise Failure(f"{label} unexpectedly succeeded")
    return result


def alembic(
    pgpass: Path, db: str, user: str, revision: str, **kw
) -> subprocess.CompletedProcess:
    """Run ``alembic upgrade`` exactly as the production deploy step does."""
    extra = {
        "MIGRATION_DATABASE_URL": sa_url(db, user),
    }
    if kw.pop("production_mode", True):
        extra["MIGRATION_SCHEMA_OWNER"] = RUNTIME_OWNER_ROLE
        extra["MIGRATION_PRODUCTION_MODE"] = "1"
    return run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        child_env(pgpass, db, extra),
        label=f"alembic upgrade {revision} as {user}",
        **kw,
    )


def versions(db: str) -> set[str]:
    with connect(db, ADMIN_USER) as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT version_num FROM alembic_version")
        }


def provision(directory: Path) -> Path:
    passwords = {
        ADMIN_USER: ADMIN_PASSWORD,
        RUNTIME_OWNER_ROLE: secrets.token_urlsafe(24),
        MIGRATOR_ROLE: secrets.token_urlsafe(24),
    }
    pgpass = write_pgpass(directory, passwords)
    os.environ["PGPASSFILE"] = str(pgpass)

    with connect("postgres", ADMIN_USER) as conn:
        for db in (SENSITIVITY_DB, MAIN_DB):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(db)
                )
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(MAIN_DB)))
    with connect(MAIN_DB, ADMIN_USER) as conn:
        for extension in EXTENSIONS:
            conn.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                    sql.Identifier(extension)
                )
            )

    # The repository's own elevated one-off provisioning, as an operator runs
    # it. It sets no password.
    bootstrap_env = {
        **child_env(pgpass, MAIN_DB),
        "BOOTSTRAP_DATABASE_URL": (
            f"postgresql://{ADMIN_USER}@{HOST}:{PORT}/{MAIN_DB}"
        ),
    }
    for script in (
        "scripts/bootstrap_commercial_module_prereqs.py",
        "scripts/bootstrap_outbox_dispatcher_roles.py",
    ):
        run(
            [sys.executable, script, "--repair"],
            bootstrap_env,
            expect_ok=True,
            label=script,
        )

    with connect(MAIN_DB, ADMIN_USER) as conn:
        for role in (RUNTIME_OWNER_ROLE, MIGRATOR_ROLE):
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(passwords[role])
                )
            )
        # Baseline the host provides: the runtime owner may create in public;
        # it deliberately has no database-level CREATE.
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                sql.Identifier(RUNTIME_OWNER_ROLE)
            )
        )
        flags = conn.execute(
            "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles "
            "WHERE rolname = ANY(%s)",
            ([RUNTIME_OWNER_ROLE, MIGRATOR_ROLE],),
        ).fetchall()
    posture = {name: (bool(b), bool(s)) for name, b, s in flags}
    require(
        posture.get(RUNTIME_OWNER_ROLE) == (False, False),
        "dotmac_app is NOBYPASSRLS and not a superuser",
    )
    require(
        posture.get(MIGRATOR_ROLE) == (True, False),
        "dotmac_sub_migrator is BYPASSRLS and not a superuser",
    )
    require(
        not runtime_is_definer_member(),
        "the repairs did not grant dotmac_app membership of the definer role",
    )
    return pgpass


def runtime_is_definer_member() -> bool:
    with connect(MAIN_DB, ADMIN_USER) as conn:
        row = conn.execute(
            "SELECT pg_has_role(%s, 'app_admin', 'MEMBER')", (RUNTIME_OWNER_ROLE,)
        ).fetchone()
    return bool(row and row[0])


def repair_again(pgpass: Path) -> None:
    """The elevated repairs must be idempotent and must not re-grant the runtime
    role any membership of the definer role."""
    bootstrap_env = {
        **child_env(pgpass, MAIN_DB),
        "BOOTSTRAP_DATABASE_URL": (
            f"postgresql://{ADMIN_USER}@{HOST}:{PORT}/{MAIN_DB}"
        ),
    }
    for script in (
        "scripts/bootstrap_commercial_module_prereqs.py",
        "scripts/bootstrap_outbox_dispatcher_roles.py",
    ):
        run(
            [sys.executable, script, "--repair"],
            bootstrap_env,
            expect_ok=True,
            label=f"{script} --repair (second run)",
        )


def preflight(pgpass: Path) -> None:
    """The deploy's restricted --verify-only, run as the runtime role."""
    for script in (
        "scripts/bootstrap_commercial_module_prereqs.py",
        "scripts/bootstrap_outbox_dispatcher_roles.py",
    ):
        run(
            [sys.executable, script, "--verify-only"],
            child_env(
                pgpass,
                MAIN_DB,
                {"MIGRATION_DATABASE_URL": sa_url(MAIN_DB, RUNTIME_OWNER_ROLE)},
            ),
            expect_ok=True,
            label=f"{script} --verify-only as dotmac_app",
        )
    ok("restricted preflight passes (migrator contract verified as dotmac_app)")


def seed(pgpass: Path) -> tuple[str, str]:
    """One tenant (from migrations) and one confirmed settlement."""
    with connect(MAIN_DB, ADMIN_USER) as conn:
        row = conn.execute(
            "SELECT id::text FROM public.tenants ORDER BY created_at LIMIT 1"
        ).fetchone()
    if row is None:
        raise Failure("the migration chain created no tenant to seed against")
    tenant = row[0]
    account, settlement = str(uuid.uuid4()), str(uuid.uuid4())
    with connect(MAIN_DB, RUNTIME_OWNER_ROLE) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        conn.execute(
            "INSERT INTO mod_billing.billing_accounts "
            "(id, tenant_id, external_account_ref, currency, minor_units, created_at) "
            "VALUES (%s, %s, 'ci-account', 'NGN', 2, now())",
            (account, tenant),
        )
        conn.execute(
            "INSERT INTO mod_billing.confirmed_settlements "
            "(id, tenant_id, billing_account_id, source_system, "
            " source_settlement_key, source_version, request_fingerprint, amount, "
            " currency, minor_units, occurred_at, observed_at, "
            " confirmation_evidence, funding_lane, created_at) "
            "VALUES (%s, %s, %s, 'ci-fixture', 'ci-settlement-1', 'v1', %s, "
            " 100.00, 'NGN', 2, now(), now(), 'ci-fixture', 'available_credit', now())",
            (settlement, tenant, account, "0" * 64),
        )
        count = conn.execute(
            "SELECT count(*) FROM mod_billing.confirmed_settlements"
        ).fetchone()
    require(count is not None and count[0] == 1, "one confirmed settlement seeded")
    return tenant, settlement


def clone_for_sensitivity() -> None:
    with connect("postgres", ADMIN_USER) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(SENSITIVITY_DB), sql.Identifier(MAIN_DB)
            )
        )


def owned_by_migrator(db: str) -> dict[str, int]:
    queries = {
        "relations": "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
        "WHERE r.rolname = %(m)s",
        "functions": "SELECT count(*) FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
        "WHERE r.rolname = %(m)s",
        "types": "SELECT count(*) FROM pg_type t JOIN pg_roles r ON r.oid = t.typowner "
        "WHERE r.rolname = %(m)s AND t.typrelid = 0 AND t.typelem = 0",
        "schemas": "SELECT count(*) FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
        "WHERE r.rolname = %(m)s",
    }
    with connect(db, ADMIN_USER) as conn:
        return {
            label: int(conn.execute(query, {"m": MIGRATOR_ROLE}).fetchone()[0])
            for label, query in queries.items()
        }


def assert_upgraded_state(db: str, tenant: str, settlement: str) -> None:
    require(
        versions(db) == set(repository_heads()),
        "alembic_version equals the repository's exact heads",
    )
    with connect(db, ADMIN_USER) as conn:
        claims = conn.execute(
            "SELECT settlement_id::text FROM mod_billing.cash_source_claims"
        ).fetchall()
        require(
            [row[0] for row in claims] == [settlement],
            "bi_0006 backfilled exactly one cash-source claim for the settlement",
        )
        unforced = conn.execute(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname LIKE 'mod\\_%%' AND c.relkind = 'r' "
            "AND NOT c.relforcerowsecurity AND c.relname NOT LIKE 'platform\\_%%'"
        ).fetchall()
        require(not unforced, "every tenant-plane mod_* table is still FORCE RLS")
        wrong_owner = conn.execute(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE n.nspname LIKE 'mod\\_%%' AND c.relkind = 'r' AND r.rolname <> %s",
            (RUNTIME_OWNER_ROLE,),
        ).fetchall()
        require(not wrong_owner, "every mod_* table is owned by dotmac_app")
        runtime = conn.execute(
            "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = %s",
            (RUNTIME_OWNER_ROLE,),
        ).fetchone()
        require(runtime == (False, False), "dotmac_app is still NOBYPASSRLS")
    residue = owned_by_migrator(db)
    require(
        not any(residue.values()),
        f"nothing is owned by the migrator after the upgrade ({residue})",
    )
    with connect(db, RUNTIME_OWNER_ROLE) as conn:
        without = conn.execute(
            "SELECT count(*) FROM mod_billing.cash_source_claims"
        ).fetchone()
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        with_tenant = conn.execute(
            "SELECT count(*) FROM mod_billing.cash_source_claims"
        ).fetchone()
    require(
        without is not None and without[0] == 0,
        "runtime dotmac_app sees 0 claims without the tenant GUC",
    )
    require(
        with_tenant is not None and with_tenant[0] == 1,
        "runtime dotmac_app sees 1 claim with the tenant GUC",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="prodroles-") as raw:
        directory = Path(raw)
        directory.chmod(0o700)
        pgpass = provision(directory)
        preflight(pgpass)

        # (i) The production pre-deploy state, built as the migrator.
        for revision in PRODUCTION_PRE_DEPLOY_REVISIONS:
            alembic(pgpass, MAIN_DB, MIGRATOR_ROLE, revision, expect_ok=True)
        require(
            versions(MAIN_DB) == set(PRODUCTION_PRE_DEPLOY_REVISIONS),
            "pre-deploy state equals the production head set",
        )
        residue = owned_by_migrator(MAIN_DB)
        require(
            not any(residue.values()),
            f"the pre-deploy build left nothing owned by the migrator ({residue})",
        )
        tenant, settlement = seed(pgpass)
        clone_for_sensitivity()

        # (ii) The deploy's Alembic step: migrator, production mode.
        alembic(pgpass, MAIN_DB, MIGRATOR_ROLE, "heads", expect_ok=True)
        assert_upgraded_state(MAIN_DB, tenant, settlement)
        repair_again(pgpass)
        require(
            not runtime_is_definer_member(),
            "a second --repair still leaves dotmac_app outside the definer role",
        )
        preflight(pgpass)

        # (iii) Sensitivity: the same chain as restricted dotmac_app (the
        # legacy identity, no production flags) must fail at bi_0006.
        before = versions(SENSITIVITY_DB)
        result = alembic(
            pgpass,
            SENSITIVITY_DB,
            RUNTIME_OWNER_ROLE,
            "heads",
            production_mode=False,
            expect_ok=False,
        )
        output = result.stdout + result.stderr
        running = [line for line in output.splitlines() if "Running upgrade" in line]
        require(bool(running), "the legacy run started upgrading")
        require(
            "bi_0006_cash_source_claim" in running[-1],
            "the legacy run as dotmac_app failed at bi_0006 "
            f"(last revision attempted: {running[-1].strip()[-80:]})",
        )
        require(
            "InsufficientPrivilege" in output or "row-level security policy" in output,
            "the legacy failure is the RLS InsufficientPrivilege error",
        )
        require(
            versions(SENSITIVITY_DB) == before,
            "the failed legacy run rolled back completely",
        )

        # (iv) Production mode refuses to run as the runtime role.
        refusal = alembic(
            pgpass,
            SENSITIVITY_DB,
            RUNTIME_OWNER_ROLE,
            "heads",
            expect_ok=False,
        )
        require(
            "must connect as 'dotmac_sub_migrator'" in refusal.stdout + refusal.stderr,
            "production mode refuses dotmac_app as the migration identity",
        )
        require(
            versions(SENSITIVITY_DB) == before,
            "the refused run changed nothing",
        )
    ok("production-role migration rehearsal complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
