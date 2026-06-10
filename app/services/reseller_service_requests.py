"""Reseller new-service / installation requests.

Submission auto-flags serviceability from fiber-plant proximity (nearest
active cabinet / access point within ``_SERVICEABLE_RADIUS_KM``); staff work
the queue and every status change notifies the reseller on push + email.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.service_request import (
    ResellerServiceRequest,
    Serviceability,
    ServiceRequestStatus,
)
from app.services.common import apply_pagination, coerce_uuid

logger = logging.getLogger(__name__)

_SERVICEABLE_RADIUS_KM = 1.5


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def check_serviceability(db: Session, lat: float | None, lng: float | None):
    """Distance-based pre-check against mapped plant. Returns (flag, nearest_km).

    Honest about its limits: with no coordinates or no mapped plant at all it
    says ``unknown`` rather than guessing."""
    if lat is None or lng is None:
        return Serviceability.unknown, None
    from app.models.network import FdhCabinet

    points = [
        (c.latitude, c.longitude)
        for c in db.query(FdhCabinet.latitude, FdhCabinet.longitude)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    ]
    if not points:
        return Serviceability.unknown, None
    nearest = min(_haversine_km(lat, lng, p[0], p[1]) for p in points)
    flag = (
        Serviceability.serviceable
        if nearest <= _SERVICEABLE_RADIUS_KM
        else Serviceability.not_serviceable
    )
    return flag, round(nearest, 2)


def _serialize(req: ResellerServiceRequest) -> dict:
    return {
        "id": str(req.id),
        "subscriber_id": str(req.subscriber_id) if req.subscriber_id else None,
        "contact_name": req.contact_name,
        "contact_phone": req.contact_phone,
        "contact_email": req.contact_email,
        "address": req.address,
        "latitude": req.latitude,
        "longitude": req.longitude,
        "serviceability": req.serviceability.value,
        "status": req.status.value,
        "notes": req.notes,
        "admin_notes": req.admin_notes,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
    }


def create_request(
    db: Session,
    reseller_id: str,
    *,
    subscriber_id: str | None,
    contact_name: str | None,
    contact_phone: str | None,
    contact_email: str | None,
    address: str | None,
    latitude: float | None,
    longitude: float | None,
    notes: str | None,
) -> dict:
    if not subscriber_id and not (contact_name and contact_phone):
        raise HTTPException(
            status_code=400,
            detail="Provide an existing customer or lead contact name + phone",
        )
    if subscriber_id is not None:
        from app.services.reseller_portal import _get_customer_account

        if _get_customer_account(db, reseller_id, subscriber_id) is None:
            raise HTTPException(status_code=404, detail="Customer account not found")

    flag, nearest_km = check_serviceability(db, latitude, longitude)
    req = ResellerServiceRequest(
        reseller_id=coerce_uuid(reseller_id),
        subscriber_id=coerce_uuid(subscriber_id) if subscriber_id else None,
        contact_name=(contact_name or "").strip() or None,
        contact_phone=(contact_phone or "").strip() or None,
        contact_email=(contact_email or "").strip() or None,
        address=(address or "").strip() or None,
        latitude=latitude,
        longitude=longitude,
        serviceability=flag,
        notes=(notes or "").strip() or None,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    out = _serialize(req)
    out["nearest_plant_km"] = nearest_km
    return out


def list_for_reseller(
    db: Session, reseller_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    query = (
        db.query(ResellerServiceRequest)
        .filter(ResellerServiceRequest.reseller_id == coerce_uuid(reseller_id))
        .order_by(ResellerServiceRequest.created_at.desc())
    )
    return [_serialize(r) for r in apply_pagination(query, limit, offset).all()]


def list_admin(
    db: Session, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[dict]:
    query = db.query(ResellerServiceRequest).order_by(
        ResellerServiceRequest.created_at.desc()
    )
    if status:
        query = query.filter(
            ResellerServiceRequest.status == ServiceRequestStatus(status)
        )
    out = []
    for r in apply_pagination(query, limit, offset).all():
        d = _serialize(r)
        d["reseller_id"] = str(r.reseller_id)
        d["reseller_name"] = r.reseller.name if r.reseller else None
        out.append(d)
    return out


def update_status(
    db: Session,
    request_id: str,
    *,
    status: str,
    admin_notes: str | None = None,
) -> dict:
    req = db.get(ResellerServiceRequest, coerce_uuid(request_id))
    if req is None:
        raise HTTPException(status_code=404, detail="Service request not found")
    try:
        new_status = ServiceRequestStatus(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status") from None
    old_status = req.status
    req.status = new_status
    if admin_notes is not None:
        req.admin_notes = admin_notes.strip() or None
    req.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(req)
    if old_status != new_status:
        _notify_reseller(db, req)
    return _serialize(req)


def _notify_reseller(db: Session, req: ResellerServiceRequest) -> None:
    """Push + email the reseller's portal users on a status change. Best-effort."""
    try:
        from app.models.notification import NotificationChannel
        from app.models.subscriber import ResellerUser, Subscriber
        from app.schemas.notification import NotificationCreate
        from app.services.notification import notifications as notifications_svc

        # Portal users link via ResellerUser (Subscriber.reseller_id marks the
        # reseller's CUSTOMERS, not its operators).
        users = (
            db.query(Subscriber)
            .join(ResellerUser, ResellerUser.subscriber_id == Subscriber.id)
            .filter(ResellerUser.reseller_id == req.reseller_id)
            .filter(ResellerUser.is_active.is_(True))
            .all()
        )
        who = req.contact_name or "your customer"
        subject = f"Service request {req.status.value.replace('_', ' ')}"
        body = (
            f"Your service request for {who} is now "
            f"'{req.status.value.replace('_', ' ')}'."
        )
        if req.admin_notes:
            body += f"\n\nNote from our team: {req.admin_notes}"
        for user in users:
            if not user.email:
                continue
            for channel in (NotificationChannel.push, NotificationChannel.email):
                try:
                    notifications_svc.create(
                        db,
                        NotificationCreate(
                            channel=channel,
                            subscriber_id=user.id,
                            recipient=user.email,
                            subject=subject,
                            body=body,
                            category="service",
                            event_type="service_request_status",
                        ),
                    )
                except Exception:
                    logger.warning("service-request notification failed", exc_info=True)
    except Exception:
        logger.warning("service-request notify block failed", exc_info=True)
