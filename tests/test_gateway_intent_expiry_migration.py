"""Legacy gateway expiry backfill remains narrow and forward-safe."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/549_gateway_intent_terminal_state.py"
    )
    spec = importlib.util.spec_from_file_location("gateway_intent_expiry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_only_expires_elapsed_unsettled_supported_gateway_rows(
    monkeypatch,
):
    migration = _migration()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert len(statements) == 1
    statement = " ".join(statements[0].split())
    assert "status = 'expired'" in statement
    assert "status = 'pending'" in statement
    assert "completed_payment_id IS NULL" in statement
    assert "provider_type IN ('paystack', 'flutterwave')" in statement
    assert "expires_at <= CURRENT_TIMESTAMP" in statement
    assert "direct_bank_transfer" not in statement


def test_downgrade_does_not_reopen_expired_attempts(monkeypatch):
    migration = _migration()
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == []
