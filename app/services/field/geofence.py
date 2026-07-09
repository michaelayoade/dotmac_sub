"""Geofence auto-status for imported field jobs.

When enabled, a fresh technician location ping can auto-start an assigned
scheduled/dispatched work order once the technician is inside the configured
arrival radius. The transition still goes through the native field transition
engine, so idempotency, status guards, timers, and sub-authoritative activity
metadata stay in one place.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import _location, _profile_from_principal, _scoped_query
from app.services.field.transitions import field_transitions

logger = logging.getLogger(__name__)

_GEOFENCE_NS = uuid.UUID("9f1c0d2e-7b3a-4c6e-9a8d-1e2f3a4b5c6d")
DEFAULT_ARRIVAL_RADIUS_M = 120.0
_ARRIVABLE_STATUSES = {"scheduled", "dispatched"}


def geofence_enabled(db: Session) -> bool:
    row = _setting_row(db, "geofence_auto_status_enabled")
    if row is None:
        return False
    value = row.value_json if row.value_json is not None else row.value_text
    return str(value).strip().lower() in {"true", "1", "yes"}


def arrival_radius_m(db: Session) -> float:
    row = _setting_row(db, "geofence_arrival_radius_m")
    if row is None:
        return DEFAULT_ARRIVAL_RADIUS_M
    value = row.value_json if row.value_json is not None else row.value_text
    try:
        radius = float(str(value))
    except (TypeError, ValueError):
        return DEFAULT_ARRIVAL_RADIUS_M
    return radius if radius > 0 else DEFAULT_ARRIVAL_RADIUS_M


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius_m * math.asin(min(1.0, math.sqrt(a)))


def evaluate(
    db: Session,
    principal: dict[str, Any],
    latitude: float,
    longitude: float,
) -> list[dict[str, Any]]:
    if not geofence_enabled(db):
        return []

    profile = _profile_from_principal(db, principal)
    radius = arrival_radius_m(db)
    fired: list[dict[str, Any]] = []
    for row in _arrivable_jobs(db, profile):
        location = _location(row)
        if location.latitude is None or location.longitude is None:
            continue
        distance = haversine_m(
            latitude,
            longitude,
            float(location.latitude),
            float(location.longitude),
        )
        if distance > radius:
            continue

        client_event_id = uuid.uuid5(_GEOFENCE_NS, f"start:{row.crm_work_order_id}")
        try:
            result = field_transitions.apply(
                db,
                principal,
                row.crm_work_order_id,
                event="start",
                client_event_id=client_event_id,
                latitude=latitude,
                longitude=longitude,
                note="Auto-started on geofence arrival",
                payload={"source": "geofence", "distance_m": round(distance, 1)},
            )
        except HTTPException:
            continue

        if not result.get("replayed"):
            fired.append(
                {
                    "crm_work_order_id": row.crm_work_order_id,
                    "event": "start",
                    "distance_m": round(distance, 1),
                }
            )
    return fired


def _setting_row(db: Session, key: str) -> DomainSetting | None:
    return (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.field)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )


def _arrivable_jobs(db: Session, profile) -> list[WorkOrderMirror]:
    return (
        _scoped_query(db, profile)
        .filter(WorkOrderMirror.status.in_(_ARRIVABLE_STATUSES))
        .all()
    )
