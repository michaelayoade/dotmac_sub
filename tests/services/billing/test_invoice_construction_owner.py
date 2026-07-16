from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import InvoiceStatus, LedgerEntry, TaxApplication
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services.billing.invoices import InvoiceLines, Invoices


def test_system_invoice_and_line_staging_record_exact_document_audit(
    db_session, subscriber_account
):
    invoice = Invoices.stage_system_invoice(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.draft,
            currency="NGN",
        ),
        reason="owner-test",
    )
    payload = SystemInvoiceLineCreate(
        invoice_id=invoice.id,
        description="Recurring service",
        quantity=Decimal("2.000"),
        unit_price=Decimal("25.00"),
        amount=Decimal("50.00"),
        tax_application=TaxApplication.exempt,
        metadata_={"kind": "test"},
        billing_line_key=f"owner-test:{invoice.id}",
    )
    line = InvoiceLines.stage_system_line(
        db_session,
        payload,
        reason="owner-test",
    )
    db_session.commit()

    assert line.amount == Decimal("50.00")
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.invoice_id == invoice.id)
        .count()
        == 0
    )
    invoice_audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "stage_system_invoice")
        .filter(AuditEvent.entity_id == str(invoice.id))
        .one()
    )
    line_audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "stage_system_invoice_line")
        .filter(AuditEvent.entity_id == str(line.id))
        .one()
    )
    assert invoice_audit.metadata_["ledger_transaction_id"] is None
    assert line_audit.metadata_["amount"] == "50.00"
    assert line_audit.metadata_["ledger_transaction_id"] is None

    replay = InvoiceLines.stage_system_line(
        db_session,
        payload,
        reason="owner-test-retry",
    )
    assert replay.id == line.id
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "stage_system_invoice_line")
        .filter(AuditEvent.entity_id == str(line.id))
        .count()
        == 1
    )


def test_system_line_rejects_source_key_drift(db_session, subscriber_account):
    invoice = Invoices.stage_system_invoice(
        db_session,
        InvoiceCreate(account_id=subscriber_account.id),
        reason="owner-test",
    )
    key = f"owner-test:{invoice.id}"
    InvoiceLines.stage_system_line(
        db_session,
        SystemInvoiceLineCreate(
            invoice_id=invoice.id,
            description="Recurring service",
            unit_price=Decimal("25.00"),
            billing_line_key=key,
        ),
        reason="owner-test",
    )

    with pytest.raises(HTTPException) as exc:
        InvoiceLines.stage_system_line(
            db_session,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                description="Recurring service",
                unit_price=Decimal("30.00"),
                billing_line_key=key,
            ),
            reason="owner-test-drift",
        )

    assert exc.value.status_code == 409
    assert "different invoice line" in exc.value.detail


def test_system_line_rejects_nonconstructible_invoice_state(
    db_session, subscriber_account
):
    invoice = Invoices.stage_system_invoice(
        db_session,
        InvoiceCreate(account_id=subscriber_account.id),
        reason="owner-test",
    )
    invoice.status = InvoiceStatus.overdue
    db_session.flush()

    with pytest.raises(HTTPException) as exc:
        InvoiceLines.stage_system_line(
            db_session,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                description="Late usage",
                unit_price=Decimal("10.00"),
                billing_line_key=f"late-usage:{invoice.id}",
            ),
            reason="owner-test",
        )

    assert exc.value.status_code == 409
    assert "draft or issued" in exc.value.detail
