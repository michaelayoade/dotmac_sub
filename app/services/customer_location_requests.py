from __future__ import annotations

from datetime import UTC, datetime
from math import isclose
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.gis import (
    CustomerLocationChangeRequest,
    CustomerLocationChangeRequestStatus,
    GeoLocation,
    GeoLocationType,
)
from app.models.subscriber import Address, AddressType, Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services import gis as gis_service

DEFAULT_MAP_CENTER = [9.06, 7.49]


def _validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    lat = float(latitude)
    lon = float(longitude)
    if not (-90.0 <= lat <= 90.0):
        raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
    if not (-180.0 <= lon <= 180.0):
        raise HTTPException(
            status_code=400, detail="Longitude must be between -180 and 180"
        )
    return lat, lon


def _address_summary(address: Address | None, subscriber: Subscriber | None) -> dict[str, Any]:
    if address:
        return {
            "address_id": str(address.id),
            "label": address.label,
            "address_type": address.address_type.value if address.address_type else None,
            "address_line1": address.address_line1,
            "address_line2": address.address_line2,
            "city": address.city,
            "region": address.region,
            "postal_code": address.postal_code,
            "country_code": address.country_code,
        }
    if subscriber:
        return {
            "address_id": None,
            "label": "Subscriber contact address",
            "address_type": None,
            "address_line1": subscriber.address_line1,
            "address_line2": subscriber.address_line2,
            "city": subscriber.city,
            "region": subscriber.region,
            "postal_code": subscriber.postal_code,
            "country_code": subscriber.country_code,
        }
    return {
        "address_id": None,
        "label": None,
        "address_type": None,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "region": None,
        "postal_code": None,
        "country_code": None,
    }


def _address_label(address: Address | None, subscriber: Subscriber | None) -> str:
    summary = _address_summary(address, subscriber)
    parts = [
        summary.get("label"),
        summary.get("address_line1"),
        summary.get("city"),
        summary.get("region"),
    ]
    return ", ".join(str(part) for part in parts if part) or "Service location"


def _resolve_service_address(db: Session, subscriber_id: str) -> Address | None:
    active_subscription = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscription.service_address_id.isnot(None))
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if active_subscription and active_subscription.service_address:
        return active_subscription.service_address

    address = (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .filter(Address.address_type == AddressType.service)
        .filter(Address.is_primary.is_(True))
        .first()
    )
    if address:
        return address

    address = (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .filter(Address.is_primary.is_(True))
        .order_by(Address.created_at.asc())
        .first()
    )
    if address:
        return address

    return (
        db.query(Address)
        .filter(Address.subscriber_id == subscriber_id)
        .order_by(Address.created_at.asc())
        .first()
    )


def _ensure_target_address(
    db: Session,
    subscriber: Subscriber,
    location_request: CustomerLocationChangeRequest,
) -> Address:
    address = None
    if location_request.address_id:
        address = db.get(Address, location_request.address_id)
    if address is None:
        address = _resolve_service_address(db, str(subscriber.id))
    if address is not None:
        return address

    if not (subscriber.address_line1 or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Subscriber has no address record to attach the approved map pin to",
        )

    address = Address(
        subscriber_id=subscriber.id,
        address_type=AddressType.service,
        label="Primary service",
        address_line1=subscriber.address_line1,
        address_line2=subscriber.address_line2,
        city=subscriber.city,
        region=subscriber.region,
        postal_code=subscriber.postal_code,
        country_code=subscriber.country_code,
        is_primary=True,
    )
    db.add(address)
    db.flush()
    return address


def _upsert_geo_location_for_address(db: Session, address: Address) -> None:
    if address.latitude is None or address.longitude is None:
        return
    existing = (
        db.query(GeoLocation).filter(GeoLocation.address_id == address.id).first()
    )
    name = _address_label(address, None)
    if existing:
        existing.name = name
        existing.location_type = GeoLocationType.address
        existing.latitude = float(address.latitude)
        existing.longitude = float(address.longitude)
        existing.is_active = True
        gis_service._sync_location_geometry(existing)
        return
    geo_location = GeoLocation(
        name=name,
        location_type=GeoLocationType.address,
        latitude=float(address.latitude),
        longitude=float(address.longitude),
        address_id=address.id,
        is_active=True,
    )
    gis_service._sync_location_geometry(geo_location)
    db.add(geo_location)


