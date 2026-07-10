"""Field-side fiber capture over sub's network-plant truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.field_attachment import FieldAttachment
from app.models.field_fiber import FIELD_FIBER_TEST_TYPES, FieldFiberTestResult
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    OLTDevice,
)
from app.models.work_order_mirror import WorkOrderMirror
from app.services import fiber_change_requests
from app.services.common import coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query

_TESTABLE_ASSET_MODELS = {
    "fiber_strand": FiberStrand,
    "fiber_splice": FiberSplice,
    "splice_closure": FiberSpliceClosure,
    "fiber_access_point": FiberAccessPoint,
    "fdh": FdhCabinet,
    "fdh_cabinet": FdhCabinet,
    "olt": OLTDevice,
    "olt_device": OLTDevice,
}
_SPLICEABLE_STRAND_STATUSES = {
    FiberStrandStatus.available,
    FiberStrandStatus.reserved,
}


def propose_splice(
    db: Session,
    principal: dict[str, Any],
    *,
    closure_id: str,
    from_strand_id: str,
    to_strand_id: str,
    tray_id: str | None = None,
    position: int | None = None,
    splice_type: str | None = None,
    loss_db: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    profile = _profile_from_principal(db, principal)
    closure_uuid = _uuid_or_422(closure_id, "closure_id")
    from_uuid = _uuid_or_422(from_strand_id, "from_strand_id")
    to_uuid = _uuid_or_422(to_strand_id, "to_strand_id")
    if from_uuid == to_uuid:
        raise HTTPException(
            status_code=422, detail="A strand cannot be spliced to itself"
        )

    closure = db.get(FiberSpliceClosure, closure_uuid)
    if closure is None or not closure.is_active:
        raise HTTPException(status_code=404, detail="Splice closure not found")

    _load_spliceable_strand(db, from_uuid, "from")
    _load_spliceable_strand(db, to_uuid, "to")

    tray_uuid = _uuid_or_422(tray_id, "tray_id") if tray_id else None
    if tray_uuid is not None:
        tray = db.get(FiberSpliceTray, tray_uuid)
        if tray is None:
            raise HTTPException(status_code=404, detail="Splice tray not found")
        if tray.closure_id != closure.id:
            raise HTTPException(
                status_code=422, detail="Splice tray does not belong to this closure"
            )
        if position is not None:
            occupied = (
                db.query(FiberSplice)
                .filter(FiberSplice.tray_id == tray.id)
                .filter(FiberSplice.position == position)
                .first()
            )
            if occupied is not None:
                raise HTTPException(
                    status_code=409, detail="That tray position is already occupied"
                )

    existing = (
        db.query(FiberSplice)
        .filter(
            or_(
                (FiberSplice.from_strand_id == from_uuid)
                & (FiberSplice.to_strand_id == to_uuid),
                (FiberSplice.from_strand_id == to_uuid)
                & (FiberSplice.to_strand_id == from_uuid),
            )
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="A splice between these strands already exists"
        )

    pair = {str(from_uuid), str(to_uuid)}
    for request in _pending_splice_requests(db):
        payload = request.payload or {}
        if {
            str(payload.get("from_strand_id")),
            str(payload.get("to_strand_id")),
        } == pair:
            return _proposal_response(request, replayed=True)

    payload = {
        "closure_id": str(closure.id),
        "from_strand_id": str(from_uuid),
        "to_strand_id": str(to_uuid),
        "tray_id": str(tray_uuid) if tray_uuid else None,
        "position": position,
        "splice_type": splice_type,
        "loss_db": loss_db,
        "notes": note,
        "field_actor": {
            "technician_id": str(profile.id),
            "person_id": str(profile.person_id),
            "system_user_id": str(profile.system_user_id)
            if profile.system_user_id
            else None,
        },
    }
    request = fiber_change_requests.create_request(
        db,
        asset_type="fiber_splice",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=None,
    )
    return _proposal_response(request, replayed=False)


def record_test(
    db: Session,
    principal: dict[str, Any],
    *,
    crm_work_order_id: str,
    asset_type: str,
    asset_id: str,
    test_type: str,
    wavelength_nm: int | None = None,
    value_db: float | None = None,
    unit: str | None = None,
    passed: bool | None = None,
    instrument: str | None = None,
    measured_at: datetime | None = None,
    notes: str | None = None,
    attachment_id: str | None = None,
    client_ref: str | None = None,
) -> FieldFiberTestResult:
    profile = _profile_from_principal(db, principal)
    row = _scoped_work_order(db, profile, crm_work_order_id)
    normalized_type = _normalize_asset_type(asset_type)
    if test_type not in FIELD_FIBER_TEST_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown test_type '{test_type}'")
    model = _TESTABLE_ASSET_MODELS.get(normalized_type)
    if model is None:
        raise HTTPException(
            status_code=400, detail=f"Unsupported asset type: {asset_type}"
        )
    asset_uuid = _uuid_or_422(asset_id, "asset_id")
    if db.get(model, asset_uuid) is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    client_uuid = _uuid_or_422(client_ref, "client_ref") if client_ref else None
    if client_uuid is not None:
        existing = (
            db.query(FieldFiberTestResult)
            .filter(FieldFiberTestResult.client_ref == client_uuid)
            .one_or_none()
        )
        if existing is not None:
            return existing

    attachment_uuid = (
        _uuid_or_422(attachment_id, "attachment_id") if attachment_id else None
    )
    if attachment_uuid is not None:
        attachment = db.get(FieldAttachment, attachment_uuid)
        if (
            attachment is None
            or not attachment.is_active
            or attachment.work_order_mirror_id != row.id
        ):
            raise HTTPException(status_code=404, detail="Attachment not found")

    result = FieldFiberTestResult(
        work_order_mirror_id=row.id,
        crm_work_order_id=row.crm_work_order_id,
        asset_type=normalized_type,
        asset_id=asset_uuid,
        test_type=test_type,
        wavelength_nm=wavelength_nm,
        value_db=value_db,
        unit=unit,
        passed=passed,
        instrument=instrument,
        attachment_id=attachment_uuid,
        measured_by_technician_id=profile.id,
        measured_by_person_id=profile.person_id,
        measured_by_system_user_id=profile.system_user_id,
        measured_at=measured_at,
        notes=notes,
        client_ref=client_uuid,
    )
    db.add(result)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if client_uuid is not None:
            existing = (
                db.query(FieldFiberTestResult)
                .filter(FieldFiberTestResult.client_ref == client_uuid)
                .one_or_none()
            )
            if existing is not None:
                return existing
        raise
    db.refresh(result)
    return result


def list_tests(
    db: Session,
    principal: dict[str, Any],
    *,
    crm_work_order_id: str,
) -> list[FieldFiberTestResult]:
    profile = _profile_from_principal(db, principal)
    row = _scoped_work_order(db, profile, crm_work_order_id)
    return (
        db.query(FieldFiberTestResult)
        .filter(FieldFiberTestResult.work_order_mirror_id == row.id)
        .order_by(FieldFiberTestResult.created_at.desc())
        .all()
    )


def _pending_splice_requests(db: Session) -> list[FiberChangeRequest]:
    return (
        db.query(FiberChangeRequest)
        .filter(FiberChangeRequest.asset_type == "fiber_splice")
        .filter(FiberChangeRequest.status == FiberChangeRequestStatus.pending)
        .all()
    )


def _proposal_response(
    request: FiberChangeRequest, *, replayed: bool
) -> dict[str, Any]:
    payload = request.payload or {}
    return {
        "change_request_id": request.id,
        "status": request.status.value,
        "replayed": replayed,
        "closure_id": payload.get("closure_id"),
        "from_strand_id": payload.get("from_strand_id"),
        "to_strand_id": payload.get("to_strand_id"),
    }


def _load_spliceable_strand(db: Session, strand_id, label: str) -> FiberStrand:
    strand = db.get(FiberStrand, strand_id)
    if strand is None or not strand.is_active:
        raise HTTPException(status_code=404, detail=f"{label} strand not found")
    if strand.status not in _SPLICEABLE_STRAND_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"{label} strand is {strand.status.value}; only available or reserved strands can be spliced",
        )
    return strand


def _scoped_work_order(db: Session, profile, crm_work_order_id: str) -> WorkOrderMirror:
    row = (
        _scoped_query(db, profile)
        .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def _normalize_asset_type(asset_type: str) -> str:
    value = (asset_type or "").strip().lower()
    if value == "fiber_splice_closure":
        return "splice_closure"
    if value == "fdh_cabinet":
        return "fdh"
    if value == "olt_device":
        return "olt"
    return value


def _uuid_or_422(value, field_name: str):
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field_name}") from exc
