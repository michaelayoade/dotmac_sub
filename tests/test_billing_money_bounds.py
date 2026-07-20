"""Upper bounds on money/rate schema fields (#27 follow-up).

Payment/credit amounts are `Numeric(12,2)` (max 9,999,999,999.99) and tax rate
is `Numeric(6,4)` (max 99.9999). The create/update schemas enforced ge=0/gt=0
but no maximum, so a huge value passed validation then overflowed the column
at commit (DataError). They now also enforce an upper bound (`lt`) so a
too-large value is a clean ValidationError instead.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.billing import (
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteUpdate,
    InvoiceCreate,
    InvoiceLineCreate,
    LedgerEntryCreate,
    PaymentAllocationCreate,
    PaymentCreate,
    PaymentUpdate,
    TaxRateCreate,
    TaxRateUpdate,
)


def test_payment_accepts_large_but_in_range_amount():
    p = PaymentCreate(account_id=uuid4(), amount=Decimal("9999999999.99"))
    assert p.amount == Decimal("9999999999.99")


@pytest.mark.parametrize("amount", ["10000000000.00", "99999999999999", "0", "-1"])
def test_payment_rejects_out_of_range_amount(amount):
    with pytest.raises(ValidationError):
        PaymentCreate(account_id=uuid4(), amount=Decimal(amount))


def test_payment_update_rejects_huge_amount():
    with pytest.raises(ValidationError):
        PaymentUpdate(amount=Decimal("10000000000"))


@pytest.mark.parametrize("total", ["10000000000.00", "-1"])
def test_credit_note_rejects_out_of_range_total(total):
    with pytest.raises(ValidationError):
        CreditNoteCreate(account_id=uuid4(), total=Decimal(total))


def test_credit_note_update_rejects_huge_total():
    with pytest.raises(ValidationError):
        CreditNoteUpdate(total=Decimal("10000000000"))


def test_tax_rate_accepts_normal_rate():
    assert TaxRateCreate(name="VAT", rate=Decimal("7.5")).rate == Decimal("7.5")


@pytest.mark.parametrize("rate", ["100", "1000", "-1"])
def test_tax_rate_rejects_out_of_range(rate):
    with pytest.raises(ValidationError):
        TaxRateCreate(name="VAT", rate=Decimal(rate))


def test_tax_rate_update_rejects_out_of_range():
    with pytest.raises(ValidationError):
        TaxRateUpdate(rate=Decimal("100"))


# --- Refactor guard: money bounds moved off the shared *Base onto *Create -----
# (docs/audit-read-model-constraints.md). The matching *Read models serialize
# negatives — see test_invoice_read_negative_lines.py. These assert the bounds
# still reject bad *input* after the move.


def test_invoice_create_rejects_negative_total():
    InvoiceCreate(account_id=uuid4(), total=Decimal("10.00"))  # accepts valid
    with pytest.raises(ValidationError):
        InvoiceCreate(account_id=uuid4(), total=Decimal("-1"))


def test_invoice_line_create_rejects_negative_and_zero_qty():
    InvoiceLineCreate(invoice_id=uuid4(), description="ok", amount=Decimal("1"))
    with pytest.raises(ValidationError):
        InvoiceLineCreate(invoice_id=uuid4(), description="x", amount=Decimal("-1"))
    with pytest.raises(ValidationError):
        InvoiceLineCreate(invoice_id=uuid4(), description="x", quantity=Decimal("0"))


def test_credit_note_line_create_rejects_negative():
    CreditNoteLineCreate(credit_note_id=uuid4(), description="ok", amount=Decimal("1"))
    with pytest.raises(ValidationError):
        CreditNoteLineCreate(
            credit_note_id=uuid4(), description="x", amount=Decimal("-1")
        )


def test_payment_allocation_create_rejects_negative():
    PaymentAllocationCreate(payment_id=uuid4(), invoice_id=uuid4(), amount=Decimal("1"))
    with pytest.raises(ValidationError):
        PaymentAllocationCreate(
            payment_id=uuid4(), invoice_id=uuid4(), amount=Decimal("-1")
        )


def test_ledger_entry_create_rejects_negative():
    LedgerEntryCreate(account_id=uuid4(), entry_type="debit", amount=Decimal("1"))
    with pytest.raises(ValidationError):
        LedgerEntryCreate(account_id=uuid4(), entry_type="debit", amount=Decimal("-1"))
