"""The single authority for the bundled FreeRADIUS DSN.

Both RADIUS writers must resolve the same target through these functions so they
cannot split-brain.
"""

import pytest

from app.services import radius_dsn

_ENV_KEYS = [
    "RADIUS_SYNC_DB_URL",
    "RADIUS_DB_DSN",
    "RADIUS_DB_HOST",
    "RADIUS_DB_PORT",
    "RADIUS_DB_NAME",
    "RADIUS_DB_USER",
    "RADIUS_DB_PASS",
]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_dsn_normalized_to_sqlalchemy_form(monkeypatch):
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@h:5432/radius")
    assert radius_dsn.resolve_radius_dsn() == "postgresql+psycopg://u:p@h:5432/radius"


def test_libpq_form_round_trips(monkeypatch):
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@h:5432/radius")
    libpq = radius_dsn.radius_dsn_libpq()
    assert libpq == "postgresql://u:p@h:5432/radius"
    assert "+psycopg" not in libpq


def test_sync_url_takes_precedence_over_dsn(monkeypatch):
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@h1:5432/radius")
    monkeypatch.setenv("RADIUS_SYNC_DB_URL", "postgresql://u:p@h2:5433/rad2")
    assert radius_dsn.resolve_radius_dsn() == "postgresql+psycopg://u:p@h2:5433/rad2"


def test_localhost_rewritten_to_container_host(monkeypatch):
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@localhost:5432/radius")
    monkeypatch.setenv("RADIUS_DB_HOST", "postgres-local")
    assert radius_dsn.resolve_radius_dsn() == (
        "postgresql+psycopg://u:p@postgres-local:5432/radius"
    )


def test_non_default_port_localhost_kept(monkeypatch):
    # A host-mapped port (e.g. 9001) means the URL is intentionally host-facing.
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@localhost:9001/radius")
    monkeypatch.setenv("RADIUS_DB_HOST", "postgres-local")
    assert radius_dsn.resolve_radius_dsn() == (
        "postgresql+psycopg://u:p@localhost:9001/radius"
    )


def test_constructed_from_parts_when_no_url(monkeypatch):
    monkeypatch.setenv("RADIUS_DB_HOST", "rhost")
    monkeypatch.setenv("RADIUS_DB_NAME", "rdb")
    monkeypatch.setenv("RADIUS_DB_USER", "ruser")
    monkeypatch.setenv("RADIUS_DB_PASS", "rpass")
    assert radius_dsn.resolve_radius_dsn() == (
        "postgresql+psycopg://ruser:rpass@rhost:5432/rdb"
    )


def test_both_writers_resolve_identical_target(monkeypatch):
    """The population sweep (libpq) and the sync path (SQLAlchemy) must point at
    the same database — same host/port/name/user, differing only by driver."""
    monkeypatch.setenv("RADIUS_DB_DSN", "postgresql://u:p@h:5432/radius")
    sweep = radius_dsn.radius_dsn_libpq()  # radius_population uses this
    sync = radius_dsn.resolve_radius_dsn()  # radius.py uses this
    assert sweep == sync.replace("postgresql+psycopg://", "postgresql://", 1)
