"""Thin admin adapter for reviewed network-operation recovery."""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network_operation import NetworkOperationTargetType
from app.services import web_admin as web_admin_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.network_operation_dispatch import (
    DispatchResolution,
    NetworkOperationDispatchError,
    ResolveDispatchCommand,
    list_reconciliation_needed_dispatches,
    resolve_reconciliation_needed_dispatch,
)
from app.services.network_operation_recovery import (
    NetworkOperationRecoveryError,
    redrive_operation,
)
from app.services.network_operations import network_operations
from app.services.web_network_ont_actions._common import actor_name_from_request
from app.web.templates import templates

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


_RESOLVE_PERMISSION = "network:ont:write"
_LIST_PATH = "/admin/network/dispatch-reconciliation"


def _resolve_redirect(
    return_ont_id: str, *, status: str, message: str
) -> RedirectResponse:
    if return_ont_id:
        url = f"/admin/network/onts/{quote_plus(return_ont_id)}?tab=operations"
    else:
        url = _LIST_PATH
    sep = "&" if "?" in url else "?"
    url += f"{sep}feedback_status={quote_plus(status)}"
    url += f"&feedback_message={quote_plus(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.get(
    "/dispatch-reconciliation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def dispatch_reconciliation_list(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    """Read-only list of dispatches awaiting an owner resolution."""
    from app.services.auth_dependencies import has_permission

    auth = getattr(request.state, "auth", None) or {}
    rows = []
    for dispatch in list_reconciliation_needed_dispatches(db):
        operation = dispatch.operation
        rows.append(
            {
                "id": str(dispatch.id),
                "command_name": dispatch.command_name,
                "created_at": dispatch.created_at,
                "last_error": dispatch.last_error or "",
                "operation_id": str(dispatch.operation_id),
                "operation_status": operation.status.value if operation else "",
                "target_type": operation.target_type.value if operation else "",
                "target_id": str(operation.target_id) if operation else "",
            }
        )
    context = {
        "request": request,
        "active_page": "dispatch-reconciliation",
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "dispatches": rows,
        "resolutions": [item.value for item in DispatchResolution],
        "can_resolve_network_dispatch": bool(auth)
        and has_permission(auth, db, _RESOLVE_PERMISSION),
    }
    return templates.TemplateResponse(
        "admin/network/dispatch_reconciliation.html", context
    )


@router.post(
    "/dispatches/{dispatch_id}/resolve",
    dependencies=[Depends(require_permission(_RESOLVE_PERMISSION))],
)
def resolve_network_dispatch(
    request: Request,
    dispatch_id: str,
    resolution: str = Form(..., max_length=32),
    evidence: str = Form(..., min_length=8, max_length=2000),
    expected_status: str = Form(..., max_length=40),
    return_ont_id: str = Form("", max_length=64),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Close a reconciliation_needed dispatch. Never touches the device."""
    actor_id = web_admin_service.get_actor_id(request)
    if not actor_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        resolve_reconciliation_needed_dispatch(
            db,
            ResolveDispatchCommand(
                dispatch_id=dispatch_id,
                resolution=DispatchResolution(resolution),
                evidence=evidence,
                actor=str(actor_id),
                expected_status=expected_status,
            ),
        )
    except ValueError as exc:
        # NetworkOperationDispatchError and an unknown resolution value are
        # both ValueErrors; neither changed anything (the owner rolled back).
        message = (
            exc.message
            if isinstance(exc, NetworkOperationDispatchError)
            else "Resolution is not recognised."
        )
        return _resolve_redirect(return_ont_id, status="error", message=message)
    return _resolve_redirect(
        return_ont_id, status="success", message="Dispatch closed with your evidence."
    )
