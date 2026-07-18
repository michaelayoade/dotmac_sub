from __future__ import annotations

import importlib.util
from pathlib import Path


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


def test_payment_prepaid_application_revision_is_linear_and_structural():
    migration = _load_migration()

    assert migration.revision == "357_payment_prepaid_applications"
    assert migration.down_revision == "356_party_first_referral_capture"
    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "payment_prepaid_applications" in source
    assert "payment_settlements.id" in source
    assert "service_entitlements.id" in source
    assert "payment_allocations.id" in source
    assert "invoice_closures.id" in source
    assert "ck_payment_prepaid_applications_period_order" in source
