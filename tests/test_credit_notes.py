from decimal import Decimal

from app.models.billing import CreditNoteStatus
from app.schemas.billing import (
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
    InvoiceCreate,
)
from app.services import billing as billing_service


def test_credit_note_apply_reduces_invoice_balance(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        ),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.issued,
            currency="USD",
        ),
    )
    billing_service.credit_note_lines.create(
        db_session,
        CreditNoteLineCreate(
            credit_note_id=credit_note.id,
            description="Service credit",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
        ),
    )
    application = billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("30.00")),
    )
    assert application.amount == Decimal("30.00")
    refreshed_invoice = billing_service.invoices.get(db_session, str(invoice.id))
    assert refreshed_invoice.balance_due == Decimal("70.00")
    refreshed_credit_note = billing_service.credit_notes.get(db_session, str(credit_note.id))
    assert refreshed_credit_note.applied_total == Decimal("30.00")
    assert refreshed_credit_note.status == CreditNoteStatus.partially_applied


def test_credit_note_apply_without_lines_uses_total(db_session, subscriber_account):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("120.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("120.00"),
            balance_due=Decimal("120.00"),
        ),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.issued,
            currency="USD",
            subtotal=Decimal("50.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("50.00"),
        ),
    )
    application = billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id),
    )
    assert application.amount == Decimal("50.00")
    refreshed_invoice = billing_service.invoices.get(db_session, str(invoice.id))
    assert refreshed_invoice.balance_due == Decimal("70.00")
    refreshed_credit_note = billing_service.credit_notes.get(db_session, str(credit_note.id))
    assert refreshed_credit_note.total == Decimal("50.00")
    assert refreshed_credit_note.applied_total == Decimal("50.00")
    assert refreshed_credit_note.status == CreditNoteStatus.applied
