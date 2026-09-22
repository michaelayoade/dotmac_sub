"""Customer CSV recurring amounts must consume canonical financial owners."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPORT_OWNER = ROOT / "app/services/web_customer_lists.py"
ADMIN_ADAPTER = ROOT / "app/web/admin/customers.py"


def test_customer_recurring_export_uses_owner_decisions() -> None:
    source = EXPORT_OWNER.read_text(encoding="utf-8")
    assert "resolve_customer_chargeability(db, account_ids)" in source
    assert "resolve_billing_profiles(db, customers)" in source
    assert "resolve_subscription_reference_price(" in source
    assert "ChargeabilityReason.active_billing_treatment" in source
    assert "profile.effective_mode.value" in source


def test_customer_csv_adapter_does_not_calculate_charges() -> None:
    source = ADMIN_ADAPTER.read_text(encoding="utf-8")
    export_route = source.split("def export_customers(", maxsplit=1)[1].split(
        "def customer_availability_report(", maxsplit=1
    )[0]
    assert "build_customer_csv_export(" in export_route
    assert "Subscription.unit_price" not in export_route
    assert "billing_cycle" not in export_route
