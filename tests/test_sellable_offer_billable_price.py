"""An offer on sale must have something to charge for it.

Production carries offers that are active, available for sale and priced at
N0.00 — the configuration behind migration 489's incident, where two
subscriptions were created against a free duplicate of "25 Mbps Fiber" in a
single day. That migration adjudicated the two rows it knew about; it did not
stop the next one being made.

Zero is not a discount. It reads as a real product to every picker, and nothing
downstream treats it as an error: the recurring run skips a zero-amount line and
advances the billing anchor as though it had charged.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
)
from app.services.web_catalog_offers import (
    SellableOfferWithoutBillablePrice,
    assert_sellable_offer_has_a_billable_price,
)


def _offer(db_session, *, sellable=True):
    offer = CatalogOffer(
        name="475 Mbps Fiber",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        is_active=True,
        available_for_services=sellable,
    )
    db_session.add(offer)
    db_session.flush()
    return offer


def _price(db_session, offer, amount, *, active=True, kind=PriceType.recurring):
    price = OfferPrice(
        offer_id=offer.id,
        price_type=kind,
        amount=Decimal(amount),
        currency="NGN",
        is_active=active,
    )
    db_session.add(price)
    db_session.flush()
    return price


def _assert(db_session, offer, *, sellable=True, incoming=None):
    db_session.refresh(offer)
    assert_sellable_offer_has_a_billable_price(
        offer,
        available_for_services=sellable,
        incoming_price_amount=incoming,
    )


def test_a_sellable_offer_priced_at_zero_is_refused(db_session):
    offer = _offer(db_session)
    _price(db_session, offer, "0.00")
    with pytest.raises(SellableOfferWithoutBillablePrice) as caught:
        _assert(db_session, offer)
    assert caught.value.code == "catalog.offer.sellable_without_billable_price"


def test_a_sellable_offer_with_no_recurring_price_at_all_is_refused(db_session):
    offer = _offer(db_session)
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer)


def test_a_sellable_offer_with_a_real_price_passes(db_session):
    offer = _offer(db_session)
    _price(db_session, offer, "537500.00")
    _assert(db_session, offer)


def test_a_withdrawn_offer_may_keep_its_zero_price_and_history(db_session):
    """The rule is about offering for sale, not about the row existing."""
    offer = _offer(db_session, sellable=False)
    _price(db_session, offer, "0.00")
    _assert(db_session, offer, sellable=False)


def test_raising_a_zero_priced_offer_in_the_same_submission_is_allowed(db_session):
    """Without this the operator is refused for the very state they are fixing."""
    offer = _offer(db_session)
    _price(db_session, offer, "0.00")
    _assert(db_session, offer, incoming="537500.00")


def test_an_incoming_zero_does_not_rescue_a_zero_priced_offer(db_session):
    offer = _offer(db_session)
    _price(db_session, offer, "0.00")
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer, incoming="0.00")


def test_a_malformed_incoming_amount_falls_through_to_stored_state(db_session):
    """Parsing is the price validator's job; this guard must not guess."""
    offer = _offer(db_session)
    _price(db_session, offer, "0.00")
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer, incoming="not-a-number")


def test_an_inactive_price_does_not_count(db_session):
    offer = _offer(db_session)
    _price(db_session, offer, "537500.00", active=False)
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer)


def test_a_one_time_price_does_not_make_an_offer_sellable(db_session):
    """An install fee is not a recurring price."""
    offer = _offer(db_session)
    _price(db_session, offer, "50000.00", kind=PriceType.one_time)
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer)


def test_two_active_recurring_prices_are_refused_as_ambiguous(db_session):
    """Matches the portal's existing exactly-one rule; two is not a price."""
    offer = _offer(db_session)
    _price(db_session, offer, "537500.00")
    _price(db_session, offer, "430000.00")
    with pytest.raises(SellableOfferWithoutBillablePrice):
        _assert(db_session, offer)


def test_an_offer_that_does_not_exist_yet_is_not_judged(db_session):
    """Prices hang off the offer; the update path re-checks once it exists."""
    assert_sellable_offer_has_a_billable_price(None, available_for_services=True)
