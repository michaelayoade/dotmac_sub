"""Pin auto-approval + address geocode-on-save.

Small pin nudges from an approved location auto-verify; first pins and large
moves stay in the manual queue. Self-service address edits back-fill the service
Address coordinates without overwriting an existing pin.
"""

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.gis import CustomerLocationChangeRequestStatus, GeoLocation
from app.models.subscriber import Address, AddressType, Subscriber, UserType
from app.services import customer_location_requests as svc


def _subscriber(db, *, lat=None, lon=None):
    subscriber = Subscriber(
        first_name="Pin",
        last_name="User",
        email="pin.user@example.com",
        user_type=UserType.customer,
        address_line1="1 Test Street",
        city="Abuja",
        region="FCT",
        country_code="NG",
    )
    db.add(subscriber)
    db.flush()
    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="Primary service",
        address_line1="1 Test Street",
        city="Abuja",
        region="FCT",
        country_code="NG",
        is_primary=True,
        latitude=lat,
        longitude=lon,
    )
    db.add(address)
    db.commit()
    db.refresh(subscriber)
    db.refresh(address)
    return subscriber, address


def _submit(db, subscriber, lat, lon):
    return svc.submit_request(
        db,
        subscriber_id=str(subscriber.id),
        latitude=lat,
        longitude=lon,
        customer_note=None,
        actor_id=str(subscriber.id),
        actor_name="Pin User",
    )


def test_small_move_auto_approves_and_updates_address(db_session):
    subscriber, address = _subscriber(db_session, lat=9.06, lon=7.49)
    # ~55 m north — well within the default 250 m radius.
    result = _submit(db_session, subscriber, 9.0605, 7.49)

    assert result.status == CustomerLocationChangeRequestStatus.approved
    assert (result.metadata_ or {})["auto_decision"]["approved"] is True
    db_session.refresh(address)
    assert abs(float(address.latitude) - 9.0605) < 1e-6
    # The approved pin is mirrored onto a GeoLocation for the map.
    geo = (
        db_session.query(GeoLocation).filter(GeoLocation.address_id == address.id).one()
    )
    assert abs(float(geo.latitude) - 9.0605) < 1e-6


def test_large_move_stays_pending(db_session):
    subscriber, _ = _subscriber(db_session, lat=9.06, lon=7.49)
    # ~4.4 km away — over the radius, needs a human.
    result = _submit(db_session, subscriber, 9.10, 7.49)

    assert result.status == CustomerLocationChangeRequestStatus.pending
    decision = (result.metadata_ or {})["auto_decision"]
    assert decision["approved"] is False
    assert decision["signals"]["move_distance_m"] > 250


def test_first_pin_stays_pending(db_session):
    # No current coordinates -> no baseline -> manual review.
    subscriber, _ = _subscriber(db_session, lat=None, lon=None)
    result = _submit(db_session, subscriber, 9.07, 7.50)

    assert result.status == CustomerLocationChangeRequestStatus.pending
    assert (result.metadata_ or {})["auto_decision"]["signals"].get("first_pin") is True


def test_disabled_flag_keeps_small_move_pending(db_session):
    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="location_auto_approve_enabled",
            value_text="false",
            is_active=True,
        )
    )
    db_session.commit()
    subscriber, _ = _subscriber(db_session, lat=9.06, lon=7.49)
    result = _submit(db_session, subscriber, 9.0605, 7.49)

    assert result.status == CustomerLocationChangeRequestStatus.pending
    assert (result.metadata_ or {})["auto_decision"]["approved"] is False


def test_geocode_backfills_coordinates_when_missing(db_session, monkeypatch):
    subscriber, address = _subscriber(db_session, lat=None, lon=None)
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address",
        lambda db, data: {**data, "latitude": 9.11, "longitude": 7.41},
    )
    out = svc.geocode_service_address(db_session, subscriber)

    assert out is not None
    assert out["latitude"] == 9.11
    db_session.refresh(address)
    assert abs(float(address.latitude) - 9.11) < 1e-6
    assert abs(float(address.longitude) - 7.41) < 1e-6


def test_geocode_skips_when_pin_already_set(db_session, monkeypatch):
    subscriber, address = _subscriber(db_session, lat=9.06, lon=7.49)
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address",
        lambda db, data: {**data, "latitude": 1.0, "longitude": 1.0},
    )
    out = svc.geocode_service_address(db_session, subscriber)

    assert out is None  # existing pin preserved
    db_session.refresh(address)
    assert abs(float(address.latitude) - 9.06) < 1e-6


def test_geocode_force_overrides_existing_pin(db_session, monkeypatch):
    subscriber, address = _subscriber(db_session, lat=9.06, lon=7.49)
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address",
        lambda db, data: {**data, "latitude": 9.20, "longitude": 7.55},
    )
    out = svc.geocode_service_address(db_session, subscriber, force=True)

    assert out is not None
    db_session.refresh(address)
    assert abs(float(address.latitude) - 9.20) < 1e-6
