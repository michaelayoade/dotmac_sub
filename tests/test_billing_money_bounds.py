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
    CreditNoteUpdate,
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
