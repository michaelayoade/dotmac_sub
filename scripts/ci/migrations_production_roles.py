#!/usr/bin/env python3
"""Migrate a fresh database with production-shaped roles, then audit the result.

Every other CI migration runs as a superuser, which cannot fail the way a
production deploy can. This lane reproduces the production role shape:

* ``dotmac_app``: LOGIN, NOBYPASSRLS, not superuser; the runtime role. It owns
  the objects that predate the grant model, holds USAGE but NOT CREATE on
  ``public``, and is bound by row-level security;
* ``app_admin``: LOGIN, BYPASSRLS, not superuser; the migrator
  (``module_database_roles.v1``), a member of ``dotmac_app`` with INHERIT and SET
  and no ADMIN. ``dotmac_app`` is NOT a member of ``app_admin``.

It runs ``alembic upgrade heads`` as ``app_admin`` with
``MIGRATION_RUNTIME_ROLE=dotmac_app`` and asserts the Starter/ERP model: the
migrator owns every new object, the runtime role holds the DML (sequence)
grants on all of them, the runtime role cannot create in ``public`` or run DDL
on what the migrator owns, every module table is FORCE row-level security and
the runtime role sees nothing without a tenant. It then proves the lane is
sensitive: without the edge, with the old direction, and as the runtime role,
the same setup fails.

Role passwords are generated per run for this disposable cluster and are never
printed. Connection details are never printed.

Usage::

    PRODROLES_ADMIN_URL=postgresql://postgres:...@localhost:5432/postgres \\
        python -m scripts.ci.migrations_production_roles
"""

from __future__ import annotations

import os
import secrets
import subprocess  # nosec B404 - runs this repository's own alembic
import sys
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.bootstrap_commercial_module_prereqs import (  # noqa: E402
    bootstrap as bootstrap_modules,
)
from scripts.bootstrap_outbox_dispatcher_roles import (  # noqa: E402
    bootstrap as bootstrap_outbox,
)
from scripts.bootstrap_outbox_dispatcher_roles import (  # noqa: E402
    verify as verify_outbox,
)

ADMIN_URL_VAR = "PRODROLES_ADMIN_URL"
OWNER = "dotmac_app"
MIGRATOR = "app_admin"
EXTENSIONS = ("postgis", "postgis_topology", "pg_trgm", "btree_gist", "pgcrypto")
RELAY_DEFINERS = frozenset(
    {
        "claim_outbox_batch",
        "settle_outbox_event",
        "claim_platform_outbox_batch",
        "settle_platform_outbox_event",
    }
)
ALEMBIC_TAIL_LINES = 40

#: Objects in the application schemas that are not extension members.
_APP_SCHEMAS = "(n.nspname = 'public' OR n.nspname LIKE 'mod\\_%%')"
_NOT_EXTENSION = (
    "NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.deptype = 'e' "
    "AND e.classid = '{catalog}'::regclass AND e.objid = {alias}.oid)"
)


# --------------------------------------------------------------------------
# Pure assertions (unit-tested in tests/scripts/test_migrations_production_roles.py)
# --------------------------------------------------------------------------


def edge_violations(
    edges: Iterable[tuple[str, str, bool, bool, bool]],
) -> tuple[str, ...]:
    """``(role, member, admin_option, inherit_option, set_option)`` rows."""

    rows = list(edges)
    violations: list[str] = []
    new = [row for row in rows if (row[0], row[1]) == (OWNER, MIGRATOR)]
    old = [row for row in rows if (row[0], row[1]) == (MIGRATOR, OWNER)]
    if not new:
        violations.append(f"{MIGRATOR} is not a member of {OWNER}")
    for _role, _member, admin, inherit, set_option in new:
        if admin:
            violations.append(f"{MIGRATOR} holds ADMIN OPTION on {OWNER}")
        if not inherit:
            violations.append(f"{MIGRATOR} lacks INHERIT on {OWNER}")
        if not set_option:
            violations.append(f"{MIGRATOR} lacks SET on {OWNER}")
    if old:
        violations.append(f"{OWNER} is a member of {MIGRATOR} (old direction)")
    return tuple(violations)


