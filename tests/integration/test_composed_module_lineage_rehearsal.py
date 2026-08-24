"""`alembic upgrade heads` must actually create a composed module's schema.

`tests/architecture/test_composed_module_lineage_discovery.py` proves the
module's revisions reach the revision map Alembic walks. That is necessary and
not sufficient: a lineage can be discovered and still be recorded without its
DDL landing, or land without the row-level security its tenant plane requires.

This is the same claim against a real database, through the same entry path
production uses — the checked-in `alembic.ini` and `alembic/env.py`, with
nothing set late.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from alembic import command
from alembic.config import Config
from app import config as app_config
from app.migration_lineages import COMPOSED_MODULE_LINEAGES

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _declared_lineages() -> tuple[str, ...]:
    """The composed lineages, from their one owner in `app/migration_lineages.py`."""
    return COMPOSED_MODULE_LINEAGES


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _psycopg_url(url: URL) -> str:
    return _render(url).replace("postgresql+psycopg", "postgresql")


def _sub_config(database_url: URL) -> Config:
    """Sub's REAL entry path. Deliberately does not set `version_locations`.

    Setting it here would reproduce the workaround and test the workaround.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


@pytest.fixture
def isolated_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    configured = os.getenv("TEST_DATABASE_URL")
    if not configured:
        raise pytest.UsageError("composed lineage rehearsal requires TEST_DATABASE_URL")
    base_url = make_url(configured)
    if not base_url.drivername.startswith("postgresql"):
        raise pytest.UsageError("composed lineage rehearsal requires PostgreSQL")

    name = f"dotmac_composed_rehearsal_{uuid4().hex}"
    maintenance = base_url.set(database="postgres")
    with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        database_url = base_url.set(database=name)
        monkeypatch.setattr(
            app_config,
            "settings",
            replace(app_config.settings, database_url=_render(database_url)),
        )
        yield database_url
    finally:
        with psycopg.connect(_psycopg_url(maintenance), autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def test_upgrade_heads_records_and_creates_each_composed_module(
    isolated_database: URL,
) -> None:
    """Heads recorded, schema present, RLS enforced — all three or none.

    Asserting only the recorded head would pass for a lineage that ran and
    created nothing; asserting only the tables would pass for a schema someone
    created by hand.
    """
    command.upgrade(_sub_config(isolated_database), "heads")

    engine = create_engine(_render(isolated_database))
    try:
        with engine.connect() as connection:
            recorded = set(
                connection.scalars(text("SELECT version_num FROM alembic_version"))
            )
            for import_name in _declared_lineages():
                manifest = import_module(f"{import_name}.manifest").module
                schema = manifest.db_schema
                versions_dir = import_module(f"{import_name}.migrations").versions_dir()
                shipped = {
                    path.stem
                    for path in versions_dir.glob("*.py")
                    if not path.stem.startswith("__")
                }

                assert shipped & recorded, (
                    f"{import_name}: no revision of this module is recorded in "
                    f"alembic_version after `upgrade heads`; "
                    f"recorded={sorted(recorded)}"
                )

                tables = set(
                    connection.scalars(
                        text(
                            "SELECT tablename FROM pg_tables WHERE schemaname = :schema"
                        ),
                        {"schema": schema},
                    )
                )
                assert tables, f"{import_name}: schema {schema!r} has no tables"

                unprotected = sorted(
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = :schema AND c.relkind = 'r' "
                            "AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"
                        ),
                        {"schema": schema},
                    )
                )
                assert not unprotected, (
                    f"{import_name}: tables in {schema!r} without ENABLE+FORCE "
                    f"row level security: {unprotected}"
                )
    finally:
        engine.dispose()
