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
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.work_order import WorkOrder
from app.services.db_session_adapter import db_session_adapter
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    FieldEvent,
    FieldTransitionPayload,
    FieldTransitionSource,
)
from app.services.field.jobs import _location
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActorKind,
    ResolveFieldActor,
    resolve_field_actor,
    scoped_work_orders,
)
from app.services.owner_commands import CommandContext

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


@dataclass(frozen=True, slots=True)
class GeofenceQuery:
    system_user_id: uuid.UUID
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class GeofenceTransition:
    public_id: str
    event: FieldEvent
    distance_m: float


def evaluate(db: Session, query: GeofenceQuery) -> tuple[GeofenceTransition, ...]:
    if not geofence_enabled(db):
        db_session_adapter.release_read_transaction(db)
        return ()
    actor = resolve_field_actor(db, ResolveFieldActor(query.system_user_id))
    if actor.kind != FieldActorKind.technician:
        db_session_adapter.release_read_transaction(db)
        return ()
    radius = arrival_radius_m(db)
    candidates: list[tuple[str, float]] = []
    for row in (
        scoped_work_orders(db, actor)
        .filter(WorkOrder.status.in_(_ARRIVABLE_STATUSES))
        .all()
    ):
        location = _location(row)
        if location.latitude is None or location.longitude is None:
            continue
        distance = haversine_m(
            query.latitude, query.longitude, location.latitude, location.longitude
        )
        if distance <= radius:
            candidates.append((row.public_id, round(distance, 1)))
    # The query's implicit transaction is read-only. Each selected transition
    # then enters its own registered command owner and rechecks current scope.
    db_session_adapter.release_read_transaction(db)
    fired: list[GeofenceTransition] = []
    for public_id, distance in candidates:
        client_event_id = uuid.uuid5(_GEOFENCE_NS, f"start:{public_id}")
        try:
            result = field_transitions.apply(
                db,
                ApplyFieldTransition(
                    context=CommandContext.system(
                        actor=str(actor.system_user_id),
                        scope="field",
                        reason="Geofence arrival",
                        idempotency_key=str(client_event_id),
                    ),
                    requester_system_user_id=actor.system_user_id,
                    public_id=public_id,
                    event=FieldEvent.start,
                    client_event_id=client_event_id,
                    latitude=query.latitude,
                    longitude=query.longitude,
                    note="Auto-started on geofence arrival",
                    payload=FieldTransitionPayload(
                        source=FieldTransitionSource.geofence, distance_m=distance
                    ),
                ),
            )
        except FieldAccessError:
            continue
        if not result.replayed:
            fired.append(GeofenceTransition(public_id, FieldEvent.start, distance))
    return tuple(fired)


def _setting_row(db: Session, key: str) -> DomainSetting | None:
    return (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.field)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