def migrator_owned_violations(owned: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """``(catalog, name)`` rows owned by the migrator after a migration.

    The migrator owns every new object in the grant model, so ownership of a
    table or sequence is not a finding. What must hold is that the four relay
    SECURITY DEFINER functions are among them.
    """

    return tuple(
        f"relay definer {name} is not owned by {MIGRATOR}"
        for name in sorted(RELAY_DEFINERS - {n for c, n in owned if c == "pg_proc"})
    )


def grant_gap_findings(
    rows: Iterable[tuple[str, str, Sequence[str]]],
) -> tuple[str, ...]:
    """``(kind, name, missing privileges)`` rows for migrator-owned relations."""

    return tuple(
        f"{kind} {name}: {OWNER} lacks {', '.join(missing)}"
        for kind, name, missing in rows
        if missing
    )


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _sqlalchemy_url(url: URL) -> str:
    return url.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


class Lane:
    """One disposable cluster, driven as a superuser, migrated as roles."""

    def __init__(self, admin_url: URL) -> None:
        self.admin_url = admin_url
        self.passwords = {
            OWNER: secrets.token_urlsafe(24),
            MIGRATOR: secrets.token_urlsafe(24),
        }
        self.failures: list[str] = []

    # -- connections --------------------------------------------------------
    def superuser(self, database: str | None = None) -> psycopg.Connection:
        url = (
            self.admin_url
            if database is None
            else self.admin_url.set(database=database)
        )
        return psycopg.connect(_psycopg_url(url), autocommit=True)

    def as_role(self, role: str, database: str) -> URL:
        return self.admin_url.set(
            username=role, password=self.passwords[role], database=database
        )

    # -- reporting ----------------------------------------------------------
    def check(self, name: str, problems: Sequence[str]) -> None:
        if problems:
            self.failures.extend(f"{name}: {problem}" for problem in problems)
            print(f"FAIL {name}", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print(f"ok   {name}")

    # -- database lifecycle -------------------------------------------------
    def create_database(self) -> str:
        name = f"dotmac_ci_prodroles_{uuid.uuid4().hex[:12]}"
        with self.superuser() as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with psycopg.connect(
            _psycopg_url(self.admin_url.set(database=name)), autocommit=False
        ) as conn:
            if bootstrap_modules(conn, dry_run=False, repair=True) != 0:
                raise RuntimeError("commercial module bootstrap failed")
            conn.commit()
            if bootstrap_outbox(conn, dry_run=False, repair=True) != 0:
                raise RuntimeError("outbox dispatcher bootstrap failed")
            conn.commit()
        with self.superuser(name) as admin:
            for extension in EXTENSIONS:
                admin.execute(
                    sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                        sql.Identifier(extension)
                    )
                )
            for role, password in self.passwords.items():
                admin.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        # Production shape: postgres owns the database (and so `public`); the
        # migrator holds USAGE+CREATE on `public`, the runtime role only USAGE
        # (it never creates), and only the schema-bootstrap role holds CREATE
        # on the database.
        with self.superuser(name) as admin:
            admin.execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                "'dotmac_schema_bootstrap') THEN CREATE ROLE dotmac_schema_bootstrap "
                "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                "NOINHERIT; END IF; END $$"
            )
            admin.execute("GRANT dotmac_app TO dotmac_schema_bootstrap")
            admin.execute(
                sql.SQL(
                    "GRANT CONNECT, CREATE ON DATABASE {} TO dotmac_schema_bootstrap"
                ).format(sql.Identifier(name))
            )
            admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            admin.execute("GRANT USAGE ON SCHEMA public TO dotmac_app")
            admin.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_admin")
        return name

    def drop_database(self, name: str) -> None:
        with self.superuser() as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )

    # -- alembic ------------------------------------------------------------
    def alembic_upgrade(
        self,
        database: str,
        *,
        migrator: str,
        owner_env: str | None,
        target: str = "heads",
        as_superuser: bool = False,
        env_var: str = "MIGRATION_RUNTIME_ROLE",
    ) -> tuple[int, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "MIGRATION_RUNTIME_ROLE",
                "MIGRATION_OBJECT_OWNER",
                "MIGRATION_DATABASE_URL",
            }
        }
        env["DATABASE_URL"] = _sqlalchemy_url(self.as_role(OWNER, database))
        migrator_url = (
            self.admin_url.set(database=database)
            if as_superuser
            else self.as_role(migrator, database)
        )
        env["MIGRATION_DATABASE_URL"] = _sqlalchemy_url(migrator_url)
        if owner_env is not None:
            env[env_var] = owner_env
        result = subprocess.run(  # nosec B603 - fixed argv, this repo's alembic
            [sys.executable, "-m", "alembic", "upgrade", target],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, result.stdout + result.stderr

    @staticmethod
    def tail(output: str) -> str:
        return "\n".join(output.splitlines()[-ALEMBIC_TAIL_LINES:])

    # -- catalog queries ----------------------------------------------------
    def edges(
        self, conn: psycopg.Connection
    ) -> list[tuple[str, str, bool, bool, bool]]:
        return [
            (str(r[0]), str(r[1]), bool(r[2]), bool(r[3]), bool(r[4]))
            for r in conn.execute(
                "SELECT r.rolname, m.rolname, a.admin_option, a.inherit_option, "
                "a.set_option FROM pg_auth_members a "
                "JOIN pg_roles r ON r.oid = a.roleid "
                "JOIN pg_roles m ON m.oid = a.member "
                "WHERE r.rolname = ANY(%s) AND m.rolname = ANY(%s)",
                ([OWNER, MIGRATOR], [OWNER, MIGRATOR]),
            ).fetchall()
        ]

    def migrator_owned(self, conn: psycopg.Connection) -> list[tuple[str, str]]:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                """
                SELECT d.classid::regclass::text,
                       CASE d.classid
                         WHEN 'pg_class'::regclass THEN d.objid::regclass::text
                         WHEN 'pg_proc'::regclass THEN (
                              SELECT proname FROM pg_proc WHERE oid = d.objid)
                         ELSE d.objid::text END
                  FROM pg_shdepend d
                 WHERE d.dbid = (SELECT oid FROM pg_database
                                  WHERE datname = current_database())
                   AND d.deptype = 'o'
                   AND d.refobjid = %s::regrole
                """,
                (MIGRATOR,),
            ).fetchall()
        ]

    def grant_gaps(self, conn: psycopg.Connection) -> list[tuple[str, str, list[str]]]:
        """Migrator-owned relations on which the runtime role lacks a privilege."""

        rows: list[tuple[str, str, list[str]]] = []
        relations = conn.execute(
            "SELECT c.oid, c.oid::regclass::text, c.relkind::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE {_APP_SCHEMAS} AND c.relkind IN ('r','p','v','S') "
            "AND c.relowner = %s::regrole AND "
            + _NOT_EXTENSION.format(catalog="pg_class", alias="c"),
            (MIGRATOR,),
        ).fetchall()
        for oid, name, kind in relations:
            if kind == "S":
                func, privs = "has_sequence_privilege", ("USAGE", "SELECT", "UPDATE")
            else:
                func = "has_table_privilege"
                privs = ("SELECT", "INSERT", "UPDATE", "DELETE")
                if name == "alembic_version":
                    privs = ("SELECT",)
            missing = [
                priv
                for priv in privs
                if not conn.execute(
                    f"SELECT {func}(%s, %s::oid, %s)",  # nosec B608 - fixed names
                    (OWNER, oid, priv),
                ).fetchone()[0]
            ]
            rows.append(("sequence" if kind == "S" else "relation", name, missing))
        return rows


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def assert_role_shape(lane: Lane, conn: psycopg.Connection) -> None:
    rows = {
        str(r[0]): (bool(r[1]), bool(r[2]), bool(r[3]))
        for r in conn.execute(
            "SELECT rolname, rolcanlogin, rolbypassrls, rolsuper FROM pg_roles "
            "WHERE rolname = ANY(%s)",
            ([OWNER, MIGRATOR],),
        ).fetchall()
    }
    problems: list[str] = []
    if rows.get(OWNER) != (True, False, False):
        problems.append(f"{OWNER} posture {rows.get(OWNER)!r}, want login/no bypass")
    if rows.get(MIGRATOR) != (True, True, False):
        problems.append(f"{MIGRATOR} posture {rows.get(MIGRATOR)!r}, want login/bypass")
    problems.extend(edge_violations(lane.edges(conn)))
    lane.check("production role shape", problems)
    lane.check(
        "outbox role contract verifies", [] if verify_outbox(conn) == 0 else ["drift"]
    )


