"""Regression: invoice read schemas must serialize negative (credit) money.

A real invoice can carry a negative line (a credit/adjustment/true-up). The
read/response models used to inherit the create-side ``ge=0`` constraint, so
``/api/v1/invoices`` 500'd with ResponseValidationError on any such invoice.
The read models now reflect stored data without the constraint.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.billing import (
    CreditNoteApplicationRead,
    CreditNoteLineRead,
    CreditNoteRead,
    InvoiceLineRead,
    InvoiceRead,
    LedgerEntryRead,
    PaymentAllocationRead,
    PaymentRead,
    TaxRateRead,
)
from app.schemas.usage import UsageChargeRead


def test_invoice_line_read_allows_negative_amounts():
    line = InvoiceLineRead.model_validate(
        {
            "id": uuid.uuid4(),
            "invoice_id": uuid.uuid4(),
            "description": "Opening-balance true-up (credit)",
            "quantity": Decimal("1.000"),
            "unit_price": Decimal("-16333.00"),
            "amount": Decimal("-16333.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert line.unit_price == Decimal("-16333.00")
    assert line.amount == Decimal("-16333.00")


def test_invoice_read_allows_negative_totals_and_lines():
    inv = InvoiceRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "subtotal": Decimal("-16333.00"),
            "tax_total": Decimal("0.00"),
            "total": Decimal("-16333.00"),
            "balance_due": Decimal("-16333.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "lines": [
                {
                    "id": uuid.uuid4(),
                    "invoice_id": uuid.uuid4(),
                    "description": "Credit",
                    "quantity": Decimal("1.000"),
                    "unit_price": Decimal("-16333.00"),
                    "amount": Decimal("-16333.00"),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            ],
        }
    )
    assert inv.total == Decimal("-16333.00")
    assert inv.lines[0].amount == Decimal("-16333.00")


def test_invoice_read_allows_negative_payment_allocation():
    """The original #272 fix missed nested payment_allocations — a negative
    (reversal/clawback) allocation still 500'd /api/v1/invoices for the whole
    page. This is the actual cause of the live invoices-list 500.
    """
    inv = InvoiceRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "total": Decimal("0.00"),
            "balance_due": Decimal("0.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "payment_allocations": [
                {
                    "id": uuid.uuid4(),
                    "payment_id": uuid.uuid4(),
                    "invoice_id": uuid.uuid4(),
                    "amount": Decimal("-2500.00"),  # reversal/clawback
                    "created_at": datetime.now(UTC),
                }
            ],
        }
    )
    assert inv.payment_allocations[0].amount == Decimal("-2500.00")


def test_payment_allocation_read_allows_negative_amount():
    alloc = PaymentAllocationRead.model_validate(
        {
            "id": uuid.uuid4(),
            "payment_id": uuid.uuid4(),
            "invoice_id": uuid.uuid4(),
            "amount": Decimal("-2500.00"),
            "created_at": datetime.now(UTC),
        }
    )
    assert alloc.amount == Decimal("-2500.00")


def test_credit_note_read_allows_negative_money():
    """Credit notes carry credit amounts; the read tree must serialize them
    (header totals, lines, and applications) — cause of the credit-notes 500.
    """
    cn = CreditNoteRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "subtotal": Decimal("-16333.00"),
            "tax_total": Decimal("0.00"),
            "total": Decimal("-16333.00"),
            "applied_total": Decimal("-16333.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "lines": [
                {
                    "id": uuid.uuid4(),
                    "credit_note_id": uuid.uuid4(),
                    "description": "Credit line",
                    "quantity": Decimal("1.000"),
                    "unit_price": Decimal("-16333.00"),
                    "amount": Decimal("-16333.00"),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            ],
            "applications": [
                {
                    "id": uuid.uuid4(),
                    "credit_note_id": uuid.uuid4(),
                    "invoice_id": uuid.uuid4(),
                    "amount": Decimal("-16333.00"),
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            ],
        }
    )
    assert cn.total == Decimal("-16333.00")
    assert cn.lines[0].amount == Decimal("-16333.00")
    assert cn.applications[0].amount == Decimal("-16333.00")


def test_credit_note_line_and_application_read_standalone():
    line = CreditNoteLineRead.model_validate(
        {
            "id": uuid.uuid4(),
            "credit_note_id": uuid.uuid4(),
            "description": "Credit",
            "quantity": Decimal("1.000"),
            "unit_price": Decimal("-100.00"),
            "amount": Decimal("-100.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert line.amount == Decimal("-100.00")

    app = CreditNoteApplicationRead.model_validate(
        {
            "id": uuid.uuid4(),
            "credit_note_id": uuid.uuid4(),
            "invoice_id": uuid.uuid4(),
            "amount": Decimal("-100.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert app.amount == Decimal("-100.00")


# --- Audit follow-up: the same #560 class on other served read models --------


def test_invoice_and_credit_note_line_read_allow_zero_quantity():
    """#560 fixed amount/unit_price but not quantity; a zero-qty flat true-up
    line would still 500 the list. Read models must allow it."""
    base = {
        "id": uuid.uuid4(),
        "description": "Flat true-up",
        "quantity": Decimal("0.000"),
        "unit_price": Decimal("0.00"),
        "amount": Decimal("-5000.00"),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    inv_line = InvoiceLineRead.model_validate({**base, "invoice_id": uuid.uuid4()})
    cn_line = CreditNoteLineRead.model_validate(
        {**base, "credit_note_id": uuid.uuid4()}
    )
    assert inv_line.quantity == Decimal("0.000")
    assert cn_line.quantity == Decimal("0.000")


def test_ledger_entry_read_allows_signed_amount():
    """Ledger entries are inherently signed (debit/credit/reversal)."""
    entry = LedgerEntryRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "entry_type": "credit",
            "amount": Decimal("-12500.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert entry.amount == Decimal("-12500.00")


def test_payment_read_allows_zero_and_negative_amount():
    """Refund/reversal/zero payments must serialize (was gt=0)."""
    for amt in (Decimal("0.00"), Decimal("-2500.00")):
        p = PaymentRead.model_validate(
            {
                "id": uuid.uuid4(),
                "account_id": uuid.uuid4(),
                "amount": amt,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        assert p.amount == amt


def test_tax_rate_read_allows_100_percent():
    """A 100% rate sits at the create-side lt=100 bound and must read back."""
    tr = TaxRateRead.model_validate(
        {
            "id": uuid.uuid4(),
            "name": "Full pass-through",
            "rate": Decimal("100.0000"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert tr.rate == Decimal("100.0000")


def test_usage_charge_read_allows_negative_money():
    """The live /usage-charges 500: usage credits/adjustments are negative."""
    uc = UsageChargeRead.model_validate(
        {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "subscription_id": uuid.uuid4(),
            "subscriber_id": uuid.uuid4(),
            "period_start": datetime.now(UTC),
            "period_end": datetime.now(UTC),
            "total_gb": Decimal("-1.0000"),
            "included_gb": Decimal("0.0000"),
            "billable_gb": Decimal("-1.0000"),
            "unit_price": Decimal("100.0000"),
            "amount": Decimal("-100.00"),
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    assert uc.amount == Decimal("-100.00")
    assert uc.billable_gb == Decimal("-1.0000")
