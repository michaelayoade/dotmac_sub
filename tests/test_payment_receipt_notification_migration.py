from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/391_payment_receipt_notification.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "payment_receipt_notification_migration", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payment_receipt_migration_updates_only_exact_default_copy():
    module = _module()
    source = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "391_payment_receipt_notification"
    assert module.down_revision == "390_provisioning_lifecycle_sot"
    assert "AND subject = :old_subject" in source
    assert "AND body = :old_body" in source
    assert "SET is_active" not in source
    assert "receipt_number" in module._NEW_SUBJECT
    assert "receipt_url" in module._NEW_BODY
