"""Clone migrated PostgreSQL databases from a template instead of replaying.

`migrated_test_database.py` states the standard: a database-backed test claims
deployed behaviour only against a schema built by the REAL Alembic chain.  That
module applies the chain once per test environment.  It says nothing about
tests that need a database of their own, and those had been replaying the whole
chain per test -- with 601 revisions in `alembic/versions/`, one such test costs
about 50 seconds, and one file of 16 of them accounted for 852 s of a 1289 s CI
shard.

This module supplies the missing half, and it is the same half the approved
standard already prescribes: apply the chain ONCE into a template database,
then give each test a byte-identical `CREATE DATABASE ... TEMPLATE ...` copy.
The evidence is unchanged -- the schema every test sees was produced by the real
chain -- while the chain runs once per revision target rather than once per test.

What this module deliberately does NOT do is decide which tests may use it.  A
test whose claim is about the act of migrating (a fresh-chain acceptance proof,
an incremental predecessor-to-candidate proof) must keep replaying the chain;
cloning would move the thing under test into the fixture.

## Safety properties

- **Run-unique names.**  Every database this module creates carries a prefix and
  a per-process token, so concurrent shards, xdist workers and a stale crashed
  run cannot collide.
- **The template is never mutated, and that is PROVEN rather than assumed.**
  `ALLOW_CONNECTIONS false` stops NEW sessions; on its own it evicts nothing and
  says nothing about sessions that already exist.  So the order matters and is
  not interchangeable: close the migration connection, seal, and only THEN count
  sessions -- once sealed, the count cannot grow, so a zero reading is a fact
  rather than a sample.  A non-zero reading raises `TemplateNotSealed`; there is
  no path that clones from a template with a live session on it.
- **Cleanup is bounded to what this process created.**  Fixture teardown drops
  this run's own templates and clones by name.  There is deliberately NO
  automatic wildcard sweep: a prefix match cannot distinguish a crashed run's
  residue from a concurrent run's live databases, and being wrong destroys
  someone else's test run.  In ephemeral CI the container teardown owns that
  cleanup anyway.  For a long-lived development server, `python -m
  scripts.ci.template_database --drop-older-than <hours>` is an explicit,
  operator-invoked maintenance command that acts only on age and lease evidence
  it can actually read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command
from scripts.bootstrap_outbox_dispatcher_roles import (
    bootstrap as bootstrap_outbox_dispatcher_roles,
)
from scripts.ci.migrated_test_database import (
    ALEMBIC_CONFIG_PATH,
    REPOSITORY_ROOT,
    install_migration_graph_environment,
)

#: Every database this module creates starts with one of these. The maintenance
#: command below considers only names matching them, so they must never collide
#: with a database a human cares about. Both carry the `test` token that
#: `migrated_test_database._DISPOSABLE_DATABASE_TOKEN` demands, so a name this
#: module produces is one the repository's own safety rule already recognises as
#: disposable -- and one that rule would refuse to accept as a real target.
TEMPLATE_PREFIX = "dotmac_test_tmpl_"
CLONE_PREFIX = "dotmac_test_clone_"

#: Identifies databases belonging to THIS process, so a sweep can leave a
#: concurrently running shard's databases alone.
_RUN_TOKEN = uuid4().hex[:8]

_SAFE_REVISION = re.compile(r"[^a-z0-9]+")

#: PostgreSQL silently truncates identifiers at 63 bytes. Two revision targets
#: whose names truncated to the same string would share ONE template while
#: appearing to have their own -- so the budget is computed, and a name that
#: does not fit ends in a digest of the full revision rather than in whatever
#: characters happened to survive.
_MAX_IDENTIFIER_BYTES = 63


def _maintenance_url(base: URL) -> str:
    """A connectable URL that is not any of the databases being manipulated."""

    return base.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )


@contextmanager
def _admin(base: URL) -> Iterator[psycopg.Connection]:
    with psycopg.connect(_maintenance_url(base), autocommit=True) as connection:
        yield connection


def template_database_name(revision: str) -> str:
    """Deterministic per-run name for one revision target's template.

    Deterministic in the revision so two tests asking for the same target share
    one template, and unique per run so nothing outside this process can be
    mistaken for it.
    """

    slug = _SAFE_REVISION.sub("_", revision.lower()).strip("_")
    prefix = f"{TEMPLATE_PREFIX}{_RUN_TOKEN}_"
    budget = _MAX_IDENTIFIER_BYTES - len(prefix)
    if len(slug) > budget:
        digest = hashlib.blake2b(revision.encode("utf-8"), digest_size=4).hexdigest()
        slug = f"{slug[: budget - len(digest) - 1]}_{digest}"
    return f"{prefix}{slug}"


def clone_database_name() -> str:
    return f"{CLONE_PREFIX}{_RUN_TOKEN}_{uuid4().hex[:12]}"


class TemplateNotSealed(RuntimeError):
    """A template still had a live session after being sealed."""


def _terminate_sessions(admin: psycopg.Connection, database: str) -> None:
    admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (database,),
    )


def _session_count(admin: psycopg.Connection, database: str) -> int:
    row = admin.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid()",
        (database,),
    ).fetchone()
    return int(row[0]) if row else 0


def _write_lease(admin: psycopg.Connection, database: str) -> None:
    """Record who created this database and when, on the database itself.

    PostgreSQL does not store a creation time, so age-based maintenance would
    otherwise have nothing to reason about. The comment is the lease: an
    operator-invoked sweep reads it and acts only on what it can actually
    evidence, rather than on a name pattern.
    """

    lease = json.dumps(
        {
            "created_by": "scripts.ci.template_database",
            "run_token": _RUN_TOKEN,
            "pid": os.getpid(),
            "created_at": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    )
    admin.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(database), sql.Literal(lease)
        )
    )


def read_lease(base: URL, database: str) -> dict[str, object] | None:
    """Return the lease this module wrote, or None if there is not one."""

    with _admin(base) as admin:
        row = admin.execute(
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datname = %s",
            (database,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        lease = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(lease, dict):
        return None
    if lease.get("created_by") != "scripts.ci.template_database":
        return None
    return lease


def drop_database(base: URL, name: str) -> None:
    """Drop a database this module created, connections and all.

    `ALLOW_CONNECTIONS true` is restored first: a template cannot be dropped
    while it refuses connections in some PostgreSQL configurations, and the
    statement is harmless for an ordinary clone.
    """

    with _admin(base) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                sql.Identifier(name)
            )
        )
        _terminate_sessions(admin, name)
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        )


@contextmanager
def _alembic_target(url: URL) -> Iterator[None]:
    """Point `alembic/env.py` at `url` for the duration of the block.

    `env.py` resolves its target from `app_config.settings`, NOT from the
    Config's `sqlalchemy.url`, so setting the latter silently does nothing and
    the upgrade runs against whatever `DATABASE_URL` the job exported.
    """

    from app import config as app_config

    previous = app_config.settings
    app_config.settings = replace(
        previous, database_url=url.render_as_string(hide_password=False)
    )
    previous_env = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    try:
        yield
    finally:
        app_config.settings = previous
        if previous_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_env


def _psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def bootstrap_database_local_prerequisites(url: URL) -> None:
    """Apply database-local migration prerequisites to a fresh test database."""

    with psycopg.connect(_psycopg_url(url), autocommit=False) as conn:
        result = bootstrap_outbox_dispatcher_roles(conn, dry_run=False, repair=True)
    if result != 0:
        raise RuntimeError(
            "failed to bootstrap outbox dispatcher prerequisites for template database"
        )


def upgrade_to(url: URL, revision: str) -> None:
    """Run the real Alembic chain against `url` up to `revision`."""

    install_migration_graph_environment()
    with _alembic_target(url):
        config = Config(str(ALEMBIC_CONFIG_PATH))
        config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
        command.upgrade(config, revision)


def create_template(base: URL, revision: str) -> URL:
    """Build ONE migrated template for `revision`, then seal and prove it."""

    name = template_database_name(revision)
    with _admin(base) as admin:
        _terminate_sessions(admin, name)
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        _write_lease(admin, name)

    target = base.set(database=name)
    try:
        bootstrap_database_local_prerequisites(target)
        # `alembic/env.py` builds its engine with `poolclass=pool.NullPool`, so
        # the migration connection is closed rather than parked in a pool when
        # the upgrade returns. `seal_template` does not rely on that being true.
        upgrade_to(target, revision)
        seal_template(base, name)
    except Exception:
        drop_database(base, name)
        raise
    return target


def seal_template(base: URL, name: str) -> None:
    """Make the template unmutatable, then PROVE it -- in that order.

    `ALLOW_CONNECTIONS false` is not an eviction. It refuses NEW sessions and
    leaves existing ones running, so on its own it establishes nothing about
    what is already attached.

    The order is therefore load-bearing and not interchangeable:

    1. the migration connection is already closed (the caller's upgrade returned);
    2. seal, so no further session can be established;
    3. terminate whatever was already attached -- safe now, and only now,
       because nothing can reconnect behind us;
    4. count, and fail closed unless the count is zero.

    Counting before sealing would sample a number that could still grow. After
    sealing, a zero reading cannot become non-zero, which is what makes it
    evidence rather than a guess.
    """

    with _admin(base) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                sql.Identifier(name)
            )
        )
        _terminate_sessions(admin, name)
        remaining = _session_count(admin, name)
    if remaining:
        raise TemplateNotSealed(
            f"template {name!r} still has {remaining} live session(s) after "
            "sealing; it cannot be proven immutable, so it will not be cloned"
        )


def clone_from_template(base: URL, template: URL) -> URL:
    """Copy a sealed template into a fresh, writable database.

    Refuses an unsealed source. A template that still accepts connections could
    be written to between two clones, which would make two tests that believe
    they share a schema quietly disagree.
    """

    template_name = template.database
    assert template_name, "template URL names no database"
    if not template_is_sealed(base, template):
        raise TemplateNotSealed(
            f"template {template_name!r} still accepts connections; refusing to "
            "clone from a source that could be mutated between clones"
        )
    name = clone_database_name()
    with _admin(base) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template_name)
            )
        )
        _write_lease(admin, name)
    return base.set(database=name)


def template_is_sealed(base: URL, template: URL) -> bool:
    """True when PostgreSQL itself refuses connections to the template."""

    with _admin(base) as admin:
        row = admin.execute(
            "SELECT datallowconn FROM pg_database WHERE datname = %s",
            (template.database,),
        ).fetchone()
    return bool(row) and row[0] is False


def drop_stale_databases(
    base: URL, *, older_than: timedelta, dry_run: bool = True
) -> list[tuple[str, str]]:
    """Operator-invoked maintenance for a long-lived development server.

    This is deliberately NOT called from any fixture. An automatic sweep can
    only match on a name prefix, and a prefix cannot tell a crashed run's
    residue apart from a concurrent run's LIVE databases -- sparing "my own
    token" protects this process and no one else's. Being wrong destroys
    somebody's running test suite, which is a far worse outcome than some disk
    usage. In ephemeral CI the container teardown owns cleanup anyway, and
    ordinary fixture teardown already drops this run's own databases by name.

    So a database is dropped here only on evidence this function can actually
    read: a lease it wrote itself, an age past the caller's threshold, and no
    active session. Anything without a readable lease is reported and left
    alone. Defaults to a dry run; the caller must ask for the deletion.
    """

    now = datetime.now(UTC)
    candidates: list[tuple[str, str]] = []
    with _admin(base) as admin:
        rows = admin.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s OR datname LIKE %s",
            (f"{TEMPLATE_PREFIX}%", f"{CLONE_PREFIX}%"),
        ).fetchall()
        names = [name for (name,) in rows]
        for name in names:
            sessions = _session_count(admin, name)
            if sessions:
                candidates.append((name, f"skipped: {sessions} active session(s)"))
                continue
            lease = read_lease(base, name)
            if lease is None:
                candidates.append((name, "skipped: no readable lease"))
                continue
            created_at = lease.get("created_at")
            if not isinstance(created_at, str):
                candidates.append((name, "skipped: lease has no creation time"))
                continue
            try:
                age = now - datetime.fromisoformat(created_at)
            except ValueError:
                candidates.append((name, "skipped: lease creation time unreadable"))
                continue
            if age < older_than:
                candidates.append((name, f"skipped: age {age} below threshold"))
                continue
            if lease.get("run_token") == _RUN_TOKEN:
                candidates.append((name, "skipped: belongs to this process"))
                continue
            candidates.append(
                (name, f"{'would drop' if dry_run else 'dropped'}: age {age}")
            )

    if not dry_run:
        for name, disposition in candidates:
            if disposition.startswith("dropped"):
                drop_database(base, name)
    return candidates


def main(argv: Sequence[str] | None = None) -> int:
    """Maintenance entry point. Never invoked by tests."""

    parser = argparse.ArgumentParser(
        description=(
            "Report or drop stale template/clone databases left behind by "
            "crashed test runs on a long-lived development server."
        )
    )
    parser.add_argument(
        "--drop-older-than",
        type=float,
        required=True,
        metavar="HOURS",
        help="only consider databases whose lease is at least this old",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually drop; without it the command only reports",
    )
    args = parser.parse_args(argv)

    from scripts.ci.migrated_test_database import parse_test_database_target

    target = parse_test_database_target(os.getenv("TEST_DATABASE_URL"))
    report = drop_stale_databases(
        target.url,
        older_than=timedelta(hours=args.drop_older_than),
        dry_run=not args.apply,
    )
    if not report:
        print("no template or clone databases found")
        return 0
    for name, disposition in sorted(report):
        print(f"{name}: {disposition}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
