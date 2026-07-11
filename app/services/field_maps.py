"""Read helpers for the admin field maps (live map + movement playback).

Backs the dispatch ``live-map`` and ``movement-playback`` admin pages over
sub's native Phase-2 field tracking data:

* ``field_tech_presence`` — latest technician position snapshot (plain lat/lng
  floats, ``app/models/field_location.py``).
* ``field_work_order_movements`` — technician travel legs
  (``app/models/field_movement.py``).

Technician positions are plain lat/lng columns (no PostGIS geometry), so these
feeds are ordinary JSON. The ``ST_AsGeoJSON`` path is only used by the vendor
route geometry service (``app/services/vendor_routes_api.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_location import FieldTechPresence
from app.models.field_movement import FieldWorkOrderMovement
from app.models.work_order_mirror import WorkOrderMirror


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _as_utc(value)
    return aware.isoformat() if aware else None


def _technician_label(profile: TechnicianProfile | None) -> str:
    """Human label for a technician, mirroring the dispatch web service."""
    if profile is None:
        return "Unknown technician"
    user = getattr(profile, "system_user", None)
    if user is not None:
        name = (user.display_name or f"{user.first_name} {user.last_name}").strip()
        if name:
            return name
        if user.email:
            return user.email
    metadata = profile.metadata_ or {}
    for key in ("name", "display_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return profile.title or profile.crm_person_id or str(profile.person_id)


def list_technician_positions(
    db: Session,
    *,
    stale_after_seconds: int = 120,
    limit: int = 500,
) -> dict:
    """Latest known position for each technician sharing location.

    Returns a small JSON feed the live-map polls every ~30s.
    """
    rows = (
        db.query(FieldTechPresence)
        .filter(FieldTechPresence.last_latitude.isnot(None))
        .filter(FieldTechPresence.last_longitude.isnot(None))
        .order_by(FieldTechPresence.last_location_at.desc())
        .limit(limit)
        .all()
    )
    now = _now()
    items: list[dict] = []
    live_count = 0
    for presence in rows:
        last_at = _as_utc(presence.last_location_at)
        is_live = bool(
            last_at and (now - last_at).total_seconds() <= stale_after_seconds
        )
        if is_live:
            live_count += 1
        items.append(
            {
                "technician_id": str(presence.technician_id),
                "person_id": str(presence.person_id),
                "label": _technician_label(presence.technician),
                "status": presence.status,
                "latitude": presence.last_latitude,
                "longitude": presence.last_longitude,
                "accuracy_m": presence.last_location_accuracy_m,
                "last_location_at": _iso(presence.last_location_at),
                "is_live": is_live,
            }
        )
    return {
        "count": len(items),
        "live_count": live_count,
        "stale_after_seconds": stale_after_seconds,
        "items": items,
    }


def list_movement_work_orders(db: Session, *, limit: int = 200) -> list[dict]:
    """Distinct work orders that have recorded travel legs (playback picker)."""
    rows = (
        db.query(
            FieldWorkOrderMovement.crm_work_order_id,
            WorkOrderMirror.title,
        )
        .outerjoin(
            WorkOrderMirror,
            WorkOrderMirror.id == FieldWorkOrderMovement.work_order_mirror_id,
        )
        .order_by(FieldWorkOrderMovement.started_at.desc())
        .all()
    )
    seen: dict[str, str] = {}
    for crm_work_order_id, title in rows:
        if crm_work_order_id in seen:
            continue
        seen[crm_work_order_id] = (title or "").strip() or crm_work_order_id
        if len(seen) >= limit:
            break
    return [
        {"crm_work_order_id": wo_id, "label": label} for wo_id, label in seen.items()
    ]


def list_movement_points(
    db: Session,
    *,
    crm_work_order_id: str | None = None,
    technician_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 1000,
) -> dict:
    """Ordered travel points for a work order / technician movement playback.

    Each leg contributes its start point (at ``started_at``) and, once the
    technician has arrived, its arrival point (at ``arrived_at``). Points are
    returned in chronological order for the client-side scrubber.
    """
    query = db.query(FieldWorkOrderMovement)
    if crm_work_order_id:
        query = query.filter(
            FieldWorkOrderMovement.crm_work_order_id == crm_work_order_id
        )
    if technician_id:
        query = query.filter(
            FieldWorkOrderMovement.actor_technician_id == technician_id
        )
    if since is not None:
        query = query.filter(FieldWorkOrderMovement.started_at >= since)
    if until is not None:
        query = query.filter(FieldWorkOrderMovement.started_at <= until)
    legs = query.order_by(FieldWorkOrderMovement.started_at.asc()).limit(limit).all()

    points: list[dict] = []
    for leg in legs:
        if leg.start_latitude is not None and leg.start_longitude is not None:
            points.append(
                {
                    "latitude": leg.start_latitude,
                    "longitude": leg.start_longitude,
                    "captured_at": _iso(leg.started_at),
                    "kind": "start",
                    "status": leg.status,
                    "label": leg.destination_label,
                }
            )
        if leg.arrival_latitude is not None and leg.arrival_longitude is not None:
            points.append(
                {
                    "latitude": leg.arrival_latitude,
                    "longitude": leg.arrival_longitude,
                    "captured_at": _iso(leg.arrived_at or leg.started_at),
                    "kind": "arrival",
                    "status": leg.status,
                    "label": leg.destination_label,
                }
            )
    return {
        "leg_count": len(legs),
        "point_count": len(points),
        "points": points,
    }