def _audit(
    db: Session,
    *,
    actor_id: str | None,
    action: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
    actor_type: AuditActorType = AuditActorType.user,
) -> AuditEvent | None:
    return audit_service.audit_events.record(
        db,
        AuditEventCreate(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type="customer_location_change_request",
            entity_id=entity_id,
            status_code=200,
            is_success=True,
            metadata_=metadata,
        ),
        defer_until_commit=False,
    )


def _request_summary(item: CustomerLocationChangeRequest) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "status": item.status.value,
        "requested_latitude": item.requested_latitude,
        "requested_longitude": item.requested_longitude,
        "current_latitude": item.current_latitude,
        "current_longitude": item.current_longitude,
        "customer_note": item.customer_note,
        "review_note": item.review_note,
        "reviewed_by_actor_id": item.reviewed_by_actor_id,
        "reviewed_by_actor_name": item.reviewed_by_actor_name,
    }


def get_customer_location_page_context(db: Session, customer: dict) -> dict[str, Any]:
    subscriber_id = customer.get("subscriber_id")
    if not subscriber_id:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _resolve_service_address(db, str(subscriber.id))
    requests = (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == subscriber.id)
        .order_by(CustomerLocationChangeRequest.created_at.desc())
        .limit(10)
        .all()
    )
    pending_request = next(
        (
            item
            for item in requests
            if item.status == CustomerLocationChangeRequestStatus.pending
        ),
        None,
    )

    current_latitude = float(address.latitude) if address and address.latitude is not None else None
    current_longitude = float(address.longitude) if address and address.longitude is not None else None
    draft_latitude = (
        float(pending_request.requested_latitude)
        if pending_request
        else (float(current_latitude) if current_latitude is not None else None)
    )
    draft_longitude = (
        float(pending_request.requested_longitude)
        if pending_request
        else (float(current_longitude) if current_longitude is not None else None)
    )
    center = (
        [draft_latitude, draft_longitude]
        if draft_latitude is not None and draft_longitude is not None
        else DEFAULT_MAP_CENTER
    )
    has_address_anchor = bool(
        address or (subscriber.address_line1 or "").strip()
    )

    return {
        "location_address": _address_summary(address, subscriber),
        "location_address_label": _address_label(address, subscriber),
        "current_latitude": current_latitude,
        "current_longitude": current_longitude,
        "draft_latitude": draft_latitude,
        "draft_longitude": draft_longitude,
        "map_center": center,
        "pending_request": pending_request,
        "request_history": requests,
        "can_submit_request": pending_request is None and has_address_anchor,
        "has_address_anchor": has_address_anchor,
    }


def submit_request(
    db: Session,
    *,
    subscriber_id: str,
    latitude: float,
    longitude: float,
    customer_note: str | None,
    actor_id: str | None,
    actor_name: str | None,
    submitted_from_ip: str | None = None,
) -> CustomerLocationChangeRequest:
    lat, lon = _validate_coordinates(latitude, longitude)

    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _resolve_service_address(db, subscriber_id)
    if address is None and not (subscriber.address_line1 or "").strip():
        raise HTTPException(
            status_code=400,
            detail="No service address is available for this account yet",
        )
    existing_pending = (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == subscriber.id)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .order_by(CustomerLocationChangeRequest.created_at.desc())
        .first()
    )
    if existing_pending:
        raise HTTPException(
            status_code=400,
            detail="You already have a pending location correction awaiting review",
        )

    current_latitude = float(address.latitude) if address and address.latitude is not None else None
    current_longitude = float(address.longitude) if address and address.longitude is not None else None
    if (
        current_latitude is not None
        and current_longitude is not None
        and isclose(current_latitude, lat, abs_tol=1e-7)
        and isclose(current_longitude, lon, abs_tol=1e-7)
    ):
        raise HTTPException(
            status_code=400,
            detail="The new pin matches the current approved location",
        )

    location_request = CustomerLocationChangeRequest(
        subscriber_id=subscriber.id,
        address_id=address.id if address else None,
        status=CustomerLocationChangeRequestStatus.pending,
        current_latitude=current_latitude,
        current_longitude=current_longitude,
        requested_latitude=lat,
        requested_longitude=lon,
        customer_note=(customer_note or "").strip() or None,
        submitted_from_ip=submitted_from_ip,
        metadata_={
            "submitted_by_name": actor_name,
            "address_snapshot": _address_summary(address, subscriber),
        },
    )
    db.add(location_request)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        action="customer_location_change_requested",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(subscriber.id),
            "requested_latitude": lat,
            "requested_longitude": lon,
            "current_latitude": current_latitude,
            "current_longitude": current_longitude,
            "customer_note": location_request.customer_note,
        },
    )
    return location_request


