"""Native field material requests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.field_material import (
    FIELD_MATERIAL_REQUEST_PRIORITIES,
    FIELD_MATERIAL_REQUEST_STATUSES,
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldMaterialRequestItem,
)
from app.models.work_order_mirror import WorkOrderMirror
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query


def serialize_material_request(request: FieldMaterialRequest) -> dict:
    return {
        "id": request.id,
        "crm_work_order_id": request.crm_work_order_id,
        "crm_material_request_id": request.crm_material_request_id,
        "requested_by_person_id": request.requested_by_person_id,
        "requested_by_system_user_id": request.requested_by_system_user_id,
        "status": request.status,
        "priority": request.priority,
        "notes": request.notes,
        "submitted_at": request.submitted_at,
        "approved_at": request.approved_at,
        "rejected_at": request.rejected_at,
        "fulfilled_at": request.fulfilled_at,
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "items": [
            {
                "id": item.id,
                "item_id": item.item_id,
                "sku": item.item.sku if item.item else None,
                "name": item.item.name if item.item else None,
                "unit": item.item.unit if item.item else None,
                "quantity": item.quantity,
                "notes": item.notes,
            }
            for item in request.items
        ],
    }


class FieldMaterialRequests:
    @staticmethod
    def list_mine(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        profile = _profile_from_principal(db, principal)
        scoped_ids = _scoped_query(db, profile).with_entities(WorkOrderMirror.id)
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if crm_work_order_id:
            query = query.filter(
                FieldMaterialRequest.crm_work_order_id == crm_work_order_id
            )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def get(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        return serialize_material_request(request)

    @staticmethod
    def create(
        db: Session,
        principal: dict[str, Any],
        *,
        crm_work_order_id: str,
        priority: str,
        notes: str | None,
        items: list[dict[str, Any]],
    ) -> dict:
        if not items:
            raise HTTPException(status_code=422, detail="At least one item is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrderMirror.crm_work_order_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        planned_items = _validate_items(db, items)
        request = FieldMaterialRequest(
            work_order_mirror_id=row.id,
            crm_work_order_id=row.crm_work_order_id,
            requested_by_technician_id=profile.id,
            requested_by_person_id=profile.person_id,
            requested_by_system_user_id=profile.system_user_id,
            status="draft",
            priority=_priority(priority),
            notes=(notes or "").strip() or None,
        )
        db.add(request)
        db.flush()
        for item, quantity, notes in planned_items:
            request.items.append(
                FieldMaterialRequestItem(
                    item_id=item.id,
                    quantity=quantity,
                    notes=notes,
                )
            )
        _mark_pending_sync(row)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def submit(
        db: Session,
        principal: dict[str, Any],
        material_request_id: str,
    ) -> dict:
        request = _get_scoped_request(db, principal, material_request_id)
        profile = _profile_from_principal(db, principal)
        if request.requested_by_technician_id != profile.id:
            raise HTTPException(status_code=404, detail="Material request not found")
        if request.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft requests submit")
        request.status = "submitted"
        request.submitted_at = datetime.now(UTC)
        _mark_pending_sync(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)


def _get_scoped_request(
    db: Session,
    principal: dict[str, Any],
    material_request_id: str,
) -> FieldMaterialRequest:
    profile = _profile_from_principal(db, principal)
    scoped_ids = _scoped_query(db, profile).with_entities(WorkOrderMirror.id)
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.work_order_mirror_id.in_(scoped_ids))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _item(db: Session, item_id) -> FieldInventoryItem:
    item = db.get(FieldInventoryItem, item_id)
    if item is None or not item.is_active:
        raise HTTPException(status_code=404, detail="Material item not found")
    return item


def _validate_items(
    db: Session, items: list[dict[str, Any]]
) -> list[tuple[FieldInventoryItem, int, str | None]]:
    planned: list[tuple[FieldInventoryItem, int, str | None]] = []
    seen: set[str] = set()
    for entry in items:
        item = _item(db, entry.get("item_id"))
        item_key = str(item.id)
        if item_key in seen:
            raise HTTPException(status_code=422, detail="Duplicate item_id in request")
        seen.add(item_key)
        planned.append(
            (
                item,
                _quantity(entry.get("quantity")),
                (entry.get("notes") or "").strip() or None,
            )
        )
    return planned


def _quantity(value) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="quantity must be an integer"
        ) from exc
    if quantity <= 0:
        raise HTTPException(
            status_code=422, detail="quantity must be greater than zero"
        )
    return quantity


def _priority(value: str) -> str:
    priority = (value or "medium").strip().lower()
    if priority not in FIELD_MATERIAL_REQUEST_PRIORITIES:
        raise HTTPException(status_code=422, detail=f"Unsupported priority: {value}")
    return priority


def _status(value: str) -> str:
    status = (value or "").strip().lower()
    if status not in FIELD_MATERIAL_REQUEST_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unsupported status: {value}")
    return status


def _mark_pending_sync(row: WorkOrderMirror) -> None:
    metadata = dict(row.metadata_ or {})
    metadata["native_material_requests_pending_sync"] = True
    row.metadata_ = metadata


field_material_requests = FieldMaterialRequests()
