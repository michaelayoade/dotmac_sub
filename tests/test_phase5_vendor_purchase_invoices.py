from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.project import Project
from app.models.stored_file import StoredFile
from app.models.subscription_engine import SettingValueType
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.dotmac_erp.outbox import FLOW_ENDPOINTS
from app.services.dotmac_erp.purchase_invoice_sync import (
    build_purchase_invoice_payload,
)
from app.services.events.handlers.materials_lifecycle_projection import (
    MaterialsLifecycleProjectionHandler,
)
from app.services.events.types import Event, EventType
from app.services.file_storage import file_uploads
from app.services.owner_commands import CommandContext
from app.services.vendor_purchase_invoices import (
    AddVendorPurchaseInvoiceLineCommand,
    CreateVendorPurchaseInvoiceCommand,
    ReviewVendorPurchaseInvoiceCommand,
    UploadVendorPurchaseInvoiceAttachmentCommand,
    vendor_purchase_invoices,
)
from app.services.vendor_submission_proposals import (
    ConfirmVendorSubmissionCommand,
    confirm_submission,
    issue_purchase_invoice_submission,
)


def _context(*, actor: str, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor,
        scope=scope,
        reason=reason,
    )


def _chain(db):
    project = Project(name="Phase 5 fiber install")
    vendor = Vendor(
        name="Native Fiber Vendor",
        code=f"NFV-{uuid4().hex[:6]}",
        supplier_reference=str(uuid4()),
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
        procurement_order_reference="PO-2026-0042",
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


def _seed_purchase_invoice_flow(db):
    for flow in FieldErpSyncFlow:
        db.add(
            SyncFlowOwnership(
                flow=flow.value,
                owner=(
                    SyncFlowOwner.sub.value
                    if flow is FieldErpSyncFlow.purchase_invoice
                    else SyncFlowOwner.crm.value
                ),
            )
        )
    db.flush()


def _set_vendor_invoice_tax_profile(db, profile: str):
    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="vendor_purchase_invoice_erp_tax_profile",
            value_type=SettingValueType.string,
            value_text=profile,
            is_active=True,
        )
    )
    db.flush()