def positive_run(lane: Lane) -> None:
    database = lane.create_database()
    try:
        with lane.superuser(database) as conn:
            assert_role_shape(lane, conn)
        code, output = lane.alembic_upgrade(
            database, migrator=MIGRATOR, owner_env=OWNER
        )
        lane.check(
            "alembic upgrade heads as migrator",
            [] if code == 0 else [f"exit {code}\n{lane.tail(output)}"],
        )
        if code != 0:
            return
        with lane.superuser(database) as conn:
            lane.check(
                "new objects are migrator-owned; relay definers stay with it",
                migrator_owned_violations(lane.migrator_owned(conn)),
            )
            lane.check(
                "no application object belongs to a third role", foreign_owned(conn)
            )
            lane.check(
                "runtime role holds DML and sequence grants on every new object",
                grant_gap_findings(lane.grant_gaps(conn)),
            )
            lane.check(
                "every module table is FORCE row-level security", force_rls(conn)
            )
            lane.check("runtime role has no CREATE on public", no_runtime_create(conn))
        lane.check(
            "runtime role cannot create or alter in public",
            runtime_ddl_denied(lane, database),
        )
        lane.check("owner is bound by row-level security", owner_bound(lane, database))
        # The pre-grant-model variable name must keep working.
        again, output = lane.alembic_upgrade(
            database,
            migrator=MIGRATOR,
            owner_env=OWNER,
            env_var="MIGRATION_OBJECT_OWNER",
        )
        lane.check(
            "second run is a clean no-op",
            [] if again == 0 else [f"exit {again}\n{lane.tail(output)}"],
        )
    finally:
        lane.drop_database(database)


