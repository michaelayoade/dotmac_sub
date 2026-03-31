"""Admin web routes for ONT provisioning actions.

Each provisioning step is an independent action triggered by the operator
via a button on the ONT detail page. Routes delegate directly to the
service functions in ``app.services.network.ont_provision_steps``.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OntProvisioningProfile, OnuMode, WanMode
from app.schemas.network import OntUnitUpdate
from app.schemas.provisioning import ServiceOrderUpdate
from app.services import network as network_service
from app.services import provisioning as provisioning_service
from app.services import web_admin as web_admin_service
from app.services import web_network_olt_profiles as web_network_olt_profiles_service
from app.services import web_network_onts as web_network_onts_service
from app.services.auth_dependencies import require_permission
from app.services.common import coerce_uuid
from app.services.credential_crypto import encrypt_credential
from app.services.network import ont_provision_steps as steps
from app.services.network.ont_provision_steps import StepResult, validate_prerequisites

logger = logging.getLogger(__name__)
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


def _step_response(result: StepResult) -> JSONResponse:
    """Convert a StepResult into a JSONResponse with toast notification."""
    if result.waiting:
        toast_type = "info"
        phase = "waiting"
        status_code = 202
    else:
        toast_type = "success" if result.success else "error"
        phase = "succeeded" if result.success else "failed"
        status_code = 200 if result.success else 400
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


def _resolve_service_order_id_for_ont(db: Session, ont_id: str) -> str | None:
    return provisioning_service.resolve_service_order_id_for_ont(db, ont_id)


def _record_ont_step_action(
    db: Session,
    request: Request,
    ont_id: str,
    result: StepResult,
) -> None:
    """Record a provisioning step action against the active service order.

    Currently logs only; ServiceOrderAction model is pending migration.
    """
    logger.info(
        "ONT step %s for %s: success=%s waiting=%s — %s",
        result.step_name,
        ont_id,
        result.success,
        result.waiting,
        result.message,
    )


def _update_service_order_execution_context_for_ont(
    db: Session,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
) -> None:
    service_order_id = _resolve_service_order_id_for_ont(db, ont_id)
    if not service_order_id:
        return
    order = provisioning_service.service_orders.get(db, service_order_id)
    execution_context = dict(getattr(order, "execution_context", None) or {})
    ont_plan = dict(execution_context.get("ont_plan") or {})
    ont_plan[step_name] = {
        key: value for key, value in values.items() if value not in (None, "", [])
    }
    execution_context["ont_plan"] = ont_plan
    provisioning_service.service_orders.update(
        db,
        service_order_id,
        ServiceOrderUpdate(execution_context=execution_context),
    )


# ---------------------------------------------------------------------------
# Read-only routes (preflight, preview, save settings)
# ---------------------------------------------------------------------------


def _get_profile_with_services(
    db: Session, profile_id: str
) -> OntProvisioningProfile | None:
    """Fetch a provisioning profile with eagerly loaded WAN services."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.orm import selectinload

    pid = coerce_uuid(profile_id)
    if pid is None:
        return None
    stmt = (
        sa_select(OntProvisioningProfile)
        .options(selectinload(OntProvisioningProfile.wan_services))
        .where(OntProvisioningProfile.id == pid)
    )
    return db.scalars(stmt).first()


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
    profile = _get_profile_with_services(db, profile_id)
    if not profile:
        return HTMLResponse(
            '<p class="text-sm text-slate-500 dark:text-slate-400">Profile not found.</p>'
        )
    context = _base_context(request, db, active_page="onts")
    context["profile"] = profile
    context["wan_services"] = list(profile.wan_services)
    return templates.TemplateResponse(
        "admin/network/onts/_profile_preview.html", context
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    olt = getattr(ont, "olt_device", None)
    resolved_profile = web_network_onts_service.resolve_effective_provisioning_profile(
        db, ont, olt
    )
    resolved_tr069_profile, _resolved_tr069_profile_error = (
        web_network_onts_service.resolve_effective_tr069_profile_for_ont(db, ont)
    )
    profile_id = profile_id or (str(resolved_profile.id) if resolved_profile else "")
    tr069_profile_id = tr069_profile_id or getattr(
        resolved_tr069_profile, "profile_id", None
    )
    data = web_network_olt_profiles_service.command_preview_context(
        db, ont_id, profile_id, tr069_olt_profile_id=tr069_profile_id
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
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    olt = getattr(ont, "olt_device", None)
    resolved_profile = web_network_onts_service.resolve_effective_provisioning_profile(
        db, ont, olt
    )
    resolved_tr069_profile, _resolved_tr069_profile_error = (
        web_network_onts_service.resolve_effective_tr069_profile_for_ont(db, ont)
    )
    profile_id = profile_id or (str(resolved_profile.id) if resolved_profile else None)
    tr069_profile_id = tr069_profile_id or getattr(
        resolved_tr069_profile, "profile_id", None
    )
    result = validate_prerequisites(
        db,
        ont_id,
        profile_id=profile_id,
        tr069_olt_profile_id=tr069_profile_id,
    )
    return JSONResponse(result)


@router.post(
    "/onts/{ont_id}/save-provision-settings",
    dependencies=[Depends(require_permission("network:write"))],
)
def ont_save_provision_settings(
    request: Request,
    ont_id: str,
    onu_mode: str | None = Form(default=None),
    wan_protocol: str | None = Form(default=None),
    wan_vlan_id: str | None = Form(default=None),
    pppoe_username: str | None = Form(default=None),
    pppoe_password: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Persist provision-page WAN settings without starting provisioning."""
    try:
        network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except Exception:
        return JSONResponse(
            status_code=404,
            content={"success": False, "message": "ONT not found"},
        )

    onu_mode_value = (onu_mode or "").strip().lower() or None
    wan_protocol_value = (wan_protocol or "").strip().lower() or None
    pppoe_username_value = (pppoe_username or "").strip() or None
    pppoe_password_value = (pppoe_password or "").strip() or None
    wan_vlan_id_value = (wan_vlan_id or "").strip() or None

    if onu_mode_value not in {None, OnuMode.routing.value, OnuMode.bridging.value}:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid ONU mode"},
        )

    wan_mode_value: str | None = None
    if onu_mode_value == OnuMode.bridging.value:
        wan_mode_value = "bridge"
    elif wan_protocol_value == "pppoe":
        wan_mode_value = WanMode.pppoe.value
    elif wan_protocol_value == "dhcp":
        wan_mode_value = WanMode.dhcp.value
    elif wan_protocol_value == "static":
        wan_mode_value = WanMode.static_ip.value
    elif wan_protocol_value:
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid WAN protocol"},
        )

    payload = OntUnitUpdate(
        onu_mode=onu_mode_value,
        wan_mode=wan_mode_value,
        wan_vlan_id=coerce_uuid(wan_vlan_id_value),
        pppoe_username=pppoe_username_value if wan_protocol_value == "pppoe" else None,
        pppoe_password=encrypt_credential(pppoe_password_value)
        if wan_protocol_value == "pppoe" and pppoe_password_value
        else None,
    )
    network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
    return JSONResponse(
        content={"success": True, "message": "Provision settings saved"}
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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)


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
    return _step_response(result)
