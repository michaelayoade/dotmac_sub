from __future__ import annotations

from decimal import Decimal

from app.api import crm as crm_routes
from app.models.catalog import (
    AccessType,
    BillingCycle,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
)
from app.services import crm_api


def _offer(db_session, *, name="Home 100M", code="HOME100", active=True, price="15000"):
    offer = CatalogOffer(
        name=name,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        speed_download_mbps=100,
        speed_upload_mbps=50,
        is_active=active,
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


def test_list_offers_returns_recurring_price(db_session):
    offer = _offer(db_session)
    rows = crm_api.list_catalog_offers(db_session)
    row = next(r for r in rows if r["id"] == str(offer.id))
    assert row["code"] == "HOME100"
    assert row["recurring_price"] == "15000.00"
    assert row["billing_cycle"] == "monthly"
    assert row["speed_download_mbps"] == 100


def test_list_offers_active_only(db_session):
    _offer(db_session, name="Old", code="OLD", active=False)
    codes = {
        r["code"] for r in crm_api.list_catalog_offers(db_session, active_only=True)
    }
    assert "OLD" not in codes


def test_offers_endpoint_envelope(db_session):
    _offer(db_session, code="X1")
    resp = crm_routes.catalog_offers(q=None, active_only=True, db=db_session)
    assert isinstance(resp["data"], list)
    assert any(o["code"] == "X1" for o in resp["data"])
