"""The migration lock_timeout is bounded and sanitized.

Guards the deploy lock-trap fix: a schema-locking migration must fail fast on a
bounded lock_timeout rather than queue behind the live app. The raw value is
owned by ``settings.alembic_lock_timeout`` (the config owner) and interpolated
into a Postgres ``SET``, so malformed input must not reach SQL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db import resolve_migration_lock_timeout


def test_defaults_to_5s(monkeypatch):
    monkeypatch.setattr("app.db.settings", SimpleNamespace(alembic_lock_timeout="5s"))
    assert resolve_migration_lock_timeout() == "5s"


@pytest.mark.parametrize("value", ["5s", "3000ms", "30s", "2min", "0"])
def test_valid_settings_value_is_used(monkeypatch, value):
    monkeypatch.setattr("app.db.settings", SimpleNamespace(alembic_lock_timeout=value))
    assert resolve_migration_lock_timeout() == value


@pytest.mark.parametrize(
    "bad",
    ["'; DROP TABLE x; --", "5 seconds", "abc", "5s; SELECT 1", "", "-5s", "5S"],
)
def test_malformed_input_falls_back_to_default(monkeypatch, bad):
    # No injection or malformed unit reaches the SET statement.
    monkeypatch.setattr("app.db.settings", SimpleNamespace(alembic_lock_timeout=bad))
    assert resolve_migration_lock_timeout() == "5s"


def test_explicit_arg_overrides_settings(monkeypatch):
    monkeypatch.setattr("app.db.settings", SimpleNamespace(alembic_lock_timeout="99s"))
    assert resolve_migration_lock_timeout("10s") == "10s"
    assert resolve_migration_lock_timeout("bad") == "5s"
