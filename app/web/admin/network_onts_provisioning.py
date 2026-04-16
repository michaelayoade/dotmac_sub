"""Admin web routes for ONT provisioning actions.

Each provisioning step is an independent action triggered by the operator
via a button on the ONT detail page. Routes delegate directly to the
service functions in ``app.services.network.ont_provision_steps``.
"""

from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services import (
    web_network_onts_provisioning as web_onts_provisioning_service,
)
from app.services.auth_dependencies import require_permission
from app.services.network import ont_provision_steps as steps
from app.services.network.action_logging import log_network_action_result
from app.services.network.ont_provisioning.result import StepResult

templates = Jinja2Templates(directory="templates")
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
    "/onts/{ont_id}/profile-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_profile_preview(
    request: Request,
    ont_id: str,
    profile_id: str = Query(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: profile summary for the configure page."""
    preview_context = web_onts_provisioning_service.profile_preview_context(
        db,
        profile_id=profile_id,
    )
    if not preview_context:
        return HTMLResponse(
            '<p class="text-sm text-slate-500 dark:text-slate-400">Profile not found.</p>'
        )
    context = _base_context(request, db, active_page="onts")
    context.update(preview_context)
    return templates.TemplateResponse(
        "admin/network/onts/_profile_preview.html", context
    )


@router.get(
    "/onts/{ont_id}/available-static-ips",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:read"))],
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
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_provisioning_preview(
    request: Request,
    ont_id: str,
    profile_id: str | None = Query(default=None),
    tr069_profile_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """HTMX partial: Command preview for provisioning an ONT."""
    data = web_onts_provisioning_service.provisioning_preview_context(
        db,
        ont_id=ont_id,
        profile_id=profile_id,
        tr069_profile_id=tr069_profile_id,
    )
    context = _base_context(request, db, active_page="onts")
    context.update(data)
    return templates.TemplateResponse(
        "admin/network/onts/_provisioning_preview.html", context
    )


@router.get(
    "/onts/{ont_id}/preflight",
    dependencies=[Depends(require_permission("network:read"))],
)
def ont_preflight_check(
    request: Request,
    ont_id: str,
    profile_id: str | None = Query(default=None),
    tr069_profile_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Pre-flight validation for ONT provisioning. Returns JSON checklist."""
    result = web_onts_provisioning_service.preflight_result(
        db,
        ont_id=ont_id,
        profile_id=profile_id,
        tr069_profile_id=tr069_profile_id,
    )
    return JSONResponse(result)


@router.post(
    "/onts/{ont_id}/save-provision-settings",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_save_provision_settings(
    request: Request,
    ont_id: str,
    profile_id: str | None = Form(default=None),
    tr069_profile_id: str | None = Form(default=None),
    onu_mode: str | None = Form(default=None),
    mgmt_vlan_id: str | None = Form(default=None),
    mgmt_ip_mode: str | None = Form(default=None),
    mgmt_ip_address: str | None = Form(default=None),
    mgmt_subnet: str | None = Form(default=None),
    mgmt_gateway: str | None = Form(default=None),
    wan_protocol: str | None = Form(default=None),
    wan_vlan_id: str | None = Form(default=None),
    ip_pool_id: str | None = Form(default=None),
    static_ip_pool_id: str | None = Form(default=None),
    static_ip: str | None = Form(default=None),
    static_subnet: str | None = Form(default=None),
    static_gateway: str | None = Form(default=None),
    static_dns: str | None = Form(default=None),
    lan_ip: str | None = Form(default=None),
    lan_subnet: str | None = Form(default=None),
    dhcp_enabled: str | None = Form(default=None),
    dhcp_start: str | None = Form(default=None),
    dhcp_end: str | None = Form(default=None),
    wifi_enabled: str | None = Form(default=None),
    wifi_ssid: str | None = Form(default=None),
    wifi_password: str | None = Form(default=None),
    wifi_security_mode: str | None = Form(default=None),
    wifi_channel: str | None = Form(default=None),
    pppoe_username: str | None = Form(default=None),
    pppoe_password: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> Response:
    """Persist provision-page WAN settings without starting provisioning."""
    result = web_onts_provisioning_service.save_provision_settings(
        db,
        ont_id=ont_id,
        profile_id=profile_id,
        tr069_profile_id=tr069_profile_id,
        onu_mode=onu_mode,
        mgmt_vlan_id=mgmt_vlan_id,
        mgmt_ip_mode=mgmt_ip_mode,
        mgmt_ip_address=mgmt_ip_address,
        mgmt_subnet=mgmt_subnet,
        mgmt_gateway=mgmt_gateway,
        wan_protocol=wan_protocol,
        wan_vlan_id=wan_vlan_id,
        ip_pool_id=ip_pool_id,
        static_ip_pool_id=static_ip_pool_id,
        static_ip=static_ip,
        static_subnet=static_subnet,
        static_gateway=static_gateway,
        static_dns=static_dns,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
        wifi_enabled=wifi_enabled,
        wifi_ssid=wifi_ssid,
        wifi_password=wifi_password,
        wifi_security_mode=wifi_security_mode,
        wifi_channel=wifi_channel,
        pppoe_username=pppoe_username,
        pppoe_password=pppoe_password,
    )
    success = bool(result.content.get("success"))
    message = str(result.content.get("message") or "Provision settings failed")
    log_network_action_result(
        request=request,
        resource_type="ont",
        resource_id=ont_id,
        action="Save Provisioning Configuration",
        success=success,
        message=message,
        metadata={"status_code": result.status_code},
    )
    if request.headers.get("HX-Request") != "true":
        status = "success" if success else "error"
        from urllib.parse import quote_plus

        return RedirectResponse(
            f"/admin/network/onts/{ont_id}/provision?status={status}&message={quote_plus(message)}",
            status_code=303,
        )
    return JSONResponse(
        status_code=result.status_code,
        content=result.content,
    )


@router.get(
    "/onts/{ont_id}/save-provision-settings",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_save_provision_settings_get(ont_id: str) -> Response:
    """Handle accidental GET navigations to the save endpoint gracefully."""
    message = "Use the Save OLT Profile button to submit this form."
    return RedirectResponse(
        f"/admin/network/onts/{ont_id}/provision?status=error&message={quote_plus(message)}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Per-step provisioning actions (OLT SSH)
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/step/create-service-port",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_create_service_port(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    gem_index: int = Form(default=1),
    user_vlan: int | None = Form(default=None),
    tag_transform: str = Form(default="translate"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create a single OLT service-port VLAN/GEM mapping."""
    result = steps.create_service_port(
        db,
        ont_id,
        vlan_id=vlan_id,
        gem_index=gem_index,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "create_service_port",
        {
            "vlan_id": vlan_id,
            "gem_index": gem_index,
            "user_vlan": user_vlan,
            "tag_transform": tag_transform,
        },
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/configure-mgmt-ip",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_configure_mgmt_ip(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    ip_mode: str = Form(default="dhcp"),
    ip_address: str | None = Form(default=None),
    subnet: str | None = Form(default=None),
    gateway: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure management IP (IPHOST) via OLT SSH."""
    result = steps.configure_management_ip(
        db,
        ont_id,
        vlan_id=vlan_id,
        ip_mode=ip_mode,
        ip_address=ip_address,
        subnet=subnet,
        gateway=gateway,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "configure_management_ip",
        {
            "vlan_id": vlan_id,
            "ip_mode": ip_mode,
            "ip_address": ip_address,
            "subnet": subnet,
            "gateway": gateway,
        },
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/activate-internet-config",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_activate_internet_config(
    request: Request,
    ont_id: str,
    ip_index: int = Form(default=0),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Activate TCP stack on ONT management WAN."""
    result = steps.activate_internet_config(db, ont_id, ip_index=ip_index)
    _update_service_order_execution_context_for_ont(
        db, ont_id, "activate_internet_config", {"ip_index": ip_index}
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/configure-wan-olt",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_configure_wan_olt(
    request: Request,
    ont_id: str,
    ip_index: int = Form(default=0),
    profile_id: int = Form(default=0),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Set route+NAT mode on ONT management WAN via OLT SSH."""
    result = steps.configure_wan_olt(
        db,
        ont_id,
        ip_index=ip_index,
        profile_id=profile_id,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "configure_wan_olt",
        {"ip_index": ip_index, "profile_id": profile_id},
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/bind-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_bind_tr069(
    request: Request,
    ont_id: str,
    tr069_olt_profile_id: int = Form(...),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Bind a TR-069 server profile to the ONT via OLT SSH."""
    result = steps.bind_tr069(
        db,
        ont_id,
        tr069_olt_profile_id=tr069_olt_profile_id,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "bind_tr069",
        {"tr069_olt_profile_id": tr069_olt_profile_id},
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/wait-tr069-bootstrap",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_wait_tr069_bootstrap(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Poll GenieACS until the ONT registers after TR-069 binding."""
    result = steps.queue_wait_tr069_bootstrap(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


# ---------------------------------------------------------------------------
# Per-step provisioning actions (TR-069)
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/step/set-cr-credentials",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_set_cr_credentials(
    request: Request,
    ont_id: str,
    username: str = Form(default=""),
    password: str = Form(default=""),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Set TR-069 connection request credentials via ACS."""
    result = steps.set_connection_request_credentials(
        db,
        ont_id,
        username=username,
        password=password,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "set_connection_request_credentials",
        {"username": username} if username else {},
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/push-pppoe-omci",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_push_pppoe_omci(
    request: Request,
    ont_id: str,
    vlan_id: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    ip_index: int = Form(default=1),
    priority: int = Form(default=0),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Push PPPoE credentials via OMCI (OLT-side, pre-boot)."""
    result = steps.push_pppoe_omci(
        db,
        ont_id,
        vlan_id=vlan_id,
        username=username,
        password=password,
        ip_index=ip_index,
        priority=priority,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "push_pppoe_omci",
        {
            "vlan_id": vlan_id,
            "username": username,
            "ip_index": ip_index,
            "priority": priority,
        },
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/push-pppoe-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_push_pppoe_tr069(
    request: Request,
    ont_id: str,
    username: str = Form(...),
    password: str = Form(...),
    instance_index: int = Form(default=1),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Push PPPoE credentials via TR-069/ACS."""
    result = steps.push_pppoe_tr069(
        db,
        ont_id,
        username=username,
        password=password,
        instance_index=instance_index,
        retry=False,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "push_pppoe_tr069",
        {"username": username, "instance_index": instance_index},
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/configure-wan-tr069",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_configure_wan_tr069(
    request: Request,
    ont_id: str,
    wan_mode: str = Form(default="pppoe"),
    wan_vlan: int | None = Form(default=None),
    ip_address: str | None = Form(default=None),
    subnet_mask: str | None = Form(default=None),
    gateway: str | None = Form(default=None),
    dns_servers: str | None = Form(default=None),
    instance_index: int = Form(default=1),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Configure WAN connection mode via TR-069."""
    result = steps.configure_wan_tr069(
        db,
        ont_id,
        wan_mode=wan_mode,
        wan_vlan=wan_vlan,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
    )
    _update_service_order_execution_context_for_ont(
        db,
        ont_id,
        "configure_wan_tr069",
        {
            "wan_mode": wan_mode,
            "wan_vlan": wan_vlan,
            "ip_address": ip_address,
            "subnet_mask": subnet_mask,
            "gateway": gateway,
            "dns_servers": dns_servers,
            "instance_index": instance_index,
        },
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/enable-ipv6",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_enable_ipv6(
    request: Request,
    ont_id: str,
    wan_instance: int = Form(default=1),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Enable IPv6 dual-stack via TR-069."""
    result = steps.enable_ipv6(db, ont_id, wan_instance=wan_instance)
    _update_service_order_execution_context_for_ont(
        db, ont_id, "enable_ipv6", {"wan_instance": wan_instance}
    )
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


# ---------------------------------------------------------------------------
# Rollback / cleanup
# ---------------------------------------------------------------------------


@router.post(
    "/onts/{ont_id}/step/rollback-service-ports",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_rollback_service_ports(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Remove all service-ports for this ONT from the OLT."""
    result = steps.rollback_service_ports(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)


@router.post(
    "/onts/{ont_id}/step/deprovision",
    dependencies=[Depends(require_permission("network:write"))],
)
def step_deprovision(
    request: Request,
    ont_id: str,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Full deprovision: remove service-ports, deauthorize, clear DB state."""
    result = steps.deprovision(db, ont_id)
    _record_ont_step_action(db, request, ont_id, result)
    return _step_response(result, request=request, ont_id=ont_id)