def foreign_owned(conn: psycopg.Connection) -> list[str]:
    queries = {
        "relation": (
            "SELECT c.oid::regclass::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE {_APP_SCHEMAS} AND c.relkind IN ('r','p','v','m','S','f') "
            "AND c.relowner <> %s::regrole AND c.relowner <> %s::regrole AND "
            + _NOT_EXTENSION.format(catalog="pg_class", alias="c")
        ),
        "type": (
            "SELECT t.oid::regtype::text FROM pg_type t "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            f"WHERE {_APP_SCHEMAS} AND t.typtype IN ('e','d','r') "
            "AND t.typowner <> %s::regrole AND t.typowner <> %s::regrole AND "
            + _NOT_EXTENSION.format(catalog="pg_type", alias="t")
        ),
        "function": (
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE {_APP_SCHEMAS} AND p.proowner <> %s::regrole "
            "AND p.proowner <> %s::regrole AND "
            + _NOT_EXTENSION.format(catalog="pg_proc", alias="p")
        ),
    }
    problems: list[str] = []
    for kind, query in queries.items():
        params: tuple[object, ...] = (OWNER, MIGRATOR)
        problems.extend(
            f"{kind} {row[0]} is owned by neither {OWNER} nor {MIGRATOR}"
            for row in conn.execute(query, params).fetchall()
        )
    return problems


def no_runtime_create(conn: psycopg.Connection) -> list[str]:
    row = conn.execute(
        "SELECT has_schema_privilege(%s, 'public', 'CREATE'), "
        "has_schema_privilege(%s, 'public', 'CREATE')",
        (OWNER, MIGRATOR),
    ).fetchone()
    problems = []
    if row[0]:
        problems.append(f"{OWNER} holds CREATE on public")
    if not row[1]:
        problems.append(f"{MIGRATOR} lacks CREATE on public")
    return problems


def runtime_ddl_denied(
    lane: Lane, database: str, *, require_table: bool = True
) -> list[str]:
    """As the runtime role: no CREATE in public, no DDL on a migrator-owned table."""

    problems: list[str] = []
    with lane.superuser(database) as admin:
        table = admin.execute(
            "SELECT c.oid::regclass::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE {_APP_SCHEMAS} AND c.relkind = 'r' "
            "AND c.relowner = %s::regrole ORDER BY c.relname LIMIT 1",
            (MIGRATOR,),
        ).fetchone()
    attempts = {"CREATE TABLE": "CREATE TABLE public.ci_runtime_probe (id int)"}
    if table is not None:
        attempts |= {
            "ALTER TABLE": f"ALTER TABLE {table[0]} ADD COLUMN ci_runtime_probe int",
            "DROP TABLE": f"DROP TABLE {table[0]}",
            "ALTER OWNER": f"ALTER TABLE {table[0]} OWNER TO {OWNER}",
        }
    elif require_table:
        return [f"no {MIGRATOR}-owned table to probe"]
    with psycopg.connect(
        _psycopg_url(lane.as_role(OWNER, database)), autocommit=True
    ) as conn:
        for label, statement in attempts.items():
            try:
                conn.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                continue
            except psycopg.Error as exc:
                problems.append(f"{label}: unexpected {str(exc).splitlines()[0]}")
                continue
            problems.append(f"{OWNER} could run {label}")
    return problems


