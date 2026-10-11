"""Independent, commit-capable SQLite lifecycle for the OIDC fast unit lane.

Every test owns its file database and may commit real root transactions. This
is model/service logic evidence only; the migrated PostgreSQL concurrency
module owns database acceptance. No application transaction guard is bypassed.
The spatial/type compatibility and kernel-tenant subset reuse conftest's
canonical SQLite recipe (also used by test_api_happy_path's isolated engine).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

import tests.conftest as compatibility
from app.db import Base


@pytest.fixture
def oidc_unit_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'oidc-atomic.sqlite'}")

    @event.listens_for(engine, "connect")
    def prepare_sqlite(
        connection: SQLiteConnection, _record: ConnectionPoolEntry
    ) -> None:
        connection.isolation_level = None
        connection.enable_load_extension(True)
        try:
            connection.load_extension("mod_spatialite")
            compatibility._enable_sqlite_spatial_admin()
            compatibility._restore_sqlite_geometry_passthrough()
        except Exception:
            compatibility._disable_sqlite_spatial_admin()
            compatibility._enable_sqlite_geometry_passthrough()
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("SELECT InitSpatialMetaData(1)")
        except Exception:
            pass  # The canonical fast lane permits plain SQLite without Spatialite.

    @event.listens_for(engine, "begin")
    def explicit_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    try:
        with engine.connect():
            pass
        Base.metadata.create_all(engine)
        compatibility._kernel_tenant_metadata().create_all(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(oidc_unit_engine: Engine) -> Iterator[Session]:
    # Engine-bound: each owner really commits/rolls back its root transaction.
    # Disposal of the per-test database provides isolation between tests.
    with Session(oidc_unit_engine, autoflush=False) as session:
        yield session