def cancel_request(
    db: Session,
    *,
    request_id: str,
    subscriber_id: str,
    actor_id: str | None,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request or str(location_request.subscriber_id) != str(subscriber_id):
        raise HTTPException(status_code=404, detail="Location correction request not found")
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Only pending requests can be canceled")

    location_request.status = CustomerLocationChangeRequestStatus.cancelled
    location_request.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        action="customer_location_change_canceled",
        entity_id=str(location_request.id),
        metadata={"subscriber_id": subscriber_id},
    )
    return location_request


def list_requests(
    db: Session,
    *,
    status: CustomerLocationChangeRequestStatus | None = None,
    limit: int = 100,
) -> list[CustomerLocationChangeRequest]:
    query = db.query(CustomerLocationChangeRequest)
    if status is not None:
        query = query.filter(CustomerLocationChangeRequest.status == status)
    return (
        query.order_by(
            CustomerLocationChangeRequest.created_at.desc(),
            CustomerLocationChangeRequest.updated_at.desc(),
        )
        .limit(limit)
        .all()
    )


def get_admin_review_context(
    db: Session, *, status: CustomerLocationChangeRequestStatus | None = None
) -> dict[str, Any]:
    requests = list_requests(db, status=status, limit=100)
    pending_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .count()
    )
    approved_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.approved
        )
        .count()
    )
    rejected_count = (
        db.query(CustomerLocationChangeRequest)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.rejected
        )
        .count()
    )
    return {
        "location_change_requests": requests,
        "pending_location_change_count": pending_count,
        "approved_location_change_count": approved_count,
        "rejected_location_change_count": rejected_count,
        "location_change_filter": status.value if status else "",
    }


def approve_request(
    db: Session,
    *,
    request_id: str,
    actor_id: str | None,
    actor_name: str | None,
    review_note: str | None,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request:
        raise HTTPException(status_code=404, detail="Location correction request not found")
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Location correction request already processed")

    subscriber = db.get(Subscriber, location_request.subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    address = _ensure_target_address(db, subscriber, location_request)
    address.latitude = float(location_request.requested_latitude)
    address.longitude = float(location_request.requested_longitude)
    address.geom = gis_service._point_wkt(address.longitude, address.latitude)
    _upsert_geo_location_for_address(db, address)

    now = datetime.now(UTC)
    location_request.address_id = address.id
    location_request.status = CustomerLocationChangeRequestStatus.approved
    location_request.review_note = (review_note or "").strip() or None
    location_request.reviewed_by_actor_id = actor_id
    location_request.reviewed_by_actor_name = actor_name
    location_request.reviewed_at = now
    location_request.applied_at = now
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        actor_type=AuditActorType.user,
        action="customer_location_change_approved",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(subscriber.id),
            "address_id": str(address.id),
            "review_note": location_request.review_note,
            "requested_latitude": location_request.requested_latitude,
            "requested_longitude": location_request.requested_longitude,
        },
    )
    return location_request


def reject_request(
    db: Session,
    *,
    request_id: str,
    actor_id: str | None,
    actor_name: str | None,
    review_note: str | None,
) -> CustomerLocationChangeRequest:
    location_request = db.get(CustomerLocationChangeRequest, request_id)
    if not location_request:
        raise HTTPException(status_code=404, detail="Location correction request not found")
    if location_request.status != CustomerLocationChangeRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Location correction request already processed")

    location_request.status = CustomerLocationChangeRequestStatus.rejected
    location_request.review_note = (review_note or "").strip() or None
    location_request.reviewed_by_actor_id = actor_id
    location_request.reviewed_by_actor_name = actor_name
    location_request.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(location_request)

    _audit(
        db,
        actor_id=actor_id,
        actor_type=AuditActorType.user,
        action="customer_location_change_rejected",
        entity_id=str(location_request.id),
        metadata={
            "subscriber_id": str(location_request.subscriber_id),
            "review_note": location_request.review_note,
        },
    )
    return location_request
