"""An issued credit note reduces the receivable it names, at issue.

A credit note is a reduction of the receivable, not a customer deposit. Leaving
an issued note unapplied while the same account carries the invoice it credits
overstates AR: the obligation is already reduced, but the invoice keeps ageing
and keeps being dunned. Application used to be a separate manual step, so the
overstatement lasted until somebody remembered.

Issue and application are now one transaction — the note, its funding, the
application, the consumption evidence, the invoice recalculation, the audit
trail and the settlement consequence land together or not at all.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import (
    CreditNoteApplication,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.billing import (
    CreditNoteIssuePreviewRequest,
    CreditNoteIssueRequest,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance


def _invoice(db_session, account_id, total: str, **kwargs) -> Invoice:
    invoice = Invoice(
        account_id=account_id,
        status=kwargs.pop("status", InvoiceStatus.issued),
        currency=kwargs.pop("currency", "USD"),
        total=Decimal(total),
        balance_due=Decimal(kwargs.pop("balance_due", total)),
        **kwargs,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def _issue(db_session, account_id, total: str, *, invoice_id=None, key=None):
    request = CreditNoteIssuePreviewRequest(
        account_id=account_id,
        invoice_id=invoice_id,
        currency="USD",
        subtotal=Decimal(total),
        total=Decimal(total),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    preview = billing_service.credit_notes.preview_issue(db_session, request)
    confirmation = CreditNoteIssueRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key or uuid4().hex,
    )
    return preview, billing_service.credit_notes.issue_with_evidence(
        db_session, confirmation
    )


def _applications(db_session, credit_note_id) -> list[CreditNoteApplication]:
    return (
        db_session.query(CreditNoteApplication)
        .filter(CreditNoteApplication.credit_note_id == credit_note_id)
        .all()
    )


def test_exact_credit_settles_the_named_invoice_at_issue(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "75.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "75.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("75.00")
    assert preview.invoice_receivable_after == Decimal("0.00")
    assert preview.residual_credit == Decimal("0.00")
    assert preview.settles_invoice is True
    assert preview.access_consequence == "invoice_settled_by_credit_note"

    assert result.application is not None
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.status == InvoiceStatus.paid
    assert len(_applications(db_session, result.credit_note.id)) == 1


def test_partial_credit_reduces_the_receivable_and_leaves_it_open(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "100.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "40.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("40.00")
    assert preview.invoice_receivable_after == Decimal("60.00")
    assert preview.settles_invoice is False
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("60.00")
    assert invoice.status != InvoiceStatus.paid
    assert result.application.application.amount == Decimal("40.00")


def test_excess_credit_settles_the_invoice_and_keeps_the_residual_as_credit(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id, "30.00")
    before = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )

    preview, result = _issue(
        db_session, subscriber_account.id, "50.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("30.00")
    assert preview.residual_credit == Decimal("20.00")
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")
    assert result.credit_note.applied_total == Decimal("30.00")
    # Only the unapplied part survives on the account: the funding entry adds
    # the whole credit and the consumption debit removes what was applied.
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == before + Decimal("20.00")


def test_an_unlinked_note_names_no_invoice_and_applies_nothing(
    db_session, subscriber_account
):
    """Credit with no named receivable is held, and says so."""
    _invoice(db_session, subscriber_account.id, "90.00")
    before = get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    )

    preview, result = _issue(db_session, subscriber_account.id, "50.00")

    assert preview.application_amount == Decimal("0.00")
    assert preview.residual_credit == Decimal("50.00")
    assert result.application is None
    assert _applications(db_session, result.credit_note.id) == []
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == before + Decimal("50.00")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", InvoiceStatus.void),
        ("balance_due", "0.00"),
    ],
)
def test_an_invoice_that_cannot_absorb_credit_still_issues(
    db_session, subscriber_account, field, value
):
    """Ordinary states, not errors — issuing succeeds and the credit is held."""
    invoice = _invoice(db_session, subscriber_account.id, "40.00", **{field: value})

    preview, result = _issue(
        db_session, subscriber_account.id, "40.00", invoice_id=invoice.id
    )

    assert preview.application_amount == Decimal("0.00")
    assert result.application is None
    assert result.credit_note.status == CreditNoteStatus.issued


def test_replaying_the_issue_does_not_apply_twice(db_session, subscriber_account):
    """The application key is derived from the issue, so a replay is idempotent."""
    invoice = _invoice(db_session, subscriber_account.id, "60.00")
    key = uuid4().hex

    _, first = _issue(
        db_session, subscriber_account.id, "60.00", invoice_id=invoice.id, key=key
    )
    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=invoice.id,
        currency="USD",
        subtotal=Decimal("60.00"),
        total=Decimal("60.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    replay = billing_service.credit_notes.issue_with_evidence(
        db_session,
        CreditNoteIssueRequest(
            **request.model_dump(),
            preview_fingerprint=first.credit_note.issue_preview_fingerprint,
            idempotency_key=key,
        ),
    )

    assert replay.idempotent_replay is True
    assert replay.credit_note.id == first.credit_note.id
    assert len(_applications(db_session, first.credit_note.id)) == 1
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")


def test_a_failure_after_staging_rolls_back_the_whole_issue(
    db_session, subscriber_account
):
    """Atomicity is the point: no orphan note, no orphan funding, no half-apply."""
    invoice = _invoice(db_session, subscriber_account.id, "45.00")
    notes_before = db_session.query(LedgerEntry).count()
    applications_before = db_session.query(CreditNoteApplication).count()

    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=invoice.id,
        currency="USD",
        subtotal=Decimal("45.00"),
        total=Decimal("45.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    preview = billing_service.credit_notes.preview_issue(db_session, request)
    confirmation = CreditNoteIssueRequest(
        **request.model_dump(),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=uuid4().hex,
    )

    with (
        patch(
            "app.services.billing.credit_notes._stage_credit_audit",
            side_effect=RuntimeError("audit exploded"),
        ),
        pytest.raises(RuntimeError),
    ):
        billing_service.credit_notes.issue_with_evidence(db_session, confirmation)

    # The owner's rollback unwinds the session, so read the surviving state
    # fresh rather than through instances the rollback expired.
    db_session.rollback()
    assert db_session.query(CreditNoteApplication).count() == applications_before
    assert db_session.query(LedgerEntry).count() == notes_before
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.key == confirmation.idempotency_key)
        .count()
        == 0
    )


def test_manual_application_still_works_through_the_same_participant(
    db_session, subscriber_account
):
    """Both paths share one staging participant, so evidence cannot drift."""
    invoice = _invoice(db_session, subscriber_account.id, "80.00")
    target = _invoice(db_session, subscriber_account.id, "25.00")

    _, result = _issue(
        db_session, subscriber_account.id, "80.00", invoice_id=invoice.id
    )
    application = result.application.application

    assert application.ledger_entry_id is not None
    assert application.consumption_ledger_entry_id is not None
    consumption = db_session.get(LedgerEntry, application.consumption_ledger_entry_id)
    assert consumption.entry_type == LedgerEntryType.debit
    assert consumption.source == LedgerSource.credit_note
    assert consumption.invoice_id is None
    posting = db_session.get(LedgerEntry, application.ledger_entry_id)
    assert posting.invoice_id == invoice.id
    assert target.balance_due == Decimal("25.00")


def test_unrelated_negative_legacy_credit_history_does_not_leak_in(
    db_session, subscriber_account
):
    """A pre-existing debit on the account must not fund the application."""
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("500.00"),
            currency="USD",
            memo="legacy carried-in debit",
        )
    )
    db_session.commit()
    invoice = _invoice(db_session, subscriber_account.id, "35.00")

    preview, result = _issue(
        db_session, subscriber_account.id, "35.00", invoice_id=invoice.id
    )

    # Funding comes from this note's own funding entry, not the account's net
    # position, so unrelated history neither blocks nor inflates it.
    assert preview.application_amount == Decimal("35.00")
    assert result.application is not None
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")


def test_a_note_tied_to_another_invoice_is_refused_not_silently_held(
    db_session, subscriber_account
):
    """Incoherent requests still raise; only ordinary states are skipped."""
    other = _invoice(db_session, subscriber_account.id, "20.00", currency="NGN")

    request = CreditNoteIssuePreviewRequest(
        account_id=subscriber_account.id,
        invoice_id=other.id,
        currency="USD",
        subtotal=Decimal("20.00"),
        total=Decimal("20.00"),
        memo="Reviewed service credit",
        line_description="Reviewed service credit",
    )
    # Refused at preview, before any evidence exists — the currency mismatch is
    # incoherent whichever step notices it first.
    with pytest.raises(HTTPException):
        billing_service.credit_notes.preview_issue(db_session, request)


def test_a_reversible_workflow_can_hold_its_credit(db_session, subscriber_account):
    """`apply_on_issue=False` is a recorded decision, not an oversight.

    A credit note cannot be voided once applied and there is no un-apply path,
    so billing remediation — which rolls back by voiding the note it issued —
    must hold its credit rather than settle the invoice at issue.
    """
    invoice = _invoice(db_session, subscriber_account.id, "45.00")

    result = billing_service.credit_notes.issue_system(
        db_session,
        CreditNoteIssuePreviewRequest(
            account_id=subscriber_account.id,
            invoice_id=invoice.id,
            currency="USD",
            subtotal=Decimal("45.00"),
            total=Decimal("45.00"),
            memo="Billing-integrity correction",
            line_description="Billing-integrity correction",
        ),
        idempotency_key=uuid4().hex,
        commit=True,
        apply_on_issue=False,
    )

    assert result.application is None
    assert result.credit_note.applied_total == Decimal("0.00")
    assert result.preview.application_amount == Decimal("0.00")
    # Still voidable, which is the whole reason for the hold.
    assert result.credit_note.status == CreditNoteStatus.issued
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("45.00")
