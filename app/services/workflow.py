from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.schemas.workflow import StatusTransitionRequest
from app.services.common import validate_enum, coerce_uuid

from typing import TypeVar, cast

TModel = TypeVar("TModel")


def _get_by_id(db: Session, model: type[TModel], value: str | UUID) -> TModel | None:
    return cast(TModel | None, db.get(model, coerce_uuid(value)))


def transition_service_order(
    db: Session,
    service_order_id: str,
    payload: StatusTransitionRequest,
    skip_contract_check: bool = False,
) -> ServiceOrder:
    """Transition a service order to a new status.

    Blocks transition to 'provisioning' or 'active' if contract not signed.

    Args:
        db: Database session
        service_order_id: Service order ID
        payload: Status transition request
        skip_contract_check: If True, skip the contract signature check

    Raises:
        HTTPException: If service order not found or contract not signed
    """
    from app.services.contracts import contract_signatures

    service_order = _get_by_id(db, ServiceOrder, service_order_id)
    if not service_order:
        raise HTTPException(status_code=404, detail="Service order not found")

    to_status = validate_enum(payload.to_status, ServiceOrderStatus, "to_status")

    # Block transition to fulfillment stages if contract not signed
    fulfillment_stages = {ServiceOrderStatus.provisioning, ServiceOrderStatus.active}
    if to_status in fulfillment_stages and not skip_contract_check:
        if not contract_signatures.is_signed(db, service_order_id):
            raise HTTPException(
                status_code=400,
                detail="Contract must be signed before fulfillment. "
                f"Please sign at: /portal/service-orders/{service_order_id}/contract",
            )

    service_order.status = to_status
    db.commit()
    db.refresh(service_order)
    return service_order
