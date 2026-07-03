from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import crm as crm_routes
from app.models.billing import Invoice
from app.models.catalog import (
    AccessType,
    BillingCycle,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services import crm_api


@pytest.fixture(autouse=True)
def _ensure_sequence_table(db_session):
    # The SQLite test schema doesn't register document_sequences (its model isn't
    # imported before create_all); invoice-number generation needs it.
    from app.models.sequence import DocumentSequence

    DocumentSequence.__table__.create(db_session.get_bind(), checkfirst=True)


def _subscriber(db_session) -> Subscriber:
    sub = Subscriber(
        first_name="Ada",
        last_name="L",
        email=f"a-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _offer(db_session, *, code="HOME100", price="15000") -> CatalogOffer:
    offer = CatalogOffer(
        name="Home 100M",
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        is_active=True,
    )
    db_session.add(offer)
    db_session.commit()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=Decimal(price),
            currency="NGN",
        )
    )
    db_session.commit()
    return offer


def test_create_subscription_makes_sub_and_first_invoice(db_session):
    sub = _subscriber(db_session)
    offer = _offer(db_session)
    result = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=str(offer.id),
        external_ref="so-sub-1",
    )
    assert result["created"] is True
    assert result["subscription"].offer_id == offer.id
    # created pending — records the sale + first invoice for portal visibility;
    # network provisioning (active) stays sub's own job.
    assert result["subscription"].status == SubscriptionStatus.pending
    inv = result["invoice"]
    assert inv is not None
    assert (inv.metadata_ or {}).get("crm_external_ref") == "so-sub-1"
    assert (inv.metadata_ or {}).get("crm_subscription_id") == str(
        result["subscription"].id
    )
    # the invoice line is tagged to the subscription (subscription charge, not installation)
    assert any(line.subscription_id == result["subscription"].id for line in inv.lines)


def test_create_subscription_idempotent(db_session):
    sub = _subscriber(db_session)
    offer = _offer(db_session)
    r1 = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=offer.code,
        external_ref="so-sub-2",
    )
    r2 = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=offer.code,
        external_ref="so-sub-2",
    )
    assert r2["created"] is False
    assert r1["subscription"].id == r2["subscription"].id
    assert db_session.query(Invoice).filter(Invoice.account_id == sub.id).count() == 1


def test_create_subscription_resolves_offer_by_code(db_session):
    sub = _subscriber(db_session)
    _offer(db_session, code="BIZ500")
    result = crm_api.create_subscription(
        db_session, subscriber_id=str(sub.id), offer_ref="BIZ500", external_ref="so-3"
    )
    assert result["subscription"] is not None


def test_endpoint_guards(db_session):
    with pytest.raises(HTTPException) as exc:
        crm_routes.create_crm_subscription(payload={}, db=db_session)
    assert exc.value.status_code == 400


def test_endpoint_404_bad_offer(db_session):
    sub = _subscriber(db_session)
    with pytest.raises(HTTPException) as exc:
        crm_routes.create_crm_subscription(
            payload={
                "subscriber_id": str(sub.id),
                "offer_ref": "NOPE",
                "external_ref": "x",
            },
            db=db_session,
        )
    assert exc.value.status_code == 404
