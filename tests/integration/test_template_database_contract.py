"""The template-database mechanism's safety properties, against real PostgreSQL.

Cloning is only sound if the template is genuinely immutable, genuinely
session-free, and produces a copy carrying the exact schema the real Alembic
chain built. Each of those is asserted here against a live server rather than
argued for in a docstring.

The naming and identifier-budget properties are pure and live in
`tests/scripts/test_template_database_naming.py`.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from scripts.ci import template_database
from scripts.ci.migrated_test_database import repository_heads

PREDECESSOR_REVISION = "466_team_inbox_channel_ai_routes"


@pytest.fixture
def engine():
    """Satisfy the package PostgreSQL guard without building current schema.

    The package fixture calls `create_all()`, which is exactly the thing the
    template mechanism exists to avoid depending on.
    """

    class _Dialect:
        name = "postgresql"

    class _Stub:
        dialect = _Dialect()

    return _Stub()


def _render(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def test_the_template_refuses_connections_once_built(
    engine, template_base_url: URL, migrated_template: Callable[[str], URL]
) -> None:
    """Sealing is what makes "never mutated" a fact rather than a promise.

    It is also the precondition `CREATE DATABASE ... TEMPLATE` requires: the
    source must have no sessions, and a database nothing can connect to cannot
    acquire one mid-clone. This is how `template0` itself works.
    """

    template = migrated_template("heads")
    assert template_database.template_is_sealed(template_base_url, template)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_render(template))


def test_a_clone_carries_the_exact_repository_heads(
    engine,
    template_base_url: URL,
    cloned_database: Callable[[str], URL],
) -> None:
    """The evidence the standard demands must survive the copy.

    A clone is only acceptable as migration evidence if its schema is the one
    the real chain produced -- so assert the identity Alembic itself records,
    not merely that some tables exist.
    """

    clone = cloned_database("heads")
    clone_engine = create_engine(clone)
    try:
        with clone_engine.connect() as connection:
            actual = frozenset(
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalars()
            )
    finally:
        clone_engine.dispose()
    assert actual == repository_heads()


def test_one_revision_target_is_migrated_only_once(
    engine, template_base_url: URL, migrated_template: Callable[[str], URL]
) -> None:
    """The entire point: the chain runs per target, not per test."""

    first = migrated_template("heads")
    second = migrated_template("heads")
    assert first.database == second.database


def test_clones_are_writable_and_isolated_from_each_other(
    engine, cloned_database: Callable[[str], URL]
) -> None:
    """A test must be able to mutate its clone without reaching a sibling.

    Without this the mechanism would trade chain-replay cost for cross-test
    interference, which is a strictly worse bargain.
    """

    first = cloned_database("heads")
    second = cloned_database("heads")
    assert first.database != second.database

    with psycopg.connect(_render(first), autocommit=True) as conn:
        conn.execute("CREATE TABLE clone_isolation_probe (id integer primary key)")

    with psycopg.connect(_render(second), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.clone_isolation_probe') IS NOT NULL"
        ).fetchone()
    assert exists is not None and exists[0] is False


def test_mutating_a_clone_leaves_the_template_untouched(
    engine,
    template_base_url: URL,
    migrated_template: Callable[[str], URL],
    cloned_database: Callable[[str], URL],
) -> None:
    """A later clone must not inherit an earlier clone's writes."""

    template = migrated_template("heads")
    first = cloned_database("heads")
    with psycopg.connect(_render(first), autocommit=True) as conn:
        conn.execute("CREATE TABLE template_contamination_probe (id integer)")

    assert template_database.template_is_sealed(template_base_url, template)
    later = cloned_database("heads")
    with psycopg.connect(_render(later), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT to_regclass('public.template_contamination_probe') IS NOT NULL"
        ).fetchone()
    assert exists is not None and exists[0] is False


