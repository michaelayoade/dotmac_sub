"""Admin web routes for ONT provisioning actions.

Each provisioning step is an independent action triggered by the operator
via a button on the ONT detail page. Routes delegate directly to the
service functions in ``app.services.network.ont_provision_steps``.
"""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import (
    web_network_onts_provisioning as web_onts_provisioning_service,
)
from app.services import (
    web_network_provisioning_ledger as web_network_provisioning_ledger_service,
)
from app.services import (
    web_network_provisioning_triage as web_network_provisioning_triage_service,
)
from app.services.auth_dependencies import require_permission
from app.services.network import ont_provision_steps as steps
from app.services.network.action_logging import actor_label, log_network_action_result
from app.services.network.ont_provisioning.credentials import mask_credentials
from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_scope import can_manage_ont_from_request
from app.web.retired_ont_writer import retired_writer_response
from app.web.templates import templates

# Add filter for credential masking in provisioning templates
templates.env.filters["masked_credentials"] = mask_credentials
router = APIRouter(prefix="/network", tags=["web-admin-network-ont-provisioning"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "network",
        "current_user": web_admin_service.get_current_user(request),
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
    }


def _toast_headers(message: str, toast_type: str = "success") -> dict[str, str]:
    return {
        "HX-Trigger": json.dumps(
            {"showToast": {"message": message, "type": toast_type}},
            ensure_ascii=True,
        )
    }


def _ensure_ont_write_scope(
    request: Request, db: Session, ont_id: str
) -> JSONResponse | None:
    if can_manage_ont_from_request(request, db, ont_id):
        return None
    return JSONResponse(
        {"success": False, "message": "ONT scope check failed"},
        status_code=403,
        headers=_toast_headers("ONT scope check failed", "error"),
    )


def _redirect_to_request_target(
    request: Request,
    fallback_path: str,
    *,
    message: str,
    toast_type: str,
) -> RedirectResponse:
    target = request.headers.get("referer") or fallback_path
    response = RedirectResponse(target, status_code=303)
    for key, value in _toast_headers(message, toast_type).items():
        response.headers[key] = value
    return response


def _wants_html_response(request: Request) -> bool:
    """Return whether a normal browser navigation expects an HTML response."""
    return "text/html" in request.headers.get("accept", "").lower()


def _provision_page_redirect(
    ont_id: str,
    *,
    status: str,
    message: str,
    operation_id: str | None = None,
) -> RedirectResponse:
    query = f"status={quote_plus(status)}&message={quote_plus(message)}"
    if operation_id:
        query = f"operation_id={quote_plus(operation_id)}&{query}"
    return RedirectResponse(
        f"/admin/network/onts/{ont_id}/provision?{query}",
        status_code=303,
    )


def _step_response(
    result: StepResult,
    *,
    request: Request | None = None,
    ont_id: str | None = None,
) -> JSONResponse:
    """Convert a StepResult into a JSONResponse with toast notification."""
    if result.waiting:
        toast_type = "info"
        phase = "waiting"
        status_code = 202
    else:
        toast_type = "success" if result.success else "error"
        phase = "succeeded" if result.success else "failed"
        status_code = 200 if result.success else 400
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action=result.step_name,
        success=result.success,
        message=result.message,
        waiting=result.waiting,
        metadata={"critical": result.critical, "skipped": result.skipped},
    )
    return JSONResponse(
        content={
            "success": result.success or result.waiting,
            "message": result.message,
            "step_name": result.step_name,
            "duration_ms": result.duration_ms,
            "critical": result.critical,
            "skipped": result.skipped,
            "waiting": result.waiting,
            "data": result.data,
            "phase": phase,
            "operation": {
                "action": result.step_name.replace("_", " ").title(),
                "phase": phase,
                "detail": result.message,
                "data": result.data,
            },
        },
        status_code=status_code,
        headers=_toast_headers(result.message, toast_type),
    )


def _record_ont_step_action(
    db: Session,
    request: Request,
    ont_id: str,
    result: StepResult,
) -> None:
    """Log an operator-triggered ONT provisioning step."""
    web_onts_provisioning_service.record_ont_step_action(
        db,
        ont_id=ont_id,
        result=result,
    )


def _update_service_order_execution_context_for_ont(
    db: Session,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
) -> None:
    web_onts_provisioning_service.update_service_order_execution_context_for_ont(
        db,
        ont_id=ont_id,
        step_name=step_name,
        values=values,
    )


# ---------------------------------------------------------------------------
# Read-only routes (preflight, preview, save settings)
# ---------------------------------------------------------------------------


