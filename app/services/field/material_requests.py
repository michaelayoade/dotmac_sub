"""Native field material requests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import FieldErpSyncFlow, flow_owned_by_sub
from app.models.field_material import (
    FIELD_MATERIAL_REQUEST_PRIORITIES,
    FIELD_MATERIAL_REQUEST_STATUSES,
    FieldInventoryItem,
    FieldMaterialRequest,
    FieldMaterialRequestItem,
    FieldWorkOrderMaterial,
)
from app.models.work_order import WorkOrder
from app.services.common import apply_pagination, coerce_uuid
from app.services.field.jobs import _profile_from_principal, _scoped_query
from app.services.field.source import (
    mark_sub_authoritative as _mark_source_authoritative,
)

logger = logging.getLogger(__name__)

_BACKOFFICE_ISSUED_STATUSES = frozenset(
    {"issued", "fulfilled", "complete", "completed"}
)
_BACKOFFICE_REFUSED_STATUSES = frozenset(
    {"cancelled", "canceled", "rejected", "declined", "denied"}
)


def serialize_material_request(request: FieldMaterialRequest) -> dict:
    return {
        "id": request.id,
        "crm_work_order_id": request.work_order_mirror.public_id,
        "crm_material_request_id": request.crm_material_request_id,
        "requested_by_person_id": request.requested_by_person_id,
        "requested_by_system_user_id": request.requested_by_system_user_id,
        "status": request.status,
        "priority": request.priority,
        "notes": request.notes,
        "source_warehouse_code": request.source_warehouse_code,
        "erp_material_request_id": request.erp_material_request_id,
        "erp_material_status": request.erp_material_status,
        "client_ref": request.client_ref,
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
                "serial_numbers": item.serial_numbers or [],
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
        scoped = _scoped_query(db, profile)
        if crm_work_order_id:
            scoped = scoped.filter(WorkOrder.public_id == crm_work_order_id)
        scoped_ids = scoped.with_entities(WorkOrder.id)
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
        source_warehouse_code: str | None = None,
    ) -> dict:
        if not items:
            raise HTTPException(status_code=422, detail="At least one item is required")
        profile = _profile_from_principal(db, principal)
        row = (
            _scoped_query(db, profile)
            .filter(WorkOrder.public_id == crm_work_order_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        planned_items = _validate_items(db, items)
        request = FieldMaterialRequest(
            work_order_mirror_id=row.id,
            requested_by_technician_id=profile.id,
            requested_by_person_id=profile.person_id,
            requested_by_system_user_id=profile.system_user_id,
            status="draft",
            priority=_priority(priority),
            notes=(notes or "").strip() or None,
            source_warehouse_code=(source_warehouse_code or "").strip() or None,
        )
        db.add(request)
        db.flush()
        for item, quantity, notes, serial_numbers in planned_items:
            request.items.append(
                FieldMaterialRequestItem(
                    item_id=item.id,
                    quantity=quantity,
                    notes=notes,
                    serial_numbers=serial_numbers,
                )
            )
        _mark_sub_authoritative(row)
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
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def list_all(
        db: Session,
        *,
        status: str | None = None,
        crm_work_order_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Manager view: material requests across all technicians."""
        query = (
            db.query(FieldMaterialRequest)
            .options(
                selectinload(FieldMaterialRequest.items).selectinload(
                    FieldMaterialRequestItem.item
                )
            )
            .filter(FieldMaterialRequest.is_active.is_(True))
            .order_by(FieldMaterialRequest.created_at.desc())
        )
        if status:
            query = query.filter(FieldMaterialRequest.status == _status(status))
        if crm_work_order_id:
            work_order_ids = db.query(WorkOrder.id).filter(
                WorkOrder.public_id == crm_work_order_id
            )
            query = query.filter(
                FieldMaterialRequest.work_order_mirror_id.in_(work_order_ids)
            )
        return [
            serialize_material_request(request)
            for request in apply_pagination(query, limit, offset).all()
        ]

    @staticmethod
    def approve(db: Session, material_request_id: str) -> dict:
        """Approve a submitted request and atomically enqueue ERP support work.

        Once the material flow is owned by Sub, the source transition and its
        outbox intent are one transaction.  A request must never be approved in
        Sub without leaving durable work for ERP to fulfil.
        """
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests approve"
            )
        request.status = "approved"
        request.approved_at = datetime.now(UTC)
        _note_request_event(request, "approved")
        _mark_sub_authoritative(request.work_order_mirror)
        _maybe_enqueue_erp_sync(db, request)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def reject(db: Session, material_request_id: str, reason: str) -> dict:
        request = _get_request(db, material_request_id)
        if request.status != "submitted":
            raise HTTPException(
                status_code=409, detail="Only submitted requests reject"
            )
        cleaned = (reason or "").strip()
        if not cleaned:
            raise HTTPException(status_code=422, detail="reason is required")
        request.status = "rejected"
        request.rejected_at = datetime.now(UTC)
        _note_request_event(request, "rejected", reason=cleaned[:500])
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def issue(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status != "approved":
            raise HTTPException(status_code=409, detail="Only approved requests issue")
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "issued"
        _note_request_event(request, "issued")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def fulfill(db: Session, material_request_id: str) -> dict:
        _require_legacy_material_fulfilment(db)
        request = _get_request(db, material_request_id)
        if request.status not in {"approved", "issued"}:
            raise HTTPException(
                status_code=409, detail="Only approved or issued requests fulfill"
            )
        _sync_work_order_materials(db, request, status="reserved")
        request.status = "fulfilled"
        request.fulfilled_at = datetime.now(UTC)
        _note_request_event(request, "fulfilled")
        _mark_sub_authoritative(request.work_order_mirror)
        db.commit()
        db.refresh(request)
        return serialize_material_request(request)

    @staticmethod
    def apply_backoffice_outcome(
        db: Session,
        request: FieldMaterialRequest,
        *,
        erp_request_id: str | None,
        erp_status: str | None,
    ) -> bool:
        """Project one ERP-owned material outcome into the service workflow.

        ERP decides stock availability, serial allocation, issue, and
        cancellation.  This resolver is Sub's only writer for the resulting
        material dependency state.  It is idempotent so both immediate delivery
        responses and later reconciliation can safely call it.
        """
        changed = False
        normalized_status = _normalize_backoffice_status(erp_status)

        if erp_request_id:
            normalized_id = str(erp_request_id)[:120]
            if request.erp_material_request_id not in {None, normalized_id}:
                raise ValueError(
                    "ERP material request identity changed for "
                    f"{request.id}: {request.erp_material_request_id} -> {normalized_id}"
                )
            if request.erp_material_request_id != normalized_id:
                request.erp_material_request_id = normalized_id
                changed = True

        if normalized_status and request.erp_material_status != normalized_status:
            request.erp_material_status = normalized_status
            changed = True

        if normalized_status in _BACKOFFICE_ISSUED_STATUSES:
            _sync_work_order_materials(db, request, status="reserved")
            if request.status in {"approved", "issued"}:
                request.status = "fulfilled"
                request.fulfilled_at = request.fulfilled_at or datetime.now(UTC)
                _note_request_event(request, "backoffice_material_issued")
                changed = True
        elif normalized_status in _BACKOFFICE_REFUSED_STATUSES:
            if request.status in {"approved", "issued"}:
                request.status = "canceled"
                _note_request_event(
                    request,
                    "backoffice_material_refused",
                    reason=f"ERP outcome: {normalized_status}",
                )
                changed = True

        if changed:
            _mark_sub_authoritative(request.work_order_mirror)
        return changed


def _get_scoped_request(
    db: Session,
    principal: dict[str, Any],
    material_request_id: str,
) -> FieldMaterialRequest:
    profile = _profile_from_principal(db, principal)
    scoped_ids = _scoped_query(db, profile).with_entities(WorkOrder.id)
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


def _get_request(db: Session, material_request_id: str) -> FieldMaterialRequest:
    request = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.id == coerce_uuid(material_request_id))
        .filter(FieldMaterialRequest.is_active.is_(True))
        .one_or_none()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Material request not found")
    return request


