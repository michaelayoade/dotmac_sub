from app.models.audit import AuditEvent
from app.models.gis import (
    CustomerLocationChangeRequestStatus,
    GeoLocation,
    GeoLocationType,
)
from app.models.subscriber import Address, AddressType, Subscriber, UserType
from app.services import (
    customer_location_requests as customer_location_requests_service,
)


def _create_subscriber_with_service_address(db_session):
    subscriber = Subscriber(
        first_name="Map",
        last_name="Customer",
        email="map-customer@example.com",
        user_type=UserType.customer,
        address_line1="12 Service Lane",
        city="Abuja",
        region="FCT",
    )
    db_session.add(subscriber)
    db_session.flush()

    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="Primary service",
        address_line1="12 Service Lane",
        city="Abuja",
        region="FCT",
        latitude=9.0501,
        longitude=7.4902,
        is_primary=True,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(subscriber)
    db_session.refresh(address)
    return subscriber, address


def test_submit_location_request_creates_pending_request_and_audit(db_session):
    subscriber, address = _create_subscriber_with_service_address(db_session)

    result = customer_location_requests_service.submit_request(
        db_session,
        subscriber_id=str(subscriber.id),
        latitude=9.0512,
        longitude=7.4923,
        customer_note="Pin is one compound away.",
        actor_id=str(subscriber.id),
        actor_name="Map Customer",
        submitted_from_ip="203.0.113.10",
    )

    assert result.status == CustomerLocationChangeRequestStatus.pending
    assert result.address_id == address.id
    assert result.current_latitude == address.latitude
    assert result.current_longitude == address.longitude

    audit_rows = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer_location_change_requested")
        .all()
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].entity_id == str(result.id)


def test_approve_location_request_updates_address_and_geo_location(db_session):
    subscriber, address = _create_subscriber_with_service_address(db_session)
    location_request = customer_location_requests_service.submit_request(
        db_session,
        subscriber_id=str(subscriber.id),
        latitude=9.0606,
        longitude=7.5005,
        customer_note="Correct side of the road.",
        actor_id=str(subscriber.id),
        actor_name="Map Customer",
        submitted_from_ip="203.0.113.11",
    )

    approved = customer_location_requests_service.approve_request(
        db_session,
        request_id=str(location_request.id),
        actor_id="admin-1",
        actor_name="Admin User",
        review_note="Confirmed against installation record.",
    )

    db_session.refresh(address)
    assert approved.status == CustomerLocationChangeRequestStatus.approved
    assert float(address.latitude) == 9.0606
    assert float(address.longitude) == 7.5005

    geo_location = (
        db_session.query(GeoLocation).filter(GeoLocation.address_id == address.id).one()
    )
    assert geo_location.location_type == GeoLocationType.address
    assert float(geo_location.latitude) == 9.0606
    assert float(geo_location.longitude) == 7.5005

    audit_rows = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer_location_change_approved")
        .all()
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].entity_id == str(location_request.id)


def test_cancel_location_request_marks_request_canceled(db_session):
    subscriber, _address = _create_subscriber_with_service_address(db_session)
    location_request = customer_location_requests_service.submit_request(
        db_session,
        subscriber_id=str(subscriber.id),
        latitude=9.0701,
        longitude=7.5101,
        customer_note=None,
        actor_id=str(subscriber.id),
        actor_name="Map Customer",
        submitted_from_ip="203.0.113.12",
    )

    canceled = customer_location_requests_service.cancel_request(
        db_session,
        request_id=str(location_request.id),
        subscriber_id=str(subscriber.id),
        actor_id=str(subscriber.id),
    )

    assert canceled.status == CustomerLocationChangeRequestStatus.canceled
    audit_rows = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "customer_location_change_canceled")
        .all()
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].entity_id == str(location_request.id)
