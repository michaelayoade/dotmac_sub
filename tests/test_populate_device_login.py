"""Tests for populate_device_login() — staff-only RADIUS admin auth projection.

There is no reachable RADIUS Postgres in the test environment (RADIUS_DB_DSN is
blank).  Following the convention established in test_radius_accounting_import.py,
the RADIUS side is an in-memory SQLite engine whose DDL mirrors the real
admin_schema.sql tables.  The SystemUser (app) side uses the standard db_session
fixture backed by the in-memory SQLite engine from conftest.

Testable seam: populate_device_login accepts an optional _conn_factory kwarg.
When supplied, the function calls it to get a DB-API 2 connection instead of
opening psycopg.connect(RADIUS_DB_DSN).  Tests inject a thin adapter over a
SQLAlchemy in-memory SQLite engine so the RADIUS writes are inspectable.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from app.models.system_user import SystemUser
from app.services.credential_crypto import encrypt_credential
from app.services.radius_population import populate_device_login

# ---------------------------------------------------------------------------
# In-memory SQLite RADIUS admin DDL (mirrors config/freeradius/sql/admin_schema.sql)
# ---------------------------------------------------------------------------

_RADMIN_DDL = """
CREATE TABLE IF NOT EXISTS radcheck_admin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    attribute TEXT NOT NULL DEFAULT '',
    op TEXT NOT NULL DEFAULT '==',
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS radreply_admin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    attribute TEXT NOT NULL DEFAULT '',
    op TEXT NOT NULL DEFAULT '=',
    value TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# SQLite DB-API 2 adapter
# ---------------------------------------------------------------------------

