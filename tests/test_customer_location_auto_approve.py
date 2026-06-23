"""Pin auto-approval + address geocode-on-save.

Small pin nudges from an approved location auto-verify; first pins and large
moves stay in the manual queue. Self-service address edits back-fill the service
Address coordinates without overwriting an existing pin.
"""

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.gis import (
    CustomerLocationChangeRequestStatus,
    GeoArea,
    GeoAreaType,
    GeoLocation,
)
from app.models.subscriber import Address, AddressType, Subscriber, UserType
from app.services import customer_location_requests as svc


def _set_gis(db, key, value):
    db.add(
        DomainSetting(
            domain=SettingDomain.gis, key=key, value_text=value, is_active=True
        )
    )
    db.commit()


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


def test_rate_limit_blocks_second_small_move_in_window(db_session):
    # First small move auto-approves; a second small move within the window must
    # fall to manual review (bounds incremental hop-drift).
    subscriber, address = _subscriber(db_session, lat=9.06, lon=7.49)

    first = _submit(db_session, subscriber, 9.0605, 7.49)
    assert first.status == CustomerLocationChangeRequestStatus.approved

    second = _submit(db_session, subscriber, 9.0608, 7.49)  # another ~33 m hop
    assert second.status == CustomerLocationChangeRequestStatus.pending
    decision = (second.metadata_ or {})["auto_decision"]
    assert decision["approved"] is False
    assert decision["signals"]["recent_auto_approvals"] >= 1


def test_shadow_mode_records_would_approve_but_stays_pending(db_session):
    # Shadow on: evaluate + record the would-be decision, but never auto-approve.
    _set_gis(db_session, "location_auto_approve_shadow", "true")
    subscriber, address = _subscriber(db_session, lat=9.06, lon=7.49)
    result = _submit(db_session, subscriber, 9.0605, 7.49)

    assert result.status == CustomerLocationChangeRequestStatus.pending
    decision = (result.metadata_ or {})["auto_decision"]
    assert decision["shadow"] is True
    assert decision["would_approve"] is True
    assert decision["approved"] is False
    # The pin is NOT moved while shadowing.
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


def test_require_coverage_with_no_areas_still_auto_approves(db_session):
    # require_coverage on, but no coverage polygons configured -> the gate is
    # skipped (don't block customers where the map is just incomplete).
    _set_gis(db_session, "location_auto_require_coverage", "true")
    subscriber, _ = _subscriber(db_session, lat=9.06, lon=7.49)
    result = _submit(db_session, subscriber, 9.0605, 7.49)
    assert result.status == CustomerLocationChangeRequestStatus.approved


def test_require_coverage_blocks_when_outside_coverage(db_session):
    # require_coverage on AND a coverage area exists, but the pin isn't inside it
    # -> manual review.
    _set_gis(db_session, "location_auto_require_coverage", "true")
    db_session.add(
        GeoArea(name="Lagos coverage", area_type=GeoAreaType.coverage, is_active=True)
    )
    db_session.commit()
    subscriber, _ = _subscriber(db_session, lat=9.06, lon=7.49)
    result = _submit(db_session, subscriber, 9.0605, 7.49)
    assert result.status == CustomerLocationChangeRequestStatus.pending
    assert (result.metadata_ or {})["auto_decision"]["signals"]["in_coverage"] is False


def test_geocode_creates_service_address_when_none_exists(db_session, monkeypatch):
    subscriber = Subscriber(
        first_name="No",
        last_name="Address",
        email="no.address@example.com",
        user_type=UserType.customer,
        address_line1="9 New Road",
        city="Abuja",
        region="FCT",
        country_code="NG",
    )
    db_session.add(subscriber)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.geocoding.geocode_address",
        lambda db, data: {**data, "latitude": 9.05, "longitude": 7.48},
    )
    out = svc.geocode_service_address(db_session, subscriber)

    assert out is not None
    created = (
        db_session.query(Address).filter(Address.subscriber_id == subscriber.id).one()
    )
    assert abs(float(created.latitude) - 9.05) < 1e-6
    assert created.address_type == AddressType.service


def test_geocode_backoff_skips_repeat_attempt_within_window(db_session, monkeypatch):
    subscriber, _ = _subscriber(db_session, lat=None, lon=None)
    calls = {"n": 0}

    def _fake(db, data):
        calls["n"] += 1
        return {**data, "latitude": None, "longitude": None}  # never resolves

    monkeypatch.setattr("app.services.geocoding.geocode_address", _fake)

    assert svc.geocode_service_address(db_session, subscriber) is None
    assert calls["n"] == 1
    # A second save within the retry window must not hit the geocoder again.
    assert svc.geocode_service_address(db_session, subscriber) is None
    assert calls["n"] == 1
