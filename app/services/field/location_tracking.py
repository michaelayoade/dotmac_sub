"""Native field technician location ingest and presence snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.field_location import (
    FIELD_PRESENCE_STATUSES,
    FieldTechLocationPing,
    FieldTechPresence,
)
from app.services.field.jobs import _profile_from_principal

MAX_BATCH_PINGS = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _validate_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if normalized not in FIELD_PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {status}")
    return normalized


class FieldLocationTracking:
    @staticmethod
    def get_or_create_presence(
        db: Session,
        principal: dict[str, Any],
    ) -> FieldTechPresence:
        profile = _profile_from_principal(db, principal)
        presence = (
            db.query(FieldTechPresence)
            .filter(FieldTechPresence.technician_id == profile.id)
            .one_or_none()
        )
        if presence is None:
            presence = FieldTechPresence(
                technician_id=profile.id,
                person_id=profile.person_id,
            )
            db.add(presence)
            db.flush()
        return presence

    @staticmethod
    def set_sharing(
        db: Session,
        principal: dict[str, Any],
        *,
        enabled: bool,
        status: str | None = None,
    ) -> FieldTechPresence:
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        presence.location_sharing_enabled = bool(enabled)
        next_status = _validate_status(status)
        if next_status is not None:
            presence.status = next_status
        elif not enabled:
            presence.status = "off_shift"
        presence.last_seen_at = _now()
        db.commit()
        db.refresh(presence)
        return presence

    @staticmethod
    def record_ping(
        db: Session,
        principal: dict[str, Any],
        *,
        latitude: float,
        longitude: float,
        accuracy_m: float | None = None,
        captured_at: datetime | None = None,
        crm_work_order_id: str | None = None,
        source: str = "mobile",
        status: str | None = None,
        commit: bool = True,
    ) -> dict:
        presence = FieldLocationTracking.get_or_create_presence(db, principal)
        captured = _as_utc(captured_at)
        now = _now()
        ping = FieldTechLocationPing(
            technician_id=presence.technician_id,
            person_id=presence.person_id,
            crm_work_order_id=crm_work_order_id,
            latitude=float(latitude),
            longitude=float(longitude),
            accuracy_m=float(accuracy_m) if accuracy_m is not None else None,
            captured_at=captured,
            received_at=now,
            source=source or "mobile",
        )
        db.add(ping)

        next_status = _validate_status(status)
        if next_status is not None:
            presence.status = next_status
        presence.last_seen_at = now
        prior = presence.last_location_at
        if prior is not None and prior.tzinfo is None:
            prior = prior.replace(tzinfo=UTC)
        if prior is None or captured >= prior:
            presence.last_latitude = float(latitude)
            presence.last_longitude = float(longitude)
            presence.last_location_accuracy_m = (
                float(accuracy_m) if accuracy_m is not None else None
            )
            presence.last_location_at = captured

        if commit:
            db.commit()
            db.refresh(ping)
            db.refresh(presence)
        else:
            db.flush()
        return {"ping": ping, "presence": presence}

    @staticmethod
    def record_batch(
        db: Session,
        principal: dict[str, Any],
        pings: list[dict[str, Any]],
    ) -> dict:
        if len(pings) > MAX_BATCH_PINGS:
            raise HTTPException(status_code=422, detail="Batch exceeds 200 pings")

        accepted = 0
        errors: list[dict[str, Any]] = []
        last: dict | None = None
        for index, raw in enumerate(pings):
            try:
                last = FieldLocationTracking.record_ping(
                    db,
                    principal,
                    latitude=raw["latitude"],
                    longitude=raw["longitude"],
                    accuracy_m=raw.get("accuracy_m"),
                    captured_at=raw.get("captured_at"),
                    crm_work_order_id=raw.get("crm_work_order_id"),
                    source=raw.get("source", "mobile"),
                    status=raw.get("status"),
                    commit=False,
                )
                accepted += 1
            except HTTPException as exc:
                errors.append({"index": index, "detail": exc.detail})
            except (KeyError, TypeError, ValueError) as exc:
                errors.append({"index": index, "detail": str(exc)})

        db.commit()
        presence = (
            last["presence"]
            if last is not None
            else FieldLocationTracking.get_or_create_presence(db, principal)
        )
        db.refresh(presence)
        return {
            "accepted": accepted,
            "errors": errors,
            "presence": presence,
            "transitions": [],
        }


field_location_tracking = FieldLocationTracking()
