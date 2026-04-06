from __future__ import annotations

import pytest

from app import main as main_module


class _FakeInspector:
    def __init__(self, *, has_table: bool = True, columns: list[str] | None = None):
        self._has_table = has_table
        self._columns = columns or []

    def has_table(self, name: str) -> bool:
        assert name == "ont_units"
        return self._has_table

    def get_columns(self, name: str) -> list[dict[str, str]]:
        assert name == "ont_units"
        return [{"name": column} for column in self._columns]


class _FakeSession:
    def get_bind(self):
        return object()

    def close(self) -> None:
        return None


def test_assert_required_schema_accepts_contact_column(monkeypatch):
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        main_module,
        "sqlalchemy_inspect",
        lambda _bind: _FakeInspector(columns=["id", "serial_number", "contact"]),
    )

    main_module._assert_required_schema()


def test_assert_required_schema_requires_contact_column(monkeypatch):
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(
        main_module,
        "sqlalchemy_inspect",
        lambda _bind: _FakeInspector(columns=["id", "serial_number"]),
    )

    with pytest.raises(RuntimeError) as exc_info:
        main_module._assert_required_schema()

    assert "ont_units.contact" in str(exc_info.value)
    assert "alembic upgrade head" in str(exc_info.value)