def force_rls(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT c.oid::regclass::text, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname LIKE 'mod\\_%' AND c.relkind IN ('r','p')"
    ).fetchall()
    if not rows:
        return ["no module tables were created"]
    return [
        f"{name} is not ENABLE+FORCE row-level security"
        for name, enabled, forced in rows
        if not (enabled and forced)
    ]


def owner_bound(lane: Lane, database: str) -> list[str]:
    """As the runtime owner, with no tenant set, FORCE RLS still binds it."""

    problems: list[str] = []
    with psycopg.connect(
        _psycopg_url(lane.as_role(OWNER, database)), autocommit=True
    ) as conn:
        forced = conn.execute(
            "SELECT c.oid::regclass::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relforcerowsecurity AND c.relkind IN ('r','p') "
            "AND (n.nspname = 'public' OR n.nspname LIKE 'mod\\_%')"
        ).fetchall()
        for (name,) in forced:
            count = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.SQL(name))
            ).fetchone()[0]
            if count != 0:
                problems.append(
                    f"{name} shows {count} rows to {OWNER} without a tenant"
                )
        problems.extend(_insert_probe(conn, expect_rls_denial=True, who=OWNER))
    with psycopg.connect(
        _psycopg_url(lane.as_role(MIGRATOR, database)), autocommit=True
    ) as conn:
        # BYPASSRLS passes the policy and stops at the tenant foreign key
        # instead, which proves the two identities really differ.
        problems.extend(_insert_probe(conn, expect_rls_denial=False, who=MIGRATOR))
    return problems


def _insert_probe(
    conn: psycopg.Connection, *, expect_rls_denial: bool, who: str
) -> list[str]:
    try:
        conn.execute(
            "INSERT INTO public.outbox_events (id, tenant_id, event_type) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'ci-probe')"
        )
    except psycopg.errors.InsufficientPrivilege as exc:
        denied = "row-level security" in str(exc)
        if expect_rls_denial and denied:
            return []
        return [f"{who} insert probe: {str(exc).splitlines()[0]}"]
    except psycopg.errors.ForeignKeyViolation:
        return [] if not expect_rls_denial else [f"{who} was not bound by FORCE RLS"]
    return [f"{who} insert probe unexpectedly succeeded"]


#: The last Sub revision before the head: its successor ALTERs an existing table.
EARLIER_REVISION = "697_subscription_credential_permission"

_HAND_TO_OWNER_SQL = """
SELECT format('ALTER %s %s OWNER TO dotmac_app', CASE c.relkind WHEN 'v' THEN 'VIEW'
         WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
         c.oid::regclass)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE (n.nspname = 'public' OR n.nspname LIKE 'mod\\_%%') AND c.relkind IN ('r','p','v','m','f')
   AND c.relowner = 'postgres'::regrole
   AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.deptype='e' AND e.classid='pg_class'::regclass AND e.objid=c.oid)
UNION ALL
SELECT format('ALTER SEQUENCE %s OWNER TO dotmac_app', c.oid::regclass)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE (n.nspname = 'public' OR n.nspname LIKE 'mod\\_%%') AND c.relkind = 'S' AND c.relowner = 'postgres'::regrole
   AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype IN ('a','i'))
   AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.deptype='e' AND e.classid='pg_class'::regclass AND e.objid=c.oid)
UNION ALL
SELECT format('ALTER %s %s OWNER TO dotmac_app', CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, t.oid::regtype)
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
 WHERE (n.nspname = 'public' OR n.nspname LIKE 'mod\\_%%') AND t.typtype IN ('e','d','r') AND t.typowner = 'postgres'::regrole
   AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.deptype='e' AND e.classid='pg_type'::regclass AND e.objid=t.oid)
UNION ALL
SELECT format('ALTER ROUTINE %s OWNER TO dotmac_app', p.oid::regprocedure)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE (n.nspname = 'public' OR n.nspname LIKE 'mod\\_%%') AND p.proowner = 'postgres'::regrole AND NOT p.prosecdef
   AND NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.deptype='e' AND e.classid='pg_proc'::regclass AND e.objid=p.oid)
"""


