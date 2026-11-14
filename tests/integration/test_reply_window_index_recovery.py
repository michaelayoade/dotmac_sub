"""Rehearse the production reply-window index timeout and forward repair."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import URL

from alembic import command

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
