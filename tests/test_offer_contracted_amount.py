"""A subscription's contracted amount: resolved, honestly absent, or refused.

Never zero. A zero reads downstream as a real contracted amount — it renders
as a genuine price in billing summaries — while prepaid enforcement treats
`unit_price` NULL *or <= 0* as "no contracted terms" and blocks the account.
So the zero satisfied nobody: it did not make the subscription billable, and
it hid why.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceBasis,
    PriceType,
    ServiceType,
)
from app.services.catalog.subscriptions import (
    OfferPricingNotConfigured,
    contracted_amount_for_offer,
)


def _offer(db_session, *, basis: PriceBasis, amount: str | None) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {basis.value} {amount}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=basis,
        plan_category=None,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.flush()
    if amount is not None:
        db_session.add(
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal(amount),
                is_active=True,
            )
        )
        db_session.flush()
    return offer


def test_active_recurring_price_is_returned(db_session):
    offer = _offer(db_session, basis=PriceBasis.flat, amount="15000.00")

    assert contracted_amount_for_offer(db_session, offer.id) == Decimal("15000.00")


def test_flat_offer_without_a_price_is_refused_not_zeroed(db_session):
    """The case that produced the rule: fail on misconfigured, never degrade."""
    offer = _offer(db_session, basis=PriceBasis.flat, amount=None)

    with pytest.raises(OfferPricingNotConfigured) as excinfo:
        contracted_amount_for_offer(db_session, offer.id)

    assert excinfo.value.code == "catalog.offer.recurring_price_required"
    assert excinfo.value.details["price_basis"] == "flat"


def test_usage_offer_without_a_recurring_price_is_absent_not_misconfigured(db_session):
    """A usage-billed offer is charged on consumption and has no such amount.

    Refusing here would break a legitimate catalog shape, which is why the
    rule distinguishes 'absent' from 'misconfigured' rather than failing on
    any empty lookup.
    """
    offer = _offer(db_session, basis=PriceBasis.usage, amount=None)

    assert contracted_amount_for_offer(db_session, offer.id) is None


def test_a_pinned_offer_version_price_wins(db_session):
    """Resolution must mirror what the subscription is actually invoiced at.

    `billing_automation._resolve_price` prefers a pinned version's price, so
    consulting only OfferPrice would report a correctly-priced versioned offer
    as unpriced — and, once creation refuses unpriced offers, would block it.
    """
    from app.models.catalog import OfferVersion, OfferVersionPrice

    offer = _offer(db_session, basis=PriceBasis.flat, amount="10000.00")
    version = OfferVersion(
        offer_id=offer.id,
        version_number=2,
        name="v2",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add(version)
    db_session.flush()
    db_session.add(
        OfferVersionPrice(
            offer_version_id=version.id,
            price_type=PriceType.recurring,
            amount=Decimal("22000.00"),
            is_active=True,
        )
    )
    db_session.flush()

    resolved = contracted_amount_for_offer(
        db_session, offer.id, offer_version_id=version.id
    )

    assert resolved == Decimal("22000.00")
