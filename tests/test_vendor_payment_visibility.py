"""ERP-owned vendor payment state is refreshed and projected honestly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.project import Project
from app.models.vendor_routes import (
    InstallationProject,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.services.dotmac_erp.client import DotMacERPClient
from app.services.dotmac_erp.purchase_invoice_sync import (
    apply_erp_response,
    refresh_purchase_invoice_statuses,
)
from app.services.ui_contracts import StateKind
from app.services.vendor_payment_status import project_vendor_payment_status

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates/vendor/project_detail.html").read_text(encoding="utf-8")
SOT = (ROOT / "docs/SOT_RELATIONSHIP_MAP.md").read_text(encoding="utf-8")


class _ERPClient:
    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[str] = []

    def get_purchase_invoice_status(self, source_invoice_id: str) -> dict | None:
        self.calls.append(source_invoice_id)
        if self.error:
            raise self.error
        return self.response


def _invoice(db_session) -> VendorPurchaseInvoice:
    project = Project(name="Vendor payment projection")
    vendor = Vendor(name="Projection Vendor", code=f"PV-{uuid4().hex[:6]}")
    db_session.add_all([project, vendor])
    db_session.flush()
    install = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        erp_purchase_order_id="PO-2026-0042",
    )
    db_session.add(install)
    db_session.flush()
    invoice = VendorPurchaseInvoice(
        project_id=install.id,
        vendor_id=vendor.id,
        invoice_number=f"VENDOR-{uuid4().hex[:8]}",
        status=VendorPurchaseInvoiceStatus.approved.value,
        currency="NGN",
        total=Decimal("100000.00"),
        erp_purchase_invoice_id=str(uuid4()),
        # Creation response snapshot: deliberately not payment evidence.
        erp_purchase_invoice_creation_status="draft",
        erp_purchase_invoice_status=None,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def _response(invoice: VendorPurchaseInvoice, *, status: str = "partially_paid"):
    return {
        "source_invoice_id": str(invoice.id),
        "purchase_invoice_id": invoice.erp_purchase_invoice_id,
        "invoice_number": "PINV-2026-0042",
        "status": status,
        "currency": "NGN",
        "total_amount": "100000.00",
        "amount_paid": "40000.00" if status != "paid" else "100000.00",
        "balance_due": "60000.00" if status != "paid" else "0.00",
        "source_updated_at": "2026-07-20T11:55:00+00:00",
    }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_refresher_projects_reconciled_erp_observation_idempotently(db_session):
    invoice = _invoice(db_session)
    observed_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    client = _ERPClient(_response(invoice))

    first = refresh_purchase_invoice_statuses(
        db_session,
        client=client,  # type: ignore[arg-type]
        observed_at=observed_at,
    )
    db_session.refresh(invoice)

    assert first == {"processed": 1, "observed": 1, "changed": 1, "errors": []}
    assert invoice.erp_purchase_invoice_status == "partially_paid"
    assert invoice.erp_purchase_invoice_total_amount == Decimal("100000.000000")
    assert invoice.erp_purchase_invoice_amount_paid == Decimal("40000.000000")
    assert invoice.erp_purchase_invoice_balance_due == Decimal("60000.000000")
    assert _as_utc(invoice.erp_purchase_invoice_status_observed_at) == observed_at
    assert invoice.erp_purchase_invoice_status_error is None

    second = refresh_purchase_invoice_statuses(
        db_session,
        client=client,  # type: ignore[arg-type]
        observed_at=observed_at,
    )
    assert second == {"processed": 1, "observed": 1, "changed": 0, "errors": []}
    assert client.calls == [str(invoice.id), str(invoice.id)]


def test_refresh_failure_retains_last_good_observation(db_session):
    invoice = _invoice(db_session)
    observed_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    refresh_purchase_invoice_statuses(
        db_session,
        client=_ERPClient(_response(invoice)),  # type: ignore[arg-type]
        observed_at=observed_at,
    )

    result = refresh_purchase_invoice_statuses(
        db_session,
        client=_ERPClient(error=RuntimeError("ERP unavailable")),  # type: ignore[arg-type]
        observed_at=observed_at + timedelta(minutes=5),
    )
    db_session.refresh(invoice)

    assert result["processed"] == 1
    assert result["observed"] == 0
    assert result["changed"] == 0
    assert result["errors"] == [f"{invoice.id}: ERP unavailable"]
    assert invoice.erp_purchase_invoice_status == "partially_paid"
    assert _as_utc(invoice.erp_purchase_invoice_status_observed_at) == observed_at
    assert invoice.erp_purchase_invoice_status_error == "ERP unavailable"


def test_invalid_erp_observation_is_rejected_without_overwriting_last_good(db_session):
    invoice = _invoice(db_session)
    observed_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    refresh_purchase_invoice_statuses(
        db_session,
        client=_ERPClient(_response(invoice)),  # type: ignore[arg-type]
        observed_at=observed_at,
    )
    invalid = _response(invoice, status="paid")
    invalid["currency"] = "USD"

    result = refresh_purchase_invoice_statuses(
        db_session,
        client=_ERPClient(invalid),  # type: ignore[arg-type]
        observed_at=observed_at + timedelta(minutes=5),
    )
    current = db_session.get(VendorPurchaseInvoice, invoice.id)

    assert result["observed"] == 0
    assert result["changed"] == 0
    assert "currency does not match" in result["errors"][0]
    assert current.erp_purchase_invoice_status == "partially_paid"
    assert current.erp_purchase_invoice_amount_paid == Decimal("40000.000000")
    assert current.erp_purchase_invoice_status_error == (
        "ERP payment observation currency does not match"
    )


def test_creation_replay_cannot_overwrite_refreshed_payment_status(db_session):
    invoice = _invoice(db_session)
    observed_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    refresh_purchase_invoice_statuses(
        db_session,
        client=_ERPClient(_response(invoice, status="paid")),  # type: ignore[arg-type]
        observed_at=observed_at,
    )

    apply_erp_response(
        db_session,
        SimpleNamespace(
            id="replayed-create",
            entity_id=invoice.id,
            erp_response={
                "purchase_invoice_id": invoice.erp_purchase_invoice_id,
                "status": "draft",
            },
        ),
    )
    current = db_session.get(VendorPurchaseInvoice, invoice.id)

    assert current.erp_purchase_invoice_creation_status == "draft"
    assert current.erp_purchase_invoice_status == "paid"
    assert current.erp_purchase_invoice_amount_paid == Decimal("100000.000000")


def test_client_uses_source_invoice_status_contract() -> None:
    client = DotMacERPClient("https://erp.example.test", "token", retries=0)
    client.get = MagicMock(return_value={"status": "paid"})  # type: ignore[method-assign]

    result = client.get_purchase_invoice_status("source-42")

    assert result == {"status": "paid"}
    client.get.assert_called_once_with(  # type: ignore[attr-defined]
        "/api/v1/sync/sub/purchase-invoices/source-42"
    )


def _projection_row(**overrides):
    values = {
        "currency": "NGN",
        "erp_purchase_invoice_id": "erp-1",
        "erp_purchase_invoice_status": "paid",
        "erp_purchase_invoice_total_amount": Decimal("100000"),
        "erp_purchase_invoice_amount_paid": Decimal("100000"),
        "erp_purchase_invoice_balance_due": Decimal("0"),
        "erp_purchase_invoice_status_observed_at": datetime(
            2026, 7, 20, 12, 0, tzinfo=UTC
        ),
        "erp_purchase_invoice_status_source_updated_at": datetime(
            2026, 7, 20, 11, 55, tzinfo=UTC
        ),
        "erp_purchase_invoice_status_error": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_projection_distinguishes_current_stale_and_unobserved_state() -> None:
    now = datetime(2026, 7, 20, 12, 5, tzinfo=UTC)
    current = project_vendor_payment_status(_projection_row(), now=now)
    assert current.status.kind is StateKind.present
    assert current.status.value.label == "Paid"
    assert current.amount_paid.value == Decimal("100000")

    stale = project_vendor_payment_status(
        _projection_row(erp_purchase_invoice_status_error="ERP unavailable"),
        now=now,
    )
    assert stale.status.kind is StateKind.stale
    assert "refresh is delayed" in stale.detail

    unobserved = project_vendor_payment_status(
        _projection_row(
            erp_purchase_invoice_status="draft",
            erp_purchase_invoice_status_observed_at=None,
            erp_purchase_invoice_total_amount=None,
            erp_purchase_invoice_amount_paid=None,
            erp_purchase_invoice_balance_due=None,
        ),
        now=now,
    )
    assert unobserved.status.kind is StateKind.unknown
    assert unobserved.status.value is None


def test_vendor_template_and_sot_use_only_refreshed_payment_observation() -> None:
    assert "invoice.payment.status.is_present" in TEMPLATE
    assert "invoice.erp_purchase_invoice_status" not in TEMPLATE
    assert "Observed" in TEMPLATE
    assert "GET /api/v1/sync/sub/purchase-invoices/{source_invoice_id}" in SOT
    assert "never proves paid or unpaid state" in SOT