def prepare_owner_owned_database(lane: Lane, database: str) -> list[str]:
    """Reproduce production's pre-grant-model state: the earlier revision applied
    as a superuser, every object then assigned to the runtime owner. Returns the
    problems, empty when ready."""

    code, output = lane.alembic_upgrade(
        database,
        migrator=MIGRATOR,
        owner_env=None,
        target=EARLIER_REVISION,
        as_superuser=True,
    )
    if code != 0:
        return [f"exit {code}\n{lane.tail(output)}"]
    with lane.superuser(database) as admin:
        for statement in [r[0] for r in admin.execute(_HAND_TO_OWNER_SQL).fetchall()]:
            admin.execute(statement)
    return []


def existing_database_run(lane: Lane) -> None:
    """The production path: objects already belong to dotmac_app, then app_admin
    upgrades them through inherited membership (ALTER on owner tables, including
    alembic_version) and owns whatever is new, granting the runtime role DML."""

    database = lane.create_database()
    try:
        code, output = lane.alembic_upgrade(
            database,
            migrator=MIGRATOR,
            owner_env=None,
            target=EARLIER_REVISION,
            as_superuser=True,
        )
        if code != 0:
            lane.check(
                "earlier revision as superuser", [f"exit {code}\n{lane.tail(output)}"]
            )
            return
        with lane.superuser(database) as admin:
            statements = [r[0] for r in admin.execute(_HAND_TO_OWNER_SQL).fetchall()]
            for statement in statements:
                admin.execute(statement)
            lane.check(
                "existing database: pre-grant-model objects belong to the owner",
                [] if statements else ["no objects were assigned to the owner"],
            )
            lane.check("existing database: pre-state ownership", foreign_owned(admin))
            version_owner = admin.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'public.alembic_version'::regclass"
            ).fetchone()[0]
            lane.check(
                "existing database: alembic_version belongs to the owner",
                [] if version_owner == OWNER else [f"owner is {version_owner}"],
            )
        code, output = lane.alembic_upgrade(
            database, migrator=MIGRATOR, owner_env=OWNER
        )
        lane.check(
            "existing database: upgrade heads as migrator through inherited membership",
            [] if code == 0 else [f"exit {code}\n{lane.tail(output)}"],
        )
        if code != 0:
            return
        with lane.superuser(database) as admin:
            lane.check(
                "existing database: relay definers stay with the migrator",
                migrator_owned_violations(lane.migrator_owned(admin)),
            )
            lane.check(
                "existing database: mixed ownership is only owner and migrator",
                foreign_owned(admin),
            )
            lane.check(
                "existing database: runtime role holds grants on new objects",
                grant_gap_findings(lane.grant_gaps(admin)),
            )
            lane.check(
                "existing database: runtime role has no CREATE on public",
                no_runtime_create(admin),
            )
        lane.check(
            "existing database: runtime role cannot create or alter",
            runtime_ddl_denied(lane, database, require_table=False),
        )
        with lane.superuser(database) as admin:
            lane.check("existing database: module tables FORCE RLS", force_rls(admin))
    finally:
        lane.drop_database(database)


