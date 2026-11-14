"""Rehearse the production reply-window index timeout and forward repair."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command
from scripts.ci import template_database

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "710_inbox_reply_window_projection"
REPAIR = "711_repair_inbox_reply_window_index"
INDEX = "public.ix_inbox_conversations_reply_window"
CREATE = (
    "CREATE INDEX CONCURRENTLY ix_inbox_conversations_reply_window "
    "ON public.inbox_conversations "
    "(channel_type, last_qualifying_inbound_at, status, id) "
    "WHERE last_qualifying_inbound_at IS NOT NULL"
)


def _url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def _state(connection: psycopg.Connection) -> tuple[int, bool, bool]:
    row = connection.execute(
        """
        SELECT indexrelid::oid, indisvalid, indisready
        FROM pg_index WHERE indexrelid = to_regclass(%s)
        """,
        (INDEX,),
    ).fetchone()
    assert row is not None
    return int(row[0]), bool(row[1]), bool(row[2])


@pytest.fixture
def fresh_migration_database(
    template_base_url: URL, monkeypatch: pytest.MonkeyPatch
) -> Iterator[URL]:
    """Replay the real chain from an empty disposable PostgreSQL database."""
    from app import config as app_config

    name = "dotmac_test_reply_window_" + uuid4().hex
    maintenance = template_base_url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(_url(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = template_base_url.set(database=name)
    monkeypatch.setattr(
        app_config,
        "settings",
        replace(
            app_config.settings,
            database_url=target.render_as_string(hide_password=False),
            migration_database_url=target.render_as_string(hide_password=False),
        ),
    )
    try:
        template_database.bootstrap_database_local_prerequisites(target)
        yield target
    finally:
        template_database.drop_database(template_base_url, name)


def test_fresh_chain_builds_valid_reply_window_index(
    fresh_migration_database: URL,
) -> None:
    _upgrade("heads")
    with psycopg.connect(_url(fresh_migration_database)) as catalog:
        assert _state(catalog)[1:] == (True, True)
        assert catalog.execute(
            "SELECT count(*) FROM alembic_version WHERE version_num=%s", (REPAIR,)
        ).fetchone() == (1,)


def test_repair_rebuilds_invalid_index_left_by_concurrent_lock_timeout(
    cloned_database: Callable[[str], URL],
) -> None:
    url = cloned_database(PREDECESSOR)
    with psycopg.connect(_url(url), autocommit=True) as catalog:
        catalog.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        with psycopg.connect(_url(url)) as blocker:
            blocker.execute(
                sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(
                    sql.Identifier("inbox_conversations")
                )
            )
            catalog.execute("SET lock_timeout = '200ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                catalog.execute(CREATE)
            blocker.rollback()
        catalog.execute("RESET lock_timeout")
        assert _state(catalog)[1:] != (True, True)

    _upgrade(REPAIR)
    with psycopg.connect(_url(url)) as catalog:
        repaired_oid, valid, ready = _state(catalog)
        assert (valid, ready) == (True, True)
        assert catalog.execute(
            "SELECT count(*) FROM alembic_version WHERE version_num=%s", (REPAIR,)
        ).fetchone() == (1,)

    _upgrade(REPAIR)
    with psycopg.connect(_url(url)) as catalog:
        assert _state(catalog) == (repaired_oid, True, True)


def test_repair_preserves_usable_index(
    cloned_database: Callable[[str], URL],
) -> None:
    url = cloned_database(PREDECESSOR)
    with psycopg.connect(_url(url)) as catalog:
        original_oid, valid, ready = _state(catalog)
        assert (valid, ready) == (True, True)

    _upgrade(REPAIR)
    with psycopg.connect(_url(url)) as catalog:
        assert _state(catalog) == (original_oid, True, True)
