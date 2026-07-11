from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.field_erp import FieldErpSyncEvent
from app.models.field_expense import FieldExpenseRequest
from app.models.field_material import FieldMaterialRequest


def enqueue_field_erp_event(
    db: Session,
    *,
    entity_type: str,
    entity_id,
    action: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> FieldErpSyncEvent:
    event = (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.idempotency_key == idempotency_key)
        .one_or_none()
    )
    if event is None:
        event = FieldErpSyncEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            idempotency_key=idempotency_key,
            payload=payload,
            status="pending",
        )
        db.add(event)
        return event
    if event.status in {"pending", "failed"}:
        event.payload = payload
        event.status = "pending"
        event.last_error = None
    return event


def enqueue_material_request_sync(
    db: Session,
    request: FieldMaterialRequest,
    *,
    action: str,
) -> FieldErpSyncEvent:
    return enqueue_field_erp_event(
        db,
        entity_type="field_material_request",
        entity_id=request.id,
        action=action,
        payload=material_request_payload(request),
        idempotency_key=f"field-mr-{request.id}-{action}-v1",
    )


def enqueue_expense_request_sync(
    db: Session,
    request: FieldExpenseRequest,
    *,
    action: str,
) -> FieldErpSyncEvent:
    return enqueue_field_erp_event(
        db,
        entity_type="field_expense_request",
        entity_id=request.id,
        action=action,
        payload=expense_request_payload(request),
        idempotency_key=f"field-exp-{request.id}-{action}-v1",
    )


def material_request_payload(request: FieldMaterialRequest) -> dict[str, Any]:
    schedule_date = _date_string(
        request.approved_at
        or request.submitted_at
        or request.created_at
        or datetime.utcnow()
    )
    items: list[dict[str, Any]] = []
    for item in request.items:
        inventory_item = item.item
        item_code = str(item.item_id)
        unit = "PCS"
        if inventory_item is not None:
            item_code = inventory_item.sku or inventory_item.name or str(item.item_id)
            unit = inventory_item.unit or "PCS"
        items.append(
            {
                "item_code": item_code,
                "quantity": item.quantity,
                "uom": unit,
                "notes": item.notes,
            }
        )
    return {
        "omni_id": str(request.id),
        "request_type": "ISSUE",
        "status": request.status,
        "schedule_date": schedule_date,
        "requested_by_email": (
            request.requested_by_system_user.email
            if request.requested_by_system_user
            else None
        ),
        "work_order_id": request.crm_work_order_id,
        "remarks": request.notes or "",
        "items": items,
    }


def expense_request_payload(request: FieldExpenseRequest) -> dict[str, Any]:
    claim_date = _date_string(
        request.expense_date or request.submitted_at or request.created_at
    )
    return {
        "omni_id": str(request.id),
        "purpose": request.purpose,
        "claim_date": claim_date,
        "requested_by_email": (
            request.requested_by_system_user.email
            if request.requested_by_system_user
            else None
        ),
        "work_order_id": request.crm_work_order_id,
        "currency_code": request.currency,
        "remarks": request.notes or "",
        "reference_number": str(request.client_ref) if request.client_ref else None,
        "items": [
            {
                "category_code": item.category_code,
                "description": item.description,
                "claimed_amount": _decimal_string(item.amount),
                "expense_date": _date_string(
                    item.expense_date
                    or request.expense_date
                    or request.submitted_at
                    or request.created_at
                ),
                "vendor_name": item.vendor_name,
                "receipt_url": item.receipt_url,
                "notes": item.notes,
            }
            for item in request.items
        ],
    }


def _date_string(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _decimal_string(value: Decimal | None) -> str:
    if value is None:
        return "0.00"
    return str(value.quantize(Decimal("0.01")))
