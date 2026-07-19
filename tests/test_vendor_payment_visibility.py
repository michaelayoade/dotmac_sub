"""Vendor payment labels require a refreshed ERP-owned projection.

The current ERP status field is populated from the purchase-invoice creation
response and is not refreshed after AP settlement.  Until ERP exposes a read
contract and Sub owns an idempotent refresher, the vendor portal must not turn
that creation-time snapshot into a claim that money was or was not paid.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/vendor/project_detail.html").read_text(encoding="utf-8")
SERVICE = (ROOT / "app/services/vendor_purchase_invoices.py").read_text(
    encoding="utf-8"
)
SOT = (ROOT / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")


def test_creation_time_erp_status_is_not_presented_as_payment_truth():
    assert "invoice.payment" not in TEMPLATE
    assert "_payment_projection" not in SERVICE
    assert "is not a refreshed payment projection" in SOT


def test_payment_visibility_contract_names_the_missing_refresh_boundary():
    assert "dedicated ERP read contract" in SOT
    assert "idempotent Sub refresher" in SOT
