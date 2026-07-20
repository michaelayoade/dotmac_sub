from decimal import Decimal
from uuid import uuid4

from app.models.field_erp_sync import FieldErpSyncEvent
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
    VendorPurchaseInvoice,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
)
from app.services.dotmac_erp.outbox import FLOW_ENDPOINTS
from app.services.dotmac_erp.purchase_invoice_sync import (
    build_purchase_invoice_payload,
)
from app.services.vendor_purchase_invoices import vendor_purchase_invoices


def _chain(db):
    project = Project(name="Phase 5 fiber install")
    vendor = Vendor(
        name="Native Fiber Vendor", code=f"NFV-{uuid4().hex[:6]}", erp_id=str(uuid4())
    )
    reviewer = SystemUser(
        first_name="Finance",
        last_name="Reviewer",
        display_name="Finance Reviewer",
        email=f"finance-{uuid4().hex[:6]}@example.com",
    )
    db.add_all([project, vendor, reviewer])
    db.flush()
    install = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
        erp_purchase_order_id="PO-2026-0042",
    )
    db.add(install)
    db.flush()
    quote = ProjectQuote(
        project_id=install.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db.add(quote)
    db.commit()
    return install, vendor, reviewer


def test_vendor_invoice_lifecycle_and_neutral_erp_contract(db_session):
    install, vendor, reviewer = _chain(db_session)
    invoice = vendor_purchase_invoices.create(
        db_session,
        VendorPurchaseInvoiceCreate(
            project_id=install.id,
            invoice_number="VENDOR-2026-14",
            tax_rate_percent=Decimal("7.5"),
        ),
        vendor_id=str(vendor.id),
        created_by_system_user_id=str(reviewer.id),
    )
    invoice = vendor_purchase_invoices.add_line(
        db_session,
        str(invoice["id"]),
        VendorPurchaseInvoiceLineCreate(
            item_type="labor",
            description="Fiber installation labor",
            quantity=Decimal("2"),
            unit_price=Decimal("50000"),
        ),
        vendor_id=str(vendor.id),
    )
    assert invoice["subtotal"] == Decimal("100000.00")
    assert invoice["tax_total"] == Decimal("7500.00")
    assert invoice["total"] == Decimal("107500.00")

    submitted = vendor_purchase_invoices.submit(
        db_session, str(invoice["id"]), vendor_id=str(vendor.id)
    )
    assert submitted["status"] == "submitted"
    approved = vendor_purchase_invoices.approve(
        db_session,
        str(invoice["id"]),
        reviewer_system_user_id=str(reviewer.id),
        review_notes="PO and completion evidence checked",
    )
    assert approved["status"] == "approved"
    assert approved["erp_purchase_order_id"] == "PO-2026-0042"

    # Flow ownership defaults to CRM until an explicit cutover. Approval is
    # preserved locally and the repair sweep will enqueue it after ownership.
    assert db_session.query(FieldErpSyncEvent).count() == 0
    payload = build_purchase_invoice_payload(
        db_session.get(VendorPurchaseInvoice, approved["id"])
    )
    assert payload["source_invoice_id"] == str(approved["id"])
    assert payload["source_project_id"] == str(install.project_id)
    assert "crm_invoice_id" not in payload
    assert FLOW_ENDPOINTS["purchase_invoice"] == "/api/v1/sync/sub/purchase-invoices"