def _note_request_event(
    request: FieldMaterialRequest, event: str, *, reason: str | None = None
) -> None:
    metadata = dict(request.metadata_ or {})
    events = list(metadata.get("manager_events") or [])
    event_payload: dict[str, Any] = {
        "event": event,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if reason:
        event_payload["reason"] = reason
    events.append(event_payload)
    metadata["manager_events"] = events[-20:]
    if reason:
        metadata["rejection_reason"] = reason
    request.metadata_ = metadata


def _sync_work_order_materials(
    db: Session, request: FieldMaterialRequest, *, status: str
) -> None:
    existing = {
        row.item_id: row
        for row in db.query(FieldWorkOrderMaterial)
        .filter(
            FieldWorkOrderMaterial.work_order_mirror_id == request.work_order_mirror_id
        )
        .filter(FieldWorkOrderMaterial.is_active.is_(True))
        .all()
    }
    for requested_item in request.items:
        row = existing.get(requested_item.item_id)
        if row is None:
            row = FieldWorkOrderMaterial(
                work_order_mirror_id=request.work_order_mirror_id,
                item_id=requested_item.item_id,
                allocated_quantity=requested_item.quantity,
                consumed_quantity=0,
                status=status,
                notes=requested_item.notes,
                metadata_={"material_request_id": str(request.id)},
            )
            db.add(row)
            continue
        row.allocated_quantity = max(row.allocated_quantity, requested_item.quantity)
        row.status = (
            "used" if row.consumed_quantity >= row.allocated_quantity else status
        )
        if requested_item.notes:
            row.notes = requested_item.notes
        metadata = dict(row.metadata_ or {})
        metadata["material_request_id"] = str(request.id)
        row.metadata_ = metadata


def _item(db: Session, item_id) -> FieldInventoryItem:
    item = db.get(FieldInventoryItem, item_id)
    if item is None or not item.is_active:
        raise HTTPException(status_code=404, detail="Material item not found")
    return item


def _validate_items(
    db: Session, items: list[dict[str, Any]]
) -> list[tuple[FieldInventoryItem, int, str | None, list[str]]]:
    planned: list[tuple[FieldInventoryItem, int, str | None, list[str]]] = []
    seen: set[str] = set()
    for entry in items:
        item = _item(db, entry.get("item_id"))
        item_key = str(item.id)
        if item_key in seen:
            raise HTTPException(status_code=422, detail="Duplicate item_id in request")
        seen.add(item_key)
        serial_numbers = [
            str(value).strip()
            for value in (entry.get("serial_numbers") or [])
            if str(value).strip()
        ]
        if len(serial_numbers) != len(set(serial_numbers)):
            raise HTTPException(status_code=422, detail="Duplicate serial number")
        planned.append(
            (
                item,
                _quantity(entry.get("quantity")),
                (entry.get("notes") or "").strip() or None,
                serial_numbers,
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


def _normalize_backoffice_status(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized[:40] or None


def _require_legacy_material_fulfilment(db: Session) -> None:
    """Keep the old local transition available only before explicit cutover."""
    if flow_owned_by_sub(db, FieldErpSyncFlow.material_request):
        raise HTTPException(
            status_code=409,
            detail=(
                "ERP owns material issue and fulfilment after cutover; "
                "reconcile the ERP material outcome instead"
            ),
        )


def _mark_sub_authoritative(row: WorkOrder) -> None:
    _mark_source_authoritative(row, "material_requests")


def _erp_sync_enabled(db: Session) -> bool:
    """Master ERP-sync kill-switch (integration domain, default OFF).

    The switch that keeps the material-request flow INERT until cutover: when off,
    approve does not enqueue an ERP outbox row at all, so nothing can accumulate
    (or, at cutover, double-post against CRM's separate id-space). Ownership of
    the ``material_request`` flow (``sync_flow_ownership``, seeded ``crm``) is the
    second, per-flow gate enforced inside outbox delivery.
    """
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    return bool(
        settings_spec.resolve_value(
            db, SettingDomain.integration, "dotmac_erp_sync_enabled"
        )
    )


def _maybe_enqueue_erp_sync(db: Session, request: FieldMaterialRequest) -> None:
    """Add the ERP intent to the caller's approval transaction after cutover.

    Both gates must be active.  This prevents pre-cutover backlog from being
    delivered unexpectedly when ownership later changes, and makes the approval
    plus outbox row atomic once Sub owns the integration write path.
    """
    if not flow_owned_by_sub(db, FieldErpSyncFlow.material_request):
        return
    if not _erp_sync_enabled(db):
        raise HTTPException(
            status_code=503,
            detail=(
                "ERP material support is the active owner but ERP sync is disabled; "
                "approval cannot be recorded without its outbox intent"
            ),
        )

    from app.services.dotmac_erp.material_sync import (
        enqueue_material_request,
        material_request_eligibility_error,
    )

    reason = material_request_eligibility_error(request)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    if enqueue_material_request(db, request) is None:  # defensive contract guard
        raise RuntimeError(f"Failed to enqueue ERP material request {request.id}")


field_material_requests = FieldMaterialRequests()
