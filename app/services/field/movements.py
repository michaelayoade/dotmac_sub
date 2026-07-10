"""Native movement sessions for field travel."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile
from app.models.field_movement import FieldWorkOrderMovement
from app.models.work_order_mirror import WorkOrderMirror
from app.services.common import coerce_uuid
from app.services.field.jobs import _location

_CUSTOMER_DESTINATION = "customer"
_ALLOWED_DESTINATIONS = {
    _CUSTOMER_DESTINATION,
    "cabinet",
    "closure",
    "fiber_access_point",
    "pop",
    "olt",
    "other",
    "fdh",
    "splice_closure",
}
_ASSET_TO_DESTINATION = {
    "fdh": "cabinet",
    "splice_closure": "closure",
    "olt": "pop",
}


def serialize_movement(movement: FieldWorkOrderMovement) -> dict:
    return {
        "id": movement.id,
        "crm_work_order_id": movement.crm_work_order_id,
        "destination_type": movement.destination_type,
        "destination_id": movement.destination_id,
        "destination_label": movement.destination_label,
        "destination_latitude": movement.destination_latitude,
        "destination_longitude": movement.destination_longitude,
        "started_at": movement.started_at,
        "arrived_at": movement.arrived_at,
        "start_latitude": movement.start_latitude,
        "start_longitude": movement.start_longitude,
        "arrival_latitude": movement.arrival_latitude,
        "arrival_longitude": movement.arrival_longitude,
        "status": movement.status,
        "client_ref": movement.client_ref,
        "created_at": movement.created_at,
        "updated_at": movement.updated_at,
    }


def list_for_job(db: Session, row: WorkOrderMirror) -> list[dict]:
    movements = (
        db.query(FieldWorkOrderMovement)
        .filter(FieldWorkOrderMovement.work_order_mirror_id == row.id)
        .order_by(
            FieldWorkOrderMovement.started_at.asc(),
            FieldWorkOrderMovement.created_at.asc(),
        )
        .all()
    )
    return [serialize_movement(movement) for movement in movements]


def validate_destination_payload(row: WorkOrderMirror, payload: dict | None) -> None:
    movement_id = (payload or {}).get("movement_session_id")
    if movement_id:
        try:
            coerce_uuid(str(movement_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid movement_session_id"
            ) from exc
    _destination_payload(row, payload)


def is_customer_destination(payload: dict | None) -> bool:
    destination_type = (
        str((payload or {}).get("destination_type") or _CUSTOMER_DESTINATION)
        .strip()
        .lower()
    )
    return (
        _ASSET_TO_DESTINATION.get(destination_type, destination_type)
        == _CUSTOMER_DESTINATION
    )


def start_movement(
    db: Session,
    row: WorkOrderMirror,
    profile: TechnicianProfile,
    *,
    client_ref: UUID,
    occurred_at: datetime,
    latitude: float | None,
    longitude: float | None,
    payload: dict | None,
) -> FieldWorkOrderMovement:
    existing = (
        db.query(FieldWorkOrderMovement)
        .filter(FieldWorkOrderMovement.client_ref == client_ref)
        .one_or_none()
    )
    if existing is not None:
        return existing
    movement = FieldWorkOrderMovement(
        work_order_mirror_id=row.id,
        crm_work_order_id=row.crm_work_order_id,
        actor_technician_id=profile.id,
        actor_person_id=profile.person_id,
        actor_system_user_id=profile.system_user_id,
        started_at=occurred_at,
        start_latitude=latitude,
        start_longitude=longitude,
        status="en_route",
        client_ref=client_ref,
        **_destination_payload(row, payload),
    )
    db.add(movement)
    return movement


def arrive_movement(
    db: Session,
    row: WorkOrderMirror,
    profile: TechnicianProfile,
    *,
    client_ref: UUID,
    occurred_at: datetime,
    latitude: float | None,
    longitude: float | None,
    payload: dict | None,
) -> FieldWorkOrderMovement:
    existing = (
        db.query(FieldWorkOrderMovement)
        .filter(FieldWorkOrderMovement.client_ref == client_ref)
        .one_or_none()
    )
    if existing is not None:
        return existing
    movement = _movement_from_payload(db, payload)
    if movement is None:
        movement = (
            db.query(FieldWorkOrderMovement)
            .filter(FieldWorkOrderMovement.work_order_mirror_id == row.id)
            .filter(FieldWorkOrderMovement.actor_technician_id == profile.id)
            .filter(FieldWorkOrderMovement.status == "en_route")
            .order_by(FieldWorkOrderMovement.started_at.desc())
            .first()
        )
    if movement is None:
        movement = FieldWorkOrderMovement(
            work_order_mirror_id=row.id,
            crm_work_order_id=row.crm_work_order_id,
            actor_technician_id=profile.id,
            actor_person_id=profile.person_id,
            actor_system_user_id=profile.system_user_id,
            started_at=occurred_at,
            status="arrived",
            **_destination_payload(row, payload),
        )
        db.add(movement)
    elif (
        movement.work_order_mirror_id != row.id
        or movement.actor_technician_id != profile.id
    ):
        raise HTTPException(status_code=404, detail="Movement session not found")
    movement.arrived_at = occurred_at
    movement.arrival_latitude = latitude
    movement.arrival_longitude = longitude
    movement.status = "arrived"
    movement.client_ref = client_ref
    return movement


def _movement_from_payload(
    db: Session, payload: dict | None
) -> FieldWorkOrderMovement | None:
    movement_id = (payload or {}).get("movement_session_id")
    if not movement_id:
        return None
    try:
        movement_uuid = coerce_uuid(str(movement_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid movement_session_id"
        ) from exc
    return db.get(FieldWorkOrderMovement, movement_uuid)


def _destination_payload(row: WorkOrderMirror, payload: dict | None) -> dict[str, Any]:
    data = dict(payload or {})
    destination_type = (
        str(data.get("destination_type") or _CUSTOMER_DESTINATION).strip().lower()
    )
    if destination_type not in _ALLOWED_DESTINATIONS:
        raise HTTPException(
            status_code=422, detail=f"Unsupported destination_type: {destination_type}"
        )
    destination_type = _ASSET_TO_DESTINATION.get(destination_type, destination_type)
    if destination_type == _CUSTOMER_DESTINATION:
        location = _location(row)
        return {
            "destination_type": _CUSTOMER_DESTINATION,
            "destination_id": str(row.subscriber_id) if row.subscriber_id else None,
            "destination_label": data.get("destination_label") or "Customer site",
            "destination_latitude": _as_float(
                _first_present(data, "destination_latitude", location.latitude)
            ),
            "destination_longitude": _as_float(
                _first_present(data, "destination_longitude", location.longitude)
            ),
        }
    return {
        "destination_type": destination_type,
        "destination_id": str(data["destination_id"])
        if data.get("destination_id")
        else None,
        "destination_label": data.get("destination_label")
        or data.get("label")
        or destination_type.replace("_", " "),
        "destination_latitude": _as_float(
            _first_present(data, "destination_latitude", data.get("latitude"))
        ),
        "destination_longitude": _as_float(
            _first_present(data, "destination_longitude", data.get("longitude"))
        ),
    }


def _first_present(data: dict[str, Any], key: str, fallback):
    value = data.get(key)
    return fallback if value is None or value == "" else value


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
