"""Thin admin adapter for reviewed network-operation recovery."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network_operation import NetworkOperationTargetType
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.network_operation_recovery import (
    NetworkOperationRecoveryError,
    redrive_operation,
)
from app.services.network_operations import network_operations
from app.services.web_network_ont_actions._common import actor_name_from_request

router = APIRouter(prefix="/network", tags=["web-admin-network-operations"])


def _operation_redirect(
    target_type: NetworkOperationTargetType | None,
    target_id: object | None,
    *,
    status: str,
    message: str,
) -> RedirectResponse:
    if target_type == NetworkOperationTargetType.ont and target_id is not None:
        url = f"/admin/network/onts/{target_id}?tab=operations"
        url += f"&feedback_status={quote_plus(status)}"
        url += f"&feedback_message={quote_plus(message)}"
    else:
        url = "/admin/network"
    return RedirectResponse(url=url, status_code=303)


@router.post(
    "/operations/{operation_id}/redrive",
    dependencies=[Depends(require_permission("network:operation:redrive"))],
)
def redrive_network_operation(
    request: Request,
    operation_id: str,
    expected_head: str = Form(..., min_length=64, max_length=64),
    idempotency_key: str = Form(..., min_length=16, max_length=160),
    reason: str = Form(..., min_length=8, max_length=500),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    source = None
    try:
        source = network_operations.get(db, operation_id)
        result = redrive_operation(
            db,
            operation_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            reason=reason,
            initiated_by=actor_name_from_request(request),
        )
        log_audit_event(
            db,
            request,
            action="network_operation.redrive",
            entity_type="network_operation",
            entity_id=operation_id,
            actor_id=None,
            metadata={
                "outcome": result.outcome.value,
                "new_operation_id": str(result.operation.id),
                "target_type": result.operation.target_type.value,
                "target_id": str(result.operation.target_id),
                "reason": reason.strip(),
                "replayed": result.replayed,
            },
            status_code=202 if not result.replayed else 200,
        )
        db.commit()
        return _operation_redirect(
            result.operation.target_type,
            result.operation.target_id,
            status="success",
            message=result.message,
        )
    except NetworkOperationRecoveryError as exc:
        log_audit_event(
            db,
            request,
            action="network_operation.redrive",
            entity_type="network_operation",
            entity_id=operation_id,
            actor_id=None,
            metadata={"outcome": exc.code, "reason": reason.strip()},
            status_code=exc.status_code,
            is_success=False,
        )
        db.commit()
        return _operation_redirect(
            source.target_type if source is not None else None,
            source.target_id if source is not None else None,
            status="error",
            message=exc.message,
        )
