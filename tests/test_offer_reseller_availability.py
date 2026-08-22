from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import select
from starlette.datastructures import FormData

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
)
from app.models.offer_availability import OfferResellerAvailability
from app.models.subscriber import Reseller
from app.services.offer_reseller_availability import (
    RESELLER_OFFER_AVAILABILITY_SCOPE,
    SetResellerOfferAvailabilityCommand,
    set_reseller_offer_availability,
)
from app.services.owner_commands import CommandContext
from app.web.admin import resellers as admin_resellers


def _offer(db_session, name: str) -> CatalogOffer:
    offer = CatalogOffer(
        name=name,
        code=name.lower().replace(" ", "-"),
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_family="unlimited",
        status=OfferStatus.active,
        is_active=True,
        show_on_customer_portal=True,
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


def _command(reseller_id, offer_ids) -> SetResellerOfferAvailabilityCommand:
    command_id = uuid4()
    return SetResellerOfferAvailabilityCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"user:{uuid4()}",
            scope=RESELLER_OFFER_AVAILABILITY_SCOPE,
            reason="Test reseller catalog access",
            idempotency_key=f"test:{command_id}",
        ),
        reseller_id=reseller_id,
        offer_ids=tuple(offer_ids),
    )


def _assignments(db_session, reseller_id):
    return list(
        db_session.scalars(
            select(OfferResellerAvailability)
            .where(OfferResellerAvailability.reseller_id == reseller_id)
            .order_by(OfferResellerAvailability.offer_id)
        ).all()
    )


def test_assignment_replace_adds_and_deactivates_without_deleting(db_session):
    reseller = Reseller(name="Catalog partner")
    db_session.add(reseller)
    db_session.commit()
    first = _offer(db_session, "First Partner Plan")
    second = _offer(db_session, "Second Partner Plan")

    added = set_reseller_offer_availability(
        db_session,
        _command(reseller.id, (first.id, second.id)),
    )
    assert set(added.added_offer_ids) == {first.id, second.id}
    assert all(row.is_active for row in _assignments(db_session, reseller.id))
    db_session.rollback()

    removed = set_reseller_offer_availability(
        db_session,
        _command(reseller.id, (second.id,)),
    )
    assert removed.deactivated_offer_ids == (first.id,)
    rows = {row.offer_id: row for row in _assignments(db_session, reseller.id)}
    assert rows[first.id].is_active is False
    assert rows[second.id].is_active is True
    assert len(rows) == 2


def test_assignment_replace_reactivates_existing_inactive_row(db_session):
    reseller = Reseller(name="Returning catalog partner")
    db_session.add(reseller)
    db_session.commit()
    offer = _offer(db_session, "Returning Partner Plan")
    original = OfferResellerAvailability(
        offer_id=offer.id,
        reseller_id=reseller.id,
        is_active=False,
    )
    db_session.add(original)
    db_session.commit()
    original_id = original.id

    outcome = set_reseller_offer_availability(
        db_session,
        _command(reseller.id, (offer.id,)),
    )

    assert outcome.added_offer_ids == ()
    assert outcome.reactivated_offer_ids == (offer.id,)
    rows = _assignments(db_session, reseller.id)
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].is_active is True


def test_admin_route_delegates_complete_offer_selection(db_session, monkeypatch):
    reseller_id = uuid4()
    offer_ids = (uuid4(), uuid4())
    captured = []
    monkeypatch.setattr(
        admin_resellers.offer_reseller_availability,
        "set_reseller_offer_availability",
        lambda db, command: (
            captured.append(command)
            or SimpleNamespace(
                changed=True,
                added_offer_ids=offer_ids,
                reactivated_offer_ids=(),
                deactivated_offer_ids=(),
            )
        ),
    )

    response = admin_resellers.reseller_catalog_access_update(
        str(reseller_id),
        Mock(),
        FormData([("offer_ids", str(value)) for value in offer_ids]),
        {"principal_id": str(uuid4()), "principal_type": "user"},
        db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("#catalog-access")
    assert captured[0].reseller_id == reseller_id
    assert set(captured[0].offer_ids) == set(offer_ids)
    assert captured[0].context.scope == RESELLER_OFFER_AVAILABILITY_SCOPE
