from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.billing import CreditNote, CreditNoteStatus, Invoice, TaxApplication
from app.schemas.billing import (
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteUpdate,
    InvoiceCreate,
    TaxRateCreate,
)
from app.services import billing as billing_service
from app.services.locking import lock_for_update as _real_lock_for_update
from app.services.web_billing_credits import create_credit_from_form


def _issued_credit_note_and_invoice(db_session, account_id, *, amount):
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            currency="USD",
            subtotal=amount,
            tax_total=Decimal("0.00"),
            total=amount,
            balance_due=amount,
        ),
    )
    credit_note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=account_id,
            status=CreditNoteStatus.issued,
            currency="USD",
            subtotal=amount,
            tax_total=Decimal("0.00"),
            total=amount,
        ),
    )
    return credit_note, invoice


def test_credit_note_owner_persists_first_issuance_timestamp(
    db_session, subscriber_account
):
    draft = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.draft,
            currency="NGN",
        ),
    )
    assert draft.issued_at is None

    issued = billing_service.credit_notes.update(
        db_session,
        str(draft.id),
        CreditNoteUpdate(status=CreditNoteStatus.issued),
    )

    assert issued.issued_at is not None


def test_credit_form_rejects_non_positive_amounts(db_session, subscriber_account):
    with pytest.raises(ValueError, match="Amount must be greater than 0"):
        create_credit_from_form(
            db_session,
            account_id=str(subscriber_account.id),
            amount="0",
            currency="USD",
            memo=None,
        )

    with pytest.raises(ValueError, match="Amount must be greater than 0"):
        create_credit_from_form(
            db_session,
            account_id=str(subscriber_account.id),
            amount="-1",
            currency="USD",
            memo=None,
        )


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
    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
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
    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
    assert refreshed_credit_note.total == Decimal("50.00")
    assert refreshed_credit_note.applied_total == Decimal("50.00")
    assert refreshed_credit_note.status == CreditNoteStatus.applied


def test_apply_locks_credit_note_and_invoice_rows(db_session, subscriber_account):
    """Apply must SELECT ... FOR UPDATE both rows before the balance check, so
    concurrent applies can't each read a stale applied_total and over-spend the
    note. SQLite can't exercise the race (FOR UPDATE is a no-op), so we pin that
    the locks are acquired for the right rows."""
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    with patch(
        "app.services.billing.credit_notes.lock_for_update",
        wraps=_real_lock_for_update,
    ) as spy:
        billing_service.credit_notes.apply(
            db_session,
            str(credit_note.id),
            CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("40.00")),
        )
    locked_models = {call.args[1] for call in spy.call_args_list}
    assert CreditNote in locked_models
    assert Invoice in locked_models


def test_apply_cannot_exceed_credit_note_balance(db_session, subscriber_account):
    """Sequential over-application is rejected (the lock makes the same guard
    hold under concurrency): a fully-applied note has no remaining balance."""
    credit_note, invoice = _issued_credit_note_and_invoice(
        db_session, subscriber_account.id, amount=Decimal("100.00")
    )
    billing_service.credit_notes.apply(
        db_session,
        str(credit_note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("100.00")),
    )
    with pytest.raises(HTTPException) as exc:
        billing_service.credit_notes.apply(
            db_session,
            str(credit_note.id),
            CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("1.00")),
        )
    assert exc.value.status_code == 400


def test_credit_note_line_inclusive_tax_extracts_tax(
    db_session,
    subscriber_account,
):
    tax = billing_service.tax_rates.create(
        db_session,
        TaxRateCreate(name="Credit GST", rate=Decimal("10.0000")),
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
            description="Inclusive service credit",
            quantity=Decimal("1"),
            unit_price=Decimal("110.00"),
            tax_rate_id=tax.id,
            tax_application=TaxApplication.inclusive,
        ),
    )

    refreshed_credit_note = billing_service.credit_notes.get(
        db_session, str(credit_note.id)
    )
    assert refreshed_credit_note.subtotal == Decimal("100.00")
    assert refreshed_credit_note.tax_total == Decimal("10.00")
    assert refreshed_credit_note.total == Decimal("110.00")