def test_vendor_invoice_lifecycle_and_neutral_erp_contract(db_session):
    install, vendor, reviewer = _chain(db_session)
    install_id = str(install.id)
    vendor_id = str(vendor.id)
    reviewer_id = str(reviewer.id)
    create_command = CreateVendorPurchaseInvoiceCommand(
        context=_context(
            actor=reviewer_id,
            scope=vendor_id,
            reason="test_invoice_creation",
        ),
        payload=VendorPurchaseInvoiceCreate(
            project_id=install_id,
            invoice_number="VENDOR-2026-14",
            tax_rate_percent=Decimal("7.5"),
        ),
        vendor_id=vendor_id,
        created_by_system_user_id=reviewer_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    invoice = vendor_purchase_invoices.create(
        db_session,
        create_command,
    )
    invoice = vendor_purchase_invoices.add_line(
        db_session,
        AddVendorPurchaseInvoiceLineCommand(
            context=_context(
                actor=reviewer_id,
                scope=vendor_id,
                reason="test_invoice_line_addition",
            ),
            invoice_id=str(invoice["id"]),
            payload=VendorPurchaseInvoiceLineCreate(
                item_type="labor",
                description="Fiber installation labor",
                quantity=Decimal("2"),
                unit_price=Decimal("50000"),
            ),
            vendor_id=vendor_id,
        ),
    )
    assert invoice["subtotal"] == Decimal("100000.00")
    assert invoice["tax_total"] == Decimal("7500.00")
    assert invoice["total"] == Decimal("107500.00")

    proposal = issue_purchase_invoice_submission(
        db_session,
        invoice_id=str(invoice["id"]),
        vendor_id=vendor_id,
        user_id=reviewer_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    confirm_submission(
        db_session,
        ConfirmVendorSubmissionCommand(
            context=_context(
                actor=reviewer_id,
                scope=vendor_id,
                reason="test_invoice_submission_confirmation",
            ),
            confirmation_token=proposal.confirmation_token,
            vendor_id=vendor_id,
            user_id=reviewer_id,
            project_id=install_id,
        ),
    )
    submitted = vendor_purchase_invoices.get(db_session, str(invoice["id"]))
    assert submitted["status"] == "submitted"
    db_session_adapter.release_read_transaction(db_session)
    approved = vendor_purchase_invoices.review(
        db_session,
        ReviewVendorPurchaseInvoiceCommand(
            context=_context(
                actor=reviewer_id,
                scope=str(invoice["id"]),
                reason="test_invoice_approval",
            ),
            invoice_id=str(invoice["id"]),
            reviewer_system_user_id=reviewer_id,
            approve=True,
            review_notes="PO and completion evidence checked",
        ),
    )
    assert approved["status"] == "approved"
    assert approved["procurement_order_reference"] == "PO-2026-0042"

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


def test_project_completion_creates_po_backed_erp_invoice_with_tax_profile(
    db_session,
):
    install, vendor, _reviewer = _chain(db_session)
    quote = (
        db_session.query(ProjectQuote)
        .filter(ProjectQuote.project_id == install.id)
        .one()
    )
    quote.status = ProjectQuoteStatus.approved.value
    quote.currency = "NGN"
    quote.vat_rate_percent = Decimal("0.00")
    quote.subtotal = Decimal("100000.00")
    quote.tax_total = Decimal("0.00")
    quote.total = Decimal("100000.00")
    db_session.add(
        ProjectQuoteLineItem(
            quote_id=quote.id,
            item_type="labor",
            description="Completed fiber installation",
            quantity=Decimal("1.000"),
            unit_price=Decimal("100000.00"),
            amount=Decimal("100000.00"),
            is_active=True,
        )
    )
    install.status = InstallationProjectStatus.completed.value
    install.approved_quote_id = quote.id
    install.assigned_vendor_id = vendor.id
    _seed_purchase_invoice_flow(db_session)
    _set_vendor_invoice_tax_profile(db_session, "WHT 2%")
    db_session.commit()

    event = Event(
        EventType.vendor_project_completed,
        {
            "schema_version": 1,
            "project_id": str(install.id),
            "native_project_id": str(install.project_id),
            "vendor_id": str(vendor.id),
        },
    )

    MaterialsLifecycleProjectionHandler().handle(db_session, event)
    db_session.expire_all()

    invoice = (
        db_session.query(VendorPurchaseInvoice)
        .filter(VendorPurchaseInvoice.project_id == install.id)
        .one()
    )
    assert invoice.status == "approved"
    assert invoice.invoice_number == "AUTO-PO-2026-0042"
    assert invoice.procurement_order_reference == "PO-2026-0042"
    assert invoice.subtotal == Decimal("100000.00")
    assert invoice.total == Decimal("100000.00")

    row = db_session.query(FieldErpSyncEvent).one()
    assert row.flow == FieldErpSyncFlow.purchase_invoice.value
    assert row.entity_id == invoice.id
    assert row.payload["erp_purchase_order_id"] == "PO-2026-0042"
    assert row.payload["tax_profile"] == "WHT 2%"

    MaterialsLifecycleProjectionHandler().handle(db_session, event)
    assert db_session.query(FieldErpSyncEvent).count() == 1
    assert db_session.query(VendorPurchaseInvoice).count() == 1


def test_purchase_invoice_payload_includes_configured_erp_tax_profile(db_session):
    install, vendor, reviewer = _chain(db_session)
    invoice = VendorPurchaseInvoice(
        project_id=install.id,
        vendor_id=vendor.id,
        invoice_number="VENDOR-TAX-PROFILE",
        currency="NGN",
        status="approved",
        procurement_order_reference="PO-2026-0042",
        reviewed_by_system_user_id=reviewer.id,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            description="Tax profile proof",
            quantity=Decimal("1"),
            unit_price=Decimal("1000"),
            amount=Decimal("1000"),
            is_active=True,
        )
    )
    invoice.subtotal = Decimal("1000.00")
    invoice.tax_total = Decimal("0.00")
    invoice.total = Decimal("1000.00")
    payload = build_purchase_invoice_payload(invoice, erp_tax_profile="WHT 2%")
    assert payload["tax_profile"] == "WHT 2%"


def test_vendor_invoice_review_rolls_back_when_erp_enqueue_fails(
    db_session, monkeypatch
):
    install, vendor, reviewer = _chain(db_session)
    invoice = VendorPurchaseInvoice(
        project_id=install.id,
        vendor_id=vendor.id,
        invoice_number="VENDOR-ROLLBACK-ERP",
        currency="NGN",
        status="submitted",
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            description="Rollback proof",
            quantity=Decimal("1"),
            unit_price=Decimal("1000"),
            amount=Decimal("1000"),
            is_active=True,
        )
    )
    invoice_id = str(invoice.id)
    reviewer_id = str(reviewer.id)
    db_session.commit()

    def _fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("synthetic ERP enqueue failure")

    from app.services import backoffice

    monkeypatch.setattr(
        backoffice,
        "enqueue_purchase_invoice_outbox",
        _fail_enqueue,
    )
    command = ReviewVendorPurchaseInvoiceCommand(
        context=_context(
            actor=reviewer_id,
            scope=invoice_id,
            reason="test_atomic_invoice_approval",
        ),
        invoice_id=invoice_id,
        reviewer_system_user_id=reviewer_id,
        approve=True,
        review_notes="Must roll back",
    )

    vendor_purchase_invoices.review(db_session, command)
    db_session.expire_all()

    # The approval is the owner's committed fact; the payables export is a
    # receipted consequence, so an ERP-transport failure leaves the approval
    # intact and the delivery durably failed and retryable.
    persisted = db_session.get(VendorPurchaseInvoice, invoice.id)
    assert persisted is not None
    assert persisted.status == "approved"
    assert persisted.reviewed_at is not None
    from app.models.event_store import EventStatus, EventStore

    delivery = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_purchase_invoice.approved")
        .one()
    )
    assert delivery.status == EventStatus.failed


