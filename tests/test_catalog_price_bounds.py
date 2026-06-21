"""Offer/add-on price bounds (#27).

The price `amount` columns are `Numeric(10,2)` (max 99,999,999.99). The create
schemas enforced `gt=0` but had no upper bound, so a huge price overflowed the
column → DataError → 500. They now also enforce `lt=100_000_000` so a huge
price is a clean validation error instead.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.catalog import (
    AddOnPriceCreate,
    AddOnPriceUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
)


@pytest.mark.parametrize("amount", ["8000.00", "99999999.99", "1.00"])
def test_offer_price_accepts_valid_amounts(amount):
    price = OfferPriceCreate(offer_id=uuid4(), amount=Decimal(amount))
    assert price.amount == Decimal(amount)


@pytest.mark.parametrize("amount", ["100000000.00", "999999999999", "0", "-5"])
def test_offer_price_rejects_out_of_range(amount):
    with pytest.raises(ValidationError):
        OfferPriceCreate(offer_id=uuid4(), amount=Decimal(amount))


def test_offer_price_update_rejects_huge_amount():
    with pytest.raises(ValidationError):
        OfferPriceUpdate(amount=Decimal("100000000"))


@pytest.mark.parametrize("amount", ["100000000.00", "0", "-1"])
def test_add_on_price_rejects_out_of_range(amount):
    with pytest.raises(ValidationError):
        AddOnPriceCreate(add_on_id=uuid4(), amount=Decimal(amount))


def test_add_on_price_update_rejects_huge_amount():
    with pytest.raises(ValidationError):
        AddOnPriceUpdate(amount=Decimal("100000000"))
