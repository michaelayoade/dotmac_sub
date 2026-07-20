"""LGA is captured and validated against its state — never guessed.

The NCC quarterly complaints return files a Local Government Area per row, so
a wrong LGA is a wrong regulatory filing. CRM had no LGA field at all: it
guessed one from address text and defaulted the unmatched to "Municipal Area
Council, FEDERAL CAPITAL TERRITORY", turning customers it could not locate
into Abuja statistics. The column replaces that with capture, and these tests
pin the rules that keep it honest.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.schemas.subscriber import (
    AddressCreate,
    AddressUpdate,
    SubscriberCreate,
    SubscriberUpdate,
)
from app.services import subscriber as subscriber_service


def _create(db, **overrides):
    payload = {
        "first_name": "Ada",
        "last_name": "Obi",
        "email": f"s-{uuid.uuid4().hex[:8]}@example.com",
        **overrides,
    }
    return subscriber_service.Subscribers.create(db, SubscriberCreate(**payload))


# ── capture ─────────────────────────────────────────────────────────────────


def test_valid_lga_is_captured_and_canonicalised(db_session):
    """Stored in the reference table's spelling, so the return never has to
    re-interpret what was typed."""
    subscriber = _create(db_session, region="Lagos", lga="eti osa")
    assert subscriber.lga == "Eti-Osa"


def test_lga_from_another_state_is_rejected(db_session):
    """The check is per-state, not a global name lookup: Eti-Osa is a real
    LGA, just not one of Kano's."""
    with pytest.raises(HTTPException) as exc:
        _create(db_session, region="Kano", lga="Eti-Osa")
    assert exc.value.status_code == 422
    assert "not a Local Government Area" in str(exc.value.detail)


def test_unknown_lga_is_rejected(db_session):
    with pytest.raises(HTTPException) as exc:
        _create(db_session, region="Lagos", lga="Nowhere")
    assert exc.value.status_code == 422


def test_lga_without_a_state_is_rejected_not_silently_stored(db_session):
    """An LGA that cannot be validated is refused outright. Silently blanking
    it would discard what the user typed without telling them; storing it
    unvalidated is how a wrong LGA reaches the regulator."""
    with pytest.raises(HTTPException) as exc:
        _create(db_session, region=None, lga="Eti-Osa")
    assert exc.value.status_code == 422
    assert "without a state" in str(exc.value.detail)


def test_lga_is_optional(db_session):
    """Blank is the honest value for "we do not know" — the return reports the
    gap rather than inventing a location."""
    subscriber = _create(db_session, region="Lagos")
    assert subscriber.lga is None


# ── partial updates: validated against merged state ─────────────────────────


def test_update_validates_lga_against_the_stored_region(db_session):
    """The gate that a schema-only check would miss: the patch carries an LGA
    but not the region it must be valid for."""
    subscriber = _create(db_session, region="Kano")
    with pytest.raises(HTTPException) as exc:
        subscriber_service.Subscribers.update(
            db_session, str(subscriber.id), SubscriberUpdate(lga="Eti-Osa")
        )
    assert exc.value.status_code == 422


def test_update_accepts_an_lga_valid_for_the_stored_region(db_session):
    subscriber = _create(db_session, region="Lagos")
    updated = subscriber_service.Subscribers.update(
        db_session, str(subscriber.id), SubscriberUpdate(lga="Eti-Osa")
    )
    assert updated.lga == "Eti-Osa"


def test_region_and_lga_changing_together_validate_as_a_pair(db_session):
    subscriber = _create(db_session, region="Lagos", lga="Eti-Osa")
    updated = subscriber_service.Subscribers.update(
        db_session,
        str(subscriber.id),
        SubscriberUpdate(region="Kano", lga="Dala"),
    )
    assert updated.region == "Kano"
    assert updated.lga == "Dala"


def test_lga_can_be_cleared(db_session):
    subscriber = _create(db_session, region="Lagos", lga="Eti-Osa")
    updated = subscriber_service.Subscribers.update(
        db_session, str(subscriber.id), SubscriberUpdate(lga="")
    )
    assert updated.lga is None


# ── addresses carry it too ──────────────────────────────────────────────────


def test_address_lga_is_validated_on_create(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address", lambda _db, data: data
    )
    subscriber = _create(db_session)
    address = subscriber_service.addresses.create(
        db_session,
        AddressCreate(
            subscriber_id=subscriber.id,
            address_line1="1 Admiralty Way",
            region="Lagos",
            lga="eti-osa",
        ),
    )
    assert address.lga == "Eti-Osa"

    with pytest.raises(HTTPException):
        subscriber_service.addresses.create(
            db_session,
            AddressCreate(
                subscriber_id=subscriber.id,
                address_line1="2 Admiralty Way",
                region="Lagos",
                lga="Nowhere",
            ),
        )


def test_address_update_validates_against_the_stored_region(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address", lambda _db, data: data
    )
    subscriber = _create(db_session)
    address = subscriber_service.addresses.create(
        db_session,
        AddressCreate(
            subscriber_id=subscriber.id,
            address_line1="1 Admiralty Way",
            region="Kano",
        ),
    )
    with pytest.raises(HTTPException):
        subscriber_service.addresses.update(
            db_session, str(address.id), AddressUpdate(lga="Eti-Osa")
        )
