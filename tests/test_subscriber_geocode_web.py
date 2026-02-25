from __future__ import annotations

import uuid

from app.models.subscriber import Address, Subscriber
from app.services.web_subscriber_details import build_subscriber_detail_snapshot
from app.web.admin import subscribers as subscribers_web


def _create_subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Map",
        last_name="Target",
        email=f"map-{uuid.uuid4().hex}@example.com",
    )
    db_session.add(subscriber)
    db_session.commit()
    db_session.refresh(subscriber)
    return subscriber


def test_subscriber_detail_snapshot_includes_geocode_target_without_coordinates(db_session):
    subscriber = _create_subscriber(db_session)
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Sample Street",
        city="Lagos",
        region="LA",
        country_code="NG",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()

    snapshot = build_subscriber_detail_snapshot(db_session, subscriber, subscriber.id)

    assert snapshot["map_data"] is None
    assert snapshot["geocode_target"] is not None
    assert snapshot["geocode_target"]["id"] == str(address.id)
    assert snapshot["geocode_target"]["payload"]["address_line1"] == "123 Sample Street"


def test_subscriber_detail_snapshot_prefers_map_data_when_coordinates_exist(db_session):
    subscriber = _create_subscriber(db_session)
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="1 Geo Point",
        city="Lagos",
        region="LA",
        country_code="NG",
        latitude=6.5244,
        longitude=3.3792,
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()

    snapshot = build_subscriber_detail_snapshot(db_session, subscriber, subscriber.id)

    assert snapshot["map_data"] is not None
    assert snapshot["map_data"]["center"] == [6.5244, 3.3792]
    assert snapshot["geocode_target"] is None


def test_subscriber_geocode_endpoint_updates_coordinates(db_session):
    subscriber = _create_subscriber(db_session)
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="44 Plot Way",
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()

    response = subscribers_web.geocode_address(
        str(address.id),
        latitude=6.500001,
        longitude=3.300001,
        db=db_session,
    )

    db_session.refresh(address)
    assert response.status_code == 200
    assert round(float(address.latitude), 6) == 6.500001
    assert round(float(address.longitude), 6) == 3.300001
