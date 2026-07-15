from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.schemas.billing import (
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteUpdate,
    InvoiceCreate,
)
from app.services import billing as billing_service
from app.services.billing._common import get_account_credit_balance
from app.services.customer_financial_ledger import (
    calculate_customer_balance,
    customer_financial_balances_by_currency,
)
from scripts.one_off.billing_alignment_audit import (
    _batch_customer_positions,
    _batch_ledger_credit,
)


def _issue(db, account_id, amount: str = "100.00") -> CreditNote:
    value = Decimal(amount)
    return billing_service.credit_notes.create(
        db,
        CreditNoteCreate(
            account_id=account_id,
            status=CreditNoteStatus.issued,
            currency="USD",
            subtotal=value,
            total=value,
        ),
    )


def _invoice(db, account_id, amount: str = "100.00"):
    value = Decimal(amount)
    return billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            status=InvoiceStatus.issued,
            currency="USD",
            subtotal=value,
            total=value,
            balance_due=value,
        ),
    )


def test_issuance_posts_one_structurally_linked_spendable_credit(
    db_session, subscriber_account
):
    note = _issue(db_session, subscriber_account.id)

    entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.credit_note_id == note.id)
        .all()
    )
    assert len(entries) == 1
    assert entries[0].credit_note_application_id is None
    assert entries[0].entry_type == LedgerEntryType.credit
    assert entries[0].invoice_id is None
    assert entries[0].amount == Decimal("100.00")
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == Decimal("100.00")


def test_zero_value_draft_cannot_be_issued(db_session, subscriber_account):
    note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.draft,
            currency="USD",
        ),
    )

    with pytest.raises(HTTPException) as exc:
        billing_service.credit_notes.issue(db_session, str(note.id))

    assert exc.value.status_code == 400
    assert (
        billing_service.credit_notes.get(db_session, str(note.id)).status
        == CreditNoteStatus.draft
    )


def test_owner_can_stage_draft_line_and_issuance_in_callers_transaction(
    db_session, subscriber_account
):
    note = billing_service.credit_notes.create(
        db_session,
        CreditNoteCreate(
            account_id=subscriber_account.id,
            status=CreditNoteStatus.draft,
            currency="USD",
        ),
        commit=False,
    )
    billing_service.credit_note_lines.create(
        db_session,
        CreditNoteLineCreate(
            credit_note_id=note.id,
            description="Atomic caller credit",
            unit_price=Decimal("25.00"),
        ),
        commit=False,
    )
    billing_service.credit_notes.issue(db_session, str(note.id), commit=False)
    note_id = note.id

    db_session.rollback()

    assert db_session.get(CreditNote, note_id) is None
    assert (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.credit_note_id == note_id)
        .count()
        == 0
    )


def test_database_refuses_a_second_issuance_posting(db_session, subscriber_account):
    note = _issue(db_session, subscriber_account.id)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            credit_note_id=note.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.credit_note,
            amount=Decimal("100.00"),
            currency="USD",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_application_moves_credit_to_invoice_without_changing_customer_position(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id)
    note = _issue(db_session, subscriber_account.id)

    application = billing_service.credit_notes.apply(
        db_session,
        str(note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("30.00")),
    )

    application_entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.credit_note_application_id == application.id)
        .all()
    )
    assert {(entry.entry_type, entry.invoice_id) for entry in application_entries} == {
        (LedgerEntryType.debit, None),
        (LedgerEntryType.credit, invoice.id),
    }
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == Decimal("70.00")
    assert billing_service.invoices.get(
        db_session, str(invoice.id)
    ).balance_due == Decimal("70.00")
    assert calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    ) == Decimal("0.00")
    assert customer_financial_balances_by_currency(db_session, [subscriber_account.id])[
        subscriber_account.id
    ]["USD"] == Decimal("0.00")
    assert _batch_customer_positions(
        db_session, [subscriber_account.id], currency="USD"
    )[(str(subscriber_account.id), "USD")] == Decimal("0.00")
    assert _batch_ledger_credit(db_session, [subscriber_account.id], currency="USD")[
        str(subscriber_account.id)
    ] == Decimal("70.00")


def test_database_refuses_a_duplicate_application_side(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account.id)
    note = _issue(db_session, subscriber_account.id)
    application = billing_service.credit_notes.apply(
        db_session,
        str(note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("30.00")),
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            credit_note_id=note.id,
            credit_note_application_id=application.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.credit_note,
            amount=Decimal("30.00"),
            currency="USD",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_note_cannot_be_double_spent_after_wallet_consumption(
    db_session, subscriber_account
):
    invoice = _invoice(db_session, subscriber_account.id)
    note = _issue(db_session, subscriber_account.id)
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            amount=Decimal("80.00"),
            currency="USD",
            memo="Prior wallet spend",
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        billing_service.credit_notes.apply(
            db_session,
            str(note.id),
            CreditNoteApplyRequest(invoice_id=invoice.id, amount=Decimal("30.00")),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Amount exceeds spendable account credit"
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == Decimal("20.00")

    application = billing_service.credit_notes.apply(
        db_session,
        str(note.id),
        CreditNoteApplyRequest(invoice_id=invoice.id),
    )
    assert application.amount == Decimal("20.00")
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == Decimal("0.00")


def test_void_reverses_issuance_and_removes_spendable_credit(
    db_session, subscriber_account
):
    note = _issue(db_session, subscriber_account.id)

    voided = billing_service.credit_notes.void(db_session, str(note.id))

    entries = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.credit_note_id == note.id)
        .order_by(LedgerEntry.created_at.asc())
        .all()
    )
    assert voided.status == CreditNoteStatus.void
    assert len(entries) == 2
    assert entries[1].reversal_of_entry_id == entries[0].id
    assert entries[1].entry_type == LedgerEntryType.debit
    assert get_account_credit_balance(
        db_session, str(subscriber_account.id), currency="USD"
    ) == Decimal("0.00")
    assert calculate_customer_balance(
        db_session, subscriber_account.id, currency="USD"
    ) == Decimal("0.00")


def test_issued_note_and_lines_are_immutable(db_session, subscriber_account):
    note = _issue(db_session, subscriber_account.id)

    with pytest.raises(HTTPException) as exc:
        billing_service.credit_notes.update(
            db_session,
            str(note.id),
            CreditNoteUpdate(total=Decimal("200.00")),
        )

    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as line_exc:
        billing_service.credit_note_lines.create(
            db_session,
            CreditNoteLineCreate(
                credit_note_id=note.id,
                description="Late mutation",
                unit_price=Decimal("1.00"),
            ),
        )
    assert line_exc.value.status_code == 409


def test_unposted_historical_note_fails_closed(db_session, subscriber_account):
    invoice = _invoice(db_session, subscriber_account.id)
    note = CreditNote(
        account_id=subscriber_account.id,
        status=CreditNoteStatus.issued,
        currency="USD",
        subtotal=Decimal("50.00"),
        total=Decimal("50.00"),
    )
    db_session.add(note)
    db_session.commit()

    with pytest.raises(HTTPException) as apply_exc:
        billing_service.credit_notes.apply(
            db_session,
            str(note.id),
            CreditNoteApplyRequest(invoice_id=invoice.id),
        )
    assert apply_exc.value.status_code == 409
    assert "reconcile" in apply_exc.value.detail

    with pytest.raises(HTTPException) as void_exc:
        billing_service.credit_notes.void(db_session, str(note.id))
    assert void_exc.value.status_code == 409
    assert "reconcile" in void_exc.value.detail
