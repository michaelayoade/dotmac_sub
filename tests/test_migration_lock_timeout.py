"""The migration lock_timeout is bounded and sanitized.

Guards the deploy lock-trap fix: a schema-locking migration must fail fast on a
bounded lock_timeout rather than queue behind the live app. The value flows from
an ops env var into a Postgres ``SET`` statement, so malformed input must not
reach SQL.
"""

from __future__ import annotations

import pytest

from app.db import resolve_migration_lock_timeout


def test_defaults_to_5s(monkeypatch):
    monkeypatch.delenv("ALEMBIC_LOCK_TIMEOUT", raising=False)
    assert resolve_migration_lock_timeout() == "5s"


@pytest.mark.parametrize("value", ["5s", "3000ms", "30s", "2min", "0"])
def test_valid_env_override_is_used(monkeypatch, value):
    monkeypatch.setenv("ALEMBIC_LOCK_TIMEOUT", value)
    assert resolve_migration_lock_timeout() == value


@pytest.mark.parametrize(
    "bad",
    ["'; DROP TABLE x; --", "5 seconds", "abc", "5s; SELECT 1", "", "-5s", "5S"],
)
def test_malformed_input_falls_back_to_default(monkeypatch, bad):
    # No injection or malformed unit reaches the SET statement.
    monkeypatch.setenv("ALEMBIC_LOCK_TIMEOUT", bad)
    assert resolve_migration_lock_timeout() == "5s"


def test_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("ALEMBIC_LOCK_TIMEOUT", "99s")
    assert resolve_migration_lock_timeout("10s") == "10s"
    assert resolve_migration_lock_timeout("bad") == "5s"