def sensitivity(lane: Lane) -> None:
    """The lane must FAIL on the setups that broke a production deploy."""

    database = lane.create_database()
    try:
        # The edge only matters for objects the runtime role still owns, so the
        # baseline is the production pre-state, then migrated to heads.
        problems = prepare_owner_owned_database(lane, database)
        if problems:
            lane.check("sensitivity baseline prepares", problems)
            return
        code, output = lane.alembic_upgrade(
            database, migrator=MIGRATOR, owner_env=OWNER
        )
        if code != 0:
            lane.check("sensitivity baseline migrates", [lane.tail(output)])
            return
        owner_url = _psycopg_url(lane.as_role(MIGRATOR, database))
        widen = "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"

        def must_not_alter() -> list[str]:
            with psycopg.connect(owner_url, autocommit=True) as conn:
                try:
                    conn.execute(widen)
                except psycopg.errors.InsufficientPrivilege as exc:
                    if "must be owner" in str(exc):
                        return []
                    return [f"unexpected error: {str(exc).splitlines()[0]}"]
            return ["the migrator could ALTER alembic_version without the edge"]

        with lane.superuser(database) as admin:
            # 1. No edge at all.
            admin.execute(f'REVOKE "{OWNER}" FROM "{MIGRATOR}"')
            lane.check(
                "no edge: ALTER alembic_version needs ownership", must_not_alter()
            )
            code, output = lane.alembic_upgrade(
                database, migrator=MIGRATOR, owner_env=OWNER
            )
            lane.check(
                "no edge: alembic refuses by contract name",
                []
                if code != 0 and "module_database_roles.v1" in output
                else [f"exit {code}\n{lane.tail(output)}"],
            )
            admin.execute(
                f'GRANT "{OWNER}" TO "{MIGRATOR}" WITH INHERIT TRUE, SET TRUE'
            )
            # 1b. A BYPASSRLS non-superuser must not migrate without the owner variable.
            code, output = lane.alembic_upgrade(
                database, migrator=MIGRATOR, owner_env=None
            )
            lane.check(
                "unset owner variable: BYPASSRLS migrator is refused",
                []
                if code != 0 and "MIGRATION_RUNTIME_ROLE" in output
                else [f"exit {code}\n{lane.tail(output)}"],
            )
            admin.execute(f'REVOKE "{OWNER}" FROM "{MIGRATOR}"')
            # 2. The old direction (the shape before this change).
            admin.execute(f'GRANT "{MIGRATOR}" TO "{OWNER}"')
            lane.check(
                "old edge: ALTER alembic_version needs ownership", must_not_alter()
            )
            lane.check(
                "old edge: verify-only reports drift",
                [] if verify_outbox(admin) == 1 else ["verify accepted the old edge"],
            )
            code, output = lane.alembic_upgrade(
                database, migrator=MIGRATOR, owner_env=OWNER
            )
            lane.check(
                "old edge: alembic refuses",
                [] if code != 0 else ["alembic accepted the old direction"],
            )
            # 3. Repair converges to the new edge and never re-grants the old.
            admin.autocommit = False
            result = bootstrap_outbox(admin, dry_run=False, repair=True)
            admin.commit()
            admin.autocommit = True
            lane.check(
                "repair converges to the new edge only",
                [] if result == 0 else ["repair failed"],
            )
            lane.check("edge after repair", edge_violations(lane.edges(admin)))
            # 4. The final state migrates again.
            code, output = lane.alembic_upgrade(
                database, migrator=MIGRATOR, owner_env=OWNER
            )
            lane.check(
                "after repair: alembic succeeds",
                [] if code == 0 else [f"exit {code}\n{lane.tail(output)}"],
            )
    finally:
        lane.drop_database(database)

    # 5. The runtime owner is not a migrator. Either the Billing backfill
    #    (bi_0006) refuses a role that is subject to row-level security, or the
    #    outbox relay guard (557) refuses a runner that cannot SET ROLE app_admin,
    #    or the runtime role, which holds no CREATE on public, cannot even create
    #    alembic_version; which comes first is the order of events, and all three
    #    are the contract.
    database = lane.create_database()
    try:
        code, output = lane.alembic_upgrade(database, migrator=OWNER, owner_env=None)
        lane.check(
            "as the runtime owner: migration is refused",
            []
            if code != 0
            and (
                "bi_0006" in output
                or "cannot SET ROLE app_admin" in output
                or "permission denied for schema public" in output
            )
            else [f"exit {code}\n{lane.tail(output)}"],
        )
    finally:
        lane.drop_database(database)


def main() -> int:
    raw = os.environ.get(ADMIN_URL_VAR, "").strip()
    if not raw:
        print(f"{ADMIN_URL_VAR} is not set", file=sys.stderr)
        return 2
    lane = Lane(make_url(raw))
    try:
        positive_run(lane)
        existing_database_run(lane)
        sensitivity(lane)
    except (psycopg.Error, RuntimeError) as exc:
        diag = getattr(exc, "diag", None)
        detail = getattr(diag, "message_primary", None) or str(exc).splitlines()[0]
        print(f"lane aborted: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 2
    if lane.failures:
        print(f"{len(lane.failures)} check(s) failed", file=sys.stderr)
        return 1
    print("all production-role checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