def test_sealing_fails_closed_while_a_session_is_still_attached(
    engine, template_base_url: URL
) -> None:
    """The property `ALLOW_CONNECTIONS false` does NOT provide, proven directly.

    Sealing refuses new sessions; it does not evict existing ones. A template
    with a live session could be written to between two clones. So the sequence
    must detect that and refuse -- and this test holds a connection open across
    the seal to make sure it does.

    `_terminate_sessions` is patched out for the duration, because the real
    sequence evicts the straggler and would otherwise succeed. What is under
    test is the FINAL check: that a non-zero count refuses rather than warns.
    """

    name = template_database.template_database_name("session_probe")
    admin_url = template_base_url.set(drivername="postgresql", database="postgres")
    rendered_admin = admin_url.render_as_string(hide_password=False)
    with psycopg.connect(rendered_admin, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

    holder = psycopg.connect(_render(template_base_url.set(database=name)))
    try:
        original = template_database._terminate_sessions
        template_database._terminate_sessions = lambda *_args, **_kwargs: None
        try:
            with pytest.raises(template_database.TemplateNotSealed):
                template_database.seal_template(template_base_url, name)
        finally:
            template_database._terminate_sessions = original
    finally:
        holder.close()
        template_database.drop_database(template_base_url, name)


def test_cloning_refuses_an_unsealed_template(engine, template_base_url: URL) -> None:
    """A source that still accepts connections is not a template."""

    name = template_database.template_database_name("unsealed_probe")
    admin_url = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with pytest.raises(template_database.TemplateNotSealed):
            template_database.clone_from_template(
                template_base_url, template_base_url.set(database=name)
            )
    finally:
        template_database.drop_database(template_base_url, name)


def test_a_sealed_template_rejects_connections_but_remains_cloneable(
    engine, template_base_url: URL, migrated_template
) -> None:
    """Both halves matter. Sealed-and-uncloneable would be a broken mechanism."""

    template = migrated_template("heads")
    assert template_database.template_is_sealed(template_base_url, template)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_render(template))

    clone = template_database.clone_from_template(template_base_url, template)
    try:
        with psycopg.connect(_render(clone)) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        assert clone.database
        template_database.drop_database(template_base_url, clone.database)


def test_a_template_carries_the_exact_revision_it_was_asked_for(
    engine, template_base_url: URL, cloned_database
) -> None:
    """A template built for a named predecessor must stop there.

    If it silently ran to head, an incremental proof cloned from it would test
    nothing, because the DDL under test would already have been applied.
    """

    clone = cloned_database(PREDECESSOR_REVISION)
    with psycopg.connect(_render(clone)) as conn:
        heads = {
            row[0]
            for row in conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
        }
    assert heads == {PREDECESSOR_REVISION}
    assert heads != repository_heads()


def test_dropping_one_clone_leaves_its_siblings_and_the_template_intact(
    engine, template_base_url: URL, migrated_template
) -> None:
    """Teardown must be surgical, including on the exception path.

    The fixture's `finally` drops exactly the clones ITS test created. If a drop
    reached a sibling or the template, one failing test would cascade into every
    test after it. Exercised through the module directly rather than through the
    fixture, so the assertion happens after the drop rather than before it.
    """

    template = migrated_template("heads")
    first = template_database.clone_from_template(template_base_url, template)
    second = template_database.clone_from_template(template_base_url, template)
    assert first.database and second.database and template.database

    try:
        template_database.drop_database(template_base_url, first.database)

        admin_url = template_base_url.set(drivername="postgresql", database="postgres")
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:

            def _exists(name: str) -> bool:
                return (
                    admin.execute(
                        "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
                    ).fetchone()
                    is not None
                )

            assert not _exists(first.database)
            assert _exists(second.database), "dropping one clone took its sibling"
            assert _exists(template.database), "dropping a clone took the template"
        assert template_database.template_is_sealed(template_base_url, template)
    finally:
        template_database.drop_database(template_base_url, second.database)


def test_concurrent_requests_for_one_target_build_exactly_one_template(
    engine, template_base_url: URL, migrated_template
) -> None:
    """Two threads asking at once must not race into two chain replays."""

    import threading

    results: list[URL] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _request() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(migrated_template("heads"))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=_request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=600)

    assert not errors, errors
    assert len({result.database for result in results}) == 1

    # Count templates for THIS target only. The session legitimately holds one
    # template per revision target, so counting every `TEMPLATE_PREFIX%`
    # database asserts something the mechanism never promised.
    expected = template_database.template_database_name("heads")
    admin_url = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        count = admin.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (expected,)
        ).fetchone()
    assert count is not None and count[0] == 1
    assert {result.database for result in results} == {expected}
