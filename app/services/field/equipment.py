"""Field equipment capture over sub's native ONT inventory model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.network import OntAssignment, OntUnit
from app.models.work_order_mirror import WorkOrderMirror
from app.services.field.jobs import _profile_from_principal, _scoped_query


def serialize_equipment(assignment: OntAssignment) -> dict:
    unit = assignment.ont_unit
    return {
        "id": assignment.id,
        "ont_unit_id": assignment.ont_unit_id,
        "serial_number": unit.serial_number if unit else None,
        "vendor": unit.vendor if unit else None,
        "model": unit.model if unit else None,
        "subscriber_id": assignment.subscriber_id,
        "crm_work_order_id": assignment.work_order_mirror.crm_work_order_id
        if assignment.work_order_mirror
        else None,
        "assigned_at": assignment.assigned_at,
        "active": assignment.active,
        "notes": assignment.notes,
    }


class FieldEquipment:
    @staticmethod
    def record(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
        *,
        serial_number: str,
        vendor: str | None = None,
        model: str | None = None,
        notes: str | None = None,
    ) -> dict:
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        serial = _normalize_serial(serial_number)
        unit = _get_or_create_unit(db, serial, vendor=vendor, model=model)
        now = datetime.now(UTC)
        prior = (
            db.query(OntAssignment)
            .filter(
                or_(
                    OntAssignment.subscriber_id == row.subscriber_id,
                    OntAssignment.ont_unit_id == unit.id,
                )
            )
            .filter(OntAssignment.active.is_(True))
            .with_for_update()
            .all()
        )
        for assignment in prior:
            assignment.active = False
            assignment.released_at = assignment.released_at or now
            assignment.release_reason = assignment.release_reason or "field_replaced"
        db.flush()

        assignment = OntAssignment(
            ont_unit_id=unit.id,
            subscriber_id=row.subscriber_id,
            work_order_mirror_id=row.id,
            assigned_at=now,
            active=True,
            notes=(notes or "").strip() or None,
        )
        _mark_pending_sync(row, serial)
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return serialize_equipment(assignment)

    @staticmethod
    def current_for_job(
        db: Session,
        principal: dict[str, Any],
        crm_work_order_id: str,
    ) -> dict | None:
        row = _scoped_work_order(db, principal, crm_work_order_id)
        assignment = (
            db.query(OntAssignment)
            .filter(OntAssignment.subscriber_id == row.subscriber_id)
            .filter(OntAssignment.active.is_(True))
            .order_by(OntAssignment.assigned_at.desc().nullslast())
            .first()
        )
        return serialize_equipment(assignment) if assignment else None


def _scoped_work_order(
    db: Session,
    principal: dict[str, Any],
    crm_work_order_id: str,
) -> WorkOrderMirror:
    profile = _profile_from_principal(db, principal)
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _normalize_serial(serial_number: str) -> str:
    serial = (serial_number or "").strip().upper()
    if not serial:
        raise HTTPException(status_code=422, detail="serial_number is required")
    return serial


def _get_or_create_unit(
    db: Session,
    serial: str,
    *,
    vendor: str | None,
    model: str | None,
) -> OntUnit:
    unit = db.query(OntUnit).filter(OntUnit.serial_number == serial).first()
    if unit is None:
        unit = OntUnit(
            serial_number=serial,
            vendor=(vendor or "").strip() or None,
            model=(model or "").strip() or None,
            is_active=True,
            last_sync_source="field",
            last_sync_at=datetime.now(UTC),
        )
        db.add(unit)
        db.flush()
        return unit
    if vendor:
        unit.vendor = vendor.strip() or unit.vendor
    if model:
        unit.model = model.strip() or unit.model
    unit.is_active = True
    unit.last_sync_source = "field"
    unit.last_sync_at = datetime.now(UTC)
    return unit


def _mark_pending_sync(row: WorkOrderMirror, serial: str) -> None:
    metadata = dict(row.metadata_ or {})
    metadata["native_equipment_pending_sync"] = True
    metadata["last_native_equipment"] = {
        "serial_number": serial,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    row.metadata_ = metadata


field_equipment = FieldEquipment()
