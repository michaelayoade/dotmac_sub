from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/357_payment_prepaid_applications.py"
    )
    spec = importlib.util.spec_from_file_location("migration_357", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_retirement():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/394_retire_payment_prepaid_applications.py"
    )
    spec = importlib.util.spec_from_file_location("migration_394", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payment_prepaid_application_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "359_payment_prepaid_applications"
    assert migration.down_revision == "358_paystack_allocation_exceptions"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "payment_prepaid_applications" in source
    assert "payment_settlements.id" in source
    assert "service_entitlements.id" in source
    assert "payment_allocations.id" in source
    assert "invoice_closures.id" in source
    assert "ck_payment_prepaid_applications_period_order" in source


def test_payment_prepaid_application_retirement_drops_only_an_empty_table(
    monkeypatch,
):
    migration = _load_retirement()
    bind = MagicMock()
    bind.scalar.return_value = False
    drop_table = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "drop_table", drop_table)

    migration.upgrade()

    drop_table.assert_called_once_with("payment_prepaid_applications")


def test_payment_prepaid_application_retirement_fails_closed_on_evidence(monkeypatch):
    migration = _load_retirement()
    bind = MagicMock()
    bind.scalar.return_value = True
    drop_table = MagicMock()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "drop_table", drop_table)

    with pytest.raises(RuntimeError, match="contains evidence"):
        migration.upgrade()

    drop_table.assert_not_called()