def test_vendor_invoice_attachment_metadata_rolls_back_after_staging_failure(
    db_session, monkeypatch
):
    from app.services import vendor_purchase_invoice_records

    install, vendor, reviewer = _chain(db_session)
    invoice = VendorPurchaseInvoice(
        project_id=install.id,
        vendor_id=vendor.id,
        invoice_number="VENDOR-ROLLBACK-FILE",
        currency="NGN",
    )
    db_session.add(invoice)
    db_session.flush()
    invoice_id = str(invoice.id)
    vendor_id = str(vendor.id)
    reviewer_id = str(reviewer.id)
    db_session.commit()

    uploaded_keys: list[str] = []

    class _Storage:
        def upload(self, key: str, _data: bytes, _content_type: str | None):
            uploaded_keys.append(key)

    def _fail_event(*_args, **_kwargs):
        raise RuntimeError("synthetic event staging failure")

    monkeypatch.setattr(file_uploads, "storage", _Storage())
    monkeypatch.setattr(
        vendor_purchase_invoice_records,
        "_emit_change",
        _fail_event,
    )
    command = UploadVendorPurchaseInvoiceAttachmentCommand(
        context=_context(
            actor=reviewer_id,
            scope=vendor_id,
            reason="test_atomic_invoice_attachment",
        ),
        invoice_id=invoice_id,
        vendor_id=vendor_id,
        file_name="vendor-invoice.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4 rollback proof",
    )

    with pytest.raises(RuntimeError, match="synthetic event staging failure"):
        vendor_purchase_invoices.upload_attachment(db_session, command)

    persisted = db_session.get(VendorPurchaseInvoice, invoice.id)
    assert persisted is not None
    assert persisted.attachment_stored_file_id is None
    assert db_session.query(StoredFile).count() == 0
    assert len(uploaded_keys) == 1