class _SQLiteConn:
    """Wrap a SQLAlchemy in-memory SQLite engine as a minimal DB-API 2 connection.

    psycopg uses %s placeholders; SQLite uses ?.  This adapter translates them
    so populate_device_login can use the same SQL it would send to Postgres.
    """

    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine
        self._raw = engine.raw_connection()
        self._raw.execute("PRAGMA foreign_keys=ON")

    def cursor(self):
        return _SQLiteCursor(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    # Context manager support (mirrors psycopg.connect().__enter__)
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False


class _SQLiteCursor:
    """Minimal cursor that translates %s → ? for SQLite compatibility."""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    @staticmethod
    def _translate(sql: str) -> str:
        return sql.replace("%s", "?")

    def execute(self, sql: str, params=()) -> None:
        self._cur.execute(self._translate(sql), params)

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def radius_admin_engine():
    """Fresh in-memory SQLite engine with radcheck_admin / radreply_admin tables."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for stmt in _RADMIN_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    return engine


@pytest.fixture()
def radius_admin_db(radius_admin_engine):
    """Yield a SQLAlchemy connection for assertions, plus expose the conn_factory."""
    with radius_admin_engine.connect() as conn:
        yield conn


@pytest.fixture()
def conn_factory(radius_admin_engine):
    """Returns a zero-arg callable that produces a _SQLiteConn over the shared engine."""
    def _factory():
        return _SQLiteConn(radius_admin_engine)
    return _factory


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------

def _seed_staff(
    db_session,
    *,
    email: str,
    enabled: bool,
    secret: str = "s3cr3t",  # noqa: S107 - deliberate test credential.
) -> SystemUser:
    """Create a minimal active SystemUser with device-login fields set."""
    user = SystemUser(
        id=uuid.uuid4(),
        first_name="Test",
        last_name="Staff",
        email=email,
        is_active=True,
        device_login_enabled=enabled,
        device_login_secret=encrypt_credential(secret) if enabled else None,
        device_login_revoked_at=None,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_eligible_staff_projected(db_session, radius_admin_engine, conn_factory, monkeypatch):
    """An enabled staff member with router:admin permission gets Mikrotik-Group=full."""
    user = _seed_staff(db_session, email="tess@dotmac", enabled=True)

    # Short-circuit RBAC: inject effective_perms/roles on the module so
    # derive_router_tier sees "router:admin".
    monkeypatch.setattr(
        "app.services.radius_population.effective_perms",
        lambda db, uid: {"router:admin"},
    )
    monkeypatch.setattr(
        "app.services.radius_population.effective_roles",
        lambda db, uid: set(),
    )

    stats = populate_device_login(db_session, dry_run=False, _conn_factory=conn_factory)

    assert stats["considered"] >= 1
    assert stats["radcheck_upserts"] == 1
    assert stats["radreply_upserts"] == 2  # Mikrotik-Group + Service-Type

    with radius_admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT attribute, value FROM radreply_admin WHERE username=:u"),
            {"u": "tess@dotmac"},
        ).all()
    attrs = {(a, v) for a, v in rows}
    assert ("Mikrotik-Group", "full") in attrs
    assert ("Service-Type", "Administrative-User") in attrs

    with radius_admin_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM radcheck_admin WHERE username=:u"),
            {"u": "tess@dotmac"},
        ).scalar()
    assert n == 1

    # Verify the decrypt roundtrip: stored Cleartext-Password should equal the seeded plaintext secret
    with radius_admin_engine.connect() as conn:
        stored_secret = conn.execute(
            text("SELECT value FROM radcheck_admin WHERE username=:u AND attribute='Cleartext-Password'"),
            {"u": "tess@dotmac"},
        ).scalar()
    assert stored_secret == "s3cr3t"


def test_disabled_staff_removed(db_session, radius_admin_engine, conn_factory):
    """A staff member with device_login_enabled=False must have no admin RADIUS rows."""
    _seed_staff(db_session, email="ex@dotmac", enabled=False)

    stats = populate_device_login(db_session, dry_run=False, _conn_factory=conn_factory)

    assert stats["considered"] >= 1
    # removed counts the disabled user (enabled=False → treated as removed not skipped_ineligible)
    assert stats["removed"] == 1

    with radius_admin_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM radcheck_admin WHERE username=:u"),
            {"u": "ex@dotmac"},
        ).scalar()
    assert n == 0


def test_ineligible_skipped(db_session, radius_admin_engine, conn_factory, monkeypatch):
    """An enabled staff member with no router perms is skipped and counted."""
    _seed_staff(db_session, email="sup@dotmac", enabled=True)

    monkeypatch.setattr(
        "app.services.radius_population.effective_perms",
        lambda db, uid: {"customer:read"},
    )
    monkeypatch.setattr(
        "app.services.radius_population.effective_roles",
        lambda db, uid: set(),
    )

    stats = populate_device_login(db_session, dry_run=False, _conn_factory=conn_factory)

    assert stats["skipped_ineligible"] == 1
    assert stats["radcheck_upserts"] == 0

    with radius_admin_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM radcheck_admin WHERE username=:u"),
            {"u": "sup@dotmac"},
        ).scalar()
    assert n == 0


def test_dry_run_makes_no_writes(db_session, radius_admin_engine, conn_factory, monkeypatch):
    """dry_run=True must not persist any rows."""
    _seed_staff(db_session, email="dryrun@dotmac", enabled=True)

    monkeypatch.setattr(
        "app.services.radius_population.effective_perms",
        lambda db, uid: {"router:admin"},
    )
    monkeypatch.setattr(
        "app.services.radius_population.effective_roles",
        lambda db, uid: set(),
    )

    stats = populate_device_login(db_session, dry_run=True, _conn_factory=conn_factory)

    # Stats are computed even in dry_run
    assert stats["radcheck_upserts"] == 1

    with radius_admin_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM radcheck_admin")).scalar()
    assert n == 0


def test_idempotent_upsert(db_session, radius_admin_engine, conn_factory, monkeypatch):
    """Running populate_device_login twice must not duplicate rows."""
    _seed_staff(db_session, email="idem@dotmac", enabled=True)

    monkeypatch.setattr(
        "app.services.radius_population.effective_perms",
        lambda db, uid: {"router:admin"},
    )
    monkeypatch.setattr(
        "app.services.radius_population.effective_roles",
        lambda db, uid: set(),
    )

    populate_device_login(db_session, dry_run=False, _conn_factory=conn_factory)
    populate_device_login(db_session, dry_run=False, _conn_factory=conn_factory)

    with radius_admin_engine.connect() as conn:
        n_check = conn.execute(
            text("SELECT count(*) FROM radcheck_admin WHERE username=:u"),
            {"u": "idem@dotmac"},
        ).scalar()
        n_reply = conn.execute(
            text("SELECT count(*) FROM radreply_admin WHERE username=:u"),
            {"u": "idem@dotmac"},
        ).scalar()
    assert n_check == 1
    assert n_reply == 2