@router.get(
    "/onts/{ont_id}/available-static-ips",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_available_static_ips(
    request: Request,
    ont_id: str,
    static_ip_pool_id: str | None = Query(default=None),
    selected_ip: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: available static IPv4 choices for the selected ONT pool."""
    state = web_onts_provisioning_service.available_static_ipv4_choices(
        db,
        pool_id=static_ip_pool_id,
        ont_id=ont_id,
        selected_ip=selected_ip,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(state)
    return templates.TemplateResponse(
        "admin/network/onts/_available_static_ips.html", context
    )


@router.get(
    "/onts/{ont_id}/provisioning-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_provisioning_preview(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Command preview for provisioning an ONT."""
    data = web_onts_provisioning_service.provisioning_preview_context(
        db,
        ont_id=ont_id,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_provisioning_preview.html", context
    )


@router.get(
    "/onts/{ont_id}/preflight",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def ont_preflight_check(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Pre-flight validation for ONT provisioning. Returns JSON checklist."""
    result = web_onts_provisioning_service.preflight_result(
        db,
        ont_id=ont_id,
    )
    return JSONResponse(result)


@router.post(
    "/onts/{ont_id}/save-provision-settings",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_save_provision_settings(ont_id: str) -> JSONResponse:
    """Retired: wrote desired config outside the configuration lifecycle."""
    return retired_writer_response(
        "Save provisioning configuration",
        "Save settings from the Configure tab; each section is admitted by the "
        "ONT configuration lifecycle.",
    )


@router.get(
    "/onts/{ont_id}/save-provision-settings",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def ont_save_provision_settings_get(ont_id: str) -> Response:
    """Handle accidental GET navigations to the save endpoint gracefully."""
    message = "Use the Save OLT Desired Config button to submit this form."
    return RedirectResponse(
        f"/admin/network/onts/{ont_id}/provision?status=error&message={quote_plus(message)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Per-step provisioning actions (OLT SSH)
# ---------------------------------------------------------------------------
# NOTE: Individual step routes (create-service-port, configure-mgmt-ip, etc.)
# have been removed. Use the unified /provision endpoint which reads from
# OntAssignment + OltConfigPack (source of truth) and uses reconciliation.


@router.post(
    "/onts/{ont_id}/step/wait-tr069-bootstrap",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def step_wait_tr069_bootstrap(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Poll GenieACS until the ONT registers after TR-069 binding."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = steps.wait_tr069_bootstrap(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


# ---------------------------------------------------------------------------
# Rollback / cleanup
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/step/rollback-service-ports",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def step_rollback_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Remove all service-ports for this ONT from the OLT."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = steps.rollback_service_ports(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/deprovision",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def step_deprovision(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Full deprovision: remove service-ports, deauthorize, clear DB state."""
    denied = _ensure_ont_write_scope(request, db, ont_id)
    if denied is not None:
        return denied
    result = steps.deprovision(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


# ---------------------------------------------------------------------------
# Direct provisioning
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/provision",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def provision_ont_direct(ont_id: str) -> JSONResponse:
    """Retired: legacy "Provision" created/deleted service ports directly.

    Authorization and the ONT configuration lifecycle own the OLT baseline.
    ``request_ont_provisioning`` and ``ont_provision_steps`` stay because the
    authorization task (``app.tasks.ont_provisioning``) and bulk provisioning
    (``app.services.network.bulk_provisioning``) still import them; only this
    operator route is retired.
    """
    return retired_writer_response(
        "Provision ONT",
        "Authorization applies the OLT baseline and the Configure tab applies "
        "service settings; use Configure for any change.",
    )


@router.post(
    "/onts/{ont_id}/compensation-failures/{failure_id}/retry",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def retry_ont_compensation_failure(
    request: Request,
    ont_id: str,
    failure_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Retry a pending compensation failure for an ONT."""
    from app.services.network.compensation_retry import retry_compensation

    if not can_manage_ont_from_request(request, db, ont_id):
        return _redirect_to_request_target(
            request,
            f"/admin/network/onts/{ont_id}",
            message="ONT scope check failed",
            toast_type="error",
        )

    success, message = retry_compensation(
        db,
        failure_id,
        resolved_by=actor_label(request),
    )
    return _redirect_to_request_target(
        request,
        f"/admin/network/onts/{ont_id}?tab=history",
        message=message,
        toast_type="success" if success else "error",
    )


@router.post(
    "/onts/{ont_id}/compensation-failures/{failure_id}/resolve",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def resolve_ont_compensation_failure(
    request: Request,
    ont_id: str,
    failure_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Mark a compensation failure resolved from the ONT history view."""
    from app.services.network.compensation_retry import mark_resolved

    if not can_manage_ont_from_request(request, db, ont_id):
        return _redirect_to_request_target(
            request,
            f"/admin/network/onts/{ont_id}",
            message="ONT scope check failed",
            toast_type="error",
        )

    success, message = mark_resolved(
        db,
        failure_id,
        resolved_by=actor_label(request),
    )
    return _redirect_to_request_target(
        request,
        f"/admin/network/onts/{ont_id}?tab=history",
        message=message,
        toast_type="success" if success else "error",
    )


@router.post(
    "/onts/{ont_id}/compensation-failures/{failure_id}/abandon",
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def abandon_ont_compensation_failure(
    request: Request,
    ont_id: str,
    failure_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Mark a compensation failure abandoned from the ONT history view."""
    from app.services.network.compensation_retry import mark_abandoned

    if not can_manage_ont_from_request(request, db, ont_id):
        return _redirect_to_request_target(
            request,
            f"/admin/network/onts/{ont_id}",
            message="ONT scope check failed",
            toast_type="error",
        )

    success, message = mark_abandoned(
        db,
        failure_id,
        resolved_by=actor_label(request),
    )
    return _redirect_to_request_target(
        request,
        f"/admin/network/onts/{ont_id}?tab=history",
        message=message,
        toast_type="success" if success else "error",
    )


@router.get(
    "/provisioning",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def provisioning_ledger(
    request: Request,
    facet: str = Query(default="orders"),
    db: Session = Depends(get_db),
):
    context = _base_context(request, db, active_page="provisioning")
    context.update(
        web_network_provisioning_ledger_service.provisioning_ledger_data(db, facet)
    )
    return templates.TemplateResponse("admin/network/provisioning/index.html", context)


@router.get(
    "/provisioning/triage",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("provisioning:read"))],
)
def provisioning_triage(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="provisioning")
    context.update(web_network_provisioning_triage_service.provisioning_triage_data(db))
    return templates.TemplateResponse("admin/network/provisioning/triage.html", context)
